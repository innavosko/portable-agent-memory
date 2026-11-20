"""One aggregate review owned by a coordinator, with a mandatory export receipt.

The model supplies semantic conclusions and a second pass. Python verifies their
coverage/freshness and gates export; it does not call a model or authenticate the
named reviewer/user. Original records remain intact behind a canonical view.
"""
import copy
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from .errors import ValidationError
from .model import confirm, digest, empty_memory, now, record_digest, require, validate_memory
from .profile_audit import audit_profile, prepare_memory
from .profile_session import (apply_profile_event, create_profile, profile_result,
                              profile_view, refresh_profile)
from .semantic_review import (semantic_input_hash, semantic_template, validate_semantic)


def _text(value, label):
    require(isinstance(value, str) and bool(value.strip()), label + " must be nonempty text")


def _user_meaning(record):
    return digest({k: v for k, v in record.items() if k not in {"review", "currency", "sources"}})


def _projection(memory):
    result = copy.deepcopy(memory)
    result["records"] = sorted((r for r in result["records"] if r["review"]["decision"] != "exclude"), key=lambda r: r["id"])
    result.setdefault("metadata", {}).pop("last_import", None)
    return result


def _sources(state, memory=None):
    memory = memory if memory is not None else state["memory"]
    ids = {r["id"] for r in memory["records"] if r["review"]["decision"] != "exclude"}
    return [{**copy.deepcopy(s), "record_ids": sorted(set(s["record_ids"]) & ids)}
            for s in sorted(state["sources"], key=lambda s: s["id"])]


def _check_sources(sources):
    require(isinstance(sources, list), "Source inventory is required")
    seen = set()
    for source in sources:
        require(isinstance(source, dict) and set(source) == {"id", "label", "status", "record_ids", "snapshot_sha256", "note"}, "Invalid common source card")
        for name in ("id", "label", "note"):
            _text(source[name], "source." + name)
        require(source["id"] not in seen, "Duplicate common source ID")
        seen.add(source["id"])
        require(source["status"] in {"provided", "pending", "unavailable", "excluded"}, "Invalid source status")
        require(isinstance(source["record_ids"], list) and all(isinstance(x, str) for x in source["record_ids"])
                and len(source["record_ids"]) == len(set(source["record_ids"])), "Invalid source record IDs")
        fingerprint = source["snapshot_sha256"]
        require(fingerprint is None or (isinstance(fingerprint, str) and len(fingerprint) == 64
                and all(c in "0123456789abcdef" for c in fingerprint)), "Invalid source fingerprint")
        if source["status"] == "provided":
            require(fingerprint is not None, "Provided source needs a snapshot fingerprint")


def create_common(coordinator, previous=None, allow_partial=False, scope_note=None):
    _text(coordinator, "coordinator")
    require(type(allow_partial) is bool, "allow_partial must be boolean")
    if allow_partial:
        _text(scope_note, "Record the user's choice of partial scope")
    memory = copy.deepcopy(previous) if previous is not None else empty_memory()
    validate_memory(memory)
    # A previous, explicitly trusted profile is distinct from newly supplied data.
    memory.setdefault("metadata", {}).pop("common_profile", None)
    state = {"format": "portable-common-profile", "schema_version": 1, "id": str(uuid.uuid4()),
             "coordinator": coordinator, "revision": 0, "memory": memory, "previous": copy.deepcopy(memory),
             "sources": [], "profile": None, "semantic": None, "semantic_quality": None, "user_decisions": {},
             "allow_partial": allow_partial, "scope_note": scope_note, "created_at": now()}
    if previous is not None:
        state["sources"].append({"id": "previous-profile", "label": "Previously confirmed profile supplied explicitly",
            "status": "provided", "record_ids": [r["id"] for r in memory["records"]],
            "snapshot_sha256": digest(memory), "note": "Caller supplied this as an actually confirmed previous profile; not newly extracted facts."})
    return state


def validate_common(state):
    require(isinstance(state, dict) and state.get("format") == "portable-common-profile" and state.get("schema_version") == 1,
            "Invalid common profile state")
    _text(state.get("coordinator"), "coordinator")
    require(type(state.get("revision")) is int and state["revision"] >= 0, "Invalid common revision")
    require(type(state.get("allow_partial")) is bool, "Invalid partial scope choice")
    if state["allow_partial"]:
        _text(state.get("scope_note"), "Partial scope requires the user's recorded choice")
    validate_memory(state["memory"])
    validate_memory(state["previous"])
    _check_sources(state["sources"])


