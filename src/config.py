"""
config.py — 协议常量，精确对照 H5 源码
src: UpCmd.ts, DownCmd.ts, Amf3Types.ts, MsgSeq.ts, BinaryMsgWriter.ts
"""
import json
import os
import pathlib
import sys

# ── 上行命令 (UpCmd.ts) ──
class UpCmd:
    AsObj    = 10002
    Hit      = 10011
    JoinRoom = 10012
    LeaveRoom = 10013
    Login    = 10016
    Logout   = 10017
    PubMsg   = 10020
    SetRvars = 10027
    SetUvars = 10028
    XtReq    = 10033
    XtReqB   = 10034

# ── 下行命令 (DownCmd.ts) ──
class DownCmd:
    logKO  = 20021
    joinOK = 20019
    joinKO = 20018
    xtRes  = 20041
    xtResB = 20043

# ── AMF3 类型标记 (Amf3Types.ts) ──
class Amf3Type:
    kUndefinedType = 0
    kNullType      = 1
    kFalseType     = 2
    kTrueType      = 3
    kIntegerType   = 4
    kDoubleType    = 5
    kStringType    = 6
    kArrayType     = 9
    kObjectType    = 10
    kByteArrayType = 12

# ── MsgSeq 常量 (MsgSeq.ts:13-16) ──
MSGSEQ_NUM1 = 7
MSGSEQ_NUM2 = 991
MSGSEQ_NUM3 = 569
MSGSEQ_NUM4 = 911
MSGSEQ_FIRST_VAL = 79             # 第二个包的起始值 (now=0 时返回)

# ── BinaryMsgWriter 常量 (BinaryMsgWriter.ts:118-119) ──
INNER_SEQ_INIT = 18               # getMyNextSeq(0, _) 首次返回
INNER_SEQ_SATURATION = 123216728  # 饱和上限
INNER_SEQ_MOD = 108               # userId 取模

# ── 扩展消息类型标记 ──
MSG_TYPE_SYS = 0                  # 系统消息
MSG_TYPE_EXT = 1                  # 扩展消息

# ── 加密常量 ──
INNER_KEY_MASK = 0x1FFFFFFC       # 内层加密 key 掩码 (低 2 位用随机值)

# ── 配置 ──
# Writable runtime data lives beside the packaged executable. Read-only bundled
# resources live under PyInstaller's temporary _MEIPASS directory.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = pathlib.Path(sys.executable).resolve().parent
    RESOURCE_ROOT = pathlib.Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
else:
    PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
    RESOURCE_ROOT = PROJECT_ROOT
CONFIG_PATH = PROJECT_ROOT / "config.json"


def load_user_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    return {}


def default_config():
    return {
        "login_url": "https://login-aola.100bt.com/newLogin.jsp",
        "register_url": "https://service-aola.100bt.com/newRegister.jsp",
        "account": "",
        "password": "",
        "char_id": 0,
        "player_id": "",       # 游戏内用户ID (<u>), 留空则自动解析
        "session_id": "",      # 会话令牌 (<sid>), 留空则自动解析
        "platform": "CN_H5",
        "timeout": 30,
    }
