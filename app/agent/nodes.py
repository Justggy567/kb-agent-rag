"""图里的每个节点。每个节点只做一件事，返回要合并进 State 的增量。"""
from __future__ import annotations

import time

from app.agent import prompts
from app.agent.state import AgentState
from app.agent.tools import metric_tool
from app.config import settings
from app.llm import chat
from app.rag.retriever import retrieve as rag_retrieve


def _emit(config: dict):
    fn = (config.get("configurable") or {}).get("emit")
    return fn or (lambda _e: None)


def _add_usage(state: AgentState, usage: dict) -> dict:
    cur = dict(state.get("usage") or {"token_in": 0, "token_out": 0})
    cur["token_in"] = cur.get("token_in", 0) + usage.get("token_in", 0)
    cur["token_out"] = cur.get("token_out", 0) + usage.get("token_out", 0)
    return cur


# ---------------------------------------------------------------
async def rewrite(state: AgentState, config: dict) -> dict:
    """指代消解。重试时在上一次的问题上追加检索提示词。"""
    t0 = time.perf_counter()
    emit = _emit(config)
    query = state["query"]
    history = state.get("history") or ""
    retry = state.get("retry", 0)

    rewritten = query
    usage = {"token_in": 0, "token_out": 0}

    if history.strip():
        try:
            text, usage = await chat(
                prompts.REWRITE_TEMPLATE.format(history=history, question=query),
                system=prompts.REWRITE_SYSTEM,
                temperature=0,
            )
            rewritten = text.strip().strip('"') or query
        except Exception:
            rewritten = query

    if retry:
        # 第二轮换一种说法再检索：加上制度类关键词，把"销售额怎么算"扩成
        # "销售额 计算口径 标准"，命中率明显提升
        rewritten = f"{rewritten} 依据 标准 口径 规定"

    emit({"type": "step", "name": "rewrite", "ms": int((time.perf_counter() - t0) * 1000)})
    return {
        "rewritten": rewritten,
        "usage": _add_usage(state, usage),
        "timings": {**(state.get("timings") or {}), "rewrite": int((time.perf_counter() - t0) * 1000)},
    }


# ---------------------------------------------------------------
async def route(state: AgentState, config: dict) -> dict:
    t0 = time.perf_counter()
    emit = _emit(config)
    label = "kb"
    usage = {"token_in": 0, "token_out": 0}

    try:
        text, usage = await chat(
            prompts.ROUTE_TEMPLATE.format(
                history=state.get("history") or "（无历史）",
                question=state.get("rewritten") or state["query"],
            ),
            system=prompts.ROUTE_SYSTEM,
            temperature=0,
            max_tokens=8,
        )
        label = text.strip().lower()
    except Exception:
        label = "kb"

    if label not in {"kb", "sql", "both", "direct"}:
        label = "kb"

    emit({"type": "route", "route": label})
    return {
        "route": label,
        "usage": _add_usage(state, usage),
        "timings": {**(state.get("timings") or {}), "route": int((time.perf_counter() - t0) * 1000)},
    }


def route_selector(state: AgentState) -> str:
    return state.get("route", "kb")


# ---------------------------------------------------------------
async def retrieve(state: AgentState, config: dict) -> dict:
    t0 = time.perf_counter()
    emit = _emit(config)
    question = state.get("rewritten") or state["query"]

    try:
        items = await rag_retrieve(question, top_k=settings.retrieve_top_k)
    except Exception as exc:
        items = []
        emit({"type": "warn", "message": f"知识库检索失败：{exc}"})

    citations = [
        {
            "chunk_id": c["id"],
            "doc_id": c["doc_id"],
            "doc_name": c.get("doc_name"),
            "page_no": c.get("page_no"),
            "section_path": c.get("section_path"),
            "snippet": c.get("snippet") or c["content"][:160],
            "score": round(float(c.get("score", 0)), 4),
        }
        for c in items
    ]
    emit({"type": "sources", "items": [{k: v for k, v in c.items() if k != "content"} for c in citations]})
    return {
        "retrieved": items,
        "citations": citations,
        "timings": {**(state.get("timings") or {}), "retrieve": int((time.perf_counter() - t0) * 1000)},
    }


# ---------------------------------------------------------------
async def text2sql(state: AgentState, config: dict) -> dict:
    t0 = time.perf_counter()
    emit = _emit(config)
    question = state.get("rewritten") or state["query"]

    try:
        result = await metric_tool.run(question)
    except Exception as exc:
        result = {"ok": False, "error": f"查询执行失败：{exc}"}

    if result.get("ok"):
        emit({"type": "sql", "sql": result["sql"], "metric": result["metric"]})
        emit(
            {
                "type": "data",
                "chart": result["chart"],
                "columns": result["columns"],
                "rows": result["rows"][:50],
                "metric": result["metric"],
                "range": result.get("range"),
            }
        )
    else:
        emit({"type": "sql_error", "message": result.get("error", "查询失败")})

    return {
        "sql_result": result,
        "timings": {**(state.get("timings") or {}), "text2sql": int((time.perf_counter() - t0) * 1000)},
    }