def _edit(state, expected_revision, actor):
    validate_common(state)
    require(actor == state["coordinator"], "Only the designated coordinator may update this common profile")
    require(type(expected_revision) is int and expected_revision == state["revision"], "Stale common profile revision; read the current store")
    return copy.deepcopy(state)


def add_source(state, source, memory=None, *, expected_revision, actor):
    updated = _edit(state, expected_revision, actor)
    require(isinstance(source, dict) and set(source) <= {"id", "label", "status", "note", "excluded_by_user"}
            and {"id", "label", "status", "note"} <= set(source), "Source requires id, label, status and note")
    for key in ("id", "label", "note"):
        _text(source[key], "source." + key)
    require(source["status"] in {"provided", "pending", "unavailable", "excluded"}, "Invalid source status")
    if source["status"] == "excluded":
        require(source.get("excluded_by_user") is True, "Excluding a source requires the actual user's choice")
    old = next((s for s in state["sources"] if s["id"] == source["id"]), None)
    if memory is not None:
        require(source["status"] == "provided", "Only a provided source may add records")
        validate_memory(memory, allow_stale_review=True)
    else:
        require(source["status"] != "provided", "Provide the source memory, even if it has zero records")
    card = {k: source[k] for k in ("id", "label", "status", "note")}
    card.update(record_ids=sorted(set(old["record_ids"] if old else []) | {r["id"] for r in (memory or {}).get("records", [])}),
                snapshot_sha256=digest(memory) if memory is not None else None)
    if card == old:
        return updated
    if state.get("semantic") is not None:
        updated["previous"] = copy.deepcopy(state["memory"])
    if memory is not None:
        known = {r["id"]: r for r in updated["memory"]["records"]}
        owned = set(old["record_ids"]) if old else set()
        for incoming in memory["records"]:
            current = known.get(incoming["id"])
            def content(r):
                return {k: v for k, v in r.items() if k not in {"review", "currency"}}
            shared = any(s["id"] not in {source["id"], "previous-profile"} and s["status"] != "excluded"
                         and incoming["id"] in s["record_ids"] for s in state["sources"])
            if current is not None and shared:
                require(content(current) == content(incoming), "A shared record ID changed in one source; preserve both versions under distinct IDs")
            # Shared IDs may repeat identical evidence. Changed claims from another
            # source need a distinct ID so they cannot overwrite known facts.
            if current is not None and incoming["id"] not in owned:
                require(content(current) == content(incoming), "Another source reused a record ID for different content; keep distinct IDs for comparison")
                continue
            record = copy.deepcopy(incoming)
            record["review"] = {"decision": "pending"}
            known[record["id"]] = record
        merged = copy.deepcopy(updated["memory"])
        merged["records"] = list(known.values())
        for key in ("read", "unavailable", "excluded"):
            merged["coverage"][key] = list(dict.fromkeys(merged["coverage"][key] + memory["coverage"][key]))
        updated["memory"] = prepare_memory(merged, state["memory"])
    updated["sources"] = [s for s in updated["sources"] if s["id"] != card["id"]] + [card]
    if card["status"] == "excluded" and old is not None:
        other_ids = {rid for s in updated["sources"] if s["id"] != card["id"] and s["status"] != "excluded" for rid in s["record_ids"]}
        exclusive = set(card["record_ids"]) - other_ids
        decisions = [{"id": r["id"], "content_sha256": record_digest(r), "decision": "exclude"}
                     for r in updated["memory"]["records"] if r["id"] in exclusive]
        if decisions:
            updated["memory"] = confirm(updated["memory"], {"decisions": decisions})
            updated["previous"] = copy.deepcopy(updated["memory"])
    updated["semantic"] = updated["semantic_quality"] = None
    updated["revision"] += 1
    validate_common(updated)
    return updated


def common_input(state):
    validate_common(state)
    return {"memory": copy.deepcopy(state["memory"]), "sources": _sources(state),
            "coordinator": state["coordinator"], "revision": state["revision"],
            "previous_profile_session": copy.deepcopy(state["profile"]),
            "instruction": "Compare the entire combined profile, including cross-source paraphrases and conflicts. Preserve personal/work/project scopes. Reuse actual unchanged user decisions."}


