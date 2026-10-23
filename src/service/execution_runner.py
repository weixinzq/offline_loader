"""One execution/audit path for in-process and isolated account sessions."""
import asyncio
from contextvars import ContextVar

from src.scripting.composer import execute_combination


async def run_execution(account_id, context, steps, outcome, *, executor=execute_combination, **options):
    socket = getattr(context, "socket", None)
    original_send = getattr(socket, "send_xt_message", None)
    observed = callable(original_send)
    scope = ContextVar("service_execution_writes", default=False)
    token = scope.set(True)
    attempts = completed = 0
    cancelled_write = False

    async def send(*args, **kwargs):
        nonlocal attempts, completed, cancelled_write
        if not scope.get():
            return await original_send(*args, **kwargs)
        attempts += 1
        try:
            result = await original_send(*args, **kwargs)
        except asyncio.CancelledError:
            cancelled_write = True
            raise
        completed += 1
        return result

    if observed:
        socket.send_xt_message = send
    try:
        result = await executor(account_id, context, steps, **options)
    finally:
        if observed:
            socket.send_xt_message = original_send
            status = ("unknown" if cancelled_write else "succeeded" if attempts == completed
                      else "partial" if completed else "failed") if attempts else "not_attempted"
            outcome["network_write"] = {"status": status, "count": completed}
        scope.reset(token)
    sends = tuple(result.send_results)
    if not observed:
        written = sum(bool(send.success) for send in sends)
        status = ("succeeded" if written == len(sends) else "partial" if written else "failed") if sends else "unknown"
        outcome["network_write"] = {"status": status, "count": written}
    skipped = getattr(result, "skipped_repetitions", 0)
    outcome.update(success=result.success, completed_repetitions=result.completed_repetitions,
                   attempted_repetitions=result.completed_repetitions + skipped,
                   skipped_repetitions=skipped,
                   step_results=[{"repetition": step.repetition, "step": step.step, "kind": step.kind,
                                  "success": step.success, "error": step.error or None,
                                  "skipped": getattr(step, "skipped", False)}
                                 for step in result.step_results])
