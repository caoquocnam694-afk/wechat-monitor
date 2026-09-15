# -*- coding: utf-8 -*-
"""导出归档库中白名单会话的图片（type=3）与语音（type=34）。

用法：
    python export_media.py                        导出全部归档
    python export_media.py 2026-09-09             只导出该日
    python export_media.py 2026-09-01 2026-09-09  导出日期区间（含首尾两天）

图片来源：消息 XML 的 md5 → msg/attach/<会话md5>/<yyyy-mm>/Img/<md5>_h.dat（原图）
        / <md5>.dat（中图）/ <md5>_t.dat（缩略图），按可得性取最优。
        V2 密文用 image_keys.json 的 AES/XOR 密钥解密（密钥由 find_image_key_poll.py 提取）。
语音来源：media_0.db 的 VoiceInfo 表，按 (会话 username, local_id) 对应消息；
        voice_data 去掉首字节 0x02 即标准 SILK v3 文件。
        若 tools/silk/silk_v3_decoder.exe 存在则转 MP3（PCM + ffmpeg），否则保留 .silk。
输出：export_media/<会话名>/img|voice/YYYYMMDD_HHMMSS_<发送者>_<local_id>.<ext>
"""
from __future__ import annotations

import glob
import html
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

from Crypto.Cipher import AES
from Crypto.Util import Padding

from wechat_monitor import contacts, decryptor, key_extractor
from wechat_monitor.config import Config

V2_MAGIC = b"\x07\x08V2\x08\x07"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
IMG_MAGICS = [
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG", ".png"),
    (b"RIFF", ".webp"),
    (b"GIF8", ".gif"),
    (b"wxgf", ".heic"),
]
TYPE_IMAGE = 3
TYPE_VOICE = 34
MASK = 0xFFFFFFFF

cfg = Config.load("config.json").resolved()
ATTACH_DIR = os.path.join(cfg.wechat_data_dir, cfg.wxid, "msg", "attach")
MEDIA_DB = os.path.join(cfg.wechat_data_dir, cfg.wxid, "db_storage", "message", "media_0.db")
RESOURCE_DB = os.path.join(cfg.wechat_data_dir, cfg.wxid, "db_storage", "message", "message_resource.db")
KEY_CACHE = os.path.join(BASE, "image_keys.json")
SILK_DECODER = os.path.join(BASE, "tools", "silk", "silk_v3_decoder.exe")
EXPORT_ROOT = os.path.join(BASE, "export_media")
STATE_PATH = os.path.join(BASE, "export_media_state.json")
FFMPEG = shutil.which("ffmpeg")
_ACCOUNT_BASE = "_".join(cfg.wxid.split("_")[:2])  # 账号目录后缀（_595e）之前的部分

# 图片解密密钥（find_image_key_poll.py 从微信进程内存提取并缓存）
with open(KEY_CACHE, encoding="utf-8") as _f:
    _kc = json.load(_f)
AES_KEY = _kc["image_aes_key"].encode("ascii")[:16]
XOR_KEY = int(_kc["image_xor_key"])


def to_text(blob) -> str:
    """raw_content 兼容 str/bytes/zstd 压缩 bytes，统一成 str。"""
    if blob is None:
        return ""
    if isinstance(blob, str):
        return blob
    if blob[:4] == ZSTD_MAGIC:
        try:
            import zstandard as zstd
            d = zstd.ZstdDecompressor().decompressobj()
            return d.decompress(blob, max_output_size=4 << 20).decode("utf-8", "replace")
        except Exception:
            return ""
    return bytes(blob).decode("utf-8", "replace")


def safe_name(s: str, maxlen: int = 40) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s or "").strip().strip(".")
    return s[:maxlen] or "未知"


# ---- 显示名 ----

def load_uname_map(password: bytes) -> dict:
    """微信号 → 显示名（备注优先，其次昵称）。"""
    m = {}
    contact_db = os.path.join(cfg.wechat_data_dir, cfg.wxid, "db_storage", "contact", "contact.db")
    tmp = decryptor.decrypt_db(contact_db, password)
    try:
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT username, nick_name, remark FROM contact WHERE username IS NOT NULL"
        ):
            m[row["username"]] = row["remark"] or row["nick_name"] or row["username"]
        conn.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return m


