"""会议记录落本机 SQLite，另提供导出。"""
from __future__ import annotations

import logging
import sqlite3
import json
import time
from datetime import datetime

from . import config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  started_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id INTEGER NOT NULL REFERENCES meetings(id),
  turn_id INTEGER NOT NULL,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  src_lang TEXT NOT NULL DEFAULT '',
  ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_meeting ON turns(meeting_id, id);
"""


def _conn(ws: config.Workspace) -> sqlite3.Connection:
    # 建的必须是库文件自己的父目录。库在会话根（shared_root），root 可能是某一场会议的子目录，
    # 按 root 建目录会给每个会议编号凭空建一个空目录，真正要写的那层反而没保证。
    ws.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ws.db)
    conn.executescript(SCHEMA)
    return conn


def start_meeting(ws: config.Workspace, title: str = "") -> int:
    title = title or datetime.now().strftime("%Y年%m月%d日 %H:%M 会议记录")
    with _conn(ws) as conn:
        cur = conn.execute(
            "INSERT INTO meetings (title, started_at) VALUES (?, ?)", (title, time.time()))
        return int(cur.lastrowid)


def add_turn(ws: config.Workspace, meeting_id: int, turn_id: int, src: str, dst: str,
             src_lang: str) -> None:
    with _conn(ws) as conn:
        conn.execute(
            "INSERT INTO turns (meeting_id, turn_id, src, dst, src_lang, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (meeting_id, turn_id, src, dst, src_lang, time.time()))


def list_meetings(ws: config.Workspace, limit: int = 50) -> list[dict]:
    with _conn(ws) as conn:
        rows = conn.execute(
            "SELECT m.id, m.title, m.started_at, COUNT(t.id) AS turns"
            " FROM meetings m LEFT JOIN turns t ON t.meeting_id = m.id"
            " GROUP BY m.id ORDER BY m.id DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r[0], "title": r[1], "startedAt": r[2], "turns": r[3]} for r in rows]


def get_turns(ws: config.Workspace, meeting_id: int) -> list[dict]:
    with _conn(ws) as conn:
        rows = conn.execute(
            "SELECT src, dst, src_lang, ts, turn_id FROM turns WHERE meeting_id = ? ORDER BY id",
            (meeting_id,)).fetchall()
    return [{"src": r[0], "dst": r[1], "srcLang": r[2], "ts": r[3], "turnId": r[4]}
            for r in rows]


def meeting_exists(ws: config.Workspace, meeting_id: int) -> bool:
    with _conn(ws) as conn:
        return conn.execute("SELECT 1 FROM meetings WHERE id = ?", (meeting_id,)).fetchone() is not None


def _blank_conversation() -> dict:
    return {"drafts": [], "directives": [], "profile": {}}


def load_conversation(ws: config.Workspace) -> dict:
    """读这一场会的会前交代、有效指示与拟稿历史。

    文件写到一半断电就会是半截 JSON，那时候整个界面都打不开代价太大，坏了按空的算，
    处置与 context_store.load 一致。
    """
    if not ws.conversation.exists():
        return _blank_conversation()
    try:
        loaded = json.loads(ws.conversation.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        log.warning("conversation.json 读不出，按空的算：%s：%s", type(exc).__name__, exc)
        return _blank_conversation()
    if not isinstance(loaded, dict):
        return _blank_conversation()
    blank = _blank_conversation()
    blank.update({k: v for k, v in loaded.items() if k in blank})
    return blank


def load_active(ws: config.Workspace) -> int | None:
    """当前这场会议的编号。文件缺失、读坏、或编号已不在库里，一律当作没有活动会议。

    这里绝不能抛。指针坏掉是小事，界面因此整个打不开是大事。
    """
    path = ws.active
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        meeting_id = int(data["meetingId"])
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, KeyError,
            TypeError, ValueError) as exc:
        log.warning("active-meeting.json 读不出，按没有活动会议算：%s：%s",
                    type(exc).__name__, exc)
        return None
    return meeting_id if meeting_exists(ws, meeting_id) else None


def save_active(ws: config.Workspace, meeting_id: int) -> None:
    """只记编号，别的都不记。"""
    path = ws.active
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"meetingId": int(meeting_id), "at": time.time()},
                               ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def clear_active(ws: config.Workspace) -> None:
    ws.active.unlink(missing_ok=True)


def save_conversation(ws: config.Workspace, conversation: dict) -> None:
    ws.root.mkdir(parents=True, exist_ok=True)
    temp = ws.conversation.with_suffix(".tmp")
    temp.write_text(json.dumps(conversation, ensure_ascii=False), encoding="utf-8")
    temp.replace(ws.conversation)


def export_markdown(ws: config.Workspace, meeting_id: int) -> str:
    with _conn(ws) as conn:
        row = conn.execute(
            "SELECT title, started_at FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if not row:
        return ""
    started = datetime.fromtimestamp(row[1]).strftime("%Y-%m-%d %H:%M")
    lines = [f"# {row[0]}", "", f"开始时间：{started}", ""]
    for t in get_turns(ws, meeting_id):
        stamp = datetime.fromtimestamp(t["ts"]).strftime("%H:%M:%S")
        lines.append(f"**[{stamp}]** {t['src']}")
        if t["dst"] and t["dst"] != t["src"]:
            lines.append(f"　　{t['dst']}")
        lines.append("")
    return "\n".join(lines)
