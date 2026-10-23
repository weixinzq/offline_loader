"""Battle entry records only its own task status and forwards exclusions."""
import asyncio
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from run_cloud_workflows import main_async


class BattleEntryTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, cancel=False):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / 'manifest.json'
            manifest.write_text('{}')
            options = SimpleNamespace(manifest=manifest, url='http://127.0.0.1:8765',
                                      dry_run=False, stop_owned=False, exclude_account_id=['excluded'])
            row = {'account_id': 'ready', 'state': 'succeeded', 'completed_repetitions': 9999, 'error': None}

            async def execute(api, mapping, workflows, **kwargs):
                kwargs['on_update']({'task_id': 'owned', 'kind': 'execute',
                                     'state': 'succeeded', 'account_results': [row]})
                if cancel:
                    raise asyncio.CancelledError
                return {'selected': 1, 'ready': 1, 'groups': [{'account_results': [row]}]}

            runner = AsyncMock(side_effect=execute)
            with patch('run_cloud_workflows.ROOT', root), \
                 patch('run_cloud_workflows.make_specs'), \
                 patch('run_cloud_workflows.ensure_backend', new_callable=AsyncMock), \
                 patch('run_cloud_workflows.run_workflows', runner), \
                 patch.dict('os.environ', {'AOLA_ADMIN_TOKEN': 'test-token'}), \
                 patch('sys.stdout', new_callable=io.StringIO):
                if cancel:
                    with self.assertRaises(asyncio.CancelledError):
                        await main_async(options)
                else:
                    self.assertEqual(await main_async(options), 0)
            self.assertEqual(runner.call_args.kwargs['exclude_account_ids'], ['excluded'])
            status_file, = (root / 'logs/cloud-battles').glob('*/status.json')
            status = json.loads(status_file.read_text())
            self.assertEqual(status['state'], 'cancelled' if cancel else 'completed')
            self.assertEqual(status['tasks']['owned']['accounts'][0]['completed_repetitions'], 9999)
            self.assertNotIn('test-token', status_file.read_text())
            self.assertFalse((root / 'logs/cloud-initialization').exists())

    async def test_battle_status_and_exclusions(self):
        await self.exercise()

    async def test_cancellation_is_recorded(self):
        await self.exercise(cancel=True)
