"""Resumable profile review; user decisions are separate from model quality checks.

These JSON snapshots have one designated owner. Copying a snapshot does not
coordinate concurrent cloud agents or grant access to native account memory.
"""
import copy
import json
import uuid

from .model import canonical, confirm, digest, now, require, validate_memory
from .profile_audit import audit_profile, prepare_memory


def _merge_memory(previous, incoming):
    validate_memory(incoming, allow_stale_review=True)
    if previous is None:
        return copy.deepcopy(incoming)
    validate_memory(previous)
    merged = copy.deepcopy(previous)
    merged.setdefault("metadata", {}).update(copy.deepcopy(incoming.get("metadata", {})))
    records = {record["id"]: record for record in merged["records"]}
    records.update({record["id"]: copy.deepcopy(record) for record in incoming["records"]})
    merged["records"] = list(records.values())
    for key in ("read", "unavailable", "excluded"):
        merged["coverage"][key] = list(dict.fromkeys(previous["coverage"][key] + incoming["coverage"][key]))
    return merged


def _merge_assessment(previous, incoming):
    result = copy.deepcopy(incoming)
    for field, key in (("sources", "id"), ("claims", "record_id"), ("coverage", "category")):
        require(isinstance(incoming.get(field), list), f"assessment.{field} must be a list")
        current = {item[key]: copy.deepcopy(item) for item in previous.get(field, [])}
        identifiers = [item.get(key) if isinstance(item, dict) else None for item in incoming[field]]
        require(None not in identifiers and len(set(identifiers)) == len(identifiers), f"Duplicate or invalid {field}")
        current.update({item[key]: copy.deepcopy(item) for item in incoming[field]})
        result[field] = list(current.values())
    return result


def _merge_quality(previous, incoming):
    if incoming is None:
        return copy.deepcopy(previous)
    if previous is None:
        return copy.deepcopy(incoming)
    require(isinstance(incoming.get("checks"), list), "quality.checks must be a list")
    result = copy.deepcopy(incoming)
    # A merged file has no single reviewer. Attribute known checks individually;
    # unknown prior authors must not inherit the latest pass's global name.
    result.pop("reviewer", None)
    def attributed(item, parent):
        value = copy.deepcopy(item)
        if "reviewer" not in value and parent.get("reviewer") is not None:
            value["reviewer"] = copy.deepcopy(parent["reviewer"])
        return value
    checks = {item["record_id"]: attributed(item, previous) for item in previous["checks"]}
    ids = [item.get("record_id") if isinstance(item, dict) else None for item in incoming["checks"]]
    require(None not in ids and len(set(ids)) == len(ids), "Duplicate or invalid quality checks")
    checks.update({item["record_id"]: attributed(item, incoming) for item in incoming["checks"]})
    result["checks"] = list(checks.values())
    return result


def _prepare_snapshot(memory, previous):
    # Extraction output cannot sign itself off. Reuse only the actual prior
    # profile supplied separately, or decisions already made in this session.
    incoming = copy.deepcopy(memory)
    for record in incoming["records"]:
        record["review"] = {"decision": "pending"}
    return prepare_memory(incoming, previous)


def create_profile(memory, assessment, quality=None, previous=None):
    merged = _merge_memory(previous, memory)
    prepared = _prepare_snapshot(merged, previous)
    audit_profile(prepared, assessment, quality, previous)
    state = {"format": "portable-profile-session", "schema_version": 1,
             "id": str(uuid.uuid4()), "revision": 0, "created_at": now(),
             "memory": prepared, "assessment": copy.deepcopy(assessment),
             "quality": copy.deepcopy(quality), "previous": copy.deepcopy(previous),
             "ledger": {}, "coverage_choices": {}, "events": []}
    validate_profile_session(state)
    return state