def display_name(wxid: str, uname_map: dict) -> str:
    if not wxid:
        return ""
    if wxid == cfg.wxid or wxid == _ACCOUNT_BASE:
        return "我"
    return uname_map.get(wxid, wxid)


class Name2Id:
    """惰性加载各消息库 Name2Id：real_sender_id(rowid) → 微信号。"""

    def __init__(self, password: bytes):
        self.password = password
        self.cache = {}

    def get(self, db_name: str, rid):
        if db_name not in self.cache:
            self.cache[db_name] = self._load(db_name)
        return self.cache[db_name].get(rid)

    def _load(self, db_name: str) -> dict:
        path = os.path.join(cfg.wechat_data_dir, cfg.wxid, "db_storage", "message", db_name)
        if not os.path.exists(path):
            return {}
        tmp = None
        try:
            tmp = decryptor.decrypt_db(path, self.password)
            conn = sqlite3.connect(tmp)
            m = {r[0]: r[1] for r in conn.execute("SELECT rowid, user_name FROM Name2Id")}
            conn.close()
            return m
        except Exception:
            return {}
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def sender_of(raw_text: str, row, conv_name: str, uname_map: dict, n2i: Name2Id) -> str:
    """发送者显示名：raw 前缀「wxid:\n」优先，real_sender_id → Name2Id 兜底。"""
    if raw_text and "\n" in raw_text:
        head = raw_text.split("\n", 1)[0].rstrip()
        if head.endswith(":") and 0 < len(head) <= 64:
            return display_name(head[:-1], uname_map) or head[:-1]
    rid = row["real_sender_id"]
    if rid:
        uname = n2i.get(row["db_name"], rid)
        if uname:
            return display_name(uname, uname_map) or uname
    return conv_name


# ---- 图片：local_id → 文件 MD5 映射（message_resource.db） ----
# 映射链（参考 reference/wechat-decrypt-upstream/decode_image.py）：
#   消息 local_id → MessageResourceInfo.packed_info(protobuf 含文件 MD5) → Img/<md5>[_t|_h].dat
# 注意：消息 XML 里的 md5 是「原图内容」的 md5，与本地 .dat 文件名无关，不能用于定位文件。

_PB_MD5_MARKER = b"\x12\x22\x0a\x20"


def extract_md5_from_packed_info(blob):
    """从 packed_info (protobuf) 提取 32 位 hex 文件 MD5。"""
    if not blob or not isinstance(blob, (bytes, bytearray)):
        return None
    blob = bytes(blob)
    idx = blob.find(_PB_MD5_MARKER)
    if idx >= 0 and idx + len(_PB_MD5_MARKER) + 32 <= len(blob):
        cand = blob[idx + len(_PB_MD5_MARKER): idx + len(_PB_MD5_MARKER) + 32]
        try:
            s = cand.decode("ascii")
            int(s, 16)
            return s.lower()
        except (UnicodeDecodeError, ValueError):
            pass
    # 兜底：扫描 32 字节连续小写 hex
    hex_chars = set(b"0123456789abcdef")
    i = 0
    while i <= len(blob) - 32:
        if blob[i] in hex_chars:
            cand = blob[i:i + 32]
            if all(b in hex_chars for b in cand):
                try:
                    return cand.decode("ascii")
                except UnicodeDecodeError:
                    pass
            i += 32
        else:
            i += 1
    return None


def load_resource_index(password: bytes) -> dict:
    """解密 message_resource.db，返回 {(会话 username, message_local_id): 文件 MD5}。"""
    if not os.path.exists(RESOURCE_DB):
        return {}
    tmp = None
    try:
        tmp = decryptor.decrypt_db(RESOURCE_DB, password)
        conn = sqlite3.connect(tmp)
        chat = {r[0]: r[1] for r in conn.execute("SELECT rowid, user_name FROM ChatName2Id")}
        idx = {}
        for cid, lid, pi in conn.execute(
            "SELECT chat_id, message_local_id, packed_info FROM MessageResourceInfo"
        ):
            uname = chat.get(cid)
            if not uname or not pi:
                continue
            md5 = extract_md5_from_packed_info(pi)
            if md5:
                idx[(uname, lid)] = md5
        conn.close()
        return idx
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


