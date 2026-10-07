"""
amf3.py — 协议层 AMF3 序列化/反序列化，精确对照 H5 源码
src: Amf3Output.ts, Amf3Input.ts, Amf3Types.ts, ByteArray.ts
"""
from __future__ import annotations
import struct
from typing import Any
from src.config import Amf3Type


# ── AMF3 U29 编码/解码 ──

def write_u29(buf: bytearray, ref: int) -> None:
    """Amf3Output.ts:146-168 — 精确对照"""
    ref = ref & 0x3FFFFFFF
    if ref < 128:
        buf.append(ref)
    elif ref < 16384:
        buf.append((ref >> 7 & 0x7F) | 0x80)
        buf.append(ref & 0x7F)
    elif ref < 2097152:
        buf.append((ref >> 14 & 0x7F) | 0x80)
        buf.append((ref >> 7 & 0x7F) | 0x80)
        buf.append(ref & 0x7F)
    elif ref < 1073741824:
        buf.append((ref >> 22 & 0x7F) | 0x80)
        buf.append((ref >> 15 & 0x7F) | 0x80)
        buf.append((ref >> 8 & 0x7F) | 0x80)
        buf.append(ref & 0xFF)
    else:
        raise ValueError(f"U29 out of range: {ref}")


# ── AMF3 序列化 (对照 Amf3Output.ts) ──

class Amf3Writer:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._string_table: dict[str, int] = {}
        self._object_table: dict[int, int] = {}
        self._traits_count = 0

    def _write_byte(self, b: int) -> None:
        self._buf.append(b & 0xFF)

    def _write_bytes(self, data: bytes) -> None:
        self._buf.extend(data)

    def _write_u29(self, ref: int) -> None:
        write_u29(self._buf, ref)

    def _string_by_reference(self, s: str) -> bool:
        ref = self._string_table.get(s)
        if ref is not None:
            self._write_u29(ref << 1)
            return True
        self._string_table[s] = len(self._string_table)
        return False

    def _object_by_reference(self, o: Any) -> bool:
        oid = id(o)
        ref = self._object_table.get(oid)
        if ref is not None:
            self._write_u29(ref << 1)
            return True
        self._object_table[oid] = len(self._object_table)
        return False

    def _write_string_without_type(self, s: str) -> None:
        """Amf3Output.ts:122-145"""
        if len(s) == 0:
            self._write_u29(1)
            return
        if self._string_by_reference(s):
            return
        utf_len = len(s.encode('utf-8'))
        self._write_u29((utf_len << 1) | 0x1)
        self._buf.extend(s.encode('utf-8'))

    def write_null(self) -> None:
        self._write_byte(Amf3Type.kNullType)

    def write_bool(self, b: bool) -> None:
        self._write_byte(Amf3Type.kTrueType if b else Amf3Type.kFalseType)

    def write_int(self, i: int) -> None:
        """Amf3Output.ts:58-67"""
        if -268435456 <= i <= 268435455:
            self._write_byte(Amf3Type.kIntegerType)
            self._write_u29(i & 0x1FFFFFFF)
        else:
            self.write_double(float(i))

    def write_double(self, d: float) -> None:
        self._write_byte(Amf3Type.kDoubleType)
        self._buf.extend(struct.pack('>d', d))

    def write_string(self, s: str) -> None:
        self._write_byte(Amf3Type.kStringType)
        self._write_string_without_type(s)

    def write_bytearray(self, data: bytes) -> None:
        self._write_byte(Amf3Type.kByteArrayType)
        if not self._object_by_reference(data):
            _len = len(data)
            self._write_u29((_len << 1) | 0x1)
            self._buf.extend(data)

    def write_array(self, arr: list) -> None:
        self._write_byte(Amf3Type.kArrayType)
        if not self._object_by_reference(arr):
            _len = len(arr)
            self._write_u29((_len << 1) | 1)
            self._write_u29(1)
            for item in arr:
                self.write_object(item)

    def write_map(self, obj: dict) -> None:
        """Amf3Output.ts:103-120"""
        self._write_byte(Amf3Type.kObjectType)
        if not self._object_by_reference(obj):
            self._write_u29(11)
            self._traits_count += 1
            self._write_string_without_type("")
            for key in obj:
                key_str = str(key) if key is not None else ""
                self._write_string_without_type(key_str)
                self.write_object(obj[key])
            self._write_string_without_type("")

    def write_object(self, obj: Any) -> None:
        """Amf3Output.ts:22-50"""
        if obj is None:
            self.write_null()
        elif isinstance(obj, bool):
            self.write_bool(obj)
        elif isinstance(obj, int):
            self.write_int(obj)
        elif isinstance(obj, float):
            self.write_double(obj)
        elif isinstance(obj, str):
            self.write_string(obj)
        elif isinstance(obj, (bytes, bytearray)):
            self.write_bytearray(bytes(obj))
        elif isinstance(obj, list):
            self.write_array(obj)
        elif isinstance(obj, dict):
            self.write_map(obj)
        else:
            self.write_null()

    def to_bytes(self) -> bytes:
        return bytes(self._buf)


# ── AMF3 反序列化 (精确对照 Amf3Input.ts) ──

