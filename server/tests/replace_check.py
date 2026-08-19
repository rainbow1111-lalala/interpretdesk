"""换底稿三情形验证。全程指向临时目录，不碰线上 data/。"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

PROJ = "/Users/rainbow/AIwork/03-AI项目/meeting-interpreter"
sys.path.insert(0, PROJ)

from server import config, context_store, extract, llm, retrieval

tmp = Path(tempfile.mkdtemp(prefix="mi-replace-"))
WS = config.Workspace(tmp / "ws")

# 会话独立之后设置也是每人一份，测试工作区要自己配一个假模型，否则提炼会因没配模型而中止
from server import settings as settings_mod  # noqa: E402
settings_mod.save(WS, settings_mod.Settings(), {
    "text": {"base_url": "https://test.example/v1", "api_key": "sk-test", "model": "m"}})

LIVE_DOCS = Path(PROJ) / "data" / "docs"
before_live = sorted(p.name for p in LIVE_DOCS.glob("*.txt"))


async def fake_stream(*a, **k):
    for piece in ['{"matter":"测试","parties":[],"issues":[],', '"terms":[],"my_position":""}']:
        yield piece


async def fake_rebuild(ws, *a, **k):
    return 0


llm.stream = fake_stream
retrieval.rebuild_index = fake_rebuild

src = tmp / "src"
src.mkdir()


def mkfile(name: str, body: str) -> Path:
    p = src / name
    p.write_text(body, encoding="utf-8")
    return p


def docs() -> list[str]:
    return sorted(d["name"] for d in retrieval.list_docs(WS))


def trashed() -> list[str]:
    return sorted(p.name for p in WS.trash.rglob("*.txt")) if WS.trash.exists() else []


async def main() -> int:
    ok = True

    # 情形一：首次上传两份（累加模式，无旧底稿）
    await context_store.build(WS, [mkfile("甲方尽调要点.txt", "甲方内容" * 20),
                                   mkfile("法规摘录.txt", "法规内容" * 20)], "")
    assert docs() == ["法规摘录", "甲方尽调要点"], docs()
    print("情形一 首次上传两份 →", docs())

    # 情形二：同一场会补材料（追加）
    await context_store.build(WS, [mkfile("补充邮件.txt", "邮件内容" * 20)], "", replace=False)
    got = docs()
    ok &= got == ["law", "补充邮件", "法规摘录", "甲方尽调要点"][1:] or got == [
        "法规摘录", "甲方尽调要点", "补充邮件"]
    print("情形二 追加一份 →", got, "（三份都在）" if len(got) == 3 else "【异常】")
    ok &= len(got) == 3

    # 情形三：换一场会（替换）
    await context_store.build(WS, [mkfile("新会议提纲.txt", "新会内容" * 20)], "", replace=True)
    got = docs()
    print("情形三 换新底稿 →", got, "｜回收站：", trashed())
    ok &= got == ["新会议提纲"]
    ok &= len(trashed()) == 3

    # 情形四：换新时唯一的文件读不出来，旧底稿必须原样保住
    bad = src / "坏文件.txt"
    bad.write_text("x", encoding="utf-8")
    real_extract = extract.extract

    def boom(p):
        if p.name == "坏文件.txt":
            raise extract.ExtractError("这份读不出来")
        return real_extract(p)

    extract.extract = boom
    ctx = await context_store.build(WS, [bad], "", replace=True)
    got = docs()
    print("情形四 换新但文件读不出 →", got, "｜失败提示：",
          [s for s in ctx.sources if "读不出来" in s])
    ok &= got == ["新会议提纲"]
    extract.extract = real_extract

    # 线上底稿一根汗毛都没动
    after_live = sorted(p.name for p in LIVE_DOCS.glob("*.txt"))
    ok &= after_live == before_live
    print("线上 data/docs 未受影响 →", after_live == before_live, after_live)

    shutil.rmtree(tmp, ignore_errors=True)
    print("结果：" + ("通过" if ok else "不通过"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
