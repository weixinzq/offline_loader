"""Find a storage entry and send cultivation messages using its i value."""
from __future__ import annotations

from src.game.pets.storage import find_storage_i
from src.messaging.dispatcher import SendResult, execute_messages
from src.messaging.parser import SendMessage

SCRIPT_NAME = "仓库亚比查询"
SCRIPT_DESCRIPTION = "按 r 查询首条仓库记录"
#send={"id":15,"param":{"lv":false,"ap":false,"petId":8,"equIds":[25,26,27,28]},"cmd":"54_27"}|
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
        SendMessage(42, "SA231229_sl", {"mt": 1, "ids": f"[{i},1,2,{i},1,2,3,4,5]"}),
        SendMessage(15,"54_22",{"handler":"HKR200306","chapter":3,"pos":0,"type":99}),
        SendMessage(id = 13, cmd = "1409", param={"useAiType":1}),
        SendMessage(id=-1, cmd="#wait", param={}),
        SendMessage(15,"54_22",{"handler":"HKR200306","chapter":3,"pos":1,"type":99}),
        SendMessage(id = 13, cmd = "1409", param={"useAiType":1}),
        SendMessage(id=-1, cmd="#wait", param={}),
        SendMessage(15,"54_22",{"handler":"HKR200306","chapter":3,"pos":2,"type":99}),
        SendMessage(id = 13, cmd = "1409", param={"useAiType":1}),
        SendMessage(id=-1, cmd="#wait", param={}),
        SendMessage(15,"54_22",{"handler":"HKR200306","chapter":3,"pos":3,"type":99}),
        SendMessage(id = 13, cmd = "1409", param={"useAiType":1}),
        SendMessage(id=-1, cmd="#wait", param={}),
        SendMessage(21, "14_19", {"num": 10}),
        SendMessage(42, "EU250926_xbr", {"index": 0}),
        SendMessage(42, "EU250926_upgrade", {"index": 0, "type": 0, "petId": i}),
    )

#send={"id":15,"param":{"handler":"HKR200306","chapter":3,"pos":0,"type":99},"cmd":"54_22"}|
async def run(context) -> tuple[SendResult, ...]:
    await context.socket.send_xt_message(13, "1222", {"pms": "1"})
    i = await find_storage_i(context, context.params.get("r"))
    context.log(f"仓库查询结果：i={i}")
    return tuple(await execute_messages(
        context.user_id, context, cultivation_messages(i), preserve_current_battle=True,
    ))
