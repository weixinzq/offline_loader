"""Run ten configured workflows through the existing service HTTP API."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import aiohttp
from pydantic import ValidationError

from src.service.contracts import TaskSpec


# 在对应出口的 steps 中填写任务。每项配置会用于该出口的全部就绪账号。
# 脚本示例：[{"kind": "script", "script_id": "从 scripts list 复制的实际 ID"}]
# 消息示例：[{"kind": "messages", "messages": [
#     {"id": -1, "cmd": "#time", "param": {"seconds": 1}}
# ]}]
# 可以在 steps 中组合多个消息、脚本步骤；脚本内部配置不会被改写。
GROUP_WORKFLOWS = [
    {"exit": 1, "steps": [{"kind": "script", "script_id": "double_auto_escape"}], "repetitions": 9999, "repeat_interval": 0.2, "message_delay": 0.2},  # 违心11～40
    {"exit": 2, "steps": [{"kind": "script", "script_id": "double_auto_escape"}], "repetitions": 9999, "repeat_interval": 0.2, "message_delay": 0.2},  # 违心41～70
    {"exit": 3, "steps": [{"kind": "script", "script_id": "double_auto_escape"}], "repetitions": 9999, "repeat_interval": 0.2, "message_delay": 0.2},  # 违心71～100
    {"exit": 4, "steps": [{"kind": "script", "script_id": "double_auto_escape"}], "repetitions": 9999, "repeat_interval": 0.2, "message_delay": 0.2},  # 违心101～131，跳过104
    {"exit": 5, "steps": [{"kind": "script", "script_id": "double_auto_escape"}], "repetitions": 9999, "repeat_interval": 0.2, "message_delay": 0.2},  # 违心132～161
    {"exit": 6, "steps": [{"kind": "script", "script_id": "double_auto_escape"}], "repetitions": 9999, "repeat_interval": 0.2, "message_delay": 0.2},  # 违心162～191
    {"exit": 7, "steps": [{"kind": "script", "script_id": "double_auto_escape"}], "repetitions": 9999, "repeat_interval": 0.2, "message_delay": 0.2},  # 违心192～221
    {"exit": 8, "steps": [{"kind": "script", "script_id": "double_auto_escape"}], "repetitions": 9999, "repeat_interval": 0.2, "message_delay": 0.2},  # 违心222～252，跳过244
    {"exit": 9, "steps": [{"kind": "script", "script_id": "double_auto_escape"}], "repetitions": 9999, "repeat_interval": 0.2, "message_delay": 0.2},  # 违心253～282
    {"exit": 10, "steps": [{"kind": "script", "script_id": "double_auto_escape"}], "repetitions": 9999, "repeat_interval": 0.2, "message_delay": 0.2},  # 违心283～312
]

# 单独分配任务的账号；其余账号仍使用所在出口的配置。
ACCOUNT_SCRIPT_OVERRIDES = {"违心11": "main_double_auto"}

ROOT = Path(__file__).resolve().parent
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
BLOCKED = {"verification_required", "rate_limited", "login_blocked"}
MAX_CONNECTION_ATTEMPTS = 5  # 包含首次登录；只补试未就绪账号。
LOGIN_CONCURRENCY = 20


def select_account_groups(accounts: list[dict]) -> list[list[dict]]:
    """Used during provisioning only; normal runs use the saved stable IDs."""
    selected = []
    for account in accounts:
        match = re.fullmatch(r"违心(\d+)", account["label"])
        if match and int(match[1]) >= 11:
            selected.append((int(match[1]), account))
    selected.sort(key=lambda item: item[0])
    selected = [account for _, account in selected[:300]]
    if len(selected) != 300 or selected[0]["label"] != "违心11":
        raise ValueError("需要从违心11开始的300个现有账号")
    identities = [str(account["account"]).strip().casefold() for account in selected]
    if len(set(identities)) != 300 or len({account["label"] for account in selected}) != 300:
        raise ValueError("选定账号存在重复标签或重复登录账号")
    return [selected[offset:offset + 30] for offset in range(0, 300, 30)]


def make_specs(manifest: dict, workflows: list[dict]) -> list[dict]:
    groups = manifest.get("groups", [])
    if manifest.get("version") != 1 or [group.get("exit") for group in groups] != list(range(1, 11)):
        raise ValueError("出口映射必须为 version=1，依次包含出口1至10")
    if len(workflows) != 10 or {item.get("exit") for item in workflows} != set(range(1, 11)):
        raise ValueError("GROUP_WORKFLOWS 必须包含10个不同的出口配置")
    if len({group["group_id"] for group in groups}) != 10:
        raise ValueError("出口分组ID不可重复")
    ids = [account["account_id"] for group in groups for account in group["accounts"]]
    if len(ids) != 300 or len(set(ids)) != 300 or any(len(group["accounts"]) != 30 for group in groups):
        raise ValueError("出口映射必须包含300个不同账号，每组30个")
    configurations = {item["exit"]: item for item in workflows}
    empty = [group["exit"] for group in groups if not configurations[group["exit"]].get("steps")]
    if empty:
        raise ValueError(f"请先填写 GROUP_WORKFLOWS 的 steps，尚未填写的出口：{empty}；未登录任何账号")
    specs = []
    for group in groups:
        options = {key: value for key, value in configurations[group["exit"]].items() if key != "exit"}
        try:
            spec = TaskSpec.model_validate({**options, "kind": "execute", "session_policy": "existing",
                "mode": "bounded", "concurrency": 30, "account_ids": [row["account_id"] for row in group["accounts"]]})
        except ValidationError as error:
            raise ValueError(f"出口{group['exit']:02}任务配置无效：{error.errors(include_input=False)}") from None
        specs.append(spec.model_dump(mode="json"))
    return specs


class Backend:
    def __init__(self, session):
        self.session = session

    async def request(self, method, path, body=None):
        async with self.session.request(method, path, json=body) as response:
            data = await response.json()
            if response.status >= 400:
                error = data.get("error", {})
                raise ValueError(f"{error.get('code', response.status)}: {error.get('message', '后台请求失败')}")
            return data


async def preflight(api, manifest, specs, excluded=frozenset(), account_script_overrides=None):
    accounts, groups, scripts = await asyncio.gather(*(api.request("GET", path) for path in
        ("/api/v1/accounts", "/api/v1/egress-groups", "/api/v1/scripts")))
    by_id = {account["account_id"]: account for account in accounts}
    by_group = {group["group_id"]: group for group in groups}
    script_ids = {script["script_id"] for script in scripts}
    labels = {row["label"] for group in manifest["groups"] for row in group["accounts"]}
    for label, script_id in (account_script_overrides or {}).items():
        if label not in labels or script_id not in script_ids:
            raise ValueError(f"单独账号任务配置无效：{label} → {script_id}；未登录任何账号")
    identities = set()
    for group, spec in zip(manifest["groups"], specs):
        current = by_group.get(group["group_id"])
        if (not current or not current["routing_confirmed"] or current["max_online"] != 30
                or current["clash_policy"] != group["clash_policy"]):
            raise ValueError(f"出口{group['exit']:02}分组或路由配置发生变化，请重新核对")
        for saved in group["accounts"]:
            account = by_id.get(saved["account_id"])
            if not account or account["label"] != saved["label"] or account.get("egress_group_id") != group["group_id"]:
                raise ValueError(f"账号映射发生变化：{saved['label']}；不会自动重新分配账号")
            identity = str(account["account"]).strip().casefold()
            if identity in identities:
                raise ValueError(f"登录账号重复：{saved['label']}")
            identities.add(identity)
        for step in spec["steps"]:
            if step["kind"] == "script" and step["script_id"] not in script_ids:
                raise ValueError(f"出口{group['exit']:02}的专用脚本不存在：{step['script_id']}")
            if step["kind"] == "messages":
                await api.request("POST", "/api/v1/messages/validate", {"messages": step["messages"]})
    # Reconnect preflight also permits an offline session awaiting cleanup; no connection is changed.
    checked = await api.request("POST", "/api/v1/tasks/validate", {
        "kind": "reconnect", "account_ids": [row for spec in specs for row in spec["account_ids"] if row not in excluded], "concurrency": LOGIN_CONCURRENCY})
    if checked["max_online"] < 300 or checked.get("login_concurrency") != LOGIN_CONCURRENCY:
        raise ValueError(f"后台需要 --max-online 300 --login-concurrency {LOGIN_CONCURRENCY}，"
                         f"实际登录并发为 {checked.get('login_concurrency')}；请检查服务版本和启动参数")


async def run_workflows(api, manifest, workflows, *, dry_run=False, reconnect_all=False,
                        exclude_account_ids=(), account_script_overrides=None,
                        on_update=None, sleep=asyncio.sleep, log=print):
    specs = make_specs(manifest, workflows)
    account_script_overrides = dict(account_script_overrides or {})
    excluded = set(exclude_account_ids)
    identities = {row for spec in specs for row in spec["account_ids"]}
    if excluded - identities:
        raise ValueError(f"排除账号不在固定映射中：{sorted(excluded - identities)}")
    if excluded == identities:
        raise ValueError("全部300账号均被排除，没有可执行账号")
    await preflight(api, manifest, specs, excluded, account_script_overrides)
    if dry_run:
        return {"dry_run": True, "accounts": 300 - len(excluded), "excluded_account_ids": sorted(excluded),
                "account_script_overrides": account_script_overrides,
                "login_concurrency": LOGIN_CONCURRENCY, "execution_concurrency": 300 - len(excluded),
                "groups": [{"exit": group["exit"], "accounts": sum(row["account_id"] not in excluded for row in group["accounts"]),
                            "repetitions": spec["repetitions"], "steps": len(spec["steps"])}
                           for group, spec in zip(manifest["groups"], specs)]}
    # Recheck actual exits before any login, also after a backend restart.
    probes = await asyncio.gather(*(api.request("POST", f"/api/v1/egress-groups/{group['group_id']}/probe")
                                   for group in manifest["groups"]))
    ips = [probe.get("exit_ip") for probe in probes]
    if not all(ips) or len(set(ips)) != 10:
        raise ValueError("未取得10个不同的公网出口，未开始登录")
    created = set()
    running = []

    async def submit(path, body):
        request = asyncio.create_task(api.request("POST", path, body))
        try:
            record = await asyncio.shield(request)
        except asyncio.CancelledError:
            record = await request
            created.add(record["task_id"])
            if on_update:
                on_update(record)
            raise
        created.add(record["task_id"])
        if on_update:
            on_update(record)
        return record["task_id"]

    async def wait(task_id):
        while True:
            try:
                task = await api.request("GET", f"/api/v1/tasks/{task_id}")
            except (aiohttp.ClientError, TimeoutError) as error:
                log(f"任务{task_id}状态查询失败（{type(error).__name__}），稍后重试；不会重发执行任务")
                await sleep(2)
                continue
            if on_update:
                on_update(task)
            if task["state"] in TERMINAL:
                return task
            await sleep(1)

    async def connect_group(group, outcome, missing):
        try:
            task_id = await submit("/api/v1/sessions/reconnect", {
                "account_ids": missing, "concurrency": LOGIN_CONCURRENCY})
        except (ValueError, aiohttp.ClientError, TimeoutError) as error:
            outcome["error"] = str(error)
            return
        outcome["connection_tasks"].append(task_id)
        for account_id in missing:
            outcome["connection_attempts"][account_id] += 1
        task = await wait(task_id)
        outcome["connection_results"].update({row["account_id"]: row for row in task["account_results"]})
        codes = {row["error"]["code"] for row in task["account_results"] if row.get("error")}
        if task.get("error"):
            codes.add(task["error"]["code"])
        if codes & BLOCKED:
            outcome["blocked"] = sorted(codes & BLOCKED)
            log(f"出口{group['exit']:02}：触发登录停止规则，保留已就绪账号")

    async def execute_group(group, spec, connection, ready):
        selected = [row for row in group["accounts"] if row["account_id"] in ready]
        result = {**connection, "ready": len(selected),
                  "skipped": [row["label"] for row in group["accounts"] if row["account_id"] not in ready],
                  "task_id": None, "state": "skipped"}
        log(f"出口{group['exit']:02}：就绪{len(selected)}/30，跳过{result['skipped']}")
        async def execute_batch(accounts, script_id):
            payload = {**spec, "account_ids": [row["account_id"] for row in accounts]}
            if script_id is not None:
                payload["steps"] = [{"kind": "script", "script_id": script_id}]
            batch = {"task_id": None, "state": "failed", "account_results": []}
            task_id = None
            try:
                task_id = await submit("/api/v1/tasks", payload)
                log(f"出口{group['exit']:02}：执行任务 {task_id}，账号{len(accounts)}个，"
                    f"脚本{script_id or [step.get('script_id', step['kind']) for step in spec['steps']]}，轮次{spec['repetitions']}")
                task = await wait(task_id)
                batch.update(task_id=task_id, state=task["state"], account_results=task["account_results"])
            except (ValueError, aiohttp.ClientError, TimeoutError) as error:
                if task_id is not None:
                    await api.request("POST", f"/api/v1/tasks/{task_id}/cancel")
                    await wait(task_id)
                batch.update(task_id=task_id, state="failed", error=f"{type(error).__name__}: {error}")
            return batch

        batches = {}
        for row in selected:
            batches.setdefault(account_script_overrides.get(row["label"]), []).append(row)
        if batches:
            outcomes = await asyncio.gather(*(execute_batch(accounts, script_id) for script_id, accounts in batches.items()))
            result.update(outcomes[0])
            result["task_ids"] = [item["task_id"] for item in outcomes if item["task_id"]]
            result["account_results"] = [row for item in outcomes for row in item["account_results"]]
            if len(outcomes) > 1:
                result["state"] = "succeeded" if all(item["state"] == "succeeded" for item in outcomes) else "failed"
        return result

    try:
        connections = [{"exit": group["exit"], "connection_tasks": [], "blocked": None,
                        "connection_attempts": {row["account_id"]: 0 for row in group["accounts"]},
                        "connection_results": {}} for group in manifest["groups"]]
        cycle = 0
        while True:
            sessions = {row["account_id"]: row for row in await api.request("GET", "/api/v1/sessions")}
            pending = []
            for group, outcome in zip(manifest["groups"], connections):
                if outcome["blocked"] or outcome.get("error"):
                    continue
                missing = [row["account_id"] for row in group["accounts"]
                           if row["account_id"] not in excluded
                           and outcome["connection_attempts"][row["account_id"]] < MAX_CONNECTION_ATTEMPTS
                           and ((cycle == 0 and reconnect_all)
                                or sessions.get(row["account_id"], {}).get("state") != "ready")]
                if missing:
                    pending.append((group, outcome, missing))
            if not pending:
                break
            if cycle:
                await sleep(2)
            cycle += 1
            for group, outcome, missing in pending:
                log(f"出口{group['exit']:02}：连接批次{cycle}，待连接{len(missing)}个；每账号最多{MAX_CONNECTION_ATTEMPTS}次")
            running = [asyncio.create_task(connect_group(*item)) for item in pending]
            await asyncio.gather(*running)
        ready = {row["account_id"] for row in await api.request("GET", "/api/v1/sessions") if row["state"] == "ready"}
        ready -= excluded
        if reconnect_all:
            ready &= {account_id for outcome in connections
                      for account_id, row in outcome["connection_results"].items() if row["state"] == "succeeded"}
        running = [asyncio.create_task(execute_group(group, spec, connection, ready))
                   for group, spec, connection in zip(manifest["groups"], specs, connections)]
        results = await asyncio.gather(*running)
        return {"selected": 300 - len(excluded), "excluded_account_ids": sorted(excluded),
                "ready": sum(row["ready"] for row in results), "groups": results}
    except BaseException:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        # Only cancel this run's tasks. Persistent ready connections remain owned by the backend.
        async def cancel_owned():
            for task_id in sorted(created):
                task = await api.request("POST", f"/api/v1/tasks/{task_id}/cancel")
                if task["state"] not in TERMINAL:
                    await wait(task_id)
        try:
            async with asyncio.timeout(25):
                await cancel_owned()
        except (TimeoutError, ValueError, aiohttp.ClientError):
            log(f"本次任务清理尚未全部确认，请检查任务编号：{sorted(created)}")
        raise


async def ensure_backend(api, url, dry_run):
    try:
        await api.request("GET", "/api/v1/health")
    except aiohttp.ClientConnectionError:
        if dry_run or url != "http://127.0.0.1:8765":
            raise ValueError("后台未运行；dry-run不会启动后台") from None
        await asyncio.to_thread(subprocess.run, [str(ROOT / "manage.sh"), "start"], check=True)
        for _ in range(30):
            await asyncio.sleep(0.5)
            try:
                await api.request("GET", "/api/v1/health")
                break
            except aiohttp.ClientConnectionError:
                continue
        else:
            raise ValueError("后台启动后未就绪")


async def stop_latest_workflow(api, directory, *, sleep=asyncio.sleep):
    paths = sorted(directory.glob("*/status.json"))
    if not paths:
        print("没有战斗运行记录")
        return
    path = paths[-1]
    status = json.loads(path.read_text(encoding="utf-8"))
    task_ids = list(status.get("tasks", {}))
    # Task IDs come from this workflow's record; unrelated web tasks are untouched.
    async def cancel(task_id):
        task = await api.request("POST", f"/api/v1/tasks/{task_id}/cancel")
        while task["state"] not in TERMINAL:
            await sleep(1)
            task = await api.request("GET", f"/api/v1/tasks/{task_id}")
        return task_id, task
    async with asyncio.timeout(50):
        tasks = await asyncio.gather(*(cancel(task_id) for task_id in task_ids))
    for task_id, task in tasks:
        rows = [{key: row.get(key) for key in ("account_id", "state", "completed_repetitions", "attempted_repetitions", "skipped_repetitions", "error")}
                for row in task["account_results"]]
        status["tasks"][task_id].update(state=task["state"], accounts=rows)
    status["state"] = "cancelled"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    print(f"已确认本轮{len(tasks)}个后端任务结束；就绪连接保留")


async def main_async(options):
    if options.stop_owned:
        token = os.environ.get("AOLA_ADMIN_TOKEN") or (ROOT / "data/service/admin.token").read_text().strip()
        async with aiohttp.ClientSession(base_url=options.url, headers={"Authorization": "Bearer " + token},
                                        trust_env=False, timeout=aiohttp.ClientTimeout(total=30)) as session:
            await stop_latest_workflow(Backend(session), ROOT / "logs/cloud-battles")
        return 0
    manifest = json.loads(options.manifest.read_text(encoding="utf-8"))
    make_specs(manifest, GROUP_WORKFLOWS)  # Empty templates cannot start a service or log in.
    token = os.environ.get("AOLA_ADMIN_TOKEN") or (ROOT / "data/service/admin.token").read_text().strip()
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(base_url=options.url, headers={"Authorization": "Bearer " + token},
                                    trust_env=False, timeout=timeout) as session:
        api = Backend(session)
        await ensure_backend(api, options.url, options.dry_run)
        arguments = dict(exclude_account_ids=options.exclude_account_id,
                         account_script_overrides=ACCOUNT_SCRIPT_OVERRIDES)
        if options.dry_run:
            result = await run_workflows(api, manifest, GROUP_WORKFLOWS, dry_run=True, **arguments)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
        directory = ROOT / "logs" / "cloud-battles" / run_id
        directory.mkdir(parents=True, mode=0o700)
        status = {"run_id": run_id, "state": "running", "tasks": {}}
        failures = set()

        def write(name, data):
            path = directory / name
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)

        with (directory / "run.log").open("w", encoding="utf-8", buffering=1) as stream:
            def log(message):
                line = datetime.now().astimezone().isoformat() + " " + str(message).replace(token, "[redacted]")
                stream.write(line + "\n")
                print(line, flush=True)

            def update(task):
                rows = [{key: row.get(key) for key in ("account_id", "state", "completed_repetitions", "attempted_repetitions", "skipped_repetitions", "error")}
                        for row in task["account_results"]]
                status["tasks"][task["task_id"]] = {"kind": task["kind"], "state": task["state"], "accounts": rows}
                for row in rows:
                    key = (task["task_id"], row["account_id"])
                    if row["state"] == "failed" and key not in failures:
                        failures.add(key)
                        log(f"任务{task['task_id']}，账号{row['account_id']}失败：{row['error']}")
                write("status.json", status)

            log(f"本轮战斗日志：{directory}；不会执行初始化文件")
            write("status.json", status)
            try:
                result = await run_workflows(api, manifest, GROUP_WORKFLOWS, on_update=update, log=log, **arguments)
                succeeded = sum(row["state"] == "succeeded" for group in result["groups"] for row in group.get("account_results", []))
                status.update(state="completed" if succeeded == result["selected"] else "partial",
                              selected=result["selected"], ready=result["ready"], succeeded=succeeded,
                              finished_at=datetime.now().astimezone().isoformat())
                write("result.json", result)
                log(f"本轮结束：选定{result['selected']}，就绪{result['ready']}，完成全部轮次{succeeded}")
                return 0 if status["state"] == "completed" else 1
            except BaseException as error:
                status.update(state="cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                              error=str(error).replace(token, "[redacted]"))
                raise
            finally:
                write("status.json", status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "data/service/cloud-workflows.json")
    parser.add_argument("--url", default=os.environ.get("AOLA_SERVICE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stop-owned", action="store_true", help="取消最近一轮记录的后台任务，保留就绪连接")
    parser.add_argument("--exclude-account-id", action="append", default=[], help="排除账号ID；可重复指定，仅本轮生效")
    try:
        return asyncio.run(main_async(parser.parse_args()))
    except KeyboardInterrupt:
        print("已请求停止本次工作流，已就绪的常驻连接保留")
        return 130
    except (ValueError, OSError, aiohttp.ClientError, TimeoutError, subprocess.CalledProcessError) as error:
        message = str(error)
        if token := os.environ.get("AOLA_ADMIN_TOKEN"):
            message = message.replace(token, "[redacted]")
        print(f"工作流未完成：{message}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
