"""Pure helpers for building and parsing game protocol messages."""
from __future__ import annotations

import struct

from src.protocol.amf3 import Amf3Reader, Amf3Writer
from src.config import MSG_TYPE_EXT, MSG_TYPE_SYS, UpCmd
from src.protocol.encrypt import encrypt_byte_array


def pack_utf(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def pack_short(value: int) -> bytes:
    return struct.pack(">H", value & 0xFFFF)


def pack_int(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFFFF)


def build_message(msg_type: int, action: int, room_id: int, body: bytes) -> bytes:
    return (
        struct.pack(">B", msg_type)
        + pack_short(action)
        + pack_int(room_id)
        + body
    )


def build_login_message(zone: str, user_id: str, session_id: str) -> bytes:
    body = pack_utf(zone) + pack_utf(user_id) + pack_utf(session_id)
    return build_message(MSG_TYPE_SYS, UpCmd.Login, -1, body)


def build_xt_request(
    ext_id: int,
    cmd: str,
    params: dict,
    seq: int,
    room_id: int = -1,
    encrypt: bool = True,
) -> bytes:
    """Build the inner EXT request body, including its encrypted AMF3 data."""
    params[":ext_seq;"] = seq

    params_writer = Amf3Writer()
    params_writer.write_object(params)
    params_bytes = bytearray(params_writer.to_bytes())
    encoded_params = encrypt_byte_array(seq, params_bytes) if encrypt else params_bytes

    wrapper = Amf3Writer()
    wrapper.write_map({"data": bytes(encoded_params)})

    return (
        struct.pack(">B", MSG_TYPE_EXT)
        + pack_short(UpCmd.XtReq)
        + pack_int(room_id)
        + pack_short(ext_id)
        + pack_utf(cmd)
        + wrapper.to_bytes()
    )


def parse_legacy_fields(data: bytes) -> dict:
    """Parse names followed by AMF3 values, with an optional EXT prefix.

    The field names are AMF3 strings without type markers and share their
    string-reference table with the values that follow them.
    """
    starts = [0]
    if data[:1] == bytes((MSG_TYPE_EXT,)):
        starts.append(1)

    for start in starts:
        payload = data[start:]
        position = 0
        names: list[str] = []

        while position < len(payload):
            if names:
                reader = Amf3Reader(payload[position:])
                reader._string_table.extend(names)
                fields = {}
                try:
                    for name in names:
                        fields[name] = reader.read_object()
                except (EOFError, ValueError, IndexError, struct.error):
                    fields = {}
                if fields and reader._pos == len(payload) - position:
                    return fields

            name_length = payload[position]
            if name_length < 3 or name_length > 200 or name_length % 2 == 0:
                break

            byte_length = (name_length - 1) // 2
            name_start = position + 1
            name_end = name_start + byte_length
            if name_end > len(payload):
                break
            try:
                name = payload[name_start:name_end].decode("utf-8")
            except UnicodeDecodeError:
                break
            if not name or not name.isprintable():
                break

            names.append(name)
            position = name_start + name_length // 2

    return {}
