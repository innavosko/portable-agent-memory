"""Immutable folder deliveries, independent of the selected cloud provider.

Files are written/read in the execution environment. Cloud upload/download is
performed by actual host tools; a local sync folder is not upload confirmation.
Tickets must come from the agreed sender handoff. Hashes prove consistency with
that ticket, not the identity of its author.
"""
import copy
import hashlib
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path

from .model import canonical, check_no_secret_fields, now, require
from .package import MAX_TOTAL, decode_json, json_bytes, read_package, safe_name, write_new
from .storage import create_route, normalize_location

MAX_PACKAGE = MAX_TOTAL + 5_000_000
MAX_HEADER = 1_000_000
SCOPE = {"profile", "projects", "skills", "integrations", "automations"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
CONTENTS = {"manifest.json", "memory.zip", "READY"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def identifier(value, label):
    require(isinstance(value, str) and _ID.fullmatch(value) is not None, f"Invalid {label}")
    safe_name(value)
    return value


def identity(value):
    require(isinstance(value, dict) and set(value) == {"agent_id", "workspace_id"}, "Recipient/sender requires agent_id and workspace_id")
    for name in value:
        require(isinstance(value[name], str) and 0 < len(value[name].strip()) <= 160, f"Invalid {name}")
    check_no_secret_fields(value)
    return copy.deepcopy(value)


def checked_path(value, boundary=None):
    require(isinstance(value, (str, os.PathLike)) and str(value).strip(), "An explicit file or folder path is required")
    path = Path(os.path.abspath(value))
    targets = [path]
    if boundary is not None:
        boundary = Path(os.path.abspath(boundary))
        require(path == boundary or boundary in path.parents, "Path is outside the selected folder")
        for parent in path.parents:
            targets.append(parent)
            if parent == boundary:
                break
    else:
        targets.extend(path.parents)
    for target in targets:
        try:
            try:
                info = target.lstat()
            except FileNotFoundError:
                continue
            # st_reparse_tag is available on supported Windows Python versions;
            # Path.is_junction() only arrived in Python 3.12.
            linked = stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) == 0xA0000003
        except PermissionError:
            # A host can grant a selected descendant while denying metadata on
            # AppData/home. Never probe past that grant. Files and all paths
            # inside an explicitly selected boundary still must be inspectable.
            if boundary is None and target != path:
                break
            raise
        require(not linked, "Links/junctions are not supported in a delivery")
    return path


def bounded_read(path, limit, boundary=None):
    path = checked_path(path, boundary)
    require(path.is_file() and path.stat().st_size <= limit, f"Missing or oversized delivery file: {path.name}")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, f"Delivery file grew beyond its limit: {path.name}")
    return raw


def inspect_snapshot(raw):
    require(isinstance(raw, bytes) and len(raw) <= MAX_PACKAGE, "Invalid package snapshot")
    with tempfile.TemporaryDirectory(prefix="memory-delivery-check-") as temp:
        file = Path(temp) / "memory.zip"
        file.write_bytes(raw)
        return read_package(file)


def validate_ticket(ticket):
    fields = {"format", "schema_version", "transfer_id", "version", "manifest_sha256", "package_sha256", "package_id", "recipient"}
    require(isinstance(ticket, dict) and set(ticket) == fields, "Invalid delivery ticket")
    require(ticket["format"] == "portable-memory-delivery-ticket" and type(ticket["schema_version"]) is int and ticket["schema_version"] == 1, "Unsupported delivery ticket")
    identifier(ticket["transfer_id"], "transfer_id")
    require(type(ticket["version"]) is int and 1 <= ticket["version"] <= 999999, "Invalid delivery version")
    for name in ("manifest_sha256", "package_sha256"):
        require(isinstance(ticket[name], str) and _HASH.fullmatch(ticket[name]), f"Invalid {name}")
    require(isinstance(ticket["package_id"], str) and ticket["package_id"], "Missing package id")
    if ticket["recipient"] is not None:
        identity(ticket["recipient"])
    canonical(ticket)


