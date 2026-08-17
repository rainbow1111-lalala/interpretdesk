"""会前背景：把上传的文件抽成文本，提炼一份会议底稿，并生成术语锁定表。

术语锁定用确定性替换，不交给模型现场发挥。Live Translate 那条流不接受文字指令，没法把术语
表喂进去，所以译文出来之后再按表校正，这是目前唯一能管住当事人名称与法律术语译法的办法。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import config, extract, llm, retrieval, settings as settings_mod

log = logging.getLogger(__name__)

BRIEF_PATH = config.DATA_DIR / "context.json"
# 提炼时喂给强档模型的上限。四份底稿合计十四万字是真实体量，留出余量。
MAX_DOC_CHARS = 200_000

BRIEF_PROMPT = """你在为一名中国律师准备一场与外国律师的英文会议。下面是会前背景文件全文。

请输出严格的 JSON，字段如下：
{
  "matter": "一句话说明这是什么事项",
  "parties": [{"en": "英文名称", "zh": "中文名称", "role": "在本事项中的角色"}],
  "issues": ["本次会议可能讨论的争点，每条一句"],
  "terms": [{"en": "英文术语或专有名词", "zh": "应当采用的中文译法", "variants": ["模型可能误译成的其他中文说法"]}],
  "my_position": "我方立场与底线，若文件未体现则写空字符串"
}

terms 只收对译法敏感的条目：当事人名称、协议定义术语、法律术语（如 representations and
warranties、indemnity、material adverse change）、金额与条款编号的表达习惯。variants 要尽量写全
机器翻译可能吐出的其他中文说法，至少四条，把同义词逐个换过来，例如 representations and
warranties 的 zh 是"陈述与保证"，variants 就写 ["声明和保证", "陈述和担保", "声明与担保",
"陈述及保证", "表述和保证"]。不必列举只有连词不同的写法（"和""与""及"互换的情形程序会自动处理）。
variants 里不要写包含在 zh 里面的短词，例如 zh 是"第三方托管"时不要写"托管"。

只输出 JSON，不要解释，不要代码块围栏。

