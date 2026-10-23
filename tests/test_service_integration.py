"""Real SQLite, script registry and dispatch integration with in-memory game I/O."""
from __future__ import annotations

import asyncio
import gc
from pathlib import Path
import tempfile
from time import perf_counter
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import weakref

from src.accounts.login import LoginResult, ZoneData
from src.accounts.manager import AccountConnection
from src.network.context import AppContext, MessageRouter
from src.service.application import Application
from src.service.contracts import ServiceError, TaskSpec
from src.service.instance_lock import InstanceLock


RANK_REPLY = {"_cmd": "71_29", "r": 1, "sai": [
    {"y": 2026, "m": 10, "t": 1, "s": 40, "tt": 254, "wt": 93, "w": 366},
]}


class InMemorySocket:
    def __init__(self, factory):
        self.factory = factory
        self.connected = True
        self.disconnect_reason = ""
        self.active_room_id = -1
        self.active_room_name = ""
        self.incoming = asyncio.Queue()
        self.router = None

    async def send_xt_message(self, ext_id, command, params):
        if not self.connected:
            raise ConnectionError("模拟连接已关闭")
        self.factory.sent.append((ext_id, command, params))
        if command == "71_29":
            if not self.router._subscribers:
                raise AssertionError("脚本必须先订阅再发送")
            self.factory.send_started.set()
            self.incoming.put_nowait({"_cmd": "unrelated"})
            if self.factory.respond:
                self.incoming.put_nowait(RANK_REPLY)

    async def recv_message(self, timeout=5):
        try:
            return await asyncio.wait_for(self.incoming.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def close(self):
        self.factory.close_attempts += 1
        if self.factory.close_failures:
            self.factory.close_failures -= 1
            raise ConnectionError("模拟真实上下文关闭失败")
        if self.connected:
            self.factory.active -= 1
        self.connected = False


class InMemoryConnection(AccountConnection):
    def __init__(self, spec, factory):
        super().__init__(spec)
        self.factory = factory

    async def connect(self, retries=1):
        return await super().connect(opener=self.factory.open_context, retries=retries)


class InMemoryGame:
    def __init__(self):
        self.active = self.peak = self.opens = 0
        self.connections = []
        self.transports = []
        self.contexts = []
        self.sent = []
        self.respond = True
        self.close_failures = self.close_attempts = 0
        self.send_started = asyncio.Event()

    def __call__(self, spec):
        connection = InMemoryConnection(spec, self)
        self.connections.append(connection)
        return connection

    async def open_context(self, config):
        self.opens += 1
        socket = InMemorySocket(self)
        router = MessageRouter(socket)
        socket.router = router
        context = AppContext(
            config=config,
            login_result=LoginResult(success=True, user_id="75238543",
                                     session_id=f"mock-runtime-sid-{self.opens}"),
            zone=ZoneData(1, "offline", "example.invalid", 0),
            socket=socket, messages=router, player_initialized=True,
        )
        router.start()
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.transports.append((socket, router))
        self.contexts.append(weakref.ref(context))
        await asyncio.sleep(0)
        return context


class ApplicationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.game = InMemoryGame()
        # The real router reports its expected stop on every mock disconnect.
        self.warning_patch = patch("src.network.context.logger.warning")
        self.warning_patch.start()
        self.application = Application(self.directory, connection_factory=self.game)
        await self.application.start()

    async def asyncTearDown(self):
        await self.application.close()
        self.warning_patch.stop()
        self.temporary.cleanup()

    def add_account(self, index, password=None):
        return self.application.accounts.create({
            "label": f"模拟账号 {index}", "account": f"mock-{index}@example.invalid",
            "password": password or f"offline-password-{index}",
        })["account_id"]

    @staticmethod
    def specification(account_ids, **options):
        value = {"account_ids": account_ids, "repeat_interval": 0, "message_delay": 0,
                 "steps": [{"kind": "messages", "messages": [
                     {"id": -1, "cmd": "#time", "param": {"seconds": 0}},
                 ]}]}
        value.update(options)
        if value.get("kind", "execute") != "execute":
            value["steps"] = []
        return TaskSpec.model_validate(value)

    async def finish(self, record):
        run = self.application.tasks._runs.get(record["task_id"])
        if run is not None:
            await asyncio.wait_for(asyncio.shield(run.task), 30)
        return self.application.tasks.get(record["task_id"])

    def assert_transports_closed(self):
        self.assertEqual(self.game.active, 0)
        self.assertTrue(all(connection.context is None for connection in self.game.connections))
        for socket, router in self.game.transports:
            self.assertFalse(socket.connected)
            self.assertIsNone(router._task)
            self.assertFalse(router._subscribers)
        self.assertEqual(self.application.sessions._claims, {})
        self.assertEqual(self.application.sessions._reservations, {})
        gc.collect()
        self.assertFalse(any(context() is not None for context in self.game.contexts))

    async def test_default_local_instruction_has_no_network_write_and_short_secret_keeps_ids(self):
        account_id = self.add_account(1, password="a")
        job = await self.application.tasks.submit(self.specification([account_id], repetitions=2))
        result = await self.finish(job)
        self.assertEqual(result["task_id"], job["task_id"])
        self.assertEqual(result["account_results"][0]["account_id"], account_id)
        self.assertEqual(result["spec"]["account_ids"], [account_id])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["account_results"][0]["completed_repetitions"], 2)
        self.assertEqual(result["account_results"][0]["network_write"], {"status": "not_attempted", "count": 0})
        self.assertEqual(self.game.sent, [])
        self.assert_transports_closed()

    async def test_registered_rank_script_uses_capabilities_and_real_router_then_closes_subscription(self):
        account_id = self.add_account(1)
        job = await self.application.tasks.submit(self.specification([account_id], steps=[
            {"kind": "script", "script_id": "rank_info"},
        ]))
        result = await self.finish(job)
        self.assertEqual(result["state"], "succeeded")
        row = result["account_results"][0]
        self.assertEqual(row["network_write"], {"status": "succeeded", "count": 1})
        self.assertEqual(row["business_confirmation"], "unknown")
        self.assertEqual(self.game.sent, [(12, "71_29", {"vUId": 75238543})])
        logs = self.application.tasks.logs(job["task_id"])
        self.assertTrue(any("钻石军长IV" in row["message"] for row in logs))
        self.assert_transports_closed()

    async def test_cancelling_registered_script_restores_socket_and_retains_existing_session(self):
        account_id = self.add_account(1)
        await self.finish(await self.application.tasks.submit(self.specification([account_id], kind="connect")))
        socket, router = self.game.transports[-1]
        original_send = socket.send_xt_message
        self.game.respond = False
        job = await self.application.tasks.submit(self.specification([account_id], session_policy="existing", steps=[
            {"kind": "script", "script_id": "rank_info"},
        ]))
        await asyncio.wait_for(self.game.send_started.wait(), 3)
        await self.application.tasks.cancel(job["task_id"])
        result = await self.finish(job)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["account_results"][0]["network_write"], {"status": "succeeded", "count": 1})
        self.assertEqual(socket.send_xt_message, original_send)
        self.assertFalse(router._subscribers)
        self.assertEqual(self.game.active, 1)
        self.assertEqual(self.application.sessions.list()[0]["state"], "ready")

    async def test_background_session_write_is_not_counted_as_task_write(self):
        account_id = self.add_account(1)
        await self.finish(await self.application.tasks.submit(self.specification([account_id], kind="connect")))
        socket, router = self.game.transports[-1]
        self.game.respond = False

        async def background_heartbeat():
            await self.game.send_started.wait()
            await socket.send_xt_message(18, "background", {})
            router.publish(RANK_REPLY)

        heartbeat = asyncio.create_task(background_heartbeat())
        try:
            job = await self.application.tasks.submit(self.specification([account_id], session_policy="existing", steps=[
                {"kind": "script", "script_id": "rank_info"},
            ]))
            result = await self.finish(job)
            self.assertEqual(result["state"], "succeeded")
            self.assertEqual(result["account_results"][0]["network_write"], {"status": "succeeded", "count": 1})
            self.assertEqual(len(self.game.sent), 2)
        finally:
            await heartbeat

    async def test_real_account_connection_retries_failed_context_cleanup_before_releasing(self):
        account_id = self.add_account(1)
        self.game.close_failures = 2
        job = await self.application.tasks.submit(self.specification([account_id]))
        result = await self.finish(job)
        self.assertEqual(result["state"], "cancelling")
        self.assertEqual(result["error"]["code"], "cleanup_failed")
        self.assertIsNone(result["finished_at"])
        self.assertIsNotNone(self.game.connections[0].context)
        self.assertEqual(self.game.close_attempts, 2)
        self.assertEqual(self.game.active, 1)
        self.assertTrue(self.application.sessions.is_busy(account_id))
        await self.application.tasks.cancel(job["task_id"])
        result = await self.finish(job)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.game.close_attempts, 3)
        self.assert_transports_closed()

    async def test_one_hundred_real_store_sessions_events_and_repeat_runs_release_resources(self):
        ids = [self.add_account(index) for index in range(101)]
        self.application.events.queue_size = 16
        queue = self.application.events.subscribe()
        elapsed = []
        try:
            for _ in range(2):
                started = perf_counter()
                job = await self.application.tasks.submit(self.specification(ids[:100], mode="all_online", concurrency=7))
                with self.assertRaises(ServiceError) as error:
                    await self.application.tasks.submit(self.specification(ids[100:]))
                self.assertEqual(error.exception.code, "capacity_exceeded")
                result = await self.finish(job)
                elapsed.append(perf_counter() - started)
                self.assertEqual(result["state"], "succeeded")
                self.assertEqual(len(result["account_results"]), 100)
                self.assertEqual(self.game.peak, 100)
                self.assertEqual(queue.qsize(), 16)
                self.assertLessEqual(len(self.application.sessions._sessions), 100)
                self.assert_transports_closed()
                events = [queue.get_nowait() for _ in range(queue.qsize())]
                self.assertEqual(events[-1]["event"], "task")
                self.assertEqual(events[-1]["data"]["state"], "succeeded")
                self.assertTrue(all(event["event"] in {"session", "task", "log"} for event in events))
            print("real SQLite + 100 mock sessions, seconds per run:", ", ".join(f"{value:.3f}" for value in elapsed))
        finally:
            self.application.events.unsubscribe(queue)
        self.assertFalse(self.application.events._subscribers)
        self.assertEqual(self.game.opens, 200)

    async def test_restart_interrupts_persisted_work_without_replaying_and_all_sessions_are_offline(self):
        account_id = self.add_account(1, password="a")
        await self.finish(await self.application.tasks.submit(self.specification([account_id], kind="connect")))
        pending_id = uuid4().hex
        self.application.store.save_task({
            "task_id": pending_id, "kind": "execute", "state": "running", "error": None,
            "created_at": "2026-10-15T00:00:00+00:00", "finished_at": None,
            "account_results": [{"account_id": account_id, "state": "running"}],
        })
        await self.application.close()
        opens = self.game.opens
        self.application = Application(self.directory, connection_factory=self.game)
        await self.application.start()
        recovered = self.application.tasks.get(pending_id)
        self.assertEqual(recovered["task_id"], pending_id)
        self.assertEqual(recovered["state"], "interrupted")
        self.assertEqual(recovered["error"]["code"], "service_restart")
        self.assertEqual(recovered["account_results"][0]["account_id"], account_id)
        self.assertEqual(recovered["account_results"][0]["state"], "interrupted")
        self.assertTrue(all(row["state"] == "offline" for row in self.application.sessions.list()))
        self.assertFalse(self.application.tasks._runs)
        self.assertEqual(self.game.opens, opens)
        self.assertEqual(self.game.active, 0)

    async def test_task_shutdown_failure_still_closes_sessions_store_and_instance_lock(self):
        account_id = self.add_account(1)
        await self.finish(await self.application.tasks.submit(self.specification([account_id], kind="connect")))
        with patch.object(self.application.tasks, "close", new=AsyncMock(side_effect=RuntimeError("模拟任务关闭失败"))), \
                patch.object(self.application.sessions, "shutdown", wraps=self.application.sessions.shutdown) as shutdown, \
                patch.object(self.application.store.engine, "dispose", wraps=self.application.store.engine.dispose) as dispose:
            with self.assertRaisesRegex(RuntimeError, "模拟任务关闭失败"):
                await self.application.close()
            shutdown.assert_awaited_once()
            dispose.assert_called_once()
        self.assertIsNone(self.application._lock.stream)
        replacement_lock = InstanceLock(self.directory / "service.lock")
        replacement_lock.acquire()
        replacement_lock.release()
        self.assert_transports_closed()


if __name__ == "__main__":
    unittest.main()
