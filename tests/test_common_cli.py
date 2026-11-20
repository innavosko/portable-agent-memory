"""Actual CLI/package flows with fictional source excerpts and attestations."""
from pathlib import Path
import tempfile
import unittest

from portable_memory.migration import MigrationStore, create_migration
from portable_memory.common_profile import CommonStore
from portable_memory.package import load_json
from scripts.demo_common_profile import (CLIJourney, COORDINATOR, confirmed, demo,
                                        memory_of, record)


class CommonCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def observation(self, oid, resource, input_file):
        return {"id": oid, "label": resource, "kind": "profile",
            "identity": {"namespace": "fictional-provider", "account_id": "fictional-work",
                         "workspace_id": "fictional-space", "resource_id": resource},
            "identity_evidence": "Explicit synthetic source identity, not a real account",
            "surface": {"agent_id": "Codex", "product": "Codex", "interface": "desktop"},
            "selected": True, "input": {"kind": "memory", "path": str(input_file)}}

    def linked(self):
        flow = CLIJourney(self.root)
        memory = confirmed(memory_of(record("known-work-rule", "Рабочее письмо сначала покажи черновиком.")))
        previous = flow.save("previous", memory)
        config = {"coordinator": COORDINATOR, "actors": [COORDINATOR],
            "known_answers": {"scope": ["profile"], "destination": {"kind": "upload", "label": "Fictional receiving chat"},
                              "delivery": "as_ready"},
            "sources": [self.observation("work-observation", "work-context", previous)]}
        migration = MigrationStore.create(self.root / "migration.sqlite", create_migration(config))
        flow.cli("common-create", "--store", flow.store, "--coordinator", COORDINATOR,
                 "--previous", previous, "--allow-partial", "--scope-note", "Fictional user's chosen partial input scope",
                 "--migration-store", migration.path)
        initial = flow.view()
        self.assertEqual("source_map_changed", initial["status"])
        flow.cli("common-sync", "--store", flow.store, "--revision", initial["revision"], "--actor", COORDINATOR)
        sid = migration.load()["sources"][0]["id"]
        flow.source(sid, "Codex work supplied context", "provided", memory)
        reviewed = flow.review()
        self.assertTrue(reviewed["export_allowed"])
        self.assertIsNone(reviewed["question"])
        memory_path, _ = flow.result("initial-reviewed")
        flow.export(memory_path, "initial-package")
        return flow, migration, sid, memory_path

    def migration_event(self, migration, kind, data):
        state = migration.load()
        return migration.event({"id": f"fictional-{kind}-{state['revision']}", "actor": COORDINATOR,
                                "type": kind, "data": data}, expected_revision=state["revision"])

    def test_real_cli_reuses_confirmation_resolves_conflict_and_imports_once(self):
        report = demo(self.root / "scenario")
        self.assertTrue(report["passed"])
        self.assertEqual(4, report["records_imported"])
        self.assertEqual(4, report["repeat_duplicate_records"])
        self.assertEqual("not_applied", report["native_memory"])
        calls = load_json(self.root / "scenario" / "command-report.json")
        self.assertTrue(any(c["command"] == "export" and c["exit_code"] == 2 for c in calls))
        self.assertTrue(any(c["command"] == "common-event" for c in calls))

    def test_new_migration_source_blocks_old_export_before_common_source_is_called(self):
        flow, migration, _, memory_path = self.linked()
        late_file = flow.save("late-empty-source", memory_of())
        self.migration_event(migration, "register_sources", {"observations": [
            self.observation("late-observation", "chatgpt-personal", late_file)]})
        changed = flow.view()
        self.assertEqual("source_map_changed", changed["status"])
        self.assertFalse(changed["export_allowed"])
        _, error = flow.export(memory_path, "old-export-after-new-source", expected=2)
        self.assertIn("source map changed", str(error).lower())
        flow.cli("common-sync", "--store", flow.store, "--revision", changed["revision"], "--actor", COORDINATOR)
        late = next(s for s in flow.view()["sources"] if s["label"] == "chatgpt-personal")
        self.assertEqual("pending", late["status"])
        flow.source(late["id"], "ChatGPT personal supplied empty fixture", "provided", memory_of())
        final_view = flow.review()
        self.assertIsNone(final_view["question"])
        self.assertTrue(final_view["export_allowed"])
        memory_v2, _ = flow.result("new-source-reviewed")
        flow.export(memory_v2, "new-source-package")

    def test_changed_existing_migration_input_requires_source_recheck_after_sync(self):
        flow, migration, sid, memory_path = self.linked()
        new_file = flow.save("revised-work-input", memory_of(
            record("known-work-rule", "Новая вымышленная инструкция требует повторной проверки.")))
        self.migration_event(migration, "provide_input", {"source_id": sid,
            "input": {"kind": "memory", "path": str(new_file)}, "evidence": "Fictional user supplied a changed snapshot"})
        changed = flow.view()
        self.assertEqual("source_map_changed", changed["status"])
        flow.export(memory_path, "old-export-after-replacement", expected=2)
        flow.cli("common-sync", "--store", flow.store, "--revision", changed["revision"], "--actor", COORDINATOR)
        synced = flow.view()
        source = next(s for s in synced["sources"] if s["id"] == sid)
        self.assertEqual("pending", source["status"])
        self.assertFalse(synced["export_allowed"])
        self.assertEqual("needs_common_review", synced["status"])

    def test_wrong_actor_and_stale_revision_do_not_mutate_shared_cli_store(self):
        flow, _, _, _ = self.linked()
        before = flow.view()
        card = flow.save("unauthorized-source", {"id": "another-source", "label": "Fictional late source",
            "status": "pending", "note": "Synthetic test only"})
        flow.cli("common-source", "--store", flow.store, "--source", card, "--revision", before["revision"],
                 "--actor", "other-agent", expected=2)
        flow.cli("common-source", "--store", flow.store, "--source", card, "--revision", before["revision"] - 1,
                 "--actor", COORDINATOR, expected=2)
        after = flow.view()
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(before["sources"], after["sources"])
        self.assertEqual(before["export_allowed"], after["export_allowed"])

    def test_deselected_mapped_source_excludes_only_its_exclusive_records(self):
        flow, migration, sid, memory_path = self.linked()
        original = load_json(memory_path)
        extra = record("only-in-work-source", "Вымышленное уникальное правило этого источника.")
        flow.source(sid, "Codex work supplied context", "provided", memory_of(*original["records"], extra))
        flow.review()
        self.migration_event(migration, "select_source", {"source_id": sid, "selected": False})
        view = flow.view()
        flow.cli("common-sync", "--store", flow.store, "--revision", view["revision"], "--actor", COORDINATOR)
        state = CommonStore(flow.store).load()
        self.assertEqual(next(s for s in state["sources"] if s["id"] == sid)["status"], "excluded")
        records = {r["id"]: r for r in state["memory"]["records"]}
        self.assertEqual(records["only-in-work-source"]["review"]["decision"], "exclude")
        self.assertEqual(records["known-work-rule"]["review"]["decision"], "active")


if __name__ == "__main__":
    unittest.main()
