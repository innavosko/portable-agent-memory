"""Local recipient ledger. It is not a shared cloud database or a watcher.

The supplied copy is checked against an independently received sender ticket.
An accepted import uses that same byte snapshot. The ledger prevents repeat
imports for this recipient; host setup remains a separate, explicit operation.
"""
import copy
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .delivery import (MAX_HEADER, bounded_read, checked_path, identifier,
                       identity, sha, verify_delivery)
from .importing import apply_import, plan_import
from .model import digest, now, require, validate_memory
from .package import MAX_FILE, decode_json, json_bytes, safe_name, write_new
from .setup import create_setup, setup_view, validate_setup
from .storage import normalize_location


def _key(recipient):
    return digest(identity(recipient))


def _connect(path, *, create=False):
    path = checked_path(path)
    require(create or path.is_file(), "Recipient ledger does not exist")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=15)
    connection.row_factory = sqlite3.Row
    if create:
        connection.execute("CREATE TABLE IF NOT EXISTS deliveries (transfer_id TEXT NOT NULL, version INTEGER NOT NULL, recipient_key TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(transfer_id, version, recipient_key))")
        connection.commit()
    return connection


def _find(connection, transfer_id, recipient, version=None):
    args = [transfer_id, _key(recipient)]
    sql = "SELECT transfer_id,version,recipient_key,body FROM deliveries WHERE transfer_id=? AND recipient_key=?"
    if version is not None:
        sql += " AND version=?"
        args.append(version)
    row = connection.execute(sql + " ORDER BY version DESC LIMIT 1", args).fetchone()
    if row is None:
        return None
    body = json.loads(row["body"])
    require(isinstance(body, dict) and isinstance(body.get("ticket"), dict)
            and body["ticket"].get("transfer_id") == row["transfer_id"] == transfer_id
            and body["ticket"].get("version") == row["version"]
            and body.get("recipient") == recipient
            and _key(body["recipient"]) == row["recipient_key"],
            "Recipient ledger entry does not match its transfer, version or recipient")
    return body


def _check_receipt(receipt):
    require(isinstance(receipt, dict) and receipt.get("format") == "portable-memory-delivery-receipt"
            and receipt.get("schema_version") == 1, "Invalid recipient receipt")
    workspace = checked_path(receipt["workspace"])
    memory = decode_json(bounded_read(workspace / "memory.json", 50_000_000, workspace), "workspace memory")
    validate_memory(memory)
    portable = decode_json(bounded_read(workspace / "receipt.json", MAX_HEADER, workspace), "portable receipt")
    require(digest(memory) == receipt["memory_sha256"], "Imported workspace changed; review the current state before continuing")
    require(digest(portable) == receipt["portable_receipt_sha256"]
            and portable.get("format") == "portable-memory-import-receipt"
            and portable.get("package_sha256") == receipt["ticket"]["package_sha256"]
            and portable.get("target") == "portable_workspace", "Imported workspace receipt changed")
    for name, expected in receipt["incoming_files"].items():
        safe_name(name)
        raw = bounded_read(workspace / "incoming" / name, MAX_FILE, workspace)
        require(len(raw) == expected["bytes"] and sha(raw) == expected["sha256"],
                "Imported source file changed: " + name)
    state = receipt["setup"]
    validate_setup(state)
    expected = state["delivery"]
    require(expected["transfer_id"] == receipt["ticket"]["transfer_id"]
            and expected["version"] == receipt["ticket"]["version"]
            and expected["package_sha256"] == receipt["ticket"]["package_sha256"]
            and expected["recipient"] == receipt["recipient"]
            and expected["workspace"] == receipt["workspace"]
            and digest(state["memory"]) == receipt["memory_sha256"], "Setup does not belong to this delivery")
    return memory


def _public(receipt, *, reused=False, latest_version=None):
    result = copy.deepcopy(receipt)
    result["reused"] = reused
    result["setup_view"] = setup_view(receipt["setup"])
    result["status"] = "portable_workspace_prepared"
    result["next_action"] = "continue_receiving_setup"
    if latest_version is not None and latest_version > receipt["ticket"]["version"]:
        result["status"] = "superseded"
        result["next_action"] = "continue_latest_delivery"
        result["latest_version"] = latest_version
    result["limitations"] = ["This is a local recipient ledger, not cloud synchronization.",
                             "Native memory, skills and account connections require actual receiving-host tools.",
                             "Native setup results are host attestations, not operations performed by this program."]
    return result


