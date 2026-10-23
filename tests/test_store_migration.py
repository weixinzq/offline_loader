"""Durability, migration safety, and public capability boundary checks."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.service.accounts import AccountService
from src.service.contracts import ServiceError, TaskSpec
from src.service.events import EventHub
from src.service.execution import MessageService, ScriptContext
from src.service.instance_lock import InstanceLock
from src.service.migration import convert_legacy_job
from src.service.store import Store
from src.scripting.loader import discover_scripts


class StoreMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory / "service")
        self.events = EventHub(queue_size=2)
        self.sessions = type("Sessions", (), {"is_busy": lambda _, key: False})()
        self.accounts = AccountService(self.store, self.sessions, self.events)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def account(self, label="fixture", password="fixture-password"):
        return self.accounts.create({"label": label, "account": f"login-{label}", "password": password})

    def test_restart_preserves_accounts_and_interrupts_without_replaying(self):
        account = self.account()
        task_id = "a1b2c3"
        self.store.save_task({"task_id": task_id, "state": "running", "account_results": [
            {"account_id": account["account_id"], "state": "running"}], "created_at": "2026-10-15"})
        self.store.close()
        self.store = Store(self.directory / "service")
        self.store.interrupt_pending()
        self.assertEqual(self.store.get_task(task_id)["state"], "interrupted")
        self.assertEqual(self.store.get_task(task_id)["account_results"][0]["state"], "interrupted")
        self.assertEqual(self.store.list_accounts(), [account])
        self.assertNotIn("password", account)
        self.assertEqual(self.store.get_account(account["account_id"], private=True)["password"], "fixture-password")
        if os.name != "nt":
            self.assertEqual((self.directory / "service").stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.directory / "service/service.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_upgrade_from_initial_schema_preserves_accounts_without_assigning_groups(self):
        account = self.account()
        with self.store.engine.begin() as connection:
            connection.exec_driver_sql('DROP TABLE egress_groups')
            connection.exec_driver_sql("UPDATE alembic_version SET version_num='0001'")
        self.store.close()
        self.store = Store(self.directory / 'service')
        self.assertEqual(self.store.list_accounts(), [account])
        self.assertEqual(self.store.list_egress_groups(), [])
        self.assertIsNone(self.store.get_account(account['account_id']).get('egress_group_id'))

    def test_redaction_never_corrupts_generated_identifiers(self):
        account = self.account(password="a")
        task_id = "aaaa01234"
        self.store.save_task({"task_id": task_id, "state": "failed", "account_results": [
            {"account_id": account["account_id"], "state": "failed",
             "network_write": {"status": "not_attempted", "count": 0},
             "business_confirmation": "unknown"}],
            "error": {"code": "service_restart", "message": "password=a"}})
        result = self.store.get_task(task_id)
        self.assertEqual(result["task_id"], task_id)
        self.assertEqual(result["account_results"][0]["account_id"], account["account_id"])
        self.assertEqual(result["account_results"][0]["network_write"]["status"], "not_attempted")
        self.assertEqual(result["error"]["code"], "service_restart")
        self.assertNotIn("password=a", result["error"]["message"])
        self.assertEqual(self.store.redact({"status": "a", "task_id": "a"}),
                         {"status": "<redacted>", "task_id": "<redacted>"})

    def test_script_discovery_does_not_import_test_tools(self):
        import importlib
        with patch("src.scripting.loader.importlib.import_module", wraps=importlib.import_module) as imported:
            scripts = discover_scripts()
        self.assertTrue(any(script.module_name == "scripts.rank_info" for script in scripts))
        self.assertFalse(any(call.args[0].startswith("scripts.test_") for call in imported.call_args_list))

    def test_log_pagination_redacts_runtime_sid_and_password(self):
        self.account()
        self.store.add_secret("runtime-only-sid")
        first = self.store.append_log("task", None, "password=fixture-password sid=runtime-only-sid")
        second = self.store.append_log("task", None, "next")
        self.assertNotIn("fixture-password", first["message"])
        self.assertNotIn("runtime-only-sid", first["message"])
        self.assertEqual(self.store.get_logs("task", after=first["id"], limit=1), [second])

    def test_account_import_checks_all_inputs_before_writing(self):
        existing = self.account()
        with self.assertRaises(ServiceError):
            self.accounts.import_config({"accounts": [
                {"label": "other", "account": "fake-other", "password": "private"},
                {"label": "fixture", "account": "fake-fixture", "password": "private"}]})
        self.assertEqual(self.accounts.list(), [existing])

    def test_edit_busy_account_is_rejected_without_replacing_others(self):
        first, second = self.account("first"), self.account("second")
        self.sessions.is_busy = lambda key: key == first["account_id"]
        with self.assertRaises(ServiceError):
            self.accounts.update(first["account_id"], {"label": "changed"})
        self.accounts.update(second["account_id"], {"label": "changed"})
        self.assertEqual(self.accounts.get(first["account_id"]), first)

    def test_instance_lock_rejects_second_owner_and_can_be_reacquired(self):
        first = InstanceLock(self.directory / "runtime.lock")
        second = InstanceLock(self.directory / "runtime.lock")
        first.acquire()
        try:
            with self.assertRaises(ServiceError):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()

    def test_legacy_conversion_preserves_source_and_removes_proxy_settings(self):
        account = self.account()
        source = self.directory / "messages.txt"
        source.write_text('#time=0|', encoding="utf-8")
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        raw = {"clash": {"binary": "unused"}, "workers": [{"id": "old-worker",
               "accounts": {"labels": ["fixture"]}, "proxy": {"node": "unused"},
               "steps": [{"kind": "messages", "file": "messages.txt"}]}]}
        converted = convert_legacy_job(raw, [account], self.directory)
        template = TaskSpec.model_validate(converted["tasks"][0])
        self.assertEqual(template.account_ids, [account["account_id"]])
        self.assertEqual(template.steps[0].messages[0].cmd, "#time")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        self.assertFalse(converted["source_modified"])
        self.assertNotIn("proxy", converted["tasks"][0])

    def test_message_preflight_cannot_open_a_connection_and_rejects_session_replay(self):
        service = MessageService()
        with patch("asyncio.open_connection", side_effect=AssertionError("network during preflight")):
            self.assertEqual(service.parse("#wait|#time=0|")["count"], 2)
            with self.assertRaises(ServiceError):
                service.parse('#send={"id":18,"cmd":"55_2","param":{}}|')

    def test_slow_event_consumers_are_bounded(self):
        queue = self.events.subscribe()
        for index in range(1000):
            self.events.publish("task", {"sequence": index})
        self.assertEqual(queue.qsize(), 2)
        self.assertEqual(queue.get_nowait()["data"]["sequence"], 998)
        self.events.unsubscribe(queue)
        self.assertEqual(len(self.events._subscribers), 0)

    def test_script_capabilities_have_no_receive_loop_or_close_method(self):
        context = type("Context", (), {"socket": object(), "messages": object()})()
        exposed = ScriptContext(context)
        self.assertFalse(hasattr(exposed.socket, "recv_message"))
        self.assertFalse(hasattr(exposed.socket, "close"))
        self.assertFalse(hasattr(exposed, "config"))
        self.assertFalse(hasattr(exposed, "login_result"))


if __name__ == "__main__":
    unittest.main()
