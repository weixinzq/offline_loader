"""Messaging parser for captured ``#send=<JSON>|`` messages."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable
from typing import Any


SEND_PREFIX = "#send="
SEND_SUFFIX = "|"
_REQUIRED_FIELDS = frozenset({"id", "cmd", "param"})
_JSON_DECODER = json.JSONDecoder()
_FILE_IGNORED_MESSAGES = frozenset({(18, "55_2")})


class SendMessageParseError(ValueError):
    """Raised when captured send text does not match the required format."""


class SendMessageFileError(SendMessageParseError):
    """Raised when a message file cannot be read or contains an invalid line."""


@dataclass(frozen=True)
class SendMessage:
    """One EXT message, or a local #wait/#time instruction with id=-1."""

    id: int
    cmd: str
    param: dict[str, Any]


def parse_send_message(text: str) -> SendMessage:
    """Parse one local instruction or strict ``#send=<JSON object>|`` message.

    The #send path deliberately does not trim input or infer extension IDs. File
    import applies its session-owned-message filtering only after this strict
    syntax and type validation succeeds.
    """
    if not isinstance(text, str):
        raise SendMessageParseError("消息必须是字符串")
    instruction = text.strip().removesuffix("|").strip()
    if instruction == "#wait":
        return SendMessage(-1, "#wait", {})
    if instruction.startswith("#time"):
        match = re.fullmatch(r"#time\s*=\s*(\S+)", instruction)
        try:
            seconds = float(match[1]) if match is not None else float("nan")
        except ValueError:
            seconds = float("nan")
        if not math.isfinite(seconds) or seconds < 0:
            raise SendMessageParseError("#time 必须指定大于或等于 0 的有限秒数")
        return SendMessage(-1, "#time", {"seconds": seconds})
    if not text.startswith(SEND_PREFIX):
        raise SendMessageParseError(f"消息必须以 {SEND_PREFIX!r} 开头")
    if not text.endswith(SEND_SUFFIX):
        raise SendMessageParseError(f"消息必须以 {SEND_SUFFIX!r} 结尾")

    payload = text[len(SEND_PREFIX) : -len(SEND_SUFFIX)]
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SendMessageParseError(
            f"JSON 格式错误（第 {exc.lineno} 行，第 {exc.colno} 列）：{exc.msg}"
        ) from exc

    return _validate_send_object(value)


def _validate_send_object(value: Any) -> SendMessage:
    if not isinstance(value, dict):
        raise SendMessageParseError("#send 的 JSON 内容必须是对象")

    fields = set(value)
    missing = _REQUIRED_FIELDS - fields
    if missing:
        names = "、".join(sorted(missing))
        raise SendMessageParseError(f"缺少必需字段：{names}")
    unexpected = fields - _REQUIRED_FIELDS
    if unexpected:
        names = "、".join(sorted(map(str, unexpected)))
        raise SendMessageParseError(f"包含未知字段：{names}")

    ext_id = value["id"]
    if isinstance(ext_id, bool) or not isinstance(ext_id, int):
        raise SendMessageParseError("字段 id 必须是整数")
    if not 0 <= ext_id <= 0xFFFF:
        raise SendMessageParseError("字段 id 必须在 0 到 65535 之间")

    cmd = value["cmd"]
    if not isinstance(cmd, str):
        raise SendMessageParseError("字段 cmd 必须是字符串")

    param = value["param"]
    if not isinstance(param, dict):
        raise SendMessageParseError("字段 param 必须是 JSON 对象")

    return SendMessage(id=ext_id, cmd=cmd, param=param)


def _line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def parse_pipe_separated_messages(
    text: str, source: str = "<消息文件>"
) -> list[SendMessage]:
    """Parse pipe-terminated segments, preserving pipes inside JSON strings.

    Whitespace-only segments and full-line // comments are ignored. A final
    unterminated message is accepted for compatibility with existing files.
    """
    if not isinstance(text, str):
        raise SendMessageFileError(f"{source}：消息文件内容必须是字符串")
    text = "\n".join(
        "" if line.lstrip().startswith("//") else line
        for line in text.split("\n")
    )

    length = len(text)
    position = 0

    def consume_delimiter(start: int) -> tuple[int, int]:
        current = start
        pipe_count = 0
        while current < length and (text[current] == "|" or text[current].isspace()):
            if text[current] == "|":
                pipe_count += 1
            current += 1
        if pipe_count == 0 and start != 0:
            line = _line_number(text, start)
            raise SendMessageFileError(
                f"{source}：第 {line} 行：消息之间必须使用 '|' 分隔"
            )
        return current, pipe_count

    position, _leading_pipes = consume_delimiter(position)
    messages: list[SendMessage] = []
    while position < length:
        message_line = _line_number(text, position)
        try:
            if text.startswith(SEND_PREFIX, position):
                payload_start = position + len(SEND_PREFIX)
                while payload_start < length and text[payload_start].isspace():
                    payload_start += 1
                value, payload_end = _JSON_DECODER.raw_decode(text, payload_start)
                message = _validate_send_object(value)
            else:
                payload_end = text.find("|", position)
                if payload_end == -1:
                    payload_end = length
                message = parse_send_message(text[position:payload_end].strip() + "|")
            messages.append(message)
        except json.JSONDecodeError as exc:
            line = _line_number(text, exc.pos)
            raise SendMessageFileError(
                f"{source}：第 {line} 行：JSON 格式错误：{exc.msg}"
            ) from exc
        except SendMessageParseError as exc:
            raise SendMessageFileError(
                f"{source}：第 {message_line} 行：{exc}"
            ) from exc

        position = payload_end
        while position < length and text[position].isspace():
            position += 1
        if position >= length:
            break
        if text[position] != "|":
            line = _line_number(text, position)
            raise SendMessageFileError(
                f"{source}：第 {line} 行：JSON 后必须是竖线分隔符或文件结尾"
            )
        position, _pipe_count = consume_delimiter(position)

    if not messages:
        raise SendMessageFileError(f"{source}：文件中没有可执行消息")
    return messages


def parse_send_message_lines(
    lines: Iterable[str], source: str = "<消息文件>"
) -> list[SendMessage]:
    """Parse one message per line, allowing blank lines and ``//`` comments."""
    messages: list[SendMessage] = []
    for line_number, line in enumerate(lines, start=1):
        text = line.rstrip("\r\n")
        if not text.strip() or text.lstrip().startswith("//"):
            continue
        try:
            messages.append(parse_send_message(text))
        except SendMessageParseError as exc:
            raise SendMessageFileError(
                f"{source}：第 {line_number} 行：{exc}"
            ) from exc
    return messages


def parse_send_message_file(path: Path) -> list[SendMessage]:
    """Read and fully validate a UTF-8 message file before any sending occurs."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
        messages = parse_pipe_separated_messages(text, str(path))
    except SendMessageFileError:
        raise
    except (OSError, UnicodeError) as exc:
        raise SendMessageFileError(f"{path}：无法读取文件：{exc}") from exc
    messages = [
        message
        for message in messages
        if (message.id, message.cmd) not in _FILE_IGNORED_MESSAGES
    ]
    if not messages:
        raise SendMessageFileError(
            f"{path}：文件中没有可执行消息（id=18/cmd=55_2 由会话服务管理）"
        )
    return messages
