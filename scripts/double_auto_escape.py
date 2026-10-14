"""Wait for battle entry, check opponent IDs, then optionally send messages."""
from __future__ import annotations

import asyncio

from src.messaging.dispatcher import SendResult
from src.messaging.parser import SendMessage
from src.network.context import AppContext
from src.scripting.composer import MessageBatchStep, ScriptStep, execute_combination
from src.scripting.loader import InteractionScript, ScriptExecutionError
from src.messaging.parser import parse_send_message
SCRIPT_NAME = "双排自动逃跑"
SCRIPT_DESCRIPTION = "前置消息 → 等待战斗就绪 → 判断对方 ID → 条件后置消息"
WAIT_TIMEOUT_SECONDS = 60.0

# 填写不触发后续操作的对方 ID

SPECIFIED_OPPONENT_IDS: set[int] = set(94617668)

# 前置消息沿用 auto_escape.py，可在这里修改匹配参数。
BEFORE_STEPS: tuple[MessageBatchStep, ...] = (
    MessageBatchStep((
        SendMessage(id=42, cmd="AHM230616_1", param={"tid": 302}),
        SendMessage(id=42, cmd="SA231229_match", param={"mt": 1}),
    )),
)

# 仅当任一对方 ID 不在指定集合中时执行；
OPTIONAL_STEPS: tuple[MessageBatchStep, ...] = (    
    MessageBatchStep((
        SendMessage(-1, "#time", {"seconds": 5}),
        #SendMessage(id=-1, cmd="#wait", param={}),
    )),
)
AFTER_STEPS: tuple[MessageBatchStep, ...] =(
    MessageBatchStep((
        SendMessage(id = 13, cmd = "1404", param={"turn":0}),
        SendMessage(id=-1, cmd="#wait", param={}),
    )),
)
async def run(context: AppContext) -> tuple[SendResult, ...]:
    if not BEFORE_STEPS:
        raise ValueError("请先填写 double_auto_escape.py 的 BEFORE_STEPS")
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
                        message = await subscription.wait_for(lambda m: True, timeout=1)
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

    async def process_opponents(wait_context: AppContext) -> tuple[SendResult, ...]:
        # 接收路由已从 pmmList[*].battleView.pmmId 更新当前战斗参战者缓存。
        opponent_ids = set(wait_context.auto_battle.players) - {int(wait_context.user_id)}
        if not opponent_ids:
            raise RuntimeError("未读取到参战对方 ID，不能判断是否执行后续操作")
        wait_context.log(f"参战对方 ID：{sorted(opponent_ids)}")
        unmatched_ids = opponent_ids - SPECIFIED_OPPONENT_IDS
        if not unmatched_ids:
            wait_context.log("对方 ID 均在指定集合中，跳过后续操作")
            return ()
        wait_context.log(f"对方 ID 不在指定集合中：{sorted(unmatched_ids)}，触发后续操作")
        result = await execute_combination(
            wait_context.user_id, wait_context, OPTIONAL_STEPS, preserve_current_battle=True
        )
        if not result.success:
            failure = next(step for step in result.step_results if not step.success)
            raise ScriptExecutionError(
                f"后续步骤 {failure.step}（{failure.description}）失败：{failure.error}",
                result.send_results,
            )
        wait_context.log("条件后置消息步骤已全部提交")
        return result.send_results

    try:
        steps = (
            *BEFORE_STEPS,
            ScriptStep(InteractionScript(
                module_name=f"{__name__}.wait_for_entry",
                name="等待本次战斗就绪",
                description="等待 2303/2401/2426/2402",
                run=wait_for_entry,
            )),
            ScriptStep(InteractionScript(
                module_name=f"{__name__}.process_opponents",
                name="判断对方 ID 并执行后续消息",
                description="任一对方 ID 不在指定集合中时执行后续步骤",
                run=process_opponents,
            )),
            *AFTER_STEPS,
        )
        result = await execute_combination(
            context.user_id, context, steps, preserve_current_battle=True
        )
        if not result.success:
            failure = next(step for step in result.step_results if not step.success)
            raise ScriptExecutionError(
                f"步骤 {failure.step}（{failure.description}）失败：{failure.error}",
                result.send_results,
            )
        return result.send_results
    finally:
        subscription.close()
