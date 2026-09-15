# -*- coding: utf-8 -*-
"""入口：密钥提取 / 单次轮询 / 单次推送 / 守护服务。"""
from __future__ import annotations

import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from wechat_monitor.config import Config


def cmd_extract_key(config: Config) -> None:
    from wechat_monitor import key_extractor

    password = key_extractor.extract_password(config.weixin_path)
    key_extractor.save_password(config.key_cache_path, password)
    print(f"密钥已提取并缓存到 {config.key_cache_path}")
    print(f"password hex: {password.hex()}")


def cmd_once(config: Config) -> None:
    from wechat_monitor import contacts, key_extractor, poller
    from wechat_monitor.archive import Archive

    password = key_extractor.load_cached_password(config.key_cache_path)
    if password is None:
        print("未找到密钥缓存，请先运行 `python main.py --extract-key`")
        return
    accounts = poller.discover_accounts(config.wechat_data_dir)
    wxid = config.wxid or (accounts[0] if accounts else None)
    if wxid is None:
        print(f"未发现微信账号目录: {config.wechat_data_dir}")
        return
    names = contacts.load_collect_names(config.collect_list_path)
    mapping = contacts.ContactMapping(config.wechat_data_dir, wxid, password)
    tables = mapping.resolve_collect_tables(names)
    print(f"白名单 {len(names)} 个名字，解析出 {len(tables)} 个会话表")
    archive = Archive(config.archive_db_path)
    try:
        added = poller.poll_account(config.wechat_data_dir, wxid, password, archive, tables)
        print(f"账号 {wxid} 单次轮询完成，新增 {added} 条消息")
    finally:
        archive.close()


def cmd_run(config: Config) -> None:
    from wechat_monitor.service import Service

    service = Service(config)
    try:
        service.run()
    except KeyboardInterrupt:
        service.stop()


def cmd_push_once(config: Config) -> None:
    from wechat_monitor.service import Service

    service = Service(config)
    service.push_once()


def main() -> None:
    parser = argparse.ArgumentParser(description="微信聊天记录增量采集与推送")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--extract-key", action="store_true", help="提取密钥并缓存（需先退出微信）")
    parser.add_argument("--once", action="store_true", help="只轮询一次后退出")
    parser.add_argument("--push-once", action="store_true", help="轮询一次并执行一次批量推送后退出")
    args = parser.parse_args()

    config = Config.load(args.config).resolved()

    if args.extract_key:
        cmd_extract_key(config)
    elif args.once:
        cmd_once(config)
    elif args.push_once:
        cmd_push_once(config)
    else:
        cmd_run(config)


if __name__ == "__main__":
    main()