V1_MAGIC = b"\x07\x08V1\x08\x07"
V1_KEY = b"cfcd208495d565ef"  # V1 格式固定 AES key（md5("0")[:16]，上游已验证）


def decrypt_image(data: bytes):
    """V2/V1（AES-ECB + 明文段 + 尾部 XOR）解密；旧版单字节 XOR 兜底。失败返回 None。"""
    if data[:6] in (V2_MAGIC, V1_MAGIC) and len(data) >= 15:
        key = V1_KEY if data[:6] == V1_MAGIC else AES_KEY
        aes_size, xor_size = struct.unpack_from("<II", data, 6)
        # PKCS7 总是补 1..16 字节：aes_size 为 16 倍数时也需 +16（整块 padding）
        rem = aes_size % 16
        aligned = aes_size + (16 - rem if rem else 16)
        if len(data) < 15 + aligned + xor_size:
            return None
        ct = data[15:15 + aligned]
        raw_end = len(data) - xor_size if xor_size else len(data)
        raw = data[15 + aligned:raw_end]
        tail = data[raw_end:]
        head = AES.new(key, AES.MODE_ECB).decrypt(ct)
        try:
            head = Padding.unpad(head, AES.block_size)
        except ValueError:
            head = head[:aes_size]
        return head + raw + bytes(b ^ XOR_KEY for b in tail)
    for magic, _ext in IMG_MAGICS:
        if len(data) >= len(magic):
            k = data[0] ^ magic[0]
            if bytes(b ^ k for b in data[:len(magic)]) == magic:
                return bytes(b ^ k for b in data)
    return None


def img_ext(plain: bytes) -> str:
    for magic, ext in IMG_MAGICS:
        if plain[:len(magic)] == magic:
            return ext
    return ".bin"


def locate_image_candidates(talker_md5: str, md5s: list, ts: int) -> list:
    """返回按质量排序的候选 dat 文件：_h 原图 > 中图 > _t 缩略图。

    消息月份目录找不到时在其他月份目录兜底（月末下载延迟等）。
    """
    if not talker_md5:
        return []
    month = time.strftime("%Y-%m", time.localtime(ts))
    cands = []
    for md5 in md5s:
        if not md5:
            continue
        base = os.path.join(ATTACH_DIR, talker_md5, month, "Img", md5)
        hits = [base + s for s in ("_h.dat", ".dat", "_t.dat") if os.path.exists(base + s)]
        if not hits:
            hits = glob.glob(os.path.join(ATTACH_DIR, talker_md5, "*", "Img", md5 + "*"))
        cands.extend(hits)

    def rank(p: str) -> int:
        b = os.path.basename(p)
        if b.endswith("_h.dat"):
            return 0
        if b.endswith("_t.dat"):
            return 2
        return 1

    return sorted(dict.fromkeys(cands), key=rank)


def wxgf_to_jpg(data: bytes):
    """wxgf 容器（微信 HEIC 封装）→ HEVC Annex-B 裸流 → ffmpeg 解码首帧 JPG。

    起始码 0x00000001 / 0x000001 之后即 Annex-B 裸流，ffmpeg 可直接解码。
    失败（无 ffmpeg / 无起始码 / 解码失败）返回 None。
    """
    if not FFMPEG:
        return None
    start = data.find(b"\x00\x00\x00\x01")
    if start < 0:
        start = data.find(b"\x00\x00\x01")
    if start < 0:
        return None
    fd, tmp_in = tempfile.mkstemp(suffix=".hevc")
    os.close(fd)
    tmp_out = tmp_in + ".jpg"
    try:
        with open(tmp_in, "wb") as f:
            f.write(data[start:])
        subprocess.run(
            [FFMPEG, "-y", "-i", tmp_in, "-frames:v", "1", "-q:v", "2", tmp_out],
            capture_output=True, timeout=120,
        )
        if os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
            with open(tmp_out, "rb") as f:
                return f.read()
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        for p in (tmp_in, tmp_out):
            try:
                os.remove(p)
            except OSError:
                pass


# ---- 语音 ----

