"""仓库查询与记录查找；使用调用方已有会话，不自行建立连接。"""
from __future__ import annotations

RESPONSE_TIMEOUT_SECONDS = 30.0


async def find_storage_i(context, r: int) -> int:
    """在关键词 5856 的第 1 页结果中，返回首条 r 匹配记录的 i。"""
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
