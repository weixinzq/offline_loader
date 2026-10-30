"""Linux-friendly CLI: all business operations use the public HTTP interface."""
from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from typing import Any

import aiohttp
import typer


app = typer.Typer(no_args_is_help=True, help="Manage the Aola background service.")
accounts_app = typer.Typer(no_args_is_help=True, help="Account configuration.")
sessions_app = typer.Typer(no_args_is_help=True, help="Persistent game sessions.")
tasks_app = typer.Typer(no_args_is_help=True, help="Validate and manage background tasks.")
scripts_app = typer.Typer(no_args_is_help=True, help="Available execution scripts.")
logs_app = typer.Typer(no_args_is_help=True, help="Read and follow task logs.")
egress_app = typer.Typer(no_args_is_help=True, help="Configure isolated outbound groups and Clash rules.")
servers_app = typer.Typer(no_args_is_help=True, help="Test game login availability through assigned egress groups.")
for name, commands in (("accounts", accounts_app), ("sessions", sessions_app),
                       ("tasks", tasks_app), ("scripts", scripts_app), ("logs", logs_app), ("egress", egress_app),
                       ("servers", servers_app)):
    app.add_typer(commands, name=name)


@dataclass
class Options:
    url: str
    json_output: bool


@app.callback()
def options(
    ctx: typer.Context,
    url: str = typer.Option("http://127.0.0.1:8765", "--url", envvar="AOLA_SERVICE_URL"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    ctx.obj = Options(url.rstrip("/"), json_output)


def _emit(value: Any, options: Options):
    if value is not None:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=None if options.json_output else 2))


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise typer.BadParameter(f"Cannot read JSON file {path.name}: {type(error).__name__}") from None


async def _request(options: Options, method: str, endpoint: str, body: Any = None):
    async with aiohttp.ClientSession(trust_env=False,
                                     timeout=aiohttp.ClientTimeout(total=30)) as client:
        async with client.request(method, options.url + "/api/v1" + endpoint, json=body) as response:
            if response.status == 204:
                return None
            try:
                value = await response.json()
            except (ValueError, aiohttp.ContentTypeError):
                raise ValueError(f"Service returned HTTP {response.status} without a JSON response") from None
            if response.status >= 400:
                detail = value.get("error", {}) if isinstance(value, dict) else {}
                raise ValueError(f"{detail.get('code', response.status)}: "
                                 f"{detail.get('message', 'Service request failed')}")
            return value


def _run(ctx: typer.Context, operation):
    try:
        return asyncio.run(operation)
    except (ValueError, aiohttp.ClientError, TimeoutError) as error:
        message = str(error)
        if ctx.obj.json_output:
            _emit({"error": {"code": "cli_error", "message": message}}, ctx.obj)
        else:
            typer.echo(message, err=True)
        raise typer.Exit(1) from None


def _call(ctx: typer.Context, method: str, endpoint: str, body: Any = None):
    _emit(_run(ctx, _request(ctx.obj, method, endpoint, body)), ctx.obj)


