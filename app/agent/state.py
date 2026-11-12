from __future__ import annotations

from typing import Literal, TypedDict

Route = Literal["kb", "sql", "both", "direct"]


class Citation(TypedDict, total=False):
    chunk_id: int
    doc_id: int
    doc_name: str
    page_no: int | None
    section_path: str | None
    snippet: str
    score: float


class AgentState(TypedDict, total=False):
    # 输入
    query: str                 # 本轮原始问题
    history: str               # 最近 3 轮对话，拼好的纯文本

    # 中间态
    rewritten: str             # 指代消解后的问题
    route: Route
    clarify_question: str      # 参数不全时反问
    retrieved: list[dict]      # 知识库召回
    sql_result: dict | None    # 语义层查询结果
    citations: list[Citation]
    retry: int                 # 重试次数，上限 settings.max_retry
    grade_action: str          # pass / retry，给条件边用

    # 输出
    answer: str
    usage: dict                # {"token_in": n, "token_out": n}
    timings: dict              # {"route": 12.3, "retrieve": 210.5, ...}
    error: str | None
