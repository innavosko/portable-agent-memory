"""Fictional offline CLI journey, including the manual copy between two folders."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from portable_memory.model import confirm, empty_memory, record_digest
from scripts.synthetic_fixture_audit import save_fixture_store


ROOT = Path(__file__).resolve().parents[1]


class DeliveryCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recipient = {"agent_id": "fictional-import-agent", "workspace_id": "fictional-personal"}
        self.config = {
            "transfer_id": "fictional-cli-transfer", "version": 1,
            "sender": {"agent_id": "fictional-export-agent", "workspace_id": "fictional-source"},
            "recipient": self.recipient,
            "route": {"sender": {"provider": "google_drive", "access": "manual"},
                      "recipient": {"provider": "yandex_disk", "access": "manual"}, "copy_mode": "user_copy"},
            "setup_scope": ["profile"],
        }
        memory = empty_memory()
        memory["records"] = [{
            "id": "language", "kind": "preference", "title": "Fictional language preference", "value": "Russian",
            "scope": "personal", "provenance": "user_statement", "currency": "unknown", "links": [],
            "sources": [{"id": "fictional", "locator": "Synthetic approved test source", "observed_at": None}],
            "review": {"decision": "pending"},
        }]
        memory = confirm(memory, {"decisions": [{"id": "language", "decision": "active",
                                                 "content_sha256": record_digest(memory["records"][0])}]})
        self.common_store = self.root / "common-profile.sqlite"
        audited = save_fixture_store(self.common_store, memory)
        self.memory_file = self.write_json("memory.json", audited["memory"], bom=True)
        self.config_file = self.write_json("config.json", self.config, bom=True)
        self.recipient_file = self.write_json("recipient.json", self.recipient, bom=True)
        self.package = self.root / "memory.zip"
        self.cli("export", self.memory_file, "--common-store", self.common_store, "--out", self.package)
        self.sending = self.root / "sending"
        self.receiving = self.root / "receiving-copy"
        self.results = self.root / "receiving-results"
        self.store = self.root / "receiving-state" / "delivery.sqlite"
        self.ticket = self.root / "sender-ticket.json"

    def write_json(self, name, value, *, bom=False):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8-sig" if bom else "utf-8")
        return path

    def cli(self, *args, expected=0, markdown=False):
        result = subprocess.run([sys.executable, "-B", "-m", "portable_memory", *map(str, args)], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
        text = result.stdout if expected == 0 else result.stderr
        return text if markdown else json.loads(text)

    def prepared_copy(self):
        prepared = self.cli("delivery-prepare", self.package, "--config", self.config_file,
                            "--root", self.sending, "--ticket-out", self.ticket)
        # This is an explicit fictional user copy, not an assertion of cloud access.
        shutil.copytree(prepared["folder"], self.receiving)
        return prepared

    def receive_args(self):
        return ["delivery-receive", self.receiving, "--ticket", self.ticket, "--recipient", self.recipient_file,
                "--store", self.store, "--root", self.results]

    def receipt_args(self):
        return ["delivery-receipt", "--store", self.store, "--transfer-id", self.config["transfer_id"],
                "--version", "1", "--recipient", self.recipient_file]

    def test_prepare_copy_preview_accept_and_setup_continue_through_cli(self):
        prepared = self.prepared_copy()
        self.assertEqual(prepared["publication"], "not_verified")
        self.assertEqual(set(Path(prepared["folder"]).iterdir()),
                         {Path(prepared["folder"]) / name for name in ("manifest.json", "memory.zip", "READY")})
        # Input files with a BOM, including the external ticket, work normally.
        ticket = json.loads(self.ticket.read_text(encoding="utf-8"))
        self.ticket.write_text(json.dumps(ticket), encoding="utf-8-sig")
        checked = self.cli("delivery-check", self.receiving, "--ticket", self.ticket, "--recipient", self.recipient_file)
        self.assertTrue(checked["verified"])
        self.assertNotIn("raw_package", checked)
        self.assertNotIn("package", checked)
        self.assertNotIn("manifest", checked)
        current = self.write_json("fictional-current-empty.json", empty_memory())
        preview = self.cli(*self.receive_args(), "--current", current)
        self.assertEqual(preview["status"], "awaiting_plan_acceptance")
        self.assertFalse(preview["writes_performed"])
        self.assertFalse(self.store.exists())
        self.assertFalse(self.results.exists())
        plan_file = self.write_json("reviewed-plan.json", preview["plan"], bom=True)
        accepted = self.cli(*self.receive_args(), "--current", current, "--plan", plan_file, "--accept")
        self.assertEqual(accepted["status"], "portable_workspace_prepared")
        self.assertEqual(accepted["setup_view"]["status"], "needs_setup")
        state_path = accepted["setup_path"]
        view = self.cli("setup-view", state_path)
        self.assertEqual(view["items"][0]["id"], "profile")
        before = Path(state_path).read_bytes()
        capability = self.write_json("capability-event.json", {
            "id": "fictional-capability", "actor": "agent", "type": "capability",
            "data": {"name": "native_memory", "available": True,
                     "evidence": "Synthetic host fixture reports this recipient's native operation as available"},
        }, bom=True)
        next_path = self.root / "setup-after-capability.json"
        self.cli("setup-event", state_path, "--event", capability, "--revision", "0", "--out", next_path)
        self.assertEqual(Path(state_path).read_bytes(), before)
        saved = self.cli("delivery-save-setup", "--store", self.store, "--state", next_path)
        self.assertEqual(saved["setup"]["revision"], 1)
        self.assertEqual(self.cli(*self.receipt_args())["setup"]["revision"], 1)
        done_event = self.write_json("verification-event.json", {
            "id": "fictional-readback", "actor": "agent", "type": "step_result",
            "data": {"step_id": "profile", "status": "verified",
                     "evidence": "Synthetic host fixture reports applying the requested profile",
                     "read_back": "Synthetic host fixture read-back matches Russian and personal scope"},
        }, bom=True)
        done_path = self.root / "setup-after-readback.json"
        completed = self.cli("setup-event", next_path, "--event", done_event, "--revision", "1", "--out", done_path)
        self.assertEqual(completed["status"], "completed_within_requested_scope")
        self.cli("delivery-save-setup", "--store", self.store, "--state", done_path)
        receipt = self.cli(*self.receipt_args())
        self.assertEqual(receipt["setup_view"]["status"], "completed_within_requested_scope")
        self.assertEqual(receipt["setup"]["items"][0]["result"]["verification"], "host_attestation")
        self.assertEqual(receipt["native_memory"], "not_applied_by_import")
        replay = self.cli(*self.receive_args(), "--accept")
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["setup"]["revision"], 2)
        markdown = self.cli("setup-view", done_path, "--format", "markdown", markdown=True)
        self.assertIn("сама не подключает сервисы", markdown)

    def test_ticket_cannot_be_written_inside_envelope(self):
        folder = self.sending / "Перенос памяти" / self.config["transfer_id"] / "outbox" / "package-v0001"
        error = self.cli("delivery-prepare", self.package, "--config", self.config_file, "--root", self.sending,
                         "--ticket-out", folder / "ticket.json", expected=2)
        self.assertIn("separately", error["error"])
        self.assertFalse(self.sending.exists())

    def test_existing_ticket_is_preserved_before_any_preparation(self):
        self.ticket.write_text("previously saved ticket", encoding="utf-8")
        error = self.cli("delivery-prepare", self.package, "--config", self.config_file, "--root", self.sending,
                         "--ticket-out", self.ticket, expected=2)
        self.assertIn("already exists", error["error"])
        self.assertEqual(self.ticket.read_text(), "previously saved ticket")
        self.assertFalse(self.sending.exists())

    def test_partial_copy_and_wrong_recipient_fail_without_import(self):
        self.prepared_copy()
        other = self.write_json("other-recipient.json", {**self.recipient, "workspace_id": "another-workspace"})
        error = self.cli("delivery-check", self.receiving, "--ticket", self.ticket, "--recipient", other, expected=2)
        self.assertIn("another recipient", error["error"])
        (self.receiving / "memory.zip").unlink()
        error = self.cli("delivery-check", self.receiving, "--ticket", self.ticket, "--recipient", self.recipient_file, expected=2)
        self.assertIn("not complete", error["error"])
        self.assertFalse(self.store.exists())

    def test_publication_requires_cloud_readback_attestation_and_writes_receipt(self):
        self.prepared_copy()
        location = self.write_json("fictional-cloud-location.json", {
            "provider": "google_drive", "resource_id": "google_drive::fictional-account::fictional-folder",
            "access": "verified", "capabilities": {"read": True, "write": True},
            "evidence": "Synthetic host read-back fixture; no real cloud account is accessed",
        }, bom=True)
        output = self.root / "publication.json"
        receipt = self.cli("delivery-published", self.receiving, "--ticket", self.ticket, "--location", location,
                           "--observation", "Synthetic host fixture supplied these read-back files", "--out", output)
        self.assertEqual(receipt["publication"], "host_readback_verified")
        saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved["publication"], "host_readback_verified")
        local = self.write_json("local-location.json", {"provider": "local", "access": "manual"})
        self.cli("delivery-published", self.receiving, "--ticket", self.ticket, "--location", local,
                 "--observation", "Only a local copy exists", "--out", self.root / "invalid-publication.json", expected=2)
        self.assertFalse((self.root / "invalid-publication.json").exists())

    def test_storage_catalog_and_help_do_not_claim_live_connections(self):
        catalog = self.cli("storage-options")
        self.assertIn("google_drive", {item["id"] for item in catalog})
        self.assertIn("yandex_disk", {item["id"] for item in catalog})
        self.assertTrue(all("connected" not in item for item in catalog))
        text = self.cli("--help", markdown=True)
        for command in ("delivery-prepare", "delivery-check", "delivery-published", "delivery-receive",
                        "delivery-receipt", "delivery-save-setup", "setup-view", "setup-event"):
            self.assertIn(command, text)


if __name__ == "__main__":
    unittest.main()
