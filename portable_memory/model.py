"""Versioned records and content-bound human review."""
import copy
import hashlib
import json
import re
from datetime import datetime, timezone

from .errors import ValidationError

KINDS = {"profile", "contact", "account", "integration", "rule", "preference",
         "project", "idea", "artifact", "workflow", "automation", "resource", "skill"}
DECISIONS = {"pending", "active", "historical", "exclude"}
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SECRET_KEYS = {"password", "access_token", "refresh_token", "api_key", "private_key",
               "session_cookie", "recovery_codes"}


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Not JSON-compatible: {exc}") from exc


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def record_digest(record):
    return digest({key: value for key, value in record.items() if key != "review"})


def empty_memory():
    return {"format": "portable-memory", "schema_version": 1, "metadata": {},
            "coverage": {"read": [], "unavailable": [], "excluded": []}, "records": []}


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def text_field(value, label):
    require(isinstance(value, str) and bool(value.strip()), f"{label}: nonempty text required")


def check_no_secret_fields(value):
    # This catches named credential fields, not every secret embedded in prose.
    if isinstance(value, dict):
        for key, item in value.items():
            require(key.casefold() not in SECRET_KEYS, f"Credential field prohibited: {key}")
            check_no_secret_fields(item)
    elif isinstance(value, list):
        for item in value:
            check_no_secret_fields(item)


def validate_memory(memory, *, allow_stale_review=False):
    require(isinstance(memory, dict), "Memory must be an object")
    require(memory.get("format") == "portable-memory", "Unsupported memory format")
    require(type(memory.get("schema_version")) is int and memory["schema_version"] == 1,
            "Unsupported schema version")
    require(isinstance(memory.get("metadata", {}), dict), "metadata must be an object")
    coverage = memory.get("coverage")
    require(isinstance(coverage, dict), "coverage is required")
    for key in ("read", "unavailable", "excluded"):
        require(isinstance(coverage.get(key), list) and
                all(isinstance(x, str) for x in coverage[key]), f"coverage.{key}: string list required")
    require(isinstance(memory.get("records"), list), "records must be a list")
    seen = set()
    for record in memory["records"]:
        require(isinstance(record, dict), "record must be an object")
        rid = record.get("id")
        require(isinstance(rid, str) and ID.fullmatch(rid), "Invalid record id")
        require(rid not in seen, f"Duplicate record id: {rid}")
        seen.add(rid)
        require(record.get("kind") in KINDS, f"{rid}: unsupported kind")
        for field in ("title", "scope"):
            text_field(record.get(field), f"{rid}.{field}")
        require("value" in record and record["value"] is not None, f"{rid}: value is required")
        require(record.get("provenance") in {"user_statement", "source", "model_inference"},
                f"{rid}: invalid provenance")
        require(record.get("currency") in {"current", "historical", "unknown"}, f"{rid}: invalid currency")
        sources = record.get("sources")
        require(isinstance(sources, list) and sources, f"{rid}: at least one actual source required")
        for source in sources:
            require(isinstance(source, dict), f"{rid}: source must be an object")
            text_field(source.get("id"), f"{rid}: source id")
            text_field(source.get("locator"), f"{rid}: source locator")
            require(source.get("observed_at") is None or isinstance(source["observed_at"], str),
                    f"{rid}: source date must be text or null")
        require(isinstance(record.get("links", []), list), f"{rid}: links must be a list")
        for link in record.get("links", []):
            require(isinstance(link, dict), f"{rid}: invalid link")
            require(isinstance(link.get("target"), str) and ID.fullmatch(link["target"]), f"{rid}: invalid link target")
            text_field(link.get("relation"), f"{rid}: relation")
            require(link.get("basis") in {"source", "inferred"}, f"{rid}: invalid link basis")
        review = record.get("review")
        require(isinstance(review, dict) and review.get("decision") in DECISIONS, f"{rid}: review required")
        if review["decision"] != "pending" and not allow_stale_review:
            require(review.get("content_sha256") == record_digest(record), f"{rid}: content changed; review again")
            text_field(review.get("reviewed_at"), f"{rid}: review date")
            if review["decision"] == "active":
                require(record["currency"] == "current", f"{rid}: active record must be current")
            if review["decision"] == "historical":
                require(record["currency"] == "historical", f"{rid}: historical review must be historical")
    canonical(memory)
    check_no_secret_fields(memory)


