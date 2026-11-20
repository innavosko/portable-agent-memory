"""Validate explicit, content-bound semantic review attestations.

The host model or human supplies the semantic decisions and a second quality
assessment. This module neither calls a model nor determines equivalence itself.
It checks coverage, freshness, grouping and scope boundaries; it never changes
facts, user decisions, or selects a winner for a conflict.
"""
from __future__ import annotations

import copy
import re

from .model import canonical, digest, require, validate_memory

_HASH = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER = "Укажите фактического проверяющего"
_SOURCE_FIELDS = {"id", "label", "status", "record_ids", "snapshot_sha256", "note"}
_REVIEW_FIELDS = {"format", "schema_version", "input_sha256", "reviewer", "covered_record_ids",
                  "covered_source_ids", "cross_source_checked", "groups"}
_QUALITY_FIELDS = {"format", "schema_version", "review_sha256", "reviewer", "decision", "note"}
_GROUP_FIELDS = {"id", "relation", "record_ids", "canonical_id", "note"}
_REVIEWER_KINDS = {"same_model_second_pass", "separate_agent", "human"}


def _text(value, field):
    require(isinstance(value, str) and bool(value.strip()) and "\x00" not in value,
            field + ": nonempty text required")


def _hash(value, field):
    require(isinstance(value, str) and _HASH.fullmatch(value), "Invalid " + field)


def _ids(value, field):
    require(isinstance(value, list), field + " must be a list")
    for item in value:
        _text(item, field + " entry")
    require(len(value) == len(set(value)), "Repeated ID in " + field)
    return set(value)


def _reviewer(value, *, named=False):
    require(isinstance(value, dict) and set(value) == {"kind", "label"}, "Invalid semantic reviewer")
    require(isinstance(value["kind"], str) and value["kind"] in _REVIEWER_KINDS, "Invalid semantic reviewer kind")
    _text(value["label"], "reviewer.label")
    if named:
        require(value["label"].strip() != _PLACEHOLDER, "Name the actual semantic reviewer before approval")


def _inputs(memory, sources):
    # The coordinator owns user-decision validity. A stale ordinary review must
    # not prevent computing the changed content hash needed for a new model pass.
    validate_memory(memory, allow_stale_review=True)
    require(isinstance(sources, list), "Semantic sources must be a list")
    known_records = {record["id"] for record in memory["records"]}
    source_ids, cards = set(), []
    for source in sources:
        require(isinstance(source, dict) and set(source) == _SOURCE_FIELDS, "Invalid semantic source card")
        for name in ("id", "label", "status"):
            _text(source[name], "source." + name)
        require(isinstance(source["note"], str), "source.note must be text")
        require(source["id"] not in source_ids, "Repeated semantic source ID")
        source_ids.add(source["id"])
        members = _ids(source["record_ids"], "source.record_ids")
        require(members <= known_records, "Source card references an unknown record")
        if source["snapshot_sha256"] is not None:
            _hash(source["snapshot_sha256"], "source snapshot hash")
        card = copy.deepcopy(source)
        card["record_ids"] = sorted(members)
        cards.append(card)
    cards.sort(key=lambda item: item["id"])
    records = {record["id"]: record for record in memory["records"] if record["review"]["decision"] != "exclude"}
    return records, cards


def semantic_input_hash(memory, sources):
    """Hash semantic records and the complete explicit source inventory.

    All record payload fields except review/currency/metadata are significant,
    including provenance, source references, links and scope. Record/source order
    and source membership order are not significant. Excluded membership is.
    """
    records, cards = _inputs(memory, sources)
    payload = [{key: value for key, value in records[rid].items() if key not in {"review", "currency", "metadata"}}
               for rid in sorted(records)]
    return digest({"records": payload, "sources": cards})


def semantic_template(memory, sources):
    """Return an incomplete checklist, never an automatically approved review."""
    records, cards = _inputs(memory, sources)
    return {"format": "portable-semantic-review", "schema_version": 1,
            "input_sha256": semantic_input_hash(memory, sources),
            "reviewer": {"kind": "same_model_second_pass", "label": _PLACEHOLDER},
            "covered_record_ids": sorted(records), "covered_source_ids": [card["id"] for card in cards],
            "cross_source_checked": False, "groups": []}