def _validate_manifest(manifest):
    fields = {"format", "schema_version", "transfer_id", "migration_id", "version", "package_id", "sender", "recipient", "route", "setup_scope", "created_at", "files"}
    require(isinstance(manifest, dict) and set(manifest) == fields, "Invalid delivery manifest")
    require(manifest["format"] == "portable-memory-delivery" and type(manifest["schema_version"]) is int and manifest["schema_version"] == 1, "Unsupported delivery manifest")
    identifier(manifest["transfer_id"], "transfer_id")
    if manifest["migration_id"] is not None:
        identifier(manifest["migration_id"], "migration_id")
    require(type(manifest["version"]) is int and 1 <= manifest["version"] <= 999999, "Invalid version")
    identity(manifest["sender"])
    if manifest["recipient"] is not None:
        identity(manifest["recipient"])
    require(isinstance(manifest["package_id"], str) and manifest["package_id"], "Missing package id")
    require(isinstance(manifest["created_at"], str) and manifest["created_at"], "Missing creation time")
    require(isinstance(manifest["setup_scope"], list) and all(isinstance(s, str) and s in SCOPE for s in manifest["setup_scope"]) and
            len(set(manifest["setup_scope"])) == len(manifest["setup_scope"]), "Invalid setup scope")
    from .storage import validate_route
    validate_route(manifest["route"])
    require(isinstance(manifest["files"], dict) and set(manifest["files"]) == {"memory.zip"}, "Unexpected delivery inventory")
    item = manifest["files"]["memory.zip"]
    require(isinstance(item, dict) and set(item) == {"sha256", "bytes"} and type(item["bytes"]) is int and 0 < item["bytes"] <= MAX_PACKAGE and
            isinstance(item["sha256"], str) and _HASH.fullmatch(item["sha256"]), "Invalid package inventory")
    canonical(manifest)


def _ticket(manifest, raw):
    result = {"format": "portable-memory-delivery-ticket", "schema_version": 1,
              "transfer_id": manifest["transfer_id"], "version": manifest["version"],
              "manifest_sha256": sha(raw), "package_sha256": manifest["files"]["memory.zip"]["sha256"],
              "package_id": manifest["package_id"], "recipient": manifest["recipient"]}
    validate_ticket(result)
    return result


def _ready(ticket):
    return {"format": "portable-memory-ready", "schema_version": 1,
            "transfer_id": ticket["transfer_id"], "version": ticket["version"],
            "manifest_sha256": ticket["manifest_sha256"], "package_sha256": ticket["package_sha256"]}


def verify_delivery(folder, ticket, recipient=None):
    """Verify one fixed snapshot against expectations supplied outside the folder."""
    validate_ticket(ticket)
    if recipient is not None:
        recipient = identity(recipient)
        require(ticket["recipient"] is None or ticket["recipient"] == recipient, "This package is intended for another recipient/workspace")
    folder = checked_path(folder)
    require(folder.is_dir(), "Delivery folder is not available")
    names = {entry.name for entry in folder.iterdir()}
    missing, extra = CONTENTS - names, names - CONTENTS
    require(not missing, "Copy is not complete; missing: " + ", ".join(sorted(missing)))
    require(not extra, "Unexpected delivery files: " + ", ".join(sorted(extra)))
    raw_manifest = bounded_read(folder / "manifest.json", MAX_HEADER, folder)
    require(sha(raw_manifest) == ticket["manifest_sha256"], "Delivery manifest differs from the expected sender ticket")
    manifest = decode_json(raw_manifest, "delivery manifest")
    _validate_manifest(manifest)
    require(_ticket(manifest, raw_manifest) == ticket, "Transfer, version or package differs from the agreed ticket")
    ready = decode_json(bounded_read(folder / "READY", 16_384, folder), "READY")
    require(ready == _ready(ticket), "Invalid or stale READY marker")
    raw_package = bounded_read(folder / "memory.zip", MAX_PACKAGE, folder)
    require(len(raw_package) == manifest["files"]["memory.zip"]["bytes"] and sha(raw_package) == ticket["package_sha256"], "Copied package is incomplete or changed")
    package = inspect_snapshot(raw_package)
    require(package["manifest"]["package_id"] == ticket["package_id"], "Inner package id does not match the delivery")
    return {"manifest": manifest, "ticket": copy.deepcopy(ticket), "package": package, "raw_package": raw_package,
            "report": {"verified": True, "transfer_id": ticket["transfer_id"], "version": ticket["version"],
                       "package_sha256": ticket["package_sha256"], "recipient": recipient,
                       "files_checked": sorted(CONTENTS), "native_memory": "not_applied",
                       "limitations": ["Hashes confirm the expected bytes, not the sender's identity.",
                                       "Checking a local copy does not prove its cloud location or publication."]}}


