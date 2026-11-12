"""
SQL 安全校验：用 sqlglot 做 AST 白名单，而不是正则黑名单。

为什么不用正则（这是 ChatBI 生产代码里的一处真实缺陷）：
    re.compile(r'\\b(DELETE|UPDATE|...)\\b')  // DELETE/**/FROM 就能绕过
    而且它无法校验"这条 SQL 到底碰了哪几张表"

三层防线：
    1. 解析失败 -> 拒绝
    2. 不是纯 SELECT（含子查询里的写操作）-> 拒绝
    3. 引用了 nw 之外的 schema/表 -> 拒绝
    4. 没有 LIMIT -> 自动补上（不是拒绝，因为补上就安全了）

最后一层不在这里，而在数据库：执行 SQL 用的是只有 SELECT 权限的 nw_readonly 账号。
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

ALLOWED_SCHEMA = "nw"
DEFAULT_LIMIT = 100
FORBIDDEN_FUNCS = {
    "pg_sleep", "pg_read_file", "pg_ls_dir", "lo_import", "lo_export",
    "dblink", "set_config", "current_setting",
}


class UnsafeSQLError(ValueError):
    pass


@dataclass
class GuardResult:
    sql: str
    tables: list[str]


def validate(raw_sql: str, default_limit: int = DEFAULT_LIMIT) -> GuardResult:
    sql = raw_sql.strip().rstrip(";")
    if not sql:
        raise UnsafeSQLError("空 SQL")

    try:
        statements = sqlglot.parse(sql, read="postgres")
    except Exception as exc:
        raise UnsafeSQLError(f"SQL 无法解析：{exc}") from exc

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeSQLError("只允许单条语句")

    root = statements[0]
    if not isinstance(root, exp.Select):
        raise UnsafeSQLError(f"只允许 SELECT，检测到 {type(root).__name__.upper()}")

    # 子查询 / CTE 里也不能藏写操作
    for node in root.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
                             exp.Alter, exp.TruncateTable, exp.Command)):
            raise UnsafeSQLError("SQL 中包含写操作")

    # 函数黑名单
    for node in root.find_all(exp.Func):
        name = (node.sql_name() or "").lower()
        if name in FORBIDDEN_FUNCS:
            raise UnsafeSQLError(f"禁止调用函数 {name}")

    # 表白名单：必须都在 nw schema
    tables: list[str] = []
    for table in root.find_all(exp.Table):
        schema = (table.db or "").lower()
        name = (table.name or "").lower()
        if schema != ALLOWED_SCHEMA:
            raise UnsafeSQLError(f"禁止访问 {schema or 'public'}.{name}，只允许 {ALLOWED_SCHEMA}.*")
        tables.append(f"{schema}.{name}")

    if not tables:
        raise UnsafeSQLError("SQL 未引用任何业务表")

    # 强制 LIMIT
    if root.args.get("limit") is None:
        root = root.limit(default_limit)

    return GuardResult(sql=root.sql(dialect="postgres"), tables=sorted(set(tables)))
