"""Public input contracts shared by the HTTP adapter and application services."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from datetime import datetime
from ipaddress import IPv4Address
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ServiceError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AccountInput(InputModel):
    label: str = Field(min_length=1, max_length=120)
    account: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1000, repr=False)
    char_id: int = Field(default=0, ge=0, strict=True)
    zone_index: int = Field(default=1, ge=0, strict=True)
    egress_group_id: str | None = Field(default=None, min_length=1, max_length=64)


class AccountPatch(InputModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    account: str | None = Field(default=None, min_length=1, max_length=200)
    password: str | None = Field(default=None, min_length=1, max_length=1000, repr=False)
    char_id: int | None = Field(default=None, ge=0, strict=True)
    zone_index: int | None = Field(default=None, ge=0, strict=True)
    egress_group_id: str | None = Field(default=None, min_length=1, max_length=64)


class EgressGroupInput(InputModel):
    name: str = Field(min_length=1, max_length=80)
    clash_policy: str = Field(min_length=1, max_length=120)
    max_online: int = Field(default=30, ge=1, le=100, strict=True)
    routing_confirmed: bool = False

    @model_validator(mode="after")
    def validate_names(self):
        self.name, self.clash_policy = self.name.strip(), self.clash_policy.strip()
        if not self.name or not self.clash_policy or any(char in self.clash_policy for char in ",\r\n"):
            raise ValueError("分组名称和 Clash 策略名称无效")
        if self.clash_policy.upper() in {"DIRECT", "REJECT", "REJECT-DROP", "PASS"}:
            raise ValueError("出口分组需要指定代理节点或策略组")
        return self


class EgressAssignment(InputModel):
    account_ids: list[str] = Field(min_length=1)
    egress_group_id: str | None = None

    @model_validator(mode="after")
    def unique_accounts(self):
        if len(self.account_ids) != len(set(self.account_ids)):
            raise ValueError("账号 ID 不可重复")
        return self


class MessageSpec(InputModel):
    id: int = Field(ge=-1, le=65535, strict=True)
    cmd: str
    param: dict[str, Any]


class MessagesStep(InputModel):
    kind: Literal["messages"]
    messages: list[MessageSpec] = Field(min_length=1)


class ScriptStepSpec(InputModel):
    kind: Literal["script"]
    script_id: str = Field(min_length=1)


StepSpec = Annotated[MessagesStep | ScriptStepSpec, Field(discriminator="kind")]


class ServerTarget(InputModel):
    host: str
    flash_port: int = Field(ge=0, le=65535, strict=True)
    h5_port: int = Field(ge=0, le=65535, strict=True)
    domain: str = Field(default="", max_length=253)
    server_index: int | None = Field(default=None, ge=0, strict=True)
    zone_index: int | None = Field(default=None, ge=1, strict=True)
    transport: Literal["flash", "h5"] | None = None

    @field_validator("host")
    @classmethod
    def valid_host(cls, value):
        return str(IPv4Address(value))

    @field_validator("domain")
    @classmethod
    def valid_domain(cls, value):
        value = value.lower().rstrip(".")
        if value and not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
            raise ValueError("H5 域名不能包含 URL、端口或路径")
        return value


class TaskSpec(InputModel):
    kind: Literal["connect", "reconnect", "disconnect", "execute", "server_probe"] = "execute"
    account_ids: list[str] = Field(min_length=1)
    session_policy: Literal["existing", "temporary"] = "temporary"
    mode: Literal["bounded", "all_online"] = "bounded"
    concurrency: int = Field(default=5, ge=1, le=100, strict=True)
    repetitions: int = Field(default=1, ge=1, strict=True)
    repeat_interval: float = Field(default=0.5, ge=0)
    message_delay: float = Field(default=0.2, ge=0)
    steps: list[StepSpec] = Field(default_factory=list)
    server_targets: list[ServerTarget] = Field(default_factory=list, max_length=200)
    probe_timeout: float | None = Field(default=None, gt=0, le=120)
    probe_attempts: int | None = Field(default=None, ge=1, le=25, strict=True)
    planned_at: datetime | None = None
    probe_until: datetime | None = None

    @model_validator(mode="after")
    def validate_task(self):
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError("账号 ID 不可重复")
        if self.kind == "execute" and not self.steps:
            raise ValueError("执行任务至少需要一个步骤")
        if self.kind not in {"execute", "server_probe"} and self.steps:
            raise ValueError("连接操作不接受执行步骤")
        if self.kind == "server_probe":
            if self.steps and (len(self.steps) != 1 or self.steps[0].kind != "script"):
                raise ValueError("服务器测试只允许登录后执行一个脚本一次")
            if not self.server_targets:
                raise ValueError("服务器测试至少需要一个服务器条目")
            keys = [(s.host, s.flash_port, s.h5_port, s.domain) for s in self.server_targets]
            if len(keys) != len(set(keys)):
                raise ValueError("服务器条目不可重复")
            if self.session_policy != "temporary" or self.mode != "bounded" or self.repetitions != 1:
                raise ValueError("服务器测试只支持 temporary/bounded，且只能测试一轮")
        elif self.server_targets or self.planned_at is not None or self.probe_until is not None:
            raise ValueError("服务器条目和计划时间仅用于 server_probe")
        if self.planned_at is not None and self.planned_at.utcoffset() is None:
            raise ValueError("计划时间必须包含时区")
        if self.probe_until is not None:
            if self.probe_until.utcoffset() is None or self.planned_at is None:
                raise ValueError("每秒测试需要包含时区的开始和结束时间")
            if not 0 < (self.probe_until - self.planned_at).total_seconds() <= 25:
                raise ValueError("每秒测试窗口必须大于0秒且不超过25秒")
            if any(t.zone_index is None or t.transport is None for t in self.server_targets):
                raise ValueError("每秒测试必须明确区服和单一传输方式")
        elif (self.probe_attempts or 1) > 5:
            raise ValueError("普通服务器测试每种传输最多5次")
        return self
