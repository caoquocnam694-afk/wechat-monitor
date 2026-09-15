# -*- coding: utf-8 -*-
"""从微信进程提取数据库主密钥（Frida spawn），并支持本地缓存。

微信 4.1.11+ 不再以 `x'<hex>'` 字符串缓存密钥，password 以二进制形式存在于
codec 配置函数参数中，因此必须用 Frida spawn 在启动早期 hook 才能捕获。
"""
from __future__ import annotations

import os
import time
from typing import Optional

import frida

# weixin.dll 偏移（4.1.12.26 验证）
CODEC_CFG_OFFSET = 0x3486140
PASSWORD_LEN = 32


_FRIDA_JS = r"""
function tryHook() {
    try {
        var mod = Process.getModuleByName("Weixin.dll");
        if (!mod) return false;
        var base = mod.base;
        Interceptor.attach(base.add(%(offset)d), {
            onEnter: function (args) {
                try {
                    var ctx = this.context;
                    // frida 17 移除了 Memory.readByteArray，改用指针对象的 readByteArray
                    var buf = ctx.rcx.readByteArray(%(plen)d);
                    send({type: "password", data: Array.from(new Uint8Array(buf))});
                } catch (e) {
                    send({type: "error", data: String(e)});
                }
            }
        });
        send({type: "hooked"});
        return true;
    } catch (e) {
        return false;
    }
}
var tries = 0;
var timer = setInterval(function () {
    if (tryHook()) { clearInterval(timer); }
    else if (++tries > 60) { clearInterval(timer); send({type: "error", data: "Weixin.dll 未加载"}); }
}, 2000);
"""


def _is_weixin_running() -> bool:
    device = frida.get_local_device()
    for p in device.enumerate_processes():
        if p.name.lower() in ("weixin.exe", "wechat.exe"):
            return True
    return False


def extract_password(weixin_path: str, timeout: float = 180.0) -> bytes:
    """spawn 微信并 hook codec 配置函数，返回 32 字节 password。

    要求：调用前必须先关闭已运行的微信（spawn 需要启动早期注入）。
    """
    if _is_weixin_running():
        raise RuntimeError(
            "检测到微信正在运行。密钥提取需要 Frida spawn 在启动早期注入，"
            "请先退出微信后再执行（提取完成后可重新登录微信）。"
        )

    device = frida.get_local_device()
    pid = device.spawn([weixin_path])
    session = device.attach(pid)
    script = session.create_script(_FRIDA_JS % {"offset": CODEC_CFG_OFFSET, "plen": PASSWORD_LEN})

    result: dict = {}

    def on_message(message, data):
        if message.get("type") == "send":
            payload = message.get("payload", {})
            if payload.get("type") == "password":
                result["password"] = bytes(payload["data"])
            elif payload.get("type") == "error":
                result["error"] = payload.get("data")

    script.on("message", on_message)
    script.load()
    device.resume(pid)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if "password" in result or "error" in result:
            break
        time.sleep(0.5)

    try:
        session.detach()
    except Exception:
        pass

    if "password" in result:
        return result["password"]
    raise RuntimeError("密钥提取失败: " + str(result.get("error", "超时未捕获到密钥")))


def load_cached_password(key_cache_path: str) -> Optional[bytes]:
    if os.path.exists(key_cache_path):
        with open(key_cache_path, "r", encoding="utf-8") as f:
            hex_str = f.read().strip()
        if hex_str:
            try:
                return bytes.fromhex(hex_str)
            except ValueError:
                return None
    return None


def save_password(key_cache_path: str, password: bytes) -> None:
    parent = os.path.dirname(key_cache_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(key_cache_path, "w", encoding="utf-8") as f:
        f.write(password.hex())


def get_password(weixin_path: str, key_cache_path: str, force: bool = False) -> bytes:
    """优先读缓存，缺失或 force 时用 Frida 重新提取并缓存。"""
    if not force:
        cached = load_cached_password(key_cache_path)
        if cached:
            return cached
    password = extract_password(weixin_path)
    save_password(key_cache_path, password)
    return password