def validate_profile_session(state):
    require(isinstance(state, dict) and state.get("format") == "portable-profile-session" and
            type(state.get("schema_version")) is int and state["schema_version"] == 1, "Unsupported profile session")
    require(isinstance(state.get("id"), str) and state["id"].strip(), "Profile session id is required")
    validate_memory(state.get("memory"))
    if state.get("previous") is not None:
        validate_memory(state["previous"])
    events = state.get("events")
    require(isinstance(events, list) and type(state.get("revision")) is int and state["revision"] == len(events), "Invalid profile revision")
    require(all(isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("sha256"), str) for e in events), "Invalid profile event ledger")
    require(len({e["id"] for e in events}) == len(events), "Duplicate profile event ids")
    ledger = state.get("ledger")
    require(isinstance(ledger, dict), "Invalid profile question ledger")
    for key, entry in ledger.items():
        require(isinstance(key, str) and isinstance(entry, dict) and entry.get("status") in {"shown", "deferred"}, "Invalid question state")
    choices = state.get("coverage_choices", {})
    require(isinstance(choices, dict), "Invalid saved coverage choices")
    require(isinstance(state.get("assessment"), dict), "Invalid profile assessment")
    actual_coverage = {entry["category"]: entry for entry in state["assessment"].get("coverage", [])}
    for category, choice in choices.items():
        require(isinstance(category, str) and isinstance(choice, dict) and choice.get("category") == category and
                choice.get("status") in {"excluded", "not_applicable"} and choice == actual_coverage.get(category),
                "Saved user category choice is inconsistent with the assessment")
    audit_profile(state["memory"], state["assessment"], state.get("quality"), state.get("previous"))
    canonical(state)


def _audit(state):
    return audit_profile(state["memory"], state["assessment"], state.get("quality"), state.get("previous"),
                         semantic_aliases=state.get("semantic_aliases"), semantic_conflicts=state.get("semantic_conflicts"))


def _blocked(report):
    # A hallucinated, unsupported candidate does not revoke a prior user fact.
    # It remains pending and first goes back to the agent for source checking.
    by_id = {record["id"]: record for record in report["records"]}
    blocked = {record["id"] for record in report["records"]
               if record["decision"] in {"active", "historical"} and not record["quality_ready"]}
    for conflict in report["conflicts"]:
        if all(by_id[rid]["quality_ready"] for rid in conflict["record_ids"]):
            blocked.update(conflict["record_ids"])
    return sorted(blocked)


def _record_fingerprint(record):
    return digest({k: v for k, v in record.items() if k not in {"review", "currency"}})


def _question_sets(state, report):
    """Keep postponed/shown facts stable when a category gains other facts."""
    raw = report["questions"]
    current = {q["id"]: q for q in raw}
    records = {r["id"]: r for r in state["memory"]["records"]}
    review_ids = {rid for q in raw if q["kind"] == "review" for rid in q["record_ids"]}
    saved, masked = [], set()
    for qid, entry in state["ledger"].items():
        original = entry.get("question")
        if original and original["kind"] == "review":
            ids = [rid for rid in original["record_ids"] if rid in review_ids and
                   entry.get("record_fingerprints", {}).get(rid) == _record_fingerprint(records[rid])]
            question = {**copy.deepcopy(original), "record_ids": ids} if ids else None
            masked.update(ids)
        else:
            question = copy.deepcopy(current.get(qid))
        if question is not None:
            saved.append((question, entry["status"]))
    candidates = []
    for original in raw:
        if original["kind"] == "review":
            ids = [rid for rid in original["record_ids"] if rid not in masked]
            if not ids:
                continue
            question = {**copy.deepcopy(original), "record_ids": ids}
            if ids != original["record_ids"]:
                question["id"] = "review-" + digest({"category": question.get("category"),
                    "records": {rid: _record_fingerprint(records[rid]) for rid in ids}})[:24]
        elif original["id"] in state["ledger"]:
            continue
        else:
            question = copy.deepcopy(original)
        candidates.append(question)
    return candidates, saved


def _apply_coverage_choices(state):
    choices = state.get("coverage_choices", {})
    coverages = {entry["category"]: entry for entry in state["assessment"]["coverage"]}
    coverages.update(copy.deepcopy(choices))
    state["assessment"]["coverage"] = list(coverages.values())
    excluded_ids = {claim["record_id"] for claim in state["assessment"]["claims"]
                    if choices.get(claim["category"], {}).get("status") == "excluded"}
    from .model import record_digest
    decisions = [{"id": record["id"], "content_sha256": record_digest(record), "decision": "exclude"}
                 for record in state["memory"]["records"] if record["id"] in excluded_ids and record["review"]["decision"] != "exclude"]
    if decisions:
        state["memory"] = confirm(state["memory"], {"decisions": decisions})


