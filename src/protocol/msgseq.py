"""
msgseq.py — 消息序号生成器，精确对照 H5 源码

src:
  MsgSeq.ts:11-41          → MsgSeq (外层包序号)
  BinaryMsgWriter.ts:104-135 → InnerSeq (内层消息序号)
"""
import random
from src.protocol.as3_compat import as3_remainder, to_as3_int32
from src.config import (
    MSGSEQ_NUM1, MSGSEQ_NUM2, MSGSEQ_NUM3, MSGSEQ_NUM4,
    MSGSEQ_FIRST_VAL, INNER_SEQ_INIT, INNER_SEQ_SATURATION,
    INNER_SEQ_MOD, INNER_KEY_MASK,
)

# H5 JavaScript 中 ~0x1FFFFFFC 的 32 位结果
# JS: ~0x1FFFFFFC = 0xE0000003
# Python: ~0x1FFFFFFC 是无限精度负数，需要 mask 到 32 位
INNER_NOT_MASK_32 = (~INNER_KEY_MASK) & 0xFFFFFFFF


class MsgSeq:
    """
    外层包序号生成器 — 精确对照 MsgSeq.ts:11-41

    每发送一个 TCP 包时调用 genPacketNum() → 生成包序号，
    序号用于外层 XOR 加密的 key。
    """
    def __init__(self, session_id: str = ""):
        # MsgSeq.ts:13-16
        self.num1 = MSGSEQ_NUM1    # 7
        self.num2 = MSGSEQ_NUM2    # 991
        self.num3 = MSGSEQ_NUM3    # 569
        self.num4 = MSGSEQ_NUM4    # 911
        self._hash_session_id = 0
        self._is_first_time = True
        self._has_hashed = False
        self._session_id = session_id

    def _hash_as3(self, s: str) -> int:
        """MsgSeq.ts:34-41 — AS3 UTF-16 code-unit string hash."""
        hash_value = 0
        encoded = s.encode("utf-16-be", errors="surrogatepass")
        for offset in range(0, len(encoded), 2):
            code_unit = int.from_bytes(encoded[offset:offset + 2], "big")
            hash_value = to_as3_int32(31 * hash_value + code_unit)
        return hash_value

    def next(self, now: int, user_id: int) -> int:
        """
        MsgSeq.ts:21-33

        首次 (now=0, is_first_time=True) → 返回 79
        后续 → next = now*7 + userId%991 + hashSession%569
               if next >= 2147483647: next = next%1047483647 + 911
        """
        if now == 0 and self._is_first_time:
            self._is_first_time = False
            return MSGSEQ_FIRST_VAL  # 79

        if not self._has_hashed:
            self._has_hashed = True
            self._hash_session_id = self._hash_as3(self._session_id)

        # AS3/JavaScript remainder keeps the dividend's sign.  Python's `%`
        # does not, which corrupts the first calculated packet number whenever
        # the signed session hash is negative.
        _next = (
            now * self.num1
            + as3_remainder(user_id, self.num2)
            + as3_remainder(self._hash_session_id, self.num3)
        )
        if _next >= 2147483647:
            msg_id = as3_remainder(_next, 1047483647) + self.num4
        else:
            msg_id = _next
        return to_as3_int32(msg_id)


class InnerSeq:
    """
    内层消息序号生成器 — 精确对照 BinaryMsgWriter.ts:104-135

    每发送一条扩展消息 (sendXtMessage) 时调用 getMessageSequence(extId)
    生成序号，用于内层 AMF3 体 XOR + swap 加密的 key。
    """
    def __init__(self):
        # BinaryMsgWriter.ts:16 — seqMap: dict[extId → lastSeq]
        self._seq_map = {}

    def _rand(self) -> int:
        """BinaryMsgWriter.ts:133-134 — 随机数 [0, 65534]"""
        return random.randint(0, 65534)

    def get_my_next_seq(self, now: int, user_id: int) -> int:
        """
        BinaryMsgWriter.ts:114-132

        首次 (now=0) → 返回 18
        后续:
          real = (now & 0x1FFFFFFC) >> 2        ← 去掉低 2 位
          next = real * 2 + userId % 108
          result = next < 123216728 ? next : 204

        然后低 2 位填入随机值:
          tmp = (rand << 17) + (rand << 2) + (rand % 4)
          result = ((tmp & (~0x1FFFFFFC)) | (result << 2))
        """
        if now == 0:
            result = INNER_SEQ_INIT  # 18
        else:
            real = (now & INNER_KEY_MASK) >> 2
            _next = real * 2 + (user_id % INNER_SEQ_MOD)
            result = _next if _next < INNER_SEQ_SATURATION else 204

        # 低 2 位填入随机值, bits 28-30 也随机
        tmp0 = self._rand()          # [0, 65534]
        tmp1 = self._rand()
        tmp2 = self._rand() % 4
        tmp = to_as3_int32((tmp0 << 17) + (tmp1 << 2) + tmp2)
        # H5 源码: tmp & (~0x1FFFFFFC)
        # AS3 的 `&` / `|` 最终产生 signed int。保留负号很重要：
        # :ext_seq; 会进入 AMF3，写成相同位模式的无符号正数并不等价。
        result = (tmp & INNER_NOT_MASK_32) | (result << 2)
        return to_as3_int32(result)

    def get_message_sequence(self, ext_id: int, user_id: int) -> int:
        """
        BinaryMsgWriter.ts:104-113

        每个 extension ID 有独立的序列号计数器。
        """
        if ext_id not in self._seq_map:
            self._seq_map[ext_id] = 0
        seq = self.get_my_next_seq(self._seq_map[ext_id], user_id)
        self._seq_map[ext_id] = seq
        return seq
