"""The service uses system sockets and never environment/application proxies."""
import os
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web

from src.accounts.login import http_login
from src.network.socket_client import GameSocket


class SystemNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_login_ignores_config_and_all_proxy_sources(self):
        requested = []

        async def handler(request):
            requested.append(request.path)
            params = await request.post()
            if request.path == "/roles":
                return web.Response(text="0,123456789,fixture,0")
            return web.Response(text=f'<r><c>ok</c><sid>mock-session</sid><d>{params["account"]}</d><u>123456789</u></r>')

        app = web.Application()
        app.router.add_post("/{path}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        environment = {key: "http://127.0.0.1:1" for key in
                       ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}
        environment.update(AOLA_PROXY_URL="socks5://127.0.0.1:1", NO_PROXY="", no_proxy="")
        try:
            with patch.dict(os.environ, environment), \
                 patch("src.accounts.login.load_user_config", side_effect=AssertionError("legacy config read")), \
                 patch("src.network.proxy.process_proxy", side_effect=AssertionError("application proxy read")):
                result = await http_login("offline-network-fixture", "offline-password",
                    network_mode="system", proxy_url="socks5://127.0.0.1:1",
                    register_url=f"http://127.0.0.1:{port}/roles",
                    login_url=f"http://127.0.0.1:{port}/login")
            self.assertTrue(result.success, result.error_msg)
            self.assertEqual(requested, ["/roles", "/login"])
        finally:
            await runner.cleanup()

    async def test_websocket_explicitly_disables_library_proxy_discovery(self):
        transport = AsyncMock()
        connect = AsyncMock(return_value=transport)
        with patch("src.network.proxy.process_proxy", side_effect=AssertionError("application proxy read")), \
             patch("websockets.connect", connect):
            socket = GameSocket(proxy_url="socks5://127.0.0.1:1", network_mode="system")
            self.assertTrue(await socket.connect_ws("offline.invalid", 443))
            self.assertIsNone(socket.proxy_url)
            self.assertIn("proxy", connect.call_args.kwargs)
            self.assertIsNone(connect.call_args.kwargs["proxy"])
            await socket.close()

    async def test_tcp_uses_system_connection_without_proxy(self):
        reader, writer = AsyncMock(), AsyncMock()
        reader.readexactly.return_value = b"\0"
        open_connection = AsyncMock(return_value=(reader, writer))
        with patch("src.network.proxy.process_proxy", side_effect=AssertionError("application proxy read")), \
             patch("asyncio.open_connection", open_connection):
            socket = GameSocket(proxy_url="socks5://127.0.0.1:1", network_mode="system")
            self.assertTrue(await socket.connect_tcp("offline.invalid", 12345))
            open_connection.assert_awaited_once_with("offline.invalid", 12345)
            # StreamWriter.close is synchronous.
            writer.close = lambda: None
            await socket.close()
