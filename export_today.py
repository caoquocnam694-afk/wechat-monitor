# -*- coding: utf-8 -*-
"""导出今天（本地日期）的采集记录为 txt，按会话分群。

数据源：归档库 archive.db（白名单会话已被守护进程完整归档）。
发送者显示：优先备注名，其次昵称，微信号兜底；群消息正文前缀 + real_sender_id 双路解析。
每条格式：HH:MM 发送者: 内容（文本）/ [文件] 文件名 / [图片][语音] 等类型标记。
"""
import os, re, sys, sqlite3, time
import xml.etree.ElementTree as ET
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
from wechat_monitor import contacts, formatter, key_extractor, decryptor
from wechat_monitor.config import Config

cfg = Config.load('config.json').resolved()
password = key_extractor.load_cached_password(cfg.key_cache_path)

# ---- 微信号 → 显示名（备注优先） ----
uname_to_name = {}
contact_db = f"{cfg.wechat_data_dir}\\{cfg.wxid}\\db_storage\\contact\\contact.db"
try:
    tmp = decryptor.decrypt_db(contact_db, password)
    try:
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT username, nick_name, remark FROM contact WHERE username IS NOT NULL"
        ).fetchall():
            uname_to_name[row["username"]] = row["remark"] or row["nick_name"] or row["username"]
        conn.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
except Exception as e:
    print(f"警告：联系人昵称解析失败，将显示微信号: {e}")

# ---- 每个消息库的 Name2Id：real_sender_id(rowid) → 微信号 ----
# 微信把发送者存成整数 id（Name2Id 表的 rowid），需按库逐个解密读取
name2id = {}  # {db_name: {rid: username}}
msg_dir = os.path.join(cfg.wechat_data_dir, cfg.wxid, "db_storage", "message")
import glob
for db_path in sorted(glob.glob(os.path.join(msg_dir, "message_*.db"))):
    base = os.path.basename(db_path)
    if "fts" in base or "resource" in base:
        continue
    try:
        tmp = decryptor.decrypt_db(db_path, password)
        try:
            conn = sqlite3.connect(tmp)
            conn.row_factory = sqlite3.Row
            name2id[base] = {
                r["rowid"]: r["user_name"]
                for r in conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
            }
            conn.close()
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    except Exception as e:
        print(f"警告：{base} 发送者映射读取失败: {e}")

# ---- 白名单表 ----
names = contacts.load_collect_names(cfg.collect_list_path)
mapping = contacts.ContactMapping(cfg.wechat_data_dir, cfg.wxid, password)
tables = set()
for n in names:
    t = mapping.table_of_name(n)
    if t:
        tables.add(t)

# ---- 目标日期零点（本机本地时间）；默认今天，可传参数指定 YYYY-MM-DD ----
now = time.time()
lt = time.localtime(now)
if len(sys.argv) > 1:
    lt = time.strptime(sys.argv[1], "%Y-%m-%d")
today_epoch = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))

# ---- 取今天全部消息 ----
conn = sqlite3.connect(cfg.archive_db_path)
conn.row_factory = sqlite3.Row
ph = ",".join("?" for _ in tables)
rows = conn.execute(
    f"SELECT db_name, table_name, local_id, create_time, local_type, content, raw_content, real_sender_id "
    f"FROM messages WHERE table_name IN ({ph}) AND create_time >= ? AND create_time < ? "
    f"ORDER BY table_name, create_time, local_id",
    tuple(tables) + (today_epoch, today_epoch + 86400),
).fetchall()
conn.close()

groups = {}
for r in rows:
    groups.setdefault(r["table_name"], []).append(r)


_ACCOUNT_BASE = "_".join(cfg.wxid.split("_")[:2])  # 去掉账号目录后缀（_595e）


def display_name(wxid):
    """微信号 → 显示名；自己的微信号显示为「我」。"""
    if not wxid:
        return ""
    if wxid == cfg.wxid or wxid == _ACCOUNT_BASE:
        return "我"
    return uname_to_name.get(wxid, wxid)


def sender_of(r, is_group, name):
    """解析发送者显示名：群消息正文前缀优先，其次 real_sender_id(rowid)→微信号→昵称。"""
    content = r["content"] or ""
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    prefix_name = None
    body = content
    if "\n" in content:
        head = content.split("\n", 1)[0].rstrip()
        if head.endswith(":") and len(head) <= 64:
            prefix_name = head[:-1]
            body = content.split("\n", 1)[1]
    if prefix_name:
        return display_name(prefix_name) or prefix_name, body
    rid = r["real_sender_id"]
    if rid:
        uname = name2id.get(r["db_name"], {}).get(rid)
        if uname:
            return display_name(uname) or uname, body
    return (display_name(name) or name, body)


