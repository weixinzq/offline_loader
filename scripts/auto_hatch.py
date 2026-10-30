"""
    #send={"id":17,"param":null,"cmd":"1-8"}|                      发送用于更新孵化状态及查询收取id
    #send={"id":17,"param":{},"cmd":"1=1"}|                        发送用于查询未孵化eggId
    #send={"id":17,"param":{"eggId":345,"id":1},"cmd":"1-4"}|      发送后开始孵化一个蛋，参数eggId是亚比蛋永久唯一仓库id，id是孵化格id
    #send={"id":13,"cmd":"1223","param":{"loc":4,"id":518}}|       发送后收取一个已孵化待收取的亚比，参数id是永久唯一收取id，且非孵化渠道获取的亚比不使该id增加

1. 先发一次1-8消息，从回包 ``es`` 里查看已经孵化完但未收取的蛋；
   每项的 ``ei`` 是 eggId，``pmid`` 是收取 id。
2. 对每个待收取蛋发送1223消息，参数 `{"loc": 4, "id": pmid}`。
3. 恢复完成后发送一次1=1消息，查询当前未孵化蛋列表；
   每项的 ``ei`` 是 eggId。
4. 未孵化蛋按每批最多3个，同时占用 1/2/3 号孵化格发送1-4消息；
   整批等待一次孵化时间后，发1-8消息等到`st=100`再逐个用 ``pmid`` 收取。
5. 收取后发1-8消息确认该 ``ei`` 已从待收取列表消失。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from src.config import PROJECT_ROOT
from src.network.context import AppContext, MessageSubscription

SCRIPT_NAME = "孵化所有亚比蛋"
SCRIPT_DESCRIPTION = "查询账号下未孵化和待收取的蛋，自动孵化并收取"

EXT_ID = 17
HATCH_CMD = "1-4"
COLLECT_EXT_ID = 13
COLLECT_CMD = "1223"
COLLECT_LOC = 4
COLLECT_RESPONSE_CMD = "2223"

# 每批最多同时孵化3个蛋
HATCH_SLOT_IDS: tuple[int, ...] = (1, 2, 3)
#孵化开始后固定等待1.1秒；其它是回包等待上限
HATCH_WAIT_SECONDS = 1.1
HATCH_RESULT_TIMEOUT = 0.6
HATCH_ECHO_TIMEOUT = 5.0
PREPARE_TIMEOUT = 2.0
REFRESH_TIMEOUT = 0.8
VERIFY_TIMEOUT = 0.8
READY_POLL_INTERVAL = 0.2

COLLECT_TIMEOUT = 0.8
COLLECT_MAX_ROUNDS = 3
COLLECT_PARAM_TEMPLATE: dict[str, object] = {
    "loc": COLLECT_LOC,
    "id": "{collectId}",
}

#无效蛋会回包MessageBox_2 (s=-1, id=14) 完善后应该不会出现孵化到不存在的蛋的情况，保险起见还是保留了相关模块
FAILURE_MESSAGE_BOX_SIGNATURES = frozenset({(-1, 14)})

_WATCH_COMMANDS = frozenset(
    {
        HATCH_CMD,
        "1-8",
        "1=1",
        COLLECT_CMD,
        COLLECT_RESPONSE_CMD,
        "MessageBox_2",
    }
)


def _json_default(value: object) -> object:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bytes_hex": bytes(value).hex()}
    return str(value)


def _short(value: object, limit: int = 300) -> str:
    text = json.dumps(value, ensure_ascii=False, default=_json_default)
    return text if len(text) <= limit else text[:limit] + "…"


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _append_record(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(record, ensure_ascii=False, default=_json_default) + "\n"
        )


def _new_log_path(prefix: str) -> Path:
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return log_dir / f"{prefix}-{stamp}.jsonl"


def _record_message(
    context: AppContext,
    path: Path,
    phase: str,
    message: dict,
) -> None:
    cmd = str(message.get("_cmd", "?"))
    if cmd not in _WATCH_COMMANDS:
        return
    _append_record(
        path,
        {
            "timeLocal": datetime.now().astimezone().isoformat(timespec="seconds"),
            "phase": phase,
            "cmd": cmd,
            "message": message,
        },
    )
    context.log(f"[{phase}] cmd={cmd}")


async def _send(
    context: AppContext,
    cmd: str,
    param: dict,
    *,
    ext_id: int = EXT_ID,
) -> None:
    context.assert_automation_allowed()
    if not context.socket.connected or context.messages.disconnected.is_set():
        raise ConnectionError("账号连接已断开")
    await context.socket.send_xt_message(ext_id, cmd, dict(param))
    context.log(f"发送 id={ext_id} cmd={cmd} param={_short(param)}")


async def _wait_for_cmd(
    context: AppContext,
    subscription: MessageSubscription,
    path: Path,
    phase: str,
    expected_cmd: str,
    timeout: float,
) -> dict | None:
    """Wait until a message with expected_cmd arrives, or timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            message = await subscription.wait_for(
                lambda _item: True, timeout=remaining
            )
        except asyncio.TimeoutError:
            return None
        _record_message(context, path, phase, message)
        if str(message.get("_cmd", "")) == expected_cmd:
            return message


