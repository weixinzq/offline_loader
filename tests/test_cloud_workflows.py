"""300-account orchestration against real services with fake game connections."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from run_cloud_workflows import ACCOUNT_SCRIPT_OVERRIDES, GROUP_WORKFLOWS, make_specs, run_workflows, select_account_groups, stop_latest_workflow
from src.service.contracts import TaskSpec
from src.service.runtime import SessionService
from src.service.tasks import TaskService
from tests.test_service_runtime import MemoryStore, FakeEvents, ConnectionFactory, FakeConnection, FakeScripts, FakeMessages, successful_result


class AttemptConnection(FakeConnection):
    async def connect(self, retries=1):
        login = self.spec.config["account"]
        self.factory.attempts[login] = self.factory.attempts.get(login, 0) + 1
        connected = await super().connect(retries=retries)
        if connected and (login in self.factory.permanent or
                          login in self.factory.transient and self.factory.attempts[login] == 1):
            async def fail_initialization():
                raise ConnectionError("模拟初始化失败")
            self.context.wait_for_player_initialization = fail_initialization
        return connected


class AttemptFactory(ConnectionFactory):
    def __init__(self):
        super().__init__()
        self.attempts, self.transient, self.permanent = {}, set(), set()

    def __call__(self, spec):
        connection = AttemptConnection(self, spec)
        self.connections.append(connection)
        return connection


class BackendFixture:
    def __init__(self):
        self.store, self.events, self.factory = MemoryStore(), FakeEvents(), AttemptFactory()
        self.groups, self.manifest = [], {"version": 1, "groups": []}
        self.calls, self.delays, self.executions = [], [], []
        self.script_ids = ["alpha", "beta"]
        self.execution_gate = None
        self.execution_started = asyncio.Event()
        self.execution_active = self.execution_peak = 0
        self.sessions = SessionService(self.store, self.events, self.factory, max_online=300, login_concurrency=20)
        self.sessions.egress = self
        self.tasks = TaskService(self.store, self.sessions, FakeScripts(), FakeMessages(), self.events, self.execute)
        numbers = [i for i in range(11, 313) if i not in (104, 244)]
        for index in range(10):
            group = {"group_id": f"g{index+1}", "name": f"出口{index+1:02}", "clash_policy": f"node-{index+1}",
                     "max_online": 30, "routing_confirmed": True, "exit_ip": f"192.0.2.{index+1}"}
            self.groups.append(group)
            accounts = []
            for number in numbers[index * 30:(index + 1) * 30]:
                account_id = self.store.add_account(number)
                self.store.accounts[account_id].update(label=f"违心{number}", egress_group_id=group["group_id"])
                accounts.append({"account_id": account_id, "label": f"违心{number}"})
            self.manifest["groups"].append({**group, "exit": index+1, "accounts": accounts})

    def check(self, group_id, **options):
        return next(group for group in self.groups if group["group_id"] == group_id)

    async def execute(self, account_id, context, steps, **options):
        self.executions.append((account_id, steps, options))
        self.execution_active += 1
        self.execution_peak = max(self.execution_peak, self.execution_active)
        if self.execution_active == 300:
            self.execution_started.set()
        try:
            if self.execution_gate:
                await self.execution_gate.wait()
            return successful_result(options["repetitions"])
        finally:
            self.execution_active -= 1

    async def sleep(self, seconds):
        self.delays.append(seconds)
        await asyncio.sleep(0)

    async def request(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        if path == "/api/v1/accounts": return self.store.list_accounts()
        if path == "/api/v1/sessions": return self.sessions.list()
        if path == "/api/v1/scripts": return [{"script_id": value} for value in self.script_ids]
        if path == "/api/v1/egress-groups": return deepcopy(self.groups)
        if path == "/api/v1/messages/validate": return {"valid": True}
        if path.endswith("/probe"):
            return deepcopy(self.check(path.split('/')[-2]))
        if path == "/api/v1/tasks/validate": return self.tasks.validate(body)
        if path == "/api/v1/sessions/reconnect":
            return await self.tasks.submit(TaskSpec.model_validate({**body, "kind": "reconnect"}))
        if path == "/api/v1/tasks": return await self.tasks.submit(TaskSpec.model_validate(body))
        if path.endswith("/cancel"): return await self.tasks.cancel(path.split('/')[-2])
        if path.startswith("/api/v1/tasks/"): return self.tasks.get(path.split('/')[-1])
        raise AssertionError((method, path))

    def run(self, **kwargs):
        configs = [{**row, "steps": [{"kind": "script", "script_id": "alpha" if row["exit"] % 2 else "beta"}],
                    "repetitions": row["exit"]} for row in GROUP_WORKFLOWS]
        return run_workflows(self, self.manifest, configs, sleep=self.sleep, log=lambda value: None, **kwargs)

    async def close(self):
        if self.execution_gate: self.execution_gate.set()
        if self.factory.initialization_gate: self.factory.initialization_gate.set()
        if self.factory.cleanup_gate: self.factory.cleanup_gate.set()
        await self.tasks.close()
        await self.sessions.shutdown()


class AccountSelectionTests(unittest.TestCase):
    def test_numeric_selection_skips_missing_and_keeps_exact_groups(self):
        fixture = BackendFixture()
        rows = list(reversed(fixture.store.list_accounts()))
        groups = select_account_groups(rows)
        self.assertEqual([len(group) for group in groups], [30]*10)
        self.assertEqual([group[-1]["label"] for group in groups], [f"违心{i}" for i in (40,70,100,131,161,191,221,252,282,312)])
        rows[-1]["account"] = rows[-2]["account"]
        with self.assertRaisesRegex(ValueError, "重复"):
            select_account_groups(rows)

    def test_empty_templates_and_drifted_manifest_fail_before_network(self):
        fixture = BackendFixture()
        empty = [{**row, "steps": []} for row in GROUP_WORKFLOWS]
        with self.assertRaisesRegex(ValueError, "尚未填写"):
            make_specs(fixture.manifest, empty)
        fixture.manifest["groups"][0]["accounts"] = []
        with self.assertRaisesRegex(ValueError, "300"):
            make_specs(fixture.manifest, GROUP_WORKFLOWS)
        self.assertFalse(fixture.calls)

    def test_default_workflows_are_battles_only_with_requested_timing(self):
        fixture = BackendFixture()
        specs = make_specs(fixture.manifest, GROUP_WORKFLOWS)
        for spec in specs:
            self.assertEqual(spec['steps'], [{'kind': 'script', 'script_id': 'double_auto_escape'}])
            self.assertEqual(spec['repetitions'], 9999)
            self.assertEqual(spec['repeat_interval'], 0.2)
            self.assertEqual(spec['concurrency'], 30)
        self.assertEqual(ACCOUNT_SCRIPT_OVERRIDES, {'违心11': 'main_double_auto'})


class CloudWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = BackendFixture()

    async def asyncTearDown(self):
        await self.backend.close()

    async def test_special_account_runs_main_once_and_other_299_keep_group_steps(self):
        backend = self.backend
        backend.script_ids.append('main_double_auto')
        result = await backend.run(account_script_overrides=ACCOUNT_SCRIPT_OVERRIDES)
        submitted = [body for method, path, body in backend.calls if method == 'POST' and path == '/api/v1/tasks']
        main_tasks = [body for body in submitted if body['steps'] == [{'kind': 'script', 'script_id': 'main_double_auto'}]]
        self.assertEqual(len(submitted), 11)
        self.assertEqual([body['account_ids'] for body in main_tasks], [['id-011']])
        self.assertFalse(any('id-011' in body['account_ids'] for body in submitted if body not in main_tasks))
        self.assertEqual(len(backend.executions), 300)
        self.assertEqual(len({row[0] for row in backend.executions}), 300)
        for account_id, steps, options in backend.executions:
            group = next(g for g in backend.manifest['groups'] if any(a['account_id'] == account_id for a in g['accounts']))
            expected = 'main_double_auto' if account_id == 'id-011' else 'alpha' if group['exit'] % 2 else 'beta'
            self.assertEqual(steps[0].script_id, expected)
            self.assertEqual(options['repetitions'], group['exit'])
        self.assertEqual((result['ready'], len(result['groups']), len(result['groups'][0]['account_results'])), (300, 10, 30))
        self.assertLessEqual(backend.factory.max_logins, 20)

    async def test_special_account_exclusion_does_not_login_or_submit_main(self):
        backend = self.backend
        backend.script_ids.append('main_double_auto')
        result = await backend.run(account_script_overrides=ACCOUNT_SCRIPT_OVERRIDES, exclude_account_ids=['id-011'])
        self.assertEqual(result['ready'], 299)
        self.assertNotIn(backend.store.accounts['id-011']['account'], backend.factory.attempts)
        self.assertFalse(any(row[1][0].script_id == 'main_double_auto' for row in backend.executions))

    async def test_invalid_special_routing_fails_before_login(self):
        for overrides in [{'违心11': 'missing'}, {'不存在': 'alpha'}]:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, '单独账号任务配置无效'):
                await self.backend.run(account_script_overrides=overrides)
        self.assertFalse(self.backend.factory.calls)

    async def test_cancellation_owns_both_special_and_regular_tasks(self):
        backend = self.backend
        backend.script_ids.append('main_double_auto')
        backend.execution_gate = asyncio.Event()
        runner = asyncio.create_task(backend.run(account_script_overrides=ACCOUNT_SCRIPT_OVERRIDES))
        await asyncio.wait_for(backend.execution_started.wait(), 20)
        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runner
        execute_tasks = [task for task in backend.tasks.list() if task['kind'] == 'execute']
        self.assertEqual(len(execute_tasks), 11)
        self.assertTrue(all(task['state'] == 'cancelled' for task in execute_tasks))
        self.assertEqual(backend.execution_active, 0)
        self.assertEqual(backend.factory.active, 300)

    async def test_poll_timeout_keeps_monitoring_without_resubmitting(self):
        backend = self.backend
        request = backend.request
        timed_out = set()

        async def flaky(method, path, body=None):
            result = await request(method, path, body)
            if (method == 'GET' and path.startswith('/api/v1/tasks/')
                    and result['kind'] == 'execute' and result['task_id'] not in timed_out):
                timed_out.add(result['task_id'])
                raise TimeoutError
            return result

        backend.request = flaky
        result = await backend.run()
        self.assertEqual(len(timed_out), 10)
        self.assertTrue(all(group['state'] == 'succeeded' for group in result['groups']))
        self.assertEqual(len(backend.executions), 300)
        self.assertEqual(sum(path == '/api/v1/tasks' for _, path, _ in backend.calls), 10)

    async def test_stop_recorded_tasks_when_launcher_is_already_gone(self):
        backend = self.backend
        backend.store.tasks['unrelated'] = {'task_id': 'unrelated', 'state': 'running'}
        backend.execution_gate = asyncio.Event()
        connected = await backend.tasks.submit(TaskSpec(kind='connect', account_ids=['id-011', 'id-041']))
        await backend.tasks._runs[connected['task_id']].task
        for account_id in ('id-011', 'id-041'):
            await backend.tasks.submit(TaskSpec(account_ids=[account_id], session_policy='existing',
                                               steps=[{'kind': 'script', 'script_id': 'alpha'}]))
        async with asyncio.timeout(20):
            while backend.execution_active < 2:
                await asyncio.sleep(0)
        owned = {task_id: {'kind': 'execute', 'state': 'running'}
                 for task_id, run in backend.tasks._runs.items() if run.spec.kind == 'execute'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run'
            path.mkdir()
            (path / 'status.json').write_text(json.dumps({'tasks': owned}), encoding='utf-8')
            await stop_latest_workflow(backend, Path(directory), sleep=backend.sleep)
        self.assertTrue(all(backend.tasks.get(task_id)['state'] == 'cancelled' for task_id in owned))
        self.assertEqual(backend.execution_active, 0)
        self.assertEqual(backend.factory.active, 2)
        self.assertEqual(backend.store.tasks['unrelated']['state'], 'running')

    async def test_excluded_offline_and_ready_accounts_never_connect_or_execute(self):
        backend = self.backend
        connected = await backend.tasks.submit(TaskSpec(kind='connect', account_ids=['id-011']))
        await backend.tasks._runs[connected['task_id']].task
        login = backend.store.accounts['id-011']['account']
        offline_login = backend.store.accounts['id-041']['account']
        updates = []
        result = await backend.run(exclude_account_ids=['id-011', 'id-041'], on_update=updates.append)
        self.assertEqual(result['ready'], 298)
        self.assertEqual(result['selected'], 298)
        self.assertEqual(backend.factory.attempts[login], 1)
        self.assertNotIn(offline_login, backend.factory.attempts)
        self.assertFalse({'id-011', 'id-041'} & {row[0] for row in backend.executions})
        self.assertTrue(updates)
        checked = [body for _, path, body in backend.calls if path == '/api/v1/tasks/validate']
        self.assertFalse({'id-011', 'id-041'} & set(checked[0]['account_ids']))

    async def test_unknown_and_all_exclusions_fail_before_network(self):
        with self.assertRaisesRegex(ValueError, '不在固定映射'):
            await self.backend.run(exclude_account_ids=['typo'])
        with self.assertRaisesRegex(ValueError, '全部300'):
            await self.backend.run(exclude_account_ids=list(self.backend.store.accounts))
        self.assertFalse(self.backend.calls)

    async def test_dry_run_reports_exclusions_without_login(self):
        result = await self.backend.run(dry_run=True, exclude_account_ids=['id-011'])
        self.assertEqual(result['accounts'], 299)
        self.assertEqual(result['groups'][0]['accounts'], 29)
        self.assertFalse(self.backend.factory.calls)

    async def test_execution_failure_stops_only_that_account_without_relogin(self):
        backend = self.backend
        execute = backend.tasks.executor

        async def fail_one(account_id, context, steps, **options):
            result = await execute(account_id, context, steps, **options)
            if account_id == 'id-011':
                result.success = False
                result.completed_repetitions = 0
            return result

        backend.tasks.executor = fail_one
        result = await backend.run()
        rows = [row for group in result['groups'] for row in group.get('account_results', [])]
        self.assertEqual(sum(row['state'] == 'failed' for row in rows), 1)
        self.assertEqual(sum(row['state'] == 'succeeded' for row in rows), 299)
        self.assertEqual(sum(row[0] == 'id-011' for row in backend.executions), 1)
        self.assertTrue(all(count == 1 for count in backend.factory.attempts.values()))

    async def test_300_execute_together_after_global_twenty_login_limit(self):
        backend = self.backend
        backend.execution_gate = asyncio.Event()
        runner = asyncio.create_task(backend.run())
        try:
            await asyncio.wait_for(backend.execution_started.wait(), 20)
            self.assertEqual(backend.factory.active, 300)
            self.assertLessEqual(backend.factory.max_logins, 20)
            self.assertEqual(backend.execution_peak, 300)
            backend.execution_gate.set()
            result = await runner
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
        self.assertEqual(result["ready"], 300)
        self.assertTrue(all(group["state"] == "succeeded" for group in result["groups"]))
        self.assertEqual(backend.factory.active, 300)
        for account_id, steps, options in backend.executions:
            group_number = next(group["exit"] for group in backend.manifest["groups"]
                                if account_id in [row["account_id"] for row in group["accounts"]])
            self.assertEqual(options["repetitions"], group_number)
            self.assertEqual(steps[0].script_id, "alpha" if group_number % 2 else "beta")
        self.assertFalse(backend.sessions._claims)

    async def test_retry_only_failed_and_keep_partial_success(self):
        backend = self.backend
        transient = backend.store.accounts['id-011']['account']
        permanent = backend.store.accounts['id-041']['account']
        backend.factory.transient.add(transient)
        backend.factory.permanent.add(permanent)
        result = await backend.run()
        self.assertEqual(result["ready"], 299)
        self.assertEqual(backend.factory.attempts[transient], 2)
        self.assertEqual(backend.factory.attempts[permanent], 5)
        self.assertTrue(all(count == 1 for login, count in backend.factory.attempts.items() if login not in (transient, permanent)))
        self.assertEqual(result["groups"][1]["skipped"], ['违心41'])
        self.assertEqual(backend.delays.count(2), 4)

    async def test_verification_stops_only_its_group_and_others_execute(self):
        backend = self.backend
        backend.factory.verifications.add(backend.store.accounts['id-011']['account'])
        result = await backend.run()
        self.assertEqual(len(result["groups"][0]["connection_tasks"]), 1)
        self.assertIn("verification_required", result["groups"][0]["blocked"])
        self.assertTrue(all(group['ready'] == 30 and group['state'] == 'succeeded' for group in result['groups'][1:]))
        self.assertTrue(all(count <= 1 for login, count in backend.factory.attempts.items()
                            if login in {backend.store.accounts[f'id-{i:03}']['account'] for i in range(11,41)}))

    async def test_no_ready_group_is_skipped_and_existing_ready_does_not_relogin(self):
        backend = self.backend
        connected = await backend.tasks.submit(TaskSpec(kind="connect", account_ids=['id-011']))
        await backend.tasks._runs[connected['task_id']].task
        backend.factory.permanent.update(backend.store.accounts[f'id-{i:03}']['account'] for i in range(41,71))
        result = await backend.run()
        self.assertEqual(backend.factory.attempts[backend.store.accounts['id-011']['account']], 1)
        self.assertEqual(result['groups'][1]['state'], 'skipped')
        self.assertEqual(result['ready'], 270)

    async def test_dry_run_and_invalid_nodes_never_login(self):
        backend = self.backend
        report = await backend.run(dry_run=True)
        self.assertTrue(report['dry_run'])
        self.assertFalse(backend.tasks.list())
        self.assertFalse(backend.factory.calls)
        self.assertFalse(any(path.endswith('/probe') for _, path, _ in backend.calls))
        backend.groups[1]['exit_ip'] = backend.groups[0]['exit_ip']
        with self.assertRaisesRegex(ValueError, '不同'):
            await backend.run()
        self.assertFalse(backend.factory.calls)

    async def test_reconnect_all_replaces_existing_session_only_on_first_attempt(self):
        backend = self.backend
        connected = await backend.tasks.submit(TaskSpec(kind="connect", account_ids=['id-011']))
        await backend.tasks._runs[connected['task_id']].task
        login = backend.store.accounts['id-011']['account']
        result = await backend.run(reconnect_all=True)
        self.assertEqual(result['ready'], 300)
        self.assertEqual(backend.factory.attempts[login], 2)
        self.assertTrue(all(count == 1 for account, count in backend.factory.attempts.items() if account != login))
        self.assertEqual(len(backend.executions), 300)
        self.assertLessEqual(backend.factory.max_logins, 20)

    async def test_mapping_drift_rejected_without_mutation(self):
        self.backend.store.accounts['id-011']['label'] = '改名'
        with self.assertRaisesRegex(ValueError, '映射'):
            await self.backend.run()
        self.assertFalse(self.backend.factory.calls)

    async def test_login_slots_cover_initialization_and_cancel_cleanup(self):
        backend = self.backend
        backend.factory.initialization_gate = asyncio.Event()
        runner = asyncio.create_task(backend.run())
        while backend.factory.active < 20:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(len(backend.factory.calls), 20)
        backend.factory.cleanup_gate = asyncio.Event()
        runner.cancel()
        await asyncio.wait_for(backend.factory.cleanup_started.wait(), 20)
        self.assertEqual(backend.sessions._login_slots._value, 0)
        self.assertEqual(len(backend.factory.calls), 20)
        backend.factory.cleanup_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await runner
        self.assertEqual(backend.factory.active, 0)
        self.assertEqual(backend.sessions._login_slots._value, 20)
        self.assertFalse(backend.sessions._claims)

    async def test_cancel_execution_preserves_300_residents_and_unrelated_task(self):
        backend = self.backend
        backend.store.tasks["unrelated"] = {"task_id": "unrelated", "state": "running"}
        backend.execution_gate = asyncio.Event()
        runner = asyncio.create_task(backend.run())
        await asyncio.wait_for(backend.execution_started.wait(), 20)
        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runner
        self.assertEqual(backend.factory.active, 300)
        self.assertFalse(backend.sessions._claims)
        self.assertEqual(backend.execution_active, 0)
        self.assertEqual(backend.store.tasks["unrelated"]["state"], "running")
        self.assertFalse(any(path == "/api/v1/tasks/unrelated/cancel" for _, path, _ in backend.calls))

