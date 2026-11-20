"""Reviewable merge plans. Application writes a portable workspace, not host memory."""
import copy
import os
import tempfile
from pathlib import Path

from .errors import ValidationError
from .model import digest, empty_memory, now, render_profile, require, validate_memory
from .package import json_bytes, read_package, write_new


def _check_workspace_paths(output, files, *, windows=None):
    """Check legacy Windows limits before creating a partial import tree."""
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return
    output = Path(output).absolute()
    # Match the short staging names used below, including tempfile's 8-char ID.
    staged = output.parent / ".pm-12345678" / "w"
    relative = ["memory.json", "ACTIVE_PROFILE.md", "PROFILE.md", "receipt.json"] + ["incoming/" + name for name in files]
    for base in (output, staged):
        for name in relative:
            path = base / name
            units = len(str(path).encode("utf-16-le")) // 2
            parent_units = len(str(path.parent).encode("utf-16-le")) // 2
            require(units < 260 and parent_units < 248,
                    "Windows receiving path is too long. Choose a shorter output folder and retry; no new workspace was written.")


def identity(record):
    return (record["kind"], record["scope"].strip().casefold(), record["title"].strip().casefold())


def meaning(record):
    # New evidence or graph edges are changes too; never silently discard them.
    return digest({k: v for k, v in record.items() if k not in {"id", "review"}})


def plan_import(package, current=None):
    data = read_package(package)
    known_current = current is not None
    current = copy.deepcopy(current if current is not None else empty_memory())
    validate_memory(current)
    by_id = {r["id"]: r for r in current["records"]}
    incoming_keys = [identity(r) for r in data["memory"]["records"]]
    items = []
    for incoming in data["memory"]["records"]:
        matches = [r for r in current["records"] if identity(r) == identity(incoming)]
        same = by_id.get(incoming["id"])
        if same is not None and all(r["id"] != same["id"] for r in matches):
            matches.append(same)
        if incoming["review"]["decision"] == "pending":
            status, action = "unreviewed", "defer"
        elif incoming_keys.count(identity(incoming)) > 1:
            status, action = "ambiguous", "defer"
        elif len(matches) > 1:
            status, action = "ambiguous", "defer"
        elif matches and matches[0]["id"] == incoming["id"] and meaning(matches[0]) == meaning(incoming) and matches[0]["review"]["decision"] == incoming["review"]["decision"]:
            status, action = "duplicate", "skip"
        elif matches:
            status, action = "conflict", "defer"
        else:
            status, action = "new", "add"
        items.append({"id": incoming["id"], "title": incoming["title"], "status": status,
                      "current_ids": [r["id"] for r in matches], "action": action})
    # Until aliases/conflicts are reconciled, do not add new records with broken
    # relationships to deferred incoming entities (including transitive links).
    incoming_by_id = {r["id"]: r for r in data["memory"]["records"]}
    changed = True
    while changed:
        changed = False
        deferred = {item["id"] for item in items if item["action"] == "defer"}
        for item in items:
            if item["status"] == "new" and any(link["target"] in deferred for link in incoming_by_id[item["id"]].get("links", [])):
                item["status"], item["action"] = "blocked_dependency", "defer"
                changed = True
    return {"format": "portable-memory-import-plan", "schema_version": 1,
            "package_sha256": data["sha256"], "current_sha256": digest(current),
            "current_memory": current, "current_was_provided": known_current,
            "items": items, "target": "portable_workspace",
            "limitations": ["Native memory, skills installation, accounts and authorizations are not changed."] +
                           ([] if known_current else ["Current host memory was not provided; host conflicts are unknown."])}


