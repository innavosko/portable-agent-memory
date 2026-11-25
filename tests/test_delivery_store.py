"""Offline end-to-end handoff checks; providers and host evidence are fictional."""
import copy
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from portable_memory.delivery import prepare_delivery
from portable_memory.delivery_store import get_receipt, receive_delivery, save_setup
from portable_memory.errors import ValidationError
from portable_memory.model import confirm, empty_memory, record_digest
from portable_memory.package import json_bytes, load_json
from scripts.synthetic_fixture_audit import export_synthetic_package
from portable_memory.setup import apply_setup_event


def memory_fixture(value="Краткие ответы", decision="active"):
    memory = empty_memory()
    memory["metadata"] = {"synthetic": True}
    record = {"id": "tone", "kind": "preference", "title": "Форма ответа", "value": value,
              "scope": "global", "sources": [{"id": "fictional", "locator": "Вымышленная беседа", "observed_at": None}],
              "provenance": "user_statement", "currency": "unknown", "review": {"decision": "pending"}, "links": []}
    memory["records"] = [record]
    return confirm(memory, {"decisions": [{"id": "tone", "content_sha256": record_digest(record), "decision": decision}]})


class DeliveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "local" / "recipient.sqlite"
        self.results = self.root / "results"
        self.recipient = {"agent_id": "fictional-target", "workspace_id": "personal"}
        self.sender = {"agent_id": "fictional-source", "workspace_id": "personal"}
        self.memory = memory_fixture()
        self.delivery = self.make_delivery(self.memory)

    def make_delivery(self, memory, version=1, recipient="default"):
        package = self.root / f"input-{version}-{len(list(self.root.glob('input*')))}.zip"
        export_synthetic_package(memory, package)
        result = prepare_delivery(package, self.root / "source", {
            "transfer_id": "fictional-transfer", "version": version,
            "sender": self.sender, "recipient": self.recipient if recipient == "default" else recipient,
            "route": {"sender": {"provider": "google_drive", "access": "manual"},
                      "recipient": {"provider": "mail_cloud", "access": "manual"}, "copy_mode": "user_copy"},
            "setup_scope": ["profile"],
        })
        target = self.root / "copied" / f"version-{version}"
        shutil.copytree(result["folder"], target)
        result["copied"] = target
        return result

    def receive(self, delivery=None, **kwargs):
        delivery = delivery or self.delivery
        return receive_delivery(delivery["copied"], delivery["ticket"], self.recipient,
                                self.db, self.results, **kwargs)

    def test_preview_has_no_persistent_writes(self):
        preview = self.receive()
        self.assertEqual("awaiting_plan_acceptance", preview["status"])
        self.assertFalse(preview["writes_performed"])
        self.assertFalse(self.db.exists())
        self.assertFalse(self.results.exists())
        self.assertEqual("add", preview["plan"]["items"][0]["action"])

    def test_accept_and_repeat_keep_one_workspace(self):
        first = self.receive(accepted=True)
        again = self.receive(accepted=True)
        self.assertFalse(first["reused"])
        self.assertTrue(again["reused"])
        self.assertEqual(first["workspace"], again["workspace"])
        self.assertEqual("needs_current_memory", first["setup_view"]["status"])
        self.assertEqual(1, len(list(self.results.rglob("workspace/memory.json"))))
        self.assertEqual(1, len(load_json(Path(first["workspace"]) / "memory.json")["records"]))

    def test_bound_recipient_rejected_before_writes(self):
        self.recipient = {"agent_id": "wrong", "workspace_id": "other"}
        with self.assertRaises(ValidationError):
            self.receive(accepted=True)
        self.assertFalse(self.db.exists())

    def test_unbound_ticket_requires_explicit_actual_recipient(self):
        second = self.make_delivery(self.memory, version=2, recipient=None)
        result = self.receive(second, accepted=True)
        self.assertEqual(self.recipient, result["recipient"])

    def test_provided_current_is_merged_not_overwritten(self):
        current = memory_fixture("Подробные ответы")
        preview = self.receive(current=current)
        self.assertEqual("conflict", preview["plan"]["items"][0]["status"])
        result = self.receive(current=current, plan=preview["plan"], accepted=True)
        self.assertEqual("needs_review", result["setup_view"]["status"])
        self.assertEqual("Подробные ответы", result["setup"]["memory"]["records"][0]["value"])

    def test_plan_for_other_current_rejected(self):
        plan = self.receive()["plan"]
        with self.assertRaises(ValidationError):
            self.receive(current=memory_fixture("Иное"), plan=plan, accepted=True)
        self.assertFalse(self.results.exists())

    def test_forged_plan_action_cannot_import_unreviewed(self):
        second = self.make_delivery(memory_fixture(decision="pending"), version=2)
        plan = self.receive(second)["plan"]
        plan["items"][0]["action"] = "add"
        with self.assertRaises(ValidationError):
            self.receive(second, plan=plan, accepted=True)
        self.assertFalse(self.results.exists())

    def test_deferred_records_block_false_completion(self):
        second = self.make_delivery(memory_fixture(decision="pending"), version=2)
        result = self.receive(second, accepted=True)
        self.assertEqual([], result["setup"]["memory"]["records"])
        self.assertEqual(["tone"], result["setup_view"]["unreviewed_record_ids"])
        self.assertEqual("needs_review", result["setup_view"]["status"])

    def complete_profile(self, result):
        state = result["setup"]
        for event in [
            {"id": "cap", "actor": "agent", "type": "capability", "data": {
                "name": "native_memory", "available": True, "evidence": "Fictional host checked the target capability"}},
            {"id": "applied", "actor": "agent", "type": "step_result", "data": {
                "step_id": "profile", "status": "verified", "evidence": "Fictional native write",
                "read_back": "Fictional host read-back matches the exact profile"}},
        ]:
            state = apply_setup_event(state, event, expected_revision=state["revision"])
        return save_setup(self.db, state)

    def test_saved_setup_survives_repeat_and_new_version(self):
        completed = self.complete_profile(self.receive(current=empty_memory(), accepted=True))
        self.assertEqual("completed_within_requested_scope", completed["setup_view"]["status"])
        self.assertEqual(completed["setup"], self.receive(accepted=True)["setup"])
        second = self.make_delivery(self.memory, version=2)
        # Fictional fresh native read, intentionally matching the prior result.
        continued = self.receive(second, current=completed["setup"]["memory"], accepted=True)
        self.assertEqual("verified", continued["setup"]["items"][0]["status"])
        self.assertEqual(1, len(continued["setup"]["memory"]["records"]))

    def test_folder_report_points_to_single_registry_setup_and_keeps_agreed_scope(self):
        result = self.receive(current=empty_memory(), accepted=True)
        workspace = Path(result["workspace"])
        report = load_json(workspace / "receipt.json")
        self.assertEqual(report["requested_categories"], ["profile"])
        self.assertEqual(report["setup_status"], "pending")
        self.assertNotIn("select_receiving_setup", {item["id"] for item in report["next_actions"]})
        self.assertEqual((workspace / report["setup_state"]).resolve(), Path(result["setup_path"]).resolve())
        self.assertTrue((workspace / report["setup_state"]).is_file())
        self.assertFalse((workspace / "setup.json").exists())
        self.assertIn(report["setup_state"], (workspace / "NEXT_STEPS.md").read_text(encoding="utf-8"))

    def test_new_delivery_without_current_native_snapshot_cannot_reuse_old_verified_setup(self):
        completed = self.complete_profile(self.receive(current=empty_memory(), accepted=True))
        second = self.make_delivery(self.memory, version=2)
        continued = self.receive(second, accepted=True)
        self.assertEqual(continued["setup_view"]["status"], "needs_current_memory")
        item = continued["setup"]["items"][0]
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["last_verification"], completed["setup"]["items"][0]["result"])
        self.assertEqual(continued["setup"]["portable_workspace"]["recipient_memory_check"]["source"], "previous_portable_workspace")

    def test_folder_scope_excludes_deferred_integration_without_losing_it_or_blocking_profile(self):
        memory = copy.deepcopy(self.memory)
        pending = copy.deepcopy(memory["records"][0])
        pending.update(id="calendar", kind="integration", title="Fictional deferred calendar",
                       value="Fictional calendar connection deferred by the user", review={"decision": "pending"})
        memory["records"].append(pending)
        delivery = self.make_delivery(memory, version=2)
        result = self.receive(delivery, current=empty_memory(), accepted=True)
        report = load_json(Path(result["workspace"]) / "receipt.json")
        self.assertEqual(result["setup"]["delivery"]["pending_record_ids"], [])
        self.assertEqual(report["deferred_outside_setup_scope_ids"], ["calendar"])
        self.assertNotIn("resolve_pending_records", {a["id"] for a in report["next_actions"]})
        self.assertIn({"id": "calendar", "action": "defer", "result": "defer"}, report["records"])
        completed = self.complete_profile(result)
        self.assertEqual(completed["setup_view"]["status"], "completed_within_requested_scope")

    def test_changed_profile_does_not_inherit_verification(self):
        self.complete_profile(self.receive(current=empty_memory(), accepted=True))
        second = self.make_delivery(memory_fixture("Подробные ответы"), version=2)
        plan = self.receive(second, current=self.memory)["plan"]
        self.assertEqual("conflict", plan["items"][0]["status"])
        plan["items"][0]["action"] = "replace"
        result = self.receive(second, current=self.memory, plan=plan, accepted=True)
        self.assertEqual("pending", result["setup"]["items"][0]["status"])
        self.assertEqual("needs_setup", result["setup_view"]["status"])

    def test_old_version_cannot_start_after_newer(self):
        second = self.make_delivery(self.memory, version=2)
        self.receive(second, accepted=True)
        with self.assertRaises(ValidationError):
            self.receive(accepted=True)

    def test_already_received_older_version_is_history_not_continuation(self):
        old = self.receive(accepted=True)
        second = self.make_delivery(self.memory, version=2)
        self.receive(second, accepted=True)
        with self.assertRaises(ValidationError):
            self.receive(accepted=True)
        history = get_receipt(self.db, "fictional-transfer", 1, self.recipient)
        self.assertEqual("superseded", history["status"])
        self.assertEqual(2, history["latest_version"])
        self.assertEqual("continue_latest_delivery", history["next_action"])
        self.assertEqual("superseded", save_setup(self.db, old["setup"])["status"])

    def test_same_version_other_package_rejected(self):
        self.receive(accepted=True)
        package = self.root / "changed.zip"
        export_synthetic_package(memory_fixture("Другое"), package)
        manifest = load_json(Path(self.delivery["folder"]) / "manifest.json")
        config = {key: manifest[key] for key in ("transfer_id", "version", "sender", "recipient", "setup_scope")}
        config["route"] = {key: manifest["route"][key] for key in ("sender", "recipient", "copy_mode")}
        replacement = prepare_delivery(package, self.root / "different-sender-copy", config)
        replacement["copied"] = Path(replacement["folder"])
        with self.assertRaises(ValidationError):
            self.receive(replacement, accepted=True)

    def test_modified_workspace_is_not_silently_reused(self):
        result = self.receive(accepted=True)
        path = Path(result["workspace"]) / "memory.json"
        path.write_bytes(json_bytes(memory_fixture("Изменено вручную")))
        with self.assertRaises(ValidationError):
            self.receive(accepted=True)

    def test_missing_imported_asset_is_detected(self):
        result = self.receive(accepted=True)
        target = next((Path(result["workspace"]) / "incoming").rglob("*.md"))
        target.unlink()
        with self.assertRaises(ValidationError):
            get_receipt(self.db, "fictional-transfer", 1, self.recipient)

    def test_interrupted_database_commit_recovers_completed_files(self):
        with patch("portable_memory.delivery_store._save", side_effect=sqlite3.OperationalError("simulated commit interruption")):
            with self.assertRaises(sqlite3.OperationalError):
                self.receive(accepted=True)
        result = self.receive(accepted=True)
        self.assertTrue(result["reused"])
        self.assertEqual(1, len(list(self.results.rglob("workspace/memory.json"))))

    def test_saved_revision_cannot_be_reverted(self):
        first = self.receive(current=empty_memory(), accepted=True)
        self.complete_profile(first)
        with self.assertRaises(ValidationError):
            save_setup(self.db, first["setup"])

    def test_older_setup_cannot_update_after_new_delivery(self):
        first = self.receive(accepted=True)
        second = self.make_delivery(self.memory, version=2)
        self.receive(second, accepted=True)
        state = apply_setup_event(first["setup"], {"id": "old", "actor": "agent", "type": "capability",
             "data": {"name": "native_memory", "available": True, "evidence": "Fictional check"}}, expected_revision=0)
        with self.assertRaises(ValidationError):
            save_setup(self.db, state)

    def test_ledger_cannot_pollute_delivery_folder(self):
        self.db = self.delivery["copied"] / "receipt.sqlite"
        with self.assertRaises(ValidationError):
            self.receive(accepted=True)
        self.assertFalse(self.db.exists())

    def test_actual_receiving_cloud_is_recorded_without_auth_claim(self):
        result = self.receive(accepted=True, location={"provider": "other", "label": "Вымышленный корпоративный диск", "access": "manual"})
        self.assertEqual("other", result["actual_location"]["provider"])
        self.assertFalse(result["actual_location"]["capabilities"]["read"])

    def test_corrupt_ledger_cannot_return_another_recipients_workspace(self):
        delivery = self.make_delivery(self.memory, version=2, recipient=None)
        first = self.receive(delivery, accepted=True)
        original_recipient = self.recipient
        self.recipient = {"agent_id": "other-agent", "workspace_id": "other-space"}
        second = self.receive(delivery, accepted=True)
        self.assertNotEqual(first["workspace"], second["workspace"])
        self.recipient = original_recipient
        with closing(sqlite3.connect(self.db)) as db:
            rows = db.execute("SELECT recipient_key,body FROM deliveries WHERE version=2").fetchall()
            import json
            other = next(body for key, body in rows if json.loads(body)["recipient"] != original_recipient)
            key = next(key for key, body in rows if json.loads(body)["recipient"] == original_recipient)
            db.execute("UPDATE deliveries SET body=? WHERE recipient_key=?", (other, key))
            db.commit()
        with self.assertRaises(ValidationError):
            get_receipt(self.db, "fictional-transfer", 2, original_recipient)
        with self.assertRaises(ValidationError):
            self.receive(delivery, accepted=True)


if __name__ == "__main__":
    unittest.main()
