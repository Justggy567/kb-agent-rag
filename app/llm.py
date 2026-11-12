"""DeepSeek 调用封装。只暴露两个方法：chat（一次性）和 stream（流式）。"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings


class LLMError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    if not settings.deepseek_api_key or settings.deepseek_api_key.startswith("sk-xxxx"):
        raise LLMError("未配置 DEEPSEEK_API_KEY，请复制 .env.example 为 .env 并填写")
    return {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }


def _payload(
    messages: list[dict],
    temperature: float,
    json_mode: bool,
    stream: bool,
    max_tokens: int | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if max_tokens:
        body["max_tokens"] = max_tokens
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


async def chat(
    prompt: str,
    system: str | None = None,
    temperature: float = 0.1,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> tuple[str, dict]:
    """一次性返回 (文本, usage)。"""
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers=_headers(),
            json=_payload(messages, temperature, json_mode, False, max_tokens),
        )
    if resp.status_code >= 400:
        # 不把上游原文直接抛给前端，这里收敛成一句可读的话
        raise LLMError(f"模型调用失败（HTTP {resp.status_code}）")

    data = resp.json()
    text = data["choices"][0]["message"]["content"] or ""
    usage = data.get("usage") or {}
    return text, {
        "token_in": usage.get("prompt_tokens", 0),
        "token_out": usage.get("completion_tokens", 0),
    }


async def chat_json(prompt: str, system: str | None = None) -> tuple[dict, dict]:
    """要求模型输出 JSON，解析失败时退回空 dict 而不是抛异常。"""
    text, usage = await chat(prompt, system=system, temperature=0, json_mode=True)
    try:
        return json.loads(text), usage
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1]), usage
            except json.JSONDecodeError:
                pass
    return {}, usage


async def stream(messages: list[dict], temperature: float = 0.3) -> AsyncIterator[dict]:
    """逐 token 产出 {"delta": str} 或 {"usage": {...}}。"""
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10, read=300)) as client:
        async with client.stream(
            "POST",
            f"{settings.deepseek_base_url}/chat/completions",
            headers=_headers(),
            json=_payload(messages, temperature, False, True, None),
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise LLMError(f"模型调用失败（HTTP {resp.status_code}）")

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    u = chunk["usage"]
                    yield {
                        "usage": {
                            "token_in": u.get("prompt_tokens", 0),
                            "token_out": u.get("completion_tokens", 0),
                        }
                    }

                for choice in chunk.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        yield {"delta": delta}


async def embed_with_ollama(texts: list[str]) -> list[list[float]]:
    """Ollama /api/embed，一次可传多条。"""
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{settings.ollama_base_url}/api/embed",
            json={"model": settings.embed_model, "input": texts},
        )
    if resp.status_code >= 400:
        raise LLMError(f"Embedding 失败（HTTP {resp.status_code}），确认 ollama 已启动且已 pull {settings.embed_model}")
    data = resp.json()
    vectors = data.get("embeddings") or []
    if len(vectors) != len(texts):
        raise LLMError("Embedding 返回数量不匹配")
    return vectors
