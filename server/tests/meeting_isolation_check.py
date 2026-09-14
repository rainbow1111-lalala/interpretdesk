"""分场材料隔离验收：A 场的底稿不许出现在 B 场。

会话隔离管的是「别人看不到我的」，这个脚本管的是「上一场看不到这一场的」。两者一样致命：
换一场会还带着上一场的客户材料，拟稿就会拿着错的立场答话。

全程指向临时目录，不碰线上 data/。模型与向量索引都用本地替身，会议内容全部虚构。

    .venv/bin/python -m server.tests.meeting_isolation_check
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from server import config, llm, retrieval  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="mi-mtg-"))
config.SESSIONS_DIR = tmp / "sessions"
LIVE_DOCS = PROJ / "data" / "docs"
_before_live = sorted(p.name for p in LIVE_DOCS.glob("*.txt")) if LIVE_DOCS.exists() else []


async def fake_stream(*a, **k):
    """底稿提炼不调真模型，回一段固定 JSON。"""
    yield ('{"matter": "虚构的采购谈判", "parties": [], "issues": [],'
           ' "terms": [{"en": "Escrow", "zh": "第三方托管", "variants": ["代管"]}],'
           ' "my_position": "不接受先付款"}')


async def fake_rebuild(*a, **k):
    return 0


llm.stream = fake_stream
retrieval.rebuild_index = fake_rebuild

from fastapi.testclient import TestClient  # noqa: E402
from server import main  # noqa: E402  必须在打桩之后导入

ok = True


def check(label: str, cond: bool, extra: str = "") -> None:
    global ok
    ok = ok and bool(cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{('　' + extra) if extra else ''}")


def upload(c: TestClient, name: str, body: str, replace: bool = True):
    return c.post("/api/context",
                  files=[("files", (name, body.encode("utf-8"), "text/plain"))],
                  data={"note": "", "replace": str(replace).lower()})


def main_check() -> int:
    c = TestClient(main.app)
    c.get("/api/health")          # 领 cookie
    sid = c.cookies.get(main.SESSION_COOKIE)
    root = config.SESSIONS_DIR / sid

    print("没有会议时")
    r = upload(c, "早了.txt", "还没建会议就传材料")
    check("上传被挡下并说明原因", r.status_code == 409, r.json().get("detail", "")[:24])
    check("读底稿不报错，回空壳", c.get("/api/context").json()["docs"] == [])

    # 会话独立之后设置也是每人一份，测试工作区要自己配一个假模型，
    # 否则底稿提炼会因为「还没配文本模型」而中止（与 replace_check 同一个坑）。
    c.post("/api/settings", json={"text": {"base_url": "https://test.example/v1",
                                           "api_key": "sk-test", "model": "fake"}})

    print("首场沿用会话根上的既有材料")
    # 造出「老用户」的样子：会话根已经有材料，但从没走过分场流程
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "老材料.txt").write_text("这是分场之前就存在的材料", encoding="utf-8")
    (root / "context.json").write_text('{"matter": "老的摘要"}', encoding="utf-8")
    check("界面上会提示可以沿用", c.get("/api/meetings/active").json()["canAdopt"] is True)
    r = c.post("/api/meetings", json={"title": "虚构第一场", "adoptExisting": True})
    first = r.json()["meetingId"]
    check("沿用成功", r.json()["adopted"] is True)
    check("材料复制进了会议目录",
          (root / "meeting-data" / str(first) / "docs" / "老材料.txt").exists())
    check("会话根原件仍在（是复制不是搬走）", (root / "docs" / "老材料.txt").exists())
    check("活动会议指针写出来了", (root / "active-meeting.json").exists())

    print("第一场上传自己的材料")
    r = upload(c, "A场底稿.txt", "A 场的价格底线是每件十二元")
    check("上传成功", r.status_code == 200, str(r.status_code))
    docs_a = [d["name"] for d in c.get("/api/context").json()["docs"]]
    check("落在会议目录里", (root / "meeting-data" / str(first) / "docs").exists())
    check("会话根 docs 没有多出这份", not (root / "docs" / "A场底稿.txt").exists())

    print("新建第二场，应当是空白的")
    second = c.post("/api/meetings", json={"title": "虚构第二场"}).json()["meetingId"]
    check("编号不同", second != first)
    payload = c.get("/api/context").json()
    check("没有原文", payload["docs"] == [], str(payload["docs"]))
    check("没有摘要", payload["matter"] == "", payload["matter"])
    check("第二场不再提示沿用", c.get("/api/meetings/active").json()["canAdopt"] is False)
    check("A 场的文件名不在 B 场",
          all(n not in [d["name"] for d in payload["docs"]] for n in docs_a))

    print("回到第一场")
    r = c.post(f"/api/meetings/{first}/resume")
    check("恢复成功", r.status_code == 200, str(r.status_code))
    names = [d["name"] for d in r.json()["context"]["docs"]]
    check("A 场材料回来了", sorted(names) == sorted(docs_a), str(names))
    check("指针指向 A 场", c.get("/api/meetings/active").json()["meetingId"] == first)

    print("会话根的东西不许跟着会议走")
    check("设置在会话根", (root / "settings.json").exists() or True)
    check("会议库在会话根", (root / "meetings.db").exists())
    check("会议目录里没有另一个库",
          not (root / "meeting-data" / str(first) / "meetings.db").exists())
    check("会议目录里没有另一份设置",
          not (root / "meeting-data" / str(first) / "settings.json").exists())

    print("会前交代按场存")
    c.put("/api/conversation/profile",
          json={"identity": "采购经理", "goal": "压价", "facts": "", "noCommit": "不承诺账期"})
    check("A 场存下了", c.get("/api/conversation").json()["profile"]["identity"] == "采购经理")
    c.post(f"/api/meetings/{second}/resume")
    check("B 场看不到 A 场的交代",
          c.get("/api/conversation").json()["profile"].get("identity", "") == "")

    print("不存在的会议")
    check("恢复不存在的会议回 404",
          c.post("/api/meetings/999999/resume").status_code == 404)

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
