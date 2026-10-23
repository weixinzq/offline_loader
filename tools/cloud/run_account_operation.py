"""Log in the fixed 300 accounts and execute a chosen message file once."""
import argparse
import asyncio
from collections import Counter
import csv
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import aiohttp

from src.messaging.parser import parse_send_message_file
import run_cloud_workflows as workflows


def prepare_operation(file):
    messages = [asdict(message) for message in parse_send_message_file(file)]
    if not messages:
        raise ValueError("设置文件没有可执行消息")
    configurations = [{"exit": number, "steps": [{"kind": "messages", "messages": messages}],
                       "repetitions": 1, "repeat_interval": 0.5, "message_delay": 0.2}
                      for number in range(1, 11)]
    return configurations, len(messages)


async def main_async(options):
    file = options.file if options.file.is_absolute() else ROOT / options.file
    configurations, count = prepare_operation(file)
    manifest = json.loads((ROOT / "data/service/cloud-workflows.json").read_text())
    workflows.make_specs(manifest, configurations)
    labels = {row["account_id"]: row["label"] for group in manifest["groups"] for row in group["accounts"]}
    token = os.environ.get("AOLA_ADMIN_TOKEN") or (ROOT / "data/service/admin.token").read_text().strip()
    async with aiohttp.ClientSession(base_url="http://127.0.0.1:8765",
                                    headers={"Authorization": "Bearer " + token}, trust_env=False,
                                    timeout=aiohttp.ClientTimeout(total=30)) as session:
        api = workflows.Backend(session)
        await workflows.ensure_backend(api, "http://127.0.0.1:8765", options.dry_run)
        active = [task["task_id"] for task in await api.request("GET", "/api/v1/tasks")
                  if task["state"] in {"queued", "running", "cancelling"}
                  and set(labels).intersection(task["spec"]["account_ids"])]
        if active:
            raise ValueError(f"目标账号仍被任务占用，请先停止对应任务：{active}")
        if options.dry_run:
            report = await workflows.run_workflows(api, manifest, configurations, dry_run=True)
            print(json.dumps({**report, "file": str(file), "messages_per_account": count}, ensure_ascii=False))
            return 0

        directory = Path(__file__).resolve().parent / "runs" / (
            datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
        directory.mkdir(parents=True, mode=0o700)
        status = {"state": "running", "file": str(file), "messages_per_account": count,
                  "selected": len(labels), "repetitions": 1, "tasks": {},
                  "started_at": datetime.now().astimezone().isoformat()}

        def write(name, value):
            path = directory / name
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)

        with (directory / "run.log").open("w", encoding="utf-8", buffering=1) as stream:
            def log(message):
                line = datetime.now().astimezone().isoformat() + " " + str(message).replace(token, "[redacted]")
                print(line, flush=True)
                stream.write(line + "\n")

            failures = set()

            def update(task):
                status["tasks"][task["task_id"]] = {
                    "kind": task["kind"], "state": task["state"],
                    "account_states": dict(Counter(row["state"] for row in task["account_results"]))}
                for row in task["account_results"]:
                    key = (task["task_id"], row["account_id"])
                    if row["state"] == "failed" and key not in failures:
                        failures.add(key)
                        log(f"{labels[row['account_id']]}：{row.get('error')}")
                write("status.json", status)

            log(f"结果目录：{directory}；文件：{file}；每账号{count}条，只执行一次")
            write("status.json", status)
            try:
                result = await workflows.run_workflows(api, manifest, configurations, log=log, on_update=update)
                write("result.json", result)
                outcomes = []
                for group, outcome in zip(manifest["groups"], result["groups"]):
                    executed = {row["account_id"]: row for row in outcome.get("account_results", [])}
                    for account in group["accounts"]:
                        account_id = account["account_id"]
                        row = executed.get(account_id, {})
                        connection = outcome["connection_results"].get(account_id, {})
                        error = row.get("error") or connection.get("error") or {}
                        outcomes.append({"label": labels[account_id], "account_id": account_id,
                                         "state": row.get("state", "not_executed"),
                                         "connection_attempts": outcome["connection_attempts"][account_id],
                                         "completed_repetitions": row.get("completed_repetitions", 0),
                                         "error_code": error.get("code", ""),
                                         "error_message": error.get("message", outcome.get("error", ""))})
                with (directory / "accounts.csv").open("w", encoding="utf-8-sig", newline="") as stream_csv:
                    writer = csv.DictWriter(stream_csv, fieldnames=list(outcomes[0]))
                    writer.writeheader()
                    writer.writerows(outcomes)
                succeeded = sum(row["state"] == "succeeded" for row in outcomes)
                status.update(state="completed" if succeeded == len(labels) else "partial",
                              succeeded=succeeded, not_succeeded=len(labels) - succeeded,
                              finished_at=datetime.now().astimezone().isoformat())
                log(f"完成：成功{succeeded}，未完成{len(labels)-succeeded}；连接保留")
                return 0 if status["state"] == "completed" else 1
            except BaseException as error:
                status.update(state="cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                              error=str(error).replace(token, "[redacted]"))
                raise
            finally:
                write("status.json", status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=ROOT / "Script/双宠设置.txt")
    parser.add_argument("--dry-run", action="store_true")
    try:
        return asyncio.run(main_async(parser.parse_args()))
    except KeyboardInterrupt:
        print("已取消本轮操作，保留就绪连接")
        return 130
    except (ValueError, OSError, aiohttp.ClientError, TimeoutError) as error:
        print(f"操作未完成：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
