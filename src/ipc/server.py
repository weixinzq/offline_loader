"""C# desktop bridge that keeps the existing Python game backend intact."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Awaitable

from src.accounts.manager import (
    AccountConfigError,
    AccountConnection,
    AccountManager,
    AccountSpec,
    load_account_specs,
)
from src.config import CONFIG_PATH, default_config, load_user_config
from src.accounts.session import open_context
from src.messaging.audit_log import append_send_results
from src.messaging.dispatcher import dispatch_to_accounts
from src.messaging.parser import (
    SendMessage,
    parse_pipe_separated_messages,
    parse_send_message_file,
)
from src.scripting.composer import MessageBatchStep, ScriptStep, execute_combination_for_accounts
from src.scripting.loader import InteractionScript, discover_scripts
from src.ipc.named_pipe import NamedPipeTransport
from src.ui.runtime_log import get_runtime_log_path


logger = logging.getLogger(__name__)
LOGIN_CONCURRENCY = 5
LOGIN_START_INTERVAL = 0.5


class BridgeError(ValueError):
    pass


class DesktopBridge:
    def __init__(self, pipe: NamedPipeTransport):
        self.pipe = pipe
        self.startup_error = ""
        self.config_write_blocked = False
        try:
            self.config = load_user_config()
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.config = default_config()
            self.startup_error = f"无法读取 config.json：{exc}"
            self.config_write_blocked = True
        try:
            specs = load_account_specs(self.config)
        except AccountConfigError as exc:
            specs = []
            if not self.startup_error:
                self.startup_error = str(exc)
            if not self.config:
                self.config = default_config()
        self.manager = AccountManager.from_specs(specs)
        self.scripts = discover_scripts()
        self.messages: tuple[SendMessage, ...] = ()
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.task_labels: dict[str, set[str]] = {}
        self._last_statuses: tuple[tuple[str, str], ...] = ()
        self._stopping = False
        self._connection_action = ""

    async def run(self) -> None:
        monitor = asyncio.create_task(self._monitor_accounts())
        try:
            await self._emit_accounts(force=True)
            await self._emit(
                "scripts",
                scripts=[
                    {
                        "module": script.module_name,
                        "name": script.name,
                        "description": script.description,
                    }
                    for script in self.scripts
                ],
            )
            if self.startup_error:
                await self._log(
                    f"账号配置尚未完成：{self.startup_error}", "error"
                )
            while not self._stopping:
                request = await asyncio.to_thread(self.pipe.read_json)
                if request is None:
                    break
                await self._handle_request(request)
        finally:
            monitor.cancel()
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            await self.manager.disconnect_all()

    async def _handle_request(self, request: dict) -> None:
        request_id = request.get("id")
        action = request.get("action")
        payload = request.get("payload") or {}
        if not isinstance(request_id, str) or not isinstance(action, str):
            return
        try:
            data = await self._dispatch(action, payload)
        except Exception as exc:
            logger.exception("bridge request failed: %s", action)
            await self._respond(request_id, False, error=str(exc))
        else:
            await self._respond(request_id, True, data=data)

    async def _dispatch(self, action: str, payload: dict) -> Any:
        if action == "get_state":
            return self._state_payload()
        if action == "get_accounts":
            connection_task = self.tasks.get("connection")
            return {
                "accounts": self._accounts_payload(),
                "connection_busy": bool(
                    connection_task is not None and not connection_task.done()
                ),
            }
        if action == "get_account":
            connection = self._connection(str(payload.get("label", "")))
            config = connection.spec.config
            return {
                "label": connection.spec.label,
                "account": str(config.get("account", "")),
                "char_id": int(config.get("char_id", 0)),
                "zone_index": int(config.get("zone_index", 1025)),
            }
        if action in {"add_account", "edit_account", "delete_account"}:
            return await self._mutate_account(action, payload)
        if action in {"connect", "reconnect", "disconnect"}:
            labels = self._labels(payload)
            current = self.tasks.get("connection")
            if (action == "disconnect" and current is not None and not current.done()
                    and self._connection_action != "disconnect"):
                current.cancel()
                await asyncio.gather(current, return_exceptions=True)
                if self.tasks.get("connection") is current:
                    self.tasks.pop("connection", None)
                    self.task_labels.pop("connection", None)
            self._ensure_labels_idle(labels)
            self._start_task(
                "connection",
                labels,
                self._run_connection_action(action, labels),
            )
            self._connection_action = action
            return {"started": True}
        if action == "parse_message":
            self.messages = tuple(
                parse_pipe_separated_messages(str(payload.get("text", "")), "<粘贴文本>")
            )
            await self._emit_messages()
            return {"count": len(self.messages)}
        if action == "parse_message_file":
            self.messages = tuple(
                parse_send_message_file(Path(str(payload.get("path", ""))))
            )
            await self._emit_messages()
            return {"count": len(self.messages)}
        if action == "clear_messages":
            self.messages = ()
            await self._emit_messages()
            return {"count": 0}
        if action == "send_messages":
            if not self.messages:
                raise BridgeError("请先解析消息或消息文件")
            labels = self._online_labels(payload)
            self._ensure_labels_idle(labels)
            self._start_task("send", labels, self._run_send(labels))
            return {"started": True}
        if action == "run_script":
            labels = self._online_labels(payload)
            self._ensure_labels_idle(labels)
            script = self._script(str(payload.get("module", "")))
            self._start_task("script", labels, self._run_script(labels, script))
            return {"started": True}
        if action == "run_combination":
            labels = self._online_labels(payload)
            self._ensure_labels_idle(labels)
            steps = self._combination_steps(payload.get("steps"))
            repetitions = int(payload.get("repetitions", 1))
            interval = float(payload.get("interval", 0.5))
            self._start_task(
                "combination",
                labels,
                self._run_combination(labels, steps, repetitions, interval),
            )
            return {"started": True}
        if action == "cancel_task":
            kind = str(payload.get("kind", ""))
            task = self.tasks.get(kind)
            if task is not None:
                task.cancel()
            return {"cancelled": task is not None}
        if action == "shutdown":
            self._stopping = True
            return {"stopping": True}
        raise BridgeError(f"不支持的操作：{action}")

    def _state_payload(self) -> dict:
        return {
            "accounts": self._accounts_payload(),
            "scripts": [
                {
                    "module": script.module_name,
                    "name": script.name,
                    "description": script.description,
                }
                for script in self.scripts
            ],
            "messages": self._messages_payload(),
        }

    def _accounts_payload(self) -> list[dict]:
        return [
            {
                "label": connection.spec.label,
                "status": "在线" if connection.online else "离线",
            }
            for connection in self.manager.connections
        ]

    def _messages_payload(self) -> list[dict]:
        return [
            {
                "sequence": index,
                "id": message.id,
                "cmd": message.cmd,
                "param": message.param,
            }
            for index, message in enumerate(self.messages, start=1)
        ]

    async def _mutate_account(self, action: str, payload: dict) -> dict:
        if self.config_write_blocked:
            raise BridgeError("config.json 无法读取，请修复配置文件后重新启动程序")
        old_label = str(payload.get("old_label", ""))
        if action != "add_account":
            current = self._connection(old_label)
            if current.online:
                raise BridgeError("请先断开该账号再修改或删除")
            if current.context is not None:
                await current.disconnect()
        specs = list(self.manager.connections)
        if action == "delete_account":
            specs = [item for item in specs if item.spec.label != old_label]
            if not specs:
                raise BridgeError("至少保留一个账号")
        else:
            label = str(payload.get("label", "")).strip()
            account = str(payload.get("account", "")).strip()
            password = str(payload.get("password", ""))
            char_id = int(payload.get("char_id", 0))
            zone_index = int(payload.get("zone_index", 1025))
            if not label or not account:
                raise BridgeError("账号标签和登录账号不能为空")
            if char_id < 0:
                raise BridgeError("charId 必须是非负整数")
            if any(
                item.spec.label == label and item.spec.label != old_label
                for item in specs
            ):
                raise BridgeError(f"账号标签重复：{label}")
            if action == "add_account":
                if not password:
                    raise BridgeError("新增账号必须填写密码")
                new_config = {
                    **self._shared_config(),
                    "label": label,
                    "account": account,
                    "password": password,
                    "char_id": char_id,
                    "zone_index": zone_index,
                }
                specs.append(AccountConnection(AccountSpec(label, new_config)))
            else:
                current = self._connection(old_label)
                new_config = dict(current.spec.config)
                new_config.update(
                    label=label,
                    account=account,
                    char_id=char_id,
                    zone_index=zone_index,
                )
                if password:
                    new_config["password"] = password
                index = specs.index(current)
                specs[index] = AccountConnection(AccountSpec(label, new_config))

        self._save_connections(specs)
        self.config = load_user_config()
        self.manager = AccountManager.from_specs(load_account_specs(self.config))
        await self._emit_accounts(force=True)
        return {"saved": True}

    def _shared_config(self) -> dict:
        return {
            key: value
            for key, value in self.config.items()
            if key not in {
                "accounts", "account", "password", "label", "char_id", "zone_index"
            }
        }

    def _save_connections(self, connections: list[AccountConnection]) -> None:
        root = self._shared_config()
        accounts = []
        for item in connections:
            entry = {
                "label": item.spec.label,
                "account": item.spec.config["account"],
                "password": item.spec.config["password"],
                "char_id": int(item.spec.config.get("char_id", 0)),
                "zone_index": int(item.spec.config.get("zone_index", 1025)),
            }
            for key, value in item.spec.config.items():
                if key in entry or key == "accounts":
                    continue
                if key not in root or root[key] != value:
                    entry[key] = value
            accounts.append(entry)
        root["accounts"] = accounts
        load_account_specs(root)
        temporary = CONFIG_PATH.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(root, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(CONFIG_PATH)

    async def _run_connection_action(self, action: str, labels: list[str]) -> None:
        completed = 0
        skipped = 0
        errors: list[str] = []
        start_lock = asyncio.Lock()
        next_start = 0.0
        pause_reason = ""
        verification_lock = asyncio.Lock()
        verification_ready = asyncio.Event()
        verification_ready.set()
        verification_waiters = 0

        def pause(reason: str) -> None:
            nonlocal pause_reason
            if not pause_reason:
                pause_reason = reason
                for task in workers:
                    if task is not asyncio.current_task() and not task.done():
                        task.cancel()

        async def verify_manually(connection) -> bool:
            nonlocal verification_waiters
            verification_waiters += 1
            verification_ready.clear()
            try:
                async with verification_lock:
                    if pause_reason:
                        return False
                    label = connection.spec.label
                    await self._log(
                        f"账号 {label} 需要人工验证：登录队列等待中，请在打开的 Edge 窗口完成官方验证；"
                        "完成后自动继续，关闭窗口或点击断开全部可停止。",
                        "connection",
                    )

                    async def opener(config):
                        connection.state = "等待人工验证"
                        return await open_context(config, interactive=True)

                    success = await connection.connect(opener=opener, retries=1)
                    if success:
                        await self._log(f"账号 {label} 人工验证后的连接已建立，继续初始化及后续登录。", "connection")
                    else:
                        pause("人工验证或后续登录未完成")
                    return success
            finally:
                verification_waiters -= 1
                if verification_waiters == 0:
                    verification_ready.set()

        async def run_one(label: str) -> None:
            nonlocal completed, skipped, next_start, pause_reason
            connection = self._connection(label)
            try:
                if action == "disconnect":
                    if connection.context is None:
                        skipped += 1
                        return
                    await connection.disconnect()
                    completed += 1
                    return
                if action == "connect" and connection.online:
                    skipped += 1
                    return
                async with start_lock:
                    await verification_ready.wait()
                    loop = asyncio.get_running_loop()
                    while next_start > loop.time():
                        await asyncio.sleep(max(0.001, next_start - loop.time()))
                    await verification_ready.wait()
                    if pause_reason:
                        return
                    next_start = loop.time() + LOGIN_START_INTERVAL
                if action == "reconnect":
                    await connection.disconnect()
                success = await connection.connect()
                if not success and getattr(connection, "login_block_reason", "") == "登录需要人机验证":
                    success = await verify_manually(connection)
                if success and connection.context is not None:
                    connection.context.log_callback = (
                        lambda message, account=label: self._schedule_log(
                            f"账号 {account}：{message}", "script"
                        )
                    )
                    await connection.context.wait_for_player_initialization()
                    completed += 1
                else:
                    message = f"账号 {label} 连接失败：{connection.last_error}"
                    errors.append(message)
                    logger.warning(message)
                    blocked = getattr(connection, "login_block_reason", "")
                    if blocked:
                        pause(blocked)
            except asyncio.CancelledError:
                if action != "disconnect" and connection.context is not None:
                    await connection.disconnect()
                    connection.state = "已取消"
                raise
            except Exception as exc:
                if action != "disconnect" and connection.context is not None:
                    await connection.disconnect()
                connection.last_error = str(exc)
                message = f"账号 {label} 连接异常：{exc}"
                errors.append(message)
                logger.warning(message)

        pending = iter(labels)

        async def worker() -> None:
            for label in pending:
                await verification_ready.wait()
                if pause_reason:
                    return
                await run_one(label)

        async def report_progress() -> None:
            previous = None
            while True:
                await asyncio.sleep(5)
                progress = (completed, len(errors), skipped)
                if progress != previous:
                    previous = progress
                    await self._log(
                        f"登录进度：就绪 {completed}，失败 {len(errors)}，"
                        f"未完成 {len(labels) - completed - len(errors) - skipped}",
                        "connection",
                    )

        if action == "disconnect":
            workers = [asyncio.create_task(run_one(label)) for label in labels]
        else:
            workers = [asyncio.create_task(worker())
                       for _ in range(min(LOGIN_CONCURRENCY, len(labels)))]
        progress_task = asyncio.create_task(report_progress()) if action != "disconnect" else None
        try:
            # The one-second monitor publishes progress while the batch runs.
            results = await asyncio.gather(*workers, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    raise result
        finally:
            for task in workers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if progress_task is not None:
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            await self._emit_accounts()
        name = {"connect": "连接", "reconnect": "重连", "disconnect": "断开"}[action]
        if pause_reason:
            await self._log(
                f"登录队列已暂停：{pause_reason}。本轮就绪 {completed}，失败 {len(errors)}，"
                f"未完成 {len(labels) - completed - len(errors) - skipped}；已就绪连接保留。",
                "error",
            )
        else:
            await self._log(
                f"{name}完成：成功 {completed}，失败 {len(errors)}，跳过 {skipped}",
                "error" if errors else "connection",
            )
        for message in errors[:5]:
            await self._log(message, "error")
        if len(errors) > 5:
            await self._log("其余失败详情请查看运行日志。", "error")

    async def _run_send(self, labels: list[str]) -> None:
        targets = self._targets(labels)
        started = asyncio.get_running_loop().time()
        batches = await dispatch_to_accounts(targets, self.messages)
        elapsed = asyncio.get_running_loop().time() - started
        log_path = append_send_results(batches)
        results = [result for batch in batches.values() for result in batch]
        failures = [result for result in results if not result.success]
        await self._log(
            f"发送完成：账号 {len(batches)}，成功 {len(results) - len(failures)} 条，"
            f"失败 {len(failures)} 条；发送耗时 {elapsed:.2f} 秒。详细记录：{log_path}",
            "error" if failures else "send",
        )
        for result in failures[:5]:
            await self._log(
                f"账号 {result.account_label} 发送 {result.cmd} 失败：{result.error}",
                "error",
            )
        if len(failures) > 5:
            await self._log("其余发送失败详情请查看上述记录文件。", "error")

    @contextmanager
    def _task_logs_to_file(self, targets):
        callbacks = [(context, context.log_callback) for _label, context in targets]
        try:
            for label, context in targets:
                context.log_callback = (
                    lambda message, account=label: logger.info("账号 %s：%s", account, message)
                )
            yield
        finally:
            for context, callback in callbacks:
                context.log_callback = callback

    async def _run_script(
        self, labels: list[str], script: InteractionScript
    ) -> None:
        targets = self._targets(labels)
        started = asyncio.get_running_loop().time()
        errors = []

        async def run_one(label: str) -> None:
            context = self._connection(label).context
            assert context is not None
            try:
                await script.run(context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = f"账号 {label} 脚本 {script.name} 失败：{exc}"
                errors.append(message)
                logger.warning(message)
            else:
                logger.info("账号 %s 脚本 %s 执行完成", label, script.name)

        with self._task_logs_to_file(targets):
            await asyncio.gather(*(run_one(label) for label, _context in targets))
        elapsed = asyncio.get_running_loop().time() - started
        await self._log(
            f"专用任务完成：{script.name}；账号 {len(targets)}，成功 {len(targets) - len(errors)}，"
            f"失败 {len(errors)}；执行耗时 {elapsed:.2f} 秒。详细记录：{get_runtime_log_path()}",
            "error" if errors else "script",
        )
        for message in errors[:5]:
            await self._log(message, "error")
        if len(errors) > 5:
            await self._log("其余脚本失败详情请查看上述记录文件。", "error")

    async def _run_combination(
        self,
        labels: list[str],
        steps: tuple[MessageBatchStep | ScriptStep, ...],
        repetitions: int,
        interval: float,
    ) -> None:
        targets = self._targets(labels)
        started = asyncio.get_running_loop().time()
        with self._task_logs_to_file(targets):
            results = await execute_combination_for_accounts(
                targets, steps, repetitions, interval
            )
        elapsed = asyncio.get_running_loop().time() - started
        batches = {
            label: [item for item in result.send_results if item.ext_id >= 0]
            for label, result in results.items()
        }
        log_path = append_send_results(batches) if any(batches.values()) else None
        errors = []
        for label, result in results.items():
            logger.info("账号 %s 组合任务完成轮数 %s/%s", label,
                        result.completed_repetitions, result.requested_repetitions)
            for step in result.step_results:
                logger.info("账号 %s 第 %s 轮步骤 %s：%s；成功 %s；%s", label,
                            step.repetition, step.step, step.description, step.success, step.error)
            if not result.success:
                failure = next(
                    (step for step in result.step_results if not step.success), None
                )
                message = f"账号 {label} 组合任务失败：{failure.error if failure else '未完整执行'}"
                errors.append(message)
                logger.warning(message)
        send_results = [item for batch in batches.values() for item in batch]
        send_failures = sum(not item.success for item in send_results)
        records = f"发送明细：{log_path}。" if log_path is not None else ""
        await self._log(
            f"组合任务完成：账号 {len(results)}，成功 {len(results) - len(errors)}，失败 {len(errors)}；"
            f"消息成功 {len(send_results) - send_failures} 条，失败 {send_failures} 条；"
            f"执行耗时 {elapsed:.2f} 秒。{records}步骤与脚本详情：{get_runtime_log_path()}",
            "error" if errors or send_failures else "send",
        )
        for message in errors[:5]:
            await self._log(message, "error")
        if len(errors) > 5:
            await self._log("其余组合任务失败详情请查看上述记录文件。", "error")

    def _start_task(
        self, kind: str, labels: list[str], awaitable: Awaitable[Any]
    ) -> None:
        current = self.tasks.get(kind)
        if current is not None and not current.done():
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            raise BridgeError("已有同类任务正在执行")
        task = asyncio.create_task(awaitable, name=f"bridge-{kind}")
        self.tasks[kind] = task
        self.task_labels[kind] = set(labels)
        asyncio.create_task(self._watch_task(kind, task))

    async def _watch_task(self, kind: str, task: asyncio.Task[Any]) -> None:
        try:
            await task
        except asyncio.CancelledError:
            await self._log(f"{self._task_name(kind)}已取消", "error")
            status, error = "cancelled", ""
        except Exception as exc:
            logger.exception("bridge task failed: %s", kind)
            await self._log(f"{self._task_name(kind)}异常：{exc}", "error")
            status, error = "failed", str(exc)
        else:
            status, error = "completed", ""
        finally:
            if self.tasks.get(kind) is task:
                self.tasks.pop(kind, None)
                self.task_labels.pop(kind, None)
        await self._emit("task", kind=kind, status=status, error=error)

    @staticmethod
    def _task_name(kind: str) -> str:
        return {
            "connection": "连接操作",
            "send": "发送任务",
            "script": "脚本任务",
            "combination": "组合任务",
        }.get(kind, kind)

    def _ensure_labels_idle(self, labels: list[str]) -> None:
        selected = set(labels)
        for kind, busy in self.task_labels.items():
            if kind != "connection" and selected & busy:
                names = "、".join(sorted(selected & busy))
                raise BridgeError(f"账号正在执行其他任务：{names}")

    def _labels(self, payload: dict) -> list[str]:
        raw = payload.get("labels")
        if not isinstance(raw, list) or not raw:
            raise BridgeError("请至少选择一个账号")
        labels = [str(label) for label in raw]
        for label in labels:
            self._connection(label)
        return labels

    def _online_labels(self, payload: dict) -> list[str]:
        labels = self._labels(payload)
        offline = [label for label in labels if not self._connection(label).online]
        if offline:
            raise BridgeError(f"以下账号不在线：{'、'.join(offline)}")
        return labels

    def _targets(self, labels: list[str]):
        return [
            (label, self._connection(label).context)
            for label in labels
            if self._connection(label).context is not None
        ]

    def _connection(self, label: str) -> AccountConnection:
        for connection in self.manager.connections:
            if connection.spec.label == label:
                return connection
        raise BridgeError(f"找不到账号：{label}")

    def _script(self, module: str) -> InteractionScript:
        for script in self.scripts:
            if script.module_name == module:
                return script
        raise BridgeError(f"找不到脚本：{module}")

    def _combination_steps(self, raw: Any):
        if not isinstance(raw, list) or not raw:
            raise BridgeError("组合任务至少需要一个步骤")
        steps = []
        for item in raw:
            if not isinstance(item, dict):
                raise BridgeError("组合步骤格式无效")
            kind = item.get("kind")
            if kind == "messages":
                messages = item.get("messages")
                if not isinstance(messages, list) or not messages:
                    raise BridgeError("通用消息步骤不能为空")
                steps.append(
                    MessageBatchStep(
                        tuple(
                            SendMessage(
                                int(message["id"]),
                                str(message["cmd"]),
                                dict(message["param"]),
                            )
                            for message in messages
                        )
                    )
                )
            elif kind == "script":
                steps.append(ScriptStep(self._script(str(item.get("module", "")))))
            else:
                raise BridgeError(f"不支持的组合步骤：{kind}")
        return tuple(steps)

    async def _monitor_accounts(self) -> None:
        while True:
            await asyncio.sleep(1)
            for connection in self.manager.connections:
                was_online = connection.state == "在线"
                connection.refresh_state()
                if was_online and not connection.online and connection.last_error:
                    await self._log(
                        f"账号 {connection.spec.label} 已离线：{connection.last_error}",
                        "error",
                    )
            await self._emit_accounts()

    async def _emit_accounts(self, force: bool = False) -> None:
        accounts = self._accounts_payload()
        statuses = tuple(
            (item["label"], item["status"]) for item in accounts
        )
        if force or statuses != self._last_statuses:
            self._last_statuses = statuses
            await self._emit("accounts", accounts=accounts)

    async def _emit_messages(self) -> None:
        await self._emit("messages", messages=self._messages_payload())

    def _schedule_log(self, message: str, category: str) -> None:
        asyncio.get_running_loop().create_task(self._log(message, category))

    async def _log(self, message: str, category: str) -> None:
        await self._emit("log", message=message, category=category)

    async def _emit(self, event: str, **payload: Any) -> None:
        await asyncio.to_thread(
            self.pipe.write_json, {"type": "event", "event": event, **payload}
        )

    async def _respond(
        self, request_id: str, ok: bool, data: Any = None, error: str = ""
    ) -> None:
        message = {"type": "response", "id": request_id, "ok": ok}
        if ok:
            message["data"] = data
        else:
            message["error"] = error
        await asyncio.to_thread(self.pipe.write_json, message)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipe", required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    from src.ui.runtime_log import configure_runtime_logging

    configure_runtime_logging(capture_console=True)
    with NamedPipeTransport(args.pipe) as pipe:
        asyncio.run(DesktopBridge(pipe).run())


if __name__ == "__main__":
    main()
