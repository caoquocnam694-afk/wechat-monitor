# -*- coding: utf-8 -*-
"""消息事件格式化：把归档库的原始消息转成「消息事件」结构。

消息事件是采集端推送给用户系统的统一数据形态（比「任务」更底层），
用户系统在此基础上自行做任务分解、检索、归档、文件处理等。

字段结构（已与用户确认）：
{
  "message_id": "db:table:local_id",
  "conversation": {"id": username, "name": 会话名, "is_group": bool},
  "sender": {"id": wxid 或 null, "name": 发送者名 或 null},
  "sent_at": "YYYY-MM-DD HH:MM:SS",
  "type": "text" | "file",
  "content": 文本正文（仅 text），
  "mentions": [被 @ 的名字...],
  "mention_all": bool,
  "file": {  // 仅 type=file
      "file_name", "file_ext", "size_bytes", "local_path"
  }
}

采集端不做任何语义判断（无 AI）；@ 了谁、是否 @ 本人等交给用户系统。
"""
from __future__ import annotations

import html
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from . import contacts

# 微信 PC 版 @ 之后的分隔符（特殊空格 U+2005）
AT_SEP = " "

# 视为「@所有人」而非「@具体某人」的标识
_MENTION_ALL_MARKERS = {"所有人", "全部人", "全体成员", "all", "All", "ALL", "everyone", "全体"}

# 采集的消息类型：text（低32位=1）、file（低32位=49 且 appmsg type=6）
_TYPE_TEXT = 1
_TYPE_APPMSG = 49

# 文件本体允许的扩展名（文档类，明文存储；图片 .dat 加密另议）
_FILE_EXTS = {
    "doc", "docx", "pdf", "xls", "xlsx", "ppt", "pptx",
    "txt", "csv", "zip", "rar", "7z", "m4a", "mp3", "wav",
    "mp4", "mov", "avi", "jpg", "jpeg", "png", "gif", "bmp",
}

_MSG_XML_TYPE_RE = re.compile(r"<type>(\d+)</type>")


def _fmt_time(ts: Any) -> str:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (OverflowError, ValueError, OSError):
        return ""


