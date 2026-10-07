"""Credential-free JSON-lines audit logging for send results."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from src.config import PROJECT_ROOT
from src.messaging.dispatcher import SendResult


def append_send_results(
    batches: dict[str, list[SendResult]], path: Path | None = None
) -> Path:
    """Append send outcomes without login names, passwords or session IDs."""
    if path is None:
        day = datetime.now().astimezone().strftime("%Y%m%d")
        path = PROJECT_ROOT / "logs" / f"send-{day}.log"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as file:
        for results in batches.values():
            for result in results:
                file.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
    return path
