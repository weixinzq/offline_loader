"""Application orchestration: account modes, persistent menus and dispatch."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import Sequence

from src.accounts.manager import (
    AccountConfigError,
    AccountConnection,
    AccountManager,
    AccountSpec,
    load_account_specs,
)
from src.messaging.audit_log import append_send_results
from src.config import CONFIG_PATH, default_config, load_user_config
from src.network.context import AppContext
from src.messaging.dispatcher import SendResult, dispatch_to_accounts
from src.scripting.loader import InteractionScript, discover_scripts
from src.messaging.parser import (
    SendMessage,
    SendMessageParseError,
    parse_pipe_separated_messages,
    parse_send_message_file,
)


class ConsoleInput:
    """Read stdin on one daemon thread so disconnects can interrupt the menu."""

    def __init__(self):
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._thread = threading.Thread(
            target=self._read_lines,
            name="console-input",
            daemon=True,
        )
        self._thread.start()

    def _read_lines(self) -> None:
        while True:
            line = sys.stdin.readline()
            value = line.rstrip("\r\n") if line else None
            self._loop.call_soon_threadsafe(self._queue.put_nowait, value)
            if line == "":
                return

    async def readline(self, prompt: str) -> str | None:
        print(prompt, end="", flush=True)
        return await self._queue.get()


def ensure_config() -> tuple[dict, list[AccountSpec]] | None:
    try:
        config = load_user_config()
        specs = load_account_specs(config)
    except (AccountConfigError, json.JSONDecodeError, OSError) as exc:
        if not CONFIG_PATH.exists():
            template = default_config()
            template.pop("account", None)
            template.pop("password", None)
            template.pop("char_id", None)
            template["accounts"] = [
                {
                    "label": "账号 1",
                    "account": "你的账号",
                    "password": "你的密码",
                    "char_id": 0,
                    "zone_index": 1025,
                }
            ]
            CONFIG_PATH.write_text(
                json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(f"配置无效：{exc}", flush=True)
        print(f"请填写配置文件：{CONFIG_PATH}", flush=True)
        return None
    return config, specs


def parse_index_selection(text: str, count: int, allow_all: bool) -> list[int]:
    """Parse one-based comma-separated menu indexes into stable unique indexes."""
    choice = text.strip().lower()
    if allow_all and choice in {"a", "all", "全部"}:
        return list(range(count))
    if not choice:
        raise ValueError("选择不能为空")

    selected: list[int] = []
    for part in choice.replace("，", ",").split(","):
        try:
            index = int(part.strip()) - 1
        except ValueError as exc:
            raise ValueError(f"无效序号：{part}") from exc
        if not 0 <= index < count:
            raise ValueError(f"序号超出范围：{index + 1}")
        if index not in selected:
            selected.append(index)
    return selected


async def choose_items(
    console: ConsoleInput,
    items: Sequence,
    label_getter,
    prompt: str,
    allow_all: bool,
) -> list:
    if not items:
        return []
    for index, item in enumerate(items, start=1):
        print(f"  {index}. {label_getter(item)}", flush=True)
    if allow_all:
        print("  a. 全部", flush=True)
    raw = await console.readline(prompt)
    if raw is None:
        return []
    try:
        indexes = parse_index_selection(raw, len(items), allow_all)
    except ValueError as exc:
        print(f"无效选择：{exc}", flush=True)
        return []
    return [items[index] for index in indexes]


async def read_menu_choice(
    context: AppContext, console: ConsoleInput
) -> str | None:
    choice_task = asyncio.create_task(console.readline("请选择："))
    disconnected_task = asyncio.create_task(context.messages.disconnected.wait())
    done, pending = await asyncio.wait(
        {choice_task, disconnected_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if disconnected_task in done and disconnected_task.result():
        return None
    return choice_task.result()


def print_message_summary(messages: Sequence[SendMessage]) -> None:
    print(f"\n已完整解析 {len(messages)} 条消息：", flush=True)
    for index, message in enumerate(messages, start=1):
        print(f"  {index}. id={message.id}, cmd={message.cmd}", flush=True)


async def confirm(console: ConsoleInput, prompt: str = "确认发送？[y/N]：") -> bool:
    raw = await console.readline(prompt)
    return raw is not None and raw.strip().lower() in {"y", "yes", "是"}


async def prompt_pasted_message(console: ConsoleInput) -> list[SendMessage]:
    raw = await console.readline("请粘贴消息序列（用 | 分隔，支持 #send、#wait、#time）：")
    if raw is None:
        return []
    try:
        return parse_pipe_separated_messages(raw, "<粘贴文本>")
    except SendMessageParseError as exc:
        print(f"解析失败：{exc}", flush=True)
        return []


async def prompt_message_file(console: ConsoleInput) -> list[SendMessage]:
    raw = await console.readline("请输入消息文件路径：")
    if raw is None or not raw.strip():
        return []
    path = Path(raw.strip().strip('"'))
    try:
        return parse_send_message_file(path)
    except SendMessageParseError as exc:
        print(f"解析失败：{exc}", flush=True)
        return []


def print_send_results(batches: dict[str, list[SendResult]]) -> None:
    print("\n发送结果：", flush=True)
    for account, results in batches.items():
        if not results:
            print(f"  [{account}] 未发送", flush=True)
            continue
        for result in results:
            status = "成功" if result.success else f"失败：{result.error}"
            print(
                f"  [{account}] #{result.sequence} id={result.ext_id} "
                f"cmd={result.cmd} — {status} ({result.timestamp})",
                flush=True,
            )


async def dispatch_with_confirmation(
    console: ConsoleInput,
    targets: Sequence[AccountConnection],
    messages: Sequence[SendMessage],
) -> None:
    if not messages:
        return
    online = [item for item in targets if item.online and item.context is not None]
    if not online:
        print("没有可用的在线账号。", flush=True)
        return
    print_message_summary(messages)
    print("目标账号：" + "、".join(item.spec.label for item in online), flush=True)
    print("策略：账号间并行；账号内原序；战斗按服务器阶段推进；失败即停止。")
    if not await confirm(console):
        print("已取消发送。", flush=True)
        return

    dispatch_task = asyncio.create_task(
        dispatch_to_accounts(
            [(item.spec.label, item.context) for item in online], messages
        ),
        name="message-dispatch",
    )
    cancel_task = asyncio.create_task(
        console.readline("发送中；输入 c 可取消任务："), name="dispatch-cancel-input"
    )
    done, _pending = await asyncio.wait(
        {dispatch_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if (
        dispatch_task not in done
        and cancel_task in done
        and cancel_task.result() is not None
    ):
        if cancel_task.result().strip().lower() in {"c", "cancel", "取消"}:
            dispatch_task.cancel()
            await asyncio.gather(dispatch_task, return_exceptions=True)
            print("发送任务已取消；账号连接继续保持。", flush=True)
            return
    if not cancel_task.done():
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)

    batches = await dispatch_task
    print_send_results(batches)
    try:
        log_path = append_send_results(batches)
    except OSError as exc:
        print(f"发送结果日志写入失败：{exc}", flush=True)
    else:
        print(f"发送结果已记录：{log_path}", flush=True)


def print_connection_status(connections: Sequence[AccountConnection]) -> None:
    print("\n账号状态：", flush=True)
    for connection in connections:
        connection.refresh_state()
        suffix = f"；原因={connection.last_error}" if connection.last_error else ""
        zone = ""
        if connection.online and connection.context is not None:
            current = connection.context.zone
            zone = f"；区服={current.zone_index} {current.zone_name}"
        print(f"  {connection.spec.label}：{connection.state}{zone}{suffix}", flush=True)


async def run_script_selection(
    connection: AccountConnection, console: ConsoleInput
) -> None:
    context = connection.context
    if context is None or not connection.online:
        print("账号不在线。", flush=True)
        return
    scripts: list[InteractionScript] = discover_scripts()
    if not scripts:
        print("没有可用交互脚本。", flush=True)
        return
    print("\n可用网络交互脚本：", flush=True)
    selected = await choose_items(
        console,
        scripts,
        lambda script: script.name,
        "请选择脚本：",
        allow_all=False,
    )
    if not selected:
        return
    script = selected[0]
    print(f"[脚本] 开始执行：{script.name}", flush=True)
    try:
        await script.run(context)
    except asyncio.TimeoutError:
        print("[脚本] 等待服务器响应超时。", flush=True)
    except Exception as exc:
        print(f"[脚本] 执行失败：{exc}", flush=True)
    else:
        print(f"[脚本] 执行完成：{script.name}", flush=True)


async def run_single_menu(
    connection: AccountConnection, console: ConsoleInput
) -> None:
    context = connection.context
    if context is None:
        return
    while connection.online:
        print(
            "\n账号操作：\n"
            "  1. 粘贴消息序列\n"
            "  2. 选择消息文件并执行\n"
            "  3. 运行专用交互脚本\n"
            "  4. 查看连接状态\n"
            "  5. 断开并返回主菜单",
            flush=True,
        )
        choice = await read_menu_choice(context, console)
        if choice is None:
            connection.refresh_state()
            print("连接已断开，返回主菜单。", flush=True)
            return
        choice = choice.strip().lower()
        if choice == "1":
            await dispatch_with_confirmation(
                console, [connection], await prompt_pasted_message(console)
            )
        elif choice == "2":
            await dispatch_with_confirmation(
                console, [connection], await prompt_message_file(console)
            )
        elif choice == "3":
            await run_script_selection(connection, console)
        elif choice == "4":
            print_connection_status([connection])
        elif choice in {"5", "q", "quit", "exit"}:
            return
        else:
            print("无效选择。", flush=True)


async def run_single_mode(spec: AccountSpec, console: ConsoleInput) -> None:
    connection = AccountConnection(spec)
    print(f"正在登录：{spec.label}", flush=True)
    try:
        if not await connection.connect():
            print(f"登录失败：{connection.last_error}", flush=True)
            return
        print_connection_status([connection])
        await run_single_menu(connection, console)
    finally:
        await connection.disconnect()


async def choose_connections(
    console: ConsoleInput,
    connections: Sequence[AccountConnection],
    prompt: str,
) -> list[AccountConnection]:
    return await choose_items(
        console,
        connections,
        lambda item: f"{item.spec.label}（{item.state}）",
        prompt,
        allow_all=True,
    )


async def run_multi_menu(manager: AccountManager, console: ConsoleInput) -> None:
    while True:
        for connection in manager.connections:
            connection.refresh_state()
        print(
            "\n多账号操作：\n"
            "  1. 粘贴消息序列\n"
            "  2. 选择消息文件并执行\n"
            "  3. 查看账号状态\n"
            "  4. 重连指定账号\n"
            "  5. 断开指定账号\n"
            "  6. 断开全部并返回主菜单",
            flush=True,
        )
        raw = await console.readline("请选择：")
        if raw is None:
            return
        choice = raw.strip().lower()
        if choice in {"1", "2"}:
            online = [item for item in manager.connections if item.online]
            targets = await choose_connections(console, online, "选择目标账号：")
            if not targets:
                continue
            messages = (
                await prompt_pasted_message(console)
                if choice == "1"
                else await prompt_message_file(console)
            )
            await dispatch_with_confirmation(console, targets, messages)
        elif choice == "3":
            print_connection_status(manager.connections)
        elif choice == "4":
            offline = [item for item in manager.connections if not item.online]
            targets = await choose_connections(console, offline, "选择重连账号：")
            for connection in targets:
                print(f"正在重连：{connection.spec.label}", flush=True)
                await connection.connect()
            print_connection_status(manager.connections)
        elif choice == "5":
            online = [item for item in manager.connections if item.online]
            targets = await choose_connections(console, online, "选择断开账号：")
            await asyncio.gather(*(item.disconnect() for item in targets))
            print_connection_status(manager.connections)
        elif choice in {"6", "q", "quit", "exit"}:
            return
        else:
            print("无效选择。", flush=True)


async def run_multi_mode(specs: list[AccountSpec], console: ConsoleInput) -> None:
    selected = await choose_items(
        console,
        specs,
        lambda spec: spec.label,
        "选择需要登录的账号：",
        allow_all=True,
    )
    if not selected:
        return
    manager = AccountManager.from_specs(selected)
    try:
        print("正在并行登录（最大并发数 5）……", flush=True)
        await manager.connect_all(max_concurrency=5)
        print_connection_status(manager.connections)
        if any(item.online for item in manager.connections):
            await run_multi_menu(manager, console)
    finally:
        await manager.disconnect_all()


async def run_application() -> None:
    loaded = ensure_config()
    if loaded is None:
        return
    _config, specs = loaded

    print("=== 奥拉星长连接客户端 ===", flush=True)
    console = ConsoleInput()
    while True:
        print(
            "\n主菜单：\n"
            "  1. 单账号登录\n"
            "  2. 多账号登录\n"
            "  3. 退出",
            flush=True,
        )
        raw = await console.readline("请选择：")
        if raw is None:
            return
        choice = raw.strip().lower()
        if choice == "1":
            selected = await choose_items(
                console,
                specs,
                lambda spec: spec.label,
                "选择账号：",
                allow_all=False,
            )
            if selected:
                await run_single_mode(selected[0], console)
        elif choice == "2":
            await run_multi_mode(specs, console)
        elif choice in {"3", "q", "quit", "exit"}:
            return
        else:
            print("无效选择。", flush=True)


def main() -> None:
    from src.ui.runtime_log import configure_runtime_logging

    configure_runtime_logging(capture_console=False)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        asyncio.run(run_application())
    except KeyboardInterrupt:
        print("\n已取消并退出。", flush=True)
