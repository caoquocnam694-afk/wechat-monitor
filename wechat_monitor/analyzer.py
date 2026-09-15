# -*- coding: utf-8 -*-
"""AI 任务分析器：调用 DeepSeek，从一批增量消息中提取「任务」。

输入：一批已经过过滤规则的消息（列表，每条含 source_name/sender/content/
     create_time/message_id 等）。
输出：任务列表，每条含 title/content/assignee/deadline/source_type/
     source_name/sender/message_time/message_id/raw_text。

负责人与完成时间：要求 AI 尽量明确；若消息里客观没有 → 输出 null，
由用户后续手动补齐。
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

_SYSTEM_PROMPT = """你是一个严谨的任务提取助手。用户会给你一批微信聊天消息（每条带编号）。
请从这些消息中提取所有「任务」。

「任务」的定义：消息中提出的、需要某人去执行、跟进、完成或确认的事项，
包括但不限于：指派、待办、请求、承诺、安排、提醒、截止事项等。

对每个任务，输出以下字段：
- index: 该任务来自哪条消息的编号（整数）
- title: 任务标题（一句话，简洁明确）
- content: 任务详细内容（完整、可独立理解）
- assignee: 任务负责人姓名。如果消息中明确提到或能明确推断出负责人，填该人姓名；
  否则填 null。
- deadline: 完成/截止时间。仅依据消息正文中明确提到的时间表述（如「明天」「下午4点」「下周三」「8月30日」）
  推断并规范为"YYYY-MM-DD HH:MM"格式；消息正文中没有任何时间表述时填 null。
  绝对不要把消息的发送时间（时间列）当作 deadline。

硬性要求：
1. 提取所有任务，不要遗漏；一条消息含多个任务时拆成多条。
2. 负责人和完成时间尽量明确，能推断就填出来，不要偷懒写 null。
3. 只在客观信息确实缺失时才填 null。
4. 没有任务的消息直接忽略。
5. 只输出一个 JSON 对象，不要输出任何解释文字。

输出格式：
{"tasks": [{"index": 0, "title": "...", "content": "...", "assignee": "...", "deadline": "..."}]}
"""


def _fmt_time(ts: Any) -> str:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (OverflowError, ValueError, OSError):
        return ""


def _fmt_date(ts: Any) -> str:
    """把时间戳格式化为「当天」日期 YYYY-MM-DD，用作无明确截止时间时的默认值。"""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (OverflowError, ValueError, OSError):
        return ""


def _extract_sender(content: str, source_name: str) -> str:
    """群消息正文以「发送者:\\n」开头，提取发送者；否则回退为会话名。"""
    if content and "\n" in content:
        head = content.split("\n", 1)[0].rstrip()
        if head.endswith(":") and len(head) <= 64:
            return head[:-1]
    return source_name


def _strip_sender_prefix(content: str) -> str:
    """去掉群消息正文开头的「发送者:\\n」前缀，返回纯正文。"""
    if content and "\n" in content:
        head, rest = content.split("\n", 1)
        if head.endswith(":") and len(head) <= 64:
            return rest
    return content


def _call_deepseek(prompt: str, api_key: str, model: str, base_url: str) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    last_err: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
        except Exception as e:  # 网络截断/超时/JSON 错误均重试
            last_err = e
            if attempt < 3:
                time.sleep(2 * attempt)
    raise RuntimeError(f"DeepSeek 调用失败: {last_err}")


def _build_prompt(messages: List[Dict[str, Any]]) -> str:
    lines = []
    for i, m in enumerate(messages):
        sender = _extract_sender(m.get("content", ""), m.get("source_name", ""))
        text = _strip_sender_prefix(m.get("content", ""))
        t = _fmt_time(m.get("create_time"))
        lines.append(
            f"{i} | 来源={m.get('source_name', '')} | 发送者={sender} "
            f"| 时间={t} | 内容={text}"
        )
    return "请分析以下消息并提取任务：\n" + "\n".join(lines)


def analyze_messages(
    messages: List[Dict[str, Any]],
    api_key: str,
    model: str = "deepseek-chat",
    base_url: str = "https://api.deepseek.com",
) -> List[Dict[str, Any]]:
    """对一批消息做任务提取。

    messages 每条需含：source_name, content, create_time, message_id；
    可选 source_type / is_group。
    返回任务列表（无任务则空列表）。
    """
    if not messages or not api_key:
        return []

    prompt = _build_prompt(messages)
    raw = _call_deepseek(prompt, api_key, model, base_url)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 容错：尝试截取首个 JSON 对象
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return []
        data = json.loads(raw[start : end + 1])

    tasks = data.get("tasks") or []
    out: List[Dict[str, Any]] = []
    for t in tasks:
        idx = t.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(messages):
            continue
        m = messages[idx]
        msg_time = _fmt_time(m.get("create_time"))
        deadline = t.get("deadline") or None
        if deadline and msg_time:
            # AI 偶尔会把消息发送时间误当成 deadline，检测到即回退为当天
            d = deadline.strip()
            if d == msg_time or d == msg_time[:16]:
                deadline = None
        # 无明确截止时间时，默认填消息当天日期（用户规则：没有截止时间就是当天）
        if not deadline:
            deadline = _fmt_date(m.get("create_time")) or None
        out.append(
            {
                "title": (t.get("title") or "").strip(),
                "content": (t.get("content") or "").strip(),
                "assignee": t.get("assignee") or None,
                "deadline": deadline,
                "source_type": "group" if m.get("is_group") else "contact",
                "source_name": m.get("source_name", ""),
                "sender": _extract_sender(m.get("content", ""), m.get("source_name", "")),
                "message_time": _fmt_time(m.get("create_time")),
                "message_id": m.get("message_id", ""),
                "raw_text": _strip_sender_prefix(m.get("content", "")),
            }
        )
    return out
