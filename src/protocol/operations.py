"""
protocol.py — 奥拉星 H5 客户端高层协议
对照 H5 源码: BootLoader.ts, SocketClient.ts, MemberClient.ts
"""
from __future__ import annotations
import asyncio
import random
from src.network.socket_client import GameSocket

# Extension IDs — 对照 ExtMap.ts
EXT_USER       = 1     # UserExtension: getStartInfo, getCurrentTime
EXT_TEAM       = 2     # TeamExtension: 宠物相关
EXT_BUDDY      = 3     # BuddyManagerExtension: 战斗/好友
EXT_MEMBER     = 9     # MemberExtension: 11_1 (会员权限)
EXT_POCKET     = 13    # PocketMonsterExtension
EXT_MATERIAL   = 21    # MaterialExtension: 物品
EXT_ROB_MEDAL  = 25    # RobMedalArenaExtension: 亲密度
EXT_DAILY_TASK = 30    # DailyTaskExtension: 每日任务
EXT_ACTIVITY   = 1008  # 活动

#"mt2i":["2744_4986:2951_5119:3572_6032:3308_5791:2994_5717:3479_6018=27895083","2744_4986:2951_5119:3572_6032:3308_5791:2994_5717:3479_6018=2102438"
def parse_mt250816_scores(panel: dict, index: int = 0) -> tuple[int, int]:
    """Return ``(historical_best, current)`` from an MT250816 panel push."""
    if panel.get("_cmd") != "MT250816_panel":
        raise ValueError("not an MT250816_panel response")

    entries = panel.get("mt2i")
    pair_start = index * 2
    if not isinstance(entries, list) or pair_start + 1 >= len(entries):
        raise ValueError(f"MT250816 score pair for index={index} is missing")

    def score(entry: object) -> int:
        if not isinstance(entry, str) or "=" not in entry:
            raise ValueError(f"invalid MT250816 score entry: {entry!r}")
        try:
            return int(entry.rsplit("=", 1)[1])
        except ValueError as exc:
            raise ValueError(f"invalid MT250816 score entry: {entry!r}") from exc

    return score(entries[pair_start]), score(entries[pair_start + 1])


def choose_mt250816_type(panel: dict, index: int = 0) -> int:
    """Return 1 to keep the current score, otherwise 0 for historical best."""
    historical_best, current = parse_mt250816_scores(panel, index)
    return 1 if current > historical_best else 0


async def keep_mt250816_best(
    sock: GameSocket, panel: dict, index: int = 0
) -> tuple[int, int, int]:
    """Compare the panel scores and send the corresponding keep command."""
    historical_best, current = parse_mt250816_scores(panel, index)
    keep_type = 1 if current > historical_best else 0
    await sock.send_xt_message(
        42,
        "MT250816_t2c",
        {"index": index, "type": keep_type},
    )
    return historical_best, current, keep_type


async def init_game(
    sock: GameSocket, last_login_time: int = 0, delay_ms: int = 100
) -> None:
    """
    游戏初始化 — 对照 BootLoader.ts:77-96
    
    H5 在 logOK 后并行请求服务器时间、会员权限和启动信息。
    $ev (ext=0) 是内部事件，跳过。
    """
    print("  发送初始化命令...")

    await sock.send_xt_message(EXT_USER, "getCurrentTime", {})
    await asyncio.sleep(delay_ms / 1000.0)

    await sock.send_xt_message(EXT_MEMBER, "11_1", {})
    await asyncio.sleep(delay_ms / 1000.0)

    await sock.send_xt_message(
        EXT_USER,
        "getStartInfo",
        {
            "firstLogin": last_login_time < 0,
            "lastLoginTime": last_login_time,
        },
    )
    await asyncio.sleep(delay_ms / 1000.0)
    print("  已发送最小玩家启动初始化序列")


async def send_activity_cmd(
    sock: GameSocket,
    cmd_head: str,
    suffix: str,
    params: dict | None = None,
) -> None:
    """发送活动命令 — ext_id = 42 (HolidayExtension)"""
    cmd = f"{cmd_head}{suffix}"
    await sock.send_xt_message(42, cmd, params or {})
