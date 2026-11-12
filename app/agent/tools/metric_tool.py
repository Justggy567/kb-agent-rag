"""Text2SQL 工具：指标匹配 -> 槽位抽取 -> 编译 SQL -> 只读执行。

这是本项目准确率的关键。整条链路里 LLM 只输出一个 JSON：
    {"metric_code": "gmv", "dims": ["月份"], "start_date": "...", "end_date": "..."}
它无法写出 DELETE，也无法 join 到没授权的表，因为它根本没机会写 SQL。
"""
from __future__ import annotations

import calendar
import json
import re
import time
from datetime import date, datetime, timedelta

from app.agent.tools import sql_guard
from app.db import pool, ro_pool
from app.llm import chat_json
from app.semantic.metrics import (
    DOMAINS,
    METRIC_BY_CODE,
    METRICS,
    Metric,
    catalog_text,
)

SLOT_SYSTEM = """你是企业数据查询的"语义解析器"，只输出 JSON，不写 SQL。

任务：把用户问题解析成指标 + 维度 + 时间范围。

规则：
1. metric_code 必须从给定清单里选一个；清单里没有能回答该问题的指标时，输出 {"metric_code": ""}
2. dims 只能选该指标允许的维度；用户问"趋势/变化"时选 月份，问"排名/最多/最高"时选对应维度
3. 时间一律输出绝对日期（YYYY-MM-DD），不要输出"上个月"这种相对说法
4. 用户没提时间就留空字符串，不要臆造
5. filters 只能放该指标允许的维度，值用中文，例如 {"区域": "华东区"}
6. 只输出 JSON，不要解释"""

SLOT_TEMPLATE = """今天是 {today}。

【可用指标清单】
{catalog}

【用户问题】
{question}

{time_hint}

输出格式：
{{"metric_code": "...", "dims": ["..."], "start_date": "YYYY-MM-DD 或 空",
  "end_date": "YYYY-MM-DD 或 空", "filters": {{}}, "limit": 10}}"""


# ---------------------------------------------------------------
# 时间解析：先在 Python 里算好，减轻模型负担
# ---------------------------------------------------------------
def _month_range(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def resolve_time_hint(question: str, today: date | None = None) -> tuple[date, date] | None:
    today = today or date.today()

    if m := re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", question):
        return _month_range(int(m.group(1)), int(m.group(2)))
    if m := re.search(r"(?:近|最近|过去)\s*(\d+)\s*个?月", question):
        n = max(1, int(m.group(1)))
        end = today
        start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        for _ in range(n - 1):
            start = (start - timedelta(days=1)).replace(day=1)
        return start, end
    if m := re.search(r"(?:近|最近|过去)\s*(\d+)\s*天", question):
        return today - timedelta(days=int(m.group(1)) - 1), today
    if "上个月" in question or "上月" in question:
        first_this = today.replace(day=1)
        return _month_range(*(first_this - timedelta(days=1)).timetuple()[:2])
    if "本月" in question or "这个月" in question:
        return _month_range(today.year, today.month)
    if "上季度" in question:
        q = (today.month - 1) // 3
        end_month = q * 3
        start_month = end_month - 2
        year = today.year
        if q == 0:
            year, start_month, end_month = year - 1, 10, 12
        return _month_range(year, start_month)[0], _month_range(year, end_month)[1]
    if "去年" in question:
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if m := re.search(r"(\d{4})\s*年", question):
        y = int(m.group(1))
        return date(y, 1, 1), date(y, 12, 31)
    if "今年" in question or "本年" in question:
        return date(today.year, 1, 1), today
    return None


# ---------------------------------------------------------------
# 指标加载：数据库为准，代码里的 METRICS 只作兜底
# 这样加指标只要往 metrics 表插一行，不用改代码、不用发版
# ---------------------------------------------------------------
_cache: dict = {"at": 0.0, "list": [], "by_code": {}}


def _row_to_metric(row) -> Metric:
    def _json(value) -> list:
        if value is None:
            return []
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return []
        return value

    return Metric(
        code=row["code"],
        name=row["name"],
        aliases=[a for a in _json(row["aliases"]) if a != row["name"]],
        domain=row["domain"],
        expr=row["expr"],
        unit=row["unit"] or "",
        chart_hint=row["chart_hint"] or "bar",
        description=row["description"] or "",
        allowed_dims=_json(row["allowed_dims"]),
    )


async def load_metrics() -> tuple[list[Metric], dict[str, Metric]]:
    now = time.time()
    if _cache["list"] and now - _cache["at"] < 60:
        return _cache["list"], _cache["by_code"]

    try:
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """SELECT code, name, aliases, domain, expr, unit, chart_hint,
                          allowed_dims, description
                   FROM metrics ORDER BY code"""
            )
        items = [_row_to_metric(r) for r in rows]
        if items:
            _cache.update(at=now, list=items, by_code={m.code: m for m in items})
            return items, _cache["by_code"]
    except Exception:
        pass

    _cache.update(at=now, list=METRICS, by_code=METRIC_BY_CODE)
    return METRICS, METRIC_BY_CODE


# ---------------------------------------------------------------
# 指标匹配：先别名精确命中，命中不了再问模型
# ---------------------------------------------------------------
def match_metric_by_alias(question: str, items: list[Metric] | None = None) -> Metric | None:
    best: tuple[int, Metric] | None = None
    for metric in items or METRICS:
        for alias in [metric.name, *metric.aliases]:
            if alias and alias in question:
                score = len(alias)
                if best is None or score > best[0]:
                    best = (score, metric)
    return best[1] if best else None


