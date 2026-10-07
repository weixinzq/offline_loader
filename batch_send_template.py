"""并发登录全部配置账号，并为每个账号发送一条定制消息的独立脚本模板。

运行前只需要修改下方“消息定制区”。本文件不会被桌面的“专用脚本”列表加载。
从项目根目录运行：python batch_send_template.py
"""
from __future__ import annotations

import asyncio
from typing import Any

from src.accounts.manager import AccountConnection, AccountManager, load_account_specs
from src.config import load_user_config
from src.messaging.dispatcher import execute_messages
from src.messaging.parser import SendMessage


# ==================== 消息定制区：从这里开始修改 ====================

MAX_LOGIN_CONCURRENCY = 5
EXTENSION_ID = 1
COMMAND = "rename"


def build_message(
    account_label: str,
    account_index: int,
    account_config: dict[str, Any],
) -> SendMessage:
    """为一个账号生成消息；account_index 从 1 开始，顺序与 config.json 一致。"""
    return SendMessage(
        id=EXTENSION_ID,
        cmd=COMMAND,
        param={
            "rmember": False,
            "newName": f"违心{account_index}",
        },
    )


# send={"id":1,"param":{"rmember":false,"newName":"违心0"},"cmd":"rename"}|
# ==================== 消息定制区：到这里结束 ====================


def prepare_messages(
    connections: list[AccountConnection],
) -> dict[str, SendMessage]:
    """在联网前生成并检查所有消息，避免模板未改好时误登录。"""
    if COMMAND == "CHANGE_ME":
        raise ValueError("请先修改 batch_send_template.py 中的 COMMAND")

    prepared: dict[str, SendMessage] = {}
    for index, connection in enumerate(connections, start=1):
        prepared[connection.spec.label] = build_message(
            connection.spec.label,
            index,
            connection.spec.config,
        )
    return prepared


async def send_one(
    connection: AccountConnection,
    message: SendMessage,
) -> None:
    label = connection.spec.label
    context = connection.context
    if context is None or not connection.online:
        print(f"[{label}] 跳过：{connection.last_error or connection.state}")
        return

    try:
        await context.wait_for_player_initialization()
        results = await execute_messages(label, context, (message,))
    except Exception as exc:
        print(f"[{label}] 发送失败：{exc}")
        return

    for result in results:
        if result.success:
            # 普通消息的 success 表示已提交到连接，不等同于服务器业务处理成功。
            print(f"[{label}] 已提交：{result.cmd}")
        else:
            print(f"[{label}] 发送失败：{result.cmd}，{result.error}")


async def main() -> None:
    config = load_user_config()

    manager = AccountManager.from_specs(load_account_specs(config))
    messages = prepare_messages(manager.connections)
    connection_slots = asyncio.Semaphore(MAX_LOGIN_CONCURRENCY)
    total = len(manager.connections)

    print(f"准备处理 {total} 个账号，同时最多保持 {MAX_LOGIN_CONCURRENCY} 个连接……")

    async def process_one(index: int, connection: AccountConnection) -> None:
        label = connection.spec.label
        async with connection_slots:
            print(f"[{index}/{total}] {label} 开始登录")
            try:
                connected = await connection.connect()
                if not connected:
                    print(f"[{index}/{total}] {label} 登录失败：{connection.last_error}")
                    return
                await send_one(connection, messages[label])
            finally:
                try:
                    await connection.disconnect()
                except Exception as exc:
                    print(f"[{index}/{total}] {label} 断开异常：{exc}")
                else:
                    print(f"[{index}/{total}] {label} 已释放连接")

    try:
        await asyncio.gather(
            *(
                process_one(index, connection)
                for index, connection in enumerate(manager.connections, start=1)
            )
        )
    finally:
        # 处理 Ctrl+C 或任务异常时仍清理所有可能存活的连接。
        await manager.disconnect_all()


if __name__ == "__main__":
    asyncio.run(main())