def _review_shape(review):
    require(isinstance(review, dict) and set(review) == _REVIEW_FIELDS, "Invalid semantic review fields")
    require(review["format"] == "portable-semantic-review" and type(review["schema_version"]) is int
            and review["schema_version"] == 1, "Unsupported semantic review")
    _hash(review["input_sha256"], "semantic input hash")
    _reviewer(review["reviewer"])
    _ids(review["covered_record_ids"], "covered_record_ids")
    _ids(review["covered_source_ids"], "covered_source_ids")
    require(type(review["cross_source_checked"]) is bool, "cross_source_checked must be boolean")
    require(isinstance(review["groups"], list), "Semantic groups must be a list")
    group_ids, record_ids = set(), set()
    for group in review["groups"]:
        require(isinstance(group, dict) and set(group) == _GROUP_FIELDS, "Invalid semantic group fields")
        _text(group["id"], "group.id")
        require(group["id"] not in group_ids, "Repeated semantic group ID")
        group_ids.add(group["id"])
        require(isinstance(group["relation"], str) and group["relation"] in {"equivalent", "conflict", "compatible"},
                "Invalid semantic relation")
        members = _ids(group["record_ids"], "group.record_ids")
        require(bool(members), "A semantic group cannot be empty")
        require(not members & record_ids, "A record occurs in more than one semantic group")
        record_ids.update(members)
        _text(group["note"], "group.note")
        if group["relation"] == "equivalent":
            require(len(members) >= 2, "Equivalent groups require at least two records")
            require(isinstance(group["canonical_id"], str) and group["canonical_id"] in members,
                    "Equivalent group canonical_id must identify a member")
        else:
            require(group["canonical_id"] is None, "Only equivalent groups may choose a canonical ID")
            if group["relation"] == "conflict":
                require(len(members) >= 2, "Conflict groups require at least two records")
    canonical(review)
    return record_ids


def semantic_quality_template(review):
    """Prepare a second-pass attestation tied to this exact review, as uncertain."""
    _review_shape(review)
    return {"format": "portable-semantic-quality", "schema_version": 1,
            "review_sha256": digest(review), "reviewer": {"kind": "same_model_second_pass", "label": _PLACEHOLDER},
            "decision": "uncertain", "note": "Проверить полноту охвата, смысловые совпадения, противоречия и области действия по источникам."}


def validate_semantic(memory, sources, review, quality):
    """Require a fresh, complete, model/human-attested review and quality pass.

    'supported' is supplied by the actual checker; this function authenticates no
    model identity and performs no semantic classification. Equivalent relations
    yield aliases only. Conflict relations yield issues without choosing winners.
    """
    records, cards = _inputs(memory, sources)
    grouped = _review_shape(review)
    require(review["input_sha256"] == semantic_input_hash(memory, sources), "Semantic review is stale for the supplied records or sources")
    require(set(review["covered_record_ids"]) == set(records), "Semantic review does not cover every nonexcluded record exactly")
    require(set(review["covered_source_ids"]) == {card["id"] for card in cards}, "Semantic review does not cover every source exactly")
    require(grouped == set(records), "Every nonexcluded record must occur in exactly one semantic group")
    require(review["cross_source_checked"] is True, "The actual cross-source semantic pass has not been completed")
    _reviewer(review["reviewer"], named=True)
    require(isinstance(quality, dict) and set(quality) == _QUALITY_FIELDS, "Invalid semantic quality fields")
    require(quality["format"] == "portable-semantic-quality" and type(quality["schema_version"]) is int
            and quality["schema_version"] == 1, "Unsupported semantic quality")
    _hash(quality["review_sha256"], "quality review hash")
    _reviewer(quality["reviewer"], named=True)
    require(isinstance(quality["decision"], str) and quality["decision"] in {"supported", "uncertain", "contradicted"},
            "Invalid semantic quality decision")
    _text(quality["note"], "quality.note")
    require(quality["review_sha256"] == digest(review), "Semantic quality is stale for this review")
    require(quality["decision"] == "supported", "Semantic quality check has not supported this review")
    canonical(quality)
    aliases, conflicts = {}, []
    for group in review["groups"]:
        ids = group["record_ids"]
        if group["relation"] in {"equivalent", "conflict"}:
            require(len({(records[rid]["kind"], records[rid]["scope"]) for rid in ids}) == 1,
                    "Equivalent/conflict groups must preserve the same exact record kind and scope")
        if group["relation"] == "equivalent":
            aliases.update({rid: group["canonical_id"] for rid in ids if rid != group["canonical_id"]})
        elif group["relation"] == "conflict":
            conflicts.append({"id": group["id"], "record_ids": sorted(ids), "note": group["note"]})
    return {"aliases": aliases, "conflicts": conflicts, "review_sha256": digest(review)}
