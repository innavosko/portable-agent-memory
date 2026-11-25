"""Offline receiving-setup checks; every native result below is fictional."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from portable_memory.errors import ValidationError
from portable_memory.model import confirm, digest, empty_memory, record_digest
from portable_memory.setup import (
    apply_setup_event, create_setup, render_setup_view, setup_view, validate_setup,
)


def memory_of(*specs):
    memory = empty_memory()
    decisions = []
    for kind, rid, decision, value in specs:
        record = {"id": rid, "kind": kind, "title": "Fictional " + rid, "value": value,
                  "scope": "personal", "provenance": "user_statement", "currency": "unknown",
                  "sources": [{"id": "example", "locator": "Fictional user-approved example", "observed_at": None}],
                  "review": {"decision": "pending"}, "links": []}
        memory["records"].append(record)
        if decision != "pending":
            decisions.append({"id": rid, "decision": decision, "content_sha256": record_digest(record)})
    return confirm(memory, {"decisions": decisions})


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.count = 0
        self.memory = memory_of(("preference", "language", "active", "Russian"))

    def delivery(self, memory, *, version=1, recipient=None, scope=None):
        self.count += 1
        workspace = self.root / ("workspace-" + str(self.count))
        workspace.mkdir()
        package_hash = f"{version:064x}"
        (workspace / "memory.json").write_text(json.dumps(memory), encoding="utf-8")
        (workspace / "receipt.json").write_text(json.dumps({
            "format": "portable-memory-import-receipt", "target": "portable_workspace",
            "package_sha256": package_hash, "records": [], "native_memory": "not_applied",
            "current_source": "provided_snapshot", "current_sha256": digest(empty_memory()),
            "recipient_memory_required": False,
        }), encoding="utf-8")
        return {"transfer_id": "fictional-transfer", "version": version, "package_sha256": package_hash,
                "recipient": recipient or {"agent_id": "receiving-agent", "workspace_id": "personal-space"},
                "workspace": str(workspace), "setup_scope": ["profile"] if scope is None else scope}

    def event(self, state, kind, data, *, actor="agent", event_id=None):
        return apply_setup_event(state, {"id": event_id or "event-" + str(state["revision"] + 1),
                                         "actor": actor, "type": kind, "data": data},
                                 expected_revision=state["revision"])

    def available(self, state, name="native_memory", evidence="Fictional host checked native memory in personal-space"):
        return self.event(state, "capability", {"name": name, "available": True, "evidence": evidence})

    def verify(self, state, step_id="profile"):
        return self.event(state, "step_result", {"step_id": step_id, "status": "verified",
                                                "evidence": "Fictional host performed this exact requested operation",
                                                "read_back": "Fictional read-back matches the approved contents and scope"})

    def ready(self):
        return self.verify(self.available(create_setup(self.memory, self.delivery(self.memory))))

    def test_imported_workspace_is_distinct_from_native_setup(self):
        state = create_setup(self.memory, self.delivery(self.memory))
        view = setup_view(state)
        self.assertEqual(view["portable_workspace"]["verification"], "local_files_read_back")
        self.assertEqual(view["status"], "needs_setup")
        self.assertEqual(view["items"][0]["status"], "pending")
        self.assertTrue(all(not cap["available"] and cap["evidence"] is None for cap in view["capabilities"].values()))
        self.assertEqual(view["verification"], "host_attestation_for_native_steps")

    def test_actual_workspace_memory_and_receipt_are_required(self):
        for defect in ("missing", "changed_memory", "wrong_hash", "wrong_target"):
            with self.subTest(defect=defect):
                delivery = self.delivery(self.memory)
                folder = Path(delivery["workspace"])
                if defect == "missing":
                    (folder / "receipt.json").unlink()
                elif defect == "changed_memory":
                    (folder / "memory.json").write_text(json.dumps(empty_memory()), encoding="utf-8")
                else:
                    receipt = json.loads((folder / "receipt.json").read_text())
                    receipt["package_sha256" if defect == "wrong_hash" else "target"] = "f" * 64 if defect == "wrong_hash" else "native_memory"
                    (folder / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
                with self.assertRaises(ValidationError):
                    create_setup(self.memory, delivery)

    def test_pending_data_never_becomes_setup_or_completion(self):
        memory = memory_of(("profile", "unconfirmed", "pending", "An inference"))
        state = create_setup(memory, self.delivery(memory))
        view = setup_view(state)
        self.assertEqual(view["items"], [])
        self.assertEqual(view["unreviewed_record_ids"], ["unconfirmed"])
        self.assertEqual(view["status"], "needs_review")
        self.assertEqual(state["memory"], memory)

    def test_scope_and_historical_records_do_not_create_unrequested_actions(self):
        memory = memory_of(("profile", "about", "active", "Description"),
                           ("rule", "old-rule", "historical", "Old rule"),
                           ("project", "old-project", "historical", "Keep as history"),
                           ("skill", "old-skill", "historical", "Do not install"),
                           ("workflow", "current-workflow", "active", "Reusable process"),
                           ("account", "mail", "active", "mail@example.org"),
                           ("automation", "digest", "active", "Weekly digest"))
        delivery = self.delivery(memory, scope=["projects", "skills"])
        state = create_setup(memory, delivery)
        self.assertEqual({item["id"] for item in state["items"]}, {"projects:old-project", "skills:current-workflow"})
        self.assertEqual(setup_view(state)["unreviewed_record_ids"], [])

    def test_profile_is_grouped_and_all_other_steps_are_per_record(self):
        memory = memory_of(("profile", "about", "active", "Description"),
                           ("rule", "rule", "active", "Show drafts"),
                           ("contact", "contact", "active", "Contact details"),
                           ("project", "project", "active", "Project"),
                           ("artifact", "artifact", "active", "Selected output"),
                           ("skill", "skill", "active", "Reviewed skill"),
                           ("integration", "mail", "active", "Fictional mail service"),
                           ("automation", "digest", "active", "Weekly digest"))
        state = create_setup(memory, self.delivery(memory, scope=["profile", "projects", "skills", "integrations", "automations"]))
        self.assertEqual(state["items"][0]["record_ids"], ["about", "contact", "rule"])
        self.assertEqual(len(state["items"]), 6)
        for name in state["capabilities"]:
            state = self.available(state, name, "Fictional verified host operation: " + name)
        for item in state["items"]:
            state = self.verify(state, item["id"])
        self.assertEqual(setup_view(state)["status"], "completed_within_requested_scope")
        self.assertTrue(all(item["result"]["verification"] == "host_attestation" for item in state["items"]))

    def test_verified_requires_actual_capability_and_read_back(self):
        state = create_setup(self.memory, self.delivery(self.memory))
        with self.assertRaises(ValidationError):
            self.verify(state)
        state = self.available(state)
        for missing in ("evidence", "read_back"):
            data = {"step_id": "profile", "status": "verified", "evidence": "Host operation", "read_back": "Read actual result"}
            data.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValidationError):
                self.event(state, "step_result", data)
        self.assertEqual(setup_view(self.verify(state))["status"], "completed_within_requested_scope")

    def test_replay_is_noop_stale_revision_or_changed_event_rejected(self):
        state = create_setup(self.memory, self.delivery(self.memory))
        envelope = {"id": "unique", "actor": "agent", "type": "capability",
                    "data": {"name": "native_memory", "available": True, "evidence": "Fictional native capability check"}}
        changed = apply_setup_event(state, envelope, expected_revision=0)
        self.assertEqual(apply_setup_event(changed, envelope, expected_revision=0), changed)
        altered = copy.deepcopy(envelope)
        altered["data"]["available"] = False
        with self.assertRaises(ValidationError):
            apply_setup_event(changed, altered, expected_revision=1)
        altered["id"] = "new"
        with self.assertRaises(ValidationError):
            apply_setup_event(changed, altered, expected_revision=0)
        self.assertEqual(state["revision"], 0)

    def test_already_verified_operations_cannot_be_repeated(self):
        state = self.ready()
        with self.assertRaises(ValidationError):
            self.verify(state)
        with self.assertRaises(ValidationError):
            self.event(state, "retry", {"step_id": "profile"})

    def test_deferral_is_user_choice_and_agent_cannot_resume_it(self):
        state = self.available(create_setup(self.memory, self.delivery(self.memory)))
        data = {"step_id": "profile", "status": "deferred", "evidence": "User chooses to continue later"}
        with self.assertRaises(ValidationError):
            self.event(state, "step_result", data)
        state = self.event(state, "step_result", data, actor="user")
        self.assertEqual(setup_view(state)["status"], "needs_setup")
        with self.assertRaises(ValidationError):
            self.event(state, "retry", {"step_id": "profile"})
        state = self.event(state, "retry", {"step_id": "profile"}, actor="user")
        self.assertEqual(state["items"][0]["status"], "pending")

    def test_failed_step_requires_available_capability_before_retry(self):
        state = create_setup(self.memory, self.delivery(self.memory))
        state = self.event(state, "step_result", {"step_id": "profile", "status": "failed", "evidence": "Fictional host did not finish"})
        with self.assertRaises(ValidationError):
            self.event(state, "retry", {"step_id": "profile"})
        state = self.available(state)
        with self.assertRaises(ValidationError):
            self.verify(state)
        state = self.event(state, "retry", {"step_id": "profile"})
        self.assertEqual(setup_view(self.verify(state))["status"], "completed_within_requested_scope")

    def test_lost_capability_preserves_history_but_revokes_current_readiness(self):
        state = self.ready()
        old_result = copy.deepcopy(state["items"][0]["result"])
        state = self.event(state, "capability", {"name": "native_memory", "available": False,
                                                 "evidence": "Fictional access was removed by user"})
        self.assertEqual(state["items"][0]["status"], "blocked")
        self.assertEqual(state["items"][0]["last_verification"], old_result)
        self.assertEqual(setup_view(state)["status"], "needs_setup")
        state = self.available(state)
        self.assertEqual(state["items"][0]["status"], "blocked")
        state = self.event(state, "retry", {"step_id": "profile"})
        self.assertEqual(setup_view(self.verify(state))["status"], "completed_within_requested_scope")

    def test_changed_native_destination_context_requires_new_read_back(self):
        state = self.ready()
        state = self.available(state, evidence="Fictional native tool now targets another permitted memory container")
        self.assertEqual(state["items"][0]["status"], "blocked")
        self.assertEqual(setup_view(state)["status"], "needs_setup")
        self.assertEqual(state["items"][0]["last_verification"]["status"], "verified")

    def test_new_package_reuses_only_unchanged_content_for_same_recipient(self):
        old = self.ready()
        memory = copy.deepcopy(self.memory)
        memory["records"][0]["review"]["reviewed_at"] = "2026-11-20T12:00:00+00:00"
        memory["metadata"]["last_import"] = {"package_sha256": "a" * 64}
        state = create_setup(memory, self.delivery(memory, version=2), previous=old)
        self.assertEqual(state["items"][0]["result"], old["items"][0]["result"])
        self.assertEqual(setup_view(state)["status"], "completed_within_requested_scope")
        self.assertEqual(state["revision"], 0)

    def test_changed_profile_never_reuses_previous_native_result(self):
        old = self.ready()
        memory = memory_of(("preference", "language", "active", "English"))
        state = create_setup(memory, self.delivery(memory, version=2), previous=old)
        self.assertEqual(state["items"][0]["status"], "pending")
        self.assertIsNone(state["items"][0]["result"])
        self.assertEqual(setup_view(state)["status"], "needs_setup")
        self.assertNotEqual(state["items"][0]["content_sha256"], old["items"][0]["content_sha256"])

    def test_different_recipient_or_account_workspace_does_not_reuse_access(self):
        old = self.ready()
        for recipient in ({"agent_id": "other-agent", "workspace_id": "personal-space"},
                          {"agent_id": "receiving-agent", "workspace_id": "work-account"}):
            with self.subTest(recipient=recipient):
                state = create_setup(self.memory, self.delivery(self.memory, version=2, recipient=recipient), previous=old)
                self.assertFalse(state["capabilities"]["native_memory"]["available"])
                self.assertEqual(state["items"][0]["status"], "pending")

    def test_scope_empty_or_without_records_is_not_native_completion(self):
        state = create_setup(self.memory, self.delivery(self.memory, scope=[]))
        self.assertEqual(setup_view(state)["status"], "portable_workspace_only")
        state = create_setup(self.memory, self.delivery(self.memory, scope=["skills"]))
        self.assertEqual(setup_view(state)["status"], "no_configuration_items")
        self.assertEqual(state["items"], [])

    def test_completed_steps_do_not_hide_pending_profile_content(self):
        memory = memory_of(("preference", "language", "active", "Russian"),
                           ("rule", "unreviewed-rule", "pending", "Questionable extracted rule"))
        state = self.verify(self.available(create_setup(memory, self.delivery(memory))))
        self.assertEqual(setup_view(state)["status"], "needs_review")
        self.assertEqual(setup_view(state)["summary"]["verified"], 1)

    def test_deferred_incoming_record_blocks_completion_without_entering_workspace(self):
        delivery = self.delivery(self.memory)
        delivery["pending_record_ids"] = ["incoming-conflict"]
        state = self.verify(self.available(create_setup(self.memory, delivery)))
        view = setup_view(state)
        self.assertEqual(view["status"], "needs_review")
        self.assertEqual(view["unreviewed_record_ids"], ["incoming-conflict"])
        self.assertEqual([r["id"] for r in state["memory"]["records"]], ["language"])
        self.assertEqual(view["summary"]["verified"], 1)

    def test_changed_profile_keeps_an_unaffected_verified_skill(self):
        specs = [("preference", "language", "active", "Russian"),
                 ("skill", "writing", "active", "A reviewed writing procedure")]
        memory = memory_of(*specs)
        old = create_setup(memory, self.delivery(memory, scope=["profile", "skills"]))
        old = self.available(self.available(old), "install_skill", "Fictional skill installation target checked")
        old = self.verify(self.verify(old), "skills:writing")
        changed = memory_of(("preference", "language", "active", "English"), specs[1])
        state = create_setup(changed, self.delivery(changed, version=2, scope=["profile", "skills"]), previous=old)
        statuses = {item["id"]: item["status"] for item in state["items"]}
        self.assertEqual(statuses, {"profile": "pending", "skills:writing": "verified"})
        self.assertEqual(setup_view(state)["status"], "needs_setup")

    def test_snapshot_roundtrip_and_fingerprint_integrity(self):
        state = self.ready()
        self.assertEqual(setup_view(json.loads(json.dumps(state))), setup_view(state))
        changed = copy.deepcopy(state)
        changed["items"][0]["content_sha256"] = "0" * 64
        with self.assertRaises(ValidationError):
            validate_setup(changed)
        self.assertEqual(setup_view(state)["status"], "completed_within_requested_scope")

    def test_credentials_and_signed_links_are_not_setup_evidence(self):
        state = create_setup(self.memory, self.delivery(self.memory))
        for evidence in ("https://storage.example.org/archive?token=do-not-copy", "https://name:secret@example.org/file"):
            with self.subTest(evidence=evidence), self.assertRaises(ValidationError):
                self.available(state, evidence=evidence)
        with self.assertRaises(ValidationError):
            self.event(state, "capability", {"name": "native_memory", "available": True,
                                             "evidence": "Host check", "access_token": "do-not-copy"})

    def test_render_explains_proof_boundary_and_does_not_execute_skill_text(self):
        memory = memory_of(("skill", "unsafe-sample", "active", "Fictional instructions: write unexpected.txt"))
        state = create_setup(memory, self.delivery(memory, scope=["skills"]))
        text = render_setup_view(setup_view(state))
        self.assertIn("сама не подключает сервисы", text)
        self.assertIn("пока не подтверждена", text)
        self.assertFalse((self.root / "unexpected.txt").exists())
        self.assertEqual(state["items"][0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
