# -*- coding: utf-8 -*-
"""
verify.py — verify key algorithms against H5 source
"""
import asyncio
import json
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace

from src.accounts.manager import (
    AccountConnection,
    AccountConfigError,
    AccountManager,
    AccountSpec,
    load_account_specs,
)
from src.accounts.login import (
    LoginResult,
    ZoneData,
    build_login_params,
    parse_login_response,
    parse_role_response,
    select_role,
)
import src.accounts.login as account_login
import src.accounts.session as account_session
from src.accounts.session import start_player_initialization
from src.protocol.as3_compat import as3_remainder, to_as3_int32
from src.app import parse_index_selection
from src.messaging.audit_log import append_send_results
from src.config import MSGSEQ_FIRST_VAL, INNER_SEQ_INIT
from src.config import DownCmd, MSG_TYPE_SYS
from src.messaging.dispatcher import dispatch_to_accounts
from src.messaging.dispatcher import execute_messages
from src.messaging.auto_battle import AutoBattle
from src.protocol.msgseq import MsgSeq, InnerSeq
from src.protocol.encrypt import encrypt_byte_array
from src.protocol.amf3 import Amf3Writer, Amf3Reader
from src.network.socket_client import GameSocket
from src.protocol.operations import parse_mt250816_scores, choose_mt250816_type
from src.protocol.message_codec import build_message, pack_int, pack_utf
from src.network.context import (
    AppContext,
    BattleLifecycle,
    MessageRouter,
    configure_battle_receive_logging,
)
import src.network.context as network_context
from src.scripting.composer import (
    MessageBatchStep,
    ScriptStep,
    execute_combination,
    execute_combination_for_accounts,
)
from src.scripting.loader import InteractionScript
from src.messaging.parser import (
    SendMessage,
    SendMessageFileError,
    SendMessageParseError,
    parse_send_message,
    parse_send_message_file,
    parse_send_message_lines,
    parse_pipe_separated_messages,
)
from scripts.mt250816_1 import run as run_mt250816
import src.ipc.server as ipc_server
import scripts.auto_battle as battle_sequence_script
from src.scripting.loader import discover_scripts


def decode_server_ext(message: dict) -> dict:
    """Round a fake server push through the production downlink decoder."""
    writer = Amf3Writer()
    writer.write_object(message)
    return GameSocket()._decode_server_body(bytes((1,)) + writer.to_bytes())


def test_msgseq():
    print("=== MsgSeq ===")
    seq = MsgSeq("test_session_id")
    v = seq.next(0, 123456)
    assert v == MSGSEQ_FIRST_VAL, f"Expected {MSGSEQ_FIRST_VAL}, got {v}"
    print(f"  first(0, 123456) = {v} [OK]")
    v2 = seq.next(v, 123456)
    print(f"  next({v}, 123456) = {v2}")
    v3 = seq.next(v2, 123456)
    print(f"  next({v2}, 123456) = {v3} [OK]")


def test_as3_integer_semantics():
    """Cover Python/AS3 differences that affect the second encrypted packet."""
    print("=== AS3 Integer Semantics ===")
    assert to_as3_int32(0x80000048) == -2147483576
    assert to_as3_int32(0xFFFFFFFF) == -1
    assert as3_remainder(-570, 569) == -1
    assert (-570 % 569) == 568  # Demonstrate why native Python `%` is unsafe here.

    # Force the random high bit used by BinaryMsgWriter.getMyNextSeq().
    # AS3 returns signed 0x80000048, not Python's unsigned 2147483720.
    values = iter((0x4000, 0, 0))
    inner = InnerSeq()
    inner._rand = lambda: next(values)
    seq = inner.get_my_next_seq(0, 123456)
    assert seq == -2147483576, seq
    assert (seq & 0x1FFFFFFC) >> 2 == INNER_SEQ_INIT

    writer = Amf3Writer()
    writer.write_object({":ext_seq;": seq})
    decoded = Amf3Reader(writer.to_bytes()).read_object()[":ext_seq;"]
    assert decoded == float(seq)
    print("  signed int32 + truncating remainder + AMF3 sign [OK]")


def test_inner_seq():
    """verify InnerSeq logic"""
    print("=== InnerSeq ===")
    inner = InnerSeq()
    # get_my_next_seq(0, userid) mixes internal result=18 with random bits
    # Final: ((random & ~0x1FFFFFFC) | (18 << 2))
    v = inner.get_my_next_seq(0, 123456)
    # Extract the non-random part: (v & 0x1FFFFFFC) >> 2 should be 18
    import struct
    from src.config import INNER_KEY_MASK
    internal = (v & INNER_KEY_MASK) >> 2
    assert internal == INNER_SEQ_INIT, f"Expected internal {INNER_SEQ_INIT}, got {internal}"
    print(f"  first(0, 123456) = {v} (internal={internal}) [OK]")

    # Next call: (last & 0x1FFFFFFC) >> 2 = 18,
    # real = 18, next = 18*2 + 123456%108 = 36 + 12 = 48
    v2 = inner.get_my_next_seq(v, 123456)
    internal2 = (v2 & INNER_KEY_MASK) >> 2
    assert internal2 == 48, f"Expected internal 48, got {internal2}"
    print(f"  next({v}, 123456) = {v2} (internal={internal2}) [OK]")


def test_encrypt():
    print("=== encryptByteArray ===")
    seq = 18
    test_data = bytearray([0x0A, 0x0B, 0x06, 0x05, 0x01, 0x00, 0x00, 0x00])
    orig = list(test_data)
    encrypt_byte_array(seq, test_data)
    print(f"  input:  {orig}")
    print(f"  output: {list(test_data)}")


def test_amf3_roundtrip():
    print("=== AMF3 Round-trip ===")
    test_cases = [
        (42, [42]),
        (-10, [-10]),
        ("hello", ["hello"]),
        (True, [True]),
        (False, [False]),
        ([1, 2, 3], [[1, 2, 3]]),
        ({"key": "value"}, [{"key": "value"}]),
    ]
    for obj, expected in test_cases:
        w = Amf3Writer()
        w.write_object(obj)
        data = w.to_bytes()
        r = Amf3Reader(data)
        decoded = r.read_object()
        ok = (obj == decoded)
        tag = "OK" if ok else "FAIL"
        print(f"  [{tag}] {repr(obj)[:50]} -> {len(data)} bytes")
        if not ok:
            print(f"    got: {decoded!r}")


def test_amf3_nested():
    print("=== AMF3 Nested ===")
    obj = {"cmd": "test_cmd", "ext_seq": 18, "params": {"w": 1, "i": 2}}
    w = Amf3Writer()
    w.write_object(obj)
    data = w.to_bytes()
    print(f"  Serialized: {len(data)} bytes, hex={data.hex()[:80]}")
    r = Amf3Reader(data)
    decoded = r.read_object()
    assert decoded == obj, f"Mismatch: {decoded}"
    print(f"  Round-trip: OK")


def test_msgseq_reproducible():
    """Verify MsgSeq gives consistent output"""
    print("=== MsgSeq Reproducibility ===")
    s1 = MsgSeq("sid_abc123")
    s2 = MsgSeq("sid_abc123")
    uid = 555555
    now = 0
    for _ in range(5):
        n1 = s1.next(now, uid)
        n2 = s2.next(now, uid)
        assert n1 == n2, f"Not reproducible: {n1} vs {n2}"
        now = n1
    print(f"  Reproducible: OK (5 iterations)")


def test_legacy_push_string_references():
    """Legacy field names share AMF3's string table with field values."""
    # Prefix=EXT; names=mt2i,_cmd; values=["mt2i","x=20"],"MT250816_panel".
    raw = bytes.fromhex(
        "01"
        "096d743269"
        "095f636d64"
        "09050106000609783d3230"
        "061d4d543235303831365f70616e656c"
    )
    decoded = GameSocket()._parse_sys_fields(raw)
    assert decoded == {
        "mt2i": ["mt2i", "x=20"],
        "_cmd": "MT250816_panel",
    }, decoded
    print("=== Legacy EXT Push ===")
    print("  prefixed fields + shared string references [OK]")


def test_mt250816_score_choice():
    panel = {
        "_cmd": "MT250816_panel",
        "mt2i": [
            "2640_5236:2951_5119:2744_4986:3419_6006:3479_6018:3016_5736=0",
            "2640_5236:2951_5119:2744_4986:3419_6006:3479_6018:3016_5736=24792081",
        ],
    }
    assert parse_mt250816_scores(panel) == (0, 24792081)
    assert choose_mt250816_type(panel) == 1
    print("=== MT250816 Score Choice ===")
    print("  historical=0 current=24792081 -> type=1 [OK]")


def test_mt250816_script_contract():
    panel = {
        "_cmd": "MT250816_panel",
        "mt2i": ["route=0", "route=24792081"],
    }

    class FakeSocket:
        def __init__(self):
            self.connected = True
            self.sent = []
            self.router = None

        async def send_xt_message(self, ext_id, cmd, params, **kwargs):
            self.sent.append((ext_id, cmd, dict(params)))
            if cmd == "MT250816_panel":
                self.router.publish(panel)

    async def exercise(active=False):
        socket = FakeSocket()
        router = MessageRouter(socket)
        socket.router = router
        user_logs = []
        context = AppContext({}, SimpleNamespace(user_id="123"), None, socket, router, user_logs.append)
        if active:
            context.battle.begin_entry(-1)
            router.publish({"_cmd": 2303, "battleId": -10})
            router.publish({"_cmd": 2401, "battleUniqueId": 1, "pmmList": [
                {"battleView": {"pmmId": 123, "slotId": 0}},
            ]})
            router.publish({"_cmd": 2402, "pt": 0})
        await run_mt250816(context)
        return socket.sent, user_logs

    sent, user_logs = asyncio.run(exercise())
    assert sent == [
        (42, "MT250816_panel", {}),
        (42, "MT250816_t2c", {"index": 0, "type": 1}),
    ], sent
    active_sent, _logs = asyncio.run(exercise(active=True))
    assert active_sent[-1] == (13, "1404", {"turn": 0, "reqPSId": 0})
    assert user_logs == [
        "四象第一关:历史最佳=0，本次值=24792081,选择保留本次值"
    ]
    print("=== MT250816 Script Contract ===")
    print("  panel request -> comparison -> keep command + explicit UI log [OK]")


def test_send_message_parser():
    text = '#send={"id":42,"param":{"index":0,"type":0},"cmd":"MT250816_t2c"}|'
    assert parse_send_message(text) == SendMessage(
        id=42,
        cmd="MT250816_t2c",
        param={"index": 0, "type": 0},
    )

    invalid_cases = (
        (text.removeprefix("#send="), "开头"),
        (text.removesuffix("|"), "结尾"),
        ("#send=[]|", "必须是对象"),
        ('#send={"id":42,"cmd":"x"}|', "缺少必需字段"),
        ('#send={"id":true,"cmd":"x","param":{}}|', "id 必须是整数"),
        ('#send={"id":65536,"cmd":"x","param":{}}|', "0 到 65535"),
        ('#send={"id":42,"cmd":1,"param":{}}|', "cmd 必须是字符串"),
        ('#send={"id":42,"cmd":"x","param":[]}|', "param 必须是 JSON 对象"),
        ('#send={"id":42,"cmd":"x","param":{},"delay":1}|', "未知字段"),
    )
    for invalid, expected_reason in invalid_cases:
        try:
            parse_send_message(invalid)
        except SendMessageParseError as exc:
            assert expected_reason in str(exc), (invalid, str(exc))
        else:
            raise AssertionError(f"Expected parse failure: {invalid}")

    print("=== #send Parser ===")
    print("  valid message + strict format/type validation [OK]")


def test_send_message_file_parser():
    first = '#send={"id":1,"cmd":"first","param":{}}|'
    second = '#send={"id":42,"cmd":"second","param":{"value":2}}|'
    heartbeat = '#send={"id":18,"cmd":"55_2","param":{"time":431}}|'
    parsed = parse_send_message_lines(
        ["\n", "// comment\n", first + "\n", "  // comment\n", second]
    )
    assert [message.cmd for message in parsed] == ["first", "second"]

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "messages.txt"
        path.write_text(
            first + "\n" + heartbeat + "\n" + second + "\n", encoding="utf-8"
        )
        assert parse_send_message_file(path) == parsed

        heartbeat_only = Path(directory) / "heartbeat-only.txt"
        heartbeat_only.write_text(heartbeat + "\n", encoding="utf-8")
        try:
            parse_send_message_file(heartbeat_only)
        except SendMessageFileError as exc:
            assert "55_2" in str(exc)
            assert "没有可执行消息" in str(exc)
        else:
            raise AssertionError("Expected a heartbeat-only file to be empty")

        bad_path = Path(directory) / "bad.txt"
        bad_path.write_text(first + "\n#send=[]|\n", encoding="utf-8")
        try:
            parse_send_message_file(bad_path)
        except SendMessageFileError as exc:
            error = str(exc)
            assert str(bad_path) in error
            assert "第 2 行" in error
        else:
            raise AssertionError("Expected a filename/line-number parse error")

    print("=== #send File Parser ===")
    print("  blank/comment lines + 55_2 filtering + source locations [OK]")


def test_pipe_separated_message_file():
    text = "\n".join(
        [
            '||#send={"id":42,"param":{"id":0,"pi":0,"num":1,"i":0},"cmd":"FSE211210_1"}|',
            '|#send={"id":42,"param":{"id":1,"pi":0,"num":1,"i":0},"cmd":"FSE211210_1"}|',
            '|#send={"id":42,"param":{"id":14,"pi":400,"num":5,"i":0},"cmd":"FSE211210_1"}|',
            '|#send={"id":42,"param":{"id":4,"pi":240,"num":5,"i":0},"cmd":"FSE211210_1"}|',
            '|#send={"id":42,"param":{"id":5,"pi":480,"num":5,"i":0},"cmd":"FSE211210_1"}|',
            '|#send={"id":42,"param":{"id":17,"pi":400,"num":5,"i":0},"cmd":"FSE211210_1"}|',
            '|#send={"id":42,"param":{"id":7,"pi":400,"num":5,"i":0},"cmd":"FSE211210_1"}|',
            '|#send={"id":42,"param":{"id":8,"pi":320,"num":4,"i":0},"cmd":"FSE211210_1"}|',
            '|#send={"id":42,"param":{"id":10,"pi":200,"num":5,"i":0},"cmd":"FSE211210_1"}|',
            '|#send={"id":42,"param":{"id":11,"pi":600,"num":1,"i":0},"cmd":"FSE211210_1"}|',
            '|#send={"id":42,"param":{"id":16,"pi":1600,"num":10,"i":0},"cmd":"FSE211210_1"}|',
            '|#send={"id":42,"param":{"id":12,"pi":1200,"num":1,"i":0},"cmd":"FSE211210_1"}',
        ]
    )
    parsed = parse_pipe_separated_messages(text, "pipe-example.txt")
    assert len(parsed) == 12
    assert [message.param["id"] for message in parsed] == [
        0,
        1,
        14,
        4,
        5,
        17,
        7,
        8,
        10,
        11,
        16,
        12,
    ]
    assert all(message.cmd == "FSE211210_1" for message in parsed)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "pipe-messages.txt"
        path.write_text(text, encoding="utf-8")
        assert parse_send_message_file(path) == parsed

    single_pipe = (
        '|#send={"id":42,"param":{},"cmd":"a"}'
        '|#send={"id":42,"param":{},"cmd":"b"}'
    )
    assert [message.cmd for message in parse_pipe_separated_messages(single_pipe)] == ["a", "b"]

    print("=== Pipe-separated #send File ===")
    print("  optional leading pipes + empty segments + optional final pipe [OK]")


