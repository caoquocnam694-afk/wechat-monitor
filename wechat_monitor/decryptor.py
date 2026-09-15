# -*- coding: utf-8 -*-
"""微信 4.x SQLCipher 4 数据库只读解密。

参数（微信 4.1.12.26 验证）：
- 加密：AES-256-CBC + HMAC-SHA512
- 页大小 4096，reserve 80（IV 16 @ 4016，HMAC 64 @ 4032）
- 每库独立 salt = 文件头 16 字节；password 经 PBKDF2 派生 enc_key。
本模块只读原库，解密结果写入临时文件，绝不改写原数据库。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
import tempfile
from typing import Tuple

from Crypto.Cipher import AES

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80
SQLITE_HDR = b"SQLite format 3\x00"
PBKDF2_ITERATIONS = 256000


def derive_enc_key(password: bytes, salt: bytes) -> bytes:
    """由 password 和每库 salt 派生 enc_key。"""
    return hashlib.pbkdf2_hmac("sha512", password, salt, PBKDF2_ITERATIONS, dklen=KEY_SZ)


def derive_mac_key(enc_key: bytes, salt: bytes) -> bytes:
    """由 enc_key 和 salt 派生 MAC 密钥（SQLCipher 4 标准）。"""
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)


def verify_page1(enc_key: bytes, salt: bytes, page1: bytes) -> bool:
    """校验第 1 页 HMAC，用于判断 password 是否匹配该库。"""
    mac_key = derive_mac_key(enc_key, salt)
    hmac_data = page1[SALT_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]
    stored_hmac = page1[PAGE_SZ - HMAC_SZ : PAGE_SZ]
    hh = hmac.new(mac_key, hmac_data, hashlib.sha512)
    hh.update(struct.pack("<I", 1))
    return hh.digest() == stored_hmac


def decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    """解密单个页。第 1 页需要还原 SQLite 头。"""
    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
        page = SQLITE_HDR + AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted) + b"\x00" * RESERVE_SZ
    else:
        encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
        page = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted) + b"\x00" * RESERVE_SZ
    return page


def decrypt_db_to_file(db_path: str, out_path: str, enc_key: bytes) -> int:
    """把加密库完整解密到 out_path，返回页数。"""
    file_size = os.path.getsize(db_path)
    total_pages = (file_size + PAGE_SZ - 1) // PAGE_SZ
    with open(db_path, "rb") as fin, open(out_path, "wb") as fout:
        for pgno in range(1, total_pages + 1):
            page_data = fin.read(PAGE_SZ)
            if len(page_data) < PAGE_SZ:
                page_data += b"\x00" * (PAGE_SZ - len(page_data))
            fout.write(decrypt_page(enc_key, page_data, pgno))
    return total_pages


def read_salt(db_path: str) -> Tuple[bytes, bytes]:
    """读取首字节前 4096 字节，返回 (salt, page1)。"""
    with open(db_path, "rb") as fh:
        page1 = fh.read(PAGE_SZ)
    if len(page1) < PAGE_SZ:
        raise ValueError(f"数据库过小，无法解密: {db_path}")
    return page1[:SALT_SZ], page1


def decrypt_db(db_path: str, password: bytes) -> str:
    """解密单个数据库到临时文件，返回临时文件路径（调用方负责查询后删除）。"""
    salt, page1 = read_salt(db_path)
    enc_key = derive_enc_key(password, salt)
    if not verify_page1(enc_key, salt, page1):
        raise ValueError(f"密码不匹配该数据库（HMAC 校验失败）: {db_path}")
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    decrypt_db_to_file(db_path, tmp, enc_key)
    return tmp
