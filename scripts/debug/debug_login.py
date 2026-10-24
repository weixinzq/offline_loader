import re

with open(r"D:\game\aqcs-helper\gamemain~20260723224138.js", "r", encoding="utf-8", errors="replace") as f:
    c = f.read()

# Search for login URL patterns
patterns = [
    r'loginUrl\s*[=:]\s*["\']([^"\']+)["\']',
    r'login_url\s*[=:]\s*["\']([^"\']+)["\']',
    r'"([^"]*login[^"]*\.(action|jsp|php)[^"]*)"',
    r'"([^"]*Login[^"]*\.[^"]*)"',
]

for pat in patterns:
    matches = re.findall(pat, c, re.IGNORECASE)
    for m in matches:
        val = m if isinstance(m, str) else m[0]
        if 'login' in val.lower() and len(val) > 5:
            print(f"  {val}")
