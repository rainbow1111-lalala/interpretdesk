"""把流式碎片攒成可读的双语卡片。

模型吐的源语与译文都是碎片，且两侧碎片不一一对应，所以先按「一段话」攒，段落收口时再
尝试按句配对：源语句数与译文句数相同就逐句配，不同就整段配，不硬凑。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Awaitable, Callable

from . import config

Emit = Callable[[dict], Awaitable[None]]

_EN_SPLIT = re.compile(r"(?<=[.!?])\s+")
_ZH_SPLIT = re.compile(r"(?<=[。！？；])")
_CONJ = re.compile(r"[和与及]")


def split_sentences(text: str, lang: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts = _ZH_SPLIT.split(text) if _is_cjk(lang, text) else _EN_SPLIT.split(text)
    return [p.strip() for p in parts if p.strip()]


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "!", "?", "。", "！", "？"))


def _is_cjk(lang: str, text: str) -> bool:
    if lang.lower().startswith("zh"):
        return True
    return bool(re.search(r"[一-鿿]", text))


def pair_sentences(src: str, dst: str, src_lang: str, dst_lang: str) -> list[dict]:
    s = split_sentences(src, src_lang)
    d = split_sentences(dst, dst_lang)
    if len(s) == len(d) and len(s) > 1:
        return [{"src": a, "dst": b} for a, b in zip(s, d)]
    return [{"src": src.strip(), "dst": dst.strip()}]


def build_replacer(table: dict[str, str]):
    """把术语表编成一次正则替换。

    首选译法本身也进备选项并映射回自己，按长度从长到短排列，正则从左到右一遍扫完。这样
    「第三方托管账户」里先命中「第三方托管」原样保留，不会被变体「托管账户」再套一层，也
    不会出现 A 换成 B、B 又被换成 C 的连锁。
    """
    if not table:
        return lambda text: text
    mapping = {k: v for k, v in table.items() if k and v}
    # 首选译法也参与匹配并映射回自己，顺带借下面的连词等价规则把「陈述和保证」收进
    # 「陈述与保证」，不必指望变体表穷举
    for v in list(mapping.values()):
        mapping.setdefault(v, v)

    parts, repls = [], []
    for i, key in enumerate(sorted(mapping, key=len, reverse=True)):
        # 中文法律短语里「和」「与」「及」互换不改变意思，匹配时按等价处理
        body = _CONJ.sub("[和与及]", re.escape(key))
        # 英文词加词边界，否则 act 会打进 action 这类长词里
        if key[0].isascii() and key[0].isalnum():
            body = r"\b" + body
        if key[-1].isascii() and key[-1].isalnum():
            body = body + r"\b"
        parts.append(f"(?P<g{i}>{body})")
        repls.append(mapping[key])
    pattern = re.compile("|".join(parts))

    def replace(text: str) -> str:
        return pattern.sub(lambda m: repls[int(m.lastgroup[1:])], text)

    return replace


class TurnBuilder:
    """累积当前这一段，到点收口。

    两类引擎的事件语义不同，必须分开处理，否则字幕会重复叠加。Gemini 那条给的是增量片段，
    直接接在后面；百炼那条给的是**从这段话开头累积到当前的全文快照**（实抓原始事件核实过，
    不是「当前一句」），每次要覆盖，收尾事件（commit）一段只来一次，带整段全文。cumulative
    就是这个开关。

    卡片可能在一段话讲完之前就被切走（超长强切、静音收口、暂停），而引擎后续快照仍带整段
    全文。_flushed 记录本段已经随此前卡片上屏的字符数，之后的快照只取未上屏的后缀，否则
    新卡片会把旧译文整段重播一遍。段落真正结束（close(final=True)）时清零。
    """

    def __init__(self, emit: Emit, glossary: dict[str, str] | None = None,
                 cumulative: bool = False):
        self.emit = emit
        self._replace = build_replacer(glossary or {})
        self.cumulative = cumulative
        self.turn_id = 0
        self._done = {"src": "", "dst": ""}
        self._cur = {"src": "", "dst": ""}
        self._flushed = {"src": 0, "dst": 0}
        self._seen = {"src": 0, "dst": 0}
        self.src_lang = ""
        self.dst_lang = ""
        self.first_at = 0.0
        self.last_at = 0.0
        self._lock = asyncio.Lock()

    def set_glossary(self, glossary: dict[str, str]) -> None:
        self._replace = build_replacer(glossary or {})

    def _text(self, side: str) -> str:
        joiner = " " if self._done[side] and self._cur[side] else ""
        return f"{self._done[side]}{joiner}{self._cur[side]}"

    @property
    def src(self) -> str:
        return self._text("src")

    @property
    def dst(self) -> str:
        return self._text("dst")

    @property
    def open(self) -> bool:
        return bool(self.src.strip() or self.dst.strip())

    def _reset(self) -> None:
        self._done = {"src": "", "dst": ""}
        self._cur = {"src": "", "dst": ""}
        self.src_lang = self.dst_lang = ""

    async def add(self, side: str, text: str, lang: str, commit: bool = False) -> None:
        async with self._lock:
            now = time.monotonic()
            if not self.open:
                self.first_at = now
            self.last_at = now
            if self.cumulative:
                if len(text) < self._flushed[side]:
                    # 全文比已上屏的还短，说明引擎已开始新的一段（重连或漏了收尾事件）
                    self._flushed[side] = 0
                self._seen[side] = len(text)
                fresh = text[self._flushed[side]:]
                self._cur[side] = fresh
                if commit:
                    joiner = " " if self._done[side] and fresh else ""
                    self._done[side] = f"{self._done[side]}{joiner}{fresh}"
                    self._cur[side] = ""
            else:
                self._done[side] += text
            if side == "src":
                self.src_lang = lang or self.src_lang
            else:
                self.dst_lang = lang or self.dst_lang
            await self._emit_partial()
            if len(self.src) >= config.TURN_MAX_CHARS:
                await self._close()

    async def _emit_partial(self) -> None:
        # 术语替换放在整段上做，逐片替换会把跨片的术语漏掉
        await self.emit({
            "type": "partial",
            "turnId": self.turn_id,
            "src": self.src.strip(),
            "dst": self._replace(self.dst).strip(),
            "srcLang": self.src_lang,
            "dstLang": self.dst_lang,
            "echo": _is_cjk(self.src_lang, self.src),
        })

    async def close(self, final: bool = False) -> None:
        """final=True 表示这段话真正结束（turn_complete），本段的上屏偏移随之清零；
        False 是中途切卡（超长、静音、暂停），偏移要带到下一张卡，见类注释。"""
        async with self._lock:
            await self._close(final)

    async def _close(self, final: bool = False) -> None:
        if final:
            self._flushed = {"src": 0, "dst": 0}
            self._seen = {"src": 0, "dst": 0}
        else:
            self._flushed = dict(self._seen)
        if not self.open:
            return
        payload = {
            "type": "turn",
            "turnId": self.turn_id,
            "pairs": pair_sentences(self.src, self._replace(self.dst),
                                    self.src_lang, self.dst_lang),
            "srcLang": self.src_lang,
            "dstLang": self.dst_lang,
            "echo": _is_cjk(self.src_lang, self.src),
            "ts": time.time(),
        }
        self._reset()
        self.turn_id += 1
        await self.emit(payload)

    async def watch_idle(self) -> None:
        """静音够久就收口。这一步决定卡片的粒度，比标点更可靠。"""
        while True:
            await asyncio.sleep(0.3)
            if not self.open:
                continue
            idle = time.monotonic() - self.last_at
            # 原话说完了不等于译文吐完了，两侧都收尾才提前收口，否则等硬上限
            ends_clean = _ends_sentence(self.src) and (
                not self.dst.strip() or _ends_sentence(self.dst))
            if idle >= config.TURN_HARD_CLOSE_S or (
                idle >= config.TURN_IDLE_CLOSE_S and ends_clean
            ):
                await self.close()
