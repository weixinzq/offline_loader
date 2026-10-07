"""MT250816: request panel data and keep the larger score."""
from __future__ import annotations

from src.network.context import AppContext
from src.network.socket_client import GameSocket
from src.messaging.dispatcher import DEFAULT_MESSAGE_DELAY, _end_previous_battle
#from src.protocol.operations import keep_mt250816_best
import asyncio
SCRIPT_NAME = "四象一"
SCRIPT_DESCRIPTION = "读取历史最佳与本次值，并发送最终选择"

EXT_ID = 42
PANEL_COMMAND = "MT250816_panel"
PANEL_TIMEOUT_SECONDS = 30.0


def is_panel_message(message: dict) -> bool:
    return message.get("_cmd") == PANEL_COMMAND


async def request_panel(context: AppContext) -> None:
    await context.socket.send_xt_message(EXT_ID, PANEL_COMMAND, {})


#"mt2i":["2744_4986:2951_5119:3572_6032:3308_5791:2994_5717:3479_6018=27895083",
# "2744_4986:2951_5119:3572_6032:3308_5791:2994_5717:3479_6018=2102438"]

def parse_mt250816_score(
    message :dict
) ->tuple[int, int] :
    if message.get("_cmd") != PANEL_COMMAND:
        raise ValueError("not mt250816 value")
    entries = message.get("mt2i")
    def score(entry : object) -> int:
        if not isinstance(entry,str) or "=" not in entry:
            raise ValueError("invalue mt2i value")
        try:
            return int(entry.rsplit("=",1)[1]) 
        except ValueError as exc:
            raise ValueError("invalue score value: {enrty!r}") from exc
    return score(entries[0]), score(entries[1])

async def keep_mt250816_best(
    sock : GameSocket,
    panel : dict,
    index :int  #四关id
) ->tuple[int, int, int]:
    historical_best, current = parse_mt250816_score(panel)
    keep_type = 1 if current > historical_best else 0
    await sock.send_xt_message(
        42,
        "MT250816_t2c",
        {"index": index, "type": keep_type},
    )
    return historical_best, current, keep_type
        
        




async def run(context: AppContext) -> None:
    subscription = context.messages.subscribe()
    try:
        await request_panel(context)
        panel = await subscription.wait_for(
            is_panel_message, timeout=PANEL_TIMEOUT_SECONDS
        )
    finally:
        subscription.close()

    historical_best, current, keep_type = await keep_mt250816_best(
        context.socket, panel, index=0
    )
    keep_name = "本次值" if keep_type == 1 else "历史最佳"
    context.log(
        f"四象第一关:历史最佳={historical_best}，本次值={current},"
        f"选择保留{keep_name}"
    )
    if context.battle.phase in {"active", "ending"}:
        if not context.battle.end_request_sent:
            await asyncio.sleep(DEFAULT_MESSAGE_DELAY)
        await _end_previous_battle(context, 0)
