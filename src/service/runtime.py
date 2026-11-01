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
    exit_ip: str | None = None
    waiter: asyncio.Future | None = None


class SessionService:
    def __init__(self, store, events, connection_factory=AccountConnection, max_online=100,
                 login_concurrency=None):
        self.store = store
        self.events = events
        self.connection_factory = connection_factory
        self.login_concurrency = 20 if login_concurrency is None else login_concurrency
        if self.login_concurrency < 1:
            raise ValueError("在线容量和登录并发必须为正整数")
        self._login_slots = asyncio.Semaphore(self.login_concurrency)
        self._sessions: dict[str, _Session] = {}
        self._claims: dict[str, str] = {}
        self._claim_accounts: dict[str, str] = {}
        self.egress = None
        self._waiting = []
        self._turn = 0
        self._last_used = {}
        self.on_state = None
        self._lock = asyncio.Lock()
        self._maintenance: set[asyncio.Task] = set()
        self.on_log: Callable[[str, str, str], None] | None = None

    def _publish(self, session: _Session) -> None:
        self.events.publish("session", self._public(session))
        if self.on_state:
            self.on_state(session)

    @staticmethod
    def _public(session: _Session) -> dict:
        return {
            "account_id": session.account_id, "state": session.state,
            "error": session.error, "owner_task_id": session.owner_task_id,
            "egress_group_id": session.egress_group_id, "exit_ip": session.exit_ip,
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
            rows.append({**row, "label": account.get("label", "")})
        return rows

    @property
    def waiting_count(self):
        return sum(not future.done() for _, future in self._waiting)

    @property
    def max_online(self):
        return len(self.egress.available()) * 25 if self.egress else 0

    def egress_usage(self, group_id):
        return {"online_count": sum(s.slot and s.egress_group_id == group_id for s in self._sessions.values()),
                "reserved_count": 0}

    def wake(self):
        # All grants are synchronous on the service event loop: no capacity can be
        # observed and consumed by two requests between the check and the grant.
        candidates = self.egress.available() if self.egress else []
        while self._waiting:
            session, future = self._waiting[0]
            if future.done():
                self._waiting.pop(0)
                continue
            usage = {g["exit_ip"]: sum(s.slot and s.exit_ip == g["exit_ip"]
                                      for s in self._sessions.values()) for g in candidates}
            available = [g for g in candidates if usage[g["exit_ip"]] < 25]
            if not available:
                break
            group = min(available, key=lambda g: (usage[g["exit_ip"]], self._last_used.get(g["group_id"], 0)))
            self._waiting.pop(0)
            self._turn += 1
            self._last_used[group["group_id"]] = self._turn
            session.egress_group_id, session.exit_ip = group["group_id"], group["exit_ip"]
            session.slot = True
            session.state = "connecting"
            future.set_result(None)
            self._publish(session)
        self.events.publish("egress", {"waiting_count": self.waiting_count})

    async def _lease(self, task_id, account):
        session = _Session(account["account_id"], account_identity(account), None,
                           Redactor(self.store, account), state="waiting_egress", owner_task_id=task_id)
        future = asyncio.get_running_loop().create_future()
        session.waiter = future
        self._sessions[session.account_id] = session
        self._waiting.append((session, future))
        self._publish(session)
        self.wake()
        try:
            await future
            return session
        except BaseException:
            if (session, future) in self._waiting:
                self._waiting.remove((session, future))
            await self._disconnect(session)
            raise
        finally:
            session.waiter = None

    def _connection(self, session, account):
        config = {k: v for k, v in account.items() if k not in {"account_id", "proxy_url", "egress_group_id"}}
        config.update(network_mode="system", browser_verification=False,
                      use_environment_proxy=False, read_user_config=False,
                      service_account_id=session.account_id, egress_group_id=session.egress_group_id)
        if getattr(self.store, "directory", None) is not None:
            config["diagnostic_dir"] = str(self.store.directory / "login-failures")
        session.connection = self.connection_factory(AccountSpec(account.get("label", session.account_id), config))
        return session.connection

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
        needed = sum(not (self._sessions.get(a["account_id"]) and
                          self._sessions[a["account_id"]].slot) for a in eligible)
        return needed, eligible, skipped

    def check(self, spec, accounts):
        needed, eligible, skipped = self._check(spec, accounts)
        return {"required_capacity": needed, "max_online": self.max_online,
                "login_concurrency": self.login_concurrency, "account_count": len(eligible),
                "skipped_accounts": skipped}

    async def reserve(self, task_id, spec, accounts):
        async with self._lock:
            _, eligible, skipped = self._check(spec, accounts)
            for account in eligible:
                self._claims[account_identity(account)] = task_id
                self._claim_accounts[account["account_id"]] = task_id
            return skipped

    def release_account(self, task_id, account):
        session = self._sessions.get(account["account_id"])
        if session and session.slot and session.owner_task_id == task_id:
            return
        identity = account_identity(account)
        if self._claims.get(identity) == task_id:
            self._claims.pop(identity, None)
        if self._claim_accounts.get(account["account_id"]) == task_id:
            self._claim_accounts.pop(account["account_id"], None)

    def release(self, task_id):
        self._claims = {key: owner for key, owner in self._claims.items() if owner != task_id}
        self._claim_accounts = {key: owner for key, owner in self._claim_accounts.items() if owner != task_id}

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

    async def probe_server(self, task_id, account, target, timeout, outcome, *, steps=(), attempts=1, window=None,
                           start_at=None, start_before=None):
        if self._claims.get(account_identity(account)) != task_id:
            raise ServiceError("account_busy", "探测任务没有账号独占权", 409)
        session = await self._lease(task_id, account)
        try:
            async with self._login_slots:
                connection = self._connection(session, account)
                session.state = "connecting"
                self._publish(session)
                log = lambda message: self.on_log(task_id, account["account_id"], message) if self.on_log else None
                await connection.probe_server(target, timeout, outcome, steps=steps, attempts=attempts,
                                              log=log, window=window, start_at=start_at, start_before=start_before)
        finally:
            await self._disconnect(session)

    async def connect(self, task_id, account, *, temporary=False, reconnect=False):
        session = self._sessions.get(account["account_id"])
        if session and session.slot:
            if not reconnect and session.state == "ready" and session.connection.online:
                return session.connection.context
            if not reconnect:
                raise ServiceError("session_conflict", "会话正在清理，请先断开或重连", 409)
            await self._disconnect(session)
        session = await self._lease(task_id, account)
        try:
            async with self._login_slots:
                self._connection(session, account)
                return await self._connect(task_id, session, temporary=temporary)
        except BaseException:
            await self._disconnect(session)
            raise

    async def _connect(self, task_id, session, *, temporary=False):
        session.owner_task_id = task_id
        session.state, session.error = "connecting", None
        self._publish(session)
        try:
            connected = await session.connection.connect(retries=1)
            context = session.connection.context
            if context is not None:
                context.log_callback = lambda text: self._log(session, text)
            if not connected or context is None:
                reason = getattr(session.connection, "login_block_reason", "")
                verification = any(marker in str(reason).casefold() for marker in
                                   ("人机验证", "captcha", "verification"))
                verification = verification or getattr(session.connection, "state", "") == "验证未完成"
                blocked_code = "verification_required" if verification else "login_blocked" if reason else None
                if reason and any(marker in str(reason).casefold() for marker in
                                  ("429", "频繁", "频率", "限流", "rate limit", "rate_limit", "too many")):
                    blocked_code = "rate_limited"
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
            raise

    def context(self, task_id: str, account_id: str):
        session = self._sessions.get(account_id)
        if (session is None or session.state != "ready" or not session.connection.online
                or self._claims.get(session.identity) != task_id):
            raise ServiceError("session_not_ready", "账号会话尚未就绪", 409)
        return session.connection.context

    async def _disconnect(self, session: _Session, *, retain_slot=False) -> None:
        if not session.slot:
            session.state = "offline"
            session.owner_task_id = None
            self._publish(session)
            self.wake()
            return
        session.state = "disconnecting"
        self._publish(session)
        if session.cleanup is None or session.cleanup.done():
            async def close_connection():
                if session.connection is not None:
                    await session.connection.disconnect()
            session.cleanup = asyncio.create_task(close_connection())
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
        session.slot = False
        session.owner_task_id = None
        session.egress_group_id = None
        session.exit_ip = None
        session.state = "offline"
        self._publish(session)
        self.wake()
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
                       self._claims.get(session.identity) == task_id and (session.connection is None or not session.connection.online)
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
