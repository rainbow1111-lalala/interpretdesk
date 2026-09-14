"""Gemini Live Translate 客户端。

一条 WebSocket 同时拿到源语转写、中文译文转写、译文音频。两件实测过的事情写在代码里，
不要按官方文档回退：inputAudioTranscription 与 outputAudioTranscription 必须放在 setup
根层（放进 generationConfig 会被 1007 拒绝）；单条连接寿命有限，必须靠 sessionResumption
的 handle 续接，否则字幕会在会议中途断掉。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Awaitable, Callable

import websockets

from . import config

log = logging.getLogger(__name__)

Emit = Callable[[dict], Awaitable[None]]


def build_setup(target_lang: str, handle: str | None) -> dict:
    setup: dict = {
        "model": config.LIVE_MODEL,
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "translationConfig": {
                "targetLanguageCode": target_lang,
                "echoTargetLanguage": True,
            },
        },
        # 实测：这两个字段只能放在 setup 根层
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        "sessionResumption": {"handle": handle} if handle else {},
        "contextWindowCompression": {"slidingWindow": {}},
    }
    return {"setup": setup}


class LiveTranslateClient:
    """把音频喂进去，把转写事件吐出来，断线自己接回来。"""

    def __init__(self, emit: Emit, target_lang: str = config.TARGET_LANG):
        self.emit = emit
        self.target_lang = target_lang
        self.audio_q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=200)
        self._handle: str | None = None
        self._stop = asyncio.Event()
        # 与 QwenLiveClient 对齐：服务端确认这一路收完了，上层停止记录时等它
        self.finished = asyncio.Event()
        self.audio_tokens = 0
        self.response_tokens = 0

    def feed(self, pcm: bytes) -> None:
        """非阻塞投喂。队列满了丢最旧的，宁可掉一帧也不让字幕越来越滞后。"""
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

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._one_connection()
                attempt = 0
            except Exception as exc:  # 连接层任何异常都走重连
                if self._stop.is_set():
                    break
                attempt += 1
                delay = min(0.5 * 2 ** (attempt - 1), 8.0)
                log.warning("live 连接中断（第 %d 次）：%s", attempt, exc)
                await self.emit({
                    "type": "status",
                    "state": "reconnecting",
                    "detail": f"{type(exc).__name__}: {exc}"[:200],
                    "attempt": attempt,
                })
                self._drop_stale_audio()
                await asyncio.sleep(delay)
        self.finished.set()
        await self.emit({"type": "status", "state": "closed"})

    def _drop_stale_audio(self) -> None:
        """重连期间攒下来的音频已经没有意义，只留最后 1 秒。"""
        keep = 1000 // config.CHUNK_MS
        items: list[bytes | None] = []
        while True:
            try:
                items.append(self.audio_q.get_nowait())
            except asyncio.QueueEmpty:
                break
        for item in items[-keep:]:
            try:
                self.audio_q.put_nowait(item)
            except asyncio.QueueFull:
                break

    async def _one_connection(self) -> None:
        url = f"{config.LIVE_URL}?key={config.api_key()}"
        async with websockets.connect(url, max_size=None, ping_interval=20) as ws:
            await ws.send(json.dumps(build_setup(self.target_lang, self._handle)))
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if "setupComplete" not in first:
                raise RuntimeError(f"setup 被拒绝：{json.dumps(first)[:300]}")
            resumed = self._handle is not None
            await self.emit({"type": "status", "state": "live", "resumed": resumed})

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
                await ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
                return
            await ws.send(json.dumps({"realtimeInput": {"audio": {
                "data": base64.b64encode(pcm).decode(),
                "mimeType": f"audio/pcm;rate={config.SAMPLE_RATE}",
            }}}))

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)

            if h := msg.get("sessionResumptionUpdate", {}).get("newHandle"):
                self._handle = h
            if go := msg.get("goAway"):
                log.info("服务端预告断开：%s", go)
                await self.emit({"type": "status", "state": "goaway",
                                 "timeLeft": go.get("timeLeft")})
            if usage := msg.get("usageMetadata"):
                for d in usage.get("promptTokensDetails", []):
                    if d.get("modality") == "AUDIO":
                        self.audio_tokens += d.get("tokenCount", 0)
                self.response_tokens += usage.get("responseTokenCount", 0) or 0

            sc = msg.get("serverContent")
            if not sc:
                continue
            if it := sc.get("inputTranscription"):
                if text := it.get("text"):
                    await self.emit({"type": "src", "text": text,
                                     "lang": it.get("languageCode", "")})
            if ot := sc.get("outputTranscription"):
                if text := ot.get("text"):
                    await self.emit({"type": "dst", "text": text,
                                     "lang": ot.get("languageCode", "")})
            for part in sc.get("modelTurn", {}).get("parts", []):
                if data := part.get("inlineData", {}).get("data"):
                    await self.emit({"type": "audio", "b64": data})
            if sc.get("turnComplete"):
                await self.emit({"type": "turn_complete"})
            if self._stop.is_set() and sc.get("turnComplete"):
                # 停止之后收到的这一次 turnComplete 就是尾句收口，上层据此立刻收摊
                self.finished.set()
                return
