# -*- coding: utf-8 -*-
"""增量轮询：发现并解密消息库，按游标产出新增消息并归档。

只读处理：每个库解密到临时文件，查询后立即删除，绝不改写原库。
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import List, Optional, Set, Tuple

from . import decryptor
from .archive import Archive

try:
    import zstandard
except ImportError:  # pragma: no cover
    zstandard = None

MSG_TABLE_PREFIX = "Msg_"
_MESSAGE_DB_RE = re.compile(r"^message_\d+\.db$")
# 微信 WCDB 压缩存储的 zstd 帧魔数
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# 各字段的候选名（用于自适应探测，兼容字段名差异）
_LOCAL_ID = ("local_id", "localId")
_SERVER_ID = ("server_id", "MsgSvrID")
_CREATE_TIME = ("create_time", "createTime", "CreateTime")
_TYPE = ("local_type", "type", "Type")
_SENDER = ("real_sender_id", "real_sender_id")
_CONTENT = ("message_content", "content", "StrContent")


def discover_accounts(wechat_data_dir: str) -> List[str]:
    """发现数据目录下所有账号目录（wxid_xxx）。"""
    if not os.path.isdir(wechat_data_dir):
        return []
    return sorted(d for d in os.listdir(wechat_data_dir) if d.startswith("wxid_"))


def discover_message_dbs(wechat_data_dir: str, wxid: str) -> List[str]:
    """发现指定账号的消息分库（message_<数字>.db），按名称排序（message_0 为最新库）。

    仅匹配纯消息分库，排除 message_fts.db / message_resource.db 等索引与资源库。
    """
    msg_dir = os.path.join(wechat_data_dir, wxid, "db_storage", "message")
    if not os.path.isdir(msg_dir):
        return []
    dbs = [os.path.join(msg_dir, f) for f in os.listdir(msg_dir) if _MESSAGE_DB_RE.match(f)]
    return sorted(dbs)


def _pick(cols: List[str], candidates: Tuple[str, ...]) -> Optional[str]:
    for c in candidates:
        if c in cols:
            return c
    return None


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _poll_table(conn: sqlite3.Connection, table: str, db_name: str, archive: Archive) -> int:
    """对单个消息表执行增量查询并归档，返回新增条数。"""
    cols = _table_columns(conn, table)
    local_id_col = _pick(cols, _LOCAL_ID)
    if local_id_col is None:
        return 0  # 无法定位自增主键，跳过

    create_time_col = _pick(cols, _CREATE_TIME)
    type_col = _pick(cols, _TYPE)
    sender_col = _pick(cols, _SENDER)
    content_col = _pick(cols, _CONTENT)
    server_id_col = _pick(cols, _SERVER_ID)

    last_local_id, last_create_time = archive.get_cursor(db_name, table)

    selected = [c for c in (local_id_col, server_id_col, create_time_col, type_col, sender_col, content_col) if c]
    sql = f'SELECT {", ".join(selected)} FROM "{table}" WHERE {local_id_col} > ? ORDER BY {local_id_col}'
    rows = conn.execute(sql, (last_local_id,)).fetchall()

    messages = []
    max_local_id = last_local_id
    max_create_time = last_create_time
    for row in rows:
        local_id = row[local_id_col] or 0
        create_time = row[create_time_col] if create_time_col else 0
        mtype = row[type_col] if type_col else None
        if isinstance(mtype, int):
            mtype &= 0xFFFFFFFF  # local_type 为 64 位编码，低 32 位才是真实类型
        content_raw = row[content_col] if content_col else None

        content_text = None
        raw_content = None
        if content_raw is not None:
            if isinstance(content_raw, str):
                content_text = content_raw
            else:
                blob = bytes(content_raw)
                # 微信 WCDB 对长文本用 zstd 压缩（帧头魔数 28 b5 2f fd）
                if blob[:4] == _ZSTD_MAGIC and zstandard is not None:
                    try:
                        blob = zstandard.ZstdDecompressor().decompress(blob)
                    except Exception:
                        pass
                raw_content = blob
                # 文本消息（local_type 低 32 位 == 1）按 UTF-8 解码
                if mtype == 1:
                    try:
                        content_text = blob.decode("utf-8", errors="replace")
                    except Exception:
                        content_text = None

        messages.append({
            "db_name": db_name,
            "table_name": table,
            "talker_wxid": None,
            "local_id": local_id,
            "server_id": row[server_id_col] if server_id_col else None,
            "create_time": create_time,
            "local_type": mtype,
            "real_sender_id": row[sender_col] if sender_col else None,
            "sender_wxid": None,
            "content": content_text,
            "raw_content": raw_content,
        })
        if local_id > max_local_id:
            max_local_id = local_id
            if create_time_col:
                max_create_time = create_time or max_create_time

    archive.insert_messages(messages)
    if messages:
        # 游标推进到本批最大 local_id，保证幂等、不重不漏
        archive.upsert_cursor(db_name, table, max_local_id, max_create_time)
    return len(messages)


def poll_message_db(
    db_path: str,
    password: bytes,
    archive: Archive,
    collect_tables: Optional[Set[str]] = None,
) -> int:
    """解密并增量归档单个消息库，返回新增消息条数。

    collect_tables 为白名单表名集合；传入时只采集其中的表，不传则采集全部。
    """
    db_name = os.path.basename(db_path)
    tmp = decryptor.decrypt_db(db_path, password)
    added = 0
    try:
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
                (MSG_TABLE_PREFIX + "%",),
            ).fetchall()
        ]
        for table in tables:
            if table.endswith("_fts"):
                continue
            if collect_tables is not None and table not in collect_tables:
                continue
            added += _poll_table(conn, table, db_name, archive)
        conn.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return added


def poll_account(
    wechat_data_dir: str,
    wxid: str,
    password: bytes,
    archive: Archive,
    collect_tables: Optional[Set[str]] = None,
) -> int:
    """轮询指定账号的所有消息库，返回新增消息总数。

    解密失败（密钥不匹配）会抛出 ValueError，由服务层决定是否重提取密钥。
    collect_tables 为白名单表名集合，只采集其中的会话。
    """
    total = 0
    for db_path in discover_message_dbs(wechat_data_dir, wxid):
        total += poll_message_db(db_path, password, archive, collect_tables)
    return total
