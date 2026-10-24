"""Submit one scheduled probe and retain its results and complete public logs."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
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
    while (remaining := (at - clock()).total_seconds()) > 0:
        sleep(min(remaining, 1))
    record = request("POST", "/tasks", body)
    task_id = record["task_id"]
    save("submission.json", record)
    save("schedule.json", {"state": "submitted", "planned_at": at.isoformat(), "task_id": task_id})
    print("Submitted " + task_id, flush=True)
    after = 0
    with (output / "task.log").open("w", encoding="utf-8") as logs:
        while True:
            record = request("GET", "/tasks/" + task_id)
            while True:
                rows = request("GET", f"/tasks/{task_id}/logs?after={after}&limit=1000")
                for row in rows:
                    logs.write(json.dumps(row, ensure_ascii=False) + "\n")
                    after = row["id"]
                logs.flush()
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
    token = os.environ.get("AOLA_ADMIN_TOKEN") or Path("data/service/admin.token").read_text().strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(method, path, body=None):
        req = urllib.request.Request("http://127.0.0.1:8765/api/v1" + path,
            data=json.dumps(body).encode() if body is not None else None, method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with opener.open(req, timeout=30) as response:
            return json.load(response)

    run(args.file, args.output, args.at, request)


if __name__ == "__main__":
    main()
