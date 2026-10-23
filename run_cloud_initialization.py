"""Reconnect the fixed 300 accounts and execute the latest initialization file once."""
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import aiohttp

from run_cloud_workflows import (
    Backend, LOGIN_CONCURRENCY, MAX_CONNECTION_ATTEMPTS, ROOT,
    ensure_backend, make_specs, run_workflows,
)
from src.messaging.parser import parse_send_message_file


def prepare_initialization(path: Path):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    messages = [asdict(message) for message in parse_send_message_file(path)]
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("初始化文件在读取期间发生变化，请重新运行")
    workflows = [{"exit": number, "steps": [{"kind": "messages", "messages": messages}],
                  "repetitions": 1, "repeat_interval": 0.5, "message_delay": 0.2}
                 for number in range(1, 11)]
    return workflows, {"file": str(path), "file_sha256": digest, "message_count": len(messages)}


def account_results(manifest, result):
    rows = []
    for group, outcome in zip(manifest["groups"], result["groups"]):
        executed = {row["account_id"]: row for row in outcome.get("account_results", [])}
        for account in group["accounts"]:
            account_id = account["account_id"]
            row = executed.get(account_id, {})
            connection = outcome["connection_results"].get(account_id, {})
            error = row.get("error") or connection.get("error") or {}
            rows.append({"label": account["label"], "exit": group["exit"],
                         "state": row.get("state", "not_executed"),
                         "connection_attempts": outcome["connection_attempts"][account_id],
                         "completed_repetitions": row.get("completed_repetitions", 0),
                         "network_messages": row.get("network_write", {}).get("count"),
                         "business_confirmation": row.get("business_confirmation", "not_executed"),
                         "error_code": error.get("code", ""),
                         "error_message": error.get("message", outcome.get("error", ""))})
    return rows


async def main_async(options):
    file = options.file if options.file.is_absolute() else ROOT / options.file
    manifest_file = options.manifest if options.manifest.is_absolute() else ROOT / options.manifest
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    workflows, metadata = prepare_initialization(file)
    make_specs(manifest, workflows)  # Invalid local input cannot start a backend or log in.
    token = os.environ.get("AOLA_ADMIN_TOKEN") or (ROOT / "data/service/admin.token").read_text().strip()
    async with aiohttp.ClientSession(base_url=options.url, headers={"Authorization": "Bearer " + token},
                                    trust_env=False, timeout=aiohttp.ClientTimeout(total=30)) as session:
        api = Backend(session)
        await ensure_backend(api, options.url, options.dry_run)
        if options.dry_run:
            result = await run_workflows(api, manifest, workflows, dry_run=True, reconnect_all=True)
            print(json.dumps({**metadata, **result, "reconnect_all": True,
                              "max_connection_attempts": MAX_CONNECTION_ATTEMPTS}, ensure_ascii=False, indent=2))
            return 0
        run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
        directory = ROOT / "logs" / "cloud-initialization" / run_id
        directory.mkdir(parents=True, mode=0o700)

        def write(name, value):
            path = directory / name
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            path.chmod(0o600)

        with (directory / "run.log").open("w", encoding="utf-8", buffering=1) as stream:
            def log(message):
                line = datetime.now().astimezone().isoformat() + " " + str(message).replace(token, "[redacted]")
                stream.write(line + "\n")
                print(line, flush=True)

            log(f"本轮日志：{directory}")
            log(f"初始化文件解析出{metadata['message_count']}条消息/指令；强制重连300账号，"
                f"登录并发{LOGIN_CONCURRENCY}，每账号最多{MAX_CONNECTION_ATTEMPTS}次")
            write("status.json", {"state": "running", **metadata, "run_id": run_id})
            try:
                result = await run_workflows(api, manifest, workflows, reconnect_all=True, log=log)
                write("result.json", {**metadata, **result})
                rows = account_results(manifest, result)
                with (directory / "accounts.csv").open("w", encoding="utf-8-sig", newline="") as csv_file:
                    writer = csv.DictWriter(csv_file, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
                success = sum(row["state"] == "succeeded" for row in rows)
                summary = {"state": "completed" if success == 300 else "partial", "run_id": run_id,
                           **metadata, "selected": 300, "succeeded": success, "not_succeeded": 300 - success,
                           "login_concurrency": LOGIN_CONCURRENCY,
                           "max_connection_attempts": MAX_CONNECTION_ATTEMPTS,
                           "finished_at": datetime.now().astimezone().isoformat()}
                write("status.json", summary)
                log(f"本轮完成：成功{success}，未完成{300-success}；连接保留，逐账号结果见 accounts.csv")
                return 0 if success == 300 else 1
            except BaseException as error:
                state = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
                write("status.json", {"state": state, **metadata, "run_id": run_id,
                                      "error": str(error).replace(token, "[redacted]")})
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=ROOT / "Script" / "初始化.txt")
    parser.add_argument("--manifest", type=Path, default=ROOT / "data/service/cloud-workflows.json")
    parser.add_argument("--url", default=os.environ.get("AOLA_SERVICE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--dry-run", action="store_true")
    try:
        return asyncio.run(main_async(parser.parse_args()))
    except KeyboardInterrupt:
        print("本次任务已请求取消，已就绪连接保留")
        return 130
    except (ValueError, OSError, aiohttp.ClientError, TimeoutError, subprocess.CalledProcessError) as error:
        message = str(error)
        if token := os.environ.get("AOLA_ADMIN_TOKEN"):
            message = message.replace(token, "[redacted]")
        print(f"初始化未完成：{message}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
