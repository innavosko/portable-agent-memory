"""Bounded, offline collection of explicitly supplied source snapshots.

Collection preserves source files and never adopts their contents as agent
instructions. It does not search accounts, fetch mail/cloud files, run scripts,
or apply native memory. Claims/resume ownership belong to the migration store.
Each attempt has a new directory; failed attempts retain an .incomplete marker.
Folder defaults collect Markdown/plain-text context, not arbitrary configuration
or every file in a user's home. Explicit includes can add selected artifacts.
Filename exclusions are not a scanner for secrets embedded in document prose.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

from .archives import ingest_archive
from .errors import ValidationError
from .model import validate_memory


MAX_FILES = 2000
MAX_FILE_BYTES = 50_000_000
MAX_TOTAL_BYTES = 200_000_000
MAX_ENTRIES = 20_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_SUFFIXES = {".md", ".txt", ".rst"}
_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".cache", "cache", "caches",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "env", "dist", "build",
    ".aws", ".ssh", ".gnupg", ".azure", ".kube", "credentials", "secrets",
}
_CONFIG_SUFFIXES = {".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml", ".pem", ".key", ".p12", ".pfx", ".p8", ".jks", ".sqlite", ".sqlite3", ".db"}
_CONFIG_NAMES = {"config", "settings", "credentials", "credential", "secrets", "secret", "password", "passwords", "tokens", "token", "auth", "oauth", "keychain", "id_rsa", "id_ed25519", "known_hosts", "authorized_keys", "package-lock", "yarn.lock", "pnpm-lock", "claude", "codex", "mcp"}


def _linked(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _no_links(path, boundary=None):
    # Inspect the selected root and descendants, not unrelated OS ancestors which
    # may be inaccessible even when the explicitly supplied source is readable.
    parts = [path]
    if boundary is not None and path != boundary:
        for parent in path.parents:
            parts.append(parent)
            if parent == boundary:
                break
    for part in parts:
        if _linked(part):
            raise ValidationError(f"Symbolic links/junctions are not supported: {part}")


def _path(value, label):
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ValidationError(f"{label} must be an explicit path")
    path = Path(os.path.abspath(value))
    _no_links(path)
    return path


def _relative(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError("include entries must be non-empty relative paths")
    normalized = value.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if not parts or normalized.startswith("/") or ":" in normalized or ".." in parts or normalized in {".", "./"}:
        raise ValidationError(f"Unsafe include path: {value!r}")
    return PurePosixPath(*parts).as_posix()


def _excluded(relative, *, is_dir=False):
    parts = PurePosixPath(relative).parts
    if any(part.casefold() in _EXCLUDED_DIRS or part.casefold().startswith(".env") for part in parts):
        return "dependency/cache/credential directory"
    if is_dir:
        return None
    name = parts[-1].casefold()
    stem = name.lstrip(".").split(".", 1)[0]
    # CLAUDE.md is a project instruction file, not the .claude.json account config.
    instruction_file = name in {"claude.md", "agents.md"}
    if (name.startswith(".env") or name.startswith(".npmrc") or name.startswith(".netrc")
            or (stem in _CONFIG_NAMES and not instruction_file) or any(word in name for word in ("credential", "private_key", "access_token", "refresh_token"))
            or Path(name).suffix in _CONFIG_SUFFIXES):
        return "configuration or credential file"
    return None


def _file_hash(path, limit=MAX_FILE_BYTES):
    _no_links(path)
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ValidationError(f"Source must be a regular file: {path}")
    if path.stat().st_size > limit:
        raise ValidationError(f"Source exceeds the {limit}-byte limit: {path.name}")
    digest, size = hashlib.sha256(), 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValidationError("Source is not a regular file")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            if size > limit:
                raise ValidationError("Source grew beyond the byte limit while reading")
            digest.update(chunk)
    return digest.hexdigest(), size


def _input(spec):
    if not isinstance(spec, dict) or set(spec) - {"kind", "path", "include"}:
        raise ValidationError("Input must contain kind, path and optional folder include list")
    kind = spec.get("kind")
    if not isinstance(kind, str) or kind not in {"archive", "memory", "folder"}:
        raise ValidationError("Input kind must be archive, memory or folder")
    path = _path(spec.get("path"), "input.path")
    if kind == "folder":
        if not path.is_dir():
            raise ValidationError("Folder source does not exist or is not a directory")
        # Requiring a selected project/subfolder avoids accidental whole-home or drive collection.
        if path == Path(path.anchor) or path.resolve() == Path.home().resolve():
            raise ValidationError("Choose an explicit project/subfolder, not a drive root or the whole home directory")
        includes = spec.get("include")
        if includes is not None:
            if not isinstance(includes, list) or any(not isinstance(item, str) for item in includes):
                raise ValidationError("input.include must be a list of relative files or directories")
            includes = sorted(set(_relative(item) for item in includes))
        return kind, path, includes
    if "include" in spec:
        raise ValidationError("include is supported only for folder inputs")
    if not path.is_file():
        raise ValidationError("Source file does not exist")
    return kind, path, None


def _folder_snapshot(root, includes):
    files, excluded, seen, traversed = [], [], set(), set()
    total, entries = 0, 0

    def visit(path, *, explicit):
        nonlocal total, entries
        relative = path.relative_to(root).as_posix()
        _no_links(path, root)
        if path != root:
            _relative(relative)
            reason = _excluded(relative, is_dir=path.is_dir())
            if reason:
                excluded.append({"path": relative, "reason": reason})
                return
        if path.is_dir():
            if relative in traversed:
                return
            traversed.add(relative)
            children = []
            with os.scandir(path) as iterator:
                for item in iterator:
                    entries += 1
                    if entries > MAX_ENTRIES:
                        raise ValidationError("Folder traversal exceeds the entry limit; select narrower includes")
                    children.append(Path(item.path))
            for child in sorted(children, key=lambda item: item.name):
                visit(child, explicit=explicit)
            return
        if not path.exists() or not path.is_file():
            raise ValidationError(f"Selected source is missing or not a regular file: {relative}")
        if not explicit and path.suffix.casefold() not in _DEFAULT_SUFFIXES:
            excluded.append({"path": relative, "reason": "not a default context/skill text file"})
            return
        portable_key = unicodedata.normalize("NFC", relative).casefold()
        if portable_key in seen:
            if any(item["path"] == relative for item in files):
                return
            raise ValidationError(f"Case/Unicode-colliding file names are not portable: {relative}")
        if len(files) >= MAX_FILES:
            raise ValidationError("Folder exceeds the file count limit; select narrower includes")
        sha256, size = _file_hash(path)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValidationError("Folder exceeds the total byte limit; select narrower includes")
        files.append({"path": relative, "sha256": sha256, "bytes": size})
        seen.add(portable_key)

    if includes is None:
        visit(root, explicit=False)
    else:
        for relative in includes:
            target = root.joinpath(*PurePosixPath(relative).parts)
            _no_links(target, root)
            if not target.exists():
                raise ValidationError(f"Explicit include does not exist: {relative}")
            visit(target, explicit=True)
    files.sort(key=lambda item: item["path"])
    excluded = [dict(item) for item in {tuple(sorted(item.items())): item for item in excluded}.values()]
    excluded.sort(key=lambda item: (item["path"], item["reason"]))
    encoded = json.dumps({"selection_policy": 1, "files": files}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), files, excluded


def fingerprint_input(input):
    """Hash provided bytes, or the selected relative paths and bytes of a folder.

    Excluded files do not affect a folder fingerprint. Changing selection or
    selected content does; timestamps alone do not. No original is modified.
    """
    kind, path, includes = _input(input)
    if kind == "folder":
        return _folder_snapshot(path, includes)[0]
    return _file_hash(path, MAX_TOTAL_BYTES if kind == "archive" else MAX_FILE_BYTES)[0]


def _write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _copy_file(source, destination, expected_sha256, *, limit=MAX_FILE_BYTES):
    _no_links(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
            size += len(chunk)
            if size > limit:
                raise ValidationError("Source grew beyond the per-file byte limit during copying")
            digest.update(chunk)
            outgoing.write(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValidationError("Source changed during copying; retry with a new snapshot and attempt")


def collect_job(job, output_root):
    """Collect one already-claimed offline job into a fresh attempt directory.

    The root orchestrator supplies ownership and monotonic attempt numbers.
    A failed attempt is never reused. Results describe collection only.
    """
    if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not _ID.fullmatch(job["id"]):
        raise ValidationError("Job requires a safe id")
    if not isinstance(job.get("source_id"), str) or not job["source_id"].strip():
        raise ValidationError("Job requires source_id")
    attempt = job.get("attempt")
    if type(attempt) is not int or attempt < 1:
        raise ValidationError("Job attempt must be a positive integer")
    spec = job.get("input")
    kind, source, includes = _input(spec)
    output_root = _path(output_root, "output_root")
    if kind == "folder":
        resolved_source, resolved_output = source.resolve(), output_root.resolve()
        if resolved_output == resolved_source or resolved_source in resolved_output.parents:
            raise ValidationError("Collection output must be outside the source folder")
    if kind == "folder":
        fingerprint, selected, excluded = _folder_snapshot(source, includes)
    else:
        fingerprint = fingerprint_input(spec)
    expected = job.get("input_sha256")
    if expected is not None and (not isinstance(expected, str) or not _HASH.fullmatch(expected) or expected != fingerprint):
        raise ValidationError("Input fingerprint differs from the claimed source snapshot")
    output = output_root / job["id"] / f"attempt-{attempt:04d}"
    _no_links(output, output_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir()
    except FileExistsError as exc:
        raise ValidationError("Attempt output already exists; use a new attempt, never overwrite") from exc
    marker = output / ".incomplete"
    marker.write_text("Collection has not completed. Retry using a new attempt directory.\n", encoding="utf-8")
    if kind == "archive":
        # Parse the exact claimed bytes, rather than a file being edited in place.
        with tempfile.TemporaryDirectory(prefix=".source-snapshot-", dir=output) as temporary:
            stable_source = Path(temporary) / source.name
            _copy_file(source, stable_source, fingerprint, limit=MAX_TOTAL_BYTES)
            report = ingest_archive(stable_source, output / "archive")
        report["data_path"] = str(output / "archive")
    elif kind == "memory":
        _copy_file(source, output / "memory.json", fingerprint)
        try:
            memory = json.loads((output / "memory.json").read_text(encoding="utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"Invalid provided memory JSON: {exc}") from exc
        validate_memory(memory)
        report = {
            "record_count": len(memory["records"]),
            "record_kinds": dict(Counter(record["kind"] for record in memory["records"])),
            "review_states": dict(Counter(record["review"]["decision"] for record in memory["records"])),
            "data_path": str(output / "memory.json"), "records_modified": False,
        }
    else:
        snapshot = output / "snapshot"
        snapshot.mkdir()
        for item in selected:
            relative = PurePosixPath(item["path"])
            _copy_file(source.joinpath(*relative.parts), snapshot.joinpath(*relative.parts), item["sha256"])
        report = {"file_count": len(selected), "bytes": sum(item["bytes"] for item in selected),
                  "files": selected, "excluded": excluded, "selection_policy": 1,
                  "data_path": str(snapshot),
                  "limitations": ["Filename exclusions do not detect secrets embedded in document content.",
                                  "Only selected local files were collected; no account-wide history was inspected."]}
    if fingerprint_input(spec) != fingerprint:
        raise ValidationError("Source snapshot changed during collection; retry with a new attempt")
    report.update({"collection_only": True, "native_memory_applied": False,
                   "source_id": job["source_id"], "input_sha256": fingerprint})
    _write_json(output / "collection-report.json", report)
    marker.unlink()
    return {"output": str(output), "input_sha256": fingerprint, "kind": kind, "report": report}


def collect_many(jobs, output_root, *, max_workers=4, worker=collect_job, on_result=None):
    """Run independent jobs concurrently and return outcomes in input order.

    One failed source does not cancel others. Waiting/unclaimed jobs must be
    filtered by the caller. No scheduling, ownership or retry is implied here.
    ``on_result(outcome)`` runs on the collecting thread as each job finishes.
    Callback failures do not suppress subsequent completions; they are reported
    together after every result has been dispatched, without changing job truth.
    """
    if type(max_workers) is not int or not 1 <= max_workers <= 8:
        raise ValidationError("max_workers must be an integer from 1 to 8")
    if not isinstance(jobs, list) or not callable(worker) or (on_result is not None and not callable(on_result)):
        raise ValidationError("Provide a jobs list, callable worker and optional callable on_result")
    ids = [job.get("id") if isinstance(job, dict) else None for job in jobs]
    if any(not isinstance(job_id, str) or not _ID.fullmatch(job_id) for job_id in ids) or len(set(ids)) != len(ids):
        raise ValidationError("Collection batch requires unique safe job IDs")
    if not jobs:
        return []

    def execute(job):
        try:
            result = worker(job, output_root)
            if not isinstance(result, dict):
                raise ValidationError("Collection worker returned no result object")
            return {"job_id": job["id"], "ok": True, "result": result}
        except Exception as exc:
            return {"job_id": job["id"], "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    results, callback_errors = [None] * len(jobs), []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs)), thread_name_prefix="memory-collect") as executor:
        pending = {executor.submit(execute, job): index for index, job in enumerate(jobs)}
        for future in as_completed(pending):
            outcome = future.result()
            results[pending[future]] = outcome
            if on_result is not None:
                try:
                    on_result(outcome)
                except Exception as exc:
                    callback_errors.append(f"{outcome['job_id']}: {type(exc).__name__}: {exc}")
    if callback_errors:
        raise ValidationError("Collection completed but result callbacks failed: " + "; ".join(callback_errors))
    return results
