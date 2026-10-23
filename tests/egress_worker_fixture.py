"""Subprocess fixture: real router/scripts with memory-only game traffic."""
import argparse
import asyncio

from src.accounts.login import LoginBlockedError
from src.service.worker_host import WorkerHost
from src.service.worker_process import bind_linux_parent
from tests.test_service_integration import InMemoryGame


class FixtureGame(InMemoryGame):
    async def open_context(self, config):
        if config['account'].endswith('-verification'):
            raise LoginBlockedError('verification_required', '模拟需要人工验证')
        if config['account'].endswith('-init-fail'):
            raise ConnectionError('模拟初始化失败')
        self.respond = not config['account'].endswith('-wait')
        context = await super().open_context(config)
        original_send = context.socket.send_xt_message
        battle_number = 0
        if config['account'].endswith('-recovery'):
            from src.messaging import battle_control
            battle_control.WAIT_TIMEOUT_SECONDS = .02

        async def send(ext_id, command, params):
            nonlocal battle_number
            result = await original_send(ext_id, command, params)
            if command == 'fixture-battle':
                battle_number += 1
                context.messages.publish({'_cmd': '2303', 'battleId': battle_number,
                    'pmmList': [{'battleView': {'pmmId': int(context.user_id), 'slotId': 7, 'pmList': []}}]})
                context.messages.publish({'_cmd': '2401', 'battleUniqueId': battle_number + 100})
                context.messages.publish({'_cmd': '2426'})
                context.messages.publish({'_cmd': '2402', 'pt': 0})
            context.log(f'fixture sent {command}')
            context.log(f"fixture sid={context.login_result.session_id} password={config['password']}")
            return result

        context.socket.send_xt_message = send
        # Deliberate noisy output proves that stdout cannot corrupt the RPC stream.
        print('fixture password=' + config['password'])
        return context


class FixtureHost(WorkerHost):
    def __init__(self):
        super().__init__(lambda spec: FixtureGame()(spec))

    async def operate(self, command, outcome):
        if command['op'] == 'probe':
            return {'exit_ip': '192.0.2.10'}
        return await super().operate(command, outcome)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent-pid', type=int, required=True)
    arguments = parser.parse_args()
    bind_linux_parent(arguments.parent_pid)
    asyncio.run(FixtureHost().run())
