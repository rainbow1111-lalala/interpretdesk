"""右栏：吸收会前背景与实时字幕，帮我拟英文回复（附中文对照）。"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from . import llm, retrieval, settings as settings_mod
from .context_store import MeetingContext

log = logging.getLogger(__name__)

SYSTEM_TEMPLATE = """你是一名熟悉中国数据合规与跨境业务的中国执业律师，正在会议现场替另一名律师接话。他与外国律师开会，需要你实时帮他组织{lang}表达。

工作方式：
1. 他要「怎么回」「帮我回」「拟一段」这类要求时，先给{lang}回复，再用单独一行 ---ZH--- 分隔，
   之后给中文对照。不写称呼语和签名。
2. 回复是在会上当场接话的口语，不是书面文书：第一句直接回应对方刚问的那个点，再补一两句
   理由；短句为主，允许缩写（we're、that's、can't）；可以用 Well、Actually、So、Look 一类
   口语衔接开头；力求快速、自然反应。默认三五句说完，只有对方明确要清单（有哪些步骤、有哪些
   风险）才逐条展开，一条一两句。念出来要像人说话，不像读文件。
3. 先看懂对方问的到底是什么，回答那一个问题。底稿只是背景参考，不是答案库：有直接对应的
   内容就用；没有的，就以执业律师的身份凭你自己的专业知识、结合底稿里的事实和立场直接回答，
   禁止拿一段主题相近的现成段落顶数。对方问「会遇到哪些实际障碍」，就逐条说障碍，不要转去
   讲岗位定位或职责。
3.1 紧跟现场议程：对方现在谈到哪就回应哪，以「此前对话」和「对方刚说完的这一句」为准。
   会议常会走到底稿没有覆盖的议题，这时候照常回答当前议题，不要把话头往底稿里的旧议题
   拉回去，更不要在无关话题里复述底稿立场。
4. 他问的是术语含义、对方话里的意思、或者要你判断形势时，直接用中文简短回答，不要输出 ---ZH---。
4.1 对方讲的是中文还是外语都要照常回应。听到中文不代表不用拟稿，一样按上面的格式给{lang}回复。
5. 下笔之前先认清他代表哪一方。底稿里的「我方立场与底线」是唯一准绳，争点是中立记述，不要
   照着争点里对方的主张写。凡是与我方立场相反的表态，一律不得出现在英文里。
6. 如果他的要求与底稿记载的立场冲突，先按他的要求写，写完在中文对照之后另起一行，用
   「提示：」开头，用不超过四十字点出冲突在哪里，由他决定。没有冲突就不要写这一行，也不要
   写「此回复符合底线」这类确认话。
7. 涉及让步、报价、承诺的表达要留余地，用 subject to、we would need to confirm、in principle
   一类措辞，不替他把底线交出去。
8. 当事人名称与术语译法一律照会议底稿给定的写法，不自行改译。
9. 不复述背景，不写前言，不解释你在做什么。"""


def system_prompt(reply_lang: str) -> str:
    """按配置的回复语言拼系统提示。回复语言就是中文时不必再给中文对照。"""
    text = SYSTEM_TEMPLATE.replace("{lang}", reply_lang or "English")
    if (reply_lang or "").strip() in ("中文", "zh", "zh-CN", "Chinese"):
        text += ("\n\n补充：本场回复语言就是中文，直接给中文回复，不要输出 ---ZH--- "
                 "分隔行，也不要再附中文对照。")
    return text


def build_prompt(ctx: MeetingContext, transcript: list[dict], history: list[dict],
                 instruction: str, excerpts: list[dict] | None = None,
                 full_text: str = "", reply_lang: str = "English") -> str:
    """稳定的内容放最前，变动的放最后。

    原文与底稿摘要每次都一样，把它们放在提示词开头，服务端的上下文缓存才能命中同一段前缀；
    最近对话和我的要求每次都变，放在末尾。顺序颠倒会让缓存失效，重复拟稿都要重新吃一遍长上下文。
    """
    parts = []
    if full_text:
        parts.append(f"【会前背景材料，供参考；对方问题超出材料时凭专业知识直接回答】\n{full_text}")
    if brief := ctx.briefing_text():
        parts.append(f"【会议底稿要点】\n{brief}")
    if transcript:
        lines = []
        # 最后一句单独拎出来。平铺成一堆「对方说」时模型不知道该回应哪一句，会去接更早的
        # 话题，实际会议里对方的话常被切成很碎的短句，这个问题尤其明显。
        # 最近对话按字符预算取而不是固定段数：一段常常只有一两句，固定 14 段只覆盖一两
        # 分钟，会议走到新议程时拟稿还停在底稿旧议题上（实战反馈）。4000 字约覆盖十几分钟。
        recent = transcript[:-1]
        picked: list[dict] = []
        used = 0
        for t in reversed(recent):
            cost = len(t.get("src") or "") + len(t.get("dst") or "")
            if used + cost > 4000 and picked:
                break
            used += cost
            picked.append(t)
        for t in reversed(picked):
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
    # 输出语言放在最末尾。放在系统提示里会被后面大段的英文底稿和英文对话盖过去，实测切成
    # 日文后仍然吐英文；挪到提示词最后一句才稳。
    if (reply_lang or "").strip() in ("中文", "zh", "zh-CN", "Chinese"):
        parts.append("【输出语言】整段回复用中文写，不要输出 ---ZH--- 分隔行。")
    else:
        parts.append(f"【输出语言】回复正文必须用{reply_lang}写，一个{reply_lang}的词都不能少；"
                     f"写完另起一行写 ---ZH---，再给中文对照。即使上文全是英文，正文也要用"
                     f"{reply_lang}。")
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

    prompt = build_prompt(ctx, transcript, history, instruction, excerpts, full_text,
                          cfg.reply_lang)
    async for chunk in llm.stream(cfg.text, model, prompt,
                                  system_prompt(cfg.reply_lang), temperature=0.4):
        yield chunk
