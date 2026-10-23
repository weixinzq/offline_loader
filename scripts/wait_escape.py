"""Check the opponent's initial backpack before optional steps and escape."""
from __future__ import annotations

from xml.etree import ElementTree

from src.messaging.dispatcher import SendResult
from src.messaging import battle_control
from src.messaging.parser import SendMessage
from src.network.context import AppContext
from src.scripting.composer import MessageBatchStep, ScriptStep, execute_combination
from src.scripting.loader import InteractionScript, ScriptExecutionError

SCRIPT_NAME = "魂器判断后逃跑"
SCRIPT_DESCRIPTION = "前置消息 → 等待入场背包 → 判断亚比及魂器 → 可选步骤 → 逃跑"

TARGET_PET_IDS = {5999, 6000}
# 58：其后的抉择；27：传说的暗黑咒语；36：狂战士之魂。
TARGET_SOUL_CONFIG_IDS = {58, 27, 36}

BEFORE_STEPS: tuple[MessageBatchStep, ...] = (
    MessageBatchStep((
        SendMessage(id=42, cmd="AHM230616_1", param={"tid": 302}),
        SendMessage(id=42, cmd="SA231229_match", param={"mt": 1}),
    )),
)

OPTIONAL_STEPS: tuple[MessageBatchStep, ...] = (
    MessageBatchStep((
        SendMessage(id=-1, cmd="#time", param={"seconds": 10}),
    )),
)

AFTER_STEPS: tuple[MessageBatchStep, ...] = (
    MessageBatchStep((
        SendMessage(id=13, cmd="1404", param={"turn": 0}),
        SendMessage(id=-1, cmd="#wait", param={}),
    )),
)


def _first_soul_config_id(pcp: str) -> int | None:
    #读魂器
    slots = ElementTree.fromstring(pcp).get("tt", "")
    first_slot = slots.split("|", 1)[0]
    if not first_slot:
        return None
    return int(first_slot.split(";", 2)[1])


async def run(context: AppContext) -> tuple[SendResult, ...]:
    if not BEFORE_STEPS:
        raise ValueError("请先填写 wait_escape.py 的 BEFORE_STEPS")
    if not all(
        isinstance(step, MessageBatchStep)
        for step in (*BEFORE_STEPS, *OPTIONAL_STEPS, *AFTER_STEPS)
    ):
        raise TypeError("前置、可选和后置步骤必须使用 MessageBatchStep")

    await context.wait_for_player_initialization()
    context.assert_automation_allowed()
    if not context.socket.connected or context.messages.disconnected.is_set():
        raise ConnectionError("账号连接已断开")
    await battle_control.require_idle(context)

    previous_epoch = context.battle.battle_epoch
    opponent_rosters: dict[int, list[dict]] = {}

    def observe_backpack(message: dict) -> None:
        cmd = str(message.get("_cmd", ""))
        if cmd not in {"2303", "2304"} or message.get("msg"):
            return
        # 2304 的完整背包位于 b；2401 上场信息不能覆盖初始背包。
        players = message.get("b", []) if cmd == "2304" else message.get("pmmList", [])
        for player in players:
            view = player.get("battleView", {})
            player_id = view.get("pmmId")
            if isinstance(player_id, int) and str(player_id) != context.user_id:
                opponent_rosters[player_id] = [
                    pet.get("pmSView", {}).copy() for pet in view.get("pmList", [])
                ]

    # 发送前建立订阅和观察器，保存即时回包中的完整对方背包。
    subscription = context.messages.subscribe(max_queue_size=500)
    context.messages.add_observer(observe_backpack)

    async def wait_for_entry(wait_context: AppContext) -> None:
        wait_context.log("前置步骤已提交，等待本次战斗完整就绪及对方背包")
        await battle_control.wait_for_entry(wait_context, subscription, previous_epoch, ready=lambda: bool(opponent_rosters))
        wait_context.log(f"本次战斗已就绪：battleId={wait_context.battle.battle_id}，turn={wait_context.battle.current_turn}")
        subscription.close()
        wait_context.messages.remove_observer(observe_backpack)

    async def process_opponents(wait_context: AppContext) -> tuple[SendResult, ...]:
        matched = False
        for player_id, pets in opponent_rosters.items():
            for pet in pets:
                if pet.get("f") not in TARGET_PET_IDS:
                    continue
                soul_id = _first_soul_config_id(pet.get("pcp", "<p />"))
                wait_context.log(
                    f"对方 {player_id}：亚比 ID={pet['f']}，第一槽魂器配置 ID={soul_id}"
                )
                if soul_id in TARGET_SOUL_CONFIG_IDS:
                    matched = True
        if not matched:
            wait_context.log("未命中亚比及第一槽魂器条件，跳过 OPTIONAL_STEPS")
            return ()
        wait_context.log("命中亚比及第一槽魂器条件，触发一次 OPTIONAL_STEPS")
        if not OPTIONAL_STEPS:
            wait_context.log("OPTIONAL_STEPS 尚未填写，继续固定后置步骤")
            return ()
        result = await execute_combination(
            wait_context.user_id, wait_context, OPTIONAL_STEPS, preserve_current_battle=True
        )
        battle_control.propagate_script_skip(result)
        if not result.success:
            failure = next(step for step in result.step_results if not step.success)
            raise ScriptExecutionError(
                f"可选步骤 {failure.step}（{failure.description}）失败：{failure.error}",
                result.send_results,
            )
        return result.send_results

    try:
        steps = (
            *BEFORE_STEPS,
            ScriptStep(InteractionScript(
                module_name=f"{__name__}.wait_for_entry",
                name="等待本次战斗及对方背包",
                description="等待战斗完整就绪及 2303/2304 初始背包",
                run=wait_for_entry,
            )),
            ScriptStep(InteractionScript(
                module_name=f"{__name__}.process_opponents",
                name="判断对方亚比及第一槽魂器",
                description="命中亚比 ID 及魂器配置 ID 时执行一次可选步骤",
                run=process_opponents,
            )),
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
        return result.send_results
    finally:
        subscription.close()
        context.messages.remove_observer(observe_backpack)
