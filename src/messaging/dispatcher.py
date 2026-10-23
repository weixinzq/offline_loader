"""Preflighted, account-isolated dispatch for imported send messages."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from src.messaging.parser import SendMessage
from src.messaging.battle_control import (
    BattleRoundSkipped, BattleStateError, abort_round, note_sent,
    current_round, wait_for_match, wait_for_end, wait_for_entry, ENTRY_TIMEOUT_SECONDS,
)
from src.network.context import AppContext


DEFAULT_MESSAGE_DELAY = 0.2
_BATTLE_RESPONSE_TIMEOUT = ENTRY_TIMEOUT_SECONDS
_BATTLE_ENTRY_COMMAND = (15, "54_22")
_ROOM_JOIN_COMMAND = (1, "cmdCreateAndJoinRoom")
_PREFIGHT_COMMAND = (16, "preFightLoad")
_SESSION_OWNED_COMMANDS = frozenset({"55_1", "55_2", "55_9"})
_TURN_COMMANDS = frozenset(f"{cmd}" for cmd in range(1401, 1411))
_ENTRY_RESPONSES = frozenset({"2303", "2401", "2426", "2402"})
_BATTLE_SERVER_COMMANDS = (
    _ENTRY_RESPONSES
    | frozenset({"joinOK", "joinKO"})
)


@dataclass(frozen=True)
class SendResult:
    account_label: str
    sequence: int
    ext_id: int
    cmd: str
    timestamp: str
    success: bool
    error: str = ""


@dataclass(frozen=True)
class _MessageItem:
    index: int
    message: SendMessage


@dataclass(frozen=True)
class _RoomJoin:
    item: _MessageItem | None
    room_name: str


@dataclass(frozen=True)
class _PassThrough:
    item: _MessageItem


@dataclass(frozen=True)
class _LocalInstruction:
    item: _MessageItem


@dataclass(frozen=True)
class _BattleEntry:
    item: _MessageItem


@dataclass(frozen=True)
class _BattleMessage:
    item: _MessageItem


@dataclass(frozen=True)
class _DispatchPlan:
    steps: tuple[object, ...]
    has_battle: bool
    has_entry: bool


@dataclass
class _BattleState:
    room_id: int
    battle_id: int | None = None
    current_turn: int | None = None


def validate_messages(messages: Sequence[SendMessage]) -> None:
    """Preflight a complete sequence without opening connections or sending."""
    segment = []
    for message in messages:
        if message.id == -1 and message.cmd == "#wait":
            if segment:
                _build_dispatch_plan(segment)
            segment = []
        else:
            segment.append(message)
    if segment:
        _build_dispatch_plan(segment)


async def end_previous_battle(context: AppContext, delay: float = 0) -> None:
    """Public script operation for finishing a previous battle safely."""
    battle = context.battle
    if battle.phase == "active" and not battle.end_request_sent:
        _assert_automation_allowed(context)
        if not context.socket.connected:
            raise ConnectionError("账号连接已断开")
        try:
            params = _escape_params(context, {"turn": 0})
        except BattleStateError as exc:
            await abort_round(context, str(exc))
        context.auto_battle.disable()
        battle.mark_end_requested()
        try:
            await context.socket.send_xt_message(13, "1404", params)
        except BaseException:
            battle.end_request_sent = False
            raise
        _context_log(context, "已提交上一场战斗结束 1404")
        if delay > 0:
            await asyncio.sleep(delay)
        if battle.last_error:
            raise RuntimeError(battle.last_error)
        battle.finish_without_confirmation()
    elif battle.phase in {"entering", "unknown"}:
        await abort_round(context, battle.last_error or f"当前战斗状态为 {battle.phase}")


def _build_dispatch_plan(messages: Sequence[SendMessage]) -> _DispatchPlan:
    indexed = tuple(_MessageItem(index, message) for index, message in enumerate(messages))
    session_items = [
        item for item in indexed if item.message.cmd in _SESSION_OWNED_COMMANDS
    ]
    if session_items:
        commands = "、".join(
            sorted({item.message.cmd for item in session_items})
        )
        raise ValueError(
            f"{commands} 由账号会话服务管理，禁止从抓包文件重放"
        )
    entries = [item for item in indexed if (item.message.id, item.message.cmd) == _BATTLE_ENTRY_COMMAND]
    if len(entries) > 1:
        raise ValueError("一份消息只能包含一个 54_22 战斗入口")

    for item in indexed:
        message = item.message
        if message.id == -1:
            if message.cmd not in {"#wait", "#time"}:
                raise ValueError("未知本地序列指令")
            if message.cmd == "#time":
                seconds = message.param.get("seconds")
                if (
                    isinstance(seconds, bool)
                    or not isinstance(seconds, (int, float))
                    or not math.isfinite(seconds)
                    or seconds < 0
                ):
                    raise ValueError("#time 必须指定大于或等于 0 的有限秒数")
        if message.cmd in _TURN_COMMANDS and message.id != 13:
            raise ValueError(f"战斗命令 {message.cmd} 必须使用 id=13")
        if message.cmd == "54_22" and message.id != 15:
            raise ValueError("战斗入口 54_22 必须使用 id=15")
        if message.cmd == "preFightLoad" and message.id != 16:
            raise ValueError("preFightLoad 必须使用 id=16")
        if message.cmd == "cmdCreateAndJoinRoom" and message.id != 1:
            raise ValueError("cmdCreateAndJoinRoom 必须使用 id=1")

    entry_index = entries[0].index if entries else None
    actions = [
        item
        for item in indexed
        if item.message.id == 13 and item.message.cmd in _TURN_COMMANDS
    ]
    if entry_index is not None and any(item.index < entry_index for item in actions):
        raise ValueError("回合消息不能出现在 54_22 战斗入口之前")

    room_items = [
        item for item in indexed if (item.message.id, item.message.cmd) == _ROOM_JOIN_COMMAND
    ]
    if len(room_items) > 1:
        raise ValueError("一份消息只能包含一个 cmdCreateAndJoinRoom")
    explicit_room = room_items[0] if room_items else None
    if explicit_room is not None:
        room_name = explicit_room.message.param.get("room")
        if not isinstance(room_name, str) or not room_name.strip():
            raise ValueError("cmdCreateAndJoinRoom 的 param.room 必须是非空字符串")
        if entry_index is not None and explicit_room.index > entry_index:
            raise ValueError("cmdCreateAndJoinRoom 必须位于 54_22 之前")

    for item in indexed:
        message = item.message
        if (message.id, message.cmd) == _PREFIGHT_COMMAND:
            if entry_index is not None and item.index < entry_index:
                raise ValueError("preFightLoad 必须位于 54_22 之后")

    steps: list[object] = []
    for item in indexed:
        message = item.message
        key = (message.id, message.cmd)
        if message.id == -1:
            steps.append(_LocalInstruction(item))
        elif key == _ROOM_JOIN_COMMAND:
            steps.append(_RoomJoin(item, str(message.param["room"]).strip()))
        elif key == _BATTLE_ENTRY_COMMAND:
            steps.append(_BattleEntry(item))
        elif key == _PREFIGHT_COMMAND or (message.id == 13 and message.cmd in _TURN_COMMANDS):
            steps.append(_BattleMessage(item))
        else:
            steps.append(_PassThrough(item))
    has_battle = any(isinstance(step, (_BattleEntry, _BattleMessage)) for step in steps)
    return _DispatchPlan(tuple(steps), has_battle, entry_index is not None)


def _make_result(
    account_label: str,
    sequence_start: int,
    item: _MessageItem,
    success: bool,
    error: str = "",
) -> SendResult:
    return SendResult(
        account_label,
        sequence_start + item.index,
        item.message.id,
        item.message.cmd,
        datetime.now().astimezone().isoformat(timespec="seconds"),
        success,
        error,
    )


def _context_log(context: AppContext, text: str) -> None:
    callback = getattr(context, "log", None)
    if callable(callback):
        callback(text)


def _assert_automation_allowed(context: AppContext) -> None:
    callback = getattr(context, "assert_automation_allowed", None)
    if callable(callback):
        callback()


async def _execute_instruction(context: AppContext, message: SendMessage) -> None:
    if message.cmd == "#time":
        seconds = message.param["seconds"]
        _context_log(context, f"暂停 {seconds:g} 秒")
        await asyncio.sleep(seconds)
        _assert_automation_allowed(context)
        return

    await wait_for_end(context)



def _escape_params(context: AppContext, params: dict) -> dict:
    own = next(iter(context.auto_battle.own_players), {})
    slot = own.get("battleView", {}).get("slotId")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        raise BattleStateError("尚未确认己方战场位置，不能发送逃跑消息")
    return {**params, "reqPSId": slot}


async def _end_previous_battle(context: AppContext, delay: float) -> None:
    battle = getattr(context, "battle", None)
    if battle is not None and battle.phase != "idle":
        await abort_round(context, battle.last_error or f"上一场战斗状态为 {battle.phase}")


class _BattleExecutor:
    def __init__(
        self,
        account_label: str,
        context: AppContext,
        plan: _DispatchPlan,
        delay: float,
        sequence_start: int,
    ) -> None:
        self.account_label = account_label
        self.context = context
        self.plan = plan
        self.delay = delay
        self.sequence_start = sequence_start
        self.results: dict[int, SendResult] = {}
        self.submitted = 0
        self.subscription = None
        self.state: _BattleState | None = None
        self.entry_item: _MessageItem | None = None
        self.entry_ready = False
        self.entry_started = False
        self.external_epoch: int | None = None
        self.failure_item: _MessageItem | None = None
        self.previous_epoch = context.battle.battle_epoch

    def _assert_live_context(self) -> None:
        if not self.context.socket.connected:
            raise ConnectionError("账号连接已断开")
        if self.context.battle.phase == "unknown":
            raise BattleStateError(
                self.context.battle.last_error or "战斗状态未知，必须重新连接"
            )
        if self.external_epoch is not None and (
            self.context.battle.phase != "active"
            or self.context.battle.battle_epoch != self.external_epoch
        ):
            raise BattleStateError("当前战斗已结束或战斗身份已改变")
        if self.state is None:
            return
        room_id = getattr(self.context.socket, "active_room_id", -1)
        if room_id != self.state.room_id:
            raise BattleStateError(
                f"战斗期间房间已改变：expected={self.state.room_id}, actual={room_id}"
            )

    async def _send(self, item: _MessageItem) -> None:
        self.failure_item = item
        self._assert_live_context()
        params = dict(item.message.param)
        if item.message.cmd == "1404":
            params = _escape_params(self.context, params)
            self.context.battle.mark_end_requested()
        try:
            await self.context.socket.send_xt_message(item.message.id, item.message.cmd, params)
            note_sent(self.context, item.message.cmd)
        except Exception:
            if item.message.cmd == "1404":
                self.context.battle.end_request_sent = False
            raise
        if item.message.cmd == "1404":
            auto_battle = getattr(self.context, "auto_battle", None)
            if auto_battle is not None:
                auto_battle.disable()
            if self.context.battle.last_error:
                raise RuntimeError(self.context.battle.last_error)
        elif item.message.cmd == "1409":
            auto_battle = getattr(self.context, "auto_battle", None)
            if auto_battle is not None:
                if params.get("useAiType") == 1:
                    auto_battle.enable()
                elif params.get("useAiType") == 0:
                    auto_battle.disable()
        self.submitted += 1
        self.results[item.index] = _make_result(
            self.account_label, self.sequence_start, item, True
        )

    async def _pause(self) -> None:
        if self.delay > 0:
            await asyncio.sleep(self.delay)

    async def _next_battle_message(self, deadline: float) -> dict:
        if self.subscription is None:
            raise BattleStateError("战斗订阅尚未建立")
        self._assert_live_context()
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("等待战斗服务器状态超时")
        try:
            message = await self.subscription.wait_for(
                lambda candidate: str(candidate.get("_cmd", ""))
                in _BATTLE_SERVER_COMMANDS,
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError("等待战斗服务器状态超时") from exc

        cmd = str(message.get("_cmd", ""))
        if cmd == "2303" and message.get("msg"):
            raise BattleStateError(f"服务器返回 {cmd}：{message['msg']}")
        if cmd in {"joinOK", "joinKO"}:
            raise BattleStateError(f"战斗期间收到房间切换结果 {cmd}")
        if cmd == "2303":
            battle_id = message.get("battleId")
            if isinstance(battle_id, bool) or not isinstance(battle_id, int) or battle_id == 0:
                raise BattleStateError(f"2303 未返回有效 battleId：{battle_id!r}")
            if self.state is not None:
                if self.state.battle_id is None:
                    self.state.battle_id = battle_id
                elif self.state.battle_id != battle_id:
                    raise BattleStateError(
                        "战斗身份已改变："
                        f"expected={self.state.battle_id}, actual={battle_id}"
                    )
        return message

    async def _ensure_entry_ready(self) -> None:
        if self.entry_ready:
            return
        if self.entry_item is None or self.state is None:
            raise BattleStateError("尚未发送 54_22 战斗入口")
        self.failure_item = self.entry_item
        _context_log(self.context, "等待战斗入口 2303/2401/2426/2402")
        await wait_for_entry(self.context, self.subscription, self.previous_epoch)
        self.state.battle_id = self.context.battle.battle_id
        self.state.current_turn = self.context.battle.current_turn
        self.entry_ready = True
        self.subscription.close()
        self.subscription = None
        self.results[self.entry_item.index] = _make_result(
            self.account_label, self.sequence_start, self.entry_item, True)
        _context_log(self.context, f"战斗已就绪：battleId={self.state.battle_id}, turn={self.state.current_turn}")

    async def _ensure_external_entry_ready(self, item: _MessageItem) -> None:
        self.failure_item = item
        battle = self.context.battle
        scope = current_round(self.context)
        if battle.phase == "idle":
            if scope is not None and scope.entry_epoch == battle.battle_epoch:
                await abort_round(self.context, "本次战斗在发送动作前已经结束")
            if scope is not None and scope.match_deadline is not None:
                await wait_for_match(self.context, self.subscription, battle.battle_epoch)
        if not battle.entry_ready:
            previous = battle.battle_epoch - (battle.phase == "active")
            await wait_for_entry(self.context, self.subscription, previous)
        self.external_epoch = battle.battle_epoch
        self.entry_ready = True
        self.subscription.close()
        self.subscription = None

    async def run(self) -> list[SendResult]:
        try:
            await self.context.wait_for_player_initialization()
            _assert_automation_allowed(self.context)
            _context_log(self.context, "玩家资料已就绪；等待房间")
            if not self.plan.has_entry:
                self.subscription = self.context.messages.subscribe(max_queue_size=500)
            for step_index, step in enumerate(self.plan.steps):
                if isinstance(step, _LocalInstruction):
                    self.failure_item = step.item
                    self._assert_live_context()
                    await _execute_instruction(self.context, step.item.message)
                    self.results[step.item.index] = _make_result(
                        self.account_label, self.sequence_start, step.item, True
                    )
                    if step_index + 1 < len(self.plan.steps):
                        await self._pause()
                    continue
                if isinstance(step, _RoomJoin):
                    if step.item is not None:
                        self.failure_item = step.item
                    await self.context.enter_room(
                        step.room_name, timeout=_BATTLE_RESPONSE_TIMEOUT
                    )
                    room_id = getattr(self.context.socket, "active_room_id", -1)
                    if not isinstance(room_id, int) or room_id <= 0:
                        raise RuntimeError(f"房间 {step.room_name} 未建立动态 roomId")
                    if step.item is not None:
                        self.results[step.item.index] = _make_result(
                            self.account_label, self.sequence_start, step.item, True
                        )
                    _context_log(
                        self.context,
                        f"已进入动态房间 {step.room_name}（roomId={room_id}）",
                    )
                    if step_index + 1 < len(self.plan.steps):
                        await self._pause()
                    continue

                if isinstance(step, _BattleEntry):
                    room_id = getattr(self.context.socket, "active_room_id", -1)
                    if (
                        isinstance(room_id, bool)
                        or not isinstance(room_id, int)
                        or (room_id != -1 and room_id <= 0)
                    ):
                        self.failure_item = step.item
                        raise RuntimeError(f"无效的动态房间 ID：{room_id!r}")
                    self.subscription = self.context.messages.subscribe(max_queue_size=500)
                    self.state = _BattleState(room_id)
                    self.entry_item = step.item
                    self.context.battle.begin_entry(room_id)
                    self.entry_started = True
                    await self._send(step.item)
                    _context_log(self.context, "已提交 54_22；等待入口辅助消息")
                    if step_index + 1 < len(self.plan.steps):
                        await self._pause()
                    continue

                if isinstance(step, _PassThrough):
                    await self._send(step.item)
                    if step_index + 1 < len(self.plan.steps):
                        await self._pause()
                    continue

                if self.entry_item is None:
                    if not self.entry_ready:
                        await self._ensure_external_entry_ready(step.item)
                else:
                    await self._ensure_entry_ready()
                await self._send(step.item)
                if step_index + 1 < len(self.plan.steps):
                    await self._pause()

            if self.entry_item is not None and not self.entry_ready:
                await self._ensure_entry_ready()
        except BattleRoundSkipped as exc:
            exc.prepend(self.results[index] for index in sorted(self.results))
            raise
        except (BattleStateError, TimeoutError) as error:
            try:
                await abort_round(self.context, str(error))
            except BattleRoundSkipped as exc:
                exc.prepend(self.results[index] for index in sorted(self.results))
                raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if (
                self.entry_started
                and self.context.battle.phase not in {"idle", "unknown"}
            ):
                self.context.battle.mark_unknown(str(exc))
            item = self.failure_item
            if item is None:
                item = next(
                    (
                        step.item
                        for step in self.plan.steps
                        if isinstance(step, (_PassThrough, _BattleEntry, _BattleMessage))
                    ),
                    None,
                )
            if item is not None:
                error = f"{exc}（已提交 {self.submitted} 条，不重试）"
                self.results[item.index] = _make_result(
                    self.account_label,
                    self.sequence_start,
                    item,
                    False,
                    error,
                )
                _context_log(self.context, f"已停止：{error}")
        finally:
            if self.subscription is not None:
                self.subscription.close()
        return [self.results[index] for index in sorted(self.results)]


async def _execute_simple_plan(
    account_label: str,
    context: AppContext,
    plan: _DispatchPlan,
    delay: float,
    stop_on_error: bool,
    sequence_start: int,
) -> list[SendResult]:
    results: list[SendResult] = []
    for step_index, step in enumerate(plan.steps):
        item = step.item
        try:
            _assert_automation_allowed(context)
            if not context.socket.connected:
                raise ConnectionError("账号连接已断开")
            if isinstance(step, _RoomJoin):
                await context.enter_room(step.room_name, timeout=_BATTLE_RESPONSE_TIMEOUT)
            elif isinstance(step, _LocalInstruction):
                await _execute_instruction(context, item.message)
            else:
                await context.socket.send_xt_message(
                    item.message.id, item.message.cmd, dict(item.message.param)
                )
                note_sent(context, item.message.cmd)
        except BattleRoundSkipped as exc:
            exc.prepend(results)
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            results.append(
                _make_result(account_label, sequence_start, item, False, str(exc))
            )
            if stop_on_error:
                break
        else:
            results.append(_make_result(account_label, sequence_start, item, True))
        if step_index + 1 < len(plan.steps) and delay > 0:
            await asyncio.sleep(delay)
    return results


async def _execute_plan(
    account_label: str,
    context: AppContext,
    plan: _DispatchPlan,
    delay: float,
    stop_on_error: bool,
    sequence_start: int,
    preserve_current_battle: bool,
) -> list[SendResult]:
    if plan.steps and (
        plan.has_entry or (not plan.has_battle and not preserve_current_battle)
    ):
        try:
            await _end_previous_battle(context, delay)
        except BattleRoundSkipped:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            item = plan.steps[0].item
            return [_make_result(account_label, sequence_start, item, False, str(exc))]

    if not plan.has_battle:
        return await _execute_simple_plan(
            account_label,
            context,
            plan,
            delay,
            stop_on_error,
            sequence_start,
        )
    executor = _BattleExecutor(account_label, context, plan, delay, sequence_start)
    return await executor.run()


async def execute_messages(
    account_label: str,
    context: AppContext,
    messages: Sequence[SendMessage],
    delay: float = DEFAULT_MESSAGE_DELAY,
    stop_on_error: bool = True,
    sequence_start: int = 1,
    *,
    preserve_current_battle: bool = False,
) -> list[SendResult]:
    """Preflight the full sequence; #wait separates independent battle plans."""
    operations: list[tuple[int, _DispatchPlan | _LocalInstruction]] = []
    start = 0
    try:
        for index, message in enumerate(messages):
            if message.id == -1 and message.cmd == "#wait":
                if index > start:
                    operations.append(
                        (start, _build_dispatch_plan(messages[start:index]))
                    )
                operations.append((index, _LocalInstruction(_MessageItem(0, message))))
                start = index + 1
        if start < len(messages):
            operations.append((start, _build_dispatch_plan(messages[start:])))
    except ValueError as exc:
        item = _MessageItem(0, messages[0])
        return [_make_result(account_label, sequence_start, item, False, str(exc))]

    preserve_current_battle = preserve_current_battle or any(
        message.id == -1 for message in messages
    )
    results: list[SendResult] = []
    try:
        for operation_index, (start, operation) in enumerate(operations):
            if isinstance(operation, _LocalInstruction):
                try:
                    await _execute_instruction(context, operation.item.message)
                except BattleRoundSkipped:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    results.append(
                        _make_result(
                            account_label, sequence_start + start,
                            operation.item, False, str(exc),
                        )
                    )
                    break
                results.append(
                    _make_result(account_label, sequence_start + start, operation.item, True)
                )
            else:
                batch = await _execute_plan(
                    account_label, context, operation, delay, stop_on_error,
                    sequence_start + start, preserve_current_battle,
                )
                results.extend(batch)
                if stop_on_error and any(not result.success for result in batch):
                    break
            if operation_index + 1 < len(operations) and delay > 0:
                await asyncio.sleep(delay)
    except BattleRoundSkipped as exc:
        exc.prepend(results)
        raise
    return results


async def dispatch_to_accounts(
    targets: Sequence[tuple[str, AppContext]],
    messages: Sequence[SendMessage],
    delay: float = DEFAULT_MESSAGE_DELAY,
) -> dict[str, list[SendResult]]:
    """Dispatch accounts concurrently while preserving order within each one."""
    batches = await asyncio.gather(
        *(
            execute_messages(label, context, messages, delay=delay)
            for label, context in targets
        )
    )
    return {
        label: results
        for (label, _context), results in zip(targets, batches, strict=True)
    }
