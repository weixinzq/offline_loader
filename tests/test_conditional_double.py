"""Conditional matching, entry races, and recovery across outer repetitions."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import scripts.double_auto_escape as escape
import scripts.main_double_auto as main
from src.messaging import battle_control
from src.messaging.parser import SendMessage
from src.network.context import AppContext, MessageRouter
from src.scripting.composer import MessageBatchStep, ScriptStep, execute_combination
from src.scripting.loader import InteractionScript
from src.service.execution import ScriptContext


class ConditionalDoubleTests(unittest.IsolatedAsyncioTestCase):
    async def run_scenarios(self, scenarios, module=main):
        sent, logs = [], []
        attempt = 0
        cancelling = asyncio.Event()

        def enter(ready=True):
            router.publish({"_cmd": "2303", "battleId": attempt, "pmmList": [
                {"battleView": {"pmmId": 100, "slotId": 7, "pmList": []}},
            ]})
            router.publish({"_cmd": "2401", "battleUniqueId": 1000 + attempt})
            router.publish({"_cmd": "2426"})
            if ready:
                router.publish({"_cmd": "2402", "pt": 0})

        class Socket:
            connected = True

            async def send_xt_message(self, ext_id, cmd, params):
                nonlocal attempt
                if cmd == "SA231229_match":
                    attempt += 1
                sent.append((attempt, ext_id, cmd, params))
                mode = scenarios[attempt - 1]
                if cmd == "SA231229_match":
                    if mode in {"match_timeout", "disconnect"}:
                        if mode == "disconnect":
                            self.connected = False
                            router.disconnected.set()
                        return
                    if mode == "direct_entry":
                        enter()
                        return
                    target = next(iter(module.SPECIFIED_PLAYER_IDS))
                    ids = [100, target] if mode in {"hit", "entry_timeout", "wait_timeout", "write_failure", "prepare_required", "prepare_failure"} else [100, 42424242]
                    router.publish({"_cmd": "SA231229_CmdMatch", "ids": [] if mode in {"bad_ids", "bad_ids_entered"} else ids})
                    if mode in {"hit", "miss_entered", "bad_ids_entered", "wait_timeout", "write_failure"}:
                        enter()
                    elif mode == "miss_partial_entry":
                        enter(ready=False)
                elif cmd == "SA231229_dpmr":
                    assert ext_id == 42 and params == {}
                    if mode == "prepare_failure":
                        raise OSError("test prepare write failure")
                    if mode == "prepare_required":
                        enter()
                elif cmd == "SA231229_cancle":
                    cancelling.set()
                    if mode in {"cancel_race", "cancel_race_without_ack"}:
                        enter()
                    if mode == "cancel_race_partial":
                        enter(ready=False)
                    if mode not in {"cancel_timeout", "cancel_race_without_ack", "manual_stop"}:
                        router.publish({"_cmd": cmd, "r": 0 if mode == "cancel_rejected" else 1})
                elif cmd == "1409":
                    if mode == "write_failure":
                        raise OSError("test write failure")
                    assert context.battle.entry_ready
                    assert params == {"useAiType": 1}
                    if mode != "wait_timeout":
                        router.publish({"_cmd": "2403", "battleUniqueId": 1000 + attempt})
                elif cmd == "1404" and mode in {"hit", "prepare_required"}:
                    assert context.battle.entry_ready
                    router.publish({"_cmd": "2403", "battleUniqueId": 1000 + attempt})

        socket = Socket()
        router = MessageRouter(socket)
        context = AppContext({}, SimpleNamespace(user_id="100"), None, socket, router, logs.append)
        context.player_initialized = True

        async def fast_combination(*args, **kwargs):
            kwargs.setdefault("message_delay", 0)
            return await execute_combination(*args, **kwargs)

        async def run_script(ctx):
            return await module.run(ScriptContext(ctx))

        steps = (
            ScriptStep(InteractionScript(module.__name__, "conditional test", "", run_script)),
            MessageBatchStep((SendMessage(42, "after-round", {}),)),
        )
        with patch.multiple(battle_control, MATCH_TIMEOUT_SECONDS=.02, ENTRY_TIMEOUT_SECONDS=.02, WAIT_TIMEOUT_SECONDS=.02), patch.multiple(
            module, WAIT_TIMEOUT_SECONDS=.02, execute_combination=fast_combination
        ):
            task = asyncio.create_task(execute_combination("test", context, steps, repetitions=len(scenarios), repeat_interval=0, message_delay=0))
            if "manual_stop" in scenarios:
                await asyncio.wait_for(cancelling.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                result = None
            else:
                result = await task
        self.assertFalse(router._subscribers)
        return result, sent, logs

    async def test_target_set_exactly_matches_the_300_exported_ids(self):
        path = Path(__file__).resolve().parents[1] / "build/role-id-300-20261020T113706/id-300.txt"
        ids = {int(value) for value in path.read_text(encoding="utf-8").splitlines()} - {75238435}
        self.assertEqual(len(ids), 299)
        self.assertEqual(main.SPECIFIED_PLAYER_IDS, ids)

    async def test_unmatched_idle_cancels_and_completes(self):
        result, sent, _ = await self.run_scenarios(["miss"])
        self.assertTrue(result.success)
        self.assertEqual([r[2] for r in sent], ["AHM230616_1", "SA231229_match", "SA231229_cancle", "after-round"])

    async def test_preparation_is_sent_after_matching_before_waiting_for_entry(self):
        for module, action in [(main, "1409"), (escape, "1404")]:
            with self.subTest(module=module.__name__):
                result, sent, _ = await self.run_scenarios(["prepare_required"], module=module)
                self.assertTrue(result.success)
                self.assertEqual([r[2] for r in sent], ["AHM230616_1", "SA231229_match", "SA231229_dpmr", action, "after-round"])
                self.assertEqual([r.sequence for r in result.send_results], list(range(1, len(result.send_results) + 1)))

    async def test_unmatched_escape_does_not_prepare(self):
        result, sent, _ = await self.run_scenarios(["miss"], module=escape)
        self.assertTrue(result.success)
        self.assertNotIn("SA231229_dpmr", [r[2] for r in sent])

    async def test_prepared_entry_timeout_skips_and_next_prepared_attempt_works(self):
        for module in [main, escape]:
            with self.subTest(module=module.__name__):
                result, sent, _ = await self.run_scenarios(["entry_timeout", "prepare_required"], module=module)
                self.assertTrue(result.success)
                self.assertEqual((result.attempted_repetitions, result.completed_repetitions, result.skipped_repetitions), (2, 1, 1))
                self.assertEqual([r[0] for r in sent if r[2] == "SA231229_dpmr"], [1, 2])
                self.assertNotIn("after-round", [r[2] for r in sent if r[0] == 1])

    async def test_preparation_write_failure_remains_fatal(self):
        result, sent, _ = await self.run_scenarios(["prepare_failure", "prepare_required"])
        self.assertFalse(result.success)
        self.assertFalse(any(r[0] == 2 for r in sent))

    async def test_matched_or_already_entered_uses_1409_after_full_readiness(self):
        for mode in ["hit", "miss_entered", "bad_ids_entered", "direct_entry"]:
            with self.subTest(mode=mode):
                result, sent, _ = await self.run_scenarios([mode])
                self.assertTrue(result.success)
                self.assertEqual([r[2] for r in sent], ["AHM230616_1", "SA231229_match", "1409", "after-round"])

    async def test_entry_during_cancel_enables_auto_even_without_cancel_ack(self):
        for mode in ["cancel_race", "cancel_race_without_ack"]:
            with self.subTest(mode=mode):
                result, sent, _ = await self.run_scenarios([mode])
                self.assertTrue(result.success)
                self.assertEqual([r[2] for r in sent], ["AHM230616_1", "SA231229_match", "SA231229_cancle", "1409", "after-round"])

    async def test_recoverable_errors_skip_remainder_then_next_attempt_works(self):
        for mode in ["bad_ids", "cancel_rejected", "cancel_timeout", "match_timeout", "entry_timeout", "wait_timeout", "miss_partial_entry", "cancel_race_partial"]:
            with self.subTest(mode=mode):
                result, sent, _ = await self.run_scenarios([mode, "hit"])
                self.assertTrue(result.success)
                self.assertEqual((result.attempted_repetitions, result.completed_repetitions, result.skipped_repetitions), (2, 1, 1))
                self.assertNotIn("after-round", [r[2] for r in sent if r[0] == 1])
                self.assertIn("after-round", [r[2] for r in sent if r[0] == 2])
                slots = [r[3]["reqPSId"] for r in sent if r[0] == 1 and r[2] == "1404"]
                self.assertEqual(slots, [7] if mode in {"wait_timeout", "miss_partial_entry", "cancel_race_partial"} else [0, 11])

    async def test_escape_entry_races_recover_and_allow_next_outer_attempt(self):
        for mode in ["miss_entered", "cancel_race", "cancel_race_without_ack"]:
            with self.subTest(mode=mode):
                result, sent, _ = await self.run_scenarios([mode, "miss"], module=escape)
                self.assertTrue(result.success)
                self.assertEqual((result.attempted_repetitions, result.completed_repetitions, result.skipped_repetitions), (2, 1, 1))
                self.assertEqual([r[3] for r in sent if r[0] == 1 and r[2] == "1404"], [{"turn": 0, "reqPSId": 7}])
                self.assertNotIn("after-round", [r[2] for r in sent if r[0] == 1])

    async def test_repeated_recoveries_do_not_stop_the_account(self):
        result, _, _ = await self.run_scenarios(["cancel_rejected"] * 3 + ["hit"])
        self.assertTrue(result.success)
        self.assertEqual((result.attempted_repetitions, result.completed_repetitions, result.skipped_repetitions), (4, 1, 3))

    async def test_disconnect_and_actual_write_failure_still_stop(self):
        for mode in ["disconnect", "write_failure"]:
            with self.subTest(mode=mode):
                result, sent, _ = await self.run_scenarios([mode, "hit"])
                self.assertFalse(result.success)
                self.assertFalse(any(r[0] == 2 for r in sent))

    async def test_manual_stop_during_cancel_does_not_recover_or_start_next_attempt(self):
        _, sent, _ = await self.run_scenarios(["manual_stop", "hit"])
        self.assertFalse(any(r[2] in {"1404", "1409", "after-round"} for r in sent))
        self.assertFalse(any(r[0] == 2 for r in sent))


if __name__ == "__main__":
    unittest.main()
