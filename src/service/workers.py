"""Parent-side pipe RPC and connection adapters for isolated outbound groups."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from src.accounts.manager import AccountConnection
from src.service.contracts import ServiceError
from src.service.events import utc_now
from src.service.worker_process import WorkerJob, prepare_worker_executable, worker_environment, worker_executable


class Worker:
    def __init__(self, pool, group_id):
        self.pool, self.group_id = pool, group_id
        self.process = None
        self.state, self.error = "offline", None
        self.exit_ip = self.exit_checked_at = None
        self.pending = {}
        self.connections = {}
        self.start_lock, self.write_lock, self.stop_lock = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()
        self.reader = self.stderr = None
        self.closing = False

    def profile(self):
        return {"worker_state": self.state, "worker_pid": self.process.pid if self.process and self.process.returncode is None else None,
                "worker_path": str(worker_executable(self.pool.directory, self.group_id).resolve()),
                "worker_error": self.error, "exit_ip": self.exit_ip, "exit_checked_at": self.exit_checked_at}

    def publish(self):
        self.pool.events.publish("egress", {"group_id": self.group_id, **self.profile()})

    async def start(self):
        async with self.start_lock:
            if self.closing:
                raise ServiceError("worker_stopping", "出口执行进程正在关闭", 503)
            if self.state == "ready" and self.process and self.process.returncode is None:
                return
            executable = prepare_worker_executable(self.pool.directory, self.group_id)
            self.state, self.error = "starting", None
            self.publish()
            try:
                if self.pool.job is None:
                    self.pool.job = WorkerJob()
                self.process = await asyncio.create_subprocess_exec(
                    str(executable), "-u", "-B", "-m", self.pool.module, "--parent-pid", str(os.getpid()),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env=worker_environment(), limit=16 * 1024 * 1024,
                    **({"creationflags": 0x08000000} if os.name == "nt" else {}))
                self.pool.job.assign(self.process.pid)
                self.reader = asyncio.create_task(self.read())
                self.stderr = asyncio.create_task(self.read_stderr())
                hello = await asyncio.wait_for(self._request("hello"), 15)
                if Path(hello["executable"]).resolve() != executable or hello["pid"] != self.process.pid:
                    raise ServiceError("worker_identity", "执行进程真实路径不匹配，已停止该组")
                self.state = "ready"
                self.publish()
            except BaseException:
                await self.lost("执行进程启动失败，请检查运行环境")
                raise

    async def read(self):
        reason = "出口执行进程已断开"
        try:
            while line := await self.process.stdout.readline():
                response = json.loads(line)
                if "id" in response:
                    future = self.pending.get(response["id"])
                    if future is not None and not future.done():
                        future.set_result(response)
                elif response.get("event") == "log":
                    connection = self.connections.get(response.get("account_id"))
                    if connection and connection.context:
                        connection.context.log(response.get("message", ""))
                elif response.get("event") == "disconnected":
                    connection = self.connections.get(response.get("account_id"))
                    if connection:
                        connection.lost(response.get("reason", "连接断开"))
        except Exception:
            reason = "出口执行进程通信失败"
        finally:
            await self.lost(reason)

    async def read_stderr(self):
        while line := await self.process.stderr.readline():
            self.error = self.pool.store.redact(line.decode("utf-8", errors="replace").strip())[:500]

    async def lost(self, reason):
        async with self.stop_lock:
            process = self.process
            if process and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            self.state = "offline" if self.closing else "failed"
            self.error = None if self.closing else reason
            for connection in list(self.connections.values()):
                connection.lost(reason)
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ServiceError("worker_lost", reason))
            self.publish()

    async def _request(self, operation, account_id=None, data=None, outcome=None):
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        message = json.dumps({"id": request_id, "op": operation, "account_id": account_id, "data": data or {}},
                             ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        try:
            sent = False
            try:
                async with self.write_lock:
                    self.process.stdin.write(message)
                    sent = True
                    await self.process.stdin.drain()
                response = await asyncio.shield(future)
            except asyncio.CancelledError:
                if not sent or operation in {"cancel", "shutdown", "hello"}:
                    raise
                # Do not release the parent account claim until the child stops its operation.
                cleanup = asyncio.create_task(self._cancel(request_id, future))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                response = cleanup.result()
                if outcome is not None:
                    outcome.update(response.get("data", {}))
                raise
            if outcome is not None:
                outcome.update(response.get("data", {}))
            if not response["ok"]:
                error = response.get("error", {})
                raise ServiceError(error.get("code", "worker_cancelled"), error.get("message", "执行操作已取消"))
            return response.get("data", {})
        except (BrokenPipeError, ConnectionError) as error:
            await self.lost("出口执行进程连接丢失")
            raise ServiceError("worker_lost", "出口执行进程连接丢失") from error
        finally:
            self.pending.pop(request_id, None)
            if future.done() and not future.cancelled():
                future.exception()
            elif not future.done():
                future.cancel()

    async def _cancel(self, request_id, future):
        try:
            await asyncio.wait_for(self._request("cancel", data={"request_id": request_id}), 15)
            return await asyncio.wait_for(asyncio.shield(future), 5)
        except Exception:
            await self.lost("执行进程未完成取消，已停止该组并关闭连接")
            return {"data": {}}

    async def request(self, *args, **kwargs):
        await self.start()
        if self.closing:
            raise ServiceError("worker_stopping", "出口执行进程正在关闭", 503)
        return await self._request(*args, **kwargs)

    async def close(self):
        self.closing = True
        async with self.start_lock:
            if self.process and self.process.returncode is None:
                try:
                    await asyncio.wait_for(self._request("shutdown"), 10)
                    self.process.stdin.close()
                    await asyncio.wait_for(self.process.wait(), 5)
                except Exception:
                    await self.lost("执行进程关闭")
        for task in (self.reader, self.stderr):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)


class RemoteMessages:
    def __init__(self):
        self.disconnected = asyncio.Event()
        self.observers = set()

    def add_disconnect_observer(self, observer):
        self.observers.add(observer)

    def disconnect(self, reason):
        self.disconnected.set()
        for observer in list(self.observers):
            observer(reason)


class RemoteContext:
    def __init__(self, connection):
        self.connection = connection
        self.messages = RemoteMessages()
        self._log_callback = None
        self._logs = []

    @property
    def log_callback(self):
        return self._log_callback

    @log_callback.setter
    def log_callback(self, callback):
        self._log_callback = callback
        for message in self._logs:
            callback(message)
        self._logs.clear()

    def log(self, message):
        if self._log_callback:
            self._log_callback(message)
        elif len(self._logs) < 200:
            self._logs.append(message)

    async def wait_for_player_initialization(self):
        await self.connection.worker.request("initialize", self.connection.account_id)

    async def execute_steps(self, steps, outcome, **options):
        await self.connection.worker.request("execute", self.connection.account_id,
                                             {"steps": steps, "options": options}, outcome=outcome)


class RemoteAccountConnection:
    def __init__(self, spec, worker):
        self.spec, self.worker = spec, worker
        self.account_id = spec.config["service_account_id"]
        self.context = None
        self.state, self.last_error, self.login_block_reason = "未连接", "", ""
        self._online = False

    @property
    def online(self):
        return self._online and self.worker.state == "ready"

    async def connect(self, retries=1):
        self.context = RemoteContext(self)
        self.worker.connections[self.account_id] = self
        result = await self.worker.request("connect", self.account_id,
            {"label": self.spec.label, "config": self.spec.config})
        self.state, self.last_error = result["state"], result["last_error"]
        self.login_block_reason = result["login_block_reason"]
        self._online = result["connected"]
        return self._online

    def lost(self, reason):
        self._online = False
        self.last_error = reason
        if self.context:
            self.context.messages.disconnect(reason)

    async def disconnect(self):
        if self.worker.process and self.worker.process.returncode is None:
            await self.worker._request("disconnect", self.account_id)
        self._online = False
        self.context = None
        self.worker.connections.pop(self.account_id, None)


class WorkerPool:
    def __init__(self, store, events, *, module="src.service.worker_host", local_factory=AccountConnection):
        self.store, self.directory, self.events = store, store.directory, events
        self.module, self.local_factory = module, local_factory
        self.workers = {}
        self.job = None

    def worker(self, group_id):
        if group_id not in self.workers:
            self.workers[group_id] = Worker(self, group_id)
        return self.workers[group_id]

    def connection(self, spec):
        group_id = spec.config.get("egress_group_id")
        return RemoteAccountConnection(spec, self.worker(group_id)) if group_id else self.local_factory(spec)

    def profile(self, group_id):
        return self.worker(group_id).profile()

    async def probe(self, group_id):
        worker = self.worker(group_id)
        result = await worker.request("probe")
        worker.exit_ip, worker.exit_checked_at = result["exit_ip"], utc_now()
        worker.publish()
        return worker.profile()

    async def stop(self, group_id):
        worker = self.workers.pop(group_id, None)
        if worker:
            await worker.close()

    async def close(self):
        try:
            await asyncio.gather(*(worker.close() for worker in self.workers.values()))
        finally:
            if self.job:
                self.job.close()
                self.job = None
