"""Outbound groups: capacity, isolated processes, scripts, cancellation and API/CLI."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner
import yaml

from src.interfaces.cli import app as cli
from src.interfaces.http import create_app
from src.service.application import Application
from src.service.contracts import EgressGroupInput, ServiceError, TaskSpec
from src.service.workers import Worker
from tests.test_service_integration import InMemoryGame


FIXTURE = 'tests.egress_worker_fixture'


class EgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.application = Application(self.directory, connection_factory=InMemoryGame(), worker_module=FIXTURE)
        await self.application.start()

    async def asyncTearDown(self):
        await self.application.close()
        self.temporary.cleanup()

    def group(self, name='测试出口', capacity=30, confirmed=True):
        return self.application.egress.create({'name': name, 'clash_policy': f'节点-{name}',
            'max_online': capacity, 'routing_confirmed': confirmed})['group_id']

    def account(self, index, group=None, *, login=None):
        return self.application.accounts.create({'label': f'测试 {index}', 'account': login or f'fixture-{index}',
            'password': f'fixture-secret-{index}', 'egress_group_id': group})['account_id']

    @staticmethod
    def spec(ids, **options):
        value = {'account_ids': ids, 'concurrency': 5, 'message_delay': 0, 'repeat_interval': 0,
                 'steps': [{'kind': 'messages', 'messages': [{'id': -1, 'cmd': '#time', 'param': {'seconds': 0}}]}]}
        value.update(options)
        if value.get('kind', 'execute') != 'execute':
            value['steps'] = []
        return TaskSpec.model_validate(value)

    async def finish(self, record):
        run = self.application.tasks._runs.get(record['task_id'])
        if run:
            await asyncio.wait_for(asyncio.shield(run.task), 45)
        return self.application.tasks.get(record['task_id'])

    async def submit(self, ids, **options):
        return await self.application.tasks.submit(self.spec(ids, **options))

    async def test_rules_are_unique_native_paths_and_unconfirmed_preflight_never_starts_process(self):
        first = self.group('一', confirmed=False)
        second = self.group('二')
        account = self.account(1, first)
        with self.assertRaises(ServiceError) as blocked:
            await self.submit([account], kind='connect')
        self.assertEqual(blocked.exception.code, 'egress_unconfirmed')
        self.assertTrue(all(worker.process is None for worker in self.application.workers.workers.values()))
        rules = yaml.safe_load(self.application.egress.rules()['yaml'])
        self.assertEqual(rules['find-process-mode'], 'strict')
        self.assertEqual(rules['mode'], 'rule')
        self.assertEqual(len(rules['rules']), 2)
        paths = [self.application.egress.get(group)['worker_path'] for group in (first, second)]
        self.assertNotEqual(*paths)
        self.assertTrue(all(Path(path).is_file() for path in paths))
        self.assertEqual(rules['rules'], [f'PROCESS-PATH,{path},节点-{name}' for path, name in zip(paths, ['一', '二'])])
        await self.application.egress.probe(first)
        self.assertEqual(self.application.egress.get(first)['exit_ip'], '192.0.2.10')
        self.assertEqual(self.application.sessions.list()[0]['state'], 'offline')

    async def test_assignment_is_atomic_preserves_credentials_and_busy_accounts_cannot_move(self):
        group = self.group()
        ids = [self.account(index) for index in range(2)]
        with self.assertRaises(ServiceError):
            self.application.accounts.assign_egress([ids[0], 'missing'], group)
        self.assertIsNone(self.application.accounts.get(ids[0]).get('egress_group_id'))
        self.application.accounts.assign_egress(ids, group)
        self.assertEqual(self.application.store.get_account(ids[0], private=True)['password'], 'fixture-secret-0')
        record = await self.finish(await self.submit([ids[0]], kind='connect'))
        self.assertEqual(record['state'], 'succeeded', record)
        with self.assertRaises(ServiceError) as busy:
            self.application.accounts.assign_egress(ids, None)
        self.assertEqual(busy.exception.code, 'account_busy')
        self.assertEqual(self.application.accounts.get(ids[1])['egress_group_id'], group)
        with self.assertRaises(ServiceError):
            await self.application.egress.update(group, {'name': 'changed', 'clash_policy': 'another'})
        await self.finish(await self.submit([ids[0]], kind='disconnect'))
        self.application.accounts.assign_egress(ids, None)
        self.assertIsNone(self.application.accounts.get(ids[0])['egress_group_id'])
        await self.application.egress.delete(group)

    async def test_group_capacity_covers_complete_lifecycle_and_persistent_preflight_is_atomic(self):
        group = self.group(capacity=2)
        ids = [self.account(index, group) for index in range(7)]
        for options in ({'kind': 'connect'}, {'mode': 'all_online'}):
            with self.assertRaises(ServiceError) as failure:
                await self.submit(ids, **options)
            self.assertEqual(failure.exception.code, 'egress_capacity_exceeded')
        self.assertIsNone(self.application.workers.worker(group).process)
        run = await self.submit(ids)
        peak = 0
        while run['task_id'] in self.application.tasks._runs:
            usage = self.application.egress.get(group)
            peak = max(peak, usage['online_count'])
            self.assertLessEqual(usage['online_count'] + usage['reserved_count'], 2)
            await asyncio.sleep(.005)
        result = self.application.tasks.get(run['task_id'])
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertGreater(peak, 0)
        self.assertTrue(all(row['network_write'] == {'status': 'not_attempted', 'count': 0} for row in result['account_results']))
        self.assertEqual(self.application.egress.get(group)['online_count'], 0)
        self.assertEqual(self.application.sessions._claims, {})
        self.assertEqual(self.application.sessions._group_reservations, {})

    async def test_scripts_cancel_preserve_partial_audit_and_keep_borrowed_session(self):
        group = self.group()
        account = self.account(1, group, login='fixture-wait')
        self.assertEqual((await self.finish(await self.submit([account], kind='connect')))['state'], 'succeeded')
        run = await self.submit([account], session_policy='existing', steps=[{'kind': 'script', 'script_id': 'rank_info'}])
        for _ in range(300):
            entries = self.application.tasks.logs(run['task_id'])
            if any('71_29' in entry['message'] for entry in entries):
                break
            await asyncio.sleep(.01)
        result = await self.application.tasks.cancel(run['task_id'])
        result = await self.finish(result)
        self.assertEqual(result['state'], 'cancelled', result)
        self.assertEqual(result['account_results'][0]['network_write'], {'status': 'succeeded', 'count': 1})
        self.assertEqual(self.application.sessions.list()[0]['state'], 'ready')
        self.assertEqual(self.application.egress.get(group)['online_count'], 1)
        self.assertEqual(self.application.sessions._claims, {})
        logs = json.dumps(self.application.store.get_logs(run['task_id']), ensure_ascii=False)
        self.assertNotIn('fixture-secret-1', logs)
        self.assertNotIn('mock-runtime-sid', logs)

    async def test_recovered_counts_cross_worker_pipe_and_keep_resident(self):
        group = self.group()
        account = self.account(1, group, login='fixture-recovery')
        await self.finish(await self.submit([account], kind='connect'))
        task = await self.submit([account], session_policy='existing', repetitions=3, steps=[
            {'kind': 'messages', 'messages': [
                {'id': 42, 'cmd': 'fixture-battle', 'param': {}},
                {'id': -1, 'cmd': '#wait', 'param': {}},
                {'id': 42, 'cmd': 'must-not-send', 'param': {}},
            ]}])
        result = await self.finish(task)
        row = result['account_results'][0]
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertEqual((row['attempted_repetitions'], row['completed_repetitions'],
                          row['skipped_repetitions']), (3, 0, 3))
        self.assertEqual(row['network_write'], {'status': 'succeeded', 'count': 6})
        self.assertTrue(all(step['skipped'] for step in row['step_results']))
        self.assertEqual(self.application.sessions.list()[0]['state'], 'ready')
        logs = self.application.tasks.logs(task['task_id'])
        self.assertFalse(any('must-not-send' in item['message'] for item in logs))

    async def test_worker_failure_isolated_from_other_group_and_duplicate_identity_still_conflicts(self):
        first, second = self.group('一'), self.group('二')
        a, b = self.account(1, first), self.account(2, second)
        result = await self.finish(await self.submit([a, b], kind='connect'))
        self.assertEqual(result['state'], 'succeeded', result)
        workers = [self.application.workers.worker(group) for group in (first, second)]
        self.assertNotEqual(workers[0].process.pid, workers[1].process.pid)
        duplicate = self.account(3, second, login='fixture-1')
        with self.assertRaises(ServiceError) as failure:
            await self.submit([duplicate], kind='connect')
        self.assertEqual(failure.exception.code, 'session_conflict')
        workers[0].process.kill()
        await asyncio.wait_for(workers[0].process.wait(), 5)
        for _ in range(100):
            if not self.application.sessions.is_busy(a):
                break
            await asyncio.sleep(.01)
        states = {row['account_id']: row['state'] for row in self.application.sessions.list()}
        self.assertEqual(states[a], 'offline')
        self.assertEqual(states[b], 'ready')
        result = await self.finish(await self.submit([b], session_policy='existing',
            steps=[{'kind': 'script', 'script_id': 'rank_info'}]))
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertEqual(result['account_results'][0]['network_write'], {'status': 'succeeded', 'count': 1})
        self.assertEqual(result['account_results'][0]['business_confirmation'], 'unknown')

    async def test_verification_stops_affected_batch_without_closing_independent_group(self):
        first, second = self.group('一'), self.group('二')
        good = self.account(1, second)
        await self.finish(await self.submit([good], kind='connect'))
        ids = [self.account(2, first, login='fixture-verification'), self.account(3, first)]
        result = await self.finish(await self.submit(ids, concurrency=1))
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['error']['code'], 'verification_required')
        self.assertEqual(self.application.egress.get(first)['online_count'], 0)
        self.assertEqual(self.application.egress.get(second)['online_count'], 1)

    async def test_policy_change_invalidates_confirmation_and_restart_keeps_group_without_worker(self):
        group = self.group()
        account = self.account(1, group)
        await self.application.egress.probe(group)
        process = self.application.workers.worker(group).process
        value = await self.application.egress.update(group, {'name': 'renamed', 'clash_policy': 'new-policy', 'routing_confirmed': True})
        self.assertFalse(value['routing_confirmed'])
        self.assertIsNotNone(process.returncode)
        await self.application.close()
        self.application = Application(self.directory, worker_module=FIXTURE)
        await self.application.start()
        self.assertEqual(self.application.egress.get(group)['worker_state'], 'offline')
        self.assertEqual(self.application.accounts.get(account)['egress_group_id'], group)
        self.assertEqual(self.application.sessions.list()[0]['state'], 'offline')

    async def test_one_hundred_grouped_sessions_reserve_global_capacity_and_close_all_workers(self):
        groups = [self.group(f'出口-{index}') for index in range(4)]
        ids = [self.account(index, groups[index // 30]) for index in range(101)]
        with self.assertRaises(ServiceError) as failure:
            await self.submit(ids, kind='connect')
        self.assertEqual(failure.exception.code, 'capacity_exceeded')
        self.assertTrue(all(worker.process is None for worker in self.application.workers.workers.values()))
        result = await self.finish(await self.submit(ids[:100], kind='connect', concurrency=10))
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertEqual([self.application.egress.get(group)['online_count'] for group in groups], [30, 30, 30, 10])
        with self.assertRaises(ServiceError) as failure:
            await self.submit(ids[100:], kind='connect')
        self.assertEqual(failure.exception.code, 'capacity_exceeded')
        processes = [worker.process for worker in self.application.workers.workers.values()]
        self.assertEqual(len({process.pid for process in processes}), 4)
        await self.application.close()
        self.assertTrue(all(process.returncode is not None for process in processes))
        self.assertFalse(self.application.sessions._claims)
        self.assertFalse(self.application.sessions._group_reservations)

    async def test_stop_during_worker_creation_waits_and_leaves_no_child(self):
        group = self.group()
        worker = self.application.workers.worker(group)
        created, release = asyncio.Event(), asyncio.Event()
        spawn = asyncio.create_subprocess_exec

        async def delayed_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            created.set()
            await release.wait()
            return process

        with patch('src.service.workers.asyncio.create_subprocess_exec', delayed_spawn):
            probe = asyncio.create_task(self.application.egress.probe(group))
            await asyncio.wait_for(created.wait(), 15)
            stop = asyncio.create_task(self.application.workers.stop(group))
            await asyncio.sleep(.02)
            self.assertFalse(stop.done())
            release.set()
            with self.assertRaises(ServiceError) as stopping:
                await probe
            self.assertEqual(stopping.exception.code, 'worker_stopping')
            await asyncio.wait_for(stop, 15)
        self.assertIsNotNone(worker.process.returncode)

    async def test_control_request_timeout_does_not_recursively_request_cancellation(self):
        worker = Worker(None, 'fixture')
        sent = asyncio.Event()
        worker.process = SimpleNamespace(stdin=SimpleNamespace(write=lambda _: sent.set(), drain=asyncio.Event().wait))
        worker._cancel = AsyncMock(return_value={})
        request = asyncio.create_task(worker._request('cancel', data={'request_id': 'missing'}))
        await sent.wait()
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(request, 1)
        worker._cancel.assert_not_awaited()
        self.assertFalse(worker.pending)


class EgressContractTests(unittest.TestCase):
    def test_policy_input_rejects_rule_injection_and_direct_actions(self):
        for policy in ('', 'node,other', 'node\nother', 'DIRECT', 'reject', 'PASS'):
            with self.assertRaises(ValidationError):
                EgressGroupInput(name='test', clash_policy=policy)

    def test_api_crud_assignment_rules_auth_and_cli_use_same_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            web = create_app(Path(directory), admin_token='fixture-admin')
            with TestClient(web) as client:
                self.assertEqual(client.get('/api/v1/egress-groups').status_code, 401)
                client.headers['Authorization'] = 'Bearer fixture-admin'
                group = client.post('/api/v1/egress-groups', json={'name': 'group', 'clash_policy': 'node'}).json()
                self.assertEqual(group['max_online'], 30)
                self.assertFalse(group['routing_confirmed'])
                account = client.post('/api/v1/accounts', json={'label': 'mock', 'account': 'mock-login', 'password': 'mock-password'}).json()
                invalid = client.patch(f"/api/v1/accounts/{account['account_id']}", json={'egress_group_id': 'missing'})
                self.assertEqual(invalid.status_code, 404)
                self.assertEqual(invalid.json()['error']['code'], 'egress_not_found')
                body = {'account_ids': [account['account_id']], 'egress_group_id': group['group_id']}
                self.assertEqual(client.post('/api/v1/accounts/assign-egress', json=body).json()['updated'], 1)
                self.assertEqual(client.delete(f"/api/v1/egress-groups/{group['group_id']}").status_code, 409)
                self.assertIn('PROCESS-PATH', client.get('/api/v1/egress-groups/rules').json()['yaml'])
                with patch('src.interfaces.cli._request', return_value={'updated': 1}) as call:
                    result = CliRunner().invoke(cli, ['--token', 'fixture-admin', '--json', 'egress', 'assign', account['account_id'], '--group', group['group_id']])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(call.call_args.args[1:], ('POST', '/accounts/assign-egress', body))
                client.post('/api/v1/accounts/assign-egress', json={**body, 'egress_group_id': None})
                self.assertEqual(client.delete(f"/api/v1/egress-groups/{group['group_id']}").status_code, 204)
