"""Account configuration and independent connection lifecycle management."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from src.network.context import AppContext
from src.accounts.session import open_context
from src.accounts.browser_login import BrowserLoginStopped


class AccountConfigError(ValueError):
    """Raised when account configuration is missing or ambiguous."""


@dataclass(frozen=True)
class AccountSpec:
    label: str
    config: dict


def _validate_credentials(config: dict, location: str) -> None:
    account = config.get("account")
    password = config.get("password")
    if not isinstance(account, str) or not account or account == "你的账号":
        raise AccountConfigError(f"{location} 缺少有效 account")
    if not isinstance(password, str) or not password or password == "你的密码":
        raise AccountConfigError(f"{location} 缺少有效 password")
    char_id = config.get("char_id", 0)
    if isinstance(char_id, bool) or not isinstance(char_id, int) or char_id < 0:
        raise AccountConfigError(f"{location} 的 char_id 必须是非负整数")
    config["char_id"] = char_id


def load_account_specs(config: dict) -> list[AccountSpec]:
    """Load optional ``accounts`` entries while preserving legacy config format."""
    raw_accounts = config.get("accounts")
    if raw_accounts is None:
        account_config = dict(config)
        _validate_credentials(account_config, "config.json")
        label = account_config.get("label", "账号 1")
        if not isinstance(label, str) or not label.strip():
            raise AccountConfigError("config.json 的 label 必须是非空字符串")
        label = label.strip()
        if label == account_config["account"]:
            raise AccountConfigError("config.json 的 label 不应使用登录账号")
        return [AccountSpec(label, account_config)]

    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise AccountConfigError("config.json 的 accounts 必须是非空数组")

    shared = {
        key: value
        for key, value in config.items()
        if key not in {"accounts", "account", "password", "label"}
    }
    specs: list[AccountSpec] = []
    labels: set[str] = set()
    for index, raw in enumerate(raw_accounts, start=1):
        location = f"accounts[{index - 1}]"
        if not isinstance(raw, dict):
            raise AccountConfigError(f"{location} 必须是对象")
        merged = {**shared, **raw}
        _validate_credentials(merged, location)
        label = raw.get("label", f"账号 {index}")
        if not isinstance(label, str) or not label.strip():
            raise AccountConfigError(f"{location} 的 label 必须是非空字符串")
        label = label.strip()
        if label == merged["account"]:
            raise AccountConfigError(f"{location} 的 label 不应使用登录账号")
        if label in labels:
            raise AccountConfigError(f"账号标签重复：{label}")
        labels.add(label)
        specs.append(AccountSpec(label, merged))
    return specs


ContextOpener = Callable[[dict], Awaitable[AppContext]]


@dataclass
class AccountConnection:
    spec: AccountSpec
    context: AppContext | None = None
    state: str = "未连接"
    last_error: str = ""
    attempts: int = 0
    retryable: bool = True

    @property
    def online(self) -> bool:
        context = self.context
        return bool(
            context is not None
            and context.socket.connected
            and not context.messages.disconnected.is_set()
        )

    def refresh_state(self) -> None:
        if self.context is not None and not self.online and self.state == "在线":
            self.state = "已断线"
            reason = (
                self.context.messages.disconnect_reason
                or self.context.socket.disconnect_reason
            )
            if reason:
                self.last_error = reason

    async def connect(
        self,
        opener: ContextOpener = open_context,
        retries: int = 3,
        retry_delay: float = 0.5,
    ) -> bool:
        if self.online:
            return True
        if self.context is not None:
            await self.context.close()
            self.context = None

        self.last_error = ""
        self.retryable = True
        for attempt in range(1, retries + 1):
            self.attempts = attempt
            self.state = "登录中"
            try:
                self.context = await opener(dict(self.spec.config))
            except asyncio.CancelledError:
                self.state = "已取消"
                raise
            except BrowserLoginStopped as exc:
                self.last_error = str(exc)
                self.state = "验证未完成"
                self.retryable = False
                return False
            except Exception as exc:
                self.last_error = str(exc)
                self.state = "登录失败"
                if attempt < retries:
                    await asyncio.sleep(retry_delay)
                continue
            self.state = "在线"
            return True
        return False

    async def disconnect(self) -> None:
        context = self.context
        self.context = None
        if context is not None:
            await context.close()
        self.state = "已断开"


@dataclass
class AccountManager:
    connections: list[AccountConnection] = field(default_factory=list)

    @classmethod
    def from_specs(cls, specs: list[AccountSpec]) -> "AccountManager":
        return cls([AccountConnection(spec) for spec in specs])

    async def connect_all(
        self,
        max_concurrency: int = 5,
        opener: ContextOpener = open_context,
        retries: int = 3,
        retry_delay: float = 3.0,
    ) -> None:
        semaphore = asyncio.Semaphore(max_concurrency)

        async def connect_one(connection: AccountConnection) -> None:
            async with semaphore:
                await connection.connect(
                    opener=opener,
                    retries=retries,
                    retry_delay=retry_delay,
                )

        await asyncio.gather(
            *(connect_one(connection) for connection in self.connections)
        )

    async def disconnect_all(self) -> None:
        await asyncio.gather(
            *(connection.disconnect() for connection in self.connections),
            return_exceptions=True,
        )
