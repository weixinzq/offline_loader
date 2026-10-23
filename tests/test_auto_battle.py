"""Offline multi-unit action and replacement contracts."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest

from src.messaging.auto_battle import AutoBattle
from src.network.context import BattleLifecycle
from src.messaging.dispatcher import _escape_params


def skill(number, pp='4/4', cd=0, ssi=0):
    return {'id': number, 'ssi': ssi, 'pp': pp, 'cd': cd}


def pet(number, order=0, hp=100, skills=None):
    return {'pmSId': order, 'pmSView': {'i': number, 'c0': hp, 'lock': 0},
            'skills': skills if skills is not None else [skill(100 + number)]}


def player(slot, pets, owner=100):
    return {'PSId': 0, 'battleView': {'pmmId': owner, 'slotId': slot, 'pmList': pets}}


class AutoBattleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent, self.messages = [], []

        async def send(ext, cmd, params):
            self.sent.append((ext, cmd, params))

        self.context = SimpleNamespace(
            socket=SimpleNamespace(connected=True, send_xt_message=send),
            battle=BattleLifecycle(), user_id='100',
            assert_automation_allowed=lambda: None, log=self.messages.append)
        self.auto = AutoBattle(self.context)
        self.context.auto_battle = self.auto

    def publish(self, message):
        self.context.battle.observe(message)
        self.auto.observe(message)

    async def start(self, own, reserves=()):
        roster = deepcopy(own)
        for entry in roster:
            entry['battleView']['pmList'].extend(deepcopy(reserves))
        self.publish({'_cmd': '2303', 'battleId': 1, 'pmmList': roster})
        self.publish({'_cmd': '2401', 'pmmList': own + [player(10, [pet(90)], owner=200)]})
        self.publish({'_cmd': '2426'})
        self.turn(0, [entry['battleView']['slotId'] for entry in own])
        self.auto.enable()
        await asyncio.sleep(0)

    def turn(self, number, slots):
        self.publish({'_cmd': '2402', 'pt': number,
                      'desc': ';'.join(f'{slot}-1-0-1-0-0-2-6' for slot in slots)})

    async def test_two_units_choose_first_available_skill_independently(self):
        await self.start([
            player(0, [pet(1, skills=[skill(11, pp='0/4'), skill(12), skill(13)])]),
            player(1, [pet(2, skills=[skill(21, cd=2), skill(22, pp='-1/4'), skill(23)])]),
        ])
        self.assertEqual([(params['reqPSId'], params['skillId']) for _, _, params in self.sent],
                         [(0, 12), (1, 22)])
        self.assertTrue(all(params['tarSId'] == 10 for _, _, params in self.sent))
        self.assertEqual(len(self.auto.own_players), 2)
        self.assertEqual(_escape_params(self.context, {'turn': 0})['reqPSId'], 0)

    async def test_three_units_deduplicate_by_slot_and_refresh_independently(self):
        await self.start([player(slot, [pet(slot + 1)]) for slot in (0, 1, 2)])
        self.assertEqual(len(self.sent), 3)
        self.turn(0, [0, 1, 2])
        await asyncio.sleep(0)
        self.assertEqual(len(self.sent), 3)
        self.publish({'_cmd': '2411', 'pmmList': [player(1, [pet(2, skills=[skill(999)])])]})
        self.turn(1, [0, 1, 2])
        await asyncio.sleep(0)
        self.assertEqual([(params['reqPSId'], params['skillId']) for _, _, params in self.sent[3:]],
                         [(0, 101), (1, 999), (2, 103)])

    async def test_only_living_units_requested_by_this_turn_act(self):
        await self.start([player(0, [pet(1)]), player(1, [pet(2)]), player(2, [pet(3, hp=0)])])
        self.sent.clear()
        self.turn(1, [1, 2])
        await asyncio.sleep(0)
        self.assertEqual([params['reqPSId'] for _, _, params in self.sent], [1])

    async def test_no_available_skill_waits_without_blocking_other_units(self):
        await self.start([player(0, [pet(1, skills=[skill(11, pp='0/4')])]),
                          player(1, [pet(2)])])
        self.assertEqual([(cmd, params['reqPSId']) for _, cmd, params in self.sent],
                         [('1406', 0), ('1401', 1)])
        self.assertEqual(self.sent[0][2], {'turn': 0, 'reqPSId': 0, 'isAuto': True})
        self.turn(0, [0, 1])
        await asyncio.sleep(0)
        self.assertEqual(len(self.sent), 2)

    async def test_one_action_failure_does_not_cancel_other_slot_tasks(self):
        send = self.context.socket.send_xt_message

        async def fail_one(ext, cmd, params):
            if params['reqPSId'] == 0:
                raise ValueError('simulated action failure before writing')
            await send(ext, cmd, params)

        self.context.socket.send_xt_message = fail_one
        await self.start([player(0, [pet(1)]), player(1, [pet(2)])])
        self.assertEqual([params['reqPSId'] for _, _, params in self.sent], [1])
        self.assertTrue(any('simulated action failure' in message for message in self.messages))

    async def test_two_replacements_are_distinct_and_exclude_active_pets(self):
        await self.start([player(0, [pet(1, order=0)]), player(1, [pet(2, order=1)]),
                          player(2, [pet(3, order=2)])],
                         reserves=[pet(4, order=3), pet(5, order=4)])
        self.sent.clear()
        self.publish({'_cmd': '2422', 'ss': '0#1'})
        self.publish({'_cmd': '2422', 'ss': '0#1'})
        await asyncio.sleep(0)
        self.assertEqual([(params['reqPSId'], params['pmId']) for _, cmd, params in self.sent if cmd == '1403'],
                         [(0, 4), (1, 5)])
        self.turn(1, [0, 1, 2])
        await asyncio.sleep(0)
        self.assertEqual(self.sent[-1][2]['reqPSId'], 2)
        self.assertEqual(self.sent[-1][1], '1401')
        self.assertEqual(sum(cmd == '1403' for _, cmd, _ in self.sent), 2)

    async def test_replacement_confirmation_releases_only_its_slot(self):
        await self.start([player(0, [pet(1)]), player(1, [pet(2)])],
                         reserves=[pet(3, order=2), pet(4, order=3)])
        self.publish({'_cmd': '2422', 'ss': '0#1'})
        await asyncio.sleep(0)
        self.publish({'_cmd': '2413', 'pmmList': [player(0, [pet(3)])]})
        self.sent.clear()
        self.turn(1, [0, 1])
        await asyncio.sleep(0)
        self.assertEqual([(cmd, params['reqPSId']) for _, cmd, params in self.sent], [('1401', 0)])

    async def test_battle_end_cancels_queued_actions_for_all_units(self):
        await self.start([player(0, [pet(1)]), player(1, [pet(2)])])
        self.sent.clear()
        self.turn(1, [0, 1])
        self.publish({'_cmd': '2403'})
        await asyncio.sleep(0)
        self.assertFalse(self.sent)
        self.assertFalse(self.auto.own_players)
