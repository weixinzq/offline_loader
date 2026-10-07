"""使用账号密码为已导入账号创建角色，并将确认后的 char_id 写回配置。

默认只检查待处理数量，不访问网络。真实执行示例：
    python create_roles_batch.py --execute --limit 5

运行真实任务前请关闭 AolaLoader，避免它同时改写 config.json。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import aiohttp

from src.accounts.login import RoleInfo, md5_password, parse_role_response


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "dist" / "AolaLoader" / "config.json"
DEFAULT_REGISTER_URL = "https://service-aola.100bt.com/newRegister.jsp"
DEFAULT_START_INDEX = 6  # 前 5 条是迁移前已经存在的账号；索引从 1 开始。
DEFAULT_LIMIT = 2000
DEFAULT_CONCURRENCY = 5
REGISTER_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "null",
    "Referer": "https://aola.100bt.com/play/play.html?webLogin_src=&webLogin_site=",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 6.2; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/87.0.4280.141 Safari/537.36"
    ),
    "X-Requested-With": "ShockwaveFlash/99.0.0.999",
}


class RoleCreationError(RuntimeError):
    """某个账号不能安全完成角色创建。"""


@dataclass(frozen=True)
class CreatedRole:
    char_id: int
    player_id: str
    nickname: str


def append_response_diagnostic(
    path: Path,
    *,
    label: str,
    stage: str,
    status: int,
    url: str,
    content_type: str,
    redirect_statuses: list[int],
    body: str,
    secrets: tuple[str, ...] = (),
) -> None:
    """保存足够定位响应问题的信息，不记录请求头、Cookie 或 token。"""
    sanitized = body
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "<REDACTED>")
    sanitized = re.sub(
        r"(?i)(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])",
        "<REDACTED_32_HEX>",
        sanitized,
    )
    limit = 4000
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "stage": stage,
        "status": status,
        "url": url,
        "content_type": content_type,
        "redirect_statuses": redirect_statuses,
        "body": sanitized[:limit],
        "body_truncated": len(sanitized) > limit,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_activation_response(text: str) -> CreatedRole:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RoleCreationError(f"创建响应不是有效 XML：{exc}") from exc

    result = (root.findtext("result") or "").strip()
    parts = result.split(":", 3)
    if len(parts) != 4 or parts[0] != "success":
        raise RoleCreationError(f"创建失败：{result or '响应缺少 result'}")
    try:
        char_id = int(parts[1])
    except ValueError as exc:
        raise RoleCreationError("创建响应中的 char_id 无效") from exc
    if char_id < 0 or not parts[2]:
        raise RoleCreationError("创建响应中的角色字段无效")
    return CreatedRole(char_id, parts[2], parts[3])


def parse_optional_roles(text: str) -> list[RoleInfo]:
    if not text.strip():
        return []
    try:
        return parse_role_response(text)
    except ValueError as exc:
        raise RoleCreationError(f"角色查询响应无法解析：{exc}") from exc


def select_existing_role(roles: list[RoleInfo], configured_char_id: int) -> RoleInfo:
    configured = next(
        (role for role in roles if role.char_id == configured_char_id), None
    )
    if configured is not None:
        return configured
    if len(roles) == 1:
        return roles[0]
    available = "、".join(str(role.char_id) for role in roles)
    raise RoleCreationError(f"已有多个角色，无法自动选择；可用 char_id：{available}")


async def query_roles(
    session: aiohttp.ClientSession,
    register_url: str,
    duoduo_id: str,
    timeout: float,
) -> list[RoleInfo]:
    async with session.post(
        register_url,
        data={"type": "query_role_info", "duoduoId": duoduo_id},
        headers=REGISTER_HEADERS,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        response.raise_for_status()
        text = await response.text(encoding="utf-8")
    return parse_optional_roles(text)


def build_password_activation_payload(account: str, password: str) -> dict[str, str]:
    return {
        "type": "try_activate",
        "bid": account,
        "pwd": md5_password(password),
        "aolaName": "",
        "clothID": "0",
        "skinID": "1",
        "petId": "0",
        "wyToken": "-1",
        "ref": "account.100bt.com",
        "from": "",
        "token": "",
    }


async def activate_role_with_password(
    session: aiohttp.ClientSession,
    register_url: str,
    account: str,
    password: str,
    timeout: float,
    label: str,
    diagnostic_log: Path,
) -> CreatedRole:
    payload = build_password_activation_payload(account, password)
    async with session.post(
        register_url,
        data=payload,
        headers=REGISTER_HEADERS,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        text = await response.text(encoding="utf-8", errors="replace")
        status = response.status
        url = str(response.url)
        content_type = response.headers.get("Content-Type", "")
        redirect_statuses = [item.status for item in response.history]
        if status >= 400:
            append_response_diagnostic(
                diagnostic_log,
                label=label,
                stage="password_activation_http_error",
                status=status,
                url=url,
                content_type=content_type,
                redirect_statuses=redirect_statuses,
                body=text,
                secrets=(account, payload["pwd"]),
            )
            response.raise_for_status()
    try:
        return parse_activation_response(text)
    except RoleCreationError as exc:
        append_response_diagnostic(
            diagnostic_log,
            label=label,
            stage="password_activation_invalid_response",
            status=status,
            url=url,
            content_type=content_type,
            redirect_statuses=redirect_statuses,
            body=text,
            secrets=(account, payload["pwd"]),
        )
        raise RoleCreationError(f"{exc}；详见 {diagnostic_log}") from exc


async def verify_created_role(
    session: aiohttp.ClientSession,
    register_url: str,
    duoduo_id: str,
    created: CreatedRole,
    timeout: float,
) -> RoleInfo:
    for attempt in range(3):
        roles = await query_roles(session, register_url, duoduo_id, timeout)
        match = next(
            (
                role
                for role in roles
                if role.char_id == created.char_id
                and role.user_id == created.player_id
            ),
            None,
        )
        if match is not None:
            return match
        if attempt < 2:
            await asyncio.sleep(0.5)
    raise RoleCreationError("创建响应成功，但角色查询未确认到相同角色")


async def recover_after_uncertain_activation(
    session: aiohttp.ClientSession,
    register_url: str,
    duoduo_id: str,
    configured_char_id: int,
    timeout: float,
) -> RoleInfo | None:
    """创建请求结果不确定时只查询，不重复发送创建请求。"""
    for attempt in range(3):
        try:
            roles = await query_roles(session, register_url, duoduo_id, timeout)
        except (aiohttp.ClientError, asyncio.TimeoutError, RoleCreationError):
            roles = []
        if roles:
            return select_existing_role(roles, configured_char_id)
        if attempt < 2:
            await asyncio.sleep(0.5)
    return None


async def ensure_role(
    entry: dict[str, Any],
    register_url: str,
    timeout: float,
    diagnostic_log: Path,
) -> RoleInfo:
    account = str(entry.get("account", ""))
    password = str(entry.get("password", ""))
    label = str(entry.get("label", "未命名账号"))
    configured_char_id = int(entry.get("char_id", 0))
    if not account or not password:
        raise RoleCreationError("配置缺少账号或密码")

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        roles = await query_roles(session, register_url, account, timeout)
        if roles:
            return select_existing_role(roles, configured_char_id)

        try:
            created = await activate_role_with_password(
                session,
                register_url,
                account,
                password,
                timeout,
                label,
                diagnostic_log,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, RoleCreationError) as exc:
            recovered = await recover_after_uncertain_activation(
                session, register_url, account, configured_char_id, timeout
            )
            if recovered is not None:
                return recovered
            raise RoleCreationError(
                f"创建结果不确定且复查仍无角色：{exc}"
            ) from exc

        return await verify_created_role(
            session, register_url, account, created, timeout
        )


def write_config_atomic(path: Path, config: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def create_backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.before-role-creation-{stamp}.json")
    shutil.copy2(path, backup)
    return backup


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--start-index", type=int, default=DEFAULT_START_INDEX)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="实际使用账号密码创建角色；省略时只进行本地预检",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    if args.start_index < 1 or args.limit < 1:
        raise ValueError("start-index 和 limit 必须大于 0")
    if not 1 <= args.concurrency <= 20:
        raise ValueError("concurrency 必须在 1 到 20 之间")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    accounts = config.get("accounts")
    if not isinstance(accounts, list):
        raise ValueError("config.json 缺少 accounts 数组")

    start = args.start_index - 1
    selected = list(enumerate(accounts[start : start + args.limit], start=start))
    if not selected:
        raise ValueError("指定范围内没有账号")
    pending = [item for item in selected if int(item[1].get("char_id", 0)) == 0]
    print(
        f"目标范围={len(selected)}，待确认或创建={len(pending)}，"
        f"已配置 char_id={len(selected) - len(pending)}"
    )
    if not args.execute:
        print("预检完成；没有访问网络。添加 --execute 才会真实创建角色。")
        return 0

    backup = create_backup(config_path)
    print(f"配置备份：{backup}")
    print(f"开始执行，并发数={args.concurrency}；请勿同时运行 AolaLoader。")

    register_url = str(config.get("register_url") or DEFAULT_REGISTER_URL)
    timeout = float(config.get("timeout", 30))
    diagnostic_log = config_path.parent / "logs" / (
        f"create-roles-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    )
    print(f"异常响应日志：{diagnostic_log}")
    semaphore = asyncio.Semaphore(args.concurrency)
    write_lock = asyncio.Lock()
    success = 0
    failed = 0

    async def process(index: int, entry: dict[str, Any]) -> None:
        nonlocal success, failed
        label = str(entry.get("label", f"账号 {index + 1}"))
        if int(entry.get("char_id", 0)) != 0:
            return
        async with semaphore:
            try:
                role = await ensure_role(
                    entry, register_url, timeout, diagnostic_log
                )
                async with write_lock:
                    accounts[index]["char_id"] = role.char_id
                    write_config_atomic(config_path, config)
                success += 1
                print(f"[{label}] 已确认角色并更新 char_id={role.char_id}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed += 1
                print(f"[{label}] 失败：{exc}")

    await asyncio.gather(*(process(index, entry) for index, entry in pending))
    print(f"执行完成：更新={success}，失败={failed}")
    return 1 if failed else 0


def main() -> int:
    args = parse_arguments()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("已由用户中止；已经确认的 char_id 均已保存。")
        return 130
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"无法启动：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
