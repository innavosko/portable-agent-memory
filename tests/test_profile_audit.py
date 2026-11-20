"""Profile quality gates, evidence provenance, and focused review regressions."""
import copy
import unittest

from portable_memory.errors import ValidationError
from portable_memory.model import confirm, empty_memory, record_digest, validate_memory
from portable_memory.profile_audit import (CATEGORIES, audit_profile, prepare_memory,
                                           quality_template)


def record(rid="name", value="Анна", *, title="Имя", scope="global", currency="unknown"):
    return {"id": rid, "kind": "profile", "title": title, "scope": scope, "value": value,
            "sources": [{"id": "s1", "locator": "Synthetic conversation", "observed_at": None}],
            "provenance": "user_statement", "currency": currency, "links": [],
            "review": {"decision": "pending"}}


def memory(*records):
    data = empty_memory()
    data["records"] = list(records)
    return data


def assessment(data, *, category="identity", predicate="name", cardinality="single"):
    return {"format": "portable-profile-assessment", "schema_version": 1,
            "sources": [{"id": "s1", "locator": "Synthetic conversation",
                         "text": "Имя: Анна. Имя: Ирина. Почта: one@example.invalid. Почта: two@example.invalid."}],
            "claims": [{"record_id": item["id"], "category": category, "subject": "Пользователь",
                        "predicate": predicate, "cardinality": cardinality,
                        "evidence": [{"source_id": "s1", "quote": "Имя: Анна."}]}
                       for item in data["records"]],
            "coverage": [{"category": key, "status": "checked" if key == category else "not_applicable",
                          "source_ids": ["s1"] if key == category else [],
                          "note": "Only supplied synthetic excerpts were checked" if key == category else "Not selected in this fixture"}
                         for key in CATEGORIES]}


def supported(data, annotation):
    result = quality_template(data, annotation)
    result["reviewer"] = {"kind": "separate_agent", "label": "Synthetic test checker; not a real model call"}
    for check in result["checks"]:
        check["decision"] = "supported"
        check["note"] = "Synthetic attestation fixture for testing the gate"
    return result


def decide(data, decision, ids=None):
    return confirm(data, {"decisions": [{"id": item["id"], "content_sha256": record_digest(item), "decision": decision}
                                      for item in data["records"] if ids is None or item["id"] in ids]})


