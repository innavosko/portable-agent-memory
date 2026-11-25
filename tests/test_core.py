import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from portable_memory.errors import ValidationError
from portable_memory.importing import apply_import, plan_import
from portable_memory.model import confirm, empty_memory, review_template, validate_memory
from portable_memory.package import load_json, read_package
from portable_memory.package import export_package as export_checked_package
from scripts.synthetic_fixture_audit import export_synthetic_package, synthetic_review

ROOT = Path(__file__).resolve().parents[1]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.memory = load_json(ROOT / "examples" / "profile.json")

    def reviewed(self):
        decisions = review_template(self.memory)
        for item in decisions["decisions"]:
            item["decision"] = {"project-1": "historical", "idea-1": "pending", "old-1": "exclude"}.get(item["id"], "active")
        return confirm(self.memory, decisions)

    def package(self, memory=None, name="memory.zip"):
        output = self.path / name
        export_synthetic_package(memory if memory is not None else self.reviewed(), output)
        return output

    def test_roundtrip_active_history_pending_excluded_and_rule_details(self):
        archive = self.package()
        data = read_package(archive)
        self.assertEqual(len(data["memory"]["records"]), 6)
        # Explicit fictional empty recipient snapshot; absence is not empty.
        plan = plan_import(archive, empty_memory())
        report = apply_import(archive, plan, self.path / "received", accepted=True)
        result = load_json(self.path / "received" / "memory.json")
        self.assertEqual(len(result["records"]), 5)
        rule = next(r for r in result["records"] if r["id"] == "rule-1")
        self.assertEqual(rule["scope"], "account:mail-1")
        self.assertTrue(rule["value"]["requires_confirmation"])
        self.assertEqual(rule["value"]["exceptions"], [])
        active = (self.path / "received" / "ACTIVE_PROFILE.md").read_text(encoding="utf-8")
        self.assertIn("nina@example.org", active)
        self.assertNotIn("Завершённый проект", active)
        self.assertNotIn("Предположение агента", active)
        self.assertEqual(report["native_memory"], "not_applied")
        self.assertTrue(all(i["action"] in {"skip", "defer"} for i in plan_import(archive, result)["items"]))

    def test_confirmation_rejects_stale_content(self):
        decisions = review_template(self.memory)
        self.memory["records"][0]["value"] = "Changed after review"
        with self.assertRaises(ValidationError):
            confirm(self.memory, decisions)

    def test_missing_common_review_is_rejected_before_any_output(self):
        output = self.path / "not-created" / "memory.zip"
        with self.assertRaisesRegex(ValidationError, "common profile review is mandatory"):
            export_checked_package(self.reviewed(), output)
        self.assertFalse(output.parent.exists())

    def test_synthetic_audit_preserves_decisions_and_validates_real_source_ids(self):
        reviewed = self.reviewed()
        audited = synthetic_review(reviewed)
        self.assertEqual(reviewed["records"], audited["memory"]["records"])
        self.assertTrue(all(item["evidence_status"] == "verified"
                            for item in audited["review"]["record_checks"]))
        self.assertEqual("pending", next(record for record in audited["memory"]["records"]
                         if record["id"] == "idea-1")["review"]["decision"])

    def test_user_reconfirmation_does_not_replace_stale_common_review(self):
        audited = synthetic_review(self.reviewed())
        changed = copy.deepcopy(audited["memory"])
        changed["records"][0]["value"] = "A newly confirmed fictional name"
        decisions = review_template(changed)
        decisions["decisions"] = [decisions["decisions"][0]]
        decisions["decisions"][0]["decision"] = "active"
        changed = confirm(changed, decisions)
        validate_memory(changed)
        output = self.path / "not-created" / "memory.zip"
        with self.assertRaisesRegex(ValidationError, "Common review is stale"):
            export_checked_package(changed, output, review=audited["review"])
        self.assertFalse(output.parent.exists())

    def test_legacy_zip_without_common_review_is_rejected(self):
        archive = self.package()
        with zipfile.ZipFile(archive) as source:
            files = {name: source.read(name) for name in source.namelist()}
        manifest = json.loads(files["manifest.json"])
        files.pop("profile-review.json")
        manifest["files"].pop("profile-review.json")
        files["manifest.json"] = json.dumps(manifest).encode()
        legacy = self.path / "legacy.zip"
        with zipfile.ZipFile(legacy, "w") as target:
            for name, raw in files.items():
                target.writestr(name, raw)
        with self.assertRaisesRegex(ValidationError, "Missing mandatory common profile review"):
            read_package(legacy)

    def test_correct_zip_checksum_does_not_validate_a_stale_common_review(self):
        archive = self.package()
        with zipfile.ZipFile(archive) as source:
            files = {name: source.read(name) for name in source.namelist()}
        review = json.loads(files["profile-review.json"])
        review["memory_sha256"] = "0" * 64
        raw = json.dumps(review).encode()
        files["profile-review.json"] = raw
        manifest = json.loads(files["manifest.json"])
        manifest["files"]["profile-review.json"] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        files["manifest.json"] = json.dumps(manifest).encode()
        replaced = self.path / "replaced-review.zip"
        with zipfile.ZipFile(replaced, "w") as target:
            for name, content in files.items():
                target.writestr(name, content)
        with self.assertRaisesRegex(ValidationError, "Common review is stale"):
            read_package(replaced)

    def test_edit_requires_new_review_even_during_partial_confirmation(self):
        memory = self.reviewed()
        memory["records"][0]["value"] = "Changed"
        with self.assertRaises(ValidationError):
            validate_memory(memory)
        fixed = confirm(memory, {"decisions": []})
        self.assertEqual(fixed["records"][0]["review"]["decision"], "pending")
        self.assertEqual(fixed["records"][1]["review"]["decision"], "active")

    def test_conflict_is_deferred_until_explicit_choice(self):
        incoming = self.reviewed()
        current = copy.deepcopy(incoming)
        current["records"][0]["value"] = "Different current name"
        decisions = review_template(current)
        decisions["decisions"] = [decisions["decisions"][0]]
        decisions["decisions"][0]["decision"] = "active"
        current = confirm(current, decisions)
        archive = self.package(incoming)
        plan = plan_import(archive, current)
        item = next(i for i in plan["items"] if i["id"] == "profile-1")
        self.assertEqual((item["status"], item["action"]), ("conflict", "defer"))
        item["action"] = "replace"
        apply_import(archive, plan, self.path / "merged", accepted=True)
        merged = load_json(self.path / "merged" / "memory.json")
        self.assertEqual(merged["records"][0]["id"], "mail-1")
        self.assertEqual(next(r for r in merged["records"] if r["id"] == "profile-1")["value"], "Обращаться: Нина")

    def test_alias_conflict_defers_dependent_links(self):
        memory = self.reviewed()
        current = empty_memory()
        account = copy.deepcopy(memory["records"][1])
        account["id"] = "local-mail"
        current["records"] = [account]
        decision = review_template(current)
        decision["decisions"][0]["decision"] = "active"
        current = confirm(current, decision)
        plan = plan_import(self.package(memory), current)
        by_id = {item["id"]: item for item in plan["items"]}
        self.assertEqual(by_id["mail-1"]["status"], "conflict")
        self.assertEqual(by_id["rule-1"]["status"], "blocked_dependency")
        self.assertEqual(by_id["rule-1"]["action"], "defer")

    def test_package_tamper_and_extra_file_rejected(self):
        archive = self.package()
        with zipfile.ZipFile(archive) as source:
            members = {n: source.read(n) for n in source.namelist()}
        for name, changes in (("edited.zip", {"PROFILE.md": b"changed"}), ("extra.zip", {"run.py": b"unlisted"})):
            altered = self.path / name
            with zipfile.ZipFile(altered, "w") as target:
                for key, value in {**members, **changes}.items():
                    target.writestr(key, value)
            with self.assertRaises(ValidationError):
                read_package(altered)

    def test_cannot_force_pending_or_rewrite_plan_classification(self):
        archive = self.package()
        plan = plan_import(archive)
        pending = next(i for i in plan["items"] if i["id"] == "idea-1")
        pending["action"] = "add"
        with self.assertRaises(ValidationError):
            apply_import(archive, plan, self.path / "forced", accepted=True)
        self.assertFalse((self.path / "forced").exists())
        pending["status"] = "new"
        with self.assertRaises(ValidationError):
            apply_import(archive, plan, self.path / "forged", accepted=True)

    def test_schema_rejects_duplicate_ids_and_credentials(self):
        duplicate = copy.deepcopy(self.memory)
        duplicate["records"].append(duplicate["records"][0])
        with self.assertRaises(ValidationError):
            validate_memory(duplicate)
        self.memory["records"][0]["value"] = {"api_key": "fixture-only-not-a-secret"}
        with self.assertRaises(ValidationError):
            validate_memory(self.memory)

    def test_package_size_and_case_collision(self):
        from unittest.mock import patch
        with patch("portable_memory.package.MAX_FILE", 5):
            with self.assertRaises(ValidationError):
                self.package()
        with zipfile.ZipFile(self.path / "case.zip", "w") as archive:
            archive.writestr("a.json", "{}")
            archive.writestr("A.json", "{}")
        with self.assertRaises(ValidationError):
            read_package(self.path / "case.zip")

    def test_apply_requires_acceptance_and_refuses_existing_directory(self):
        archive = self.package()
        plan = plan_import(archive)
        with self.assertRaises(ValidationError):
            apply_import(archive, plan, self.path / "no-permission")
        with self.assertRaises(ValidationError):
            apply_import(archive, plan, self.path, accepted=True)

    def test_cli_review_and_invalid_input_exit_code(self):
        out = self.path / "review.md"
        result = subprocess.run([sys.executable, "-m", "portable_memory", "review",
                                 str(ROOT / "examples" / "profile.json"), "--out", str(out)],
                                cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(out.exists())
        self.assertEqual(json.loads(result.stdout.decode("utf-8"))["review"], str(out))
        decisions = load_json(str(out) + ".decisions.json")
        self.assertTrue(all(x["decision"] == "pending" for x in decisions["decisions"]))
        result = subprocess.run([sys.executable, "-m", "portable_memory", "inspect", str(out)], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"error", result.stderr)


if __name__ == "__main__":
    unittest.main()
