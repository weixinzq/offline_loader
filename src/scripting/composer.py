"""Ordered combinations of generic message batches and interaction scripts."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Sequence, TypeAlias

from src.messaging.dispatcher import (
    DEFAULT_MESSAGE_DELAY,
    SendResult,
    execute_messages,
)
from src.messaging.parser import SendMessage
from src.network.context import AppContext
from src.scripting.loader import InteractionScript


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


@dataclass(frozen=True)
class CombinationResult:
    requested_repetitions: int
    completed_repetitions: int
    step_results: tuple[CombinationStepResult, ...]
    send_results: tuple[SendResult, ...]

    @property
    def success(self) -> bool:
        return (
            self.completed_repetitions == self.requested_repetitions
            and all(result.success for result in self.step_results)
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

    step_results: list[CombinationStepResult] = []
    send_results: list[SendResult] = []
    completed_repetitions = 0
    next_message_sequence = 1

    for repetition in range(1, repetitions + 1):
        for step_number, step in enumerate(steps, start=1):
            assert_allowed = getattr(context, "assert_automation_allowed", None)
            if callable(assert_allowed):
                try:
                    assert_allowed()
                except Exception as exc:
                    step_results.append(
                        CombinationStepResult(
                            repetition,
                            step_number,
                            "message_batch" if isinstance(step, MessageBatchStep) else "script",
                            step.description,
                            False,
                            str(exc),
                        )
                    )
                    return CombinationResult(
                        repetitions,
                        completed_repetitions,
                        tuple(step_results),
                        tuple(send_results),
                    )
            if isinstance(step, MessageBatchStep):
                batch = await execute_messages(
                    account_label,
                    context,
                    step.messages,
                    delay=message_delay,
                    sequence_start=next_message_sequence,
                    preserve_current_battle=preserve_current_battle,
                )
                send_results.extend(batch)
                next_message_sequence += len(batch)
                failure = next(
                    (result for result in batch if not result.success), None
                )
                if failure is not None or len(batch) != len(step.messages):
                    error = (
                        failure.error
                        if failure is not None
                        else "通用消息批次未完整执行"
                    )
                    step_results.append(
                        CombinationStepResult(
                            repetition,
                            step_number,
                            "message_batch",
                            step.description,
                            False,
                            error,
                        )
                    )
                    return CombinationResult(
                        repetitions,
                        completed_repetitions,
                        tuple(step_results),
                        tuple(send_results),
                    )
                step_results.append(
                    CombinationStepResult(
                        repetition,
                        step_number,
                        "message_batch",
                        step.description,
                        True,
                    )
                )
                if step_number < len(steps) and message_delay > 0:
                    await asyncio.sleep(message_delay)
                continue

            if not isinstance(step, ScriptStep):
                raise TypeError(f"不支持的组合步骤类型：{type(step).__name__}")
            try:
                await step.script.run(context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                step_results.append(
                    CombinationStepResult(
                        repetition,
                        step_number,
                        "script",
                        step.description,
                        False,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
                return CombinationResult(
                    repetitions,
                    completed_repetitions,
                    tuple(step_results),
                    tuple(send_results),
                )
            step_results.append(
                CombinationStepResult(
                    repetition,
                    step_number,
                    "script",
                    step.description,
                    True,
                )
            )
            if step_number < len(steps) and message_delay > 0:
                await asyncio.sleep(message_delay)

        completed_repetitions = repetition
        if repetition < repetitions and repeat_interval > 0:
            await asyncio.sleep(repeat_interval)

    return CombinationResult(
        repetitions,
        completed_repetitions,
        tuple(step_results),
        tuple(send_results),
    )


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