def after_retrieve(state: AgentState) -> str:
    """both 路线：先检索规则，再去查数据。"""
    if state.get("route") == "both" and not state.get("sql_result"):
        return "text2sql"
    return "merge"


# ---------------------------------------------------------------
async def merge(state: AgentState, config: dict) -> dict:
    return {}


# ---------------------------------------------------------------
async def grade(state: AgentState, config: dict) -> dict:
    """检索质量判定：分数太低就重写重试，最多 2 次。

    这是 LangGraph 相对普通 Chain 的核心价值——有环、有状态、能自反思。
    """
    emit = _emit(config)
    route_name = state.get("route", "kb")
    retry = state.get("retry", 0)

    if route_name in ("sql", "direct"):
        return {"grade_action": "pass"}

    retrieved = state.get("retrieved") or []
    top_score = max((float(c.get("score", 0)) for c in retrieved), default=0.0)

    if top_score < settings.grade_threshold and retry < settings.max_retry:
        emit({"type": "step", "name": "grade", "result": "retry", "score": round(top_score, 3)})
        return {"retry": retry + 1, "retrieved": [], "grade_action": "retry"}

    emit({"type": "step", "name": "grade", "result": "pass", "score": round(top_score, 3)})
    return {"grade_action": "pass"}


def grade_selector(state: AgentState) -> str:
    return "retry" if state.get("grade_action") == "retry" else "pass"


# ---------------------------------------------------------------
async def generate(state: AgentState, config: dict) -> dict:
    from app.llm import stream as llm_stream

    t0 = time.perf_counter()
    emit = _emit(config)
    route_name = state.get("route", "kb")
    sql_result = state.get("sql_result")

    # 纯闲聊
    if route_name == "direct":
        answer = (
            "你好，我是企业知识库与经营数据助手。"
            "可以问制度规定，比如「退货窗口是多久」；"
            "也可以问经营数据，比如「上月销售额最高的是哪个品类」。"
        )
        emit({"type": "token", "text": answer})
        return {"answer": answer}

    # SQL 查不到东西，也没检索到文档
    if route_name == "sql" and sql_result and not sql_result.get("ok"):
        answer = sql_result.get("error") or "这个问题暂时查不到数据。"
        emit({"type": "token", "text": answer})
        return {"answer": answer}

    context = prompts.build_context(state.get("retrieved") or [])
    sql_block = "（无）"
    if sql_result and sql_result.get("ok"):
        sql_block = (
            f"指标：{sql_result['metric']['name']}"
            f"（单位 {sql_result['metric']['unit'] or '-'}，"
            f"区间 {sql_result.get('range') or '不限'}）\n"
            + metric_tool.summarize_rows(sql_result)
        )

    messages = [
        {"role": "system", "content": prompts.ANSWER_SYSTEM},
        {
            "role": "user",
            "content": prompts.ANSWER_TEMPLATE.format(
                history=state.get("history") or "（无）",
                context=context,
                sql_block=sql_block,
                question=state.get("rewritten") or state["query"],
            ),
        },
    ]

    answer_parts: list[str] = []
    usage = {"token_in": 0, "token_out": 0}
    try:
        async for chunk in llm_stream(messages):
            if "delta" in chunk:
                answer_parts.append(chunk["delta"])
                emit({"type": "token", "text": chunk["delta"]})
            elif "usage" in chunk:
                usage = chunk["usage"]
    except Exception as exc:
        msg = f"生成回答时出错：{exc}"
        emit({"type": "error", "message": msg})
        return {"answer": "".join(answer_parts), "error": msg}

    answer = "".join(answer_parts)
    emit({"type": "step", "name": "generate", "ms": int((time.perf_counter() - t0) * 1000)})
    return {
        "answer": answer,
        "usage": _add_usage(state, usage),
        "timings": {**(state.get("timings") or {}), "generate": int((time.perf_counter() - t0) * 1000)},
    }


# ---------------------------------------------------------------
import re  # noqa: E402

_CITE_RE = re.compile(r"\[(\d+)\]")


async def verify(state: AgentState, config: dict) -> dict:
    """引用编号校验：模型可能引用 [7] 但只有 5 条来源，越界的直接剔除。"""
    answer = state.get("answer") or ""
    citations = state.get("citations") or []
    n = len(citations)

    if n == 0:
        return {}

    bad: set[int] = set()

    def _check(m: re.Match) -> str:
        idx = int(m.group(1))
        if 1 <= idx <= n:
            return m.group(0)
        bad.add(idx)
        return ""

    cleaned = _CITE_RE.sub(_check, answer)
    used = sorted({int(x) for x in _CITE_RE.findall(cleaned)})

    if bad:
        _emit(config)({"type": "step", "name": "verify", "dropped": sorted(bad)})

    return {
        "answer": cleaned,
        "citations": [citations[i - 1] for i in used if i <= n],
    }
