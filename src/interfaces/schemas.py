"""Public response shapes used by OpenAPI and generated browser types."""
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from src.service.contracts import MessageSpec, ServerTarget


class ErrorInfo(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorInfo


class TaskClearResult(BaseModel):
    deleted_count: int
    task_ids: list[str]


class HealthStatus(BaseModel):
    status: str


class AccountRecord(BaseModel):
    account_id: str
    label: str
    account: str
    char_id: int
    zone_index: int


class AccountImportResult(BaseModel):
    imported: int
    accounts: list[AccountRecord]
    source_modified: bool
    omitted_fields: list[str]


class SessionRecord(BaseModel):
    account_id: str
    label: str
    state: Literal["offline", "waiting_egress", "connecting", "initializing", "ready", "disconnecting"]
    error: str | None
    owner_task_id: str | None
    egress_group_id: str | None = None
    exit_ip: str | None = None


class EgressGroupRecord(BaseModel):
    group_id: str
    name: str
    clash_policy: str
    max_online: int
    routing_confirmed: bool
    enabled: bool
    active: bool
    waiting_count: int
    online_count: int
    reserved_count: int
    worker_state: str
    worker_pid: int | None
    worker_path: str
    worker_error: str | None
    exit_ip: str | None
    exit_checked_at: str | None


class EgressRules(BaseModel):
    yaml: str
    instructions: str


class EgressAssignmentResult(BaseModel):
    updated: int
    egress_group_id: str | None


class ScriptRecord(BaseModel):
    script_id: str
    name: str
    description: str


class MessagesValidation(BaseModel):
    valid: bool
    messages: list[MessageSpec]
    count: int


class SkippedAccount(BaseModel):
    account_id: str
    label: str
    reason: str


class TaskPreflight(BaseModel):
    valid: bool
    account_count: int
    step_count: int
    spec: dict[str, Any]
    required_capacity: int
    max_online: int
    login_concurrency: int
    skipped_accounts: list[SkippedAccount] = Field(default_factory=list)
    server_assignments: list[dict[str, Any]] = Field(default_factory=list)
    effective_concurrency: int | None = None
    max_transport_attempts: int | None = None


class NetworkWrite(BaseModel):
    status: Literal["not_attempted", "unknown", "succeeded", "partial", "failed"]
    count: int | None


class StepResult(BaseModel):
    repetition: int
    step: int
    kind: str
    success: bool
    error: str | None
    skipped: bool = False


class AccountResult(BaseModel):
    account_id: str
    state: str
    completed_repetitions: int
    attempted_repetitions: int = 0
    skipped_repetitions: int = 0
    error: ErrorInfo | None
    network_write: NetworkWrite
    business_confirmation: str
    step_results: list[StepResult] = Field(default_factory=list)
    egress_group_id: str | None = None
    exit_ip: str | None = None

    @model_validator(mode="before")
    @classmethod
    def legacy_counts(cls, value):
        if isinstance(value, dict) and "attempted_repetitions" not in value:
            return {**value, "attempted_repetitions": value.get("completed_repetitions", 0)
                    + value.get("skipped_repetitions", 0)}
        return value


TaskState = Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled", "interrupted"]


class TransportProbeResult(BaseModel):
    status: str
    stage: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: float | None = None
    stage_durations_ms: dict[str, float] = Field(default_factory=dict)
    connection_status: str = "not_attempted"
    login_status: str = "not_attempted"
    connected_at: str | None = None
    logged_in_at: str | None = None
    authentication_response: dict[str, Any] = Field(default_factory=dict)
    login_response: dict[str, Any] = Field(default_factory=dict)
    initialization_status: str = "not_attempted"
    initialized_at: str | None = None
    execution: dict[str, Any] = Field(default_factory=dict)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    skipped_slots: list[dict[str, Any]] = Field(default_factory=list)
    server_index: int | None = None
    zone_index: int | None = None
    zone_name: str | None = None
    error: str | None = None


class ServerProbeResult(BaseModel):
    target_index: int
    server: ServerTarget
    account_id: str
    egress_group_id: str
    exit_ip: str | None = None
    state: str
    started_at: str | None = None
    finished_at: str | None = None
    scheduled_at: str | None = None
    start_before: str | None = None
    error: ErrorInfo | None = None
    flash: TransportProbeResult
    h5: TransportProbeResult


class TaskResults(BaseModel):
    task_id: str
    state: TaskState
    error: ErrorInfo | None
    account_results: list[AccountResult]
    server_results: list[ServerProbeResult] = Field(default_factory=list)


class TaskRecord(TaskResults):
    kind: Literal["connect", "reconnect", "disconnect", "execute", "server_probe"]
    created_at: str
    updated_at: str | None = None
    started_at: str | None
    finished_at: str | None
    spec: dict[str, Any]


class LogEntry(BaseModel):
    id: int
    task_id: str | None
    account_id: str | None
    timestamp: str
    message: str
