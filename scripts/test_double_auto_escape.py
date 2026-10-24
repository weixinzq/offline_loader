"""Offline checks for matched-player branches; no game connections are opened."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.network.context import AppContext, MessageRouter
from src.scripting.composer import execute_combination
from src.messaging import battle_control
from src.scripting.loader import ScriptExecutionError, discover_scripts
from src.service.execution import ScriptContext

import scripts.double_auto_escape as script


class ConditionalMatchTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, ids=(100, 200), targets=(200,), mode="immediate", narrow=False):
        logs = []
        waiting = asyncio.Event()
        escaping = asyncio.Event()

        def entry(ready=True):
            router.publish({"_cmd": "2303", "battleId": 123, "pmmList": [
                {"battleView": {"pmmId": 100, "slotId": 11, "pmList": []}},
                {"battleView": {"pmmId": 200, "slotId": 0, "pmList": []}},
            ]})
            router.publish({"_cmd": "2401", "battleUniqueId": 456})
            router.publish({"_cmd": "2426"})
            if ready:
                router.publish({"_cmd": "2402", "pt": 0})

        class FakeSocket:
            connected = True
            def __init__(self):
                self.sent = []

            async def send_xt_message(self, ext_id, cmd, params):
                self.sent.append((ext_id, cmd, params))
                if cmd == "SA231229_match":
                    assert router._subscribers
                    if mode in {"match_timeout", "cancel_task", "disconnect"}:
                        return
                    router.publish({"_cmd": "irrelevant", "ids": [200]})
                    reply = {"_cmd": "SA231229_CmdMatch"}
                    if mode != "missing_ids":
                        reply["ids"] = list(ids)
                    router.publish(reply)
                    if mode == "entry_rejected":
                        router.publish({"_cmd": "2303", "msg": "test entry rejected"})
                    elif mode == "entered_before_cancel" or (200 in ids and mode != "entry_timeout"):
                        entry(ready=mode != "delayed_entry")
                elif cmd == "SA231229_cancle":
                    if mode != "cancel_timeout":
                        router.publish({"_cmd": cmd, "r": 0 if mode == "cancel_rejected" else 1})
                elif cmd == "1404":
                    if mode in {"immediate", "delayed_entry", "delayed_end", "escape_rejected"} and 200 in ids:
                        assert context.battle.entry_ready
                    escaping.set()
                    if mode == "escape_rejected":
                        router.publish({"_cmd": "2414", "msg": "test escape rejected"})
                    elif mode != "delayed_end":
                        router.publish({"_cmd": "2403", "battleUniqueId": 456})

        socket = FakeSocket()
        router = MessageRouter(socket)
        def log(message):
            logs.append(message)
            if "等待本次战斗完整就绪" in message or "等待 SA231229_CmdMatch" in message:
                waiting.set()
        context = AppContext({}, SimpleNamespace(user_id="100"), None, socket, router, log)
        context.player_initialized = True

        async def fast_combination(*args, **kwargs):
            return await execute_combination(*args, **kwargs, message_delay=0)

        with patch.multiple(battle_control, ENTRY_TIMEOUT_SECONDS=.1, MATCH_TIMEOUT_SECONDS=.1, WAIT_TIMEOUT_SECONDS=.1), patch.multiple(script,
            SPECIFIED_PLAYER_IDS=set(targets),
            WAIT_TIMEOUT_SECONDS=.1,
            execute_combination=fast_combination,
        ):
            task = asyncio.create_task(script.run(ScriptContext(context) if narrow else context))
            if mode in {"delayed_entry", "cancel_task", "disconnect"}:
                await asyncio.wait_for(waiting.wait(), 1)
                self.assertFalse(task.done())
                self.assertNotIn("1404", [row[1] for row in socket.sent])
                if mode == "delayed_entry":
                    router.publish({"_cmd": "2402", "pt": 0})
                elif mode == "cancel_task":
                    task.cancel()
                else:
                    socket.connected = False
                    router.disconnected.set()
                    router.publish({"_cmd": "wake"})
            elif mode == "delayed_end":
                await asyncio.wait_for(escaping.wait(), 1)
                await asyncio.sleep(0)
                self.assertFalse(task.done(), "Must wait for this battle's 2403")
                router.publish({"_cmd": "2403", "battleUniqueId": 455})
                await asyncio.sleep(0)
                self.assertFalse(task.done(), "Another battle's 2403 must not release the wait")
                router.publish({"_cmd": "2403", "battleUniqueId": 456})
            try:
                results, error = await asyncio.wait_for(task, 2), None
            except (ScriptExecutionError, battle_control.BattleRoundSkipped, asyncio.CancelledError) as exc:
                results, error = getattr(exc, "send_results", ()), exc
        self.assertFalse(router._subscribers)
        self.assertEqual([r.sequence for r in results], list(range(1, len(results) + 1)))
        return socket.sent, logs, results, error, context

    async def test_unmatched_cancels_without_waiting_for_entry_or_escaping(self):
        sent, logs, results, error, context = await self.exercise(ids=(100, 300))
        self.assertIsNone(error)
        self.assertEqual([row[:2] for row in sent], [(42, "AHM230616_1"), (42, "SA231229_match"), (42, "SA231229_cancle")])
        self.assertEqual(sent[-1][2], {})
        self.assertEqual(len(results), 3)
        self.assertEqual(context.battle.phase, "idle")
        self.assertTrue(any("确认取消匹配" in line for line in logs))

    async def test_matching_id_waits_and_escapes_with_actual_slot(self):
        sent, _, results, error, context = await self.exercise(narrow=True)
        self.assertIsNone(error)
        self.assertEqual([row[1] for row in sent], ["AHM230616_1", "SA231229_match", "1404"])
        self.assertEqual(sent[-1][2], {"turn": 0, "reqPSId": 11})
        self.assertEqual(results[-1].cmd, "#wait")
        self.assertEqual(context.battle.phase, "idle")

    async def test_any_specified_id_matches_even_with_other_players(self):
        sent, _, _, error, _ = await self.exercise(ids=(100, 200, 300), targets=(200, 400))
        self.assertIsNone(error)
        self.assertEqual(sent[-1][1], "1404")

    async def test_entry_not_ready_does_not_escape_early(self):
        sent, _, _, error, _ = await self.exercise(mode="delayed_entry")
        self.assertIsNone(error)
        self.assertEqual(sent[-1][1], "1404")

    async def test_escape_waits_for_this_battle_end(self):
        _, _, _, error, _ = await self.exercise(mode="delayed_end")
        self.assertIsNone(error)

    async def test_cancel_failure_or_timeout_recovers_the_round(self):
        for mode, expected in (("cancel_rejected", "未确认取消匹配成功"), ("cancel_timeout", "未收到 SA231229_cancle")):
            with self.subTest(mode=mode):
                sent, _, results, error, _ = await self.exercise(ids=(100, 300), mode=mode)
                self.assertIn(expected, str(error))
                self.assertIsInstance(error, battle_control.BattleRoundSkipped)
                self.assertEqual([row[2]["reqPSId"] for row in sent if row[1] == "1404"], [0, 11])
                self.assertEqual(len(results), 3)

    async def test_bad_or_missing_match_ids_never_choose_a_branch(self):
        for ids, mode in (((), "immediate"), ((100, True), "immediate"), ((100, "200"), "immediate"), ((100, 300), "missing_ids")):
            with self.subTest(ids=ids, mode=mode):
                sent, _, _, error, _ = await self.exercise(ids=ids, mode=mode)
                self.assertIn("缺少有效的 ids", str(error))
                self.assertIsInstance(error, battle_control.BattleRoundSkipped)
                self.assertEqual([row[1] for row in sent], ["AHM230616_1", "SA231229_match", "1404", "1404"])

    async def test_match_entry_and_escape_errors_stop_subsequent_steps(self):
        for mode, expected in (("match_timeout", "未确认本次匹配成功"), ("entry_timeout", "未确认本次战斗完整就绪"), ("entry_rejected", "test entry rejected"), ("escape_rejected", "test escape rejected"), ("disconnect", "连接已断开")):
            with self.subTest(mode=mode):
                sent, _, _, error, _ = await self.exercise(mode=mode)
                self.assertIn(expected, str(error))
                self.assertNotIn("SA231229_cancle", [row[1] for row in sent])

    async def test_cancellation_closes_subscription(self):
        sent, _, _, error, _ = await self.exercise(mode="cancel_task")
        self.assertIsInstance(error, asyncio.CancelledError)
        self.assertEqual(len(sent), 2)

    async def test_unmatched_but_already_entered_recovers_without_cancel(self):
        sent, _, _, error, _ = await self.exercise(ids=(100, 300), mode="entered_before_cancel")
        self.assertIn("已进入战斗", str(error))
        self.assertIsInstance(error, battle_control.BattleRoundSkipped)
        self.assertEqual([row[1] for row in sent], ["AHM230616_1", "SA231229_match", "1404"])
        self.assertEqual(sent[-1][2], {"turn": 0, "reqPSId": 11})

    async def test_missing_cancel_configuration_fails_before_sending(self):
        with patch.object(script, "CANCEL_STEPS", ()):
            with self.assertRaisesRegex(ValueError, "CANCEL_STEPS"):
                await script.run(SimpleNamespace())

    async def test_script_is_discoverable(self):
        self.assertTrue(any(item.module_name == "scripts.double_auto_escape" for item in discover_scripts()))


if __name__ == "__main__":
    unittest.main()