async def _send_and_wait(
    context: AppContext,
    subscription: MessageSubscription,
    path: Path,
    phase: str,
    cmd: str,
    param: dict,
    expected_cmd: str,
    timeout: float,
    *,
    ext_id: int = EXT_ID,
) -> dict | None:
    await _send(context, cmd, param, ext_id=ext_id)
    return await _wait_for_cmd(
        context, subscription, path, phase, expected_cmd, timeout
    )


async def _sleep_with_checks(context: AppContext, seconds: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, seconds)
    while True:
        context.assert_automation_allowed()
        if not context.socket.connected or context.messages.disconnected.is_set():
            raise ConnectionError("等待孵化期间账号连接已断开")
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(1.0, remaining))


def _parse_egg_ids(messages: list[dict]) -> list[int]:
    """Parse unhatched egg ids from a 1=1 es list."""
    egg_ids: list[int] = []
    for message in messages:
        if str(message.get("_cmd", "")) != "1=1":
            continue
        es = message.get("es")
        if not isinstance(es, list):
            continue
        for item in es:
            if not isinstance(item, dict):
                continue
            egg_id = _as_int(item.get("ei"))
            if egg_id is not None and egg_id > 0:
                egg_ids.append(egg_id)
    return list(dict.fromkeys(egg_ids))


def _parse_pending_entries(messages: list[dict]) -> list[dict]:
    """Parse 1-8 es entries that are hatched but still pending collection."""
    entries: list[dict] = []
    for message in messages:
        if str(message.get("_cmd", "")) != "1-8":
            continue
        es = message.get("es")
        if not isinstance(es, list):
            continue
        for item in es:
            if not isinstance(item, dict):
                continue
            egg_id = _as_int(item.get("ei"))
            pmid = _as_int(item.get("pmid"))
            if egg_id is None or egg_id <= 0:
                continue
            if pmid is None or pmid <= 0:
                continue
            entries.append(
                {
                    "egg_id": egg_id,
                    "pmid": pmid,
                    "st": _as_int(item.get("st")),
                }
            )
    return entries


def _message_box_signatures(
    messages: list[dict],
) -> set[tuple[int | None, int | None]]:
    signatures: set[tuple[int | None, int | None]] = set()
    for message in messages:
        if str(message.get("_cmd", "")) != "MessageBox_2":
            continue
        signatures.add((_as_int(message.get("s")), _as_int(message.get("id"))))
    return signatures


def _is_failure_response(messages: list[dict]) -> bool:
    return not _message_box_signatures(messages).isdisjoint(
        FAILURE_MESSAGE_BOX_SIGNATURES
    )