class Amf3Reader:
    """Amf3Input.ts — 精确对照"""
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0
        self._object_table: list[Any] = []
        self._string_table: list[str] = []
        self._traits_table: list[TraitsInfo] = []

    def _read_byte(self) -> int:
        if self._pos >= len(self._data):
            raise EOFError("AMF3 EOF")
        b = self._data[self._pos]
        self._pos += 1
        return b & 0xFF

    def _read_u29(self) -> int:
        """Amf3Input.ts:146-164 — 精确对照"""
        b = self._read_byte()
        if b < 128:
            return b
        value = (b & 0x7F) << 7
        b = self._read_byte()
        if b < 128:
            return value | b
        value = (value | (b & 0x7F)) << 7
        b = self._read_byte()
        if b < 128:
            return value | b
        value = (value | (b & 0x7F)) << 8  # ← 关键: << 8, not << 7!
        b = self._read_byte()
        return value | b

    def _read_utf(self, utflen: int) -> str:
        raw = self._data[self._pos:self._pos + utflen]
        self._pos += utflen
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            return raw.decode('latin-1')

    def _read_string(self) -> str:
        """Amf3Input.ts:60-72"""
        ref = self._read_u29()
        if (ref & 0x1) == 0:
            return self._string_table[ref >> 1]
        _len = ref >> 1
        if _len == 0:
            return ""
        s = self._read_utf(_len)
        self._string_table.append(s)
        return s

    def _read_traits(self, ref: int) -> TraitsInfo:
        """Amf3Input.ts:165-180"""
        if (ref & 0x3) == 1:
            return self._traits_table[ref >> 2]
        externalizable = (ref & 0x4) == 4
        dynamic = (ref & 0x8) == 8
        count = ref >> 4
        class_name = self._read_string()
        ti = TraitsInfo(class_name, dynamic, externalizable)
        self._traits_table.append(ti)
        for _ in range(count):
            ti.add_property(self._read_string())
        return ti

    def read_object(self) -> Any:
        """Amf3Input.ts:22-58"""
        type_byte = self._read_byte()
        return self._read_object_value(type_byte)

    def _read_object_value(self, type_byte: int) -> Any:
        """Amf3Input.ts:26-59"""
        if type_byte == Amf3Type.kUndefinedType:
            return None
        elif type_byte == Amf3Type.kNullType:
            return None
        elif type_byte == Amf3Type.kFalseType:
            return False
        elif type_byte == Amf3Type.kTrueType:
            return True
        elif type_byte == Amf3Type.kIntegerType:
            value = self._read_u29()
            # Amf3Input.ts:40: (value << 3) >> 3 — 29-bit sign extension
            # JS simulates 32-bit arithmetic; Python equivalent:
            if value >= 0x10000000:
                value -= 0x20000000
            return value
        elif type_byte == Amf3Type.kDoubleType:
            raw = self._data[self._pos:self._pos + 8]
            self._pos += 8
            return struct.unpack('>d', raw)[0]
        elif type_byte == Amf3Type.kStringType:
            return self._read_string()
        elif type_byte == Amf3Type.kArrayType:
            return self._read_array()
        elif type_byte == Amf3Type.kObjectType:
            return self._read_script_object()
        elif type_byte == Amf3Type.kByteArrayType:
            return self._read_byte_array()
        raise ValueError(f"Unknown AMF3 type byte: {type_byte}")

    def _read_array(self) -> list | dict:
        """Amf3Input.ts:73-99 — 精确复现"""
        ref = self._read_u29()
        if (ref & 0x1) == 0:
            return self._object_table[ref >> 1]
        _len = ref >> 1
        key = self._read_string()
        if key == "":
            # Dense array
            arr: list = []
            self._object_table.append(arr)
            for _ in range(_len):
                arr.append(self.read_object())
            return arr
        # Associative array with string keys
        obj: dict = {}
        self._object_table.append(obj)
        while key != "":
            obj[key] = self.read_object()
            key = self._read_string()
        for i in range(_len):
            obj[i] = self.read_object()
        return obj

    def _read_script_object(self) -> dict:
        """Amf3Input.ts:100-131"""
        ref = self._read_u29()
        if (ref & 0x1) == 0:
            return self._object_table[ref >> 1]
        ti = self._read_traits(ref)
        class_name = ti.class_name
        obj: dict = {}
        self._object_table.append(obj)
        if ti.externalizable:
            return self.read_object() or {}
        for prop in ti.properties:
            obj[prop] = self.read_object()
        if ti.dynamic:
            while True:
                name = self._read_string()
                if not name:
                    break
                obj[name] = self.read_object()
        return obj

    def _read_byte_array(self) -> bytes:
        """Amf3Input.ts:132-144"""
        ref = self._read_u29()
        if (ref & 0x1) == 0:
            return self._object_table[ref >> 1]
        _len = ref >> 1
        ba = self._data[self._pos:self._pos + _len]
        self._pos += _len
        self._object_table.append(ba)
        return ba


class TraitsInfo:
    """Amf3Input 内部类 — 对应 TraitsInfo.ts"""
    def __init__(self, class_name: str, dynamic: bool, externalizable: bool):
        self.class_name = class_name
        self.dynamic = dynamic
        self.externalizable = externalizable
        self.properties: list[str] = []

    def add_property(self, name: str) -> None:
        self.properties.append(name)

    def get_class_name(self) -> str:
        return self.class_name

    def is_dynamic(self) -> bool:
        return self.dynamic

    def is_externalizable(self) -> bool:
        return self.externalizable

    def get_properties(self) -> list[str]:
        return self.properties

    def get_property(self, index: int) -> str:
        return self.properties[index] if index < len(self.properties) else ""
