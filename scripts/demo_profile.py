"""Offline profile update with explicitly fictional evidence and user choices."""
import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portable_memory.model import confirm, empty_memory, record_digest, review_template
from portable_memory.profile_audit import quality_template
from portable_memory.profile_session import (apply_profile_event, create_profile,
                                             profile_result, profile_view, render_profile_view)
from portable_memory.package import json_bytes, write_new
from scripts.synthetic_fixture_audit import export_synthetic_package
from portable_memory.importing import apply_import, plan_import


def profile_fixture():
    """Never apply these synthetic choices to a real user's information."""
    evidence = {
        "old": "Отвечай кратко. Моя основная рабочая почта nina-old@example.org.",
        "new": "Моя основная рабочая почта nina@example.org. Встречи по 25 минут. Показывай черновик письма до отправки.",
    }

    def record(rid, kind, title, value, source):
        return {"id": rid, "kind": kind, "title": title, "value": value, "scope": "global",
                "sources": [{"id": source, "locator": "Вымышленная беседа " + source, "observed_at": None}],
                "provenance": "user_statement", "currency": "unknown", "review": {"decision": "pending"}, "links": []}

    tone = record("tone", "preference", "Стиль ответа", "Отвечать кратко", "old")
    old_mail = record("mail-old", "account", "Прежняя основная рабочая почта", "nina-old@example.org", "old")
    previous = empty_memory()
    previous["metadata"] = {"synthetic": True}
    previous["records"] = [tone, old_mail]
    decisions = review_template(previous)
    for item in decisions["decisions"]:
        item["decision"] = "active"
    previous = confirm(previous, decisions)
    memory = empty_memory()
    memory["metadata"] = {"synthetic": True}
    memory["coverage"]["read"] = ["Вымышленные беседы old и new"]
    memory["records"] = [tone, old_mail,
                         record("mail-new", "account", "Новая основная рабочая почта", "nina@example.org", "new"),
                         record("meeting", "preference", "Длительность встречи", "25 минут", "new"),
                         record("invented", "rule", "Предположение модели об отправке", "Можно отправлять письма без согласования", "new")]
    memory["records"][-1]["provenance"] = "model_inference"
    annotations = [
        ("tone", "communication", "response_style", "old", "Отвечай кратко."),
        ("mail-old", "mailboxes", "primary_work_email", "old", "Моя основная рабочая почта nina-old@example.org."),
        ("mail-new", "mailboxes", "primary_work_email", "new", "Моя основная рабочая почта nina@example.org."),
        ("meeting", "calendar", "meeting_duration", "new", "Встречи по 25 минут."),
        ("invented", "behavior", "send_permission", "new", "Показывай черновик письма до отправки."),
    ]
    assessment = {"format": "portable-profile-assessment", "schema_version": 1,
                  "sources": [{"id": key, "locator": "Вымышленная беседа " + key, "text": text} for key, text in evidence.items()],
                  "claims": [{"record_id": rid, "category": category, "subject": "user", "predicate": predicate,
                              "cardinality": "single", "evidence": [{"source_id": source, "quote": quote}]}
                             for rid, category, predicate, source, quote in annotations],
                  "coverage": [{"category": category, "status": "checked", "source_ids": ["old", "new"],
                                "note": "Просмотрены только два вымышленных фрагмента"}
                               for category in ("communication", "mailboxes", "calendar", "behavior")] + [
                      {"category": "documents", "status": "unavailable", "source_ids": [],
                       "note": "Вымышленный шаблон документа ещё не предоставлен"}]}
    quality = quality_template(memory, assessment)
    quality["reviewer"] = {"kind": "same_model_second_pass", "label": "Вымышленный результат второго прохода для теста"}
    for check in quality["checks"]:
        check["decision"] = "uncertain" if check["record_id"] == "invented" else "supported"
        check["note"] = ("Источник требует показать черновик, но не устанавливает разрешение отправлять без согласования; политика согласования остаётся неизвестной" if check["record_id"] == "invented"
                         else "Вымышленная проверка: цитата подтверждает ровно указанное содержание")
    return memory, assessment, quality, previous


def demo(output):
    output = Path(output)
    if output.exists():
        raise ValueError("Choose a new demo output folder")
    memory, assessment, quality, previous = profile_fixture()
    state = create_profile(memory, assessment, quality, previous)
    for name, value in (("memory.json", memory), ("assessment.json", assessment), ("quality.json", quality), ("previous.json", previous), ("state-0.json", state)):
        write_new(output / name, json_bytes(value))
    view = profile_view(state)
    write_new(output / "before.md", render_profile_view(view))
    assert len(view["conflicts"]) == 1
    assert view["question"]["kind"] == "conflict"
    assert "tone" not in view["question"]["record_ids"]
    by_id = {r["id"]: r for r in state["memory"]["records"]}
    event = {"id": "demo-user-resolves-mail", "actor": "user", "type": "review", "data": {"decisions": [
        {"id": rid, "content_sha256": record_digest(by_id[rid]), "decision": decision}
        for rid, decision in (("mail-old", "historical"), ("mail-new", "active"), ("invented", "exclude"))]}}
    state = apply_profile_event(state, event, expected_revision=state["revision"])
    question = profile_view(state)["question"]
    assert question and question["record_ids"] == ["meeting"]
    state = apply_profile_event(state, {"id": "demo-user-later", "actor": "user", "type": "defer",
        "data": {"question_id": question["id"], "reason": "Вымышленный пользователь проверит длительность позже"}}, expected_revision=state["revision"])
    write_new(output / "state-final.json", json_bytes(state))
    restored = json.loads((output / "state-final.json").read_text(encoding="utf-8"))
    final_view = profile_view(restored)
    assert final_view["question"] is None and len(final_view["deferred_questions"]) == 1
    result = profile_result(restored)
    write_new(output / "prepared-memory.json", json_bytes(result["memory"]))
    write_new(output / "quality-report.json", json_bytes(result["report"]))
    write_new(output / "after.md", render_profile_view(final_view))
    package = output / "memory.zip"
    export_synthetic_package(result["memory"], package,
                             assessment=restored["assessment"], quality=restored["quality"])
    plan = plan_import(package)
    receipt = apply_import(package, plan, output / "recipient", accepted=True)
    accepted_ids = {item["id"] for item in receipt["records"] if item["result"] == "stored_in_portable_workspace"}
    assert accepted_ids == {"tone", "mail-old", "mail-new"}
    report = {"version": "0.3.0", "synthetic": True, "passed": True,
              "unchanged_confirmation_reused": True, "conflict_resolved_by_fictional_user": True,
              "unsupported_inference_not_activated": True, "deferred_question_not_repeated_after_resume": True,
              "unreviewed_records": result["report"]["unreviewed_records"], "native_memory": "not_applied",
              "portable_import_confirmed_records": len(accepted_ids),
              "limits": ["Prepared synthetic evidence and second-pass verdicts; no live model API call or SaaS account used.",
                         "This does not prove the completeness or truth of arbitrary model extraction."]}
    write_new(output / "demo-report.json", json_bytes(report))
    return report


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Demonstrate profile review using fictional data only")
    parser.add_argument("--out", required=True)
    print(json.dumps(demo(parser.parse_args().out), ensure_ascii=False, indent=2))
