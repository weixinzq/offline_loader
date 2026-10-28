"""Pure message preflight and registered script capabilities."""
from dataclasses import asdict, replace
from copy import deepcopy
import json

from src.messaging.dispatcher import validate_messages
from src.messaging.parser import SendMessage, parse_pipe_separated_messages, parse_send_message
from src.scripting.composer import MessageBatchStep, ScriptStep
from src.scripting.loader import discover_scripts
from src.service.contracts import MessageSpec, ServiceError


class MessageService:
    @staticmethod
    def decode(raw):
        raw = raw.model_dump() if isinstance(raw, MessageSpec) else raw
        value = MessageSpec.model_validate(raw)
        if value.id == -1:
            if value.cmd == "#wait" and value.param == {}:
                return parse_send_message("#wait|")
            if value.cmd == "#time" and set(value.param) == {"seconds"}:
                seconds = value.param["seconds"]
                if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                    raise ValueError("等待时间必须为数字")
                return parse_send_message(f"#time={seconds}|")
            raise ValueError("未知本地指令")
        return parse_send_message("#send=" + json.dumps(value.model_dump()) + "|")

    def validate(self, raw):
        try:
            messages = [self.decode(item) for item in raw]
            if not messages:
                raise ValueError("消息不能为空")
            validate_messages(messages)
            return {"valid": True, "messages": [asdict(item) for item in messages], "count": len(messages)}
        except ValueError as exc:
            raise ServiceError("invalid_messages", str(exc)) from exc

    def parse(self, text):
        try:
            return self.validate([asdict(item) for item in parse_pipe_separated_messages(text)])
        except ValueError as exc:
            raise ServiceError("invalid_messages", str(exc)) from exc


class ScriptContext:
    """Capabilities available to trusted registered scripts, without credentials.

    Existing socket/messages properties are narrow adapters rather than the
    underlying socket or full AppContext. There is exactly one receive owner.
    """
    def __init__(self, context, params=None):
        self._context = context
        self._params = deepcopy(params) if params is not None else {}
        self.socket = ScriptSocket(context.socket)
        self.messages = ScriptMessages(context.messages)

    @property
    def params(self):
        return deepcopy(self._params)

    @property
    def user_id(self):
        return self._context.user_id

    @property
    def battle(self):
        return self._context.battle

    @property
    def auto_battle(self):
        return self._context.auto_battle

    def log(self, message):
        self._context.log(message)

    def assert_automation_allowed(self):
        self._context.assert_automation_allowed()

    async def wait_for_player_initialization(self):
        await self._context.wait_for_player_initialization()

    async def enter_room(self, room_name, timeout=15.0):
        await self._context.enter_room(room_name, timeout)


class ScriptSocket:
    def __init__(self, socket):
        self._socket = socket

    @property
    def connected(self):
        return self._socket.connected

    @property
    def active_room_id(self):
        return getattr(self._socket, "active_room_id", -1)

    async def send_xt_message(self, ext_id, cmd, params):
        await self._socket.send_xt_message(ext_id, cmd, params)


class ScriptMessages:
    def __init__(self, router):
        self._router = router

    @property
    def disconnected(self):
        return self._router.disconnected

    def subscribe(self, max_queue_size=200):
        return self._router.subscribe(max_queue_size=max_queue_size)

    def add_observer(self, callback):
        self._router.add_observer(callback)

    def remove_observer(self, callback):
        self._router.remove_observer(callback)


class ScriptRegistry:
    def __init__(self, messages):
        self.messages = messages
        self._scripts = {script.module_name.removeprefix("scripts."): script for script in discover_scripts()}

    def list(self):
        return [{"script_id": key, "name": script.name, "description": script.description} for key, script in self._scripts.items()]

    def build_steps(self, steps):
        result = []
        for step in steps:
            if step.kind == "messages":
                validated = self.messages.validate(step.messages)
                result.append(MessageBatchStep(tuple(self.messages.decode(item) for item in validated["messages"])))
            else:
                if step.script_id not in self._scripts:
                    raise ServiceError("unknown_script", "专用脚本不存在")
                script = self._scripts[step.script_id]

                async def run(context, runner=script.run, params=deepcopy(step.params)):
                    return await runner(ScriptContext(context, params))

                result.append(ScriptStep(replace(script, run=run)))
        return tuple(result)
