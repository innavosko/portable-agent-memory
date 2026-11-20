"""Keep explicit project instructions physically attached to a copied skill.

Only a new exported copy is changed. Source SKILL.md is retained as an original;
references preserve exact text, source and project scope. This does not discover
instruction dependencies, execute code, approve content or install a skill.
"""
import copy
import hashlib
import json
from pathlib import Path

from .delivery import checked_path
from .model import digest, require, validate_memory
from .package import MAX_FILE, MAX_FILES, MAX_TOTAL, json_bytes, safe_name, write_new


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def instruction_fingerprint(record):
    # A subsequent human decision changes review/currency, not the source text.
    return digest({key: value for key, value in record.items() if key not in {"review", "currency"}})


def _reference(instruction, project):
    value = instruction["value"]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    return ("# " + instruction["title"] + "\n\n"
            + "Проект: " + project["title"] + " (" + project["id"] + ")\n\n"
            + "Область действия: " + instruction["scope"] + "\n\n"
            + "Источник: " + json.dumps(instruction["sources"], ensure_ascii=False, indent=2) + "\n\n"
            + "Исходный текст / данные без пересказа:\n\n" + text + "\n").encode("utf-8")


def _appendix(instructions):
    lines = ["", "", "## Project instructions included with this copy", "",
             "These references preserve user instructions for their named project only. "
             "Apply them only after content review and explicit selection of this skill at the receiving agent. "
             "Do not turn project rules into global instructions.", ""]
    for item in instructions:
        # Paths, unlike user-provided titles, are validated and contain no link syntax.
        relative = item["reference_path"].split("/", 3)[3]
        lines.append("- [Project instructions " + item["instruction_id"] + "](" + relative + ")")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _check_project(instruction, project):
    require(instruction["kind"] == "rule" and project["kind"] == "project", "Bind a rule to an actual project record")
    require(instruction["scope"].startswith("project:") and instruction["scope"] == project["scope"],
            "Project instruction and project must have the same explicit project scope")
    if instruction.get("source_project_id") is not None:
        require(instruction["scope"] == "project:" + instruction["source_project_id"], "Instruction source project and scope differ")
        aliases = {project[key] for key in ("source_item_id", "source_project_id") if project.get(key) is not None}
        require(instruction["source_project_id"] in (aliases or {project["id"]}),
                "Instruction belongs to a different source project")
    project_links = [link["target"] for link in instruction.get("links", []) if link["relation"] == "belongs_to"]
    if project_links:
        require(set(project_links) == {project["id"]}, "Instruction source relationship points to another project")
    require(instruction["review"]["decision"] != "exclude" and project["review"]["decision"] != "exclude",
            "Excluded instructions or projects cannot be required by a skill")


