"""C# desktop bridge that keeps the existing Python game backend intact."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
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


logger = logging.getLogger(__name__)


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
            self._ensure_labels_idle(labels)
            self._start_task(
                "connection",
                labels,
                self._run_connection_action(action, labels),
            )
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
        async def run_one(label: str) -> None:
            connection = self._connection(label)
            try:
                if action == "disconnect":
                    await connection.disconnect()
                    await self._log(f"账号 {label} 已断开", "connection")
                    return
                if action == "reconnect":
                    await connection.disconnect()
                success = await connection.connect()
                if success and connection.context is not None:
                    connection.context.log_callback = (
                        lambda message, account=label: self._schedule_log(
                            f"账号 {account}：{message}", "script"
                        )
                    )
                    await self._log(f"账号 {label} 已连接", "connection")
                else:
                    await self._log(
                        f"账号 {label} 连接失败：{connection.last_error}", "error"
                    )
            except Exception as exc:
                await self._log(f"账号 {label} 连接异常：{exc}", "error")
            finally:
                # Do not wait for every selected account (including slow
                # retries) before reflecting this account's completed state.
                await self._emit_accounts(force=True)

        await asyncio.gather(*(run_one(label) for label in labels))

    async def _run_send(self, labels: list[str]) -> None:
        targets = self._targets(labels)
        batches = await dispatch_to_accounts(targets, self.messages)
        for label, results in batches.items():
            for result in results:
                action = "执行序列指令" if result.ext_id == -1 else "发送消息"
                if result.success:
                    await self._log(
                        f"{action}至账号 {label}：{result.cmd}", "send"
                    )
                else:
                    await self._log(
                        f"{action}至账号 {label} 失败：{result.cmd}，{result.error}",
                        "error",
                    )
        append_send_results(batches)

    async def _run_script(
        self, labels: list[str], script: InteractionScript
    ) -> None:
        async def run_one(label: str) -> None:
            context = self._connection(label).context
            assert context is not None
            try:
                await script.run(context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._log(
                    f"账号 {label} 脚本 {script.name} 失败：{exc}", "error"
                )

        await asyncio.gather(*(run_one(label) for label in labels))

    async def _run_combination(
        self,
        labels: list[str],
        steps: tuple[MessageBatchStep | ScriptStep, ...],
        repetitions: int,
        interval: float,
    ) -> None:
        results = await execute_combination_for_accounts(
            self._targets(labels), steps, repetitions, interval
        )
        batches = {}
        for label, result in results.items():
            batches[label] = list(result.send_results)
            for send_result in result.send_results:
                action = "执行序列指令" if send_result.ext_id == -1 else "发送消息"
                if send_result.success:
                    await self._log(
                        f"{action}至账号 {label}：{send_result.cmd}", "send"
                    )
                else:
                    await self._log(
                        f"{action}至账号 {label} 失败：{send_result.error}",
                        "error",
                    )
            if not result.success:
                failure = next(
                    (step for step in result.step_results if not step.success), None
                )
                await self._log(
                    f"账号 {label} 组合任务失败："
                    f"{failure.error if failure else '未完整执行'}",
                    "error",
                )
        if any(batches.values()):
            append_send_results(batches)

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
        statuses = tuple(
            (item["label"], item["status"]) for item in self._accounts_payload()
        )
        if force or statuses != self._last_statuses:
            self._last_statuses = statuses
            await self._emit("accounts", accounts=self._accounts_payload())

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
