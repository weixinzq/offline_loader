"""Offline CLI configuration, lifecycle, proxy, and Linux process tests."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
import yaml

from scripts._fake_mihomo import socks_connection
from src.accounts.login import http_login
from src.cli_jobs import alive
from src.network.context import BattleLifecycle, MessageRouter
from src.network.socket_client import GameSocket
from src.scripting.jobs import load_job, mihomo_config
from src.scripting.job_worker import SecretFilter, run_accounts
from src.scripting.composer import ScriptStep
from src.scripting.loader import InteractionScript


def make_job(directory):
    accounts = [{"label": f"label-{i}", "account": f"fake-{i}", "password": "test-secret", "char_id": 0}
                for i in range(1, 7)]
    (directory / "accounts.json").write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
    (directory / "nodes.yaml").write_text(yaml.safe_dump({"proxies": [{"name": "A", "type": "socks5", "server": "127.0.0.1", "port": 9999}]}), encoding="utf-8")
    (directory / "binary").touch()
    (directory / "binary").chmod(0o700)
    raw = {"accounts_file": "accounts.json", "clash": {"binary": "binary", "config": "nodes.yaml"},
           "workers": [{"id": "first", "accounts": {"ranges": [[1, 2]], "labels": ["label-2"]},
                        "proxy": {"port": 17891, "node": "A"}, "mode": "bounded", "concurrency": 2,
                        "repetitions": 2, "repeat_interval": 0, "message_delay": 0,
                        "steps": [{"kind": "messages", "messages": [
                            {"id": -1, "cmd": "#time", "param": {"seconds": 0.01}}]}]}]}
    return raw


class JobConfigTests(unittest.TestCase):
    def test_selection_validation_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = make_job(root)
            def load(value):
                (root / "tasks.json").write_text(json.dumps(value), encoding="utf-8")
                return load_job(root / "tasks.json")
            before = set(root.iterdir())
            job = load(raw)
            self.assertEqual([a["index"] for a in job["workers"][0]["accounts"]], [1, 2])
            self.assertEqual(set(root.iterdir()) - before, {root / "tasks.json"})
            cases = []
            for field, value in (("concurrency", 0), ("repetitions", True), ("mode", "bad"), ("repeat_interval", -1)):
                candidate = deepcopy(raw); candidate["workers"][0][field] = value; cases.append(candidate)
            candidate = deepcopy(raw); candidate["workers"][0]["accounts"]["ranges"] = [[1, 99]]; cases.append(candidate)
            candidate = deepcopy(raw); candidate["workers"][0]["accounts"]["labels"] = ["missing"]; cases.append(candidate)
            candidate = deepcopy(raw); candidate["workers"][0]["proxy"]["node"] = "missing"; cases.append(candidate)
            candidate = deepcopy(raw); candidate["workers"][0]["steps"] = [{"kind": "script", "module": "os"}]; cases.append(candidate)
            for candidate in cases:
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    load(candidate)
            second = deepcopy(raw["workers"][0]); second["id"] = "second"; second["accounts"] = {"ranges": [[3, 3]]}
            candidate = deepcopy(raw); candidate["workers"].append(second)
            with self.assertRaisesRegex(ValueError, "端口"):
                load(candidate)
            second["proxy"]["port"] = 17892
            accounts = json.loads((root / "accounts.json").read_text())
            accounts["accounts"][2]["account"] = accounts["accounts"][0]["account"]
            accounts["accounts"][2]["char_id"] = 1
            (root / "accounts.json").write_text(json.dumps(accounts), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "序号 1 与 3"):
                load(candidate)

    def test_file_messages_node_isolation_and_redaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); raw = make_job(root)
            (root / "messages.txt").write_text('#time=0|#wait|#send={"id":42,"cmd":"test","param":{"v":"a|b"}}|', encoding="utf-8")
            raw["workers"][0]["steps"] = [{"kind": "messages", "file": "messages.txt"}]
            (root / "tasks.json").write_text(json.dumps(raw), encoding="utf-8")
            job = load_job(root / "tasks.json")
            self.assertEqual([m["cmd"] for m in job["workers"][0]["steps"][0]["messages"]], ["#time", "#wait", "test"])
            config = mihomo_config(job, root)
            self.assertEqual(config["listeners"][0]["listen"], "127.0.0.1")
            self.assertNotIn("tun", config)
            filt = SecretFilter(job)
            self.assertNotIn("test-secret", filt.clean("test-secret fake-1"))
            self.assertEqual(filt.data({"n": 1})["n"], 1)
            self.assertEqual(filt.data(({"error": "test-secret"},))[0]["error"], "<redacted>")
            self.assertEqual(filt.data({"sid": "unknown-session"})["sid"], "<redacted>")
            import src.network.context as context_module
            with patch.object(context_module, "_battle_receive_log_path", None), patch.object(context_module, "_battle_receive_record_filter", None):
                trace = context_module.configure_battle_receive_logging(root, record_filter=filt.data)
                context_module._append_battle_receive_trace({"_cmd": "2401", "sid": "unknown-session", "message": "test-secret"})
                self.assertNotIn("test-secret", trace.read_text(encoding="utf-8"))
                self.assertNotIn("unknown-session", trace.read_text(encoding="utf-8"))
            nodes = {"proxies": [], "proxy-groups": [{"name": "A", "type": "select", "proxies": ["DIRECT"]}]}
            (root / "nodes.yaml").write_text(yaml.safe_dump(nodes), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "直连"):
                load_job(root / "tasks.json")


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def scenario(self, mode, blocked=False, cancel=False, account_failure=False):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary); raw = make_job(directory)
            raw["workers"][0]["accounts"] = {"ranges": [[1, 6]]}
            (directory / "tasks.json").write_text(json.dumps(raw), encoding="utf-8")
            worker = load_job(directory / "tasks.json")["workers"][0]; worker["mode"] = mode
            tracker = {"active": 0, "peak": 0, "started": 0, "finished": 0}
            class Connection:
                def __init__(self, spec):
                    self.spec, self.context = spec, None
                    self.last_error, self.login_block_reason, self.retryable = "", "", True
                @property
                def online(self): return self.context is not None
                async def connect(self, retries):
                    tracker["started"] += 1
                    await asyncio.sleep(0.01)
                    if blocked and self.spec.label == "label-1":
                        self.last_error, self.login_block_reason = "captcha", "captcha"
                        return False
                    tracker["active"] += 1; tracker["peak"] = max(tracker["peak"], tracker["active"])
                    sock = SimpleNamespace(connected=True)
                    self.context = SimpleNamespace(socket=sock, messages=MessageRouter(sock), battle=BattleLifecycle(),
                        wait_for_player_initialization=AsyncMock(), assert_automation_allowed=lambda: None,
                        log=lambda text: None, user_id=self.spec.label, login_result=SimpleNamespace(session_id="fake-session"))
                    return True
                async def disconnect(self):
                    if self.context:
                        await asyncio.sleep(0.005)
                        tracker["active"] -= 1; tracker["finished"] += 1; self.context = None
            async def legacy(context):
                if context.user_id == "label-2":
                    raise RuntimeError("ordinary fixture failure")
                await asyncio.sleep(0.01)
            steps = (ScriptStep(InteractionScript("fixture", "legacy", "", legacy)),)
            with patch("src.scripting.job_worker.build_steps", return_value=steps) if account_failure else nullcontext():
                task = asyncio.create_task(run_accounts(worker, directory / "results", SecretFilter(worker), connection_factory=Connection))
                if cancel:
                    await asyncio.sleep(0.025); task.cancel()
                    with self.assertRaises(asyncio.CancelledError): await task
                else:
                    code = await task
                    self.assertEqual(code, 2 if blocked or account_failure else 0)
            self.assertEqual(tracker["active"], 0)
            rows = json.loads((directory / "results/accounts.json").read_text(encoding="utf-8"))["accounts"]
            if not blocked and not cancel:
                self.assertTrue(all(r["completed_repetitions"] == 2 for r in rows if not account_failure or r["index"] != 2))
                self.assertEqual(tracker["finished"], 6)
                self.assertEqual(tracker["peak"], 2 if mode == "bounded" else 6)
            if blocked: self.assertLessEqual(tracker["started"], 2)
            if account_failure:
                self.assertEqual([r["state"] for r in rows], ["completed", "failed", "completed", "completed", "completed", "completed"])
                self.assertEqual(rows[1]["completed_repetitions"], 0)
                self.assertFalse((directory / "results/send.jsonl").exists())
                self.assertTrue(all("legacy scripts" in r["send_details"] for r in rows))

    async def test_bounded_and_all_online(self):
        await self.scenario("bounded"); await self.scenario("all_online")

    async def test_refusal_and_cancel_cleanup(self):
        for mode in ("bounded", "all_online"):
            await self.scenario(mode, blocked=True)
            await self.scenario(mode, cancel=True)

    async def test_single_failure_and_legacy_missing_details(self):
        for mode in ("bounded", "all_online"):
            await self.scenario(mode, account_failure=True)


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = [[], []]
        self.servers = [await asyncio.start_server(lambda r, w, events=e: socks_connection(r, w, events), "127.0.0.1", 0) for e in self.seen]
        self.urls = [f"socks5://127.0.0.1:{s.sockets[0].getsockname()[1]}" for s in self.servers]
    async def asyncTearDown(self):
        for server in self.servers:
            server.close(); await server.wait_closed()

    async def test_http_role_and_login_both_use_each_proxy(self):
        requested = []
        async def handler(request):
            requested.append(request.path)
            params = await request.post()
            if request.path == "/roles": return web.Response(text="0,123456789,fixture,0")
            return web.Response(text=f'<r><c>ok</c><sid>fake-session</sid><d>{params["account"]}</d><u>123456789</u></r>')
        app = web.Application(); app.router.add_post('/{path}', handler)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            for index, url in enumerate(self.urls):
                result = await http_login("fake-login", "fake-password", proxy_url=url,
                    register_url=f"http://game.invalid:{port}/roles", login_url=f"http://game.invalid:{port}/login")
                self.assertTrue(result.success, result.error_msg)
                self.assertEqual(self.seen[index], [("game.invalid", port)])  # Both POSTs share the HTTP connection.
            with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"AOLA_DIAGNOSTIC_DIR": temporary}):
                result = await http_login("fake-login", "fake-password", proxy_url="socks5://127.0.0.1:1",
                    register_url=f"http://127.0.0.1:{port}/roles", login_url=f"http://127.0.0.1:{port}/login")
                self.assertFalse(result.success)
                self.assertEqual(len(requested), 4)
        finally: await runner.cleanup()

    async def test_tcp_and_no_direct_fallback(self):
        async def handler(reader, writer):
            writer.write(b"<policy/>\0"); await writer.drain()
            await reader.read(); writer.close()
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            sock = GameSocket(proxy_url=self.urls[0])
            self.assertTrue(await sock.connect_tcp("game.invalid", port)); await sock.close()
            self.assertEqual(self.seen[0], [("game.invalid", port)])
            sock = GameSocket(proxy_url="socks5://127.0.0.1:1")
            with patch("src.network.proxy.proxy_socket", side_effect=ConnectionError("proxy down")), patch("asyncio.open_connection", new_callable=AsyncMock) as direct:
                self.assertFalse(await sock.connect_tcp("127.0.0.1", port))
                direct.assert_not_called()
        finally: server.close(); await server.wait_closed()

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture requires existing openssl")
    async def test_wss_uses_same_proxy_without_environment_override(self):
        import websockets
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=game.invalid", "-days", "1", "-keyout", str(root / "key.pem"), "-out", str(root / "cert.pem")], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(root / "cert.pem", root / "key.pem")
            async def handler(ws): await ws.wait_closed()
            async with websockets.serve(handler, "127.0.0.1", 0, ssl=context) as server:
                port = server.sockets[0].getsockname()[1]
                sock = GameSocket(proxy_url=self.urls[1])
                with patch.dict(os.environ, {"https_proxy": "http://127.0.0.1:1", "wss_proxy": "http://127.0.0.1:1"}):
                    self.assertTrue(await sock.connect_ws("game.invalid", port), sock.disconnect_reason)
                await sock.close()
                self.assertEqual(self.seen[1], [("game.invalid", port)])
                sock = GameSocket(proxy_url="socks5://127.0.0.1:1")
                with patch("src.network.proxy.proxy_socket", side_effect=ConnectionError("proxy down")), patch("websockets.connect") as direct:
                    self.assertFalse(await sock.connect_ws("127.0.0.1", port))
                    direct.assert_not_called()


@unittest.skip("Retired detached CLI entry; service process lifecycle is covered by tests.test_process_lifecycle")
class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_detach_status_stop_and_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); raw = make_job(root)
            fixture = root / "mihomo-fixture"
            fixture.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "_fake_mihomo.py").read_text(encoding="utf-8"), encoding="utf-8")
            fixture.chmod(0o700); raw["clash"]["binary"] = str(fixture)
            release, requested = asyncio.Event(), asyncio.Event()
            async def handler(request):
                requested.set(); await release.wait()
                return web.Response(status=403, text="fixture refusal")
            app = web.Application(); app.router.add_post('/roles', handler)
            runner = web.AppRunner(app); await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
            port = site._server.sockets[0].getsockname()[1]
            accounts = json.loads((root / "accounts.json").read_text())
            accounts["register_url"] = f"http://game.invalid:{port}/roles"
            (root / "accounts.json").write_text(json.dumps(accounts), encoding="utf-8")
            def free_port():
                import socket
                with socket.socket() as s:
                    s.bind(("127.0.0.1", 0)); return s.getsockname()[1]
            second = deepcopy(raw["workers"][0]); second["id"] = "second"; second["accounts"] = {"ranges": [[3, 3]]}
            raw["workers"].append(second)
            for worker in raw["workers"]: worker["proxy"]["port"] = free_port()
            task_file = root / "tasks.json"; task_file.write_text(json.dumps(raw), encoding="utf-8")
            cli = Path(__file__).resolve().parents[1] / "cli_main.py"
            async def call(*arguments):
                process = await asyncio.create_subprocess_exec(sys.executable, str(cli), *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                output, error = await asyncio.wait_for(process.communicate(), 35)
                self.assertEqual(process.returncode, 0, error.decode())
                return json.loads(output)
            runs = root / "runs"
            identity = None
            try:
                launched = await call("start", "--job", str(task_file), "--detach", "--runs-dir", str(runs))
                directory = runs / launched["run_id"]
                identity = json.loads((directory / "launch.json").read_text())
                await asyncio.wait_for(requested.wait(), 15)
                state = await call("status", "--run", launched["run_id"], "--runs-dir", str(runs))
                self.assertTrue(state["alive"])
                self.assertEqual(state["state"], "running")
                stopped = await call("stop", "--run", launched["run_id"], "--runs-dir", str(runs))
                self.assertEqual(stopped["state"], "stopped")
                self.assertFalse(alive(identity))
                for process in stopped["processes"]: self.assertFalse(alive(process))
                release.set(); requested.clear()
                launched = await call("start", "--job", str(task_file), "--detach", "--runs-dir", str(runs))
                directory = runs / launched["run_id"]; identity = json.loads((directory / "launch.json").read_text())
                for _ in range(100):
                    state = json.loads((directory / "run.json").read_text())
                    if state["state"] == "failed": break
                    await asyncio.sleep(0.1)
                self.assertEqual(state["state"], "failed")
                self.assertEqual(state["exit_code"], 2)
                for process in state["processes"]: self.assertFalse(alive(process))
                for worker in raw["workers"]:
                    content = "".join(p.read_text(encoding="utf-8") for p in (directory / worker["id"]).glob("*.log"))
                    self.assertNotIn("test-secret", content)
                    self.assertTrue((directory / worker["id"] / "accounts.json").exists())
                requested.clear(); release.clear()
                launched = await call("start", "--job", str(task_file), "--detach", "--runs-dir", str(runs))
                directory = runs / launched["run_id"]; identity = json.loads((directory / "launch.json").read_text())
                await asyncio.wait_for(requested.wait(), 15)
                import signal
                os.kill(identity["pid"], signal.SIGKILL)
                await asyncio.sleep(0.1)
                stopped = await call("stop", "--run", launched["run_id"], "--runs-dir", str(runs))
                self.assertEqual(stopped["state"], "stopped")
                for process in stopped["processes"]: self.assertFalse(alive(process))
            finally:
                release.set()
                if identity and alive(identity):
                    import signal
                    os.killpg(identity["pid"], signal.SIGTERM)
                await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