def _matches_hatch_echo(message: dict, egg_id: int, slot_id: int) -> bool:
    return (
        str(message.get("_cmd", "")) == HATCH_CMD
        and _as_int(message.get("eggId")) == egg_id
        and _as_int(message.get("id")) == slot_id
    )


async def _wait_for_hatch_echo(
    context: AppContext,
    subscription: MessageSubscription,
    path: Path,
    phase: str,
    egg_id: int,
    slot_id: int,
    timeout: float,
) -> tuple[dict | None, list[dict]]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    seen: list[dict] = []
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None, seen
        try:
            message = await subscription.wait_for(
                lambda _item: True, timeout=remaining
            )
        except asyncio.TimeoutError:
            return None, seen
        seen.append(message)
        _record_message(context, path, phase, message)
        if _matches_hatch_echo(message, egg_id, slot_id):
            return message, seen


async def _query_pending_entries(
    context: AppContext,
    subscription: MessageSubscription,
    path: Path,
    phase: str,
) -> list[dict] | None:
    message = await _send_and_wait(
        context,
        subscription,
        path,
        phase,
        "1-8",
        {},
        "1-8",
        VERIFY_TIMEOUT,
    )
    if message is None:
        return None
    return _parse_pending_entries([message])


async def _refresh_hatch_state(
    context: AppContext,
    subscription: MessageSubscription,
    path: Path,
    phase: str,
) -> list[dict]:

    message = await _send_and_wait(
        context,
        subscription,
        path,
        f"{phase} 刷新",
        "1-8",
        {},
        "1-8",
        REFRESH_TIMEOUT,
    )
    return [message] if message is not None else []


