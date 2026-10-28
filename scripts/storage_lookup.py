"""Find a storage entry and send cultivation messages using its i value."""
from __future__ import annotations

from src.messaging.dispatcher import SendResult, execute_messages
from src.messaging.parser import SendMessage

SCRIPT_NAME = "仓库亚比查询"
SCRIPT_DESCRIPTION = "按 r 查询首条仓库记录"
RESPONSE_TIMEOUT_SECONDS = 30.0

def cultivation_messages(i: int) -> tuple[SendMessage, ...]:
    # 直接在下面逐条修改消息；需要当前亚比编号的位置直接填写 i。
    return (
        SendMessage(34, "70_8", {"id": 10542, "num": 1}),
        SendMessage(43, "78_13", {}),
        SendMessage(42, "ABSSC260930_draw", {}),
        SendMessage(42, "ABSSC260930_claim", {}),
        SendMessage(42, "74_1", {"eventId": 48489}),
        SendMessage(42, "ACPT260924_1", {"index": 28}),
        SendMessage(42, "APS231229_2", {"ids": [29196, 29200, 29204, 29208], "t": 1, "ri": 5856}),
        SendMessage(13, "1222", {"pms": f'"{i}#1#2#3#4#5"'}),
        SendMessage(15, "54_27", {"lv": False, "petId": i, "equIds": [-29196, -29200, -29204, -29208], "ap": False}),
        SendMessage(13, "1241", {"exp": 4999999, "id": 7, "userAllot": True}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 0, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 0, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 0, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 1, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 1, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 1, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 2, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 2, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 2, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 3, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 3, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 3, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 4, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 4, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 4, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 5, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 5, "num": 1}),
        SendMessage(21, "14_12", {"pi": i, "ii": 3339, "pa": 5, "num": 1}),
        SendMessage(21, "14_12", {"pa": 3, "num": 1, "ii": 4263, "pi": i}),
        SendMessage(21, "14_12", {"pa": 3, "num": 1, "ii": 4263, "pi": i}),
        SendMessage(21, "14_12", {"pa": 3, "num": 1, "ii": 4263, "pi": i}),
        SendMessage(21, "14_12", {"pa": 0, "num": 1, "ii": 4263, "pi": i}),
        SendMessage(21, "14_12", {"pa": 0, "num": 1, "ii": 4263, "pi": i}),
        SendMessage(21, "14_12", {"pa": 0, "num": 1, "ii": 4263, "pi": i}),
        SendMessage(42, "EU250926_upgrade", {"index": 0, "petId": i, "type": 0}),
        SendMessage(42, "LBF260918_1", {}),
        SendMessage(42, "74_1", {"eventId": 48431}),
        SendMessage(21, "14_19", {"num": 10}),
        SendMessage(42, "EU250926_xbr", {"index": 0}),
        SendMessage(42, "EU250926_upgrade", {"index": 0, "type": 0, "petId": i}),
        SendMessage(42, "SA231229_sl", {"mt": 1, "ids": f"[{i},1,2,{i},1,2,3,4,5]"}),
    )


async def find_storage_i(context, r: int) -> int:
    """Return i from the first entry whose r matches; raise if not found."""
    if type(r) is not int:
        raise ValueError("参数 r 必须是整数，例如 {\"r\": 6000}")

    await context.wait_for_player_initialization()
    subscription = context.messages.subscribe()
    try:
        await context.socket.send_xt_message(
            13, "1226", {
                "label": [-1, -1, -1],
                "stars": ["GQ"],
                "order": 1,
                "race": [],
                "page": 1,
                "kw": "5856",
                "nsk": False,
                "sys": "",
                "elitePM": 0,
                "loc": 10,
                "asc": False,
                "pmTypes": [-2],
            },
        )
        response = await subscription.wait_for(
            lambda message: message.get("_cmd") == "1226",
            timeout=RESPONSE_TIMEOUT_SECONDS,
        )
    finally:
        subscription.close()

    for entry in response["storageList"]:
        if entry["r"] == r:
            return entry["i"]
    raise ValueError(f"仓库第 1 页未找到 r={r} 的记录")


async def run(context) -> tuple[SendResult, ...]:
    i = await find_storage_i(context, context.params.get("r"))
    context.log(f"仓库查询结果：i={i}")
    return tuple(await execute_messages(
        context.user_id, context, cultivation_messages(i), preserve_current_battle=True,
    ))
