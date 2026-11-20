"""Explicit offline audit fixtures for tests/demos, never a user-data policy.

The supplied records are authored fictional test data. This helper constructs
matching fictional evidence and scripted reviewer attestations, then uses the
ordinary coordinator and export gate. It does not call an AI reviewer and is
not included in distributed skills. Never use it to approve real user data.
Existing user decisions are trusted *only as fixture inputs* and are retained;
pending records do not become approved. Partial fixture scope is explicit.
"""
from __future__ import annotations

import copy
import json

from portable_memory.common_profile import (
    CommonStore, add_source, common_template, create_common, export_review, review_common,
)
from portable_memory.model import validate_memory
from portable_memory.package import export_package
from portable_memory.profile_audit import CATEGORIES, quality_template
from portable_memory.semantic_review import semantic_quality_template


def fictional_assessment(memory):
    """Make synthetic quoted evidence; this is not semantic inference."""
    kinds = {"profile": "identity", "account": "mailboxes", "contact": "mailboxes",
             "integration": "integrations", "rule": "behavior", "preference": "communication",
             "project": "projects", "idea": "projects", "artifact": "projects",
             "skill": "workflows", "workflow": "workflows", "resource": "resources",
             "automation": "automations"}
    assessment = {"format": "portable-profile-assessment", "schema_version": 1,
                  "sources": [], "claims": [], "coverage": []}
    by_category, source_quotes, source_locators = {}, {}, {}
    for record in memory["records"]:
        if record["review"]["decision"] == "exclude":
            continue
        category = kinds.get(record["kind"], "identity")
        # Evidence IDs must be the record's actual declared source IDs, even in
        # fictional tests. A new invented citation would not prove provenance.
        source = record["sources"][0]
        sid = source["id"]
        quote = json.dumps({"title": record["title"], "value": record["value"],
                            "scope": record["scope"]}, ensure_ascii=False, sort_keys=True)
        source_quotes.setdefault(sid, []).append(quote)
        source_locators.setdefault(sid, []).append(source["locator"])
        assessment["claims"].append({"record_id": record["id"], "category": category,
            "subject": "fictional-subject", "predicate": "fixture-field:" + record["id"],
            "cardinality": "single", "evidence": [{"source_id": sid, "quote": quote}]})
        by_category.setdefault(category, []).append(sid)
    assessment["sources"] = [{"id": sid, "locator": "; ".join(dict.fromkeys(source_locators[sid])),
                              "text": "\n".join(quotes)} for sid, quotes in source_quotes.items()]
    for category in CATEGORIES:
        sources = list(dict.fromkeys(by_category.get(category, [])))
        assessment["coverage"].append({"category": category,
            "status": "checked" if sources else "unavailable", "source_ids": sources,
            "note": "Only authored fictional fixture records were supplied; no account completeness claim."})
    return assessment


def synthetic_common_state(memory, *, assessment=None, quality=None, semantic_groups=None):
    """Use real public APIs with clearly labelled synthetic attestations.

    Optional assessments/quality preserve a demo's explicitly authored evidence
    and uncertainty instead of replacing it with all-supported fixture checks.
    Explicit semantic_groups can model a known conflict or equivalence.
    """
    validate_memory(memory)  # Do not repair stale decisions in negative tests.
    actor = "synthetic-fixture-coordinator"
    state = create_common(actor, previous=memory, allow_partial=True,
        scope_note="Explicit synthetic fixture scope: preserve pending, historical and excluded decisions; no account completeness or native activation.")
    state = add_source(state, {"id": "fictional-supplied-records", "label": "Authored fictional fixture",
        "status": "provided", "note": "Synthetic input only; no live account or reviewer was invoked."},
        memory, expected_revision=state["revision"], actor=actor)
    assessment = copy.deepcopy(assessment) if assessment is not None else fictional_assessment(state["memory"])
    if quality is None:
        quality = quality_template(state["memory"], assessment)
        quality["reviewer"] = {"kind": "human", "label": "Scripted fictional evidence checker (not a live review)"}
        for check in quality["checks"]:
            check["decision"] = "supported"
            check["note"] = "Scripted test attestation over authored fictional evidence; not a real model result."
    semantic = common_template(state)
    semantic["reviewer"] = {"kind": "human", "label": "Scripted fictional whole-profile checker (not a live review)"}
    semantic["cross_source_checked"] = True
    semantic["groups"] = (copy.deepcopy(semantic_groups) if semantic_groups is not None else [
        {"id": "fixture-group-" + rid, "relation": "compatible", "record_ids": [rid], "canonical_id": None,
         "note": "Authored independent fictional fixture field; no inference over real user data."}
        for rid in semantic["covered_record_ids"]])
    semantic_quality = semantic_quality_template(semantic)
    semantic_quality.update(reviewer={"kind": "human", "label": "Scripted fictional second-pass checker (not a live review)"},
        decision="supported", note="Explicit synthetic second-pass attestation for this exact test fixture only.")
    return review_common(state, assessment, copy.deepcopy(quality), semantic, semantic_quality,
                         expected_revision=state["revision"], actor=actor)


def synthetic_review(memory, **kwargs):
    """Return the actual coordinator's memory/review result for fictional data."""
    return export_review(synthetic_common_state(memory, **kwargs))


def save_fixture_store(path, memory, **kwargs):
    """Persist the real audited coordinator store for end-to-end CLI tests."""
    state = synthetic_common_state(memory, **kwargs)
    store = CommonStore.create(path, state)
    return store.export_review()


def export_synthetic_package(memory, output, *, assets=None, **kwargs):
    """Fixture-only wrapper: the production exporter still requires real review."""
    reviewed = synthetic_review(memory, **kwargs)
    return export_package(reviewed["memory"], output, assets=assets, review=reviewed["review"])