async def _wait_for_ready_entry(
    context: AppContext,
    subscription: MessageSubscription,
    path: Path,
    egg_id: int,
    timeout: float = 15.0,
) -> dict | None:
    """Refresh 1-8 until the egg entry reports st=100 (ready to collect)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_match: dict | None = None
    while True:
        messages = await _refresh_hatch_state(
            context, subscription, path, f"等待 eggId={egg_id} 就绪"
        )
        pending = _parse_pending_entries(messages)
        match = next(
            (entry for entry in pending if entry["egg_id"] == egg_id),
            None,
        )
        if match is not None:
            last_match = match
            if match.get("st") == 100:
                return match
        if loop.time() >= deadline:
            return last_match
        await _sleep_with_checks(context, READY_POLL_INTERVAL)


def _render_collect_params(pmid: int) -> dict:
    rendered: dict[str, object] = {}
    for key, value in COLLECT_PARAM_TEMPLATE.items():
        if value == "{collectId}":
            rendered[key] = pmid
        elif isinstance(value, str):
            rendered[key] = value.replace("{collectId}", str(pmid))
        else:
            rendered[key] = value
    return rendered


async def _collect_once(
    context: AppContext,
    subscription: MessageSubscription,
    path: Path,
    egg_id: int,
    pmid: int,
) -> tuple[bool, dict | None]:
    params = _render_collect_params(pmid)
    response = await _send_and_wait(
        context,
        subscription,
        path,
        f"收取 {egg_id} pmid={pmid}",
        COLLECT_CMD,
        params,
        COLLECT_RESPONSE_CMD,
        COLLECT_TIMEOUT,
        ext_id=COLLECT_EXT_ID,
    )
    if response is None:
        return False, None
    return _as_int(response.get("id")) not in (None, 0), response


async def _collect_egg(
    context: AppContext,
    subscription: MessageSubscription,
    path: Path,
    egg_id: int,
    pmid: int,
) -> None:
    """Collect one egg and verify it left the 1-8 pending list."""
    for round_number in range(1, COLLECT_MAX_ROUNDS + 1):
        ok, response = await _collect_once(
            context, subscription, path, egg_id, pmid
        )
        if not ok:
            detail = "没有 2223 回包" if response is None else _short(response)
            context.log(
                f"收取失败（第 {round_number}/{COLLECT_MAX_ROUNDS} 轮，"
                f"eggId={egg_id} pmid={pmid}）：{detail}"
            )
            continue

        pending = await _query_pending_entries(
            context,
            subscription,
            path,
            f"收取后检查 eggId={egg_id}",
        )
        still_pending = (
            None if pending is None
            else any(entry["egg_id"] == egg_id for entry in pending)
        )
        if still_pending is False:
            context.log(
                f"收取成功：eggId={egg_id} pmid={pmid}，"
                "待收取列表已清除"
            )
            return
        detail = "没有 1-8 回包" if pending is None else "仍在待收取列表"
        context.log(
            f"收取未生效（第 {round_number}/{COLLECT_MAX_ROUNDS} 轮，"
            f"eggId={egg_id} pmid={pmid}）：{detail}"
        )

    raise RuntimeError(
        f"eggId={egg_id} 收取失败，pmid={pmid}；该蛋仍在待收取列表"
    )


async def _query_unhatched_eggs(context: AppContext) -> list[int]:
    """Query 1=1 after recovery and return the current unhatched egg ids."""
    path = _new_log_path("egg-hatch-query")
    context.log(f"查询未孵化蛋开始，日志：{path}")
    subscription = context.messages.subscribe(max_queue_size=1000)
    try:
        message = await _send_and_wait(
            context,
            subscription,
            path,
            "查询 1=1",
            "1=1",
            {},
            "1=1",
            PREPARE_TIMEOUT,
        )
    finally:
        subscription.close()

    egg_ids = _parse_egg_ids([message]) if message is not None else []
    context.log(f"查询完成；未孵化蛋 {len(egg_ids)} 个")
    if egg_ids:
        context.log(f"前 10 个 eggId：{egg_ids[:10]}")
    return egg_ids


async def _recover_pending(context: AppContext) -> int:
    """Collect all hatched-but-uncollected eggs using 1-8 pmid."""
    path = _new_log_path("egg-hatch-recover")
    context.log(f"待收取检查开始，日志：{path}")
    subscription = context.messages.subscribe(max_queue_size=1000)
    recovered = 0
    try:
        messages = await _refresh_hatch_state(
            context, subscription, path, "恢复前"
        )
        pending = _parse_pending_entries(messages)
        if not pending:
            context.log("没有待收取的蛋")
            return 0
        context.log(f"发现 {len(pending)} 个待收取蛋：{_short(pending[:5])}")
        for entry in pending:
            egg_id = int(entry["egg_id"])
            match = await _wait_for_ready_entry(
                context, subscription, path, egg_id
            )
            if match is None or match.get("st") != 100:
                raise RuntimeError(
                    f"eggId={egg_id} 未就绪："
                    f"{_short(match)}"
                )
            pmid = int(match["pmid"])
            await _collect_egg(context, subscription, path, egg_id, pmid)
            recovered += 1
        context.log(f"待收取处理完成：共收取 {recovered} 个")
        return recovered
    finally:
        subscription.close()


async def _start_hatch(
    context: AppContext,
    subscription: MessageSubscription,
    path: Path,
    index: int,
    egg_id: int,
    slot_id: int,
) -> bool:
    """Send 1-4 on one slot and verify the hatch command was accepted."""
    phase = f"孵化 {index} eggId={egg_id} slot={slot_id}"
    context.log(f"[{index}] 开始孵化 eggId={egg_id}，格={slot_id}")
    await _send(
        context,
        HATCH_CMD,
        {"eggId": egg_id, "id": slot_id},
    )
    echo, seen = await _wait_for_hatch_echo(
        context,
        subscription,
        path,
        phase,
        egg_id,
        slot_id,
        HATCH_ECHO_TIMEOUT,
    )
    if echo is None:
        context.log(f"eggId={egg_id} 没有 1-4 回显，跳过")
        return False

    messages = list(seen)
    if not _is_failure_response(messages):
        message_box = await _wait_for_cmd(
            context,
            subscription,
            path,
            phase,
            "MessageBox_2",
            HATCH_RESULT_TIMEOUT,
        )
        if message_box is not None:
            messages.append(message_box)
    if _is_failure_response(messages):
        signatures = sorted(_message_box_signatures(messages))
        context.log(f"eggId={egg_id} 不是有效蛋：{signatures}")
        return False
    return True


async def _wait_for_batch_ready(
    context: AppContext,
    subscription: MessageSubscription,
    path: Path,
    egg_ids: tuple[int, ...],
    timeout: float = 15.0,
) -> dict[int, dict]:
    """Refresh 1-8 until every egg in the batch reports st=100."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    ready: dict[int, dict] = {}
    while True:
        messages = await _refresh_hatch_state(
            context, subscription, path, "批量就绪检查"
        )
        pending = _parse_pending_entries(messages)
        by_id = {entry["egg_id"]: entry for entry in pending}
        for egg_id in egg_ids:
            entry = by_id.get(egg_id)
            if entry is not None and entry.get("st") == 100:
                ready[egg_id] = entry
        if len(ready) == len(egg_ids):
            return ready
        if loop.time() >= deadline:
            return ready
        await _sleep_with_checks(context, READY_POLL_INTERVAL)


