"""Offline checks for conditional battle-entry message steps."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import scripts.double_auto_escape as script
from src.messaging.parser import SendMessage
from src.network.context import AppContext, MessageRouter
from src.scripting.composer import MessageBatchStep, execute_combination
from src.scripting.loader import ScriptExecutionError, discover_scripts


class DoubleAutoEscapeTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, opponents=(200,), specified=(), mode="immediate", after=True):
        logs = []
        waiting = asyncio.Event()

        class FakeSocket:
            connected = True

            def __init__(self):
                self.sent = []

            async def send_xt_message(self, ext_id, cmd, params):
                self.sent.append(cmd)
                if cmd == "after" and mode == "after_failure":
                    raise ConnectionError("test after failed")
                if cmd != "trigger":
                    return
                assert router._subscribers
                if mode == "rejected":
                    router.publish({"_cmd": 2303, "msg": "test entry rejected"})
                    return
                if mode == "timeout":
                    return
                router.publish({"_cmd": 2303, "battleId": 123, "pmmList": [
                    {"battleView": {"pmmId": player_id, "pmList": []}}
                    for player_id in (100, *opponents)
                ]})
                # A later update may omit the opponents present in the initial reply.
                router.publish({"_cmd": 2401, "battleUniqueId": 456, "pmmList": [
                    {"battleView": {"pmmId": 100, "pmList": []}}
                ]})
                router.publish({"_cmd": 2426})
                if mode not in {"delayed", "cancel", "disconnect"}:
                    router.publish({"_cmd": 2402, "pt": 0})

        socket = FakeSocket()
        router = MessageRouter(socket)

        def log(message):
            logs.append(message)
            if message == "前置步骤已提交，等待本次战斗完整就绪":
                waiting.set()

        context = AppContext(
            {}, SimpleNamespace(user_id="100"), None, socket, router, log
        )
        context.player_initialized = True

        async def fast_combination(*args, **kwargs):
            self.assertTrue(kwargs["preserve_current_battle"])
            return await execute_combination(*args, **kwargs, message_delay=0)

        with patch.multiple(
            script,
            BEFORE_STEPS=(MessageBatchStep((SendMessage(42, "trigger", {}),)),),
            AFTER_STEPS=(MessageBatchStep((SendMessage(42, "after", {}),)),) if after else (),
            SPECIFIED_OPPONENT_IDS=set(specified),
            WAIT_TIMEOUT_SECONDS=0.1,
            execute_combination=fast_combination,
        ):
            task = asyncio.create_task(script.run(context))
            if mode in {"delayed", "cancel", "disconnect"}:
                await asyncio.wait_for(waiting.wait(), 1)
                self.assertEqual(socket.sent, ["trigger"])
                self.assertFalse(task.done())
                if mode == "delayed":
                    router.publish({"_cmd": 2402, "pt": 0})
                elif mode == "cancel":
                    task.cancel()
                else:
                    socket.connected = False
                    router.disconnected.set()
                    router.publish({"_cmd": "wake"})
            try:
                results = await asyncio.wait_for(task, 2)
                error = None
            except (ScriptExecutionError, asyncio.CancelledError) as exc:
                results = getattr(exc, "send_results", ())
                error = exc

        self.assertFalse(router._subscribers)
        self.assertEqual([item.sequence for item in results], list(range(1, len(results) + 1)))
        return socket.sent, logs, error

    async def test_matching_id_skips_after(self):
        sent, logs, error = await self.exercise(specified=(200,))
        self.assertIsNone(error)
        self.assertEqual(sent, ["trigger"])
        self.assertTrue(any("均在指定集合中" in line for line in logs))

    async def test_unmatched_id_runs_after_with_immediate_replies(self):
        sent, logs, error = await self.exercise(specified=(300,))
        self.assertIsNone(error)
        self.assertEqual(sent, ["trigger", "after"])
        self.assertIn("参战对方 ID：[200]", logs)

    async def test_any_unmatched_opponent_triggers_after(self):
        sent, _, error = await self.exercise(opponents=(200, 300), specified=(200,))
        self.assertIsNone(error)
        self.assertEqual(sent, ["trigger", "after"])

    async def test_empty_set_triggers_after(self):
        sent, _, error = await self.exercise()
        self.assertIsNone(error)
        self.assertEqual(sent, ["trigger", "after"])

    async def test_unfilled_after_only_logs(self):
        sent, logs, error = await self.exercise(after=False)
        self.assertIsNone(error)
        self.assertEqual(sent, ["trigger"])
        self.assertTrue(any("后续消息尚未填写" in line for line in logs))

    async def test_missing_opponent_does_not_send_after(self):
        sent, _, error = await self.exercise(opponents=())
        self.assertIn("未读取到参战对方 ID", str(error))
        self.assertEqual(sent, ["trigger"])

    async def test_waits_for_complete_entry(self):
        sent, _, error = await self.exercise(mode="delayed")
        self.assertIsNone(error)
        self.assertEqual(sent, ["trigger", "after"])

    async def test_entry_errors_stop_after(self):
        for mode, message in (
            ("rejected", "test entry rejected"),
            ("timeout", "未确认本次战斗完整就绪"),
            ("disconnect", "连接断开"),
        ):
            with self.subTest(mode=mode):
                sent, _, error = await self.exercise(mode=mode)
                self.assertIn(message, str(error))
                self.assertEqual(sent, ["trigger"])

    async def test_cancellation_closes_subscription(self):
        sent, _, error = await self.exercise(mode="cancel")
        self.assertIsInstance(error, asyncio.CancelledError)
        self.assertEqual(sent, ["trigger"])

    async def test_after_failure_is_reported(self):
        sent, _, error = await self.exercise(mode="after_failure")
        self.assertIn("test after failed", str(error))
        self.assertEqual(sent, ["trigger", "after"])

    async def test_script_is_discoverable(self):
        self.assertTrue(any(
            item.module_name == "scripts.double_auto_escape" for item in discover_scripts()
        ))


if __name__ == "__main__":
    unittest.main()
