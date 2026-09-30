"""Bounded Responses SSE reader. No tools or partial answers are accepted."""

from __future__ import annotations

import json
import time
from typing import Any


class ResponseStreamError(ValueError):
    """Safe diagnostic; provider payloads must never be included in this error."""


def _object(text: str) -> dict:
    try:
        value = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError) as exc:
        raise ResponseStreamError("Responses 返回了无效的 JSON 事件。") from exc
    if not isinstance(value, dict):
        raise ResponseStreamError("Responses 事件必须是对象。")
    return value


def completed_text(payload: dict) -> str:
    if payload.get("status") != "completed" or payload.get("error") or payload.get("incomplete_details"):
        raise ResponseStreamError("Responses 输出未完整结束；请检查输出限额或稍后重试。")
    output = payload.get("output")
    if not isinstance(output, list):
        raise ResponseStreamError("Responses 缺少完整输出。")
    fragments: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            raise ResponseStreamError("Responses 输出格式无效。")
        kind = item.get("type")
        if kind == "reasoning":
            continue
        if kind != "message" or item.get("role") != "assistant":
            raise ResponseStreamError("Responses 包含非文本或工具输出，未接受评分。")
        if item.get("status", "completed") != "completed":
            raise ResponseStreamError("Responses 消息未完整结束。")
        parts = item.get("content")
        if not isinstance(parts, list):
            raise ResponseStreamError("Responses 消息内容格式无效。")
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                raise ResponseStreamError("Responses 返回拒绝或非文本内容，未接受评分。")
            text = part.get("text")
            if not isinstance(text, str):
                raise ResponseStreamError("Responses 文本内容无效。")
            fragments.append(text)
    text = "".join(fragments)
    if not text.strip():
        raise ResponseStreamError("Responses 返回空内容，未接受评分。")
    return text


def read_completed_response(response: Any, *, max_bytes: int, timeout: float) -> tuple[str, dict]:
    """Consume UTF-8 SSE through a completed event, with byte and wall-time caps.

    A delta or output_text.done is not a successful response. Only the canonical
    completed response is used, so partial output can never become a grade.
    """
    start = time.monotonic()
    buffer = b""
    size = 0
    data: list[str] = []
    event_name = ""
    completed: dict | None = None

    def consume_event() -> None:
        nonlocal completed, data, event_name
        if not data:
            event_name = ""
            return
        encoded = "\n".join(data)
        data = []
        if encoded == "[DONE]":
            if completed is None:
                raise ResponseStreamError("Responses 流缺少 response.completed，未接受部分结果。")
            event_name = ""
            return
        event = _object(encoded)
        kind = event.get("type", event_name)
        event_name = ""
        if not isinstance(kind, str):
            raise ResponseStreamError("Responses 事件类型无效。")
        if kind in {"error", "response.failed", "response.incomplete"} or "refusal" in kind:
            raise ResponseStreamError("Responses 失败、拒绝或输出不完整，未接受评分。")
        if "tool" in kind or "function_call" in kind:
            raise ResponseStreamError("Responses 返回工具事件，未接受评分。")
        if kind in {"response.content_part.added", "response.content_part.done"}:
            part = event.get("part")
            if isinstance(part, dict) and part.get("type") == "refusal":
                raise ResponseStreamError("Responses 返回拒绝内容，未接受评分。")
        if kind in {"response.output_item.added", "response.output_item.done"}:
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") not in {"message", "reasoning"}:
                raise ResponseStreamError("Responses 返回非文本输出，未接受评分。")
        if kind == "response.completed":
            if completed is not None:
                raise ResponseStreamError("Responses 流包含重复完成事件。")
            payload = event.get("response")
            if not isinstance(payload, dict):
                raise ResponseStreamError("Responses 完成事件缺少响应对象。")
            completed_text(payload)
            completed = payload

    def consume_line(raw: bytes) -> None:
        nonlocal event_name
        try:
            line = raw.rstrip(b"\r").decode("utf-8")
        except UnicodeError as exc:
            raise ResponseStreamError("Responses 流编码无效。") from exc
        if not line:
            consume_event()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line.startswith("event:"):
            event_name = line[6:].strip()

    for chunk in response.iter_content(chunk_size=65536):
        if time.monotonic() - start > timeout:
            raise ResponseStreamError("Responses 流总耗时超过 timeout；未接受部分结果。")
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise ResponseStreamError("Responses 流格式无效。")
        size += len(chunk)
        if size > max_bytes:
            raise ResponseStreamError("Responses 流超过 max_response_bytes；未接受部分结果。")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            consume_line(line)
        if completed is not None:
            # A completed response is terminal. Closing the caller's response
            # prevents a gateway that keeps the SSE socket open from hanging.
            return completed_text(completed), completed
    if buffer:
        consume_line(buffer)
    consume_event()
    if completed is None:
        raise ResponseStreamError("Responses 流缺少 response.completed，未接受部分结果。")
    return completed_text(completed), completed
