"""Independent state/coordination tests for the resumable migration journey."""
import copy
import json
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from portable_memory.errors import ValidationError
from portable_memory.collection import collect_job
from portable_memory.migration import (MigrationStore, apply_migration_event,
                                       create_migration, migration_view,
                                       validate_migration)


def dataset():
    return {"namespace": "openai", "account_id": "synthetic-account",
            "workspace_id": "personal", "resource_id": "history"}


def observed(oid, *, identity=True, path="history.zip", interface="web", selected=True):
    return {"id": oid, "label": "History", "kind": "history",
            "identity": dataset() if identity else None,
            "identity_evidence": "User confirmed account and workspace IDs" if identity else None,
            "surface": {"agent_id": "chatgpt-" + interface, "product": "ChatGPT", "interface": interface},
            "selected": selected,
            "input": {"kind": "archive", "path": path} if path else None}


def state_for(*sources, answers=None):
    return create_migration({"coordinator": "coordinator", "actors": ["coordinator", "worker"],
                             "known_answers": {"scope": ["history"]} if answers is None else answers,
                             "sources": list(sources)})


def event(kind, data, actor="coordinator"):
    return {"id": str(uuid.uuid4()), "actor": actor, "type": kind, "data": data}


def advance(state, kind, data, actor="coordinator"):
    return apply_migration_event(state, event(kind, data, actor), expected_revision=state["revision"])


class MigrationAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "history.json"
        self.archive.write_text(json.dumps([{"id": "synthetic-conversation", "title": "Synthetic",
                                           "current_node": "user-message", "mapping": {
                                               "user-message": {"parent": None, "message": {
                                                   "id": "user-message", "author": {"role": "user"},
                                                   "content": {"parts": ["Synthetic test content only"]}}}}}]), encoding="utf-8")

    def real_observed(self, oid="one", *, interface="web"):
        return observed(oid, path=str(self.archive), interface=interface)

    def collected(self, claim):
        return {"job_id": claim["id"], "ok": True,
                "result": collect_job(claim, self.root / "collected")}

    def test_duplicate_dataset_across_surfaces_has_only_one_job(self):
        state = state_for(observed("web"), observed("desktop", interface="desktop"))
        self.assertEqual(len(state["sources"]), 1)
        self.assertEqual(len(state["jobs"]), 1)
        self.assertEqual(len(state["sources"][0]["surfaces"]), 2)
        validate_migration(state)

    def test_reselecting_unresolved_source_still_requires_identity(self):
        state = state_for(observed("unknown", identity=False))
        source_id = state["sources"][0]["id"]
        state = advance(state, "select_source", {"source_id": source_id, "selected": False})
        state = advance(state, "select_source", {"source_id": source_id, "selected": True})
        self.assertEqual(state["jobs"][0]["status"], "needs_identity")
        self.assertEqual(migration_view(state)["automatic_actions"], [])

    def test_new_competing_input_requires_choice_when_none_was_made(self):
        state = state_for(observed("first", path="first.zip"))
        state = advance(state, "register_sources", {"observations": [observed("second", path="second.zip", interface="desktop")]})
        self.assertEqual(state["sources"][0]["input_status"], "choice_required")
        self.assertEqual(state["jobs"][0]["status"], "needs_choice")
        self.assertEqual(migration_view(state)["automatic_actions"], [])

    def test_explicit_input_choice_survives_defer_deselect_reselect_and_retry(self):
        answers = {"scope": ["history"], "destination": {"kind": "folder", "label": "Chosen folder"}, "delivery": "as_ready"}
        state = state_for(observed("first", path="first.zip"), observed("second", path="second.zip"), answers=answers)
        source_id = state["sources"][0]["id"]
        state = advance(state, "choose_input", {"source_id": source_id, "index": 1})
        chosen = copy.deepcopy(state["jobs"][0]["input"])
        job_id = state["jobs"][0]["id"]
        state = advance(state, "defer", {"job_id": job_id, "reason": "Client asked to do this later"})
        state = advance(state, "select_source", {"source_id": source_id, "selected": False})
        state = advance(state, "select_source", {"source_id": source_id, "selected": True})
        self.assertEqual(state["jobs"][0]["input"], chosen)
        self.assertEqual(state["answers"], answers)
        self.assertIsNone(migration_view(state)["question"])
        store = MigrationStore.create(self.root / "resume.sqlite", state)
        reopened = MigrationStore(self.root / "resume.sqlite").load()
        self.assertEqual(reopened["answers"], answers)
        self.assertEqual(reopened["jobs"][0]["input"], chosen)

    def test_resolving_identity_keeps_assigned_owner_and_explicit_deferral(self):
        state = state_for(observed("unknown", identity=False))
        job_id = state["jobs"][0]["id"]
        state = advance(state, "assign", {"job_id": job_id, "owner": "worker"})
        state = advance(state, "defer", {"job_id": job_id, "reason": "Client wants to continue tomorrow"})
        state = advance(state, "resolve_identity", {"source_id": state["sources"][0]["id"],
                                                   "identity": dataset(), "evidence": "User clarified identity"})
        self.assertEqual(state["sources"][0]["identity_status"], "verified")
        self.assertEqual(state["jobs"][0]["owner"], "worker")
        self.assertEqual(state["jobs"][0]["status"], "deferred")
        self.assertEqual(state["jobs"][0]["reason"], "Client wants to continue tomorrow")
        self.assertEqual(migration_view(state, "worker")["automatic_actions"], [])

    def test_resolving_alias_merges_into_one_known_dataset_job(self):
        state = state_for(observed("known"), observed("unknown", identity=False, interface="desktop"))
        unresolved = next(source for source in state["sources"] if source["identity_status"] == "unresolved")
        state = advance(state, "resolve_identity", {"source_id": unresolved["id"], "identity": dataset(), "evidence": "Same account and workspace confirmed"})
        self.assertEqual(len(state["sources"]), 1)
        self.assertEqual(len(state["jobs"]), 1)
        self.assertEqual(state["sources"][0]["observation_ids"], ["known", "unknown"])
        validate_migration(state)

    def test_question_display_does_not_grant_scope_or_approval(self):
        state = state_for(observed("one"), answers={})
        before = copy.deepcopy(state)
        view = migration_view(state)
        self.assertEqual(state, before)
        self.assertEqual(view["question"]["id"], "scope")
        state = advance(state, "question_shown", {"question_id": "scope"})
        self.assertEqual(state["answers"], {})
        view = migration_view(state)
        self.assertIsNone(view["question"])
        self.assertEqual(view["awaiting_answer"]["id"], "scope")
        self.assertEqual(view["automatic_actions"], [])
        store = MigrationStore.create(self.root / "no-approval.sqlite", state)
        with self.assertRaises(ValidationError):
            store.claim_ready("coordinator")

    def test_provided_folder_input_preserves_normalized_include_selection(self):
        value = observed("project", path=None)
        value["kind"] = "project"
        value["identity"] = {"namespace": "filesystem", "account_id": "owner",
                             "workspace_id": "device", "resource_id": "repo"}
        state = state_for(value)
        state = advance(state, "provide_input", {"source_id": state["sources"][0]["id"],
                                                  "evidence": "Client selected these files",
                                                  "input": {"kind": "folder", "path": "C:/selected/project",
                                                            "include": ["z.md", "assets\\result.xlsx", "a.md", "a.md"]}})
        self.assertEqual(state["jobs"][0]["status"], "ready")
        self.assertEqual(state["jobs"][0]["input"]["include"], ["a.md", "assets/result.xlsx", "z.md"])
        validate_migration(state)

    def test_answered_question_is_not_asked_on_resume(self):
        state = state_for(observed("one"), answers={})
        state = advance(state, "question_shown", {"question_id": "scope"})
        state = advance(state, "answer", {"key": "scope", "value": ["profile", "projects"]})
        store = MigrationStore.create(self.root / "answers.sqlite", state)
        view = migration_view(MigrationStore(store.path).load())
        self.assertIsNone(view["question"])
        self.assertIsNone(view["awaiting_answer"])
        self.assertEqual(store.load()["answers"]["scope"], ["profile", "projects"])

    def test_two_store_clients_cannot_claim_same_job(self):
        path = self.root / "concurrent.sqlite"
        MigrationStore.create(path, state_for(self.real_observed()))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: MigrationStore(path).claim_ready("coordinator"), range(2)))
        all_claims = [claim for result in results for claim in result]
        self.assertEqual(len(all_claims), 1)
        final = MigrationStore(path).load()
        self.assertEqual(final["jobs"][0]["attempt"], 1)
        self.assertEqual(final["jobs"][0]["status"], "running")

    def test_release_rejects_old_worker_completion_after_new_claim(self):
        store = MigrationStore.create(self.root / "release.sqlite", state_for(self.real_observed()))
        old_claim = store.claim_ready("coordinator")[0]
        state = store.load()
        store.event(event("release", {"job_id": old_claim["id"], "reason": "Previous execution has stopped"}), expected_revision=state["revision"])
        state = store.load()
        store.event(event("retry", {"job_id": old_claim["id"]}), expected_revision=state["revision"])
        new_claim = store.claim_ready("coordinator")[0]
        outcome = self.collected(new_claim)
        with self.assertRaises(ValidationError):
            store.finish(old_claim, outcome)
        self.assertEqual(store.load()["jobs"][0]["run_token"], new_claim["run_token"])
        store.finish(new_claim, outcome)
        self.assertEqual(store.load()["jobs"][0]["status"], "collected")

    def test_deselect_running_source_rolls_back_without_losing_claim(self):
        store = MigrationStore.create(self.root / "claim.sqlite", state_for(self.real_observed()))
        claim = store.claim_ready("coordinator")[0]
        before = store.load()
        with self.assertRaises(ValidationError):
            store.event(event("select_source", {"source_id": before["sources"][0]["id"], "selected": False}),
                        expected_revision=before["revision"])
        self.assertEqual(store.load(), before)
        self.assertEqual(store.load()["jobs"][0]["run_token"], claim["run_token"])

    def test_registering_duplicate_surface_does_not_recollect_completed_source(self):
        store = MigrationStore.create(self.root / "completed.sqlite", state_for(self.real_observed()))
        claim = store.claim_ready("coordinator")[0]
        store.finish(claim, self.collected(claim))
        state = store.load()
        state = store.event(event("register_sources", {"observations": [self.real_observed("desktop", interface="desktop")]}), expected_revision=state["revision"])
        self.assertEqual(len(state["jobs"]), 1)
        self.assertEqual(state["jobs"][0]["status"], "collected")
        self.assertEqual(store.claim_ready("coordinator"), [])

    def test_input_changed_after_claim_is_rejected_by_collection(self):
        store = MigrationStore.create(self.root / "changed.sqlite", state_for(self.real_observed()))
        claim = store.claim_ready("coordinator")[0]
        self.archive.write_text(self.archive.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(ValidationError):
            collect_job(claim, self.root / "collected")
        self.assertFalse((self.root / "collected" / claim["id"] / "attempt-0001").exists())
        store.finish(claim, {"job_id": claim["id"], "ok": False, "error": "Claimed snapshot changed"})
        self.assertEqual(store.load()["jobs"][0]["status"], "failed")

    def test_finish_rejects_wrong_job_incomplete_output_and_wrong_snapshot(self):
        store = MigrationStore.create(self.root / "verification.sqlite", state_for(self.real_observed()))
        claim = store.claim_ready("coordinator")[0]
        outcome = self.collected(claim)
        wrong_job = copy.deepcopy(outcome)
        wrong_job["job_id"] = "another-job"
        with self.assertRaises(ValidationError):
            store.finish(claim, wrong_job)
        marker = Path(outcome["result"]["output"]) / ".incomplete"
        marker.write_text("Simulated incomplete result", encoding="utf-8")
        with self.assertRaises(ValidationError):
            store.finish(claim, outcome)
        marker.unlink()
        wrong_snapshot = copy.deepcopy(outcome)
        wrong_snapshot["result"]["input_sha256"] = "0" * 64
        with self.assertRaises(ValidationError):
            store.finish(claim, wrong_snapshot)
        self.assertEqual(store.load()["jobs"][0]["status"], "running")
        store.finish(claim, outcome)
        self.assertEqual(store.load()["jobs"][0]["status"], "collected")

    def test_identity_refinement_via_registration_preserves_owner_and_deferral(self):
        state = state_for(observed("same-observation", identity=False))
        job_id = state["jobs"][0]["id"]
        state = advance(state, "assign", {"job_id": job_id, "owner": "worker"})
        state = advance(state, "defer", {"job_id": job_id, "reason": "Client postponed this source"})
        state = advance(state, "register_sources", {"observations": [observed("same-observation", identity=True)]})
        self.assertEqual(state["sources"][0]["identity_status"], "verified")
        self.assertEqual(state["jobs"][0]["owner"], "worker")
        self.assertEqual(state["jobs"][0]["status"], "deferred")
        self.assertEqual(state["jobs"][0]["reason"], "Client postponed this source")
        self.assertEqual(migration_view(state, "worker")["automatic_actions"], [])

    def test_retry_does_not_bypass_a_new_input_choice_while_deferred(self):
        state = state_for(observed("first", path="first.zip"))
        job_id = state["jobs"][0]["id"]
        state = advance(state, "defer", {"job_id": job_id, "reason": "Later"})
        state = advance(state, "register_sources", {"observations": [observed("second", path="second.zip")]})
        self.assertEqual(state["sources"][0]["input_status"], "choice_required")
        state = advance(state, "retry", {"job_id": job_id})
        self.assertEqual(state["jobs"][0]["status"], "needs_choice")
        self.assertEqual(migration_view(state)["automatic_actions"], [])

    def test_provide_input_uses_confirmed_identity_not_an_old_unresolved_observation(self):
        unverified = observed("same-observation", identity=False, path=None)
        verified = observed("same-observation", identity=True, path=None)
        state = state_for(unverified, verified)
        self.assertEqual(len(state["sources"]), 1)
        self.assertEqual(state["sources"][0]["identity_status"], "verified")
        state = advance(state, "provide_input", {"source_id": state["sources"][0]["id"],
                                                  "evidence": "User provided the requested archive",
                                                  "input": {"kind": "archive", "path": str(self.archive)}})
        self.assertEqual(len(state["sources"]), 1)
        self.assertEqual(len(state["jobs"]), 1)
        self.assertEqual(state["jobs"][0]["status"], "ready")
        self.assertEqual(state["jobs"][0]["input"]["path"], str(self.archive))

    def test_new_surface_preserves_deselection_until_explicit_reselection(self):
        state = state_for(self.real_observed("web"))
        source_id = state["sources"][0]["id"]
        state = advance(state, "select_source", {"source_id": source_id, "selected": False})
        state = advance(state, "register_sources", {"observations": [self.real_observed("desktop", interface="desktop")]})
        self.assertEqual(len(state["sources"]), 1)
        self.assertFalse(state["sources"][0]["selected"])
        self.assertEqual(state["sources"][0]["observation_ids"], ["desktop", "web"])
        self.assertEqual(state["jobs"][0]["status"], "deferred")
        self.assertEqual(migration_view(state)["automatic_actions"], [])
        state = advance(state, "select_source", {"source_id": source_id, "selected": True})
        self.assertTrue(state["sources"][0]["selected"])
        self.assertEqual(state["jobs"][0]["status"], "ready")
        self.assertEqual(migration_view(state)["automatic_actions"][0]["job_ids"], [state["jobs"][0]["id"]])


if __name__ == "__main__":
    unittest.main()