def profile_view(state):
    validate_profile_session(state)
    report = _audit(state)
    questions, saved = _question_sets(state, report)
    pending = next((q for q, status in saved if status == "shown"), None)
    question = None if pending else next(iter(questions), None)
    memory_records = {record["id"]: record for record in state["memory"]["records"]}

    def with_records(item):
        if item is None:
            return None
        return {**copy.deepcopy(item), "records": [copy.deepcopy(memory_records[rid]) for rid in item.get("record_ids", [])]}

    return {"id": state["id"], "revision": state["revision"], "summary": report["summary"],
            "records": copy.deepcopy(report["records"]), "categories": copy.deepcopy(report["categories"]),
            "conflicts": copy.deepcopy(report["conflicts"]), "agent_tasks": copy.deepcopy(report["agent_tasks"]),
            "question": with_records(question), "awaiting_answer": with_records(pending),
            "deferred_questions": [with_records(q) for q, status in saved if status == "deferred"],
            "export_blocked_records": _blocked(report),
            "limitations": report["limitations"] + [
                "Profile review is not proof of complete account access or native agent setup.",
                "JSON handoff snapshots are not synchronized across agents."]}


def _record(state, event):
    state["events"].append({"id": event["id"], "sha256": digest(event),
                            "type": event["type"], "actor": event["actor"], "at": now()})
    state["revision"] += 1


def apply_profile_event(state, event, *, expected_revision):
    validate_profile_session(state)
    require(isinstance(event, dict) and set(event) == {"id", "actor", "type", "data"}, "Invalid profile event envelope")
    require(isinstance(event["id"], str) and event["id"].strip() and event["actor"] in {"user", "agent"} and isinstance(event["data"], dict), "Invalid profile event")
    for old in state["events"]:
        if old["id"] == event["id"]:
            require(old["sha256"] == digest(event), "Event id reused with different contents")
            return copy.deepcopy(state)
    require(type(expected_revision) is int and expected_revision == state["revision"], "Stale profile session; read its current revision")
    updated = copy.deepcopy(state)
    kind, data = event["type"], event["data"]
    if kind == "review":
        require(event["actor"] == "user", "Record the user's actual decision, not a model's approval")
        require(set(data) == {"decisions"} and isinstance(data["decisions"], list) and data["decisions"], "Provide explicit record decisions")
        before = {record["id"]: record for record in _audit(state)["records"]}
        for decision in data["decisions"]:
            require(isinstance(decision, dict) and decision.get("id") in before, "Unknown review record")
            if decision.get("decision") in {"active", "historical"}:
                category = before[decision["id"]]["category"]
                require(state.get("coverage_choices", {}).get(category, {}).get("status") != "excluded",
                        "Reopen the excluded category before confirming its records")
                require(before[decision["id"]]["quality_ready"], "Check the source and semantic support before requesting confirmation")
        updated["memory"] = confirm(updated["memory"], data)
        remaining = _blocked(_audit(updated))
        for decision in data["decisions"]:
            if decision["decision"] in {"active", "historical"}:
                require(decision["id"] not in remaining, "Resolve the contradictory records together before activating one")
    elif kind in {"question_shown", "defer", "reopen"}:
        require(set(data) <= {"question_id", "reason"} and isinstance(data.get("question_id"), str), "A question id is required")
        candidates, saved = _question_sets(state, _audit(state))
        questions = {q["id"]: q for q in candidates + [q for q, _ in saved]}
        qid = data["question_id"]
        require(qid in questions, "Question changed or is already resolved; reload the profile view")
        if kind == "question_shown":
            view = profile_view(state)
            require(view["question"] is not None and view["question"]["id"] == qid, "This is not the next unshown question")
            updated["ledger"][qid] = {"status": "shown"}
        elif kind == "defer":
            require(event["actor"] == "user", "Only the user chooses to postpone a question")
            require(isinstance(data.get("reason"), str) and data["reason"].strip(), "Record the reason for postponing")
            updated["ledger"][qid] = {"status": "deferred", "reason": data["reason"]}
        else:
            require(event["actor"] == "user" and qid in updated["ledger"], "A saved user question is required")
            del updated["ledger"][qid]
        if kind != "reopen":
            updated["ledger"][qid]["question"] = copy.deepcopy(questions[qid])
            updated["ledger"][qid]["record_fingerprints"] = {
                record["id"]: _record_fingerprint(record) for record in state["memory"]["records"]
                if record["id"] in questions[qid]["record_ids"]}
    elif kind == "coverage":
        require(set(data) == {"category", "status", "source_ids", "note"}, "Coverage requires category, status, source_ids and note")
        if data["status"] in {"excluded", "not_applicable"}:
            require(event["actor"] == "user", "Only the user excludes a category or marks it inapplicable")
        if data["category"] in updated.get("coverage_choices", {}):
            require(event["actor"] == "user", "An agent cannot silently override the user's saved category choice")
        coverage = {item["category"]: item for item in updated["assessment"]["coverage"]}
        coverage[data["category"]] = copy.deepcopy(data)
        updated["assessment"]["coverage"] = list(coverage.values())
        if event["actor"] == "user":
            if data["status"] in {"excluded", "not_applicable"}:
                updated.setdefault("coverage_choices", {})[data["category"]] = copy.deepcopy(data)
            else:
                updated.setdefault("coverage_choices", {}).pop(data["category"], None)
        _apply_coverage_choices(updated)
    else:
        require(False, "Unsupported profile event")
    _record(updated, event)
    validate_profile_session(updated)
    return updated