def common_template(state):
    validate_common(state)
    return semantic_template(state["memory"], _sources(state))


def _analysis(state):
    return validate_semantic(state["memory"], _sources(state), state["semantic"], state["semantic_quality"])


def _inherit_decisions(state, analysis):
    profile = state["profile"]
    records = {r["id"]: r for r in profile["memory"]["records"]}
    prior = {r["id"]: r for r in state["previous"]["records"]}
    audit = audit_profile(profile["memory"], profile["assessment"], profile.get("quality"),
                          semantic_aliases=analysis["aliases"])
    ready = {r["id"] for r in audit["records"] if r["quality_ready"]}
    inherited = {}
    explicit_pending = {rid for rid, choice in state.get("user_decisions", {}).items() if rid in records
                        and choice["decision"] == "pending" and choice["meaning_sha256"] == _user_meaning(records[rid])}
    for rid, record in records.items():
        if record["review"]["decision"] != "pending" or rid not in ready or rid in explicit_pending:
            continue
        old = prior.get(rid)
        # New evidence for exactly the same user-facing content is model work,
        # not a reason to repeat the user's already recorded decision.
        def meaning(r):
            return {k: v for k, v in r.items() if k not in {"review", "currency", "sources"}}
        if old and old["review"]["decision"] in {"active", "historical", "exclude"} and meaning(old) == meaning(record):
            inherited[rid] = (old["review"]["decision"], rid)
    for alias, target in analysis["aliases"].items():
        canonical = records[target]
        decision = inherited.get(target, (canonical["review"]["decision"], target))[0]
        if alias not in explicit_pending and alias not in inherited and records[alias]["review"]["decision"] == "pending" and alias in ready and decision in {"active", "historical"}:
            inherited[alias] = (decision, target)
    if inherited:
        profile["memory"] = confirm(profile["memory"], {"decisions": [
            {"id": rid, "content_sha256": record_digest(records[rid]), "decision": decision}
            for rid, (decision, _) in inherited.items()]})
        for record in profile["memory"]["records"]:
            if record["id"] in inherited:
                record["review"]["inherited_from"] = inherited[record["id"]][1]
                record["review"]["basis"] = "Previously confirmed meaning; current evidence and equivalence checked in two model passes"
    state["memory"] = copy.deepcopy(profile["memory"])


def review_common(state, assessment, quality, semantic, semantic_quality, *, expected_revision, actor):
    updated = _edit(state, expected_revision, actor)
    analysis = validate_semantic(state["memory"], _sources(state), semantic, semantic_quality)
    if state["profile"] is None:
        profile = create_profile(state["memory"], assessment, quality, previous=state["previous"])
    else:
        prior_session = copy.deepcopy(state["profile"])
        actual = {r["id"]: r for r in state["memory"]["records"]}
        excluded = []
        for old in prior_session["memory"]["records"]:
            current = actual.get(old["id"])
            if current is not None and current["review"]["decision"] in {"exclude", "pending"}:
                # Coordinator source exclusion/user deferral is newer than this
                # older profile-session snapshot; do not resurrect its approval.
                old["review"] = {"decision": "pending"}
                if current["review"]["decision"] == "exclude":
                    excluded.append({"id": old["id"], "content_sha256": record_digest(old), "decision": "exclude"})
        if excluded:
            prior_session["memory"] = confirm(prior_session["memory"], {"decisions": excluded})
        profile = refresh_profile(prior_session, state["memory"], assessment, quality,
                                  expected_revision=prior_session["revision"])
    profile["semantic_aliases"] = copy.deepcopy(analysis["aliases"])
    profile["semantic_conflicts"] = copy.deepcopy(analysis["conflicts"])
    updated.update(profile=profile, semantic=copy.deepcopy(semantic), semantic_quality=copy.deepcopy(semantic_quality))
    _inherit_decisions(updated, analysis)
    updated["revision"] += 1
    # Review may restore decisions, never change the meaning checked by the model.
    _analysis(updated)
    validate_common(updated)
    return updated


