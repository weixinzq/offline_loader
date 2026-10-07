"""
encrypt.py — 协议层双层加密实现，精确对照 H5 源码

src:
  BinaryMsgWriter.ts:136-154 → encryptByteArray (内层: XOR + swap)
  SocketServerClient.ts:97-113 → writeBinaryToSocket (外层: XOR)

结构:
  登录包: 外层 XOR (key = 0, 无加密)
  扩展消息:
    1. encryptByteArray(seq, amf3_bytes) → 内层加密
    2. writeBinaryToSocket(body_bytes)   → 外层加密
"""
import struct
from src.config import INNER_KEY_MASK


def encrypt_byte_array(seq: int, data: bytearray) -> bytearray:
    """
    内层加密 — BinaryMsgWriter.ts:136-150
    
    key = (seq & 0x1FFFFFFC) >> 2
    decryptKey = [(key>>22)&0xFF, (key>>18)&0xFF, (key>>9)&0xFF, (key>>2)&0xFF]
    XOR + swap(每4字节块, idx ↔ idx+2)
    """
    key = (seq & INNER_KEY_MASK) >> 2
    decrypt_key = [
        (key >> 22) & 0xFF,
        (key >> 18) & 0xFF,
        (key >> 9) & 0xFF,
        (key >> 2) & 0xFF,
    ]

    # XOR
    key_len = len(decrypt_key)
    for i in range(len(data)):
        data[i] ^= decrypt_key[i % key_len]

    # Swap: for j in 0..nblocks, idx=j*4+(j&1), swap data[idx]↔data[idx+2]
    n_blocks = len(data) // 4
    for j in range(n_blocks):
        idx = j * 4 + (j & 1)
        data[idx], data[idx + 2] = data[idx + 2], data[idx]

    return data


def outer_xor_encrypt(payload: bytes, pkt_seq: int) -> bytes:
    """
    外层加密 — SocketServerClient.ts:97-113
    
    key = pkt_seq 的 4 字节大端表示
    对整个 payload XOR key (循环 4 字节)
    """
    if pkt_seq == 0:
        # 首个包的 key 为全零 → 无加密
        key = b'\x00\x00\x00\x00'
    else:
        key = struct.pack('>I', pkt_seq & 0xFFFFFFFF)
    key_len = len(key)
    return bytes(b ^ key[i % key_len] for i, b in enumerate(payload))
