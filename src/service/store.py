"""SQLite persistence. Runtime sessions never live in the database."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import Column, Integer, JSON, MetaData, String, Table, create_engine, select, event

from src.service.contracts import ServiceError
from src.service.events import utc_now


metadata = MetaData()
accounts = Table("accounts", metadata, Column("account_id", String, primary_key=True), Column("config", JSON, nullable=False))
tasks = Table("tasks", metadata, Column("task_id", String, primary_key=True), Column("record", JSON, nullable=False))
egress_groups = Table("egress_groups", metadata, Column("group_id", String, primary_key=True), Column("config", JSON, nullable=False))
logs = Table("logs", metadata, Column("id", Integer, primary_key=True, autoincrement=True),
             Column("task_id", String, index=True), Column("account_id", String),
             Column("timestamp", String, nullable=False), Column("message", String, nullable=False))
_SECRET_KEYS = {"password", "sid", "session_id", "session-id", "token", "authorization", "secret", "loginkey"}
_GENERATED_FIELDS = {"task_id", "account_id", "account_ids", "script_id", "created_at", "updated_at",
                     "started_at", "finished_at", "timestamp", "state", "kind", "mode", "session_policy",
                     "status", "business_confirmation", "code", "egress_group_id"}


class Store:
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.directory.chmod(0o700)
        self._secrets = set()
        self._secret_pattern = None
        db = self.directory / "service.sqlite3"
        self.engine = create_engine("sqlite:///" + db.as_posix(), connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")

        config = Config()
        config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        if os.name != "nt":
            db.chmod(0o600)
        for row in self.list_accounts(private=True):
            self._remember(row)

    def _remember(self, account):
        self.add_secret(account.get("password"))
        self.add_secret(account.get("account"))

    def add_secret(self, value):
        if value:
            value = str(value)
            additions = {value, hashlib.md5(value.encode()).hexdigest()}
            if not additions.issubset(self._secrets):
                self._secrets.update(additions)
                self._secret_pattern = None

    def redact(self, value, *, task_record=False):
        if isinstance(value, dict):
            return {key: deepcopy(item) if task_record and key in _GENERATED_FIELDS and (
                        isinstance(item, str) or item is None or
                        key == "account_ids" and isinstance(item, list) and all(isinstance(id_, str) for id_ in item)) else
                    "<redacted>" if str(key).lower() in _SECRET_KEYS else self.redact(item, task_record=task_record)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item, task_record=task_record) for item in value]
        if isinstance(value, str):
            if self._secrets:
                if self._secret_pattern is None:
                    self._secret_pattern = re.compile("|".join(re.escape(secret) for secret in sorted(self._secrets, key=len, reverse=True)))
                value = self._secret_pattern.sub("<redacted>", value)
            return re.sub(r"(?i)(sid|session[_-]id|password|token)([\"']?\s*[:=]\s*[\"']?)[^\s,\"'&}]+", r"\1\2<redacted>", value)
        return value

    @staticmethod
    def _account(row, private):
        result = {"account_id": row["account_id"], **deepcopy(row["config"])}
        if not private:
            result.pop("password", None)
        return result

    def list_accounts(self, private=False):
        with self.engine.connect() as connection:
            return [self._account(row, private) for row in connection.execute(select(accounts)).mappings()]

    def get_account(self, account_id, private=False):
        with self.engine.connect() as connection:
            row = connection.execute(select(accounts).where(accounts.c.account_id == account_id)).mappings().first()
        if row is None:
            raise ServiceError("not_found", "账号不存在", 404)
        return self._account(row, private)

    def create_account(self, config):
        account_id = uuid4().hex
        with self.engine.begin() as connection:
            connection.execute(accounts.insert().values(account_id=account_id, config=config))
        self._remember(config)
        return self.get_account(account_id)

    def update_account(self, account_id, config):
        self.get_account(account_id)
        with self.engine.begin() as connection:
            connection.execute(accounts.update().where(accounts.c.account_id == account_id).values(config=config))
        self._remember(config)
        return self.get_account(account_id)

    def delete_account(self, account_id):
        self.get_account(account_id)
        with self.engine.begin() as connection:
            connection.execute(accounts.delete().where(accounts.c.account_id == account_id))

    def list_egress_groups(self):
        with self.engine.connect() as connection:
            return [{"group_id": row.group_id, **row.config} for row in connection.execute(select(egress_groups))]

    def get_egress_group(self, group_id):
        group = next((row for row in self.list_egress_groups() if row["group_id"] == group_id), None)
        if group is None:
            raise ServiceError("egress_not_found", "出口分组不存在", 404)
        return group

    def save_egress_group(self, group_id, config):
        with self.engine.begin() as connection:
            exists = connection.execute(select(egress_groups.c.group_id).where(egress_groups.c.group_id == group_id)).first()
            if exists:
                connection.execute(egress_groups.update().where(egress_groups.c.group_id == group_id).values(config=config))
            else:
                connection.execute(egress_groups.insert().values(group_id=group_id, config=config))
        return self.get_egress_group(group_id)

    def delete_egress_group(self, group_id):
        self.get_egress_group(group_id)
        with self.engine.begin() as connection:
            connection.execute(egress_groups.delete().where(egress_groups.c.group_id == group_id))

    def save_task(self, record):
        # Task parameters are already replaced by _snapshot. Preserve generated
        # identifiers/enums even when a password is a single character.
        record = self.redact(deepcopy(record), task_record=True)
        with self.engine.begin() as connection:
            exists = connection.execute(select(tasks.c.task_id).where(tasks.c.task_id == record["task_id"])).first()
            if exists:
                connection.execute(tasks.update().where(tasks.c.task_id == record["task_id"]).values(record=record))
            else:
                connection.execute(tasks.insert().values(task_id=record["task_id"], record=record))

    def get_task(self, task_id):
        with self.engine.connect() as connection:
            row = connection.execute(select(tasks.c.record).where(tasks.c.task_id == task_id)).first()
        if row is None:
            raise ServiceError("not_found", "任务不存在", 404)
        return deepcopy(row[0])

    def list_tasks(self):
        with self.engine.connect() as connection:
            records = [deepcopy(row[0]) for row in connection.execute(select(tasks.c.record))]
        return sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)

    def interrupt_pending(self):
        for record in self.list_tasks():
            if record["state"] in {"queued", "running", "cancelling"}:
                record.update(state="interrupted", updated_at=utc_now(), error={"code": "service_restart", "message": "服务重启，任务未重放"})
                for row in record.get("account_results", []):
                    if row.get("state") not in {"succeeded", "completed", "failed", "cancelled", "skipped"}:
                        row["state"] = "interrupted"
                self.save_task(record)

    def delete_tasks(self, task_ids):
        if not task_ids:
            return
        with self.engine.begin() as connection:
            connection.execute(logs.delete().where(logs.c.task_id.in_(task_ids)))
            connection.execute(tasks.delete().where(tasks.c.task_id.in_(task_ids)))

    def append_log(self, task_id, account_id, message):
        row = {"task_id": task_id, "account_id": account_id, "timestamp": utc_now(), "message": self.redact(str(message))}
        with self.engine.begin() as connection:
            result = connection.execute(logs.insert().values(**row))
        return {"id": result.inserted_primary_key[0], **row}

    def get_logs(self, task_id, after=0, limit=200):
        query = select(logs).where(logs.c.task_id == task_id, logs.c.id > after).order_by(logs.c.id).limit(min(max(limit, 1), 1000))
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def close(self):
        self.engine.dispose()
