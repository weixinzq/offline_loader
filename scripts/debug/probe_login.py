"""probe_login.py — 探测正确的登录端点 + 参数组合来获取 <sid>"""
import asyncio, aiohttp, urllib.parse, hashlib, re
from src.config import load_user_config

conf = load_user_config()
acc = conf['account']
pwd_md5 = hashlib.md5(conf['password'].encode()).hexdigest()

# Base params like mainPy
BASE_PARAMS = {
    "account": acc,
    "password": pwd_md5,
    "logintype": "",
    "wyToken": "",
    "fromurl": "",
    "webSite": "",
    "token": "",
    "cookieId": "",
    "pi": "",
    "sessionId": "",
    "content": "",
}

# Parameters like current loader
H5_PARAMS = {
    "isnickname": "normallogin",
    "account": acc,
    "password": pwd_md5,
    "platform": "CN_H5",
    "forceLogin": "0",
    "wyToken": "-1",
}

PARAM_COMBOS = [
    ("mainPy-style", BASE_PARAMS),
    ("H5-style", H5_PARAMS),
    ("mainPy + wyToken=-1", {**BASE_PARAMS, "wyToken": "-1"}),
    # Mixed: mainPy params + H5 extras
    ("mixed", {**BASE_PARAMS, "platform": "CN_H5", "isnickname": "normallogin", "forceLogin": "0"}),
    # H5 params on mainPy endpoint pattern
    ("h5-extra", {**H5_PARAMS, "logintype": "", "fromurl": "", "webSite": "", "token": "", "cookieId": "", "pi": "", "sessionId": "", "content": ""}),
]

ENDPOINTS = [
    "https://login-aola.100bt.com/newLogin.jsp",
    "https://login-aola.100bt.com/login.action",
    "https://login-aola.100bt.com/login",
    "https://aola.100bt.com/play/login",
    "https://aola.100bt.com/play/login.jsp",
    "https://aola.100bt.com/h5/login",
    "https://aola.100bt.com/login",
]

async def probe(endpoint, params, label):
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": "https://aola.100bt.com",
        "Referer": "https://aola.100bt.com/",
    }
    body = urllib.parse.urlencode(params)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(endpoint, data=body, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=10)) as r:
                txt = await r.text()
                has_ok = "<c>ok</c>" in txt or 'ok' in txt[:50]
                has_sid = "<sid>" in txt
                has_u = "<u>" in txt
                has_d = "<d>" in txt
                # Extract sid if present
                m = re.search(r'<sid>([^<]+)</sid>', txt)
                sid = m.group(1) if m else None
                m2 = re.search(r'<u><!\[CDATA\[(\d+)\]\]></u>', txt)
                uid = m2.group(1) if m2 else None
                m3 = re.search(r'<d><!\[CDATA\[(\d+)\]\]></d>', txt)
                d_val = m3.group(1) if m3 else None
                
                status = r.status
                if has_sid:
                    print(f"  [MATCH!] {label} on {endpoint}")
                    print(f"    sid present (len={len(sid or '')})")
                    print(f"    u={uid}")
                    print(f"    d={d_val}")
                    return (endpoint, params, sid, uid, d_val)
                elif has_ok != has_sid:
                    extra = f"ok={has_ok} sid={has_sid} u={has_u} d={has_d} status={status} len={len(txt)}"
                    if has_ok:
                        print(f"  [HAS OK] {label}: {extra}")
    except Exception as e:
        pass  # skip unreachable endpoints
    return None

async def main():
    print("=== Phase 1: Probe param combos on newLogin.jsp ===")
    for label, params in PARAM_COMBOS:
        result = await probe("https://login-aola.100bt.com/newLogin.jsp", params, label)
        if result:
            print(f"\n*** FOUND WORKING COMBO ON newLogin.jsp ***")
            print(f"Endpoint: {result[0]}")
            print(f"sid present (len={len(result[2] or '')})")
            print(f"<u>: {result[3]}")
            return

    print("\n=== Phase 2: Probe other endpoints with best params ===")
    for endpoint in ENDPOINTS:
        if endpoint == "https://login-aola.100bt.com/newLogin.jsp":
            continue  # already tested
        for label, params in PARAM_COMBOS[:2]:  # just mainPy + H5 styles
            result = await probe(endpoint, params, label)
            if result:
                print(f"\n*** FOUND ON {endpoint} ***")
                print(f"Params: {label}")
                print(f"sid present (len={len(result[2] or '')})")
                return

    print("\n=== Phase 3: Try with CookieJar ===")
    jar = aiohttp.CookieJar()
    async with aiohttp.ClientSession(cookie_jar=jar) as s:
        # GET the main page first
        try:
            await s.get("https://aola.100bt.com/", timeout=aiohttp.ClientTimeout(total=10))
        except:
            pass
        # Try login with cookies
        for label, params in PARAM_COMBOS:
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Origin": "https://aola.100bt.com",
                "Referer": "https://aola.100bt.com/h5/",
            }
            body = urllib.parse.urlencode(params)
            async with s.post("https://login-aola.100bt.com/newLogin.jsp",
                            data=body, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=10)) as r:
                txt = await r.text()
                has_sid = "<sid>" in txt
                m = re.search(r'<sid>([^<]+)</sid>', txt)
                sid = m.group(1) if m else None
                if has_sid:
                    print(
                        f"[MATCH with cookies!] {label}: "
                        f"sid present (len={len(sid or '')})"
                    )
                    return
    
    print("\n*** No working combination found ***")
    print("All endpoints and param combos failed to return <sid>")

asyncio.run(main())
