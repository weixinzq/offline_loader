"""Check matched player IDs before cancelling or waiting for battle and escaping."""
from __future__ import annotations

import asyncio

from src.messaging.dispatcher import SendResult
from src.messaging import battle_control
from src.messaging.parser import SendMessage
from src.network.context import AppContext
from src.scripting.composer import MessageBatchStep, ScriptStep, execute_combination
from src.scripting.loader import InteractionScript, ScriptExecutionError
from src.messaging.parser import parse_send_message

SCRIPT_NAME = "双排自动逃跑"
SCRIPT_DESCRIPTION = "前置匹配 → 判断匹配 ID → 取消匹配，或等待战斗就绪后逃跑"
WAIT_TIMEOUT_SECONDS = battle_control.ENTRY_TIMEOUT_SECONDS

# SA231229_CmdMatch.ids 中包含任意一个指定 ID 时，等待入场后逃跑。
SPECIFIED_PLAYER_IDS: set[int] = {75238435}

# 前置消息沿用 auto_escape.py，可在这里修改匹配参数。
BEFORE_STEPS: tuple[MessageBatchStep, ...] = (
    MessageBatchStep((
        SendMessage(id=42, cmd="AHM230616_1", param={"tid": 302}),
        SendMessage(id=42, cmd="SA231229_match", param={"mt": 0}),
    )),
)

# 客户端取消匹配请求；r=1、_cmd 属于服务器回包，不放入请求参数。
CANCEL_STEPS: tuple[MessageBatchStep, ...] = (
    MessageBatchStep((
        SendMessage(id=42, cmd="SA231229_cancle", param={}),
    )),
)

