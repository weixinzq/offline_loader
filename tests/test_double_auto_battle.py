"""Offline checks for match preparation and the existing battle-ready gate."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import scripts.double_auto_battle as script
from src.network.context import AppContext, MessageRouter
from src.scripting.composer import execute_combination
from src.messaging import battle_control
from src.scripting.loader import ScriptExecutionError
from src.service.execution import ScriptContext


class DoubleAutoBattleTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, mode='normal', repetitions=1):
        sent, logs = [], []
        matching, preparing = asyncio.Event(), asyncio.Event()
        battle_number = 0

        def entry(ready=True):
            router.publish({'_cmd': '2303', 'battleId': battle_number, 'pmmList': []})
            router.publish({'_cmd': '2401', 'battleUniqueId': battle_number})
            router.publish({'_cmd': '2426'})
            if ready:
                router.publish({'_cmd': '2402', 'pt': 0})

        class Socket:
            connected = True

            async def send_xt_message(self, ext, cmd, params):
                nonlocal battle_number
                sent.append((ext, cmd, params))
                if cmd == 'SA231229_match':
                    battle_number += 1
                    self.assert_subscribed = bool(router._subscribers)
                    matching.set()
                    if mode == 'direct_entry':
                        entry()
                    elif mode != 'delayed_match':
                        router.publish({'_cmd': 'SA231229_CmdMatch', 'ids': [100, 200]})
                        router.publish({'_cmd': 'SA231229_CmdMatch', 'ids': [100, 200]})
                elif cmd == 'SA231229_dpmr':
                    preparing.set()
                    if mode == 'prepare_failed':
                        raise ConnectionError('test preparation send failed')
                    if mode != 'direct_entry' and mode != 'entry_timeout':
                        entry(ready=mode != 'delayed_entry')
                    # The capture has 2303 before the preparation acknowledgement.
                    router.publish({'_cmd': cmd, 'r': 1})
                elif cmd == '1409':
                    if not context.battle.entry_ready:
                        raise AssertionError('auto battle started before full entry')
                    router.publish({'_cmd': '2403', 'battleUniqueId': battle_number})

        socket = Socket()
        router = MessageRouter(socket)
        context = AppContext({}, SimpleNamespace(user_id='100'), None, socket, router, logs.append)
        context.player_initialized = True

        async def fast_combination(*args, **kwargs):
            return await execute_combination(*args, **kwargs, message_delay=0)

        with patch.multiple(battle_control, ENTRY_TIMEOUT_SECONDS=.1, MATCH_TIMEOUT_SECONDS=.1), patch.multiple(script, execute_combination=fast_combination):
            results, error = (), None
            for _ in range(repetitions):
                task = asyncio.create_task(script.run(ScriptContext(context)))
                if mode == 'delayed_match':
                    await asyncio.wait_for(matching.wait(), 1)
                    await asyncio.sleep(0)
                    self.assertFalse(preparing.is_set())
                    router.publish({'_cmd': 'SA231229_match', 'r': 1})
                    await asyncio.sleep(0)
                    self.assertFalse(preparing.is_set(), 'queue acknowledgement is not a match')
                    router.publish({'_cmd': 'SA231229_CmdMatch', 'ids': [100, 200]})
                elif mode == 'delayed_entry':
                    await asyncio.wait_for(preparing.wait(), 1)
                    await asyncio.sleep(0)
                    self.assertNotIn('1409', [row[1] for row in sent])
                    router.publish({'_cmd': '2402', 'pt': 0})
                try:
                    results = await asyncio.wait_for(task, 2)
                except (ScriptExecutionError, battle_control.BattleRoundSkipped) as exc:
                    error = exc
                    break
                self.assertEqual([row.sequence for row in results], list(range(1, len(results) + 1)))
        self.assertTrue(socket.assert_subscribed)
        self.assertFalse(router._subscribers)
        return sent, results, error

    async def test_settings_and_preparation_order_and_parameters(self):
        sent, results, error = await self.exercise()
        self.assertIsNone(error)
        self.assertEqual(sent, [
            (42, 'AHM230616_1', {'tid': 302}),
            (42, 'SA231229_nufl', {'mt': 0}),
            (42, 'SA231229_flr', {'ui': 0, 'mt': 0}),
            (42, 'SA231229_sl', {'mt': 0, 'ids': [6, 5, 4, 3, 2, 1]}),
            (42, 'SA231229_match', {'mt': 0}),
            (42, 'SA231229_dpmr', {}),
            (13, '1409', {'useAiType': 1}),
        ])
        self.assertEqual(results[-1].cmd, '#wait')

    async def test_ready_waits_for_match_instead_of_queue_ack(self):
        _, _, error = await self.exercise('delayed_match')
        self.assertIsNone(error)

    async def test_auto_battle_waits_for_both_sides_full_entry(self):
        _, _, error = await self.exercise('delayed_entry')
        self.assertIsNone(error)

    async def test_direct_entry_without_preparation_phase_still_works(self):
        sent, _, error = await self.exercise('direct_entry')
        self.assertIsNone(error)
        self.assertEqual(sum(cmd == 'SA231229_dpmr' for _, cmd, _ in sent), 1)

    async def test_settings_and_preparation_repeat_once_per_match(self):
        sent, _, error = await self.exercise(repetitions=2)
        self.assertIsNone(error)
        for cmd in ('SA231229_nufl', 'SA231229_flr', 'SA231229_sl', 'SA231229_dpmr', '1409'):
            self.assertEqual(sum(item[1] == cmd for item in sent), 2)

    async def test_preparation_failure_or_missing_entry_never_enables_auto(self):
        for mode in ('prepare_failed', 'entry_timeout'):
            with self.subTest(mode=mode):
                sent, _, error = await self.exercise(mode)
                self.assertIsNotNone(error)
                self.assertNotIn('1409', [row[1] for row in sent])
