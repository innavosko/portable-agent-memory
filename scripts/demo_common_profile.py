"""Real offline CLI journey with explicitly scripted, fictional review answers.

No live accounts, language-model calls or native-memory activation are involved.
The ordinary CLI/coordinator/package gates are used, with no test bypass.
"""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portable_memory.model import confirm, empty_memory, record_digest
from portable_memory.package import json_bytes, load_json, write_new
from portable_memory.profile_audit import quality_template
from scripts.synthetic_fixture_audit import fictional_assessment

COORDINATOR = "fictional-codex-work-coordinator"


def memory_of(*records):
    result = empty_memory()
    result["metadata"] = {"synthetic": True, "description": "Authored CLI scenario, not real user data"}
    result["records"] = list(records)
    return result


def record(rid, value, scope="work", source="fictional-codex-work"):
    return {"id": rid, "kind": "rule", "title": rid, "value": value, "scope": scope,
            "sources": [{"id": "quote-" + rid, "locator": "synthetic:" + source, "observed_at": None}],
            "provenance": "user_statement", "currency": "unknown", "review": {"decision": "pending"}, "links": []}


def confirmed(memory):
    return confirm(memory, {"decisions": [{"id": r["id"], "content_sha256": record_digest(r), "decision": "active"}
                                           for r in memory["records"]]})


class CLIJourney:
    """Small reusable harness; every operation invokes the real CLI subprocess."""
    def __init__(self, output):
        self.output = Path(output).absolute()
        self.store = self.output / "common.sqlite"
        self.counter = 0
        self.calls = []

    def save(self, label, value):
        self.counter += 1
        path = self.output / f"{self.counter:02d}-{label}.json"
        write_new(path, json_bytes(value))
        return path

    def path(self, label, suffix=".json"):
        self.counter += 1
        return self.output / f"{self.counter:02d}-{label}{suffix}"

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-B", "-m", "portable_memory", *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.calls.append({"command": str(args[0]), "exit_code": result.returncode, "expected": expected})
        if result.returncode != expected:
            raise AssertionError(f"{args[0]} returned {result.returncode}: {result.stderr or result.stdout}")
        return json.loads(result.stdout if expected == 0 else result.stderr)

    def view(self):
        return self.cli("common-view", "--store", self.store)

    def source(self, sid, label, status, memory=None):
        card = self.save("source-card", {"id": sid, "label": label, "status": status,
            "note": "Explicitly supplied fictional scenario source; no live account access."})
        args = ["common-source", "--store", self.store, "--source", card,
                "--revision", self.view()["revision"], "--actor", COORDINATOR]
        if memory is not None:
            args += ["--memory", self.save("source-memory", memory)]
        return self.cli(*args)

    def combined(self):
        target = self.path("combined-input")
        self.cli("common-input", "--store", self.store, "--out", target)
        return load_json(target)

    def review(self, groups=None):
        combined = self.combined()
        target = self.path("unapproved-semantic-template")
        self.cli("common-template", "--store", self.store, "--out", target)
        semantic = load_json(target)
        semantic["reviewer"] = {"kind": "human", "label": "Scripted fictional whole-profile reviewer; not a live model"}
        semantic["cross_source_checked"] = True
        semantic["groups"] = copy.deepcopy(groups) if groups is not None else [{
            "id": "fictional-" + rid, "relation": "compatible", "record_ids": [rid], "canonical_id": None,
            "note": "Explicitly authored independent fictional fact."} for rid in semantic["covered_record_ids"]]
        semantic_path = self.save("semantic-attestation", semantic)
        qa_template = self.path("unapproved-second-pass")
        self.cli("common-quality-template", semantic_path, "--out", qa_template)
        semantic_quality = load_json(qa_template)
        semantic_quality.update(reviewer={"kind": "human", "label": "Scripted fictional second reviewer; not a live model"},
                                decision="supported", note="Authored synthetic second-pass judgment over the complete input and exact scopes.")
        assessment = fictional_assessment(combined["memory"])
        # The fixture deliberately uses one predicate for all sending rules.
        # Equivalence/conflict judgments remain explicit in semantic groups.
        for claim in assessment["claims"]:
            claim["predicate"] = "outgoing_message_approval"
        quality = quality_template(combined["memory"], assessment)
        quality["reviewer"] = {"kind": "human", "label": "Scripted fictional quote-and-scope reviewer"}
        for check in quality["checks"]:
            check.update(decision="supported", note="Fictional exact-source and meaning attestation; no real reviewer was invoked.")
        return self.cli("common-review", "--store", self.store, "--revision", combined["revision"],
            "--actor", COORDINATOR, "--assessment", self.save("assessment", assessment),
            "--quality", self.save("profile-quality", quality), "--semantic", semantic_path,
            "--semantic-quality", self.save("semantic-quality", semantic_quality))

    def result(self, label):
        memory = self.path(label + "-memory")
        report = self.path(label + "-review")
        self.cli("common-result", "--store", self.store, "--out", memory, "--report", report)
        return memory, report

    def export(self, memory, label, expected=0):
        package = self.path(label, ".zip")
        response = self.cli("export", memory, "--common-store", self.store, "--out", package, expected=expected)
        if expected:
            assert not package.exists()
        return package, response


def group(gid, relation, ids, canonical=None):
    return {"id": gid, "relation": relation, "record_ids": ids, "canonical_id": canonical,
            "note": "Explicit fictional judgment: preserve source wording and exact scope; no automatic winner."}