def validate_skill_context(memory, payload, *, skill_ids=None, check_files=True):
    """Check that required rules AND their actual referenced files survived export."""
    records = {record["id"]: record for record in memory["records"]}
    for skill in memory["records"]:
        if skill_ids is not None and skill["id"] not in skill_ids:
            continue
        value = skill["value"]
        if skill["kind"] != "skill" or not isinstance(value, dict) or "project_context" not in value:
            continue
        binding = value["project_context"]
        require(isinstance(binding, dict) and set(binding) == {"format", "schema_version", "entry_path", "source_entry_path", "source_sha256", "entry_sha256", "instructions"}
                and binding["format"] == "portable-skill-project-context" and type(binding["schema_version"]) is int
                and binding["schema_version"] == 1, "Invalid skill project context")
        base = safe_name(value.get("assets_path"))
        require(base.startswith("assets/skills/") and len(base.split("/")) == 3, "Invalid bound skill asset folder")
        require(binding["entry_path"] == base + "/SKILL.md"
                and binding["source_entry_path"] == base + "/references/portable-context/SKILL.original.md",
                "Bound skill entry paths changed")
        for path, expected in ((binding["entry_path"], binding["entry_sha256"]),
                               (binding["source_entry_path"], binding["source_sha256"])):
            if check_files:
                require(path in payload and _sha(payload[path]) == expected, "Missing or changed skill entry: " + path)
        require(isinstance(binding["instructions"], list) and binding["instructions"], "Bound skill needs project instruction references")
        seen = set()
        for item in binding["instructions"]:
            require(isinstance(item, dict) and set(item) == {"instruction_id", "project_id", "scope", "content_sha256", "reference_path", "reference_sha256"},
                    "Invalid project instruction binding")
            rid, pid = item["instruction_id"], item["project_id"]
            require(isinstance(rid, str) and isinstance(pid, str) and rid in records and pid in records and rid not in seen,
                    "Required project instruction or project is missing/repeated")
            seen.add(rid)
            instruction, project = records[rid], records[pid]
            _check_project(instruction, project)
            require(item["scope"] == instruction["scope"] and item["content_sha256"] == instruction_fingerprint(instruction),
                    "Project instruction changed; rebuild its skill context before export")
            path = base + "/references/portable-context/" + rid + ".md"
            require(item["reference_path"] == path, "Project instruction reference path changed")
            expected_reference = _reference(instruction, project)
            require(_sha(expected_reference) == item["reference_sha256"],
                    "Project instruction file differs from its source record")
            if check_files:
                require(path in payload and payload[path] == expected_reference, "Missing or changed project instruction reference file")
            require(any(link["target"] == rid and link["relation"] == "requires_instruction" for link in skill.get("links", [])),
                    "Skill instruction dependency link is missing")
        if check_files:
            require(payload[binding["entry_path"]] == payload[binding["source_entry_path"]] + _appendix(binding["instructions"]),
                    "Skill does not reference the exact required project instructions")
            require(value.get("snapshot_sha256") == digest({path[len(base)+1:]: {"sha256": _sha(raw), "bytes": len(raw)}
                    for path, raw in sorted(payload.items()) if path.startswith(base + "/")}),
                    "Skill files changed; rebuild the skill snapshot and review its new contents")


def _asset_snapshot(root):
    root = checked_path(root)
    require(root.is_dir(), "A provided assets folder is required")
    files, total = {}, 0
    for path in sorted(root.rglob("*")):
        checked_path(path, root)
        if path.is_dir():
            continue
        require(path.is_file() and path.stat().st_size <= MAX_FILE, "Invalid or oversized asset")
        name = safe_name("assets/" + path.relative_to(root).as_posix())
        raw = path.read_bytes()
        total += len(raw)
        require(len(raw) <= MAX_FILE and total <= MAX_TOTAL and len(files) < MAX_FILES, "Asset snapshot exceeds limits")
        require(name.casefold() not in {key.casefold() for key in files}, "Case-colliding asset paths")
        files[name] = raw
    return files


