"""Measure account selection with 2,000 fake accounts and fully intercepted API."""
import argparse
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import time
from urllib.request import build_opener, ProxyHandler

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    options = parser.parse_args()
    frontend = Path(__file__).resolve().parents[1]
    output = frontend / "test-results"
    output.mkdir(exist_ok=True)
    url = "http://127.0.0.1:5187"
    accounts = [{"account_id": f"fake-{index:04}", "label": f"模拟账号 {index:04}",
                 "account": f"fixture-{index}@example.invalid", "char_id": 0, "zone_index": 1}
                for index in range(2000)]
    sessions = [{"account_id": account["account_id"], "label": account["label"],
                 "state": "offline", "error": None, "owner_task_id": None} for account in accounts]
    requests = []
    errors = []

    def api(route):
        request = route.request
        path = request.url.split("/api/v1", 1)[1].split("?", 1)[0]
        requests.append((request.method, path))
        assert request.method == "GET", "Selection must never start a game operation"
        if path == "/events":
            return route.fulfill(content_type="text/event-stream", body="retry: 60000\n: connected\n\n")
        data = {"/auth/session": {"authenticated": True}, "/accounts": accounts,
                "/sessions": sessions, "/tasks": [], "/scripts": [], "/egress-groups": []}
        assert path in data, path
        route.fulfill(json=data[path])

    with (output / f"account-performance-{options.label}-server.log").open("w") as log:
        server = subprocess.Popen([shutil.which("node"), str(frontend / "node_modules/vite/bin/vite.js"),
            "preview", "--host", "127.0.0.1", "--port", "5187", "--strictPort"],
            cwd=frontend, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            client = build_opener(ProxyHandler({}))
            deadline = time.monotonic() + 15
            while True:
                if server.poll() is not None:
                    raise RuntimeError("Preview failed to start; see server log")
                try:
                    with client.open(url, timeout=1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, channel="msedge")
                page = browser.new_page(viewport={"width": 1440, "height": 1080})
                page.set_default_timeout(60000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("**/api/v1/**", api)
                page.goto(url)
                first = page.get_by_role("checkbox", name="选择账号 模拟账号 0000", exact=True)
                first.wait_for(state="visible", timeout=60000)
                # Let the event stream's initial resync finish before measuring.
                page.wait_for_timeout(500)
                before_requests = len(requests)
                measure = """async (name) => {
                    const input = document.querySelector(`input[aria-label="${name}"]`);
                    const start = performance.now(); input.click();
                    await new Promise(requestAnimationFrame);
                    await new Promise(requestAnimationFrame);
                    return performance.now() - start;
                }"""
                single = [page.evaluate(measure, "选择账号 模拟账号 0000") for _ in range(6)]
                all_accounts = [page.evaluate(measure, "选择全部账号") for _ in range(4)]
                expect(first).not_to_be_checked()
                rows = page.locator(".accounts-panel tbody tr").count()
                assert not errors, errors
                page.screenshot(path=output / f"account-performance-{options.label}-desktop.png")
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=output / f"account-performance-{options.label}-mobile.png")
                result = {"accounts": len(accounts), "rendered_rows": rows, "browser": browser.version,
                    "single_toggle_ms": single, "single_median_ms": statistics.median(single),
                    "all_toggle_ms": all_accounts, "all_median_ms": statistics.median(all_accounts),
                    "requests_during_selection": len(requests) - before_requests, "page_errors": errors}
                (output / f"account-performance-{options.label}.json").write_text(
                    json.dumps(result, indent=2), encoding="utf-8")
                print(json.dumps(result))
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    main()
