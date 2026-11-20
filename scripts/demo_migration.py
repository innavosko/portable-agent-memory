"""Offline fictional demo: seven observations, four sources, late arrival/resume.

No accounts are read, no messages are sent, no background watcher is started,
and no native agent memory is modified. All choices/evidence are test fixtures.
"""

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portable_memory.errors import ValidationError
from portable_memory.migration import (
    MigrationStore, collect_migration, create_migration, migration_view, render_migration_view,
)


def prepare_config(examples_dir=None):
    """Load the fictional config and resolve input paths relative to examples.

    The file on disk remains portable and unchanged. This helper does not read
    real accounts or discover local projects outside the explicitly given folder.
    """
    examples = Path(examples_dir or ROOT / "examples").resolve()
    config = json.loads((examples / "migration.json").read_text(encoding="utf-8-sig"))
    for observation in config["sources"]:
        supplied = observation.get("input")
        if supplied is None:
            continue
        relative = Path(supplied["path"])
        if relative.is_absolute():
            raise ValidationError("Demo input paths must be relative to the examples folder")
        resolved = (examples / relative).resolve()
        if resolved != examples and examples not in resolved.parents:
            raise ValidationError("Demo input escapes the examples folder")
        supplied["path"] = str(resolved)
    return config


def _write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _check(condition, message):
    if not condition:
        raise ValidationError("Demo verification failed: " + message)


def _event(store, kind, data, event_id):
    state = store.load()
    return store.event({"id": event_id, "actor": "export-agent", "type": kind, "data": data},
                       expected_revision=state["revision"])


def demo(output):
    output = Path(output).absolute()
    if output.exists():
        raise ValidationError("Choose a new demo output directory; prior results are not overwritten")
    config = prepare_config()
    state = create_migration(config)
    _check(len(config["sources"]) == 7 and len(state["sources"]) == 4,
           "seven observations must resolve to four distinct datasets")
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "fictional-config-resolved.json", config)
    store = MigrationStore.create(output / "migration.sqlite", state)
    claude = next(source for source in state["sources"] if source["identity"]["namespace"] == "anthropic")
    claude_job = next(job for job in state["jobs"] if job["source_id"] == claude["id"])
    _event(store, "wait", {"job_id": claude_job["id"], "reason": "ДЕМО: архив Claude ещё ожидается; реальная почта не проверяется."}, "demo-wait-claude")

    initial = collect_migration(store, output / "collected", actor="export-agent", max_workers=3)
    _check(len(initial["outcomes"]) == 3 and all(item["ok"] for item in initial["outcomes"]),
           "the three available datasets must be collected successfully")
    _check(initial["view"]["counts"]["collected"] == 3 and initial["view"]["counts"]["waiting"] == 1,
           "Claude must remain waiting while the other sources are collected")
    (output / "progress.md").write_text(
        "# Вымышленная демонстрация: первый этап\n\n"
        "Это проверка на подготовленных файлах. Ответы пользователя и подтверждения источников вымышлены.\n\n"
        + render_migration_view(initial["view"])
        + "\nПочта не подключалась. Профиль остаётся черновиком; активная память не изменялась.\n",
        encoding="utf-8",
    )
    prior_results = {job["id"]: copy.deepcopy(job["result"]) for job in store.load()["jobs"] if job["status"] == "collected"}
    repeated = collect_migration(store, output / "collected", actor="export-agent", max_workers=3)
    _check(repeated["outcomes"] == [], "unchanged completed sources must not be collected again")

    _event(store, "provide_input", {
        "source_id": claude["id"],
        "input": {"kind": "archive", "path": str((ROOT / "examples" / "claude-export.json").resolve())},
        "evidence": "ДЕМО: подготовленный вымышленный файл стал доступен. Письма и сетевые сервисы не использовались.",
    }, "demo-provide-claude")
    late = collect_migration(store, output / "collected", actor="export-agent", max_workers=3)
    _check(len(late["outcomes"]) == 1 and late["outcomes"][0]["job_id"] == claude_job["id"] and late["outcomes"][0]["ok"],
           "only the newly provided Claude source must be collected")
    latest = store.load()
    _check(all(job["result"] == prior_results[job["id"]] for job in latest["jobs"] if job["id"] in prior_results),
           "previous collection results must remain unchanged")
    _check(all(job["attempt"] == 1 for job in latest["jobs"]), "each dataset must have been collected once")

    # Explicit handoff: the original authority is no longer used after this point.
    _write_json(output / "migration-snapshot.json", latest)
    resumed_store = MigrationStore.create(output / "resumed-migration.sqlite", json.loads((output / "migration-snapshot.json").read_text(encoding="utf-8")))
    resumed = collect_migration(resumed_store, output / "collected", actor="export-agent", max_workers=3)
    resumed_state = resumed_store.load()
    _check(resumed["outcomes"] == [] and resumed_state["revision"] == latest["revision"],
           "resuming an unchanged snapshot must produce no collection or duplicate events")
    _check(resumed["view"]["counts"]["collected"] == 4, "all four sources must remain available after resume")
    (output / "final-progress.md").write_text(
        "# Вымышленная демонстрация: сбор завершён\n\n"
        + render_migration_view(resumed["view"])
        + "\nЭто завершение сбора файлов. Извлечение, проверка профиля и применение памяти — отдельные этапы.\n",
        encoding="utf-8",
    )
    report = {
        "format": "portable-memory-migration-demo", "schema_version": 1,
        "synthetic": True, "client_choices_are_fictional": True,
        "output": str(output), "observations": 7, "canonical_sources": 4,
        "initial": {"jobs_collected": 3, "waiting": 1, "max_workers": 3, "outcomes": initial["outcomes"]},
        "repeat": {"jobs_collected": len(repeated["outcomes"])},
        "late_arrival": {"jobs_collected": 1, "job_id": claude_job["id"], "earlier_results_unchanged": True},
        "resume": {"jobs_collected": 0, "revision_unchanged": True,
                   "questions_unchanged": resumed["view"]["question"] == migration_view(latest)["question"]},
        "final_counts": resumed["view"]["counts"],
        "native_memory": "not_applied", "background_watch": "not_started",
        "limitations": ["Only supplied fictional files were collected.",
                        "No profile decisions were automatically confirmed.",
                        "The resumed store replaces the original authority; live cloud synchronization is not demonstrated."],
    }
    _write_json(output / "report.json", report)
    return report


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run a fully offline fictional multi-source collection demo")
    parser.add_argument("--out", required=True, help="New output directory")
    print(json.dumps(demo(parser.parse_args().out), ensure_ascii=False, indent=2))
