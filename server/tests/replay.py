"""端到端回放：把合成语音按实时速度灌进 /ws/live，打印真实收到的事件与延迟。

用法（先在另一个终端起后端）：
    uv run python -m server.tests.replay            # 英文样例
    uv run python -m server.tests.replay --zh       # 中文样例
    uv run python -m server.tests.replay --draft    # 顺带测拟稿接口
"""
from __future__ import annotations

import argparse
import asyncio
import os
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# 端口可用 MI_PORT 覆盖，好在不打扰正在跑的实例的前提下验证改动
PORT = os.environ.get("MI_PORT", "8787")
BASE = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}/ws/live"
CHUNK = 3200

SAMPLES = {
    "en": ("Alex",
           "Good morning. Before we turn to the indemnity clause, I want to confirm whether "
           "your client accepts the revised representations and warranties in section four "
           "point two. If not, we would need to revisit the escrow amount."),
    "zh": ("Tingting",
           "我们这边的意见是，第四条第二款的保证条款范围过宽，建议限定在卖方明知的范围内。"),
}


def fixture(kind: str) -> Path:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    pcm = FIXTURES / f"{kind}.pcm"
    if pcm.exists():
        return pcm
    voice, text = SAMPLES[kind]
    aiff = FIXTURES / f"{kind}.aiff"
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff),
                    "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                    str(pcm)], check=True)
    aiff.unlink(missing_ok=True)
    return pcm


async def replay(kind: str) -> bool:
    pcm = fixture(kind).read_bytes()
    seconds = len(pcm) / 32000
    print(f"喂入 {kind} 样例 {seconds:.1f} 秒，按实时速度发送\n")
    first_src = first_dst = None
    turns: list[dict] = []
    async with websockets.connect(WS, max_size=None) as ws:
        t0 = time.monotonic()

        async def feed():
            for i in range(0, len(pcm), CHUNK):
                await ws.send(pcm[i:i + CHUNK])
                await asyncio.sleep(0.1)
            # 真实会议里说完话音频还在走，补四秒静音让模型把尾巴吐完
            for _ in range(40):
                await ws.send(b"\x00" * CHUNK)
                await asyncio.sleep(0.1)
            await ws.send(json.dumps({"type": "stop"}))

        task = asyncio.create_task(feed())
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=12))
                dt = time.monotonic() - t0
                kind_ = msg.get("type")
                if kind_ == "status":
                    print(f"[{dt:5.2f}s] 状态 {msg.get('state')}"
                          f"{' 已续接' if msg.get('resumed') else ''}")
                elif kind_ == "meeting":
                    print(f"[{dt:5.2f}s] 会议 #{msg.get('meetingId')}，"
                          f"术语替换规则 {msg.get('glossarySize')} 条")
                elif kind_ == "partial":
                    if msg.get("src") and first_src is None:
                        first_src = dt
                    if msg.get("dst") and first_dst is None:
                        first_dst = dt
                    print(f"[{dt:5.2f}s] 进行中 原话={msg['src'][-46:]!r} 译文={msg['dst'][-30:]!r}")
                elif kind_ == "turn":
                    turns.append(msg)
                    print(f"[{dt:5.2f}s] 段落收口 #{msg['turnId']}，{len(msg['pairs'])} 组句对")
                    for p in msg["pairs"]:
                        print(f"          原话：{p['src']}")
                        if not msg.get("echo") and p["dst"]:
                            print(f"          译文：{p['dst']}")
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    print(f"\n首次原话上屏 {first_src if first_src else -1:.2f}s，"
          f"首次译文上屏 {first_dst if first_dst else -1:.2f}s，收口 {len(turns)} 段")
    ok = bool(turns) and first_src is not None and first_dst is not None
    print("结果：" + ("通过" if ok else "不通过，没有拿到完整的原话与译文"))
    return ok


async def draft_test() -> bool:
    print("\n=== 拟稿接口 ===")
    body = {"instruction": "针对对方最后这段话，帮我拟一段英文回复，暂不接受扩大保证范围。",
            "history": [], "quality": "fast"}
    t0 = time.monotonic()
    first = None
    text = ""
    async with httpx.AsyncClient(timeout=90) as client:
        async with client.stream("POST", f"{BASE}/api/draft", json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                obj = json.loads(payload)
                if err := obj.get("error"):
                    print("失败：", err)
                    return False
                if first is None:
                    first = time.monotonic() - t0
                text += obj.get("text", "")
    print(f"首字 {first if first else -1:.2f}s，全文 {time.monotonic() - t0:.2f}s\n")
    print(text.strip())
    ok = "---ZH---" in text and len(text) > 40
    print("\n结果：" + ("通过" if ok else "不通过，输出里没有中英分隔"))
    return ok


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zh", action="store_true", help="用中文样例")
    ap.add_argument("--draft", action="store_true", help="只测拟稿接口")
    args = ap.parse_args()
    if args.draft:
        return 0 if await draft_test() else 1
    return 0 if await replay("zh" if args.zh else "en") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