def refresh_profile(state, memory, assessment, quality=None, *, expected_revision):
    validate_profile_session(state)
    require(type(expected_revision) is int and expected_revision == state["revision"], "Stale profile session")
    updated = copy.deepcopy(state)
    updated["previous"] = copy.deepcopy(state["memory"])
    updated["memory"] = _prepare_snapshot(_merge_memory(state["memory"], memory), state["memory"])
    updated["assessment"] = _merge_assessment(state["assessment"], assessment)
    updated["quality"] = _merge_quality(state.get("quality"), quality)
    _apply_coverage_choices(updated)
    _record(updated, {"id": str(uuid.uuid4()), "actor": "agent", "type": "refresh",
                      "data": {"memory": digest(memory), "assessment": digest(assessment), "quality": digest(quality)}})
    validate_profile_session(updated)
    return updated


def profile_result(state):
    view = profile_view(state)
    memory = copy.deepcopy(state["memory"])
    for record in memory["records"]:
        if record["id"] in view["export_blocked_records"] and record["review"]["decision"] in {"active", "historical"}:
            record["review"] = {"decision": "pending"}
    validate_memory(memory)
    return {"memory": memory, "report": {**view,
            "result": "prepared_profile", "native_memory": "not_applied",
            "quality": "needs_attention" if view["agent_tasks"] or view["export_blocked_records"] else "checked_within_provided_sources",
            "unreviewed_records": [r["id"] for r in memory["records"] if r["review"]["decision"] == "pending"]}}


def render_profile_view(view):
    records = view["records"]
    confirmed = sum(r["decision"] in {"active", "historical"} for r in records)
    lines = ["# Проверка профиля пользователя", "",
             f"Найдено записей: {len(records)}. С действующим подтверждением: {confirmed}.",
             "Неизменённые подтверждённые сведения повторно согласовывать не нужно.", "", "## Что найдено и чего не хватает", ""]
    names = {"checked": "проверено по доступным материалам", "not_checked": "ещё не проверено",
             "unavailable": "источник недоступен", "excluded": "раздел исключён из обследования",
             "not_applicable": "помечено как неприменимое"}
    for category in view["categories"]:
        lines.append(f"- {category['label']}: {names.get(category['status'], category['status'])}; найдено {len(category['record_ids'])}.")
    if view["conflicts"]:
        lines += ["", "## Где есть противоречия", ""]
        for item in view["conflicts"]:
            lines.append("- " + item["question"])
    if view["agent_tasks"]:
        lines += ["", "## Что агенту нужно перепроверить самому", ""]
        lines.extend("- " + item["detail"] for item in view["agent_tasks"])
    question = view["question"]
    if question:
        lines += ["", "## Следующее уточнение", "", question["title"], "", question["why"]]
        for record in question["records"]:
            value = record["value"] if isinstance(record["value"], str) else json.dumps(record["value"], ensure_ascii=False, indent=2)
            scope = "общие сведения и правила" if record["scope"] == "global" else record["scope"]
            lines += ["", f"**{record['title']}** — {scope}", "", value, "",
                      "Источник: " + "; ".join(source["locator"] for source in record["sources"])]
    elif view["awaiting_answer"]:
        lines += ["", "Уточнение уже показано и сохранено. Ждём ответа; известные сведения повторно не спрашиваем."]
    else:
        lines += ["", "Сейчас дополнительных вопросов пользователю нет. Это не означает, что недоступные источники проверены."]
    if view["deferred_questions"]:
        lines += ["", f"Отложено пользователем: {len(view['deferred_questions'])}. Эти сведения не подтверждены автоматически."]
    if view["export_blocked_records"]:
        lines += ["", "Неразрешённые противоречия и неподтверждённые изменения не попадут в действующий профиль."]
    lines += ["", "Подготовка профиля не устанавливает навыки и не подключает аккаунты у получателя."]
    return "\n".join(lines) + "\n"
