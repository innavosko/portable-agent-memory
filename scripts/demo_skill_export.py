"""Offline export of fictional custom instructions and original skill files."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portable_memory.model import confirm, record_digest
from portable_memory.package import json_bytes, load_json, read_package, write_new
from scripts.synthetic_fixture_audit import export_synthetic_package
from portable_memory.skill_inventory import collect_skills, discover_skills


def demo(output):
    output = Path(output).absolute()
    if output.exists():
        raise ValueError("Choose a new output folder for this fictional demo")
    source = output / "provided-skills" / "weekly-review"
    write_new(source / "SKILL.md", "---\nname: weekly-review\ndescription: Summarize the supplied fictional weekly notes.\n---\n\n# Weekly review\n\nUse the supplied notes. Preserve unresolved questions. See references/format.md.\n")
    write_new(source / "references" / "format.md", "# Fictional format\n\nDecisions; next actions; unanswered questions.\n")
    write_new(source / "scripts" / "format.py", "# Fictional source code; the export must never execute this file.\nraise RuntimeError('This is a transported source, not an export command')\n")
    write_new(source / "agents" / "openai.yaml", "interface:\n  display_name: Fictional weekly review\npolicy:\n  allow_implicit_invocation: false\n")
    provenance = {"product": "fictional-agent", "account_id": "demo-account", "workspace_id": "demo-space",
                  "observed_at": "2026-11-20", "evidence": "Fictional user supplied this selected skill folder",
                  "catalog_complete": False}
    catalog = discover_skills(output / "provided-skills", provenance)
    item = catalog["items"][0]
    item.update({"selected": True, "origin": "user_created", "enabled": False, "version": "1.0",
                 "dependencies": ["Python runtime is required only when running the optional script"]})
    catalog["items"].append({"id": "unavailable-skill", "name": "Fictional cloud-only skill", "version": None,
        "origin": "third_party", "enabled": True, "selected": True, "source_ref": "fictional-catalog-item",
        "locator": {"kind": "unavailable", "reason": "The fictional catalog exposes a name but no original files"},
        "dependencies": []})
    write_new(output / "catalog.json", json_bytes(catalog))
    collected = output / "collected"
    collect_skills(catalog, collected, accepted=True)
    memory = load_json(collected / "memory.json")
    for rid, title, value, scope, locator in [
        ("agent-language", "Ответы агента", "Отвечать по-русски и кратко", "agent:fictional-agent", "Fictional user custom instructions"),
        ("project-template", "Отчёты проекта Маяк", "В отчётах проекта указывать открытые вопросы", "project:mayak", "Fictional project instructions"),
    ]:
        memory["records"].append({"id": rid, "kind": "rule", "title": title, "value": value, "scope": scope,
            "sources": [{"id": rid, "locator": locator, "observed_at": "2026-11-20"}],
            "provenance": "user_statement", "currency": "unknown", "review": {"decision": "pending"}, "links": []})
    # These explicit choices exist only for the fictional fixture. Production
    # workflows must record the user's actual content-bound decisions.
    decisions = [{"id": r["id"], "content_sha256": record_digest(r), "decision": "active"}
                 for r in memory["records"] if r["kind"] == "rule" or r["title"] == item["name"]]
    memory = confirm(memory, {"decisions": decisions})
    write_new(output / "reviewed-memory.json", json_bytes(memory))
    package = output / "memory-package.zip"
    export_synthetic_package(memory, package, assets=collected / "assets")
    readback = read_package(package)
    files = readback["files"]
    assert any(name.endswith("agents/openai.yaml") for name in files)
    assert any(name.endswith("scripts/format.py") for name in files)
    rules = [r for r in readback["memory"]["records"] if r["kind"] == "rule"]
    assert {r["scope"] for r in rules} == {"agent:fictional-agent", "project:mayak"}
    pending = [r["id"] for r in readback["memory"]["records"] if r["review"]["decision"] == "pending"]
    assert len(pending) == 1
    report = {"version": "0.5.0", "synthetic": True, "network_calls": 0,
              "provided_skill_files_preserved": sorted(name for name in files if name.startswith("assets/skills/")),
              "instruction_scopes_preserved": sorted(r["scope"] for r in rules),
              "unavailable_skill_retained_pending": len(pending) == 1,
              "code_executed": False, "native_skill_activation": "not_performed",
              "account_inventory_complete": False}
    write_new(output / "demo-report.json", json_bytes(report))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    print(json.dumps(demo(parser.parse_args().out), ensure_ascii=False, indent=2))
