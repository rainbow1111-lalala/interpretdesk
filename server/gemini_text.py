"""Gemini 文本调用：背景摘要与英文回复拟稿。拟稿要边出边看，所以走 SSE 流式。"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from . import config


def _url(method: str, model: str | None = None) -> str:
    return config.TEXT_URL.format(model=model or config.TEXT_MODEL, method=method)


def _body(prompt: str, system: str | None, temperature: float, thinking: str) -> dict:
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            # 会议里等不起长思考
            "thinkingConfig": {"thinkingLevel": thinking},
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    return body


async def generate(prompt: str, system: str | None = None, *, temperature: float = 0.3,
                   thinking: str = "low", model: str | None = None, timeout: float = 90) -> str:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            _url("generateContent", model),
            params={"key": config.api_key()},
            json=_body(prompt, system, temperature, thinking),
        )
        r.raise_for_status()
        data = r.json()
    return _text_of(data)


async def stream(prompt: str, system: str | None = None, *, temperature: float = 0.4,
                 thinking: str = "low", model: str | None = None) -> AsyncIterator[str]:
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            _url("streamGenerateContent", model),
            params={"key": config.api_key(), "alt": "sse"},
            json=_body(prompt, system, temperature, thinking),
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                chunk = _text_of(json.loads(payload))
                if chunk:
                    yield chunk


def _text_of(data: dict) -> str:
    out = []
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            if part.get("thought"):
                continue
            if text := part.get("text"):
                out.append(text)
    return "".join(out)