def get_receipt(store_path, transfer_id, version, recipient):
    identifier(transfer_id, "transfer_id")
    identity(recipient)
    require(type(version) is int and version > 0, "Invalid delivery version")
    with closing(_connect(store_path)) as connection:
        receipt = _find(connection, transfer_id, recipient, version)
        latest = _find(connection, transfer_id, recipient)
    require(receipt is not None, "This delivery has not been accepted by this recipient")
    _check_receipt(receipt)
    return _public(receipt, reused=True, latest_version=latest["ticket"]["version"])


def _save(connection, receipt):
    ticket = receipt["ticket"]
    connection.execute("INSERT INTO deliveries(transfer_id,version,recipient_key,body) VALUES(?,?,?,?)",
                       (ticket["transfer_id"], ticket["version"], _key(receipt["recipient"]),
                        json.dumps(receipt, ensure_ascii=False)))


def receive_delivery(folder, ticket, recipient, store_path, root, current=None, plan=None,
                     location=None, accepted=False):
    """Preview has no writes. Acceptance creates a new portable workspace only.

    The receiving agent must provide current native memory separately if it is
    available: a previous portable workspace is not a live native-memory read.
    """
    recipient = identity(recipient)
    checked = verify_delivery(folder, ticket, recipient)
    actual_location = normalize_location(location) if location is not None else None
    root = checked_path(root)
    ledger = checked_path(store_path)
    source_folder = checked_path(folder)
    require(ledger != source_folder and source_folder not in ledger.parents,
            "Keep the recipient ledger outside the immutable delivery folder")
    connection = _connect(ledger, create=accepted) if accepted or ledger.exists() else None
    try:
        if accepted:
            # Serialize accepts by this local receiver. No distributed lock claim.
            connection.execute("BEGIN IMMEDIATE")
        prior = _find(connection, ticket["transfer_id"], recipient, ticket["version"]) if connection else None
        latest = _find(connection, ticket["transfer_id"], recipient) if connection else None
        if latest:
            require(ticket["version"] >= latest["ticket"]["version"],
                    "A newer delivery is already accepted; continue from its receipt")
        if prior is not None:
            require(prior["ticket"] == ticket, "This version was already received with different contents")
            _check_receipt(prior)
            return _public(prior, reused=True)
        if latest:
            require(ticket["version"] > latest["ticket"]["version"], "An older delivery version cannot replace the latest one")
            last_memory = _check_receipt(latest)
        else:
            last_memory = None
        memory = current if current is not None else last_memory
        if memory is not None:
            validate_memory(memory)
        with tempfile.TemporaryDirectory(prefix="portable-receive-") as temp:
            snapshot = Path(temp) / "memory.zip"
            snapshot.write_bytes(checked["raw_package"])
            current_source = "provided_snapshot" if current is not None else "previous_portable_workspace" if latest else "not_provided"
            default_plan = plan_import(snapshot, memory, current_source=current_source)
            if plan is not None:
                require(isinstance(plan, dict) and plan.get("current_sha256") == default_plan["current_sha256"]
                        and plan.get("current_was_provided") == default_plan["current_was_provided"]
                        and plan.get("current_source") == default_plan["current_source"]
                        and plan.get("package_sha256") == ticket["package_sha256"],
                        "Plan was prepared for other data; preview this delivery again")
            selected_plan = plan if plan is not None else default_plan
            if not accepted:
                return {"status": "awaiting_plan_acceptance", "ticket": copy.deepcopy(ticket),
                        "verification": checked["report"], "plan": selected_plan,
                        "previous_setup_available": latest is not None,
                        "current_source": "provided_snapshot" if current is not None else "previous_portable_workspace" if latest else "not_provided",
                        "native_memory": "not_applied", "writes_performed": False}
            destination = checked_path(root / ticket["transfer_id"] / _key(recipient)[:20]
                                       / f"package-v{ticket['version']:04d}", root)
            require(source_folder != destination and source_folder not in destination.parents,
                    "Keep imported results outside the immutable delivery folder")
            workspace = destination / "workspace"
            checkpoint = destination / "delivery-receipt.json"
            if checkpoint.exists():
                # Recovery after files were committed but before SQLite committed.
                receipt = decode_json(bounded_read(checkpoint, 100_000_000, root), "delivery checkpoint")
                require(receipt["ticket"] == ticket and receipt["recipient"] == recipient
                        and receipt["workspace"] == str(workspace)
                        and receipt["plan_sha256"] == digest(selected_plan), "Recovery checkpoint belongs to another operation")
                _check_receipt(receipt)
                _save(connection, receipt)
                connection.commit()
                return _public(receipt, reused=True)
            require(not workspace.exists(), "An interrupted import needs recovery; use a new empty result folder, without deleting existing data")
            setup_path = destination / "setup-r0000.json"
            apply_import(snapshot, selected_plan, workspace, accepted=True,
                         setup_delivery={"recipient": recipient, "setup_scope": checked["manifest"]["setup_scope"]},
                         setup_state_path=setup_path)
            imported = decode_json(bounded_read(workspace / "memory.json", 50_000_000, root), "imported memory")
            portable = decode_json(bounded_read(workspace / "receipt.json", MAX_HEADER, root), "import receipt")
            setup_delivery = {"transfer_id": ticket["transfer_id"], "version": ticket["version"],
                              "package_sha256": ticket["package_sha256"], "recipient": recipient,
                              "workspace": str(workspace), "setup_scope": checked["manifest"]["setup_scope"],
                              "pending_record_ids": portable["pending_setup_record_ids"]}
            setup = create_setup(imported, setup_delivery, latest["setup"] if latest else None)
            write_new(setup_path, json_bytes(setup))
            receipt = {"format": "portable-memory-delivery-receipt", "schema_version": 1,
                       "ticket": copy.deepcopy(ticket), "recipient": recipient, "received_at": now(),
                       "actual_location": actual_location, "workspace": str(workspace),
                       "memory_sha256": digest(imported), "portable_receipt_sha256": digest(portable),
                       "incoming_files": {name: {"sha256": sha(raw), "bytes": len(raw)}
                                          for name, raw in checked["package"]["files"].items()},
                       "plan_sha256": digest(selected_plan), "setup": setup, "setup_path": str(setup_path),
                       "native_memory": "not_applied_by_import"}
            _check_receipt(receipt)
            write_new(checkpoint, json_bytes(receipt))
            _save(connection, receipt)
            connection.commit()
            return _public(receipt)
    finally:
        if connection is not None:
            connection.close()


