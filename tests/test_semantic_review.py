"""Fictional model attestations; no tests pretend the code classifies semantics."""
import copy
import unittest

from portable_memory.errors import ValidationError
from portable_memory.model import confirm, digest, empty_memory, review_template
from portable_memory.semantic_review import (semantic_input_hash, semantic_quality_template,
                                             semantic_template, validate_semantic)


class SemanticReviewTests(unittest.TestCase):
    def setUp(self):
        self.memory = empty_memory()
        self.memory["records"] = [self.record("brief", "Пиши кратко", "source-1"),
                                  self.record("concise", "Ответы без длинных вступлений", "source-2")]
        self.sources = [self.source("source-1", ["brief"]), self.source("source-2", ["concise"])]

    def record(self, rid, text, source, scope="global", kind="preference"):
        return {"id": rid, "kind": kind, "title": rid, "scope": scope, "value": text,
                "provenance": "source", "currency": "unknown", "sources": [{"id": source, "locator": "synthetic:" + source}],
                "links": [], "review": {"decision": "pending"}}

    def source(self, sid, ids):
        return {"id": sid, "label": sid, "status": "provided", "record_ids": ids,
                "snapshot_sha256": digest({"synthetic_source": sid}), "note": "Fictional source supplied for a unit test"}

    def review(self, memory=None, sources=None, relation="compatible"):
        memory = self.memory if memory is None else memory
        sources = self.sources if sources is None else sources
        review = semantic_template(memory, sources)
        ids = review["covered_record_ids"]
        review["reviewer"] = {"kind": "separate_agent", "label": "Fictional semantic assessor in this test"}
        review["cross_source_checked"] = True
        review["groups"] = [] if not ids else [{"id": "group-1", "relation": relation, "record_ids": ids,
                                                "canonical_id": ids[0] if relation == "equivalent" else None,
                                                "note": "Explicit fictional assessor judgment based on the supplied sources"}]
        return review

    def quality(self, review, decision="supported"):
        # This fixture is an explicit fictional second-pass result, not a mock
        # of the validator and not an automatically supported product template.
        return {"format": "portable-semantic-quality", "schema_version": 1, "review_sha256": digest(review),
                "reviewer": {"kind": "same_model_second_pass", "label": "Fictional second-pass reviewer"},
                "decision": decision, "note": "The test explicitly supplies this quality attestation."}

    def test_templates_never_approve_or_group_without_actual_model_pass(self):
        review = semantic_template(self.memory, self.sources)
        quality = semantic_quality_template(review)
        self.assertFalse(review["cross_source_checked"])
        self.assertEqual(review["groups"], [])
        self.assertEqual(quality["decision"], "uncertain")
        self.assertEqual(quality["review_sha256"], digest(review))
        with self.assertRaises(ValidationError):
            validate_semantic(self.memory, self.sources, review, quality)

    def test_paraphrase_merges_only_with_explicit_supported_equivalence_and_never_mutates(self):
        review = self.review(relation="equivalent")
        quality = self.quality(review)
        before = copy.deepcopy((self.memory, self.sources, review, quality))
        result = validate_semantic(self.memory, self.sources, review, quality)
        self.assertEqual(result["aliases"], {"concise": "brief"})
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(before, (self.memory, self.sources, review, quality))
        self.assertTrue(all(r["review"]["decision"] == "pending" for r in self.memory["records"]))
        compatible = self.review(relation="compatible")
        self.assertEqual(validate_semantic(self.memory, self.sources, compatible, self.quality(compatible))["aliases"], {})

    def test_conflict_reports_issue_and_never_picks_newest_or_canonical_winner(self):
        self.memory["records"][1]["value"] = "Всегда подробно объясняй каждый шаг"
        review = self.review(relation="conflict")
        result = validate_semantic(self.memory, self.sources, review, self.quality(review))
        self.assertEqual(result["aliases"], {})
        self.assertEqual(result["conflicts"], [{"id": "group-1", "record_ids": ["brief", "concise"], "note": review["groups"][0]["note"]}])
        review["groups"][0]["canonical_id"] = "brief"
        with self.assertRaises(ValidationError):
            validate_semantic(self.memory, self.sources, review, self.quality(review))

    def test_same_labels_in_different_work_scopes_cannot_be_merged_or_called_same_scope_conflict(self):
        for record, scope in zip(self.memory["records"], ("project:work", "project:personal")):
            record["title"] = "Общий стиль"
            record["scope"] = scope
        for relation in ("equivalent", "conflict"):
            review = self.review(relation=relation)
            with self.subTest(relation=relation), self.assertRaises(ValidationError):
                validate_semantic(self.memory, self.sources, review, self.quality(review))
        review = self.review()
        self.assertEqual(validate_semantic(self.memory, self.sources, review, self.quality(review))["aliases"], {})

    def test_exact_kinds_and_scopes_are_preserved_without_casefolding(self):
        for field, value in (("kind", "rule"), ("scope", "GLOBAL")):
            memory = copy.deepcopy(self.memory)
            memory["records"][1][field] = value
            review = self.review(memory=memory, relation="equivalent")
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_semantic(memory, self.sources, review, self.quality(review))

    def test_new_empty_source_invalidates_previous_review_even_without_new_records(self):
        review = self.review()
        sources = self.sources + [self.source("late-empty-source", [])]
        with self.assertRaisesRegex(ValidationError, "stale"):
            validate_semantic(self.memory, sources, review, self.quality(review))
        refreshed = self.review(sources=sources)
        self.assertIn("late-empty-source", refreshed["covered_source_ids"])
        validate_semantic(self.memory, sources, refreshed, self.quality(refreshed))

    def test_source_snapshot_status_and_membership_changes_invalidate_review(self):
        review = self.review()
        for field, value in (("snapshot_sha256", "0" * 64), ("status", "unavailable"), ("record_ids", []),
                             ("note", "New source evidence")):
            sources = copy.deepcopy(self.sources)
            sources[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_semantic(self.memory, sources, review, self.quality(review))

    def test_record_payload_provenance_links_and_source_changes_invalidate_review(self):
        review = self.review()
        changes = [("value", "Changed"), ("title", "Changed title"), ("scope", "project:new"),
                   ("provenance", "model_inference"), ("sources", [{"id": "source-1", "locator": "new:source"}]),
                   ("links", [{"target": "concise", "relation": "related", "basis": "source"}])]
        for field, value in changes:
            memory = copy.deepcopy(self.memory)
            memory["records"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_semantic(memory, self.sources, review, self.quality(review))

    def test_user_review_and_currency_changes_do_not_force_new_semantic_pass(self):
        review = self.review()
        decisions = review_template(self.memory)
        decisions["decisions"][0]["decision"] = "active"
        decisions["decisions"][1]["decision"] = "historical"
        approved = confirm(self.memory, decisions)
        approved["metadata"]["last_import"] = {"at": "future transport time"}
        approved["records"][0]["metadata"] = {"ui": "changed"}
        self.assertEqual(semantic_input_hash(self.memory, self.sources), semantic_input_hash(approved, self.sources))
        validate_semantic(approved, self.sources, review, self.quality(review))

    def test_exclusion_changes_membership_and_requires_updated_coverage(self):
        review = self.review()
        decisions = review_template(self.memory)
        decisions["decisions"][0]["decision"] = "exclude"
        excluded = confirm(self.memory, decisions)
        with self.assertRaises(ValidationError):
            validate_semantic(excluded, self.sources, review, self.quality(review))
        refreshed = self.review(memory=excluded)
        self.assertEqual(refreshed["covered_record_ids"], ["concise"])
        validate_semantic(excluded, self.sources, refreshed, self.quality(refreshed))

    def test_record_source_and_membership_order_do_not_change_input_hash(self):
        memory = copy.deepcopy(self.memory)
        memory["records"].reverse()
        sources = copy.deepcopy(self.sources)
        sources[0]["record_ids"] = ["brief", "concise"]
        changed_order = copy.deepcopy(sources)
        changed_order.reverse()
        next(s for s in changed_order if s["id"] == "source-1")["record_ids"].reverse()
        self.assertEqual(semantic_input_hash(self.memory, sources), semantic_input_hash(memory, changed_order))

    def test_false_coverage_omitted_source_and_unassigned_records_are_rejected(self):
        original = self.review()
        variants = []
        missing_record = copy.deepcopy(original); missing_record["covered_record_ids"] = ["brief"]; variants.append(missing_record)
        missing_source = copy.deepcopy(original); missing_source["covered_source_ids"] = ["source-1"]; variants.append(missing_source)
        missing_group_record = copy.deepcopy(original); missing_group_record["groups"][0]["record_ids"] = ["brief"]; variants.append(missing_group_record)
        unchecked = copy.deepcopy(original); unchecked["cross_source_checked"] = False; variants.append(unchecked)
        extra_record = copy.deepcopy(original); extra_record["covered_record_ids"].append("invented"); variants.append(extra_record)
        for review in variants:
            with self.subTest(review=review), self.assertRaises(ValidationError):
                validate_semantic(self.memory, self.sources, review, self.quality(review))

    def test_overlapping_groups_repeated_ids_and_invalid_canonical_are_rejected(self):
        original = self.review(relation="equivalent")
        variants = []
        overlap = copy.deepcopy(original)
        overlap["groups"].append({"id": "group-2", "relation": "compatible", "record_ids": ["brief"], "canonical_id": None, "note": "Repeated"})
        variants.append(overlap)
        repeated = copy.deepcopy(original); repeated["groups"][0]["record_ids"].append("brief"); variants.append(repeated)
        wrong_canonical = copy.deepcopy(original); wrong_canonical["groups"][0]["canonical_id"] = "unknown"; variants.append(wrong_canonical)
        single = copy.deepcopy(original); single["groups"][0]["record_ids"] = ["brief"]; variants.append(single)
        repeated_coverage = copy.deepcopy(original); repeated_coverage["covered_record_ids"].append("brief"); variants.append(repeated_coverage)
        for review in variants:
            with self.subTest(review=review), self.assertRaises(ValidationError):
                validate_semantic(self.memory, self.sources, review, self.quality(review))

    def test_quality_must_be_supported_fresh_and_actually_attributed(self):
        review = self.review()
        for quality in (None, semantic_quality_template(review), self.quality(review, "uncertain"), self.quality(review, "contradicted")):
            with self.subTest(quality=quality), self.assertRaises(ValidationError):
                validate_semantic(self.memory, self.sources, review, quality)
        stale = self.quality(review)
        changed = copy.deepcopy(review); changed["groups"][0]["note"] = "Revised semantic judgment"
        with self.assertRaises(ValidationError):
            validate_semantic(self.memory, self.sources, changed, stale)
        unnamed = semantic_quality_template(review); unnamed["decision"] = "supported"
        with self.assertRaises(ValidationError):
            validate_semantic(self.memory, self.sources, review, unnamed)
        review["reviewer"]["label"] = "  "
        with self.assertRaises(ValidationError):
            validate_semantic(self.memory, self.sources, review, self.quality(review))

    def test_empty_profile_still_requires_source_coverage_and_explicit_passes(self):
        memory = empty_memory()
        sources = [self.source("source-with-no-records", [])]
        sources[0].update(status="unavailable", snapshot_sha256=None)
        review = self.review(memory, sources)
        result = validate_semantic(memory, sources, review, self.quality(review))
        self.assertEqual(result, {"aliases": {}, "conflicts": [], "review_sha256": digest(review)})
        review["covered_source_ids"] = []
        with self.assertRaises(ValidationError):
            validate_semantic(memory, sources, review, self.quality(review))

    def test_malformed_types_source_cards_and_schema_are_not_truthy_approvals(self):
        for field, value in (("cross_source_checked", 1), ("schema_version", True), ("groups", {}),
                             ("covered_record_ids", "brief,concise"), ("input_sha256", "not-a-hash")):
            review = self.review(); review[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_semantic(self.memory, self.sources, review, self.quality(review))
        for source in ({**self.sources[0], "record_ids": ["unknown"]}, {**self.sources[0], "snapshot_sha256": 1},
                       {**self.sources[0], "extra": "unsupported"}):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                semantic_input_hash(self.memory, [source])
        with self.assertRaises(ValidationError):
            semantic_input_hash(self.memory, [self.sources[0], self.sources[0]])


if __name__ == "__main__":
    unittest.main()
