"""One worker's account lifecycle and private, redacted result files."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import hashlib
import json
import logging
from pathlib import Path
import re

from src.accounts.manager import AccountConnection, AccountSpec
from src.messaging.audit_log import append_send_results
from src.scripting.composer import execute_combination
from src.scripting.jobs import build_steps

_SECRET_KEYS = frozenset({"account", "password", "username", "session-id", "sid", "token",
                         "secret", "uuid", "url", "private-key", "psk", "auth", "auth-str",
                         "passphrase", "authorization", "proxy-authorization", "loginkey"})


class SecretFilter(logging.Filter):
    def __init__(self, job):
        super().__init__()
        self.values = set()
        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).lower().replace("_", "-") in _SECRET_KEYS and item:
                        self.add(str(item))
                    else:
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        collect(job)

    def add(self, value):
        if value:
            self.values.add(str(value))
            self.values.add(hashlib.md5(str(value).encode()).hexdigest())

    def clean(self, text):
        for value in sorted(self.values, key=len, reverse=True):
            text = text.replace(value, "<redacted>")
        return re.sub(r"(\[LOGIN\] UserId:)\s*.*", r"\1 <redacted>", text)

    def filter(self, record):
        record.msg, record.args = self.clean(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = self.clean(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True

    def data(self, value):
        if isinstance(value, str):
            return self.clean(value)
        if isinstance(value, dict):
            return {key: "<redacted>" if str(key).lower().replace("_", "-") in _SECRET_KEYS
                    else self.data(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.data(item) for item in value]
        return value


def write_json(path: Path, value):
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.chmod(0o600)
    pending.replace(path)


async def run_accounts(worker, directory: Path, sanitizer, *, connection_factory=AccountConnection):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    steps = build_steps(worker["steps"])
    blocked = asyncio.Event()
    connections = []
    records = {}
    def publish():
        write_json(directory / "accounts.json", sanitizer.data({
            "worker": worker["id"], "mode": worker["mode"],
            "login_blocked": blocked.is_set(), "accounts": list(records.values()),
        }))
    for entry in worker["accounts"]:
        records[entry["label"]] = {"index": entry["index"], "label": entry["label"],
                                   "state": "queued", "completed_repetitions": 0}
        config = dict(entry["config"])
        config["proxy_url"] = f"socks5://127.0.0.1:{worker['proxy']['port']}"
        config["browser_verification"] = False
        connections.append(connection_factory(AccountSpec(entry["label"], config)))
    publish()

    async def connect(connection):
        row = records[connection.spec.label]
        if blocked.is_set():
            row["state"], row["error"] = "skipped", "此进程已停止后续登录"
            publish()
            return False
        row["state"] = "connecting"
        publish()
        if not await connection.connect(retries=1):
            row["state"], row["error"] = "failed", sanitizer.clean(connection.last_error)
            if connection.login_block_reason or not connection.retryable:
                blocked.set()
            publish()
            return False
        sanitizer.add(connection.context.login_result.session_id)
        sanitizer.add(connection.context.user_id)
        await connection.context.wait_for_player_initialization()
        row["state"] = "ready"
        connection.context.log_callback = lambda text: logging.getLogger(__name__).info(
            "%s: %s", connection.spec.label, text,
        )
        publish()
        return True

    async def execute(connection):
        row = records[connection.spec.label]
        message_sequence = 0
        for repetition in range(1, worker["repetitions"] + 1):
            if blocked.is_set():
                row["state"], row["error"] = "stopped", "此进程遇到登录限制，停止任务"
                break
            row["state"] = "running"
            publish()
            result = await execute_combination(
                connection.spec.label, connection.context, steps,
                message_delay=worker["message_delay"],
            )
            sends = [replace(item, sequence=item.sequence + message_sequence, error=sanitizer.clean(item.error),
                             account_label=sanitizer.clean(item.account_label), cmd=sanitizer.clean(item.cmd))
                     for item in result.send_results if item.ext_id >= 0]
            message_sequence += len(result.send_results)
            if sends:
                append_send_results({connection.spec.label: sends}, directory / "send.jsonl")
            details = asdict(result)
            with (directory / "steps.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(sanitizer.data({"label": connection.spec.label,
                           "repetition": repetition, "result": details}), ensure_ascii=False) + "\n")
            row["send_details"] = "tracked writes only; legacy scripts may not return details"
            if not result.success:
                row["state"] = "failed"
                row["error"] = sanitizer.clean(next(s.error for s in result.step_results if not s.success))
                break
            row["completed_repetitions"] = repetition
            publish()
            if repetition < worker["repetitions"] and worker["repeat_interval"]:
                await asyncio.sleep(worker["repeat_interval"])
        else:
            row["state"] = "completed"
        publish()

    async def guarded(connection, action):
        try:
            await action(connection)
        except asyncio.CancelledError:
            records[connection.spec.label]["state"] = "stopped"
            raise
        except Exception as exc:
            records[connection.spec.label].update(state="failed", error=sanitizer.clean(str(exc)))
        finally:
            publish()

    semaphore = asyncio.Semaphore(worker["concurrency"])
    async def lifecycle(connection):
        async with semaphore:
            try:
                if await connect(connection):
                    await execute(connection)
            finally:
                await connection.disconnect()

    async def login_only(connection):
        async with semaphore:
            await connect(connection)

    tasks = []
    async def wait_tasks():
        group = asyncio.gather(*tasks, return_exceptions=True)
        refusal = asyncio.create_task(blocked.wait())
        try:
            await asyncio.wait((group, refusal), return_when=asyncio.FIRST_COMPLETED)
            if blocked.is_set():
                for task in tasks:
                    task.cancel()
            await group
        finally:
            refusal.cancel()
            await asyncio.gather(refusal, return_exceptions=True)
    try:
        if worker["mode"] == "bounded":
            tasks = [asyncio.create_task(guarded(c, lifecycle)) for c in connections]
            await wait_tasks()
        else:
            tasks = [asyncio.create_task(guarded(c, login_only)) for c in connections]
            await wait_tasks()
            if not blocked.is_set():
                tasks = [asyncio.create_task(guarded(c, execute)) for c in connections
                         if c.online and records[c.spec.label]["state"] == "ready"]
                await wait_tasks()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(c.disconnect() for c in connections), return_exceptions=True)
        for row in records.values():
            if row["state"] not in {"completed", "failed", "skipped", "stopped"}:
                row["state"] = "stopped"
        publish()
    return 0 if all(row["state"] == "completed" for row in records.values()) else 2
