"""Account-local battle identity, entry readiness, and lifecycle state."""
from __future__ import annotations

from collections import deque


class BattleLifecycle:
    """Persistent battle gate for one logged-in account."""

    def __init__(self) -> None:
        self.phase = "idle"
        self.battle_epoch = 0
        self.battle_id: int | None = None
        self.battle_unique_id: int | float | None = None
        self._retired_battle_unique_id: int | float | None = None
        self._unconfirmed_battle: tuple[
            int, int | float | None, int | None, int | None, set[str]
        ] | None = None
        self.end_request_sent = False
        self.current_turn: int | None = None
        self.room_id: int | None = None
        self.last_error = ""
        self.entry_responses: set[str] = set()
        self._abandoned_ids = deque(maxlen=64)
        self._abandoned_unique_ids = deque(maxlen=64)
        self._ignore_orphans = False

    def is_stale(self, message: dict) -> bool:
        if message.get("battleId") is not None and message["battleId"] in self._abandoned_ids:
            return True
        if message.get("battleUniqueId") is not None and message["battleUniqueId"] in self._abandoned_unique_ids:
            return True
        return self._ignore_orphans and str(message.get("_cmd", "")) in {
            "2304", "2305", "2401", "2426", "2402", "2403", "2405", "2411", "2423",
        }

    def abandon(self) -> None:
        if self.battle_id is not None:
            self._abandoned_ids.append(self.battle_id)
        if self.battle_unique_id is not None:
            self._abandoned_unique_ids.append(self.battle_unique_id)
        if self._unconfirmed_battle is not None:
            self._abandoned_ids.append(self._unconfirmed_battle[0])
            if self._unconfirmed_battle[1] is not None:
                self._abandoned_unique_ids.append(self._unconfirmed_battle[1])
        self.finish_without_confirmation()
        self._unconfirmed_battle = None
        self.last_error = ""
        self._ignore_orphans = True

    @property
    def entry_ready(self) -> bool:
        return self.phase == "active" and {"2303", "2401", "2426", "2402"}.issubset(
            self.entry_responses
        )

    def begin_entry(self, room_id: int) -> None:
        if self.phase in {"entering", "unknown"}:
            raise RuntimeError(f"当前战斗状态为 {self.phase}，不能提交新的 54_22")
        if self.battle_unique_id is not None:
            self._retired_battle_unique_id = self.battle_unique_id
        self._unconfirmed_battle = None
        self.phase = "entering"
        self.battle_id = None
        self.battle_unique_id = None
        self.end_request_sent = False
        self.current_turn = None
        self.room_id = room_id
        self.last_error = ""
        self.entry_responses.clear()

    def mark_end_requested(self) -> None:
        self.end_request_sent = True
        self.last_error = ""

    def finish_without_confirmation(self) -> None:
        if self.phase in {"active", "ending"} and self.battle_id is not None and {
            "2303", "2401", "2426", "2402"
        }.issubset(self.entry_responses):
            self._unconfirmed_battle = (
                self.battle_id,
                self.battle_unique_id,
                self.current_turn,
                self.room_id,
                set(self.entry_responses),
            )
        else:
            self._unconfirmed_battle = None
        if self.battle_unique_id is not None:
            self._retired_battle_unique_id = self.battle_unique_id
        self.phase = "idle"
        self.battle_id = None
        self.battle_unique_id = None
        self.current_turn = None
        self.room_id = None
        self.end_request_sent = False
        self.entry_responses.clear()

    def mark_unknown(self, reason: str) -> None:
        self.phase = "unknown"
        self.last_error = reason
        self._unconfirmed_battle = None

    def observe(self, message: dict) -> None:
        if self.is_stale(message):
            return
        cmd = str(message.get("_cmd", ""))
        if self.phase == "idle" and self._unconfirmed_battle is not None and cmd in {
            "2405", "2411", "2402", "2423"
        }:
            (
                self.battle_id,
                self.battle_unique_id,
                self.current_turn,
                self.room_id,
                self.entry_responses,
            ) = self._unconfirmed_battle
            self._unconfirmed_battle = None
            self._retired_battle_unique_id = None
            self.phase = "active"
            self.end_request_sent = True
        if cmd == "2303":
            if message.get("msg"):
                if self.phase == "entering":
                    self.phase = "idle"
                    self.battle_id = None
                    self.battle_unique_id = None
                    self.current_turn = None
                    self.room_id = None
                    self.last_error = str(message["msg"])
                    self.entry_responses.clear()
                return
            battle_id = message.get("battleId")
            if isinstance(battle_id, bool) or not isinstance(battle_id, int) or battle_id == 0:
                if self.phase == "entering":
                    self.mark_unknown(f"2303 未返回有效 battleId：{battle_id!r}")
                return
            if self.phase in {"idle", "entering"}:
                if self.phase == "idle":
                    self._unconfirmed_battle = None
                    self.entry_responses.clear()
                    self.current_turn = None
                self.battle_epoch += 1
                self._ignore_orphans = False
                self.phase = "active"
                self.battle_id = battle_id
                self.last_error = ""
                self.entry_responses.add("2303")
            elif self.battle_id is not None and self.battle_id != battle_id:
                self.mark_unknown(
                    f"战斗身份已改变：expected={self.battle_id}, actual={battle_id}"
                )
            return
        if cmd == "2401" and self.phase in {"entering", "active"}:
            self.battle_unique_id = message.get("battleUniqueId")
            self.entry_responses.add("2401")
            return
        if cmd == "2426" and self.phase in {"entering", "active"}:
            self.entry_responses.add("2426")
            return
        if cmd == "2402":
            turn = message.get("pt")
            if isinstance(turn, int) and not isinstance(turn, bool) and turn >= 0:
                self.current_turn = turn
                if self.phase in {"entering", "active"}:
                    self.entry_responses.add("2402")
            return
        if cmd == "2414":
            if message.get("msg") and self.phase in {"entering", "active", "ending"}:
                self.last_error = f"服务器拒绝逃跑：{message['msg']}"
                self.end_request_sent = False
                if self.phase == "ending":
                    self.phase = "active"
                return
            if self.end_request_sent and self.phase in {"entering", "active"}:
                self.phase = "ending"
            return
        if cmd == "2403":
            unique_id = message.get("battleUniqueId")
            if self._unconfirmed_battle is not None and (
                unique_id is None or unique_id == self._unconfirmed_battle[1]
            ):
                self._unconfirmed_battle = None
            if unique_id is not None and (
                unique_id == self._retired_battle_unique_id
                or (
                    self.battle_unique_id is not None
                    and unique_id != self.battle_unique_id
                )
            ):
                return
            if self.phase in {"entering", "active", "ending"}:
                self.phase = "idle"
                self.battle_id = None
                self.battle_unique_id = None
                self._retired_battle_unique_id = None
                self.end_request_sent = False
                self.current_turn = None
                self.room_id = None
                self.last_error = ""
                self.entry_responses.clear()
