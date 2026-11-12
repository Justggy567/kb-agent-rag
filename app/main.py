"""应用入口。

启动顺序很重要：
    DB 连接池 -> LangGraph checkpointer -> 编译图 -> 挂静态资源
checkpointer 建表失败时自动降级到内存版，保证本地能起来（重启会丢会话）。
"""
from __future__ import annotations

import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


from app.agent.graph import build_graph
from app.api import chat as chat_api
from app.api import documents as doc_api
from app.config import settings
from app.db import close_pool, init_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("app")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


async def _make_checkpointer():
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            conninfo=settings.dsn,
            max_size=6,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        log.info("checkpointer: PostgreSQL（会话可跨重启恢复）")
        return saver, pool
    except Exception as exc:  # noqa: BLE001
        from langgraph.checkpoint.memory import MemorySaver

        log.warning("checkpointer 初始化失败，降级为内存版：%s", exc)
        return MemorySaver(), None


# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     await init_pool()
#     saver, ck_pool = await _make_checkpointer()
#     app.state.graph = build_graph(checkpointer=saver)
#     app.state.ck_pool = ck_pool
#     log.info("图已编译，服务就绪")
#     try:
#         yield
#     finally:
#         await close_pool()
#         if ck_pool is not None:
#             with contextlib.suppress(Exception):
#                 await ck_pool.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ck_pool = None

    try:
        await init_pool()

        saver, ck_pool = await _make_checkpointer()

        app.state.graph = build_graph(
            checkpointer=saver
        )
        app.state.ck_pool = ck_pool

        log.info("图已编译，服务就绪")

        yield

    finally:
        try:
            if ck_pool is not None:
                await ck_pool.close()
        finally:
            await close_pool()





app = FastAPI(title="企业知识库与 Agent 智能问答平台", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_api.router)
app.include_router(doc_api.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "model": settings.deepseek_model, "embed": settings.embed_model}


@app.get("/api/graph")
async def graph_mermaid():
    from app.agent.graph import build_mermaid

    return {"mermaid": build_mermaid()}


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")
