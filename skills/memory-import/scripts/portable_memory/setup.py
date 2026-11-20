"""Receiving-agent setup ledger after a verified portable-workspace import.

This module reads the imported workspace, but never writes native memory,
installs a skill, runs imported instructions, authorizes accounts or schedules
anything. Native results are explicit host attestations: the calling host must
actually perform and read back the operation before submitting ``verified``.
Actor names and evidence strings are not cryptographic proof or authentication.
Snapshots have one owner; copying JSON does not synchronize independent agents.
"""

import copy
import json
from pathlib import Path
import re
import uuid
from urllib.parse import parse_qsl, urlsplit

from .errors import ValidationError
from .model import canonical, check_no_secret_fields, digest, now, require, validate_memory


SCOPES = ("profile", "projects", "skills", "integrations", "automations")
CAPABILITIES = {
    "profile": "native_memory", "projects": "project_files",
    "skills": "install_skill", "integrations": "connect_integration",
    "automations": "create_automation",
}
KINDS = {
    "profile": {"profile", "contact", "rule", "preference"},
    "projects": {"project", "idea", "artifact", "resource"},
    "skills": {"skill", "workflow"},
    "integrations": {"account", "integration"},
    "automations": {"automation"},
}
STATUSES = {"pending", "blocked", "deferred", "failed", "verified"}
_HASH = re.compile(r"^[0-9a-f]{64}$")
_LABELS = {"profile": "Профиль и правила", "projects": "Проекты и материалы",
           "skills": "Навыки", "integrations": "Подключения", "automations": "Автоматизации"}
_STATUS_LABELS = {"pending": "ожидает настройки", "blocked": "недоступно",
                  "deferred": "отложено пользователем", "failed": "нужно исправить",
                  "verified": "результат подтверждён принимающим агентом"}


def _fields(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional),
            "Unexpected or missing setup fields")


def _text(value, label):
    require(isinstance(value, str) and value.strip() and len(value) <= 16000, f"{label}: nonempty text required")
    # Basic leakage guard, not a general-purpose secret detector. Do not put
    # credentials, signed download links or authorization grants in this ledger.
    for url in re.findall(r"https?://[^\s<>]+", value):
        parsed = urlsplit(url)
        keys = [key.casefold() for key, _ in parse_qsl(parsed.query + "&" + parsed.fragment)]
        forbidden = {"token", "access_token", "refresh_token", "api_key", "key", "signature", "sig",
                     "password", "credential", "authorization", "code"}
        require(not parsed.username and not parsed.password and
                not any(key in forbidden or key.startswith("x-amz-") or key.startswith("x-goog-") for key in keys),
                "Do not include credentials or signed URLs in setup evidence")


def _hash(value, label):
    require(isinstance(value, str) and _HASH.fullmatch(value), f"Invalid {label}")


def _recipient(recipient):
    _fields(recipient, {"agent_id", "workspace_id"})
    for key in recipient:
        _text(recipient[key], key)