def test_message_sequence_directive_parser():
    text = (
        ' \n| |\n||#send={\n"id":42,"cmd":"first",'
        '"param":{"text":"a|b\\\"|c"}}| \n| #wait |\n'
        '#time  =  0.01 | |#send={"id":42,"cmd":"last","param":{}}|'
    )
    parsed = parse_pipe_separated_messages(text)
    assert [message.cmd for message in parsed] == ["first", "#wait", "#time", "last"]
    assert parsed[0].param["text"] == 'a|b"|c'
    assert parsed[1] == SendMessage(-1, "#wait", {})
    assert parsed[2] == SendMessage(-1, "#time", {"seconds": 0.01})
    assert parse_send_message("#wait") == parsed[1]
    assert parse_send_message(" #time = 0 | ") == SendMessage(-1, "#time", {"seconds": 0.0})
    for invalid in ("#time=-1|", "#time=nan|", "#time=inf|", "#time=|", "#time=abc|", "#wait extra|"):
        try:
            parse_pipe_separated_messages(invalid)
        except SendMessageFileError:
            pass
        else:
            raise AssertionError(f"Expected an invalid directive to fail: {invalid}")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "sequence.txt"
        path.write_text("// comment\n" + text, encoding="utf-8")
        assert parse_send_message_file(path) == parsed

    async def check_desktop_import():
        bridge = object.__new__(ipc_server.DesktopBridge)
        events = []

        async def emit(event, **payload):
            events.append((event, payload))

        bridge._emit = emit
        response = await bridge._dispatch("parse_message", {"text": text})
        assert response == {"count": 4}
        assert bridge.messages == tuple(parsed)
        payload = events[0][1]["messages"]
        assert [item["sequence"] for item in payload] == [1, 2, 3, 4]
        assert payload[1]["id"] == -1 and payload[1]["cmd"] == "#wait"
        steps = bridge._combination_steps([{"kind": "messages", "messages": payload}])
        assert steps[0].messages == tuple(parsed)

    asyncio.run(check_desktop_import())
    print("=== Message Sequence Directives ===")
    print("  multiline JSON + whitespace segments + quoted pipes + wait/time validation [OK]")


def test_message_sequence_wait_and_time():
    class FakeSocket:
        connected = True

        def __init__(self):
            self.sent = []
            self.context = None
            self.number = 0

        async def send_xt_message(self, ext_id, cmd, params):
            self.sent.append((cmd, asyncio.get_running_loop().time()))
            if cmd == "54_22":
                self.number += 1
                router = self.context.messages
                for message in (
                    {"_cmd": 2303, "battleId": self.number},
                    {"_cmd": 2401, "battleUniqueId": self.number},
                    {"_cmd": 2426}, {"_cmd": 2402, "pt": 0},
                ):
                    router.publish(message)
            if cmd == "1409" and self.number:
                async def finish():
                    await asyncio.sleep(0.01)
                    self.context.messages.publish({"_cmd": 2403, "battleUniqueId": self.number})
                asyncio.create_task(finish())

    def make_context(active=True):
        socket = FakeSocket()
        router = MessageRouter(socket)
        battle = BattleLifecycle()
        router.add_observer(battle.observe)
        started = asyncio.Event()

        def log(text):
            if text == "等待当前战斗结束":
                started.set()

        async def initialized():
            pass

        context = SimpleNamespace(
            socket=socket, messages=router, battle=battle,
            wait_for_player_initialization=initialized,
            assert_automation_allowed=lambda: None, log=log,
        )
        socket.context = context
        if active:
            for message in (
                {"_cmd": 2303, "battleId": 123},
                {"_cmd": 2401, "battleUniqueId": 456},
                {"_cmd": 2426}, {"_cmd": 2402, "pt": 0},
            ):
                router.publish(message)
        return context, started

    async def waiting_case(mode):
        context, started = make_context()
        messages = parse_pipe_separated_messages(
            '#send={"id":13,"cmd":"1409","param":{"useAiType":1}}|'
            '#wait|#time = 0.01|#send={"id":42,"cmd":"after","param":{}}|'
        )
        task = asyncio.create_task(execute_messages("account", context, messages, delay=0))
        await asyncio.wait_for(started.wait(), 1)
        assert [cmd for cmd, _time in context.socket.sent] == ["1409"]
        context.messages.publish({"_cmd": 2414, "reqId": 999})
        context.messages.publish({"_cmd": 2403, "battleUniqueId": 999})
        await asyncio.sleep(0)
        assert not task.done()  # Escape notification / another battle's end cannot release #wait.
        if mode == "cancel":
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Expected sequence cancellation")
        else:
            ended_at = asyncio.get_running_loop().time()
            if mode == "complete":
                context.messages.publish({"_cmd": 2403, "battleUniqueId": 456})
            elif mode == "disconnect":
                context.socket.connected = False
                context.messages.disconnected.set()
            else:
                context.battle.battle_epoch += 1
                context.messages.publish({"_cmd": 2405})
            results = await asyncio.wait_for(task, 2)
            if mode == "complete":
                assert [cmd for cmd, _time in context.socket.sent] == ["1409", "after"]
                assert context.socket.sent[-1][1] - ended_at >= 0.009
                assert len(results) == 4 and all(result.success for result in results)
                assert [result.sequence for result in results] == [1, 2, 3, 4]
            else:
                assert [cmd for cmd, _time in context.socket.sent] == ["1409"]
                assert len(results) == 2 and not results[-1].success
        assert not context.messages._subscribers

    async def other_cases():
        context, _started = make_context()
        results = await execute_messages("account", context, [parse_send_message("#time=0.01|")], delay=0)
        assert results[0].success and context.battle.phase == "active"
        assert context.socket.sent == []  # A pause must not escape an existing battle.

        context, _started = make_context(active=False)
        messages = parse_pipe_separated_messages(
            '#send={"id":15,"cmd":"54_22","param":{}}|'
            '#time=0|#send={"id":13,"cmd":"1409","param":{"useAiType":1}}|#wait|'
            '#send={"id":15,"cmd":"54_22","param":{}}|'
            '#send={"id":13,"cmd":"1409","param":{"useAiType":1}}|#wait|'
            '#send={"id":42,"cmd":"after","param":{}}|'
        )
        results = await asyncio.wait_for(execute_messages("account", context, messages, delay=0), 2)
        assert len(results) == 8 and all(result.success for result in results)
        assert [cmd for cmd, _time in context.socket.sent] == ["54_22", "1409", "54_22", "1409", "after"]
        assert not context.messages._subscribers

        results = await execute_messages("account", context, [parse_send_message("#wait|")], delay=0)
        assert results[0].success  # Already finished before reaching the marker.
        context.socket.sent.clear()
        results = await execute_messages("account", context, [
            SendMessage(42, "first", {}), parse_send_message("#wait|"),
            SendMessage(14, "1401", {}),
        ], delay=0)
        assert not results[0].success and context.socket.sent == []  # Preflight the entire sequence.

    for mode in ("complete", "disconnect", "cancel", "identity"):
        asyncio.run(waiting_case(mode))
    asyncio.run(other_cases())
    print("=== Message Sequence Execution ===")
    print("  natural battle end + seconds + no local TX/escape + cleanup + full preflight [OK]")


def test_account_config_models():
    legacy = load_account_specs({"account": "old", "password": "secret"})
    assert len(legacy) == 1 and legacy[0].config["account"] == "old"
    assert legacy[0].config["char_id"] == 0

    specs = load_account_specs(
        {
            "login_url": "https://example.invalid/login",
            "timeout": 20,
            "zone_index": 1025,
            "accounts": [
                {
                    "label": "A",
                    "account": "one",
                    "password": "p1",
                    "char_id": 1,
                },
                {
                    "label": "B",
                    "account": "two",
                    "password": "p2",
                    "char_id": 2,
                    "zone_index": 1001,
                },
            ],
        }
    )
    assert [spec.label for spec in specs] == ["A", "B"]
    assert [spec.config["char_id"] for spec in specs] == [1, 2]
    assert [spec.config["zone_index"] for spec in specs] == [1025, 1001]
    assert all(spec.config["timeout"] == 20 for spec in specs)

    try:
        load_account_specs(
            {
                "accounts": [
                    {"label": "same", "account": "a", "password": "p"},
                    {"label": "same", "account": "b", "password": "p"},
                ]
            }
        )
    except AccountConfigError as exc:
        assert "标签重复" in str(exc)
    else:
        raise AssertionError("Expected duplicate account labels to fail")

    try:
        load_account_specs(
            {"account": "bad", "password": "p", "char_id": -1}
        )
    except AccountConfigError as exc:
        assert "char_id" in str(exc)
    else:
        raise AssertionError("Expected a negative char_id to fail")

    print("=== Account Config ===")
    print("  legacy default + per-account charId/zones [OK]")


def test_fixed_role_login_contract():
    response = (
        "0,100000001,角色甲,1788413919000,0#6;7,14770|"
        "1,100000002,角色乙,1788413427000,0#6;7,163330|"
        "2,100000003,角色丙,1785551216000,0#6;7,14911"
    )
    roles = parse_role_response(response)
    selected = select_role(roles, 0)
    assert selected.user_id == "100000001"
    assert selected.nickname == "角色甲"
    params = build_login_params("200000000", "secret", 0)
    assert params["charId"] == "0"
    assert params["account"] == "200000000"
    assert params["password"] != "secret"

    login = parse_login_response(
        "<r><c>ok</c><sid>redacted</sid><d>200000000</d>"
        "<u>100000001</u><svr>host:8000:8100:example.invalid;</svr>"
        "<zn>1025 Test/0/1/0/0/0/0</zn></r>"
    )
    assert login.success
    assert login.duoduo_id == "200000000"
    assert login.user_id == selected.user_id
    try:
        select_role(roles, 3)
    except ValueError as exc:
        assert "可用值：0、1、2" in str(exc)
    else:
        raise AssertionError("Expected an unavailable charId to fail")

    print("=== Fixed Role Login ===")
    print("  query_role_info + charId + returned userId validation [OK]")


def test_explicit_zone_selection():
    other = ZoneData(1104, "Other", "example.invalid", 8100, 8000, domain="example.invalid")
    tcp_only = ZoneData(1, "TCP", "example.invalid", 0, 8000)
    ws_only = ZoneData(2, "WSS", "", 8100, domain="example.invalid")
    unavailable = ZoneData(3, "Unavailable", "", 0)
    result = LoginResult(success=True, zone_list=[other, tcp_only, ws_only, unavailable])
    assert account_session.select_zone(result, 99) is None
    assert account_session.select_zone(result, 1) is tcp_only
    assert account_session.select_zone(result, 2) is ws_only
    assert account_session.select_zone(result, 3) is None

    async def fake_http_login(*args, **kwargs):
        assert kwargs["timeout"] == 37
        return result

    async def run():
        config = {"account": "test", "password": "test", "zone_index": 1, "timeout": 37}
        _, selected = await account_session.authenticate(config)
        assert selected is tcp_only
        config["zone_index"] = 99
        try:
            await account_session.authenticate(config)
        except ConnectionError as exc:
            assert "99" in str(exc)
        else:
            raise AssertionError("Missing requested zone must fail instead of choosing another zone")

    original = account_session.http_login
    account_session.http_login = fake_http_login
    try:
        asyncio.run(run())
    finally:
        account_session.http_login = original
    print("  exact zone selection + TCP/WSS availability + missing-zone rejection [OK]")


def test_fixed_role_http_sequence():
    from urllib.parse import urlencode

    requests = []
    responses = [
        "0,100000001,角色甲,1788413919000,0#6;7,14770|"
        "1,100000002,角色乙,1788413427000,0#6;7,163330",
        "<r><c>ok</c><sid>redacted</sid><d>200000000</d>"
        "<u>100000001</u><svr>host:8000:8100:example.invalid;</svr>"
        "<zn>1025 Test/0/1/0/0/0/0</zn></r>",
    ]

    class FakeResponse:
        def __init__(self, text):
            self._text = text

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def raise_for_status(self):
            return None

        async def read(self):
            return self._text.encode("utf-8")

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def post(self, url, data, headers, timeout):
            requests.append((url, data, timeout.total))
            return FakeResponse(responses[len(requests) - 1])

    original_session = account_login.aiohttp.ClientSession
    account_login.aiohttp.ClientSession = FakeSession
    try:
        result = asyncio.run(
            account_login.http_login(
                "200000000",
                "secret",
                char_id=0,
                login_url="https://example.invalid/newLogin.jsp",
                register_url="https://example.invalid/newRegister.jsp",
                timeout=17,
            )
        )
    finally:
        account_login.aiohttp.ClientSession = original_session

    assert result.success and result.user_id == "100000001"
    assert [item[0].rsplit("/", 1)[-1] for item in requests] == [
        "newRegister.jsp",
        "newLogin.jsp",
    ]
    assert requests[0][1] == "type=query_role_info&duoduoId=200000000"
    assert requests[1][1] == urlencode(account_login.build_login_params("200000000", "secret", 0))
    assert result.session_id == "redacted"
    assert [item[2] for item in requests] == [17] * 2
    print("=== Fixed Role HTTP Sequence ===")
    print("  original role query + password login; no added SSO requests [OK]")


def test_password_login_flow():
    from aiohttp import web
    from unittest.mock import patch

    async def run():
        requests = []

        async def game(request):
            params = await request.post()
            account = params.get("account") or params["duoduoId"]
            assert request.headers["Origin"] == "https://aola.100bt.com"
            if request.path == "/roles":
                requests.append((account, "roles"))
                response = web.Response(text=f"0,{account}-default,默认角色|2,{account}-selected,指定角色")
                response.set_cookie("role_session", account, path="/")
                return response
            requests.append((account, "login"))
            assert request.cookies.get("role_session") == account
            assert dict(params) == account_login.build_login_params(account, "test-password", 2)
            if account == "forbidden":
                return web.Response(status=403, text="Forbidden")
            if account == "captcha":
                return web.Response(text='<html><script src="TCaptcha.js"></script>TencentCaptcha</html>')
            if account == "wrong-password":
                return web.Response(text="<r><c>invalid</c></r>")
            player = "other-role" if account == "wrong-role" else f"{account}-selected"
            sid = "" if account == "missing-sid" else f"{account}-sid"
            identity = "other-account" if account == "mismatch" else account
            return web.Response(text=f"<r><c>ok</c><sid>{sid}</sid><d>{identity}</d><u>{player}</u></r>")

        app = web.Application()
        app.router.add_post("/login", game)
        app.router.add_post("/roles", game)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://localhost:{site._server.sockets[0].getsockname()[1]}"
        try:
            original_post = account_login.aiohttp.ClientSession.post

            def local_post(session, url, *args, **kwargs):
                assert url in (base + "/login", base + "/roles"), url
                return original_post(session, url, *args, **kwargs)

            with tempfile.TemporaryDirectory() as tmp, \
                    patch.object(account_login, "PROJECT_ROOT", Path(tmp)), \
                    patch.object(account_login.aiohttp.ClientSession, "post", local_post):
                async def login(account):
                    return await account_login.http_login(account, "test-password", char_id=2,
                                                          login_url=base + "/login", register_url=base + "/roles")
                results = await asyncio.gather(login("account-a"), login("account-b"))
                for account, result in zip(("account-a", "account-b"), results):
                    assert result.success and result.session_id == f"{account}-sid"
                    assert [stage for name, stage in requests if name == account] == ["roles", "login"]
                for account, expected, blocked in (
                    ("forbidden", "403", True), ("captcha", "人机验证", True),
                    ("wrong-password", "密码错误", False), ("mismatch", "不匹配", False),
                    ("wrong-role", "角色校验失败", False),
                    ("missing-sid", "缺少 sid", False),
                ):
                    result = await login(account)
                    assert not result.success and expected in result.error_msg, result.error_msg
                    assert bool(result.blocked_reason) == blocked
                    assert [stage for name, stage in requests if name == account] == ["roles", "login"]
                saved = "".join(p.read_text("utf-8") for p in Path(tmp).rglob("*.response.txt"))
                assert "test-password" not in saved and account_login.md5_password("test-password") not in saved
        finally:
            await runner.cleanup()

    asyncio.run(run())
    print("  real loopback password login + isolated account cookies + rejection/role/sid validation [OK]")


