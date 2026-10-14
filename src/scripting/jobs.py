"""Read and preflight configuration-driven CLI jobs without opening connections."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re

import yaml

from src.accounts.manager import load_account_specs
from src.config import default_config
from src.messaging.dispatcher import _build_dispatch_plan
from src.messaging.parser import (
    parse_send_message, parse_send_message_file,
)
from src.scripting.composer import MessageBatchStep, ScriptStep
from src.scripting.loader import discover_scripts


def positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须是正整数")
    return value


def seconds(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} 必须是非负有限秒数")
    return value


def message_dict(message):
    return {"id": message.id, "cmd": message.cmd, "param": message.param}


def decode_message(raw):
    if not isinstance(raw, dict):
        raise ValueError("messages 中每项必须是消息对象")
    if raw.get("id") == -1:
        if raw.get("cmd") == "#wait" and raw.get("param") == {}:
            return parse_send_message("#wait|")
        if raw.get("cmd") == "#time":
            duration = seconds(raw.get("param", {}).get("seconds"), "#time.seconds")
            return parse_send_message(f"#time={duration}|")
        raise ValueError("未知本地消息指令")
    return parse_send_message("#send=" + json.dumps(raw, ensure_ascii=False) + "|")


def normalize_steps(raw, base):
    if not isinstance(raw, list) or not raw:
        raise ValueError("steps 必须是非空数组")
    modules = {script.module_name for script in discover_scripts()}
    result = []
    for step in raw:
        if not isinstance(step, dict):
            raise ValueError("组合步骤必须是对象")
        if step.get("kind") == "script":
            module = step.get("module")
            if module not in modules:
                raise ValueError("专用脚本不在可选脚本列表中")
            result.append({"kind": "script", "module": module})
        elif step.get("kind") == "messages":
            if ("messages" in step) == ("file" in step):
                raise ValueError("消息步骤必须二选一指定 messages 或 file")
            if "file" in step:
                messages = parse_send_message_file((base / step["file"]).resolve())
            else:
                if not isinstance(step["messages"], list):
                    raise ValueError("messages 必须是数组")
                messages = [decode_message(item) for item in step["messages"]]
            if not messages:
                raise ValueError("消息步骤不能为空")
            segment = []
            for message in messages:
                if message.id == -1 and message.cmd == "#wait":
                    if segment:
                        _build_dispatch_plan(segment)
                    segment = []
                else:
                    segment.append(message)
            if segment:
                _build_dispatch_plan(segment)
            result.append({"kind": "messages", "messages": [message_dict(m) for m in messages]})
        else:
            raise ValueError("组合步骤只支持 messages 和 script")
    return result


def build_steps(raw):
    modules = {script.module_name: script for script in discover_scripts()}
    return tuple(
        ScriptStep(modules[step["module"]]) if step["kind"] == "script"
        else MessageBatchStep(tuple(decode_message(m) for m in step["messages"]))
        for step in raw
    )


def load_job(path: Path):
    path = path.resolve()
    base = path.parent
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    specs = load_account_specs(json.loads((base / raw["accounts_file"]).read_text(encoding="utf-8-sig")))
    clash = raw["clash"]
    binary = (base / clash["binary"]).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Mihomo 可执行文件不存在")
    node_file = (base / clash["config"]).resolve()
    nodes = yaml.safe_load(node_file.read_text(encoding="utf-8-sig"))
    if not isinstance(nodes, dict):
        raise ValueError("Clash 节点配置必须是 YAML 对象")
    # Import only outbound definitions; never enable the input's TUN or ports.
    nodes = {key: deepcopy(nodes[key]) for key in ("proxies", "proxy-groups", "proxy-providers") if key in nodes}
    provider_files = {}
    provider_nodes = []
    for name, provider in nodes.get("proxy-providers", {}).items():
        if provider.get("type") == "file":
            provider_file = (node_file.parent / provider["path"]).resolve()
            if not provider_file.is_relative_to(node_file.parent):
                raise ValueError("文件代理集合必须位于节点配置目录内")
            content = yaml.safe_load(provider_file.read_text(encoding="utf-8-sig"))
            provider_files[name] = content
            provider_nodes.extend(content["proxies"])
    proxies = {p["name"]: p for p in nodes.get("proxies", []) + provider_nodes}
    groups = {p["name"]: p for p in nodes.get("proxy-groups", [])}
    names = set(proxies) | set(groups)
    def check_route(name, visited=None):
        visited = set() if visited is None else visited
        if name in visited:
            raise ValueError("代理组路由循环")
        if name in {"DIRECT", "PASS"} or proxies.get(name, {}).get("type") in {"direct", "pass"}:
            raise ValueError("任务代理路由不能包含直连出口")
        if name in groups:
            for target in groups[name].get("proxies", []):
                check_route(target, visited | {name})
    if not isinstance(raw.get("workers"), list) or not raw["workers"]:
        raise ValueError("workers 必须是非空数组")
    ids, ports, accounts = set(), set(), {}
    workers = []
    for worker in raw["workers"]:
        worker_id = worker["id"]
        if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", worker_id) or worker_id in ids or worker_id == "mihomo":
            raise ValueError("进程 id 无效或重复")
        ids.add(worker_id)
        selection = worker["accounts"]
        selected = set()
        for bounds in selection.get("ranges", []):
            if not isinstance(bounds, list) or len(bounds) != 2:
                raise ValueError("账号区间必须是 [起始序号, 结束序号]")
            first, last = (positive_int(v, "账号序号") for v in bounds)
            if last < first or last > len(specs):
                raise ValueError("账号区间越界")
            selected.update(range(first - 1, last))
        for label in selection.get("labels", []):
            matches = [i for i, spec in enumerate(specs) if spec.label == label]
            if not matches:
                raise ValueError("账号标签不存在")
            selected.update(matches)
        if not selected:
            raise ValueError("进程必须选择至少一个账号")
        entries = []
        for index in sorted(selected):
            spec = specs[index]
            account = spec.config["account"]
            if account in accounts:
                raise ValueError(f"登录账号重复：序号 {accounts[account]} 与 {index + 1}，不同角色也不能同时运行")
            accounts[account] = index + 1
            entries.append({"index": index + 1, "label": spec.label,
                            "config": {**default_config(), **spec.config}})
        proxy = worker["proxy"]
        port = positive_int(proxy["port"], "代理端口")
        if port <= 1024 or port > 65535 or port in ports:
            raise ValueError("代理端口须大于 1024 且不能重复")
        ports.add(port)
        if proxy["node"] not in names or proxy["node"] in {"DIRECT", "REJECT"}:
            raise ValueError("代理节点不存在或不是可用代理节点")
        check_route(proxy["node"])
        mode = worker.get("mode", "bounded")
        if mode not in {"bounded", "all_online"}:
            raise ValueError("mode 必须是 bounded 或 all_online")
        workers.append({
            "id": worker_id, "accounts": entries, "proxy": dict(proxy), "mode": mode,
            "concurrency": positive_int(worker.get("concurrency", 5), "concurrency"),
            "repetitions": positive_int(worker.get("repetitions", 1), "repetitions"),
            "repeat_interval": seconds(worker.get("repeat_interval", 0.5), "repeat_interval"),
            "message_delay": seconds(worker.get("message_delay", 0.2), "message_delay"),
            "steps": normalize_steps(worker["steps"], base),
        })
    return {"binary": str(binary), "nodes": nodes, "provider_files": provider_files, "workers": workers}


def mihomo_config(job, directory):
    config = deepcopy(job["nodes"])
    for number, (name, provider) in enumerate(config.get("proxy-providers", {}).items()):
        path = directory / "providers" / f"{number}.yaml"
        provider["path"] = str(path)
        if name in job["provider_files"]:
            path.write_text(yaml.safe_dump(job["provider_files"][name], allow_unicode=True), encoding="utf-8")
            path.chmod(0o600)
    config.update({
        "allow-lan": False, "log-level": "warning", "mode": "rule",
        "listeners": [{"name": w["id"], "type": "socks", "listen": "127.0.0.1",
                       "port": w["proxy"]["port"], "proxy": w["proxy"]["node"]}
                      for w in job["workers"]],
        "rules": ["MATCH,REJECT"],
    })
    return config