def _text_of(content: Any) -> str:
    """content 可能是 str、bytes 或 None，统一返回 str。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return bytes(content).decode("utf-8", errors="replace")
    except Exception:
        return ""


def extract_ats(content: str) -> List[str]:
    """提取消息里所有被 @ 的显示名（@ 到分隔符之间的文本）。"""
    if not content:
        return []
    ats = []
    parts = content.split("@")
    for part in parts[1:]:
        name = part.split(AT_SEP, 1)[0].strip()
        if name:
            ats.append(name)
    return ats


def _mentions_of(content: str) -> tuple:
    """返回 (mentions 列表, 是否 @所有人)。"""
    ats = extract_ats(content)
    mention_all = any(a in _MENTION_ALL_MARKERS for a in ats)
    mentions = [a for a in ats if a not in _MENTION_ALL_MARKERS]
    return mentions, mention_all


def _strip_sender_prefix(content: str) -> str:
    """去掉群消息正文开头的「发送者:\\n」前缀，返回纯正文。"""
    if content and "\n" in content:
        head, rest = content.split("\n", 1)
        if head.endswith(":") and len(head) <= 64:
            return rest
    return content


def _sender_name(content: str, source_name: str) -> Optional[str]:
    """群消息正文以「发送者:\\n」开头时返回发送者名；否则返回 None。"""
    if content and "\n" in content:
        head = content.split("\n", 1)[0].rstrip()
        if head.endswith(":") and len(head) <= 64:
            return head[:-1]
    return None


def _parse_file_meta(raw: Any) -> Optional[Dict[str, Any]]:
    """从 type=49 文件消息的 XML 里提取文件名、大小、扩展名。"""
    xml = _text_of(raw)
    if not xml:
        return None
    # XML 里中文可能被转义，先 html.unescape 一次
    xml_clean = html.unescape(xml)
    try:
        root = ET.fromstring(xml_clean)
    except ET.ParseError:
        return None
    # 只处理真正的文件（appmsg/type == 6）
    appmsg = root.find("appmsg")
    if appmsg is None:
        return None
    typ = appmsg.findtext("type")
    if typ != "6":
        return None
    title = (appmsg.findtext("title") or "").strip()
    if not title:
        return None
    size = 0
    appattach = appmsg.find("appattach")
    if appattach is not None:
        try:
            size = int((appattach.findtext("totallen") or "0").strip())
        except ValueError:
            size = 0
    ext = ""
    if "." in title:
        ext = title.rsplit(".", 1)[1].lower()
    return {"file_name": title, "file_ext": ext, "size_bytes": size}


def _locate_file(
    file_name: str,
    wechat_data_dir: str,
    wxid: str,
    collect_dir: str,
) -> Optional[str]:
    """按文件名在微信文件目录里定位本体，拷贝到采集目录，返回落盘路径。

    微信文件散落两处：
      - msg/file/<年月>/<原名(可能带 (n) 后缀)>
      - msg/attach/<hash>/<年月>/Rec/<hash>/F/<n>/<原名>
    先精确匹配，再忽略重名后缀 (n) 匹配；找到即拷贝，找不到返回 None。
    """
    if not collect_dir:
        return None
    account_dir = os.path.join(wechat_data_dir, wxid)
    search_roots = [
        os.path.join(account_dir, "msg", "file"),
        os.path.join(account_dir, "msg", "attach"),
    ]
    stem, ext = os.path.splitext(file_name)
    candidates = [file_name]
    # 微信对重名文件加 (n) 后缀，也纳入匹配
    candidates.append(stem + "(1)" + ext)

    src = None
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f in candidates or f == file_name:
                    src = os.path.join(dirpath, f)
                    break
            if src:
                break
        if src:
            break
    if not src:
        return None

    os.makedirs(collect_dir, exist_ok=True)
    # 落盘文件名加时间戳前缀避免同名覆盖
    base = os.path.basename(src)
    dest = os.path.join(collect_dir, base)
    counter = 1
    while os.path.exists(dest):
        stem2, ext2 = os.path.splitext(base)
        dest = os.path.join(collect_dir, f"{stem2}({counter}){ext2}")
        counter += 1
    try:
        with open(src, "rb") as fin, open(dest, "wb") as fout:
            fout.write(fin.read())
    except OSError:
        return None
    return dest


def format_event(
    m: Dict[str, Any],
    mapping: "contacts.ContactMapping",
    wechat_data_dir: str = "",
    wxid: str = "",
    collect_dir: str = "",
) -> Optional[Dict[str, Any]]:
    """把一条归档消息 dict 转成消息事件 dict；非目标类型返回 None。"""
    local_type = m.get("local_type") or 0
    if isinstance(local_type, int):
        local_type &= 0xFFFFFFFF

    table = m.get("table_name", "")
    source_name = mapping.name_of_table(table) or table
    username = mapping.username_of_table(table)
    is_group = mapping.is_group_table(table)

    # --- text ---
    if local_type == _TYPE_TEXT:
        content = _text_of(m.get("content"))
        if not content:
            return None
        sender_name = _sender_name(content, source_name)
        body = _strip_sender_prefix(content)
        mentions, mention_all = _mentions_of(content)
        event = {
            "message_id": m.get("message_id", ""),
            "conversation": {"id": username, "name": source_name, "is_group": is_group},
            # sender.name 为必填；单聊/无法识别时回退为会话名
            "sender": {"id": None, "name": sender_name or source_name},
            "sent_at": _fmt_time(m.get("create_time")),
            "type": "text",
            "content": body,
            "mentions": mentions,
            "mention_all": mention_all,
            "file": None,
        }
        return event

    # --- file ---
    if local_type == _TYPE_APPMSG:
        meta = _parse_file_meta(m.get("raw_content") or m.get("content"))
        if meta is None:
            return None
        local_path = _locate_file(
            meta["file_name"], wechat_data_dir, wxid, collect_dir
        )
        event = {
            "message_id": m.get("message_id", ""),
            "conversation": {"id": username, "name": source_name, "is_group": is_group},
            # sender.name 为必填；文件消息发送者无法从正文解析，回退为会话名
            "sender": {"id": None, "name": source_name},
            "sent_at": _fmt_time(m.get("create_time")),
            "type": "file",
            "content": None,
            "mentions": [],
            "mention_all": False,
            "file": {
                "file_name": meta["file_name"],
                "file_ext": meta["file_ext"] or None,
                "size_bytes": meta["size_bytes"],
                "local_path": local_path,
            },
        }
        return event

    return None