async def _run_hatch(
    context: AppContext,
    egg_ids: list[int],
) -> Path:
    path = _new_log_path("auto-hatch")
    context.log(f"自动孵化开始，日志：{path}")
    if not egg_ids:
        context.log("没有未孵化蛋")
        return path

    subscription = context.messages.subscribe(max_queue_size=2000)
    succeeded = 0
    skipped = 0
    batch_size = len(HATCH_SLOT_IDS)
    try:
        for offset in range(0, len(egg_ids), batch_size):
            batch = egg_ids[offset : offset + batch_size]
            started: list[tuple[int, int]] = []
            for position, egg_id in enumerate(batch, start=offset + 1):
                slot_id = HATCH_SLOT_IDS[len(started)]
                if await _start_hatch(
                    context,
                    subscription,
                    path,
                    position,
                    egg_id,
                    slot_id,
                ):
                    started.append((egg_id, slot_id))
                else:
                    skipped += 1

            if not started:
                continue
            context.log(
                f"本批 {len(started)} 个蛋已提交，等待 "
                f"{HATCH_WAIT_SECONDS:g} 秒后收取"
            )
            await _sleep_with_checks(context, HATCH_WAIT_SECONDS)
            ready = await _wait_for_batch_ready(
                context,
                subscription,
                path,
                tuple(egg_id for egg_id, _slot in started),
            )
            for egg_id, slot_id in started:
                entry = ready.get(egg_id)
                if entry is None:
                    skipped += 1
                    context.log(
                        f"eggId={egg_id}（格 {slot_id}）未就绪，跳过"
                    )
                    continue
                pmid = int(entry["pmid"])
                context.log(
                    f"eggId={egg_id}（格 {slot_id}）已就绪，pmid={pmid}"
                )
                await _collect_egg(
                    context, subscription, path, egg_id, pmid
                )
                succeeded += 1
    finally:
        subscription.close()

    context.log(
        f"自动孵化完成：成功 {succeeded} 个，跳过 {skipped} 个"
    )
    return path


async def run(context: AppContext) -> None:
    await context.wait_for_player_initialization()
    context.assert_automation_allowed()
    if not context.socket.connected or context.messages.disconnected.is_set():
        raise ConnectionError("账号连接已断开")

    await _recover_pending(context)
    egg_ids = await _query_unhatched_eggs(context)
    await _run_hatch(context, egg_ids)
