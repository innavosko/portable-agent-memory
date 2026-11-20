"""Project instructions must survive the actual copied skill and every merge."""
import copy
import tempfile
import unittest
from pathlib import Path

from portable_memory.cli import parser, run
from portable_memory.errors import ValidationError
from portable_memory.importing import _check_workspace_paths, apply_import, plan_import
from portable_memory.model import confirm, empty_memory, record_digest
from portable_memory.package import json_bytes, load_json, read_package, write_new
from scripts.synthetic_fixture_audit import export_synthetic_package
from portable_memory.skill_context import bind_skill_context, validate_skill_context


def reviewed(memory, decisions=None):
    return confirm(memory, {"decisions": [
        {"id": r["id"], "content_sha256": record_digest(r), "decision": (decisions or {}).get(r["id"], "active")}
        for r in memory["records"]]})


class ProjectSkillContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.assets = self.root / "provided-assets"
        self.original = b"---\nname: weekly-report\ndescription: Make a weekly report.\n---\nUse provided notes.\n"
        write_new(self.assets / "skills/weekly/SKILL.md", self.original)
        write_new(self.assets / "skills/weekly/scripts/run.py", "raise RuntimeError('DO NOT EXECUTE')\n")
        self.memory = empty_memory()
        for rid, kind, title, value, scope in [
            ("project-alpha", "project", "Проект Альфа", "Работа над вымышленным проектом", "project:alpha"),
            ("rule-alpha", "rule", "Правило отчёта", "Только для Альфы:\nСначала решения.\nНе удалять открытые вопросы.\n", "project:alpha"),
            ("skill-weekly", "skill", "Недельный отчёт", {"assets_path": "assets/skills/weekly", "status": "partial", "enabled": False,
             "snapshot_sha256": "old", "excluded": [".env"]}, "skill:weekly"),
        ]:
            self.memory["records"].append({"id": rid, "kind": kind, "title": title, "value": value, "scope": scope,
                "sources": [{"id": "synthetic-source", "locator": "fixture:" + rid, "observed_at": "2026-11-20"}],
                "provenance": "source", "currency": "unknown", "review": {"decision": "pending"}, "links": []})
        self.memory["records"][1]["source_project_id"] = "alpha"
        self.memory["records"][0]["source_item_id"] = "alpha"
        self.memory["records"][1]["links"] = [{"target": "project-alpha", "relation": "belongs_to", "basis": "source"}]
        self.bindings = {"format": "portable-skill-context-bindings", "schema_version": 1,
            "items": [{"skill_id": "skill-weekly", "project_id": "project-alpha", "instruction_ids": ["rule-alpha"]}]}

    def bind(self, memory=None, bindings=None, name="bound"):
        out = self.root / name
        report = bind_skill_context(memory or self.memory, self.assets, bindings or self.bindings, out, accepted=True)
        return load_json(out / "memory.json"), out, report

    def package(self, memory, out, name="memory.zip"):
        package = self.root / name
        export_synthetic_package(memory, package, assets=out / "assets")
        return package

    def test_copied_skill_references_verbatim_instruction_and_retains_original(self):
        before = copy.deepcopy(self.memory)
        memory, out, report = self.bind()
        self.assertEqual(self.memory, before)
        base = out / "assets/skills/weekly"
        self.assertEqual((self.assets / "skills/weekly/SKILL.md").read_bytes(), self.original)
        self.assertEqual((base / "references/portable-context/SKILL.original.md").read_bytes(), self.original)
        self.assertIn(b"references/portable-context/rule-alpha.md", (base / "SKILL.md").read_bytes())
        reference = (base / "references/portable-context/rule-alpha.md").read_text(encoding="utf-8")
        self.assertIn(before["records"][1]["value"], reference)
        self.assertIn("project:alpha", reference)
        self.assertIn("fixture:rule-alpha", reference)
        self.assertEqual(memory["records"][2]["value"]["status"], "partial")
        self.assertFalse(memory["records"][2]["value"]["enabled"])
        self.assertTrue(report["originals_unchanged"])
        self.assertEqual(report["native_installation"], "not_performed")

    def test_binding_requires_selection_and_new_output(self):
        out = self.root / "out"
        with self.assertRaises(ValidationError):
            bind_skill_context(self.memory, self.assets, self.bindings, out)
        self.assertFalse(out.exists())
        self.bind(name="out")
        with self.assertRaises(ValidationError):
            self.bind(name="out")

    def test_different_project_scope_is_rejected_before_writing(self):
        self.memory["records"][0]["scope"] = "project:beta"
        with self.assertRaises(ValidationError):
            self.bind()
        self.assertFalse((self.root / "bound").exists())

    def test_same_scope_does_not_override_actual_project_identity(self):
        self.memory["records"][0]["source_item_id"] = "different-project"
        with self.assertRaises(ValidationError):
            self.bind()

    def test_actual_source_id_takes_precedence_over_coinciding_record_id(self):
        self.memory["records"][0]["source_item_id"] = "another-source"
        self.memory["records"][0]["id"] = "alpha"
        self.memory["records"][1]["links"] = []
        self.bindings["items"][0]["project_id"] = "alpha"
        with self.assertRaises(ValidationError):
            self.bind()

    def test_actual_project_relationship_cannot_point_elsewhere(self):
        self.memory["records"][1]["links"][0]["target"] = "another-project"
        with self.assertRaises(ValidationError):
            self.bind()

    def test_absent_original_is_not_invented(self):
        (self.assets / "skills/weekly/SKILL.md").unlink()
        with self.assertRaises(ValidationError):
            self.bind()
        self.assertFalse((self.root / "bound").exists())

    def test_binding_resets_only_changed_skill_review(self):
        memory, _, _ = self.bind(reviewed(self.memory))
        self.assertEqual([r["review"]["decision"] for r in memory["records"]], ["active", "active", "pending"])

    def test_review_then_export_import_preserves_files_and_second_import_is_duplicate(self):
        memory, out, _ = self.bind()
        memory = reviewed(memory)
        package = self.package(memory, out)
        data = read_package(package)
        plan = plan_import(package)
        received = self.root / "received"
        receipt = apply_import(package, plan, received, accepted=True)
        self.assertEqual(receipt["skills"], "not_installed")
        self.assertEqual((received / "incoming/assets/skills/weekly/SKILL.md").read_bytes(), data["files"]["assets/skills/weekly/SKILL.md"])
        again = plan_import(package, load_json(received / "memory.json"))
        self.assertEqual({i["status"] for i in again["items"]}, {"duplicate"})

    def test_required_excluded_instruction_blocks_package(self):
        memory, out, _ = self.bind()
        memory = reviewed(memory, {"rule-alpha": "exclude"})
        with self.assertRaises(ValidationError):
            self.package(memory, out)
        self.assertFalse((self.root / "memory.zip").exists())

    def test_changed_instruction_needs_rebuild_even_after_review(self):
        memory, out, _ = self.bind()
        memory["records"][1]["value"] = "Изменённое правило"
        memory = reviewed(memory)
        with self.assertRaises(ValidationError):
            self.package(memory, out)

    def test_missing_and_tampered_reference_are_not_silently_packaged(self):
        memory, out, _ = self.bind()
        ref = out / "assets/skills/weekly/references/portable-context/rule-alpha.md"
        original = ref.read_bytes()
        for raw in (None, b"Incorrect shortened summary"):
            with self.subTest(raw=raw):
                if raw is None:
                    ref.unlink()
                else:
                    ref.write_bytes(raw)
                with self.assertRaises(ValidationError):
                    self.package(memory, out)
        ref.write_bytes(original)
        self.package(memory, out)

    def test_modified_skill_entry_cannot_drop_context_links(self):
        memory, out, _ = self.bind()
        (out / "assets/skills/weekly/SKILL.md").write_bytes(self.original)
        with self.assertRaises(ValidationError):
            self.package(memory, out)

    def test_changed_script_requires_new_snapshot_and_review(self):
        memory, out, _ = self.bind()
        memory = reviewed(memory)
        (out / "assets/skills/weekly/scripts/run.py").write_text("print('Changed code')", encoding="utf-8")
        with self.assertRaises(ValidationError):
            self.package(memory, out)

    def test_retained_bound_skill_without_old_files_blocks_incomplete_workspace(self):
        bound, _, _ = self.bind()
        current = reviewed(bound)
        package = self.root / "unrelated.zip"
        export_synthetic_package(empty_memory(), package)
        plan = plan_import(package, current)
        with self.assertRaises(ValidationError):
            apply_import(package, plan, self.root / "received", accepted=True)
        self.assertFalse((self.root / "received").exists())

    def test_existing_bound_skill_rejects_replacement_with_different_rule(self):
        bound, _, _ = self.bind()
        current = reviewed(bound)
        incoming = copy.deepcopy(self.memory)
        incoming["records"] = [incoming["records"][1]]
        incoming["records"][0]["value"] = "New incompatible rule"
        incoming = reviewed(incoming)
        package = self.root / "update.zip"
        export_synthetic_package(incoming, package)
        plan = plan_import(package, current)
        plan["items"][0]["action"] = "replace"
        with self.assertRaises(ValidationError):
            apply_import(package, plan, self.root / "received", accepted=True)
        self.assertFalse((self.root / "received").exists())

    def test_merging_new_skill_with_mismatched_existing_rule_is_blocked(self):
        bound, out, _ = self.bind()
        incoming = reviewed(bound)
        current = copy.deepcopy(self.memory)
        current["records"] = current["records"][:2]
        current["records"][1]["value"] = "An older incompatible instruction"
        current = reviewed(current)
        package = self.package(incoming, out)
        plan = plan_import(package, current)
        self.assertEqual(next(i for i in plan["items"] if i["id"] == "skill-weekly")["status"], "blocked_dependency")
        # A reviewed conflict can be retained, but the dependent new skill stays deferred.
        next(i for i in plan["items"] if i["id"] == "rule-alpha")["action"] = "keep_current"
        receipt = apply_import(package, plan, self.root / "received", accepted=True)
        self.assertEqual(next(i for i in receipt["records"] if i["id"] == "skill-weekly")["action"], "defer")

    def test_cli_binds_actual_files(self):
        write_new(self.root / "memory.json", json_bytes(self.memory))
        write_new(self.root / "bindings.json", json_bytes(self.bindings))
        output = self.root / "cli-bound"
        run(parser().parse_args(["skill-bind-context", str(self.root / "memory.json"), "--assets", str(self.assets),
            "--bindings", str(self.root / "bindings.json"), "--out", str(output), "--accept"]))
        self.assertTrue((output / "memory.json").is_file())

    def test_cli_context_template_and_collect_leave_unknowns_pending(self):
        source = {"account_id": "demo", "workspace_id": "personal", "observed_at": "2026-11-20", "evidence": "Synthetic test"}
        write_new(self.root / "source.json", json_bytes(source))
        template, out = self.root / "context.json", self.root / "context-out"
        run(parser().parse_args(["context-template", "--platform", "dots", "--source", str(self.root / "source.json"), "--out", str(template)]))
        report = run(parser().parse_args(["context-collect", str(template), "--out", str(out), "--accept"]))
        self.assertEqual(report["summary"]["pending"], 15)
        self.assertFalse(report["summary"]["complete_account_export"])

    def test_windows_long_destination_fails_before_any_writes_with_actionable_error(self):
        output = self.root / ("long-receiving-folder-" * 9) / "result"
        with self.assertRaisesRegex(ValidationError, "shorter output folder"):
            _check_workspace_paths(output, ["assets/skills/weekly/references/portable-context/rule.md"], windows=True)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