def bind_skill_context(memory, assets, bindings, output, accepted=False):
    validate_memory(memory)
    require(accepted is True, "Explicit acceptance of project-to-skill bindings is required")
    require(isinstance(bindings, dict) and set(bindings) == {"format", "schema_version", "items"}
            and bindings["format"] == "portable-skill-context-bindings" and type(bindings["schema_version"]) is int
            and bindings["schema_version"] == 1 and isinstance(bindings["items"], list) and bindings["items"],
            "Invalid project instruction bindings")
    destination, asset_root = checked_path(output), checked_path(assets)
    require(not destination.exists() and destination != asset_root and asset_root not in destination.parents,
            "Choose a new output outside the source assets folder")
    payload = _asset_snapshot(asset_root)
    result = copy.deepcopy(memory)
    records = {record["id"]: record for record in result["records"]}
    grouped, used = {}, set()
    for entry in bindings["items"]:
        require(isinstance(entry, dict) and set(entry) == {"skill_id", "project_id", "instruction_ids"}, "Invalid binding item")
        sid, pid = entry["skill_id"], entry["project_id"]
        require(isinstance(sid, str) and isinstance(pid, str) and sid in records and pid in records, "Unknown skill/project record")
        skill, project = records[sid], records[pid]
        require(skill["kind"] == "skill" and isinstance(skill["value"], dict) and skill["review"]["decision"] != "exclude", "A selected skill record is required")
        require("project_context" not in skill["value"], "This skill is already bound; use the original source bundle for a new version")
        ids = entry["instruction_ids"]
        require(isinstance(ids, list) and ids, "Choose at least one actual project instruction")
        for rid in ids:
            require(isinstance(rid, str) and rid in records and (sid, rid) not in used, "Unknown or duplicate skill instruction")
            used.add((sid, rid))
            _check_project(records[rid], project)
            grouped.setdefault(sid, []).append((records[rid], project))
    report = {"format": "portable-skill-context-report", "schema_version": 1, "skills": [],
              "originals_unchanged": True, "native_installation": "not_performed",
              "limitations": ["Only explicitly selected dependencies are bound; undiscovered instructions remain unknown.",
                              "A reference file preserves context but does not prove native compatibility or user approval."]}
    for sid, instructions in grouped.items():
        skill = records[sid]
        base = safe_name(skill["value"].get("assets_path"))
        require(base.startswith("assets/skills/") and len(base.split("/")) == 3, "Skill must have supplied original files")
        entry_path = base + "/SKILL.md"
        require(entry_path in payload, "Original SKILL.md is unavailable")
        original = payload[entry_path]
        try:
            original.decode("utf-8-sig")
        except UnicodeDecodeError:
            require(False, "Original SKILL.md must be UTF-8")
        source_entry = base + "/references/portable-context/SKILL.original.md"
        require(not any(path.startswith(base + "/references/portable-context/") for path in payload), "Reserved context directory already exists; choose a clean original bundle")
        payload[source_entry] = original
        attached = []
        for instruction, project in instructions:
            reference_path = base + "/references/portable-context/" + instruction["id"] + ".md"
            raw = _reference(instruction, project)
            payload[reference_path] = raw
            attached.append({"instruction_id": instruction["id"], "project_id": project["id"], "scope": instruction["scope"],
                             "content_sha256": instruction_fingerprint(instruction), "reference_path": reference_path,
                             "reference_sha256": _sha(raw)})
            for target, relation in ((instruction["id"], "requires_instruction"), (project["id"], "uses_project_context")):
                link = {"target": target, "relation": relation, "basis": "source"}
                if link not in skill.setdefault("links", []):
                    skill["links"].append(link)
        payload[entry_path] = original + _appendix(attached)
        skill["value"]["project_context"] = {"format": "portable-skill-project-context", "schema_version": 1,
            "entry_path": entry_path, "source_entry_path": source_entry, "source_sha256": _sha(original),
            "entry_sha256": _sha(payload[entry_path]), "instructions": attached}
        skill["value"]["original_snapshot_sha256"] = skill["value"].get("snapshot_sha256")
        skill["value"]["snapshot_sha256"] = digest({path[len(base)+1:]: {"sha256": _sha(raw), "bytes": len(raw)}
                                                   for path, raw in sorted(payload.items()) if path.startswith(base + "/")})
        skill["value"]["bundle_variant"] = "copy_with_explicit_project_context"
        skill["review"] = {"decision": "pending"}
        report["skills"].append({"skill_id": sid, "instruction_ids": [item["instruction_id"] for item in attached],
                                 "project_ids": sorted({item["project_id"] for item in attached}), "review": "pending"})
    validate_memory(result)
    validate_skill_context(result, payload)
    require(len(payload) <= MAX_FILES and sum(map(len, payload.values())) <= MAX_TOTAL
            and all(len(raw) <= MAX_FILE for raw in payload.values()), "Bound skill assets exceed package limits")
    destination.mkdir(parents=True)
    write_new(destination / ".incomplete", "Context binding has not finished.\n")
    for name, raw in sorted(payload.items()):
        write_new(checked_path(destination / name, destination), raw)
    write_new(destination / "memory.json", json_bytes(result))
    write_new(destination / "binding-report.json", json_bytes(report))
    (destination / ".incomplete").unlink()
    return report
