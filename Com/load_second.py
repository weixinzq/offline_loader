import asyncio
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import aiohttp

from src.config import load_user_config
from src.accounts.manager import AccountConnection, load_account_specs
from src.messaging.parser import parse_send_message_file
from src.scripting.loader import discover_scripts
from src.scripting.composer import (
    MessageBatchStep,
    ScriptStep,
    execute_combination,
)

START_XML_URL = "https://aola.100bt.com/play/start.xml"


async def main():
#指定单个账号，可并行多账号
    spec = next(
        s for s in load_account_specs(load_user_config())
        if s.label == "账号 1"
    )
    spec.config["browser_verification"] = True

# 加载通用消息
    messages = parse_send_message_file(
        Path(__file__).resolve().parent / "messages.txt"
    )

# 加载脚本
    script = next(
        s for s in discover_scripts()
        if s.module_name == "scripts.mt250816_1"  # 四象一
    )

    steps = [
        #MessageBatchStep(tuple(messages))
        ScriptStep(script),
    ]

    while True:
        connection = AccountConnection(spec)
        try:
            start = time.perf_counter()

            # 尝试连接
            if not await connection.connect():
                if not connection.retryable:
                    print(f"登录已停止: {connection.last_error}")
                    break
                raise ConnectionError(connection.last_error)
            now_time = time.time()
            local_time = time.localtime(now_time)
            formatted_time = time.strftime('%Y-%m-%d %H:%M:%S', local_time)
            print(f"连接成功,当前时间{formatted_time}")
            context = connection.context
            context.log_callback = lambda message: print(
                f"[{spec.label}] {message}", flush=True
            )
           # 等待玩家初始化
            await context.wait_for_player_initialization()
            # 执行组合
            result = await execute_combination(
                spec.label,
                context,
                steps,
                repetitions=1,
                message_delay=0.2,
            )

            end = time.perf_counter()

            print(
                f"组合完成, 耗时 {end - start:.2f} 秒"
                if result.success
                else "组合失败"
            )

            for step in result.step_results:
                if not step.success:
                    print(step.description, step.error)

            # 正常执行完毕后退出 while
            break
        except ConnectionError as e:
            print(f"连接失败: {e}")
            await asyncio.sleep(0.5)  #暂停时间
        finally:

            await connection.disconnect()


if __name__ == "__main__":
    asyncio.run(main())




