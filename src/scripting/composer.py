"""Ordered combinations of generic message batches and interaction scripts."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, replace
from typing import Sequence, TypeAlias

from src.messaging.dispatcher import (
    DEFAULT_MESSAGE_DELAY,
    SendResult,
    execute_messages,
)
from src.game.battle.control import BattleRoundSkipped, begin_round, current_round, reset_round
from src.messaging.parser import SendMessage
from src.network.context import AppContext
from src.scripting.loader import InteractionScript, ScriptExecutionError


@dataclass(frozen=True)
class MessageBatchStep:
    messages: tuple[SendMessage, ...]

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("通用消息批次不能为空")

    @property
    def description(self) -> str:
        commands = ", ".join(message.cmd for message in self.messages[:3])
        if len(self.messages) > 3:
            commands += ", …"
        return f"通用消息 {len(self.messages)} 条：{commands}"


@dataclass(frozen=True)
class ScriptStep:
    script: InteractionScript

    @property
    def description(self) -> str:
        return f"专用脚本：{self.script.name}"


CombinationStep: TypeAlias = MessageBatchStep | ScriptStep


@dataclass(frozen=True)
class CombinationStepResult:
    repetition: int
    step: int
    kind: str
    description: str
    success: bool
    error: str = ""
    skipped: bool = False


@dataclass(frozen=True)
class CombinationResult:
    requested_repetitions: int
    completed_repetitions: int
    step_results: tuple[CombinationStepResult, ...]
    send_results: tuple[SendResult, ...]
    skipped_repetitions: int = 0

    @property
    def attempted_repetitions(self) -> int:
        return self.completed_repetitions + self.skipped_repetitions

    @property
    def success(self) -> bool:
        return (
            self.attempted_repetitions == self.requested_repetitions
            and all(result.success or result.skipped for result in self.step_results)
        )


def _validate_options(
    steps: Sequence[CombinationStep],
    repetitions: int,
    repeat_interval: float,
) -> None:
    if not steps:
        raise ValueError("组合任务至少需要一个步骤")
    if isinstance(repetitions, bool) or not isinstance(repetitions, int):
        raise ValueError("发送次数必须是整数")
    if repetitions < 1:
        raise ValueError("发送次数必须至少为 1")
    if not math.isfinite(repeat_interval) or repeat_interval < 0:
        raise ValueError("每轮间隔必须是大于或等于 0 的有限秒数")


async def execute_combination(
    account_label: str,
    context: AppContext,
    steps: Sequence[CombinationStep],
    repetitions: int = 1,
    repeat_interval: float = 0.5,
    message_delay: float = DEFAULT_MESSAGE_DELAY,
    *,
    preserve_current_battle: bool = False,
) -> CombinationResult:
    """Execute ordered steps, then wait and repeat the complete combination."""
    steps = tuple(steps)
    _validate_options(steps, repetitions, repeat_interval)
    owner = current_round(context) is None
    step_results: list[CombinationStepResult] = []
    send_results: list[SendResult] = []
    completed_repetitions = skipped_repetitions = 0
    next_message_sequence = 1

    def result():
        return CombinationResult(repetitions, completed_repetitions, tuple(step_results),
                                 tuple(send_results), skipped_repetitions)

    for repetition in range(1, repetitions + 1):
        token = begin_round(context, repetition) if owner else None
        step_number = 1
        try:
            for step_number, step in enumerate(steps, start=1):
                kind = "message_batch" if isinstance(step, MessageBatchStep) else "script"
                error = ""
                sends = ()
                try:
                    assert_allowed = getattr(context, "assert_automation_allowed", None)
                    if callable(assert_allowed):
                        assert_allowed()
                    if isinstance(step, MessageBatchStep):
                        sends = await execute_messages(
                            account_label, context, step.messages, delay=message_delay,
                            sequence_start=next_message_sequence,
                            preserve_current_battle=preserve_current_battle)
                        if len(sends) != len(step.messages):
                            error = "通用消息批次未完整执行"
                    elif isinstance(step, ScriptStep):
                        sends = await step.script.run(context) or ()
                    else:
                        raise TypeError(f"不支持的组合步骤类型：{type(step).__name__}")
                except BattleRoundSkipped:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    if isinstance(exc, ScriptExecutionError):
                        sends = exc.send_results
                for item in sends:
                    send_results.append(replace(item, account_label=account_label,
                                                sequence=next_message_sequence))
                    next_message_sequence += 1
                failure = next((item for item in sends if not item.success), None)
                if failure is not None:
                    error = failure.error or "脚本消息发送失败"
                step_results.append(CombinationStepResult(
                    repetition, step_number, kind, step.description, not error, error))
                if error:
                    return result()
                if step_number < len(steps) and message_delay > 0:
                    await asyncio.sleep(message_delay)
            completed_repetitions += 1
        except BattleRoundSkipped as exc:
            for item in exc.send_results:
                send_results.append(replace(item, account_label=account_label,
                                            sequence=next_message_sequence))
                next_message_sequence += 1
            if not owner:
                exc.send_results = tuple(send_results)
                raise
            skipped_repetitions += 1
            for number in range(step_number, len(steps) + 1):
                step = steps[number - 1]
                step_results.append(CombinationStepResult(
                    repetition, number,
                    "message_batch" if isinstance(step, MessageBatchStep) else "script",
                    step.description, False,
                    str(exc) if number == step_number else "本轮已恢复跳过", skipped=True))
        finally:
            if token is not None:
                reset_round(token)
        if repetition < repetitions and repeat_interval > 0:
            await asyncio.sleep(repeat_interval)
    return result()


async def execute_combination_for_accounts(
    targets: Sequence[tuple[str, AppContext]],
    steps: Sequence[CombinationStep],
    repetitions: int = 1,
    repeat_interval: float = 0.5,
    message_delay: float = DEFAULT_MESSAGE_DELAY,
) -> dict[str, CombinationResult]:
    """Run one combination concurrently on independent account contexts."""
    targets = tuple(targets)
    steps = tuple(steps)
    if not targets:
        raise ValueError("组合任务至少需要一个在线账号")
    _validate_options(steps, repetitions, repeat_interval)
    results = await asyncio.gather(
        *(
            execute_combination(
                label,
                context,
                steps,
                repetitions=repetitions,
                repeat_interval=repeat_interval,
                message_delay=message_delay,
            )
            for label, context in targets
        )
    )
    return {
        label: result
        for (label, _context), result in zip(targets, results, strict=True)
    }