def test_browser_password_sequence():
    from unittest.mock import patch
    import src.accounts.browser_login as browser_module
    import playwright.async_api as playwright_api

    calls = []
    page = SimpleNamespace()

    async def no_op(*args, **kwargs):
        pass

    page.goto = page.wait_for_load_state = no_op

    async def new_page():
        return page

    async def new_context():
        return SimpleNamespace(new_page=new_page)

    async def launch(**kwargs):
        assert kwargs["headless"] is False
        return SimpleNamespace(new_context=new_context, close=no_op)

    class FakePlaywright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        async def __aexit__(self, *args):
            return False

    async def post(page, url, params, stage, *args):
        calls.append((stage, params))
        if stage == "browser_role_query":
            text = "2,selected-role,角色"
        else:
            assert params == account_login.build_login_params("test-account", "test-password", 2)
            text = "<r><c>ok</c><sid>final-sid</sid><d>test-account</d><u>selected-role</u></r>"
        return text, None, text.encode("utf-8")

    with patch.object(playwright_api, "async_playwright", FakePlaywright), \
            patch.object(browser_module, "_post_form", post):
        result = asyncio.run(browser_module.browser_login("test-account", "test-password", 2,
                                                         "https://example.invalid/login", "https://example.invalid/roles"))
    assert result.success and result.session_id == "final-sid"
    assert [stage for stage, _ in calls] == ["browser_role_query", "browser_login"]
    assert calls[-1][1]["charId"] == "2"
    print("  isolated manual-browser path preserves original password login stages [OK]")


def test_login_failure_diagnostics():
    from unittest.mock import patch

    account, password = "200000000", "private-password"
    role = "0,100000001,角色甲"
    success = "<r><c>ok</c><sid>private-sid</sid><d>200000000</d><u>100000001</u></r>"
    captcha = (
        '<html><script src="TCaptcha.js"></script><script>TencentCaptcha; /WafCaptcha</script>'
        f'{account} {password} {account_login.md5_password(password)}'
        '<sid>private-sid</sid>{"token":"private-token"}</html>'
    )

    class FakeResponse:
        def __init__(self, text, status=200):
            self.body = text.encode("utf-8")
            self.status = status
            self.headers = {"Content-Type": "text/html", "Retry-After": "60", "Set-Cookie": "private-cookie"}
            self.url = "https://example.invalid/login?token=private-query-token"
            self.history = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def read(self):
            return self.body

        def raise_for_status(self):
            if self.status >= 400:
                raise account_login.aiohttp.ClientError(f"HTTP {self.status}")

    cases = [
        ([FakeResponse(role), FakeResponse(captcha)], "login", 200, "人机验证"),
        ([FakeResponse(role), FakeResponse("<html>Forbidden</html>", 403)], "login", 403, "403"),
        ([FakeResponse("Too Many Requests", 429)], "role_query", 429, "限流"),
        ([FakeResponse(role), FakeResponse("<html>Unavailable</html>", 503)], "login", 503, "HTML"),
        ([FakeResponse(role), asyncio.TimeoutError()], "login", None, "timed out"),
        ([FakeResponse(role), FakeResponse(success.replace("100000001", "100000002"))], "login", 200, "角色校验失败"),
        ([FakeResponse(role), FakeResponse(success)], None, None, None),
    ]
    for responses, stage, status, expected in cases:
        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def post(self, *args, **kwargs):
                response = responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(account_login, "PROJECT_ROOT", Path(tmp)), \
                patch.object(account_login.aiohttp, "ClientSession", FakeSession):
            result = asyncio.run(account_login.http_login(
                account, password, login_url="https://example.invalid/login",
                register_url="https://example.invalid/roles",
            ))
            files = list(Path(tmp).rglob("*.json"))
            if stage is None:
                assert result.success and not files
                continue
            assert not result.success and expected in result.error_msg
            assert bool(result.blocked_reason) == (status in (403, 429) or expected == "人机验证")
            assert len(files) == 1 and str(files[0]) in result.error_msg
            metadata = json.loads(files[0].read_text("utf-8"))
            assert metadata["stage"] == stage and metadata["http_status"] == status
            body_path = files[0].with_name(metadata["response_file"])
            saved = files[0].read_text("utf-8") + body_path.read_text("utf-8")
            for secret in (account, password, account_login.md5_password(password),
                           "private-sid", "private-token", "private-query-token", "private-cookie"):
                assert secret not in saved, secret
            if expected == "人机验证":
                assert metadata["captcha_markers"] == ["TCaptcha.js", "TencentCaptcha", "/WafCaptcha"]
            if status is None:
                assert metadata["final_url"] is None and metadata["body_bytes"] == 0
    print("  login failure evidence + CAPTCHA/HTTP/timeout analysis + redaction [OK]")


