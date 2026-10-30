"""Account configuration ownership, independent of live sessions."""
from src.accounts.manager import load_account_specs
from src.service.contracts import AccountInput, AccountPatch, ServiceError


class AccountService:
    def __init__(self, store, sessions, events, egress=None):
        self.store, self.sessions, self.events = store, sessions, events
        self.egress = egress

    def list(self):
        return self.store.list_accounts()

    def get(self, account_id):
        return self.store.get_account(account_id)

    @staticmethod
    def _normalize(payload):
        value = AccountInput.model_validate(payload)
        value.label = value.label.strip()
        value.account = value.account.strip()
        # Reuse the existing credential and label rules, without file access.
        load_account_specs({"accounts": [value.model_dump()]})
        return value.model_dump()

    def _unique_label(self, label, account_id=None):
        if any(row["label"] == label and row["account_id"] != account_id for row in self.list()):
            raise ServiceError("duplicate_label", "账号标签已存在", 409)

    def create(self, payload):
        try:
            raw = payload.model_dump() if isinstance(payload, AccountInput) else payload
            config = self._normalize(raw)
            self._unique_label(config["label"])
            row = self.store.create_account(config)
        except ValueError as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError("invalid_account", "账号字段无效，请检查标签、凭据及区服") from exc
        self.events.publish("account", row)
        return row

    def _assert_idle(self, account_id):
        if self.sessions.is_busy(account_id):
            raise ServiceError("account_busy", "账号在线或正被任务占用，请先断开并等待清理", 409)

    def update(self, account_id, payload):
        self._assert_idle(account_id)
        original = self.store.get_account(account_id, private=True)
        patch = payload if isinstance(payload, AccountPatch) else AccountPatch.model_validate(payload)
        raw = {key: value for key, value in original.items() if key != "account_id"}
        raw.update(patch.model_dump(exclude_none=True))
        try:
            config = self._normalize(raw)
        except ValueError as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError("invalid_account", "账号字段无效") from exc
        self._unique_label(config["label"], account_id)
        row = self.store.update_account(account_id, config)
        self.events.publish("account", row)
        return row

    def delete(self, account_id):
        self._assert_idle(account_id)
        self.store.delete_account(account_id)
        self.events.publish("account", {"account_id": account_id, "deleted": True})

    def assign_egress(self, account_ids, group_id):
        raise ServiceError("egress_assignment_retired", "账号统一从 IP 池申请出口，不再固定分组", 410)

    def import_config(self, config):
        try:
            specs = load_account_specs(config)
            inputs = [self._normalize({key: spec.config[key] for key in AccountInput.model_fields if key in spec.config} | {"label": spec.label}) for spec in specs]
            labels = [row["label"] for row in inputs]
            if len(set(labels)) != len(labels):
                raise ValueError("duplicate labels")
            for row in inputs:
                self._unique_label(row["label"])
        except ValueError as exc:
            raise ServiceError("invalid_import", "导入配置无效或标签重复；原文件未修改") from exc
        rows = [self.create(row) for row in inputs]
        return {"imported": len(rows), "accounts": rows, "source_modified": False,
                "omitted_fields": sorted(set(config) - {"accounts", *AccountInput.model_fields})}