def demo(output):
    output = Path(output).absolute()
    if output.exists():
        raise ValueError("Choose a new output folder for this fictional scenario")
    flow = CLIJourney(output)
    original = confirmed(memory_of(
        record("work-draft", "Перед отправкой рабочего письма покажи мне черновик."),
        record("project-draft", "В проекте Маяк черновик внутреннего отчёта можно готовить без предварительного согласования.", "project:mayak")))
    original_review = copy.deepcopy(original["records"][0]["review"])
    previous = flow.save("previous-confirmed-fixture", original)
    flow.cli("common-create", "--store", flow.store, "--coordinator", COORDINATOR,
             "--previous", previous, "--allow-partial", "--scope-note",
             "Fictional user's explicit choice: continue with available sources while Claude Code is pending.")
    flow.source("chatgpt-personal", "ChatGPT personal supplied excerpt", "provided", memory_of(
        record("personal-wording", "Не отправляй рабочие письма, пока я не посмотрю подготовленный текст.", source="fictional-chatgpt-personal")))
    flow.source("claude-code", "Claude Code supplied project context", "pending")
    equivalents = [group("work-wordings", "equivalent", ["work-draft", "personal-wording"], "work-draft"),
                   group("project-scope", "compatible", ["project-draft"])]
    first = flow.review(equivalents)
    assert first["question"] is None and first["export_allowed"]
    memory_v1, review_v1 = flow.result("first")
    v1 = load_json(memory_v1)
    by_id = {r["id"]: r for r in v1["records"]}
    assert by_id["work-draft"]["review"] == original_review
    assert by_id["personal-wording"]["review"]["inherited_from"] == "work-draft"
    assert by_id["project-draft"]["scope"] == "project:mayak"
    assert load_json(review_v1)["review"]["readiness"] == "partial"
    package_v1, _ = flow.export(memory_v1, "first-package")
    flow.cli("inspect", package_v1)

    late = flow.source("claude-code", "Claude Code supplied project context", "provided", memory_of(
        record("late-work-rule", "Рабочие письма отправляй сразу, без предварительного показа черновика.", source="fictional-claude-code")))
    assert not late["export_allowed"] and late["question"] is None
    _, rejected = flow.export(memory_v1, "stale-package-must-not-exist", expected=2)
    conflicts = [group("work-conflict", "conflict", ["work-draft", "personal-wording", "late-work-rule"]),
                 group("project-scope", "compatible", ["project-draft"])]
    after_review = flow.review(conflicts)
    question = after_review["question"]
    assert question is not None and question["kind"] == "conflict"
    assert set(question["record_ids"]) == {"work-draft", "personal-wording", "late-work-rule"}
    combined = flow.combined()
    by_id = {r["id"]: r for r in combined["memory"]["records"]}
    assert by_id["late-work-rule"]["review"]["decision"] == "pending"
    choices = {"work-draft": "historical", "personal-wording": "historical", "late-work-rule": "active"}
    event = {"id": "fictional-user-resolves-work-rule", "actor": "user", "type": "review", "data": {"decisions": [
        {"id": rid, "content_sha256": record_digest(by_id[rid]), "decision": decision} for rid, decision in choices.items()]}}
    flow.cli("common-event", "--store", flow.store, "--event", flow.save("synthetic-user-event", event),
             "--revision", combined["revision"], "--actor", COORDINATOR)
    final_view = flow.view()
    assert final_view["question"] is None and final_view["export_allowed"]
    memory_v2, _ = flow.result("final")
    final = load_json(memory_v2)
    final_by_id = {r["id"]: r for r in final["records"]}
    assert {rid: final_by_id[rid]["review"]["decision"] for rid in choices} == choices
    assert final_by_id["project-draft"] == original["records"][1]
    package_v2, _ = flow.export(memory_v2, "final-package")
    plan = flow.path("import-plan")
    flow.cli("plan", package_v2, "--out", plan)
    workspace = output / "received-portable-workspace"
    receipt = flow.cli("apply", package_v2, "--plan", plan, "--out", workspace, "--accept")
    repeat = flow.path("repeat-import-plan")
    flow.cli("plan", package_v2, "--current", workspace / "memory.json", "--out", repeat)
    repeat_plan = load_json(repeat)
    assert all(item["action"] == "skip" and item["status"] == "duplicate" for item in repeat_plan["items"])
    assert receipt["native_memory"] == "not_applied"
    report = {"scenario": "whole_profile_cli_review_with_late_source", "synthetic_only": True,
        "passed": True, "coordinator": COORDINATOR, "source_labels": ["Codex work", "ChatGPT personal", "Claude Code"],
        "commands_executed": len(flow.calls), "scripted_model_and_user_attestations": True,
        "network_calls": 0, "live_model_calls": 0, "live_accounts_accessed": False,
        "unchanged_confirmation_reused": True, "paraphrase_inherited_without_repeat_question": True,
        "project_scope_preserved": True, "partial_export_while_waiting": True,
        "late_source_invalidated_old_export": bool(rejected), "conflict_question_record_ids": sorted(question["record_ids"]),
        "conflict_resolved_only_by_explicit_fictional_user": True, "records_imported": len(final["records"]),
        "repeat_duplicate_records": len(repeat_plan["items"]), "native_memory": "not_applied",
        "limitations": ["Source excerpts, semantic conclusions, second-pass judgments and user answers are authored fixtures.",
                        "No real model, account, cloud transport, email watcher or native agent setup was tested.",
                        "The ordinary coordinator, SQLite authority, CLI, package and portable import gates were exercised."]}
    write_new(output / "command-report.json", json_bytes(flow.calls))
    write_new(output / "demo-report.json", json_bytes(report))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(demo(parser.parse_args().out), ensure_ascii=False, indent=2))
