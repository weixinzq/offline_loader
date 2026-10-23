"""A parent crash must not leave independent outbound connections alive."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


def alive(pid):
    if os.name == 'nt':
        from ctypes import wintypes as w
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        api.OpenProcess.restype = w.HANDLE
        api.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        api.CloseHandle.argtypes = [w.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = w.DWORD()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
    except FileNotFoundError:
        return False


class WorkerLifetimeTests(unittest.TestCase):
    def test_parent_kill_closes_owned_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            worker_pid = None
            with (path / 'owner.log').open('w', encoding='utf-8') as log:
                process = subprocess.Popen([sys.executable, '-u', '-B', '-m', 'tests.egress_owner_fixture', str(path / 'service')],
                    cwd=Path(__file__).resolve().parents[1], stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline and process.poll() is None:
                        text = (path / 'owner.log').read_text(encoding='utf-8')
                        for line in text.splitlines():
                            if line.startswith('{"worker_pid":'):
                                worker_pid = json.loads(line)['worker_pid']
                                break
                        if worker_pid:
                            break
                        time.sleep(.05)
                    self.assertIsNotNone(worker_pid, (path / 'owner.log').read_text(encoding='utf-8'))
                    self.assertTrue(alive(worker_pid))
                    process.kill()
                    process.wait(timeout=5)
                    deadline = time.monotonic() + 5
                    while alive(worker_pid) and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertFalse(alive(worker_pid), 'Worker survived its owning service process')
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    if worker_pid and alive(worker_pid):
                        os.kill(worker_pid, signal.SIGTERM)
