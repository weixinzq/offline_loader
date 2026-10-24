"""Server availability: local fixtures only, never official servers or real accounts."""
import asyncio
import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from src.accounts.login import LoginResult, parse_login_response, parse_server_list
from src.interfaces.cli import app as cli, server_probe_csv
from src.interfaces.http import create_app
from src.network.server_probe import probe_server
from src.service.application import Application
from src.service.contracts import ServiceError, TaskSpec
from src.service.runtime import Redactor
from tests.server_probe_worker_fixture import FakeSocket, login_result


def target(number=10, **values):
    return {"host": f"192.0.2.{number}", "flash_port": 8000, "h5_port": 8100,
            "domain": "offline.invalid", **values}


def spec(accounts, targets=None, **values):
    return TaskSpec.model_validate({"kind": "server_probe", "account_ids": accounts,
        "server_targets": targets or [target()], "probe_timeout": 2, **values})


class TopologyTests(unittest.TestCase):
    def test_empty_and_invalid_server_slots_do_not_shift_zone_indices(self):
        text = "192.0.2.1:8000:0:;;192.0.2.2:8001:8101:offline.invalid;bad:port:0:;"
        self.assertEqual([s.server_index for s in parse_server_list(text)], [0, 2])
        result = parse_login_response(f"<r><c>ok</c><svr>{text}</svr><zn>7 测试星/2/1/0/0/0/9</zn></r>")
        self.assertEqual(len(result.server_list), 2)
        self.assertEqual((result.zone_list[0].host, result.zone_list[0].server_index), ("192.0.2.2", 2))

    def test_task_rejects_duplicate_endpoints_and_incompatible_modes(self):
        for values in ({"server_targets": [target(), target(server_index=9)]},
                       {"session_policy": "existing"}, {"mode": "all_online"},
                       {"repetitions": 2}, {"planned_at": "2026-10-24T12:00:00"}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                spec(["fixture-account"], **values)


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSocket.active.clear()
        self.config = {"account": "fixture-account", "password": "fixture-password"}
        self.redactor = Redactor(None, self.config)

    async def invoke(self, result=None, socket_class=FakeSocket, destination=None, timeout=.3):
        outcome = {}
        authenticate = AsyncMock(side_effect=[result or login_result("fixture-sid-one"),
                                             result or login_result("fixture-sid-two")])
        with patch("src.network.server_probe.http_login", authenticate), \
             patch("src.network.server_probe.GameSocket", socket_class):
            await probe_server(self.config, destination or target(), timeout, outcome, self.redactor)
        self.assertFalse(FakeSocket.active)
        return outcome, authenticate

    async def test_separate_results_fresh_sessions_and_no_credentials(self):
        sessions = []

        class Socket(FakeSocket):
            async def login(inner, zone, user, session):
                sessions.append(session)
                if inner.transport == "h5":
                    inner.login_response = {"_cmd": "logKO", "msg": "sid=" + session}
                    return None
                return await super().login(zone, user, session)

        result, authenticate = await self.invoke(socket_class=Socket)
        self.assertEqual(result["flash"]["status"], "available")
        self.assertEqual(result["h5"]["status"], "login_rejected")
        self.assertEqual(sessions, ["fixture-sid-one", "fixture-sid-two"])
        self.assertEqual(authenticate.await_count, 2)
        self.assertNotIn("fixture-sid", json.dumps(result))
        self.assertEqual(result["flash"]["zone_index"], 1000)
        self.assertIn("login", result["flash"]["stage_durations_ms"])

    async def test_unknown_command_with_positive_id_is_not_login_success(self):
        class Socket(FakeSocket):
            async def login(inner, *args):
                return {"_cmd": "unrelated", "id": 123}
        result, _ = await self.invoke(socket_class=Socket)
        self.assertEqual(result["flash"]["status"], "login_unconfirmed")

    async def test_supported_ports_and_zone_mapping_are_not_guessed(self):
        result, authenticate = await self.invoke(destination=target(h5_port=0, domain=""))
        self.assertEqual(result["h5"]["status"], "not_supported")
        self.assertEqual(authenticate.await_count, 1)
        self.assertEqual(result["flash"]["status"], "endpoint_missing")
        missing = login_result()
        missing.zone_list = []
        result, _ = await self.invoke(result=missing)
        self.assertEqual(result["flash"]["status"], "no_zone")

    async def test_endpoint_is_resolved_again_when_source_index_changes(self):
        result, _ = await self.invoke(destination=target(server_index=99))
        self.assertEqual(result["flash"]["server_index"], 0)
        self.assertEqual(result["flash"]["status"], "available")

    async def test_stage_timeout_closes_connection(self):
        class Socket(FakeSocket):
            async def login(inner, *args):
                await asyncio.Event().wait()
        result, _ = await self.invoke(socket_class=Socket, timeout=.03)
        self.assertEqual(result["flash"]["status"], "timeout")
        self.assertEqual(result["flash"]["stage"], "login")
        self.assertEqual(result["flash"]["connection_status"], "succeeded")

    async def test_cancellation_waits_for_cleanup(self):
        started, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Socket(FakeSocket):
            async def login(inner, *args):
                started.set()
                await asyncio.Event().wait()

            async def close(inner):
                closing.set()
                await release.wait()
                await super().close()

        outcome = {}
        with patch("src.network.server_probe.http_login", AsyncMock(return_value=login_result())), \
             patch("src.network.server_probe.GameSocket", Socket):
            running = asyncio.create_task(probe_server(self.config, target(), 1, outcome, self.redactor))
            await started.wait()
            running.cancel()
            await closing.wait()
            self.assertFalse(running.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await running
        self.assertFalse(FakeSocket.active)
        self.assertEqual(outcome["flash"]["status"], "cancelled")
        self.assertIsNotNone(outcome["flash"]["finished_at"])

    async def test_blocked_authentication_stops_second_transport(self):
        blocked = LoginResult(False, error_msg="fixture captcha", blocked_reason="verification_required")
        result, authenticate = await self.invoke(result=blocked)
        self.assertEqual(result["blocked_code"], "verification_required")
        self.assertEqual(result["flash"]["status"], "authentication_failed")
        self.assertEqual(authenticate.await_count, 1)

    async def test_five_attempt_limit_and_each_reply_is_recorded(self):
        class Socket(FakeSocket):
            async def login(inner, *args):
                inner.login_response = {"_cmd": "logKO", "msg": "maintenance", "sid": inner.session_id}
                return None
        outcome = {}
        with patch("src.network.server_probe.http_login", AsyncMock(return_value=login_result())), \
             patch("src.network.server_probe.GameSocket", Socket), \
             patch("src.network.server_probe.asyncio.sleep", AsyncMock()):
            await probe_server(self.config, target(), .3, outcome, self.redactor, attempts=5)
        for name in ("flash", "h5"):
            self.assertEqual(len(outcome[name]["attempts"]), 5)
            self.assertEqual(outcome[name]["login_response"]["_cmd"], "logKO")
            self.assertIsNotNone(outcome[name]["connected_at"])
        self.assertNotIn("fixture-sid", json.dumps(outcome))
        self.assertFalse(FakeSocket.active)

    async def test_script_waits_for_initialization_and_does_not_retry_after_login(self):
        events = []
        calls = 0

        class Socket(FakeSocket):
            async def login(inner, *args):
                nonlocal calls
                calls += 1
                if calls < 3:
                    inner.login_response = {"_cmd": "logKO", "msg": "maintenance"}
                    return None
                return await super().login(*args)

        class Context:
            def __init__(inner, config, login, zone, socket, router, **options):
                inner.socket = socket

            async def wait_for_player_initialization(inner):
                events.append("initialize")

            async def close(inner):
                events.append("close")
                await inner.socket.close()

        async def execute(account, context, steps, result, **options):
            self.assertEqual(events[-1], "initialize")
            events.append("execute")
            self.assertEqual(options["repetitions"], 1)
            # A script failure must not reconnect and repeat its side effects.
            result.update(success=False, step_results=[{"error": "fixture script failure"}])

        outcome = {}
        with patch("src.network.server_probe.http_login", AsyncMock(return_value=login_result())), \
             patch("src.network.server_probe.GameSocket", Socket), \
             patch("src.network.server_probe.MessageRouter", return_value=SimpleNamespace(start=lambda: None)), \
             patch("src.network.server_probe.AppContext", Context), \
             patch("src.network.server_probe.start_player_initialization"), \
             patch("src.network.server_probe.run_execution", execute), \
             patch("src.network.server_probe.asyncio.sleep", AsyncMock()):
            await probe_server(self.config, target(), .3, outcome, self.redactor, steps=(object(),), attempts=5)
        self.assertEqual(calls, 4)  # Flash succeeds on third attempt, H5 on first.
        self.assertEqual(events, ["initialize", "execute", "close"] * 2)
        self.assertEqual(len(outcome["flash"]["attempts"]), 3)
        self.assertEqual(len(outcome["h5"]["attempts"]), 1)
        self.assertEqual(outcome["h5"]["initialization_status"], "succeeded")
        self.assertEqual(outcome["h5"]["execution"]["status"], "failed")
        self.assertEqual(outcome["h5"]["login_status"], "succeeded")
        self.assertIsNotNone(outcome["flash"]["logged_in_at"])
        self.assertFalse(FakeSocket.active)

    async def test_waf_status_is_returned_without_using_retry_budget(self):
        result = LoginResult(False, error_msg="WAF TencentCaptcha", blocked_reason="登录需要人机验证",
                             http_stage="role_query", http_status=403)
        outcome = {}
        login = AsyncMock(return_value=result)
        with patch("src.network.server_probe.http_login", login):
            await probe_server(self.config, target(), .3, outcome, self.redactor, attempts=5)
        self.assertEqual(login.await_count, 1)
        self.assertEqual(outcome["blocked_code"], "verification_required")
        response = outcome["flash"]["authentication_response"]
        self.assertEqual(response["http_status"], 403)
        self.assertEqual(response["http_stage"], "role_query")
        self.assertEqual(len(outcome["flash"]["attempts"]), 1)


class ProbeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.application = Application(Path(self.temporary.name), max_online=3, login_concurrency=1,
                                       worker_module="tests.server_probe_worker_fixture")
        await self.application.start()

    async def asyncTearDown(self):
        await self.application.close()
        self.temporary.cleanup()

    def account(self, suffix="one", *, confirmed=True, grouped=True):
        group = self.application.egress.create({"name": "测试出口" + suffix, "clash_policy": "fixture-policy",
                    "max_online": 1, "routing_confirmed": confirmed})["group_id"] if grouped else None
        account = self.application.accounts.create({"label": "测试" + suffix, "account": "fixture-" + suffix,
                    "password": "fixture-password-" + suffix, "egress_group_id": group})["account_id"]
        return account, group

    async def finish(self, record):
        run = self.application.tasks._runs.get(record["task_id"])
        if run:
            await asyncio.wait_for(asyncio.shield(run.task), 15)
        return self.application.tasks.get(record["task_id"])

    async def test_preflight_requires_group_and_confirmation_without_worker_or_login(self):
        for values, code in (({"grouped": False}, "egress_required"),
                             ({"confirmed": False}, "egress_unconfirmed")):
            account, _ = self.account(str(len(self.application.store.list_accounts())), **values)
            with self.assertRaises(ServiceError) as error:
                self.application.tasks.validate(spec([account]))
            self.assertEqual(error.exception.code, code)
        self.assertTrue(all(w.process is None for w in self.application.workers.workers.values()))

    async def test_worker_routing_assignment_results_and_capacity_cleanup(self):
        first, first_group = self.account()
        second, second_group = self.account("reject")
        task = spec([first, second], [target(10), target(11), target(12)], concurrency=3)
        checked = self.application.tasks.validate(task)
        self.assertEqual(checked["effective_concurrency"], 1)
        self.assertEqual(checked["max_transport_attempts"], 6)
        self.assertEqual([row["account_id"] for row in checked["server_assignments"]], [first, second, first])
        record = await self.finish(await self.application.tasks.submit(task))
        self.assertEqual(record["state"], "failed")
        rows = record["server_results"]
        self.assertTrue(all(row["flash"]["status"] == "available" for row in rows))
        self.assertEqual(rows[1]["h5"]["status"], "login_rejected")
        self.assertEqual(rows[1]["egress_group_id"], second_group)
        self.assertEqual(rows[0]["exit_ip"], "192.0.2.10")
        intervals = sorted((row["flash"]["started_at"], row["h5"]["finished_at"]) for row in rows)
        for previous, current in zip(intervals, intervals[1:]):
            self.assertGreaterEqual(current[0], previous[1])
        for group in (first_group, second_group):
            worker = self.application.workers.worker(group)
            stats = await worker.request("inspect_probes")
            self.assertEqual(stats["active"], 0)
            self.assertEqual(stats["probing"], 0)
            self.assertEqual(stats["peak"], 1)
        self.assertFalse(self.application.sessions._claims)
        self.assertFalse(self.application.sessions._reservations)
        self.assertFalse(self.application.sessions._group_reservations)
        text = json.dumps(record) + json.dumps(self.application.tasks.logs(record["task_id"]))
        self.assertNotIn("fixture-sid", text)
        self.assertNotIn("fixture-password", text)

    async def test_probe_rejects_resident_account_without_disconnecting_it(self):
        account, _ = self.account()
        connected = await self.finish(await self.application.tasks.submit(TaskSpec(kind="connect", account_ids=[account])))
        self.assertEqual(connected["state"], "succeeded", connected)
        with self.assertRaises(ServiceError) as error:
            self.application.tasks.validate(spec([account]))
        self.assertEqual(error.exception.code, "session_conflict")
        self.assertEqual(self.application.sessions.list()[0]["state"], "ready")

    async def test_exit_probe_failure_does_not_attempt_game_login(self):
        account, group = self.account()
        with patch.object(self.application.egress, "probe", AsyncMock(side_effect=ServiceError("egress_probe_failed", "fixture exit failed"))):
            result = await self.finish(await self.application.tasks.submit(spec([account])))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["server_results"][0]["error"]["code"], "egress_probe_failed")
        self.assertEqual(result["server_results"][0]["flash"]["status"], "not_tested")
        self.assertIsNone(self.application.workers.worker(group).process)
        self.assertFalse(self.application.sessions.is_busy(account))

    async def test_cancel_waits_for_child_cleanup_before_releasing_claims(self):
        account, group = self.account("wait")
        record = await self.application.tasks.submit(spec([account], [target(10), target(11)]))
        worker = self.application.workers.worker(group)
        for _ in range(200):
            if worker.process and worker.state == "ready":
                stats = await worker.request("inspect_probes")
                if stats["active"]:
                    break
            await asyncio.sleep(.01)
        else:
            self.fail("fixture probe did not start")
        with self.assertRaises(ServiceError):
            await self.application.tasks.submit(spec([account]))
        await self.application.tasks.cancel(record["task_id"])
        result = await self.finish(record)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["server_results"][0]["flash"]["status"], "cancelled")
        self.assertEqual(result["server_results"][1]["state"], "cancelled")
        self.assertEqual((await worker.request("inspect_probes"))["active"], 0)
        self.assertFalse(self.application.sessions.is_busy(account))

    async def test_blocked_account_stops_remaining_targets(self):
        account, _ = self.account("blocked")
        result = await self.finish(await self.application.tasks.submit(spec([account], [target(10), target(11)])))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["code"], "verification_required")
        self.assertEqual(result["server_results"][1]["state"], "skipped")
        self.assertFalse(self.application.sessions.is_busy(account))

    async def test_worker_loss_releases_claims_after_process_exit(self):
        account, group = self.account("wait")
        record = await self.application.tasks.submit(spec([account]))
        worker = self.application.workers.worker(group)
        for _ in range(200):
            if worker.process and worker.state == "ready" and (await worker.request("inspect_probes"))["active"]:
                break
            await asyncio.sleep(.01)
        else:
            self.fail("fixture probe did not start")
        worker.process.kill()
        await worker.process.wait()
        result = await self.finish(record)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["server_results"][0]["error"]["code"], "worker_lost")
        self.assertFalse(self.application.sessions.is_busy(account))


