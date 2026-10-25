"""Submit one scheduled probe and retain its results and complete public logs."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import time
import urllib.request

from src.interfaces.cli import server_probe_csv


def run(file, output, at, request, *, clock=lambda: datetime.now(timezone.utc), sleep=time.sleep):
    if at.utcoffset() is None or at <= clock():
        raise ValueError("启动时间必须包含时区且晚于当前时间")
    body = json.loads(file.read_text(encoding="utf-8"))
    body.update(kind="server_probe", planned_at=at.isoformat())
    checked = request("POST", "/tasks/validate", body)
    output.mkdir(mode=0o700)

    def save(name, value):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    save("preflight.json", checked)
    save("schedule.json", {"state": "waiting", "planned_at": at.isoformat()})
    print("Waiting until " + at.isoformat(), flush=True)
    # Prepare workers and check exits before a fixed one-second login window.
    dispatch_at = at - timedelta(seconds=10) if body.get("probe_until") else at
    while (remaining := (dispatch_at - clock()).total_seconds()) > 0:
        sleep(min(remaining, 1))
    record = request("POST", "/tasks", body)
    task_id = record["task_id"]
    save("submission.json", record)
    save("schedule.json", {"state": "submitted", "planned_at": at.isoformat(), "task_id": task_id})
    print("Submitted " + task_id, flush=True)
    after = 0
    with (output / "task.log").open("w", encoding="utf-8") as logs, \
         (output / "task-beijing.log").open("w", encoding="utf-8") as beijing_logs:
        while True:
            record = request("GET", "/tasks/" + task_id)
            while True:
                rows = request("GET", f"/tasks/{task_id}/logs?after={after}&limit=1000")
                for row in rows:
                    line = json.dumps(row, ensure_ascii=False) + "\n"
                    logs.write(line)
                    beijing_logs.write(re.sub(
                        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
                        lambda m: datetime.fromisoformat(m.group().replace("Z", "+00:00")).astimezone(
                            timezone(timedelta(hours=8))).isoformat(), line))
                    after = row["id"]
                logs.flush()
                beijing_logs.flush()
                if len(rows) < 1000:
                    break
            save("results.json", record)
            if record["state"] not in {"queued", "running", "cancelling"}:
                break
            sleep(2)
    (output / "results.csv").write_text(server_probe_csv(record), encoding="utf-8-sig")
    save("schedule.json", {"state": record["state"], "planned_at": at.isoformat(), "task_id": task_id})
    print("Finished " + record["state"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--at", type=datetime.fromisoformat, required=True)
    args = parser.parse_args()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(method, path, body=None):
        req = urllib.request.Request("http://127.0.0.1:8765/api/v1" + path,
            data=json.dumps(body).encode() if body is not None else None, method=method,
            headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=30) as response:
            return json.load(response)

    run(args.file, args.output, args.at, request)


if __name__ == "__main__":
    main()
