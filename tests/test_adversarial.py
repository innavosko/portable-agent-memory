"""Regression checks from an independent review of package/merge behavior."""
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from portable_memory.errors import ValidationError
from portable_memory.importing import apply_import, plan_import
from portable_memory.model import confirm, empty_memory, record_digest
from portable_memory.package import read_package
from scripts.synthetic_fixture_audit import export_synthetic_package


def reviewed_record(rid="rule-1", value="Use primary@example.invalid", links=None):
    record = {
        "id": rid, "kind": "rule", "title": "Default outgoing mailbox",
        "scope": "global", "value": value,
        "sources": [{"id": "test-source", "locator": "Synthetic review conversation", "observed_at": None}],
        "provenance": "user_statement", "currency": "current", "links": links or [],
        "review": {"decision": "pending"},
    }
    document = empty_memory()
    document["records"] = [record]
    return confirm(document, {"decisions": [{"id": rid, "decision": "active", "content_sha256": record_digest(record)}]})["records"][0]


def memory(*records):
    document = empty_memory()
    document["records"] = list(records)
    return document


class AdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "memory.zip"

    def test_changed_graph_edge_is_not_silently_skipped(self):
        current = memory(reviewed_record())
        updated = memory(reviewed_record(links=[{"target": "project-2", "relation": "applies-to", "basis": "source"}]))
        export_synthetic_package(updated, self.package)
        plan = plan_import(self.package, current)
        self.assertNotEqual(plan["items"][0]["status"], "duplicate")
        self.assertNotEqual(plan["items"][0]["action"], "skip")

    def test_conflicting_incoming_records_are_not_both_automatically_added(self):
        incoming = memory(reviewed_record("rule-one", "Use one@example.invalid"),
                          reviewed_record("rule-two", "Use two@example.invalid"))
        export_synthetic_package(incoming, self.package, semantic_groups=[{
            "id": "explicit-fixture-mailbox-conflict", "relation": "conflict",
            "record_ids": ["rule-one", "rule-two"], "canonical_id": None,
            "note": "The fictional instructions name different default outgoing mailboxes in the same scope."
        }])
        try:
            plan = plan_import(self.package, empty_memory())
        except ValidationError:
            return  # Rejecting an internally ambiguous incoming package is valid.
        self.assertFalse(all(item["action"] == "add" for item in plan["items"]))

    def test_original_zip_path_rejected_before_windows_normalization(self):
        export_synthetic_package(memory(reviewed_record()), self.package)
        hostile = self.root / "hostile.zip"
        with zipfile.ZipFile(self.package) as source, zipfile.ZipFile(hostile, "w") as destination:
            for item in source.infolist():
                name = item.filename.replace("wiki/", "wiki\\")
                info = zipfile.ZipInfo(name)
                info.filename = name  # Ensure Windows writer doesn't normalize fixture.
                destination.writestr(info, source.read(item))
        with self.assertRaises(ValidationError):
            read_package(hostile)

    def test_failed_import_does_not_publish_partial_workspace(self):
        export_synthetic_package(memory(reviewed_record()), self.package)
        plan = plan_import(self.package, empty_memory())
        target = self.root / "received"
        from portable_memory import importing
        original = importing.write_new
        writes = 0

        def fail_midway(path, data):
            nonlocal writes
            writes += 1
            if writes == 3:
                raise OSError("Simulated storage interruption")
            return original(path, data)

        with patch("portable_memory.importing.write_new", side_effect=fail_midway):
            with self.assertRaises(OSError):
                apply_import(self.package, plan, target, accepted=True)
        self.assertFalse(target.exists())

    def test_content_edit_invalidates_confirmation(self):
        document = memory(reviewed_record())
        document["records"][0]["value"] = "Changed after review"
        with self.assertRaises(ValidationError):
            export_synthetic_package(document, self.package)

    def test_repeat_import_of_unchanged_package_is_a_skip(self):
        document = memory(reviewed_record())
        export_synthetic_package(document, self.package)
        plan = plan_import(self.package, document)
        self.assertEqual(plan["items"][0]["status"], "duplicate")
        report = apply_import(self.package, plan, self.root / "repeated", accepted=True)
        self.assertEqual(report["records"][0]["action"], "skip")

    def test_excluded_value_is_absent_from_generated_package(self):
        original = reviewed_record()
        document = memory(original)
        document = confirm(document, {"decisions": [{"id": original["id"], "decision": "exclude", "content_sha256": record_digest(original)}]})
        export_synthetic_package(document, self.package)
        loaded = read_package(self.package)
        self.assertEqual(loaded["memory"]["records"], [])
        for content in loaded["files"].values():
            self.assertNotIn(b"primary@example.invalid", content)

    def test_failed_exclusive_export_does_not_delete_another_writers_file(self):
        sentinel = b"another writer completed this file"

        def competing_writer(*args, **kwargs):
            self.package.write_bytes(sentinel)
            raise FileExistsError("Destination created by another writer")

        with patch("portable_memory.package.zipfile.ZipFile", side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                export_synthetic_package(memory(reviewed_record()), self.package)
        self.assertTrue(self.package.exists())
        self.assertEqual(self.package.read_bytes(), sentinel)

    def test_package_checksum_and_content_come_from_the_same_snapshot(self):
        export_synthetic_package(memory(reviewed_record()), self.package)
        alternate = self.root / "alternate.zip"
        export_synthetic_package(memory(reviewed_record(value="Use altered@example.invalid")), alternate)
        original_bytes = self.package.read_bytes()
        alternate_bytes = alternate.read_bytes()
        original_read = Path.read_bytes

        def switch_after_read(path):
            value = original_read(path)
            if path == self.package:
                path.write_bytes(alternate_bytes)
            return value

        with patch.object(Path, "read_bytes", switch_after_read):
            loaded = read_package(self.package)
        self.assertEqual(loaded["sha256"], hashlib.sha256(original_bytes).hexdigest())
        self.assertEqual(loaded["memory"]["records"][0]["value"], "Use primary@example.invalid")

    def test_apply_rejects_package_changed_after_plan_validation(self):
        export_synthetic_package(memory(reviewed_record()), self.package)
        alternate = self.root / "alternate.zip"
        export_synthetic_package(memory(reviewed_record(value="Use altered@example.invalid")), alternate)
        reviewed_data = read_package(self.package)
        changed_data = read_package(alternate)
        plan = plan_import(self.package, empty_memory())
        reads = 0

        def changing_package(path):
            nonlocal reads
            reads += 1
            return reviewed_data if reads == 1 else changed_data

        target = self.root / "received"
        with patch("portable_memory.importing.read_package", side_effect=changing_package):
            try:
                report = apply_import(self.package, plan, target, accepted=True)
            except ValidationError:
                self.assertFalse(target.exists())
                return
        # A single-read implementation is also safe: it applies reviewed data.
        import json
        stored = json.loads((target / "memory.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["records"][0]["value"], "Use primary@example.invalid")


if __name__ == "__main__":
    unittest.main()
