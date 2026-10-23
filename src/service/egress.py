"""Outbound configuration and account assignment, without operating Clash."""
from uuid import uuid4
import yaml

from src.service.contracts import EgressGroupInput, ServiceError
from src.service.worker_process import prepare_worker_executable


class EgressService:
    def __init__(self, store, sessions, workers, events):
        self.store, self.sessions, self.workers, self.events = store, sessions, workers, events
        self._changing = set()

    def get(self, group_id):
        group = self.store.get_egress_group(group_id)
        return {**group, **self.workers.profile(group_id), **self.sessions.egress_usage(group_id)}

    def list(self):
        return [self.get(group["group_id"]) for group in self.store.list_egress_groups()]

    def check(self, group_id, *, connecting=False):
        if group_id in self._changing:
            raise ServiceError("egress_changing", "出口分组正在修改，请稍后重试", 409)
        group = self.store.get_egress_group(group_id)
        if connecting and not group["routing_confirmed"]:
            raise ServiceError("egress_unconfirmed", f"出口分组“{group['name']}”尚未确认 Clash 规则", 409)
        return group

    def create(self, value):
        value = value if isinstance(value, EgressGroupInput) else EgressGroupInput.model_validate(value)
        if any(group["name"] == value.name for group in self.store.list_egress_groups()):
            raise ServiceError("egress_duplicate", "出口分组名称已存在", 409)
        group_id = uuid4().hex
        prepare_worker_executable(self.store.directory, group_id)
        self.store.save_egress_group(group_id, value.model_dump())
        self.events.publish("egress", {"group_id": group_id})
        return self.get(group_id)

    def _idle(self, group_id):
        if any(self.sessions.is_busy(account["account_id"]) for account in self.store.list_accounts()
               if account.get("egress_group_id") == group_id):
            raise ServiceError("egress_busy", "分组存在在线账号或任务占用，请先断开并等待清理", 409)

    async def update(self, group_id, value):
        original = self.check(group_id)
        self._idle(group_id)
        value = value if isinstance(value, EgressGroupInput) else EgressGroupInput.model_validate(value)
        if any(group["name"] == value.name and group["group_id"] != group_id for group in self.store.list_egress_groups()):
            raise ServiceError("egress_duplicate", "出口分组名称已存在", 409)
        if original["clash_policy"] != value.clash_policy:
            value = value.model_copy(update={"routing_confirmed": False})
        self._changing.add(group_id)
        try:
            await self.workers.stop(group_id)
            self.store.save_egress_group(group_id, value.model_dump())
            self.events.publish("egress", {"group_id": group_id})
            return self.get(group_id)
        finally:
            self._changing.discard(group_id)

    async def delete(self, group_id):
        self.check(group_id)
        self._idle(group_id)
        if any(account.get("egress_group_id") == group_id for account in self.store.list_accounts()):
            raise ServiceError("egress_in_use", "分组仍有关联账号，请先移出账号", 409)
        self._changing.add(group_id)
        try:
            await self.workers.stop(group_id)
            self.store.delete_egress_group(group_id)
            self.events.publish("egress", {"group_id": group_id, "deleted": True})
        finally:
            self._changing.discard(group_id)

    async def probe(self, group_id):
        self.check(group_id)
        await self.workers.probe(group_id)
        return self.get(group_id)

    def rules(self):
        groups = self.store.list_egress_groups()
        rules = [f"PROCESS-PATH,{prepare_worker_executable(self.store.directory, group['group_id'])},{group['clash_policy']}"
                 for group in groups]
        return {"yaml": yaml.safe_dump({"mode": "rule", "find-process-mode": "strict", "rules": rules},
                                       allow_unicode=True, sort_keys=False),
                "instructions": "将 rules 合并到 Clash 当前规则最前面；策略名称必须已存在。程序不会自动修改 Clash。"}
