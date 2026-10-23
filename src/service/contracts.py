"""Public input contracts shared by the HTTP adapter and application services."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class TaskSpec(InputModel):
    kind: Literal["connect", "reconnect", "disconnect", "execute"] = "execute"
    account_ids: list[str] = Field(min_length=1)
    session_policy: Literal["existing", "temporary"] = "temporary"
    mode: Literal["bounded", "all_online"] = "bounded"
    concurrency: int = Field(default=5, ge=1, le=100, strict=True)
    repetitions: int = Field(default=1, ge=1, strict=True)
    repeat_interval: float = Field(default=0.5, ge=0)
    message_delay: float = Field(default=0.2, ge=0)
    steps: list[StepSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_task(self):
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError("账号 ID 不可重复")
        if self.kind == "execute" and not self.steps:
            raise ValueError("执行任务至少需要一个步骤")
        if self.kind != "execute" and self.steps:
            raise ValueError("连接操作不接受执行步骤")
        return self
