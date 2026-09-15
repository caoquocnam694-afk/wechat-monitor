# -*- coding: utf-8 -*-
"""配置加载：从 JSON 文件读取运行参数，缺省时采用默认值。

采集端是纯管道（无 AI）：只负责把「白名单会话」里的消息与文件，
按「消息事件」结构批量推送到用户系统。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

DEFAULTS = {
    # 必须在 config.json 中填写；公开版本不包含任何本机路径。
    "wechat_data_dir": "",
    "archive_db_path": "archive.db",
    "poll_interval_seconds": 30,
    "key_cache_path": "password.key",
    "weixin_path": "",
    "log_path": "wechat_monitor.log",
    "wxid": "",
    # 会话白名单（每行一个群名/联系人名），采集范围唯一依据
    "collect_list_path": "collect_list.txt",
    # 消息事件推送（实时，可多个接收端点，全部成功后游标才前进）
    "webhook_urls": [],
    "bearer_token": "",
    # 文件收集：文件本体落盘目录（相对路径解析到项目根目录）
    "file_collect_dir": "collected_files",
}


@dataclass
class Config:
    wechat_data_dir: str
    archive_db_path: str
    poll_interval_seconds: int = 30
    key_cache_path: str = "password.key"
    weixin_path: str = ""
    log_path: str = "wechat_monitor.log"
    wxid: str = ""
    collect_list_path: str = "collect_list.txt"
    webhook_urls: List[str] = field(default_factory=list)
    bearer_token: str = ""
    file_collect_dir: str = "collected_files"

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        cfg = dict(DEFAULTS)
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            cfg.update({k: v for k, v in loaded.items() if k in cfg})
        return cls(**cfg)

    def resolved(self, base_dir: str | Path | None = None) -> "Config":
        """把相对路径解析到项目根目录下，返回一个新的 Config。"""
        root = Path(base_dir) if base_dir else Path.cwd()
        for key in (
            "archive_db_path",
            "key_cache_path",
            "log_path",
            "collect_list_path",
            "file_collect_dir",
        ):
            val = getattr(self, key)
            if val and not os.path.isabs(val):
                setattr(self, key, str(root / val))
        return self
