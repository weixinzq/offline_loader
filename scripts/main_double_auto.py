"""Cancel unmatched searches or enable automatic battle after entry."""
from __future__ import annotations

import asyncio

from src.messaging.dispatcher import SendResult
from src.messaging import battle_control
from src.messaging.parser import SendMessage
from src.network.context import AppContext
from src.scripting.composer import MessageBatchStep, ScriptStep, execute_combination
from src.scripting.loader import InteractionScript, ScriptExecutionError
from src.messaging.parser import parse_send_message

SCRIPT_NAME = "双排吃送分"
SCRIPT_DESCRIPTION = "前置匹配 → 判断匹配 ID → 取消匹配，或进入战斗"
WAIT_TIMEOUT_SECONDS = battle_control.ENTRY_TIMEOUT_SECONDS

# SA231229_CmdMatch.ids 中包含任意一个指定 ID 时，等待入场，否则取消匹配
SPECIFIED_PLAYER_IDS: set[int] = {
    75238436, 75238437, 75238479, 75238438, 75238480, 75238481, 75238482,
    75238532, 75238483, 15223857, 75238533, 75238439, 75238534, 75238485, 75238486,
    75238535, 75238487, 75238536, 75238440, 75238488, 75238489, 75238490, 75238537,
    75238491, 75238492, 75238538, 75238539, 75238540, 75238441, 75238442, 75238541,
    75238542, 75238543, 75238493, 75238494, 75238443, 75238544, 75238545, 75238444,
    75238552, 75238553, 75238554, 75238504, 75238495, 75238446, 75238447, 75238555,
    75238556, 75238557, 75238448, 75238451, 75238547, 75238452, 75238548, 75238457,
    75238505, 75238558, 75238454, 75238500, 75238501, 75238455, 15223893, 75238506,
    75238507, 75238550, 75238551, 75238458, 75238508, 75238560, 75238459, 75238561,
    75238509, 75238562, 75238460, 75238563, 75238564, 75238461, 75238565, 75238566,
    75238510, 75238567, 75238511, 75238568, 75238512, 75238462, 75238463, 75238569,
    75238464, 75238465, 75238570, 75238571, 15223858, 75238631, 75238632, 75238468,
    75238513, 75238514, 75238577, 75238620, 75238573, 75238515, 75238621, 75238516,
    75238622, 75238578, 75238517, 75238579, 75238575, 75238669, 75238623, 75238670,
    75238633, 75238624, 75238625, 75238626, 75238671, 75238627, 75238672, 75238634,
    75238673, 75238674, 75238629, 75238630, 75238635, 15223894, 75238676, 75238677,
    75238678, 75238679, 75238637, 75238580, 75238680, 75238638, 75238581, 75238639,
    75238681, 75238682, 75238683, 75238684, 75238685, 75238640, 75238641, 75238642,
    75238582, 75238686, 75238583, 75238643, 75238687, 75238688, 75238584, 75238585,
    75238689, 75238586, 75238587, 75238644, 75238645, 75238690, 75238588, 75238691,
    75238646, 75238692, 75238693, 75238694, 75238695, 75238647, 75238589, 75238648,
    75238649, 75238590, 75238696, 75238591, 75238592, 75238650, 75238593, 75238594,
    75238651, 15223938, 75238697, 75238596, 75238698, 75238597, 75238699, 75238700,
    75238598, 75238652, 75238599, 75238653, 75238701, 75238654, 75238600, 75238702,
    75238601, 75238703, 75238655, 75238704, 75238705, 75238706, 75238656, 75238657,
    75238707, 75238708, 75238602, 75238709, 75238710, 75238711, 75238603, 75238604,
    75238658, 75238605, 75238712, 75238713, 75238606, 75238714, 15223895, 75238659,
    75238717, 75238660, 75238608, 75238718, 75238609, 75238610, 75238611, 75238719,
    75238661, 75238662, 75238612, 75238613, 75238614, 75238615, 75238663, 75238616,
    75238664, 75238720, 75238617, 75238618, 75238665, 75238721, 75238666, 75238722,
    75238769, 75238770, 75238667, 75238771, 75238668, 75238723, 75238724, 75238725,
    75238819, 75238820, 75238726, 75238727, 75238728, 75238729, 75238730, 75238772,
    75238773, 75238731, 15223939, 75238733, 75238734, 75238735, 75238736, 75238774,
    75238775, 75238821, 75238776, 75238822, 75238777, 75238778, 75238737, 75238779,
    75238780, 75238738, 75238739, 75238740, 75238741, 75238781, 75238742, 75238823,
    75238782, 75238743, 75238744, 75238824, 75238745, 75238783, 75238746, 75238747,
    75238825, 75238826, 75238784, 75238748, 75238749, 75238750, 75238751, 75238827,
    75238828, 75238752, 75238785, 75238786,
}

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
        SendMessage(id = 13, cmd = "1409", param={"useAiType":1}),
        SendMessage(id=-1, cmd="#wait", param={}),
    )),
)
async def run(context: AppContext) -> tuple[SendResult, ...]:
    if not BEFORE_STEPS or not CANCEL_STEPS or not AFTER_STEPS:
        raise ValueError("请先填写 main_double_auto.py 的 BEFORE_STEPS、CANCEL_STEPS 和 AFTER_STEPS")
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

    async def wait_for_reply(wait_context: AppContext, command: str) -> dict | None:
        if command == "SA231229_CmdMatch":
            return await battle_control.wait_for_match(wait_context, subscription, previous_epoch)
        deadline = asyncio.get_running_loop().time() + WAIT_TIMEOUT_SECONDS
        while True:
            wait_context.assert_automation_allowed()
            if not wait_context.socket.connected or wait_context.messages.disconnected.is_set():
                raise ConnectionError(f"等待 {command} 期间连接断开")
            if wait_context.battle.phase == "unknown":
                await battle_control.abort_round(wait_context, wait_context.battle.last_error or "战斗状态未知")
            if wait_context.battle.battle_epoch != previous_epoch or wait_context.battle.phase != "idle":
                return None
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

    async def continue_battle(wait_context: AppContext) -> tuple[SendResult, ...]:
        prepare_steps = (
            MessageBatchStep((SendMessage(id=42, cmd="SA231229_dpmr", param={}),)),
        ) if wait_context.battle.battle_epoch == previous_epoch else ()
        steps = (
            *prepare_steps,
            ScriptStep(InteractionScript(
                module_name=f"{__name__}.wait_for_entry",
                name="等待本次战斗就绪",
                description="准备后等待 2303/2401/2426/2402",
                run=wait_for_entry,
            )),
            *AFTER_STEPS,
        )
        result = await execute_combination(
            wait_context.user_id, wait_context, steps, preserve_current_battle=True
        )
        battle_control.propagate_script_skip(result)
        if not result.success:
            failure = next(step for step in result.step_results if not step.success)
            raise ScriptExecutionError(f"自动战斗步骤失败：{failure.error}", result.send_results)
        return result.send_results

    async def wait_for_cancel(wait_context: AppContext) -> tuple[SendResult, ...] | None:
        reply = await wait_for_reply(wait_context, "SA231229_cancle")
        if reply is None or wait_context.battle.battle_epoch != previous_epoch or wait_context.battle.phase != "idle":
            wait_context.log("取消匹配期间已入场，转为自动战斗")
            return await continue_battle(wait_context)
        if reply.get("r") != 1:
            await battle_control.abort_round(wait_context, f"服务器未确认取消匹配成功：r={reply.get('r')!r}")
        wait_context.log("服务器已确认取消匹配，本轮结束")

    async def process_match(wait_context: AppContext) -> tuple[SendResult, ...]:
        wait_context.log("前置步骤已提交，等待 SA231229_CmdMatch")
        reply = await wait_for_reply(wait_context, "SA231229_CmdMatch")
        ids = reply.get("ids") if reply is not None else None
        entered = wait_context.battle.battle_epoch != previous_epoch or wait_context.battle.phase != "idle"
        if not entered and reply is not None and (not isinstance(ids, list) or not ids or any(type(player_id) is not int for player_id in ids)):
            await battle_control.abort_round(wait_context, "匹配回包缺少有效的 ids，不能判断后续操作")
        if ids is not None:
            wait_context.log(f"本次匹配 ID：{ids}")
        if entered or ids is None or SPECIFIED_PLAYER_IDS.intersection(ids):
            wait_context.log("匹配 ID 命中指定集合或已经入场，等待完整就绪后自动战斗")
            branch = (
                ScriptStep(InteractionScript(
                    module_name=f"{__name__}.continue_battle",
                    name="等待就绪并自动战斗",
                    description="等待 2303/2401/2426/2402 后自动战斗并等待结束",
                    run=continue_battle,
                )),
            )
        else:
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
                description="未命中则取消；命中或已经入场则自动战斗并等待结束",
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
