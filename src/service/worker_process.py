"""Dedicated executable identities and parent-owned worker process lifetime."""
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import sys

from src.service.contracts import ServiceError


def worker_executable(directory, group_id):
    return Path(directory) / "egress-workers" / group_id / ("AolaWorker.exe" if os.name == "nt" else "aola-worker")


def prepare_worker_executable(directory, group_id):
    if sys.platform not in {"win32", "linux"} or getattr(sys, "frozen", False):
        raise ServiceError("worker_platform", "出口执行进程需要 Windows/Linux Python 3.12 环境")
    target = worker_executable(directory, group_id)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    base = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    sources = [(base, target)]
    if os.name == "nt":
        for pattern in ("python*.dll", "vcruntime*.dll"):
            sources.extend((source, target.parent / source.name) for source in base.parent.glob(pattern))
    for source, destination in sources:
        if destination.exists():
            if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(destination.read_bytes()).digest():
                raise ServiceError("worker_runtime_changed", "执行进程文件与当前 Python 不一致，请停服后移走该组运行目录")
        else:
            shutil.copy2(source, destination)
        if os.name != "nt":
            destination.chmod(0o700)
    return target.resolve()


def worker_environment():
    environment = dict(os.environ)
    environment.pop("AOLA_ADMIN_TOKEN", None)
    environment["PYTHONHOME"] = sys.base_prefix
    environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([
        str(Path(__file__).resolve().parents[2]),
        *(str(Path(entry).resolve()) for entry in sys.path if entry),
    ]))
    environment["PYTHONIOENCODING"] = "utf-8"
    if sys.platform == "linux":
        environment["LD_LIBRARY_PATH"] = str(Path(sys.base_prefix) / "lib") + os.pathsep + environment.get("LD_LIBRARY_PATH", "")
    return environment


def actual_executable():
    if os.name == "nt":
        buffer = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetModuleFileNameW(None, buffer, len(buffer)):
            raise ctypes.WinError()
        return Path(buffer.value).resolve()
    return Path("/proc/self/exe").resolve()


def bind_linux_parent(parent_pid):
    if sys.platform == "linux":
        import signal
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot bind worker to parent lifetime")
        if os.getppid() != parent_pid:
            raise SystemExit(0)


class WorkerJob:
    """Closing this Windows handle kills every worker if the parent exits."""
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                        ("Flags", w.DWORD), ("Minimum", ctypes.c_size_t), ("Maximum", ctypes.c_size_t),
                        ("Processes", w.DWORD), ("Affinity", ctypes.c_size_t),
                        ("Priority", w.DWORD), ("Scheduling", w.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Counters", Counters), ("ProcessMemory", ctypes.c_size_t),
                        ("JobMemory", ctypes.c_size_t), ("PeakProcess", ctypes.c_size_t), ("PeakJob", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        handle = self.api.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.api.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle

    def assign(self, pid):
        if self.handle is None:
            return
        process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.CloseHandle(process)

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None
