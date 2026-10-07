"""wss_debug.py — 测试 WSS 连接到某个指定区服"""
import asyncio, ssl, struct, websockets
from src.accounts.login import http_login
from src.config import load_user_config

conf = load_user_config()

def pack_utf(s):
    b = s.encode('utf-8')
    return struct.pack('>H', len(b)) + b

async def main():
    r = await http_login(conf['account'], conf['password'], conf['login_url'])
    # Pick a specific zone
    z = None
    for x in r.zone_list:
        if x.zone_index == 1025 and x.domain and x.port > 0:
            z = x
            break
    if not z:
        print("Zone not found")
        return
    
    zone_str = f"{z.zone_index} {z.zone_name}"
    server = f"{z.domain}:{z.port}"
    uid = conf.get('player_id') or r.user_id
    sid = conf.get('session_id') or r.session_id
    
    print(f"Zone: {zone_str!r}")
    print(f"Server: {server}")
    print(f"UID: {uid}")
    
    body = b'\x00' + struct.pack('>H', 10016) + struct.pack('>I', 0xFFFFFFFF)
    body += pack_utf(zone_str) + pack_utf(uid) + pack_utf(sid)
    print(f"Inner body: {len(body)}B (credential bytes hidden)")
    
    total_len = len(body) + 4
    packet = struct.pack('>I', total_len) + struct.pack('>I', 0) + body
    print(f"Full packet: {len(packet)}B")
    
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    
    ws = await asyncio.wait_for(
        websockets.connect(f"wss://{server}", ssl=ssl_ctx,
            additional_headers={"Origin": "https://aola.100bt.com"},
            user_agent_header="Mozilla/5.0"),
        timeout=10)
    print(f"Connected to {server}")
    
    await ws.send(packet)
    print(f"Sent {len(packet)} bytes")
    
    try:
        resp = await asyncio.wait_for(ws.recv(), timeout=15)
        resp = bytes(resp)
        tl = struct.unpack('>I', resp[0:4])[0]
        sq = struct.unpack('>I', resp[4:8])[0]
        raw_body = resp[8:]
        # Try both XOR and non-XOR
        key = struct.pack('>I', sq & 0xFFFFFFFF)
        body_xor = bytes(b ^ key[i % 4] for i, b in enumerate(raw_body))
        
        print(f"Response: totalLen={tl} seq={sq}")
        print(f"Raw body ({len(raw_body)}B): {raw_body.hex()}")
        print(f"XOR body ({len(body_xor)}B): {body_xor.hex()}")
        
        status_raw = struct.unpack('>I', raw_body[0:4])[0]
        status_xor = struct.unpack('>I', body_xor[0:4])[0]
        print(f"Status: raw={status_raw} xor={status_xor}")
        
        # Try to find AMF3 or UTF-8 text in body
        for label, data in [("raw", raw_body), ("xor", body_xor)]:
            try:
                text = data.decode('utf-8', errors='replace')
                if any(c.isalpha() for c in text[:30]):
                    print(f"  {label} text: {text[:200]}")
            except:
                pass
            # Try AMF3 parsing
            try:
                from src.protocol.amf3 import Amf3Reader
                rdr = Amf3Reader(data)
                obj = rdr.read_object()
                print(f"  {label} AMF3: {obj}")
            except Exception as e:
                pass
    except websockets.exceptions.ConnectionClosed as e:
        print(f"Closed: {e.code} {e.reason}")

asyncio.run(main())