AFTER_STEPS: tuple[MessageBatchStep, ...] =(
    MessageBatchStep((
        SendMessage(id = 13, cmd = "1404", param={"turn":0}),
        SendMessage(id=-1, cmd="#wait", param={}),
    )),
)
async def run(context: AppContext) -> tuple[SendResult, ...]:
    if not BEFORE_STEPS or not CANCEL_STEPS or not AFTER_STEPS:
        raise ValueError("请先填写 double_auto_escape.py 的 BEFORE_STEPS、CANCEL_STEPS 和 AFTER_STEPS")
    if not SPECIFIED_PLAYER_IDS:
        raise ValueError("请先填写 SPECIFIED_PLAYER_IDS 中的目标 ID")
    if not all(
        isinstance(step, MessageBatchStep) for step in (*BEFORE_STEPS, *CANCEL_STEPS, *AFTER_STEPS)
    ):
        raise TypeError("前置、取消和后置步骤必须使用 MessageBatchStep")

    await context.wait_for_player_initialization()
    context.assert_automation_allowed()
    if not context.socket.connected or context.messages.disconnected.is_set():
        raise ConnectionError("账号连接已断开")
    await battle_control.require_idle(context)

    previous_epoch = context.battle.battle_epoch
    expected_epoch = previous_epoch + 1
    subscription = context.messages.subscribe(max_queue_size=500)

    async def wait_for_entry(wait_context: AppContext) -> None:
        wait_context.log("前置步骤已提交，等待本次战斗完整就绪")
        await battle_control.wait_for_entry(wait_context, subscription, previous_epoch)
        wait_context.log(f"本次战斗已就绪：battleId={wait_context.battle.battle_id}，turn={wait_context.battle.current_turn}")

    async def wait_for_reply(wait_context: AppContext, command: str) -> dict:
        if command == "SA231229_CmdMatch":
            return await battle_control.wait_for_match(wait_context, subscription, previous_epoch, allow_direct_entry=False)
        deadline = asyncio.get_running_loop().time() + WAIT_TIMEOUT_SECONDS
        while True:
            wait_context.assert_automation_allowed()
            if not wait_context.socket.connected or wait_context.messages.disconnected.is_set():
                raise ConnectionError(f"等待 {command} 期间连接断开")
            if wait_context.battle.phase == "unknown":
                await battle_control.abort_round(wait_context, wait_context.battle.last_error or "战斗状态未知")
            if wait_context.battle.battle_epoch != previous_epoch or wait_context.battle.phase != "idle":
                await battle_control.abort_round(wait_context, "取消匹配期间战斗状态已改变")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await battle_control.abort_round(wait_context, f"{WAIT_TIMEOUT_SECONDS:g} 秒内未收到 {command}")
            try:
                message = await subscription.wait_for(lambda m: True, timeout=min(remaining, 1))
            except TimeoutError:
                continue
            cmd = str(message.get("_cmd", ""))
            if cmd == "2303" and message.get("msg"):
                await battle_control.abort_round(wait_context, str(message["msg"]))
            if cmd == command:
                return message

    async def wait_for_cancel(wait_context: AppContext) -> None:
        reply = await wait_for_reply(wait_context, "SA231229_cancle")
        if reply.get("r") != 1:
            await battle_control.abort_round(wait_context, f"服务器未确认取消匹配成功：r={reply.get('r')!r}")
        if wait_context.battle.battle_epoch != previous_epoch or wait_context.battle.phase != "idle":
            await battle_control.abort_round(wait_context, "取消匹配期间战斗状态已改变")
        wait_context.log("服务器已确认取消匹配，本轮结束")

    async def process_match(wait_context: AppContext) -> tuple[SendResult, ...]:
        wait_context.log("前置步骤已提交，等待 SA231229_CmdMatch")
        reply = await wait_for_reply(wait_context, "SA231229_CmdMatch")
        ids = reply.get("ids")
        if not isinstance(ids, list) or not ids or any(type(player_id) is not int for player_id in ids):
            await battle_control.abort_round(wait_context, "匹配回包缺少有效的 ids，不能判断后续操作")
        wait_context.log(f"本次匹配 ID：{ids}")
        if SPECIFIED_PLAYER_IDS.intersection(ids):
            wait_context.log("匹配 ID 命中指定集合，等待本次战斗就绪后逃跑")
            prepare_steps = (
                MessageBatchStep((SendMessage(id=42, cmd="SA231229_dpmr", param={}),)),
            ) if wait_context.battle.battle_epoch == previous_epoch else ()
            branch = (
                *prepare_steps,
                ScriptStep(InteractionScript(
                    module_name=f"{__name__}.wait_for_entry",
                    name="等待本次战斗就绪",
                    description="等待 2303/2401/2426/2402",
                    run=wait_for_entry,
                )),
                *AFTER_STEPS,
            )
        else:
            if wait_context.battle.battle_epoch != previous_epoch or wait_context.battle.phase != "idle":
                await battle_control.abort_round(wait_context, "匹配未命中但已进入战斗，不能继续取消匹配")
            wait_context.log("匹配 ID 未命中指定集合，取消匹配并结束本轮")
            branch = (
                *CANCEL_STEPS,
                ScriptStep(InteractionScript(
                    module_name=f"{__name__}.wait_for_cancel",
                    name="等待取消匹配确认",
                    description="检查 SA231229_cancle.r=1",
                    run=wait_for_cancel,
                )),
            )
        result = await execute_combination(
            wait_context.user_id, wait_context, branch, preserve_current_battle=True
        )
        battle_control.propagate_script_skip(result)
        if not result.success:
            failure = next(step for step in result.step_results if not step.success)
            raise ScriptExecutionError(
                f"分支步骤 {failure.step}（{failure.description}）失败：{failure.error}",
                result.send_results,
            )
        return result.send_results

    try:
        steps = (
            *BEFORE_STEPS,
            ScriptStep(InteractionScript(
                module_name=f"{__name__}.process_match",
                name="判断匹配 ID 并选择分支",
                description="未命中则取消；命中则等入场后逃跑并等待结束",
                run=process_match,
            )),
        )
        result = await execute_combination(
            context.user_id, context, steps, preserve_current_battle=True
        )
        battle_control.propagate_script_skip(result)
        if not result.success:
            failure = next(step for step in result.step_results if not step.success)
            raise ScriptExecutionError(
                f"步骤 {failure.step}（{failure.description}）失败：{failure.error}",
                result.send_results,
            )
        return result.send_results
    finally:
        subscription.close()