def apply_import(package, plan, output, *, accepted=False):
    require(accepted, "Explicit acceptance of the merge plan is required")
    require(isinstance(plan, dict) and plan.get("format") == "portable-memory-import-plan" and
            plan.get("schema_version") == 1 and plan.get("target") == "portable_workspace", "Invalid import plan")
    current = plan.get("current_memory")
    validate_memory(current)
    require(plan.get("current_sha256") == digest(current), "Current snapshot changed; regenerate plan")
    expected = plan_import(package, current if plan.get("current_was_provided") else None)
    require(expected["current_sha256"] == plan["current_sha256"], "Current snapshot mismatch")
    require(expected["package_sha256"] == plan.get("package_sha256"), "Package changed; regenerate plan")
    require(isinstance(plan.get("items"), list) and len(plan["items"]) == len(expected["items"]), "Plan items changed")
    data = read_package(package)
    require(data["sha256"] == plan["package_sha256"], "Package changed during import")
    incoming = {r["id"]: r for r in data["memory"]["records"]}
    merged = copy.deepcopy(current)
    records = {r["id"]: r for r in merged["records"]}
    receipt = []
    for item, base in zip(plan["items"], expected["items"]):
        require(isinstance(item, dict) and {k: v for k, v in item.items() if k != "action"} ==
                {k: v for k, v in base.items() if k != "action"}, "Plan classification changed; regenerate")
        allowed = {"new": {"add", "defer"}, "duplicate": {"skip"},
                   "conflict": {"replace", "keep_current", "defer"},
                   "ambiguous": {"defer"}, "unreviewed": {"defer"}, "blocked_dependency": {"defer"}}[base["status"]]
        action = item.get("action")
        require(action in allowed, f"Invalid action for {item['id']}: {action}")
        if action in {"add", "replace"}:
            record = copy.deepcopy(incoming[item["id"]])
            if action == "replace":
                replaced_id = item["current_ids"][0]
                # Preserve the local ID so existing links continue to refer to the same entity.
                if replaced_id != record["id"]:
                    require(False, "Different IDs for one entity: reconcile IDs and review before replacing")
                records.pop(replaced_id)
            records[record["id"]] = record
        receipt.append({"id": item["id"], "action": action,
                        "result": "stored_in_portable_workspace" if action in {"add", "replace"} else action})
    merged["records"] = list(records.values())
    for key in ("read", "unavailable", "excluded"):
        merged["coverage"][key] = list(dict.fromkeys(merged["coverage"][key] + data["memory"]["coverage"][key]))
    merged.setdefault("metadata", {}).pop("common_review_required", None)
    merged["metadata"].update(copy.deepcopy(data["memory"].get("metadata", {})))
    merged["metadata"]["last_import"] = {"package_sha256": data["sha256"], "at": now()}
    validate_memory(merged)
    # Keeping an older conflicting rule must not detach a skill from the exact
    # project instructions it was reviewed with. The JSON current snapshot does
    # not supply old assets. A complete incoming version must include files of
    # retained bound skills too; never publish a new workspace without them.
    from .skill_context import validate_skill_context
    validate_skill_context(merged, data["files"])
    from .common_profile import validate_export_review
    try:
        validate_export_review(merged, data["review"])
        common_review = "checked_for_combined_profile"
    except ValidationError:
        # A valid incoming package does not certify a different union with the
        # recipient's existing profile. Preserve actual user decisions, stage
        # the files, and block active-profile use until the common review runs.
        supplied = {r["id"]: r for r in data["memory"]["records"]}
        retained = {r["id"]: r for r in merged["records"]}
        unchanged_subset = all(rid in supplied and supplied[rid] == record for rid, record in retained.items())
        only_pending_omitted = all(r["review"]["decision"] == "pending" for rid, r in supplied.items() if rid not in retained)
        if unchanged_subset and only_pending_omitted and not current["records"]:
            # The first import may defer pending facts from an already checked
            # partial package. No new union or changed fact was introduced.
            common_review = "checked_subset_pending_not_applied"
        else:
            merged["metadata"].pop("common_profile", None)
            merged["metadata"]["common_review_required"] = True
            common_review = "required_for_combined_profile"
    output = Path(output)
    require(not output.exists(), f"Output already exists: {output}")
    _check_workspace_paths(output, data["files"])
    report = {"format": "portable-memory-import-receipt", "created_at": now(),
              "package_sha256": data["sha256"], "target": "portable_workspace", "records": receipt,
              "native_memory": "not_applied", "integrations": "not_connected", "skills": "not_installed",
              "common_review": common_review,
              "limitations": expected["limitations"]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pm-", dir=output.parent) as temp:
        stage = Path(temp) / "w"
        stage.mkdir()
        write_new(stage / "memory.json", json_bytes(merged))
        write_new(stage / "ACTIVE_PROFILE.md", render_profile(merged, active_only=True))
        write_new(stage / "PROFILE.md", render_profile(merged))
        write_new(stage / "receipt.json", json_bytes(report))
        for name, content in data["files"].items():
            write_new(stage / "incoming" / name, content)
        # Verify before making the new workspace visible at its final location.
        from .package import load_json
        require(load_json(stage / "memory.json") == merged, "Output verification failed")
        require(not output.exists(), f"Output appeared during import: {output}")
        os.rename(stage, output)
    return report
