"""Visible browser login; official challenges are completed by the user."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import urlsplit


class BrowserLoginStopped(ConnectionError):
    """Interactive login failed; automatic retries must stop."""


def is_captcha_page(text: str) -> bool:
    return any(marker in text.lower() for marker in (
        "tcaptcha.js", "tencentcaptcha", "/wafcaptcha",
    ))


async def _post_form(page, url, params, stage, account, password, timeout=300):
    from src.accounts.login import _save_login_failure

    responses = asyncio.Queue()
    last_response, last_body = None, b""

    def on_response(response):
        if (response.request.is_navigation_request()
                and response.frame == page.main_frame):
            responses.put_nowait(response)

    def on_close(*_):
        responses.put_nowait(None)

    page.on("response", on_response)
    page.on("close", on_close)
    try:
        async with asyncio.timeout(timeout):
            # Native form navigation lets the returned challenge execute at its
            # real origin. Do not load saved challenge HTML or replay tickets.
            await page.evaluate("""({url, params}) => {
                const form = document.createElement('form');
                form.method = 'POST';
                form.action = url;
                form.acceptCharset = 'UTF-8';
                for (const [name, value] of Object.entries(params)) {
                    const input = document.createElement('input');
                    input.type = 'hidden';
                    input.name = name;
                    input.value = value;
                    form.appendChild(input);
                }
                document.documentElement.appendChild(form);
                form.submit();
            }""", {"url": url, "params": params})
            while True:
                response = await responses.get()
                if response is None:
                    raise BrowserLoginStopped("验证窗口已关闭，已停止自动登录")
                if 300 <= response.status < 400:
                    continue
                last_response = SimpleNamespace(
                    status=response.status, url=response.url, history=[],
                    headers={"Content-Type": response.headers.get("content-type", ""),
                             "Retry-After": response.headers.get("retry-after", "")},
                )
                last_body = await response.body()
                text = last_body.decode("utf-8", errors="replace")
                if is_captcha_page(text):
                    print("[登录验证] 请在 Edge 窗口手动完成官方验证；程序正在等待（最多 5 分钟）。", flush=True)
                    continue
                if response.status >= 400:
                    raise BrowserLoginStopped(f"浏览器登录 HTTP {response.status}")
                # A captcha callback's HTTP 200 is not login success. Only a
                # document response is returned for the normal protocol checks.
                return text, last_response, last_body
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            error = "浏览器验证等待超时，已停止自动登录"
        elif isinstance(exc, BrowserLoginStopped):
            error = str(exc)
        else:
            error = f"浏览器登录失败（{type(exc).__name__}），已停止自动登录"
        raise BrowserLoginStopped(_save_login_failure(
            stage, url, last_response, last_body, error, account, password,
        )) from None
    finally:
        page.remove_listener("response", on_response)
        page.remove_listener("close", on_close)


async def browser_login(account, password, char_id, login_url, register_url):
    from src.accounts.login import (
        _save_login_failure, build_login_params, parse_login_response,
        parse_role_response, select_role, validate_game_login,
    )

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise BrowserLoginStopped(
            "浏览器验证需要 Playwright，请运行 python -m pip install -r requirements.txt"
        ) from None

    print("[登录验证] 正在打开独立 Edge 窗口；验证完成后自动继续登录。", flush=True)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=False)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                # Establish the real site origin before submitting the form.
                # This is a fresh isolated context, never the user's Edge profile.
                origin = urlsplit(login_url)
                await page.goto(f"{origin.scheme}://{origin.netloc}/", wait_until="domcontentloaded")
                for stage, url, params in (
                    ("browser_role_query", register_url, {"type": "query_role_info", "duoduoId": account}),
                    ("browser_login", login_url, build_login_params(account, password, char_id)),
                ):
                    text, response, body = await _post_form(page, url, params, stage, account, password)
                    try:
                        if stage == "browser_role_query":
                            role = select_role(parse_role_response(text), char_id)
                            # The response body can arrive before the document commits.
                            await page.wait_for_load_state("domcontentloaded")
                            continue
                        result = parse_login_response(text)
                        validate_game_login(result, account, role)
                        return result
                    except (ValueError, ConnectionError) as exc:
                        raise BrowserLoginStopped(_save_login_failure(
                            stage, url, response, body, str(exc), account, password,
                        )) from None
            finally:
                await browser.close()
    except BrowserLoginStopped:
        raise
    except Exception as exc:
        raise BrowserLoginStopped(
            f"无法完成 Edge 浏览器登录（{type(exc).__name__}）；请确认 Edge 已安装且窗口未关闭，已停止自动登录"
        ) from None