class ProfileAuditTests(unittest.TestCase):
    def test_quality_template_is_uncertain_not_an_automatic_approval(self):
        data = memory(record())
        annotation = assessment(data)
        template = quality_template(data, annotation)
        self.assertEqual(template["checks"][0]["decision"], "uncertain")
        self.assertEqual(template["reviewer"]["kind"], "same_model_second_pass")
        result = audit_profile(data, annotation, template)
        self.assertEqual(result["records"][0]["evidence_status"], "verified")
        self.assertFalse(result["records"][0]["quality_ready"])
        self.assertFalse(result["records"][0]["can_confirm"])
        self.assertFalse(result["questions"])
        self.assertTrue(any(task["kind"] == "quality" for task in result["agent_tasks"]))

    def test_verified_evidence_and_fresh_support_allow_grouped_review(self):
        data = memory(record("name"), record("role", "Исследователь", title="Роль"))
        annotation = assessment(data)
        annotation["claims"][1]["predicate"] = "role"
        result = audit_profile(data, annotation, supported(data, annotation))
        self.assertTrue(all(entry["can_confirm"] for entry in result["records"]))
        self.assertEqual([q["kind"] for q in result["questions"]], ["review"])
        self.assertEqual(result["questions"][0]["record_ids"], ["name", "role"])
        self.assertTrue(all(item["review"]["decision"] == "pending" for item in data["records"]))

    def test_quote_must_be_exact_in_provided_text_and_declared_record_source(self):
        data = memory(record())
        for source_id, quote in [("s2", "Имя: Анна."), ("s1", "имя: Анна."), ("s1", "Придуманная цитата")]:
            with self.subTest(source_id=source_id, quote=quote):
                annotation = assessment(data)
                annotation["claims"][0]["evidence"] = [{"source_id": source_id, "quote": quote}]
                result = audit_profile(data, annotation, supported(data, annotation))
                self.assertEqual(result["records"][0]["evidence_status"], "invalid")
                self.assertFalse(result["records"][0]["quality_ready"])
                self.assertFalse(result["questions"])
                self.assertTrue(any(task["kind"] == "evidence" for task in result["agent_tasks"]))

    def test_matching_quote_in_undeclared_source_is_not_accepted(self):
        data = memory(record())
        annotation = assessment(data)
        annotation["sources"].append({"id": "s2", "locator": "Other file", "text": "Имя: Анна."})
        annotation["claims"][0]["evidence"][0]["source_id"] = "s2"
        result = audit_profile(data, annotation, supported(data, annotation))
        self.assertFalse(result["records"][0]["can_confirm"])

    def test_missing_annotation_and_empty_evidence_need_agent_work(self):
        data = memory(record())
        for missing in (True, False):
            annotation = assessment(data)
            if missing:
                annotation["claims"] = []
            else:
                annotation["claims"][0]["evidence"] = []
            result = audit_profile(data, annotation)
            self.assertEqual(result["records"][0]["evidence_status"], "missing")
            self.assertFalse(result["records"][0]["quality_ready"])
            self.assertFalse(result["questions"])
            self.assertTrue(result["agent_tasks"])

    def test_changed_cited_source_text_invalidates_quality(self):
        data = memory(record())
        annotation = assessment(data)
        quality = supported(data, annotation)
        annotation["sources"][0]["text"] += " Новое обстоятельство."
        result = audit_profile(data, annotation, quality)
        self.assertEqual(result["records"][0]["quality_status"], "stale")
        self.assertFalse(result["records"][0]["quality_ready"])

    def test_changed_annotation_provenance_or_scope_invalidates_quality(self):
        for change in ("subject", "scope", "sources", "provenance"):
            with self.subTest(change=change):
                data = memory(record())
                annotation = assessment(data)
                quality = supported(data, annotation)
                if change == "subject":
                    annotation["claims"][0]["subject"] = "Другой человек"
                elif change == "scope":
                    data["records"][0]["scope"] = "project:other"
                elif change == "sources":
                    data["records"][0]["sources"][0]["locator"] = "Another location"
                else:
                    data["records"][0]["provenance"] = "model_inference"
                result = audit_profile(data, annotation, quality)
                self.assertEqual(result["records"][0]["quality_status"], "stale")
                self.assertFalse(result["records"][0]["quality_ready"])

    def test_quality_fingerprint_ignores_review_currency_and_uncited_sources(self):
        data = memory(record())
        annotation = assessment(data)
        before = quality_template(data, annotation)["checks"][0]["claim_sha256"]
        data = decide(data, "active")
        annotation["sources"].append({"id": "unused", "locator": "Not cited", "text": "Unrelated new content"})
        after = quality_template(data, annotation)["checks"][0]["claim_sha256"]
        self.assertEqual(before, after)

    def test_prepare_reuses_only_unchanged_prior_confirmation_and_currency(self):
        previous = decide(memory(record()), "active")
        extracted = memory(record())
        prepared = prepare_memory(extracted, previous)
        self.assertEqual(prepared["records"], previous["records"])
        validate_memory(prepared)
        self.assertEqual(extracted["records"][0]["review"]["decision"], "pending")

    def test_changed_scope_sources_and_provenance_cannot_inherit_confirmation(self):
        previous = decide(memory(record()), "active")
        for field in ("scope", "sources", "provenance", "value"):
            with self.subTest(field=field):
                changed = copy.deepcopy(previous)
                if field == "sources":
                    changed["records"][0][field][0]["locator"] = "Other source"
                else:
                    changed["records"][0][field] = "project:new" if field == "scope" else ("source" if field == "provenance" else "Ирина")
                prepared = prepare_memory(changed, previous)
                self.assertEqual(prepared["records"][0]["review"], {"decision": "pending"})

    def test_valid_historical_and_excluded_decisions_are_preserved(self):
        for action in ("historical", "exclude"):
            previous = decide(memory(record()), action)
            prepared = prepare_memory(memory(record()), previous)
            self.assertEqual(prepared["records"], previous["records"])
            result = audit_profile(prepared, assessment(prepared), previous=previous)
            self.assertFalse(result["questions"])

    def test_current_valid_historical_overrides_prior_active_confirmation(self):
        previous = decide(memory(record()), "active")
        current = decide(previous, "historical")
        self.assertEqual(prepare_memory(current, previous)["records"][0]["review"]["decision"], "historical")
        report = audit_profile(current, assessment(current), previous=previous)
        self.assertEqual(report["records"][0]["decision"], "historical")
        self.assertFalse(report["conflicts"])

    def test_audit_does_not_restore_previous_confirmation_over_explicit_pending(self):
        previous = decide(memory(record()), "active")
        pending = decide(previous, "pending")
        report = audit_profile(pending, assessment(pending), previous=previous)
        self.assertEqual(report["records"][0]["decision"], "pending")
        self.assertFalse(report["records"][0]["quality_ready"])

    def test_valid_current_confirmation_needs_no_new_quality_pass_or_interview(self):
        data = decide(memory(record()), "active")
        result = audit_profile(data, assessment(data))
        self.assertEqual(result["records"][0]["quality_status"], "human_confirmed")
        self.assertTrue(result["records"][0]["quality_ready"])
        self.assertFalse(result["questions"])

    def test_fresh_contradiction_blocks_even_existing_confirmation(self):
        data = decide(memory(record()), "active")
        annotation = assessment(data)
        quality = supported(data, annotation)
        quality["checks"][0]["decision"] = "contradicted"
        quality["checks"][0]["note"] = "Source refers to another person"
        result = audit_profile(data, annotation, quality)
        self.assertFalse(result["records"][0]["quality_ready"])
        self.assertFalse(result["records"][0]["can_confirm"])
        self.assertFalse(result["questions"])
        self.assertTrue(any(task["kind"] == "quality" for task in result["agent_tasks"]))

    def test_same_fact_conflicts_despite_different_titles_and_subject_case(self):
        data = memory(record("one", "Анна", title="Моё имя"), record("two", "Ирина", title="Обращение"))
        annotation = assessment(data)
        annotation["claims"][1]["subject"] = "  ПОЛЬЗОВАТЕЛЬ\t"
        result = audit_profile(data, annotation, supported(data, annotation))
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["questions"][0]["kind"], "conflict")
        self.assertTrue(all(entry["quality_ready"] for entry in result["records"]))
        self.assertFalse(any(entry["can_confirm"] for entry in result["records"]))

    def test_same_title_is_not_a_fact_match_without_matching_annotation(self):
        data = memory(record("one", "Анна"), record("two", "Ирина"))
        annotation = assessment(data)
        annotation["claims"][1]["subject"] = "Коллега"
        self.assertFalse(audit_profile(data, annotation, supported(data, annotation))["conflicts"])
        annotation["claims"][1]["subject"] = "Пользователь"
        annotation["claims"][1]["predicate"] = "preferred_address"
        self.assertFalse(audit_profile(data, annotation, supported(data, annotation))["conflicts"])

    def test_separate_scopes_and_multiple_cardinality_do_not_conflict(self):
        data = memory(record("one", "one@example.invalid", scope="project:a"),
                      record("two", "two@example.invalid", scope="project:b"))
        annotation = assessment(data, category="mailboxes", predicate="address")
        self.assertFalse(audit_profile(data, annotation, supported(data, annotation))["conflicts"])
        data["records"][1]["scope"] = "project:a"
        annotation = assessment(data, category="mailboxes", predicate="address", cardinality="multiple")
        self.assertFalse(audit_profile(data, annotation, supported(data, annotation))["conflicts"])

    def test_model_historical_label_does_not_resolve_a_pending_conflict(self):
        data = memory(record("one", "Анна"), record("two", "Ирина", currency="historical"))
        annotation = assessment(data)
        result = audit_profile(data, annotation, supported(data, annotation))
        self.assertEqual(len(result["conflicts"]), 1)
        historical = decide(data, "historical", {"two"})
        result = audit_profile(historical, annotation, supported(historical, annotation))
        self.assertFalse(result["conflicts"])

    def test_unsupported_competing_claim_goes_to_agent_before_user(self):
        data = decide(memory(record("one", "Анна")), "active")
        data["records"].append(record("two", "Ирина"))
        annotation = assessment(data)
        quality = supported(data, annotation)
        next(check for check in quality["checks"] if check["record_id"] == "two")["decision"] = "uncertain"
        result = audit_profile(data, annotation, quality)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertFalse(result["questions"])
        self.assertTrue(result["records"][0]["quality_ready"])
        self.assertFalse(result["records"][1]["quality_ready"])
        self.assertTrue(any(task["kind"] == "conflict" for task in result["agent_tasks"]))

    def test_question_order_is_conflict_then_review_then_checked_empty_gap(self):
        data = memory(record("one", "Анна"), record("two", "Ирина"), record("goal", "Learn", title="Goal"))
        annotation = assessment(data)
        annotation["claims"][2].update(category="goals", predicate="goal", cardinality="multiple")
        for item in annotation["coverage"]:
            if item["category"] in {"goals", "mailboxes"}:
                item.update(status="checked", source_ids=["s1"])
        result = audit_profile(data, annotation, supported(data, annotation))
        self.assertEqual([question["kind"] for question in result["questions"]], ["conflict", "review", "gap"])
        self.assertIn("наличие", next(item for item in result["categories"] if item["id"] == "mailboxes")["finding"])

    def test_missing_coverage_is_not_checked_and_does_not_force_interview(self):
        data = memory()
        annotation = assessment(data)
        annotation["coverage"] = []
        result = audit_profile(data, annotation)
        self.assertTrue(all(category["status"] == "not_checked" for category in result["categories"]))
        self.assertFalse(result["questions"])
        self.assertTrue(result["agent_tasks"])

    def test_checked_coverage_without_supplied_sources_is_not_trusted(self):
        data = memory()
        annotation = assessment(data)
        annotation["sources"] = []
        result = audit_profile(data, annotation)
        self.assertEqual(result["categories"][0]["status"], "not_checked")
        self.assertFalse(result["questions"])

    def test_unknown_unavailable_excluded_and_not_applicable_are_distinct(self):
        data = memory()
        annotation = assessment(data)
        annotation["coverage"] = [{"category": category, "status": status, "source_ids": [], "note": status}
                                  for category, status in [("mailboxes", "unavailable"), ("goals", "excluded"), ("calendar", "not_applicable")]]
        result = audit_profile(data, annotation)
        statuses = {item["id"]: item["status"] for item in result["categories"]}
        self.assertEqual(statuses["identity"], "not_checked")
        self.assertEqual(statuses["mailboxes"], "unavailable")
        self.assertEqual(statuses["goals"], "excluded")
        self.assertEqual(statuses["calendar"], "not_applicable")
        self.assertFalse(result["questions"])

    def test_issue_ids_are_stable_and_change_with_actual_conflict_content(self):
        data = memory(record("one", "Анна"), record("two", "Ирина"))
        annotation = assessment(data)
        quality = supported(data, annotation)
        first = audit_profile(data, annotation, quality)
        second = audit_profile(copy.deepcopy(data), copy.deepcopy(annotation), copy.deepcopy(quality))
        self.assertEqual(first, second)
        changed = copy.deepcopy(data)
        changed["records"][1]["value"] = "Другое имя"
        third = audit_profile(changed, annotation, supported(changed, annotation))
        self.assertNotEqual(first["conflicts"][0]["id"], third["conflicts"][0]["id"])

    def test_strict_duplicate_and_shape_validation(self):
        data = memory(record())
        base = assessment(data)
        for field in ("sources", "claims", "coverage"):
            broken = copy.deepcopy(base)
            broken[field].append(copy.deepcopy(broken[field][0]))
            with self.subTest(field=field), self.assertRaises(ValidationError):
                audit_profile(data, broken)
        broken = copy.deepcopy(base)
        broken["claims"][0]["evidence"] *= 2
        with self.assertRaises(ValidationError):
            audit_profile(data, broken)
        for mutator in (lambda item: item.update(schema_version=True),
                        lambda item: item.update(extra="unknown"),
                        lambda item: item["claims"][0].update(record_id="missing"),
                        lambda item: item["claims"][0]["evidence"][0].update(quote="")):
            broken = copy.deepcopy(base)
            mutator(broken)
            with self.assertRaises(ValidationError):
                audit_profile(data, broken)

    def test_quality_validation_and_optional_reviewer_provenance(self):
        data = memory(record())
        annotation = assessment(data)
        quality = supported(data, annotation)
        quality["checks"] *= 2
        with self.assertRaises(ValidationError):
            audit_profile(data, annotation, quality)
        quality = supported(data, annotation)
        del quality["reviewer"]
        self.assertIsNone(audit_profile(data, annotation, quality)["quality_reviewer"])
        quality["reviewer"] = {"kind": "independent_magic", "label": "No actual checker"}
        with self.assertRaises(ValidationError):
            audit_profile(data, annotation, quality)

    def test_per_check_reviewer_overrides_global_without_changing_quality_hash(self):
        data = memory(record("name"), record("role", "Исследователь", title="Роль"))
        annotation = assessment(data)
        annotation["claims"][1]["predicate"] = "role"
        quality = supported(data, annotation)
        quality["reviewer"] = {"kind": "same_model_second_pass", "label": "Current second pass"}
        before = audit_profile(data, annotation, quality)
        specific = {"kind": "separate_agent", "label": "Original checker of retained claim"}
        next(check for check in quality["checks"] if check["record_id"] == "name")["reviewer"] = specific
        after = audit_profile(data, annotation, quality)
        records = {entry["id"]: entry for entry in after["records"]}
        self.assertEqual(records["name"]["quality_reviewer"], specific)
        self.assertEqual(records["role"]["quality_reviewer"], quality["reviewer"])
        self.assertEqual([entry["claim_sha256"] for entry in before["records"]], [entry["claim_sha256"] for entry in after["records"]])
        self.assertTrue(all(entry["quality_ready"] for entry in after["records"]))

    def test_invalid_per_check_reviewer_is_rejected(self):
        data = memory(record())
        annotation = assessment(data)
        for reviewer in [{"kind": "magic", "label": "Invalid"}, {"kind": "human", "label": ""}, {"kind": "human"}]:
            with self.subTest(reviewer=reviewer):
                quality = supported(data, annotation)
                quality["checks"][0]["reviewer"] = reviewer
                with self.assertRaises(ValidationError):
                    audit_profile(data, annotation, quality)
    def test_audit_does_not_mutate_memory_assessment_quality_or_previous(self):
        data = memory(record())
        previous = decide(memory(record()), "active")
        annotation = assessment(data)
        quality = supported(data, annotation)
        originals = copy.deepcopy([data, annotation, quality, previous])
        audit_profile(data, annotation, quality, previous)
        self.assertEqual([data, annotation, quality, previous], originals)


if __name__ == "__main__":
    unittest.main()
