"""wss_test2.py — 用完整 API login 获取的 sid 测试 WSS"""
import asyncio, ssl, struct, websockets
from src.accounts.login import http_login
from src.config import load_user_config
from src.network.socket_client import _pack_utf

conf = load_user_config()

async def main():
    r = await http_login(conf['account'], conf['password'], conf['login_url'])
    print(f"sid loaded (len={len(r.session_id)})")
    print(f"uid={r.user_id}")
    
    # Find zone 1025
    z = None
    for x in r.zone_list:
        if x.zone_index == 1025 and x.domain and x.port > 0:
            z = x
            break
    if not z: return
    
    zone_str = f"{z.zone_index} {z.zone_name}"
    server = f"{z.domain}:{z.port}"
    print(f"Zone: {zone_str!r}")
    print(f"Server: {server}")
    
    body = b'\x00' + struct.pack('>H', 10016) + struct.pack('>I', 0xFFFFFFFF)
    body += _pack_utf(zone_str) + _pack_utf(r.user_id) + _pack_utf(r.session_id)
    
    packet = struct.pack('>I', len(body)+4) + struct.pack('>I', 0) + body
    print(f"Packet: {len(packet)}B (credential bytes hidden)")
    
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    
    ws = await websockets.connect(f"wss://{server}", ssl=ssl_ctx,
        additional_headers={"Origin": "https://aola.100bt.com"},
        user_agent_header="Mozilla/5.0")
    print("Connected")
    
    await ws.send(packet)
    print(f"Sent {len(packet)} bytes")
    
    resp = await asyncio.wait_for(ws.recv(), timeout=15)
    resp = bytes(resp)
    tl = struct.unpack('>I', resp[0:4])[0]
    sq = struct.unpack('>I', resp[4:8])[0]
    raw = resp[8:]
    print(f"Response: totalLen={tl} seq={sq} rawLen={len(raw)}")
    
    # Check first byte for type
    msg_type = raw[0]
    print(f"Raw type byte: {msg_type} (0=SYS, 1=EXT)")
    
    # Print raw text
    try:
        print(f"Raw text: {raw.decode('utf-8', errors='replace')[:200]}")
    except: pass
    
    # If SYS, don't XOR
    if msg_type == 1:
        key = struct.pack('>I', sq & 0xFFFFFFFF)
        raw = bytes(b ^ key[i % 4] for i, b in enumerate(raw))
        print(f"XOR text: {raw.decode('utf-8', errors='replace')[:200]}")
    
    await ws.close()

asyncio.run(main())
