"""Account sessions and atomic ownership shared by every application entry point."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import re
from typing import Callable
from urllib.parse import quote_plus

from src.accounts.manager import AccountConnection, AccountSpec
from src.service.contracts import ServiceError


def account_identity(account: dict) -> str:
    return str(account["account"]).strip().casefold()


class Redactor:
    """Keep credentials out of errors before they reach events or persistence."""

    _secret_keys = frozenset({
        "account", "username", "password", "pwd", "sid", "token", "sessionid",
        "session_id", "session-id", "authorization", "loginkey", "wytoken",
        "secret", "ticket", "randstr",
    })

    def __init__(self, store, *values):
        self.store = store
        self.values: set[str] = set()
        for value in values:
            self.collect(value)

    def add(self, value) -> None:
        if value is None or value == "":
            return
        value = str(value)
        self.values.update({value, quote_plus(value), hashlib.md5(value.encode()).hexdigest()})
        add_secret = getattr(self.store, "add_secret", None)
        if add_secret is not None:
            add_secret(value)

    def collect(self, value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in self._secret_keys:
                    self.add(item)
                else:
                    self.collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.collect(item)

    def clean(self, value: object) -> str:
        text = str(value)
        for secret in sorted(self.values, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        text = re.sub(
            r'(?i)(["\']?\b(?:sid|token|session_?id|password|pwd|loginkey|authorization|ticket|randstr)["\']?\s*[:=]\s*["\']?)[^"\'\s<>&;,}]+',
            r"\1[redacted]", text,
        )
        text = re.sub(
            r"(?is)(<(?:sid|token|sessionId|password|loginkey)\b[^>]*>).*?(</(?:sid|token|sessionId|password|loginkey)\s*>)",
            r"\1[redacted]\2", text,
        )
        redact = getattr(self.store, "redact", None)
        return redact(text) if redact is not None else text


@dataclass
class _Session:
    account_id: str
    identity: str
    connection: object
    redactor: Redactor
    state: str = "offline"
    error: str | None = None
    owner_task_id: str | None = None
    slot: bool = False
    cleanup: asyncio.Task | None = None
    egress_group_id: str | None = None


class SessionService:
    def __init__(self, store, events, connection_factory=AccountConnection, max_online=100,
                 login_concurrency=None):
        self.store = store
        self.events = events
        self.connection_factory = connection_factory
        self.max_online = max_online
        self.login_concurrency = max_online if login_concurrency is None else login_concurrency
        if self.max_online < 1 or self.login_concurrency < 1:
            raise ValueError("在线容量和登录并发必须为正整数")
        self._login_slots = asyncio.Semaphore(self.login_concurrency)
        self._sessions: dict[str, _Session] = {}
        self._claims: dict[str, str] = {}
        self._claim_accounts: dict[str, str] = {}
        self._reservations: dict[str, int] = {}
        self._group_reservations: dict[str, dict] = {}
        self.egress = None
        self._lock = asyncio.Lock()
        self._maintenance: set[asyncio.Task] = set()
        self.on_log: Callable[[str, str, str], None] | None = None
        self.on_blocked: Callable[[str, str], None] | None = None

    def _publish(self, session: _Session) -> None:
        self.events.publish("session", self._public(session))

    @staticmethod
    def _public(session: _Session) -> dict:
        return {
            "account_id": session.account_id, "state": session.state,
            "error": session.error, "owner_task_id": session.owner_task_id,
            "egress_group_id": session.egress_group_id,
        }

    def list(self) -> list[dict]:
        rows = []
        for account in self.store.list_accounts():
            session = self._sessions.get(account["account_id"])
            if session is not None and session.state == "ready" and not session.connection.online:
                self._observe_disconnect(session,
                    getattr(session.connection, "last_error", "") or "连接已经断开"
                )
            row = self._public(session) if session else {
                "account_id": account["account_id"], "state": "offline",
                "error": None, "owner_task_id": None,
            }
            rows.append({**row, "label": account.get("label", ""), "egress_group_id": account.get("egress_group_id")})
        return rows

    def egress_usage(self, group_id):
        return {"online_count": sum(session.slot and session.egress_group_id == group_id for session in self._sessions.values()),
                "reserved_count": sum(value.get(group_id, 0) for value in self._group_reservations.values())}

    def execution_limits(self, accounts):
        groups = {}
        for account in accounts:
            group_id = account.get("egress_group_id")
            groups[group_id] = self.egress.check(group_id)["max_online"] if group_id else self.max_online
        return groups

    def _group_needs(self, spec, accounts):
        counts = {}
        for account in accounts:
            needs = spec.kind == "server_probe" or spec.kind == "execute" and spec.session_policy == "temporary"
            if spec.kind in {"connect", "reconnect"}:
                needs = not any(session.slot and session.account_id == account["account_id"] for session in self._sessions.values())
            if needs:
                group_id = account.get("egress_group_id")
                counts[group_id] = counts.get(group_id, 0) + 1
        if spec.kind in {"execute", "server_probe"} and spec.mode == "bounded":
            limits = self.execution_limits(accounts)
            counts = {group: min(count, spec.concurrency, limits[group]) for group, count in counts.items()}
        return counts

    def is_busy(self, account_id: str) -> bool:
        session = self._sessions.get(account_id)
        if account_id in self._claim_accounts or (session is not None and session.slot):
            return True
        account = self.store.get_account(account_id, private=True)
        identity = account_identity(account)
        return identity in self._claims or any(
            item.identity == identity and item.slot for item in self._sessions.values()
        )

    def _check(self, spec, accounts: list[dict]) -> tuple[int, list[dict], list[dict]]:
        identities = [account_identity(account) for account in accounts]
        if len(set(identities)) != len(identities):
            raise ServiceError("duplicate_account", "同一登录账号不能在一个任务中重复出现", 409)
        eligible, skipped = [], []
        for account, identity in zip(accounts, identities):
            name = account.get("label") or account["account"]
            if spec.kind == "execute" and spec.session_policy == "existing":
                session = self._sessions.get(account["account_id"])
                if (session is None or not session.slot or session.state != "ready"
                        or not session.connection.online):
                    skipped.append({"account_id": account["account_id"], "label": name,
                                    "reason": "连接已断开或尚未初始化完成，已跳过"})
                    continue
            eligible.append(account)
            group_id = account.get("egress_group_id")
            if spec.kind == "server_probe" and not group_id:
                raise ServiceError("egress_required", "服务器测试账号必须属于已确认的代理出口分组", 409)
            if group_id and spec.kind != "disconnect":
                self.egress.check(group_id, connecting=True)
            if identity in self._claims:
                raise ServiceError("account_busy", f"账号“{name}”正被其他任务占用", 409)
            existing = next((item for item in self._sessions.values()
                             if item.identity == identity and item.slot), None)
            if existing is not None and existing.account_id != account["account_id"]:
                raise ServiceError("session_conflict", "同一登录账号已有其他配置的会话", 409)
            if existing is not None and existing.state == "disconnecting" and spec.kind != "disconnect":
                raise ServiceError("session_conflict", "会话正在清理，请等待断开完成", 409)
            if spec.kind == "server_probe" and existing is not None:
                raise ServiceError("session_conflict", "服务器测试只能使用离线账号", 409)
            if spec.kind == "execute":
                if spec.session_policy == "temporary" and existing is not None:
                    raise ServiceError("session_conflict", "临时任务不能借用已存在的会话", 409)
            elif spec.kind == "connect" and existing is not None and (
                existing.state != "ready" or not existing.connection.online
            ):
                raise ServiceError("session_conflict", "会话尚未就绪，请先断开或重连", 409)

        if not eligible:
            names = "、".join(row["label"] for row in skipped)
            raise ServiceError("session_not_ready", f"没有可执行的就绪会话：{names}。请先连接这些账号。", 409)
        group_needs = self._group_needs(spec, eligible)
        for group_id, count in group_needs.items():
            if group_id:
                group = self.egress.check(group_id)
                usage = self.egress_usage(group_id)
                if usage["online_count"] + usage["reserved_count"] + count > group["max_online"]:
                    raise ServiceError("egress_capacity_exceeded", f"出口分组“{group['name']}”在线及预留连接不能超过 {group['max_online']} 个", 409)
        needed = sum(group_needs.values())
        if spec.kind in {"execute", "server_probe"} and spec.mode == "bounded":
            needed = min(needed, spec.concurrency)
        used = sum(item.slot for item in self._sessions.values()) + sum(self._reservations.values())
        if used + needed > self.max_online:
            raise ServiceError("capacity_exceeded", f"在线及预留连接不能超过 {self.max_online} 个", 409)
        return needed, eligible, skipped

    def check(self, spec, accounts: list[dict]) -> dict:
        needed, eligible, skipped = self._check(spec, accounts)
        return {"required_capacity": needed, "max_online": self.max_online,
                "login_concurrency": self.login_concurrency,
                "account_count": len(eligible), "skipped_accounts": skipped}

    async def reserve(self, task_id: str, spec, accounts: list[dict]) -> list[dict]:
        async with self._lock:
            needed, eligible, skipped = self._check(spec, accounts)
            for account in eligible:
                self._claims[account_identity(account)] = task_id
                self._claim_accounts[account["account_id"]] = task_id
            self._reservations[task_id] = needed
            self._group_reservations[task_id] = self._group_needs(spec, eligible)
            return skipped

    def release(self, task_id: str) -> None:
        self._claims = {key: owner for key, owner in self._claims.items() if owner != task_id}
        self._claim_accounts = {key: owner for key, owner in self._claim_accounts.items() if owner != task_id}
        self._reservations.pop(task_id, None)
        self._group_reservations.pop(task_id, None)

    def _log(self, session: _Session, message: object) -> None:
        message = session.redactor.clean(message)
        task_id = self._claims.get(session.identity)
        if task_id is not None and self.on_log is not None:
            self.on_log(task_id, session.account_id, message)
        else:
            self.events.publish("log", {"account_id": session.account_id, "task_id": None, "message": message})

    def _observe_disconnect(self, session: _Session, reason: str) -> None:
        if session.state in {"offline", "disconnecting"}:
            return
        session.state = "offline"
        session.error = session.redactor.clean(reason or "连接已经断开")
        self._publish(session)
        if session.identity not in self._claims:
            task = asyncio.create_task(self._close_idle(session))
            self._maintenance.add(task)
            task.add_done_callback(self._maintenance.discard)

    async def _close_idle(self, session: _Session) -> None:
        if session.identity in self._claims:
            return
        try:
            await self._disconnect(session)
        except Exception as exc:
            session.error = session.redactor.clean(exc)
            self._publish(session)

    async def probe_server(self, task_id, account, target, timeout, outcome, *, steps=(), attempts=1):
        if self._claims.get(account_identity(account)) != task_id or not account.get("egress_group_id"):
            raise ServiceError("account_busy", "探测任务没有代理测试账号的独占权", 409)
        self.egress.check(account["egress_group_id"], connecting=True)
        config = dict(account)
        config.pop("account_id", None)
        config.pop("proxy_url", None)
        config.update(network_mode="system", browser_verification=False,
                      use_environment_proxy=False, read_user_config=False,
                      service_account_id=account["account_id"],
                      diagnostic_dir=str(self.store.directory / "login-failures"))
        connection = self.connection_factory(AccountSpec(account.get("label", account["account_id"]), config))
        log = lambda message: self.on_log(task_id, account["account_id"], message) if self.on_log else None
        # Reservations remain held until the worker acknowledges cleanup, including cancellation.
        async with self._login_slots:
            await connection.probe_server(target, timeout, outcome, steps=steps, attempts=attempts, log=log)

    async def connect(self, task_id: str, account: dict, *, temporary: bool, reconnect=False):
        # All batches and child processes share this login/initialization limit.
        async with self._login_slots:
            return await self._connect(task_id, account, temporary=temporary, reconnect=reconnect)

    async def _connect(self, task_id: str, account: dict, *, temporary: bool, reconnect=False):
        account_id = account["account_id"]
        session = self._sessions.get(account_id)
        if session is not None and session.slot:
            if not reconnect and session.state == "ready" and session.connection.online:
                return session.connection.context
            if not reconnect:
                raise ServiceError("session_conflict", "会话已经存在", 409)
            await self._disconnect(session, retain_slot=True)
        else:
            if self._reservations.get(task_id, 0) < 1:
                raise ServiceError("capacity_exceeded", "任务没有可用连接预留", 409)
            config = dict(account)
            config.pop("account_id", None)
            config.pop("proxy_url", None)
            config.update(network_mode="system", browser_verification=False,
                          use_environment_proxy=False, read_user_config=False, service_account_id=account_id)
            if getattr(self.store, "directory", None) is not None:
                config["diagnostic_dir"] = str(self.store.directory / "login-failures")
            session = _Session(account_id, account_identity(account),
                               self.connection_factory(AccountSpec(account.get("label", account_id), config)),
                               Redactor(self.store, account), slot=True, egress_group_id=account.get("egress_group_id"))
            self._reservations[task_id] -= 1
            self._group_reservations[task_id][session.egress_group_id] -= 1
            self._sessions[account_id] = session
        session.owner_task_id = task_id
        session.state, session.error = "connecting", None
        self._publish(session)
        try:
            connected = await session.connection.connect(retries=1)
            context = session.connection.context
            if not connected or context is None:
                reason = getattr(session.connection, "login_block_reason", "")
                verification = any(marker in str(reason).casefold() for marker in
                                   ("人机验证", "captcha", "verification"))
                verification = verification or getattr(session.connection, "state", "") == "验证未完成"
                blocked_code = "verification_required" if verification else "login_blocked" if reason else None
                if reason and any(marker in str(reason).casefold() for marker in
                                  ("429", "频繁", "频率", "限流", "rate limit", "rate_limit", "too many")):
                    blocked_code = "rate_limited"
                if blocked_code and self.on_blocked is not None:
                    self.on_blocked(task_id, blocked_code)
                raise ServiceError(blocked_code or "connection_failed",
                                   session.redactor.clean(session.connection.last_error or "账号连接失败"))
            login_result = getattr(context, "login_result", None)
            session.redactor.add(getattr(login_result, "session_id", ""))
            session.redactor.add(getattr(login_result, "user_id", ""))
            context.log_callback = lambda text: self._log(session, text)
            messages = getattr(context, "messages", None)
            observer = getattr(messages, "add_disconnect_observer", None)
            if observer is not None:
                observer(lambda reason: self._observe_disconnect(session, reason))
            session.state = "initializing"
            self._publish(session)
            await context.wait_for_player_initialization()
            if not session.connection.online:
                raise ServiceError("connection_failed", "初始化期间连接已断开")
            session.state = "ready"
            session.owner_task_id = task_id if temporary else None
            self._publish(session)
            return context
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                session.error = session.redactor.clean(exc)
            await self._disconnect(session)
            raise

    def context(self, task_id: str, account_id: str):
        session = self._sessions.get(account_id)
        if (session is None or session.state != "ready" or not session.connection.online
                or self._claims.get(session.identity) != task_id):
            raise ServiceError("session_not_ready", "账号会话尚未就绪", 409)
        return session.connection.context

    async def _disconnect(self, session: _Session, *, retain_slot=False) -> None:
        if not session.slot:
            return
        session.state = "disconnecting"
        self._publish(session)
        if session.cleanup is None or session.cleanup.done():
            session.cleanup = asyncio.create_task(session.connection.disconnect())
        cleanup = session.cleanup
        interrupted = False
        try:
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
                await cleanup
        except Exception as exc:
            session.error = session.redactor.clean(exc)
            self._publish(session)
            raise ServiceError("cleanup_failed", session.error) from None
        if not retain_slot:
            owner = session.owner_task_id
            session.slot = False
            session.owner_task_id = None
            if owner in self._reservations:
                self._reservations[owner] += 1
                group_reservations = self._group_reservations[owner]
                group_reservations[session.egress_group_id] = group_reservations.get(session.egress_group_id, 0) + 1
        session.state = "offline"
        self._publish(session)
        if interrupted:
            raise asyncio.CancelledError

    async def disconnect(self, account_id: str) -> None:
        session = self._sessions.get(account_id)
        if session is not None:
            await self._disconnect(session)

    async def disconnect_owned(self, task_id: str, account_id: str) -> None:
        session = self._sessions.get(account_id)
        if session is not None and session.owner_task_id == task_id:
            await self._disconnect(session)

    async def cleanup_task(self, task_id: str) -> None:
        pending = [self._disconnect(session) for session in self._sessions.values()
                   if session.slot and (session.owner_task_id == task_id or (
                       self._claims.get(session.identity) == task_id and not session.connection.online
                   ))]
        errors = await asyncio.gather(*pending, return_exceptions=True)
        failures = [error for error in errors if isinstance(error, BaseException)]
        if failures:
            raise ServiceError("cleanup_failed", "任务连接清理失败，请重试取消任务")

    async def shutdown(self) -> None:
        errors = await asyncio.gather(*(self._disconnect(session) for session in self._sessions.values()
                                        if session.slot), return_exceptions=True)
        if self._maintenance:
            await asyncio.gather(*self._maintenance, return_exceptions=True)
        if any(isinstance(error, BaseException) for error in errors):
            raise ServiceError("cleanup_failed", "服务关闭时部分会话清理失败")
