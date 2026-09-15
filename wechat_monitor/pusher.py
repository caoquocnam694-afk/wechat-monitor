# -*- coding: utf-8 -*-
"""消息事件推送器：把「消息事件」批量 POST 到用户系统的多个接收端点。

请求体：
    {"source": "wechat-tool", "events": [...]}

鉴权：Bearer Token（请求头 Authorization: Bearer <token>）。
支持多个 webhook 地址：全部端点返回 2xx 才算推送成功（任一失败则整体失败，
游标不前进，由服务层重试；各端点按 message_id 幂等去重）。
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any, Dict, List, Tuple

# 用户系统每条事件可能做 AI 处理，批量过大或超时过短都易失败；用小批量 + 长超时
_TIMEOUT = 300
_BATCH_SIZE = 20


def _post(url: str, payload: Dict[str, Any], bearer_token: str) -> Tuple[bool, str]:
    """向单个端点 POST 一批事件。返回 (是否成功, 说明)。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if bearer_token:
        headers["Authorization"] = "Bearer " + bearer_token

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        return False, f"网络错误: {e.reason}"
    except TimeoutError as e:
        return False, f"超时: {e}"

    if 200 <= status < 300:
        return True, f"HTTP {status}"
    return False, f"HTTP {status} {body[:300]}"


def push_batch(
    events: List[Dict[str, Any]],
    webhook_urls: List[str],
    bearer_token: str,
) -> Tuple[bool, str]:
    """把同一批事件推送到所有端点。返回 (是否全部成功, 说明)。"""
    if not webhook_urls:
        return False, "webhook_urls 为空，跳过"
    if not events:
        return True, "空事件列表，跳过"

    payload = {"source": "wechat-tool", "events": events}
    for url in webhook_urls:
        ok, msg = _post(url, payload, bearer_token)
        if not ok:
            return False, f"端点 {url}: {msg}"
    return True, f"全部 {len(webhook_urls)} 个端点成功"


def push_all(
    events: List[Dict[str, Any]],
    webhook_urls: List[str],
    bearer_token: str,
) -> Tuple[int, int]:
    """按 _BATCH_SIZE 分批推送全部事件到所有端点。返回 (成功数, 失败数)。"""
    if not events:
        return 0, 0
    ok_cnt = 0
    fail_cnt = 0
    for i in range(0, len(events), _BATCH_SIZE):
        batch = events[i : i + _BATCH_SIZE]
        ok, _msg = push_batch(batch, webhook_urls, bearer_token)
        if ok:
            ok_cnt += len(batch)
        else:
            fail_cnt += len(batch)
            break  # 失败即停，避免乱序
    return ok_cnt, fail_cnt
