"""Choose available skills and replace fainted pets for the current account."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.network.context import AppContext


class AutoBattle:
    def __init__(self, context: AppContext) -> None:
        self.context = context
        self.enabled = False
        self.players: dict[int, dict] = {}
        self.turn_message: dict | None = None
        self.sent_turns: set[tuple[int, int, int]] = set()
        self.reserve_pets: dict[int, dict] = {}
        self.backpack_order: dict[int, int] = {}
        self.roster_turn: int | None = None
        self.pending_switch: tuple[int, int] | None = None
        self.switch_sent = False

    def enable(self) -> None:
        self.enabled = True
        self._submit_current_turn()

    def disable(self) -> None:
        self.enabled = False

    def observe(self, message: dict) -> None:
        cmd = str(message.get("_cmd", ""))
        if cmd == "2303" and not message.get("msg"):
            self.disable()
            self.players.clear()
            self.turn_message = None
            self.sent_turns.clear()
            self.reserve_pets.clear()
            self.backpack_order.clear()
            self.roster_turn = None
            self.pending_switch = None
            self.switch_sent = False
        elif cmd == "2403":
            self.disable()
            self.players.clear()
            self.turn_message = None
            self.reserve_pets.clear()
            self.backpack_order.clear()
            self.roster_turn = None
            self.pending_switch = None
            self.switch_sent = False
            return

        if cmd == "2402":
            turn = message.get("pt")
            if isinstance(turn, int) and not isinstance(turn, bool) and turn >= 0:
                if self.roster_turn is not None and turn > self.roster_turn:
                    for pet in self.reserve_pets.values():
                        pet["lock"] = max(0, pet.get("lock", 0) - (turn - self.roster_turn))
                self.roster_turn = turn

        player_list = message.get("pmmList")
        if isinstance(player_list, list):
            for player in player_list:
                if not isinstance(player, dict):
                    continue
                view = player.get("battleView")
                if isinstance(view, dict) and isinstance(view.get("pmmId"), int):
                    self.players[view["pmmId"]] = player
                    if str(view["pmmId"]) == self.context.user_id:
                        for index, pet in enumerate(view.get("pmList", [])):
                            pet_view = pet.get("pmSView", {})
                            pet_id = pet_view.get("i")
                            if not isinstance(pet_id, int) or pet_id <= 0:
                                continue
                            if cmd in {"2303", "2304"}:
                                self.backpack_order.setdefault(pet_id, pet.get("pmSId", index))
                            if pet_id in self.backpack_order:
                                self.reserve_pets.setdefault(pet_id, {}).update(pet_view)
                        for pet_view in player.get("pmInBagList", []):
                            pet_id = pet_view.get("i")
                            if pet_id in self.reserve_pets:
                                self.reserve_pets[pet_id].update(pet_view)

        own = (
            self.players.get(int(self.context.user_id))
            if cmd in {"2422", "2413"} and self.players else None
        )
        if own is not None:
            own_view = own["battleView"]
            pets = own_view.get("pmList", [])
            if pets:
                pet_id = pets[0].get("pmSView", {}).get("i")
                slot = own_view.get("slotId")
                switch_slots = str(message.get("ss", "")).replace("-", "").split("#")
                if cmd == "2422" and str(slot) in switch_slots:
                    if pet_id in self.reserve_pets:
                        self.reserve_pets[pet_id]["c0"] = 0
                        request = (slot, pet_id)
                        if request != self.pending_switch:
                            self.pending_switch = request
                            self.switch_sent = False
                        self.turn_message = None
                elif cmd == "2413" and self.pending_switch is not None:
                    if slot == self.pending_switch[0] and pet_id != self.pending_switch[1]:
                        self.pending_switch = None
                        self.switch_sent = False

        if cmd == "2402":
            self.turn_message = message
            self._submit_current_turn()
        elif self.pending_switch is not None:
            self._submit_current_turn()

    def _submit_current_turn(self) -> None:
        if not self.enabled or not self.context.battle.entry_ready:
            return
        if self.pending_switch is not None:
            self._submit_switch()
            return
        turn_message = self.turn_message
        if turn_message is None:
            return
        turn = turn_message.get("pt")
        if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
            return
        try:
            own_id = int(self.context.user_id)
        except ValueError:
            return
        own = self.players.get(own_id)
        if own is None:
            return
        own_view = own.get("battleView", {})
        slot = own_view.get("slotId")
        if not isinstance(slot, int):
            return
        if not any(
            len(fields) > 3
            and fields[0] == str(slot)
            and fields[1] == "1"
            and fields[3] == "1"
            for fields in (
                entry.split("-") for entry in str(turn_message.get("desc", "")).split(";")
            )
        ):
            return
        key = (self.context.battle.battle_epoch, turn, slot)
        if key in self.sent_turns:
            return

        pets = own_view.get("pmList")
        if not isinstance(pets, list) or not pets or not isinstance(pets[0], dict):
            return
        hp = pets[0].get("pmSView", {}).get("c0")
        if isinstance(hp, (int, float)) and hp <= 0:
            return
        skills = pets[0].get("skills")
        if not isinstance(skills, list):
            return
        skill_id = None
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            try:
                remaining = int(str(skill.get("pp", "")).split("/", 1)[0])
                cooldown = int(skill.get("cd", 0))
            except ValueError:
                continue
            if (
                isinstance(skill.get("id"), int)
                and skill["id"] > 0
                and isinstance(skill.get("ssi"), int)
                and skill["ssi"] >= 0
                and (remaining == -1 or remaining > 0)
                and cooldown <= 0
            ):
                skill_id = skill["id"]
                break
        if skill_id is None:
            return

        opponent = next(
            (
                player for player_id, player in self.players.items()
                if player_id != own_id and isinstance(player.get("battleView", {}).get("slotId"), int)
            ),
            None,
        )
        if opponent is None:
            return
        target_view = opponent["battleView"]
        params = {
            "turn": turn,
            "reqPSId": slot,
            "tarPSId": opponent.get("PSId", 0),
            "tarSId": target_view["slotId"],
            "ussi": -1,
            "isAuto": False,
            "skillId": skill_id,
        }
        self.sent_turns.add(key)
        asyncio.create_task(self._send_action(key, "1401", params))

    def _submit_switch(self) -> None:
        if self.switch_sent:
            return
        turn = self.context.battle.current_turn
        if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
            return
        slot, dead_pet_id = self.pending_switch
        candidates = [
            pet_id for pet_id, pet in self.reserve_pets.items()
            if pet_id != dead_pet_id and pet.get("c0", 0) > 0
        ]
        if not candidates:
            return
        unlocked = [
            pet_id for pet_id in candidates
            if self.reserve_pets[pet_id].get("lock", 0) <= 0
        ]
        if unlocked:
            candidates = unlocked
        else:
            # The client permits the least-locked pet when all reserves are locked.
            lock = min(self.reserve_pets[pet_id].get("lock", 0) for pet_id in candidates)
            candidates = [
                pet_id for pet_id in candidates
                if self.reserve_pets[pet_id].get("lock", 0) == lock
            ]
        pet_id = min(candidates, key=self.backpack_order.__getitem__)
        params = {"turn": turn, "reqPSId": slot, "pmId": pet_id}
        key = (self.context.battle.battle_epoch, turn, slot)
        self.switch_sent = True
        asyncio.create_task(self._send_action(key, "1403", params, self.pending_switch))

    async def _send_action(
        self, key: tuple[int, int, int], cmd: str, params: dict,
        switch_request: tuple[int, int] | None = None,
    ) -> None:
        sent = False
        action = "自动换宠" if cmd == "1403" else "自动出手"
        try:
            if (
                not self.enabled
                or not self.context.socket.connected
                or self.context.battle.phase != "active"
                or self.context.battle.battle_epoch != key[0]
                or self.context.battle.current_turn != key[1]
            ):
                return
            if self.pending_switch != switch_request:
                return
            self.context.assert_automation_allowed()
            await self.context.socket.send_xt_message(13, cmd, params)
            sent = True
            if cmd == "1403":
                self.context.log(f"自动换宠：背包第 {self.backpack_order[params['pmId']] + 1} 位，已提交上场请求")
            else:
                self.context.log(f"自动出手：第 {key[1] + 1} 回合，技能 {params['skillId']}")
        except Exception as exc:
            self.context.log(f"{action}失败：{exc}")
        finally:
            if cmd == "1403" and not sent and self.pending_switch == switch_request:
                self.switch_sent = False