def prepare_delivery(package_path, root, config):
    require(isinstance(config, dict) and {"sender", "recipient", "version", "route", "setup_scope"} <= set(config) and
            not set(config) - {"transfer_id", "migration_id", "sender", "recipient", "version", "route", "setup_scope"}, "Invalid delivery configuration")
    route = create_route(config["route"])
    raw = bounded_read(package_path, MAX_PACKAGE)
    package = inspect_snapshot(raw)
    transfer_id = identifier(config.get("transfer_id") or str(uuid.uuid4()), "transfer_id")
    manifest = {"format": "portable-memory-delivery", "schema_version": 1, "transfer_id": transfer_id,
                "migration_id": config.get("migration_id"), "version": config["version"], "package_id": package["manifest"]["package_id"],
                "sender": identity(config["sender"]), "recipient": identity(config["recipient"]) if config["recipient"] is not None else None,
                "route": route, "setup_scope": copy.deepcopy(config["setup_scope"]), "created_at": now(),
                "files": {"memory.zip": {"sha256": sha(raw), "bytes": len(raw)}}}
    _validate_manifest(manifest)
    root = checked_path(root)
    folder = checked_path(root / "Перенос памяти" / transfer_id / "outbox" / f"package-v{manifest['version']:04d}", root)
    if folder.exists():
        # Re-running preparation may return exactly the same immutable version.
        existing_raw = bounded_read(folder / "manifest.json", MAX_HEADER, root)
        existing = decode_json(existing_raw, "existing delivery manifest")
        _validate_manifest(existing)
        require({k: v for k, v in existing.items() if k != "created_at"} == {k: v for k, v in manifest.items() if k != "created_at"},
                "This delivery version already contains other data; prepare a new version")
        ticket = _ticket(existing, existing_raw)
        verify_delivery(folder, ticket)
        reused = True
    else:
        manifest_raw = json_bytes(manifest)
        ticket = _ticket(manifest, manifest_raw)
        folder.mkdir(parents=True)
        # A partially written directory has no valid READY; never overwrite it.
        write_new(folder / "memory.zip", raw)
        write_new(folder / "manifest.json", manifest_raw)
        write_new(folder / "READY", json_bytes(_ready(ticket)))
        verify_delivery(folder, ticket)
        reused = False
    from .storage import route_view
    return {"folder": str(folder), "ticket": ticket, "reused": reused,
            "route": route_view(route), "publication": "not_verified",
            "copy_folder_name": folder.name,
            "recipient_relative_folder": f"Перенос памяти/{transfer_id}/inbox/{folder.name}",
            "message": "Пакет подготовлен. Передайте папку версии и карточку передачи выбранному получателю. Размещение в облаке проверяется отдельно."}


def confirm_publication(observed_folder, ticket, location, observation):
    location = normalize_location(location)
    require(location["provider"] not in {"local", "chat_upload"} and location["access"] == "verified" and location["capabilities"]["read"],
            "Cloud publication requires a real cloud read-back observation, not a local/manual location")
    require(isinstance(observation, str) and observation.strip(), "Describe the actual host read-back result")
    # Apply the same credential/link guard as other route evidence.
    normalize_location({**location, "evidence": observation})
    # This assertion comes from the host; the function itself performs no HTTP.
    check = verify_delivery(observed_folder, ticket)
    expected = check["manifest"]["route"]["sender"]
    require(location["provider"] == expected["provider"]
            and (expected["resource_id"] is None or location["resource_id"] == expected["resource_id"]),
            "Observed publication is in a different location; agree the changed route and prepare a new delivery version")
    return {"format": "portable-memory-publication", "schema_version": 1, "ticket": ticket,
            "location": location, "observation": observation, "verified_at": now(),
            "files_verified": check["report"]["files_checked"], "publication": "host_readback_verified",
            "basis": "Host supplied the downloaded copy and its actual cloud observation; no network request is made by this command."}
