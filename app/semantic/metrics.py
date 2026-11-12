"""
指标语义层 —— 整个 Text2SQL 准确率的来源。

核心思想（抄自 ChatBI 生产系统）：
    LLM 不写 SQL，只做两件事：① 选哪个指标  ② 填哪几个参数。
    SQL 骨架是预定义的，幻觉空间从"整条 SQL"压缩到"选错指标"。

所以这里定义三样东西：
    DOMAINS  每个分析域一条 FROM 子句（连表关系写死，模型碰不到）
    DIMS     维度中文名 -> SQL 表达式
    METRICS  指标中文名/别名 -> 聚合表达式 + 允许的维度
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------
# 分析域
# ---------------------------------------------------------------
DOMAINS: dict[str, dict[str, str]] = {
    "order": {
        "label": "订单明细域",
        "from_sql": """
            FROM nw.orders o
            JOIN nw.order_items i ON i.order_id = o.order_id
            JOIN nw.products p     ON p.product_id = i.product_id
            JOIN nw.categories c   ON c.category_id = p.category_id
            JOIN nw.customers cu   ON cu.customer_id = o.customer_id
            LEFT JOIN nw.employees e ON e.employee_id = o.employee_id
            LEFT JOIN nw.shippers s  ON s.shipper_id = o.ship_via
        """,
        "time_col": "o.order_date",
    },
    "order_head": {
        "label": "订单主表域",
        "from_sql": """
            FROM nw.orders o
            JOIN nw.customers cu ON cu.customer_id = o.customer_id
            LEFT JOIN nw.employees e ON e.employee_id = o.employee_id
            LEFT JOIN nw.shippers s  ON s.shipper_id = o.ship_via
        """,
        "time_col": "o.order_date",
    },
    "product": {
        "label": "商品域",
        "from_sql": """
            FROM nw.products p
            JOIN nw.categories c ON c.category_id = p.category_id
        """,
        "time_col": None,
    },
}

# ---------------------------------------------------------------
# 维度
# ---------------------------------------------------------------
_COMMON_DIMS = {
    "月份": "to_char(o.order_date, 'YYYY-MM')",
    "季度": "to_char(o.order_date, 'YYYY-\"Q\"Q')",
    "客户": "cu.company_name",
    "国家": "cu.country",
    "城市": "cu.city",
    "区域": "cu.region_group",
    "员工": "coalesce(e.last_name, '未分配')",
    "承运商": "coalesce(s.company_name, '未指定')",
}

_COMMON_ORDER_DIMS = {
    "品类": "c.category_name",
    "商品": "p.product_name",
}

DIMS: dict[str, dict[str, str]] = {
    "order": {**_COMMON_DIMS, **_COMMON_ORDER_DIMS},
    "order_head": dict(_COMMON_DIMS),
    "product": {
        "品类": "c.category_name",
        "商品": "p.product_name",
        "库存状态": (
            "CASE WHEN p.units_in_stock = 0 THEN '缺货' "
            "WHEN p.units_in_stock < p.reorder_level THEN '低于再订货点' "
            "ELSE '正常' END"
        ),
    },
}


@dataclass(frozen=True)
class Metric:
    code: str
    name: str
    aliases: list[str]
    domain: str
    expr: str
    unit: str = ""
    chart_hint: str = "bar"
    description: str = ""
    allowed_dims: list[str] = field(default_factory=list)

    def dims(self) -> dict[str, str]:
        return {d: DIMS[self.domain][d] for d in self.allowed_dims if d in DIMS[self.domain]}


METRICS: list[Metric] = [
    Metric(
        code="gmv",
        name="销售额",
        aliases=["销售额", "营收", "收入", "成交额", "卖了多少钱", "交易额", "销售金额"],
        domain="order",
        expr="SUM(i.line_amount)",
        unit="元",
        chart_hint="bar",
        description="订单明细金额之和，已扣除折扣。口径见《数据字典》第二十一条。",
        allowed_dims=["月份", "季度", "品类", "商品", "客户", "国家", "城市", "区域", "员工"],
    ),
    Metric(
        code="order_cnt",
        name="订单数",
        aliases=["订单数", "订单量", "多少单", "单量", "订单笔数"],
        domain="order",
        expr="COUNT(DISTINCT o.order_id)",
        unit="单",
        chart_hint="card",
        description="去重后的订单编号数量。",
        allowed_dims=["月份", "季度", "品类", "客户", "国家", "城市", "区域", "员工"],
    ),
    Metric(
        code="customer_cnt",
        name="下单客户数",
        aliases=["客户数", "下单客户数", "多少客户", "活跃客户"],
        domain="order",
        expr="COUNT(DISTINCT o.customer_id)",
        unit="家",
        chart_hint="card",
        description="区间内产生过订单的客户去重数。",
        allowed_dims=["月份", "季度", "国家", "城市", "区域"],
    ),
    Metric(
        code="aov",
        name="客单价",
        aliases=["客单价", "平均订单金额", "单均金额"],
        domain="order",
        expr="SUM(i.line_amount) / NULLIF(COUNT(DISTINCT o.order_id), 0)",
        unit="元",
        chart_hint="line",
        description="销售额 ÷ 订单数。",
        allowed_dims=["月份", "季度", "品类", "区域", "国家"],
    ),
    Metric(
        code="qty",
        name="销量",
        aliases=["销量", "卖了多少件", "销售数量", "件数"],
        domain="order",
        expr="SUM(i.quantity)",
        unit="件",
        chart_hint="bar",
        description="订单明细的购买数量合计。",
        allowed_dims=["月份", "品类", "商品", "区域", "国家"],
    ),
    Metric(
        code="discount_rate",
        name="平均折扣率",
        aliases=["折扣率", "平均折扣", "折扣水平", "打了多少折"],
        domain="order",
        expr="SUM(i.unit_price * i.quantity * i.discount) / NULLIF(SUM(i.unit_price * i.quantity), 0)",
        unit="",
        chart_hint="line",
        description="按金额加权的平均折扣，取值 0~1。口径见《促销折扣审批规范》。",
        allowed_dims=["月份", "品类", "客户", "区域", "国家"],
    ),
    Metric(
        code="freight",
        name="运费合计",
        aliases=["运费", "物流费用", "运输成本"],
        domain="order_head",
        expr="SUM(o.freight)",
        unit="元",
        chart_hint="bar",
        description="订单头表的 Freight 字段合计（每单只算一次，需用 order_head 域避免明细放大）。",
        allowed_dims=["月份", "承运商", "国家", "区域"],
    ),
    Metric(
        code="avg_ship_days",
        name="平均发货时长",
        aliases=["发货时长", "平均发货天数", "履约时效", "多久发货"],
        domain="order_head",
        expr="AVG(EXTRACT(EPOCH FROM (o.shipped_date - o.order_date)) / 86400.0)",
        unit="天",
        chart_hint="line",
        description="下单到发货的自然天数，只统计已发货订单。",
        allowed_dims=["月份", "承运商", "区域", "国家"],
    ),
    Metric(
        code="late_order_cnt",
        name="逾期订单数",
        aliases=["逾期订单", "超期订单", "延迟发货订单"],
        domain="order_head",
        expr="COUNT(*) FILTER (WHERE o.shipped_date > o.required_date)",
        unit="单",
        chart_hint="card",
        description="实际发货日期晚于要求日期的订单数。",
        allowed_dims=["月份", "承运商", "区域", "国家", "客户"],
    ),
    Metric(
        code="product_cnt",
        name="商品数",
        aliases=["商品数", "在售商品数", "SKU 数", "多少个商品"],
        domain="product",
        expr="COUNT(*)",
        unit="个",
        chart_hint="card",
        description="商品表中的商品条目数（含已停产）。",
        allowed_dims=["品类", "库存状态"],
    ),
    Metric(
        code="stock_qty",
        name="库存量",
        aliases=["库存", "库存量", "现有库存", "存货"],
        domain="product",
        expr="SUM(p.units_in_stock)",
        unit="件",
        chart_hint="bar",
        description="UnitsInStock 合计。口径见《库存预警管理办法》。",
        allowed_dims=["品类", "商品", "库存状态"],
    ),
    Metric(
        code="below_reorder",
        name="低于再订货点商品数",
        aliases=["低于再订货点", "库存不足商品数", "需要补货的商品"],
        domain="product",
        expr="COUNT(*) FILTER (WHERE p.units_in_stock < p.reorder_level)",
        unit="个",
        chart_hint="card",
        description="库存量小于再订货点的商品数量。",
        allowed_dims=["品类", "商品"],
    ),
]

METRIC_BY_CODE: dict[str, Metric] = {m.code: m for m in METRICS}
METRIC_BY_NAME: dict[str, Metric] = {m.name: m for m in METRICS}


def all_aliases() -> dict[str, str]:
    """别名 -> 指标 code，用于关键词快速匹配（不花 token）。"""
    mapping: dict[str, str] = {}
    for m in METRICS:
        for a in [m.name, *m.aliases]:
            mapping[a.lower()] = m.code
    return mapping


def catalog_text(items: list[Metric] | None = None) -> str:
    """塞给 LLM 的指标清单。列清楚域与可用维度，模型才不会乱填。"""
    lines = []
    for m in items or METRICS:
        lines.append(
            f"- {m.code} | {m.name} | 域={m.domain} | 维度={','.join(m.allowed_dims)} | {m.description}"
        )
    return "\n".join(lines)
