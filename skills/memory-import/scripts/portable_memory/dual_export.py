"""Preserve the selected portable context alongside a GigaAgent projection.

The portable package uses the reviewed source memory, never the constrained
projection. Native-memory changes and delivery through a chat remain host work.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile

from .completion import GUIDE, INSTRUCTION, RESERVED, preflight_handoff, prepare_handoff
from .delivery import checked_path
from .model import digest, require
from .package import export_package, json_bytes, read_package, safe_name
from .recipients import build_giga_export, parse_giga_export, validate_giga_preview


def _key(path):
    # Portable destinations must not depend on a case-sensitive filesystem.
    return str(path).casefold()


def _paths(output, report, full_output, handoff_output, assets):
    output = checked_path(output)
    report = checked_path(report)
    full = checked_path(full_output) if full_output else output.with_name(output.stem + "-full.zip")
    handoff = checked_path(handoff_output) if handoff_output else output.with_name(output.stem + "-import")
    paths = (output, report, full, handoff)
    require(output.suffix.lower() == ".md", "GigaAgent profile must have the .md extension")
    require(full.suffix.lower() == ".zip", "Full portable package must have the .zip extension")
    require(output.name.casefold() not in RESERVED, "Choose a profile filename distinct from the import instructions")
    for path in paths:
        safe_name(path.name)
        require(not path.exists() and not path.is_symlink(), f"Output already exists: {path}")
        parent = path.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        require(parent.is_dir(), "Export output has a parent that is not a directory")
    for index, first in enumerate(paths):
        for second in paths[index + 1:]:
            require(_key(first) != _key(second) and
                    _key(first) not in {_key(p) for p in second.parents} and
                    _key(second) not in {_key(p) for p in first.parents},
                    "Full package, profile, report and GigaAgent folder must be separate nonoverlapping paths")
    if assets is not None:
        base = checked_path(assets)
        require(base.is_dir(), "Selected assets must be an existing directory")
        for path in paths:
            require(_key(base) != _key(path) and _key(base) not in {_key(p) for p in path.parents},
                    "Export outputs must stay outside the selected assets directory")
    return output, report, full, handoff


def _identity(info):
    return info.st_dev, info.st_ino


def _publish(files, handoff):
    """Create only new outputs; roll back our own files after a write failure.

    All bytes have already been validated in staging. This is not a distributed
    transaction: a process/OS crash can leave files, but no successful result is
    returned. Exclusive creates and identity-checked cleanup preserve a competing
    writer's files rather than deleting an arbitrary destination.
    """
    created = []
    directory_identity = None
    try:
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.mkdir()
        directory_identity = _identity(handoff.lstat())
        for path, data in files:
            checked_path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                created.append((path, _identity(os.fstat(handle.fileno()))))
                handle.write(data)
    except Exception:
        for path, identity in reversed(created):
            try:
                if _identity(path.lstat()) == identity and not path.is_symlink():
                    path.unlink()
            except OSError:
                pass
        if directory_identity is not None:
            try:
                if _identity(handoff.lstat()) == directory_identity:
                    handoff.rmdir()
            except OSError:
                pass
        raise


def export_giga_bundle(memory, review, projection, preview, *, output, report,
                       instruction, full_output=None, handoff_output=None,
                       assets=None, no_assets=False, asset_scope_note=None):
    """Stage and verify both deliverables before publishing their actual files."""
    require(type(no_assets) is bool and bool(assets is not None) != no_assets,
            "Choose exactly one: --assets for selected originals, or --no-assets with --asset-scope-note")
    if asset_scope_note is not None:
        require(isinstance(asset_scope_note, str) and bool(asset_scope_note.strip()) and
                len(asset_scope_note) <= 4000 and "\x00" not in asset_scope_note,
                "Asset scope note must be nonempty text of at most 4000 characters")
    require(not no_assets or asset_scope_note is not None,
            "--no-assets requires --asset-scope-note describing the actual chosen scope or unavailable originals")
    output, report, full, destination = _paths(output, report, full_output, handoff_output, assets)
    preflight_handoff("gigaagent", destination, instruction=instruction)
    result = build_giga_export(memory, review, projection)
    require(preview is not None,
            "Prepare giga-preview, show its Markdown to the user, then supply the current preview JSON with --preview")
    validate_giga_preview(preview, memory, review, projection)
    require(bool(parse_giga_export(result["markdown"])["records"]),
            "No reviewed GigaAgent facts are ready to import; resolve the profile review first")

    with tempfile.TemporaryDirectory(prefix="portable-memory-giga-") as temporary:
        root = Path(temporary)
        staged_full = root / "full.zip"
        export_package(memory, staged_full, assets=assets, review=review)
        package = read_package(staged_full)
        expected = copy.deepcopy(memory)
        expected["records"] = [r for r in memory["records"] if r["review"]["decision"] != "exclude"]
        require(digest(package["memory"]) == digest(expected), "Full package does not preserve the reviewed source memory")
        require(digest(package["review"]) == digest(review), "Full package does not preserve the common review")
        asset_files = [{"path": name, **metadata} for name, metadata in package["manifest"]["files"].items()
                       if name.startswith("assets/")]
        full_info = {
            "path": str(full), "sha256": package["sha256"],
            "package_id": package["manifest"]["package_id"],
            "record_count": package["manifest"]["record_count"],
            "excluded_record_count": package["manifest"]["excluded_record_count"],
            "source_memory_sha256": digest(memory),
            "memory_sha256": digest(package["memory"]),
            "coverage": package["memory"]["coverage"],
            "assets_selection": "no_assets_declared" if no_assets else "provided_directory",
            "asset_scope_note": asset_scope_note,
            "asset_count": len(asset_files), "asset_files": asset_files,
            "scope": "reviewed_nonexcluded_records_and_explicitly_supplied_files",
            "native_memory": "not_written", "upload_to_gigaagent": False,
            "limitations": ["No account-wide discovery is performed; unavailable originals are not reconstructed.",
                            "Supplied original files are not automatically redacted; their inclusion must follow the selected scope."],
        }
        staged_profile = root / output.name
        staged_profile.write_text(result["markdown"], encoding="utf-8", newline="\n")
        handoff = prepare_handoff("gigaagent", [staged_profile], root / "handoff", instruction=instruction)
        staged_handoff = Path(handoff["directory"])
        user_files = [str(full), str(destination / output.name), str(destination / INSTRUCTION)]
        report_data = {**result["report"], "full_package": full_info,
                       "user_delivery_files": user_files,
                       "gigaagent_upload_files": [str(destination / INSTRUCTION), str(destination / output.name)]}
        payload = [(full, staged_full.read_bytes()), (output, staged_profile.read_bytes()),
                   (report, json_bytes(report_data))]
        payload.extend((destination / p.name, p.read_bytes()) for p in staged_handoff.iterdir())
        _publish(payload, destination)
        handoff.update(directory=str(destination), guide=str(destination / GUIDE))
    return {"output": str(output), "report": str(report), "full_package": full_info,
            "full_package_verified": True,
            "handoff": handoff, "user_delivery_files": user_files,
            "gigaagent_upload_files": report_data["gigaagent_upload_files"],
            "delivery": "local_files_prepared", "native_memory_applied": False}
