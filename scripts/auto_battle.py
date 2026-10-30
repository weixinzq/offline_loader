"""Run message steps, wait for a new battle, then run the remaining steps."""
from __future__ import annotations


from src.messaging.parser import SendMessage
from src.messaging.dispatcher import SendResult
from src.game.battle import control as battle_control
from src.network.context import AppContext
from src.scripting.composer import MessageBatchStep, ScriptStep, execute_combination
from src.scripting.loader import InteractionScript, ScriptExecutionError
from src.messaging.parser import parse_send_message

SCRIPT_NAME = "多排自动匹配战斗"
SCRIPT_DESCRIPTION = "前置消息步骤 → 等待新战斗就绪 → 后置消息步骤"

# 在这里填写消息。每个 MessageBatchStep 是一个步骤，内部消息按顺序执行。
# 前后均可添加多个步骤；参数未填写时，脚本会报错，不发送消息。
# 填写形式（将 ... 换成真实内容，再取消注释）：
# MessageBatchStep((
#     SendMessage(id=..., cmd="...", param={}),
#     SendMessage(id=..., cmd="...", param={}),
# )),
#send={"id":42,"param":{"mt":1},"cmd":"SA231229_match"}|
#send={"id":42,"param":{"tid":302},"cmd":"AHM230616_1"}|

#send={"id":13,"param":{"turn":0},"cmd":"1404"}|


#小匹配

#send={"id":42,"param":{"tid":302},"cmd":"AHM230616_1"}|
#send={"id":42,"param":{"mt":3},"cmd":"SA231229_match"}|


#auto
#send={"id":13,"param":{"useAiType":1},"cmd":"1409"}|
before = MessageBatchStep((

    SendMessage(id = 42, cmd = "AHM230616_1", param={"tid":302}),
    SendMessage(id = 42, cmd = "SA231229_match", param={"mt":1})
)
)

after = MessageBatchStep((
    SendMessage(id = 13, cmd = "1409", param={"useAiType":1}),
    SendMessage(id=-1, cmd="#wait", param={}),
)
)
BEFORE_STEPS: tuple[MessageBatchStep, ...] = (before,)
AFTER_STEPS: tuple[MessageBatchStep, ...] = (after,)


async def run(context: AppContext) -> tuple[SendResult, ...]:
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
    await battle_control.require_idle(context)

    previous_epoch = context.battle.battle_epoch
    # 在前置消息发送之前订阅，保留即时回包；不另开底层接收循环。
    subscription = context.messages.subscribe(max_queue_size=500)

    async def wait_for_entry(wait_context: AppContext) -> None:
        wait_context.log("前置步骤已提交，等待本次战斗完整就绪")
        await battle_control.wait_for_entry(wait_context, subscription, previous_epoch)
        wait_context.log(f"本次战斗已就绪：battleId={wait_context.battle.battle_id}，turn={wait_context.battle.current_turn}")
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
        battle_control.propagate_script_skip(result)
        if not result.success:
            failure = next(step for step in result.step_results if not step.success)
            raise ScriptExecutionError(
                f"步骤 {failure.step}（{failure.description}）失败：{failure.error}",
                result.send_results,
            )
        context.log("战斗就绪后的消息步骤已全部提交")
        return result.send_results
    finally:
        subscription.close()
