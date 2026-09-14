"""拟稿的依据与待确认项：解析模型给的引用，逐条拿原文核对。

为什么要程序核对：模型说「根据底稿第三段」时，它可能真的在引用，也可能在编造一个听起来
像出处的说法。会议现场没有时间回去翻原文，所以由程序做这件事——编号是服务端在拼提示词时
发下去的，原文服务端自己留着，核对就是拿模型抄回来的那句话去原文里找。

判定只有一种：归一化之后的精确子串包含。不做模糊匹配、不做编辑距离、不设相似度阈值。
这个功能的全部价值就在「程序核对」四个字上，任何阈值都是把「像不像」的判断塞回代码里；
模型改写过的句子按定义就不是原文摘录，如实标出来比放宽阈值有用。

核不过的不丢弃，一律转进待确认并写明原因。丢弃会让模型看起来永远正确，而用户恰恰需要
知道助手刚才凭空断言了一句。
"""
from __future__ import annotations

import re
import unicodedata

# 模型输出的四段结构，顺序固定
REPLY_MARK = "---ZH---"
REF_MARK = "---依据---"
TODO_MARK = "---待确认---"

# 归一化之后短于这个长度的摘录没法核对，一两个词在长文里必然命中，等于没核
MIN_QUOTE = 8

_SPACE = re.compile(r"\s+")
# 中文句号、顿号一类在 NFKC 下不会折成半角，模型抄原文时中英标点混用很常见，
# 索性把标点整个去掉再比。摘录至少 MIN_QUOTE 字，去掉标点也不会误判成命中。
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]", re.UNICODE)
# 形如 [E3] “原文摘录” — 用途，或 E3 | 原文摘录
_ITEM = re.compile(r"^[\-*\s]*\[?([A-Z]\d{1,3})\]?\s*[|｜:：]?\s*(.+)$")


def normalise(text: str) -> str:
    """把全角半角、空白、标点的差别抹平再比对。

    模型抄原文时常把英文直引号换成中文弯引号、把句号写成半角、在中英之间加空格。
    这些都不算改写，抹平了再比才不会把真引用判成假的。字母数字与汉字一个不动，
    所以改了词、换了数字、调了语序，一样会被判成核不过。
    """
    text = unicodedata.normalize("NFKC", text)
    text = _SPACE.sub("", text)
    text = _PUNCT.sub("", text)
    return text.casefold()


def split_sections(full: str) -> tuple[str, str, str]:
    """把模型输出切成正文（含中文对照）、依据区、待确认区。"""
    body, ref, todo = full, "", ""
    if REF_MARK in body:
        body, rest = body.split(REF_MARK, 1)
        ref = rest
        if TODO_MARK in ref:
            ref, todo = ref.split(TODO_MARK, 1)
    elif TODO_MARK in body:
        body, todo = body.split(TODO_MARK, 1)
    return body, ref, todo


def _split_quote(raw: str) -> tuple[str, str]:
    """拆出摘录与后面那句用途说明。

    优先认引号，因为摘录里本来就可能带破折号；没有引号时才退回按破折号切。
    """
    for left, right in (("“", "”"), ("「", "」"), ('"', '"')):
        if left in raw and right in raw[raw.index(left) + 1:]:
            start = raw.index(left) + 1
            end = raw.index(right, start)
            return raw[start:end], raw[end + 1:].strip(" —–-|｜")
    parts = re.split(r"\s+[—–-]{1,2}\s+", raw, maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def parse_and_verify(ref_text: str, todo_text: str,
                     sources: dict[str, dict]) -> dict:
    """核对依据区的每一条，返回给前端的结构。

    sources 是拼提示词时服务端自己编的 {编号: {"text": 原文, "doc": 出处}}，
    不依赖模型回传的原文。
    """
    verified: list[dict] = []
    pending: list[dict] = []

    for line in ref_text.splitlines():
        line = line.strip()
        if not line or line in ("无", "（无）", "-", "—"):
            continue
        m = _ITEM.match(line)
        if not m:
            continue
        tag, rest = m.group(1), m.group(2).strip()
        quote, note = _split_quote(rest)
        item = {"tag": tag, "quote": quote, "note": note}
        if tag not in sources:
            pending.append({**item, "reason": f"[{tag}] 不在本次提供的材料里，无法核对"})
            continue
        norm_quote = normalise(quote)
        if len(norm_quote) < MIN_QUOTE:
            pending.append({**item, "reason": f"[{tag}] 的摘录太短，无法核对"})
            continue
        if norm_quote in normalise(sources[tag].get("text", "")):
            verified.append({**item, "doc": sources[tag].get("doc", "")})
        else:
            pending.append({**item,
                            "reason": f"[{tag}] 的原文里找不到这句话，可能是改写过的"})

    todo = [line.strip(" -*·　") for line in todo_text.splitlines()
            if line.strip(" -*·　") and line.strip(" -*·　") not in ("无", "（无）")]
    return {"verified": verified, "pending": pending, "todo": todo}

