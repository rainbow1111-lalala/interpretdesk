"""会议纪要：把笔录交给强档模型整理成正式纪要，并出 md、docx、pdf。

不赶时间，用强档模型，宁可慢也要准。三条纪律写进提示词：时间由程序给不许模型编；参会人只写
笔录或底稿里能认出来的，认不出就写待确认；待办事项只写记录里确实说过的承诺。

docx 按用户的成稿格式来：全篇仿宋，正文小四，行距 26，标题分级加粗，两端对齐，带页码。
pdf 由 docx 转，保证两种格式看起来一样。
"""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import config, llm, settings as settings_mod, store
from .context_store import MeetingContext

log = logging.getLogger(__name__)

MINUTES_DIR = config.DATA_DIR / "minutes"

PROMPT = """你在为一名中国律师整理一场中英文会议的纪要。下面是这场会议的逐句笔录，以及会前底稿。

请输出 markdown 格式的会议纪要，结构如下：

# （会议标题，一行，写明事项与会议性质，例如「关于某某事项的电话会议纪要」）

**会议时间**：{when}
**参会人**：（在这里列出参会人）
**记录方式**：实时同传转录，经人工核对前有待复核

## 一、会议概要

（三到五句话说明本次会议讨论了什么、达成了什么、遗留什么。）

## 二、讨论事项

（按议题归类，不按发言先后。每个议题一个三级标题，标题下用编号段落写清楚各方表达的立场、
理由和分歧所在。同一议题的内容集中在一处，不要在多处重复展开。）

## 三、后续事项

（只写记录里确实出现过的承诺、待办、下次会议安排。没有就写「本次会议未形成明确的后续事项」。）

写作要求：
1. 会议时间由我给出，直接照抄，不要自己推算或编造。
2. 参会人只写笔录或底稿里能确认身份的人，能认出机构就写机构。任何认不出的写「待确认」，
   绝对不要根据常理推测姓名或职务。
3. 后续事项只写记录里确实说过的承诺，没说过的一律不写。
4. 用中文书面语，平实客观，不用口语化总结，不用破折号。英文专有名词首次出现时括注中文。
5. 转录可能有识别错误，遇到明显不通的句子按上下文合理理解，不要照抄错字，也不要替当事人补充
   他没说过的意思。
6. 只输出 markdown 正文，不要写解释，不要用代码块围栏包住整篇。

会前底稿要点：
{brief}

逐句笔录：
{transcript}"""


def _transcript_text(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        stamp = datetime.fromtimestamp(t["ts"]).strftime("%H:%M:%S")
        src = (t.get("src") or "").strip()
        dst = (t.get("dst") or "").strip()
        if not src:
            continue
        lines.append(f"[{stamp}] {src}")
        if dst and dst != src:
            lines.append(f"        （译）{dst}")
    return "\n".join(lines)


def meeting_window(meeting_id: int) -> str:
    """会议时间由程序算，不交给模型。"""
    turns = store.get_turns(meeting_id)
    meetings = {m["id"]: m for m in store.list_meetings(500)}
    started = meetings.get(meeting_id, {}).get("startedAt")
    if not started:
        return "待确认"
    begin = datetime.fromtimestamp(started)
    end = datetime.fromtimestamp(turns[-1]["ts"]) if turns else begin
    minutes = max(1, round((end - begin).total_seconds() / 60))
    return (f"{begin.strftime('%Y年%m月%d日 %H:%M')} 至 {end.strftime('%H:%M')}"
            f"（约 {minutes} 分钟）")


async def generate(meeting_id: int, ctx: MeetingContext) -> str:
    turns = store.get_turns(meeting_id)
    if not turns:
        raise RuntimeError("这场会议没有记录到内容，无法生成纪要")
    cfg = settings_mod.load()
    if not cfg.text.ready():
        raise RuntimeError("还没配文本模型，先在模型设置里填 base URL 与 model name")

    prompt = (PROMPT
              .replace("{when}", meeting_window(meeting_id))
              .replace("{brief}", ctx.briefing_text() or "（无）")
              .replace("{transcript}", _transcript_text(turns)))
    log.info("生成纪要：会议 #%d，%d 段笔录，模型 %s",
             meeting_id, len(turns), cfg.strong_model())

    parts: list[str] = []
    async for piece in llm.stream(cfg.text, cfg.strong_model(), prompt, temperature=0.3):
        parts.append(piece)
    text = "".join(parts).strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    if not text:
        raise RuntimeError("模型没有返回内容")

    MINUTES_DIR.mkdir(parents=True, exist_ok=True)
    (MINUTES_DIR / f"{meeting_id}.md").write_text(text, encoding="utf-8")
    return text


def load(meeting_id: int) -> str:
    path = MINUTES_DIR / f"{meeting_id}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def title_of(markdown: str, fallback: str = "会议纪要") -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback
