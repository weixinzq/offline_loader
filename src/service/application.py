"""Composition root shared by service hosts and offline tests."""
from pathlib import Path

from src.accounts.manager import AccountConnection
from src.service.accounts import AccountService
from src.service.events import EventHub
from src.service.execution import MessageService, ScriptRegistry
from src.service.store import Store
from src.service.runtime import SessionService
from src.service.tasks import TaskService
from src.service.instance_lock import InstanceLock
from src.service.workers import WorkerPool
from src.service.egress import EgressService


class Application:
    def __init__(self, data_dir: Path, *, connection_factory=AccountConnection, executor=None, max_online=100,
                 worker_module="src.service.worker_host", login_concurrency=None):
        self.store = Store(Path(data_dir))
        self.events = EventHub()
        self.messages = MessageService()
        self.scripts = ScriptRegistry(self.messages)
        self.workers = WorkerPool(self.store, self.events, module=worker_module, local_factory=connection_factory)
        self.sessions = SessionService(self.store, self.events, connection_factory=self.workers.connection,
                                       max_online=max_online, login_concurrency=login_concurrency)
        self.egress = EgressService(self.store, self.sessions, self.workers, self.events)
        self.sessions.egress = self.egress
        self.workers.on_lost = self.egress.failed
        self.accounts = AccountService(self.store, self.sessions, self.events, self.egress)
        self.tasks = TaskService(self.store, self.sessions, self.scripts, self.messages, self.events, executor=executor)
        self._started = False
        self._lock = InstanceLock(self.store.directory / "service.lock")

    async def start(self):
        if not self._started:
            self._lock.acquire()
            try:
                self.store.interrupt_pending()
            except BaseException:
                self._lock.release()
                self.store.close()
                raise
            self._started = True

    async def close(self):
        try:
            try:
                await self.tasks.close()
            finally:
                try:
                    await self.sessions.shutdown()
                finally:
                    await self.workers.close()
        finally:
            self.store.close()
            self._lock.release()