def _delivery(delivery):
    _fields(delivery, {"transfer_id", "version", "package_sha256", "recipient", "workspace", "setup_scope"},
            {"pending_record_ids"})
    _text(delivery["transfer_id"], "transfer_id")
    require(type(delivery["version"]) is int and delivery["version"] > 0, "version must be a positive integer")
    _hash(delivery["package_sha256"], "package hash")
    _recipient(delivery["recipient"])
    _text(delivery["workspace"], "workspace")
    scope = delivery["setup_scope"]
    require(isinstance(scope, list) and all(isinstance(s, str) and s in SCOPES for s in scope)
            and len(set(scope)) == len(scope), "Invalid or repeated setup scope")
    pending = delivery.get("pending_record_ids", [])
    require(isinstance(pending, list) and all(isinstance(rid, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", rid)
            for rid in pending) and len(set(pending)) == len(pending), "Invalid unresolved incoming record ids")
    check_no_secret_fields(delivery)


def _read_json(path):
    require(path.is_file() and not path.is_symlink() and not (hasattr(path, "is_junction") and path.is_junction()),
            f"A regular workspace file is required: {path.name}")
    require(path.stat().st_size <= 50 * 1024 * 1024, "Workspace JSON exceeds the setup size limit")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValidationError(f"Cannot read workspace {path.name}: {exc}") from exc


def _workspace(memory, delivery):
    path = Path(delivery["workspace"]).absolute()
    require(path.is_dir(), "Imported portable workspace does not exist")
    # These are read-only, explicitly selected files. Checking unrelated parent
    # directory metadata is unnecessary and can be forbidden by host policies.
    require(not path.is_symlink() and not (hasattr(path, "is_junction") and path.is_junction()),
            "Setup workspace must not be a link or junction")
    loaded = _read_json(path / "memory.json")
    require(canonical(loaded) == canonical(memory), "Workspace memory differs from the provided memory")
    receipt = _read_json(path / "receipt.json")
    require(isinstance(receipt, dict) and receipt.get("format") == "portable-memory-import-receipt"
            and receipt.get("target") == "portable_workspace"
            and receipt.get("package_sha256") == delivery["package_sha256"],
            "Portable workspace receipt does not match this package")
    return {"status": "verified", "target": "portable_workspace", "path": str(path),
            "memory_sha256": digest(memory), "receipt_sha256": digest(receipt),
            "package_sha256": delivery["package_sha256"], "verified_at": now(),
            "verification": "local_files_read_back"}


def _context(recipient, name, capability):
    if not capability["available"]:
        return None
    return digest({"recipient": recipient, "capability": name, "evidence": capability["evidence"]})


def _steps(memory, scope):
    items = []
    for kind in SCOPES:
        if kind not in scope:
            continue
        records = sorted((r for r in memory["records"] if r["kind"] in KINDS[kind]
                          and (r["review"]["decision"] == "active"
                               or kind == "projects" and r["review"]["decision"] == "historical")), key=lambda r: r["id"])
        groups = [records] if kind == "profile" and records else [[r] for r in records]
        for group in groups:
            semantic = [{k: v for k, v in r.items() if k != "review"} for r in group]
            step_id = "profile" if kind == "profile" else kind + ":" + group[0]["id"]
            items.append({"id": step_id, "kind": kind,
                          "title": _LABELS[kind] if kind == "profile" else group[0]["title"],
                          "record_ids": [r["id"] for r in group], "capability": CAPABILITIES[kind],
                          "content_sha256": digest({"kind": kind, "records": semantic}),
                          "status": "pending", "result": None, "last_verification": None})
    return items


def _unreviewed(memory, scope):
    kinds = set().union(*(KINDS[s] for s in scope)) if scope else set()
    return sorted(r["id"] for r in memory["records"] if r["kind"] in kinds and r["review"]["decision"] == "pending")


def create_setup(memory, delivery, previous=None):
    """Create a setup plan from actual imported files, without applying settings.

    A previous state is a host-provided snapshot for the SAME native recipient
    identity. Its capability evidence is reused as an attestation, not live
    rechecked. A host detecting changed access must submit a capability event.
    """
    validate_memory(memory)
    _delivery(delivery)
    workspace = _workspace(memory, delivery)
    capabilities = {name: {"available": False, "evidence": None, "context_sha256": None}
                    for name in CAPABILITIES.values()}
    state = {"format": "portable-memory-setup", "schema_version": 1, "id": str(uuid.uuid4()),
             "revision": 0, "created_at": now(), "delivery": copy.deepcopy(delivery),
             "memory": copy.deepcopy(memory), "portable_workspace": workspace,
             "capabilities": capabilities, "items": _steps(memory, delivery["setup_scope"]), "events": []}
    if previous is not None:
        validate_setup(previous)
        if previous["delivery"]["recipient"] == delivery["recipient"]:
            state["capabilities"] = copy.deepcopy(previous["capabilities"])
            prior_items = {item["id"]: item for item in previous["items"]}
            for item in state["items"]:
                old = prior_items.get(item["id"])
                if old is None or old["content_sha256"] != item["content_sha256"]:
                    continue
                context = state["capabilities"][item["capability"]]["context_sha256"]
                if old["status"] == "verified":
                    if context and old["result"]["capability_context_sha256"] == context:
                        item.update({key: copy.deepcopy(old[key]) for key in ("status", "result", "last_verification")})
                else:
                    item.update({key: copy.deepcopy(old[key]) for key in ("status", "result", "last_verification")})
    validate_setup(state)
    return state


def _result(result, item, recipient):
    if result is None:
        return
    _fields(result, {"status", "evidence", "actor", "at", "content_sha256", "recipient",
                     "capability_context_sha256", "verification"}, {"read_back"})
    require(result["status"] in STATUSES - {"pending"} and result["actor"] in {"user", "agent"}, "Invalid setup result")
    _text(result["evidence"], "result evidence")
    _text(result["at"], "result date")
    require(result["content_sha256"] == item["content_sha256"] and result["recipient"] == recipient,
            "Setup result belongs to other content or recipient")
    if result["capability_context_sha256"] is not None:
        _hash(result["capability_context_sha256"], "capability context")
    if result["status"] == "verified":
        _text(result.get("read_back"), "actual read-back evidence")
        require(result["verification"] == "host_attestation" and result["capability_context_sha256"] is not None,
                "Native verification requires a host attestation with capability context")
    else:
        require(result["verification"] == "reported_status", "Invalid nonverified setup result")
    if result["status"] == "deferred":
        require(result["actor"] == "user", "Only the user defers setup")


def validate_setup(state):
    """Validate a self-contained snapshot; this does not reconnect to the host."""
    _fields(state, {"format", "schema_version", "id", "revision", "created_at", "delivery", "memory",
                    "portable_workspace", "capabilities", "items", "events"})
    require(state["format"] == "portable-memory-setup" and type(state["schema_version"]) is int
            and state["schema_version"] == 1, "Unsupported setup format")
    _text(state["id"], "setup id")
    _text(state["created_at"], "creation date")
    _delivery(state["delivery"])
    validate_memory(state["memory"])
    workspace = state["portable_workspace"]
    _fields(workspace, {"status", "target", "path", "memory_sha256", "receipt_sha256", "package_sha256",
                        "verified_at", "verification"})
    require(workspace["status"] == "verified" and workspace["target"] == "portable_workspace"
            and workspace["verification"] == "local_files_read_back"
            and workspace["memory_sha256"] == digest(state["memory"])
            and workspace["package_sha256"] == state["delivery"]["package_sha256"], "Invalid portable workspace verification")
    _hash(workspace["receipt_sha256"], "receipt hash")
    _text(workspace["path"], "workspace path")
    _text(workspace["verified_at"], "workspace verification date")
    caps = state["capabilities"]
    require(isinstance(caps, dict) and set(caps) == set(CAPABILITIES.values()), "Invalid capability list")
    recipient = state["delivery"]["recipient"]
    for name, capability in caps.items():
        _fields(capability, {"available", "evidence", "context_sha256"})
        require(type(capability["available"]) is bool, "Capability availability must be boolean")
        if capability["evidence"] is not None:
            _text(capability["evidence"], "capability evidence")
        require(not capability["available"] or capability["evidence"] is not None, "Available capability needs evidence")
        require(capability["context_sha256"] == _context(recipient, name, capability), "Capability context changed")
    expected = _steps(state["memory"], state["delivery"]["setup_scope"])
    require(isinstance(state["items"], list) and len(state["items"]) == len(expected), "Setup item list changed")
    for item, base in zip(state["items"], expected):
        _fields(item, set(base))
        require(all(item[key] == base[key] for key in base if key not in {"status", "result", "last_verification"}),
                "Setup step content changed; create a new setup plan")
        require(item["status"] in STATUSES, "Invalid setup status")
        _result(item["result"], item, recipient)
        _result(item["last_verification"], item, recipient)
        require(item["last_verification"] is None or item["last_verification"]["status"] == "verified", "Invalid historical result")
        if item["status"] == "pending":
            require(item["result"] is None, "Pending setup must not claim a result")
        else:
            require(item["result"] is not None and item["result"]["status"] == item["status"], "Setup result/status mismatch")
        if item["status"] == "verified":
            cap = caps[item["capability"]]
            require(cap["available"] and item["result"]["capability_context_sha256"] == cap["context_sha256"],
                    "Verified step has no current matching capability")
    require(isinstance(state["events"], list) and type(state["revision"]) is int
            and state["revision"] == len(state["events"]), "Invalid setup revision")
    ids = set()
    for event in state["events"]:
        _fields(event, {"id", "sha256", "type", "actor", "at"})
        _text(event["id"], "event id")
        _hash(event["sha256"], "event fingerprint")
        require(event["id"] not in ids and event["actor"] in {"user", "agent"}
                and event["type"] in {"capability", "step_result", "retry"}, "Invalid event history")
        ids.add(event["id"])
        _text(event["at"], "event time")
    check_no_secret_fields(state)
    canonical(state)


def _make_result(state, item, status, evidence, actor, read_back=None):
    result = {"status": status, "evidence": evidence, "actor": actor, "at": now(),
              "content_sha256": item["content_sha256"], "recipient": copy.deepcopy(state["delivery"]["recipient"]),
              "capability_context_sha256": state["capabilities"][item["capability"]]["context_sha256"],
              "verification": "host_attestation" if status == "verified" else "reported_status"}
    if read_back is not None:
        result["read_back"] = read_back
    return result


def apply_setup_event(state, event, *, expected_revision):
    validate_setup(state)
    _fields(event, {"id", "actor", "type", "data"})
    _text(event["id"], "event id")
    require(event["actor"] in {"user", "agent"} and isinstance(event["data"], dict), "Invalid setup event")
    check_no_secret_fields(event)
    for prior in state["events"]:
        if prior["id"] == event["id"]:
            require(prior["sha256"] == digest(event), "Event id reused with other contents")
            return copy.deepcopy(state)
    require(type(expected_revision) is int and state["revision"] == expected_revision, "Stale setup revision")
    result = copy.deepcopy(state)
    kind, data = event["type"], event["data"]
    if kind == "capability":
        _fields(data, {"name", "available", "evidence"})
        require(data["name"] in result["capabilities"] and type(data["available"]) is bool, "Invalid capability")
        _text(data["evidence"], "capability evidence")
        capability = {"available": data["available"], "evidence": data["evidence"], "context_sha256": None}
        capability["context_sha256"] = _context(result["delivery"]["recipient"], data["name"], capability)
        result["capabilities"][data["name"]] = capability
        for item in result["items"]:
            if item["capability"] == data["name"] and item["status"] == "verified" and (
                    not capability["available"] or item["result"]["capability_context_sha256"] != capability["context_sha256"]):
                item["last_verification"] = copy.deepcopy(item["result"])
                item["status"] = "blocked"
                item["result"] = _make_result(result, item, "blocked",
                    "Capability access or context changed; verify the receiving setup again. " + data["evidence"], event["actor"])
    elif kind in {"step_result", "retry"}:
        _fields(data, {"step_id"} if kind == "retry" else {"step_id", "status", "evidence"},
                () if kind == "retry" else {"read_back"})
        item = next((i for i in result["items"] if i["id"] == data["step_id"]), None)
        require(item is not None, "Unknown setup step")
        require(item["status"] != "verified", "This step is already verified; do not perform it twice")
        if kind == "retry":
            require(item["status"] in {"blocked", "failed", "deferred"}, "Only an unfinished reported step can be retried")
            require(item["status"] != "deferred" or event["actor"] == "user", "Only the user resumes deferred setup")
            require(result["capabilities"][item["capability"]]["available"], "Retry needs an available capability")
            item["status"], item["result"] = "pending", None
        else:
            status = data["status"]
            require(status in STATUSES - {"pending"}, "Invalid reported setup status")
            require(item["status"] == "pending", "Retry the unfinished step before submitting another result")
            _text(data["evidence"], "step evidence")
            require(status != "deferred" or event["actor"] == "user", "Only the user defers setup")
            if status == "verified":
                require(not result["memory"].get("metadata", {}).get("common_review_required"),
                        "Review the combined existing and incoming profile before applying native setup")
                require(result["capabilities"][item["capability"]]["available"], "Required host capability is unavailable")
                _text(data.get("read_back"), "actual read-back evidence")
            elif "read_back" in data:
                _text(data["read_back"], "read-back evidence")
            item["status"] = status
            item["result"] = _make_result(result, item, status, data["evidence"], event["actor"], data.get("read_back"))
            if status == "verified":
                item["last_verification"] = copy.deepcopy(item["result"])
    else:
        require(False, "Unsupported setup event")
    result["events"].append({"id": event["id"], "sha256": digest(event), "type": kind,
                             "actor": event["actor"], "at": now()})
    result["revision"] += 1
    validate_setup(result)
    return result


def setup_view(state):
    validate_setup(state)
    unreviewed = sorted(set(_unreviewed(state["memory"], state["delivery"]["setup_scope"]))
                        | set(state["delivery"].get("pending_record_ids", [])))
    common_review_required = state["memory"].get("metadata", {}).get("common_review_required") is True
    items = state["items"]
    if unreviewed or common_review_required:
        status = "needs_review"
    elif not state["delivery"]["setup_scope"]:
        status = "portable_workspace_only"
    elif not items:
        status = "no_configuration_items"
    elif all(item["status"] == "verified" for item in items):
        status = "completed_within_requested_scope"
    else:
        status = "needs_setup"
    return {"id": state["id"], "revision": state["revision"], "status": status,
            "recipient": copy.deepcopy(state["delivery"]["recipient"]),
            "setup_scope": copy.deepcopy(state["delivery"]["setup_scope"]),
            "portable_workspace": copy.deepcopy(state["portable_workspace"]),
            "capabilities": copy.deepcopy(state["capabilities"]), "items": copy.deepcopy(items),
            "unreviewed_record_ids": unreviewed,
            "common_review_required": common_review_required,
            "summary": {"total": len(items), **{s: sum(i["status"] == s for i in items) for s in sorted(STATUSES)}},
            "verification": "host_attestation_for_native_steps",
            "limitations": [
                "Native setup results are host attestations, not independent API verification by this program.",
                "No imported instructions are executed; integrations and automations require actual host tools and authorization.",
                "Portable workspace files were checked when this snapshot was created; capabilities are not monitored in the background.",
                "A previous capability snapshot is reusable only for the same recipient identity; the host must report access changes.",
                "Snapshots do not synchronize separate agents and contain no reusable authorization grants."]}


def render_setup_view(view):
    statuses = {"portable_workspace_only": "Получена переносимая папка. Дополнительная настройка не запрошена.",
                "needs_review": "Часть данных ещё ждёт проверки пользователем.",
                "no_configuration_items": "В выбранном объёме нет подтверждённых записей для настройки.",
                "completed_within_requested_scope": "Все запрошенные шаги подтверждены принимающим агентом.",
                "needs_setup": "Папка получена; настройка принимающего агента ещё продолжается."}
    lines = ["# Настройка принимающего агента", "",
             "Подтверждение настроек — отчёт принимающего агента о выполненном действии и проверке результата. "
             "Эта программа сама не подключает сервисы и не проверяет их через API.", "",
             statuses[view["status"]], "", "Папка с данными: проверена чтением файлов при создании этого отчёта.",
             "Получатель: " + view["recipient"]["agent_id"] + " / " + view["recipient"]["workspace_id"], ""]
    for item in view["items"]:
        lines.append("- " + item["title"] + ": " + _STATUS_LABELS[item["status"]] + ".")
        if item["result"]:
            lines.append("  Основание: " + item["result"]["evidence"])
            if item["result"].get("read_back"):
                lines.append("  Проверка результата: " + item["result"]["read_back"])
        elif not view["capabilities"][item["capability"]]["available"]:
            lines.append("  Доступность нужного действия у получателя пока не подтверждена.")
    if view["unreviewed_record_ids"]:
        lines += ["", "Ожидают проверки: " + ", ".join(view["unreviewed_record_ids"]) + ". Автоматически не применены."]
    if view.get("common_review_required"):
        lines += ["", "Объединённый профиль требует общей проверки входящих сведений вместе с прежней памятью получателя."]
    lines += ["", "Установка навыка не запускает содержащиеся в нём инструкции. "
              "Права доступа проверяются у получателя; данные для входа не переносятся.", ""]
    return "\n".join(lines)