@app.command()
def serve(
    data_dir: Path = typer.Option(Path("data/service"), "--data-dir", envvar="AOLA_DATA_DIR"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    frontend_dir: Path | None = typer.Option(None, "--frontend-dir", envvar="AOLA_FRONTEND_DIR",
                                            help="Built browser frontend directory; defaults to frontend/dist."),
    max_online: int = typer.Option(100, "--max-online", min=1, hidden=True, help="Deprecated; capacity is derived from the IP pool."),
    login_concurrency: int | None = typer.Option(None, "--login-concurrency", min=1,
                                               envvar="AOLA_LOGIN_CONCURRENCY",
                                               help="Global login and initialization limit; defaults to 20."),
):
    """Run a single-worker backend; use HTTPS reverse proxying for remote access."""
    import uvicorn
    from src.interfaces.http import create_app

    uvicorn.run(create_app(data_dir,
                          frontend_dir=frontend_dir, max_online=max_online, login_concurrency=login_concurrency),
                host=host, port=port, workers=1, timeout_graceful_shutdown=10)


@accounts_app.command("list")
def accounts_list(ctx: typer.Context):
    _call(ctx, "GET", "/accounts")


@accounts_app.command("get")
def accounts_get(ctx: typer.Context, account_id: str):
    _call(ctx, "GET", f"/accounts/{account_id}")


@accounts_app.command("create")
def accounts_create(
    ctx: typer.Context,
    account: str = typer.Option(..., "--account"),
    label: str = typer.Option(..., "--label", help="Stable label distinct from the login account."),
    password: str = typer.Option(..., "--password", prompt=True, hide_input=True),
    char_id: int = typer.Option(0, "--char-id", min=0),
    zone_index: int = typer.Option(1, "--zone-index", min=0),
):
    _call(ctx, "POST", "/accounts", {"account": account, "label": label,
          "password": password, "char_id": char_id, "zone_index": zone_index})


@accounts_app.command("update")
def accounts_update(
    ctx: typer.Context, account_id: str,
    account: str | None = typer.Option(None, "--account"),
    label: str | None = typer.Option(None, "--label"),
    password: str | None = typer.Option(None, "--password", hide_input=True),
    char_id: int | None = typer.Option(None, "--char-id", min=0),
    zone_index: int | None = typer.Option(None, "--zone-index", min=0),
):
    values = {"account": account, "label": label, "password": password,
              "char_id": char_id, "zone_index": zone_index}
    _call(ctx, "PATCH", f"/accounts/{account_id}", {k: v for k, v in values.items() if v is not None})


@accounts_app.command("delete")
def accounts_delete(ctx: typer.Context, account_id: str):
    _call(ctx, "DELETE", f"/accounts/{account_id}")


@accounts_app.command("import")
def accounts_import(ctx: typer.Context, file: Path = typer.Option(..., "--file", exists=True, dir_okay=False)):
    """Read a local legacy configuration; submit its content, never its path."""
    _call(ctx, "POST", "/accounts/import", {"config": _load(file)})


@sessions_app.command("list")
def sessions_list(ctx: typer.Context):
    _call(ctx, "GET", "/sessions")


def _session_command(operation: str):
    def command(ctx: typer.Context, account_ids: list[str] = typer.Argument(...),
                concurrency: int = typer.Option(1, "--concurrency", min=1, max=100)):
        _call(ctx, "POST", f"/sessions/{operation}",
              {"account_ids": account_ids, "concurrency": concurrency})
    return command


for _operation in ("connect", "reconnect", "disconnect"):
    sessions_app.command(_operation)(_session_command(_operation))


@tasks_app.command("validate")
def tasks_validate(ctx: typer.Context, file: Path = typer.Option(..., "--file", exists=True, dir_okay=False)):
    """Preflight a task snapshot without logging in or sending game messages."""
    _call(ctx, "POST", "/tasks/validate", _load(file))


@tasks_app.command("migrate")
def tasks_migrate(
    ctx: typer.Context,
    file: Path = typer.Option(..., "--file", exists=True, dir_okay=False),
    output: Path | None = typer.Option(None, "--output", dir_okay=False,
                                       help="Write a new conversion report; existing files are never overwritten."),
):
    """Convert a local legacy job into task snapshots without starting tasks or editing inputs."""
    from src.service.contracts import ServiceError
    from src.service.migration import convert_legacy_job

    raw = _load(file)

    async def convert():
        accounts = await _request(ctx.obj, "GET", "/accounts")
        try:
            return convert_legacy_job(raw, accounts, file.resolve().parent)
        except ServiceError:
            raise
        except ValueError:
            raise ValueError("Cannot convert legacy job: invalid task fields") from None
        except (KeyError, TypeError, AttributeError, OSError):
            raise ValueError("Cannot convert legacy job: invalid fields or unreadable local input") from None

    result = _run(ctx, convert())
    if output is not None:
        try:
            with output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        except OSError as error:
            raise typer.BadParameter(f"Cannot create conversion report {output.name}: {type(error).__name__}") from None
    _emit(result, ctx.obj)


@tasks_app.command("start")
def tasks_start(ctx: typer.Context, file: Path = typer.Option(..., "--file", exists=True, dir_okay=False)):
    _call(ctx, "POST", "/tasks", _load(file))


@tasks_app.command("list")
def tasks_list(ctx: typer.Context):
    _call(ctx, "GET", "/tasks")


@servers_app.command("probe")
def servers_probe(ctx: typer.Context,
                  file: Path = typer.Option(..., "--file", exists=True, dir_okay=False),
                  at: str | None = typer.Option(None, "--at", help="One-shot ISO 8601 start time, including timezone."),
                  dry_run: bool = typer.Option(False, "--dry-run", help="Validate assignments without game traffic.")):
    """Submit one probe batch; --at waits locally before submission."""
    from src.service.contracts import TaskSpec

    async def operation():
        value = _load(file)
        if not isinstance(value, dict):
            raise ValueError("Probe file must contain a JSON object")
        value = {"kind": "server_probe", **value}
        if at is not None:
            value["planned_at"] = at
        spec = TaskSpec.model_validate(value)
        if spec.kind != "server_probe":
            raise ValueError("This command only accepts server_probe tasks")
        if spec.planned_at is not None and spec.planned_at <= datetime.now(timezone.utc):
            raise ValueError("Planned time has already passed")
        body = spec.model_dump(mode="json")
        checked = await _request(ctx.obj, "POST", "/server-probes/validate", body)
        if dry_run:
            return checked
        if spec.planned_at is not None:
            while (remaining := (spec.planned_at - datetime.now(timezone.utc)).total_seconds()) > 0:
                await asyncio.sleep(min(remaining, 30))
        else:
            body["planned_at"] = datetime.now(timezone.utc).isoformat()
        return await _request(ctx.obj, "POST", "/server-probes", body)

    _emit(_run(ctx, operation()), ctx.obj)


def server_probe_csv(record):
    stream = io.StringIO(newline="")
    columns = ["task_id", "planned_at", "batch_started_at", "batch_finished_at", "target_index", "host",
               "flash_port", "h5_port", "domain", "source_server_index", "account_id", "egress_group_id", "scheduled_at", "start_before",
               "exit_ip", "transport", "status", "stage", "server_index", "zone_index", "zone_name",
               "started_at", "finished_at", "duration_ms", "authenticate_ms", "connect_ms", "login_ms",
               "connection_status", "login_status", "connected_at", "logged_in_at", "initialized_at",
               "initialization_status", "initialize_ms", "execute_ms", "authentication_response",
               "login_response", "execution", "attempts", "skipped_slots", "error"]
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for server in record.get("server_results", []):
        for transport in ("flash", "h5"):
            result = server[transport]
            row = {name: result.get(name) for name in columns if name in result}
            row.update({name: server.get(name) for name in ("target_index", "account_id", "egress_group_id", "exit_ip", "scheduled_at", "start_before")})
            row.update({name: server["server"][name] for name in ("host", "flash_port", "h5_port", "domain")})
            row.update(task_id=record["task_id"], planned_at=record.get("spec", {}).get("planned_at"),
                       batch_started_at=record.get("started_at"), batch_finished_at=record.get("finished_at"),
                       source_server_index=server["server"].get("server_index"), transport=transport)
            row.update({name + "_ms": result.get("stage_durations_ms", {}).get(name)
                        for name in ("authenticate", "connect", "login", "initialize", "execute")})
            for name in ("authentication_response", "login_response", "execution", "attempts", "skipped_slots"):
                row[name] = json.dumps(result.get(name), ensure_ascii=False)
            if server.get("error"):
                row["error"] = result.get("error") or server["error"]["message"]
            writer.writerow(row)
    return stream.getvalue()


@servers_app.command("results")
def servers_results(ctx: typer.Context, task_id: str,
                    format: str = typer.Option("json", "--format", help="json or csv"),
                    output: Path | None = typer.Option(None, "--output", dir_okay=False)):
    """Read a probe task and optionally export a new JSON/CSV file."""
    if format not in {"json", "csv"}:
        raise typer.BadParameter("Format must be json or csv")

    async def operation():
        record = await _request(ctx.obj, "GET", f"/tasks/{task_id}")
        if record["kind"] != "server_probe":
            raise ValueError("Task is not a server_probe")
        return record

    record = _run(ctx, operation())
    text = server_probe_csv(record) if format == "csv" else json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        try:
            with output.open("x", encoding="utf-8-sig" if format == "csv" else "utf-8", newline="") as stream:
                stream.write(text)
        except OSError as error:
            raise typer.BadParameter(f"Cannot create export {output.name}: {type(error).__name__}") from None
        _emit({"task_id": task_id, "output": str(output), "format": format}, ctx.obj)
    elif format == "csv":
        typer.echo(text, nl=False)
    else:
        _emit(record, ctx.obj)


@tasks_app.command("status")
def tasks_status(ctx: typer.Context, task_id: str):
    _call(ctx, "GET", f"/tasks/{task_id}")


@tasks_app.command("cancel")
def tasks_cancel(ctx: typer.Context, task_id: str):
    _call(ctx, "POST", f"/tasks/{task_id}/cancel")


@tasks_app.command("results")
def tasks_results(ctx: typer.Context, task_id: str):
    _call(ctx, "GET", f"/tasks/{task_id}/results")


@scripts_app.command("list")
def scripts_list(ctx: typer.Context):
    _call(ctx, "GET", "/scripts")


@app.command("parse")
def parse(ctx: typer.Context, file: Path = typer.Option(..., "--file", exists=True, dir_okay=False)):
    """Parse local message text into the public message snapshot format."""
    try:
        text = file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise typer.BadParameter(f"Cannot read message file {file.name}") from None
    _call(ctx, "POST", "/messages/parse", {"text": text})


@logs_app.command("show")
def logs_show(ctx: typer.Context, task_id: str):
    _call(ctx, "GET", f"/tasks/{task_id}/logs")


async def _follow(options: Options, task_id: str | None):
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=45)
    async with aiohttp.ClientSession(trust_env=False, timeout=timeout) as client:
        async with client.get(options.url + "/api/v1/events",
                              params={"task_id": task_id} if task_id else None) as response:
            if response.status >= 400:
                raise ValueError(f"Event subscription failed: HTTP {response.status}")
            async for raw in response.content:
                line = raw.decode("utf-8").strip()
                if line.startswith("data: "):
                    _emit(json.loads(line[6:]), options)


