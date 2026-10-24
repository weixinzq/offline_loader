"""One-shot game login probes; called only inside the selected egress worker."""
import asyncio
from datetime import datetime, timezone
from time import monotonic

from src.accounts.login import http_login
from src.network.socket_client import GameSocket


def now():
    return datetime.now(timezone.utc).isoformat()


def transport_result():
    return {"status": "not_tested", "stage": None, "started_at": None, "finished_at": None,
            "duration_ms": None, "stage_durations_ms": {},
            "connection_status": "not_attempted", "login_status": "not_attempted",
            "server_index": None, "zone_index": None, "zone_name": None, "error": None}


def blocked_code(reason):
    text = str(reason).casefold()
    if any(marker in text for marker in ("captcha", "verification", "人机验证")):
        return "verification_required"
    if any(marker in text for marker in ("429", "限流", "频繁", "rate_limit", "rate limit", "too many")):
        return "rate_limited"
    return "login_blocked" if reason else None


async def close_socket(socket, timeout):
    transports = [getattr(getattr(socket, name, None), "transport", None) for name in ("_ws", "_writer")]
    try:
        await asyncio.wait_for(socket.close(), timeout)
    except TimeoutError:
        for transport in transports:
            if transport is not None:
                transport.abort()
        raise


async def probe_server(config, target, timeout, outcome, redactor):
    """Fill partial results even on cancellation; never return credentials or raw replies."""
    outcome.update(flash=transport_result(), h5=transport_result())
    if not target["flash_port"]:
        outcome["flash"]["status"] = "not_supported"
    if not target["h5_port"] or not target["domain"]:
        outcome["h5"]["status"] = "not_supported"
    for transport in ("flash", "h5"):
        row = outcome[transport]
        if row["status"] == "not_supported":
            continue
        started = monotonic()
        stage_started = started
        row.update(status="running", stage="authenticate", started_at=now())
        socket = None
        try:
            async with asyncio.timeout(timeout):
                login = await http_login(config["account"], config["password"],
                    char_id=config.get("char_id", 0), login_url=config.get("login_url"),
                    register_url=config.get("register_url"), browser_verification=False,
                    network_mode="system", timeout=timeout, diagnostic_dir=config.get("diagnostic_dir"))
            row["stage_durations_ms"]["authenticate"] = round((monotonic() - stage_started) * 1000, 1)
            redactor.add(login.session_id)
            redactor.add(login.user_id)
            if not login.success:
                code = blocked_code(login.blocked_reason)
                row.update(status="authentication_failed", error=redactor.clean(login.error_msg))
                if code:
                    outcome["blocked_code"] = code
                    break
                continue
            # Indices can change between snapshots: resolve the complete endpoint again.
            server = next((s for s in login.server_list if
                (s.host, s.flash_port, s.h5_port, s.domain.lower().rstrip(".")) ==
                (target["host"], target["flash_port"], target["h5_port"], target["domain"])), None)
            if server is None:
                row.update(status="endpoint_missing", stage="resolve", error="登录响应中未找到目标服务器条目")
                continue
            row["server_index"] = server.server_index
            zones = [z for z in login.zone_list if z.server_index == server.server_index]
            if not zones:
                row.update(status="no_zone", stage="resolve", error="服务器条目没有关联的可测试区服")
                continue
            zone = min(zones, key=lambda z: z.zone_index)
            row.update(zone_index=zone.zone_index, zone_name=zone.zone_name, stage="connect")
            stage_started = monotonic()
            socket = GameSocket(login.session_id, network_mode="system")
            async with asyncio.timeout(timeout):
                connected = await (socket.connect_tcp(server.host, server.flash_port, timeout)
                    if transport == "flash" else socket.connect_ws(server.domain, server.h5_port, timeout))
            row["stage_durations_ms"]["connect"] = round((monotonic() - stage_started) * 1000, 1)
            row["connection_status"] = "succeeded" if connected else "failed"
            if not connected:
                row.update(status="connection_failed", error=redactor.clean(socket.disconnect_reason))
                continue
            row["stage"] = "login"
            stage_started = monotonic()
            async with asyncio.timeout(timeout):
                reply = await socket.login(f"{zone.zone_index} {zone.zone_name}", login.user_id, login.session_id)
            row["stage_durations_ms"]["login"] = round((monotonic() - stage_started) * 1000, 1)
            # The existing protocol also has a legacy numeric-id success reply without _cmd.
            command = (reply or {}).get("_cmd")
            legacy_success = command is None and str((reply or {}).get("id", "")).isdigit() and int(reply["id"]) > 0
            success = bool(reply) and (command == "logOK" or legacy_success)
            rejected = socket.login_response.get("_cmd") == "logKO"
            row.update(status="available" if success else "login_rejected" if rejected else "login_unconfirmed",
                       login_status="succeeded" if success else "rejected" if rejected else "unconfirmed",
                       error=None if success else redactor.clean(socket.login_response.get("msg") or "未收到游戏登录成功响应"))
        except TimeoutError:
            row.update(status="timeout", error="阶段超时")
            if row["stage"] in {"connect", "login"}:
                row["connection_status" if row["stage"] == "connect" else "login_status"] = "timeout"
        except asyncio.CancelledError:
            row["status"] = "cancelled"
            raise
        except Exception as error:
            row.update(status="failed", error=redactor.clean(error))
        finally:
            if row["stage"] in {"authenticate", "connect", "login"} and row["stage"] not in row["stage_durations_ms"]:
                row["stage_durations_ms"][row["stage"]] = round((monotonic() - stage_started) * 1000, 1)
            try:
                if socket is not None:
                    cleanup = asyncio.create_task(close_socket(socket, max(1, timeout)))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        row["status"] = "cancelled"
                        await cleanup
                        raise
                    except TimeoutError:
                        row.update(status="cleanup_failed", error="关闭连接超时，已中止底层传输")
            finally:
                row.update(finished_at=now(), duration_ms=round((monotonic() - started) * 1000, 1))
