"""问答接口：SSE 流式输出 + 会话持久化。"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.db import pool

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    session_id: str | None = None


class SessionCreate(BaseModel):
    title: str | None = None


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def initial_state(question: str, history: str = "") -> dict:
    """每轮都要显式重置中间态。

    LangGraph 的 checkpointer 会按 thread_id 累积状态，如果只传 query，
    上一轮的 citations / sql_result / retry 会漏到这一轮，出现"新问题带着旧答案"。
    这里把可变字段全部覆盖一遍，是最省心的做法。
    """
    return {
        "query": question,
        "history": history,
        "rewritten": "",
        "route": "kb",
        "clarify_question": "",
        "retrieved": [],
        "sql_result": None,
        "citations": [],
        "answer": "",
        "retry": 0,
        "grade_action": "",
        "error": None,
        "usage": {"token_in": 0, "token_out": 0},
        "timings": {},
    }


async def _load_history(session_id: str, limit: int = 6) -> str:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT role, content FROM messages
               WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2""",
            session_id,
            limit,
        )
    rows = list(reversed(rows))
    if not rows:
        return ""
    name = {"user": "用户", "assistant": "助手"}
    return "\n".join(f"{name.get(r['role'], r['role'])}：{(r['content'] or '')[:200]}" for r in rows)


async def _save(session_id: str, role: str, content: str, **extra) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO messages (session_id, role, content, citations, route,
                                     token_in, token_out, latency_ms, trace_id)
               VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)""",
            session_id,
            role,
            content,
            json.dumps(extra.get("citations")) if extra.get("citations") else None,
            extra.get("route"),
            extra.get("token_in"),
            extra.get("token_out"),
            extra.get("latency_ms"),
            extra.get("trace_id"),
        )


@router.post("/sessions")
async def create_session(payload: SessionCreate | None = None):
    title = (payload.title if payload else None) or "新会话"
    async with pool().acquire() as conn:
        sid = await conn.fetchval(
            "INSERT INTO sessions (title) VALUES ($1) RETURNING id", title
        )
    return {"session_id": str(sid), "title": title}


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str):
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT role, content, citations, route, created_at
               FROM messages WHERE session_id = $1 ORDER BY created_at""",
            uuid.UUID(session_id),
        )
    return {
        "items": [
            {
                "role": r["role"],
                "content": r["content"],
                "citations": r["citations"],
                "route": r["route"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest, request: Request):
    """SSE 接口。事件类型：step / route / sources / sql / data / token / error / done。"""
    graph = request.app.state.graph
    t0 = time.perf_counter()

    session_id = payload.session_id
    if not session_id:
        async with pool().acquire() as conn:
            session_id = str(
                await conn.fetchval(
                    "INSERT INTO sessions (title) VALUES ($1) RETURNING id",
                    payload.question[:20],
                )
            )

    history = await _load_history(session_id)
    await _save(session_id, "user", payload.question)

    queue: asyncio.Queue[dict] = asyncio.Queue()

    def emit(event: dict) -> None:
        queue.put_nowait(event)

    async def run() -> dict:
        return await graph.ainvoke(
            initial_state(payload.question, history),
            config={
                "configurable": {"thread_id": session_id, "emit": emit},
                "recursion_limit": 12,
            },
        )

    task = asyncio.create_task(run())

    async def event_gen():
        yield _sse({"type": "session", "session_id": session_id})
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.3)
                except asyncio.TimeoutError:
                    if task.done():
                        while not queue.empty():
                            yield _sse(queue.get_nowait())
                        break
                    if await request.is_disconnected():
                        task.cancel()
                        return
                    continue
                yield _sse(event)

            result = task.result()
            latency = int((time.perf_counter() - t0) * 1000)
            usage = result.get("usage") or {}

            citations = result.get("citations") or []
            if citations:
                yield _sse({"type": "citations", "items": citations})

            await _save(
                session_id,
                "assistant",
                result.get("answer") or "",
                citations=citations,
                route=result.get("route"),
                token_in=usage.get("token_in"),
                token_out=usage.get("token_out"),
                latency_ms=latency,
            )
            yield _sse(
                {
                    "type": "done",
                    "session_id": session_id,
                    "route": result.get("route"),
                    "usage": usage,
                    "timings": result.get("timings"),
                    "latency_ms": latency,
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield _sse({"type": "error", "message": f"处理失败：{exc}"})
            yield _sse({"type": "done", "error": True})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # 没有这行，Nginx 会把流攒成一次性吐出
        },
    )


@router.post("/chat")
async def chat_once(payload: ChatRequest, request: Request):
    """非流式版本，评测脚本用这个跑批更省事。"""
    graph = request.app.state.graph
    history = ""
    if payload.session_id:
        history = await _load_history(payload.session_id)

    result = await graph.ainvoke(
        initial_state(payload.question, history),
        config={
            "configurable": {"thread_id": payload.session_id or str(uuid.uuid4())},
            "recursion_limit": 12,
        },
    )
    return {
        "answer": result.get("answer"),
        "route": result.get("route"),
        "citations": result.get("citations"),
        "sql_result": result.get("sql_result"),
        "usage": result.get("usage"),
        "timings": result.get("timings"),
    }
