"""Real separate-process recipient CLI flows with fictional model/tool inputs.

No native memory tool or SaaS account is called. Explicit fixture attestations
exercise the ordinary gates; they are not a claim that Python understands facts.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from portable_memory.common_profile import CommonStore, create_common
from portable_memory.package import json_bytes, load_json
from test_common_profile import COORDINATOR, confirmed, memory_of, record, reviewed
from test_recipients import fact, snapshot


ROOT = Path(__file__).resolve().parents[1]


class RecipientCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.counter = 0
        self.store_path = self.root / "common.sqlite"
        memory = confirmed(memory_of(record("concise-work", "Пользователь предпочитает краткие рабочие ответы."),
                                     record("format-lists", "Пользователь просил оформлять перечни списками.")))
        state = reviewed(create_common(COORDINATOR, previous=memory),
                         predicates={"concise-work": "response-length", "format-lists": "list-format"})
        CommonStore.create(self.store_path, state)

    def path(self, label, suffix=".json"):
        self.counter += 1
        return self.root / f"{self.counter:02d}-{label}{suffix}"

    def save(self, label, value):
        path = self.path(label)
        path.write_bytes(json_bytes(value))
        return path

    def cli(self, *args, expected=0, default_asset_scope=True):
        if (default_asset_scope and args and args[0] == "giga-export" and
                "--assets" not in args and "--no-assets" not in args):
            args = (*args, "--no-assets", "--asset-scope-note", "Fictional fixture selects reviewed records only.")
        result = subprocess.run([sys.executable, "-B", "-m", "portable_memory", *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(expected, result.returncode, result.stderr or result.stdout)
        return json.loads(result.stdout if expected == 0 else result.stderr)

    def prepared_projection(self):
        memory_path, common_report = self.path("common-result"), self.path("common-report")
        self.cli("common-result", "--store", self.store_path, "--out", memory_path, "--report", common_report)
        template_path = self.path("giga-template")
        result = self.cli("giga-template", "--memory", memory_path, "--common-store", self.store_path, "--out", template_path)
        self.assertTrue(result["projection_review_required"])
        projection = load_json(template_path)
        self.assertEqual("pending", projection["quality"]["decision"])
        self.assertTrue(all(entry["disposition"] == "draft" for entry in projection["entries"]))
        projection["metadata"]["subject"] = "Вымышленный пользователь теста CLI"
        for index, entry in enumerate(projection["entries"]):
            entry.update(disposition="ready", reason="Fictional source-supported and explicitly confirmed fact.",
                         record=fact("fact-" + str(index), content=f"Пользователь придерживается рабочего правила {index}.",
                                     observed_at="неизвестно" if index == 0 else "2022-04-01T10:11:12+03:00"))
        projection_path = self.save("filled-projection", projection)
        return memory_path, projection_path

    def reviewed_projection(self):
        memory_path, projection_path = self.prepared_projection()
        return memory_path, self.review_projection(load_json(projection_path))

    def review_projection(self, projection):
        projection_path = self.save("projection-for-fixture-check", projection)
        quality_path = self.path("projection-quality-template")
        response = self.cli("giga-quality-template", "--projection", projection_path, "--out", quality_path)
        self.assertTrue(response["semantic_review_required"])
        quality = load_json(quality_path)
        self.assertEqual("pending", quality["decision"])
        quality.update(decision="supported", reviewer={"kind": "human", "label": "Explicit fictional source/projection checker"},
                       note="Fixture check: exact subject, scope, evidence, currency, exclusions and atomic facts.")
        projection = load_json(projection_path)
        projection["quality"] = quality
        return self.save("reviewed-projection", projection)

    def preview(self, memory_path, projection_path):
        preview_path = self.path("giga-preview")
        result = self.cli("giga-preview", "--memory", memory_path, "--common-store", self.store_path,
                          "--projection", projection_path, "--out", preview_path)
        self.assertFalse(result["native_memory_applied"])
        self.assertEqual(result["preview"], load_json(preview_path))
        return preview_path

    def exported_document(self):
        memory_path, projection_path = self.reviewed_projection()
        preview_path = self.preview(memory_path, projection_path)
        markdown_path, report_path = self.path("client-memory-export", ".md"), self.path("giga-export-report")
        result = self.cli("giga-export", "--memory", memory_path, "--common-store", self.store_path,
                          "--projection", projection_path, "--preview", preview_path,
                          "--out", markdown_path, "--report", report_path)
        self.assertFalse(result["native_memory_applied"])
        self.assertTrue(markdown_path.is_file())
        handoff = result["handoff"]
        delivered = Path(handoff["directory"])
        self.assertEqual(markdown_path.read_bytes(), (delivered / markdown_path.name).read_bytes())
        self.assertTrue((delivered / "import-instruction.md").is_file())
        self.assertEqual("gigaagent", handoff["target"])
        self.assertIn(markdown_path.name, Path(handoff["guide"]).read_text(encoding="utf-8"))
        self.assertEqual({".md"}, {path.suffix for path in delivered.iterdir()})
        self.assertEqual(["import-instruction.md", markdown_path.name], handoff["upload_files"])
        self.assertIsNone(handoff["manifest"])
        self.assertTrue(Path(result["full_package"]["path"]).is_file())
        self.assertEqual(3, len(result["user_delivery_files"]))
        self.assertNotIn(result["full_package"]["path"], result["gigaagent_upload_files"])
        return markdown_path, report_path

    def comparisons(self, document_path, snapshot_path):
        template_path = self.path("comparison-template")
        self.cli("giga-comparison-template", "--document", document_path, "--snapshot", snapshot_path, "--out", template_path)
        comparisons = load_json(template_path)
        self.assertEqual("pending", comparisons["quality"]["decision"])
        self.assertFalse(comparisons["incoming_and_existing_checked"])
        comparisons["incoming_and_existing_checked"] = True
        for item in comparisons["items"]:
            item.update(relation="new", existing_names=[], note="Fictional separate current-memory check found distinct new facts.")
        filled_path = self.save("filled-comparisons", comparisons)
        quality_path = self.path("comparison-quality-template")
        self.cli("giga-comparison-quality-template", "--comparisons", filled_path, "--out", quality_path)
        quality = load_json(quality_path)
        self.assertEqual("pending", quality["decision"])
        quality.update(decision="supported", reviewer={"kind": "human", "label": "Fictional second receiver checker"},
                       note="Fictional check across incoming facts and complete current-memory snapshot.")
        comparisons["quality"] = quality
        return self.save("reviewed-comparisons", comparisons)

    def test_whole_flow_separate_cli_calls_preserve_dates_and_never_write_native_memory(self):
        markdown_path, report_path = self.exported_document()
        current_path = self.save("actual-tool-snapshot-fictional-fixture", snapshot())
        original_snapshot_bytes = current_path.read_bytes()
        comparison_path = self.comparisons(markdown_path, current_path)
        plan_path = self.path("native-plan")
        response = self.cli("giga-plan", "--document", markdown_path, "--snapshot", current_path,
                            "--comparisons", comparison_path, "--out", plan_path)
        self.assertFalse(response["native_memory_applied"])
        plan = load_json(plan_path)
        self.assertEqual("not_written", plan["native_memory"])
        self.assertEqual(["add", "add"], [item["action"] for item in plan["items"]])
        payloads = [item["save_arguments"] for item in plan["items"]]
        self.assertNotIn("observed_at", payloads[0])
        self.assertEqual("2022-04-01T10:11:12+03:00", payloads[1]["observed_at"])
        self.assertTrue(all(len(p["content"]) <= 500 and p["content"].endswith("\n") for p in payloads))
        empty_readback = self.save("no-native-tool-was-called", [])
        readback_report = self.path("unverified-report")
        self.cli("giga-readback", "--plan", plan_path, "--observed", empty_readback, "--out", readback_report)
        self.assertEqual("not_fully_verified", load_json(readback_report)["native_memory"])
        # These are deliberately fabricated TEST fixtures, never production evidence.
        readbacks = self.save("explicitly-fictional-native-readback-fixture", payloads)
        successful_report = self.path("fixture-readback-report")
        self.cli("giga-readback", "--plan", plan_path, "--observed", readbacks, "--out", successful_report)
        verified = load_json(successful_report)
        self.assertEqual("verified_supplied_readbacks", verified["native_memory"])
        self.assertTrue(all(item["verified"] for item in verified["records"]))
        self.assertEqual(original_snapshot_bytes, current_path.read_bytes())
        self.assertEqual("not_written", load_json(report_path)["native_memory"])

    def test_unapproved_projection_cannot_export_and_creates_no_outputs(self):
        memory_path, projection_path = self.prepared_projection()
        target, report = self.path("blocked", ".md"), self.path("blocked-report")
        result = self.cli("giga-export", "--memory", memory_path, "--common-store", self.store_path,
                          "--projection", projection_path, "--out", target, "--report", report, expected=2)
        self.assertIn("check", result["error"].lower())
        self.assertFalse(target.exists())
        self.assertFalse(report.exists())

    def test_giga_export_requires_explicit_original_file_scope_before_either_output(self):
        memory, projection = self.reviewed_projection()
        preview = self.preview(memory, projection)
        target, report = self.path("scope-profile", ".md"), self.path("scope-report")
        args = ("giga-export", "--memory", memory, "--common-store", self.store_path,
                "--projection", projection, "--preview", preview, "--out", target, "--report", report)
        for scope in ((), ("--no-assets",), ("--assets", self.root, "--no-assets", "--asset-scope-note", "Conflicting fixture.")):
            with self.subTest(scope=scope):
                result = self.cli(*args, *scope, expected=2, default_asset_scope=False)
                self.assertIn("asset", result["error"].lower())
                self.assertFalse(target.exists())
                self.assertFalse(report.exists())
                self.assertFalse(target.with_name(target.stem + "-full.zip").exists())
        result = self.cli(*args, "--no-assets", "--asset-scope-note", "Only fictional reviewed records were selected.",
                          default_asset_scope=False)
        self.assertTrue(result["full_package_verified"])
        self.assertEqual(3, len(result["user_delivery_files"]))

    def test_new_source_invalidates_common_store_and_blocks_prepared_target_export(self):
        memory_path, projection_path = self.reviewed_projection()
        preview_path = self.preview(memory_path, projection_path)
        state = CommonStore(self.store_path).load()
        source_path = self.save("new-source-card", {"id": "late-source", "label": "Late fictional source",
                                                    "status": "provided", "note": "Fixture only: a new source arrived after target review."})
        source_memory = self.save("new-source-facts", memory_of(record("different-fact", "Пользователь изменил рабочее правило.")))
        changed = self.cli("common-source", "--store", self.store_path, "--source", source_path, "--memory", source_memory,
                           "--revision", state["revision"], "--actor", COORDINATOR)
        self.assertFalse(changed["export_allowed"])
        target, report = self.path("stale-target", ".md"), self.path("stale-report")
        self.cli("giga-export", "--memory", memory_path, "--common-store", self.store_path,
                 "--projection", projection_path, "--preview", preview_path,
                 "--out", target, "--report", report, expected=2)
        self.assertFalse(target.exists())
        self.assertFalse(report.exists())

    def test_partial_current_list_cannot_prepare_writes_even_with_reviewed_comparison(self):
        markdown_path, _ = self.exported_document()
        current_path = self.save("partial-current-memory", snapshot(complete=False))
        comparison_path = self.comparisons(markdown_path, current_path)
        plan_path = self.path("partial-plan")
        self.cli("giga-plan", "--document", markdown_path, "--snapshot", current_path,
                 "--comparisons", comparison_path, "--out", plan_path)
        plan = load_json(plan_path)
        self.assertEqual(["defer", "defer"], [item["action"] for item in plan["items"]])
        self.assertTrue(all(item["save_arguments"] is None for item in plan["items"]))

    def test_changed_comparison_invalidates_second_pass_and_existing_files_are_not_overwritten(self):
        markdown_path, _ = self.exported_document()
        original_markdown = markdown_path.read_bytes()
        current_path = self.save("current-memory", snapshot())
        comparison_path = self.comparisons(markdown_path, current_path)
        changed = copy.deepcopy(load_json(comparison_path))
        changed["items"][0]["note"] = "Changed after the second pass."
        stale_path = self.save("stale-comparison", changed)
        plan_path = self.path("blocked-plan")
        result = self.cli("giga-plan", "--document", markdown_path, "--snapshot", current_path,
                          "--comparisons", stale_path, "--out", plan_path, expected=2)
        self.assertIn("stale", result["error"].lower())
        self.assertFalse(plan_path.exists())
        self.cli("giga-plan", "--document", markdown_path, "--snapshot", current_path,
                 "--comparisons", comparison_path, "--out", markdown_path, expected=2)
        self.assertEqual(original_markdown, markdown_path.read_bytes())

    def test_giga_completion_collision_is_reported_before_export_writes(self):
        memory, projection = self.reviewed_projection()
        preview = self.preview(memory, projection)
        target, report = self.path("profile", ".md"), self.path("report")
        handoff = self.root / "already-present"
        handoff.mkdir()
        marker = handoff / "untouched.md"
        marker.write_text("Existing user data", encoding="utf-8")
        self.cli("giga-export", "--memory", memory, "--common-store", self.store_path,
                 "--projection", projection, "--preview", preview, "--out", target, "--report", report,
                 "--handoff-out", handoff, expected=2)
        self.assertFalse(target.exists())
        self.assertFalse(report.exists())
        self.assertEqual("Existing user data", marker.read_text(encoding="utf-8"))
        invalid_pairs = [(target, marker / "report.json", self.root / "receive-a"),
                         (target, report, target / "receive"),
                         (target, report, report / "receive"),
                         (target, target / "report.json", self.root / "receive-b"),
                         (self.root / "wrong-extension.txt", report, self.root / "receive-c")]
        for data_path, report_path, directory in invalid_pairs:
            with self.subTest(output=str(data_path), report=str(report_path), handoff=str(directory)):
                self.cli("giga-export", "--memory", memory, "--common-store", self.store_path,
                         "--projection", projection, "--preview", preview,
                         "--out", data_path, "--report", report_path,
                         "--handoff-out", directory, expected=2)
                self.assertFalse(data_path.exists())
                self.assertFalse(report_path.exists())
                self.assertFalse(directory.exists())

    def test_export_creates_concrete_handoff_for_each_supported_route(self):
        memory, report = self.path("common"), self.path("common-report")
        self.cli("common-result", "--store", self.store_path, "--out", memory, "--report", report)
        # Use the real source skill and runtime, so fresh checkouts need no ignored dist ZIP.
        installer = self.path("receiving-skill", ".zip")
        skill = ROOT / "skills" / "memory-import"
        with zipfile.ZipFile(installer, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in skill.rglob("*.md"):
                archive.write(file, "memory-import/" + file.relative_to(skill).as_posix())
            archive.writestr("memory-import/scripts/memory_tool.py", "from portable_memory.cli import main\nmain()\n")
            for file in (ROOT / "portable_memory").glob("*.py"):
                archive.write(file, "memory-import/scripts/portable_memory/" + file.name)
        for target in ("chatgpt", "claude", "gigaagent_pro", "other"):
            with self.subTest(target=target):
                output = self.path(target, ".zip")
                result = self.cli("export", memory, "--common-store", self.store_path,
                                  "--target", target, "--out", output, "--import-skill", installer)
                handoff = result["handoff"]
                delivered = Path(handoff["directory"])
                self.assertEqual(output.read_bytes(), (delivered / output.name).read_bytes())
                self.assertEqual(target, handoff["target"])
                self.assertEqual("not_written", handoff["native_memory"])
                self.assertEqual(target in {"chatgpt", "claude"}, (delivered / "memory-import.zip").exists())
                self.cli("inspect", delivered / output.name)
                if target == "gigaagent_pro":
                    self.assertIn("Amberkeep carry memory", Path(handoff["guide"]).read_text(encoding="utf-8"))

    def test_portable_export_rejects_wrong_target_and_nested_handoff_before_writes(self):
        memory, report = self.path("common"), self.path("common-report")
        self.cli("common-result", "--store", self.store_path, "--out", memory, "--report", report)
        for target in ("gigaagent", "not-a-recipient"):
            output = self.path("blocked", ".zip")
            self.cli("export", memory, "--common-store", self.store_path,
                     "--target", target, "--out", output, expected=2)
            self.assertFalse(output.exists())
        folder = self.root / "nested-handoff"
        self.cli("export", memory, "--common-store", self.store_path, "--out", folder / "data.zip",
                 "--handoff-out", folder, expected=2)
        self.assertFalse(folder.exists())
        output_parent = self.root / "data.zip"
        self.cli("export", memory, "--common-store", self.store_path, "--out", output_parent,
                 "--handoff-out", output_parent / "receive", expected=2)
        self.assertFalse(output_parent.exists())
        misleading = self.root / "data.md"
        self.cli("export", memory, "--common-store", self.store_path, "--out", misleading, expected=2)
        self.assertFalse(misleading.exists())

    def test_standalone_handoff_cannot_treat_arbitrary_markdown_as_giga_profile(self):
        bad_profile = self.path("ordinary-text", ".md")
        bad_profile.write_text("# About the user\nLikes short answers.\n", encoding="utf-8")
        destination = self.root / "bad-handoff"
        self.cli("handoff-prepare", "--target", "gigaagent", "--files", bad_profile,
                 "--out", destination, expected=2)
        self.assertFalse(destination.exists())

    def test_preview_required_and_changed_projection_needs_a_fresh_preview(self):
        memory, projection = self.reviewed_projection()
        target, report = self.path("final-profile", ".md"), self.path("final-report")
        response = self.cli("giga-export", "--memory", memory, "--common-store", self.store_path,
                            "--projection", projection, "--out", target, "--report", report, expected=2)
        self.assertIn("giga-preview", response["error"])
        self.assertFalse(target.exists())
        self.assertFalse(report.exists())
        prior = self.preview(memory, projection)
        changed = load_json(projection)
        changed["entries"][0]["record"]["description"] = "Уточнённая формулировка вымышленного правила"
        updated = self.review_projection(changed)
        self.cli("giga-export", "--memory", memory, "--common-store", self.store_path,
                 "--projection", updated, "--preview", prior, "--out", target, "--report", report, expected=2)
        self.assertFalse(target.exists())
        self.assertFalse(report.exists())
        fresh = self.preview(memory, updated)
        result = self.cli("giga-export", "--memory", memory, "--common-store", self.store_path,
                          "--projection", updated, "--preview", fresh, "--out", target, "--report", report)
        self.assertFalse(result["native_memory_applied"])
        self.assertTrue(target.is_file())

    def test_preview_exposes_exclusions_before_files_and_tampering_cannot_hide_them(self):
        memory, projection = self.reviewed_projection()
        data = load_json(projection)
        data["entries"][1].update(disposition="clarify", reason="Нужна проверка области применения вымышленного правила.")
        projection = self.review_projection(data)
        preview_path = self.preview(memory, projection)
        preview = load_json(preview_path)
        self.assertEqual(len(preview["ready"]), 1)
        self.assertEqual(len(preview["excluded"]), 1)
        excluded = preview["excluded"][0]
        self.assertEqual(excluded["name"], data["entries"][1]["record"]["name"])
        self.assertEqual(excluded["reason"], data["entries"][1]["reason"])
        self.assertTrue(excluded["next_action"])
        self.assertFalse(list(self.root.rglob("*.md")))
        target, report = self.path("profile", ".md"), self.path("report")
        tampered = copy.deepcopy(preview)
        tampered["excluded"] = []
        tampered_path = self.save("hidden-exclusion", tampered)
        self.cli("giga-export", "--memory", memory, "--common-store", self.store_path,
                 "--projection", projection, "--preview", tampered_path, "--out", target, "--report", report, expected=2)
        self.assertFalse(target.exists())
        self.assertFalse(report.exists())
        result = self.cli("giga-export", "--memory", memory, "--common-store", self.store_path,
                          "--projection", projection, "--preview", preview_path, "--out", target, "--report", report)
        delivered = Path(result["handoff"]["directory"])
        self.assertFalse((delivered / preview_path.name).exists())
        self.assertEqual({".md"}, {p.suffix for p in delivered.iterdir()})


if __name__ == "__main__":
    unittest.main()
