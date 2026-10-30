"""Explicitly enabled, verified exits shared by all account connections."""
from ipaddress import ip_address
from uuid import uuid4
import yaml

from src.service.contracts import EgressGroupInput, ServiceError
from src.service.worker_process import prepare_worker_executable


class EgressService:
    def __init__(self, store, sessions, workers, events):
        self.store, self.sessions, self.workers, self.events = store, sessions, workers, events
        self._changing = set()
        self._verified = set()

    def get(self, group_id):
        group = self.store.get_egress_group(group_id)
        profile = self.workers.profile(group_id)
        return {**group, "max_online": 25, "enabled": group.get("enabled", False),
                **profile, **self.sessions.egress_usage(group_id),
                "active": group_id in self._verified and group.get("enabled", False)
                          and group["routing_confirmed"] and group_id not in self._changing,
                "waiting_count": self.sessions.waiting_count}

    def list(self):
        return [self.get(group["group_id"]) for group in self.store.list_egress_groups()]

    def available(self):
        return [group for group in self.list() if group["active"] and group.get("exit_ip")]

    def check(self, group_id, *, connecting=False):
        if group_id in self._changing:
            raise ServiceError("egress_changing", "出口正在修改，请稍后重试", 409)
        group = self.get(group_id)
        if connecting and not group["active"]:
            raise ServiceError("egress_unconfirmed", "出口需要检测、确认规则并启用", 409)
        return group

    def _enable_check(self, group_id, value):
        if not value.enabled:
            return
        if not value.routing_confirmed or group_id not in self._verified:
            raise ServiceError("egress_unconfirmed", "请先检测出口并确认 Clash 规则", 409)
        ip = self.workers.profile(group_id)["exit_ip"]
        for group in self.list():
            if group["group_id"] != group_id and group.get("exit_ip") == ip:
                if group["enabled"] or group["online_count"]:
                    raise ServiceError("egress_duplicate_ip", "相同公网 IP 已在池中启用或仍有连接", 409)
        for session in self.sessions._sessions.values():
            if session.slot and session.egress_group_id != group_id and session.exit_ip == ip:
                raise ServiceError("egress_duplicate_ip", "该公网 IP 仍有其他出口的连接", 409)

    def create(self, value):
        value = value if isinstance(value, EgressGroupInput) else EgressGroupInput.model_validate(value)
        if value.enabled:
            raise ServiceError("egress_unconfirmed", "新出口须先检测并确认后启用", 409)
        if any(group["name"] == value.name for group in self.list()):
            raise ServiceError("egress_duplicate", "出口名称已存在", 409)
        group_id = uuid4().hex
        prepare_worker_executable(self.store.directory, group_id)
        self.store.save_egress_group(group_id, value.model_dump())
        self.events.publish("egress", {"group_id": group_id})
        return self.get(group_id)

    def _idle(self, group_id):
        if self.sessions.egress_usage(group_id)["online_count"]:
            raise ServiceError("egress_busy", "出口仍有连接或正在清理，请先停用并断开", 409)

    async def update(self, group_id, value):
        original = self.check(group_id)
        value = value if isinstance(value, EgressGroupInput) else EgressGroupInput.model_validate(value)
        if any(g["name"] == value.name and g["group_id"] != group_id for g in self.list()):
            raise ServiceError("egress_duplicate", "出口名称已存在", 409)
        route_changed = original["clash_policy"] != value.clash_policy
        if route_changed:
            self._idle(group_id)
            value = value.model_copy(update={"routing_confirmed": False, "enabled": False})
        self._enable_check(group_id, value)
        self._changing.add(group_id)
        try:
            if route_changed:
                self._verified.discard(group_id)
                await self.workers.stop(group_id)
            self.store.save_egress_group(group_id, value.model_dump())
        finally:
            self._changing.discard(group_id)
        self.events.publish("egress", {"group_id": group_id})
        self.sessions.wake()
        return self.get(group_id)

    async def delete(self, group_id):
        self.check(group_id)
        self._idle(group_id)
        self._changing.add(group_id)
        try:
            self._verified.discard(group_id)
            await self.workers.stop(group_id)
            self.store.delete_egress_group(group_id)
        finally:
            self._changing.discard(group_id)
        self.events.publish("egress", {"group_id": group_id, "deleted": True})
        self.sessions.wake()

    def failed(self, group_id):
        self._verified.discard(group_id)
        group = self.store.get_egress_group(group_id)
        self.store.save_egress_group(group_id, {**group, "enabled": False})
        self.events.publish("egress", {"group_id": group_id})
        self.sessions.wake()

    async def probe(self, group_id):
        self.check(group_id)
        self._idle(group_id)
        self._verified.discard(group_id)
        self._changing.add(group_id)
        try:
            result = await self.workers.probe(group_id)
            self.workers.worker(group_id).exit_ip = str(ip_address(result["exit_ip"]))
            self._verified.add(group_id)
            group = self.store.get_egress_group(group_id)
            self._enable_check(group_id, EgressGroupInput.model_validate(
                {k: v for k, v in group.items() if k in EgressGroupInput.model_fields}))
        except BaseException:
            self.failed(group_id)
            raise
        finally:
            self._changing.discard(group_id)
        self.sessions.wake()
        return self.get(group_id)

    def rules(self):
        rules = [f"PROCESS-PATH,{prepare_worker_executable(self.store.directory, g['group_id'])},{g['clash_policy']}"
                 for g in self.store.list_egress_groups()]
        return {"yaml": yaml.safe_dump({"mode": "rule", "find-process-mode": "strict", "rules": rules},
                                       allow_unicode=True, sort_keys=False),
                "instructions": "将 rules 合并到 Clash 规则最前面。直连出口使用 DIRECT；程序不自动改写 Clash。"}
