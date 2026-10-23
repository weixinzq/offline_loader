"""Private pipe worker. It owns game contexts, never the service database."""
import argparse
import asyncio
from contextvars import ContextVar
import ipaddress
import json
import logging
import os
from pathlib import Path
import sys

import aiohttp
from pydantic import TypeAdapter

from src.accounts.manager import AccountConnection, AccountSpec
from src.service.contracts import StepSpec, ServiceError
from src.service.execution import MessageService, ScriptRegistry
from src.service.execution_runner import run_execution
from src.service.runtime import Redactor
from src.service.worker_process import actual_executable, bind_linux_parent


class WorkerHost:
    def __init__(self, connection_factory=AccountConnection):
        self.factory = connection_factory
        self.connections = {}
        self.commands = {}
        self.registry = ScriptRegistry(MessageService())
        self.redactor = Redactor(None)
        self.account_scope = ContextVar("worker_account", default=None)
        self.output = sys.stdout

    def write(self, message):
        self.output.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.output.flush()

    def log(self, message, account_id=None):
        self.write({"event": "log", "account_id": account_id or self.account_scope.get(),
                    "message": self.redactor.clean(message)})

    async def operate(self, command, outcome):
        operation, account_id, data = command["op"], command.get("account_id"), command.get("data", {})
        if operation == "hello":
            return {"pid": os.getpid(), "executable": str(actual_executable())}
        if operation == "probe":
            try:
                async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=12)) as client:
                    async with client.get("https://api.ipify.org?format=json") as response:
                        response.raise_for_status()
                        result = await response.json()
                        return {"exit_ip": str(ipaddress.ip_address(result["ip"]))}
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as error:
                raise ServiceError("egress_probe_failed", "出口检测失败，请检查 Clash 规则、节点连接和公网 IP 服务可用性") from error
        if operation == "cancel":
            target = self.commands.get(data["request_id"])
            if target is not None and target is not asyncio.current_task():
                target.cancel()
                await asyncio.gather(target, return_exceptions=True)
            return {}
        if operation == "shutdown":
            current = asyncio.current_task()
            pending = [task for task in self.commands.values() if task is not current]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(*(connection.disconnect() for connection in self.connections.values()))
            return {}
        if operation == "connect":
            if account_id in self.connections:
                raise ServiceError("worker_account_busy", "账号已经存在于执行进程", 409)
            config = data["config"]
            self.redactor.collect(config)
            connection = self.factory(AccountSpec(data["label"], config))
            self.connections[account_id] = connection
            connected = await connection.connect(retries=1)
            context = connection.context
            if context is not None:
                login = getattr(context, "login_result", None)
                self.redactor.add(getattr(login, "session_id", ""))
                self.redactor.add(getattr(login, "user_id", ""))
                context.log_callback = lambda text: self.log(text, account_id)
                messages = getattr(context, "messages", None)
                if hasattr(messages, "add_disconnect_observer"):
                    messages.add_disconnect_observer(lambda reason: self.write({
                        "event": "disconnected", "account_id": account_id, "reason": self.redactor.clean(reason)}))
            return {"connected": connected, "state": connection.state,
                    "last_error": self.redactor.clean(connection.last_error),
                    "login_block_reason": connection.login_block_reason}
        if operation == "disconnect":
            connection = self.connections.get(account_id)
            if connection is not None:
                await connection.disconnect()
            self.connections.pop(account_id, None)
            return {}
        connection = self.connections.get(account_id)
        if connection is None:
            raise ServiceError("worker_session_missing", "执行进程中的账号会话不存在", 409)
        context = connection.context
        if context is None:
            raise ServiceError("worker_session_missing", "账号尚未连接", 409)
        if operation == "initialize":
            await context.wait_for_player_initialization()
            if not connection.online:
                raise ServiceError("connection_failed", "初始化期间连接已断开")
            return {}
        if operation == "execute":
            self.redactor.collect(data["steps"])
            steps = self.registry.build_steps(TypeAdapter(list[StepSpec]).validate_python(data["steps"]))
            await run_execution(account_id, context, steps, outcome, **data["options"])
            for step in outcome.get("step_results", []):
                if step["error"]:
                    step["error"] = self.redactor.clean(step["error"])
            return outcome
        raise ServiceError("worker_operation", "未知执行操作")

    async def handle(self, command):
        request_id = command["id"]
        token = self.account_scope.set(command.get("account_id"))
        outcome = {"network_write": {"status": "unknown", "count": None}}
        try:
            result = await self.operate(command, outcome)
            self.write({"id": request_id, "ok": True, "data": result})
        except asyncio.CancelledError:
            self.write({"id": request_id, "ok": False, "cancelled": True, "data": outcome})
        except Exception as error:
            self.write({"id": request_id, "ok": False, "data": outcome,
                        "error": {"code": getattr(error, "code", "worker_operation_failed"),
                                  "message": self.redactor.clean(error)}})
        finally:
            self.account_scope.reset(token)
            self.commands.pop(request_id, None)

    async def run(self):
        host = self

        class SafeOutput:
            def write(self, text):
                if text.strip():
                    host.log(text.rstrip())
                return len(text)

            def flush(self):
                pass

        sys.stdout = sys.stderr = SafeOutput()
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)
        try:
            while line := await asyncio.to_thread(sys.stdin.buffer.readline):
                command = json.loads(line)
                task = asyncio.create_task(self.handle(command))
                self.commands[command["id"]] = task
        finally:
            for task in list(self.commands.values()):
                task.cancel()
            await asyncio.gather(*list(self.commands.values()), return_exceptions=True)
            await asyncio.gather(*(connection.disconnect() for connection in self.connections.values()), return_exceptions=True)


def main(connection_factory=AccountConnection):
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    arguments = parser.parse_args()
    bind_linux_parent(arguments.parent_pid)
    asyncio.run(WorkerHost(connection_factory).run())


if __name__ == "__main__":
    main()