def common_event(state, event, *, expected_revision, actor):
    updated = _edit(state, expected_revision, actor)
    _analysis(state)
    require(state["profile"] is not None, "Run the common review first")
    updated["profile"] = apply_profile_event(state["profile"], event, expected_revision=state["profile"]["revision"])
    updated["memory"] = copy.deepcopy(updated["profile"]["memory"])
    # A new user decision supersedes all previously inherited approvals. Never
    # turn an explicit 'pending' back into the old 'active' during inheritance.
    if event["type"] == "review":
        choices = {d["id"]: d["decision"] for d in event["data"]["decisions"]}
        for record in updated["memory"]["records"]:
            if record["id"] in choices:
                updated.setdefault("user_decisions", {})[record["id"]] = {"meaning_sha256": _user_meaning(record), "decision": choices[record["id"]]}
        inherited_changes = []
        for record in updated["memory"]["records"]:
            target = record["review"].get("inherited_from")
            if target in choices and record["id"] not in choices:
                inherited_changes.append({"id": record["id"], "content_sha256": record_digest(record), "decision": choices[target]})
        if inherited_changes:
            updated["memory"] = confirm(updated["memory"], {"decisions": inherited_changes})
            updated["profile"]["memory"] = copy.deepcopy(updated["memory"])
    updated["previous"] = copy.deepcopy(updated["memory"])
    try:
        _inherit_decisions(updated, _analysis(updated))
    except ValidationError:
        # An explicit exclusion changes the reviewed membership. The next model
        # pass rechecks the new set; already confirmed answers remain in state.
        updated["semantic"] = updated["semantic_quality"] = None
    updated["revision"] += 1
    validate_common(updated)
    return updated


def common_view(state):
    validate_common(state)
    result = {"id": state["id"], "coordinator": state["coordinator"], "revision": state["revision"],
              "sources": copy.deepcopy(state["sources"]), "allow_partial": state["allow_partial"],
              "status": "needs_common_review", "export_allowed": False, "question": None,
              "next_action": "The coordinator must review the entire combined input; do not restart the user interview."}
    try:
        analysis = _analysis(state)
    except (ValidationError, TypeError, KeyError):
        return result
    result.update(status="reviewed", semantic_aliases=analysis["aliases"], semantic_conflicts=analysis["conflicts"],
                  profile=profile_view(state["profile"]))
    result["question"] = result["profile"]["question"]
    try:
        output = export_review(state)
        result.update(status=output["review"]["readiness"], export_allowed=True,
                      next_action="Export the reviewed version; native setup is a separate step.")
    except ValidationError as exc:
        result.update(status="needs_resolution", next_action=str(exc))
    return result


def _unresolved_conflicts(memory, conflicts):
    records = {r["id"]: r for r in memory["records"]}
    return [group for group in conflicts if len([rid for rid in group["record_ids"]
            if records[rid]["review"]["decision"] in {"active", "pending"}]) > 1]


def export_review(state):
    validate_common(state)
    analysis = _analysis(state)
    require(state["profile"] is not None, "Common profile audit is missing")
    result = profile_result(state["profile"])
    memory = copy.deepcopy(result["memory"])
    unresolved = _unresolved_conflicts(memory, analysis["conflicts"])
    conflicted = {rid for group in unresolved for rid in group["record_ids"]}
    for record in memory["records"]:
        if record["id"] in conflicted and record["review"]["decision"] == "active":
            record["review"] = {"decision": "pending"}
    missing_sources = [s["id"] for s in state["sources"] if s["status"] in {"pending", "unavailable"}]
    pending = [r["id"] for r in memory["records"] if r["review"]["decision"] == "pending"]
    partial = bool(missing_sources or pending or unresolved or result["report"]["agent_tasks"])
    require(not partial or state["allow_partial"], "Resolve the remaining review tasks or record the user's explicit partial scope; no unchecked fact becomes active")
    sources = _sources(state, memory)
    memory.setdefault("metadata", {}).pop("common_review_required", None)
    memory.setdefault("metadata", {})["common_profile"] = {"coordinator": state["coordinator"], "sources": sources,
        "aliases": copy.deepcopy(analysis["aliases"]), "readiness": "partial" if partial else "complete_within_supplied_sources",
        "semantic_review_sha256": digest(state["semantic"])}
    audit = audit_profile(memory, state["profile"]["assessment"], state["profile"].get("quality"), semantic_aliases=analysis["aliases"])
    by_id = {r["id"]: r for r in audit["records"]}
    review = {"format": "portable-common-profile-review", "schema_version": 1,
        "coordinator": state["coordinator"], "sources": sources, "semantic": copy.deepcopy(state["semantic"]),
        "semantic_quality": copy.deepcopy(state["semantic_quality"]), "allow_partial": state["allow_partial"],
        "scope_note": state["scope_note"], "readiness": memory["metadata"]["common_profile"]["readiness"],
        "record_checks": [{"id": r["id"], "content_sha256": record_digest(r),
                           "quality_ready": by_id[r["id"]]["quality_ready"], "evidence_status": by_id[r["id"]]["evidence_status"]}
                          for r in memory["records"] if r["review"]["decision"] != "exclude"],
        "memory_sha256": digest(_projection(memory)), "created_at": now(),
        "limitations": ["Semantic conclusions are supplied model/human attestations, not independently executed by Python.",
                        "The review covers supplied sources, not unknown accounts or native setup."]}
    validate_export_review(memory, review)
    return {"memory": memory, "review": review, "report": result["report"]}


