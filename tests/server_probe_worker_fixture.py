"""Real egress subprocess with entirely simulated authentication/game transport."""
import argparse
import asyncio

from src.accounts.login import LoginResult, parse_login_response
from src.network import server_probe
from src.service.worker_process import bind_linux_parent
from tests.egress_worker_fixture import FixtureHost


def login_result(session="fixture-sid", *, port=8000):
    servers = ";".join(f"192.0.2.{number}:{port}:8100:offline.invalid" for number in range(10, 16))
    zones = ";".join(f"{1000 + index} 测试星{index}/{index}/1/0/0/0/1" for index in range(6))
    return parse_login_response(f"<r><c>ok</c><sid>{session}</sid><u>9123456</u><svr>{servers}</svr><zn>{zones}</zn></r>")


async def fake_login(account, password, **options):
    if account.endswith("-blocked"):
        return LoginResult(False, error_msg="fixture captcha", blocked_reason="verification_required")
    return login_result("fixture-sid-" + account)


class FakeSocket:
    active = set()
    peak = 0

    def __init__(self, session_id, **options):
        assert options == {"network_mode": "system"}
        self.session_id = session_id
        self.login_response = {}
        self.disconnect_reason = "fixture connection rejected"

    async def connect_tcp(self, host, port, timeout):
        self.transport = "flash"
        self.active.add(self)
        type(self).peak = max(type(self).peak, len(self.active))
        return True

    async def connect_ws(self, domain, port, timeout):
        await self.connect_tcp(domain, port, timeout)
        self.transport = "h5"
        return True

    async def login(self, zone, user_id, session_id):
        if self.session_id.endswith("-wait"):
            await asyncio.Event().wait()
        if self.transport == "h5" and self.session_id.endswith("-reject"):
            self.login_response = {"_cmd": "logKO", "msg": "fixture maintenance"}
            return None
        self.login_response = {"_cmd": "logOK", "id": 9123456}
        return self.login_response

    async def close(self):
        await asyncio.sleep(.01)
        self.active.discard(self)


class ProbeHost(FixtureHost):
    async def operate(self, command, outcome):
        if command["op"] == "inspect_probes":
            return {"active": len(FakeSocket.active), "peak": FakeSocket.peak, "probing": len(self.probing)}
        return await super().operate(command, outcome)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    arguments = parser.parse_args()
    bind_linux_parent(arguments.parent_pid)
    server_probe.http_login = fake_login
    server_probe.GameSocket = FakeSocket
    asyncio.run(ProbeHost().run())
