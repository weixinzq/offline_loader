"""Linux process acceptance: signals, active SSE and restart without replay."""
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import build_opener, ProxyHandler, Request


@unittest.skipUnless(sys.platform == "linux", "POSIX service signal acceptance")
class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.process = None
        self.logs = []
        self.client = build_opener(ProxyHandler({}))

    def tearDown(self):
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        for stream in self.logs:
            stream.close()
        self.temporary.cleanup()

    def start(self, *, production=False):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/api/v1"
        log = (self.directory / f"process-{len(self.logs)}.log").open("w+")
        self.logs.append(log)
        args = ([str(Path(sys.executable).with_name("aola")), "--token", "process-fixture-token", "serve"] if production else
                ["-m", "tests.test_process_lifecycle", "--fixture"])
        self.process = subprocess.Popen([sys.executable, "-u", *args,
            "--data-dir", str(self.directory / "service"), "--port", str(port)],
            cwd=self.directory if production else Path(__file__).resolve().parents[1],
            stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                log.seek(0)
                self.fail("Fixture exited before startup: " + log.read())
            try:
                self.request("GET", "/health")
                return
            except OSError:
                time.sleep(0.05)
        self.fail("Service did not become ready")

    def request(self, method, path, body=None):
        request = Request(self.url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer process-fixture-token", "Content-Type": "application/json"})
        with self.client.open(request, timeout=5) as response:
            return json.load(response)

    def running_task(self):
        account = self.request("POST", "/accounts", {
            "label": "process-fixture", "account": "offline-process@example.invalid", "password": "fixture-password"})
        task = self.request("POST", "/tasks", {"account_ids": [account["account_id"]],
            "steps": [{"kind": "messages", "messages": [{"id": -1, "cmd": "#time", "param": {"seconds": 60}}]}]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.request("GET", "/sessions")[0]["state"] == "ready":
                return account, task
            time.sleep(0.02)
        self.fail("Mock session did not initialize")

    def assert_restored_without_replay(self, account, task, *, abrupt=False):
        self.start()
        self.assertEqual(self.request("GET", "/accounts")[0]["account_id"], account["account_id"])
        self.assertEqual(self.request("GET", "/sessions")[0]["state"], "offline")
        record = self.request("GET", f"/tasks/{task['task_id']}")
        self.assertEqual(record["state"], "interrupted")
        # SIGKILL prevents the in-process write observer from persisting its final
        # result. Recovery must retain uncertainty rather than invent zero writes.
        expected = {"status": "unknown", "count": None} if abrupt else {"status": "not_attempted", "count": 0}
        self.assertEqual(record["account_results"][0]["network_write"], expected)
        self.process.terminate()
        self.process.wait(timeout=15)
        self.assertIn(self.process.returncode, (0, -signal.SIGTERM))
        self.logs[-1].seek(0)
        self.assertIn('"mock_opens": 0', self.logs[-1].read())

    def test_sigterm_with_active_sse_finishes_cleanup_and_restores_offline(self):
        self.start()
        account, task = self.running_task()
        request = Request(self.url + "/events", headers={"Authorization": "Bearer process-fixture-token"})
        with self.client.open(request, timeout=20) as events:
            self.assertEqual(events.readline(), b": connected\n")
            self.process.terminate()
            self.process.wait(timeout=15)
        self.assertIn(self.process.returncode, (0, -signal.SIGTERM))
        self.logs[0].seek(0)
        self.assertIn('"mock_active_after_shutdown": 0', self.logs[0].read())
        self.assert_restored_without_replay(account, task)

    def test_sigkill_restart_marks_interrupted_and_never_replays(self):
        self.start()
        account, task = self.running_task()
        self.process.kill()
        self.process.wait(timeout=5)
        self.assert_restored_without_replay(account, task, abrupt=True)

    def test_production_entry_and_separate_cli_share_http_state(self):
        self.start(production=True)
        self.request("POST", "/accounts", {
            "label": "offline-only", "account": "offline-production@example.invalid", "password": "fixture-password"})
        result = subprocess.run([sys.executable, str(Path(sys.executable).with_name("aola")), "--url", self.url.removesuffix("/api/v1"),
            "--token", "process-fixture-token", "--json", "accounts", "list"],
            cwd=self.directory, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]["label"], "offline-only")
        group = self.request("POST", "/egress-groups", {"name": "offline-group", "clash_policy": "offline-node"})
        result = subprocess.run([sys.executable, str(Path(sys.executable).with_name("aola")), "--url", self.url.removesuffix("/api/v1"),
            "--token", "process-fixture-token", "--json", "egress", "list"],
            cwd=self.directory, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]["group_id"], group["group_id"])
        self.assertIn("PROCESS-PATH", self.request("GET", "/egress-groups/rules")["yaml"])
        self.assertNotIn("password", result.stdout)
        self.process.terminate()
        self.process.wait(timeout=15)
        self.assertIn(self.process.returncode, (0, -signal.SIGTERM))


def run_fixture():
    import argparse
    import uvicorn
    from src.interfaces.http import create_app
    from src.service.application import Application
    from tests.test_service_integration import InMemoryGame

    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    options = parser.parse_args()
    game = InMemoryGame()
    application = Application(options.data_dir, connection_factory=game)
    close_application = application.close

    async def close_with_evidence():
        await close_application()
        print(json.dumps({"mock_opens": game.opens, "mock_active_after_shutdown": game.active}), flush=True)

    application.close = close_with_evidence
    uvicorn.run(create_app(options.data_dir, admin_token="process-fixture-token", application=application),
                host="127.0.0.1", port=options.port, timeout_graceful_shutdown=10, log_level="warning")


if __name__ == "__main__":
    if "--fixture" in sys.argv:
        run_fixture()
    else:
        unittest.main()