def compile_sql(
    metric: Metric,
    dims: list[str],
    start: date | None,
    end: date | None,
    filters: dict[str, str] | None = None,
    limit: int = 10,
) -> str:
    domain = DOMAINS[metric.domain]
    dim_map = metric.dims()

    dims = [d for d in dims if d in dim_map][:3]
    select_parts = [f'{dim_map[d]} AS "{d}"' for d in dims]
    select_parts.append(f'{metric.expr} AS "指标值"')

    where = []
    time_col = domain["time_col"]
    if time_col and start and end:
        where.append(f"{time_col} >= DATE '{start.isoformat()}'")
        where.append(f"{time_col} < DATE '{end.isoformat()}' + INTERVAL '1 day'")

    for key, value in (filters or {}).items():
        if key in dim_map and value:
            safe = str(value).replace("'", "''")
            where.append(f"{dim_map[key]} = '{safe}'")

    sql = f"SELECT {', '.join(select_parts)}\n{domain['from_sql'].strip()}"
    if where:
        sql += "\nWHERE " + "\n  AND ".join(where)
    if dims:
        sql += f"\nGROUP BY {', '.join(str(i + 1) for i in range(len(dims)))}"
        # 时间维度按正序，其余按值降序（"最高/最多"是常见问法）
        if dims[0] in ("月份", "季度"):
            sql += "\nORDER BY 1 ASC"
        else:
            sql += '\nORDER BY "指标值" DESC NULLS LAST'
    sql += f"\nLIMIT {max(1, min(limit, 50))}"
    return sql


# ---------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------
async def run(question: str) -> dict:
    """返回 {ok, sql, rows, columns, chart, metric, plan, error, elapsed_ms}。"""
    t0 = time.perf_counter()
    today = date.today()
    items, by_code = await load_metrics()

    hint = resolve_time_hint(question, today)
    hint_text = (
        f"【时间提示】已解析出区间：{hint[0]} 至 {hint[1]}，请直接使用。"
        if hint
        else "【时间提示】问题中未明确区间，可将 start_date/end_date 留空。"
    )

    plan, _usage = await chat_json(
        SLOT_TEMPLATE.format(
            today=today.isoformat(),
            catalog=catalog_text(items),
            question=question,
            time_hint=hint_text,
        ),
        system=SLOT_SYSTEM,
    )

    code = (plan.get("metric_code") or "").strip()
    if not code:
        alias_hit = match_metric_by_alias(question, items)
        code = alias_hit.code if alias_hit else ""

    if not code or code not in by_code:
        return {
            "ok": False,
            "error": "没有找到能回答该问题的指标。当前指标体系覆盖销售额、订单量、折扣、库存等经营指标。",
            "plan": plan,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }

    metric = by_code[code]
    dims = plan.get("dims") or []
    filters = plan.get("filters") or {}
    limit = int(plan.get("limit") or 10)

    start = end = None
    try:
        if plan.get("start_date"):
            start = datetime.strptime(plan["start_date"], "%Y-%m-%d").date()
        if plan.get("end_date"):
            end = datetime.strptime(plan["end_date"], "%Y-%m-%d").date()
    except ValueError:
        start = end = None
    if hint and not (start and end):
        start, end = hint

    sql = compile_sql(metric, dims, start, end, filters, limit)
    guard = sql_guard.validate(sql)

    async with ro_pool().acquire() as conn:
        # SET LOCAL 只在事务内生效，必须放进 transaction
        async with conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '8s'")
            records = await conn.fetch(guard.sql)

    rows = [
        {k: (str(v) if v is not None else None) for k, v in dict(r).items()}
        for r in records
    ]
    columns = list(rows[0].keys()) if rows else []

    chart = metric.chart_hint
    if dims and dims[0] in ("月份", "季度"):
        chart = "line"
    elif dims:
        chart = "bar"

    return {
        "ok": True,
        "sql": guard.sql,
        "tables": guard.tables,
        "rows": rows,
        "columns": columns,
        "chart": chart,
        "metric": {"code": metric.code, "name": metric.name, "unit": metric.unit},
        "dims": [d for d in dims if d in metric.dims()],
        "range": [start.isoformat(), end.isoformat()] if start and end else None,
        "plan": plan,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


def summarize_rows(result: dict, max_rows: int = 10) -> str:
    """把结果转成给模型看的紧凑文本，比 JSON 省一半 token。"""
    if not result.get("ok"):
        return result.get("error", "查询失败")
    rows = result.get("rows") or []
    if not rows:
        return "查询执行成功，但该条件下没有数据。"

    unit = (result.get("metric") or {}).get("unit") or ""
    cols = result.get("columns") or []
    lines = [" | ".join(cols)]
    for r in rows[:max_rows]:
        cells = []
        for c in cols:
            v = r.get(c)
            if c == "指标值" and v is not None:
                try:
                    cells.append(f"{float(v):,.2f}{unit}")
                except (TypeError, ValueError):
                    cells.append(str(v))
            else:
                cells.append("" if v is None else str(v))
        lines.append(" | ".join(cells))
    if len(rows) > max_rows:
        lines.append(f"...共 {len(rows)} 行")
    return "\n".join(lines)
