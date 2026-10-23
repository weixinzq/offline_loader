"""Optional browser smoke test: all API requests are intercepted; no game traffic.

Run from the repository root with a Python environment containing Playwright:
  python frontend/tests/browser_smoke.py --channel msedge
The Vue development server must already be listening on --url.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5179")
    parser.add_argument("--channel", default="msedge")
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / "test-results"
    output.mkdir(exist_ok=True)
    account = {"account_id": "a1", "label": "浏览器测试账号", "account": "smoke-user", "char_id": 0, "zone_index": 1}
    task = {
        "task_id": "browser-smoke-task", "kind": "execute", "state": "succeeded", "error": None,
        "created_at": "2026-10-15T08:00:00Z", "started_at": None, "finished_at": None, "spec": {},
        "account_results": [{"account_id": "a1", "state": "succeeded", "error": None,
                             "completed_repetitions": 1, "network_write": {"status": "succeeded", "count": 1},
                             "business_confirmation": "unknown", "step_results": []}],
    }
    authenticated = False
    submitted = False
    preflighted = False
    groups = []
    assigned = False
    errors: list[str] = []

    def api(route):
        nonlocal authenticated, submitted, preflighted, assigned
        request = route.request
        path = request.url.split("/api/v1", 1)[1].split("?", 1)[0]
        data = request.post_data_json if request.post_data else None
        if path == "/auth/session":
            if request.method == "POST":
                assert data == {"token": "browser-only-secret"}
                authenticated = True
            return route.fulfill(status=200 if authenticated else 401, json={"authenticated": authenticated})
        assert authenticated, "Only authentication is allowed before login"
        if path == "/accounts":
            result = [account]
        elif path == "/sessions":
            result = [{"account_id": "a1", "label": account["label"], "state": "ready", "error": None, "owner_task_id": None}]
        elif path == "/scripts":
            result = []
        elif path == "/egress-groups":
            if request.method == 'POST':
                assert data['max_online'] == 30 and data['routing_confirmed'] is False
                groups.append({**data, 'group_id': 'g1', 'online_count': 0, 'reserved_count': 0,
                    'worker_state': 'offline', 'worker_pid': None, 'worker_error': None,
                    'worker_path': 'D:/test/egress-workers/g1/AolaWorker.exe', 'exit_ip': None, 'exit_checked_at': None})
                result = groups[0]
            else:
                result = groups
        elif path == '/egress-groups/rules':
            result = {'yaml': 'mode: rule\nfind-process-mode: strict\nrules:\n- PROCESS-PATH,D:/test/egress-workers/g1/AolaWorker.exe,测试节点',
                      'instructions': '合并至当前规则最前面'}
        elif path == '/egress-groups/g1/probe':
            groups[0].update(exit_ip='192.0.2.10', worker_state='ready', worker_pid=12345, exit_checked_at='2026-10-15T12:00:00Z')
            result = groups[0]
        elif path == '/egress-groups/g1':
            assert request.method == 'PUT'
            groups[0].update(data)
            result = groups[0]
        elif path == '/accounts/assign-egress':
            assert data == {'account_ids': ['a1'], 'egress_group_id': 'g1'}
            account['egress_group_id'] = 'g1'
            assigned = True
            result = {'updated': 1, 'egress_group_id': 'g1'}
        elif path == "/messages/parse":
            assert data == {"text": "浏览器测试消息"}
            result = {"valid": True, "messages": [{"id": 1, "cmd": "42", "param": {}}], "count": 1}
        elif path == "/tasks/validate":
            assert data["account_ids"] == ["a1"]
            assert data["session_policy"] == "temporary"
            preflighted = True
            result = {"valid": True, "account_count": 1, "step_count": 1, "spec": data, "required_capacity": 1, "max_online": 100}
        elif path == "/tasks":
            if request.method == "POST":
                assert preflighted
                assert data["steps"] == [{"kind": "messages", "messages": [{"id": 1, "cmd": "42", "param": {}}]}]
                submitted = True
                task["spec"] = data
                result = task
            else:
                result = [task] if submitted else []
        elif path == "/tasks/browser-smoke-task":
            result = task
        elif path == "/tasks/browser-smoke-task/logs":
            result = [{"id": 1, "task_id": task["task_id"], "account_id": "a1", "timestamp": task["created_at"], "message": "模拟任务完成"}]
        elif path == "/events":
            return route.fulfill(content_type="text/event-stream", body="retry: 60000\n: connected\n\n")
        else:
            raise AssertionError(f"Unexpected API request: {request.method} {path}")
        route.fulfill(json=result)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel=args.channel)
        page = browser.new_page(viewport={"width": 1440, "height": 1080}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/v1/**", api)
        page.goto(args.url)
        page.get_by_label("管理员凭据").fill("browser-only-secret")
        page.get_by_role("button", name="进入工作台").click()
        expect(page.get_by_text("浏览器测试账号", exact=True)).to_be_visible()
        page.get_by_role("button", name="编排任务", exact=False).first.click()
        page.get_by_role("checkbox", name="浏览器测试账号 smoke-user").check()
        page.get_by_label("添加消息").fill("浏览器测试消息")
        page.get_by_role("button", name="解析并添加").click()
        expect(page.get_by_text("发送 1 条消息")).to_be_visible()
        page.get_by_role("button", name="预检任务", exact=True).click()
        expect(page.get_by_text("预检未登录账号或发送游戏消息。")).to_be_visible()
        page.get_by_role("button", name="提交任务", exact=True).click()
        expect(page.get_by_role("table").last.get_by_text("未知", exact=True)).to_be_visible()
        expect(page.get_by_role("log")).to_contain_text("模拟任务完成")
        page.screenshot(path=output / "desktop.png", full_page=True)
        assert page.evaluate("localStorage.length") == 0
        assert page.evaluate("sessionStorage.length") == 0
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=output / "mobile.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile page overflows horizontally"
        page.set_viewport_size({'width': 1440, 'height': 1080})
        page.get_by_role('button', name='出口分组', exact=False).click()
        page.get_by_label('分组名称', exact=True).fill('测试出口')
        page.get_by_label('Clash 节点或策略名称').fill('测试节点')
        page.get_by_role('button', name='创建分组', exact=True).click()
        expect(page.locator('article')).to_contain_text('测试出口')
        page.get_by_role('button', name='生成 Clash 规则').click()
        expect(page.get_by_label('Clash 规则片段')).to_have_value(re.compile('.*PROCESS-PATH.*', re.S))
        page.get_by_role('button', name='检测出口', exact=True).click()
        expect(page.locator('article')).to_contain_text('192.0.2.10')
        page.get_by_role('button', name='编辑', exact=True).click()
        page.get_by_role('checkbox', name='已在 Clash 中启用本组进程规则，并核对出口').check()
        page.get_by_role('button', name='保存分组', exact=True).click()
        expect(page.locator('article')).to_contain_text('规则已确认')
        page.screenshot(path=output / 'egress-desktop.png', full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path=output / 'egress-mobile.png', full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Group page overflows horizontally'
        page.set_viewport_size({'width': 1440, 'height': 1080})
        page.get_by_role('button', name='账号管理', exact=False).first.click()
        page.get_by_role('checkbox', name='选择账号 浏览器测试账号').check()
        page.get_by_label('选择出口分组').select_option('g1')
        page.get_by_role('button', name='分配所选账号').click()
        expect(page.locator('.accounts-panel tbody tr')).to_contain_text('测试出口')
        assert assigned
        assert submitted and preflighted
        assert not errors, errors
        browser.close()
    report = {"passed": True, "mock_api": True, "submitted": submitted, "preflighted": preflighted, 'egress_assigned': assigned,
              "viewports": ["1440x1080", "390x844"], "page_errors": errors}
    (output / "browser-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
