"""Authentication, zone selection, connection and context lifecycle."""
from __future__ import annotations

import asyncio
import logging

from src.network.context import AppContext, MessageRouter
from src.accounts.login import LoginResult, ZoneData, http_login
from src.network.socket_client import login_and_connect
from src.protocol.operations import init_game

logger = logging.getLogger(__name__)


def select_zone(result: LoginResult, preferred_zone: int = 1025) -> ZoneData | None:
    available = [
        zone for zone in result.zone_list
        if (zone.host and zone.flash_port > 0) or (zone.domain and zone.port > 0)
    ]
    return next(
        (zone for zone in available if zone.zone_index == preferred_zone),
        None,
    )


async def authenticate(config: dict) -> tuple[LoginResult, ZoneData]:
    result = await http_login(
        config["account"],
        config["password"],
        char_id=int(config.get("char_id", 0)),
        login_url=config.get("login_url"),
        register_url=config.get("register_url"),
        browser_verification=bool(config.get("browser_verification", False)),
    )
    if not result.success:
        raise ConnectionError(result.error_msg or "HTTP 登录失败")

    zone_index = int(config.get("zone_index", 1))
    zone = select_zone(result, zone_index)
    if zone is None:
        raise ConnectionError(f"指定区服 {zone_index} 不存在或没有可用的 TCP/WSS 地址")
    return result, zone


async def initialize_player_context(
    context: AppContext, timeout: float = 15.0
) -> None:
    """Run the H5 post-login handshake and wait for player-data readiness."""
    start_info = context.messages.subscribe(max_queue_size=200)
    try:
        login_response = getattr(context.socket, "login_response", {})
        last_login_time = int(login_response.get("lastLoginTime", 0) or 0)
        await init_game(
            context.socket,
            last_login_time=last_login_time,
        )
        expected = {"getCurrentTime", "11_1r", "getStartInfo"}
        seen: set[str] = set()
        deadline = asyncio.get_running_loop().time() + timeout
        while seen != expected:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                missing = "、".join(sorted(expected - seen))
                raise ConnectionError(f"等待启动响应超时：{missing}")
            try:
                response = await start_info.wait_for(
                    lambda message: str(message.get("_cmd", "")) in expected,
                    timeout=remaining,
                )
            except asyncio.TimeoutError as exc:
                missing = "、".join(sorted(expected - seen))
                raise ConnectionError(f"等待启动响应超时：{missing}") from exc
            if response.get("msg"):
                raise ConnectionError(str(response["msg"]))
            seen.add(str(response.get("_cmd", "")))

        for command, response_command in (("1101", "2101"), ("1212", "2212")):
            await context.socket.send_xt_message(13, command, {})
            try:
                response = await start_info.wait_for(
                    lambda message, expected=response_command: str(
                        message.get("_cmd", "")
                    )
                    == expected,
                    timeout=timeout,
                )
            except asyncio.TimeoutError as exc:
                raise ConnectionError(
                    f"等待玩家资料响应 {response_command} 超时"
                ) from exc
            if response.get("msg"):
                raise ConnectionError(
                    f"服务器返回 {response_command}：{response['msg']}"
                )
    finally:
        start_info.close()


async def initialize_session_clock(
    context: AppContext, timeout: float = 15.0
) -> None:
    """Start the official 55_9 -> 55_1 -> periodic 55_2 session flow."""
    responses = context.messages.subscribe(max_queue_size=50)
    try:
        await context.socket.send_xt_message(18, "55_9", {"platformId": 1})
        try:
            response = await responses.wait_for(
                lambda message: str(message.get("_cmd", "")) == "55_9",
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ConnectionError("等待会话响应 55_9 超时") from exc
        if response.get("msg"):
            raise ConnectionError(f"服务器返回 55_9：{response['msg']}")
        if "r" in response and response.get("r") != 1:
            raise ConnectionError(f"服务器返回 55_9.r={response.get('r')!r}")

        await context.socket.send_xt_message(18, "55_1", {})
        try:
            response = await responses.wait_for(
                lambda message: str(message.get("_cmd", "")) == "55_1",
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ConnectionError("等待会话响应 55_1 超时") from exc
        if response.get("msg"):
            raise ConnectionError(f"服务器返回 55_1：{response['msg']}")
        session_time = response.get("time")
        if (
            isinstance(session_time, bool)
            or not isinstance(session_time, int)
            or session_time < 0
        ):
            raise ConnectionError(f"55_1.time 必须是非负整数：{session_time!r}")
        context.session_clock.start(session_time)
    finally:
        responses.close()


async def _run_player_initialization(
    context: AppContext, timeout: float
) -> None:
    context.log("正在初始化玩家资料")
    try:
        await initialize_player_context(context, timeout=timeout)
        await initialize_session_clock(context, timeout=timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        context.player_initialization_error = str(exc)
        context.log(f"玩家资料初始化失败：{exc}")
        logger.warning("player initialization failed: %s", exc)
    else:
        context.player_initialized = True
        context.player_initialization_error = ""
        context.log("玩家资料已就绪；尚未进入房间")
        logger.info("player initialization completed")


def start_player_initialization(
    context: AppContext, timeout: float = 15.0
) -> None:
    context.player_initialization_task = asyncio.create_task(
        _run_player_initialization(context, timeout),
        name="player-context-initialization",
    )


async def open_context(config: dict) -> AppContext:
    result, zone = await authenticate(config)
    socket = await login_and_connect(
        zone,
        result.user_id,
        result.session_id,
        timeout=float(config.get("timeout", 2)),
    )
    if socket is None:
        raise ConnectionError("游戏长连接登录失败")

    router = MessageRouter(socket)
    context = AppContext(config, result, zone, socket, router)
    router.start()
    start_player_initialization(
        context, timeout=float(config.get("timeout", 15))
    )
    return context
