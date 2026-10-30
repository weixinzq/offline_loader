"""Choose skills and replace fainted pets independently for each controlled slot."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.network.context import AppContext


class AutoBattle:
    def __init__(self, context: AppContext) -> None:
        self.context = context
        self.enabled = False
        self.players: dict[int, dict] = {}  # Field slot -> player; one owner may occupy several slots.
        self.turn_message: dict | None = None
        self.sent_turns: set[tuple[int, int, int]] = set()
        self.reserve_pets: dict[int, dict] = {}
        self.backpack_order: dict[int, int] = {}
        self.roster_turn: int | None = None
        self.pending_switches: dict[int, int] = {}
        self.switch_targets: dict[int, int] = {}
        self._pending_actions: set[asyncio.Task] = set()

    @property
    def own_players(self) -> list[dict]:
        return [player for player in self.players.values()
                if str(player["battleView"]["pmmId"]) == self.context.user_id]

    def enable(self) -> None:
        self.enabled = True
        self._submit_current_turn()

    def disable(self) -> None:
        self.enabled = False

    def _schedule(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._pending_actions.add(task)
        task.add_done_callback(self._pending_actions.discard)

    async def abort_pending(self) -> None:
        self.disable()
        tasks = tuple(self._pending_actions)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def clear_battle(self) -> None:
        self.disable()
        self.players.clear()
        self.turn_message = None
        self.sent_turns.clear()
        self.reserve_pets.clear()
        self.backpack_order.clear()
        self.roster_turn = None
        self.pending_switches.clear()
        self.switch_targets.clear()

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
            self.pending_switches.clear()
            self.switch_targets.clear()
        elif cmd == "2403":
            self.disable()
            self.players.clear()
            self.turn_message = None
            self.reserve_pets.clear()
            self.backpack_order.clear()
            self.roster_turn = None
            self.pending_switches.clear()
            self.switch_targets.clear()
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
                    slot = view.get("slotId")
                    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
                        continue
                    self.players[slot] = player
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

        if cmd in {"2422", "2413"}:
            for own in self.own_players:
                own_view = own["battleView"]
                pets = own_view.get("pmList", [])
                if not pets:
                    continue
                pet_id = pets[0].get("pmSView", {}).get("i")
                slot = own_view["slotId"]
                switch_slots = str(message.get("ss", "")).replace("-", "").split("#")
                if cmd == "2422" and str(slot) in switch_slots:
                    if pet_id in self.reserve_pets:
                        self.reserve_pets[pet_id]["c0"] = 0
                        if pet_id != self.pending_switches.get(slot):
                            self.pending_switches[slot] = pet_id
                            self.switch_targets.pop(slot, None)
                elif cmd == "2413" and slot in self.pending_switches:
                    if pet_id != self.pending_switches[slot]:
                        self.pending_switches.pop(slot)
                        self.switch_targets.pop(slot, None)

        if cmd == "2402":
            self.turn_message = message
            self._submit_current_turn()
        elif self.pending_switches:
            self._submit_current_turn()

    def _submit_current_turn(self) -> None:
        if not self.enabled or not self.context.battle.entry_ready:
            return
        for slot, pet_id in self.pending_switches.items():
            self._submit_switch(slot, pet_id)
        turn_message = self.turn_message
        if turn_message is None:
            return
        turn = turn_message.get("pt")
        if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
            return
        for own in self.own_players:
            if own["battleView"]["slotId"] not in self.pending_switches:
                self._submit_unit_turn(own, turn, turn_message)

    def _submit_unit_turn(self, own: dict, turn: int, turn_message: dict) -> None:
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
            except (TypeError, ValueError):
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
        params = {
            "turn": turn,
            "reqPSId": slot,
            "isAuto": skill_id is None,
        }
        cmd = "1406"
        if skill_id is not None:
            opponent = next(
                (player for player in self.players.values()
                 if str(player["battleView"]["pmmId"]) != self.context.user_id),
                None,
            )
            if opponent is None:
                return
            params.update(tarPSId=opponent.get("PSId", 0), tarSId=opponent["battleView"]["slotId"],
                          ussi=-1, skillId=skill_id)
            cmd = "1401"
        self.sent_turns.add(key)
        self._schedule(self._send_action(key, cmd, params))

    def _submit_switch(self, slot: int, dead_pet_id: int) -> None:
        if slot in self.switch_targets:
            return
        turn = self.context.battle.current_turn
        if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
            return
        active_pets = {pet.get("pmSView", {}).get("i") for player in self.own_players
                       for pet in player["battleView"].get("pmList", [])[:1]}
        candidates = [
            pet_id for pet_id, pet in self.reserve_pets.items()
            if pet_id != dead_pet_id and pet.get("c0", 0) > 0
            and pet_id not in active_pets and pet_id not in self.switch_targets.values()
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
        self.switch_targets[slot] = pet_id
        self._schedule(self._send_action(key, "1403", params, (slot, dead_pet_id)))

    async def _send_action(
        self, key: tuple[int, int, int], cmd: str, params: dict,
        switch_request: tuple[int, int] | None = None,
    ) -> None:
        sent = False
        action = "自动换宠" if cmd == "1403" else "自动待机" if cmd == "1406" else "自动出手"
        try:
            if (
                not self.enabled
                or not self.context.socket.connected
                or self.context.battle.phase != "active"
                or self.context.battle.battle_epoch != key[0]
                or self.context.battle.current_turn != key[1]
            ):
                return
            if switch_request is not None:
                if self.pending_switches.get(key[2]) != switch_request[1]:
                    return
            elif key[2] in self.pending_switches:
                return
            self.context.assert_automation_allowed()
            await self.context.socket.send_xt_message(13, cmd, params)
            sent = True
            if cmd == "1403":
                self.context.log(f"自动换宠：位置 {key[2]}，背包第 {self.backpack_order[params['pmId']] + 1} 位，已提交上场请求")
            elif cmd == "1406":
                self.context.log(f"自动待机：第 {key[1] + 1} 回合，位置 {key[2]}，无可用技能")
            else:
                self.context.log(f"自动出手：第 {key[1] + 1} 回合，位置 {key[2]}，技能 {params['skillId']}")
        except Exception as exc:
            self.context.log(f"{action}失败：位置 {key[2]}，{exc}")
        finally:
            if cmd == "1403" and not sent and self.pending_switches.get(key[2]) == switch_request[1]:
                self.switch_targets.pop(key[2], None)
