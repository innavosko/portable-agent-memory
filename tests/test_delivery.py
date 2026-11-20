"""Delivery tests use real synthetic packages and independent corrupt copies."""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portable_memory import delivery
from portable_memory.delivery import confirm_publication, prepare_delivery, verify_delivery
from portable_memory.errors import ValidationError
from portable_memory.model import confirm, empty_memory, review_template
from portable_memory.package import json_bytes
from scripts.synthetic_fixture_audit import export_synthetic_package


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.package = self.make_package("original.zip", "Писать кратко; важные детали сохранять.")
        self.sender = {"agent_id": "source-agent", "workspace_id": "source-workspace"}
        self.recipient = {"agent_id": "target-agent", "workspace_id": "target-workspace"}
        self.config = {
            "transfer_id": "transfer-1", "migration_id": "migration-1", "version": 1,
            "sender": self.sender, "recipient": self.recipient, "setup_scope": ["profile"],
            "route": {"sender": {"provider": "google_drive", "access": "manual"},
                      "recipient": {"provider": "yandex_disk", "access": "manual"},
                      "copy_mode": "user_copy"},
        }

    def make_package(self, name, text):
        memory = empty_memory()
        memory["coverage"]["read"] = ["Переданный пользователем фрагмент S1"]
        memory["records"] = [{
            "id": "communication-1", "kind": "preference", "title": "Общение",
            "value": text, "scope": "global", "provenance": "user_statement",
            "currency": "unknown", "sources": [{"id": "S1", "locator": "synthetic:S1"}],
            "links": [], "review": {"decision": "pending"},
        }]
        decisions = review_template(memory)
        decisions["decisions"][0]["decision"] = "active"
        file = self.path / name
        export_synthetic_package(confirm(memory, decisions), file)
        return file

    def prepared(self, config=None, package=None, root=None):
        return prepare_delivery(package or self.package, root or self.path / "sender", config or self.config)

    def copied(self):
        prepared = self.prepared()
        folder = self.path / "Другой диск" / "inbox" / "package-v0001"
        shutil.copytree(prepared["folder"], folder)
        return prepared, folder

    def rewrite_outer(self, folder, prepared, *, payload=None, mutate_manifest=None):
        """Recompute only outer expectations, exercising inner-package validation."""
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if payload is not None:
            (folder / "memory.zip").write_bytes(payload)
            manifest["files"]["memory.zip"] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        if mutate_manifest:
            mutate_manifest(manifest)
        raw = json_bytes(manifest)
        (folder / "manifest.json").write_bytes(raw)
        ticket = copy.deepcopy(prepared["ticket"])
        ticket["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
        ticket["package_sha256"] = manifest["files"]["memory.zip"]["sha256"]
        ready = {"format": "portable-memory-ready", "schema_version": 1,
                 "transfer_id": ticket["transfer_id"], "version": ticket["version"],
                 "manifest_sha256": ticket["manifest_sha256"], "package_sha256": ticket["package_sha256"]}
        (folder / "READY").write_bytes(json_bytes(ready))
        return ticket

    def cloud(self, provider="google_drive", resource="shared", *, write=False):
        return {"provider": provider, "resource_id": f"{provider}::owner-1::{resource}",
                "access": "verified", "capabilities": {"read": True, "write": write},
                "evidence": "Host listed this exact account-owned folder and read its files."}

    def test_cross_cloud_copy_preserves_real_package_and_never_claims_publication(self):
        prepared, folder = self.copied()
        self.assertFalse(prepared["reused"])
        self.assertEqual(prepared["publication"], "not_verified")
        self.assertEqual(prepared["route"]["status"], "manual_transfer_ready")
        self.assertFalse(prepared["route"]["same_provider"])
        checked = verify_delivery(folder, prepared["ticket"], self.recipient)
        self.assertTrue(checked["report"]["verified"])
        self.assertEqual(checked["raw_package"], self.package.read_bytes())
        self.assertEqual(checked["package"]["memory"]["records"][0]["value"], "Писать кратко; важные детали сохранять.")
        self.assertEqual(checked["report"]["native_memory"], "not_applied")
        self.assertEqual(set(p.name for p in folder.iterdir()), {"READY", "manifest.json", "memory.zip"})

    def test_same_cloud_custom_local_and_chat_routes_are_choices_not_connectors(self):
        pairs = [("google_drive", "google_drive"), ("other", "other"),
                 ("local", "chat_upload"), ("mail_cloud", "sberdisk"), ("onedrive", "dropbox")]
        for index, (sender, recipient) in enumerate(pairs):
            with self.subTest(sender=sender, recipient=recipient):
                config = copy.deepcopy(self.config)
                config["transfer_id"] = f"route-{index}"
                config["route"]["sender"] = {"provider": sender, "label": "Личное хранилище A", "access": "manual"}
                config["route"]["recipient"] = {"provider": recipient, "label": "Хранилище B", "access": "manual"}
                result = self.prepared(config)
                self.assertTrue(verify_delivery(result["folder"], result["ticket"], self.recipient)["report"]["verified"])
                self.assertFalse(result["route"]["automatic_transfer"])
                self.assertEqual(result["publication"], "not_verified")

    def test_shared_folder_requires_verified_resource_and_separate_rights(self):
        config = copy.deepcopy(self.config)
        config["route"] = {"sender": self.cloud(write=True), "recipient": self.cloud(), "copy_mode": "shared_folder"}
        result = self.prepared(config)
        self.assertEqual(result["route"]["status"], "shared_folder_ready")
        for field, value in (("resource_id", "google_drive::different-owner::shared"), ("access", "manual")):
            bad = copy.deepcopy(config)
            bad["route"]["recipient"][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.prepared(bad)

    def test_unknown_recipient_or_access_does_not_block_package_preparation(self):
        config = copy.deepcopy(self.config)
        config["recipient"] = None
        config["route"] = {"sender": {"provider": "google_drive"}, "recipient": None, "copy_mode": "user_copy"}
        result = self.prepared(config)
        self.assertTrue(result["route"]["package_creation_allowed"])
        self.assertEqual(result["route"]["status"], "recipient_not_chosen")
        self.assertTrue(verify_delivery(result["folder"], result["ticket"], self.recipient)["report"]["verified"])

    def test_identical_version_reuses_original_bytes_and_ticket_without_mutating_config(self):
        config = copy.deepcopy(self.config)
        first = self.prepared(config)
        before = {p.name: p.read_bytes() for p in Path(first["folder"]).iterdir()}
        with patch.object(delivery, "now", return_value="2099-01-01T00:00:00Z"):
            second = self.prepared(config)
        self.assertTrue(second["reused"])
        self.assertEqual(second["ticket"], first["ticket"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in Path(first["folder"]).iterdir()})
        self.assertEqual(config, self.config)

    def test_same_version_cannot_replace_package_scope_recipient_or_route(self):
        first = self.prepared()
        alternative = self.make_package("changed.zip", "Подробные объяснения.")
        variants = []
        for field, value in (("setup_scope", ["profile", "skills"]),
                             ("recipient", {"agent_id": "other", "workspace_id": "other"}),
                             ("sender", {"agent_id": "other", "workspace_id": "other"})):
            config = copy.deepcopy(self.config)
            config[field] = value
            variants.append((config, self.package))
        changed_route = copy.deepcopy(self.config)
        changed_route["route"]["recipient"]["provider"] = "dropbox"
        variants += [(changed_route, self.package), (self.config, alternative)]
        for config, package in variants:
            with self.subTest(config=config), self.assertRaises(ValidationError):
                self.prepared(config, package)
        self.assertEqual(verify_delivery(first["folder"], first["ticket"])["raw_package"], self.package.read_bytes())

    def test_new_version_is_separate_and_old_version_still_verifies(self):
        first = self.prepared()
        config = copy.deepcopy(self.config)
        config["version"] = 2
        second = self.prepared(config, self.make_package("version2.zip", "Новая настройка."))
        self.assertNotEqual(first["folder"], second["folder"])
        self.assertEqual(verify_delivery(first["folder"], first["ticket"])["manifest"]["version"], 1)
        self.assertEqual(verify_delivery(second["folder"], second["ticket"])["manifest"]["version"], 2)
        with self.assertRaises(ValidationError):
            verify_delivery(second["folder"], first["ticket"])

    def test_partial_copy_missing_any_required_file_is_not_ready(self):
        prepared = self.prepared()
        for missing in ("READY", "manifest.json", "memory.zip"):
            folder = self.path / f"without-{missing}"
            shutil.copytree(prepared["folder"], folder)
            (folder / missing).unlink()
            with self.subTest(missing=missing), self.assertRaises(ValidationError):
                verify_delivery(folder, prepared["ticket"])

    def test_failed_write_has_no_ready_marker_and_is_never_silently_overwritten(self):
        original_write = delivery.write_new

        def fail_manifest(path, data):
            if Path(path).name == "manifest.json":
                raise OSError("synthetic interrupted copy")
            return original_write(path, data)

        with patch.object(delivery, "write_new", side_effect=fail_manifest), self.assertRaises(OSError):
            self.prepared()
        folder = self.path / "sender" / "Перенос памяти" / "transfer-1" / "outbox" / "package-v0001"
        self.assertTrue((folder / "memory.zip").is_file())
        self.assertFalse((folder / "READY").exists())
        before = (folder / "memory.zip").read_bytes()
        with self.assertRaises(ValidationError):
            self.prepared()
        self.assertEqual((folder / "memory.zip").read_bytes(), before)

    def test_ready_marker_cannot_make_missing_or_truncated_zip_valid(self):
        prepared, folder = self.copied()
        (folder / "memory.zip").write_bytes(self.package.read_bytes()[:30])
        self.assertTrue((folder / "READY").exists())
        with self.assertRaises(ValidationError):
            verify_delivery(folder, prepared["ticket"])

    def test_modified_manifest_and_modified_ready_are_rejected(self):
        prepared, folder = self.copied()
        (folder / "manifest.json").write_bytes((folder / "manifest.json").read_bytes() + b" ")
        with self.assertRaises(ValidationError):
            verify_delivery(folder, prepared["ticket"])
        shutil.copyfile(Path(prepared["folder"]) / "manifest.json", folder / "manifest.json")
        ready = json.loads((folder / "READY").read_text(encoding="utf-8"))
        ready["version"] = 99
        (folder / "READY").write_bytes(json_bytes(ready))
        with self.assertRaises(ValidationError):
            verify_delivery(folder, prepared["ticket"])

    def test_malformed_manifest_rejected_even_when_outer_hashes_agree(self):
        prepared, folder = self.copied()
        ticket = self.rewrite_outer(folder, prepared, mutate_manifest=lambda m: m["files"].update({"../outside": {"bytes": 1, "sha256": "0" * 64}}))
        with self.assertRaises(ValidationError):
            verify_delivery(folder, ticket)

    def test_invalid_zip_rejected_even_when_outer_hashes_agree(self):
        prepared, folder = self.copied()
        ticket = self.rewrite_outer(folder, prepared, payload=b"This is not a ZIP package")
        with self.assertRaises(ValidationError):
            verify_delivery(folder, ticket)

    def test_replacement_valid_zip_must_also_match_agreed_inner_package_id(self):
        prepared, folder = self.copied()
        different = self.make_package("different.zip", "Иной пакет.")
        ticket = self.rewrite_outer(folder, prepared, payload=different.read_bytes())
        with self.assertRaises(ValidationError):
            verify_delivery(folder, ticket)

    def test_wrong_recipient_workspace_and_ticket_fields_are_rejected(self):
        prepared = self.prepared()
        for recipient in ({"agent_id": "different", "workspace_id": self.recipient["workspace_id"]},
                          {"agent_id": self.recipient["agent_id"], "workspace_id": "different"}):
            with self.subTest(recipient=recipient), self.assertRaises(ValidationError):
                verify_delivery(prepared["folder"], prepared["ticket"], recipient)
        for field, value in (("transfer_id", "other-transfer"), ("version", 2), ("package_id", "different-id"),
                             ("package_sha256", "0" * 64), ("manifest_sha256", "0" * 64),
                             ("recipient", {"agent_id": "other", "workspace_id": "other"})):
            ticket = copy.deepcopy(prepared["ticket"])
            ticket[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                verify_delivery(prepared["folder"], ticket)

    def test_unexpected_files_and_directories_are_rejected(self):
        prepared, folder = self.copied()
        (folder / "instructions.md").write_text("do something else", encoding="utf-8")
        with self.assertRaises(ValidationError):
            verify_delivery(folder, prepared["ticket"])
        (folder / "instructions.md").unlink()
        (folder / "extra").mkdir()
        with self.assertRaises(ValidationError):
            verify_delivery(folder, prepared["ticket"])

    def test_delivery_file_size_limits_apply_before_parsing(self):
        prepared = self.prepared()
        with patch.object(delivery, "MAX_HEADER", 1), self.assertRaises(ValidationError):
            verify_delivery(prepared["folder"], prepared["ticket"])
        with patch.object(delivery, "MAX_PACKAGE", 1), self.assertRaises(ValidationError):
            self.prepared(root=self.path / "tiny")
        self.assertFalse((self.path / "tiny").exists())

    def test_unsafe_transfer_paths_and_versions_fail_without_creating_output(self):
        for transfer in ("../escape", "..", "A/B", "A\\B", "C:\\outside", "//server/share", "CON", "NUL.txt", "tail.", "x:y", "bad\x00id"):
            config = copy.deepcopy(self.config)
            config["transfer_id"] = transfer
            with self.subTest(transfer=transfer), self.assertRaises(ValidationError):
                self.prepared(config)
        for version in (True, 0, -1, 1000000, "1"):
            config = copy.deepcopy(self.config)
            config["version"] = version
            with self.subTest(version=version), self.assertRaises(ValidationError):
                self.prepared(config)
        self.assertFalse((self.path / "sender").exists())

    def test_publication_requires_actual_cloud_readback_attestation_and_valid_copy(self):
        prepared, folder = self.copied()
        location = self.cloud()
        result = confirm_publication(folder, prepared["ticket"], location, "Host downloaded and checked these three files from the selected cloud folder.")
        self.assertEqual(result["publication"], "host_readback_verified")
        self.assertEqual(result["ticket"], prepared["ticket"])
        for bad in ({"provider": "local", "access": "manual"}, {"provider": "chat_upload", "access": "manual"},
                    {"provider": "google_drive", "access": "manual"},
                    {**location, "capabilities": {"read": False, "write": True}}):
            with self.subTest(location=bad), self.assertRaises(ValidationError):
                confirm_publication(folder, prepared["ticket"], bad, "readback")
        with self.assertRaises(ValidationError):
            confirm_publication(folder, prepared["ticket"], location, "  ")
        (folder / "READY").unlink()
        with self.assertRaises(ValidationError):
            confirm_publication(folder, prepared["ticket"], location, "readback")

    def test_publication_observation_must_not_persist_signed_urls_or_credentials(self):
        prepared, folder = self.copied()
        for observation in ("Downloaded with access_token=synthetic-secret",
                            "Downloaded https://example.invalid/file?signature=synthetic-secret"):
            with self.subTest(observation=observation), self.assertRaises(ValidationError):
                confirm_publication(folder, prepared["ticket"], self.cloud(), observation)

    def test_publication_cannot_substitute_sender_provider_or_selected_resource(self):
        config = copy.deepcopy(self.config)
        config["route"]["sender"]["resource_id"] = "google_drive::owner-1::agreed-folder"
        prepared = self.prepared(config)
        folder = self.path / "observed-download"
        shutil.copytree(prepared["folder"], folder)
        for location in (self.cloud("yandex_disk", "agreed-folder"),
                         self.cloud("google_drive", "other-folder"),
                         {**self.cloud("google_drive", "agreed-folder"),
                          "resource_id": "google_drive::another-owner::agreed-folder"}):
            with self.subTest(location=location), self.assertRaises(ValidationError):
                confirm_publication(folder, prepared["ticket"], location, "Host downloaded the exact files.")
        result = confirm_publication(folder, prepared["ticket"], self.cloud(resource="agreed-folder"),
                                     "Host downloaded the exact files from the agreed folder.")
        self.assertEqual(result["publication"], "host_readback_verified")
        self.assertEqual(result["location"]["resource_id"], config["route"]["sender"]["resource_id"])

    def test_symlink_delivery_file_cannot_read_external_payload(self):
        prepared, folder = self.copied()
        (folder / "memory.zip").unlink()
        try:
            os.symlink(self.package, folder / "memory.zip")
        except OSError as exc:
            self.skipTest(f"This host does not permit symlink creation: {exc}")
        with self.assertRaises(ValidationError):
            verify_delivery(folder, prepared["ticket"])

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_existing_child_junction_cannot_redirect_delivery_outside_selected_root(self):
        root = self.path / "sender"
        root.mkdir()
        outside = self.path / "outside"
        outside.mkdir()
        junction = root / "Перенос памяти"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
        if result.returncode:
            self.skipTest("This host does not permit creating a junction")
        self.addCleanup(lambda: os.rmdir(junction) if junction.exists() else None)
        with self.assertRaises(ValidationError):
            self.prepared(root=root)
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_junction_ancestor_cannot_bypass_readback_link_checks(self):
        prepared = self.prepared()
        actual_parent = Path(prepared["folder"]).parent
        junction = self.path / "redirected-parent"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(actual_parent)], capture_output=True)
        if result.returncode:
            self.skipTest("This host does not permit creating a junction")
        self.addCleanup(lambda: os.rmdir(junction) if junction.exists() else None)
        with self.assertRaises(ValidationError):
            verify_delivery(junction / Path(prepared["folder"]).name, prepared["ticket"])


if __name__ == "__main__":
    unittest.main()