def common_result(state):
    return export_review(state)


def validate_export_review(memory, review):
    validate_memory(memory)
    require(isinstance(review, dict) and review.get("format") == "portable-common-profile-review" and review.get("schema_version") == 1,
            "A current common profile review is mandatory before export/import; run the coordinator review")
    _check_sources(review.get("sources"))
    _text(review.get("coordinator"), "review.coordinator")
    require(type(review.get("allow_partial")) is bool, "Invalid partial review policy")
    if review["allow_partial"]:
        _text(review.get("scope_note"), "Missing actual partial scope choice")
    require(review.get("memory_sha256") == digest(_projection(memory)), "Common review is stale: profile, decisions or scope changed")
    projected = _projection(memory)
    analysis = validate_semantic(projected, review["sources"], review.get("semantic"), review.get("semantic_quality"))
    expected_meta = {"coordinator": review["coordinator"], "sources": review["sources"], "aliases": analysis["aliases"],
                     "readiness": review.get("readiness"), "semantic_review_sha256": digest(review["semantic"])}
    require(memory.get("metadata", {}).get("common_profile") == expected_meta, "Common profile metadata differs from its checked review")
    records = {r["id"]: r for r in projected["records"]}
    checks = review.get("record_checks")
    require(isinstance(checks, list) and len(checks) == len(records), "Incomplete profile quality checks")
    seen = set()
    for check in checks:
        require(isinstance(check, dict) and set(check) == {"id", "content_sha256", "quality_ready", "evidence_status"}, "Invalid profile quality check")
        rid = check["id"]
        require(isinstance(rid, str) and rid in records and rid not in seen, "Missing/repeated profile record check")
        seen.add(rid)
        require(check["content_sha256"] == record_digest(records[rid]) and type(check["quality_ready"]) is bool,
                "Stale profile quality check")
        if records[rid]["review"]["decision"] in {"active", "historical"}:
            require(check["quality_ready"], "An active fact has not passed profile quality review")
    unresolved = _unresolved_conflicts(projected, analysis["conflicts"])
    for conflict in unresolved:
        require(not any(records[rid]["review"]["decision"] == "active" for rid in conflict["record_ids"]),
                "An unresolved semantic conflict cannot contain an active rule")
    partial = bool(unresolved or any(r["review"]["decision"] == "pending" for r in records.values())
                   or any(s["status"] in {"pending", "unavailable"} for s in review["sources"]))
    require(review["readiness"] in {"partial", "complete_within_supplied_sources"}, "Invalid readiness")
    require(not partial or (review["allow_partial"] and review["readiness"] == "partial"), "Partial profile cannot be declared complete")
    return analysis


