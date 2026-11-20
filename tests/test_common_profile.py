"""Aggregate-profile behavior with explicit fictional model and user decisions."""
import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from portable_memory.common_profile import (CommonStore, add_source, common_event, common_input,
    common_result, common_template, common_view, create_common, review_common, validate_export_review)
from portable_memory.errors import ValidationError
from portable_memory.model import confirm, digest, empty_memory, record_digest, render_profile, review_template
from portable_memory.package import export_package, read_package
from portable_memory.profile_audit import CATEGORIES, quality_template
from portable_memory.semantic_review import semantic_quality_template


COORDINATOR = "fictional-coordinator"


def memory_of(*records):
    memory = empty_memory()
    memory["records"] = list(records)
    return memory


def record(rid, text="Краткие ответы", source="codex-work", scope="global"):
    return {"id": rid, "kind": "preference", "title": rid, "scope": scope, "value": text,
            "sources": [{"id": "quote-" + rid, "locator": "synthetic:" + source}],
            "provenance": "user_statement", "currency": "unknown", "links": [], "review": {"decision": "pending"}}


def confirmed(memory, decision="active"):
    choices = review_template(memory)
    for item in choices["decisions"]:
        item["decision"] = decision
    return confirm(memory, choices)


def assessment_and_quality(memory, predicate="response_style", predicates=None):
    """Actual literal checks, with clearly fictional second-pass attestations."""
    sources, claims = [], []
    for item in memory["records"]:
        source = item["sources"][0]
        text = item["value"] if isinstance(item["value"], str) else json.dumps(item["value"], ensure_ascii=False)
        sources.append({"id": source["id"], "locator": source["locator"], "text": text})
        claims.append({"record_id": item["id"], "category": "communication", "subject": "user",
                       "predicate": (predicates or {}).get(item["id"], predicate), "cardinality": "single", "evidence": [{"source_id": source["id"], "quote": text}]})
    assessment = {"format": "portable-profile-assessment", "schema_version": 1, "sources": sources, "claims": claims,
                  "coverage": [{"category": cid, "status": "checked" if cid == "communication" else "not_applicable",
                                "source_ids": [s["id"] for s in sources] if cid == "communication" else [],
                                "note": "Fictional fixture: scope and evidence explicitly supplied."} for cid in CATEGORIES]}
    quality = quality_template(memory, assessment)
    quality["reviewer"] = {"kind": "separate_agent", "label": "Fictional literal-and-meaning checker"}
    for check in quality["checks"]:
        check.update(decision="supported", note="Fictional second pass supports this exact supplied quote and scope.")
    return assessment, quality


def reviewed(state, relation="compatible", canonical=None, predicates=None):
    common = common_input(state)
    semantic = common_template(state)
    semantic["reviewer"] = {"kind": "separate_agent", "label": "Fictional whole-profile semantic reviewer"}
    semantic["cross_source_checked"] = True
    ids = semantic["covered_record_ids"]
    semantic["groups"] = [] if not ids else [{"id": "all-records", "relation": relation, "record_ids": ids,
        "canonical_id": (canonical or ids[0]) if relation == "equivalent" else None,
        "note": "Explicit fictional cross-source judgment; no model ran inside the Python test."}]
    semantic_quality = semantic_quality_template(semantic)
    semantic_quality.update(reviewer={"kind": "same_model_second_pass", "label": "Fictional second global pass"},
                            decision="supported", note="Fictional second pass checks coverage, relation and scope.")
    assessment, quality = assessment_and_quality(common["memory"], predicates=predicates)
    return review_common(state, assessment, quality, semantic, semantic_quality,
                         expected_revision=state["revision"], actor=COORDINATOR)


def provided(state, sid, memory):
    return add_source(state, {"id": sid, "label": sid, "status": "provided", "note": "Fictional explicitly supplied source."}, memory,
                      expected_revision=state["revision"], actor=COORDINATOR)


def user_decisions(state, choices, event_id="actual-user-decision"):
    records = {item["id"]: item for item in state["memory"]["records"]}
    event = {"id": event_id, "actor": "user", "type": "review", "data": {"decisions": [
        {"id": rid, "content_sha256": record_digest(records[rid]), "decision": decision} for rid, decision in choices.items()]}}
    return common_event(state, event, expected_revision=state["revision"], actor=COORDINATOR)


class CommonProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def prior_state(self, **kwargs):
        return create_common(COORDINATOR, previous=confirmed(memory_of(record("canonical"))), **kwargs)

    def test_three_sources_late_paraphrases_inherit_actual_confirmation_without_new_question(self):
        state = reviewed(self.prior_state())
        original_confirmation = copy.deepcopy(state["memory"]["records"][0]["review"])
        state = provided(state, "personal-chatgpt-web", memory_of(record("personal-wording", "Без длинных вступлений", "chatgpt-personal")))
        self.assertEqual(common_view(state)["status"], "needs_common_review")
        state = reviewed(state, "equivalent", "canonical")
        self.assertIsNone(common_view(state)["question"])
        state = provided(state, "claude-code", memory_of(record("claude-wording", "Коротко, с необходимыми деталями", "claude-code")))
        state = reviewed(state, "equivalent", "canonical")
        result = common_result(state)
        self.assertEqual(len(result["review"]["sources"]), 3)
        self.assertEqual(result["memory"]["metadata"]["common_profile"]["aliases"],
                         {"personal-wording": "canonical", "claude-wording": "canonical"})
        records = {r["id"]: r for r in result["memory"]["records"]}
        self.assertEqual(records["canonical"]["review"], original_confirmation)
        for rid in ("personal-wording", "claude-wording"):
            self.assertEqual(records[rid]["review"]["decision"], "active")
            self.assertEqual(records[rid]["review"]["inherited_from"], "canonical")
        self.assertTrue(common_view(state)["export_allowed"])
        self.assertIsNone(common_view(state)["question"])
        displayed = render_profile(result["memory"], active_only=True)
        self.assertEqual(displayed.count("## canonical (canonical)"), 1)
        self.assertNotIn("## personal-wording", displayed)
        self.assertNotIn("## claude-wording", displayed)
        for locator in ("synthetic:codex-work", "synthetic:chatgpt-personal", "synthetic:claude-code"):
            self.assertIn(locator, displayed)
        self.assertEqual(len(result["memory"]["records"]), 3)

    def test_conflict_latest_source_does_not_win_and_user_resolves_actual_currency(self):
        state = reviewed(self.prior_state())
        state = provided(state, "new-source", memory_of(record("new-rule", "Всегда развёрнутые ответы", "later-source")))
        state = reviewed(state, "conflict")
        self.assertFalse(common_view(state)["export_allowed"])
        with self.assertRaises(ValidationError):
            common_result(state)
        records = {r["id"]: r for r in state["memory"]["records"]}
        self.assertEqual(records["canonical"]["value"], "Краткие ответы")
        self.assertEqual(records["new-rule"]["review"]["decision"], "pending")
        state = user_decisions(state, {"canonical": "historical", "new-rule": "active"})
        result = common_result(state)
        states = {r["id"]: r["review"]["decision"] for r in result["memory"]["records"]}
        self.assertEqual(states, {"canonical": "historical", "new-rule": "active"})

    def test_global_conflict_with_different_titles_and_predicates_still_asks_one_focused_question(self):
        state = reviewed(self.prior_state())
        state = provided(state, "personal-source", memory_of(record("different-name", "Всегда длинные подробные объяснения", "personal")))
        state = reviewed(state, "conflict", predicates={"canonical": "reply_length", "different-name": "explanation_detail"})
        view = common_view(state)
        self.assertFalse(view["export_allowed"])
        self.assertIsNotNone(view["question"])
        self.assertEqual(view["question"]["kind"], "conflict")
        self.assertEqual({r["id"] for r in view["question"]["records"]}, {"canonical", "different-name"})
        self.assertTrue(view["question"]["id"].startswith("semantic-conflict-"))

    def test_empty_late_source_invalidates_ready_export_and_requires_new_model_pass_only(self):
        state = reviewed(self.prior_state())
        prior = common_result(state)
        state = provided(state, "later-empty-source", empty_memory())
        self.assertFalse(common_view(state)["export_allowed"])
        with self.assertRaises(ValidationError):
            common_result(state)
        state = reviewed(state)
        result = common_result(state)
        self.assertEqual(result["memory"]["records"][0]["review"], prior["memory"]["records"][0]["review"])
        self.assertIsNone(common_view(state)["question"])
        self.assertIn("later-empty-source", result["review"]["semantic"]["covered_source_ids"])

    def test_direct_export_without_aggregate_receipt_is_blocked_even_for_confirmed_memory(self):
        raw = confirmed(memory_of(record("known")))
        with self.assertRaises(ValidationError):
            export_package(raw, self.root / "unsafe.zip")
        self.assertFalse((self.root / "unsafe.zip").exists())

    def test_fresh_common_receipt_roundtrip_and_stale_payload_or_proof_rejected(self):
        result = common_result(reviewed(self.prior_state()))
        archive = self.root / "reviewed.zip"
        export_package(result["memory"], archive, review=result["review"])
        self.assertEqual(read_package(archive)["memory"], result["memory"])
        changed = copy.deepcopy(result["memory"])
        changed["records"][0]["value"] = "Edited after review"
        changed = confirmed(changed)
        with self.assertRaises(ValidationError):
            validate_export_review(changed, result["review"])
        for change in ("source", "semantic", "quality", "check"):
            proof = copy.deepcopy(result["review"])
            if change == "source": proof["sources"][0]["snapshot_sha256"] = "0" * 64
            if change == "semantic": proof["semantic"]["groups"][0]["note"] = "Modified judgment"
            if change == "quality": proof["semantic_quality"]["decision"] = "uncertain"
            if change == "check": proof["record_checks"][0]["content_sha256"] = "0" * 64
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_export_review(result["memory"], proof)

    def test_new_source_reusing_record_id_for_different_content_cannot_overwrite(self):
        state = reviewed(self.prior_state())
        before = copy.deepcopy(state)
        with self.assertRaises(ValidationError):
            provided(state, "other-agent", memory_of(record("canonical", "Completely different statement", "other-agent")))
        self.assertEqual(state, before)

    def test_update_of_shared_record_id_cannot_erase_the_other_sources_original_claim(self):
        original = memory_of(record("shared"))
        state = provided(create_common(COORDINATOR), "source-a", original)
        state = provided(state, "source-b", original)
        before = copy.deepcopy(state)
        with self.assertRaises(ValidationError):
            provided(state, "source-a", memory_of(record("shared", "Different new claim from A")))
        self.assertEqual(state, before)
        self.assertEqual(state["memory"]["records"][0]["value"], "Краткие ответы")

    def test_identical_reregistration_is_idempotent_and_keeps_semantic_receipt(self):
        incoming = memory_of(record("first"))
        state = provided(create_common(COORDINATOR), "source-a", incoming)
        state = reviewed(state)
        same = provided(state, "source-a", incoming)
        self.assertEqual(same, state)

    def test_modified_owned_source_invalidates_review_without_inheriting_changed_fact_approval(self):
        state = provided(create_common(COORDINATOR), "source-a", memory_of(record("first")))
        state = reviewed(state)
        state = user_decisions(state, {"first": "active"})
        state = provided(state, "source-a", memory_of(record("first", "Changed preference")))
        self.assertIsNone(state["semantic"])
        self.assertEqual(state["memory"]["records"][0]["review"]["decision"], "pending")
        state = reviewed(state)
        self.assertIsNotNone(common_view(state)["question"])

    def test_explicit_user_pending_is_not_overwritten_by_inherited_old_confirmation(self):
        state = reviewed(self.prior_state())
        state = user_decisions(state, {"canonical": "pending"}, "user-wants-to-reconsider")
        self.assertEqual(state["memory"]["records"][0]["review"]["decision"], "pending")
        self.assertFalse(common_view(state)["export_allowed"])

    def test_explicit_pending_for_alias_is_not_overridden_by_canonical_confirmation(self):
        state = reviewed(self.prior_state())
        state = provided(state, "personal-source", memory_of(record("alias", "Без длинных вступлений", "personal")))
        state = reviewed(state, "equivalent", "canonical")
        state = user_decisions(state, {"alias": "pending"}, "user-reconsiders-this-alias")
        by_id = {r["id"]: r for r in state["memory"]["records"]}
        self.assertEqual(by_id["canonical"]["review"]["decision"], "active")
        self.assertEqual(by_id["alias"]["review"]["decision"], "pending")
        state = reviewed(state, "equivalent", "canonical")
        self.assertEqual(next(r for r in state["memory"]["records"] if r["id"] == "alias")["review"]["decision"], "pending")

    def test_shown_or_deferred_question_is_preserved_when_equivalent_source_arrives(self):
        for deferred in (False, True):
            state = provided(create_common(COORDINATOR), "first", memory_of(record("canonical")))
            state = reviewed(state)
            question = common_view(state)["question"]
            event = {"id": "saved-question", "actor": "user" if deferred else "agent",
                     "type": "defer" if deferred else "question_shown", "data": {"question_id": question["id"]}}
            if deferred:
                event["data"]["reason"] = "User requested to decide later"
            state = common_event(state, event, expected_revision=state["revision"], actor=COORDINATOR)
            state = provided(state, "late", memory_of(record("alias", "Коротко и по существу", "late")))
            state = reviewed(state, "equivalent", "canonical")
            view = common_view(state)
            self.assertIsNone(view["question"])
            saved = view["profile"]["deferred_questions"][0] if deferred else view["profile"]["awaiting_answer"]
            self.assertEqual(saved["id"], question["id"])
            self.assertTrue(all(r["review"]["decision"] == "pending" for r in state["memory"]["records"]))

    def test_record_exclusion_requires_new_semantic_pass_and_does_not_leak_to_package(self):
        state = reviewed(self.prior_state())
        state = user_decisions(state, {"canonical": "exclude"})
        self.assertFalse(common_view(state)["export_allowed"])
        self.assertEqual(common_input(state)["sources"][0]["record_ids"], [])
        state = reviewed(state)
        result = common_result(state)
        path = self.root / "excluded.zip"
        export_package(result["memory"], path, review=result["review"])
        read = read_package(path)
        self.assertEqual(read["memory"]["records"], [])
        self.assertEqual(read["review"]["record_checks"], [])
        self.assertEqual(read["review"]["semantic"]["covered_record_ids"], [])
        self.assertTrue(all("Краткие ответы" not in raw.decode("utf8") for raw in read["files"].values()))

    def test_excluded_source_does_not_leave_its_exclusive_records_in_export(self):
        state = provided(create_common(COORDINATOR), "only-source", memory_of(record("private-only", "Do not transfer this source")))
        state = reviewed(state)
        state = user_decisions(state, {"private-only": "active"})
        state = add_source(state, {"id": "only-source", "label": "only-source", "status": "excluded",
                                  "note": "User explicitly excluded this source from transfer", "excluded_by_user": True},
                           expected_revision=state["revision"], actor=COORDINATOR)
        state = reviewed(state)
        result = common_result(state)
        active_export_ids = {r["id"] for r in result["memory"]["records"] if r["review"]["decision"] != "exclude"}
        self.assertNotIn("private-only", active_export_ids)

    def test_unavailable_source_blocks_complete_but_explicit_partial_scope_preserves_uncertainty(self):
        for partial in (False, True):
            state = self.prior_state(allow_partial=partial, scope_note="User selected currently available sources" if partial else None)
            state = add_source(state, {"id": "cloud-unavailable", "label": "Cloud source", "status": "unavailable", "note": "Host has no actual access"},
                               expected_revision=state["revision"], actor=COORDINATOR)
            state = reviewed(state)
            if partial:
                result = common_result(state)
                self.assertEqual(result["review"]["readiness"], "partial")
                self.assertEqual(result["review"]["sources"][1]["status"], "provided")
                self.assertTrue(any(s["status"] == "unavailable" for s in result["review"]["sources"]))
            else:
                with self.assertRaises(ValidationError):
                    common_result(state)

    def test_coordinator_and_revision_are_checked_without_modifying_input(self):
        state = create_common(COORDINATOR)
        source = {"id": "one", "label": "One", "status": "pending", "note": "Fictional planned source"}
        for actor, revision in (("other-agent", 0), (COORDINATOR, 99), (COORDINATOR, True)):
            with self.subTest(actor=actor, revision=revision), self.assertRaises(ValidationError):
                add_source(state, source, expected_revision=revision, actor=actor)
        self.assertEqual(state["revision"], 0)
        self.assertEqual(state["sources"], [])

    def test_two_sqlite_clients_cannot_commit_using_the_same_stale_revision(self):
        db = self.root / "common.sqlite"
        CommonStore.create(db, create_common(COORDINATOR))
        clients = [CommonStore(db), CommonStore(db)]

        def update(index):
            try:
                clients[index].update(lambda state: add_source(state,
                    {"id": "source-" + str(index), "label": "Source", "status": "pending", "note": "Fictional source"},
                    expected_revision=0, actor=COORDINATOR))
                return "committed"
            except ValidationError:
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, range(2)))
        self.assertCountEqual(results, ["committed", "stale"])
        state = clients[0].load()
        self.assertEqual(state["revision"], 1)
        self.assertEqual(len(state["sources"]), 1)

    def test_store_export_rejects_old_result_before_creating_any_output_path(self):
        state = reviewed(self.prior_state())
        result = common_result(state)
        store = CommonStore.create(self.root / "authority.sqlite", state)
        store.update(lambda current: provided(current, "late-empty", empty_memory()))
        output = self.root / "must-not-be-created" / "old.zip"
        with self.assertRaises(ValidationError):
            store.export_package(result["memory"], output)
        self.assertFalse(output.parent.exists())


if __name__ == "__main__":
    unittest.main()
