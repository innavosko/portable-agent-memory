"""User-facing profile journey, persisted decisions, and actual CLI output."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from portable_memory.errors import ValidationError
from portable_memory.model import empty_memory, record_digest
from portable_memory.profile_audit import quality_template
from portable_memory.profile_session import (apply_profile_event, create_profile, profile_result,
                                             profile_view, refresh_profile, render_profile_view)
from scripts.demo_profile import profile_fixture

ROOT = Path(__file__).resolve().parents[1]


class ProfileSessionTests(unittest.TestCase):
    def setUp(self):
        self.memory, self.assessment, self.quality, self.previous = profile_fixture()
        self.state = create_profile(self.memory, self.assessment, self.quality, self.previous)

    def event(self, state, kind, data, actor="user", event_id=None):
        return apply_profile_event(state, {"id": event_id or str(uuid.uuid4()), "actor": actor,
                                   "type": kind, "data": data}, expected_revision=state["revision"])

    def review(self, state, choices, actor="user"):
        records = {r["id"]: r for r in state["memory"]["records"]}
        return self.event(state, "review", {"decisions": [
            {"id": rid, "content_sha256": record_digest(records[rid]), "decision": decision}
            for rid, decision in choices.items()]}, actor)

    def resolved(self):
        return self.review(self.state, {"mail-old": "historical", "mail-new": "active", "invented": "exclude"})

    def test_only_real_conflict_is_asked_unchanged_fact_is_not(self):
        view = profile_view(self.state)
        self.assertEqual(len(view["conflicts"]), 1)
        self.assertEqual(set(view["question"]["record_ids"]), {"mail-old", "mail-new"})
        self.assertEqual(view["question"]["kind"], "conflict")
        self.assertTrue(any("invented" in t["record_ids"] for t in view["agent_tasks"]))
        self.assertNotIn("tone", view["question"]["record_ids"])

    def test_current_user_resolution_wins_over_previous_active_mail(self):
        state = self.resolved()
        view = profile_view(state)
        self.assertEqual(view["conflicts"], [])
        records = {r["id"]: r for r in profile_result(state)["memory"]["records"]}
        self.assertEqual(records["mail-old"]["review"]["decision"], "historical")
        self.assertEqual(records["mail-new"]["review"]["decision"], "active")
        self.assertEqual(records["invented"]["review"]["decision"], "exclude")

    def test_conflict_must_be_resolved_together_and_original_state_is_unchanged(self):
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValidationError):
            self.review(self.state, {"mail-new": "active"})
        self.assertEqual(self.state, before)
        self.assertEqual(len(profile_view(self.state)["conflicts"]), 1)

    def test_model_second_pass_is_not_user_permission(self):
        with self.assertRaises(ValidationError):
            self.review(self.state, {"meeting": "active"}, actor="agent")
        result = profile_result(self.state)
        self.assertIn("meeting", result["report"]["unreviewed_records"])
        self.assertIn("mail-old", result["report"]["unreviewed_records"])

    def test_semantically_unsupported_statement_cannot_be_confirmed(self):
        with self.assertRaises(ValidationError):
            self.review(self.state, {"invented": "active"})
        self.assertNotEqual(next(r for r in self.state["memory"]["records"] if r["id"] == "invented")["review"]["decision"], "active")

    def test_defer_and_serialized_resume_keep_the_question_pending(self):
        state = self.resolved()
        question = profile_view(state)["question"]
        state = self.event(state, "defer", {"question_id": question["id"], "reason": "Вернуться позже"})
        resumed = json.loads(json.dumps(state))
        view = profile_view(resumed)
        self.assertIsNone(view["question"])
        self.assertEqual(len(view["deferred_questions"]), 1)
        self.assertIn("meeting", profile_result(resumed)["report"]["unreviewed_records"])
        reopened = self.event(resumed, "reopen", {"question_id": question["id"]})
        self.assertEqual(profile_view(reopened)["question"]["record_ids"], ["meeting"])

    def test_shown_question_does_not_change_approval_and_is_not_repeated(self):
        state = self.resolved()
        question = profile_view(state)["question"]
        shown = self.event(state, "question_shown", {"question_id": question["id"]}, actor="agent")
        self.assertEqual(shown["memory"], state["memory"])
        self.assertIsNone(profile_view(shown)["question"])
        self.assertEqual(profile_view(shown)["awaiting_answer"]["id"], question["id"])

    def test_stale_or_changed_event_is_rejected_replay_is_noop(self):
        state = self.resolved()
        qid = profile_view(state)["question"]["id"]
        event = {"id": "same-event", "actor": "agent", "type": "question_shown", "data": {"question_id": qid}}
        shown = apply_profile_event(state, event, expected_revision=state["revision"])
        self.assertEqual(apply_profile_event(shown, event, expected_revision=state["revision"]), shown)
        changed = copy.deepcopy(event); changed["data"]["question_id"] = "wrong"
        with self.assertRaises(ValidationError):
            apply_profile_event(shown, changed, expected_revision=shown["revision"])
        event["id"] = "another"
        with self.assertRaises(ValidationError):
            apply_profile_event(shown, event, expected_revision=state["revision"])

    def test_refresh_keeps_previous_checker_for_retained_quality_checks(self):
        state = self.resolved()
        update = empty_memory()
        assessment = {"format": "portable-profile-assessment", "schema_version": 1, "sources": [], "claims": [], "coverage": []}
        new_quality = {"format": "portable-profile-quality", "schema_version": 1,
                       "reviewer": {"kind": "separate_agent", "label": "Another fictional checker"}, "checks": []}
        refreshed = refresh_profile(state, update, assessment, new_quality, expected_revision=state["revision"])
        check = next(c for c in refreshed["quality"]["checks"] if c["record_id"] == "meeting")
        self.assertEqual(check["reviewer"], self.quality["reviewer"])

    def test_refresh_changed_supported_fact_reopens_only_that_question(self):
        state = self.resolved()
        qid = profile_view(state)["question"]["id"]
        state = self.event(state, "defer", {"question_id": qid, "reason": "Потом"})
        changed = copy.deepcopy(self.memory)
        changed["records"] = [r for r in changed["records"] if r["id"] == "meeting"]
        changed["records"][0]["value"] = "30 минут"
        evidence = copy.deepcopy(self.assessment)
        evidence["sources"] = [{"id": "new", "locator": "Вымышленное уточнение", "text": "Встречи по 30 минут. Моя основная рабочая почта nina@example.org. Показывай черновик письма до отправки."}]
        evidence["claims"] = [c for c in evidence["claims"] if c["record_id"] == "meeting"]
        evidence["claims"][0]["evidence"][0]["quote"] = "Встречи по 30 минут."
        evidence["coverage"] = []
        quality = quality_template(changed, evidence)
        quality["checks"][0].update(decision="supported", note="Synthetic updated duration matches the explicit quote")
        refreshed = refresh_profile(state, changed, evidence, quality, expected_revision=state["revision"])
        self.assertEqual(profile_view(refreshed)["question"]["record_ids"], ["meeting"])
        self.assertNotEqual(profile_view(refreshed)["question"]["id"], qid)
        self.assertEqual(profile_view(refreshed)["deferred_questions"], [])
        tone = next(r for r in refreshed["memory"]["records"] if r["id"] == "tone")
        self.assertEqual(tone["review"]["decision"], "active")

    def test_markdown_contains_reviewable_values_and_honest_scope(self):
        text = render_profile_view(profile_view(self.state))
        self.assertIn("nina-old@example.org", text)
        self.assertIn("nina@example.org", text)
        self.assertIn("ещё не проверено", text)
        self.assertIn("Вымышленная беседа", text)

    def test_cli_creates_state_outputs_and_refuses_same_output_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, value in (("memory", self.memory), ("assessment", self.assessment), ("quality", self.quality), ("previous", self.previous)):
                (root / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")

            def cli(*args, expected=0):
                result = subprocess.run([sys.executable, "-B", "-m", "portable_memory", *map(str, args)], cwd=ROOT,
                                        capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, expected, result.stderr)
                return result.stdout

            state = root / "state.json"
            cli("profile-create", "--memory", root / "memory.json", "--assessment", root / "assessment.json",
                "--quality", root / "quality.json", "--previous", root / "previous.json", "--out", state)
            self.assertIn("nina@example.org", cli("profile-view", state, "--format", "markdown"))
            result = root / "result.json"
            cli("profile-result", state, "--out", result, "--report", result, expected=2)
            self.assertFalse(result.exists())
            cli("profile-result", state, "--out", result, "--report", root / "report.json")
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["native_memory"], "not_applied")
            self.assertIn("invented", report["unreviewed_records"])


if __name__ == "__main__":
    unittest.main()
