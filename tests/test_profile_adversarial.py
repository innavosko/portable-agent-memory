"""Independent profile-flow regressions using the public preparation/review APIs."""

import copy
import unittest

from portable_memory.errors import ValidationError
from portable_memory.model import confirm, empty_memory, record_digest
from portable_memory.profile_audit import quality_template
from portable_memory.profile_session import (
    apply_profile_event, create_profile, profile_result, profile_view, refresh_profile,
)


def fixture(rid="language", value="Russian", category="communication", predicate="language"):
    source_id = "source-" + rid
    quote = f"For this fictional example, {predicate} is {value}."
    memory = empty_memory()
    memory["records"] = [{
        "id": rid, "kind": "preference", "title": predicate, "value": value,
        "scope": "personal", "provenance": "user_statement", "currency": "unknown",
        "sources": [{"id": source_id, "locator": "Fictional excerpt " + rid, "observed_at": None}],
        "review": {"decision": "pending"}, "links": [],
    }]
    assessment = {
        "format": "portable-profile-assessment", "schema_version": 1,
        "sources": [{"id": source_id, "locator": "Fictional excerpt " + rid, "text": quote}],
        "claims": [{"record_id": rid, "category": category, "subject": "Fictional user",
                    "predicate": predicate, "cardinality": "single",
                    "evidence": [{"source_id": source_id, "quote": quote}]}],
        "coverage": [{"category": category, "status": "checked", "source_ids": [source_id],
                      "note": "Only this fictional excerpt was reviewed."}],
    }
    quality = quality_template(memory, assessment)
    quality["reviewer"] = {"kind": "separate_agent", "label": "Fictional independent reviewer"}
    for check in quality["checks"]:
        check.update(decision="supported", note="The fictional statement supports the exact value and scope.")
    return memory, assessment, quality


def empty_assessment():
    return {"format": "portable-profile-assessment", "schema_version": 1,
            "sources": [], "claims": [], "coverage": []}


def event(state, kind, data, *, actor="user", event_id=None):
    return apply_profile_event(
        state,
        {"id": event_id or f"event-{state['revision'] + 1}", "actor": actor, "type": kind, "data": data},
        expected_revision=state["revision"],
    )


def decide(state, rid, decision):
    record = next(record for record in state["memory"]["records"] if record["id"] == rid)
    return event(state, "review", {"decisions": [{"id": rid, "decision": decision,
                                                  "content_sha256": record_digest(record)}]})


