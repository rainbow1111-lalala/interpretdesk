"""文本层：OpenAI 兼容的 /v1/chat/completions。

底稿提炼、字幕译文、右栏拟稿都走这里。填什么服务商都行，只要兼容这个协议：OpenAI、DeepSeek、
Kimi、通义、OpenRouter，以及本机 vLLM 或 Ollama。api_key 留空时不发鉴权头，本地端点常这样。
"""
from __future__ import annotations

import json
import time
from typing import AsyncIterator

import httpx

from .settings import Engine


def _headers(engine: Engine) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if engine.api_key:
        headers["authorization"] = f"Bearer {engine.api_key}"
    return headers


def _endpoint(engine: Engine, path: str) -> str:
    return f"{engine.base_url.rstrip('/')}/{path.lstrip('/')}"


# 思考模式全档关掉。会议里首字延迟是第一指标，底稿提炼也吃过它的亏（整段 JSON 憋到最后才
# 回，撞了读超时）。别的服务商不认这个字段会返回 400，那就去掉重发一次，并记住这个端点不
# 支持，只付一次代价。
_NO_THINKING_FIELD = {"enable_thinking": False}
_field_supported: dict[str, bool] = {}


def _extra(engine: Engine) -> dict:
    if _field_supported.get(engine.base_url) is False:
        return {}
    return dict(_NO_THINKING_FIELD)


def _rejected_extra(status: int, body: str) -> bool:
    if status != 400:
        return False
    lowered = body.lower()
    return any(hint in lowered for hint in
               ("enable_thinking", "unknown", "unsupported", "invalid_parameter",
                "unrecognized", "extra fields"))


def _messages(prompt: str, system: str | None) -> list[dict]:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return msgs


async def generate(engine: Engine, model: str, prompt: str, system: str | None = None, *,
                   temperature: float = 0.3, timeout: float = 120) -> str:
    base = {"model": model, "messages": _messages(prompt, system),
            "temperature": temperature, "stream": False}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in (0, 1):
            extra = _extra(engine) if attempt == 0 else {}
            r = await client.post(_endpoint(engine, "chat/completions"),
                                  headers=_headers(engine), json={**base, **extra})
            if r.status_code >= 400 and extra and _rejected_extra(r.status_code, r.text):
                _field_supported[engine.base_url] = False
                continue
            r.raise_for_status()
            return _first_text(r.json())
    return ""


async def stream(engine: Engine, model: str, prompt: str, system: str | None = None, *,
                 temperature: float = 0.4) -> AsyncIterator[str]:
    base = {"model": model, "messages": _messages(prompt, system),
            "temperature": temperature, "stream": True}
    async with httpx.AsyncClient(timeout=180) as client:
        for attempt in (0, 1):
            extra = _extra(engine) if attempt == 0 else {}
            async with client.stream("POST", _endpoint(engine, "chat/completions"),
                                     headers=_headers(engine),
                                     json={**base, **extra}) as r:
                if r.status_code >= 400:
                    detail = (await r.aread()).decode(errors="replace")[:300]
                    if extra and _rejected_extra(r.status_code, detail):
                        _field_supported[engine.base_url] = False
                        continue
                    raise RuntimeError(f"文本层返回 {r.status_code}：{detail}")
                async for chunk in _sse_text(r):
                    yield chunk
                return


async def _sse_text(response: httpx.Response) -> AsyncIterator[str]:
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            # 有些服务商把推理过程放 reasoning_content，会议里不看这一段
            if text := delta.get("content"):
                yield text


def _first_text(data: dict) -> str:
    for choice in data.get("choices", []):
        message = choice.get("message") or {}
        if content := message.get("content"):
            return content
    return ""


async def probe(engine: Engine, model: str) -> dict:
    """连通性测试：真发一次最小请求，返回耗时与实际回复，不猜。"""
    started = time.monotonic()
    try:
        text = await generate(engine, model, "回复两个字：已连通", temperature=0,
                              timeout=45)
        return {"ok": True, "seconds": round(time.monotonic() - started, 2),
                "reply": text.strip()[:60], "model": model}
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:200] if exc.response is not None else ""
        return {"ok": False, "seconds": round(time.monotonic() - started, 2),
                "error": f"HTTP {exc.response.status_code}：{body}"}
    except Exception as exc:
        return {"ok": False, "seconds": round(time.monotonic() - started, 2),
                "error": f"{type(exc).__name__}：{exc}"[:220]}


async def embed(engine: Engine, model: str, texts: list[str]) -> list[list[float]]:
    """OpenAI 兼容的 /v1/embeddings。取回的顺序按 index 排，不假设服务端原样返回。"""
    if not texts:
        return []
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(_endpoint(engine, "embeddings"), headers=_headers(engine),
                              json={"model": model, "input": texts})
        r.raise_for_status()
        rows = r.json().get("data") or []
    rows.sort(key=lambda row: row.get("index", 0))
    return [row.get("embedding") or [] for row in rows]


async def list_models(engine: Engine) -> list[str]:
    """拉 /v1/models 给界面做候选，拉不到就算了，不阻塞保存。"""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(_endpoint(engine, "models"), headers=_headers(engine))
            r.raise_for_status()
            data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        return sorted({m.get("id", "") for m in (items or []) if m.get("id")})
    except Exception:
        return []
