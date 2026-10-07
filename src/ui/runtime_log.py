"""Persistent diagnostics for the Python backend and CLI."""
from __future__ import annotations

import logging
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

from src.config import PROJECT_ROOT


_LOGGER_NAME = "aqcs.runtime"
_runtime_log_path: Path | None = None


class LoggerStream:
    """Line-buffered stdout/stderr replacement writing to a file logger."""

    def __init__(self, logger: logging.Logger, level: int):
        self.logger = logger
        self.level = level
        self._buffer = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                if line.strip():
                    self.logger.log(self.level, line.rstrip("\r"))
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._buffer.strip():
                self.logger.log(self.level, self._buffer.strip())
            self._buffer = ""

    def isatty(self) -> bool:
        return False


def _select_log_path() -> Path:
    day = datetime.now().astimezone().strftime("%Y%m%d")
    preferred = PROJECT_ROOT / "logs" / f"runtime-{day}.log"
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        with preferred.open("a", encoding="utf-8"):
            pass
        return preferred
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "AolaLoader" / "logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback / f"runtime-{day}.log"


def configure_runtime_logging(capture_console: bool = True) -> Path:
    """Configure rotating UTF-8 logs once and return the active file path."""
    global _runtime_log_path
    if _runtime_log_path is not None:
        return _runtime_log_path

    path = _select_log_path()
    handler = RotatingFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
        )
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.info("desktop runtime logging started: %s", path)
    from src.network.context import configure_battle_receive_logging

    battle_receive_path = configure_battle_receive_logging(path.parent)
    logger.info("battle receive logging started: %s", battle_receive_path)

    if capture_console:
        sys.stdout = LoggerStream(logger, logging.INFO)
        sys.stderr = LoggerStream(logger, logging.ERROR)

    def log_uncaught(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        logger.critical(
            "uncaught exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    sys.excepthook = log_uncaught
    _runtime_log_path = path
    return path


def get_runtime_log_path() -> Path | None:
    return _runtime_log_path