@logs_app.command("follow")
def logs_follow(ctx: typer.Context, task_id: str | None = typer.Argument(None)):
    """Follow live events; interrupting the CLI leaves backend tasks running."""
    try:
        _run(ctx, _follow(ctx.obj, task_id))
    except KeyboardInterrupt:
        return


@egress_app.command("list")
def egress_list(ctx: typer.Context):
    _call(ctx, "GET", "/egress-groups")


@egress_app.command("create")
def egress_create(ctx: typer.Context, name: str = typer.Option(..., "--name"),
                  policy: str = typer.Option(..., "--policy"),
                  max_online: int = typer.Option(25, "--max-online", min=25, max=25)):
    _call(ctx, "POST", "/egress-groups", {"name": name, "clash_policy": policy, "max_online": max_online})


@egress_app.command("update")
def egress_update(ctx: typer.Context, group_id: str,
                  file: Path = typer.Option(..., "--file", exists=True, dir_okay=False)):
    """Submit name, clash_policy, max_online and routing_confirmed from a JSON file."""
    _call(ctx, "PUT", f"/egress-groups/{group_id}", _load(file))


@egress_app.command("confirm")
def egress_confirm(ctx: typer.Context, group_id: str):
    """Confirm that the exported process rule is active in Clash."""
    async def operation():
        value = await _request(ctx.obj, "GET", f"/egress-groups/{group_id}")
        body = {key: value[key] for key in ("name", "clash_policy", "max_online", "enabled")}
        return await _request(ctx.obj, "PUT", f"/egress-groups/{group_id}", {**body, "routing_confirmed": True})
    _emit(_run(ctx, operation()), ctx.obj)


