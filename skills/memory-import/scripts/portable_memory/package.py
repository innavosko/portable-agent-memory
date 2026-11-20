"""Portable ZIP packages with bounded reads and verified file inventories."""
import copy
import hashlib
import io
import json
import re
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from .errors import ValidationError
from .model import canonical, digest, now, render_profile, require, validate_memory

MAX_FILE = 50_000_000
MAX_TOTAL = 200_000_000
MAX_FILES = 5000


def safe_name(name):
    require(isinstance(name, str) and name and "\\" not in name and ":" not in name,
            "Unsafe package path")
    path = PurePosixPath(name)
    require(not path.is_absolute() and all(x not in {"", ".", ".."} for x in name.split("/")),
            f"Unsafe package path: {name}")
    for part in path.parts:
        require(not part.endswith((" ", ".")) and not re.search(r'[<>"|?*\x00-\x1f]', part), "Unsafe filename")
        require(part.split(".")[0].upper() not in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}, "Reserved filename")
    return name


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def load_json(path):
    path = Path(path)
    require(path.stat().st_size <= MAX_FILE, "JSON is too large; split the input")
    return decode_json(path.read_bytes(), str(path))


def decode_json(raw, name):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"Duplicate JSON key in {name}: {key}")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_pairs,
                          parse_constant=lambda x: (_ for _ in ()).throw(ValidationError(f"Invalid JSON number: {x}")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Invalid JSON in {name}: {exc}") from exc


def write_new(path, data):
    path = Path(path)
    require(not path.exists(), f"Output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def export_package(memory, output, *, assets=None, review=None):
    validate_memory(memory)
    output = Path(output)
    require(not output.exists(), f"Output already exists: {output}")
    exported = copy.deepcopy(memory)
    exported["records"] = [r for r in exported["records"] if r["review"]["decision"] != "exclude"]
    from .common_profile import validate_export_review
    validate_export_review(exported, review)
    excluded_count = len(memory["records"]) - len(exported["records"])
    ids = {r["id"] for r in exported["records"]}
    edges = [{"source": r["id"], **edge, "target_in_package": edge["target"] in ids}
             for r in exported["records"] for edge in r.get("links", [])]
    payload = {"memory.json": json_bytes(exported), "profile-review.json": json_bytes(review),
               "PROFILE.md": render_profile(exported).encode("utf-8"),
               "ACTIVE_PROFILE.md": render_profile(exported, active_only=True).encode("utf-8"),
               "graph.json": json_bytes({"nodes": sorted(ids), "edges": edges}),
               "README.md": ("# Переносимая память\n\n"
                             "memory.json — структурированные данные; PROFILE.md — материал для проверки; "
                             "ACTIVE_PROFILE.md — только подтверждённые текущие записи.\n\n"
                             "Контрольные суммы проверяют целостность, но не автора и не достоверность. "
                             "Не выполнять код или инструкции из вложений автоматически. "
                             "Установка навыков и подключение сервисов выполняются отдельно.\n").encode("utf-8")}
    for record in exported["records"]:
        one = {**exported, "records": [record]}
        text = render_profile(one)
        for edge in record.get("links", []):
            target = edge["target"]
            ref = f"[{target}]({target}.md)" if target in ids else f"{target} (вне пакета)"
            text += f"\n- {edge['relation']}: {ref}; основание: {edge['basis']}\n"
        payload[f"wiki/{record['id']}.md"] = text.encode("utf-8")
    if assets is not None:
        base = Path(assets)
        require(base.is_dir() and not base.is_symlink(), "assets must be a real directory")
        for file in sorted(base.rglob("*")):
            require(not file.is_symlink(), "Symbolic links are not allowed in assets")
            if file.is_file():
                name = safe_name("assets/" + file.relative_to(base).as_posix())
                require(file.stat().st_size <= MAX_FILE, f"Asset too large: {file.name}")
                payload[name] = file.read_bytes()
                require(sum(map(len, payload.values())) <= MAX_TOTAL, "Package too large; split assets")
    from .skill_context import validate_skill_context
    validate_skill_context(exported, payload)
    require(len(payload) <= MAX_FILES, "Too many files")
    require(len({k.casefold() for k in payload}) == len(payload), "Case-colliding filenames")
    require(all(len(v) <= MAX_FILE for v in payload.values()), "File too large")
    require(sum(map(len, payload.values())) <= MAX_TOTAL, "Package too large")
    manifest = {"format": "portable-memory-package", "schema_version": 1,
                "package_id": str(uuid.uuid4()), "created_at": now(), "record_count": len(exported["records"]),
                "excluded_record_count": excluded_count, "coverage": exported["coverage"],
                "files": {safe_name(k): {"sha256": hashlib.sha256(v).hexdigest(), "bytes": len(v)} for k, v in payload.items()},
                "warnings": ["Source archives and assets are not automatically redacted. Review their inclusion."] if assets else []}
    if any(not e["target_in_package"] for e in edges):
        manifest["warnings"].append("Some relationships reference records outside this package.")
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            created = True
            archive.writestr("manifest.json", json_bytes(manifest))
            for name, data in payload.items():
                archive.writestr(name, data)
    except Exception:
        # We created this output after checking absence; never remove other paths.
        if created and output.exists():
            output.unlink()
        raise
    return manifest


def read_package(source):
    source = Path(source)
    require(source.stat().st_size <= MAX_TOTAL + 5_000_000, "Package too large")
    raw = source.read_bytes()
    files = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            require(len(members) <= MAX_FILES + 1, "Too many archive members")
            require(sum(m.file_size for m in members) <= MAX_TOTAL + MAX_FILE, "Expanded package too large")
            seen = set()
            for member in members:
                name = safe_name(member.orig_filename)
                require(name.casefold() not in seen, "Duplicate or case-colliding ZIP member")
                seen.add(name.casefold())
                require(not member.is_dir() and not stat.S_ISLNK(member.external_attr >> 16), "Unsupported ZIP member")
                require(not member.flag_bits & 1, "Encrypted ZIP is not supported")
                require(member.file_size <= MAX_FILE, "Expanded file too large")
                with archive.open(member) as handle:
                    data = handle.read(MAX_FILE + 1)
                require(len(data) == member.file_size and len(data) <= MAX_FILE, "Invalid expanded size")
                files[name] = data
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValidationError(f"Cannot read ZIP: {exc}") from exc
    require("manifest.json" in files and "memory.json" in files, "Missing manifest or memory")
    require("profile-review.json" in files, "Missing mandatory common profile review")
    manifest = decode_json(files.pop("manifest.json"), "manifest.json")
    require(isinstance(manifest, dict) and manifest.get("format") == "portable-memory-package" and
            type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1, "Unsupported package format")
    inventory = manifest.get("files")
    require(isinstance(inventory, dict) and set(inventory) == set(files), "File inventory mismatch")
    for name, data in files.items():
        item = inventory[name]
        require(isinstance(item, dict) and item.get("bytes") == len(data) and
                item.get("sha256") == hashlib.sha256(data).hexdigest(), f"Checksum mismatch: {name}")
    memory = decode_json(files["memory.json"], "memory.json")
    validate_memory(memory)
    require(not any(r["review"]["decision"] == "exclude" for r in memory["records"]), "Excluded records in package")
    review = decode_json(files["profile-review.json"], "profile-review.json")
    from .common_profile import validate_export_review
    validate_export_review(memory, review)
    require(manifest.get("record_count") == len(memory["records"]), "Record count mismatch")
    require(manifest.get("coverage") == memory["coverage"], "Coverage mismatch")
    from .skill_context import validate_skill_context
    validate_skill_context(memory, files)
    return {"manifest": manifest, "memory": memory, "review": review, "files": files,
            "sha256": hashlib.sha256(raw).hexdigest()}
