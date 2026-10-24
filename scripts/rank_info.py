"""Query the account's ranked seasons and log the displayed rank."""
from __future__ import annotations

from src.network.context import AppContext

SCRIPT_NAME = "段位信息"
SCRIPT_DESCRIPTION = "查询当前账号各赛季段位、星数和战绩并输出日志"
RESPONSE_TIMEOUT_SECONDS = 30.0

# PetFightSpace3RankLevelConfig：累计星数起点及对应的小段位。
RANK_LEVELS = (
    (1, "新手"),
    (2, "青铜兵士III"), (3, "青铜兵士II"), (4, "青铜兵士I"),
    (6, "白银战士III"), (8, "白银战士II"), (10, "白银战士I"),
    (13, "黄金勇士III"), (16, "黄金勇士II"), (19, "黄金勇士I"),
    (23, "铂金战将IV"), (26, "铂金战将III"),
    (29, "铂金战将II"), (32, "铂金战将I"),
    (36, "钻石军长V"), (39, "钻石军长IV"), (42, "钻石军长III"),
    (45, "钻石军长II"), (48, "钻石军长I"),
    (52, "传奇大师V"), (56, "传奇大师IV"), (60, "传奇大师III"),
    (64, "传奇大师II"), (68, "传奇大师I"),
    (73, "王者英雄"), (123, "荣耀王者"),
)
MATCH_TYPES = {0: "双宠排位模式", 1: "多亚比排位模式", 5: "狂野模式"}


def parse_rank(star_num: int) -> tuple[str, int]:
    for start, name in reversed(RANK_LEVELS):
        if star_num >= start:
            return name, star_num - start + 1
    return "未定级", 0


async def run(context: AppContext) -> None:
    await context.wait_for_player_initialization()
    subscription = context.messages.subscribe()
    try:
        await context.socket.send_xt_message(
            12, "71_29", {"vUId": int(context.user_id)}
        )
        response = await subscription.wait_for(
            lambda message: message.get("_cmd") == "71_29",
            timeout=RESPONSE_TIMEOUT_SECONDS,
        )
    finally:
        subscription.close()

    if response.get("r") != 1:
        raise RuntimeError("段位查询失败")
    seasons = response["sai"]
    if not seasons:
        context.log("段位信息：暂无排位赛季记录")
    for season in seasons:
        rank, stars = parse_rank(season["s"])
        context.log(
            f"{season['y']}年{season['m']}月 {MATCH_TYPES[season['t']]}："
            f"{rank}，{stars}星；场次{season['tt']}，胜场{season['wt']}，"
            f"胜率{season['w'] / 10:g}%"
        )
