"""会议记录落本机 SQLite，另提供导出。"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

from . import config

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
    ws.root.mkdir(parents=True, exist_ok=True)
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
            "SELECT src, dst, src_lang, ts FROM turns WHERE meeting_id = ? ORDER BY id",
            (meeting_id,)).fetchall()
    return [{"src": r[0], "dst": r[1], "srcLang": r[2], "ts": r[3]} for r in rows]


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
