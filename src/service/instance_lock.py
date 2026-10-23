"""Prevent two service processes from owning the same runtime directory."""
import os

from src.service.contracts import ServiceError


class InstanceLock:
    def __init__(self, path):
        self.path, self.stream = path, None

    def acquire(self):
        self.stream = self.path.open("a+b")
        if os.name != "nt":
            self.path.chmod(0o600)
        try:
            if os.fstat(self.stream.fileno()).st_size == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise ServiceError("service_already_running", "此数据目录已有后台实例运行", 409) from exc

    def release(self):
        if self.stream is not None:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None
