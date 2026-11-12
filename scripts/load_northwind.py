"""
导入 Northwind 并把原始库"翻译"成干净的小写 schema。

做三件事：
1. 下载并导入官方 Northwind PG dump（进 public schema，保持原样不动）
2. 把所有日期整体平移，让"上月""近 3 个月"这类问法有数据
3. 建 nw.* 视图，字段统一成 snake_case，并补上 region_group / line_amount 两个派生列

为什么要视图而不是改原表：
- 原表带引号大小写（"CustomerID"），写 SQL 时到处要转义，Text2SQL 极容易错
- 视图可以随时重建，不动原始数据
- 语义层只需要面向 nw.* 一套口子

用法：
    python -m scripts.load_northwind
    python -m scripts.load_northwind --sql-file ./northwind.sql   # 离线用本地文件
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

NORTHWIND_URL = (
    "https://raw.githubusercontent.com/pthom/northwind_psql/master/northwind.sql"
)

# 数据整体平移的量：让最后一批订单落在 2026-10（即"上个月"附近）
DATE_SHIFT = "28 years 6 months"

# 逻辑字段 -> 可能的原始列名（统一小写后匹配）
TABLE_COLUMNS: dict[str, dict[str, str]] = {
    "customers": {
        "customer_id": "customerid",
        "company_name": "companyname",
        "contact_name": "contactname",
        "contact_title": "contacttitle",
        "city": "city",
        "region": "region",
        "country": "country",
        "phone": "phone",
    },
    "orders": {
        "order_id": "orderid",
        "customer_id": "customerid",
        "employee_id": "employeeid",
        "order_date": "orderdate",
        "required_date": "requireddate",
        "shipped_date": "shippeddate",
        "ship_via": "shipvia",
        "freight": "freight",
        "ship_name": "shipname",
        "ship_city": "shipcity",
        "ship_country": "shipcountry",
    },
    "order_details": {
        "order_id": "orderid",
        "product_id": "productid",
        "unit_price": "unitprice",
        "quantity": "quantity",
        "discount": "discount",
    },
    "products": {
        "product_id": "productid",
        "product_name": "productname",
        "supplier_id": "supplierid",
        "category_id": "categoryid",
        "quantity_per_unit": "quantityperunit",
        "unit_price": "unitprice",
        "units_in_stock": "unitsinstock",
        "units_on_order": "unitsonorder",
        "reorder_level": "reorderlevel",
        "discontinued": "discontinued",
    },
    "categories": {
        "category_id": "categoryid",
        "category_name": "categoryname",
        "description": "description",
    },
    "employees": {
        "employee_id": "employeeid",
        "last_name": "lastname",
        "first_name": "firstname",
        "title": "title",
        "hire_date": "hiredate",
        "city": "city",
        "country": "country",
    },
    "shippers": {
        "shipper_id": "shipperid",
        "company_name": "companyname",
        "phone": "phone",
    },
}

# 业务区域划分，与《数据字典》第六条一致
REGION_CASE = """CASE
        WHEN country IN ('UK','Germany','France','Sweden','Spain','Italy','Austria',
                         'Belgium','Denmark','Finland','Ireland','Norway','Poland',
                         'Portugal','Switzerland') THEN '华东区'
        WHEN country IN ('USA','Canada','Mexico') THEN '北美区'
        WHEN country IN ('Brazil','Argentina','Venezuela') THEN '南美区'
        WHEN country IN ('Japan','Singapore') THEN '亚太区'
        ELSE '其他'
    END"""

EXTRA_EXPR: dict[str, dict[str, str]] = {
    "customers": {"region_group": REGION_CASE},
}


async def table_exists(conn: asyncpg.Connection, name: str) -> bool:
    return await conn.fetchval(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND lower(table_name)=$1",
        name,
    ) is not None


async def resolve(conn: asyncpg.Connection, logical_table: str) -> tuple[str, dict[str, str]]:
    """返回 (真实表名, {逻辑列: 真实列})。找不到的列直接跳过。"""
    real_table = await conn.fetchval(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND lower(table_name)=$1",
        logical_table,
    )
    if not real_table:
        raise RuntimeError(f"Northwind 缺少表 {logical_table}，导入可能失败了")

    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=$1",
        real_table,
    )
    actual = {r["column_name"].lower(): r["column_name"] for r in rows}

    mapping: dict[str, str] = {}
    for logical_col, raw in TABLE_COLUMNS[logical_table].items():
        if raw in actual:
            mapping[logical_col] = actual[raw]
    return real_table, mapping


async def download_sql(sql_file: Path | None) -> str:
    if sql_file and sql_file.exists():
        return sql_file.read_text(encoding="utf-8", errors="ignore")
    print(f"下载 Northwind dump: {NORTHWIND_URL}")
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(NORTHWIND_URL)
        resp.raise_for_status()
        text = resp.text
    sql_file = Path(__file__).parent / "northwind.sql"
    sql_file.write_text(text, encoding="utf-8")
    print(f"已保存到 {sql_file}（下次可离线复用）")
    return text


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sql-file", type=Path, default=None)
    parser.add_argument("--force", action="store_true", help="已有数据也重新导入")
    args = parser.parse_args()

    conn = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    if await table_exists(conn, "orders") and not args.force:
        print("检测到 orders 表已存在，跳过导入（加 --force 强制重来）")
    else:
        sql = await download_sql(args.sql_file)
        print("执行 Northwind dump ...")
        try:
            await conn.execute(sql)
        except Exception as exc:  # dump 里可能有可忽略的报错
            print(f"导入过程中出现提示（通常可忽略）：{exc}")
        print("导入完成")

    # ---- 1. 日期平移 ----
    real_orders, cols = await resolve(conn, "orders")
    date_cols = [
        cols["order_date"],
        cols["required_date"],
        cols["shipped_date"],
    ]
    set_clause = ", ".join(f'"{c}" = "{c}" + INTERVAL \'{DATE_SHIFT}\'' for c in date_cols)
    span = await conn.fetchrow(
        f'SELECT min("{cols["order_date"]}") AS lo, max("{cols["order_date"]}") AS hi '
        f'FROM public."{real_orders}"'
    )
    print(f"平移前订单区间：{span['lo']} ~ {span['hi']}")

    if span["hi"] and span["hi"].year < 2024:
        await conn.execute(f'UPDATE public."{real_orders}" SET {set_clause}')
        print(f"已整体平移 {DATE_SHIFT}")
    else:
        print("日期已经在 2024 年之后，跳过平移")

    span = await conn.fetchrow(
        f'SELECT min("{cols["order_date"]}") AS lo, max("{cols["order_date"]}") AS hi '
        f'FROM public."{real_orders}"'
    )
    print(f"平移后订单区间：{span['lo']} ~ {span['hi']}")

    # ---- 2. 建 nw 视图 ----
    await conn.execute("CREATE SCHEMA IF NOT EXISTS nw")

    for logical_table in TABLE_COLUMNS:
        real_table, mapping = await resolve(conn, logical_table)
        select_parts = [f'"{real}" AS {logical}' for logical, real in mapping.items()]
        for extra_name, expr in EXTRA_EXPR.get(logical_table, {}).items():
            select_parts.append(f"{expr} AS {extra_name}")
        ddl = (
            f'CREATE OR REPLACE VIEW nw.{logical_table} AS '
            f'SELECT {", ".join(select_parts)} FROM public."{real_table}"'
        )
        await conn.execute(ddl)
        print(f"  nw.{logical_table}  ({len(mapping)} 列)")

    # 明细金额做个派生列，避免每个指标都重复写乘法
    await conn.execute(
        """
        CREATE OR REPLACE VIEW nw.order_items AS
        SELECT order_id, product_id, unit_price, quantity, discount,
               unit_price * quantity * (1 - discount) AS line_amount
        FROM nw.order_details
        """
    )

    # ---- 3. 授权给只读账号 ----
    await conn.execute("GRANT USAGE ON SCHEMA nw TO nw_readonly")
    await conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA nw TO nw_readonly")
    await conn.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA nw GRANT SELECT ON TABLES TO nw_readonly"
    )

    stats = await conn.fetchrow(
        """
        SELECT (SELECT count(*) FROM nw.orders)       AS orders,
               (SELECT count(*) FROM nw.order_items)  AS items,
               (SELECT count(*) FROM nw.products)     AS products,
               (SELECT count(*) FROM nw.customers)    AS customers
        """
    )
    print(
        f"完成：订单 {stats['orders']} / 明细 {stats['items']} / "
        f"商品 {stats['products']} / 客户 {stats['customers']}"
    )
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
