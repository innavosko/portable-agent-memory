"""Identify source datasets independently of the apps that expose them.

The caller supplies identity evidence from the user or an actually observed
account/workspace. This module validates that declaration; it does not discover
accounts, authenticate evidence, scan files, or infer identity from an email,
display title, path, product name, or matching contents.

An identity is namespace + account_id + workspace_id + dataset kind + resource_id.
For a shared local repository, use namespace='filesystem' and the same owner,
device/workspace, and repository resource identifiers for all observing agents.
The agent/product/interface belongs to a surface, never to dataset identity.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from pathlib import PurePosixPath

from .errors import ValidationError

IDENTITY_FIELDS = ("namespace", "account_id", "workspace_id", "resource_id")
SURFACE_FIELDS = ("agent_id", "product", "interface")
OBSERVATION_FIELDS = {"id", "label", "kind", "identity", "identity_evidence",
                      "surface", "selected", "input"}
INPUT_KINDS = {"archive", "memory", "folder"}


def _require(condition, message):
    if not condition:
        raise ValidationError(message)


def _text(value, name):
    _require(isinstance(value, str) and bool(value.strip()), f"{name}: nonempty text required")
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:32]


def _identity(value, *, complete=False):
    if value is None:
        _require(not complete, "A complete source identity is required")
        return None
    _require(isinstance(value, dict), "identity must be an object or null")
    _require(not set(value) - set(IDENTITY_FIELDS), "Unknown identity field; use namespace for the dataset provider")
    result = {}
    for key in IDENTITY_FIELDS:
        item = value.get(key)
        if item is not None:
            _text(item, f"identity.{key}")
        elif complete:
            raise ValidationError(f"identity.{key}: a confirmed identifier is required")
        result[key] = item
    return result


def _confirmed(observation):
    value = observation["identity"]
    return (value is not None and all(value[key] is not None for key in IDENTITY_FIELDS)
            and observation["identity_evidence"] is not None)


def _normalize_observation(value):
    _require(isinstance(value, dict), "Each observation must be an object")
    _require(set(value) == OBSERVATION_FIELDS, "Observation fields must be id, label, kind, identity, identity_evidence, surface, selected, input")
    observation = {key: _text(value.get(key), key) for key in ("id", "label", "kind")}
    observation["identity"] = _identity(value.get("identity"))
    evidence = value.get("identity_evidence")
    if evidence is not None:
        _text(evidence, "identity_evidence")
    observation["identity_evidence"] = evidence
    surface = value.get("surface")
    _require(isinstance(surface, dict) and set(surface) == set(SURFACE_FIELDS),
             "surface requires agent_id, product, interface")
    observation["surface"] = {key: _text(surface[key], f"surface.{key}") for key in SURFACE_FIELDS}
    _require(type(value.get("selected")) is bool, "selected must be a boolean")
    observation["selected"] = value["selected"]
    candidate = value.get("input")
    if candidate is not None:
        _require(isinstance(candidate, dict) and {"kind", "path"} <= set(candidate)
                 and not set(candidate) - {"kind", "path", "include"},
                 "input requires kind, path and optional folder include, or null")
        _require(candidate["kind"] in INPUT_KINDS, "Unsupported input kind")
        normalized_input = {"kind": candidate["kind"], "path": _text(candidate["path"], "input.path")}
        if "include" in candidate:
            _require(candidate["kind"] == "folder", "include is supported only for folder inputs")
            includes = candidate["include"]
            _require(isinstance(includes, list), "input.include must be a list of relative paths")
            paths = []
            for item in includes:
                _text(item, "input.include entry")
                relative = item.replace("\\", "/")
                parts = PurePosixPath(relative).parts
                _require(parts and not relative.startswith("/") and ":" not in relative
                         and "\x00" not in relative and ".." not in parts and relative not in {".", "./"},
                         "input.include entries must stay inside the selected folder")
                paths.append(PurePosixPath(*parts).as_posix())
            normalized_input["include"] = sorted(set(paths))
        candidate = normalized_input
    observation["input"] = candidate
    return observation


def _merge_observation_identity(variants):
    """A stable observation ID may gain evidence, never switch its dataset."""
    _require(len({item["kind"] for item in variants}) == 1,
             "The same observation ID cannot identify different dataset kinds")
    for field in IDENTITY_FIELDS:
        known = {item["identity"][field] for item in variants
                 if item["identity"] is not None and item["identity"][field] is not None}
        _require(len(known) <= 1, f"Conflicting identity.{field} for observation {variants[0]['id']}")
    confirmed = [item["identity"] for item in variants if _confirmed(item)]
    return confirmed[0] if confirmed else None


def _make_source(observations, identity):
    observations = sorted(observations, key=_canonical)
    kind = observations[0]["kind"]
    observation_ids = sorted({item["id"] for item in observations})
    if identity is None:
        _require(len(observation_ids) == 1, "Unresolved observations must remain separate")
        source_id = "unresolved-" + _digest(observation_ids[0])
    else:
        source_id = "src-" + _digest({"kind": kind, "identity": identity})
    labels = sorted({item["label"] for item in observations})
    surfaces = {_canonical(item["surface"]): item["surface"] for item in observations}
    inputs = {}
    for observation in observations:
        candidate = observation["input"]
        if candidate is not None:
            key = _canonical(candidate)
            inputs.setdefault(key, {**candidate, "observation_ids": set()})
            inputs[key]["observation_ids"].add(observation["id"])
    input_candidates = [{**inputs[key], "observation_ids": sorted(inputs[key]["observation_ids"])}
                        for key in sorted(inputs)]
    input_status = "missing" if not input_candidates else ("ready" if len(input_candidates) == 1 else "choice_required")
    selected = any(item["selected"] for item in observations)
    evidence = {(_canonical({"observation_id": item["id"], "evidence": item["identity_evidence"]})):
                {"observation_id": item["id"], "evidence": item["identity_evidence"]}
                for item in observations if item["identity_evidence"] is not None}
    blockers = []
    if not selected:
        blockers.append("not_selected")
    if identity is None:
        blockers.append("identity_unresolved")
    if input_status == "missing":
        blockers.append("input_missing")
    elif input_status == "choice_required":
        blockers.append("input_choice_required")
    return {
        "id": source_id, "kind": kind, "label": labels[0], "labels": labels,
        "identity": copy.deepcopy(identity), "identity_status": "verified" if identity is not None else "unresolved",
        "identity_evidence": [evidence[key] for key in sorted(evidence)],
        "observation_ids": observation_ids, "observations": copy.deepcopy(observations),
        "surfaces": [surfaces[key] for key in sorted(surfaces)], "selected": selected,
        "inputs": input_candidates,
        "input": ({key: copy.deepcopy(value) for key, value in input_candidates[0].items()
                   if key != "observation_ids"} if len(input_candidates) == 1 else None),
        "input_status": input_status, "collection_ready": not blockers,
        "collection_blockers": blockers,
    }


def normalize_sources(observations: list) -> list:
    """Return deterministic dataset inventory, preserving all observations.

    Exact re-registration is idempotent. Repeated observation IDs may carry new
    labels, surfaces, evidence, or input candidates for the SAME dataset. Their
    variants are retained; conflicting identity fields or kinds are rejected.
    Different unknown observations never merge, even if paths/titles/emails match.
    Identity values are compared exactly; this function does not case-fold IDs or
    normalize paths that may belong to a different machine or cloud provider.
    """
    _require(isinstance(observations, list), "observations must be a list")
    unique = {}
    for observation in observations:
        normalized = _normalize_observation(observation)
        unique[_canonical(normalized)] = normalized
    by_observation_id = defaultdict(list)
    for normalized in unique.values():
        by_observation_id[normalized["id"]].append(normalized)
    groups, identities = defaultdict(list), {}
    for observation_id in sorted(by_observation_id):
        variants = by_observation_id[observation_id]
        identity = _merge_observation_identity(variants)
        key = ("verified", _canonical({"kind": variants[0]["kind"], "identity": identity})) if identity is not None else ("unresolved", observation_id)
        groups[key].extend(variants)
        identities[key] = identity
    sources = [_make_source(groups[key], identities[key]) for key in sorted(groups)]
    return sorted(sources, key=lambda source: source["id"])


def validate_source(source):
    """Validate all derived fields against the underlying observations.

    Change observations and normalize again to modify selection or input choices;
    merely setting collection_ready=True cannot bypass missing identity evidence.
    """
    _require(isinstance(source, dict), "source must be an object")
    _require(isinstance(source.get("observations"), list) and source["observations"],
             "source must retain its observations")
    rebuilt = normalize_sources(source["observations"])
    _require(len(rebuilt) == 1 and rebuilt[0] == source,
             "Source inventory fields do not match observations; normalize again")


def resolve_identity(sources: list, source_id: str, identity: dict, evidence: str) -> list:
    """Resolve one source using explicit confirmed identifiers and evidence.

    Can merge an unresolved source into an already-known dataset. Observation IDs,
    surfaces, inputs, and selection survive the merge. A verified identity cannot
    be reassigned to a different dataset. Existing non-null identity fragments
    must agree with the clarification, so a stale observation ID is not reused.
    """
    _require(isinstance(sources, list), "sources must be a list")
    _text(source_id, "source_id")
    resolved = _identity(identity, complete=True)
    _text(evidence, "identity evidence")
    matches = []
    source_ids = set()
    for source in sources:
        validate_source(source)
        _require(source["id"] not in source_ids, "Duplicate canonical source ID")
        source_ids.add(source["id"])
        if source["id"] == source_id:
            matches.append(source)
    _require(len(matches) == 1, "Source ID not found")
    target = matches[0]
    if target["identity"] is not None:
        _require(target["identity"] == resolved, "Cannot change a verified source identity")
        return copy.deepcopy(sources)
    observations = []
    for source in sources:
        for original in source["observations"]:
            observation = copy.deepcopy(original)
            if source["id"] == source_id:
                if observation["identity"] is not None:
                    for field, value in observation["identity"].items():
                        _require(value is None or value == resolved[field],
                                 f"Resolved identity conflicts with prior {field}; use a new observation for another dataset")
                observation["identity"] = copy.deepcopy(resolved)
                observation["identity_evidence"] = evidence
            observations.append(observation)
    return normalize_sources(observations)
