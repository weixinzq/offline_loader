"""Reusable initialization file parsing and one-execution-per-account contracts."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from run_cloud_initialization import prepare_initialization, account_results
from run_cloud_workflows import run_workflows
from src.service.contracts import TaskSpec
from src.service.execution import MessageService
from tests.test_cloud_workflows import BackendFixture


class InitializationFileTests(unittest.TestCase):
    def test_latest_file_is_parsed_with_local_instructions_and_json_pipes(self):
        with TemporaryDirectory() as folder:
            file = Path(folder) / "初始化.txt"
            file.write_text('#time=0| ||\n#send={"id":42,"cmd":"example","param":{"text":"a|b"}}|', encoding="utf-8-sig")
            workflows, metadata = prepare_initialization(file)
            self.assertEqual(len(workflows), 10)
            messages = workflows[0]["steps"][0]["messages"]
            self.assertEqual(metadata["message_count"], 2)
            self.assertEqual(messages[1]["param"]["text"], "a|b")
            MessageService().validate(messages)
            old_digest = metadata["file_sha256"]
            file.write_text("#time=1|", encoding="utf-8")
            changed, metadata = prepare_initialization(file)
            self.assertNotEqual(metadata["file_sha256"], old_digest)
            self.assertEqual(metadata["message_count"], 1)
            self.assertEqual(changed[0]["steps"][0]["messages"][0]["param"], {"seconds": 1})

    def test_invalid_file_fails_before_any_backend_call(self):
        with TemporaryDirectory() as folder:
            file = Path(folder) / "初始化.txt"
            file.write_text("#send=invalid|", encoding="utf-8")
            with self.assertRaises(ValueError):
                prepare_initialization(file)


class InitializationWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = BackendFixture()

    async def asyncTearDown(self):
        await self.backend.close()

    async def test_force_reconnect_and_only_one_initialization_per_account(self):
        backend = self.backend
        connected = await backend.tasks.submit(TaskSpec(kind="connect", account_ids=['id-011']))
        await backend.tasks._runs[connected['task_id']].task
        with TemporaryDirectory() as folder:
            file = Path(folder) / '初始化.txt'
            file.write_text('#time=0|', encoding='utf-8')
            workflows, _ = prepare_initialization(file)
        result = await run_workflows(backend, backend.manifest, workflows, reconnect_all=True,
                                     sleep=backend.sleep, log=lambda value: None)
        self.assertEqual(result['ready'], 300)
        self.assertEqual(len(backend.executions), 300)
        self.assertTrue(all(options['repetitions'] == 1 and steps[0].kind == 'messages'
                            for _, steps, options in backend.executions))
        self.assertEqual(backend.factory.attempts[backend.store.accounts['id-011']['account']], 2)
        self.assertEqual(backend.factory.max_logins, 20)
        rows = account_results(backend.manifest, result)
        self.assertEqual(len(rows), 300)
        self.assertTrue(all(row['state'] == 'succeeded' for row in rows))

    async def test_dropped_before_execution_is_retried_within_account_budget(self):
        backend = self.backend
        backend.factory.permanent.add(backend.store.accounts['id-041']['account'])
        original_request = backend.request
        snapshots = 0

        async def request(method, path, body=None):
            nonlocal snapshots
            record = await original_request(method, path, body)
            if method == 'GET' and path == '/api/v1/sessions':
                snapshots += 1
                if snapshots == 2:
                    await backend.sessions.disconnect('id-011')
                    record = await original_request(method, path, body)
            return record

        backend.request = request
        result = await backend.run(reconnect_all=True)
        self.assertEqual(result['ready'], 299)
        self.assertEqual(backend.factory.attempts[backend.store.accounts['id-011']['account']], 2)
        self.assertEqual(backend.factory.attempts[backend.store.accounts['id-041']['account']], 5)
        self.assertEqual(sum(account == 'id-011' for account, _, _ in backend.executions), 1)
        self.assertEqual(max(count for group in result['groups'] for count in group['connection_attempts'].values()), 5)

    async def test_old_five_concurrency_backend_is_rejected_before_login(self):
        self.backend.sessions.login_concurrency = 5
        with self.assertRaisesRegex(ValueError, '实际登录并发为 5'):
            await self.backend.run(dry_run=True)
        self.assertFalse(self.backend.factory.calls)
        self.assertFalse(self.backend.tasks.list())

    async def test_blocked_group_cannot_initialize_an_old_session_without_reconnecting(self):
        backend = self.backend
        connected = await backend.tasks.submit(TaskSpec(kind='connect', account_ids=['id-040']))
        await backend.tasks._runs[connected['task_id']].task
        backend.factory.verifications.add(backend.store.accounts['id-011']['account'])
        result = await backend.run(reconnect_all=True)
        self.assertIn('verification_required', result['groups'][0]['blocked'])
        self.assertNotIn('id-040', [account for account, _, _ in backend.executions])
        self.assertEqual(backend.factory.attempts[backend.store.accounts['id-040']['account']], 1)
        self.assertTrue(all(group['state'] == 'succeeded' for group in result['groups'][1:]))


if __name__ == '__main__':
    unittest.main()
