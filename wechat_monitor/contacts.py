# -*- coding: utf-8 -*-
"""联系人/群名映射：解密 contact.db，建立「会话名 ↔ 消息表名」双向映射。

消息表名规则：Msg_<MD5(username)>，其中 username 形如
- 群聊：45948442385@chatroom
- 个人：wxid_xxx 或微信号
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from typing import Dict, List, Optional, Set

from . import decryptor


def md5_table(username: str) -> str:
    """由会话 username 计算消息表名 Msg_<MD5(username)>。"""
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def load_collect_names(collect_list_path: str) -> List[str]:
    """读取采集白名单文件，返回非空、非注释的名字列表（保序）。"""
    names: List[str] = []
    if not collect_list_path or not os.path.exists(collect_list_path):
        return names
    with open(collect_list_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            names.append(line)
    return names


class ContactMapping:
    """会话名 ↔ 消息表名 的映射，基于实时 contact.db 构建。

    额外提供会话对象信息（username / 是否群聊），用于生成消息事件的
    conversation 字段。
    """

    def __init__(self, wechat_data_dir: str, wxid: str, password: bytes):
        self.table_to_name: Dict[str, str] = {}
        self.name_to_table: Dict[str, str] = {}
        self.table_to_is_group: Dict[str, bool] = {}
        self.table_to_username: Dict[str, str] = {}
        self.name_to_username: Dict[str, str] = {}
        self._build(wechat_data_dir, wxid, password)

    def _build(self, wechat_data_dir: str, wxid: str, password: bytes) -> None:
        contact_db = os.path.join(
            wechat_data_dir, wxid, "db_storage", "contact", "contact.db"
        )
        if not os.path.exists(contact_db):
            return
        tmp = decryptor.decrypt_db(contact_db, password)
        try:
            conn = sqlite3.connect(tmp)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT username, nick_name, remark FROM contact WHERE username IS NOT NULL"
            ).fetchall()
            for row in rows:
                username = row["username"]
                name = row["nick_name"] or row["remark"] or username
                table = md5_table(username)
                self.table_to_name[table] = name
                self.name_to_table[name] = table
                self.table_to_is_group[table] = username.endswith("@chatroom")
                self.table_to_username[table] = username
                self.name_to_username[name] = username
            conn.close()
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def name_of_table(self, table_name: str) -> Optional[str]:
        """消息表名 → 会话名（群名/联系人名）。"""
        return self.table_to_name.get(table_name)

    def table_of_name(self, name: str) -> Optional[str]:
        """会话名 → 消息表名。"""
        return self.name_to_table.get(name)

    def username_of_table(self, table_name: str) -> Optional[str]:
        """消息表名 → 会话 username。"""
        return self.table_to_username.get(table_name)

    def is_group_table(self, table_name: str) -> bool:
        """消息表是否属于群聊。"""
        return self.table_to_is_group.get(table_name, False)

    def resolve_collect_tables(self, names: List[str]) -> Set[str]:
        """把白名单名字列表解析成消息表名集合（名字查不到则跳过）。"""
        tables: Set[str] = set()
        for name in names:
            table = self.name_to_table.get(name)
            if table:
                tables.add(table)
        return tables
