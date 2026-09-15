# -*- coding: utf-8 -*-
"""消息归档：写入本地 SQLite，按消息唯一标识去重，并原子维护增量游标。"""
from __future__ import annotations

import sqlite3
import time
from typing import Iterable, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    db_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    talker_wxid TEXT,
    local_id INTEGER,
    server_id INTEGER,
    create_time INTEGER,
    local_type INTEGER,
    real_sender_id INTEGER,
    sender_wxid TEXT,
    content TEXT,
    raw_content BLOB,
    archived_at INTEGER,
    UNIQUE(db_name, table_name, local_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(create_time);
CREATE TABLE IF NOT EXISTS cursors (
    db_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    last_local_id INTEGER DEFAULT 0,
    last_create_time INTEGER DEFAULT 0,
    PRIMARY KEY (db_name, table_name)
);
CREATE TABLE IF NOT EXISTS analyzed_messages (
    message_id TEXT NOT NULL,
    title TEXT NOT NULL,
    pushed_at INTEGER NOT NULL,
    PRIMARY KEY (message_id, title)
);
CREATE TABLE IF NOT EXISTS analysis_cursor (
    key TEXT PRIMARY KEY,
    value INTEGER
);
CREATE TABLE IF NOT EXISTS push_cursor (
    key TEXT PRIMARY KEY,
    value INTEGER
);
"""


class Archive:
    """归档库封装：负责消息去重写入与游标持久化。"""

    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def get_cursor(self, db_name: str, table_name: str) -> Tuple[int, int]:
        row = self.conn.execute(
            "SELECT last_local_id, last_create_time FROM cursors WHERE db_name=? AND table_name=?",
            (db_name, table_name),
        ).fetchone()
        return (row[0], row[1]) if row else (0, 0)

    def upsert_cursor(self, db_name: str, table_name: str, last_local_id: int, last_create_time: int) -> None:
        self.conn.execute(
            """INSERT INTO cursors(db_name, table_name, last_local_id, last_create_time)
               VALUES(?,?,?,?)
               ON CONFLICT(db_name, table_name)
               DO UPDATE SET last_local_id=excluded.last_local_id, last_create_time=excluded.last_create_time""",
            (db_name, table_name, last_local_id, last_create_time),
        )
        self.conn.commit()

    def insert_messages(self, messages: Iterable[dict]) -> int:
        """批量插入消息，重复消息被忽略，返回新增条数。"""
        added = 0
        for m in messages:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO messages
                   (db_name, table_name, talker_wxid, local_id, server_id, create_time,
                    local_type, real_sender_id, sender_wxid, content, raw_content, archived_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    m.get("db_name"),
                    m.get("table_name"),
                    m.get("talker_wxid"),
                    m.get("local_id"),
                    m.get("server_id"),
                    m.get("create_time"),
                    m.get("local_type"),
                    m.get("real_sender_id"),
                    m.get("sender_wxid"),
                    m.get("content"),
                    m.get("raw_content"),
                    int(time.time()),
                ),
            )
            if cur.rowcount > 0:
                added += 1
        self.conn.commit()
        return added

    def get_last_analyzed_id(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM analysis_cursor WHERE key='last_analyzed_id'"
        ).fetchone()
        return row[0] if row else 0

    def set_last_analyzed_id(self, last_id: int) -> None:
        self.conn.execute(
            """INSERT INTO analysis_cursor(key, value) VALUES('last_analyzed_id', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (last_id,),
        )
        self.conn.commit()

    def get_last_pushed_id(self) -> int:
        """推送游标：归档库中已推送到用户系统的最大自增主键。"""
        row = self.conn.execute(
            "SELECT value FROM push_cursor WHERE key='last_pushed_id'"
        ).fetchone()
        return row[0] if row else 0

    def set_last_pushed_id(self, last_id: int) -> None:
        self.conn.execute(
            """INSERT INTO push_cursor(key, value) VALUES('last_pushed_id', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (last_id,),
        )
        self.conn.commit()

    def reset_push_cursor(self) -> None:
        """把推送游标清零，使下一次推送从归档库最早一条开始（用于全量重推）。"""
        self.conn.execute("DELETE FROM push_cursor WHERE key='last_pushed_id'")
        self.conn.commit()

    def fetch_messages_after_id(self, after_id: int, tables: "set[str] | None" = None) -> list:
        """取出自增主键大于 after_id 的全部消息（按 id 升序），用于批量推送。

        tables 为白名单表名集合；传入时只返回这些表的消息。
        """
        if tables:
            placeholders = ",".join("?" for _ in tables)
            sql = (
                "SELECT id, db_name, table_name, local_id, create_time, local_type, content, raw_content "
                f"FROM messages WHERE id > ? AND table_name IN ({placeholders}) ORDER BY id"
            )
            rows = self.conn.execute(sql, (after_id, *tables)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, db_name, table_name, local_id, create_time, local_type, content, raw_content "
                "FROM messages WHERE id > ? ORDER BY id",
                (after_id,),
            ).fetchall()
        return self._rows_to_messages(rows)

    def fetch_messages_since_time(self, start_time: int) -> list:
        """取出 create_time >= start_time 的全部消息（按 id 升序）。"""
        rows = self.conn.execute(
            "SELECT id, db_name, table_name, local_id, create_time, local_type, content, raw_content "
            "FROM messages WHERE create_time >= ? ORDER BY id",
            (start_time,),
        ).fetchall()
        return self._rows_to_messages(rows)

    def get_max_id(self) -> int:
        """返回归档库中最大自增主键（用于把推送游标初始化到当前位置）。"""
        row = self.conn.execute("SELECT MAX(id) FROM messages").fetchone()
        return row[0] or 0

    @staticmethod
    def _rows_to_messages(rows) -> list:
        return [
            {
                "id": r[0],
                "db_name": r[1],
                "table_name": r[2],
                "local_id": r[3],
                "create_time": r[4],
                "local_type": r[5],
                "content": r[6],
                "raw_content": r[7],
                "message_id": f"{r[1]}:{r[2]}:{r[3]}",
            }
            for r in rows
        ]

    def is_analyzed(self, message_id: str, title: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM analyzed_messages WHERE message_id=? AND title=?",
            (message_id, title),
        ).fetchone()
        return row is not None

    def mark_analyzed(self, message_id: str, title: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO analyzed_messages(message_id, title, pushed_at) VALUES(?,?,?)",
            (message_id, title, int(time.time())),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