class ProfileAdversarialTests(unittest.TestCase):
    def test_new_extraction_cannot_bring_its_own_valid_active_confirmation(self):
        memory, assessment, quality = fixture()
        preapproved = confirm(memory, {"decisions": [{"id": "language", "decision": "active",
                                                     "content_sha256": record_digest(memory["records"][0])}]})
        state = create_profile(preapproved, assessment, quality)
        self.assertEqual(state["memory"]["records"][0]["review"]["decision"], "pending")
        result = profile_result(state)
        self.assertEqual(result["report"]["unreviewed_records"], ["language"])
        self.assertEqual(result["report"]["native_memory"], "not_applied")

    def test_literal_quote_without_semantic_check_is_not_ready(self):
        memory, assessment, _ = fixture("mail", "colleague@example.org", "mailboxes", "owner_email")
        # The substring exists, but its context says that the address is somebody else's.
        assessment["sources"][0]["text"] = "This describes a colleague, not the user: " + assessment["sources"][0]["text"]
        state = create_profile(memory, assessment)
        record = profile_view(state)["records"][0]
        self.assertEqual(record["evidence_status"], "verified")
        self.assertFalse(record["quality_ready"])
        self.assertIsNone(profile_view(state)["question"])
        with self.assertRaises(ValidationError):
            decide(state, "mail", "active")

    def test_supported_quality_does_not_replace_the_user_decision(self):
        memory, assessment, quality = fixture()
        state = create_profile(memory, assessment, quality)
        view = profile_view(state)
        self.assertTrue(view["records"][0]["quality_ready"])
        self.assertEqual(view["records"][0]["decision"], "pending")
        with self.assertRaises(ValidationError):
            event(state, "review", {"decisions": [{"id": "language", "decision": "active",
                                                     "content_sha256": record_digest(state["memory"]["records"][0])}]}, actor="agent")
        self.assertEqual(profile_result(state)["memory"]["records"][0]["review"]["decision"], "pending")

    def test_quality_becomes_stale_when_claim_value_changes(self):
        memory, assessment, quality = fixture()
        state = create_profile(memory, assessment, quality)
        changed = copy.deepcopy(memory)
        changed["records"][0]["value"] = "English"
        refreshed = refresh_profile(state, changed, empty_assessment(), expected_revision=0)
        record = profile_view(refreshed)["records"][0]
        self.assertEqual(record["quality_status"], "stale")
        self.assertFalse(record["quality_ready"])
        with self.assertRaises(ValidationError):
            decide(refreshed, "language", "active")

    def test_quality_becomes_stale_when_surrounding_source_context_changes(self):
        memory, assessment, quality = fixture()
        state = create_profile(memory, assessment, quality)
        incoming = empty_assessment()
        incoming["sources"] = copy.deepcopy(assessment["sources"])
        incoming["sources"][0]["text"] += " Correction: this quoted example belongs to someone else."
        refreshed = refresh_profile(state, empty_memory(), incoming, expected_revision=0)
        record = profile_view(refreshed)["records"][0]
        self.assertEqual(record["evidence_status"], "verified")
        self.assertEqual(record["quality_status"], "stale")
        self.assertFalse(record["quality_ready"])

    def test_partial_refresh_retains_prior_confirmed_record_and_its_evidence(self):
        memory, assessment, quality = fixture()
        state = decide(create_profile(memory, assessment, quality), "language", "active")
        old_record = copy.deepcopy(state["memory"]["records"][0])
        incoming, new_assessment, new_quality = fixture("meeting", "25 minutes", "calendar", "duration")
        refreshed = refresh_profile(state, incoming, new_assessment, new_quality, expected_revision=state["revision"])
        by_id = {record["id"]: record for record in refreshed["memory"]["records"]}
        self.assertEqual(by_id["language"], old_record)
        self.assertEqual(by_id["meeting"]["review"]["decision"], "pending")
        self.assertEqual({source["id"] for source in refreshed["assessment"]["sources"]}, {"source-language", "source-meeting"})
        self.assertEqual(profile_view(refreshed)["question"]["record_ids"], ["meeting"])

    def test_user_historical_choice_survives_prior_active_and_new_extraction(self):
        memory, assessment, quality = fixture()
        previous = confirm(memory, {"decisions": [{"id": "language", "decision": "active",
                                                  "content_sha256": record_digest(memory["records"][0])}]})
        state = create_profile(memory, assessment, quality, previous=previous)
        state = decide(state, "language", "historical")
        self.assertEqual(profile_view(state)["records"][0]["decision"], "historical")
        refreshed = refresh_profile(state, previous, assessment, quality, expected_revision=state["revision"])
        record = refreshed["memory"]["records"][0]
        self.assertEqual(record["review"]["decision"], "historical")
        self.assertEqual(record["currency"], "historical")
        self.assertIsNone(profile_view(refreshed)["question"])

    def test_explicit_pending_decision_does_not_restore_old_active_on_audit(self):
        memory, assessment, quality = fixture()
        previous = confirm(memory, {"decisions": [{"id": "language", "decision": "active",
                                                  "content_sha256": record_digest(memory["records"][0])}]})
        state = create_profile(memory, assessment, quality, previous=previous)
        state = decide(state, "language", "pending")
        self.assertEqual(profile_view(state)["records"][0]["decision"], "pending")
        self.assertEqual(profile_result(state)["memory"]["records"][0]["review"]["decision"], "pending")

    def test_defer_is_preserved_when_refresh_does_not_change_question(self):
        memory, assessment, quality = fixture()
        state = create_profile(memory, assessment, quality)
        question = profile_view(state)["question"]
        state = event(state, "defer", {"question_id": question["id"], "reason": "User will review this later"})
        refreshed = refresh_profile(state, empty_memory(), empty_assessment(), expected_revision=state["revision"])
        view = profile_view(refreshed)
        self.assertIsNone(view["question"])
        self.assertEqual([item["id"] for item in view["deferred_questions"]], [question["id"]])

    def test_new_fact_in_same_category_does_not_reask_unchanged_deferred_fact(self):
        memory, assessment, quality = fixture()
        state = create_profile(memory, assessment, quality)
        question = profile_view(state)["question"]
        state = event(state, "defer", {"question_id": question["id"], "reason": "Keep the language question for later"})
        incoming, new_assessment, new_quality = fixture("tone", "concise", "communication", "tone")
        refreshed = refresh_profile(state, incoming, new_assessment, new_quality, expected_revision=state["revision"])
        view = profile_view(refreshed)
        self.assertEqual(view["question"]["record_ids"], ["tone"])
        deferred_ids = {rid for question in view["deferred_questions"] for rid in question["record_ids"]}
        self.assertIn("language", deferred_ids)

    def test_new_fact_does_not_repeat_a_question_already_shown(self):
        memory, assessment, quality = fixture()
        state = create_profile(memory, assessment, quality)
        question = profile_view(state)["question"]
        state = event(state, "question_shown", {"question_id": question["id"]}, actor="agent")
        incoming, new_assessment, new_quality = fixture("tone", "concise", "communication", "tone")
        refreshed = refresh_profile(state, incoming, new_assessment, new_quality, expected_revision=state["revision"])
        view = profile_view(refreshed)
        self.assertIsNone(view["question"])
        self.assertIsNotNone(view["awaiting_answer"])
        self.assertEqual(view["awaiting_answer"]["record_ids"], ["language"])

    def test_agent_cannot_defer_user_review_or_apply_stale_revision(self):
        memory, assessment, quality = fixture()
        state = create_profile(memory, assessment, quality)
        question = profile_view(state)["question"]
        with self.assertRaises(ValidationError):
            event(state, "defer", {"question_id": question["id"], "reason": "Model wants fewer questions"}, actor="agent")
        shown = event(state, "question_shown", {"question_id": question["id"]}, actor="agent")
        with self.assertRaises(ValidationError):
            refresh_profile(shown, empty_memory(), empty_assessment(), expected_revision=0)

    def test_explicit_category_exclusion_excludes_existing_records_from_result(self):
        memory, assessment, quality = fixture()
        state = create_profile(memory, assessment, quality)
        state = event(state, "coverage", {"category": "communication", "status": "excluded",
                                          "source_ids": ["source-language"],
                                          "note": "User chooses not to transfer communication preferences."})
        result = profile_result(state)
        self.assertEqual(result["memory"]["records"][0]["review"]["decision"], "exclude")
        self.assertFalse(any(question.get("category") == "communication" for question in
                             [profile_view(state)["question"], profile_view(state)["awaiting_answer"]] if question))

    def test_agent_refresh_cannot_overwrite_user_excluded_category(self):
        memory, assessment, quality = fixture()
        state = create_profile(memory, assessment, quality)
        state = event(state, "coverage", {"category": "communication", "status": "excluded",
                                          "source_ids": ["source-language"],
                                          "note": "User excluded this category from the transfer."})
        incoming, new_assessment, new_quality = fixture("tone", "concise", "communication", "tone")
        refreshed = refresh_profile(state, incoming, new_assessment, new_quality, expected_revision=state["revision"])
        category = next(item for item in profile_view(refreshed)["categories"] if item["id"] == "communication")
        self.assertEqual(category["status"], "excluded")
        result = profile_result(refreshed)
        self.assertEqual({record["id"]: record["review"]["decision"] for record in result["memory"]["records"]},
                         {"language": "exclude", "tone": "exclude"})
        self.assertFalse(profile_view(refreshed)["question"] and
                         profile_view(refreshed)["question"]["category"] == "communication")

    def test_excluded_category_requires_user_reopening_before_activation(self):
        memory, assessment, quality = fixture()
        state = create_profile(memory, assessment, quality)
        state = event(state, "coverage", {"category": "communication", "status": "excluded",
                                          "source_ids": ["source-language"],
                                          "note": "User excluded communication preferences."})
        for decision in ("active", "historical"):
            with self.subTest(decision=decision), self.assertRaises(ValidationError):
                decide(state, "language", decision)
        reopen = {"category": "communication", "status": "checked",
                  "source_ids": ["source-language"], "note": "User explicitly reopens this category."}
        with self.assertRaises(ValidationError):
            event(state, "coverage", reopen, actor="agent")
        state = event(state, "coverage", reopen)
        # Reopening only permits a new decision; it does not approve the fact.
        self.assertNotEqual(state["memory"]["records"][0]["review"]["decision"], "active")
        state = decide(state, "language", "active")
        self.assertEqual(profile_result(state)["memory"]["records"][0]["review"]["decision"], "active")

    def test_refresh_does_not_assign_a_new_reviewer_to_unattributed_old_check(self):
        memory, assessment, quality = fixture()
        del quality["reviewer"]
        state = create_profile(memory, assessment, quality)
        self.assertIsNone(profile_view(state)["records"][0]["quality_reviewer"])
        incoming, new_assessment, new_quality = fixture("meeting", "25 minutes", "calendar", "duration")
        new_quality["reviewer"] = {"kind": "separate_agent", "label": "Reviewer of the new calendar excerpt only"}
        refreshed = refresh_profile(state, incoming, new_assessment, new_quality, expected_revision=state["revision"])
        records = {record["id"]: record for record in profile_view(refreshed)["records"]}
        self.assertIsNone(records["language"]["quality_reviewer"])
        self.assertEqual(records["meeting"]["quality_reviewer"], new_quality["reviewer"])

    def test_new_evidence_does_not_silently_erase_user_not_applicable_choice(self):
        memory, assessment, quality = fixture()
        state = create_profile(memory, assessment, quality)
        state = decide(state, "language", "active")
        state = event(state, "coverage", {"category": "calendar", "status": "not_applicable",
                                          "source_ids": [], "note": "User says calendar setup is not applicable."})
        incoming, new_assessment, new_quality = fixture("meeting", "25 minutes", "calendar", "duration")
        refreshed = refresh_profile(state, incoming, new_assessment, new_quality, expected_revision=state["revision"])
        view = profile_view(refreshed)
        category = next(item for item in view["categories"] if item["id"] == "calendar")
        self.assertEqual(category["status"], "not_applicable")
        record = next(item for item in profile_result(refreshed)["memory"]["records"] if item["id"] == "meeting")
        self.assertEqual(record["review"]["decision"], "pending")
        self.assertIsNotNone(view["question"])
        self.assertEqual(view["question"]["category"], "calendar")

    def test_unsupported_new_candidate_does_not_revoke_prior_user_fact(self):
        memory, assessment, _ = fixture()
        previous = confirm(memory, {"decisions": [{"id": "language", "decision": "active",
                                                  "content_sha256": record_digest(memory["records"][0])}]})
        incoming, candidate_assessment, _ = fixture("language-alt", "English", "communication", "language")
        merged_assessment = copy.deepcopy(assessment)
        merged_assessment["sources"].extend(candidate_assessment["sources"])
        merged_assessment["claims"].extend(candidate_assessment["claims"])
        state = create_profile(incoming, merged_assessment, previous=previous)
        result = profile_result(state)
        decisions = {record["id"]: record["review"]["decision"] for record in result["memory"]["records"]}
        self.assertEqual(decisions, {"language": "active", "language-alt": "pending"})
        self.assertEqual(profile_view(state)["export_blocked_records"], [])


if __name__ == "__main__":
    unittest.main()
