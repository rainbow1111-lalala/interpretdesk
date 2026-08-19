"""清空底稿之后不许有残留。全程指向临时目录，不碰线上 data/。

验四处：磁盘原文与索引、内存摘要、内存预取片段、正在跑的字幕流水线里的术语表。
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

PROJ = "/Users/rainbow/AIwork/03-AI项目/meeting-interpreter"
sys.path.insert(0, PROJ)

from server import context_store, retrieval
from server import main as srv
from server.segmenter import TurnBuilder

LIVE = Path(PROJ) / "data"
before_live = sorted(p.name for p in (LIVE / "docs").glob("*.txt"))

tmp = Path(tempfile.mkdtemp(prefix="mi-clear-"))
retrieval.DOCS_DIR = tmp / "docs"
retrieval.TRASH_DIR = tmp / "trash"
retrieval.INDEX_PATH = tmp / "index.json"
context_store.BRIEF_PATH = tmp / "context.json"


async def main() -> int:
    ok = True
    emitted: list[dict] = []

    async def emit(p):
        emitted.append(p)

    # 摆出一场会开到一半的样子：底稿在、索引在、片段预取好了、字幕流水线拿着术语表
    srv.state.context = context_store.MeetingContext(
        matter="上一场会",
        terms=[{"en": "escrow", "zh": "托管账户", "variants": ["代管账户"]}],
    )
    context_store.save(srv.state.context)
    retrieval.store_doc("上一场底稿", "上一场会的原文内容")
    retrieval.INDEX_PATH.write_text('{"model":"x","entries":[{"doc":"上一场底稿"}]}',
                                    encoding="utf-8")
    srv.state.excerpts = [{"doc": "上一场底稿", "text": "上一场会的片段"}]

    builder = TurnBuilder(emit, srv.state.context.glossary(), cumulative=True)
    srv.state.builder = builder

    # 清空前：术语表确实在改字幕
    await builder.add("dst", "这笔钱进代管账户", "zh")
    before = emitted[-1]["dst"]
    assert before == "这笔钱进托管账户", before
    print("清空前 字幕按底稿术语表校正 →", before)
    await builder.close(final=True)

    # 点「清空全部底稿」
    info = await srv.clear_context()

    checks = {
        "磁盘原文已清空": info["docs"] == [],
        "原文索引已删除": not retrieval.INDEX_PATH.exists(),
        "底稿摘要已清空": info["matter"] == "" and info["glossarySize"] == 0,
        "内存预取片段已清空": srv.state.excerpts == [],
        "原文挪进了回收站": len(list(tmp.joinpath("trash").rglob("*.txt"))) == 1,
    }

    # 清空后：字幕流水线不许再按已删底稿的术语表改字
    emitted.clear()
    await builder.add("dst", "这笔钱进代管账户", "zh")
    after = emitted[-1]["dst"]
    checks["字幕流水线术语表已换掉"] = after == "这笔钱进代管账户"
    print("清空后 字幕不再被旧术语表改动 →", after)

    for name, passed in checks.items():
        print(("  ok  " if passed else "  FAIL") + " " + name)
        ok &= passed

    after_live = sorted(p.name for p in (LIVE / "docs").glob("*.txt"))
    ok &= after_live == before_live
    print("线上 data/docs 未受影响 →", after_live == before_live)

    shutil.rmtree(tmp, ignore_errors=True)
    print("结果：" + ("通过" if ok else "不通过"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
