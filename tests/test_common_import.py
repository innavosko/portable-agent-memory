"""Actual package/import/setup integration with authored fictional review inputs."""
import copy
import tempfile
import unittest
from pathlib import Path

from portable_memory.errors import ValidationError
from portable_memory.importing import apply_import, plan_import
from portable_memory.model import confirm, empty_memory, review_template
from portable_memory.package import load_json, read_package
from portable_memory.setup import apply_setup_event, create_setup, setup_view
from scripts.synthetic_fixture_audit import export_synthetic_package


def fixture_record(rid, value, *, scope="global"):
    return {"id": rid, "kind": "preference", "title": "Fictional " + rid, "scope": scope,
            "value": value, "currency": "unknown", "provenance": "user_statement",
            "sources": [{"id": "source-" + rid, "locator": "synthetic:original-" + rid}],
            "links": [], "review": {"decision": "pending"}}


def authored_memory(*records, approved=True):
    memory = empty_memory()
    memory["records"] = list(records)
    if approved:
        decisions = review_template(memory)
        for decision in decisions["decisions"]:
            decision["decision"] = "active"
        memory = confirm(memory, decisions)
    return memory


def equivalence_groups(extra_ids=()):
    return [{"id": "fictional-equivalent-style", "relation": "equivalent", "record_ids": ["style", "paraphrase"],
             "canonical_id": "style", "note": "Authored fictional test treats these exact two expressions as equivalent."}] + [
        {"id": "fictional-independent-" + rid, "relation": "compatible", "record_ids": [rid], "canonical_id": None,
         "note": "Authored independent fixture field; no live semantic review."} for rid in extra_ids]


class CommonImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.incoming = authored_memory(fixture_record("style", "Кратко, с необходимыми деталями"),
                                       fixture_record("paraphrase", "Без длинных вступлений; детали сохранить"))
        self.assets = self.root / "selected-assets"
        (self.assets / "sources").mkdir(parents=True)
        self.raw = "Private authored fixture original: обсуждение и детали, не пересказ.\n"
        (self.assets / "sources/original.txt").write_text(self.raw, encoding="utf-8")
        self.package = self.root / "incoming.zip"
        export_synthetic_package(self.incoming, self.package, assets=self.assets, semantic_groups=equivalence_groups())

    def import_to(self, name, package=None, current=None):
        package = self.package if package is None else package
        folder = self.root / name
        plan = plan_import(package, current)
        receipt = apply_import(package, plan, folder, accepted=True)
        return folder, load_json(folder / "memory.json"), receipt

    def setup(self, folder, memory, receipt, version=1):
        return create_setup(memory, {"transfer_id": "synthetic-transfer", "version": version,
            "package_sha256": receipt["package_sha256"], "recipient": {"agent_id": "fictional-receiver", "workspace_id": "fictional-personal"},
            "workspace": str(folder), "setup_scope": ["profile"],
            "pending_record_ids": [item["id"] for item in receipt["records"] if item["action"] == "defer"]})

    def capability(self, state):
        return apply_setup_event(state, {"id": "fictional-capability", "actor": "agent", "type": "capability",
            "data": {"name": "native_memory", "available": True,
                     "evidence": "Fictional host capability attestation in an offline test; no native operation performed."}},
            expected_revision=state["revision"])

    def verified_event(self, state):
        return apply_setup_event(state, {"id": "fictional-native-result", "actor": "agent", "type": "step_result",
            "data": {"step_id": "profile", "status": "verified", "evidence": "Fictional offline host write attestation.",
                     "read_back": "Fictional offline read-back attestation for the exact approved profile."}},
            expected_revision=state["revision"])

    def test_initial_validated_package_to_empty_recipient_preserves_aliases_and_canonical_display(self):
        folder, memory, receipt = self.import_to("initial", current=empty_memory())
        self.assertEqual(receipt["common_review"], "checked_for_combined_profile")
        self.assertFalse(memory["metadata"].get("common_review_required", False))
        self.assertEqual(memory["metadata"]["common_profile"]["aliases"], {"paraphrase": "style"})
        self.assertEqual({r["id"] for r in memory["records"]}, {"style", "paraphrase"})
        active = (folder / "ACTIVE_PROFILE.md").read_text(encoding="utf-8")
        self.assertEqual(active.count("## Fictional style (style)"), 1)
        self.assertNotIn("## Fictional paraphrase", active)
        self.assertIn("synthetic:original-style", active)
        self.assertIn("synthetic:original-paraphrase", active)
        self.assertEqual((folder / "incoming/assets/sources/original.txt").read_text(encoding="utf-8"), self.raw)
        setup = self.setup(folder, memory, receipt)
        self.assertFalse(setup_view(setup)["common_review_required"])
        self.assertEqual(setup_view(setup)["status"], "needs_setup")

    def test_existing_current_requires_union_review_then_fresh_package_restores_setup_without_reinterview(self):
        current = authored_memory(fixture_record("calendar", "Календарь ведём по Europe/Moscow", scope="calendar:personal"))
        actual_decisions = {r["id"]: copy.deepcopy(r["review"]) for r in self.incoming["records"] + current["records"]}
        folder, merged, receipt = self.import_to("union-needs-review", current=current)
        self.assertEqual(receipt["common_review"], "required_for_combined_profile")
        self.assertTrue(merged["metadata"]["common_review_required"])
        self.assertNotIn("common_profile", merged["metadata"])
        self.assertEqual({r["id"] for r in merged["records"]}, set(actual_decisions))
        self.assertEqual({r["id"]: r["review"] for r in merged["records"]}, actual_decisions)
        active = (folder / "ACTIVE_PROFILE.md").read_text(encoding="utf-8")
        for record in merged["records"]:
            self.assertNotIn(record["value"], active)
        self.assertEqual((folder / "incoming/assets/sources/original.txt").read_text(encoding="utf-8"), self.raw)
        setup = self.capability(self.setup(folder, merged, receipt))
        self.assertEqual(setup_view(setup)["status"], "needs_review")
        with self.assertRaises(ValidationError):
            self.verified_event(setup)

        # Run the ordinary aggregate gate using explicit fictional source/QA
        # attestations. No user decisions are synthesized or reconfirmed here.
        fresh = self.root / "reviewed-union.zip"
        export_synthetic_package(merged, fresh, assets=self.assets, semantic_groups=equivalence_groups(["calendar"]))
        self.assertFalse(read_package(fresh)["memory"]["metadata"].get("common_review_required", False))
        ready_folder, ready, ready_receipt = self.import_to("union-reviewed", package=fresh, current=merged)
        self.assertEqual(ready_receipt["common_review"], "checked_for_combined_profile")
        self.assertFalse(ready["metadata"].get("common_review_required", False))
        self.assertEqual({r["id"]: r["review"] for r in ready["records"]}, actual_decisions)
        self.assertEqual(ready["metadata"]["common_profile"]["aliases"], {"paraphrase": "style"})
        active = (ready_folder / "ACTIVE_PROFILE.md").read_text(encoding="utf-8")
        self.assertIn("## Fictional style (style)", active)
        self.assertIn("## Fictional calendar (calendar)", active)
        self.assertNotIn("## Fictional paraphrase", active)
        restored = self.capability(self.setup(ready_folder, ready, ready_receipt, version=2))
        self.assertEqual(setup_view(restored)["status"], "needs_setup")
        self.assertEqual(setup_view(self.verified_event(restored))["status"], "completed_within_requested_scope")

    def test_deferred_source_only_record_and_private_original_are_preserved_without_activation(self):
        source_only = authored_memory(fixture_record("source-draft", "Private source-only fixture idea; not user-approved"), approved=False)
        package = self.root / "source-only.zip"
        export_synthetic_package(source_only, package, assets=self.assets)
        folder, merged, receipt = self.import_to("source-only-received", package=package)
        self.assertEqual(merged["records"], [])
        self.assertEqual(receipt["common_review"], "checked_subset_pending_not_applied")
        self.assertFalse(merged["metadata"].get("common_review_required", False))
        self.assertEqual(receipt["records"], [{"id": "source-draft", "action": "defer", "result": "defer"}])
        original = load_json(folder / "incoming/memory.json")
        self.assertEqual(original["records"][0], source_only["records"][0])
        self.assertEqual(original["records"][0]["review"]["decision"], "pending")
        self.assertEqual((folder / "incoming/assets/sources/original.txt").read_text(encoding="utf-8"), self.raw)
        self.assertNotIn("Private source-only fixture idea", (folder / "ACTIVE_PROFILE.md").read_text(encoding="utf-8"))
        self.assertEqual(setup_view(self.setup(folder, merged, receipt))["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
