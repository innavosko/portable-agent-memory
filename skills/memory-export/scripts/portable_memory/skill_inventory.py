"""Offline snapshots of explicitly supplied skill folders or individual ZIPs.

No account discovery, dependency installation, or execution of imported code.
Catalog metadata is a source/host assertion, not authenticated platform access.
"""
from __future__ import annotations

import copy
import hashlib
import io
import os
import stat
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

from .delivery import checked_path
from .errors import ValidationError
from .model import ID, canonical, digest, empty_memory, require, validate_memory
from .package import json_bytes, safe_name, write_new

MAX_FILES = 5000
MAX_FILE_BYTES = 50_000_000
MAX_TOTAL_BYTES = 200_000_000
MAX_ARCHIVE_BYTES = 205_000_000
MAX_ENTRIES = 20_000
_SOURCE_FIELDS = {"product", "account_id", "workspace_id", "observed_at", "evidence", "catalog_complete"}
_ITEM_FIELDS = {"id", "name", "version", "origin", "enabled", "selected", "source_ref", "locator"}
_ORIGINS = {"user_created", "third_party", "platform", "unknown"}
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
              ".cache", "cache", "caches", ".pytest_cache", ".mypy_cache", ".ruff_cache",
              ".aws", ".ssh", ".gnupg", ".azure", ".kube", "credentials", "secrets"}
_SECRET_NAMES = {".npmrc", ".netrc", "credentials", "credential", "secrets", "secret", "password",
                 "passwords", "tokens", "token", "auth", "oauth", "keychain", "id_rsa", "id_ed25519"}
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".p8", ".jks"}
_LIMITATIONS = [
    "Only the supplied catalog and files were inspected; no platform account was searched.",
    "catalog_complete is the source's assertion about this catalog, not a verified whole-account scan.",
    "full_snapshot means a complete copy of supplied admissible files, not a complete platform export or a working skill.",
    "Dependencies and native compatibility are not verified; imported code is never executed.",
    "Source enabled/disabled state is a preference for later setup, not activation at the recipient.",
    "Credential filenames are excluded; this is not a scanner for secrets embedded in arbitrary file content.",
]


def _text(value, field):
    require(isinstance(value, str) and bool(value.strip()) and "\x00" not in value,
            f"{field}: nonempty text required")


def _source(source):
    require(isinstance(source, dict) and set(source) == _SOURCE_FIELDS, "Invalid skill catalog source fields")
    for field in ("product", "observed_at", "evidence"):
        _text(source[field], "source." + field)
    for field in ("account_id", "workspace_id"):
        if source[field] is not None:
            _text(source[field], "source." + field)
    require(type(source["catalog_complete"]) is bool, "catalog_complete must be boolean")
    canonical(source)


def validate_catalog(catalog):
    require(isinstance(catalog, dict) and set(catalog) == {"format", "schema_version", "source", "items"},
            "Invalid skill catalog fields")
    require(catalog["format"] == "portable-skill-catalog" and type(catalog["schema_version"]) is int
            and catalog["schema_version"] == 1, "Unsupported skill catalog")
    _source(catalog["source"])
    require(isinstance(catalog["items"], list) and len(catalog["items"]) <= MAX_FILES, "Invalid skill catalog items")
    seen = set()
    known_sources = set()
    for item in catalog["items"]:
        require(isinstance(item, dict) and _ITEM_FIELDS <= set(item) and not set(item) - _ITEM_FIELDS - {"dependencies"},
                "Invalid skill item fields")
        sid = item["id"]
        require(isinstance(sid, str) and ID.fullmatch(sid), "Invalid skill id")
        safe_name(sid)
        key = unicodedata.normalize("NFC", sid).casefold()
        require(key not in seen, "Duplicate or case-colliding skill id")
        seen.add(key)
        for field in ("name", "source_ref"):
            _text(item[field], "skill." + field)
        if item["version"] is not None:
            _text(item["version"], "skill.version")
        source = catalog["source"]
        if source["account_id"] is not None and source["workspace_id"] is not None:
            identity = (source["product"], source["account_id"], source["workspace_id"], item["source_ref"], item["version"])
            require(identity not in known_sources,
                    "Repeated confirmed skill source/version; use one catalog item for this source, not one per agent surface")
            known_sources.add(identity)
        require(isinstance(item["origin"], str) and item["origin"] in _ORIGINS, "Invalid skill origin")
        require(item["enabled"] is None or type(item["enabled"]) is bool, "enabled must be boolean or null")
        require(type(item["selected"]) is bool, "selected must be boolean")
        locator = item["locator"]
        require(isinstance(locator, dict) and isinstance(locator.get("kind"), str), "Invalid skill locator")
        if locator["kind"] in {"folder", "zip"}:
            require(set(locator) == {"kind", "path"}, "File locator requires kind and path only")
            _text(locator["path"], "locator.path")
        else:
            require(locator["kind"] == "unavailable" and set(locator) == {"kind", "reason"}, "Invalid unavailable locator")
            _text(locator["reason"], "locator.reason")
        dependencies = item.get("dependencies", [])
        require(isinstance(dependencies, list), "dependencies must be a text list")
        for dependency in dependencies:
            _text(dependency, "dependency")
    canonical(catalog)


