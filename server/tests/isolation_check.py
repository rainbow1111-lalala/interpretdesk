"""会话隔离验收：两个浏览器互相看不到对方的任何东西。

这是多人版的成败所在，串一次号就是泄露别人的客户材料，所以断言写得比别处狠。
全程指向临时目录，不碰线上 data/。用系统 python 运行：

    .venv/bin/python -m server.tests.isolation_check
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from .. import config, llm, retrieval

PROJ = Path(__file__).resolve().parents[2]
LIVE_DOCS = PROJ / "data" / "docs"
_before_live = sorted(p.name for p in LIVE_DOCS.glob("*.txt")) if LIVE_DOCS.exists() else []

tmp = Path(tempfile.mkdtemp(prefix="mi-iso-"))
config.SESSIONS_DIR = tmp / "sessions"


async def _fake_stream(*a, **k):
    yield '{"matter":"某某事项","parties":[],"issues":[],"terms":[],"my_position":""}'


async def _fake_rebuild(ws, *a, **k):
    return 0


llm.stream = _fake_stream
retrieval.rebuild_index = _fake_rebuild

from .. import main  # noqa: E402  必须在打桩之后导入


def upload(client: TestClient, name: str, body: str):
    return client.post("/api/context",
                       files={"files": (name, body.encode("utf-8"), "text/plain")},
                       data={"replace": "false"})


def main_check() -> int:
    ok = True
    checks: dict[str, bool] = {}

    # 两个独立的 cookie 罐，等于两台机器上的两个浏览器
    jia = TestClient(main.app)
    yi = TestClient(main.app)

    # 新会话默认什么都没配，各人先填自己的模型。这本身就是多人版要的：谁也用不了谁的额度
    r = jia.get("/api/context")
    checks["新会话开局是空的"] = r.json()["docs"] == [] and r.json()["matter"] == ""
    for client, who in ((jia, "jia"), (yi, "yi")):
        client.post("/api/settings", json={
            "text": {"base_url": f"https://{who}.example/v1", "api_key": f"sk-{who}",
                     "model": f"m-{who}"}})

    r = upload(jia, "甲的底稿.txt", "甲方客户的并购交易材料，绝密")
    assert r.status_code == 200, r.text
    jia_docs = [d["name"] for d in r.json()["docs"]]
    checks["甲上传成功"] = jia_docs == ["甲的底稿"]

    # 乙这时候什么都不该看见
    r = yi.get("/api/context")
    yi_docs = [d["name"] for d in r.json()["docs"]]
    checks["乙看不到甲的底稿"] = yi_docs == []
    checks["乙拿不到甲的摘要"] = r.json()["matter"] == ""

    # 乙检索也不该命中甲的原文
    r = yi.post("/api/context/search", json={"query": "并购交易", "k": 3})
    checks["乙检索不到甲的原文"] = r.json().get("hits") == []

    r = upload(yi, "乙的底稿.txt", "乙方客户的劳动仲裁材料")
    yi_docs = [d["name"] for d in r.json()["docs"]]
    checks["乙上传成功且只有自己的"] = yi_docs == ["乙的底稿"]

    # 甲回头看，仍然只有自己的
    r = jia.get("/api/context")
    checks["甲不受乙影响"] = [d["name"] for d in r.json()["docs"]] == ["甲的底稿"]

    # 设置与 API key 各存各的
    y = yi.get("/api/settings").json()["settings"]
    checks["乙拿到的是自己的端点"] = y["text"]["base_url"] == "https://yi.example/v1"
    checks["乙看不到甲的模型名"] = y["text"]["model"] == "m-yi"
    j = jia.get("/api/settings").json()["settings"]
    checks["甲自己的设置还在"] = j["text"]["base_url"] == "https://jia.example/v1"
    checks["甲的 key 不回显明文"] = j["text"]["api_key"] == "" and j["text"]["has_key"]

    # 会议库分开
    checks["甲乙会议库互不可见"] = (jia.get("/api/meetings").json() == []
                                    and yi.get("/api/meetings").json() == [])

    # 清空只清自己的
    jia.delete("/api/context")
    checks["甲清空后乙的底稿还在"] = (
        [d["name"] for d in yi.get("/api/context").json()["docs"]] == ["乙的底稿"])

    # 伪造的会话 id 不许拼进路径
    bad = TestClient(main.app)
    bad.cookies.set(main.SESSION_COOKIE, "../../../../etc")
    r = bad.get("/api/context")
    checks["伪造会话 id 被拒并换发"] = (
        r.status_code == 200 and r.json()["docs"] == []
        and "mi_sid" in r.headers.get("set-cookie", ""))
    checks["没有跳出会话根目录"] = not (tmp / "sessions" / ".." / "etc").exists()

    # 上传限额：超大文件要被挡住，且回的是 413 不是 502
    big = b"x" * (config.MAX_UPLOAD_BYTES + 1024)
    r = jia.post("/api/context", files={"files": ("巨大.txt", big, "text/plain")},
                 data={"replace": "false"})
    checks["超限上传被挡且回 413"] = r.status_code == 413
    checks["超限后底稿没被动过"] = (
        [d["name"] for d in jia.get("/api/context").json()["docs"]] == [])

    # 份数上限：一次超过 MAX_SESSION_DOCS 份要被挡住，且不写盘
    many = [("files", (f"第{i}份.txt", b"x" * 32, "text/plain"))
            for i in range(config.MAX_SESSION_DOCS + 1)]
    r = jia.post("/api/context", files=many, data={"replace": "false"})
    checks["超份数上传被挡且回 413"] = r.status_code == 413
    checks["超份数后底稿仍为空"] = (
        [d["name"] for d in jia.get("/api/context").json()["docs"]] == [])

    # 两个会话确实落在不同目录
    dirs = sorted(p.name for p in (tmp / "sessions").iterdir() if p.is_dir())
    checks["磁盘上是两个独立目录"] = len(dirs) == 2

    for name, passed in checks.items():
        print(("  ok  " if passed else "  FAIL") + " " + name)
        ok &= passed

    after = sorted(p.name for p in LIVE_DOCS.glob("*.txt")) if LIVE_DOCS.exists() else []
    ok &= after == _before_live
    print("线上 data/docs 未受影响 →", after == _before_live)

    shutil.rmtree(tmp, ignore_errors=True)
    print("结果：" + ("通过" if ok else "不通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main_check())
