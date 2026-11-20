"""Synthetic Dots/Duet context -> copied skill -> reviewed portable import.

All sources, account identities, user decisions and tasks in this demonstration
are fictional. No account is accessed, imported code is never executed, and no
native agent memory, integration, skill or schedule is activated.
"""

import argparse
from collections import Counter
import copy
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portable_memory.agent_context import collect_context, context_template
from portable_memory.importing import apply_import, plan_import
from portable_memory.model import confirm, empty_memory, record_digest, validate_memory
from portable_memory.package import json_bytes, load_json, read_package, write_new
from scripts.synthetic_fixture_audit import export_synthetic_package
from portable_memory.skill_context import bind_skill_context, validate_skill_context
from portable_memory.skill_inventory import collect_skills, discover_skills


OBSERVED_AT = "2026-11-20T12:00:00+00:00"
INSTRUCTIONS = {
    "dots": "Для проекта «Альфа»:\n1. Сначала показывай черновик отчёта.\n2. Используй шаблон templates/report.md.\n3. Не применяй это правило к другим проектам.",
    "duet": "Для проекта «Бета» сохраняй суммы в рублях без округления.\nВ итоговом отчёте отдельно отмечай неподтверждённые цифры.",
}


def make_snapshot(platform):
    """A partial explicitly supplied snapshot, never a whole-account claim."""
    project = "alpha" if platform == "dots" else "beta"
    label = "Альфа" if platform == "dots" else "Бета"
    source = {"account_id": "fictional-" + platform + "-account", "workspace_id": "fictional-personal-space",
              "observed_at": OBSERVED_AT,
              "evidence": "SYNTHETIC supplied JSON fixture; no live platform or account access"}
    snapshot = context_template(platform, source)
    categories = {entry["id"]: entry for entry in snapshot["categories"]}

    def add(category, kind, item_id, title, value, scope=None, project_id=None):
        reference = "fixture://" + platform + "/" + category + "/" + item_id
        item = {"id": item_id, "title": platform + ": " + title, "kind": kind,
                "scope": scope or "project:" + project, "value": value, "source_ref": reference}
        if project_id is not None:
            item["project_id"] = project_id
        categories[category].update(status="provided",
            evidence="SYNTHETIC fixture includes this exact entry; no discovery beyond the supplied data",
            source_refs=[reference], items=[item])

    add("projects", "project", project, "Проект " + label,
        {"name": label, "status": "active", "description": "Вымышленная подготовка проектного отчёта"}, project_id=project)
    add("project_instructions", "rule", "report-rule", "Правило отчёта " + label,
        INSTRUCTIONS[platform], project_id=project)
    add("account_instructions", "rule", "communication", "Явная инструкция пользователя",
        "Отвечай по-русски. Перед внешней отправкой покажи черновик и дождись моего решения.", scope="account:" + platform)
    add("agent_rules", "rule", "mail-draft", "Правило рабочей почты",
        "Черновик письма в nina@example.org показывай до отправки.", scope="integration:" + platform + "-mail")
    add("saved_memory", "preference", "format", "Предпочтение оформления",
        "Для этой рабочей области сохраняй итоги в Markdown.", scope="workspace:" + platform)
    add("agent_notes", "idea", "note", "Заметка о проекте " + label,
        "Вымышленная идея: отделять решения от предположений и отмечать основания.", project_id=project)
    add("responsibilities", "workflow", "responsibility", "Обязанность по проекту " + label,
        {"description": "Поддерживать список открытых вопросов проекта", "owner": "Вымышленная Нина",
         "state": "active"}, project_id=project)
    add("active_tasks", "workflow", "current-task", "Текущая задача " + label,
        {"description": "Подготовить первый черновик отчёта", "status": "in_progress",
         "due": "2026-11-25", "next_step": "Собрать подтверждённые показатели", "owner": "Вымышленная Нина"},
        project_id=project)
    add("recurring_tasks", "automation", "weekly-task", "Регулярная задача " + label,
        {"description": "Еженедельный черновик статуса", "schedule": "Пятница, 10:00",
         "timezone": "Europe/Moscow", "source_state": "active",
         "delivery": "Подготовить черновик в проекте, не отправлять автоматически"}, project_id=project)
    add("integrations", "integration", "mail", "Рабочая почта " + platform,
        {"service": "Fictional Mail", "address": "nina@example.org", "source_state": "connected",
         "rules": ["Сначала показывать черновик", "Не смешивать личную и проектную переписку"]},
        scope="integration:" + platform + "-mail")
    add("channels_and_destinations", "resource", "channel", "Канал результатов " + label,
        {"channel": "Вымышленная папка проекта", "resource_id": platform + "::fictional-owner::" + project,
         "purpose": "Хранить согласованные результаты", "access_at_recipient": "unverified"}, project_id=project)
    add("artifacts", "artifact", "report", "Результат работы " + label,
        {"title": "Черновик структуры отчёта", "source_reference": "fixture://" + platform + "/project-report.md",
         "original_file_status": "reference_only_in_context_snapshot"}, project_id=project)
    categories["skills"].update(status="unavailable", evidence="SYNTHETIC source snapshot has no platform skill catalog",
                                source_refs=["fixture://" + platform + "/catalog-not-provided"])
    categories["conversations"].update(status="excluded", evidence="SYNTHETIC user choice: do not transfer complete conversation history",
                                       excluded_by_user=True)
    # agent_instructions deliberately stays pending: absence is not inferred.
    return snapshot