def review_template(memory):
    validate_memory(memory, allow_stale_review=True)
    return {"decisions": [{"id": r["id"], "content_sha256": record_digest(r), "decision": "pending"}
                          for r in memory["records"]]}


def confirm(memory, decisions):
    """Apply explicit decisions to the exact content that was presented for review."""
    validate_memory(memory, allow_stale_review=True)
    require(isinstance(decisions, dict) and isinstance(decisions.get("decisions"), list), "decisions list required")
    result = copy.deepcopy(memory)
    by_id = {r["id"]: r for r in result["records"]}
    # An edited record cannot carry its old confirmation through a partial review.
    for record in result["records"]:
        if record["review"].get("content_sha256") != record_digest(record):
            record["review"] = {"decision": "pending"}
    seen = set()
    for decision in decisions["decisions"]:
        require(isinstance(decision, dict), "Invalid decision")
        rid = decision.get("id")
        require(rid in by_id and rid not in seen, f"Unknown or repeated decision: {rid}")
        seen.add(rid)
        record = by_id[rid]
        require(decision.get("content_sha256") == record_digest(record), f"{rid}: stale review decision")
        action = decision.get("decision")
        require(action in DECISIONS, f"{rid}: invalid review decision")
        if action == "active":
            record["currency"] = "current"
        elif action == "historical":
            record["currency"] = "historical"
        record["review"] = {"decision": action}
        if action != "pending":
            record["review"].update(content_sha256=record_digest(record), reviewed_at=now())
    validate_memory(result)
    return result


def render_profile(memory, *, active_only=False):
    lines = ["# Профиль и контекст", "", "Материалы для проверки; записи не предоставляют доступ к сервисам.", ""]
    if active_only and memory.get("metadata", {}).get("recipient_memory_required") is True:
        return "\n".join(lines + ["Текущая память получателя не предоставлена. Активный профиль удержан до её чтения и сопоставления с входящими сведениями.", ""])
    if active_only and memory.get("metadata", {}).get("common_review_required") is True:
        return "\n".join(lines + ["Объединённый профиль ещё не проверен. Ведущий агент должен сверить текущие и входящие сведения перед применением.", ""])
    aliases = memory.get("metadata", {}).get("common_profile", {}).get("aliases", {})
    indexed = {record["id"]: record for record in memory["records"]}
    for record in memory["records"]:
        if record["id"] in aliases and aliases[record["id"]] in indexed:
            continue
        decision = record["review"]["decision"]
        if decision == "exclude" or (active_only and decision != "active"):
            continue
        value = record["value"] if isinstance(record["value"], str) else json.dumps(record["value"], ensure_ascii=False, indent=2)
        lines += [f"## {record['title']} ({record['id']})", "",
                  f"Тип: {record['kind']}. Область: {record['scope']}. Решение: {decision}.", "", value, "",
                  "Источники: " + "; ".join(s["locator"] for s in record["sources"]), ""]
        additional = [other for alias, target in aliases.items() if target == record["id"] and alias in indexed for other in indexed[alias]["sources"]]
        if additional:
            lines += ["Дополнительные источники того же смысла: " + "; ".join(dict.fromkeys(s["locator"] for s in additional)),
                      "Исходные формулировки сохранены отдельными записями в memory.json.", ""]
    lines += ["## Охват", "", json.dumps(memory["coverage"], ensure_ascii=False, indent=2), ""]
    return "\n".join(lines)


def render_review(memory):
    validate_memory(memory, allow_stale_review=True)
    clean = copy.deepcopy(memory)
    for record in clean["records"]:
        if record["review"].get("content_sha256") != record_digest(record):
            record["review"] = {"decision": "pending"}
        # Excluded entries are still visible in a LOCAL review, before export.
        if record["review"]["decision"] == "exclude":
            record["review"] = {"decision": "pending"}
    return ("# Проверка перед переносом\n\n"
            "Для каждой записи выберите active, historical, exclude или pending. "
            "Неизменённый шаблон оставляет всё непроверенным.\n\n" + render_profile(clean))
