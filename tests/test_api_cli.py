"""HTTP contracts and real loopback CLI checks; no game login or outbound traffic."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import aiohttp
from fastapi.testclient import TestClient
from fastapi import Request
from pydantic import ValidationError
from typer.testing import CliRunner
import uvicorn

from src.interfaces.cli import app as cli
from src.interfaces.http import COOKIE_NAME, SessionInput, _same_origin, create_app, load_admin_token
from src.service.application import Application


class SimulatedConnection:
    instances = []

    def __init__(self, spec):
        self.spec = spec
        self.context = None
        self.online = False
        self.last_error = ""
        self.login_block_reason = ""
        self.state = "offline"
        self.instances.append(self)

    async def connect(self, retries=1):
        async def initialize():
            await asyncio.sleep(0.01)
        self.context = SimpleNamespace(wait_for_player_initialization=initialize, log_callback=None)
        self.online = True
        return True

    async def disconnect(self):
        await asyncio.sleep(0.01)
        self.online = False
        self.context = None


async def simulated_executor(_account_id, context, _steps, **options):
    await asyncio.sleep(0.1)
    context.log_callback("simulated local write completed")
    return SimpleNamespace(success=True, send_results=(SimpleNamespace(success=True),),
                           completed_repetitions=options["repetitions"],
                           step_results=(SimpleNamespace(repetition=1, step=1, kind="messages",
                                                         success=True, error=""),))


def account_payload(label="sample"):
    return {"label": label, "account": "simulated-" + label,
            "password": "offline-test-secret", "char_id": 0, "zone_index": 1}


def task_payload(account_id, policy="temporary"):
    return {"kind": "execute", "account_ids": [account_id], "session_policy": policy,
            "repeat_interval": 0, "message_delay": 0,
            "steps": [{"kind": "messages", "messages": [
                {"id": -1, "cmd": "#time", "param": {"seconds": 0}}]}]}


def wait_task(client, task_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = client.get(f"/api/v1/tasks/{task_id}").json()
        if record["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return record
        time.sleep(0.01)
    raise AssertionError("Simulated task did not finish")


class AdapterInvariantTests(unittest.TestCase):
    def test_origin_validation_handles_malformed_origins_and_default_ports(self):
        for origin in ("http://testserver", "http://testserver:80", "http://TESTSERVER"):
            request = Request({"type": "http", "scheme": "http", "path": "/", "query_string": b"",
                               "headers": [(b"host", b"testserver"), (b"origin", origin.encode())]})
            self.assertTrue(_same_origin(request), origin)
        for origin in ("http://[broken", "null", "http://testserver:invalid", "http://testserver:81",
                       "https://testserver", "http://other.invalid", "http://user@testserver",
                       "http://testserver/path", "http://testserver?query=1"):
            request = Request({"type": "http", "scheme": "http", "path": "/", "query_string": b"",
                               "headers": [(b"host", b"testserver"), (b"origin", origin.encode())]})
            self.assertFalse(_same_origin(request), origin)

    def test_session_inputs_reject_invalid_batches_before_application_dispatch(self):
        for body in ({"account_ids": ["same", "same"]},
                     {"account_ids": ["sample"], "concurrency": True}):
            with self.assertRaises(ValidationError):
                SessionInput.model_validate(body)
        batch = SessionInput.model_validate({"account_ids": [str(number) for number in range(101)]})
        self.assertEqual(len(batch.account_ids), 101)

    def test_service_host_limits_workers_and_shutdown_wait_for_streams(self):
        with patch("src.interfaces.http.load_admin_token"), patch("src.interfaces.http.create_app") as factory, \
                patch("uvicorn.run") as run:
            invocation = CliRunner().invoke(cli, ["--token", "test-only-token", "serve", "--secure-cookies"])
        self.assertEqual(invocation.exit_code, 0, invocation.output)
        self.assertEqual(run.call_args.kwargs["workers"], 1)
        self.assertEqual(run.call_args.kwargs["timeout_graceful_shutdown"], 10)
        self.assertTrue(factory.call_args.kwargs["secure_cookies"])
        self.assertEqual(factory.call_args.kwargs["max_online"], 100)
        self.assertIsNone(factory.call_args.kwargs["login_concurrency"])
        with patch("src.interfaces.http.load_admin_token"), patch("src.interfaces.http.create_app") as factory, \
                patch("uvicorn.run"):
            invocation = CliRunner().invoke(cli, ["--token", "test-only-token", "serve",
                                                  "--max-online", "300", "--login-concurrency", "20"])
        self.assertEqual(invocation.exit_code, 0, invocation.output)
        self.assertEqual(factory.call_args.kwargs["max_online"], 300)
        self.assertEqual(factory.call_args.kwargs["login_concurrency"], 20)
        for flag in ("--max-online", "--login-concurrency"):
            self.assertNotEqual(CliRunner().invoke(cli, ["serve", flag, "0"]).exit_code, 0)

    def test_non_utf8_message_file_reports_an_input_error(self):
        invalid = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")
        with patch("pathlib.Path.read_text", side_effect=invalid):
            result = CliRunner().invoke(cli, ["parse", "--file", __file__])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("Cannot read message file", result.output)

    def test_account_creation_requires_an_explicit_stable_label(self):
        result = CliRunner().invoke(cli, ["accounts", "create", "--account", "sample-login",
                                         "--password", "local-test-password"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--label", result.output)


class APIContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        SimulatedConnection.instances.clear()
        self.application = Application(self.directory, connection_factory=SimulatedConnection,
                                       executor=simulated_executor)
        self.frontend = self.directory / "frontend"
        self.frontend.mkdir()
        (self.frontend / "index.html").write_text("<html>Local application</html>", encoding="utf-8")
        (self.frontend / "app.js").write_text("console.log('application')", encoding="utf-8")
        self.web = create_app(self.directory, admin_token="admin-test-token", application=self.application,
                              frontend_dir=self.frontend)
        self.client = TestClient(self.web)
        self.client.__enter__()
        self.client.headers["Authorization"] = "Bearer admin-test-token"

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()

    def create_account(self, label="sample"):
        response = self.client.post("/api/v1/accounts", json=account_payload(label))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("password", response.json())
        return response.json()["account_id"]

    def test_authentication_and_validation_never_echo_passwords(self):
        self.client.headers.pop("Authorization")
        self.assertEqual(self.client.get("/api/v1/accounts").status_code, 401)
        self.client.headers["Authorization"] = "Bearer incorrect"
        self.assertEqual(self.client.get("/api/v1/accounts").status_code, 401)
        self.client.headers["Authorization"] = "Bearer admin-test-token"
        secret = "do-not-reflect-password"
        value = account_payload()
        value["password"] = {"submitted": secret}
        response = self.client.post("/api/v1/accounts", json=value)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("input", response.text)
        account_id = self.create_account()
        self.assertNotIn("offline-test-secret", self.client.get("/api/v1/accounts").text)
        task = task_payload(account_id)
        task["file"] = "C:/must-not-read.json"
        self.assertEqual(self.client.post("/api/v1/tasks/validate", json=task).status_code, 422)
        for endpoint in ("/accounts/import", "/messages/parse"):
            response = self.client.post("/api/v1" + endpoint, json={"file": "C:/must-not-read.json"})
            self.assertEqual(response.status_code, 422)
        for script in self.client.get("/api/v1/scripts").json():
            self.assertNotIn("module", script)
            self.assertNotIn("module_name", script)
        for operation in ("connect", "reconnect", "disconnect"):
            response = self.client.post("/api/v1/sessions/" + operation,
                                        json={"account_ids": [account_id, account_id]})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(SimulatedConnection.instances, [])

    def test_cookie_authentication_requires_same_origin_for_writes(self):
        self.client.headers.pop("Authorization")
        body = {"token": "admin-test-token"}
        self.assertEqual(self.client.post("/api/v1/auth/session", json=body).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/auth/session", json=body,
                         headers={"Origin": "http://elsewhere.invalid"}).status_code, 403)
        login = self.client.post("/api/v1/auth/session", json=body,
                                 headers={"Origin": "http://testserver"})
        self.assertEqual(login.status_code, 200)
        cookie = login.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertNotIn("admin-test-token", cookie)
        self.assertEqual(self.client.get("/api/v1/accounts").status_code, 200)
        self.assertEqual(self.client.post("/api/v1/accounts", json=account_payload()).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/accounts", json=account_payload(),
                         headers={"Origin": "http://testserver"}).status_code, 201)
        self.assertEqual(self.client.delete("/api/v1/auth/session",
                         headers={"Origin": "http://testserver"}).status_code, 204)
        self.assertEqual(self.client.get("/api/v1/accounts").status_code, 401)

    def test_preflight_persistent_session_ownership_and_cleanup(self):
        first = self.create_account("first")
        second = self.create_account("second")
        parsed = self.client.post("/api/v1/messages/parse", json={"text": "#time=0|"})
        self.assertEqual(parsed.status_code, 200, parsed.text)
        preflight = self.client.post("/api/v1/tasks/validate", json=task_payload(first))
        self.assertEqual(preflight.status_code, 200, preflight.text)
        self.assertEqual(SimulatedConnection.instances, [])
        started = self.client.post("/api/v1/sessions/connect", json={"account_ids": [first]}).json()
        self.assertEqual(wait_task(self.client, started["task_id"])["state"], "succeeded")
        response = self.client.patch(f"/api/v1/accounts/{second}", json={"label": "edited"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.patch(f"/api/v1/accounts/{first}", json={"label": "busy"}).status_code, 409)
        conflict = self.client.post("/api/v1/tasks", json=task_payload(first))
        self.assertEqual(conflict.status_code, 409)
        existing = task_payload(first, "existing")
        started = self.client.post("/api/v1/tasks", json=existing).json()
        self.assertEqual(self.client.post("/api/v1/tasks", json=existing).status_code, 409)
        result = wait_task(self.client, started["task_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["account_results"][0]["business_confirmation"], "unknown")
        self.assertEqual(result["account_results"][0]["network_write"]["status"], "succeeded")
        self.assertIsNone(result["account_results"][0]["step_results"][0]["error"])
        self.assertEqual(self.client.get("/api/v1/sessions").json()[0]["state"], "ready")
        logs = self.client.get(f"/api/v1/tasks/{started['task_id']}/logs").json()
        self.assertEqual(logs[0]["message"], "simulated local write completed")
        self.assertEqual(self.client.get(f"/api/v1/tasks/{started['task_id']}/logs",
                         params={"after": logs[-1]["id"]}).json(), [])
        temporary = self.client.post("/api/v1/tasks", json=task_payload(second)).json()
        self.assertEqual(wait_task(self.client, temporary["task_id"])["state"], "succeeded")
        by_id = {row["account_id"]: row for row in self.client.get("/api/v1/sessions").json()}
        self.assertEqual(by_id[first]["state"], "ready")
        self.assertEqual(by_id[second]["state"], "offline")

    def test_existing_tasks_report_skipped_accounts_through_public_api(self):
        first, offline = self.create_account("online"), self.create_account("offline")
        connected = self.client.post("/api/v1/sessions/connect", json={"account_ids": [first]}).json()
        self.assertEqual(wait_task(self.client, connected["task_id"])["state"], "succeeded")
        payload = task_payload(first, "existing")
        payload["account_ids"] = [offline, first]
        checked = self.client.post("/api/v1/tasks/validate", json=payload)
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json()["account_count"], 1)
        self.assertEqual(checked.json()["skipped_accounts"][0]["account_id"], offline)
        started = self.client.post("/api/v1/tasks", json=payload)
        self.assertEqual(started.status_code, 202, started.text)
        result = wait_task(self.client, started.json()["task_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual([row["state"] for row in result["account_results"]], ["skipped", "succeeded"])

    def test_openapi_exposes_inputs_and_sanitized_output_types(self):
        schema = self.web.openapi()
        models = schema["components"]["schemas"]
        self.assertIn("TaskSpec", models)
        self.assertIn("TaskRecord", models)
        self.assertNotIn("password", models["AccountRecord"]["properties"])
        self.assertIn("HTTPBearer", schema["components"]["securitySchemes"])
        self.assertNotIn("/{path}", schema["paths"])
        self.assertIn("APIKeyCookie", schema["components"]["securitySchemes"])
        event_types = schema["paths"]["/api/v1/events"]["get"]["responses"]["200"]["content"]
        self.assertEqual(set(event_types), {"text/event-stream"})

    def test_online_capacity_is_checked_by_service_instead_of_batch_length(self):
        imported = self.client.post("/api/v1/accounts/import", json={"config": {
            "accounts": [account_payload(f"capacity-{number}") for number in range(101)],
        }})
        self.assertEqual(imported.status_code, 200, imported.text)
        account_ids = [row["account_id"] for row in imported.json()["accounts"]]
        payload = task_payload(account_ids[0])
        payload.update(account_ids=account_ids, mode="bounded", concurrency=3)
        bounded = self.client.post("/api/v1/tasks/validate", json=payload)
        self.assertEqual(bounded.status_code, 200, bounded.text)
        self.assertEqual(bounded.json()["required_capacity"], 3)
        payload["mode"] = "all_online"
        refused = self.client.post("/api/v1/tasks", json=payload)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "capacity_exceeded")
        self.assertEqual(SimulatedConnection.instances, [])
        self.assertEqual(self.client.get("/api/v1/tasks").json(), [])

    def test_frontend_static_assets_history_routes_and_api_404(self):
        self.client.headers.pop("Authorization")
        self.assertEqual(self.client.get("/").text, "<html>Local application</html>")
        self.assertEqual(self.client.get("/tasks/history").text, "<html>Local application</html>")
        self.assertIn("application", self.client.get("/app.js").text)
        self.assertEqual(self.client.head("/app.js").status_code, 200)
        self.assertEqual(self.client.get("/missing.js").status_code, 404)
        self.assertEqual(self.client.get("/assets/missing").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/unknown").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/unknown", json={}).status_code, 404)
        self.assertEqual(self.client.get("/api/unknown").status_code, 404)
        self.assertEqual(self.client.get("/%2e%2e/admin.token").status_code, 404)


class LoopbackCLIAndEventsTests(unittest.TestCase):
    def test_cli_uses_http_and_sse_disconnect_does_not_cancel_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            release_execution = threading.Event()
            finish_execution = threading.Event()

            async def held_executor(*args, **kwargs):
                while not release_execution.is_set():
                    await asyncio.sleep(0.01)
                args[1].log_callback("simulated task is still running")
                while not finish_execution.is_set():
                    await asyncio.sleep(0.01)
                return await simulated_executor(*args, **kwargs)

            application = Application(Path(temporary), connection_factory=SimulatedConnection,
                                      executor=held_executor)
            web = create_app(temporary, admin_token="loopback-test-token", application=application)
            server = uvicorn.Server(uvicorn.Config(web, log_level="error", lifespan="on"))
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(128)
                port = listener.getsockname()[1]
                thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
                thread.start()
                try:
                    deadline = time.monotonic() + 5
                    while not server.started and thread.is_alive() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(server.started)
                    base = ["--url", f"http://127.0.0.1:{port}", "--token", "loopback-test-token", "--json"]
                    runner = CliRunner()
                    with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
                        created = runner.invoke(cli, base + ["accounts", "create", "--label", "cli-sample",
                             "--account", "simulated-cli", "--password", "local-cli-secret"])
                    self.assertEqual(created.exit_code, 0, created.output)
                    account_id = json.loads(created.stdout)["account_id"]
                    messages = Path(temporary) / "messages.txt"
                    messages.write_text("#time=0|", encoding="utf-8")
                    legacy = Path(temporary) / "legacy.json"
                    legacy.write_text(json.dumps({"workers": [{"id": "old-worker",
                        "accounts": {"labels": ["cli-sample"]},
                        "steps": [{"kind": "messages", "file": "messages.txt"}]}]}), encoding="utf-8")
                    original = legacy.read_bytes()
                    report_file = Path(temporary) / "migration.json"
                    migrated = runner.invoke(cli, base + ["tasks", "migrate", "--file", str(legacy),
                                                         "--output", str(report_file)])
                    self.assertEqual(migrated.exit_code, 0, migrated.output)
                    report = json.loads(migrated.stdout)
                    self.assertEqual(report["tasks"][0]["account_ids"], [account_id])
                    self.assertFalse(report["source_modified"])
                    self.assertEqual(json.loads(report_file.read_text(encoding="utf-8")), report)
                    self.assertEqual(legacy.read_bytes(), original)
                    self.assertNotIn("file", report["tasks"][0]["steps"][0])
                    self.assertEqual(runner.invoke(cli, base + ["tasks", "list"]).stdout.strip(), "[]")
                    refused = runner.invoke(cli, base + ["tasks", "migrate", "--file", str(legacy),
                                                        "--output", str(legacy)])
                    self.assertNotEqual(refused.exit_code, 0)
                    self.assertEqual(legacy.read_bytes(), original)
                    snapshot = Path(temporary) / "task.json"
                    snapshot.write_text(json.dumps(task_payload(account_id)), encoding="utf-8")
                    before = runner.invoke(cli, base + ["tasks", "validate", "--file", str(snapshot)])
                    self.assertEqual(before.exit_code, 0, before.output)
                    self.assertTrue(json.loads(before.stdout)["valid"])
                    started = runner.invoke(cli, base + ["tasks", "start", "--file", str(snapshot)])
                    self.assertEqual(started.exit_code, 0, started.output)
                    task_id = json.loads(started.stdout)["task_id"]

                    async def read_one_event():
                        async with aiohttp.ClientSession(trust_env=False,
                                 headers={"Authorization": "Bearer loopback-test-token"}) as client:
                            async with client.get(f"http://127.0.0.1:{port}/api/v1/events",
                                                  params={"task_id": task_id}) as response:
                                self.assertEqual(response.status, 200)
                                self.assertEqual(await response.content.readuntil(b"\n\n"), b": connected\n\n")
                                release_execution.set()
                                frame = await asyncio.wait_for(response.content.readuntil(b"\n\n"), timeout=5)
                                self.assertIn(b'"task_id":"' + task_id.encode() + b'"', frame)
                    asyncio.run(read_one_event())
                    still_running = runner.invoke(cli, base + ["tasks", "status", task_id])
                    self.assertEqual(json.loads(still_running.stdout)["state"], "running")
                    cleanup_deadline = time.monotonic() + 2
                    while application.events._subscribers and time.monotonic() < cleanup_deadline:
                        time.sleep(0.01)
                    self.assertEqual(len(application.events._subscribers), 0)
                    finish_execution.set()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        status = runner.invoke(cli, base + ["tasks", "status", task_id])
                        self.assertEqual(status.exit_code, 0, status.output)
                        record = json.loads(status.stdout)
                        if record["state"] in {"succeeded", "failed", "cancelled"}:
                            break
                        time.sleep(0.01)
                    self.assertEqual(record["state"], "succeeded")
                    listed = runner.invoke(cli, base + ["accounts", "list"])
                    self.assertNotIn("local-cli-secret", listed.output)
                    self.assertEqual(json.loads(listed.stdout)[0]["account_id"], account_id)
                    rejected = runner.invoke(cli, ["--url", f"http://127.0.0.1:{port}", "--token", "incorrect",
                                                   "--json", "logs", "follow"])
                    self.assertEqual(rejected.exit_code, 1)
                    self.assertEqual(json.loads(rejected.stdout)["error"]["code"], "cli_error")
                finally:
                    release_execution.set()
                    finish_execution.set()
                    server.should_exit = True
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())

    def test_generated_administrator_token_is_stable_and_not_a_cookie(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {}, clear=True):
            root = Path(temporary)
            first = load_admin_token(root)
            self.assertGreaterEqual(len(first), 40)
            self.assertEqual(load_admin_token(root), first)
            self.assertEqual((root / "admin.token").read_text().strip(), first)


if __name__ == "__main__":
    unittest.main()
