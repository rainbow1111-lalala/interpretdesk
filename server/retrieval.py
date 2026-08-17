"""底稿原文的存放与检索。

上传的文件抽成纯文本后留在 data/docs/，一是让上传变成累加而不是替换，二是让原文在会议中可查。
检索走向量：切块、算向量、存本机，拟稿时按最近对话与我的要求取最相关的几块塞进提示词。

原文与检索并用而不是二选一：拟稿时先把原文按 context_full_chars 直接放进上下文，装不下的部分
再由检索补相关片段。上限存在的原因是首字延迟，实测一万九千字全文进上下文时首字 1.8 到 3.3 秒。

为什么不引向量数据库：几百个块的余弦相似度纯 Python 算一遍是毫秒级，不值得为它加依赖。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from pathlib import Path

from . import config, llm
from .settings import Engine

log = logging.getLogger(__name__)

DOCS_DIR = config.DATA_DIR / "docs"
INDEX_PATH = config.DATA_DIR / "index.json"
TRASH_DIR = config.DATA_DIR / "trash"

CHUNK_CHARS = 700
CHUNK_OVERLAP = 120
EMBED_BATCH = 10
# 索引的块数上限。一份三百万字的文件会切出四千多块，那是几千次向量调用，时间和费用都不该
# 这么花。会前底稿用不到那么多，超出就截断并在日志里说清楚。
MAX_INDEX_CHUNKS = 800


def _safe_name(name: str) -> str:
    stem = re.sub(r"[^\w一-鿿.\- ]", "_", Path(name).name).strip()
    return (stem or "未命名") + (".txt" if not stem.endswith(".txt") else "")


def store_doc(name: str, text: str) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / _safe_name(name)).write_text(text, encoding="utf-8")


def list_docs() -> list[dict]:
    if not DOCS_DIR.exists():
        return []
    return sorted(
        ({"name": p.stem, "chars": len(p.read_text(encoding="utf-8", errors="replace"))}
         for p in DOCS_DIR.glob("*.txt")),
        key=lambda d: d["name"])


def _recycle(paths: list[Path]) -> Path | None:
    """删掉的原文挪进 data/trash/<时间戳>/，不真删。

    会前底稿是手工整理出来的，一次误点清空、或者哪个脚本顺手发一条 DELETE，就再也找不回来。
    挪一下几乎不花时间，换来的是随时能取回。索引不用挪，它由原文重建。
    """
    items = [p for p in paths if p.exists()]
    if not items:
        return None
    bin_dir = TRASH_DIR / time.strftime("%Y%m%d-%H%M%S")
    bin_dir.mkdir(parents=True, exist_ok=True)
    for p in items:
        target = bin_dir / p.name
        # 同一秒内删两次同名文件时不要互相覆盖
        seq = 1
        while target.exists():
            target = bin_dir / f"{p.stem}-{seq}{p.suffix}"
            seq += 1
        p.rename(target)
    log.info("%d 份原文挪进 %s，需要时可取回", len(items), bin_dir)
    return bin_dir


def remove_doc(name: str) -> bool:
    path = DOCS_DIR / _safe_name(name)
    if path.exists():
        _recycle([path])
        INDEX_PATH.unlink(missing_ok=True)
        return True
    return False


def clear_docs() -> Path | None:
    """清空已存原文，返回这批文件挪去了哪个回收站目录。"""
    bin_dir = _recycle(sorted(DOCS_DIR.glob("*.txt"))) if DOCS_DIR.exists() else None
    INDEX_PATH.unlink(missing_ok=True)
    return bin_dir


def all_text(limit: int = 120_000) -> str:
    """所有已存原文拼在一起，供底稿提炼。"""
    blocks = []
    for p in sorted(DOCS_DIR.glob("*.txt")) if DOCS_DIR.exists() else []:
        blocks.append(f"【{p.stem}】\n{p.read_text(encoding='utf-8', errors='replace')}")
    return "\n\n".join(blocks)[:limit]


def chunk(text: str) -> list[str]:
    """按段落攒到七百字上下切一块，块间留一点重叠，免得把一个条款切成两半都读不懂。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        if len(buffer) + len(para) + 1 <= CHUNK_CHARS:
            buffer = f"{buffer}\n{para}" if buffer else para
            continue
        if buffer:
            chunks.append(buffer)
            buffer = buffer[-CHUNK_OVERLAP:] + "\n" + para if len(para) < CHUNK_CHARS else para
        else:
            # 单段就超长，硬切
            for i in range(0, len(para), CHUNK_CHARS - CHUNK_OVERLAP):
                chunks.append(para[i:i + CHUNK_CHARS])
            buffer = ""
    if buffer:
        chunks.append(buffer)
    return [c for c in chunks if len(c.strip()) >= 20]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def rebuild_index(engine: Engine, model: str) -> int:
    """把所有原文切块算向量存下来。上传或删除文件之后调一次。"""
    entries: list[dict] = []
    for path in sorted(DOCS_DIR.glob("*.txt")) if DOCS_DIR.exists() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        for piece in chunk(text):
            entries.append({"doc": path.stem, "text": piece})
    if not entries:
        INDEX_PATH.unlink(missing_ok=True)
        return 0
    if len(entries) > MAX_INDEX_CHUNKS:
        log.warning("原文切出 %d 块，超过上限 %d，只索引前面的部分。"
                    "底稿太长时建议先删掉用不上的文件。", len(entries), MAX_INDEX_CHUNKS)
        entries = entries[:MAX_INDEX_CHUNKS]

    # 批次并行。串行算一百多块要十几秒，会前等这一下没必要；并发压到 4 是给服务端留余量。
    batches = [entries[i:i + EMBED_BATCH] for i in range(0, len(entries), EMBED_BATCH)]
    gate = asyncio.Semaphore(4)

    async def fill(batch: list[dict]) -> None:
        async with gate:
            vectors = await llm.embed(engine, model, [e["text"] for e in batch])
        for entry, vector in zip(batch, vectors):
            entry["vec"] = vector

    await asyncio.gather(*(fill(b) for b in batches))

    entries = [e for e in entries if e.get("vec")]
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps({"model": model, "entries": entries},
                                     ensure_ascii=False), encoding="utf-8")
    log.info("底稿索引建好：%d 块，模型 %s", len(entries), model)
    return len(entries)


def index_size() -> int:
    if not INDEX_PATH.exists():
        return 0
    try:
        return len(json.loads(INDEX_PATH.read_text(encoding="utf-8")).get("entries", []))
    except Exception:
        return 0


async def search(engine: Engine, model: str, query: str, k: int = 3) -> list[dict]:
    """按相似度取最相关的几块原文。索引不存在就返回空，不报错，拟稿照常走摘要。"""
    if not INDEX_PATH.exists() or not query.strip():
        return []
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    entries = data.get("entries") or []
    if not entries:
        return []
    vectors = await llm.embed(engine, data.get("model") or model, [query[:2000]])
    if not vectors:
        return []
    probe = vectors[0]
    scored = [(_cosine(probe, e["vec"]), e) for e in entries if e.get("vec")]
    scored.sort(key=lambda pair: -pair[0])
    return [{"doc": e["doc"], "text": e["text"], "score": round(score, 3)}
            for score, e in scored[:k]]