class CommonStore:
    """One local authority; separate SaaS copies do not synchronize this store."""
    def __init__(self, path):
        self.path = Path(path)

    @classmethod
    def create(cls, path, state, migration_store=None):
        validate_common(state)
        path = Path(path)
        require(not path.exists(), "Common store already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        if migration_store is not None:
            state = copy.deepcopy(state)
            state["migration_store"] = str(Path(migration_store).absolute())
            state["migration_sha256"] = None
        import json
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE common_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), body TEXT NOT NULL)")
            db.execute("INSERT INTO common_state VALUES (1, ?)", (json.dumps(state, ensure_ascii=False),))
        return cls(path)

    def load(self):
        import json
        require(self.path.is_file(), "Common store does not exist")
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT body FROM common_state WHERE singleton=1").fetchall()
        require(len(rows) == 1, "Common store is corrupt")
        state = json.loads(rows[0][0])
        validate_common(state)
        return state

    def update(self, operation):
        import json
        require(self.path.is_file(), "Common store does not exist")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM common_state WHERE singleton=1").fetchone()
            require(row is not None, "Common store is corrupt")
            result = operation(json.loads(row[0]))
            validate_common(result)
            db.execute("UPDATE common_state SET body=? WHERE singleton=1", (json.dumps(result, ensure_ascii=False),))
        return result

    def _migration(self, state):
        if not state.get("migration_store"):
            return None, None
        from .migration import MigrationStore
        migration = MigrationStore(state["migration_store"]).load()
        fingerprint = digest({"sources": migration["sources"], "jobs": migration["jobs"]})
        return migration, fingerprint

    def ensure_current(self, state):
        _, fingerprint = self._migration(state)
        require(fingerprint is None or fingerprint == state.get("migration_sha256"),
                "The source map changed: run common-sync and recheck the combined profile before exporting")

    def sync(self, expected_revision, actor):
        def operation(state):
            updated = _edit(state, expected_revision, actor)
            migration, fingerprint = self._migration(state)
            require(migration is not None, "No migration store is linked")
            if fingerprint == state.get("migration_sha256"):
                return updated
            known = {s["id"]: s for s in updated["sources"]}
            prior_fingerprints = state.get("migration_source_fingerprints", {})
            current_fingerprints = {source["id"]: digest({"source": source, "jobs": [j for j in migration["jobs"] if j["source_id"] == source["id"]]})
                                    for source in migration["sources"]}
            for source in migration["sources"]:
                if source["id"] not in known:
                    known[source["id"]] = {"id": source["id"], "label": source["label"],
                        "status": "pending" if source["selected"] else "excluded", "record_ids": [], "snapshot_sha256": None,
                        "note": "Source map selection: supply its actual records for common review." if source["selected"] else "Not selected in the user source map."}
                elif not source["selected"]:
                    known[source["id"]].update(status="excluded", snapshot_sha256=None,
                        note="User source-map selection excludes this source from the current transfer.")
                elif source["selected"] and current_fingerprints[source["id"]] != prior_fingerprints.get(source["id"]):
                    known[source["id"]].update(status="pending", snapshot_sha256=None,
                        note="The mapped source changed; collect/reconcile its current data before treating it as checked.")
            updated["sources"] = list(known.values())
            excluded_ids = {rid for s in updated["sources"] if s["status"] == "excluded" for rid in s["record_ids"]}
            retained_ids = {rid for s in updated["sources"] if s["status"] != "excluded" for rid in s["record_ids"]}
            choices = [{"id": r["id"], "content_sha256": record_digest(r), "decision": "exclude"}
                       for r in updated["memory"]["records"] if r["id"] in excluded_ids - retained_ids]
            if choices:
                updated["memory"] = confirm(updated["memory"], {"decisions": choices})
                updated["previous"] = copy.deepcopy(updated["memory"])
            updated["migration_sha256"] = fingerprint
            updated["migration_source_fingerprints"] = current_fingerprints
            updated["semantic"] = updated["semantic_quality"] = None
            updated["revision"] += 1
            return updated
        return self.update(operation)

    def export_review(self):
        state = self.load()
        self.ensure_current(state)
        return export_review(state)

    def export_package(self, memory, output, *, assets=None):
        # Hold the common authority while checking and writing: another local
        # source update cannot slip between the review and the exported bytes.
        import json
        from .package import export_package
        require(self.path.is_file(), "Common store does not exist")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM common_state WHERE singleton=1").fetchone()
            require(row is not None, "Common store is corrupt")
            state = json.loads(row[0])
            self.ensure_current(state)
            result = export_review(state)
            require(_projection(memory) == _projection(result["memory"]), "This file is not the latest common result; run common-result again")
            return export_package(memory, output, assets=assets, review=result["review"])
