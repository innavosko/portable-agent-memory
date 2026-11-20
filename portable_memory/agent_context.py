"""Preserve explicit Dots/Duet context snapshots with honest per-source coverage.

This module consumes supplied JSON data. It does not access either platform,
summarize instructions, infer missing capabilities, or execute any source text.
"""
from __future__ import annotations

import copy

from .delivery import checked_path
from .model import canonical, check_no_secret_fields, digest, empty_memory, require, validate_memory
from .package import json_bytes, write_new

MAX_INPUT_BYTES = 50_000_000
MAX_TOTAL_BYTES = 200_000_000
MAX_ITEMS = 5000
PLATFORMS = {"dots", "duet"}
CATEGORIES = {
    "account_instructions": "Инструкции аккаунта",
    "agent_instructions": "Инструкции агента",
    "agent_rules": "Правила агента",
    "saved_memory": "Сохранённая память",
    "agent_notes": "Заметки агента",
    "projects": "Проекты и их контекст",
    "project_instructions": "Инструкции проектов",
    "skills": "Сведения о навыках",
    "responsibilities": "Порученные обязанности",
    "active_tasks": "Текущие задачи",
    "recurring_tasks": "Регулярные задачи",
    "integrations": "Подключения и правила работы с ними",
    "channels_and_destinations": "Каналы и места передачи результатов",
    "artifacts": "Работы и файлы результатов",
    "conversations": "Диалоги",
}
KINDS_BY_CATEGORY = {
    "account_instructions": {"rule"}, "agent_instructions": {"rule"}, "agent_rules": {"rule"},
    "saved_memory": {"profile", "contact", "account", "preference", "rule", "resource", "idea"},
    "agent_notes": {"profile", "idea", "resource"}, "projects": {"project"},
    "project_instructions": {"rule"}, "skills": {"skill"}, "responsibilities": {"workflow"},
    "active_tasks": {"workflow"}, "recurring_tasks": {"automation"},
    "integrations": {"integration", "account"}, "channels_and_destinations": {"resource", "account"},
    "artifacts": {"artifact"}, "conversations": {"resource"},
}
STATUSES = {"pending", "provided", "unavailable", "not_found", "excluded"}
_SOURCE = {"platform", "account_id", "workspace_id", "observed_at", "evidence"}
_CATEGORY = {"id", "status", "evidence", "source_refs", "items", "excluded_by_user"}
_ITEM = {"id", "title", "kind", "scope", "value", "source_ref"}
_CREDENTIAL_FIELDS = {"password", "passwd", "access_token", "refresh_token", "api_key", "private_key",
                      "session_cookie", "recovery_codes", "client_secret", "authorization", "cookie",
                      "auth_token", "secret_key"}
_LIMITATIONS = [
    "Only the supplied snapshot was inspected; there was no account API call or whole-account discovery.",
    "The category checklist does not assert that a platform provides exports for each category.",
    "Provided values are preserved without semantic extraction, summarization, or verification of their claims.",
    "The source JSON is reserialized with unchanged values; original input whitespace and byte formatting are not retained.",
    "A not_found status describes the stated check, not proof that the user has no such data anywhere.",
    "Project/artifact/conversation references do not fetch original files or establish access to them.",
    "Skill declarations do not include original skill files; use the separate original-skill collection workflow.",
    "Responsibilities and task descriptions are user workflow data, not evidence of platform capabilities.",
    "Recurring tasks, integrations and destinations are not restored or connected by collecting this snapshot.",
    "Named credential fields are rejected; secrets embedded in arbitrary prose are not automatically redacted.",
]


def _text(value, name):
    require(isinstance(value, str) and bool(value.strip()) and "\x00" not in value, f"{name}: nonempty text required")


def _identifier(value, name):
    _text(value, name)
    require(len(value) <= 512 and not any(char in value for char in "\r\n\t"), f"Invalid {name}")


def _source(source):
    require(isinstance(source, dict) and set(source) == _SOURCE, "Invalid context source fields")
    require(isinstance(source["platform"], str) and source["platform"] in PLATFORMS, "Unsupported context platform")
    for name in ("account_id", "workspace_id"):
        if source[name] is not None:
            _identifier(source[name], "source." + name)
    for name in ("observed_at", "evidence"):
        _text(source[name], "source." + name)


def _reject_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            require(isinstance(key, str), "Context JSON object keys must be strings")
            normalized = key.casefold().replace("-", "_")
            require(normalized not in _CREDENTIAL_FIELDS, "Credential field prohibited in context: " + key)
            _reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _reject_credentials(item)


def _pending(category):
    return {"id": category, "status": "pending", "evidence": None,
            "source_refs": [], "items": [], "excluded_by_user": False}


def context_template(platform, source):
    """Create the fixed coverage checklist; no category is assumed accessible."""
    require(isinstance(platform, str) and platform in PLATFORMS, "Unsupported context platform")
    require(isinstance(source, dict), "Context source must be an object")
    source = copy.deepcopy(source)
    require(source.get("platform", platform) == platform, "Source platform differs from the chosen platform")
    source["platform"] = platform
    _source(source)
    snapshot = {"format": "portable-agent-context", "schema_version": 1, "source": source,
                "categories": [_pending(category) for category in CATEGORIES]}
    validate_context(snapshot)
    return snapshot


