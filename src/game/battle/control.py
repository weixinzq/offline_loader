"""Account-local battle deadlines and the signal that abandons one outer round."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass

MATCH_TIMEOUT_SECONDS = 5.0
ENTRY_TIMEOUT_SECONDS = 15.0
WAIT_TIMEOUT_SECONDS = 15.0


class BattleStateError(RuntimeError):
    """The transport is alive, but the current battle cannot continue."""


class BattleRoundSkipped(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.send_results = ()

    def prepend(self, results):
        self.send_results = (*results, *self.send_results)


@dataclass
class RoundScope:
    battle: object
    repetition: int
    recovering: bool = False
    match_deadline: float | None = None
    entry_epoch: int | None = None


_round: ContextVar[RoundScope | None] = ContextVar("battle_round", default=None)


def current_round(context):
    scope = _round.get()
    return scope if scope is not None and scope.battle is getattr(context, "battle", context) else None


def begin_round(context, repetition):
    return _round.set(RoundScope(getattr(context, "battle", context), repetition))


def reset_round(token):
    _round.reset(token)


def propagate_script_skip(result):
    # A directly invoked script may own its inner combination. Preserve its signal.
    if result.skipped_repetitions:
        reason = next(step.error for step in result.step_results if step.skipped)
        error = BattleRoundSkipped(reason)
        error.send_results = result.send_results
        raise error


def note_sent(context, cmd):
    scope = current_round(context)
    if scope is not None and cmd == "SA231229_match":
        scope.match_deadline = asyncio.get_running_loop().time() + MATCH_TIMEOUT_SECONDS
        scope.entry_epoch = None
    elif scope is not None and cmd == "54_22":
        scope.entry_epoch = None


def assert_connected(context):
    context.assert_automation_allowed()
    if not context.socket.connected or context.messages.disconnected.is_set():
        raise ConnectionError("账号连接已断开")


async def abort_round(context, reason):
    assert_connected(context)
    scope = current_round(context)
    if scope is None or not scope.recovering:
        if scope is not None:
            scope.recovering = True
        auto = context.auto_battle
        slots = [p.get("battleView", {}).get("slotId") for p in auto.own_players]
        slots = [slot for slot in slots if isinstance(slot, int) and not isinstance(slot, bool) and slot >= 0]
        targets = slots[:1] or [0, 11]
        await auto.abort_pending()
        for slot in targets:
            assert_connected(context)
            await context.socket.send_xt_message(13, "1404", {"turn": 0, "reqPSId": slot})
            context.log(f"恢复结束：1404 已提交，reqPSId={slot}；不等待回执")
        context.battle.abandon()
        auto.clear_battle()
        number = scope.repetition if scope is not None else 1
        context.log(f"第 {number} 轮恢复跳过：{reason}；本轮后续步骤不再发送")
    raise BattleRoundSkipped(reason)


async def require_idle(context):
    assert_connected(context)
    if context.battle.phase != "idle":
        await abort_round(context, context.battle.last_error or f"上一场战斗状态为 {context.battle.phase}")


async def _check_entry(context, previous_epoch):
    assert_connected(context)
    battle = context.battle
    if battle.phase == "unknown":
        await abort_round(context, battle.last_error or "战斗状态未知")
    if battle.battle_epoch not in (previous_epoch, previous_epoch + 1):
        await abort_round(context, "等待期间战斗身份已改变")
    if battle.battle_epoch == previous_epoch + 1 and battle.phase == "idle":
        await abort_round(context, "本次战斗在就绪前已经结束")


async def _next(context, subscription, deadline):
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    try:
        message = await subscription.wait_for(lambda _message: True, timeout=min(remaining, 1.0))
    except TimeoutError:
        if asyncio.get_running_loop().time() >= deadline:
            raise
        return {}
    if str(message.get("_cmd", "")) == "2303" and message.get("msg"):
        await abort_round(context, str(message["msg"]))
    return message


async def wait_for_match(context, subscription, previous_epoch, *, allow_direct_entry=True):
    scope = current_round(context)
    deadline = (scope.match_deadline if scope is not None else None)
    deadline = deadline if deadline is not None else asyncio.get_running_loop().time() + MATCH_TIMEOUT_SECONDS
    try:
        while True:
            await _check_entry(context, previous_epoch)
            if allow_direct_entry and context.battle.battle_epoch == previous_epoch + 1:
                return None
            message = await _next(context, subscription, deadline)
            if str(message.get("_cmd", "")) == "SA231229_CmdMatch":
                return message
    except TimeoutError:
        await abort_round(context, f"{MATCH_TIMEOUT_SECONDS:g} 秒内未确认本次匹配成功")


async def wait_for_entry(context, subscription, previous_epoch, *, ready=None):
    deadline = asyncio.get_running_loop().time() + ENTRY_TIMEOUT_SECONDS
    try:
        while True:
            await _check_entry(context, previous_epoch)
            battle = context.battle
            if battle.battle_epoch == previous_epoch + 1 and battle.entry_ready and (ready is None or ready()):
                scope = current_round(context)
                if scope is not None:
                    scope.entry_epoch = battle.battle_epoch
                return
            await _next(context, subscription, deadline)
    except TimeoutError:
        await abort_round(context, f"{ENTRY_TIMEOUT_SECONDS:g} 秒内未确认本次战斗完整就绪")


async def wait_for_end(context):
    subscription = context.messages.subscribe(max_queue_size=500)
    battle = context.battle
    epoch = None if battle.phase == "entering" else battle.battle_epoch
    deadline = asyncio.get_running_loop().time() + WAIT_TIMEOUT_SECONDS
    try:
        context.log("等待当前战斗结束")
        while True:
            assert_connected(context)
            if battle.phase == "unknown" or battle.last_error:
                await abort_round(context, battle.last_error or "战斗状态未知")
            if epoch is None and battle.phase in ("active", "ending"):
                epoch = battle.battle_epoch
            if epoch is not None and battle.battle_epoch != epoch:
                await abort_round(context, "等待结束期间战斗身份已改变")
            if battle.phase == "idle":
                context.log("当前战斗已结束或不在战斗中，继续消息序列")
                return
            try:
                await _next(context, subscription, deadline)
            except TimeoutError:
                await abort_round(context, f"#wait 超过 {WAIT_TIMEOUT_SECONDS:g} 秒")
    finally:
        subscription.close()
