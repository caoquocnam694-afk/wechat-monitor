# -*- coding: utf-8 -*-
"""消息过滤规则引擎：判断一条消息是否命中「需要 AI 分析」的规则。

规则（满足任一条即分析）：
1. 消息来自白名单群（group_whitelist，按群名匹配）；
2. 消息 @ 了本人（my_nicknames），排除 @所有人/@全体成员；
3. 消息正文包含关键词（keywords）。
"""
from __future__ import annotations

from typing import Iterable, List

# 微信 PC 版 @ 之后的分隔符（特殊空格 U+2005）
AT_SEP = " "

# 视为「@所有人」而非「@具体某人」的标识
_EXCLUDE_ATS = {"所有人", "全部人", "全体成员", "all", "All", "ALL", "everyone", "全体"}


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


class Rules:
    def __init__(self, group_whitelist: Iterable[str], my_nicknames: Iterable[str], keywords: Iterable[str]):
        self.group_whitelist = set(g for g in group_whitelist if g)
        self.my_nicknames = [n for n in my_nicknames if n]
        self.keywords = [k for k in keywords if k]

    def is_at_me(self, content: str) -> bool:
        """消息是否 @ 了本人（排除 @所有人）。"""
        for at in extract_ats(content):
            if at in _EXCLUDE_ATS:
                continue
            if at in self.my_nicknames:
                return True
        return False

    def should_analyze(self, source_name: str, content: str) -> bool:
        """判断一条消息（来源会话名 + 文本）是否需要分析。"""
        # 规则 1：白名单群
        if source_name in self.group_whitelist:
            return True
        # 规则 2：@ 本人
        if self.is_at_me(content):
            return True
        # 规则 3：关键词
        for kw in self.keywords:
            if kw in content:
                return True
        return False
