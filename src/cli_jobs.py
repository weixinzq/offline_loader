"""Linux CLI supervisor for configured account workers and private Mihomo."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import uuid

import yaml

from src.config import PROJECT_ROOT
from src.scripting.jobs import load_job, mihomo_config
from src.scripting.job_worker import SecretFilter, run_accounts, write_json
from src.ui.runtime_log import configure_runtime_logging


def birth(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


def alive(identity):
    return bool(identity.get("birth") and birth(identity["pid"]) == identity["birth"])


def owns_listener(pid, port):
    inodes = set()
    for descriptor in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    for table in ("tcp", "tcp6"):
        for line in Path(f"/proc/net/{table}").read_text().splitlines()[1:]:
            columns = line.split()
            if columns[3] == "0A" and int(columns[1].split(":")[1], 16) == port and columns[9] in inodes:
                return True
    return False


def entry_command(*arguments):
    return [sys.executable, "-B", str(PROJECT_ROOT / "cli_main.py"), *map(str, arguments)]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def run_directory(root, run_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("运行编号无效")
    directory = root / run_id
    if not directory.is_dir():
        raise ValueError("运行编号不存在")
    return directory


def status(directory):
    launch = read_json(directory / "launch.json")
    state = read_json(directory / "run.json")
    state["alive"] = alive(launch)
    if not state["alive"] and state["state"] in {"starting", "running"}:
        state["state"] = "interrupted"
    state["workers"] = []
    for worker in read_json(directory / "job.json")["workers"]:
        folder = directory / worker["id"]
        progress = folder / "accounts.json"
        record = read_json(progress) if progress.exists() else {"worker": worker["id"], "state": "pending"}
        process = next((p for p in state.get("processes", []) if p.get("name") == "worker:" + worker["id"]), {})
        record["process"] = {**process, "alive": alive(process) if process else False}
        state["workers"].append(record)
    return state


def stop(directory):
    identity = read_json(directory / "launch.json")
    state = read_json(directory / "run.json")
    identities = [identity, *state.get("processes", [])]
    if not any(alive(record) for record in identities):
        return status(directory)
    signaled_group = False
    if alive(identity):
        try:
            if os.getpgid(identity["pid"]) != identity["pid"]:
                raise ValueError("运行进程组身份不匹配，拒绝停止")
            os.killpg(identity["pid"], signal.SIGTERM)
            signaled_group = True
        except ProcessLookupError:
            pass
    for record in identities[1:]:
        if not signaled_group and alive(record):
            try:
                os.kill(record["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 20
    while any(alive(record) for record in identities) and time.monotonic() < deadline:
        time.sleep(0.1)
    forced = False
    for record in identities:
        if alive(record):
            try:
                os.kill(record["pid"], signal.SIGKILL)
                forced = True
            except ProcessLookupError:
                pass
    state = read_json(directory / "run.json")
    if forced or state["state"] in {"starting", "running"}:
        state.update(state="stopped", forced=forced, finished_at=datetime.now().astimezone().isoformat())
        write_json(directory / "run.json", state)
    return status(directory)


async def worker_main(directory, worker_id):
    job = read_json(directory / "job.json")
    worker = next(w for w in job["workers"] if w["id"] == worker_id)
    folder = directory / worker_id
    folder.mkdir(mode=0o700, exist_ok=True)
    os.environ["AOLA_PROXY_URL"] = f"socks5://127.0.0.1:{worker['proxy']['port']}"
    os.environ["AOLA_DIAGNOSTIC_DIR"] = str(folder / "login-failures")
    sanitizer = SecretFilter(worker)
    configure_runtime_logging(capture_console=True, log_directory=folder, log_filter=sanitizer)
    operation = asyncio.create_task(run_accounts(worker, folder, sanitizer))
    loop = asyncio.get_running_loop()
    def request_stop():
        if not operation.cancelling():
            operation.cancel()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)
    try:
        return await operation
    except asyncio.CancelledError:
        return 130
    finally:
        sys.stdout.flush()
        sys.stderr.flush()


async def supervise(directory):
    job = read_json(directory / "job.json")
    sanitizer = SecretFilter(job)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    processes, pumps = [], []
    state = {"run_id": directory.name, "state": "starting", "processes": [],
             "started_at": datetime.now().astimezone().isoformat()}
    async def pump(stream, path):
        with path.open("a", encoding="utf-8") as output:
            while line := await stream.readline():
                output.write(sanitizer.clean(line.decode("utf-8", errors="replace")))
                output.flush()
    async def spawn(command, log, env=None, name="mihomo"):
        process = await asyncio.create_subprocess_exec(
            *command, stdin=subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env,
        )
        processes.append(process)
        pumps.append(asyncio.create_task(pump(process.stdout, log)))
        state["processes"].append({"pid": process.pid, "birth": birth(process.pid), "name": name,
                                   "started_at": datetime.now().astimezone().isoformat()})
        write_json(directory / "run.json", state)
        return process
    result = 1
    try:
        # Check all listeners before starting Mihomo; a port conflict must not
        # accidentally send an account through an unrelated proxy.
        for worker in job["workers"]:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", worker["proxy"]["port"]))
        mihomo_dir = directory / "mihomo"
        mihomo_dir.mkdir(mode=0o700)
        (mihomo_dir / "providers").mkdir()
        config = mihomo_dir / "config.yaml"
        config.write_text(yaml.safe_dump(mihomo_config(job, mihomo_dir), allow_unicode=True), encoding="utf-8")
        config.chmod(0o600)
        check = await spawn([job["binary"], "-t", "-d", str(mihomo_dir), "-f", str(config)], directory / "mihomo-check.log", name="mihomo-check")
        try:
            code = await asyncio.wait_for(check.wait(), 30)
        except asyncio.TimeoutError:
            raise RuntimeError("Mihomo 配置检查超时")
        if code:
            raise RuntimeError("Mihomo 配置检查失败，未登录账号")
        proxy = await spawn([job["binary"], "-d", str(mihomo_dir), "-f", str(config)], directory / "mihomo.log")
        deadline = loop.time() + 30
        pending = {w["proxy"]["port"] for w in job["workers"]}
        while pending:
            if stopped.is_set():
                raise asyncio.CancelledError
            if proxy.returncode is not None or loop.time() >= deadline:
                raise RuntimeError("Mihomo 未能建立全部代理端口，未登录账号")
            for port in list(pending):
                if not owns_listener(proxy.pid, port):
                    continue
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.3)
                except (OSError, TimeoutError):
                    continue
                writer.close()
                await writer.wait_closed()
                pending.remove(port)
            if pending:
                await asyncio.sleep(0.1)
        workers = []
        for worker in job["workers"]:
            if stopped.is_set():
                raise asyncio.CancelledError
            env = dict(os.environ, PYTHONUTF8="1")
            workers.append(await spawn(entry_command("_worker", directory, worker["id"]), directory / f"{worker['id']}-console.log", env, name="worker:" + worker["id"]))
        state["state"] = "running"
        write_json(directory / "run.json", state)
        completion = asyncio.gather(*(p.wait() for p in workers))
        refusal = asyncio.create_task(stopped.wait())
        proxy_exit = asyncio.create_task(proxy.wait())
        try:
            await asyncio.wait((completion, refusal, proxy_exit), return_when=asyncio.FIRST_COMPLETED)
            if stopped.is_set():
                state["state"], result = "stopped", 130
            elif proxy_exit.done() and not completion.done():
                raise RuntimeError("Mihomo 意外退出，停止任务，禁止直连")
            else:
                codes = await completion
                result = 0 if all(code == 0 for code in codes) else 2
                state["state"] = "completed" if result == 0 else "failed"
        finally:
            refusal.cancel()
            proxy_exit.cancel()
            completion.cancel()
            await asyncio.gather(refusal, proxy_exit, completion, return_exceptions=True)
    except asyncio.CancelledError:
        state["state"], result = "stopped", 130
    except Exception as exc:
        state.update(state="failed", error=sanitizer.clean(str(exc)))
    finally:
        for process in reversed(processes):
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        try:
            await asyncio.wait_for(asyncio.gather(*(p.wait() for p in processes)), 18)
        except asyncio.TimeoutError:
            for process in processes:
                if process.returncode is None:
                    process.kill()
            await asyncio.gather(*(p.wait() for p in processes))
        await asyncio.gather(*pumps, return_exceptions=True)
        for record, process in zip(state["processes"], processes):
            record["exit_code"] = process.returncode
        state["exit_code"] = result
        state["finished_at"] = datetime.now().astimezone().isoformat()
        write_json(directory / "run.json", state)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="配置驱动的多进程组合任务")
    sub = parser.add_subparsers(dest="command", required=True, metavar="{validate,start,status,stop}")
    for name in ("validate", "start"):
        child = sub.add_parser(name)
        child.add_argument("--job", type=Path, required=True)
        if name == "start":
            child.add_argument("--detach", action="store_true")
            child.add_argument("--runs-dir", type=Path, default=PROJECT_ROOT / "logs" / "jobs")
    for name in ("status", "stop"):
        child = sub.add_parser(name)
        child.add_argument("--run", required=True)
        child.add_argument("--runs-dir", type=Path, default=PROJECT_ROOT / "logs" / "jobs")
    for name in ("_supervise", "_worker"):
        child = sub.add_parser(name)
        child.add_argument("directory", type=Path)
        if name == "_worker":
            child.add_argument("worker")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            job = load_job(args.job)
            print(json.dumps({"valid": True, "workers": [{"id": w["id"], "accounts": len(w["accounts"]),
                             "mode": w["mode"], "port": w["proxy"]["port"]} for w in job["workers"]]}, ensure_ascii=False, indent=2))
            return 0
        if os.name != "posix" or not Path("/proc").is_dir():
            raise ValueError("配置任务进程管理目前用于 Linux；Windows 请使用原交互菜单")
        os.umask(0o077)
        if args.command in {"_supervise", "_worker"}:
            operation = supervise(args.directory.resolve()) if args.command == "_supervise" else worker_main(args.directory.resolve(), args.worker)
            raise SystemExit(asyncio.run(operation))
        if args.command in {"status", "stop"}:
            directory = run_directory(args.runs_dir.resolve(), args.run)
            print(json.dumps(stop(directory) if args.command == "stop" else status(directory), ensure_ascii=False, indent=2))
            return 0
        job = load_job(args.job)
        args.runs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = args.runs_dir.resolve() / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        directory.mkdir(mode=0o700)
        write_json(directory / "job.json", job)
        write_json(directory / "run.json", {"run_id": directory.name, "state": "starting"})
        with (directory / "supervisor-console.log").open("a", encoding="utf-8") as output:
            process = subprocess.Popen(entry_command("_supervise", directory), stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                       env=dict(os.environ, PYTHONUTF8="1"))
        write_json(directory / "launch.json", {"pid": process.pid, "birth": birth(process.pid),
                                                "started_at": datetime.now().astimezone().isoformat()})
        print(json.dumps({"run_id": directory.name, "directory": str(directory), "pid": process.pid}, ensure_ascii=False), flush=True)
        if args.detach:
            return 0
        try:
            raise SystemExit(process.wait())
        except KeyboardInterrupt:
            stop(directory)
            raise SystemExit(130)
    except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as exc:
        message = "节点 YAML 无法解析，请检查文件格式" if isinstance(exc, yaml.YAMLError) else str(exc)
        print(f"配置或运行失败：{message}", file=sys.stderr)
        raise SystemExit(1)