def load_voice_index(password: bytes) -> dict:
    """解密 media_0.db，返回 {(会话 username, local_id): voice_data}。"""
    if not os.path.exists(MEDIA_DB):
        return {}
    tmp = None
    try:
        tmp = decryptor.decrypt_db(MEDIA_DB, password)
        conn = sqlite3.connect(tmp)
        n2i = {r[0]: r[1] for r in conn.execute("SELECT rowid, user_name FROM Name2Id")}
        idx = {}
        for cid, lid, data in conn.execute(
            "SELECT chat_name_id, local_id, voice_data FROM VoiceInfo"
        ):
            uname = n2i.get(cid)
            if uname and data:
                idx[(uname, lid)] = bytes(data)
        conn.close()
        return idx
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def silk_to_mp3(silk_path: str, mp3_path: str) -> bool:
    """silk_v3_decoder 解 PCM，ffmpeg 封装 MP3。缺工具或失败返回 False。

    注：PCM 采样率按 SILK 默认 24kHz 处理；若转出音频变调/时长不对需调整 -ar。
    """
    if not (os.path.exists(SILK_DECODER) and FFMPEG):
        return False
    pcm = silk_path + ".pcm"
    try:
        subprocess.run([SILK_DECODER, silk_path, pcm], capture_output=True, timeout=180)
        if not (os.path.exists(pcm) and os.path.getsize(pcm) > 0):
            return False
        subprocess.run(
            [FFMPEG, "-y", "-f", "s16le", "-ar", "24000", "-ac", "1",
             "-i", pcm, "-codec:a", "libmp3lame", "-q:a", "4", mp3_path],
            capture_output=True, timeout=180,
        )
        return os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            os.remove(pcm)
        except OSError:
            pass


def load_state() -> int:
    """上次导出节点（archive 消息自增 id）；无记录返回 0（即从头）。"""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return int(json.load(f).get("last_message_id", 0))
    except (OSError, ValueError):
        return 0


def save_state(last_id: int) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"last_message_id": last_id, "updated_at": int(time.time())},
                  f, ensure_ascii=False, indent=2)