def save_setup(store_path, state):
    """Persist an extending host-attested setup snapshot, without doing host work."""
    validate_setup(state)
    delivery = state["delivery"]
    with closing(_connect(store_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        receipt = _find(connection, delivery["transfer_id"], delivery["recipient"], delivery["version"])
        require(receipt is not None, "Accept this delivery before saving its setup")
        _check_receipt(receipt)
        old = receipt["setup"]
        latest = _find(connection, delivery["transfer_id"], delivery["recipient"])
        if digest(old) == digest(state):
            return _public(receipt, reused=True, latest_version=latest["ticket"]["version"])
        require(latest["ticket"]["version"] == delivery["version"], "Continue setup on the latest accepted delivery")
        require(state["id"] == old["id"] and state["delivery"] == old["delivery"]
                and state["memory"] == old["memory"] and state["portable_workspace"] == old["portable_workspace"]
                and state["created_at"] == old["created_at"], "Setup identity or contents changed")
        require(state["revision"] > old["revision"] and state["events"][:old["revision"]] == old["events"],
                "Stale or diverging setup history; continue from the latest receipt")
        output = checked_path(Path(receipt["workspace"]).parent / f"setup-r{state['revision']:04d}-{digest(state)[:16]}.json")
        if output.exists():
            require(decode_json(bounded_read(output, 100_000_000), "setup checkpoint") == state, "Setup checkpoint differs")
        else:
            write_new(output, json_bytes(state))
        receipt["setup"] = copy.deepcopy(state)
        receipt["setup_path"] = str(output)
        connection.execute("UPDATE deliveries SET body=? WHERE transfer_id=? AND version=? AND recipient_key=?",
                           (json.dumps(receipt, ensure_ascii=False), delivery["transfer_id"], delivery["version"], _key(delivery["recipient"])))
        connection.commit()
    return _public(receipt)