def fixture_skill(folder):
    """Create real original files; these bytes are data and are never executed."""
    files = {
        "SKILL.md": "---\nname: fictional-project-reporting\ndescription: Synthetic reporting skill fixture\n---\n\n"
                    "# Вымышленный навык отчётности\n\nИспользуй templates/report.md.\n"
                    "scripts/render_report.py — исходный пример, который при переносе запускать нельзя.\n",
        "templates/report.md": "# Отчёт проекта\n\n## Подтверждённые сведения\n\n## Открытые вопросы\n",
        "scripts/render_report.py": "# Synthetic source fixture. DO NOT execute during transfer.\n"
                                    "from pathlib import Path\n"
                                    "Path(__file__).with_name('EXECUTION_PROBE.txt').write_text('executed', encoding='utf-8')\n",
    }
    for relative, text in files.items():
        write_new(folder / relative, text)
    return {name: (folder / name).read_bytes() for name in files}


def demo(output):
    output = Path(output).absolute()
    if output.exists():
        raise ValueError("Choose a new demo output folder; previous results are preserved")
    output.mkdir(parents=True)
    inputs = output / "supplied-fixtures"
    snapshots, reports, memories = {}, {}, []
    for platform in ("dots", "duet"):
        snapshot = make_snapshot(platform)
        snapshots[platform] = snapshot
        write_new(inputs / (platform + ".json"), json_bytes(snapshot))
        target = output / ("collected-" + platform)
        reports[platform] = collect_context(snapshot, target, accepted=True)
        memory = load_json(target / "memory.json")
        assert all(record["review"]["decision"] == "pending" for record in memory["records"])
        assert reports[platform]["summary"]["complete_account_export"] is False
        assert reports[platform]["summary"]["all_requested_categories_available"] is False
        memories.append(memory)
        assert load_json(target / "assets/context/source.json") == snapshot

    original_folder = inputs / "skills" / "project-reporting"
    originals = fixture_skill(original_folder)
    catalog = discover_skills(original_folder, {
        "product": "fictional-user-supplied-skill", "account_id": "fictional-owner",
        "workspace_id": "fictional-personal-space", "observed_at": OBSERVED_AT,
        "evidence": "SYNTHETIC explicit local skill folder; no platform account scan", "catalog_complete": False,
    })
    assert len(catalog["items"]) == 1
    item = catalog["items"][0]
    item.update(selected=True, origin="user_created", version="1.0-fixture", enabled=True,
                dependencies=["The supplied report template", "Explicit project instructions selected below"])
    write_new(output / "selected-skill-catalog.json", json_bytes(catalog))
    skill_report = collect_skills(catalog, output / "collected-skills", accepted=True)
    skill_memory = load_json(output / "collected-skills/memory.json")
    skill_id = skill_memory["records"][0]["id"]
    assert skill_memory["records"][0]["review"]["decision"] == "pending"
    memories.append(skill_memory)

    combined = empty_memory()
    combined["metadata"] = {"synthetic": True, "description": "Supplied Dots/Duet fixtures and one supplied original skill"}
    for memory in memories:
        combined["records"].extend(copy.deepcopy(memory["records"]))
        for coverage in ("read", "unavailable", "excluded"):
            combined["coverage"][coverage].extend(memory["coverage"][coverage])
    validate_memory(combined)
    assert len({record["id"] for record in combined["records"]}) == len(combined["records"])

    # Both source snapshots survive without colliding at context/source.json.
    selected_assets = output / "selected-assets"
    shutil.copytree(output / "collected-skills/assets", selected_assets)
    for platform in ("dots", "duet"):
        write_new(selected_assets / "context" / platform / "source.json", json_bytes(snapshots[platform]))
    binding_items, instruction_before = [], {}
    for platform in ("dots", "duet"):
        memory = memories[0 if platform == "dots" else 1]
        project = next(r for r in memory["records"] if r.get("source_category") == "projects")
        instruction = next(r for r in memory["records"] if r.get("source_category") == "project_instructions")
        instruction_before[instruction["id"]] = copy.deepcopy(instruction)
        binding_items.append({"skill_id": skill_id, "project_id": project["id"], "instruction_ids": [instruction["id"]]})
    bindings = {"format": "portable-skill-context-bindings", "schema_version": 1, "items": binding_items}
    write_new(output / "selected-bindings.json", json_bytes(bindings))
    binding_report = bind_skill_context(combined, selected_assets, bindings, output / "bound-copy", accepted=True)
    bound = load_json(output / "bound-copy/memory.json")
    assert all(r["review"]["decision"] == "pending" for r in bound["records"])
    write_new(output / "pending-before-user-review.json", json_bytes(bound))

    # Explicit fixture decisions simulate the user's review. They are not a
    # claim that a real person has approved a real account/profile.
    decisions = {"decisions": [{"id": r["id"], "content_sha256": record_digest(r), "decision": "active"}
                               for r in bound["records"]]}
    write_new(output / "synthetic-user-decisions.json", json_bytes({
        "synthetic": True, "actor": "fictional_user", "note": "Simulated decisions over fictional data only", **decisions}))
    approved = confirm(bound, decisions)
    write_new(output / "reviewed-memory.json", json_bytes(approved))
    package = output / "memory.zip"
    export_synthetic_package(approved, package, assets=output / "bound-copy/assets")
    package_data = read_package(package)
    validate_skill_context(approved, package_data["files"])
    plan = plan_import(package)
    assert all(item["action"] == "add" for item in plan["items"])
    write_new(output / "accepted-import-plan.json", json_bytes(plan))
    workspace = output / "accepted-workspace"
    receipt = apply_import(package, plan, workspace, accepted=True)
    received = load_json(workspace / "memory.json")
    assert received["records"] == approved["records"]
    received_records = {r["id"]: r for r in received["records"]}
    received_skill = received_records[skill_id]
    binding = received_skill["value"]["project_context"]
    entry = (workspace / "incoming" / binding["entry_path"]).read_bytes()
    for bound_instruction in binding["instructions"]:
        rid = bound_instruction["instruction_id"]
        original_instruction = instruction_before[rid]
        imported_instruction = received_records[rid]
        for field in ("value", "scope", "sources", "source_project_id"):
            assert imported_instruction[field] == original_instruction[field]
        reference = (workspace / "incoming" / bound_instruction["reference_path"]).read_text(encoding="utf-8")
        assert original_instruction["value"] in reference
        assert original_instruction["scope"] in reference
        assert json.dumps(original_instruction["sources"], ensure_ascii=False, indent=2) in reference
        relative_reference = bound_instruction["reference_path"].split("/", 3)[3]
        assert relative_reference.encode("utf-8") in entry
    assert (workspace / "incoming" / binding["source_entry_path"]).read_bytes() == originals["SKILL.md"]
    asset_base = workspace / "incoming" / received_skill["value"]["assets_path"]
    assert (asset_base / "scripts/render_report.py").read_bytes() == originals["scripts/render_report.py"]
    assert (asset_base / "templates/report.md").read_bytes() == originals["templates/report.md"]
    for name, raw in originals.items():
        assert (original_folder / name).read_bytes() == raw
    for platform in ("dots", "duet"):
        assert load_json(workspace / "incoming/assets/context" / platform / "source.json") == snapshots[platform]

    repeated_plan = plan_import(package, received)
    assert all(row["status"] == "duplicate" and row["action"] == "skip" for row in repeated_plan["items"])
    repeat_workspace = output / "repeat-comparison-workspace"
    repeated_receipt = apply_import(package, repeated_plan, repeat_workspace, accepted=True)
    repeated_memory = load_json(repeat_workspace / "memory.json")
    assert repeated_memory["records"] == received["records"]
    assert not list(output.rglob("EXECUTION_PROBE.txt"))
    report = {
        "scenario": "supplied_dots_duet_context_and_project_bound_skill",
        "synthetic_only": True, "network_calls": 0,
        "confirmation": "simulated_user_decisions_over_fictional_data_only",
        "common_profile_review": "real_coordinator_validates_scripted_synthetic_attestations",
        "sources": {platform: {"records": reports[platform]["summary"]["records"],
                                  "coverage": dict(Counter(c["status"] for c in reports[platform]["categories"])),
                                  "complete_account_export": False} for platform in ("dots", "duet")},
        "records_pending_before_confirmation": len(bound["records"]),
        "records_imported": len(received["records"]),
        "original_skill_files_copied": skill_report["summary"]["files"],
        "project_instructions_bound": len(binding["instructions"]),
        "exact_instruction_text_scope_sources_preserved": True,
        "skill_entry_references_received_instruction_files": True,
        "source_skill_original_unchanged": True,
        "original_code_bytes_preserved_without_execution": True,
        "both_source_snapshots_preserved": True,
        "repeat_merge": {"duplicate_records_skipped": len(repeated_plan["items"]),
                         "record_count_unchanged": len(repeated_memory["records"]) == len(received["records"]),
                         "records_unchanged": repeated_memory["records"] == received["records"],
                         "result_written_to_new_comparison_workspace": True},
        "native_memory": receipt["native_memory"], "integrations": receipt["integrations"], "skills": receipt["skills"],
        "recurring_tasks_activated": False,
        "limitations": ["No live Dots/Duet account or API was accessed.",
                        "Supplied partial snapshots do not prove account completeness or source export capabilities.",
                        "Semantic and second-pass decisions are explicitly scripted fictional attestations; no model performed a live review.",
                        "Only explicitly selected project-to-skill dependencies were bound.",
                        "Skill source files were copied, not executed, installed or compatibility-tested.",
                        "Import produced portable workspaces only; native memory, connections and schedules were not changed."]}
    assert repeated_receipt["native_memory"] == "not_applied"
    assert binding_report["native_installation"] == "not_performed"
    write_new(output / "demo-report.json", json_bytes(report))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    print(json.dumps(demo(args.out), ensure_ascii=False, indent=2))
