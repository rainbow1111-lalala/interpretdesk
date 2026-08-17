"""阿里云百炼 LiveTranslate 实时引擎。

协议按官方文档实现：连上 WebSocket 后先发 session.update，音频用 input_audio_buffer.append
逐帧送，源语转写与译文分别从两组事件回来。modalities 只要 text，不要合成语音，省一半延迟。

文档对若干字段名给的是事件名而非载荷字段名，所以取文本时把 text、delta、transcript 都试一遍，
并把没见过的事件类型记进日志。源语言留空以启用自动识别。

实测要点（2026-08-17，真实流量核对过）：这些事件给的不是增量片段，而是当前这一句的全文，
每次要覆盖不能累加，累加会让字幕重复叠好几遍；一句的收尾信号是 .completed 与 .done，
一轮应答的结束是 response.done，用它当段落边界。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Awaitable, Callable

import websockets

from .settings import Engine

log = logging.getLogger(__name__)

Emit = Callable[[dict], Awaitable[None]]

# 源语转写
SRC_EVENTS = {
    "conversation.item.input_audio_transcription.text",
    "conversation.item.input_audio_transcription.delta",
    "conversation.item.input_audio_transcription.completed",
}
# 译文。modalities 为 ["text"] 时走 response.text.*，带语音时走 response.audio_transcript.*
DST_EVENTS = {
    "response.text.text",
    "response.text.delta",
    "response.text.done",
    "response.audio_transcript.text",
    "response.audio_transcript.delta",
    "response.audio_transcript.done",
}
# 一句收尾。收到这些就把当前这句并进定稿，后面的事件属于下一句
COMMIT_EVENTS = {"conversation.item.input_audio_transcription.completed",
                 "response.text.done", "response.audio_transcript.done"}
IGNORE_EVENTS = {"session.created", "session.updated", "response.created",
                 "response.done",
                 "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                 "input_audio_buffer.committed", "conversation.item.created",
                 "response.output_item.added", "response.content_part.added",
                 "response.output_item.done", "response.content_part.done", "rate_limits.updated"}


def _text_of(event: dict) -> str:
    for key in ("text", "delta", "transcript"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    # 有些实现把内容包一层
    for key in ("transcription", "response"):
        inner = event.get(key)
        if isinstance(inner, dict):
            for sub in ("text", "delta", "transcript"):
                if isinstance(inner.get(sub), str) and inner[sub]:
                    return inner[sub]
    return ""


def ws_url(engine: Engine, model: str) -> str:
    """把用户填的 base_url 归一成实时地址。

    百炼给的是 https://<workspace>.<region>.maas.aliyuncs.com/compatible-mode/v1 这种文本
    端点，实时端点在同域的 /api-ws/v1/realtime，这里自动换算，省得用户手拼。
    """
    raw = engine.base_url.strip().rstrip("/")
    if raw.startswith("wss://") or raw.startswith("ws://"):
        base = raw
    else:
        host = raw.replace("https://", "").replace("http://", "").split("/")[0]
        base = f"wss://{host}/api-ws/v1/realtime"
    if "/api-ws/" not in base:
        base = f"{base.split('/compatible-mode')[0].rstrip('/')}/api-ws/v1/realtime"
    return f"{base}?model={model}"


async def probe(engine: Engine, model: str, target_lang: str = "zh") -> dict:
    """连通性测试：真的连上去发一次 session.update，看服务端认不认。"""
    import time
    started = time.monotonic()
    url = ws_url(engine, model)
    if not engine.api_key:
        return {"ok": False, "error": "没填 API key", "url": url}
    try:
        headers = {"Authorization": f"Bearer {engine.api_key}"}
        async with websockets.connect(url, additional_headers=headers, max_size=None,
                                      open_timeout=20) as ws:
            client = QwenLiveClient(lambda _: asyncio.sleep(0), engine, model, target_lang)
            await ws.send(json.dumps(client.session_update()))
            deadline = asyncio.get_running_loop().time() + 20
            while asyncio.get_running_loop().time() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
                event = json.loads(raw)
                kind = event.get("type", "")
                if kind == "error" or event.get("error"):
                    detail = json.dumps(event.get("error") or event, ensure_ascii=False)
                    return {"ok": False, "url": url, "error": detail[:260]}
                if kind in ("session.created", "session.updated"):
                    return {"ok": True, "url": url, "event": kind,
                            "seconds": round(time.monotonic() - started, 2)}
            return {"ok": False, "url": url, "error": "连上了但服务端没回 session 事件"}
    except Exception as exc:
        return {"ok": False, "url": url, "error": f"{type(exc).__name__}：{exc}"[:260]}


class QwenLiveClient:
    """接口与 LiveTranslateClient 保持一致，main.py 可以直接换。"""

    def __init__(self, emit: Emit, engine: Engine, model: str, target_lang: str = "zh"):
        self.emit = emit
        self.engine = engine
        self.model = model
        # 百炼用的是 zh 这类短码，传进来的可能是 zh-CN
        self.target_lang = (target_lang or "zh").split("-")[0]
        self.audio_q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=200)
        self._stop = asyncio.Event()
        self.audio_tokens = 0
        self.response_tokens = 0
        self._unknown: set[str] = set()

    def feed(self, pcm: bytes) -> None:
        try:
            self.audio_q.put_nowait(pcm)
        except asyncio.QueueFull:
            try:
                self.audio_q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.audio_q.put_nowait(pcm)

    def stop(self) -> None:
        self._stop.set()
        try:
            self.audio_q.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def session_update(self) -> dict:
        return {
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "input_audio_format": "pcm",
                # language 留空表示自动识别源语言
                "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
                "translation": {"language": self.target_lang},
            },
        }

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._one_connection()
                attempt = 0
            except Exception as exc:
                if self._stop.is_set():
                    break
                attempt += 1
                log.warning("百炼实时连接中断（第 %d 次）：%s", attempt, exc)
                await self.emit({"type": "status", "state": "reconnecting",
                                 "detail": f"{type(exc).__name__}: {exc}"[:200],
                                 "attempt": attempt})
                await asyncio.sleep(min(0.5 * 2 ** (attempt - 1), 8.0))
        await self.emit({"type": "status", "state": "closed"})

    async def _one_connection(self) -> None:
        if not self.engine.api_key:
            raise RuntimeError("百炼实时引擎需要 API key，请在模型设置里填")
        url = ws_url(self.engine, self.model)
        headers = {"Authorization": f"Bearer {self.engine.api_key}"}
        log.info("连接百炼实时端点 %s", url)
        async with websockets.connect(url, additional_headers=headers,
                                      max_size=None, ping_interval=20) as ws:
            await ws.send(json.dumps(self.session_update()))
            await self.emit({"type": "status", "state": "live"})
            sender = asyncio.create_task(self._send_loop(ws))
            try:
                await self._recv_loop(ws)
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    async def _send_loop(self, ws) -> None:
        while True:
            pcm = await self.audio_q.get()
            if pcm is None:
                await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                await ws.send(json.dumps({"type": "session.finish"}))
                return
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode(),
            }))

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            event = json.loads(raw)
            kind = event.get("type", "")

            if kind == "error" or event.get("error"):
                detail = json.dumps(event.get("error") or event, ensure_ascii=False)[:300]
                raise RuntimeError(f"百炼返回错误：{detail}")

            if kind in SRC_EVENTS:
                if text := _text_of(event):
                    await self.emit({"type": "src", "text": text,
                                     "lang": event.get("language", ""),
                                     "commit": kind in COMMIT_EVENTS})
            elif kind in DST_EVENTS:
                if text := _text_of(event):
                    await self.emit({"type": "dst", "text": text,
                                     "lang": self.target_lang,
                                     "commit": kind in COMMIT_EVENTS})
            elif kind == "response.done":
                # 一轮应答结束，作为段落边界
                await self.emit({"type": "turn_complete"})
            elif kind == "session.finished":
                return
            elif kind not in IGNORE_EVENTS and kind not in self._unknown:
                self._unknown.add(kind)
                log.info("百炼未识别事件 %s：%s", kind,
                         json.dumps(event, ensure_ascii=False)[:400])

            if usage := event.get("usage"):
                self.audio_tokens += (usage.get("input_tokens")
                                      or usage.get("prompt_tokens") or 0)
                self.response_tokens += (usage.get("output_tokens")
                                         or usage.get("completion_tokens") or 0)
            if self._stop.is_set():
                return