背景文件全文：
---
{doc}
---"""


_DOUBLET = re.compile(r"([^和与及]+)[和与及]([^和与及]+)")


def expand_doublets(preferred: str, variants: list[str]) -> set[str]:
    """把并列短语的两个槽位做叉乘。

    中文法律里的并列短语几乎都是「前项＋连词＋后项」，模型给的变体只会覆盖其中几种组合。
    把出现过的前项与后项各自收齐再两两相配，「陈述与保证」「声明和担保」这类写法就都能收进来。
    连词写哪个不重要，匹配时三个连词等价。
    """
    lefts, rights = set(), set()
    for form in [preferred, *variants]:
        m = _DOUBLET.fullmatch((form or "").strip())
        if m:
            lefts.add(m.group(1))
            rights.add(m.group(2))
    if len(lefts) * len(rights) <= 1:
        return set()
    return {f"{left}和{right}" for left in lefts for right in rights}


@dataclass
class MeetingContext:
    matter: str = ""
    parties: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    terms: list[dict] = field(default_factory=list)
    my_position: str = ""
    sources: list[str] = field(default_factory=list)
    raw_excerpt: str = ""

    def glossary(self) -> dict[str, str]:
        """{可能的误译: 应采用的译法}，供译文事后校正。"""
        table: dict[str, str] = {}
        for t in self.terms:
            zh = (t.get("zh") or "").strip()
            if not zh:
                continue
            variants = [(v or "").strip() for v in (t.get("variants") or [])]
            for v in set(variants) | expand_doublets(zh, variants):
                # 变体是首选译法的一部分时不能替换，否则「托管」会被改成
                # 「第三方托管」，而原文本来就写着「第三方托管」，结果套成两层
                if v and v != zh and v not in zh:
                    table[v] = zh
        for p in self.parties:
            zh, en = (p.get("zh") or "").strip(), (p.get("en") or "").strip()
            if zh and en and en != zh:
                table.setdefault(en, zh)
        # 长的先替换，避免短词把长词切坏
        return dict(sorted(table.items(), key=lambda kv: -len(kv[0])))

    def briefing_text(self) -> str:
        # 我方立场放最前面。放在末尾时模型会照着争点的中立表述写，把立场说反。
        lines = []
        if self.my_position:
            lines.append(f"我方立场与底线（一切表达以此为准）：{self.my_position}")
        if self.matter:
            lines.append(f"事项：{self.matter}")
        if self.parties:
            lines.append("当事人：" + "；".join(
                f"{p.get('en','')}（{p.get('zh','')}，{p.get('role','')}）" for p in self.parties))
        if self.issues:
            lines.append("争点：" + "；".join(self.issues))
        if self.terms:
            lines.append("术语译法：" + "；".join(
                f"{t.get('en','')}＝{t.get('zh','')}" for t in self.terms))
        return "\n".join(lines)


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


async def build(paths: list[Path], extra_note: str = "") -> MeetingContext:
    """把新文件并进已存原文，再对全部原文重新提炼一次底稿。

    上传是累加而不是替换：原文留在 data/docs/，每次上传只添新的，旧的还在。要去掉某一份走
    删除接口。
    """
    failed = []
    for p in paths:
        try:
            # 抽文本是同步的，几十页 PDF 加 OCR 能卡住整个事件循环，扔线程里跑
            text, method = await asyncio.to_thread(extract.extract, p)
            text = text.strip()
        except extract.ExtractError as exc:
            failed.append(f"{p.name}（{exc}）")
            continue
        except Exception as exc:
            log.exception("抽取失败 %s", p.name)
            failed.append(f"{p.name}（读取失败：{type(exc).__name__}）")
            continue
        if text:
            retrieval.store_doc(p.stem, text)
            log.info("入库 %s：%s，%d 字", p.name, method, len(text))
        else:
            failed.append(f"{p.name}（没抽到文字）")
    if extra_note.strip():
        retrieval.store_doc("口述补充", extra_note.strip())

    doc = retrieval.all_text(MAX_DOC_CHARS)
    names = [d["name"] for d in retrieval.list_docs()] + failed
    if not doc.strip():
        return MeetingContext(sources=names)

    cfg = settings_mod.load()
    if not cfg.text.ready():
        raise RuntimeError("还没配文本模型，先在模型设置里填 base URL 与 model name")
    log.info("底稿提炼开始：%d 字，%d 份原文，模型 %s",
             len(doc), len(retrieval.list_docs()), cfg.strong_model())
    # 走流式。整段 JSON 憋到最后才回时会撞读超时，边收边攒就不会。
    parts: list[str] = []
    async for piece in llm.stream(cfg.text, cfg.strong_model(),
                                  BRIEF_PROMPT.replace("{doc}", doc),
                                  temperature=0.2):
        parts.append(piece)
    raw = "".join(parts)
    log.info("底稿提炼返回 %d 字", len(raw))
    if not raw.strip():
        raise RuntimeError("模型没有返回内容，检查强档 model name 是否支持长文本")
    try:
        data = json.loads(_strip_fence(raw))
    except json.JSONDecodeError:
        data = {}
    ctx = MeetingContext(
        matter=data.get("matter", ""),
        parties=data.get("parties", []) or [],
        issues=data.get("issues", []) or [],
        terms=data.get("terms", []) or [],
        my_position=data.get("my_position", "") or "",
        sources=names,
        raw_excerpt="",
    )
    save(ctx)

    # 原文索引重建放在提炼之后，失败不影响底稿可用，只是会议中查不了原文
    try:
        count = await retrieval.rebuild_index(cfg.text, cfg.embed_model)
        log.info("底稿原文索引 %d 块", count)
    except Exception as exc:
        log.warning("索引没建成：%s：%s", type(exc).__name__, exc)
    return ctx


def save(ctx: MeetingContext) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    BRIEF_PATH.write_text(
        json.dumps(asdict(ctx), ensure_ascii=False, indent=2), encoding="utf-8")


def load() -> MeetingContext:
    if not BRIEF_PATH.exists():
        return MeetingContext()
    try:
        return MeetingContext(**json.loads(BRIEF_PATH.read_text(encoding="utf-8")))
    except Exception:
        return MeetingContext()
