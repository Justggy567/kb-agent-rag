"""DeepSeek API 与 Ollama Embedding 的异步封装（D6 教学增强版）。

保留原项目已有的对外接口：
- chat(prompt, system=None, temperature=0.1, json_mode=False, max_tokens=None)
    -> (text, {"token_in": int, "token_out": int})
- chat_json(prompt, system=None) -> (dict, usage)
- stream(messages, temperature=0.3) -> 异步产出 {"delta": str} / {"usage": {...}}
- embed_with_ollama(texts) -> list[list[float]]

新增：chat_stream(prompt, ...) -> 异步产出文本片段 str。

与原版相比，明确设置 DeepSeek 的非思考模式（thinking=False），从而使
temperature 参数生效。如果想使用思考模式，可在 chat/stream 中传 thinking=True。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings


logger = logging.getLogger(__name__)

# 四阶段超时：建立连接 / 等待下一段响应 / 发送请求 / 等待连接池空位。
# 注意 read 是“等待下一块数据”的超时，而非整个回答的总时长。
CHAT_TIMEOUT = httpx.Timeout(connect=10, read=120, write=10, pool=10)
STREAM_TIMEOUT = httpx.Timeout(connect=10, read=300, write=10, pool=10)
EMBED_TIMEOUT = httpx.Timeout(connect=10, read=180, write=10, pool=10)


class LLMError(RuntimeError):
    """供上层业务捕获的安全错误：不附带上游响应正文、API Key 或完整请求。"""


def _headers() -> dict[str, str]:
    """生成 DeepSeek 的身份验证请求头。"""
    key = settings.deepseek_api_key
    if not key or key.startswith("sk-xxxx"):
        raise LLMError("未配置 DEEPSEEK_API_KEY，请复制 .env.example 为 .env 并填写")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _endpoint(base_url: str, path: str) -> str:
    """避免配置地址以 / 结尾时出现重复斜杠。"""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _payload(
    messages: list[dict],
    temperature: float,
    json_mode: bool,
    stream: bool,
    max_tokens: int | None,
    *,
    thinking: bool | None = False,
) -> dict[str, Any]:
    """集中构建 OpenAI 兼容的 Chat Completions 请求正文。"""
    if not 0 <= temperature <= 2:
        raise LLMError("temperature 必须处于 0 到 2 之间")
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1):
        raise LLMError("max_tokens 必须是正整数")

    body: dict[str, Any] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }
    # DeepSeek 目前默认开启思考模式，而该模式下 temperature 不生效。
    # thinking=None 表示不传此字段，遵循服务端默认值。
    if thinking is not None:
        body["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


def _usage(raw: Any) -> dict[str, int]:
    """将供应商 usage 字段统一成项目已有的两个字段。"""
    usage = raw if isinstance(raw, dict) else {}
    return {
        "token_in": usage.get("prompt_tokens", 0) or 0,
        "token_out": usage.get("completion_tokens", 0) or 0,
    }


async def chat(
    prompt: str,
    system: str | None = None,
    temperature: float = 0.1,
    json_mode: bool = False,
    max_tokens: int | None = None,
    *,
    thinking: bool | None = False,
) -> tuple[str, dict]:
    """一次性请求 DeepSeek，返回 (文本, token 用量)。

    thinking=False（默认）：使用非思考模式，temperature 可以起作用。
    thinking=True：启用思考模式，此时 temperature 会被忽略。
    thinking=None：不发送 thinking，采用服务端默认配置。
    """
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # 仅记录安全状态/异常类型，不记录 prompt、请求头、密钥或响应原文。
    try:
        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as client:
            resp = await client.post(
                _endpoint(settings.deepseek_base_url, "/chat/completions"),
                headers=_headers(),
                json=_payload(messages, temperature, json_mode, False, max_tokens, thinking=thinking),
            )
        if resp.status_code >= 400:
            logger.warning("DeepSeek chat HTTP status=%s", resp.status_code)
            raise LLMError(f"模型调用失败（HTTP {resp.status_code}）")
        data = resp.json()
    except asyncio.CancelledError:
        logger.debug("DeepSeek chat task cancelled")
        raise  # 用户离开页面或上层取消任务，不误报成网络故障。
    except httpx.TimeoutException as exc:
        logger.warning("DeepSeek chat timeout: %s", type(exc).__name__)
        raise LLMError("模型调用超时，请稍后重试") from None
    except httpx.RequestError as exc:
        logger.warning("DeepSeek chat network failure: %s", type(exc).__name__)
        raise LLMError("模型网络连接失败，请稍后重试") from None
    except ValueError:
        logger.warning("DeepSeek chat returned invalid HTTP JSON")
        raise LLMError("模型响应不是有效 JSON") from None

    try:
        text = data["choices"][0]["message"]["content"] or ""
        if not isinstance(text, str):
            raise TypeError("content is not str")
        usage = _usage(data.get("usage"))
    except (KeyError, IndexError, TypeError, AttributeError):
        logger.warning("DeepSeek chat response schema mismatch")
        raise LLMError("模型响应格式异常") from None

    logger.info("DeepSeek chat usage: token_in=%s token_out=%s", usage["token_in"], usage["token_out"])
    return text, usage


async def chat_json(
    prompt: str,
    system: str | None = None,
    *,
    max_tokens: int | None = None,
) -> tuple[dict, dict]:
    """要求 DeepSeek 返回 JSON 对象；解析失败最多重试一次。

    为兼容项目原版：两次都无法解析时返回 ({}, 累计 usage)，而不是抛异常。
    调用方应检查结果是否为空，以及业务字段是否齐全/类型正确。
    """
    json_instruction = (
        "请只返回合法的 JSON 对象，不要 Markdown 代码块或额外说明。"
        "JSON 格式示例：{\"result\": \"示例值\"}。"
        "示例仅用于说明格式；如果任务指定字段，请严格使用任务指定的字段。"
    )
    combined_system = f"{system}\n\n{json_instruction}" if system else json_instruction
    totals = {"token_in": 0, "token_out": 0}

    for attempt in range(2):  # 发送最多两次 API 请求，而不是只对同一字符串解析两次。
        text, usage = await chat(
            prompt,
            system=combined_system,
            temperature=0,
            json_mode=True,
            max_tokens=max_tokens,
            thinking=False,
        )
        totals["token_in"] += usage["token_in"]
        totals["token_out"] += usage["token_out"]
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result, totals
        except (json.JSONDecodeError, TypeError):
            pass
        logger.warning("DeepSeek JSON output invalid, attempt=%s/2", attempt + 1)

    # 兼容原函数的“失败则空字典”行为，但记录告警且统计两次 API 消耗。
    return {}, totals


async def _iter_sse_data(resp: httpx.Response) -> AsyncIterator[str]:
    """解析 Server-Sent Events 的 data 字段；允许注释行和多行 data。"""
    data_lines: list[str] = []
    async for line in resp.aiter_lines():
        if line == "":  # SSE 使用空行表示一个事件结束。
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith("data:"):
            # SSE 的冒号后面允许出现一个可选的空格。
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if data_lines:  # 兼容最后一条事件后面没有空行的情况。
        yield "\n".join(data_lines)


async def stream(
    messages: list[dict],
    temperature: float = 0.3,
    *,
    thinking: bool | None = False,
) -> AsyncIterator[dict]:
    """流式输出 {"delta": 文本片段}，最后通常还会产出 {"usage": {...}}。

    注意 delta 是“文本片段”，不保证恰好是一个 token。
    """
    done = False
    try:
        async with httpx.AsyncClient(timeout=STREAM_TIMEOUT) as client:
            async with client.stream(
                "POST",
                _endpoint(settings.deepseek_base_url, "/chat/completions"),
                headers=_headers(),
                json=_payload(messages, temperature, False, True, None, thinking=thinking),
            ) as resp:
                if resp.status_code >= 400:
                    logger.warning("DeepSeek stream HTTP status=%s", resp.status_code)
                    # 无需读取上游错误正文（可能包含内部信息）。
                    raise LLMError(f"模型调用失败（HTTP {resp.status_code}）")

                async for raw in _iter_sse_data(resp):
                    if raw.strip() == "[DONE]":
                        done = True
                        break
                    try:
                        chunk = json.loads(raw)
                        if not isinstance(chunk, dict):
                            raise ValueError("SSE data is not an object")
                    except (json.JSONDecodeError, ValueError):
                        logger.warning("DeepSeek stream invalid SSE JSON")
                        raise LLMError("模型流式数据格式异常") from None

                    # 先输出文本，再输出本片段携带的 Token 用量。
                    choices = chunk.get("choices") or []
                    if not isinstance(choices, list):
                        raise LLMError("模型流式数据格式异常")
                    for choice in choices:
                        if not isinstance(choice, dict):
                            raise LLMError("模型流式数据格式异常")
                        delta_obj = choice.get("delta") or {}
                        if not isinstance(delta_obj, dict):
                            raise LLMError("模型流式数据格式异常")
                        delta = delta_obj.get("content")
                        if isinstance(delta, str) and delta:
                            yield {"delta": delta}

                    if chunk.get("usage"):
                        usage = _usage(chunk["usage"])
                        logger.info("DeepSeek stream usage: token_in=%s token_out=%s", usage["token_in"], usage["token_out"])
                        yield {"usage": usage}

        if not done:
            logger.warning("DeepSeek stream ended without [DONE]")
            raise LLMError("模型流式响应意外中断")
    except asyncio.CancelledError:
        logger.debug("DeepSeek stream task cancelled")
        raise
    except httpx.TimeoutException as exc:
        logger.warning("DeepSeek stream timeout: %s", type(exc).__name__)
        raise LLMError("模型流式调用超时，请稍后重试") from None
    except httpx.RequestError as exc:
        logger.warning("DeepSeek stream network failure: %s", type(exc).__name__)
        raise LLMError("模型网络连接失败，请稍后重试") from None


async def chat_stream(
    prompt: str,
    system: str | None = None,
    temperature: float = 0.3,
    *,
    thinking: bool | None = False,
) -> AsyncIterator[str]:
    """D6 教学接口：只产出文本；不改变原有 stream(messages) 接口。"""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    async for item in stream(messages, temperature=temperature, thinking=thinking):
        if "delta" in item:
            yield item["delta"]


async def embed_with_ollama(texts: list[str]) -> list[list[float]]:
    """调用本地 Ollama /api/embed，一次可传多条文本。"""
    if not texts:
        return []
    try:
        async with httpx.AsyncClient(timeout=EMBED_TIMEOUT) as client:
            resp = await client.post(
                _endpoint(settings.ollama_base_url, "/api/embed"),
                json={"model": settings.embed_model, "input": texts},
            )
        if resp.status_code >= 400:
            logger.warning("Ollama embedding HTTP status=%s", resp.status_code)
            raise LLMError(
                f"Embedding 失败（HTTP {resp.status_code}），确认 Ollama 已启动且已 pull {settings.embed_model}"
            )
        data = resp.json()
    except asyncio.CancelledError:
        logger.debug("Ollama embedding task cancelled")
        raise
    except httpx.TimeoutException as exc:
        logger.warning("Ollama embedding timeout: %s", type(exc).__name__)
        raise LLMError("Embedding 调用超时，请检查本地 Ollama") from None
    except httpx.RequestError as exc:
        logger.warning("Ollama embedding network failure: %s", type(exc).__name__)
        raise LLMError("无法连接 Ollama，请检查本地服务") from None
    except ValueError:
        logger.warning("Ollama embedding invalid HTTP JSON")
        raise LLMError("Embedding 响应不是有效 JSON") from None

    if not isinstance(data, dict):
        raise LLMError("Embedding 响应格式异常")
    vectors = data.get("embeddings") or []
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise LLMError("Embedding 返回数量不匹配")
    if not all(isinstance(v, list) and v and all(isinstance(x, (int, float)) for x in v) for v in vectors):
        raise LLMError("Embedding 向量格式异常")
    if len({len(v) for v in vectors}) != 1:
        raise LLMError("Embedding 向量维度不一致")
    return vectors