@egress_app.command("enable")
def egress_enable(ctx: typer.Context, group_id: str, disable: bool = typer.Option(False, "--disable")):
    async def update():
        value = await _request(ctx.obj, "GET", f"/egress-groups/{group_id}")
        body = {key: value[key] for key in ("name", "clash_policy", "max_online", "routing_confirmed")}
        return await _request(ctx.obj, "PUT", f"/egress-groups/{group_id}", {**body, "enabled": not disable})
    _emit(_run(ctx, update()), ctx.obj)


@egress_app.command("delete")
def egress_delete(ctx: typer.Context, group_id: str):
    _call(ctx, "DELETE", f"/egress-groups/{group_id}")


@egress_app.command("rules")
def egress_rules(ctx: typer.Context):
    _call(ctx, "GET", "/egress-groups/rules")


@egress_app.command("probe")
def egress_probe(ctx: typer.Context, group_id: str):
    """Check the worker's public IP without logging in to the game."""
    _call(ctx, "POST", f"/egress-groups/{group_id}/probe")


@egress_app.command("assign")
def egress_assign(ctx: typer.Context, account_ids: list[str] = typer.Argument(...),
                  group_id: str | None = typer.Option(None, "--group"),
                  clear: bool = typer.Option(False, "--clear")):
    """Assign offline accounts to a group, or use --clear for the system default."""
    if bool(group_id) == clear:
        raise typer.BadParameter("Use exactly one of --group GROUP_ID or --clear")
    _call(ctx, "POST", "/accounts/assign-egress", {"account_ids": account_ids, "egress_group_id": group_id})


def main():
    app()


if __name__ == "__main__":
    main()