def test_bridge_account_config_persistence():
    bridge = object.__new__(ipc_server.DesktopBridge)
    bridge.config = {
        "login_url": "https://example.invalid/login",
        "timeout": 30,
        "accounts": [],
    }
    connections = [
        AccountConnection(
            AccountSpec(
                "账号 A",
                {
                    "login_url": "https://example.invalid/login",
                    "timeout": 45,
                    "label": "账号 A",
                    "account": "user-a",
                    "password": "secret-a",
                    "char_id": 2,
                    "zone_index": 1025,
                },
            )
        )
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        original_path = ipc_server.CONFIG_PATH
        ipc_server.CONFIG_PATH = Path(temp_dir) / "config.json"
        try:
            bridge._save_connections(connections)
            saved = json.loads(ipc_server.CONFIG_PATH.read_text("utf-8"))
            assert not ipc_server.CONFIG_PATH.with_suffix(".json.tmp").exists()
        finally:
            ipc_server.CONFIG_PATH = original_path
    specs = load_account_specs(saved)
    assert specs[0].label == "账号 A"
    assert specs[0].config["char_id"] == 2
    assert specs[0].config["timeout"] == 45
    print("=== C# Bridge Account Config ===")
    print("  validated atomic save + per-account override preservation [OK]")


def test_bridge_connection_status_is_incremental():
    class FakeConnection:
        def __init__(self, label, delay):
            self.spec = AccountSpec(label, {})
            self.delay = delay
            self.context = None
            self.last_error = ""
            self.state = "离线"

        @property
        def online(self):
            return self.context is not None

        async def connect(self):
            await asyncio.sleep(self.delay)
            self.context = SimpleNamespace(
                log_callback=None, wait_for_player_initialization=lambda: asyncio.sleep(0)
            )
            self.state = "在线"
            return True

        async def disconnect(self):
            self.context = None

    first = FakeConnection("A", 0)
    second = FakeConnection("B", 1.1)
    bridge = object.__new__(ipc_server.DesktopBridge)
    bridge.manager = SimpleNamespace(connections=[first, second])
    snapshots = []

    async def emit_accounts(force=False):
        snapshots.append((first.online, second.online))

    async def discard_log(_message, _category):
        return None

    bridge._emit_accounts = emit_accounts
    bridge._log = discard_log
    first.refresh_state = second.refresh_state = lambda: None

    async def run():
        monitor = asyncio.create_task(bridge._monitor_accounts())
        try:
            await bridge._run_connection_action("connect", ["A", "B"])
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    asyncio.run(run())
    assert snapshots[0] == (True, False), snapshots
    assert snapshots[-1] == (True, True), snapshots
    assert len(snapshots) == 2, snapshots
    print("=== C# Bridge Connection Status ===")
    print("  monitor publishes progress and the batch publishes final state [OK]")


def test_bridge_bulk_send_does_not_flood_events():
    from unittest.mock import patch
    from src.messaging.dispatcher import SendResult

    async def run(failure_count):
        labels = [f"account-{index}" for index in range(20)]
        batches = {
            label: [
                SendResult(label, sequence + 1, 42, f"command-{sequence}",
                           "2026-10-08T13:55:46+08:00", True)
                for sequence in range(31)
            ]
            for label in labels
        }
        for index in range(failure_count):
            label = labels[index]
            batches[label][0] = SendResult(
                label, 1, 42, "command-0", "2026-10-08T13:55:46+08:00",
                False, "模拟发送失败",
            )
        bridge = object.__new__(ipc_server.DesktopBridge)
        bridge.messages = ()
        bridge._targets = lambda selected: [(label, None) for label in selected]
        events = []

        async def dispatch(targets, messages):
            assert [label for label, _ in targets] == labels
            return batches

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "send.log"

            async def emit(event, **payload):
                # The full audit must already exist before UI reporting starts.
                records = [json.loads(line) for line in log_path.read_text("utf-8").splitlines()]
                assert len(records) == 620
                assert sum(not row["success"] for row in records) == failure_count
                events.append((event, payload))

            bridge._emit = emit
            with patch.object(ipc_server, "dispatch_to_accounts", dispatch), \
                    patch.object(ipc_server, "append_send_results", lambda data: append_send_results(data, log_path)):
                await bridge._run_send(labels)
            assert len(events) == 1 + min(failure_count, 5) + int(failure_count > 5)
            summary = events[0][1]["message"]
            assert "账号 20" in summary and f"成功 {620 - failure_count} 条" in summary
            assert f"失败 {failure_count} 条" in summary and str(log_path) in summary
            assert "发送耗时" in summary

    asyncio.run(run(0))
    asyncio.run(run(8))
    print("=== Bulk Send Reporting ===")
    print("  620 audit rows retained before UI; one success summary; at most five error samples [OK]")


def test_bridge_script_and_combination_reporting():
    from unittest.mock import patch
    from src.messaging.dispatcher import SendResult
    from src.scripting.composer import CombinationResult, CombinationStepResult

    async def run(kind, failure_count=0, cancel=False):
        labels = [f"account-{index}" for index in range(20)]
        old_logs = []
        original_callback = old_logs.append
        contexts = [SimpleNamespace(log_callback=original_callback) for _ in labels]
        connections = dict(zip(labels, (SimpleNamespace(context=item) for item in contexts)))
        bridge = object.__new__(ipc_server.DesktopBridge)
        bridge._connection = connections.__getitem__
        bridge._targets = lambda selected: [(label, connections[label].context) for label in selected]
        events = []
        entered = set()
        all_entered = asyncio.Event()
        finished = False

        async def script_run(context):
            index = context.index
            entered.add(index)
            if len(entered) == len(labels):
                all_entered.set()
            await all_entered.wait()
            for sequence in range(100):
                context.log_callback(f"script-detail-{sequence}")
            if cancel:
                await asyncio.Event().wait()
            if index < failure_count:
                raise ValueError(f"script-failure-{index}")

        for index, context in enumerate(contexts):
            context.index = index
        script = InteractionScript("scripts.reporting_test", "测试脚本", "", script_run)
        steps = (ScriptStep(script),)
        results = {}

        async def combination(targets, actual_steps, repetitions, interval):
            nonlocal finished
            assert targets == bridge._targets(labels)
            assert actual_steps == steps and repetitions == 100 and interval == 0.5
            for index, (label, context) in enumerate(targets):
                for sequence in range(100):
                    context.log_callback(f"combination-detail-{sequence}")
                failed = index < failure_count
                sends = tuple(
                    SendResult(label, sequence + 1, 42, f"command-{sequence}",
                               "2026-10-08T19:54:00+08:00", not (failed and sequence == 0),
                               f"send-failure-{index}" if failed and sequence == 0 else "")
                    for sequence in range(300)
                )
                sends += tuple(
                    SendResult(label, 301 + sequence, -1, "#wait",
                               "2026-10-08T19:54:00+08:00", not (failed and sequence == 0),
                               "wait failed" if failed and sequence == 0 else "")
                    for sequence in range(100)
                )
                results[label] = CombinationResult(
                    100, 99 if failed else 100,
                    (CombinationStepResult(100, 1, "script", "测试脚本", not failed,
                                           f"combination-failure-{index}" if failed else ""),),
                    sends,
                )
            all_entered.set()
            if cancel:
                await asyncio.Event().wait()
            finished = True
            return results

        with tempfile.TemporaryDirectory() as directory:
            runtime_path = Path(directory) / "runtime.log"
            send_path = Path(directory) / "send.log"
            handler = logging.FileHandler(runtime_path, encoding="utf-8")
            previous_level = ipc_server.logger.level
            ipc_server.logger.setLevel(logging.INFO)
            ipc_server.logger.addHandler(handler)

            async def emit(event, **payload):
                assert all(item.log_callback is original_callback for item in contexts)
                assert not old_logs
                if kind == "combination":
                    assert finished
                    records = [json.loads(line) for line in send_path.read_text("utf-8").splitlines()]
                    assert len(records) == 6000
                    assert sum(not item["success"] for item in records) == failure_count
                    assert {item["account_label"] for item in records} == set(labels)
                else:
                    assert len(entered) == 20
                events.append((event, payload))

            bridge._emit = emit
            try:
                with patch.object(ipc_server, "get_runtime_log_path", lambda: runtime_path), \
                        patch.object(ipc_server, "execute_combination_for_accounts", combination), \
                        patch.object(ipc_server, "append_send_results", lambda data: append_send_results(data, send_path)):
                    coroutine = (bridge._run_script(labels, script) if kind == "script" else
                                 bridge._run_combination(labels, steps, 100, 0.5))
                    task = asyncio.create_task(coroutine)
                    if cancel:
                        await all_entered.wait()
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        else:
                            raise AssertionError("task cancellation was swallowed")
                        assert not events
                    else:
                        await task
                        assert len(events) == 1 + min(failure_count, 5) + int(failure_count > 5)
                        summary = events[0][1]["message"]
                        assert "账号 20" in summary and f"成功 {20 - failure_count}" in summary
                        assert f"失败 {failure_count}" in summary and "执行耗时" in summary
                        assert str(runtime_path) in summary
                        if kind == "combination":
                            assert f"消息成功 {6000 - failure_count} 条，失败 {failure_count} 条" in summary
                            assert str(send_path) in summary
                        handler.flush()
                        text = runtime_path.read_text("utf-8")
                        assert text.count(f"{kind}-detail-") == 2000
                        for index in range(failure_count):
                            assert f"{kind}-failure-{index}" in text
                    assert all(item.log_callback is original_callback for item in contexts)
                    for context in contexts:
                        context.log_callback("restored")
                    assert old_logs == ["restored"] * 20
            finally:
                ipc_server.logger.removeHandler(handler)
                ipc_server.logger.setLevel(previous_level)
                handler.close()

    for kind in ("script", "combination"):
        asyncio.run(run(kind))
        asyncio.run(run(kind, failure_count=8))
        asyncio.run(run(kind, cancel=True))
    print("=== Script and Combination Reporting ===")
    print("  concurrent scripts; 6000 audit rows; bounded summaries; detail logs and callbacks survive failures/cancellation [OK]")


def test_bridge_bulk_disconnect_does_not_flood_events():
    async def run():
        bridge = object.__new__(ipc_server.DesktopBridge)
        bridge.manager = AccountManager.from_specs(
            [AccountSpec(f"offline-{index}", {}) for index in range(2008)]
        )
        bridge._last_statuses = tuple(
            (item.spec.label, "离线") for item in bridge.manager.connections
        )
        events = []

        async def emit(event, **payload):
            events.append((event, payload))

        bridge._emit = emit
        labels = [item.spec.label for item in bridge.manager.connections]
        await bridge._run_connection_action("disconnect", labels)
        assert len(events) == 1, len(events)
        assert events[0][0] == "log"
        assert "跳过 2008" in events[0][1]["message"]

        closed = []

        async def close():
            closed.append(True)

        # An offline context still owns resources and must be closed.
        bridge.manager.connections[0].context = SimpleNamespace(close=close)
        events.clear()
        await bridge._run_connection_action("disconnect", labels)
        assert closed == [True]
        assert len(events) == 1
        assert "成功 1，失败 0，跳过 2007" in events[0][1]["message"]

    asyncio.run(run())
    print("=== Bulk Disconnect Events ===")
    print("  2008 offline accounts emit one summary; stale context is closed [OK]")


def test_bridge_bulk_failures_keep_details_without_ui_flood():
    from unittest.mock import patch

    class FailedConnection:
        def __init__(self, index):
            self.spec = AccountSpec(f"failed-{index}", {})
            self.context = None
            self.online = False

        async def connect(self):
            raise ConnectionError("mock failure")

    async def run():
        bridge = object.__new__(ipc_server.DesktopBridge)
        bridge.manager = SimpleNamespace(connections=[FailedConnection(i) for i in range(20)])
        bridge._last_statuses = tuple((item.spec.label, "离线") for item in bridge.manager.connections)
        events = []

        async def emit(event, **payload):
            events.append((event, payload))

        bridge._emit = emit
        with patch.object(ipc_server.logger, "warning") as warning, \
                patch.object(ipc_server, "LOGIN_START_INTERVAL", 0):
            await bridge._run_connection_action("connect", [item.spec.label for item in bridge.manager.connections])
            assert warning.call_count == 20
        assert len(events) == 7, events
        assert "失败 20" in events[0][1]["message"]

    asyncio.run(run())
    print("=== Bulk Failure Details ===")
    print("  all failures reach runtime logging; UI receives a bounded summary [OK]")


def test_bridge_login_queue_covers_initialization_and_cancellation():
    from unittest.mock import patch

    async def run(cancel=False):
        active = peak = closed = 0
        starts = []
        gate = asyncio.Event()
        five_started = asyncio.Event()

        class FakeContext:
            def __init__(self, index):
                self.index = index
                self.log_callback = None
                self.released = False

            async def wait_for_player_initialization(self):
                nonlocal active
                if cancel:
                    await gate.wait()
                else:
                    await asyncio.sleep(0.1)
                    if self.index == 7:
                        raise ConnectionError("mock initialization failure")
                self.released = True
                active -= 1

            async def close(self):
                nonlocal active, closed
                await asyncio.sleep(0.01)
                if not self.released:
                    active -= 1
                    self.released = True
                closed += 1

        class FakeConnection:
            def __init__(self, index):
                self.index = index
                self.spec = AccountSpec(f"queued-{index}", {})
                self.context = None
                self.last_error = ""
                self.state = "离线"

            @property
            def online(self):
                return self.context is not None

            async def connect(self):
                nonlocal active, peak
                starts.append(asyncio.get_running_loop().time())
                active += 1
                peak = max(peak, active)
                if active == 5:
                    five_started.set()
                self.context = FakeContext(self.index)
                return True

            async def disconnect(self):
                context, self.context = self.context, None
                if context is not None:
                    await context.close()

        bridge = object.__new__(ipc_server.DesktopBridge)
        bridge.manager = SimpleNamespace(connections=[FakeConnection(i) for i in range(50)])
        bridge.tasks, bridge.task_labels = {}, {}
        bridge._last_statuses = ()
        bridge._connection_action = ""
        events = []

        async def emit(event, **payload):
            events.append((event, payload))

        bridge._emit = emit
        labels = [item.spec.label for item in bridge.manager.connections]
        with patch.object(ipc_server, "LOGIN_START_INTERVAL", 0.01), \
                patch.object(ipc_server.logger, "warning"):
            if cancel:
                await bridge._dispatch("connect", {"labels": labels})
                old = bridge.tasks["connection"]
                await asyncio.wait_for(five_started.wait(), 2)
                await bridge._dispatch("disconnect", {"labels": labels})
                new = bridge.tasks["connection"]
                assert new is not old and old.cancelled()
                await new
                await asyncio.sleep(0)
                assert len(starts) == 5 and closed == 5
                assert all(item.context is None for item in bridge.manager.connections)
            else:
                await bridge._run_connection_action("connect", labels)
                assert len(starts) == 50 and closed == 1
                assert sum(item.online for item in bridge.manager.connections) == 49
                assert all(b - a >= 0.008 for a, b in zip(starts, starts[1:]))
            assert active == 0 and peak == 5, (active, peak)

    asyncio.run(run())
    asyncio.run(run(cancel=True))
    print("=== Bounded Desktop Login Queue ===")
    print("  five slots cover initialization/cleanup; starts are paced; disconnect cancels queued logins [OK]")


def test_login_refusal_pauses_queue_and_preserves_ready_connections():
    from unittest.mock import patch

    async def run():
        starts, closed, events = [], [], []
        gate = asyncio.Event()

        class Context:
            def __init__(self, index):
                self.index = index
                self.log_callback = None
                self.socket = SimpleNamespace(connected=True)
                self.messages = SimpleNamespace(disconnected=asyncio.Event())

            async def wait_for_player_initialization(self):
                if self.index != 1:
                    await gate.wait()

            async def close(self):
                closed.append(self.index)
                self.socket.connected = False
                self.messages.disconnected.set()

        async def opener(config):
            index = config["index"]
            starts.append(index)
            if index == 3:
                raise account_login.LoginBlockedError("HTTP 403：服务器拒绝继续登录", "mock refused login")
            return Context(index)

        connections = [AccountConnection(AccountSpec(f"pause-{i}", {"index": i})) for i in range(20)]
        for connection in connections:
            original = connection.connect
            connection.connect = lambda original=original: original(opener=opener)
        connections[0].context = Context(0)
        bridge = object.__new__(ipc_server.DesktopBridge)
        bridge.manager = AccountManager(connections)
        bridge._last_statuses = ()

        async def emit(event, **payload):
            events.append((event, payload))

        bridge._emit = emit
        with patch.object(ipc_server, "LOGIN_START_INTERVAL", 0.01), \
                patch.object(ipc_server.logger, "warning"):
            await asyncio.wait_for(bridge._run_connection_action(
                "connect", [item.spec.label for item in connections]), 2)
        assert connections[0].online and connections[1].online
        assert all(item.context is None for item in connections[2:])
        assert connections[3].attempts == 1 and not connections[3].retryable
        assert len(starts) <= 5 and 19 not in starts
        assert 0 not in closed and 1 not in closed
        assert any("登录队列已暂停" in value.get("message", "") for _, value in events)

    asyncio.run(run())
    print("=== Login Refusal Pause ===")
    print("  refusal is not retried; queued work stops; ready sessions survive [OK]")


def test_manual_verification_queue():
    from unittest.mock import patch
    from src.accounts.browser_login import BrowserLoginStopped

    async def run(mode):
        starts, events, manual_calls, closed = [], [], [], []
        challenge = asyncio.Event()
        manual_started = asyncio.Event()
        human_completed = asyncio.Event()
        active = peak = 0

        class Context:
            def __init__(self, index):
                self.index = index
                self.socket = SimpleNamespace(connected=True)
                self.messages = SimpleNamespace(disconnected=asyncio.Event())

            async def wait_for_player_initialization(self):
                pass

            async def close(self):
                closed.append(self.index)
                self.socket.connected = False
                self.messages.disconnected.set()

        async def normal(config):
            index = config["index"]
            starts.append(index)
            if index in (1, 2):
                if index == 2:
                    challenge.set()
                await challenge.wait()
                raise account_login.LoginBlockedError("登录需要人机验证", "mock official challenge")
            return Context(index)

        async def interactive(config, *, interactive):
            nonlocal active, peak
            assert interactive is True
            manual_calls.append(config["index"])
            active += 1
            peak = max(peak, active)
            manual_started.set()
            try:
                await human_completed.wait()
                if mode == "closed":
                    raise BrowserLoginStopped("验证窗口已关闭")
                return Context(config["index"])
            finally:
                active -= 1

        connections = [AccountConnection(AccountSpec(f"manual-{i}", {"index": i})) for i in range(12)]
        for connection in connections:
            original = connection.connect
            connection.connect = lambda original=original, **kwargs: original(**kwargs) if kwargs else original(opener=normal)
        connections[0].context = Context(0)
        bridge = object.__new__(ipc_server.DesktopBridge)
        bridge.manager = AccountManager(connections)
        bridge._last_statuses = ()

        async def emit(event, **payload):
            events.append((event, payload))

        bridge._emit = emit
        with patch.object(ipc_server, "LOGIN_START_INTERVAL", 0.001), \
                patch.object(ipc_server, "open_context", interactive), \
                patch.object(ipc_server.logger, "warning"):
            task = asyncio.create_task(bridge._run_connection_action("connect", [c.spec.label for c in connections]))
            try:
                await asyncio.wait_for(manual_started.wait(), 2)
                frozen = len(starts)
                await asyncio.sleep(0.04)
                assert len(starts) == frozen and len(manual_calls) == 1 and not task.done()
                assert connections[0].online
                if mode == "cancel":
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                else:
                    human_completed.set()
                    await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert active == 0 and peak == 1
        assert connections[0].online and 0 not in closed
        if mode == "resume":
            assert len(manual_calls) == 2 and all(c.online for c in connections)
            assert any("人工验证后的连接已建立" in v.get("message", "") for _, v in events)
        else:
            assert len(manual_calls) == 1 and all(c.context is None for c in connections[1:])
            if mode == "closed":
                assert any("登录队列已暂停：人工验证或后续登录未完成" in v.get("message", "") for _, v in events)

    for mode in ("resume", "closed", "cancel"):
        asyncio.run(run(mode))
    print("  manual challenges are serialized; new accounts wait; success resumes; close/cancel drains [OK]")


def test_login_progress_reports_failures_before_batch_finishes():
    from unittest.mock import patch

    async def run():
        reported = asyncio.Event()
        gate = asyncio.Event()

        class Connection:
            def __init__(self, index):
                self.spec = AccountSpec(f"progress-{index}", {})
                self.context = None
                self.last_error = "mock failure"

            @property
            def online(self):
                return self.context is not None

            async def connect(self):
                if self.spec.label != "progress-3":
                    return False
                self.context = SimpleNamespace(
                    log_callback=None, wait_for_player_initialization=gate.wait)
                return True

            async def disconnect(self):
                self.context = None

        bridge = object.__new__(ipc_server.DesktopBridge)
        bridge.manager = SimpleNamespace(connections=[Connection(i) for i in range(4)])
        bridge._last_statuses = ()

        async def emit(event, **payload):
            if event == "log" and "登录进度" in payload["message"]:
                assert "失败 3" in payload["message"] and "未完成 1" in payload["message"]
                reported.set()

        bridge._emit = emit
        with patch.object(ipc_server, "LOGIN_START_INTERVAL", 0), \
                patch.object(ipc_server.logger, "warning"):
            task = asyncio.create_task(bridge._run_connection_action(
                "connect", [item.spec.label for item in bridge.manager.connections]))
            try:
                await asyncio.wait_for(reported.wait(), 6)
                assert not task.done()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    print("=== Live Login Progress ===")
    print("  failed/unfinished counts are visible while another account is waiting [OK]")


def test_account_connection_failure_isolation():
    class FakeContext:
        def __init__(self):
            self.socket = SimpleNamespace(connected=True)
            self.messages = SimpleNamespace(disconnected=asyncio.Event())
            self.closed = False

        async def close(self):
            self.closed = True
            self.socket.connected = False

    async def exercise():
        attempts = {"one": 0, "two": 0}

        async def opener(config):
            account = config["account"]
            attempts[account] += 1
            if account == "two":
                raise ConnectionError("login failed")
            return FakeContext()

        manager = AccountManager.from_specs(
            [
                AccountSpec("A", {"account": "one", "password": "p1"}),
                AccountSpec("B", {"account": "two", "password": "p2"}),
            ]
        )
        await manager.connect_all(
            max_concurrency=2,
            opener=opener,
            retries=3,
            retry_delay=0,
        )
        states = [connection.state for connection in manager.connections]
        online = [connection.online for connection in manager.connections]
        await manager.disconnect_all()
        return attempts, states, online

    attempts, states, online = asyncio.run(exercise())
    assert attempts == {"one": 1, "two": 3}
    assert states == ["在线", "登录失败"]
    assert online == [True, False]
    print("=== Account Connection Manager ===")
    print("  retry policy + single-account failure isolation [OK]")


def test_tcp_handshake_eof_and_connection_cleanup():
    from unittest.mock import AsyncMock, patch
    import src.network.socket_client as socket_module

    class Writer:
        def __init__(self):
            self.closes = 0

        def close(self):
            self.closes += 1

        async def wait_closed(self):
            pass

    async def run(policy, *, domain="example.invalid", login_error=False):
        reader = asyncio.StreamReader()
        reader.feed_data(policy)
        reader.feed_eof()
        writer = Writer()
        socket = GameSocket("test-session")
        fallback_calls = []

        async def connect_ws(*args):
            assert writer.closes == 1 and socket._reader is None
            assert socket._writer is None and not socket.connected
            fallback_calls.append(True)
            socket._connected = True
            return True

        socket.connect_ws = connect_ws
        socket.login = AsyncMock(side_effect=ValueError("模拟登录失败")) if login_error else AsyncMock(return_value={"_cmd": "logOK"})
        zone = SimpleNamespace(host="example.invalid", flash_port=8000, port=8100,
                               domain=domain, zone_index=1025, zone_name="test")
        with patch.object(socket_module.asyncio, "open_connection", AsyncMock(return_value=(reader, writer))), \
                patch.object(socket_module, "GameSocket", return_value=socket):
            if login_error:
                try:
                    await socket_module.login_and_connect(zone, "test-player", "test-session")
                except ValueError:
                    assert writer.closes == 1 and not socket.connected
                    assert not fallback_calls
                else:
                    raise AssertionError("Login exception was swallowed")
                return
            result = await socket_module.login_and_connect(zone, "test-player", "test-session")
        if policy.endswith(b"\0"):
            assert result is socket and socket.connected
            assert writer.closes == 0 and not fallback_calls
        elif domain:
            assert result is socket and socket.connected and len(fallback_calls) == 1
            assert writer.closes == 1
        else:
            assert result is None and not socket.connected and writer.closes == 1
            assert "安全策略" in socket.disconnect_reason and not fallback_calls
        await socket.close()
        assert writer.closes == 1

    async def cancel():
        reader = asyncio.StreamReader()
        writer = Writer()
        socket = GameSocket("test-session")
        reading = asyncio.Event()
        original_read = reader.readexactly

        async def read(count):
            reading.set()
            return await original_read(count)

        reader.readexactly = read
        with patch.object(socket_module.asyncio, "open_connection", AsyncMock(return_value=(reader, writer))):
            task = asyncio.create_task(socket.connect_tcp("example.invalid", 8000))
            await reading.wait()
            task.cancel()
            outcome = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(outcome[0], asyncio.CancelledError)
        assert writer.closes == 1 and not socket.connected
        assert socket._reader is None and socket._writer is None

    asyncio.run(run(b""))
    asyncio.run(run(b"<partial-policy"))
    asyncio.run(run(b"", domain=""))
    asyncio.run(run(b"<policy/>\0"))
    asyncio.run(run(b"<policy/>\0", login_error=True))
    asyncio.run(cancel())
    print("=== TCP Handshake Cleanup ===")
    print("  early/partial EOF cleanup before WSS; normal TCP retained; login error/cancellation cleanup [OK]")


def test_receive_error_does_not_fake_disconnect():
    class FakeSocket:
        def __init__(self):
            self.connected = True
            self.disconnect_reason = ""
            self.calls = 0

        async def recv_message(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("unsupported response")
            if self.calls == 2:
                return {"_cmd": "recovered"}
            self.connected = False
            self.disconnect_reason = "test transport closed"
            return None

    async def exercise():
        socket = FakeSocket()
        router = MessageRouter(socket)
        subscription = router.subscribe()
        router.start()
        try:
            message = await subscription.wait_for(
                lambda item: item.get("_cmd") == "recovered", timeout=1
            )
            await asyncio.wait_for(router.disconnected.wait(), timeout=1)
            return message, router
        finally:
            subscription.close()
            await router.stop()

    message, router = asyncio.run(exercise())
    assert message == {"_cmd": "recovered"}
    assert "unsupported response" in router.last_receive_error
    assert router.disconnect_reason == "test transport closed"

    disconnected = asyncio.Event()
    disconnected.set()
    connection = AccountConnection(
        AccountSpec("A", {"account": "a", "password": "p"}),
        context=SimpleNamespace(
            socket=SimpleNamespace(
                connected=False,
                disconnect_reason="WSS closed: code=1000",
            ),
            messages=SimpleNamespace(
                disconnected=disconnected,
                disconnect_reason="WSS closed: code=1000",
            ),
        ),
        state="在线",
    )
    connection.refresh_state()
    assert connection.state == "已断线"
    assert connection.last_error == "WSS closed: code=1000"
    print("=== Receive Diagnostics ===")
    print("  parse recovery + real disconnect reason propagation [OK]")


def test_battle_receive_trace_filter_and_payload():
    class FakeSocket:
        connected = True
        disconnect_reason = ""

    records = []

    class CaptureHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    context_logger = logging.getLogger("src.network.context")
    previous_level = context_logger.level
    handler = CaptureHandler()
    context_logger.addHandler(handler)
    context_logger.setLevel(logging.INFO)
    previous_receive_path = network_context._battle_receive_log_path
    previous_receive_error = network_context._battle_receive_log_error_reported
    try:
        with tempfile.TemporaryDirectory() as directory:
            network_context._battle_receive_log_path = None
            receive_path = configure_battle_receive_logging(Path(directory))
            router = MessageRouter(FakeSocket())
            router.publish(
                {
                    "_ext_id": 13,
                    "_cmd": "2402",
                    "pt": 7,
                    "nested": {"payload": b"\x00\xff"},
                }
            )
            router.publish({"_ext_id": 42, "_cmd": "ignored"})
            router.publish({"_ext_id": 15, "_cmd": "battle-entry", "ok": True})
            router.publish({"_ext_id": 16, "_cmd": "fight-ready", "ul": [1, -2]})
            router.publish({"_cmd": 2303, "battleId": 123})
            router.publish({"_cmd": "2411", "turn": 0, "skillId": 390111})
            router.publish({"_cmd": "preFightLoad", "ok": True})
            router.publish({"_cmd": "2200", "ignored": True})
            receive_records = [
                json.loads(line)
                for line in receive_path.read_text(encoding="utf-8").splitlines()
            ]
    finally:
        network_context._battle_receive_log_path = previous_receive_path
        network_context._battle_receive_log_error_reported = previous_receive_error
        context_logger.removeHandler(handler)
        context_logger.setLevel(previous_level)

    trace_records = [
        record for record in records if record.getMessage().startswith("RX BATTLE: ")
    ]
    assert len(trace_records) == 6
    payloads = [
        json.loads(record.getMessage().removeprefix("RX BATTLE: "))
        for record in trace_records
    ]
    assert [payload.get("_ext_id") for payload in payloads] == [13, 15, 16, None, None, None]
    assert payloads[0]["nested"]["payload"] == {"$bytes_hex": "00ff"}
    assert payloads[0]["pt"] == 7
    assert payloads[2]["ul"] == [1, -2]
    assert [payload["_cmd"] for payload in payloads[3:]] == [2303, "2411", "preFightLoad"]
    assert len(receive_records) == 6
    assert receive_records[0]["direction"] == "接收"
    assert receive_records[0]["cmd"] == "2402"
    assert receive_records[0]["param"]["pt"] == 7
    assert receive_records[0]["param"]["nested"]["payload"] == {
        "$bytes_hex": "00ff"
    }
    print("=== Battle Receive Trace ===")
    print("  full battle params persisted as flushed UTF-8 JSONL [OK]")


def test_player_context_initialization():
    class FakeSocket:
        connected = True
        disconnect_reason = ""

        def __init__(self):
            self.sent = []
            self.router = None
            self.login_response = {"lastLoginTime": 456}
            self.active_room_id = -1
            self.active_room_name = ""

        async def send_xt_message(self, ext_id, cmd, params):
            self.sent.append((ext_id, cmd, dict(params)))
            if cmd == "getStartInfo":
                # The real server may return this trio in any order.
                self.router.publish(decode_server_ext({"_cmd": "getStartInfo", "vfl": True}))
                self.router.publish(decode_server_ext({"_cmd": "11_1r", "vip": False}))
                self.router.publish(decode_server_ext({"_cmd": "getCurrentTime", "time": 1}))
            if cmd == "1101":
                self.router.publish(decode_server_ext({"_cmd": 2101, "simpleView": {}}))
            if cmd == "1212":
                self.router.publish(decode_server_ext({"_cmd": 2212, "pms": []}))
            if cmd == "55_9":
                self.router.publish(decode_server_ext({"_cmd": "55_9", "r": 1}))
            if cmd == "55_1":
                self.router.publish(decode_server_ext({"_cmd": "55_1", "time": 599}))

    async def exercise():
        socket = FakeSocket()
        router = MessageRouter(socket)
        socket.router = router
        context = AppContext(
            {},
            SimpleNamespace(user_id="123"),
            SimpleNamespace(),
            socket,
            router,
        )
        start_player_initialization(context, timeout=1)
        await context.wait_for_player_initialization()
        return socket.sent, context

    sent, context = asyncio.run(exercise())
    sent_commands = [(ext_id, cmd) for ext_id, cmd, _params in sent]
    assert sent_commands == [
        (1, "getCurrentTime"),
        (9, "11_1"),
        (1, "getStartInfo"),
        (13, "1101"),
        (13, "1212"),
        (18, "55_9"),
        (18, "55_1"),
    ]
    assert sent[2][2] == {
        "firstLogin": False,
        "lastLoginTime": 456,
    }
    assert context.socket.active_room_name == ""
    assert context.socket.active_room_id == -1
    assert context.player_initialized is True
    assert context.player_initialization_error == ""
    print("=== Player Context Initialization ===")
    print("  bootstrap -> 2101/2212 -> 55_9/55_1; room remains unset [OK]")


def test_server_downlink_frame_decoder():
    import struct

    def ext_frame(message):
        writer = Amf3Writer()
        writer.write_object(message)
        body = bytes((1,)) + writer.to_bytes()
        return struct.pack(">I", len(body)) + body

    class FakeWebSocket:
        def __init__(self, frame):
            self.frame = frame

        async def recv(self):
            return self.frame

    class FakeReader:
        def __init__(self, frame):
            self.parts = [frame[:4], frame[4:]]

        async def readexactly(self, _length):
            return self.parts.pop(0)

    async def exercise():
        frame_2101 = ext_frame({"_cmd": 2101, "simpleView": {"id": 123}, "pets": [1, 2]})
        frame_2212 = ext_frame({"_cmd": 2212, "pms": [{"uq": 9}]})

        wss = GameSocket()
        wss._ws = FakeWebSocket(frame_2101)
        wss_body, wss_sequence = await wss._recv_packet()

        tcp = GameSocket()
        tcp._reader = FakeReader(frame_2212)
        tcp_body, tcp_sequence = await tcp._recv_packet()
        return (
            wss._decode_server_body(wss_body),
            wss_sequence,
            tcp._decode_server_body(tcp_body),
            tcp_sequence,
        )

    decoded_2101, wss_sequence, decoded_2212, tcp_sequence = asyncio.run(exercise())
    assert decoded_2101["_cmd"] == 2101
    assert decoded_2101["simpleView"]["id"] == 123
    assert decoded_2212 == {"_cmd": 2212, "pms": [{"uq": 9}]}
    assert wss_sequence is None and tcp_sequence is None

    byte_message = GameSocket()._decode_server_body(b"\x02\x01\x02")
    assert byte_message == {"_type": 2, "data": b"\x01\x02"}

    evidence_path = Path("evidence/aola-recv-20260902-175711.jsonl")
    if evidence_path.exists():
        wanted = {
            "logOK",
            "2101",
            "2212",
            "2303",
            "2401",
            "2426",
            "2402",
            "2422",
        }
        decoded_commands = set()
        decoder = GameSocket()
        for line in evidence_path.open(encoding="utf-8"):
            record = json.loads(line)
            command = str(record.get("cmd", ""))
            if command not in wanted or command in decoded_commands:
                continue
            frame = bytes.fromhex(record["rawHex"])
            declared = struct.unpack(">I", frame[:4])[0]
            assert declared == len(frame) - 4
            decoded = decoder._decode_server_body(frame[4:])
            assert str(decoded.get("_cmd", "")) == command
            decoded_commands.add(command)
        assert decoded_commands == wanted
    print("=== Server Downlink Frames ===")
    print("  WSS/TCP + supplied 2101/2212/battle frames without sequence/XOR [OK]")


def test_headered_system_room_parser():
    socket = GameSocket()
    body = build_message(
        MSG_TYPE_SYS,
        DownCmd.joinOK,
        20758,
        pack_int(20758) + pack_utf("spacetimechallenge9") + pack_int(100) + pack_utf(""),
    )
    parsed = socket._parse_headered_system(body)
    assert parsed == {
        "_action": DownCmd.joinOK,
        "_room_id": 20758,
        "_cmd": "joinOK",
        "room_id": 20758,
        "room_name": "spacetimechallenge9",
        "max_users": 100,
        "join_data": "",
    }
    decoded = socket._decode_server_body(body)
    assert decoded["room_id"] == 20758
    assert socket.active_room_id == 20758
    sentinel = GameSocket()
    sentinel._decode_server_body(
        build_message(
            MSG_TYPE_SYS,
            DownCmd.joinOK,
            -1,
            pack_int(15732) + pack_utf("guangqipalace") + pack_int(100) + pack_utf(""),
        )
    )
    assert sentinel.active_room_id == 15732
    failed = socket._parse_headered_system(
        build_message(MSG_TYPE_SYS, DownCmd.joinKO, 20758, pack_utf("房间不可用"))
    )
    assert failed["_cmd"] == "joinKO"
    assert failed["msg"] == "房间不可用"
    mismatch = socket._decode_server_body(
        build_message(
            MSG_TYPE_SYS,
            DownCmd.joinOK,
            15732,
            pack_int(22125) + pack_utf("guangqipalace") + pack_int(100) + pack_utf(""),
        )
    )
    assert "不一致" in mismatch["parse_error"]
    assert socket.active_room_id == -1
    print("=== System Room Responses ===")
    print("  joinOK header/payload equality + mismatch fail-closed [OK]")


def test_dynamic_room_join_context():
    class FakeSocket:
        connected = True
        disconnect_reason = ""

        def __init__(self, room_id=20170):
            self.router = None
            self.sent = []
            self.active_room_id = 999
            self.active_room_name = "old-room"
            self.room_id = room_id

        async def send_xt_message(self, ext_id, cmd, params):
            self.sent.append((ext_id, cmd, dict(params)))
            if params["room"] != "missing-room":
                self.router.publish(
                    {
                        "_cmd": "joinOK",
                        "_room_id": self.room_id,
                        "room_id": self.room_id,
                        "room_name": params["room"],
                    }
                )
            else:
                self.router.publish({"_cmd": "joinKO", "msg": "房间不可用"})

    async def exercise():
        socket = FakeSocket()
        router = MessageRouter(socket)
        socket.router = router
        context = AppContext(
            {}, SimpleNamespace(user_id="123"), SimpleNamespace(), socket, router
        )
        await context.enter_room("chunqingmuchang", timeout=1)
        await context.enter_room("chunqingmuchang", timeout=1)
        assert socket.active_room_id == 20170
        try:
            await context.enter_room("missing-room", timeout=1)
        except ConnectionError as exc:
            error = str(exc)
        else:
            raise AssertionError("joinKO should fail")
        assigned = []
        for room_id in (15732, 22125):
            session_socket = FakeSocket(room_id)
            session_router = MessageRouter(session_socket)
            session_socket.router = session_router
            session_context = AppContext(
                {}, SimpleNamespace(user_id="123"), SimpleNamespace(),
                session_socket, session_router,
            )
            await session_context.enter_room("guangqipalace", timeout=1)
            assigned.append(session_socket.active_room_id)
        return socket, error, assigned

    socket, error, assigned = asyncio.run(exercise())
    assert [params["room"] for _id, _cmd, params in socket.sent] == [
        "chunqingmuchang",
        "missing-room",
    ]
    assert socket.active_room_id == -1 and socket.active_room_name == ""
    assert "房间不可用" in error
    assert assigned == [15732, 22125]
    print("=== Dynamic Room Context ===")
    print("  server room id + same-room reuse + joinKO invalidation [OK]")


def test_battle_reuses_confirmed_active_room():
    class FakeSocket:
        connected = True

        def __init__(self):
            self.sent = []
            self.router = None
            self.active_room_id = 20758
            self.active_room_name = "spacetimechallenge9"

        async def send_xt_message(self, ext_id, cmd, params):
            self.sent.append((ext_id, cmd, dict(params)))
            if cmd == "54_22":
                self.router.publish(decode_server_ext({"_cmd": 2303, "battleId": -10}))
                self.router.publish(decode_server_ext({"_cmd": 2401}))
                self.router.publish(decode_server_ext({"_cmd": 2426}))
                self.router.publish(decode_server_ext({"_cmd": 2402, "pt": 0}))
                self.router.publish(decode_server_ext({"_cmd": 2403}))

    async def exercise():
        socket = FakeSocket()
        router = MessageRouter(socket)
        socket.router = router
        battle = BattleLifecycle()
        router.add_observer(battle.observe)

        async def wait_for_player_initialization():
            return None

        context = SimpleNamespace(
            socket=socket,
            messages=router,
            user_id="123",
            wait_for_player_initialization=wait_for_player_initialization,
            enter_room=lambda *_args, **_kwargs: None,
            battle=battle,
            assert_automation_allowed=lambda: None,
            log=lambda _message: None,
        )
        results = await dispatch_to_accounts(
            [("battle", context)],
            [SendMessage(15, "54_22", {"handler": "TP230818"})],
            delay=0,
        )
        return socket.sent, results, battle

    sent, results, battle = asyncio.run(exercise())
    assert [item[1] for item in sent] == ["54_22"]
    assert results["battle"][0].success is True
    assert battle.phase == "idle" and battle.battle_epoch == 1
    print("=== Confirmed Room Reuse ===")
    print("  no handler mapping; existing dynamic room + final 2403 [OK]")


def test_active_room_is_used_for_ext_requests():
    async def exercise():
        socket = GameSocket("room-test")
        socket._connected = True
        socket.my_user_id = 123
        socket.active_room_id = 20758
        bodies = []

        async def capture(body):
            bodies.append(body)

        socket._send_raw_locked = capture
        await socket.send_xt_message(15, "54_22", {})
        await socket.send_xt_message(15, "54_22", {}, room_id=321)
        socket.active_room_id = -1
        await socket.send_xt_message(15, "54_22", {})
        return bodies

    import struct

    bodies = asyncio.run(exercise())
    assert struct.unpack(">i", bodies[0][3:7])[0] == 20758
    assert struct.unpack(">i", bodies[1][3:7])[0] == 321
    assert struct.unpack(">i", bodies[2][3:7])[0] == -1
    print("=== Active Room EXT Routing ===")
    print("  implicit active room + explicit room override [OK]")


def test_battle_entry_then_ordered_sends():
    class FakeSocket:
        connected = True

        def __init__(self, fail_entry=False, finish_entry=True):
            self.fail_entry = fail_entry
            self.finish_entry = finish_entry
            self.sent = []
            self.router = None
            self.active_room_id = -1
            self.active_room_name = ""
            self.ready_at = None

        def publish(self, message):
            self.router.publish(decode_server_ext(message))

        async def send_xt_message(self, ext_id, cmd, params):
            self.sent.append((cmd, asyncio.get_running_loop().time()))
            if cmd != "54_22":
                return
            if self.fail_entry:
                self.publish({"_cmd": 2303, "msg": "entry rejected"})
                return
            self.publish({"_cmd": 2303, "battleId": -94617668})
            if self.finish_entry:
                async def complete_entry():
                    await asyncio.sleep(0.08)
                    self.publish({"_cmd": 2401, "pmmList": [
                        {"battleView": {"pmmId": 123, "slotId": 0}},
                    ]})
                    self.publish({"_cmd": 2426})
                    self.publish({"_cmd": 2402, "pt": 0})
                    self.ready_at = asyncio.get_running_loop().time()
                asyncio.create_task(complete_entry())

    def make_context(socket):
        router = MessageRouter(socket)
        socket.router = router
        battle = BattleLifecycle()
        router.add_observer(battle.observe)

        async def initialized():
            return None

        context = SimpleNamespace(
            socket=socket,
            messages=router,
            user_id="123",
            battle=battle,
            wait_for_player_initialization=initialized,
            assert_automation_allowed=lambda: None,
            log=lambda _message: None,
        )
        context.auto_battle = AutoBattle(context)
        router.add_observer(context.auto_battle.observe)
        return context

    messages = [
        SendMessage(15, "54_22", {"handler": "MT250816_t2f"}),
        SendMessage(42, "74_1", {"eventId": 46358}),
        SendMessage(16, "preFightLoad", {"msg": "123:1"}),
        SendMessage(13, "1401", {"turn": 7}),
        SendMessage(13, "1403", {"turn": 0}),
        SendMessage(13, "1401", {"turn": 7}),
        SendMessage(13, "1408", {}),
        SendMessage(13, "1404", {"turn": 2}),
        SendMessage(42, "afterBattle", {}),
    ]

    async def exercise(socket):
        context = make_context(socket)
        results = await dispatch_to_accounts(
            [("account", context)], messages, delay=0.03
        )
        return context, results["account"]

    socket = FakeSocket()
    context, results = asyncio.run(exercise(socket))
    assert [cmd for cmd, _time in socket.sent] == [message.cmd for message in messages]
    assert all(result.success for result in results)
    assert [result.sequence for result in results] == list(range(1, len(messages) + 1))
    assert socket.sent[1][1] < socket.ready_at <= socket.sent[2][1]
    assert all(
        later[1] - earlier[1] >= 0.02
        for earlier, later in zip(socket.sent[2:], socket.sent[3:])
    ), socket.sent
    assert context.battle.phase == "active"

    async def exercise_captured_prefix():
        prefix_socket = FakeSocket()
        prefix_context = make_context(prefix_socket)
        prefix_messages = [
            SendMessage(42, "MT250816_t2op", {"index": 0}),
            SendMessage(15, "54_22", {"handler": "MT250816_t2f"}),
            SendMessage(16, "preFightLoad", {"msg": "123:1"}),
        ]
        batches = await dispatch_to_accounts(
            [("prefix", prefix_context)], prefix_messages, delay=0
        )
        return prefix_socket.sent, batches["prefix"]

    prefix_sent, prefix_results = asyncio.run(exercise_captured_prefix())
    assert [cmd for cmd, _time in prefix_sent] == [
        "MT250816_t2op", "54_22", "preFightLoad"
    ]
    assert all(result.success for result in prefix_results)

    failed_socket = FakeSocket(fail_entry=True)
    _context, failed = asyncio.run(exercise(failed_socket))
    assert [cmd for cmd, _time in failed_socket.sent] == ["54_22", "74_1"]
    assert failed[0].cmd == "54_22" and "entry rejected" in failed[0].error

    import src.messaging.dispatcher as dispatcher_module

    timeout_before = dispatcher_module._BATTLE_RESPONSE_TIMEOUT
    dispatcher_module._BATTLE_RESPONSE_TIMEOUT = 0.03
    try:
        incomplete_socket = FakeSocket(finish_entry=False)
        _context, incomplete = asyncio.run(exercise(incomplete_socket))
    finally:
        dispatcher_module._BATTLE_RESPONSE_TIMEOUT = timeout_before
    assert [cmd for cmd, _time in incomplete_socket.sent] == ["54_22", "74_1"]
    assert incomplete[0].cmd == "54_22" and not incomplete[0].success
    print("=== Battle Entry Then Ordered Sends ===")
    print("  no room + entry gate + source order + common delay [OK]")


def test_externally_started_battle_actions():
    class FakeSocket:
        connected = True
        active_room_id = -1

        def __init__(self):
            self.sent = []
            self.router = None
            self.ready_at = None

        def publish(self, message):
            self.router.publish(decode_server_ext(message))

        def enter_battle(self):
            self.publish({"_cmd": 2303, "battleId": -94617668})
            self.publish({"_cmd": 2401, "battleUniqueId": 123, "pmmList": [
                {"battleView": {"pmmId": 241627493, "slotId": 0}},
            ]})
            self.publish({"_cmd": 2426})
            self.publish({"_cmd": 2402, "pt": 0})
            self.ready_at = asyncio.get_running_loop().time()

        async def send_xt_message(self, ext_id, cmd, params):
            self.sent.append((cmd, asyncio.get_running_loop().time()))
            if cmd == "matchRequest":
                async def complete_match():
                    await asyncio.sleep(0.01)
                    self.enter_battle()
                asyncio.create_task(complete_match())

    def make_context(socket):
        router = MessageRouter(socket)
        socket.router = router
        battle = BattleLifecycle()
        router.add_observer(battle.observe)

        async def initialized():
            return None

        context = SimpleNamespace(
            socket=socket,
            messages=router,
            battle=battle,
            user_id="241627493",
            wait_for_player_initialization=initialized,
            assert_automation_allowed=lambda: None,
            log=lambda _message: None,
        )
        context.auto_battle = AutoBattle(context)
        router.add_observer(context.auto_battle.observe)
        return context

    async def already_entered():
        socket = FakeSocket()
        context = make_context(socket)
        socket.enter_battle()
        results = await dispatch_to_accounts(
            [("account", context)],
            [SendMessage(13, "1409", {"useAiType": 1})],
            delay=0,
        )
        return socket, context, results["account"]

    socket, context, results = asyncio.run(already_entered())
    assert [cmd for cmd, _time in socket.sent] == ["1409"]
    assert all(result.success for result in results)
    assert context.auto_battle.enabled

    async def match_then_act():
        socket = FakeSocket()
        context = make_context(socket)
        results = await dispatch_to_accounts(
            [("account", context)],
            [
                SendMessage(42, "matchRequest", {}),
                SendMessage(13, "1409", {"useAiType": 1}),
            ],
            delay=0,
        )
        return socket, context, results["account"]

    socket, context, results = asyncio.run(match_then_act())
    assert [cmd for cmd, _time in socket.sent] == ["matchRequest", "1409"]
    assert socket.sent[1][1] >= socket.ready_at
    assert all(result.success for result in results)
    assert context.auto_battle.enabled

    async def battle_continues_after_end_request():
        socket = FakeSocket()
        context = make_context(socket)
        socket.enter_battle()
        first = await dispatch_to_accounts(
            [("account", context)], [SendMessage(14, "53_1", {})], delay=0
        )
        assert first["account"][0].success
        assert context.battle.phase == "idle"
        socket.publish({"_cmd": 2414})
        socket.publish({"_cmd": 2405, "reqId": 94617668, "reqSId": 0})
        second = await dispatch_to_accounts(
            [("account", context)],
            [SendMessage(13, "1409", {"useAiType": 1})],
            delay=0,
        )
        return socket, context, second["account"]

    import src.messaging.dispatcher as dispatcher
    original_timeout = dispatcher._BATTLE_RESPONSE_TIMEOUT
    dispatcher._BATTLE_RESPONSE_TIMEOUT = 0.02
    try:
        socket, context, results = asyncio.run(battle_continues_after_end_request())
    finally:
        dispatcher._BATTLE_RESPONSE_TIMEOUT = original_timeout
    assert [cmd for cmd, _time in socket.sent] == ["1404", "53_1", "1409"]
    assert context.battle.entry_ready
    assert results[0].success

    ended = BattleLifecycle()
    for message in (
        {"_cmd": "2303", "battleId": 13843},
        {"_cmd": "2401", "battleUniqueId": 1434811},
        {"_cmd": "2426"},
        {"_cmd": "2402", "pt": 0},
    ):
        ended.observe(message)
    ended.finish_without_confirmation()
    ended.observe({"_cmd": "2403", "battleUniqueId": 1434811})
    ended.observe({"_cmd": "2405", "reqId": 94617668})
    assert ended.phase == "idle"

    async def without_entry():
        socket = FakeSocket()
        context = make_context(socket)
        return socket, await dispatch_to_accounts(
            [("account", context)],
            [SendMessage(13, "1409", {"useAiType": 1})],
            delay=0,
        )

    original_timeout = dispatcher._BATTLE_RESPONSE_TIMEOUT
    dispatcher._BATTLE_RESPONSE_TIMEOUT = 0.02
    try:
        socket, results = asyncio.run(without_entry())
    finally:
        dispatcher._BATTLE_RESPONSE_TIMEOUT = original_timeout
    assert socket.sent == []
    assert not results["account"][0].success
    assert "等待当前账号进入战斗超时" in results["account"][0].error

    print("=== Externally Started Battle Actions ===")
    print("  existing/match battles proceed; absent battle blocks 1409 [OK]")


def test_auto_battle_uses_first_available_skill():
    class FakeSocket:
        connected = True

        def __init__(self):
            self.sent = []

        async def send_xt_message(self, ext_id, cmd, params):
            self.sent.append((ext_id, cmd, params))

    async def scenario():
        socket = FakeSocket()
        battle = BattleLifecycle()
        context = SimpleNamespace(
            socket=socket,
            battle=battle,
            user_id="241627493",
            assert_automation_allowed=lambda: None,
            log=lambda _message: None,
        )
        auto = AutoBattle(context)

        def publish(message):
            battle.observe(message)
            auto.observe(message)

        players = [
            {"battleView": {"pmmId": 241627493, "slotId": 11, "pmList": [
                {"skills": [
                    {"id": 100, "ssi": 0, "pp": "0/4", "cd": 0},
                    {"id": 101, "ssi": 1, "pp": "2/4", "cd": 0},
                ]}
            ]}},
            {"battleView": {"pmmId": 94617668, "slotId": 0, "pmList": [
                {"skills": [{"id": 200, "ssi": 0, "pp": "4/4", "cd": 0}]}
            ]}},
        ]
        publish({"_cmd": "2303", "battleId": 13843})
        publish({"_cmd": "2401", "battleUniqueId": 1434811, "pmmList": players})
        publish({"_cmd": "2426"})
        publish({"_cmd": "2402", "pt": 0, "desc": "11-1-0-1-0-0-2-6"})
        auto.enable()
        await asyncio.sleep(0)
        assert socket.sent == [(13, "1401", {
            "turn": 0, "reqPSId": 11, "tarPSId": 0, "tarSId": 0,
            "ussi": -1, "isAuto": False, "skillId": 101,
        })]

        publish({"_cmd": "2402", "pt": 0, "desc": "11-1-0-1-0-0-2-6"})
        await asyncio.sleep(0)
        assert len(socket.sent) == 1

        players[0]["battleView"]["pmList"][0]["skills"][0]["pp"] = "3/4"
        publish({"_cmd": "2411", "pmmList": players})
        publish({"_cmd": "2402", "pt": 1, "desc": "11-1-0-1-0-0-4-6"})
        await asyncio.sleep(0)
        assert len(socket.sent) == 2
        assert socket.sent[1][2]["skillId"] == 100
        assert socket.sent[1][2]["turn"] == 1

        publish({"_cmd": "2403", "battleUniqueId": 1434811})
        publish({"_cmd": "2402", "pt": 2, "desc": "11-1-0-1-0-0-6-6"})
        await asyncio.sleep(0)
        assert len(socket.sent) == 2

    asyncio.run(scenario())
    print("=== Auto Battle ===")
    print("  current turn + first available skill + no duplicate + stop [OK]")


def test_auto_battle_replaces_fainted_pet():
    AutoBattle(SimpleNamespace()).observe({"_cmd": 2101})

    class FakeSocket:
        connected = True

        def __init__(self):
            self.sent = []

        async def send_xt_message(self, ext_id, cmd, params):
            self.sent.append((ext_id, cmd, params))

    def pet(pet_id, bag_slot, hp=100, lock=0):
        return {
            "pmSId": bag_slot,
            "pmSView": {"i": pet_id, "c0": hp, "lock": lock},
            "skills": [{"id": 101, "ssi": 0, "pp": "4/4", "cd": 0}],
        }

    async def scenario(slot):
        socket = FakeSocket()
        battle = BattleLifecycle()
        context = SimpleNamespace(
            socket=socket, battle=battle, user_id="241627493",
            assert_automation_allowed=lambda: None, log=lambda _message: None,
        )
        auto = AutoBattle(context)

        def publish(message):
            battle.observe(message)
            auto.observe(message)

        def player(pets):
            return {"PSId": 0, "battleView": {
                "pmmId": 241627493, "slotId": slot, "pmList": pets,
            }}

        def turn(number):
            publish({"_cmd": 2402, "pt": number,
                     "desc": f"{slot}-1-0-1-0-0-2-6"})

        # Full roster arrives before the active-pet-only updates. IDs are
        # deliberately unrelated to backpack positions and list order.
        publish({"_cmd": 2303, "battleId": 13843, "pmmList": [player([
            pet(900, 0), pet(100, 4), pet(700, 2),
            pet(600, 1, lock=2), pet(800, 3),
        ])]})
        opponent_slot = 11 if slot == 0 else 0
        opponent = {"battleView": {
            "pmmId": 94617668, "slotId": opponent_slot,
            "pmList": [pet(50, 0)],
        }}
        publish({"_cmd": 2401, "pmmList": [player([pet(900, 0)]), opponent]})
        publish({"_cmd": 2426})
        turn(0)
        auto.enable()
        await asyncio.sleep(0)
        assert socket.sent[-1][2]["reqPSId"] == slot
        update = player([pet(900, 0)])
        update["pmInBagList"] = [{"i": 700, "c0": 0, "lock": 0}]
        publish({"_cmd": 2423, "pmmList": [update]})

        publish({"_cmd": 2422, "ss": str(opponent_slot), "xx": str(slot)})
        await asyncio.sleep(0)
        assert len(socket.sent) == 1  # Only ss requests a replacement.

        dying = {"_cmd": 2422, "ss": f"{slot}-", "xx": ""}
        publish(dying)
        publish(dying)
        await asyncio.sleep(0)
        assert socket.sent[-1] == (13, "1403", {
            "turn": 0, "reqPSId": slot, "pmId": 800,
        })
        assert len(socket.sent) == 2
        turn(0)
        await asyncio.sleep(0)
        assert len(socket.sent) == 2  # Wait for replacement confirmation.

        # Active updates reset pmSId to zero; keep the original backpack rank.
        publish({"_cmd": 2413, "reqId": 241627493, "reqSId": slot,
                 "pmmList": [player([pet(800, 0)])]})
        turn(1)
        await asyncio.sleep(0)
        assert socket.sent[-1][1] == "1401"
        publish(dying)
        await asyncio.sleep(0)
        assert socket.sent[-1] == (13, "1403", {
            "turn": 1, "reqPSId": slot, "pmId": 100,
        })

        publish({"_cmd": 2413, "reqId": 241627493, "reqSId": slot,
                 "pmmList": [player([pet(100, 0)])]})
        turn(2)
        await asyncio.sleep(0)
        publish(dying)
        await asyncio.sleep(0)
        assert socket.sent[-1][1:] == ("1403", {
            "turn": 2, "reqPSId": slot, "pmId": 600,
        })  # Its lock expired; lower ranks are dead.

        publish({"_cmd": 2413, "reqId": 241627493, "reqSId": slot,
                 "pmmList": [player([pet(600, 0)])]})
        count = len(socket.sent)
        publish(dying)
        await asyncio.sleep(0)
        assert len(socket.sent) == count  # No living reserve remains.
        publish({"_cmd": 2403})
        auto.enable()
        await asyncio.sleep(0)
        assert len(socket.sent) == count

        # Starting another battle clears the previous roster and requests.
        publish({"_cmd": 2303, "battleId": 13844, "pmmList": [player([
            pet(901, 0), pet(801, 1),
        ])]})
        publish({"_cmd": 2401, "pmmList": [player([pet(901, 0)]), opponent]})
        publish({"_cmd": 2426})
        turn(0)
        publish(dying)
        await asyncio.sleep(0)
        assert len(socket.sent) == count  # Disabled automation records the request.
        auto.enable()
        auto.disable()
        await asyncio.sleep(0)
        assert len(socket.sent) == count
        auto.enable()
        await asyncio.sleep(0)
        assert socket.sent[-1][1:] == ("1403", {
            "turn": 0, "reqPSId": slot, "pmId": 801,
        })

        publish({"_cmd": 2403})
        publish({"_cmd": 2303, "battleId": 13845, "pmmList": [player([
            pet(902, 0), pet(802, 3, lock=1),
            pet(702, 1, lock=3), pet(602, 2, lock=1),
        ])]})
        publish({"_cmd": 2401, "pmmList": [player([pet(902, 0)]), opponent]})
        publish({"_cmd": 2426})
        turn(0)
        publish(dying)
        auto.enable()
        await asyncio.sleep(0)
        assert socket.sent[-1][1:] == ("1403", {
            "turn": 0, "reqPSId": slot, "pmId": 602,
        })  # All locked: smallest lock, then backpack order, as in the client.

        publish({"_cmd": 2413, "pmmList": [player([pet(602, 0)])]})
        count = len(socket.sent)
        publish(dying)
        publish({"_cmd": 2403})
        await asyncio.sleep(0)
        assert len(socket.sent) == count  # A queued replacement cannot outlive battle end.

    asyncio.run(scenario(0))
    asyncio.run(scenario(11))
    print("=== Auto Battle Replacement ===")
    print("  backpack order + HP/lock + dedup + confirmation + stop/reset [OK]")


def test_battle_end_request_without_final_response():
    class FakeSocket:
        connected = True

        def __init__(self, fail_end=False):
            self.fail_end = fail_end
            self.router = None
            self.sent = []
            self.active_room_id = -1
            self.active_room_name = ""
            self.entries = 0

        def publish(self, message):
            self.router.publish(decode_server_ext(message))

        async def send_xt_message(self, ext_id, cmd, params):
            if cmd == "1404" and self.fail_end:
                raise ConnectionError("end send failed")
            self.sent.append((ext_id, cmd, dict(params)))
            if cmd == "54_22":
                self.entries += 1
                self.publish({"_cmd": 2303, "battleId": -241627493})
                self.publish({"_cmd": 2401, "battleUniqueId": self.entries, "pmmList": [
                    {"battleView": {"pmmId": 123, "slotId": 0}},
                ]})
                self.publish({"_cmd": 2426})
                self.publish({"_cmd": 2402, "pt": 0})
            elif cmd == "1404":
                self.publish({"_cmd": 2414, "reqId": 123})

    def make_context(socket):
        router = MessageRouter(socket)
        socket.router = router
        battle = BattleLifecycle()
        router.add_observer(battle.observe)

        async def initialized():
            return None

        context = SimpleNamespace(
            socket=socket,
            messages=router,
            user_id="123",
            battle=battle,
            wait_for_player_initialization=initialized,
            assert_automation_allowed=lambda: None,
            log=lambda _message: None,
        )
        context.auto_battle = AutoBattle(context)
        router.add_observer(context.auto_battle.observe)
        return context

    entry = [SendMessage(15, "54_22", {"handler": "MT250816_t2f"})]

    async def repeated_entries():
        socket = FakeSocket()
        context = make_context(socket)
        first = await dispatch_to_accounts([("account", context)], entry, delay=0)
        second = await dispatch_to_accounts(
            [("account", context)],
            [
                SendMessage(42, "MT250816_t2rc", {"index": 0}),
                SendMessage(42, "MT250816_t2op", {"index": 0}),
                *entry,
            ],
            delay=0,
        )
        socket.publish({"_cmd": 2403, "battleUniqueId": 1})
        return socket, context, first, second

    socket, context, first, second = asyncio.run(repeated_entries())
    assert all(result.success for result in first["account"] + second["account"])
    assert [cmd for _id, cmd, _params in socket.sent] == [
        "54_22", "1404", "MT250816_t2rc", "MT250816_t2op", "54_22"
    ]
    assert socket.sent[1] == (13, "1404", {"turn": 0, "reqPSId": 0})
    assert context.battle.phase == "active" and context.battle.battle_epoch == 2
    assert context.battle.battle_unique_id == 2

    async def repeated_combination():
        socket = FakeSocket()
        context = make_context(socket)

        async def choose_score(script_context):
            await script_context.socket.send_xt_message(
                42, "MT250816_t2c", {"index": 0, "type": 0}
            )

        script = InteractionScript("scripts.test_score", "成绩选择", "", choose_score)
        result = await execute_combination(
            "account",
            context,
            [MessageBatchStep(tuple(entry)), ScriptStep(script)],
            repetitions=2,
            repeat_interval=0,
            message_delay=0,
        )
        return socket.sent, result

    combination_sent, combination_result = asyncio.run(repeated_combination())
    assert combination_result.success
    assert [cmd for _id, cmd, _params in combination_sent] == [
        "54_22", "MT250816_t2c", "1404", "54_22", "MT250816_t2c"
    ]

    async def explicit_end_request():
        socket = FakeSocket()
        context = make_context(socket)
        first = await dispatch_to_accounts(
            [("account", context)],
            entry + [SendMessage(13, "1404", {"turn": 0, "reqPSId": 0})],
            delay=0,
        )
        second = await dispatch_to_accounts([("account", context)], entry, delay=0)
        return socket, first, second

    explicit_socket, first, second = asyncio.run(explicit_end_request())
    assert all(result.success for result in first["account"] + second["account"])
    assert [cmd for _id, cmd, _params in explicit_socket.sent] == [
        "54_22", "1404", "54_22"
    ]

    async def failed_end_request():
        socket = FakeSocket(fail_end=True)
        context = make_context(socket)
        first = await dispatch_to_accounts([("account", context)], entry, delay=0)
        second = await dispatch_to_accounts([("account", context)], entry, delay=0)
        return socket, first, second

    failed_socket, first, second = asyncio.run(failed_end_request())
    assert first["account"][0].success
    assert not second["account"][0].success
    assert [cmd for _id, cmd, _params in failed_socket.sent] == ["54_22"]
    print("=== Battle End Without Final 2403 ===")
    print("  1404 before next batch; no duplicate; stale 2403 and send failure handled [OK]")


def test_session_clock_and_owned_message_rejection():
    class FakeSocket:
        connected = True
        disconnect_reason = ""

        def __init__(self):
            self.sent = []
            self.active_room_id = -1
            self.active_room_name = ""

        async def send_xt_message(self, ext_id, cmd, params):
            self.sent.append((ext_id, cmd, dict(params)))

    async def exercise_clock():
        import time

        socket = FakeSocket()
        router = MessageRouter(socket)
        logs = []
        context = AppContext(
            {}, SimpleNamespace(user_id="123"), SimpleNamespace(),
            socket, router, log_callback=logs.append,
        )
        context.session_clock.interval = 0.03
        context.session_clock.start(599)
        await asyncio.sleep(0.04)
        first_values = [params["time"] for _id, cmd, params in socket.sent if cmd == "55_2"]
        time.sleep(0.065)
        await asyncio.sleep(0.005)
        await context.session_clock.stop()
        values = [params["time"] for _id, cmd, params in socket.sent if cmd == "55_2"]
        router.publish({"_cmd": "55_2", "cw": -2})
        try:
            context.assert_automation_allowed()
        except RuntimeError as exc:
            block_error = str(exc)
        else:
            raise AssertionError("cw should block account automation")
        return first_values, values, block_error, logs

    first_values, values, block_error, logs = asyncio.run(exercise_clock())
    assert first_values == [598]
    assert values == [598, 596]
    assert "cw" in block_error and any("cw" in item for item in logs)

    class ImportSocket:
        connected = True

        def __init__(self):
            self.sent = []

        async def send_xt_message(self, ext_id, cmd, params):
            self.sent.append((ext_id, cmd, dict(params)))

    async def imported_session_message():
        socket = ImportSocket()
        context = SimpleNamespace(socket=socket)
        result = await dispatch_to_accounts(
            [("imported", context)],
            [
                SendMessage(42, "before", {}),
                SendMessage(18, "55_2", {"time": 431}),
            ],
            delay=0,
        )
        return socket, result

    import_socket, result = asyncio.run(imported_session_message())
    assert import_socket.sent == []
    assert result["imported"][0].success is False
    assert "会话服务管理" in result["imported"][0].error
    print("=== Session Clock ===")
    print("  monotonic 55_2 without catch-up + cw block + import rejection [OK]")


def test_dispatcher_account_isolation():
    messages = [
        SendMessage(1, "first", {"value": 1}),
        SendMessage(42, "fail", {"value": 2}),
        SendMessage(42, "third", {"value": 3}),
    ]

    class FakeSocket:
        def __init__(self, label, fail=False):
            self.label = label
            self.fail = fail
            self.connected = True
            self.sent = []

        async def send_xt_message(self, ext_id, cmd, params):
            params[":ext_seq;"] = self.label
            if self.fail and cmd == "fail":
                raise RuntimeError("rejected")
            self.sent.append((ext_id, cmd, dict(params)))

    async def exercise():
        socket_a = FakeSocket("A")
        socket_b = FakeSocket("B", fail=True)
        contexts = [
            ("A", SimpleNamespace(socket=socket_a)),
            ("B", SimpleNamespace(socket=socket_b)),
        ]
        results = await dispatch_to_accounts(contexts, messages, delay=0)
        return socket_a, socket_b, results

    socket_a, socket_b, results = asyncio.run(exercise())
    assert [item[1] for item in socket_a.sent] == ["first", "fail", "third"]
    assert [item[1] for item in socket_b.sent] == ["first"]
    assert [result.success for result in results["A"]] == [True, True, True]
    assert [result.success for result in results["B"]] == [True, False]
    assert all(":ext_seq;" not in message.param for message in messages)
    assert socket_a.sent[0][2][":ext_seq;"] == "A"
    assert socket_b.sent[0][2][":ext_seq;"] == "B"

    with tempfile.TemporaryDirectory() as directory:
        log_path = Path(directory) / "send.log"
        append_send_results(results, log_path)
        records = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(records) == 5
        assert set(records[0]) == {
            "account_label",
            "sequence",
            "ext_id",
            "cmd",
            "timestamp",
            "success",
            "error",
        }
        assert "password" not in records[0] and "session_id" not in records[0]

    print("=== Multi-account Dispatcher ===")
    print("  concurrent accounts + order + isolation + safe audit log [OK]")


def test_game_socket_serializes_concurrent_sends():
    class FakeWebSocket:
        def __init__(self):
            self.active_sends = 0
            self.max_active_sends = 0
            self.packets = []

        async def send(self, packet):
            self.active_sends += 1
            self.max_active_sends = max(
                self.max_active_sends, self.active_sends
            )
            try:
                await asyncio.sleep(0.01)
                self.packets.append(packet)
            finally:
                self.active_sends -= 1

    async def exercise():
        socket = GameSocket("concurrent-send-test")
        transport = FakeWebSocket()
        socket._ws = transport
        socket._connected = True
        socket.my_user_id = 123456
        await asyncio.gather(
            socket.send_xt_message(42, "first", {}),
            socket.send_xt_message(42, "second", {}),
            socket.send_xt_message(42, "third", {}),
        )
        return transport

    transport = asyncio.run(exercise())
    assert len(transport.packets) == 3
    assert transport.max_active_sends == 1
    print("=== GameSocket Send Serialization ===")
    print("  concurrent callers serialize per account [OK]")


def test_combined_message_and_script_execution():
    events = []

    class FakeSocket:
        connected = True

        async def send_xt_message(self, ext_id, cmd, params):
            params[":ext_seq;"] = len(events) + 1
            events.append(("message", ext_id, cmd))

    async def run_script(_context):
        events.append(("script", "interactive"))

    first = SendMessage(1, "first", {"value": 1})
    second = SendMessage(42, "second", {"value": 2})
    third = SendMessage(42, "third", {"value": 3})
    script = InteractionScript(
        "scripts.test_combination",
        "组合测试脚本",
        "",
        run_script,
    )
    steps = [
        MessageBatchStep((first, second)),
        ScriptStep(script),
        MessageBatchStep((third,)),
    ]
    context = SimpleNamespace(socket=FakeSocket())

    result = asyncio.run(
        execute_combination(
            "组合账号",
            context,
            steps,
            repetitions=2,
            repeat_interval=0,
            message_delay=0,
        )
    )
    assert events == [
        ("message", 1, "first"),
        ("message", 42, "second"),
        ("script", "interactive"),
        ("message", 42, "third"),
        ("message", 1, "first"),
        ("message", 42, "second"),
        ("script", "interactive"),
        ("message", 42, "third"),
    ]
    assert result.success
    assert result.completed_repetitions == 2
    assert len(result.step_results) == 6
    assert [item.sequence for item in result.send_results] == list(range(1, 7))
    assert all(":ext_seq;" not in message.param for message in (first, second, third))

    failed_events = []

    class FailingSocket:
        connected = True

        async def send_xt_message(self, _ext_id, cmd, _params):
            failed_events.append(("message", cmd))

    async def fail_script(_context):
        failed_events.append(("script", "fail"))
        raise RuntimeError("script rejected")

    failed_script = InteractionScript(
        "scripts.fail_combination",
        "失败脚本",
        "",
        fail_script,
    )
    failed_result = asyncio.run(
        execute_combination(
            "失败账号",
            SimpleNamespace(socket=FailingSocket()),
            [
                MessageBatchStep((first,)),
                ScriptStep(failed_script),
                MessageBatchStep((third,)),
            ],
            repetitions=3,
            repeat_interval=0,
            message_delay=0,
        )
    )
    assert failed_events == [("message", "first"), ("script", "fail")]
    assert not failed_result.success
    assert failed_result.completed_repetitions == 0
    assert failed_result.step_results[-1].error == "RuntimeError: script rejected"

    timed_sends = []

    class TimedSocket:
        connected = True

        async def send_xt_message(self, _ext_id, cmd, _params):
            timed_sends.append((cmd, asyncio.get_running_loop().time()))

    async def send_script_packet(script_context):
        await script_context.socket.send_xt_message(42, "script_packet", {})

    timed_script = InteractionScript(
        "scripts.timed_combination", "间隔测试脚本", "", send_script_packet
    )
    timed_context = SimpleNamespace(socket=TimedSocket())
    timed_result = asyncio.run(
        execute_combination(
            "间隔账号",
            timed_context,
            [
                MessageBatchStep((first,)),
                ScriptStep(timed_script),
                ScriptStep(timed_script),
                MessageBatchStep((third,)),
            ],
            message_delay=0.02,
        )
    )
    assert timed_result.success
    assert [cmd for cmd, _ in timed_sends] == [
        "first", "script_packet", "script_packet", "third"
    ]
    assert all(
        later[1] - earlier[1] >= 0.015
        for earlier, later in zip(timed_sends, timed_sends[1:])
    )
    print("=== Combined Message and Script Execution ===")
    print("  ordered repeats + stop-on-error + step spacing [OK]")


def test_multi_account_combination_isolation():
    class FakeSocket:
        connected = True

        def __init__(self, events):
            self.events = events

        async def send_xt_message(self, _ext_id, cmd, _params):
            self.events.append(("message", cmd))

    async def account_script(context):
        context.events.append(("script", context.label))
        if context.label == "B":
            raise RuntimeError("account B rejected")

    script = InteractionScript(
        "scripts.multi_account_combination",
        "多账号组合测试",
        "",
        account_script,
    )
    first = SendMessage(1, "before", {})
    second = SendMessage(42, "after", {})
    steps = [
        MessageBatchStep((first,)),
        ScriptStep(script),
        MessageBatchStep((second,)),
    ]
    contexts = []
    for label in ("A", "B"):
        events = []
        contexts.append(
            (
                label,
                SimpleNamespace(
                    label=label,
                    events=events,
                    socket=FakeSocket(events),
                ),
            )
        )

    results = asyncio.run(
        execute_combination_for_accounts(
            contexts,
            steps,
            repetitions=2,
            repeat_interval=0,
            message_delay=0,
        )
    )
    assert contexts[0][1].events == [
        ("message", "before"),
        ("script", "A"),
        ("message", "after"),
        ("message", "before"),
        ("script", "A"),
        ("message", "after"),
    ]
    assert contexts[1][1].events == [
        ("message", "before"),
        ("script", "B"),
    ]
    assert results["A"].success
    assert results["A"].completed_repetitions == 2
    assert not results["B"].success
    assert results["B"].completed_repetitions == 0
    print("=== Multi-account Combination ===")
    print("  concurrent accounts + per-account failure isolation [OK]")


def test_battle_sequence_script():
    class FakeSocket:
        connected = True

        def __init__(self, mode):
            self.mode = mode
            self.sent = []
            self.router = None

        async def send_xt_message(self, ext_id, cmd, params):
            self.sent.append(cmd)
            if self.mode == "send_failure" and cmd == "prepare_a":
                raise ConnectionError("test send failed")
            if self.mode == "after_failure" and cmd == "after_b":
                raise ConnectionError("test after failed")
            if cmd == "trigger":
                assert self.router._subscribers  # Subscribe before the trigger write.
                if self.mode == "rejected":
                    self.router.publish({"_cmd": 2303, "msg": "test entry rejected"})
                elif self.mode != "timeout":
                    self.router.publish({"_cmd": 2303, "battleId": 123})
                    self.router.publish({"_cmd": 2401, "battleUniqueId": 456})
                    if self.mode in {"immediate", "after_failure"}:
                        self.router.publish({"_cmd": 2426})
                        self.router.publish({"_cmd": 2402, "pt": 0})

    async def fast_combination(*args, **kwargs):
        return await execute_combination(*args, **kwargs, message_delay=0)

    original = (
        battle_sequence_script.BEFORE_STEPS,
        battle_sequence_script.AFTER_STEPS,
        battle_sequence_script.WAIT_TIMEOUT_SECONDS,
        battle_sequence_script.execute_combination,
    )
    assert any(
        script.module_name == "scripts.auto_battle"
        for script in discover_scripts()
    )

    async def exercise(mode):
        socket = FakeSocket(mode)
        router = MessageRouter(socket)
        socket.router = router
        waiting = asyncio.Event()

        def log(text):
            if text == "前置步骤已提交，等待本次战斗完整就绪":
                waiting.set()

        context = AppContext(
            {}, SimpleNamespace(user_id="200000000"), None, socket, router, log
        )
        context.player_initialized = True
        if mode == "existing":
            for message in (
                {"_cmd": 2303, "battleId": 1},
                {"_cmd": 2401, "battleUniqueId": 2},
                {"_cmd": 2426}, {"_cmd": 2402, "pt": 0},
            ):
                router.publish(message)

        task = asyncio.create_task(battle_sequence_script.run(context))
        if mode in {"delayed", "disconnect", "identity", "ended", "cancel", "blocked"}:
            await asyncio.wait_for(waiting.wait(), 1)
            assert socket.sent == ["prepare_a", "prepare_b", "trigger"]
            assert not task.done()
            if mode == "delayed":
                router.publish({"_cmd": 2403, "battleUniqueId": 999})
                await asyncio.sleep(0.01)
                assert not task.done() and context.battle.phase == "active"
                router.publish({"_cmd": 2426})
                await asyncio.sleep(0)
                assert not task.done()  # Three entry messages are insufficient.
                router.publish({"_cmd": 2402, "pt": 0})
            elif mode == "disconnect":
                socket.connected = False
                router.disconnected.set()
            elif mode == "identity":
                router.publish({"_cmd": 2403, "battleUniqueId": 456})
                router.publish({"_cmd": 2303, "battleId": 789})
            elif mode == "ended":
                router.publish({"_cmd": 2403, "battleUniqueId": 456})
            elif mode == "blocked":
                context.automation_block_reason = "test automation blocked"
                router.publish({"_cmd": 2405})
            else:
                task.cancel()

        error = None
        try:
            await asyncio.wait_for(task, 2)
        except (RuntimeError, ValueError, ConnectionError, asyncio.CancelledError) as exc:
            error = exc
        if mode in {"delayed", "immediate"}:
            assert error is None, error
            assert socket.sent == [
                "prepare_a", "prepare_b", "trigger", "after_a", "after_b", "after_c"
            ]
            assert context.battle.entry_ready  # After batches must not send 1404.
        elif mode in {"empty", "existing"}:
            assert error is not None and socket.sent == []
        elif mode == "send_failure":
            assert "test send failed" in str(error)
            assert socket.sent == ["prepare_a"]
        elif mode == "after_failure":
            assert "test after failed" in str(error)
            assert socket.sent[-1] == "after_b" and "after_c" not in socket.sent
        else:
            assert error is not None, mode
            assert socket.sent == ["prepare_a", "prepare_b", "trigger"]
            expected = {
                "rejected": "test entry rejected",
                "timeout": "未确认本次战斗完整就绪",
                "disconnect": "连接断开",
                "identity": "战斗身份已改变",
                "ended": "本次战斗在就绪前已经结束",
                "blocked": "test automation blocked",
            }
            if mode in expected:
                assert expected[mode] in str(error), error
        assert not router._subscribers

    try:
        battle_sequence_script.execute_combination = fast_combination
        battle_sequence_script.BEFORE_STEPS = ()
        battle_sequence_script.AFTER_STEPS = ()
        asyncio.run(exercise("empty"))
        battle_sequence_script.BEFORE_STEPS = (
            MessageBatchStep((SendMessage(42, "prepare_a", {}), SendMessage(42, "prepare_b", {}))),
            MessageBatchStep((SendMessage(42, "trigger", {}),)),
        )
        battle_sequence_script.AFTER_STEPS = (
            MessageBatchStep((SendMessage(42, "after_a", {}), SendMessage(42, "after_b", {}))),
            MessageBatchStep((SendMessage(42, "after_c", {}),)),
        )
        for mode in (
            "delayed", "immediate", "rejected", "disconnect", "identity", "ended",
            "cancel", "blocked", "existing", "send_failure", "after_failure",
        ):
            asyncio.run(exercise(mode))
        battle_sequence_script.WAIT_TIMEOUT_SECONDS = 0.02
        asyncio.run(exercise("timeout"))
    finally:
        (
            battle_sequence_script.BEFORE_STEPS,
            battle_sequence_script.AFTER_STEPS,
            battle_sequence_script.WAIT_TIMEOUT_SECONDS,
            battle_sequence_script.execute_combination,
        ) = original
    print("=== Battle Sequence Script ===")
    print("  message steps + new entry gate + no escape + failures/cancel cleanup [OK]")


def test_battle_escape_slot_and_confirmation():
    class FakeSocket:
        connected = True

        def __init__(self, user_id, slot, reject):
            self.user_id = user_id
            self.slot = slot
            self.reject = reject
            self.sent = []
            self.matches = 0
            self.router = None
            self.active_room_id = -1

        async def send_xt_message(self, ext_id, cmd, params):
            self.sent.append((cmd, dict(params)))
            if cmd == "trigger":
                self.matches += 1
                for message in (
                    {"_cmd": 2303, "battleId": self.user_id},
                    {"_cmd": 2401, "battleUniqueId": self.matches,
                     "pmmList": [
                         {"PSId": 0, "battleView": {"pmmId": self.user_id + 100,
                                                   "slotId": 11 - self.slot}},
                         {"PSId": 0, "battleView": {"pmmId": self.user_id,
                                                   "slotId": self.slot}},
                     ]},
                    {"_cmd": 2426}, {"_cmd": 2402, "pt": 0},
                ):
                    self.router.publish(message)
            elif cmd == "1404":
                assert params["reqPSId"] == self.slot
                if self.reject:
                    self.router.publish({"_cmd": 2414, "msg": "你不是对应战场位置的所有者！"})
                else:
                    self.router.publish({"_cmd": 2414, "isEscape": True,
                                         "reqId": self.user_id, "reqSId": self.slot})

    async def fast_combination(*args, **kwargs):
        return await execute_combination(*args, **kwargs, message_delay=0)

    async def exercise():
        contexts, sockets, waiting, tasks = [], [], [], []
        script = InteractionScript("scripts.auto_battle", "escape", "", battle_sequence_script.run)
        for index in range(8):
            socket = FakeSocket(1000 + index, 0 if index % 2 == 0 else 11, index in {4, 6})
            router = MessageRouter(socket)
            socket.router = router
            event = asyncio.Event()
            context = AppContext(
                {}, SimpleNamespace(user_id=str(socket.user_id)), None, socket, router,
                lambda text, ready=event: ready.set() if text == "等待当前战斗结束" else None,
            )
            context.player_initialized = True
            sockets.append(socket)
            contexts.append(context)
            waiting.append(event)
            tasks.append(asyncio.create_task(execute_combination(
                str(index), context, [ScriptStep(script)], repetitions=2,
                repeat_interval=0, message_delay=0,
            )))
        try:
            for repetition in (1, 2):
                live = [i for i in range(8) if not sockets[i].reject]
                await asyncio.wait_for(asyncio.gather(*(waiting[i].wait() for i in live)), 2)
                for i in live:
                    if sockets[i].reject:
                        continue
                    assert sockets[i].matches == repetition and not tasks[i].done()
                    contexts[i].messages.publish({"_cmd": 2403, "battleUniqueId": 999})
                await asyncio.sleep(0)
                for i in live:
                    if not sockets[i].reject:
                        assert sockets[i].matches == repetition and not tasks[i].done()
                        waiting[i].clear()
                        contexts[i].messages.publish({"_cmd": 2403, "battleUniqueId": repetition})
            results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
            for index, (socket, context, result) in enumerate(zip(sockets, contexts, results)):
                assert result.success == (not socket.reject)
                assert result.completed_repetitions == (0 if socket.reject else 2)
                network_sends = [item for item in result.send_results if item.ext_id >= 0]
                assert len(network_sends) == (2 if socket.reject else 4)
                assert sum(not item.success for item in network_sends) == int(socket.reject)
                assert all(item.account_label == str(index) for item in result.send_results)
                assert [item.sequence for item in result.send_results] == list(range(1, len(result.send_results) + 1))
                if socket.reject:
                    assert "你不是对应战场位置的所有者" in result.step_results[-1].error
                    assert socket.matches == 1
                    assert context.battle.phase == "active" and not context.battle.end_request_sent
                else:
                    assert context.battle.phase == "idle"
                assert not context.messages._subscribers
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def late_rejection_or_missing_slot(missing_slot=False):
        socket = FakeSocket(2000, 11, False)
        router = MessageRouter(socket)
        socket.router = router
        waiting = asyncio.Event()
        context = AppContext(
            {}, SimpleNamespace(user_id="9999" if missing_slot else "2000"), None, socket, router,
            lambda text: waiting.set() if text == "等待当前战斗结束" else None,
        )
        context.player_initialized = True
        task = asyncio.create_task(battle_sequence_script.run(context))
        try:
            if not missing_slot:
                await asyncio.wait_for(waiting.wait(), 2)
                assert not task.done() and context.battle.phase == "ending"
                router.publish({"_cmd": 2414, "msg": "你不是对应战场位置的所有者！"})
            try:
                await asyncio.wait_for(task, 2)
                raise AssertionError("escape error must stop the script")
            except RuntimeError as exc:
                expected = "尚未确认己方战场位置" if missing_slot else "你不是对应战场位置的所有者"
                assert expected in str(exc), exc
            if missing_slot:
                assert [cmd for cmd, _params in socket.sent] == ["trigger"]
            assert context.battle.phase == "active" and not context.battle.end_request_sent
            assert not router._subscribers
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    original = battle_sequence_script.BEFORE_STEPS, battle_sequence_script.execute_combination
    try:
        assert any(message.cmd == "#wait" for step in battle_sequence_script.AFTER_STEPS
                   for message in step.messages)
        battle_sequence_script.BEFORE_STEPS = (MessageBatchStep((SendMessage(42, "trigger", {}),)),)
        battle_sequence_script.execute_combination = fast_combination
        asyncio.run(exercise())
        asyncio.run(late_rejection_or_missing_slot())
        asyncio.run(late_rejection_or_missing_slot(missing_slot=True))
    finally:
        battle_sequence_script.BEFORE_STEPS, battle_sequence_script.execute_combination = original
    print("=== Battle Escape Confirmation ===")
    print("  own slots 0/11 + eight accounts + rejection isolation + matching 2403 before repeat [OK]")


def test_menu_index_selection():
    assert parse_index_selection("2,1,2", 3, allow_all=True) == [1, 0]
    assert parse_index_selection("全部", 3, allow_all=True) == [0, 1, 2]
    try:
        parse_index_selection("4", 3, allow_all=False)
    except ValueError:
        pass
    else:
        raise AssertionError("Expected out-of-range menu selection to fail")
    print("=== Menu Selection ===")
    print("  explicit, deduplicated and all-account selections [OK]")


if __name__ == "__main__":
    test_msgseq()
    test_as3_integer_semantics()
    test_inner_seq()
    test_encrypt()
    test_amf3_roundtrip()
    test_amf3_nested()
    test_msgseq_reproducible()
    test_legacy_push_string_references()
    test_mt250816_score_choice()
    test_mt250816_script_contract()
    test_send_message_parser()
    test_send_message_file_parser()
    test_pipe_separated_message_file()
    test_message_sequence_directive_parser()
    test_message_sequence_wait_and_time()
    test_account_config_models()
    test_fixed_role_login_contract()
    test_explicit_zone_selection()
    test_fixed_role_http_sequence()
    test_password_login_flow()
    test_browser_password_sequence()
    test_login_failure_diagnostics()
    test_bridge_account_config_persistence()
    test_bridge_connection_status_is_incremental()
    test_bridge_bulk_send_does_not_flood_events()
    test_bridge_script_and_combination_reporting()
    test_bridge_bulk_disconnect_does_not_flood_events()
    test_bridge_bulk_failures_keep_details_without_ui_flood()
    test_bridge_login_queue_covers_initialization_and_cancellation()
    test_login_refusal_pauses_queue_and_preserves_ready_connections()
    test_manual_verification_queue()
    test_login_progress_reports_failures_before_batch_finishes()
    test_account_connection_failure_isolation()
    test_tcp_handshake_eof_and_connection_cleanup()
    test_receive_error_does_not_fake_disconnect()
    test_battle_receive_trace_filter_and_payload()
    test_player_context_initialization()
    test_server_downlink_frame_decoder()
    test_headered_system_room_parser()
    test_dynamic_room_join_context()
    test_battle_reuses_confirmed_active_room()
    test_active_room_is_used_for_ext_requests()
    test_battle_entry_then_ordered_sends()
    test_externally_started_battle_actions()
    test_auto_battle_uses_first_available_skill()
    test_auto_battle_replaces_fainted_pet()
    test_battle_end_request_without_final_response()
    test_session_clock_and_owned_message_rejection()
    test_dispatcher_account_isolation()
    test_game_socket_serializes_concurrent_sends()
    test_combined_message_and_script_execution()
    test_multi_account_combination_isolation()
    test_battle_sequence_script()
    test_battle_escape_slot_and_confirmation()
    test_menu_index_selection()
    print("\n=== All tests passed ===")