_FWD_RE = re.compile(r"<recorditem><!\[CDATA\[(.*?)\]\]></recorditem>", re.S)


def parse_forward_record(raw):
    """解析 type=19 合并转发聊天记录。

    返回内部消息列表，每条为 {datatype, sender, time, text}；非转发记录返回 None。
    """
    xml = raw
    if isinstance(xml, bytes):
        xml = xml.decode("utf-8", errors="replace")
    if not xml or "<type>19</type>" not in xml:
        return None
    m = _FWD_RE.search(xml)
    if not m:
        return None
    try:
        root = ET.fromstring(m.group(1))
    except ET.ParseError:
        return None
    items = []
    for di in root.iter("dataitem"):
        dt = di.get("datatype") or ""
        text = (di.findtext("datatitle") or "").strip()
        if not text:
            text = (di.findtext("datadesc") or "").strip()
        # 内部正文换行折叠为单行，保持每条一行
        text = re.sub(r"\s*\n\s*", " / ", text).strip()
        items.append({
            "datatype": dt,
            "sender": (di.findtext("sourcename") or "").strip() or "未知",
            "time": (di.findtext("sourcetime") or "").strip(),
            "text": text,
        })
    return items or None


def fmt_inner_time(st):
    """内部消息原时间 → MM-DD HH:MM（可能跨天，故带日期）。"""
    if not st:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", st)
    if m:
        return f"{m.group(1)[5:]} {m.group(2)}"
    return st


_INNER_TYPE_LABEL = {
    "2": "[图片]",
    "8": "[文件]",
    "17": "[表情]",
    "19": "[转发的聊天记录]",
}


def render_inner_item(it):
    """内部消息 → 展示文本：文本直接显示，媒体/文件打类型标记。"""
    dt = it["datatype"]
    text = it["text"]
    if dt == "1":
        return text or "(空)"
    label = _INNER_TYPE_LABEL.get(dt)
    if label:
        return f"{label} {text}".rstrip()
    if text.startswith("["):  # 已是 [图片]/[视频] 之类的标记
        return text
    if text:
        return f"[类型{dt}] {text}"
    return f"[类型{dt}]"


lines = []
total = 0
for table in sorted(groups, key=lambda t: mapping.name_of_table(t) or t):
    msgs = groups[table]
    name = mapping.name_of_table(table) or table
    is_group = mapping.is_group_table(table)
    lines.append("=" * 46)
    lines.append(f"【{'群聊' if is_group else '单聊'}】{name}   （今日 {len(msgs)} 条）")
    lines.append("-" * 46)
    for r in msgs:
        mt = r["local_type"]
        if isinstance(mt, int):
            mt &= 0xFFFFFFFF
        ts = time.strftime("%H:%M", time.localtime(r["create_time"]))
        sender, body = sender_of(r, is_group, name)
        if mt == 1:  # 文本
            line = f"{ts} {sender}: {body}"
        elif mt == 49:  # 文件 或 合并转发聊天记录
            fwd = parse_forward_record(r["raw_content"] or r["content"])
            if fwd is not None:
                lines.append(f"{ts} {sender}: [转发的聊天记录] {len(fwd)} 条")
                for it in fwd:
                    lines.append(
                        f"    {fmt_inner_time(it['time'])} {it['sender']}: {render_inner_item(it)}"
                    )
                total += 1
                continue
            meta = formatter._parse_file_meta(r["raw_content"] or r["content"])
            fname = meta["file_name"] if meta else "(非文件消息)"
            line = f"{ts} {sender}: [文件] {fname}"
        elif mt == 3:
            line = f"{ts} {sender}: [图片]"
        elif mt == 34:
            line = f"{ts} {sender}: [语音]"
        elif mt == 43:
            line = f"{ts} {sender}: [视频]"
        elif mt == 47:
            line = f"{ts} {sender}: [表情]"
        elif mt == 10000:
            line = f"{ts} [系统] {body}"
        else:
            line = f"{ts} {sender}: [类型{mt}] {body[:60]}"
        lines.append(line)
        total += 1

out = f"今天_{time.strftime('%Y-%m-%d', lt)}_分群记录.txt"
lines.append("")
lines.append(f"共 {total} 条消息，{len(groups)} 个会话")
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"已导出: {out}")
print(f"共 {total} 条，{len(groups)} 个会话")
for table in sorted(groups, key=lambda t: mapping.name_of_table(t) or t):
    print(f"  {mapping.name_of_table(table) or table}: {len(groups[table])} 条")
