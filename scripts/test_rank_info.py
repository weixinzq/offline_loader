"""Offline checks for rank conversion and the routed query response."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from scripts import rank_info
from src.network.context import MessageRouter
from src.scripting.loader import discover_scripts


class RankInfoTests(unittest.IsolatedAsyncioTestCase):
    def test_rank_boundaries(self):
        cases = (
            (0, ("未定级", 0)),
            (1, ("新手", 1)),
            (38, ("钻石军长V", 3)),
            (39, ("钻石军长IV", 1)),
            (40, ("钻石军长IV", 2)),
            (41, ("钻石军长IV", 3)),
            (42, ("钻石军长III", 1)),
            (72, ("传奇大师I", 5)),
            (73, ("王者英雄", 1)),
            (122, ("王者英雄", 50)),
            (123, ("荣耀王者", 1)),
        )
        for star_num, expected in cases:
            with self.subTest(star_num=star_num):
                self.assertEqual(rank_info.parse_rank(star_num), expected)

    def test_script_discovery(self):
        script = next(
            item for item in discover_scripts()
            if item.module_name == "scripts.rank_info"
        )
        self.assertEqual(script.name, "段位信息")

    def make_context(self, response):
        socket = SimpleNamespace()
        router = MessageRouter(socket)
        logs = []

        async def send(ext_id, command, params):
            self.assertTrue(router._subscribers, "必须先订阅再发送")
            router.publish({"_cmd": "unrelated"})
            if response is not None:
                router.publish(response)

        socket.send_xt_message = AsyncMock(side_effect=send)
        context = SimpleNamespace(
            user_id="75238543", socket=socket, messages=router,
            wait_for_player_initialization=AsyncMock(), log=logs.append,
        )
        return context, logs

    async def test_captured_history(self):
        context, logs = self.make_context({
            "r": 1, "_cmd": "71_29", "sai": [{
                "tt": 254, "s": 40, "t": 1, "w": 366, "zr": 32,
                "y": 2026, "z": 1015, "m": 10, "wt": 93, "tr": 252,
            }],
        })
        await rank_info.run(context)
        context.wait_for_player_initialization.assert_awaited_once()
        context.socket.send_xt_message.assert_awaited_once_with(
            12, "71_29", {"vUId": 75238543}
        )
        self.assertEqual(logs, [
            "2026年10月 多亚比排位模式：钻石军长IV，2星；"
            "场次254，胜场93，胜率36.6%"
        ])
        self.assertFalse(context.messages._subscribers)

    async def test_empty_history(self):
        context, logs = self.make_context({"r": 1, "_cmd": "71_29", "sai": []})
        await rank_info.run(context)
        self.assertEqual(logs, ["段位信息：暂无排位赛季记录"])

    async def test_rejected_query(self):
        context, logs = self.make_context({"r": 0, "_cmd": "71_29"})
        with self.assertRaisesRegex(RuntimeError, "段位查询失败"):
            await rank_info.run(context)
        self.assertEqual(logs, [])
        self.assertFalse(context.messages._subscribers)

    async def test_timeout_closes_subscription(self):
        context, logs = self.make_context(None)
        with patch.object(rank_info, "RESPONSE_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(asyncio.TimeoutError):
                await rank_info.run(context)
        self.assertEqual(logs, [])
        self.assertFalse(context.messages._subscribers)


if __name__ == "__main__":
    unittest.main()
