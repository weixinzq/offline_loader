"""Killable parent used only by the worker lifetime regression test."""
import asyncio
import json
from pathlib import Path
import sys

from src.service.application import Application
from src.service.contracts import TaskSpec


async def run(directory):
    application = Application(directory, worker_module='tests.egress_worker_fixture')
    await application.start()
    try:
        group = application.egress.create({'name': 'lifetime-fixture', 'clash_policy': 'offline-node', 'routing_confirmed': True})
        account = application.accounts.create({'label': 'mock', 'account': 'offline-lifetime',
            'password': 'fixture-password', 'egress_group_id': group['group_id']})
        task = await application.tasks.submit(TaskSpec(kind='connect', account_ids=[account['account_id']]))
        await application.tasks._runs[task['task_id']].task
        record = application.tasks.get(task['task_id'])
        if record['state'] != 'succeeded':
            raise RuntimeError(record)
        worker = application.workers.worker(group['group_id'])
        print(json.dumps({'worker_pid': worker.process.pid}), flush=True)
        await asyncio.Event().wait()
    finally:
        await application.close()


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1])))
