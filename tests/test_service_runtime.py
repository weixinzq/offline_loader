"""Offline lifecycle tests: fake logins, initialization and network writes only."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from time import perf_counter
import unittest

from src.service.contracts import ServiceError, TaskSpec
from src.service.runtime import SessionService
from src.service.tasks import TaskService


class MemoryStore:
    def __init__(self):
        self.directory = Path("unused-test-data")
        self.accounts = {}
        self.tasks = {}
        self.logs = []
        self.secrets = set()

    def add_account(self, index, *, login=None):
        account_id = f"id-{index:03}"
        self.accounts[account_id] = {
            "account_id": account_id, "label": f"标签 {index}",
            "account": login or f"fake-login-{index}@example.invalid",
            "password": f"secret-password-{index}", "char_id": 0, "zone_index": 1,
        }
        return account_id

    def get_account(self, account_id, private=False):
        if account_id not in self.accounts:
            raise ServiceError("not_found", "账号不存在", 404)
        account = deepcopy(self.accounts[account_id])
        if not private:
            account.pop("password")
        return account

    def list_accounts(self):
        return [self.get_account(account_id) for account_id in self.accounts]

    def add_secret(self, value):
        if value:
            self.secrets.add(str(value))

    def redact(self, value):
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
        return value

    def save_task(self, record):
        self.tasks[record["task_id"]] = self.redact(deepcopy(record))

    def get_task(self, task_id):
        if task_id not in self.tasks:
            raise ServiceError("not_found", "任务不存在", 404)
        return deepcopy(self.tasks[task_id])

    def list_tasks(self):
        return deepcopy(list(self.tasks.values()))

    def append_log(self, task_id, account_id, message):
        row = {"id": len(self.logs) + 1, "task_id": task_id, "account_id": account_id,
               "timestamp": "test-time", "message": self.redact(message)}
        self.logs.append(row)
        return deepcopy(row)

    def get_logs(self, task_id, after=0, limit=200):
        return deepcopy([row for row in self.logs if row["task_id"] == task_id and row["id"] > after][:limit])


class FakeEvents:
    def __init__(self):
        self.rows = []

    def publish(self, name, data):
        self.rows.append((name, deepcopy(data)))


class FakeMessages:
    def validate(self, raw):
        return {"valid": True, "count": len(raw)}


class FakeScripts:
    def build_steps(self, steps):
        return tuple(steps)


class FakeContext:
    def __init__(self, factory, config):
        self.factory, self.config = factory, config
        self.log_callback = None
        self.login_result = SimpleNamespace(session_id="private-runtime-sid", user_id="private-player-id")
        self.observers = []
        self.messages = SimpleNamespace(add_disconnect_observer=self.observers.append)

    async def wait_for_player_initialization(self):
        gate = self.factory.initialization_gate
        if gate is not None:
            await gate.wait()
        if self.config["account"] in self.factory.initialization_failures:
            raise ConnectionError("初始化失败 password=" + self.config["password"])


class FakeConnection:
    def __init__(self, factory, spec):
        self.factory, self.spec = factory, spec
        self.context = None
        self.online = False
        self.last_error = ""
        self.login_block_reason = ""
        self.state = "offline"

    async def connect(self, retries=1):
        self.factory.calls.append(self.spec.config["account"])
        self.factory.configs.append(self.spec.config)
        self.factory.logins += 1
        self.factory.max_logins = max(self.factory.max_logins, self.factory.logins)
        try:
            await asyncio.sleep(0)
            if self.spec.config["account"] in self.factory.verifications:
                self.login_block_reason = "登录需要人机验证"
                self.last_error = "需要人机验证"
                return False
            if self.spec.config["account"] in self.factory.blocked:
                self.login_block_reason = self.factory.blocked[self.spec.config["account"]]
                self.last_error = self.login_block_reason
                return False
            self.context = FakeContext(self.factory, self.spec.config)
            self.online = True
            self.factory.active += 1
            self.factory.peak = max(self.factory.peak, self.factory.active)
            return True
        finally:
            self.factory.logins -= 1

    async def disconnect(self):
        self.factory.disconnect_calls += 1
        self.factory.cleanup_started.set()
        if self.factory.cleanup_gate is not None:
            await self.factory.cleanup_gate.wait()
        await asyncio.sleep(0)
        if self.factory.cleanup_failures:
            self.factory.cleanup_failures -= 1
            raise ConnectionError("模拟清理失败")
        if self.online:
            self.factory.active -= 1
        self.online = False
        self.context = None


class ConnectionFactory:
    def __init__(self):
        self.calls, self.configs, self.connections = [], [], []
        self.active = self.peak = self.logins = self.max_logins = 0
        self.initialization_gate = self.cleanup_gate = None
        self.verifications, self.initialization_failures = set(), set()
        self.construction_failures = set()
        self.blocked = {}
        self.cleanup_failures = self.disconnect_calls = 0
        self.cleanup_started = asyncio.Event()

    def __call__(self, spec):
        if spec.config["account"] in self.construction_failures:
            raise RuntimeError("模拟连接构造失败")
        connection = FakeConnection(self, spec)
        self.connections.append(connection)
        return connection


def successful_result(repetitions=1):
    return SimpleNamespace(success=True, completed_repetitions=repetitions,
                           send_results=(SimpleNamespace(success=True),), step_results=())


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.events, self.factory = MemoryStore(), FakeEvents(), ConnectionFactory()
        self.ids = [self.store.add_account(index) for index in range(6)]
        self.executions = []
        self.execution_gate = None
        self.execution_started = asyncio.Event()

        async def execute(account_id, context, steps, **options):
            self.executions.append((account_id, self.factory.active, options))
            self.execution_started.set()
            if self.execution_gate is not None:
                await self.execution_gate.wait()
            return successful_result(options["repetitions"])

        self.sessions = SessionService(self.store, self.events, self.factory)
        self.tasks = TaskService(self.store, self.sessions, FakeScripts(), FakeMessages(), self.events, execute)

    async def asyncTearDown(self):
        if self.factory.cleanup_gate is not None:
            self.factory.cleanup_gate.set()
        if self.factory.initialization_gate is not None:
            self.factory.initialization_gate.set()
        if self.execution_gate is not None:
            self.execution_gate.set()
        await self.tasks.close()
        await self.sessions.shutdown()

    def spec(self, accounts=None, **options):
        value = {"account_ids": accounts or [self.ids[0]], "steps": [
            {"kind": "messages", "messages": [{"id": 1, "cmd": "test", "param": {}}]}]}
        value.update(options)
        if value.get("kind", "execute") != "execute":
            value["steps"] = []
        return TaskSpec.model_validate(value)

    async def wait_task(self, task_id):
        run = self.tasks._runs.get(task_id)
        if run is not None:
            await asyncio.wait_for(asyncio.shield(run.task), 15)
        return self.tasks.get(task_id)

    async def wait_state(self, account_id, state):
        async def wait():
            while next(row for row in self.sessions.list() if row["account_id"] == account_id)["state"] != state:
                await asyncio.sleep(0)
        await asyncio.wait_for(wait(), 2)

    async def test_preflight_does_not_connect_and_hides_message_parameters(self):
        spec = self.spec()
        spec.steps[0].messages[0].param = {"token": "captured-message-token"}
        result = self.tasks.validate(spec)
        self.assertTrue(result["valid"])
        self.assertEqual(result["spec"]["steps"][0]["messages"][0]["param"], "[redacted]")
        self.assertEqual(self.factory.calls, [])
        self.assertFalse(self.sessions.is_busy(self.ids[0]))

    async def test_ready_requires_initialization_and_system_network_config(self):
        self.factory.initialization_gate = asyncio.Event()
        submitted = await self.tasks.submit(self.spec(kind="connect"))
        await self.wait_state(self.ids[0], "initializing")
        self.assertEqual(self.tasks.get(submitted["task_id"])["state"], "running")
        self.factory.initialization_gate.set()
        result = await self.wait_task(submitted["task_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(self.factory.active, 1)
        config = self.factory.configs[0]
        self.assertEqual(config["network_mode"], "system")
        self.assertFalse(config["browser_verification"])
        self.assertNotIn("proxy_url", config)

    async def test_existing_tasks_leave_persistent_session_connected(self):
        connect = await self.tasks.submit(self.spec(kind="connect"))
        await self.wait_task(connect["task_id"])
        with self.assertRaises(ServiceError) as error:
            await self.tasks.submit(self.spec())
        self.assertEqual(error.exception.code, "session_conflict")
        self.execution_gate = asyncio.Event()
        job = await self.tasks.submit(self.spec(session_policy="existing"))
        await self.execution_started.wait()
        await self.tasks.cancel(job["task_id"])
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(self.factory.active, 1)
        self.assertEqual(self.sessions.list()[0]["state"], "ready")

    async def test_existing_task_skips_unready_accounts_and_preserves_residents(self):
        connect = await self.tasks.submit(self.spec([self.ids[0]], kind="connect"))
        await self.wait_task(connect["task_id"])
        spec = self.spec([self.ids[1], self.ids[0], self.ids[2]], session_policy="existing", repetitions=3)
        preview = self.tasks.validate(spec)
        self.assertEqual(preview["account_count"], 1)
        self.assertEqual([row["account_id"] for row in preview["skipped_accounts"]], [self.ids[1], self.ids[2]])
        self.assertEqual(preview["skipped_accounts"][0]["label"], "标签 1")
        self.assertEqual(len(self.factory.calls), 1)
        job = await self.tasks.submit(spec)
        self.assertNotIn(self.ids[1], self.sessions._claim_accounts)
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual([row["state"] for row in result["account_results"]], ["skipped", "succeeded", "skipped"])
        self.assertEqual(result["account_results"][1]["completed_repetitions"], 3)
        self.assertEqual(result["account_results"][0]["network_write"], {"status": "not_attempted", "count": 0})
        self.assertEqual([row[0] for row in self.executions], [self.ids[0]])
        self.assertTrue(self.factory.connections[0].online)
        self.assertEqual(len(self.factory.calls), 1)
        self.assertTrue(any("标签 1" in row["message"] and "跳过" in row["message"] for row in self.store.logs))

    async def test_existing_task_with_no_ready_accounts_names_them_without_creating_task(self):
        for operation in (self.tasks.validate, self.tasks.submit):
            with self.assertRaises(ServiceError) as error:
                if operation == self.tasks.validate:
                    operation(self.spec(self.ids[:2], session_policy="existing"))
                else:
                    await operation(self.spec(self.ids[:2], session_policy="existing"))
            self.assertEqual(error.exception.code, "session_not_ready")
            self.assertIn("标签 0", error.exception.message)
            self.assertIn("标签 1", error.exception.message)
        self.assertFalse(self.store.tasks)
        self.assertFalse(self.sessions._claims)
        self.assertFalse(self.factory.calls)

    async def test_existing_submit_rechecks_connection_after_preflight(self):
        connect = await self.tasks.submit(self.spec(self.ids[:2], kind="connect"))
        await self.wait_task(connect["task_id"])
        spec = self.spec(self.ids[:2], session_policy="existing")
        self.assertEqual(self.tasks.validate(spec)["account_count"], 2)
        self.factory.connections[0].online = False
        self.factory.active -= 1
        job = await self.tasks.submit(spec)
        result = await self.wait_task(job["task_id"])
        self.assertEqual([row["state"] for row in result["account_results"]], ["skipped", "succeeded"])
        self.assertEqual([row[0] for row in self.executions], [self.ids[1]])

    async def test_existing_queued_disconnect_fails_only_that_account(self):
        connect = await self.tasks.submit(self.spec(self.ids[:3], kind="connect"))
        await self.wait_task(connect["task_id"])
        self.execution_gate = asyncio.Event()
        job = await self.tasks.submit(self.spec(self.ids[:3], session_policy="existing", concurrency=1))
        await self.execution_started.wait()
        self.factory.connections[1].online = False
        self.factory.active -= 1
        self.execution_gate.set()
        result = await self.wait_task(job["task_id"])
        self.assertEqual([row["state"] for row in result["account_results"]], ["succeeded", "failed", "succeeded"])
        self.assertEqual(result["account_results"][1]["error"]["code"], "session_not_ready")
        self.assertEqual([row[0] for row in self.executions], [self.ids[0], self.ids[2]])

    async def test_real_login_identity_cannot_be_duplicated_or_claimed_concurrently(self):
        duplicate = self.store.add_account(99, login=self.store.accounts[self.ids[0]]["account"])
        with self.assertRaises(ServiceError) as error:
            self.tasks.validate(self.spec([self.ids[0], duplicate]))
        self.assertEqual(error.exception.code, "duplicate_account")
        first = await self.tasks.submit(self.spec())
        with self.assertRaises(ServiceError) as error:
            await self.tasks.submit(self.spec([duplicate]))
        self.assertEqual(error.exception.code, "account_busy")
        self.assertTrue(self.sessions.is_busy(duplicate))
        await self.wait_task(first["task_id"])
        self.assertFalse(self.sessions.is_busy(duplicate))

    async def test_bounded_limits_complete_lifecycle(self):
        result = await self.tasks.submit(self.spec(self.ids, concurrency=2))
        result = await self.wait_task(result["task_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(self.factory.peak, 2)
        self.assertEqual(self.factory.active, 0)
        self.assertEqual(len(self.executions), 6)
        self.assertTrue(all(not self.sessions.is_busy(account_id) for account_id in self.ids))

    async def test_all_online_logs_in_with_limit_then_executes_with_every_session_online(self):
        job = await self.tasks.submit(self.spec(self.ids, mode="all_online", concurrency=2))
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(self.factory.peak, 6)
        self.assertLessEqual(self.factory.max_logins, 2)
        self.assertEqual({active for _, active, _ in self.executions}, {6})
        self.assertEqual(self.factory.active, 0)

    async def test_all_online_capacity_is_reserved_atomically_before_login(self):
        self.sessions.max_online = 3
        first = await self.tasks.submit(self.spec(self.ids[:2], mode="all_online"))
        with self.assertRaises(ServiceError) as error:
            await self.tasks.submit(self.spec(self.ids[2:4], mode="all_online"))
        self.assertEqual(error.exception.code, "capacity_exceeded")
        self.assertEqual(self.factory.calls, [])
        await self.wait_task(first["task_id"])

    async def test_immediate_cancellation_releases_claim_without_starting_login(self):
        job = await self.tasks.submit(self.spec(self.ids))
        result = await self.tasks.cancel(job["task_id"])
        self.assertEqual(result["state"], "cancelling")
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(self.factory.calls, [])
        next_job = await self.tasks.submit(self.spec(self.ids))
        self.assertEqual((await self.wait_task(next_job["task_id"]))["state"], "succeeded")

    async def test_cancellation_keeps_claim_until_cleanup_finishes(self):
        self.execution_gate = asyncio.Event()
        self.factory.cleanup_gate = asyncio.Event()
        job = await self.tasks.submit(self.spec())
        await self.execution_started.wait()
        await self.tasks.cancel(job["task_id"])
        await self.factory.cleanup_started.wait()
        await self.tasks.cancel(job["task_id"])
        await self.tasks.cancel(job["task_id"])
        self.assertEqual(self.factory.disconnect_calls, 1)
        self.assertEqual(self.tasks.get(job["task_id"])["state"], "cancelling")
        self.assertTrue(self.sessions.is_busy(self.ids[0]))
        with self.assertRaises(ServiceError):
            await self.tasks.submit(self.spec())
        self.factory.cleanup_gate.set()
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(self.factory.active, 0)
        self.assertFalse(self.sessions.is_busy(self.ids[0]))
        self.assertEqual(result["account_results"][0]["network_write"]["status"], "unknown")

    async def test_cleanup_failure_keeps_claim_and_cancel_retries_until_released(self):
        self.factory.cleanup_failures = 2
        job = await self.tasks.submit(self.spec())
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "cancelling")
        self.assertEqual(result["error"]["code"], "cleanup_failed")
        self.assertIsNone(result["finished_at"])
        self.assertTrue(self.sessions.is_busy(self.ids[0]))
        with self.assertRaises(ServiceError):
            await self.tasks.submit(self.spec())
        await self.tasks.cancel(job["task_id"])
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "failed")
        self.assertIsNotNone(result["finished_at"])
        self.assertEqual(self.factory.active, 0)
        self.assertFalse(self.sessions.is_busy(self.ids[0]))

    async def test_cancel_during_final_cleanup_does_not_interrupt_finalizer(self):
        self.factory.cleanup_gate = asyncio.Event()
        job = await self.tasks.submit(self.spec(mode="all_online"))
        await self.factory.cleanup_started.wait()
        self.assertTrue(self.tasks._runs[job["task_id"]].finalizing)
        await self.tasks.cancel(job["task_id"])
        self.assertEqual(self.tasks.get(job["task_id"])["state"], "cancelling")
        self.assertTrue(self.sessions.is_busy(self.ids[0]))
        self.factory.cleanup_gate.set()
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "cancelled")
        self.assertFalse(self.sessions.is_busy(self.ids[0]))

    async def test_shutdown_during_final_cleanup_marks_interrupted_after_cleanup(self):
        self.factory.cleanup_gate = asyncio.Event()
        job = await self.tasks.submit(self.spec(mode="all_online"))
        await self.factory.cleanup_started.wait()
        closing = asyncio.create_task(self.tasks.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        self.assertTrue(self.sessions.is_busy(self.ids[0]))
        self.factory.cleanup_gate.set()
        await asyncio.wait_for(closing, 3)
        self.assertEqual(self.tasks.get(job["task_id"])["state"], "interrupted")
        self.assertFalse(self.sessions.is_busy(self.ids[0]))

    async def test_shutdown_after_cancellation_does_not_cancel_cleanup_again(self):
        self.execution_gate = asyncio.Event()
        self.factory.cleanup_gate = asyncio.Event()
        job = await self.tasks.submit(self.spec())
        await self.execution_started.wait()
        await self.tasks.cancel(job["task_id"])
        await self.factory.cleanup_started.wait()
        closing = asyncio.create_task(self.tasks.close())
        await asyncio.sleep(0)
        self.assertEqual(self.factory.disconnect_calls, 1)
        self.factory.cleanup_gate.set()
        await asyncio.wait_for(closing, 3)
        self.assertEqual(self.tasks.get(job["task_id"])["state"], "interrupted")
        self.assertFalse(self.sessions.is_busy(self.ids[0]))

    async def test_shutdown_retries_completed_cleanup_failure_before_marking_interrupted(self):
        self.factory.cleanup_failures = 2
        job = await self.tasks.submit(self.spec())
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "cancelling")
        self.assertTrue(self.tasks._runs[job["task_id"]].task.done())
        await self.tasks.close()
        self.assertEqual(self.tasks.get(job["task_id"])["state"], "interrupted")
        self.assertEqual(self.factory.active, 0)
        self.assertFalse(self.sessions.is_busy(self.ids[0]))

    async def test_shutdown_before_task_starts_does_not_attempt_login(self):
        job = await self.tasks.submit(self.spec())
        await self.tasks.close()
        self.assertEqual(self.tasks.get(job["task_id"])["state"], "interrupted")
        self.assertEqual(self.factory.calls, [])
        self.assertFalse(self.sessions.is_busy(self.ids[0]))

    async def test_shutdown_marks_running_tasks_interrupted_without_replay(self):
        self.execution_gate = asyncio.Event()
        job = await self.tasks.submit(self.spec())
        await self.execution_started.wait()
        await self.tasks.close()
        result = self.tasks.get(job["task_id"])
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(self.factory.active, 0)
        self.assertEqual(len(self.factory.calls), 1)
        self.assertEqual(len(result["account_results"]), 1)

    async def test_verification_stops_only_affected_batch_and_cleans_temporary_connections(self):
        self.execution_gate = asyncio.Event()
        other = await self.tasks.submit(self.spec([self.ids[5]]))
        await self.execution_started.wait()
        self.factory.verifications.add(self.store.accounts[self.ids[0]]["account"])
        batch = await self.tasks.submit(self.spec(self.ids[:4], mode="all_online", concurrency=2))
        result = await self.wait_task(batch["task_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["code"], "verification_required")
        self.assertEqual(self.tasks.get(other["task_id"])["state"], "running")
        self.assertEqual(self.factory.active, 1)
        self.assertFalse(any(account_id in self.ids[:4] for account_id, _, _ in self.executions))
        self.assertLessEqual(len(self.factory.calls), 3)
        self.execution_gate.set()
        self.assertEqual((await self.wait_task(other["task_id"]))["state"], "succeeded")

    async def test_initialization_failure_never_executes_and_releases_slot(self):
        self.factory.initialization_failures.add(self.store.accounts[self.ids[0]]["account"])
        job = await self.tasks.submit(self.spec())
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.executions, [])
        self.assertEqual(self.factory.active, 0)
        self.assertNotIn("secret-password", str(self.events.rows))

    async def test_blocked_login_stops_waiting_workers_before_slow_cleanup(self):
        self.factory.verifications.add(self.store.accounts[self.ids[0]]["account"])
        self.factory.cleanup_gate = asyncio.Event()
        job = await self.tasks.submit(self.spec(self.ids, mode="all_online", concurrency=2))
        await self.factory.cleanup_started.wait()
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertLessEqual(len(self.factory.calls), 2)
        self.assertEqual(self.executions, [])
        self.assertTrue(self.sessions.is_busy(self.ids[0]))
        self.factory.cleanup_gate.set()
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["error"]["code"], "verification_required")
        self.assertEqual(self.factory.active, 0)

    async def test_rate_limit_stops_only_the_affected_batch(self):
        self.factory.blocked[self.store.accounts[self.ids[0]]["account"]] = "HTTP 429：服务器拒绝继续登录"
        job = await self.tasks.submit(self.spec(self.ids, concurrency=1))
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["code"], "rate_limited")
        self.assertEqual(len(self.factory.calls), 1)
        self.assertEqual(self.factory.active, 0)
        unaffected = await self.tasks.submit(self.spec([self.ids[5]]))
        self.assertEqual((await self.wait_task(unaffected["task_id"]))["state"], "succeeded")

    async def test_persistent_disconnect_observer_releases_capacity(self):
        self.sessions.max_online = 1
        job = await self.tasks.submit(self.spec(kind="connect"))
        await self.wait_task(job["task_id"])
        connection = self.factory.connections[0]
        connection.online = False
        self.factory.active -= 1
        connection.context.observers[0]("模拟掉线")
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertFalse(self.sessions.is_busy(self.ids[0]))
        self.assertIsNone(connection.context)
        replacement = await self.tasks.submit(self.spec([self.ids[1]], kind="connect"))
        self.assertEqual((await self.wait_task(replacement["task_id"]))["state"], "succeeded")

    async def test_persistent_disconnect_detected_by_listing_releases_capacity(self):
        job = await self.tasks.submit(self.spec(kind="connect"))
        await self.wait_task(job["task_id"])
        connection = self.factory.connections[0]
        connection.online = False
        self.factory.active -= 1
        self.assertEqual(self.sessions.list()[0]["state"], "offline")
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertFalse(self.sessions.is_busy(self.ids[0]))
        self.assertIsNone(connection.context)

    async def test_failed_idle_cleanup_can_be_retried_by_disconnect_task(self):
        job = await self.tasks.submit(self.spec(kind="connect"))
        await self.wait_task(job["task_id"])
        self.factory.cleanup_failures = 1
        connection = self.factory.connections[0]
        connection.online = False
        self.factory.active -= 1
        connection.context.observers[0]("模拟掉线")
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertTrue(self.sessions.is_busy(self.ids[0]))
        job = await self.tasks.submit(self.spec(kind="disconnect"))
        self.assertEqual((await self.wait_task(job["task_id"]))["state"], "succeeded")
        self.assertFalse(self.sessions.is_busy(self.ids[0]))

    async def test_connection_factory_exception_does_not_lose_bounded_reservation(self):
        self.factory.construction_failures.add(self.store.accounts[self.ids[0]]["account"])
        job = await self.tasks.submit(self.spec(self.ids[:2], concurrency=1))
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual([row["state"] for row in result["account_results"]], ["failed", "succeeded"])
        self.assertFalse(any(self.sessions.is_busy(account_id) for account_id in self.ids))
        self.assertEqual(self.sessions._reservations, {})

    async def test_drop_during_existing_task_is_cleaned_without_retaining_capacity(self):
        job = await self.tasks.submit(self.spec(kind="connect"))
        await self.wait_task(job["task_id"])
        self.execution_gate = asyncio.Event()
        job = await self.tasks.submit(self.spec(session_policy="existing"))
        await self.execution_started.wait()
        connection = self.factory.connections[0]
        connection.online = False
        self.factory.active -= 1
        connection.context.observers[0]("任务中掉线")
        self.execution_gate.set()
        await self.wait_task(job["task_id"])
        self.assertFalse(self.sessions.is_busy(self.ids[0]))
        self.assertIsNone(connection.context)

    async def test_successful_write_does_not_claim_business_confirmation(self):
        job = await self.tasks.submit(self.spec(repetitions=2))
        result = await self.wait_task(job["task_id"])
        row = result["account_results"][0]
        self.assertEqual(row["network_write"], {"status": "succeeded", "count": 1})
        self.assertEqual(row["business_confirmation"], "unknown")
        self.assertEqual(row["completed_repetitions"], 2)

    async def test_reconnect_reuses_capacity_but_reinitializes(self):
        self.sessions.max_online = 1
        job = await self.tasks.submit(self.spec(kind="connect"))
        await self.wait_task(job["task_id"])
        job = await self.tasks.submit(self.spec(kind="reconnect"))
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(len(self.factory.calls), 2)
        self.assertEqual(self.factory.peak, 1)
        job = await self.tasks.submit(self.spec(kind="disconnect"))
        self.assertEqual((await self.wait_task(job["task_id"]))["state"], "succeeded")
        self.assertEqual(self.factory.active, 0)

    async def test_credentials_runtime_sid_and_message_token_never_reach_logs_or_events(self):
        async def execute(account_id, context, steps, **options):
            context.log_callback("password=" + context.config["password"] +
                                 " sid=" + context.login_result.session_id + " captured-message-token")
            raise RuntimeError("token=captured-message-token")

        self.tasks.executor = execute
        spec = self.spec()
        spec.steps[0].messages[0].param = {"token": "captured-message-token"}
        job = await self.tasks.submit(spec)
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "failed")
        output = str(self.store.tasks) + str(self.events.rows) + str(self.store.logs)
        for secret in ("secret-password-0", "private-runtime-sid", "captured-message-token"):
            self.assertNotIn(secret, output)
        logs = self.tasks.logs(job["task_id"])
        self.assertGreaterEqual(len(logs), 2)
        self.assertEqual(len(self.tasks.logs(job["task_id"], after=logs[0]["id"], limit=1)), 1)

    async def test_one_hundred_sessions_and_event_consumption_release_every_resource(self):
        for index in range(6, 101):
            self.store.add_account(index)
        ids = list(self.store.accounts)[:100]
        started = perf_counter()
        job = await self.tasks.submit(self.spec(ids, mode="all_online", concurrency=7))
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(self.factory.peak, 100)
        self.assertEqual(self.factory.active, 0)
        self.assertEqual(len(result["account_results"]), 100)
        self.assertEqual(sum(name == "session" and row["state"] == "ready"
                             for name, row in self.events.rows), 100)
        self.assertFalse(any(self.sessions.is_busy(account_id) for account_id in ids))
        self.assertEqual(self.sessions._reservations, {})
        self.assertLess(perf_counter() - started, 15)
        self.assertTrue(all(name in {"session", "task", "log"} for name, _ in self.events.rows))

    async def test_more_than_one_hundred_accounts_run_bounded_but_cannot_all_stay_online(self):
        for index in range(6, 101):
            self.store.add_account(index)
        ids = list(self.store.accounts)
        self.assertEqual(len(ids), 101)
        with self.assertRaises(ServiceError) as error:
            await self.tasks.submit(self.spec(ids, mode="all_online", concurrency=7))
        self.assertEqual(error.exception.code, "capacity_exceeded")
        self.assertEqual(self.factory.calls, [])
        self.assertEqual(self.sessions._claims, {})
        self.assertEqual(self.sessions._reservations, {})
        job = await self.tasks.submit(self.spec(ids, mode="bounded", concurrency=7))
        result = await self.wait_task(job["task_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(len(result["account_results"]), 101)
        self.assertEqual(len(self.executions), 101)
        self.assertLessEqual(self.factory.peak, 7)
        self.assertEqual(self.factory.active, 0)
        self.assertFalse(any(self.sessions.is_busy(account_id) for account_id in ids))
        self.assertEqual(self.sessions._claims, {})
        self.assertEqual(self.sessions._reservations, {})


if __name__ == "__main__":
    unittest.main()
