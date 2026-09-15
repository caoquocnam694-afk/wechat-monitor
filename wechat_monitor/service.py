# -*- coding: utf-8 -*-
"""守护服务：纯采集管道（实时推送）。

- 轮询：每 poll_interval_seconds 秒，把「白名单会话」的新消息增量归档。
- 推送：每轮轮询结束后立即推送（实时）；若某次推送失败，游标不前进，
  下一轮自动重试，直到成功为止。
- 多接收端点：推送到全部 webhook_urls，任一失败则整体失败并重试。

采集端无 AI、无语义判断；白名单来自 collect_list.txt（每轮重新读取，热加载）。
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional, Set

from . import contacts, formatter, key_extractor, poller, pusher
from .archive import Archive
from .config import Config


class Service:
    def __init__(self, config: Config):
        self.config = config
        self._stop = False
        self._password: Optional[bytes] = None
        self._logger = self._setup_logger(config.log_path)

    def _setup_logger(self, log_path: str) -> logging.Logger:
        logger = logging.getLogger("wechat_monitor")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        return logger

    def _resolve_wxid(self) -> str:
        if self.config.wxid:
            return self.config.wxid
        accounts = poller.discover_accounts(self.config.wechat_data_dir)
        if not accounts:
            raise RuntimeError(f"未发现微信账号目录: {self.config.wechat_data_dir}")
        return accounts[0]

    def _get_password(self) -> bytes:
        if self._password is None:
            self._password = key_extractor.load_cached_password(self.config.key_cache_path)
        if self._password is None:
            raise RuntimeError(
                "未找到密钥缓存，请先运行 `python main.py --extract-key` 提取密钥（需先退出微信）。"
            )
        return self._password

    def _resolve_collect_tables(self, password: bytes, wxid: str):
        """读白名单文件 → 解析成消息表名集合（每轮重新读取，支持热加载）。"""
        names = contacts.load_collect_names(self.config.collect_list_path)
        mapping = contacts.ContactMapping(self.config.wechat_data_dir, wxid, password)
        tables = mapping.resolve_collect_tables(names)
        missing = [n for n in names if n not in mapping.name_to_table]
        return tables, mapping, missing

    # ---- 推送 ----

    def _push_batch(
        self,
        archive: Archive,
        tables: Set[str],
        mapping: "contacts.ContactMapping",
        wxid: str,
    ) -> bool:
        """取上次推送以来的新消息 → 格式化 → 批量推送 → 成功后推进游标。

        返回是否全部推送成功；失败时游标不前进，调用方应置 push_pending 以便重试。
        """
        if not tables:
            self._logger.warning("白名单为空或全部无法解析，本轮不推送")
            return False
        last_id = archive.get_last_pushed_id()
        msgs = archive.fetch_messages_after_id(last_id, tables)
        if not msgs:
            return True

        events = []
        for m in msgs:
            ev = formatter.format_event(
                m,
                mapping,
                wechat_data_dir=self.config.wechat_data_dir,
                wxid=wxid,
                collect_dir=self.config.file_collect_dir,
            )
            if ev is not None:
                events.append(ev)

        if not events:
            # 没有可推送的事件（如全是图片/语音等未纳入类型），游标照常前进
            archive.set_last_pushed_id(msgs[-1]["id"])
            self._logger.info(
                "实时推送：%d 条新消息，无 text/file 事件，游标推进到 %d",
                len(msgs), msgs[-1]["id"],
            )
            return True

        ok_cnt, fail_cnt = pusher.push_all(
            events, self.config.webhook_urls, self.config.bearer_token
        )
        if fail_cnt == 0:
            archive.set_last_pushed_id(msgs[-1]["id"])
            self._logger.info(
                "实时推送：%d 条事件全部成功（%d 个端点），游标推进到 %d",
                ok_cnt, len(self.config.webhook_urls), msgs[-1]["id"],
            )
            return True
        self._logger.error(
            "实时推送：%d 成功 / %d 失败，游标不推进（下一轮自动重试）", ok_cnt, fail_cnt
        )
        return False

    # ---- 主循环 ----

    def run(self) -> None:
        wxid = self._resolve_wxid()
        self._logger.info(
            "启动实时采集推送：账号=%s 数据目录=%s 轮询间隔=%ss 接收端点=%d 个",
            wxid, self.config.wechat_data_dir,
            self.config.poll_interval_seconds, len(self.config.webhook_urls),
        )
        archive = Archive(self.config.archive_db_path)
        push_pending = False
        try:
            while not self._stop:
                try:
                    password = self._get_password()
                    tables, mapping, missing = self._resolve_collect_tables(password, wxid)
                    if missing:
                        self._logger.warning(
                            "白名单中有 %d 个名字在联系人里查不到：%s",
                            len(missing), ", ".join(missing),
                        )
                    added = poller.poll_account(
                        self.config.wechat_data_dir, wxid, password, archive, tables
                    )
                    if added:
                        self._logger.info("本轮轮询：新增 %d 条消息", added)

                    # 实时推送：有新增，或上次推送失败待重试时立即推送
                    if added or push_pending:
                        try:
                            ok = self._push_batch(archive, tables, mapping, wxid)
                            push_pending = not ok
                        except Exception as e:
                            self._logger.error("实时推送出错: %s", e)
                            push_pending = True
                except ValueError as e:
                    self._logger.error("解密失败（密钥可能已失效）: %s", e)
                    self._logger.error("请重新运行 `python main.py --extract-key` 更新密钥缓存")
                    self._password = None
                except RuntimeError as e:
                    self._logger.error("%s", e)
                    self._password = None
                except Exception as e:
                    self._logger.error("轮询出错: %s", e)
                self._sleep_interruptibly(self.config.poll_interval_seconds)
        finally:
            archive.close()
            self._logger.info("归档库已关闭，服务退出")

    def _sleep_interruptibly(self, seconds: int) -> None:
        end = time.time() + seconds
        while time.time() < end and not self._stop:
            time.sleep(0.5)

    def stop(self) -> None:
        self._stop = True

    def push_once(self) -> None:
        """手动触发一次完整流程：轮询归档 → 推送。"""
        wxid = self._resolve_wxid()
        password = self._get_password()
        archive = Archive(self.config.archive_db_path)
        try:
            tables, mapping, missing = self._resolve_collect_tables(password, wxid)
            if missing:
                self._logger.warning(
                    "白名单中有 %d 个名字查不到：%s", len(missing), ", ".join(missing)
                )
            added = poller.poll_account(
                self.config.wechat_data_dir, wxid, password, archive, tables
            )
            self._logger.info("轮询完成，新增 %d 条消息", added)
            self._push_batch(archive, tables, mapping, wxid)
        finally:
            archive.close()
