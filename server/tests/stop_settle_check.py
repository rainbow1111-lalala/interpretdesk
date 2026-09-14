"""停止记录之后的收口握手。

实战风险：用户点停止，前端立刻关连接并弹出纪要，而语音服务还在吐最后一句。实测百炼的
原话比译文晚 6.6 秒到，原来固定睡 3.5 秒就收摊，尾句直接丢掉，纪要里少一段没人发现。

这里不碰真语音服务，用替身把两种情形都跑一遍：正常收到结束信号，以及结束信号永远不来。

    .venv/bin/python -m server.tests.stop_settle_check
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from server import config, llm, retrieval  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="mi-settle-"))
config.SESSIONS_DIR = tmp / "sessions"
# 把上限压小，脚本才跑得快；比例与线上一致（安静窗约为上限的六分之一）
config.SETTLE_MAX_S = 1.5
config.SETTLE_QUIET_S = 0.3
LIVE_DOCS = PROJ / "data" / "docs"
_before_live = sorted(p.name for p in LIVE_DOCS.glob("*.txt")) if LIVE_DOCS.exists() else []


async def fake_stream(*a, **k):
    yield '{"matter": "", "parties": [], "issues": [], "terms": [], "my_position": ""}'


async def fake_rebuild(*a, **k):
    return 0


llm.stream = fake_stream
retrieval.rebuild_index = fake_rebuild

from fastapi.testclient import TestClient  # noqa: E402
from server import main, qwen_live, store  # noqa: E402

ok = True


def check(label: str, cond: bool, extra: str = "") -> None:
    global ok
    ok = ok and bool(cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{('　' + extra) if extra else ''}")


class FakeLive:
    """假语音客户端：停止之后才吐最后一句，模拟原话滞后。

    tail_delay 是停止后多久吐尾句；announce=False 表示永远不发结束信号，用来验超时兜底。
    """

    def __init__(self, emit, engine=None, model="", target_lang="zh",
                 tail_delay: float = 0.25, announce: bool = True):
        self.emit = emit
        self.tail_delay = tail_delay
        self.announce = announce
        self.finished = asyncio.Event()
        self._stop = asyncio.Event()
        self.audio_tokens = 0
        self.response_tokens = 0

    def feed(self, pcm: bytes) -> None:
        pass

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self._stop.wait()
        await asyncio.sleep(self.tail_delay)
        await self.emit({"type": "src", "text": "这是停止之后才到的最后一句",
                         "lang": "zh", "commit": True})
        await self.emit({"type": "dst", "text": "the tail sentence",
                         "lang": "en", "commit": True})
        await self.emit({"type": "turn_complete"})
        if self.announce:
            self.finished.set()
        else:
            await asyncio.sleep(30)


def use_fake(**kw):
    def build(emit, engine=None, model="", target_lang="zh"):
        return FakeLive(emit, engine, model, target_lang, **kw)
    qwen_live.QwenLiveClient = build


def prepare(c: TestClient) -> int:
    c.get("/api/health")
    c.post("/api/settings", json={"text": {"base_url": "https://test.example/v1",
                                           "api_key": "sk-test", "model": "fake"},
                                  "speech": {"base_url": "https://test.example",
                                             "api_key": "sk-test", "model": "fake"}})
    return c.post("/api/meetings", json={"title": "虚构会议"}).json()["meetingId"]


def drain(sock, limit: float = 6.0) -> list[dict]:
    """收到 ended 为止。"""
    got = []
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        msg = sock.receive_json()
        got.append(msg)
        if msg.get("type") == "ended":
            break
    return got


def main_check() -> int:
    print("一、正常收到结束信号")
    use_fake(tail_delay=0.25, announce=True)
    c = TestClient(main.app)
    mid = prepare(c)
    with c.websocket_connect("/ws/live") as sock:
        check("连上就告知是哪一场", sock.receive_json().get("meetingId") == mid)
        sock.send_bytes(b"\x00" * 3200)
        sock.send_json({"type": "stop"})
        got = drain(sock)
    kinds = [m.get("type") for m in got]
    states = [m.get("state") for m in got if m.get("type") == "status"]
    ended = next(m for m in got if m["type"] == "ended")
    check("收尾期间告诉前端正在收尾", "settling" in states, str(states))
    check("停止之后到的那一句仍然上了屏", any(m.get("type") == "turn" for m in got))
    check("ended 是最后一条", kinds[-1] == "ended")
    check("ended 说明已确认收尾", ended["settled"] is True)

    s = main._sessions[c.cookies.get(main.SESSION_COOKIE)]
    turns = store.get_turns(s.sws, mid)
    check("尾句已落库", len(turns) == 1 and "最后一句" in turns[0]["src"],
          str([t["src"][:12] for t in turns]))
    check("ended 报的条数与库一致", ended["turns"] == len(turns), str(ended["turns"]))

    print("二、结束信号永远不来")
    use_fake(tail_delay=0.25, announce=False)
    c2 = TestClient(main.app)
    mid2 = prepare(c2)
    began = time.monotonic()
    with c2.websocket_connect("/ws/live") as sock:
        sock.receive_json()
        sock.send_json({"type": "stop"})
        got2 = drain(sock, limit=config.SETTLE_MAX_S + 4)
    spent = time.monotonic() - began
    ended2 = next(m for m in got2 if m["type"] == "ended")
    check("超时也照样发 ended", ended2["type"] == "ended")
    check("并且标明没等到确认", ended2["settled"] is False)
    check("等待没有超过上限太多", spent < config.SETTLE_MAX_S + 3, f"{spent:.1f} 秒")
    s2 = main._sessions[c2.cookies.get(main.SESSION_COOKIE)]
    check("尾句仍然落了库", len(store.get_turns(s2.sws, mid2)) == 1)

    print("三、录音期间不许出纪要")
    use_fake(tail_delay=0.1, announce=True)
    c3 = TestClient(main.app)
    mid3 = prepare(c3)
    with c3.websocket_connect("/ws/live") as sock:
        sock.receive_json()
        r = c3.post(f"/api/meetings/{mid3}/minutes")
        check("录音中回 409", r.status_code == 409, r.json().get("detail", "")[:20])
        sock.send_json({"type": "stop"})
        drain(sock)
    r = c3.post(f"/api/meetings/{mid3}/minutes")
    check("收尾之后不再是 409", r.status_code != 409, str(r.status_code))

    print("四、同一浏览器只允许一路录音")
    use_fake(tail_delay=0.1, announce=True)
    c4 = TestClient(main.app)
    prepare(c4)
    with c4.websocket_connect("/ws/live") as sock:
        sock.receive_json()
        with c4.websocket_connect("/ws/live") as sock2:
            second = sock2.receive_json()
        check("第二路被拒", second.get("code") == "busy", str(second)[:60])
        sock.send_json({"type": "stop"})
        drain(sock)
    with c4.websocket_connect("/ws/live") as sock3:
        again = sock3.receive_json()
        check("第一路结束后可以再录", again.get("type") == "meeting", str(again)[:60])
        sock3.send_json({"type": "stop"})
        drain(sock3)

    print("五、没有会议时不许录")
    c5 = TestClient(main.app)
    c5.get("/api/health")
    with c5.websocket_connect("/ws/live") as sock:
        check("被拒并说明原因",
              sock.receive_json().get("code") == "no_meeting")

    after = sorted(p.name for p in LIVE_DOCS.glob("*.txt")) if LIVE_DOCS.exists() else []
    check("线上 data/docs 未受影响", after == _before_live)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        code = main_check()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("结果：" + ("通过" if code == 0 else "不通过"))
    sys.exit(code)