def validate_context(snapshot):
    require(isinstance(snapshot, dict) and set(snapshot) == {"format", "schema_version", "source", "categories"},
            "Invalid context snapshot fields")
    require(snapshot["format"] == "portable-agent-context" and type(snapshot["schema_version"]) is int
            and snapshot["schema_version"] == 1, "Unsupported context snapshot")
    _source(snapshot["source"])
    require(isinstance(snapshot["categories"], list) and len(snapshot["categories"]) <= len(CATEGORIES), "Invalid context category list")
    seen, total, project_aliases = set(), 0, {}
    for category in snapshot["categories"]:
        require(isinstance(category, dict) and set(category) == _CATEGORY, "Invalid context category fields")
        cid = category["id"]
        require(isinstance(cid, str) and cid in CATEGORIES and cid not in seen, "Unknown or repeated context category")
        seen.add(cid)
        status = category["status"]
        require(isinstance(status, str) and status in STATUSES, "Invalid context coverage status")
        require(type(category["excluded_by_user"]) is bool, "excluded_by_user must be boolean")
        require(category["excluded_by_user"] == (status == "excluded"), "Only an explicit user choice may exclude a category")
        if category["evidence"] is not None:
            _text(category["evidence"], "category evidence")
        refs = category["source_refs"]
        require(isinstance(refs, list), "source_refs must be a string list")
        for ref in refs:
            _text(ref, "source reference")
        require(len(refs) == len(set(refs)), "Repeated category source reference")
        require(isinstance(category["items"], list), "Context category items must be a list")
        if status == "provided":
            require(bool(category["items"]), "provided requires at least one actual item")
        else:
            require(not category["items"], "Only provided categories may contain items")
        if status != "pending":
            _text(category["evidence"], "Observed/excluded category evidence")
        if status in {"provided", "unavailable", "not_found"}:
            require(bool(refs), "Observed category requires actual source/check references")
        item_ids = set()
        for item in category["items"]:
            require(isinstance(item, dict) and _ITEM <= set(item)
                    and not set(item) - _ITEM - {"project_id", "observed_at"}, "Invalid context item fields")
            _identifier(item["id"], "source item id")
            require(item["id"] not in item_ids, "Repeated item id within a context category")
            item_ids.add(item["id"])
            _text(item["title"], "item title")
            _text(item["scope"], "item scope")
            require(isinstance(item["kind"], str) and item["kind"] in KINDS_BY_CATEGORY[cid], "Item kind is incompatible with its source category")
            require(item["value"] is not None, "Context item value cannot be null")
            if isinstance(item["value"], str):
                _text(item["value"], "item value")
            _text(item["source_ref"], "item source reference")
            require(item["source_ref"] in refs, "Item reference must belong to its observed category")
            if "observed_at" in item and item["observed_at"] is not None:
                _text(item["observed_at"], "item observation date")
            if "project_id" in item:
                _identifier(item["project_id"], "source project id")
            if cid == "project_instructions":
                require("project_id" in item, "Project instructions need their explicit source project_id")
                require(item["scope"] == "project:" + item["project_id"], "Project instructions require scope project:<project_id>")
            if cid == "projects":
                for alias in {item["id"], item.get("project_id", item["id"])}:
                    require(alias not in project_aliases or project_aliases[alias] == item["id"],
                            "Ambiguous project identity in supplied context")
                    project_aliases[alias] = item["id"]
            total += 1
            require(total <= MAX_ITEMS, "Too many supplied context items; split the snapshot")
    _reject_credentials(snapshot)
    check_no_secret_fields(snapshot)
    require(len(canonical(snapshot)) <= MAX_INPUT_BYTES, "Context snapshot exceeds input size limit")


def _identity(snapshot):
    source = snapshot["source"]
    identity = {key: source[key] for key in ("platform", "account_id", "workspace_id")}
    unresolved = source["account_id"] is None or source["workspace_id"] is None
    if unresolved:
        # Two unknown accounts cannot be declared the same source merely because
        # a platform reuses item IDs. Stable repeats of this exact snapshot work.
        identity["unresolved_snapshot_sha256"] = digest(snapshot)
    return identity, unresolved


def _record_id(identity, category, item_id):
    return "context-" + digest({"source": identity, "category": category, "item_id": item_id})[:32]


