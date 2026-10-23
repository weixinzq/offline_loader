"""Recovery uses fake sockets; no real accounts or game traffic."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.messaging import battle_control as control
from src.messaging.dispatcher import execute_messages
from src.messaging.parser import SendMessage
from src.network.context import AppContext, MessageRouter
from src.scripting.composer import execute_combination, MessageBatchStep, ScriptStep
from src.scripting.loader import InteractionScript
from src.service.execution_runner import run_execution
from src.interfaces.schemas import AccountResult


def script_step(function):
    return ScriptStep(InteractionScript('test.' + function.__name__, function.__name__, '', function))


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def context(self, callback=None):
        sent, logs = [], []

        class Socket:
            connected = True
            active_room_id = 1

            async def send_xt_message(self, ext, cmd, params):
                sent.append((ext, cmd, params))
                if callback:
                    await callback(cmd, params)

        socket = Socket()
        router = MessageRouter(socket)
        context = AppContext({}, SimpleNamespace(user_id='100'), None, socket, router, logs.append)
        context.player_initialized = True
        return context, router, sent, logs

    def entry(self, router, number=1, ready=True, slot=7):
        router.publish({'_cmd': '2303', 'battleId': number, 'pmmList': [
            {'battleView': {'pmmId': 100, 'slotId': slot, 'pmList': []}}]})
        router.publish({'_cmd': '2401', 'battleUniqueId': 1000 + number})
        router.publish({'_cmd': '2426'})
        if ready:
            router.publish({'_cmd': '2402', 'pt': 0})

    async def test_nested_wait_aborts_all_remaining_steps_and_counts_writes(self):
        number = 0

        async def send(cmd, params):
            if cmd == 'prepared' and number == 2:
                router.publish({'_cmd': '2403', 'battleUniqueId': 1000 + number})

        context, router, sent, logs = self.context(send)

        async def enter(ctx):
            nonlocal number
            number += 1
            self.entry(router, number)

        async def nested(ctx):
            result = await execute_combination('100', ctx, (
                script_step(enter), MessageBatchStep((SendMessage(42, 'prepared', {}),
                    SendMessage(-1, '#wait', {}), SendMessage(42, 'inner_after', {}))),
            ), message_delay=0, preserve_current_battle=True)
            return result.send_results

        outcome = {}
        steps = (script_step(nested), MessageBatchStep((SendMessage(42, 'outer_after', {}),)))
        with patch.object(control, 'WAIT_TIMEOUT_SECONDS', .02):
            await run_execution('100', context, steps, outcome, repetitions=2,
                                repeat_interval=0, message_delay=0, preserve_current_battle=True)
        self.assertTrue(outcome['success'])
        self.assertEqual((outcome['attempted_repetitions'], outcome['completed_repetitions'],
                          outcome['skipped_repetitions']), (2, 1, 1))
        self.assertEqual([cmd for _, cmd, _ in sent],
                         ['prepared', '1404', 'prepared', 'inner_after', 'outer_after'])
        self.assertEqual(sent[1], (13, '1404', {'turn': 0, 'reqPSId': 7}))
        self.assertEqual(outcome['network_write'], {'status': 'succeeded', 'count': 5})
        self.assertFalse(router._subscribers)
        self.assertEqual(sum('恢复跳过' in line for line in logs), 1)

    async def test_unknown_state_fallback_zero_eleven_and_next_round(self):
        context, router, sent, logs = self.context()
        context.battle.mark_unknown('test unknown')
        steps = (script_step(control.require_idle), MessageBatchStep((SendMessage(42, 'next', {}),)))
        result = await execute_combination('100', context, steps, repetitions=2,
                                           repeat_interval=0, message_delay=0)
        self.assertTrue(result.success)
        self.assertEqual(result.attempted_repetitions, 2)
        self.assertEqual(result.skipped_repetitions, 1)
        self.assertEqual(sent, [(13, '1404', {'turn': 0, 'reqPSId': 0}),
                                (13, '1404', {'turn': 0, 'reqPSId': 11}), (42, 'next', {})])
        self.assertEqual([item.cmd for item in result.send_results], ['next'])

    async def test_repeated_detection_only_recovers_once(self):
        context, _, sent, _ = self.context()

        async def twice(ctx):
            try:
                await control.abort_round(ctx, 'first')
            except control.BattleRoundSkipped:
                await control.abort_round(ctx, 'second')

        result = await execute_combination('100', context, (script_step(twice),), message_delay=0)
        self.assertTrue(result.success)
        self.assertEqual(result.skipped_repetitions, 1)
        self.assertEqual([p['reqPSId'] for _, _, p in sent], [0, 11])

    async def test_match_deadline_is_not_reset_by_unrelated_messages(self):
        context, router, sent, _ = self.context()
        subscription = router.subscribe()

        async def match(ctx):
            await execute_messages('100', ctx, [SendMessage(42, 'SA231229_match', {})], delay=0)
            await asyncio.sleep(.02)  # This time is part of the matching deadline.
            await control.wait_for_match(ctx, subscription, 0)

        async def noise():
            while True:
                router.publish({'_cmd': 'unrelated'})
                await asyncio.sleep(.002)

        producer = asyncio.create_task(noise())
        try:
            with patch.object(control, 'MATCH_TIMEOUT_SECONDS', .03):
                result = await asyncio.wait_for(execute_combination(
                    '100', context, (script_step(match),), message_delay=0), .2)
            self.assertEqual(result.skipped_repetitions, 1)
            self.assertEqual([cmd for _, cmd, _ in sent], ['SA231229_match', '1404', '1404'])
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            subscription.close()

    async def test_partial_entry_timeout_skips_auto(self):
        context, router, sent, _ = self.context()
        sub = router.subscribe()
        self.entry(router, ready=False)

        async def entry(ctx):
            await control.wait_for_entry(ctx, sub, 0)

        try:
            with patch.object(control, 'ENTRY_TIMEOUT_SECONDS', .02):
                result = await execute_combination('100', context, (script_step(entry),
                    MessageBatchStep((SendMessage(13, '1409', {'useAiType': 1}),))), message_delay=0)
            self.assertEqual(result.skipped_repetitions, 1)
            self.assertEqual([cmd for _, cmd, _ in sent], ['1404'])
            self.assertTrue(all(step.skipped for step in result.step_results))
        finally:
            sub.close()

    async def test_manual_cancel_does_not_end_battle_or_continue(self):
        context, router, sent, _ = self.context()
        self.entry(router)
        task = asyncio.create_task(execute_combination('100', context,
            (MessageBatchStep((SendMessage(-1, '#wait', {}), SendMessage(42, 'after', {}))),),
            repetitions=2, message_delay=0))
        while not router._subscribers:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sent, [])
        self.assertEqual(context.battle.phase, 'active')
        self.assertFalse(router._subscribers)

    async def test_disconnect_and_failed_end_write_remain_fatal(self):
        for mode in ('disconnect', 'write_failure'):
            async def fail(cmd, params):
                raise ConnectionError('test write failed')
            context, _, sent, _ = self.context(fail if mode == 'write_failure' else None)
            context.battle.mark_unknown('test unknown')
            if mode == 'disconnect':
                context.socket.connected = False
            result = await execute_combination('100', context,
                (script_step(control.require_idle),), repetitions=3, message_delay=0)
            self.assertFalse(result.success)
            self.assertEqual(result.skipped_repetitions, 0)
            self.assertEqual(len(sent), int(mode == 'write_failure'))

    async def test_old_packets_cannot_resurrect_or_end_new_battle(self):
        context, router, _, _ = self.context()
        self.entry(router, 1)
        try:
            await control.abort_round(context, 'test')
        except control.BattleRoundSkipped:
            pass
        sub = router.subscribe()
        router.publish({'_cmd': '2405'})
        router.publish({'_cmd': '2303', 'battleId': 1})
        self.assertEqual(context.battle.phase, 'idle')
        self.assertTrue(sub._queue.empty())
        self.entry(router, 2)
        router.publish({'_cmd': '2403', 'battleUniqueId': 1001})
        router.publish({'_cmd': '2402', 'battleId': 1, 'pt': 99})
        self.assertTrue(context.battle.entry_ready)
        self.assertEqual(context.battle.current_turn, 0)
        sub.close()

    async def test_queued_auto_action_is_cancelled_before_end(self):
        context, router, sent, _ = self.context()
        self.entry(router)
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def old_action():
            entered.set()
            await gate.wait()
            await context.socket.send_xt_message(13, '1401', {})

        context.auto_battle._schedule(old_action())
        await entered.wait()
        try:
            await control.abort_round(context, 'test')
        except control.BattleRoundSkipped:
            pass
        gate.set()
        await asyncio.sleep(0)
        self.assertEqual([cmd for _, cmd, _ in sent], ['1404'])
        self.assertFalse(context.auto_battle._pending_actions)

    async def test_legacy_account_counts_remain_compatible(self):
        row = AccountResult.model_validate({'account_id': 'a', 'state': 'failed',
            'completed_repetitions': 33, 'error': None,
            'network_write': {'status': 'unknown', 'count': None}, 'business_confirmation': 'unknown'})
        self.assertEqual(row.attempted_repetitions, 33)
        self.assertEqual(row.skipped_repetitions, 0)

    async def test_all_9999_recovered_attempts_finish_without_claiming_normal_completion(self):
        context, _, sent, _ = self.context()

        async def timeout(ctx):
            await control.abort_round(ctx, 'test timeout')

        result = await execute_combination('100', context, (script_step(timeout),),
                                           repetitions=9999, repeat_interval=0, message_delay=0)
        self.assertTrue(result.success)
        self.assertEqual((result.attempted_repetitions, result.completed_repetitions,
                          result.skipped_repetitions), (9999, 0, 9999))
        self.assertEqual(len(sent), 19998)
        self.assertEqual(result.send_results, ())

    async def test_accounts_have_independent_recovery_scopes(self):
        first, _, sent_first, _ = self.context()
        second, _, sent_second, _ = self.context()

        async def recover(ctx):
            await asyncio.sleep(0)
            await control.abort_round(ctx, 'test account')

        results = await asyncio.gather(
            execute_combination('first', first, (script_step(recover),),
                                repetitions=2, repeat_interval=0),
            execute_combination('second', second, (MessageBatchStep((SendMessage(42, 'normal', {}),)),),
                                repetitions=2, repeat_interval=0))
        self.assertEqual(results[0].skipped_repetitions, 2)
        self.assertEqual(results[1].completed_repetitions, 2)
        self.assertEqual([cmd for _, cmd, _ in sent_first], ['1404'] * 4)
        self.assertEqual([cmd for _, cmd, _ in sent_second], ['normal'] * 2)

    def test_production_deadlines(self):
        self.assertEqual((control.MATCH_TIMEOUT_SECONDS, control.ENTRY_TIMEOUT_SECONDS,
                          control.WAIT_TIMEOUT_SECONDS), (5, 15, 15))

    async def test_native_entry_uses_current_room_and_handles_early_end(self):
        for early in (False, True):
            async def send(cmd, params):
                if cmd == '54_22':
                    self.entry(router)
                    if early:
                        router.publish({'_cmd': '2403', 'battleUniqueId': 1001})
                elif cmd == '1409':
                    self.assertTrue(context.battle.entry_ready)
                    router.publish({'_cmd': '2403', 'battleUniqueId': 1001})

            context, router, sent, _ = self.context(send)
            result = await execute_combination('100', context, (MessageBatchStep((
                SendMessage(15, '54_22', {'handler': 'test'}), SendMessage(42, 'auxiliary', {}),
                SendMessage(13, '1409', {'useAiType': 1}), SendMessage(-1, '#wait', {}))),),
                message_delay=0)
            self.assertTrue(result.success)
            self.assertEqual(result.skipped_repetitions, int(early))
            self.assertEqual(context.socket.active_room_id, 1)
            if early:
                self.assertNotIn('1409', [cmd for _, cmd, _ in sent])
            else:
                self.assertEqual([cmd for _, cmd, _ in sent], ['54_22', 'auxiliary', '1409'])

    async def test_native_partial_entry_recovers_instead_of_sending_actions(self):
        async def send(cmd, params):
            if cmd == '54_22':
                self.entry(router, ready=False)

        context, router, sent, _ = self.context(send)
        with patch.object(control, 'ENTRY_TIMEOUT_SECONDS', .02):
            result = await execute_combination('100', context, (MessageBatchStep((
                SendMessage(15, '54_22', {'handler': 'test'}), SendMessage(42, 'auxiliary', {}),
                SendMessage(13, '1409', {'useAiType': 1}))),), message_delay=0)
        self.assertTrue(result.success)
        self.assertEqual(result.skipped_repetitions, 1)
        self.assertEqual([cmd for _, cmd, _ in sent], ['54_22', 'auxiliary', '1404'])

    async def test_builtin_auto_and_escape_scripts_keep_normal_flow(self):
        from scripts import auto_battle, auto_escape
        from src.service.execution import ScriptContext
        for module in (auto_battle, auto_escape):
            async def send(cmd, params):
                if cmd == 'SA231229_match':
                    self.entry(router)
                elif cmd in ('1409', '1404'):
                    self.assertTrue(context.battle.entry_ready)
                    if cmd == '1404':
                        self.assertEqual(params['reqPSId'], 7)
                    router.publish({'_cmd': '2403', 'battleUniqueId': 1001})

            context, router, sent, _ = self.context(send)

            async def fast(*args, **kwargs):
                return await execute_combination(*args, **kwargs, message_delay=0)

            with patch.object(module, 'execute_combination', fast):
                sends = await module.run(ScriptContext(context))
            self.assertEqual(sends[-1].cmd, '#wait')
            self.assertEqual([cmd for _, cmd, _ in sent],
                             ['AHM230616_1', 'SA231229_match', '1409' if module is auto_battle else '1404'])
