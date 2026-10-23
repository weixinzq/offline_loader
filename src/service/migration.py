"""One-time conversion of legacy task files; source files stay untouched."""
from pathlib import Path

from src.service.contracts import ServiceError, TaskSpec
from src.service.execution import MessageService


def convert_legacy_job(raw, accounts, base_dir: Path):
    """Map legacy account selections to imported stable IDs, one task per worker."""
    templates, reports = [], []
    by_label = {row["label"]: row for row in accounts}
    source_accounts = accounts
    if raw.get("accounts_file"):
        import json
        from src.accounts.manager import load_account_specs
        source = (Path(base_dir) / raw["accounts_file"]).resolve()
        specs = load_account_specs(json.loads(source.read_text(encoding="utf-8-sig")))
        source_accounts = []
        for spec in specs:
            row = by_label.get(spec.label)
            if row is None or row["account"] != spec.config["account"]:
                raise ServiceError("migration_account_missing", "请先导入任务引用的账号配置，标签与登录账号必须一致")
            source_accounts.append(row)
    messages = MessageService()
    for worker in raw.get("workers", []):
        selected = set()
        selection = worker.get("accounts", {})
        for bounds in selection.get("ranges", []):
            if (not isinstance(bounds, list) or len(bounds) != 2 or
                    any(isinstance(n, bool) or not isinstance(n, int) for n in bounds) or
                    not 1 <= bounds[0] <= bounds[1] <= len(source_accounts)):
                raise ServiceError("invalid_migration", "旧任务账号区间无效")
            selected.update(row["account_id"] for row in source_accounts[bounds[0] - 1:bounds[1]])
        for label in selection.get("labels", []):
            if label not in by_label:
                raise ServiceError("migration_account_missing", "旧任务引用未导入账号标签")
            selected.add(by_label[label]["account_id"])
        steps = []
        for step in worker.get("steps", []):
            if step.get("kind") == "script":
                steps.append({"kind": "script", "script_id": step["module"].removeprefix("scripts.")})
            elif step.get("kind") == "messages":
                if "file" in step:
                    text = (Path(base_dir) / step["file"]).resolve().read_text(encoding="utf-8-sig")
                    parsed = messages.parse(text)["messages"]
                else:
                    parsed = messages.validate(step["messages"])["messages"]
                steps.append({"kind": "messages", "messages": parsed})
            else:
                raise ServiceError("invalid_migration", "旧任务含未知步骤")
        spec = TaskSpec(account_ids=sorted(selected), steps=steps, mode=worker.get("mode", "bounded"),
                        concurrency=worker.get("concurrency", 5), repetitions=worker.get("repetitions", 1),
                        repeat_interval=worker.get("repeat_interval", 0.5), message_delay=worker.get("message_delay", 0.2))
        templates.append(spec.model_dump())
        reports.append({"worker": worker.get("id"), "accounts": len(selected), "converted": True})
    if not templates:
        raise ServiceError("invalid_migration", "旧任务没有 workers")
    return {"tasks": templates, "reports": reports, "source_modified": False,
            "removed_configuration": ["clash", "workers[].proxy"], "network": "external_clash_tun"}
