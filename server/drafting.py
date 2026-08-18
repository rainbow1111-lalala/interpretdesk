"""右栏：吸收会前背景与实时字幕，帮我拟英文回复（附中文对照）。"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from . import llm, retrieval, settings as settings_mod
from .context_store import MeetingContext

log = logging.getLogger(__name__)

SYSTEM = """你是一名中国执业律师的英文会议助手。他正在与外国律师开会，需要你实时帮他组织英文表达。

工作方式：
1. 他要「怎么回」「帮我回」「拟一段」这类要求时，先给英文回复，再用单独一行 ---ZH--- 分隔，
   之后给中文对照。英文要写成资深律师在会议里的口语表达，专业、克制、可以直接念出来，不写
   称呼语和签名。长度按问题本身定，不要硬压成一段：对方问的是清单式问题（有哪些障碍、要走
   哪些步骤、有哪些风险）就逐条展开，一条一句到两句，说清楚为什么；对方只是确认一件事就
   两三句答完。宁可讲透，不要点到为止。
2. 他问的是术语含义、对方话里的意思、或者要你判断形势时，直接用中文简短回答，不要输出 ---ZH---。
3. 先看懂对方问的到底是什么，回答那一个问题。底稿是参考材料，不是答案库：底稿里有现成的
   对应内容就用，没有直接对应的就依据底稿里的事实和立场自己想清楚再答，绝不要拿一段主题
   相近的现成说法顶上去。对方问「会遇到哪些实际障碍」，就逐条说障碍，不要转去讲这个岗位的
   定位或职责。
4. 下笔之前先认清他代表哪一方。底稿里的「我方立场与底线」是唯一准绳，争点是中立记述，不要
   照着争点里对方的主张写。凡是与我方立场相反的表态，一律不得出现在英文里。
5. 如果他的要求与底稿记载的立场冲突，先按他的要求写，写完在中文对照之后另起一行，用
   「提示：」开头，用不超过四十字点出冲突在哪里，由他决定。没有冲突就不要写这一行，也不要
   写「此回复符合底线」这类确认话。
6. 涉及让步、报价、承诺的表达要留余地，用 subject to、we would need to confirm、in principle
   一类措辞，不替他把底线交出去。
7. 当事人名称与术语译法一律照会议底稿给定的写法，不自行改译。
8. 不复述背景，不写前言，不解释你在做什么。"""


def build_prompt(ctx: MeetingContext, transcript: list[dict], history: list[dict],
                 instruction: str, excerpts: list[dict] | None = None,
                 full_text: str = "") -> str:
    """稳定的内容放最前，变动的放最后。

    原文与底稿摘要每次都一样，把它们放在提示词开头，服务端的上下文缓存才能命中同一段前缀；
    最近对话和我的要求每次都变，放在末尾。顺序颠倒会让缓存失效，重复拟稿都要重新吃一遍长上下文。
    """
    parts = []
    if full_text:
        parts.append(f"【底稿原文全文，回答时以此为准】\n{full_text}")
    if brief := ctx.briefing_text():
        parts.append(f"【会议底稿要点】\n{brief}")
    if transcript:
        lines = []
        # 最后一句单独拎出来。平铺成一堆「对方说」时模型不知道该回应哪一句，会去接更早的
        # 话题，实际会议里对方的话常被切成很碎的短句，这个问题尤其明显。
        for t in transcript[-14:-1]:
            src = (t.get("src") or "").strip()
            dst = (t.get("dst") or "").strip()
            if src:
                lines.append(f"对方：{src}")
            if dst and dst != src:
                lines.append(f"　（译）{dst}")
        if lines:
            parts.append("【此前对话，只作背景】\n" + "\n".join(lines))
        last = transcript[-1]
        last_src = (last.get("src") or "").strip()
        last_dst = (last.get("dst") or "").strip()
        if last_src:
            block = f"【对方刚说完这一句，回应的就是它】\n{last_src}"
            if last_dst and last_dst != last_src:
                block += f"\n（译）{last_dst}"
            parts.append(block)
    if history:
        turns = [f"{'我' if h.get('role') == 'user' else '助手'}：{h.get('text','').strip()}"
                 for h in history[-6:] if h.get("text")]
        if turns:
            parts.append("【此前交互】\n" + "\n".join(turns))
    if excerpts:
        blocks = [f"（{e['doc']}）{e['text']}" for e in excerpts]
        parts.append("【底稿原文里可能相关的片段，只在确实能回答对方问题时引用】\n" + "\n\n".join(blocks))
    parts.append(f"【我的要求】\n{instruction.strip()}")
    return "\n\n".join(parts)


async def stream_draft(ctx: MeetingContext, transcript: list[dict], history: list[dict],
                       instruction: str, quality: str = "fast",
                       excerpts: list[dict] | None = None) -> AsyncIterator[str]:
    cfg = settings_mod.load()
    if not cfg.text.ready():
        raise RuntimeError("还没配文本模型，先在模型设置里填 base URL 与 model name")
    model = cfg.strong_model() if quality == "good" else cfg.text.model

    # 底稿原文直接进上下文，装不下的部分用预取好的检索片段补。
    # 检索不放在这条路径上：它要多一次向量往返，实测会把首字从 3 秒推到 4 秒以上。
    # 片段由会议过程中后台预取，见 main.py 的 refresh_excerpts。
    full_text = retrieval.all_text(cfg.context_full_chars) if cfg.context_full_chars else ""
    log.info("拟稿上下文：原文全文 %d 字，预取片段 %d 块，模型 %s",
             len(full_text), len(excerpts or []), model)

    prompt = build_prompt(ctx, transcript, history, instruction, excerpts, full_text)
    async for chunk in llm.stream(cfg.text, model, prompt, SYSTEM, temperature=0.4):
        yield chunk
