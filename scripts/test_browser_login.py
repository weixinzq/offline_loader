"""Offline verification: all browser traffic stays on a local test server."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from playwright.async_api import async_playwright

from src.accounts import login
from src.accounts.browser_login import BrowserLoginStopped, _post_form
from src.accounts.manager import AccountConnection, AccountSpec


class BrowserLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root_patch = patch.object(login, "PROJECT_ROOT", Path(self.tmp.name))
        self.root_patch.start()
        self.requests = []
        self.completed = asyncio.Event()

        async def root(request):
            return web.Response(text="<html>Local login fixture</html>", content_type="text/html")

        async def role(request):
            self.requests.append((request.path, request.method, dict(await request.post())))
            return web.Response(text="0,123,测试角色", headers={"Set-Cookie": "role=ready; Path=/"})

        async def challenge(request):
            self.requests.append((request.path, request.method, dict(await request.post())))
            if request.cookies.get("verified") == "yes":
                assert request.cookies.get("role") == "ready"
                return web.Response(text="<r><c>ok</c><sid>test-sid</sid><u>123</u></r>", content_type="text/xml")
            return web.Response(text='''<html><body>
                <!-- TencentCaptcha: local fixture, no external SDK -->
                <button id="complete" onclick="fetch('/WafCaptcha', {method:'POST'}).then(() => location.reload())">Complete simulated verification</button>
                </body></html>''', content_type="text/html")

        async def verify(request):
            self.completed.set()
            return web.Response(text="ok", headers={"Set-Cookie": "verified=yes; Path=/"})

        app = web.Application()
        app.router.add_get("/", root)
        app.router.add_post("/roles", role)
        app.router.add_route("*", "/login", challenge)
        app.router.add_post("/WafCaptcha", verify)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel="msedge", headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        await self.page.goto(self.base)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        await self.runner.cleanup()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_manual_challenge_resumes_post_in_same_context(self):
        # Exercise the real browser orchestration while substituting only launch
        # and the user's click on a local fixture (never a real CAPTCHA).
        with patch.object(type(self.playwright.chromium), "launch", AsyncMock(return_value=self.browser)):
            task = asyncio.create_task(login.http_login(
                "test-account", "test-password", 0,
                self.base + "/login", self.base + "/roles", browser_verification=True,
            ))
            try:
                async with asyncio.timeout(15):
                    while True:
                        contexts = [c for c in self.browser.contexts if c != self.context]
                        if contexts and contexts[0].pages:
                            page = contexts[0].pages[0]
                            break
                        await asyncio.sleep(0.05)
                    await page.wait_for_url(self.base + "/login", wait_until="domcontentloaded")
                    await page.locator("#complete").wait_for()
                    self.assertFalse(task.done())
                    await page.locator("#complete").click()
                    result = await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(result.success)
        self.assertEqual(result.user_id, "123")
        posts = [r for r in self.requests if r[0] == "/login"]
        self.assertEqual([r[1] for r in posts], ["POST", "POST", "POST"])
        self.assertEqual(posts[0][2], posts[1][2])
        self.assertEqual(posts[1][2], posts[2][2])
        self.assertEqual(posts[0][2]["password"], login.md5_password("test-password"))
        files = list(Path(self.tmp.name).rglob("*.json"))
        self.assertEqual(len(files), 1)  # Initial HTTP challenge was saved.
        self.assertEqual(json.loads(files[0].read_text("utf-8"))["stage"], "login")

    async def test_close_and_timeout_save_evidence(self):
        for close in (False, True):
            page = await self.context.new_page()
            await page.goto(self.base)
            task = asyncio.create_task(_post_form(page, self.base + "/login", {},
                                                 "browser_login", "test-account", "test-password",
                                                 timeout=5 if close else 0.8))
            if close:
                await page.locator("#complete").wait_for()
                await page.close()
            with self.assertRaises(BrowserLoginStopped) as caught:
                await task
            self.assertIn("窗口已关闭" if close else "超时", str(caught.exception))
        files = list(Path(self.tmp.name).rglob("*.json"))
        self.assertEqual(len(files), 2)
        for path in files:
            self.assertEqual(json.loads(path.read_text("utf-8"))["http_status"], 200)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_verification_failure_is_not_retried(self):
        opener = AsyncMock(side_effect=BrowserLoginStopped("验证窗口已关闭"))
        connection = AccountConnection(AccountSpec("test", {}))
        self.assertFalse(await connection.connect(opener=opener, retry_delay=0))
        self.assertEqual(opener.await_count, 1)
        self.assertFalse(connection.retryable)


if __name__ == "__main__":
    unittest.main()
