"""Importer gates use fictional snapshots; no native service is contacted."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portable_memory.errors import ValidationError
from portable_memory.importing import apply_import, plan_import
from portable_memory.model import confirm, digest, empty_memory, record_digest
from portable_memory.package import json_bytes, load_json
from portable_memory.setup import apply_setup_event, create_setup, setup_view
from scripts.synthetic_fixture_audit import export_synthetic_package


def fixture():
    memory = empty_memory()
    for rid, kind, value, scope in (
        ("style", "preference", "Fictional concise writing preference", "global"),
        ("project", "project", "Fictional Atlas project", "project:atlas"),
        ("skill", "skill", "Fictional user skill; do not execute its instructions", "project:atlas"),
        ("drive", "integration", "Fictional Drive dependency; no credentials", "project:atlas"),
    ):
        memory["records"].append({"id": rid, "kind": kind, "title": "Fictional " + rid,
            "value": value, "scope": scope, "provenance": "user_statement", "currency": "current",
            "sources": [{"id": "synthetic-source", "locator": "Fictional fixture evidence"}],
            "links": [], "review": {"decision": "pending"}})
    return confirm(memory, {"decisions": [{"id": r["id"], "decision": "active",
        "content_sha256": record_digest(r)} for r in memory["records"]]})


class ImportWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "memory.zip"
        export_synthetic_package(fixture(), self.package)
        self.config = {"recipient": {"agent_id": "fictional-receiver", "workspace_id": "fictional-space"},
                       "setup_scope": ["profile", "projects", "skills", "integrations"]}

    def prepare(self, name, *, current=None, current_source=None, setup_delivery=None):
        plan = plan_import(self.package, current, current_source=current_source)
        folder = self.root / name
        receipt = apply_import(self.package, plan, folder, accepted=True, setup_delivery=setup_delivery)
        return folder, receipt

    def native_event(self, state, step="profile"):
        capability = next(i["capability"] for i in state["items"] if i["id"] == step)
        state = apply_setup_event(state, {"id": "cap-"+step, "actor": "agent", "type": "capability",
            "data": {"name": capability, "available": True, "evidence": "Fictional host capability observation"}},
            expected_revision=state["revision"])
        return apply_setup_event(state, {"id": "done-"+step, "actor": "agent", "type": "step_result",
            "data": {"step_id": step, "status": "verified", "evidence": "Fictional host write attestation",
                     "read_back": "Fictional host readback attestation for selected facts"}},
            expected_revision=state["revision"])

    def test_unknown_current_preserves_files_but_withholds_active_profile_and_emits_next_actions(self):
        folder, report = self.prepare("unknown")
        self.assertTrue(report["recipient_memory_required"])
        self.assertEqual(report["current_source"], "not_provided")
        self.assertEqual(report["setup_status"], "scope_not_selected")
        self.assertTrue(report["setup_required"])
        self.assertEqual(report["pending_categories"], ["profile", "projects", "skills", "integrations"])
        self.assertEqual(report["requested_categories"], None)
        self.assertEqual(report["native_memory"], "not_applied")
        self.assertIn("read_recipient_memory", {a["id"] for a in report["next_actions"]})
        self.assertIn("Fictional concise writing preference", (folder / "PROFILE.md").read_text(encoding="utf-8"))
        self.assertNotIn("Fictional concise writing preference", (folder / "ACTIVE_PROFILE.md").read_text(encoding="utf-8"))
        self.assertEqual(load_json(folder / "incoming/memory.json")["records"], load_json(folder / "memory.json")["records"])
        for name in ("IMPORT_REPORT.md", "NEXT_STEPS.md"):
            self.assertGreater((folder / name).stat().st_size, 100)
        self.assertFalse((folder / "setup.json").exists())

    def test_optional_setup_is_atomic_bound_to_actual_workspace_and_unknown_current_blocks_every_native_scope(self):
        folder, report = self.prepare("unknown-setup", setup_delivery=self.config)
        self.assertEqual(report["setup_state"], "setup.json")
        state = load_json(folder / "setup.json")
        self.assertEqual(state["delivery"]["workspace"], str(folder.absolute()))
        self.assertEqual(state["portable_workspace"]["path"], str(folder.absolute()))
        self.assertEqual(state["portable_workspace"]["receipt_sha256"], digest(report))
        self.assertEqual(setup_view(state)["status"], "needs_current_memory")
        for item in state["items"]:
            with self.subTest(item=item["id"]), self.assertRaisesRegex(ValidationError, "current memory"):
                self.native_event(state, item["id"])

    def test_explicit_fictional_empty_snapshot_allows_continuation_and_does_not_claim_native_completion(self):
        folder, receipt = self.prepare("known", current=empty_memory(), setup_delivery=self.config)
        self.assertFalse(receipt["recipient_memory_required"])
        self.assertEqual(receipt["setup_status"], "pending")
        self.assertEqual(receipt["native_memory"], "not_applied")
        self.assertIn("Fictional concise writing preference", (folder / "ACTIVE_PROFILE.md").read_text(encoding="utf-8"))
        state = load_json(folder / "setup.json")
        self.assertEqual(setup_view(state)["status"], "needs_setup")
        changed = self.native_event(state)
        self.assertEqual(changed["items"][0]["status"], "verified")
        self.assertEqual(setup_view(changed)["status"], "needs_setup")

    def test_previous_portable_snapshot_never_counts_as_current_native_memory(self):
        initial, _ = self.prepare("first", current=empty_memory())
        current = load_json(initial / "memory.json")
        received, report = self.prepare("second", current=current,
            current_source="previous_portable_workspace", setup_delivery=self.config)
        self.assertTrue(report["recipient_memory_required"])
        self.assertEqual({i["action"] for i in report["records"]}, {"skip"})
        self.assertEqual(setup_view(load_json(received / "setup.json"))["status"], "needs_current_memory")

    def test_absent_snapshot_cannot_be_switched_to_known_by_a_boolean_or_origin_change(self):
        plan = plan_import(self.package)
        for field, value in (("current_was_provided", "yes"), ("current_source", "provided_snapshot"),
                             ("recipient_memory_required", False)):
            changed = copy.deepcopy(plan)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                apply_import(self.package, changed, self.root / "rejected", accepted=True)
            self.assertFalse((self.root / "rejected").exists())

    def test_invalid_setup_scope_and_failed_setup_leave_no_partial_workspace(self):
        bad = copy.deepcopy(self.config)
        bad["setup_scope"].append("execute_everything")
        with self.assertRaises(ValidationError):
            self.prepare("bad-scope", current=empty_memory(), setup_delivery=bad)
        self.assertFalse((self.root / "bad-scope").exists())
        with patch("portable_memory.setup.create_setup", side_effect=ValidationError("synthetic failure")):
            with self.assertRaises(ValidationError):
                self.prepare("failed-setup", current=empty_memory(), setup_delivery=self.config)
        self.assertFalse((self.root / "failed-setup").exists())

    def test_actual_receipt_is_rechecked_before_native_verification(self):
        folder, report = self.prepare("receipt", current=empty_memory(), setup_delivery=self.config)
        state = load_json(folder / "setup.json")
        altered = copy.deepcopy(report)
        altered["limitations"].append("Changed after setup creation")
        (folder / "receipt.json").write_bytes(json_bytes(altered))
        with self.assertRaisesRegex(ValidationError, "Workspace or recipient-memory check changed"):
            self.native_event(state)

    def test_unknown_gate_cannot_be_removed_from_workspace_metadata(self):
        folder, _ = self.prepare("stripped", setup_delivery=self.config)
        state = load_json(folder / "setup.json")
        memory = load_json(folder / "memory.json")
        memory["metadata"].pop("recipient_memory_required")
        (folder / "memory.json").write_bytes(json_bytes(memory))
        with self.assertRaisesRegex(ValidationError, "removed its current-memory requirement"):
            create_setup(memory, state["delivery"])

    def test_no_setup_scope_is_not_misreported_as_native_completion(self):
        config = copy.deepcopy(self.config)
        config["setup_scope"] = []
        folder, report = self.prepare("files-only", setup_delivery=config)
        self.assertFalse(report["setup_required"])
        self.assertEqual(report["setup_status"], "not_requested")
        self.assertEqual(report["next_actions"], [])
        self.assertEqual(setup_view(load_json(folder / "setup.json"))["status"], "portable_workspace_only")

    def test_new_common_review_does_not_export_receiver_local_gate_but_next_receiver_still_needs_snapshot(self):
        folder, _ = self.prepare("old-receiver", setup_delivery=self.config)
        before = load_json(folder / "setup.json")
        with self.assertRaises(ValidationError):
            self.native_event(before)
        reviewed = self.root / "reviewed-again.zip"
        # Explicit synthetic common-review fixture; no actual LLM/API call.
        export_synthetic_package(load_json(folder / "memory.json"), reviewed)
        from portable_memory.package import read_package
        self.assertNotIn("recipient_memory_required", read_package(reviewed)["memory"]["metadata"])
        plan = plan_import(reviewed)
        self.assertTrue(plan["recipient_memory_required"])
        next_folder = self.root / "new-receiver"
        apply_import(reviewed, plan, next_folder, accepted=True, setup_delivery=self.config)
        with self.assertRaises(ValidationError):
            self.native_event(load_json(next_folder / "setup.json"))

    def test_deferred_integration_outside_selected_setup_scope_does_not_block_profile_projects_skills(self):
        memory = fixture()
        memory["records"][-1]["review"] = {"decision": "pending"}
        package = self.root / "deferred-integration.zip"
        export_synthetic_package(memory, package)
        selected = copy.deepcopy(self.config)
        selected["setup_scope"] = ["profile", "projects", "skills"]
        folder = self.root / "selected-scope"
        report = apply_import(package, plan_import(package, empty_memory()), folder,
                              accepted=True, setup_delivery=selected)
        self.assertEqual(report["common_review"], "checked_subset_pending_not_applied")
        self.assertEqual(report["pending_setup_record_ids"], [])
        self.assertEqual(report["deferred_outside_setup_scope_ids"], ["drive"])
        self.assertIn({"id": "drive", "action": "defer", "result": "defer"}, report["records"])
        self.assertNotIn("resolve_pending_records", {a["id"] for a in report["next_actions"]})
        self.assertIn("drive", (folder / "IMPORT_REPORT.md").read_text(encoding="utf-8"))
        state = load_json(folder / "setup.json")
        self.assertEqual(state["delivery"]["pending_record_ids"], [])
        self.assertEqual(setup_view(state)["status"], "needs_setup")
        for step_id in [item["id"] for item in state["items"]]:
            state = self.native_event(state, step_id)
        self.assertEqual(setup_view(state)["status"], "completed_within_requested_scope")
        self.assertNotIn("drive", {r["id"] for r in state["memory"]["records"]})
        self.assertIn("drive", {r["id"] for r in load_json(folder / "incoming/memory.json")["records"]})


if __name__ == "__main__":
    unittest.main()
