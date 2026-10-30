"""HTTP adapter. Business state belongs to the application services."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.service.application import Application
from src.service.contracts import AccountInput, AccountPatch, EgressAssignment, EgressGroupInput, MessageSpec, ServiceError, TaskSpec, ScriptTaskInput
from src.interfaces.schemas import (
    AccountImportResult, AccountRecord, TaskClearResult, ErrorResponse, HealthStatus,
    LogEntry, MessagesValidation, ScriptRecord, SessionRecord, TaskPreflight,
    TaskRecord, TaskResults, EgressGroupRecord, EgressRules, EgressAssignmentResult,
)


API_PREFIX = "/api/v1"


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigInput(InputModel):
    config: dict[str, Any]


class ParseInput(InputModel):
    text: str


class ValidateMessagesInput(InputModel):
    messages: list[MessageSpec]


class SessionInput(InputModel):
    account_ids: list[str] = Field(min_length=1)
    concurrency: int = Field(default=1, ge=1, le=100, strict=True)

    @field_validator("account_ids")
    @classmethod
    def unique_accounts(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Account IDs must be unique")
        return values


def default_data_dir() -> Path:
    return Path(os.environ.get("AOLA_DATA_DIR", "data/service"))


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    try:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or parsed.username is not None or
                parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
            return False
        defaults = {"http": 80, "https": 443}
        return (parsed.scheme, parsed.hostname, parsed.port or defaults.get(parsed.scheme)) == (
            request.url.scheme, request.url.hostname, request.url.port or defaults.get(request.url.scheme))
    except ValueError:
        return False


def create_app(
    data_dir: str | Path | None = None,
    application: Application | None = None,
    frontend_dir: str | Path | None = None,
    max_online: int = 100,
    login_concurrency: int | None = None,
) -> FastAPI:
    directory = Path(data_dir) if data_dir is not None else default_data_dir()
    frontend = (Path(frontend_dir) if frontend_dir is not None else
                Path(__file__).resolve().parents[2] / "frontend" / "dist").resolve()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.application = application or Application(directory, max_online=max_online,
                                                         login_concurrency=login_concurrency)
        try:
            await app.state.application.start()
            yield
        finally:
            await app.state.application.close()

    app = FastAPI(title="Aola background service", version="1.0.0", lifespan=lifespan,
                  responses={status: {"model": ErrorResponse} for status in (400, 403, 404, 409, 422, 503)})

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, error: ServiceError):
        return JSONResponse(status_code=error.status, content={"error": {
            "code": error.code, "message": error.message,
        }})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError):
        # Pydantic's default response includes the submitted value, which can be a password.
        details = [{"loc": list(item["loc"]), "type": item["type"], "msg": item["msg"]}
                   for item in error.errors()]
        return JSONResponse(status_code=422, content={"error": {
            "code": "invalid_request", "message": "Request validation failed", "details": details,
        }})

    async def check_origin(request: Request):
        if (request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin")
                and not _same_origin(request)):
            raise ServiceError("origin_mismatch", "A matching Origin header is required", 403)

    @app.get(API_PREFIX + "/health", response_model=HealthStatus)
    async def health():
        return {"status": "ok"}

    api = APIRouter(prefix=API_PREFIX, dependencies=[Depends(check_origin)])

    @api.get("/accounts", response_model=list[AccountRecord])
    async def accounts(request: Request):
        return request.app.state.application.accounts.list()

    @api.post("/accounts", status_code=201, response_model=AccountRecord)
    async def create_account(body: AccountInput, request: Request):
        return request.app.state.application.accounts.create(body)

    @api.post("/accounts/import", response_model=AccountImportResult)
    async def import_accounts(body: ConfigInput, request: Request):
        return request.app.state.application.accounts.import_config(body.config)

    @api.post("/accounts/assign-egress", response_model=EgressAssignmentResult)
    async def assign_egress(body: EgressAssignment, request: Request):
        return request.app.state.application.accounts.assign_egress(body.account_ids, body.egress_group_id)

    @api.get("/egress-groups", response_model=list[EgressGroupRecord])
    async def egress_groups(request: Request):
        return request.app.state.application.egress.list()

    @api.post("/egress-groups", status_code=201, response_model=EgressGroupRecord)
    async def create_egress_group(body: EgressGroupInput, request: Request):
        return request.app.state.application.egress.create(body)

    @api.get("/egress-groups/rules", response_model=EgressRules)
    async def egress_rules(request: Request):
        return request.app.state.application.egress.rules()

    @api.get("/egress-groups/{group_id}", response_model=EgressGroupRecord)
    async def egress_group(group_id: str, request: Request):
        return request.app.state.application.egress.get(group_id)

    @api.put("/egress-groups/{group_id}", response_model=EgressGroupRecord)
    async def update_egress_group(group_id: str, body: EgressGroupInput, request: Request):
        return await request.app.state.application.egress.update(group_id, body)

    @api.delete("/egress-groups/{group_id}", status_code=204)
    async def delete_egress_group(group_id: str, request: Request):
        await request.app.state.application.egress.delete(group_id)

    @api.post("/egress-groups/{group_id}/probe", response_model=EgressGroupRecord)
    async def probe_egress_group(group_id: str, request: Request):
        return await request.app.state.application.egress.probe(group_id)

    @api.get("/accounts/{account_id}", response_model=AccountRecord)
    async def account(account_id: str, request: Request):
        return request.app.state.application.accounts.get(account_id)

    @api.patch("/accounts/{account_id}", response_model=AccountRecord)
    async def update_account(account_id: str, body: AccountPatch, request: Request):
        return request.app.state.application.accounts.update(account_id, body)

    @api.delete("/accounts/{account_id}", status_code=204)
    async def delete_account(account_id: str, request: Request):
        request.app.state.application.accounts.delete(account_id)

    @api.get("/sessions", response_model=list[SessionRecord])
    async def sessions(request: Request):
        return request.app.state.application.sessions.list()

    async def session_operation(kind: str, body: SessionInput, request: Request):
        spec = TaskSpec(kind=kind, account_ids=body.account_ids, concurrency=body.concurrency)
        return await request.app.state.application.tasks.submit(spec)

    @api.post("/sessions/connect", status_code=202, response_model=TaskRecord)
    async def connect(body: SessionInput, request: Request):
        return await session_operation("connect", body, request)

    @api.post("/sessions/reconnect", status_code=202, response_model=TaskRecord)
    async def reconnect(body: SessionInput, request: Request):
        return await session_operation("reconnect", body, request)

    @api.post("/sessions/disconnect", status_code=202, response_model=TaskRecord)
    async def disconnect(body: SessionInput, request: Request):
        return await session_operation("disconnect", body, request)

    @api.post("/messages/parse", response_model=MessagesValidation)
    async def parse_messages(body: ParseInput, request: Request):
        return request.app.state.application.messages.parse(body.text)

    @api.post("/messages/validate", response_model=MessagesValidation)
    async def validate_messages(body: ValidateMessagesInput, request: Request):
        return request.app.state.application.messages.validate(body.messages)

    @api.get("/scripts", response_model=list[ScriptRecord])
    async def scripts(request: Request):
        return request.app.state.application.scripts.list()

    @api.post("/tasks/validate", response_model=TaskPreflight)
    async def validate_task(body: ScriptTaskInput, request: Request):
        return request.app.state.application.tasks.validate(body.to_spec())

    @api.post("/tasks", status_code=202, response_model=TaskRecord)
    async def start_task(body: ScriptTaskInput, request: Request):
        return await request.app.state.application.tasks.submit(body.to_spec())

    @api.post("/server-probes/validate", response_model=TaskPreflight)
    async def validate_probe(body: TaskSpec, request: Request):
        if body.kind != "server_probe":
            raise ServiceError("invalid_task", "仅支持服务器探测")
        return request.app.state.application.tasks.validate(body)

    @api.post("/server-probes", status_code=202, response_model=TaskRecord)
    async def start_probe(body: TaskSpec, request: Request):
        if body.kind != "server_probe":
            raise ServiceError("invalid_task", "仅支持服务器探测")
        return await request.app.state.application.tasks.submit(body)

    @api.get("/tasks", response_model=list[TaskRecord])
    async def tasks(request: Request):
        return request.app.state.application.tasks.list()

    @api.delete("/tasks", response_model=TaskClearResult)
    async def clear_tasks(request: Request):
        return request.app.state.application.tasks.clear_finished()

    @api.get("/tasks/{task_id}", response_model=TaskRecord)
    async def task(task_id: str, request: Request):
        return request.app.state.application.tasks.get(task_id)

    @api.post("/tasks/{task_id}/cancel", response_model=TaskRecord)
    async def cancel(task_id: str, request: Request):
        return await request.app.state.application.tasks.cancel(task_id)

    @api.get("/tasks/{task_id}/results", response_model=TaskResults)
    async def results(task_id: str, request: Request):
        return request.app.state.application.tasks.results(task_id)

    @api.get("/tasks/{task_id}/logs", response_model=list[LogEntry])
    async def logs(task_id: str, request: Request,
                   after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
        return request.app.state.application.tasks.logs(task_id, after=after, limit=limit)

    @api.get("/events", response_class=StreamingResponse,
             responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
    async def events(request: Request, task_id: str | None = None):
        hub = request.app.state.application.events

        async def stream():
            queue = hub.subscribe()
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    payload = jsonable_encoder(event)
                    event_task_id = payload.get("task_id") or payload.get("data", {}).get("task_id")
                    if task_id and event_task_id != task_id:
                        continue
                    yield "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    app.include_router(api)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                   include_in_schema=False)
    async def frontend_file(path: str, request: Request):
        # API typos and missing assets must retain their ordinary 404 semantics.
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        if request.method not in {"GET", "HEAD"}:
            raise HTTPException(status_code=405, headers={"Allow": "GET, HEAD"})
        target = (frontend / path).resolve()
        if not target.is_relative_to(frontend):
            raise HTTPException(status_code=404)
        if target.is_file():
            return FileResponse(target)
        if Path(path).suffix or path.startswith("assets/"):
            raise HTTPException(status_code=404)
        index = (frontend / "index.html").resolve()
        if index.is_relative_to(frontend) and index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404)

    return app