class ProbeAdapterTests(unittest.TestCase):
    def test_scheduled_runner_submits_once_at_deadline_and_exports_all_logs(self):
        from tools.run_server_probe import run
        clock = datetime.now(timezone.utc)
        at = clock + timedelta(seconds=2)
        requests = []
        record = {"task_id": "fixture-task", "kind": "server_probe", "state": "succeeded", "server_results": []}

        def request(method, path, body=None):
            requests.append((method, path, clock))
            if path == "/tasks/validate":
                return {"valid": True}
            if "/logs?" in path:
                return [{"id": 1, "message": "fixture result"}]
            return record

        def advance(seconds):
            nonlocal clock
            clock += timedelta(seconds=seconds)

        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "probe.json"
            file.write_text(json.dumps(spec(["fixture-account"]).model_dump(mode="json")))
            output = Path(directory) / "result"
            run(file, output, at, request, clock=lambda: clock, sleep=advance)
            submissions = [r for r in requests if r[:2] == ("POST", "/tasks")]
            self.assertEqual(len(submissions), 1)
            self.assertEqual(submissions[0][2], at)
            self.assertEqual(json.loads((output / "schedule.json").read_text())["state"], "succeeded")
            self.assertIn("fixture result", (output / "task.log").read_text())
            self.assertTrue((output / "results.csv").is_file())

    def test_http_preserves_probe_fields_and_partial_results(self):
        with tempfile.TemporaryDirectory() as directory:
            application = Application(Path(directory), worker_module="tests.server_probe_worker_fixture")
            app = create_app(data_dir=directory, admin_token="fixture-admin-token", application=application)
            with TestClient(app, headers={"Authorization": "Bearer fixture-admin-token"}) as client:
                group = client.post("/api/v1/egress-groups", json={"name": "fixture-group", "clash_policy": "fixture-policy",
                                      "routing_confirmed": True}).json()["group_id"]
                account = client.post("/api/v1/accounts", json={"label": "fixture-label", "account": "fixture-account",
                           "password": "fixture-password", "egress_group_id": group}).json()["account_id"]
                body = spec([account]).model_dump(mode="json")
                body.pop("probe_timeout")
                checked = client.post("/api/v1/tasks/validate", json=body)
                self.assertEqual(checked.status_code, 200, checked.text)
                self.assertEqual(len(checked.json()["server_assignments"]), 1)
                response = client.post("/api/v1/tasks", json=body)
                self.assertEqual(response.status_code, 202, response.text)
                task_id = response.json()["task_id"]
                for _ in range(300):
                    result = client.get(f"/api/v1/tasks/{task_id}").json()
                    if result["state"] in {"succeeded", "failed", "cancelled"}:
                        break
                    time.sleep(.01)
                self.assertEqual(result["state"], "succeeded", result)
                results = client.get(f"/api/v1/tasks/{task_id}/results").json()
                self.assertEqual(results["server_results"][0]["h5"]["status"], "available")
                exported = list(csv.DictReader(io.StringIO(server_probe_csv(result))))
                self.assertEqual(len(exported), 2)
                self.assertEqual(exported[0]["transport"], "flash")

    def test_cli_dry_run_scheduling_and_csv_export(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe.json"
            path.write_text(json.dumps({"account_ids": ["fixture-id"], "server_targets": [target()]}))
            with patch("src.interfaces.cli._request", AsyncMock(return_value={"valid": True})) as request:
                result = runner.invoke(cli, ["servers", "probe", "--file", str(path), "--dry-run"])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(request.await_count, 1)
                self.assertEqual(request.await_args.args[2], "/tasks/validate")
            at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

            async def skip_wait(_seconds):
                raise KeyboardInterrupt

            with patch("src.interfaces.cli._request", AsyncMock(return_value={"valid": True})) as request, \
                 patch("src.interfaces.cli.asyncio.sleep", skip_wait):
                result = runner.invoke(cli, ["servers", "probe", "--file", str(path), "--at", at])
                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual(request.await_count, 1)
            with patch("src.interfaces.cli._request", AsyncMock()) as request:
                result = runner.invoke(cli, ["servers", "probe", "--file", str(path), "--at", "2020-01-01T00:00:00+08:00"])
                self.assertEqual(result.exit_code, 1)
                request.assert_not_awaited()
            class Clock:
                value = datetime.now(timezone.utc)

                @classmethod
                def now(cls, _timezone):
                    return cls.value

            scheduled = (Clock.value + timedelta(seconds=2)).isoformat()

            async def advance(seconds):
                Clock.value += timedelta(seconds=seconds)

            with patch("src.interfaces.cli.datetime", Clock), \
                 patch("src.interfaces.cli.asyncio.sleep", advance), \
                 patch("src.interfaces.cli._request", AsyncMock(side_effect=[{"valid": True}, {"task_id": "fixture-task"}])) as request:
                result = runner.invoke(cli, ["servers", "probe", "--file", str(path), "--at", scheduled])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(request.await_args.args[2], "/tasks")
                self.assertEqual(datetime.fromisoformat(request.await_args.args[3]["planned_at"]),
                                 datetime.fromisoformat(scheduled))
            output = Path(directory) / "results.csv"
            with patch("src.interfaces.cli._request", AsyncMock(return_value={"task_id": "fixture-task",
                       "kind": "server_probe", "server_results": []})):
                result = runner.invoke(cli, ["servers", "results", "fixture-task", "--format", "csv", "--output", str(output)])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn("connection_status", output.read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