def _root(value):
    path = checked_path(value)
    require(path != Path(path.anchor) and path.resolve() != Path.home().resolve(),
            "Choose an explicit skill folder, not a drive root or the whole home directory")
    return path


def _source_identity(source):
    return {key: source[key] for key in ("product", "account_id", "workspace_id")}


def discover_skills(root, source):
    """List only root/SKILL.md or immediate child folders/SKILL.md; write nothing.

    Discovery does not infer origin, version, enabled state, or user selection.
    Changing observation time/evidence does not change the discovered item IDs.
    """
    _source(source)
    root = _root(root)
    require(root.is_dir(), "Selected skill root is not an available folder")
    direct = checked_path(root / "SKILL.md", root)
    if direct.is_file():
        folders = [root]
    else:
        folders = []
        with os.scandir(root) as entries:
            for count, entry in enumerate(entries, 1):
                require(count <= MAX_ENTRIES, "Too many entries; choose a narrower skill folder")
                child = checked_path(entry.path, root)
                if child.is_dir() and checked_path(child / "SKILL.md", root).is_file():
                    folders.append(child)
    items = []
    for folder in sorted(folders, key=lambda path: str(path).casefold()):
        # Keep the actual local path only in the working catalog, not in exported
        # memory/report provenance. The opaque reference is still path-stable.
        source_ref = "local-skill-folder:" + digest(os.path.normcase(str(folder)))
        sid = "skill-" + digest({"source": _source_identity(source), "source_ref": source_ref})[:24]
        items.append({"id": sid, "name": folder.name, "version": None, "origin": "unknown",
                      "enabled": None, "selected": False, "source_ref": source_ref,
                      "locator": {"kind": "folder", "path": str(folder)}, "dependencies": []})
    catalog = {"format": "portable-skill-catalog", "schema_version": 1, "source": copy.deepcopy(source), "items": items}
    validate_catalog(catalog)
    return catalog


def _excluded(name, *, directory=False):
    parts = PurePosixPath(name).parts
    if any(part.casefold() in _SKIP_DIRS or part.casefold().startswith(".env") for part in parts):
        return "credential, environment, repository metadata or cache directory/file"
    if directory:
        return None
    filename = parts[-1].casefold()
    stem = filename.lstrip(".").split(".", 1)[0]
    if (filename in _SECRET_NAMES or stem in _SECRET_NAMES or Path(filename).suffix in _SECRET_SUFFIXES
            or any(word in filename for word in ("credential", "private_key", "access_token", "refresh_token"))):
        return "credential/key file"
    return None


def _read(path, limit):
    checked_path(path)
    require(path.is_file(), f"Not a regular skill source file: {path.name}")
    require(path.stat().st_size <= limit, f"Skill source file exceeds size limit: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "Skill source must be a regular file")
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, "Skill source file grew beyond size limit")
    return raw


def _add(files, name, raw):
    require(len(files) < MAX_FILES, "Too many skill files")
    require(len(raw) <= MAX_FILE_BYTES and sum(map(len, files.values())) + len(raw) <= MAX_TOTAL_BYTES,
            "Skill snapshot exceeds size limit")
    files[name] = raw


def _register(seen, name, directory):
    safe_name(name)
    key = unicodedata.normalize("NFC", name).casefold()
    require(key not in seen, "Duplicate or Unicode/case-colliding skill path: " + name)
    for parent in PurePosixPath(key).parents:
        if str(parent) != ".":
            require(seen.get(str(parent)) is not False, "File/directory collision in skill snapshot")
    if not directory:
        require(not any(other.startswith(key + "/") for other in seen), "File/directory collision in skill snapshot")
    seen[key] = directory


