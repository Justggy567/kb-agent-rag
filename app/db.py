"""数据库连接池。两个池：一个写库（应用自己），一个只读（Text2SQL 执行）。"""
from __future__ import annotations

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None
_ro_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool, _ro_pool
    _pool = await asyncpg.create_pool(settings.dsn, min_size=2, max_size=10)
    _ro_pool = await asyncpg.create_pool(settings.readonly_dsn, min_size=1, max_size=4)


async def close_pool() -> None:
    if _pool:
        await _pool.close()
    if _ro_pool:
        await _ro_pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("连接池未初始化，检查 FastAPI lifespan")
    return _pool


def ro_pool() -> asyncpg.Pool:
    if _ro_pool is None:
        raise RuntimeError("只读连接池未初始化")
    return _ro_pool
