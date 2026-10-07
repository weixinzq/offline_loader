"""Small Windows named-pipe JSON-lines transport."""
from __future__ import annotations

import ctypes
import json
import threading
from ctypes import wintypes


PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255
ERROR_PIPE_CONNECTED = 535
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class NamedPipeTransport:
    def __init__(self, name: str):
        if not name or any(char in name for char in "\\/\r\n"):
            raise ValueError("invalid pipe name")
        self.path = rf"\\.\pipe\{name}"
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle: int | None = None
        self._buffer = bytearray()
        self._write_lock = threading.Lock()
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        kernel32 = self._kernel32
        kernel32.CreateNamedPipeW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
        )
        kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel32.ConnectNamedPipe.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
        kernel32.ConnectNamedPipe.restype = wintypes.BOOL
        kernel32.ReadFile.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = kernel32.ReadFile.argtypes
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
        kernel32.DisconnectNamedPipe.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def connect(self) -> None:
        handle = self._kernel32.CreateNamedPipeW(
            self.path,
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1,
            65536,
            65536,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        if not self._kernel32.ConnectNamedPipe(handle, None):
            error = ctypes.get_last_error()
            if error != ERROR_PIPE_CONNECTED:
                self.close()
                raise ctypes.WinError(error)

    def read_json(self) -> dict | None:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                if not raw.strip():
                    continue
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("pipe message must be a JSON object")
                return value

            handle = self._require_handle()
            chunk = ctypes.create_string_buffer(8192)
            read = wintypes.DWORD()
            ok = self._kernel32.ReadFile(
                handle, chunk, len(chunk), ctypes.byref(read), None
            )
            if not ok or read.value == 0:
                return None
            self._buffer.extend(chunk.raw[: read.value])

    def write_json(self, value: dict) -> None:
        data = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with self._write_lock:
            handle = self._require_handle()
            offset = 0
            while offset < len(data):
                written = wintypes.DWORD()
                piece = data[offset:]
                buffer = ctypes.create_string_buffer(piece)
                if not self._kernel32.WriteFile(
                    handle, buffer, len(piece), ctypes.byref(written), None
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                offset += written.value

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        self._kernel32.FlushFileBuffers(handle)
        self._kernel32.DisconnectNamedPipe(handle)
        self._kernel32.CloseHandle(handle)

    def _require_handle(self) -> int:
        if self._handle is None:
            raise RuntimeError("named pipe is not connected")
        return self._handle

    def __enter__(self) -> "NamedPipeTransport":
        self.connect()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()
