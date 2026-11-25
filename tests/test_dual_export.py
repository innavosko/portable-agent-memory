"""Full selected context survives the additional bounded GigaAgent projection."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from portable_memory.common_profile import common_result, create_common
from portable_memory.dual_export import export_giga_bundle
from portable_memory.errors import ValidationError
from portable_memory.model import digest
from portable_memory.package import load_json, read_package
from portable_memory.recipients import build_giga_preview, giga_projection_template, parse_giga_export
from test_common_profile import COORDINATOR, confirmed, memory_of, record, reviewed
from test_recipients import attest, fact


ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION = ROOT / "skills/memory-export/assets/import-instruction.md"


class DualExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root / "client-memory-export.md"
        self.report = self.root / "giga-report.json"
        self.full = self.root / "client-memory-export-full.zip"
        self.handoff = self.root / "client-memory-export-import"
        raw = confirmed(memory_of(record("work-rule", "Keep the exact project instruction in its project."),
                                  record("long-context", "Selected original context. " * 40)))
        state = reviewed(create_common(COORDINATOR, previous=raw),
                         predicates={"work-rule": "work-rule", "long-context": "project-context"})
        checked = common_result(state)
        self.memory, self.review = checked["memory"], checked["review"]
        self.projection = giga_projection_template(self.memory, self.review)
        self.projection["metadata"]["subject"] = "Fictional dual-export user"
        for entry in self.projection["entries"]:
            rid = entry["source_record_ids"][0]
            original = next(r for r in self.memory["records"] if r["id"] == rid)
            entry.update(disposition="ready", reason="Explicit fictional supported source fact.",
                         record=fact(rid, content=original["value"]))
        self.projection = attest(self.projection)
        self.preview = build_giga_preview(self.memory, self.review, self.projection)

    def export(self, *, asset_scope=True, **kwargs):
        options = {"output": self.profile, "report": self.report, "instruction": INSTRUCTION}
        if asset_scope and "assets" not in kwargs:
            options.update(no_assets=True, asset_scope_note="Fictional chosen scope contains reviewed records only.")
        options.update(kwargs)
        return export_giga_bundle(self.memory, self.review, self.projection, self.preview, **options)

    def assert_no_outputs(self):
        for path in (self.profile, self.report, self.full, self.handoff):
            self.assertFalse(path.exists(), str(path))

    def test_full_record_values_survive_native_limits_and_only_two_md_are_uploads(self):
        result = self.export()
        full = read_package(result["full_package"]["path"])
        self.assertEqual(digest(self.memory), digest(full["memory"]))
        self.assertEqual(2, full["manifest"]["record_count"])
        native = parse_giga_export(self.profile.read_text(encoding="utf-8"))
        self.assertEqual(["work-rule"], [r["name"] for r in native["records"]])
        self.assertEqual("reserve", next(i["disposition"] for i in load_json(self.report)["items"]
                                         if i["name"] == "long-context"))
        self.assertEqual(self.full, Path(result["full_package"]["path"]))
        self.assertEqual(hashlib.sha256(self.full.read_bytes()).hexdigest(), result["full_package"]["sha256"])
        self.assertEqual([str(self.full), str(self.handoff / self.profile.name),
                          str(self.handoff / "import-instruction.md")], result["user_delivery_files"])
        self.assertEqual(["import-instruction.md", self.profile.name], result["handoff"]["upload_files"])
        self.assertEqual({".md"}, {p.suffix for p in self.handoff.iterdir()})
        self.assertFalse(result["native_memory_applied"])
        self.assertFalse(result["full_package"]["upload_to_gigaagent"])

    def test_selected_project_and_skill_files_are_copied_exactly_only_into_full_package(self):
        assets = self.root / "selected"
        project = assets / "project" / "rules.md"
        skill = assets / "skills" / "fictional" / "SKILL.md"
        project.parent.mkdir(parents=True)
        skill.parent.mkdir(parents=True)
        project.write_bytes(b"# Project Atlas\nKeep the owner and due date.\n")
        skill.write_bytes(b"---\nname: fictional\ndescription: Fictional artifact only.\n---\nNever execute in this test.\n")
        original = {str(p.relative_to(assets)): p.read_bytes() for p in (project, skill)}
        result = self.export(assets=assets, asset_scope_note="Two selected fictional original files.")
        package = read_package(self.full)
        for name, data in original.items():
            self.assertEqual(data, package["files"]["assets/" + name.replace("\\", "/")])
        self.assertEqual("provided_directory", result["full_package"]["assets_selection"])
        self.assertEqual(2, result["full_package"]["asset_count"])
        self.assertEqual({".md"}, {p.suffix for p in self.handoff.iterdir()})
        self.assertEqual({str(p.relative_to(assets)): p.read_bytes() for p in (project, skill)}, original)

    def test_history_and_pending_context_are_preserved_but_user_exclusions_are_not_exported(self):
        memory = copy.deepcopy(self.memory)
        excluded_text = "Fictional privacy exclusion: do not carry this statement."
        memory["records"].extend([
            confirmed(memory_of(record("past-context", "Former fictional project decision.")), "historical")["records"][0],
            record("pending-context", "Unconfirmed fictional project note."),
            confirmed(memory_of(record("privacy-omitted", excluded_text)), "exclude")["records"][0],
        ])
        state = reviewed(create_common(COORDINATOR, previous=memory, allow_partial=True,
                                       scope_note="Explicit fictional choice to preserve pending records without activation."),
                         predicates={r["id"]: r["id"] for r in memory["records"]})
        checked = common_result(state)
        self.memory, self.review = checked["memory"], checked["review"]
        projection = giga_projection_template(self.memory, self.review)
        projection["metadata"]["subject"] = "Fictional partial-context export"
        for entry in projection["entries"]:
            rid = entry["source_record_ids"][0]
            source = next(r for r in self.memory["records"] if r["id"] == rid)
            disposition = {"historical": "temporary", "pending": "clarify"}.get(source["review"]["decision"], "ready")
            entry.update(disposition=disposition, reason="Explicit fictional scoped review result.",
                         record=fact(rid, content=source["value"]))
        self.projection = attest(projection)
        self.preview = build_giga_preview(self.memory, self.review, self.projection)
        self.export()
        package = read_package(self.full)
        decisions = {r["id"]: r["review"]["decision"] for r in package["memory"]["records"]}
        self.assertEqual("historical", decisions["past-context"])
        self.assertEqual("pending", decisions["pending-context"])
        self.assertNotIn("privacy-omitted", decisions)
        self.assertFalse(any(excluded_text.encode() in data for data in package["files"].values()))
        self.assertNotIn("past-context", package["files"]["ACTIVE_PROFILE.md"].decode())
        self.assertNotIn("pending-context", package["files"]["ACTIVE_PROFILE.md"].decode())

    def test_explicit_asset_scope_is_required_and_conflicting_choices_are_rejected(self):
        for options in ({}, {"no_assets": True}, {"no_assets": True, "asset_scope_note": " "},
                        {"assets": self.root, "no_assets": True, "asset_scope_note": "Contradictory."}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                self.export(asset_scope=False, **options)
            self.assert_no_outputs()

    def test_no_assets_scope_note_exposes_unavailable_originals_without_claiming_they_were_saved(self):
        note = "Selected records only; original skill files were unavailable in this fictional source."
        result = self.export(no_assets=True, asset_scope_note=note)
        self.assertEqual(note, result["full_package"]["asset_scope_note"])
        self.assertEqual("no_assets_declared", result["full_package"]["assets_selection"])
        self.assertEqual([], result["full_package"]["asset_files"])
        self.assertEqual(0, result["full_package"]["asset_count"])

    def test_invalid_asset_collection_does_not_publish_any_part_of_dual_output(self):
        with self.assertRaises(ValidationError):
            self.export(assets=self.root / "missing")
        self.assert_no_outputs()
        assets = self.root / "selected"
        assets.mkdir()
        (assets / "oversize.bin").write_bytes(b"X" * 32)
        with patch("portable_memory.package.MAX_FILE", 16), self.assertRaises(ValidationError):
            self.export(assets=assets)
        self.assert_no_outputs()

    def test_output_collisions_and_archive_inside_giga_folder_fail_before_output(self):
        collision = self.root / "existing.zip"
        collision.write_bytes(b"another owner's bytes")
        for options in ({"full_output": collision}, {"full_output": self.handoff / "full.zip"},
                        {"full_output": self.report}, {"report": self.full},
                        {"full_output": self.profile.with_suffix(".txt")}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                self.export(**options)
            self.assert_no_outputs()
        self.assertEqual(b"another owner's bytes", collision.read_bytes())

    def test_stale_preview_does_not_create_either_deliverable(self):
        self.preview["ready"] = []
        with self.assertRaises(ValidationError):
            self.export()
        self.assert_no_outputs()

    def test_failure_after_staging_rolls_back_own_files_without_removing_competing_output(self):
        original_open = Path.open
        def race(path, mode="r", *args, **kwargs):
            if path == self.report and mode == "xb":
                with original_open(path, "wb") as handle:
                    handle.write(b"competing writer")
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, "open", race), self.assertRaises(FileExistsError):
            self.export()
        self.assertFalse(self.full.exists())
        self.assertFalse(self.profile.exists())
        self.assertFalse(self.handoff.exists())
        self.assertEqual(b"competing writer", self.report.read_bytes())


if __name__ == "__main__":
    unittest.main()
