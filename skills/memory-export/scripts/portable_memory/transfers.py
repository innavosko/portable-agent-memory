"""Evidence ledger for a transfer coordinated by two agents.

This module makes NO connector calls, sends no notifications and schedules no
background job. The host supplies observations after those actions succeeded.
Only one named owner watches a source. ``expected_revision`` protects one shared
authoritative card; it cannot coordinate independent copies in SaaS sessions.
Concurrent cloud writers need a store with conditional writes. Otherwise use an
explicit handoff of the latest card and stop the previous watcher.
The same owner retrieves the raw source. The recipient independently receives
and verifies the resulting memory package. ``notified`` records one readiness
notification per transfer, not a notification for every milestone or completion.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from .errors import ValidationError


CAPABILITIES = {"email_read", "drive_read", "download", "background", "notify"}
CHANNELS = {"email", "drive", "upload"}
MODES = {"manual", "session", "background"}
STATES = {"waiting", "source_ready", "downloaded", "package_ready", "data_verified", "imported", "cancelled"}
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SECRET = re.compile(r"(?i)(bearer\s+|(?:access_token|refresh_token|password|api_key|token|signature)\s*[=:])")


def _text(value, label, limit=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValidationError(f"{label}: expected non-empty text, maximum {limit} characters")
    if _SECRET.search(value):
        raise ValidationError(f"{label}: use a non-secret description, not credentials or a signed link")
    return value


def _sha(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValidationError("sha256: expected 64 lowercase hexadecimal characters")
    return value


def _now():
    return datetime.now(timezone.utc).isoformat()


def _route(kind, locator, label):
    if not isinstance(kind, str) or kind not in CHANNELS:
        raise ValidationError(f"{label}_kind must be email, drive or upload")
    _text(locator, f"{label}_locator")
    if "://" in locator and ("?" in locator or "#" in locator):
        raise ValidationError(f"{label}_locator must not contain a URL query or fragment; use a stable ID")


def _readable(capabilities, channel):
    return channel == "upload" or capabilities.get(f"{channel}_read") is True


def _watch_mode(card, owner, mode):
    if not isinstance(owner, str) or not isinstance(mode, str) or owner not in card["capabilities"] or mode not in MODES:
        raise ValidationError("Unknown watch owner or mode")
    caps = card["capabilities"][owner]
    if mode != "manual" and not _readable(caps, card["source_kind"]):
        raise ValidationError("Automatic checking needs the owner's source read capability; use manual mode")
    if mode == "background":
        if card["source_kind"] == "upload":
            raise ValidationError("A chat upload has no background source to watch")
        if not caps["background"] or not caps["notify"]:
            raise ValidationError("Background watch needs both a real background runner and notifications")


def create_transfer(source_agent, recipient_agent, source_kind, source_locator,
                    owner, capabilities, *, mode="session", transfer_id=None,
                    destination_kind="upload",
                    destination_locator="User supplies prepared package in recipient chat"):
    """Create a JSON-serializable card. Capability flags mean verified abilities.

    ``source_locator`` is a folder/mailbox description or stable resource ID,
    never a token or temporary signed download URL. Unknown abilities are false.
    The host still must actually create a background job before claiming one is
    running; selecting background mode only establishes capability eligibility.
    """
    source_agent = _text(source_agent, "source_agent", 120)
    recipient_agent = _text(recipient_agent, "recipient_agent", 120)
    if source_agent == recipient_agent:
        raise ValidationError("Use distinct source and recipient agent IDs")
    _route(source_kind, source_locator, "source")
    _route(destination_kind, destination_locator, "destination")
    if not isinstance(capabilities, dict) or set(capabilities) != {source_agent, recipient_agent}:
        raise ValidationError("Provide capabilities for both agents")
    normalized = {}
    for actor, supplied in capabilities.items():
        if not isinstance(supplied, dict) or set(supplied) - CAPABILITIES:
            raise ValidationError("Unknown capability")
        if any(type(value) is not bool for value in supplied.values()):
            raise ValidationError("Capability values must be booleans; unknown means false")
        normalized[actor] = {name: supplied.get(name, False) for name in sorted(CAPABILITIES)}
    card = {
        "format": "portable-memory-transfer", "schema_version": 1,
        "id": _text(transfer_id or str(uuid4()), "transfer_id", 120),
        "revision": 0, "source_agent": source_agent, "recipient_agent": recipient_agent,
        "source_kind": source_kind, "source_locator": source_locator,
        "destination_kind": destination_kind, "destination_locator": destination_locator,
        "owner": owner, "mode": mode, "capabilities": normalized,
        "state": "waiting", "access": {}, "download_sha256": None,
        "package_sha256": None, "receipt": None, "events": [], "created_at": _now(),
    }
    _watch_mode(card, owner, mode)
    return card


def validate_transfer(card):
    """Validate card structure, without treating supplied evidence as independent proof."""
    if not isinstance(card, dict):
        raise ValidationError("Transfer card must be an object")
    try:
        if card["format"] != "portable-memory-transfer" or card["schema_version"] != 1:
            raise ValidationError("Unsupported transfer format")
        base = create_transfer(card["source_agent"], card["recipient_agent"],
                               card["source_kind"], card["source_locator"],
                               card["owner"], card["capabilities"], mode=card["mode"],
                               transfer_id=card["id"], destination_kind=card["destination_kind"],
                               destination_locator=card["destination_locator"])
        if set(card) != set(base):
            raise ValidationError("Unexpected or missing transfer fields")
        if card["state"] not in STATES or type(card["revision"]) is not int:
            raise ValidationError("Invalid transfer state or revision")
        if not isinstance(card["events"], list) or card["revision"] != len(card["events"]):
            raise ValidationError("Revision must equal event count")
        event_ids = set()
        for item in card["events"]:
            if not isinstance(item, dict) or set(item) != {"id", "sha256", "type", "actor", "at"}:
                raise ValidationError("Malformed event receipt")
            _text(item["id"], "event id", 120)
            _sha(item["sha256"])
            if item["id"] in event_ids or item["actor"] not in card["capabilities"]:
                raise ValidationError("Duplicate event or unknown actor")
            event_ids.add(item["id"])
        if not isinstance(card["access"], dict) or set(card["access"]) - set(card["capabilities"]):
            raise ValidationError("Invalid access records")
        for actor, access in card["access"].items():
            _validate_access(card, actor, access)
        for name in ("download_sha256", "package_sha256"):
            if card[name] is not None:
                _sha(card[name])
        if card["state"] in {"package_ready", "data_verified", "imported"} and card["package_sha256"] is None:
            raise ValidationError("Package state requires a package digest")
        if card["state"] == "downloaded" and card["download_sha256"] is None:
            raise ValidationError("Downloaded state requires a download digest")
        if card["receipt"] is not None:
            _receipt(card["receipt"])
        if card["state"] == "imported" and card["receipt"] is None:
            raise ValidationError("Imported state requires an explicit receipt")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValidationError(f"Malformed transfer card: {exc}") from exc


def _fields(data, required, optional=()):
    if set(data) - set(required) - set(optional) or set(required) - set(data):
        raise ValidationError(f"Event data fields must be {sorted(required)} (optional: {sorted(optional)})")


def _validate_access(card, actor, data):
    _fields(data, {"channel", "available", "evidence"})
    if not isinstance(data["channel"], str) or data["channel"] not in CHANNELS or type(data["available"]) is not bool:
        raise ValidationError("Invalid access channel or availability")
    _text(data["evidence"], "access evidence")
    if data["available"] and not _readable(card["capabilities"][actor], data["channel"]):
        raise ValidationError("Read access is not supported by this actor's verified capabilities")


def _own_access(card, actor):
    access = card["access"].get(actor)
    if not access or not access["available"]:
        raise ValidationError("The actor must confirm its own access; the other agent's access is insufficient")
    return access


def _receipt(data):
    _fields(data, {"receipt_kind", "evidence"})
    if not isinstance(data["receipt_kind"], str) or data["receipt_kind"] not in {"portable_workspace", "native_memory"}:
        raise ValidationError("Specify portable_workspace or native_memory; installing skills is separate")
    _text(data["evidence"], "import evidence")


def apply_event(card, event, *, expected_revision):
    """Return a new card after recording a completed action.

    Event structure: {id, actor, type, data}. Exact replay is a no-op even with
    the original revision. Reusing an ID for changed content is rejected.
    This does not execute the action described by the event.
    """
    validate_transfer(card)
    if not isinstance(event, dict) or set(event) != {"id", "actor", "type", "data"}:
        raise ValidationError("Event must contain id, actor, type and data")
    _text(event["id"], "event id", 120)
    actor, kind, data = event["actor"], event["type"], event["data"]
    if not isinstance(actor, str) or actor not in card["capabilities"] or not isinstance(data, dict):
        raise ValidationError("Unknown actor or invalid event data")
    try:
        fingerprint = hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ValidationError("Event must be valid JSON") from exc
    for previous in card["events"]:
        if previous["id"] == event["id"]:
            if previous["sha256"] != fingerprint:
                raise ValidationError("Event ID was already used with different content")
            return copy.deepcopy(card)
    if type(expected_revision) is not int or expected_revision != card["revision"]:
        raise ValidationError("Stale transfer revision; reload the authoritative card before retrying")
    if card["state"] in {"cancelled", "imported"}:
        raise ValidationError("Transfer is closed")
    updated = copy.deepcopy(card)
    state = card["state"]
    if kind == "access_checked":
        _validate_access(card, actor, data)
        updated["access"][actor] = copy.deepcopy(data)
    elif kind == "handoff":
        _fields(data, {"owner", "mode", "evidence"})
        if actor != card["owner"]:
            raise ValidationError("Only the current owner may explicitly hand off the watch")
        _text(data["evidence"], "handoff evidence")
        _watch_mode(card, data["owner"], data["mode"])
        updated["owner"], updated["mode"] = data["owner"], data["mode"]
    elif kind == "rerouted":
        _fields(data, {"destination_kind", "destination_locator", "evidence"})
        _text(data["evidence"], "route-change evidence")
        _route(data["destination_kind"], data["destination_locator"], "destination")
        if state == "data_verified":
            raise ValidationError("Destination cannot change after recipient verification; start a new transfer")
        updated["destination_kind"] = data["destination_kind"]
        updated["destination_locator"] = data["destination_locator"]
        updated["access"].pop(card["recipient_agent"], None)
    elif kind == "source_ready":
        _fields(data, {"evidence"})
        _text(data["evidence"], "readiness evidence")
        if actor != card["owner"] or state != "waiting":
            raise ValidationError("Only the watch owner may mark a waiting source ready")
        if card["mode"] != "manual":
            access = _own_access(card, actor)
            if access["channel"] != card["source_kind"]:
                raise ValidationError("Watch owner has not checked the configured source")
        updated["state"] = "source_ready"
    elif kind == "downloaded":
        _fields(data, {"evidence", "sha256", "method"})
        _text(data["evidence"], "download evidence")
        _sha(data["sha256"])
        if actor != card["owner"]:
            raise ValidationError("Only the current owner retrieves the raw source; explicitly hand off first")
        if state != "source_ready" or not isinstance(data["method"], str) or data["method"] not in {"tool", "user_upload"}:
            raise ValidationError("Download requires a ready source and tool or user_upload method")
        access = _own_access(card, actor)
        if data["method"] == "tool" and not card["capabilities"][actor]["download"]:
            raise ValidationError("Actor cannot download; use an actual user upload")
        if data["method"] == "user_upload" and access["channel"] != "upload":
            raise ValidationError("Confirm access to the actual upload first")
        updated["download_sha256"], updated["state"] = data["sha256"], "downloaded"
    elif kind == "package_ready":
        _fields(data, {"evidence", "sha256", "origin"})
        _text(data["evidence"], "package evidence")
        _sha(data["sha256"])
        if actor != card["source_agent"] or state not in {"waiting", "source_ready", "downloaded"}:
            raise ValidationError("Only the source agent prepares a package before recipient verification")
        if not isinstance(data["origin"], str) or data["origin"] not in {"downloaded_source", "reviewed_profile"}:
            raise ValidationError("origin must be downloaded_source or reviewed_profile")
        if data["origin"] == "downloaded_source" and state != "downloaded":
            raise ValidationError("The source archive has not been downloaded")
        updated["package_sha256"], updated["state"] = data["sha256"], "package_ready"
    elif kind == "data_verified":
        _fields(data, {"evidence", "sha256"})
        _text(data["evidence"], "verification evidence")
        _sha(data["sha256"])
        if actor != card["recipient_agent"] or state != "package_ready":
            raise ValidationError("Only the recipient verifies a prepared package")
        access = _own_access(card, actor)
        if access["channel"] != card["destination_kind"]:
            raise ValidationError("Recipient must confirm its own access to the prepared package's destination")
        if data["sha256"] != card["package_sha256"]:
            raise ValidationError("Recipient verified a different package")
        updated["state"] = "data_verified"
    elif kind == "imported":
        _receipt(data)
        if actor != card["recipient_agent"] or state != "data_verified":
            raise ValidationError("Only the recipient records an import after verification")
        _own_access(card, actor)
        updated["receipt"], updated["state"] = copy.deepcopy(data), "imported"
    elif kind == "notified":
        _fields(data, {"evidence"})
        _text(data["evidence"], "notification evidence")
        if actor != card["owner"] or state == "waiting" or not card["capabilities"][actor]["notify"]:
            raise ValidationError("Only the owner with notification capability records a readiness notification")
        if any(item["type"] == "notified" for item in card["events"]):
            raise ValidationError("Readiness notification was already recorded; do not notify twice")
    elif kind == "cancelled":
        _fields(data, {"evidence"})
        _text(data["evidence"], "cancellation evidence")
        updated["state"] = "cancelled"
    else:
        raise ValidationError(f"Unsupported transfer event: {kind}")
    updated["events"].append({"id": event["id"], "sha256": fingerprint, "type": kind,
                              "actor": actor, "at": _now()})
    updated["revision"] += 1
    validate_transfer(updated)
    return updated


def next_actions(card, actor):
    """Describe pending work without claiming an external operation took place."""
    validate_transfer(card)
    if not isinstance(actor, str) or actor not in card["capabilities"]:
        raise ValidationError("Unknown actor")
    if card["state"] in {"cancelled", "imported"}:
        return ["Transfer closed; inspect the receipt for the actual import destination."]
    actions = []
    if not card["access"].get(actor, {}).get("available"):
        actions.append("Check your own access to the source or supplied file; the other agent's connection is not shared.")
    if card["state"] == "waiting":
        if actor == card["owner"]:
            actions.append({"manual": "Ask the user to supply the ready file or readiness confirmation.",
                            "session": "Check the connected source during this session; no background job is implied.",
                            "background": "Use a real authorized background job if available; the card itself does not start one."}[card["mode"]])
        else:
            actions.append(f"Watch is assigned to {card['owner']}; do not start a duplicate watch or notification.")
    elif card["state"] == "source_ready":
        if actor == card["owner"]:
            actions.append("Retrieve the raw source through an available tool or ask for an upload; readiness is not a download.")
        else:
            actions.append(f"Raw-source retrieval is assigned to {card['owner']}; wait for the prepared package or an explicit handoff, without a duplicate download.")
    elif card["state"] == "downloaded":
        actions.append("The source agent can parse the source, review selected memory and prepare the package.")
    elif card["state"] == "package_ready":
        actions.append(f"The recipient must receive and verify this exact package at {card['destination_kind']}: {card['destination_locator']}, using its own access. Package preparation does not confirm delivery.")
    elif card["state"] == "data_verified":
        actions.append("The recipient can review and apply the import plan, then record its actual result.")
    return actions
