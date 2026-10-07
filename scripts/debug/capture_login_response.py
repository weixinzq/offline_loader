"""Run one project login request and preserve the raw login response."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

import aiohttp

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.accounts.login import (
    build_login_params,
    parse_login_response,
    parse_role_response,
    select_role,
)


DEFAULT_CONFIG = PROJECT_ROOT / "dist" / "AolaLoader" / "config.json"
DEFAULT_LOGIN_URL = "https://login-aola.100bt.com/newLogin.jsp"
DEFAULT_REGISTER_URL = "https://service-aola.100bt.com/newRegister.jsp"
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Origin": "https://aola.100bt.com",
    "Referer": "https://aola.100bt.com/",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    matches = [
        entry
        for entry in config.get("accounts", [])
        if str(entry.get("account", "")) == args.account
    ]
    if len(matches) != 1:
        raise ValueError(f"配置中匹配账号数量异常：{len(matches)}")

    entry = matches[0]
    password = str(entry.get("password", ""))
    char_id = int(entry.get("char_id", 0))
    if not password:
        raise ValueError("账号配置缺少密码")

    login_url = str(config.get("login_url") or DEFAULT_LOGIN_URL)
    register_url = str(config.get("register_url") or DEFAULT_REGISTER_URL)
    timeout = aiohttp.ClientTimeout(total=float(config.get("timeout", 30)))

    async with aiohttp.ClientSession() as session:
        role_body = urllib.parse.urlencode(
            {"type": "query_role_info", "duoduoId": args.account}
        )
        async with session.post(
            register_url, data=role_body, headers=HEADERS, timeout=timeout
        ) as response:
            response.raise_for_status()
            role_text = await response.text(encoding="utf-8")
        role = select_role(parse_role_response(role_text), char_id)

        login_body = urllib.parse.urlencode(
            build_login_params(args.account, password, char_id)
        )
        async with session.post(
            login_url, data=login_body, headers=HEADERS, timeout=timeout
        ) as response:
            raw_body = await response.read()
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            final_url = str(response.url)
            redirect_statuses = [item.status for item in response.history]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = PROJECT_ROOT / "evidence"
    output_dir.mkdir(parents=True, exist_ok=True)
    extension = ".html" if "html" in content_type.lower() else ".xml"
    response_path = output_dir / f"login-response-{stamp}{extension}"
    metadata_path = output_dir / f"login-response-{stamp}.json"
    response_path.write_bytes(raw_body)

    decoded = raw_body.decode("utf-8", errors="replace")
    result = parse_login_response(decoded)
    metadata = {
        "captured_at": stamp,
        "http_status": status,
        "content_type": content_type,
        "final_url": final_url,
        "redirect_statuses": redirect_statuses,
        "body_bytes": len(raw_body),
        "parse_success": result.success,
        "parse_error": result.error_msg,
        "role_validation_passed": bool(result.success and result.user_id == role.user_id),
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"response_file={response_path}")
    print(f"metadata_file={metadata_path}")
    print(f"http_status={status}")
    print(f"content_type={content_type}")
    print(f"parse_success={result.success}")
    print(f"parse_error={result.error_msg}")
    return 0 if result.success and result.user_id == role.user_id else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
