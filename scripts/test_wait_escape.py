"""Offline checks for the initial opponent backpack and first soul slot."""
from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import scripts.wait_escape as script
from src.messaging.parser import SendMessage
from src.network.context import AppContext, MessageRouter
from src.scripting.composer import MessageBatchStep, execute_combination
from src.messaging import battle_control
from src.scripting.loader import ScriptExecutionError, discover_scripts


def pet(pet_id, slots=None):
    pcp = '<p />' if slots is None else f'<p gg="1#5" tt="{slots}" />'
    return {"pmSView": {"f": pet_id, "pcp": pcp}, "pmSId": 0}


class SoulSlotTests(unittest.TestCase):
    def test_user_example_reads_config_not_instance(self):
        self.assertEqual(script._first_soul_config_id(
            '<p tt="24949120;85;7:6#7:6#7:6#7:6|5672101;2;1:6#1:6#1:6#1:6" />'
        ), 85)

    def test_config_ids_and_second_slot(self):
        for config_id in (36, 27, 58):
            with self.subTest(config_id=config_id):
                self.assertEqual(script._first_soul_config_id(
                    f'<p tt="123;{config_id};7:6#2:6|456;85;1:6" />'
                ), config_id)
        self.assertEqual(script._first_soul_config_id('<p tt="58;85;36:6|9;27;1:6" />'), 85)

    def test_missing_or_empty_first_slot(self):
        for pcp in ('<p />', '<p tt="" />', '<p tt="|123;36;1:6" />'):
            with self.subTest(pcp=pcp):
                self.assertIsNone(script._first_soul_config_id(pcp))


class WaitEscapeTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, pets, *, mode="immediate", optional=True, own_pets=()):
        logs = []
        waiting = asyncio.Event()

        def player(player_id, roster):
            return {"battleView": {"pmmId": player_id, "pmList": list(roster)}}

        def publish_backpack():
            router.publish({"_cmd": 2304, "b": [player(200, pets)]})

        class FakeSocket:
            connected = True

            def __init__(self):
                self.sent = []

            async def send_xt_message(self, ext_id, cmd, params):
                self.sent.append(cmd)
                if cmd == "optional" and mode == "optional_failure":
                    raise ConnectionError("test optional failed")
                if cmd != "trigger":
                    return
                assert router._subscribers
                assert len(router._observers) == 2
                if mode == "rejected":
                    router.publish({"_cmd": 2303, "msg": "test entry rejected"})
                    return
                if mode == "timeout":
                    return
                router.publish({"_cmd": 2303, "battleId": 123,
                                "pmmList": [player(100, own_pets)]})
                if mode != "late_backpack":
                    publish_backpack()
                # The active-pet-only update must not erase a matching reserve pet.
                router.publish({"_cmd": 2401, "battleUniqueId": 456,
                                "pmmList": [player(200, [pet(1234)])]})
                router.publish({"_cmd": 2426})
                if mode not in {"delayed", "cancel", "disconnect"}:
                    router.publish({"_cmd": 2402, "pt": 0})

        socket = FakeSocket()
        router = MessageRouter(socket)

        def log(message):
            logs.append(message)
            if message == "前置步骤已提交，等待本次战斗完整就绪及对方背包":
                waiting.set()

        context = AppContext({}, SimpleNamespace(user_id="100"), None, socket, router, log)
        context.player_initialized = True
        original_observers = set(router._observers)

        async def fast_combination(*args, **kwargs):
            self.assertTrue(kwargs["preserve_current_battle"])
            return await execute_combination(*args, **kwargs, message_delay=0)

        with patch.multiple(battle_control, ENTRY_TIMEOUT_SECONDS=.1, MATCH_TIMEOUT_SECONDS=.1, WAIT_TIMEOUT_SECONDS=.1), patch.multiple(
            script,
            BEFORE_STEPS=(MessageBatchStep((SendMessage(42, "trigger", {}),)),),
            OPTIONAL_STEPS=(MessageBatchStep((SendMessage(42, "optional", {}),)),) if optional else (),
            AFTER_STEPS=(MessageBatchStep((SendMessage(42, "after", {}),)),),
            execute_combination=fast_combination,
        ):
            task = asyncio.create_task(script.run(context))
            if mode in {"delayed", "late_backpack", "cancel", "disconnect"}:
                await asyncio.wait_for(waiting.wait(), 1)
                self.assertEqual(socket.sent, ["trigger"])
                self.assertFalse(task.done())
                if mode == "delayed":
                    router.publish({"_cmd": 2402, "pt": 0})
                elif mode == "late_backpack":
                    self.assertTrue(context.battle.entry_ready)
                    publish_backpack()
                elif mode == "cancel":
                    task.cancel()
                else:
                    socket.connected = False
                    router.disconnected.set()
                    router.publish({"_cmd": "wake"})
            try:
                results = await asyncio.wait_for(task, 2)
                error = None
            except (ScriptExecutionError, battle_control.BattleRoundSkipped, asyncio.CancelledError) as exc:
                results = getattr(exc, "send_results", ())
                error = exc

        self.assertFalse(router._subscribers)
        self.assertEqual(router._observers, original_observers)
        self.assertEqual([item.sequence for item in results], list(range(1, len(results) + 1)))
        return socket.sent, logs, error

    async def test_both_pet_ids_and_all_soul_ids_match(self):
        for pet_id in (5999, 6000):
            for soul_id in (58, 27, 36):
                with self.subTest(pet_id=pet_id, soul_id=soul_id):
                    sent, _, error = await self.exercise([pet(pet_id, f'123;{soul_id};7:6')])
                    self.assertIsNone(error)
                    self.assertEqual(sent, ["trigger", "optional", "after"])

    async def test_no_match_still_runs_fixed_after(self):
        for pets in (
            [pet(1234, '1;36;1:6')],
            [pet(5999, '1;85;1:6')],
            [pet(5999)],
            [pet(5999, '58;85;36:6|9;27;1:6')],
            [pet(5999), pet(1234, '1;36;1:6')],
        ):
            with self.subTest(pets=pets):
                sent, _, error = await self.exercise(pets)
                self.assertIsNone(error)
                self.assertEqual(sent, ["trigger", "after"])

    async def test_own_matching_pet_does_not_trigger(self):
        sent, _, error = await self.exercise([pet(1234)], own_pets=[pet(5999, '1;36;1:6')])
        self.assertIsNone(error)
        self.assertEqual(sent, ["trigger", "after"])

    async def test_reserve_pet_survives_active_update_and_multiple_matches_run_once(self):
        sent, _, error = await self.exercise([
            pet(1234), pet(5999, '1;36;1:6'), pet(6000, '2;58;1:6')
        ])
        self.assertIsNone(error)
        self.assertEqual(sent, ["trigger", "optional", "after"])

    async def test_empty_optional_keeps_fixed_after(self):
        sent, logs, error = await self.exercise([pet(5999, '1;36;1:6')], optional=False)
        self.assertIsNone(error)
        self.assertEqual(sent, ["trigger", "after"])
        self.assertTrue(any("OPTIONAL_STEPS 尚未填写" in line for line in logs))

    async def test_waits_for_entry_and_backpack(self):
        for mode in ("delayed", "late_backpack"):
            with self.subTest(mode=mode):
                sent, _, error = await self.exercise([pet(5999, '1;36;1:6')], mode=mode)
                self.assertIsNone(error)
                self.assertEqual(sent, ["trigger", "optional", "after"])

    async def test_entry_errors_and_cancel_clean_up(self):
        for mode, text in (
            ("rejected", "test entry rejected"),
            ("timeout", "未确认本次战斗完整就绪"),
            ("disconnect", "连接已断开"),
            ("cancel", None),
        ):
            with self.subTest(mode=mode):
                sent, _, error = await self.exercise([pet(5999, '1;36;1:6')], mode=mode)
                self.assertEqual(sent, ["trigger", "1404", "1404"] if mode in {"rejected", "timeout"} else ["trigger"])
                if text is None:
                    self.assertIsInstance(error, asyncio.CancelledError)
                else:
                    self.assertIn(text, str(error))

    async def test_optional_failure_does_not_continue_after(self):
        sent, _, error = await self.exercise([pet(5999, '1;36;1:6')], mode="optional_failure")
        self.assertIn("test optional failed", str(error))
        self.assertEqual(sent, ["trigger", "optional"])

    async def test_discovery_for_new_script(self):
        with TemporaryDirectory() as directory:
            Path(directory, 'wait_escape.py').touch()
            self.assertEqual([item.module_name for item in discover_scripts(Path(directory))],
                             ["scripts.wait_escape"])


if __name__ == "__main__":
    unittest.main()