def main():
    force_all = "--all" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    t0 = t1 = None
    if args:
        t0 = int(time.mktime(time.strptime(args[0], "%Y-%m-%d")))
        end = args[1] if len(args) > 1 else args[0]
        t1 = int(time.mktime(time.strptime(end, "%Y-%m-%d"))) + 86400
    # 无日期参数且非 --all 时：增量模式，只导上次节点之后的消息
    after_id = load_state() if (not args and not force_all) else 0

    password = key_extractor.load_cached_password(cfg.key_cache_path)
    uname_map = load_uname_map(password)
    n2i = Name2Id(password)
    mapping = contacts.ContactMapping(cfg.wechat_data_dir, cfg.wxid, password)
    tables = mapping.resolve_collect_tables(contacts.load_collect_names(cfg.collect_list_path))

    conn = sqlite3.connect(cfg.archive_db_path)
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT id, db_name, table_name, local_id, create_time, local_type, raw_content, real_sender_id "
        f"FROM messages WHERE (local_type & {MASK}) IN ({TYPE_IMAGE},{TYPE_VOICE})"
    )
    params: list = []
    if tables:
        sql += f" AND table_name IN ({','.join('?' * len(tables))})"
        params += list(tables)
    if t0:
        sql += " AND create_time >= ? AND create_time < ?"
        params += [t0, t1]
        mode = f"日期范围 {args[0]} ~ {end}"
    elif after_id:
        sql += " AND id > ?"
        params.append(after_id)
        mode = f"增量（节点 id={after_id} 之后）"
    else:
        mode = "全量"
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    print(f"导出模式: {mode}；待导出图片/语音消息 {len(rows)} 条")
    if not os.path.exists(SILK_DECODER):
        print("提示：未找到 tools/silk/silk_v3_decoder.exe，语音将保留 .silk 原始格式")

    voice_idx = None
    res_idx = None
    stats = {}  # conv -> [img_ok, img_fail, voice_ok, voice_fail]
    fails = []
    max_id = after_id
    for r in rows:
        if r["id"] > max_id:
            max_id = r["id"]
        table = r["table_name"]
        conv = mapping.name_of_table(table) or table
        st = stats.setdefault(conv, [0, 0, 0, 0])
        raw = to_text(r["raw_content"])
        sender = sender_of(raw, r, conv, uname_map, n2i)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(r["create_time"]))
        base = f"{stamp}_{safe_name(sender)}_{r['local_id']}"
        mtype = r["local_type"] & MASK

        if mtype == TYPE_IMAGE:
            if res_idx is None:
                res_idx = load_resource_index(password)
            uname = mapping.username_of_table(table)
            file_md5 = res_idx.get((uname, r["local_id"])) if uname else None
            talker_md5 = table[4:] if table.startswith("Msg_") else ""
            # 依次尝试候选文件（_h 原图 > 中图 > _t 缩略图）：
            # 解密出标准格式直接采用；wxgf 尝试 ffmpeg 转 JPG（保留原图质量）；
            # 转换失败则继续尝试下一个候选，最终只能以 .heic 原样兜底
            src = plain = ext = None
            wxgf_fallback = None
            cands = locate_image_candidates(talker_md5, [file_md5], r["create_time"]) if file_md5 else []
            for cand in cands:
                try:
                    with open(cand, "rb") as f:
                        p = decrypt_image(f.read())
                except OSError:
                    p = None
                if not p:
                    continue
                if p[:4] == b"wxgf":
                    jpg = wxgf_to_jpg(p)
                    if jpg:
                        src, plain, ext = cand, jpg, ".jpg"
                        break
                    if wxgf_fallback is None:
                        wxgf_fallback = (cand, p)
                    continue
                src, plain, ext = cand, p, img_ext(p)
                break
            if plain is None and wxgf_fallback:
                src, plain = wxgf_fallback
                ext = ".heic"
            ok = False
            if plain:
                dst_dir = os.path.join(EXPORT_ROOT, safe_name(conv, 60), "img")
                os.makedirs(dst_dir, exist_ok=True)
                with open(os.path.join(dst_dir, base + ext), "wb") as f:
                    f.write(plain)
                ok = True
            st[0 if ok else 1] += 1
            if not ok:
                fails.append(f"[图片] {conv} {stamp} 文件md5={file_md5} 候选数={len(cands)}")
        else:  # TYPE_VOICE
            if voice_idx is None:
                voice_idx = load_voice_index(password)
            uname = mapping.username_of_table(table)
            data = voice_idx.get((uname, r["local_id"])) if uname else None
            ok = False
            if data:
                silk = data[1:] if data[:1] == b"\x02" else data
                dst_dir = os.path.join(EXPORT_ROOT, safe_name(conv, 60), "voice")
                os.makedirs(dst_dir, exist_ok=True)
                silk_path = os.path.join(dst_dir, base + ".silk")
                with open(silk_path, "wb") as f:
                    f.write(silk)
                mp3_path = os.path.join(dst_dir, base + ".mp3")
                if silk_to_mp3(silk_path, mp3_path):
                    os.remove(silk_path)
                ok = True
            st[2 if ok else 3] += 1
            if not ok:
                fails.append(f"[语音] {conv} {stamp} local_id={r['local_id']} (VoiceInfo 无记录)")

    print("\n== 导出统计 ==")
    ti = fi = tv = fv = 0
    for conv in sorted(stats):
        i_ok, i_bad, v_ok, v_bad = stats[conv]
        ti += i_ok; fi += i_bad; tv += v_ok; fv += v_bad
        line = f"  {conv}: 图片 {i_ok}"
        if i_bad:
            line += f"(失败{i_bad})"
        line += f"  语音 {v_ok}"
        if v_bad:
            line += f"(失败{v_bad})"
        print(line)
    print(f"  合计: 图片 {ti} 成功 / {fi} 失败；语音 {tv} 成功 / {fv} 失败")
    print(f"  输出目录: {EXPORT_ROOT}")
    if not args and max_id > after_id:
        save_state(max_id)
        print(f"  导出节点已推进到消息 id={max_id}")
    if fails:
        print(f"\n失败明细（前 20 条）:")
        for x in fails[:20]:
            print(" ", x)


if __name__ == "__main__":
    main()
