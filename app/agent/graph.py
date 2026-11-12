"""图的组装。只有这一张图，读懂了整个 Agent 就读懂了。

    START → rewrite → route ─┬─ kb ────→ retrieve ─┬─→ merge → grade ─┬─ retry → rewrite
                             │                    │                  └─ pass  → generate → verify → END
                             ├─ both ──→ retrieve ─┘ (再转 text2sql)
                             ├─ sql ───→ text2sql ──→ merge
                             └─ direct ─────────────────────────────→ generate

三个值得讲的设计：
1. **rewrite 和 route 分开**：重写是一次 LLM 调用，路由是另一次，职责不同不能合
2. **grade 有环**：检索质量不达标能回到 rewrite 换说法重试 —— 这是 Chain 做不到的
3. **verify 兜底**：模型可能引用不存在的编号，生成后统一校验，越界直接剔除
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.agent import nodes
from app.agent.state import AgentState


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)

    g.add_node("rewrite", nodes.rewrite)
    g.add_node("route", nodes.route)
    g.add_node("retrieve", nodes.retrieve)
    g.add_node("text2sql", nodes.text2sql)
    g.add_node("merge", nodes.merge)
    g.add_node("grade", nodes.grade)
    g.add_node("generate", nodes.generate)
    g.add_node("verify", nodes.verify)

    g.add_edge(START, "rewrite")
    g.add_edge("rewrite", "route")

    g.add_conditional_edges(
        "route",
        nodes.route_selector,
        {"kb": "retrieve", "sql": "text2sql", "both": "retrieve", "direct": "generate"},
    )

    # both 路线：检索完规则，再去查数据
    g.add_conditional_edges(
        "retrieve", nodes.after_retrieve, {"text2sql": "text2sql", "merge": "merge"}
    )
    g.add_edge("text2sql", "merge")
    g.add_edge("merge", "grade")

    g.add_conditional_edges("grade", nodes.grade_selector, {"retry": "rewrite", "pass": "generate"})

    g.add_edge("generate", "verify")
    g.add_edge("verify", END)

    return g.compile(checkpointer=checkpointer)


def build_mermaid() -> str:
    """调 /api/graph 时返回，前端可以直接渲染，答辩时很好用。"""
    return build_graph().get_graph().draw_mermaid()