def _folder_snapshot(root):
    files, excluded, seen = {}, [], {}
    entries_seen = 0

    def visit(folder):
        nonlocal entries_seen
        with os.scandir(folder) as entries:
            children = []
            for entry in entries:
                entries_seen += 1
                require(entries_seen <= MAX_ENTRIES, "Skill folder traversal exceeds entry limit")
                children.append(Path(entry.path))
        for path in sorted(children):
            checked_path(path, root)
            name = path.relative_to(root).as_posix()
            directory = path.is_dir()
            _register(seen, name, directory)
            reason = _excluded(name, directory=directory)
            if reason:
                excluded.append({"path": name, "reason": reason})
            elif directory:
                visit(path)
            else:
                _add(files, name, _read(path, MAX_FILE_BYTES))

    visit(root)
    require("SKILL.md" in files, "Supplied skill folder must contain a root SKILL.md")
    return files, excluded


def _zip_snapshot(path):
    archive = _read(path, MAX_ARCHIVE_BYTES)
    files, excluded, seen = {}, [], {}
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            members = zipped.infolist()
            require(len(members) <= MAX_ENTRIES, "Skill ZIP contains too many entries")
            entries, expanded = [], 0
            for member in members:
                original = member.orig_filename
                directory = original.endswith("/")
                name = original[:-1] if directory else original
                _register(seen, name, directory)
                mode = (member.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                require(file_type in {0, stat.S_IFDIR, stat.S_IFREG}, "Links or special files are not supported in skill ZIP")
                require(not (member.flag_bits & 1), "Encrypted skill ZIP is not supported")
                require(not directory or member.file_size == 0, "Directory entry contains data")
                require(member.file_size <= MAX_FILE_BYTES, "Skill ZIP member exceeds size limit")
                expanded += member.file_size
                require(expanded <= MAX_TOTAL_BYTES, "Skill ZIP expansion exceeds size limit")
                entries.append((member, name, directory))
            file_names = {name for _, name, directory in entries if not directory}
            if "SKILL.md" in file_names:
                prefix = ""
            else:
                candidates = [name[:-len("SKILL.md")] for name in file_names
                              if len(PurePosixPath(name).parts) == 2 and name.endswith("/SKILL.md")]
                require(len(candidates) == 1, "Skill ZIP needs one root SKILL.md or one enclosing skill folder")
                prefix = candidates[0]
                require(all(name == prefix[:-1] or name.startswith(prefix) for _, name, _ in entries),
                        "Skill ZIP contains files outside its single skill folder")
            for member, name, directory in entries:
                relative = name[len(prefix):] if prefix else name
                if not relative:
                    continue
                reason = _excluded(relative, directory=directory)
                if reason:
                    excluded.append({"path": relative, "reason": reason})
                elif not directory:
                    with zipped.open(member) as stream:
                        raw = stream.read(MAX_FILE_BYTES + 1)
                    require(len(raw) == member.file_size, "Skill ZIP member size differs from inventory")
                    _add(files, relative, raw)
            require("SKILL.md" in files, "Skill ZIP root instruction was not included")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
        raise ValidationError(f"Invalid or unsupported skill ZIP: {exc}") from exc
    return files, excluded


def _snapshot(item):
    locator = item["locator"]
    if locator["kind"] == "unavailable":
        return {}, [], locator["reason"]
    path = _root(locator["path"]) if locator["kind"] == "folder" else checked_path(locator["path"])
    if not path.exists():
        return {}, [], "The explicitly supplied source is not available in this execution environment."
    if locator["kind"] == "folder":
        require(path.is_dir(), "Folder skill locator does not identify a directory")
        files, excluded = _folder_snapshot(path)
    else:
        files, excluded = _zip_snapshot(path)
    return files, excluded, None


def _markdown(report):
    lines = ["# Подборка существующих навыков", "",
             "Сохранены оригинальные доступные файлы; инструкции и скрипты не выполнялись.", "",
             "Полнота предоставленного каталога заявлена источником: " + ("да" if report["source"]["catalog_complete"] else "нет"),
             "Это не проверка всего аккаунта платформы.", ""]
    labels = {"full_snapshot": "Скопированы все предоставленные допустимые файлы", "partial": "Частичная копия",
              "unavailable": "Файлы недоступны", "not_selected": "Не выбран"}
    for item in report["items"]:
        lines += [f"## {item['name']} ({item['id']})", "", labels[item["status"]] + ".", ""]
        if item.get("reason"):
            lines += ["Причина: " + item["reason"], ""]
        for excluded in item.get("excluded", []):
            lines.append(f"- Исключено: {excluded['path']} — {excluded['reason']}")
        lines += [""]
    lines += ["## Что ещё проверить", "", "Зависимости, совместимость и работу каждого навыка проверяют отдельно.",
              "Исходное состояние включён/выключен сохраняется как пожелание и не активирует навык у получателя.",
              "Все записи ожидают проверки пользователя. Секреты внутри произвольного содержимого автоматически не распознаются.", ""]
    return "\n".join(lines)


def collect_skills(catalog, output, accepted=False):
    """Copy selected originals into a NEW folder; approvals of facts stay pending.

    Input/ZIP validation finishes before output creation. A failed output write
    retains .incomplete and is never silently reused or overwritten.
    """
    validate_catalog(catalog)
    require(accepted is True, "Review the selected skill inventory and explicitly accept collection")
    output = checked_path(output)
    require(not output.exists(), "Skill collection output already exists; choose a new folder")
    selected = [item for item in catalog["items"] if item["selected"]]
    require(selected, "Select at least one skill before collection")
    for item in selected:
        if item["locator"]["kind"] == "folder":
            source = _root(item["locator"]["path"])
            require(output != source and source not in output.parents, "Collection output must be outside every selected skill folder")
    memory = empty_memory()
    memory["metadata"] = {"source": copy.deepcopy(catalog["source"]), "collection": "existing_skill_originals"}
    if not catalog["source"]["catalog_complete"]:
        memory["coverage"]["unavailable"].append("The supplied skill catalog does not claim complete coverage.")
    report = {"format": "portable-skill-inventory-report", "schema_version": 1,
              "source": copy.deepcopy(catalog["source"]), "catalog_sha256": digest(catalog), "items": [],
              "summary": {"catalog_items": len(catalog["items"]), "selected": len(selected), "full_snapshot": 0,
                          "partial": 0, "unavailable": 0, "not_selected": 0, "files": 0, "bytes": 0},
              "memory_path": "memory.json", "assets_path": "assets", "limitations": list(_LIMITATIONS)}
    payload = {}
    for item in catalog["items"]:
        entry = {"id": item["id"], "name": item["name"], "selected": item["selected"], "source_ref": item["source_ref"]}
        if not item["selected"]:
            entry["status"] = "not_selected"
            report["summary"]["not_selected"] += 1
            report["items"].append(entry)
            continue
        files, excluded, unavailable = _snapshot(item)
        inventory = {name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)} for name, raw in sorted(files.items())}
        status = "unavailable" if unavailable else "partial" if excluded else "full_snapshot"
        snapshot_sha = digest(inventory) if files else None
        asset_path = f"assets/skills/{item['id']}" if files else None
        entry.update(status=status, snapshot_sha256=snapshot_sha, assets_path=asset_path, files=inventory,
                     excluded=excluded, reason=unavailable, dependency_check="not_verified")
        report["items"].append(entry)
        report["summary"][status] += 1
        report["summary"]["files"] += len(files)
        report["summary"]["bytes"] += sum(map(len, files.values()))
        require(report["summary"]["files"] <= MAX_FILES and report["summary"]["bytes"] <= MAX_TOTAL_BYTES,
                "Selected skills exceed collection limits; split the selection")
        payload.update({f"{asset_path}/{name}": raw for name, raw in files.items()})
        scope_identity = {"source": _source_identity(catalog["source"]), "source_ref": item["source_ref"]}
        memory["records"].append({"id": item["id"], "kind": "skill", "title": item["name"],
            "scope": "skill:" + digest(scope_identity)[:24], "provenance": "source", "currency": "unknown",
            "value": {"name": item["name"], "version": item["version"], "origin": item["origin"],
                      "enabled": item["enabled"], "enabled_semantics": "source_preference_only",
                      "source_identity": scope_identity, "snapshot_sha256": snapshot_sha, "status": status,
                      "assets_path": asset_path, "dependencies": copy.deepcopy(item.get("dependencies", [])),
                      "dependency_check": "not_verified", "installation_status": "not_installed",
                      "excluded": copy.deepcopy(excluded), "unavailable_reason": unavailable},
            "sources": [{"id": "skill-source-" + digest(scope_identity)[:24], "locator": item["source_ref"],
                         "observed_at": catalog["source"]["observed_at"]}], "links": [], "review": {"decision": "pending"}})
        memory["coverage"]["unavailable" if unavailable else "read"].append(item["source_ref"])
        memory["coverage"]["excluded"].extend(item["source_ref"] + ":" + excluded_file["path"] for excluded_file in excluded)
    validate_memory(memory)
    output.mkdir(parents=True)
    write_new(output / ".incomplete", "Skill collection has not finished.\n")
    for name, raw in sorted(payload.items()):
        write_new(checked_path(output / name, output), raw)
    (output / "assets" / "skills").mkdir(parents=True, exist_ok=True)
    write_new(output / "memory.json", json_bytes(memory))
    write_new(output / "inventory-report.json", json_bytes(report))
    write_new(output / "inventory-report.md", _markdown(report))
    (output / ".incomplete").unlink()
    return report
