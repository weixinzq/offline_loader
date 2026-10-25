"""Background task orchestration independent of HTTP, CLI and desktop clients."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from src.scripting.composer import execute_combination
from src.service.contracts import ServiceError, TaskSpec
from src.service.runtime import Redactor
from src.service.execution_runner import run_execution
from src.network.server_probe import transport_result


_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted", "skipped"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot(spec: TaskSpec) -> dict:
    value = spec.model_dump(mode="json")
    for step in value["steps"]:
        for message in step.get("messages", []):
            message["param"] = "[redacted]"
    return value


@dataclass
class _Run:
    spec: TaskSpec
    accounts: list[dict]
    steps: tuple
    record: dict
    redactor: Redactor
    task: asyncio.Task | None = None
    started: bool = False
    finalizing: bool = False
    stop_reason: str | None = None
    workers: set[asyncio.Task] = field(default_factory=set)
    group_semaphores: dict = field(default_factory=dict)
    egress_profiles: dict = field(default_factory=dict)


class TaskService:
    def __init__(self, store, sessions, scripts, messages, events, executor=None):
        self.store = store
        self.sessions = sessions
        self.scripts = scripts
        self.messages = messages
        self.events = events
        self.executor = executor or execute_combination
        self._runs: dict[str, _Run] = {}
        self._closing = False
        self.sessions.on_log = self._log
        self.sessions.on_blocked = self._blocked

    def _prepare(self, spec) -> tuple[TaskSpec, list[dict], tuple]:
        spec = spec.model_copy(deep=True) if isinstance(spec, TaskSpec) else TaskSpec.model_validate(spec)
        if not spec.account_ids or len(set(spec.account_ids)) != len(spec.account_ids):
            raise ServiceError("invalid_accounts", "请选择不重复的目标账号")
        accounts = [self.store.get_account(account_id, private=True) for account_id in spec.account_ids]
        if spec.kind == "server_probe":
            accounts = accounts[:len(spec.server_targets)]
        steps = ()
        if spec.kind in {"execute", "server_probe"}:
            if spec.kind == "execute" and not spec.steps:
                raise ServiceError("invalid_steps", "执行任务至少需要一个步骤")
            for step in spec.steps:
                if step.kind == "messages":
                    self.messages.validate(step.messages)
            steps = tuple(self.scripts.build_steps(spec.steps))
        elif spec.steps:
            raise ServiceError("invalid_steps", "会话操作不能包含执行步骤")
        return spec, accounts, steps

    def validate(self, spec) -> dict:
        spec, accounts, steps = self._prepare(spec)
        capacity = self.sessions.check(spec, accounts)
        result = {"valid": True, "account_count": len(accounts), "step_count": len(steps),
                  "spec": _snapshot(spec), **capacity}
        if spec.kind == "server_probe":
            result["server_assignments"] = self._server_rows(spec, accounts)
            result["effective_concurrency"] = min(spec.concurrency, self.sessions.login_concurrency,
                capacity["required_capacity"], len(accounts))
            result["max_transport_attempts"] = (spec.probe_attempts or 1) * sum(
                int(target.flash_port > 0 and target.transport != "h5") +
                int(target.h5_port > 0 and bool(target.domain) and target.transport != "flash")
                for target in spec.server_targets)
        return result

    @staticmethod
    def _server_rows(spec, accounts):
        return [{"target_index": index, "server": target.model_dump(),
                 "account_id": accounts[index % len(accounts)]["account_id"],
                 "egress_group_id": accounts[index % len(accounts)]["egress_group_id"],
                 "exit_ip": None, "state": "queued", "started_at": None, "finished_at": None,
                 "error": None, "flash": transport_result(), "h5": transport_result()}
                for index, target in enumerate(spec.server_targets)]

    def _publish(self, run: _Run) -> None:
        self.store.save_task(run.record)
        # Store applies its credential filter before the event leaves this process.
        self.events.publish("task", self.store.get_task(run.record["task_id"]))

    def _log(self, task_id: str, account_id: str | None, message: str) -> None:
        run = self._runs.get(task_id)
        if run is not None:
            message = run.redactor.clean(message)
        row = self.store.append_log(task_id, account_id, message)
        self.events.publish("log", row)

    async def submit(self, spec) -> dict:
        if self._closing:
            raise ServiceError("service_stopping", "服务正在关闭", 503)
        spec, accounts, steps = self._prepare(spec)
        task_id = uuid4().hex
        skipped = {row["account_id"]: row for row in await self.sessions.reserve(task_id, spec, accounts)}
        record = {
            "task_id": task_id, "kind": spec.kind, "state": "queued", "created_at": _now(),
            "started_at": None, "finished_at": None, "spec": _snapshot(spec), "error": None,
            "account_results": [
                {"account_id": account["account_id"], "egress_group_id": account.get("egress_group_id"),
                 "state": "skipped" if account["account_id"] in skipped else "queued",
                 "completed_repetitions": 0,
                 "attempted_repetitions": 0, "skipped_repetitions": 0,
                 "error": {"code": "session_not_ready", "message": skipped[account["account_id"]]["reason"]}
                          if account["account_id"] in skipped else None,
                 "network_write": {"status": "not_attempted", "count": 0},
                 "business_confirmation": "unknown"}
                for account in accounts
            ],
        }
        if spec.kind == "server_probe":
            record["server_results"] = self._server_rows(spec, accounts)
        run = _Run(spec, [account for account in accounts if account["account_id"] not in skipped], steps, record,
                   Redactor(self.store, accounts, spec.model_dump(mode="json")))
        self._runs[task_id] = run
        try:
            self._publish(run)
            for row in skipped.values():
                self._log(task_id, row["account_id"], f"账号“{row['label']}”：{row['reason']}")
        except BaseException:
            self._runs.pop(task_id, None)
            self.sessions.release(task_id)
            raise
        run.task = asyncio.create_task(self._run(run), name=f"service-task-{task_id}")
        return self.get(task_id)

    def list(self) -> list[dict]:
        return self.store.list_tasks()

    def clear_finished(self) -> dict:
        task_ids = [record["task_id"] for record in self.list()
                    if record["state"] in _TERMINAL and record["task_id"] not in self._runs]
        self.store.delete_tasks(task_ids)
        if task_ids:
            self.events.publish("task", {"deleted_task_ids": task_ids})
        return {"deleted_count": len(task_ids), "task_ids": task_ids}

    def get(self, task_id: str) -> dict:
        return self.store.get_task(task_id)

    def results(self, task_id: str) -> dict:
        task = self.get(task_id)
        result = {"task_id": task_id, "state": task["state"], "error": task.get("error"),
                  "account_results": task["account_results"]}
        if task.get("kind") == "server_probe":
            result["server_results"] = task.get("server_results", [])
        return result

    def logs(self, task_id: str, after=0, limit=200):
        self.get(task_id)
        return self.store.get_logs(task_id, after=after, limit=limit)

    async def cancel(self, task_id: str) -> dict:
        record = self.get(task_id)
        if record["state"] in _TERMINAL:
            return record
        run = self._runs.get(task_id)
        if run is None:
            raise ServiceError("task_unavailable", "任务不属于当前运行进程", 409)
        if run.stop_reason is None:
            run.stop_reason = "cancelled"
            run.record["state"] = "cancelling"
            self._publish(run)
            if run.started and not run.finalizing:
                run.task.cancel()
        if run.task is not None and run.task.done():
            run.task = asyncio.create_task(self._finish(run), name=f"cleanup-task-{task_id}")
        return self.get(task_id)

    def _blocked(self, task_id: str, code: str) -> None:
        run = self._runs.get(task_id)
        if run is None:
            return
        if run.stop_reason is not None:
            return
        run.stop_reason = code
        message = "需要人工验证；该批任务已停止" if code == "verification_required" else "登录受到服务器限制；该批任务已停止"
        run.record["error"] = {"code": code, "message": message}
        current = asyncio.current_task()
        for worker in run.workers:
            if worker is not current and not worker.done():
                worker.cancel()

    def _error(self, run: _Run, row: dict, exc: Exception) -> None:
        code = getattr(exc, "code", "execution_failed")
        message = run.redactor.clean(exc)
        row.update(state="failed", error={"code": code, "message": message})
        self._log(run.record["task_id"], row["account_id"], message)
        if code in {"verification_required", "rate_limited", "login_blocked"}:
            self._blocked(run.record["task_id"], code)

    async def _connect(self, run: _Run, account: dict, row: dict) -> None:
        row["state"] = "connecting"
        self._publish(run)
        await self.sessions.connect(run.record["task_id"], account,
                                    temporary=run.spec.kind == "execute",
                                    reconnect=run.spec.kind == "reconnect")
        row["state"] = "ready"

    async def _execute(self, run: _Run, row: dict) -> None:
        account_id = row["account_id"]
        context = self.sessions.context(run.record["task_id"], account_id)
        row["state"] = "running"
        # Cancellation may interrupt a write before the legacy executor returns its audit.
        row["network_write"] = {"status": "unknown", "count": None}
        self._publish(run)
        outcome = {"network_write": {"status": "unknown", "count": None}}
        options = dict(repetitions=run.spec.repetitions, repeat_interval=run.spec.repeat_interval,
                       message_delay=run.spec.message_delay,
                       preserve_current_battle=run.spec.session_policy == "existing")
        try:
            remote = getattr(context, "execute_steps", None)
            if remote is not None:
                await remote([step.model_dump(mode="json") for step in run.spec.steps], outcome, **options)
            else:
                await run_execution(account_id, context, run.steps, outcome, executor=self.executor, **options)
        finally:
            row["network_write"] = outcome["network_write"]
        row["completed_repetitions"] = outcome["completed_repetitions"]
        row["attempted_repetitions"] = outcome.get("attempted_repetitions", row["completed_repetitions"])
        row["skipped_repetitions"] = outcome.get("skipped_repetitions", 0)
        row["state"] = "succeeded" if outcome["success"] else "failed"
        row["step_results"] = [
            {**step, "error": run.redactor.clean(step["error"]) if step["error"] else None}
            for step in outcome["step_results"]
        ]
        if not outcome["success"]:
            errors = [step["error"] for step in outcome["step_results"]
                      if step["error"] and not step.get("skipped", False)]
            row["error"] = {"code": "execution_failed",
                            "message": run.redactor.clean(errors[0] if errors else "任务未完整执行")}

    async def _one(self, run: _Run, account: dict, row: dict, semaphore, *, prepare_only=False):
        task_id = run.record["task_id"]
        try:
            async with semaphore, run.group_semaphores[account.get("egress_group_id")]:
                try:
                    if run.stop_reason:
                        return
                    if run.spec.kind == "disconnect":
                        await self.sessions.disconnect(account["account_id"])
                        row["state"] = "succeeded"
                    else:
                        if run.spec.kind != "execute" or run.spec.session_policy == "temporary":
                            await self._connect(run, account, row)
                        if not prepare_only:
                            if run.spec.kind == "execute":
                                await self._execute(run, row)
                            else:
                                row["state"] = "succeeded"
                except Exception as exc:
                    self._error(run, row, exc)
                finally:
                    # Bounded capacity includes disconnect/cleanup, not just work.
                    if (not prepare_only and run.spec.kind == "execute"
                            and run.spec.session_policy == "temporary"):
                        try:
                            await self.sessions.disconnect_owned(task_id, account["account_id"])
                        except Exception as exc:
                            self._error(run, row, exc)
        except asyncio.CancelledError:
            if row["state"] not in _TERMINAL:
                row["state"] = "interrupted" if run.stop_reason == "interrupted" else "cancelled"
            raise
        finally:
            self._publish(run)

    async def _gather(self, run: _Run, coroutines) -> None:
        workers = {asyncio.create_task(coroutine) for coroutine in coroutines}
        run.workers = workers
        outcomes = await asyncio.gather(*workers, return_exceptions=True)
        run.workers = set()
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome

    async def _execute_ready(self, run: _Run, row: dict) -> None:
        try:
            if not run.stop_reason:
                await self._execute(run, row)
        except asyncio.CancelledError:
            row["state"] = "interrupted" if run.stop_reason == "interrupted" else "cancelled"
            raise
        except Exception as exc:
            self._error(run, row, exc)
        finally:
            self._publish(run)

    async def _probe_exit(self, run, group):
        try:
            run.egress_profiles[group] = await self.sessions.egress.probe(group)
        except Exception as error:
            run.egress_profiles[group] = {"error": {"code": "egress_probe_failed",
                                                    "message": run.redactor.clean(error)}}

    async def _probe_account(self, run, account, account_row, semaphore):
        rows = [row for row in run.record["server_results"] if row["account_id"] == account["account_id"]]
        account_row["state"] = "running"
        try:
            for row in rows:
                async with semaphore, run.group_semaphores[account["egress_group_id"]]:
                    if run.stop_reason:
                        break
                    row.update(state="running", started_at=_now())
                    self._publish(run)
                    outcome = {}
                    try:
                        profile = run.egress_profiles[account["egress_group_id"]]
                        if profile.get("error"):
                            raise ServiceError(**profile["error"])
                        row["exit_ip"] = profile["exit_ip"]
                        account_row["network_write"] = {"status": "unknown", "count": None}
                        await self.sessions.probe_server(run.record["task_id"], account, row["server"],
                                                         run.spec.probe_timeout or 15, outcome,
                                                         steps=[step.model_dump() for step in run.spec.steps],
                                                         attempts=run.spec.probe_attempts or 1,
                                                         window={"start": run.spec.planned_at.isoformat(),
                                                                 "until": run.spec.probe_until.isoformat()}
                                                         if run.spec.probe_until else None)
                        if outcome.get("blocked_code"):
                            raise ServiceError(outcome["blocked_code"], "登录受到限制，停止本轮测试")
                        statuses = [outcome[name]["status"] for name in ("flash", "h5")]
                        row["state"] = "succeeded" if all(s in {"available", "not_supported", "not_selected"} for s in statuses) else "failed"
                        if any(outcome[name].get("execution", {}).get("success") is False
                               or outcome[name].get("initialization_status") in {"failed", "timeout"}
                               for name in ("flash", "h5")):
                            row["state"] = "failed"
                        if all(s == "not_supported" for s in statuses):
                            row["state"] = "skipped"
                        if row["state"] == "failed":
                            failed = next(outcome[name] for name in ("flash", "h5")
                                          if outcome[name]["status"] not in {"available", "not_supported", "not_selected"}
                                          or outcome[name].get("error"))
                            row["error"] = {"code": failed["status"], "message": failed["error"] or "未完成游戏登录测试"}
                            account_row["error"] = row["error"]
                    except asyncio.CancelledError:
                        row["state"] = "cancelled"
                        raise
                    except Exception as error:
                        row.update(state="failed", error={"code": getattr(error, "code", "probe_failed"),
                                                           "message": run.redactor.clean(error)})
                        self._error(run, account_row, error)
                    finally:
                        for name in ("flash", "h5"):
                            if name in outcome:
                                row[name] = outcome[name]
                        row["finished_at"] = _now()
                        self._log(run.record["task_id"], account["account_id"],
                                  f"服务器 {row['target_index']}：Flash={row['flash']['status']} H5={row['h5']['status']}")
                        self._publish(run)
            if account_row["state"] != "failed":
                account_row["state"] = "succeeded" if all(r["state"] in {"succeeded", "skipped"} for r in rows) else "failed"
        except asyncio.CancelledError:
            account_row["state"] = "cancelled"
            raise
        finally:
            self._publish(run)

    async def _run(self, run: _Run) -> None:
        run.started = True
        try:
            if run.stop_reason:
                raise asyncio.CancelledError
            run.record.update(state="running", started_at=_now())
            self._publish(run)
            semaphore = asyncio.Semaphore(run.spec.concurrency)
            run.group_semaphores = {group: asyncio.Semaphore(limit)
                                    for group, limit in self.sessions.execution_limits(run.accounts).items()}
            all_online = (run.spec.kind == "execute" and run.spec.session_policy == "temporary"
                          and run.spec.mode == "all_online")
            if run.spec.kind == "server_probe":
                await self._gather(run, (self._probe_exit(run, group) for group in run.group_semaphores))
                await self._gather(run, (self._probe_account(run, account, row, semaphore)
                    for account, row in zip(run.accounts, run.record["account_results"])))
                return
            await self._gather(run, (
                self._one(run, account, row, semaphore, prepare_only=all_online)
                for account, row in zip(run.accounts,
                                        (row for row in run.record["account_results"] if row["state"] != "skipped"))
            ))
            if all_online and not run.stop_reason:
                await self._gather(run, (self._execute_ready(run, row)
                                        for row in run.record["account_results"] if row["state"] == "ready"))
        except asyncio.CancelledError:
            if run.stop_reason is None:
                run.stop_reason = "cancelled"
        except Exception as exc:
            run.record["error"] = {"code": getattr(exc, "code", "execution_failed"),
                                    "message": run.redactor.clean(exc)}
        finally:
            run.finalizing = True
            for worker in run.workers:
                if not worker.done():
                    worker.cancel()
            if run.workers:
                await asyncio.gather(*run.workers, return_exceptions=True)
            await self._finish(run)

    async def _finish(self, run: _Run) -> None:
        task_id = run.record["task_id"]
        try:
            await self.sessions.cleanup_task(task_id)
        except Exception as exc:
            run.record.update(state="cancelling", error={"code": "cleanup_failed", "message": run.redactor.clean(exc)})
            self._publish(run)
            return
        self.sessions.release(task_id)
        for row in run.record.get("server_results", []):
            if row["state"] not in _TERMINAL:
                row["state"] = "interrupted" if run.stop_reason == "interrupted" else "cancelled" if run.stop_reason == "cancelled" else "skipped"
            for name in ("flash", "h5"):
                if row[name]["status"] in {"not_tested", "running"}:
                    row[name]["status"] = "interrupted" if run.stop_reason == "interrupted" else "cancelled" if run.stop_reason == "cancelled" else "not_tested"
        for row in run.record["account_results"]:
            if row["state"] not in _TERMINAL:
                row["state"] = "interrupted" if run.stop_reason == "interrupted" else "cancelled"
        if run.stop_reason == "interrupted":
            state = "interrupted"
        elif run.record["error"]:
            state = "failed"
        elif run.stop_reason == "cancelled":
            state = "cancelled"
        else:
            state = "succeeded" if all(row["state"] in {"succeeded", "skipped"}
                                       for row in run.record["account_results"]) else "failed"
        run.record.update(state=state, finished_at=_now())
        self._publish(run)
        # Do not retain submitted message parameters or credential snapshots after completion.
        run.accounts.clear()
        run.steps = ()
        self._runs.pop(task_id, None)

    async def close(self) -> None:
        self._closing = True
        runs = list(self._runs.values())
        for run in runs:
            was_stopping = run.stop_reason is not None
            run.stop_reason = "interrupted"
            run.record["state"] = "cancelling"
            self._publish(run)
            if run.task is not None and run.task.done():
                run.task = asyncio.create_task(self._finish(run))
            elif run.started and not run.finalizing and not was_stopping and run.task is not None:
                run.task.cancel()
        await asyncio.gather(*(run.task for run in runs if run.task is not None), return_exceptions=True)