def _markdown(report):
    labels = {"pending": "Ещё не проверено", "provided": "Данные предоставлены", "unavailable": "Недоступно",
              "not_found": "Не найдено при указанной проверке", "excluded": "Исключено пользователем"}
    lines = ["# Перенос контекста агента", "", "Платформа источника: " + report["source"]["platform"], "",
             "Сохранены предоставленные значения. Записи ожидают проверки; настройки у получателя не применялись.", ""]
    for category in report["categories"]:
        lines += [f"## {category['label']}", "", labels[category["status"]] + f". Записей: {len(category['record_ids'])}.", ""]
        if category["evidence"]:
            lines += ["Основание: " + category["evidence"], ""]
    if report["unresolved_projects"]:
        lines += ["## Связи проектов для проверки", ""]
        for item in report["unresolved_projects"]:
            lines.append(f"- Для «{item['title']}» указан проект {item['source_project_id']}, но его запись не предоставлена.")
        lines += [""]
    lines += ["## Что не выполнено", "",
              "Это не полный просмотр аккаунта. Недоступные и непроверенные категории остаются открытыми.",
              "Ссылки на файлы не означают получения оригиналов. Сведения о навыках не заменяют их исходные файлы.",
              "Поручения, регулярные задачи и подключения не запущены и не восстановлены. Их доступность проверяют отдельно.", ""]
    return "\n".join(lines)


def collect_context(snapshot, out, accepted=False):
    """Preserve source values and pending typed records in a new output folder."""
    validate_context(snapshot)
    require(accepted is True, "Explicit acceptance is required to collect the supplied context")
    out = checked_path(out)
    require(not out.exists(), "Context output already exists; choose a new folder")
    identity, unresolved = _identity(snapshot)
    supplied = {category["id"]: category for category in snapshot["categories"]}
    categories = [copy.deepcopy(supplied.get(cid, _pending(cid))) for cid in CATEGORIES]
    memory = empty_memory()
    memory["metadata"] = {"collection": "provided_agent_context", "source": copy.deepcopy(snapshot["source"]),
                          "identity_unresolved": unresolved, "snapshot_sha256": digest(snapshot)}
    report = {"format": "portable-agent-context-report", "schema_version": 1,
              "source": copy.deepcopy(snapshot["source"]), "snapshot_sha256": digest(snapshot),
              "identity_unresolved": unresolved, "categories": [], "unmet_categories": [], "unresolved_projects": [],
              "summary": {"categories_total": len(CATEGORIES), **{status: 0 for status in sorted(STATUSES)},
                          "records": 0, "user_review_pending": 0, "all_categories_assessed": False,
                          "all_requested_categories_available": False, "complete_account_export": False},
              "memory_path": "memory.json", "source_snapshot_path": "assets/context/source.json",
              "limitations": list(_LIMITATIONS)}
    project_ids = {}
    for category in categories:
        if category["id"] == "projects":
            for item in category["items"]:
                rid = _record_id(identity, "projects", item["id"])
                project_ids[item["id"]] = rid
                if item.get("project_id"):
                    project_ids[item["project_id"]] = rid
    for category in categories:
        cid, status = category["id"], category["status"]
        entry = {key: copy.deepcopy(category[key]) for key in ("id", "status", "evidence", "source_refs", "excluded_by_user")}
        entry.update(label=CATEGORIES[cid], record_ids=[], supplied=cid in supplied)
        report["summary"][status] += 1
        if status in {"pending", "unavailable"}:
            report["unmet_categories"].append(cid)
            memory["coverage"]["unavailable"].append(cid + ": " + status)
        elif status == "excluded":
            memory["coverage"]["excluded"].append(cid + ": " + category["evidence"])
        else:
            memory["coverage"]["read"].append(cid + ": " + status + " — " + "; ".join(category["source_refs"]))
        for item in category["items"]:
            rid = _record_id(identity, cid, item["id"])
            source_project = item.get("project_id")
            record = {"id": rid, "kind": item["kind"], "title": item["title"], "scope": item["scope"],
                      "value": copy.deepcopy(item["value"]), "provenance": "source", "currency": "unknown",
                      "source_category": cid, "source_item_id": item["id"],
                      "sources": [{"id": "context-source-" + digest({"identity": identity, "ref": item["source_ref"]})[:24],
                                   "locator": item["source_ref"], "observed_at": item.get("observed_at", snapshot["source"]["observed_at"])}],
                      "links": [], "review": {"decision": "pending"}}
            if source_project is not None:
                record["source_project_id"] = source_project
                target = project_ids.get(source_project)
                if target and target != rid:
                    record["links"].append({"target": target, "relation": "belongs_to", "basis": "source"})
                elif not target:
                    report["unresolved_projects"].append({"record_id": rid, "title": item["title"], "source_project_id": source_project})
            memory["records"].append(record)
            entry["record_ids"].append(rid)
        report["categories"].append(entry)
    count = len(memory["records"])
    report["summary"].update(records=count, user_review_pending=count,
                             all_categories_assessed=report["summary"]["pending"] == 0,
                             all_requested_categories_available=not report["unmet_categories"])
    validate_memory(memory)
    payload = {"assets/context/source.json": json_bytes(snapshot), "memory.json": json_bytes(memory),
               "context-report.json": json_bytes(report), "context-report.md": _markdown(report).encode("utf-8")}
    require(sum(map(len, payload.values())) <= MAX_TOTAL_BYTES, "Context outputs exceed size limit")
    out.mkdir(parents=True)
    write_new(out / ".incomplete", "Context collection has not finished.\n")
    for name, raw in payload.items():
        write_new(checked_path(out / name, out), raw)
    (out / ".incomplete").unlink()
    return report
