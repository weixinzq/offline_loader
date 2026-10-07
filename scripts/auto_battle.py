"""Run message steps, wait for a new battle, then run the remaining steps."""
from __future__ import annotations

import asyncio

from src.messaging.parser import SendMessage
from src.network.context import AppContext
from src.scripting.composer import MessageBatchStep, ScriptStep, execute_combination
from src.scripting.loader import InteractionScript


SCRIPT_NAME = "战斗就绪后执行消息"
SCRIPT_DESCRIPTION = "前置消息步骤 → 等待新战斗就绪 → 后置消息步骤"
WAIT_TIMEOUT_SECONDS = 60.0

# 在这里填写消息。每个 MessageBatchStep 是一个步骤，内部消息按顺序执行。
# 前后均可添加多个步骤；参数未填写时，脚本会报错，不发送消息。
# 填写形式（将 ... 换成真实内容，再取消注释）：
# MessageBatchStep((
#     SendMessage(id=..., cmd="...", param={}),
#     SendMessage(id=..., cmd="...", param={}),
# )),
BEFORE_STEPS: tuple[MessageBatchStep, ...] = ()
AFTER_STEPS: tuple[MessageBatchStep, ...] = ()


async def run(context: AppContext) -> None:
    if not BEFORE_STEPS or not AFTER_STEPS:
        raise ValueError("请先填写 auto_battle.py 的 BEFORE_STEPS 和 AFTER_STEPS")
    if not all(
        isinstance(step, MessageBatchStep) for step in (*BEFORE_STEPS, *AFTER_STEPS)
    ):
        raise TypeError("前后步骤必须使用 MessageBatchStep")

    await context.wait_for_player_initialization()
    context.assert_automation_allowed()
    if not context.socket.connected or context.messages.disconnected.is_set():
        raise ConnectionError("账号连接已断开")
    if context.battle.phase != "idle":
        raise RuntimeError("当前已有战斗或战斗状态未确认，不能触发新战斗")

    previous_epoch = context.battle.battle_epoch
    expected_epoch = previous_epoch + 1
    # 在前置消息发送之前订阅，保留即时回包；不另开底层接收循环。
    subscription = context.messages.subscribe(max_queue_size=500)

    async def wait_for_entry(wait_context: AppContext) -> None:
        wait_context.log("前置步骤已提交，等待本次战斗完整就绪")
        try:
            async with asyncio.timeout(WAIT_TIMEOUT_SECONDS):
                while True:
                    wait_context.assert_automation_allowed()
                    if (
                        not wait_context.socket.connected
                        or wait_context.messages.disconnected.is_set()
                    ):
                        raise ConnectionError("等待进入战斗期间连接断开")
                    battle = wait_context.battle
                    if battle.phase == "unknown":
                        raise RuntimeError(battle.last_error or "战斗状态未知")
                    if battle.battle_epoch not in {previous_epoch, expected_epoch}:
                        raise RuntimeError("等待期间战斗身份已改变")
                    if battle.battle_epoch == expected_epoch:
                        if battle.phase == "idle":
                            raise RuntimeError("本次战斗在就绪前已经结束")
                        if battle.entry_ready:
                            break
                    try:
                        message = await subscription.wait_for(
                            lambda m: True, timeout=1
                        )
                    except asyncio.TimeoutError:
                        continue
                    if (
                        str(message.get("_cmd", "")) == "2303"
                        and message.get("msg")
                    ):
                        raise RuntimeError(str(message["msg"]))
        except TimeoutError as exc:
            raise TimeoutError(
                f"{WAIT_TIMEOUT_SECONDS:g} 秒内未确认本次战斗完整就绪"
            ) from exc
        wait_context.log(
            f"本次战斗已就绪：battleId={wait_context.battle.battle_id}，"
            f"turn={wait_context.battle.current_turn}"
        )
        subscription.close()

    try:
        steps = (
            *BEFORE_STEPS,
            ScriptStep(
                InteractionScript(
                    module_name=f"{__name__}.wait_for_entry",
                    name="等待本次战斗就绪",
                    description="等待 2303/2401/2426/2402",
                    run=wait_for_entry,
                )
            ),
            *AFTER_STEPS,
        )
        result = await execute_combination(
            context.user_id, context, steps, preserve_current_battle=True
        )
        if not result.success:
            failure = next(step for step in result.step_results if not step.success)
            raise RuntimeError(
                f"步骤 {failure.step}（{failure.description}）失败：{failure.error}"
            )
        context.log("战斗就绪后的消息步骤已全部提交")
    finally:
        subscription.close()
