"""Evidence-bound preparation of a profile review, without semantic invention.

The host supplies excerpts and claim annotations. Literal quotation checks do not
prove meaning: a second model pass (or a named human) must attest to support. This
module neither performs that model pass nor discovers unseen account history.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections import defaultdict

from .errors import ValidationError
from .model import canonical, digest, record_digest, validate_memory

CATEGORIES = {
    "identity": "Личный и рабочий контекст",
    "goals": "Цели и ограничения",
    "mailboxes": "Почты и аккаунты",
    "integrations": "Интеграции и доступы",
    "calendar": "Календарь",
    "communication": "Общение",
    "documents": "Документы и оформление",
    "behavior": "Правила работы агента",
    "projects": "Проекты и результаты",
    "workflows": "Навыки и процессы",
    "resources": "Файлы и хранилища",
    "automations": "Автоматизации",
}
_HASH = re.compile(r"^[0-9a-f]{64}$")
_COVERAGE = {"checked", "unavailable", "excluded", "not_applicable"}
_QUALITY = {"supported", "uncertain", "contradicted"}


def _require(condition, message):
    if not condition:
        raise ValidationError(message)


def _text(value, label):
    _require(isinstance(value, str) and bool(value.strip()), f"{label}: nonempty text required")


def _fields(value, fields, label, optional=()):
    _require(isinstance(value, dict), f"{label} must be an object")
    _require(set(fields) <= set(value) and not set(value) - set(fields) - set(optional),
             f"Unexpected or missing fields in {label}")


def _semantic(record):
    # Sources, provenance, scope, links, and extra record fields remain significant.
    return {key: value for key, value in record.items() if key not in {"review", "currency"}}


def _valid_review(record):
    review = record["review"]
    decision = review["decision"]
    return (decision in {"active", "historical", "exclude"}
            and review.get("content_sha256") == record_digest(record)
            and isinstance(review.get("reviewed_at"), str) and bool(review["reviewed_at"].strip())
            and (decision != "active" or record["currency"] == "current")
            and (decision != "historical" or record["currency"] == "historical"))


def prepare_memory(memory, previous=None):
    """Preserve valid decisions; reuse a prior decision only for identical content.

    Currency is restored together with a reused review so its original content
    hash stays valid. Changed scope, evidence provenance, or content never inherits
    an old confirmation. No hash is rewritten to make stale approval look valid.
    """
    validate_memory(memory, allow_stale_review=True)
    if previous is not None:
        validate_memory(previous, allow_stale_review=True)
    prior = {record["id"]: record for record in previous["records"]} if previous is not None else {}
    result = copy.deepcopy(memory)
    for record in result["records"]:
        if _valid_review(record):
            continue
        old = prior.get(record["id"])
        if old is not None and _valid_review(old) and _semantic(old) == _semantic(record):
            record["currency"] = old["currency"]
            record["review"] = copy.deepcopy(old["review"])
        else:
            record["review"] = {"decision": "pending"}
    validate_memory(result)
    return result


def _assessment(memory, assessment):
    _fields(assessment, {"format", "schema_version", "sources", "claims", "coverage"}, "assessment")
    _require(assessment["format"] == "portable-profile-assessment" and
             type(assessment["schema_version"]) is int and assessment["schema_version"] == 1,
             "Unsupported profile assessment")
    records = {record["id"]: record for record in memory["records"]}
    sources, claims, coverage = {}, {}, {}
    for label in ("sources", "claims", "coverage"):
        _require(isinstance(assessment[label], list), f"assessment.{label} must be a list")
    for source in assessment["sources"]:
        _fields(source, {"id", "locator", "text"}, "assessment source")
        _text(source["id"], "source.id")
        _text(source["locator"], "source.locator")
        _require(isinstance(source["text"], str), "source.text must be text")
        _require(source["id"] not in sources, "Duplicate assessment source ID")
        sources[source["id"]] = copy.deepcopy(source)
    for claim in assessment["claims"]:
        _fields(claim, {"record_id", "category", "subject", "predicate", "cardinality", "evidence"}, "claim")
        _text(claim["record_id"], "claim.record_id")
        _require(claim["record_id"] in records and claim["record_id"] not in claims,
                 "Unknown or duplicate claim record ID")
        _require(isinstance(claim["category"], str) and claim["category"] in CATEGORIES, "Unknown claim category")
        _text(claim["subject"], "claim.subject")
        _text(claim["predicate"], "claim.predicate")
        _require(isinstance(claim["cardinality"], str) and claim["cardinality"] in {"single", "multiple"}, "Invalid claim cardinality")
        _require(isinstance(claim["evidence"], list), "claim.evidence must be a list")
        evidence_seen = set()
        for citation in claim["evidence"]:
            _fields(citation, {"source_id", "quote"}, "citation")
            _text(citation["source_id"], "citation.source_id")
            _text(citation["quote"], "citation.quote")
            identity = (citation["source_id"], citation["quote"])
            _require(identity not in evidence_seen, "Duplicate citation")
            evidence_seen.add(identity)
        claims[claim["record_id"]] = copy.deepcopy(claim)
    for item in assessment["coverage"]:
        _fields(item, {"category", "status", "source_ids", "note"}, "coverage entry")
        _require(isinstance(item["category"], str) and item["category"] in CATEGORIES, "Unknown coverage category")
        _require(item["category"] not in coverage, "Duplicate coverage category")
        _require(isinstance(item["status"], str) and item["status"] in _COVERAGE, "Invalid coverage status")
        _require(isinstance(item["source_ids"], list), "coverage.source_ids must be a list")
        for source_id in item["source_ids"]:
            _text(source_id, "coverage source ID")
        _require(len(set(item["source_ids"])) == len(item["source_ids"]), "Duplicate coverage source ID")
        _require(isinstance(item["note"], str), "coverage.note must be text")
        coverage[item["category"]] = copy.deepcopy(item)
    canonical(assessment)
    return sources, claims, coverage


def _claim_hash(record, claim, sources):
    cited = sorted({citation["source_id"] for citation in claim["evidence"]}) if claim is not None else []
    return digest({"record": _semantic(record), "claim": claim,
                   "cited_sources": [{"source_id": source_id, "source": sources.get(source_id)} for source_id in cited]})


def quality_template(memory, assessment):
    """Create an unapproved checklist for an actual second reading of the evidence."""
    validate_memory(memory, allow_stale_review=True)
    sources, claims, _ = _assessment(memory, assessment)
    return {"format": "portable-profile-quality", "schema_version": 1,
            "reviewer": {"kind": "same_model_second_pass", "label": "Укажите фактического проверяющего"},
            "checks": [{"record_id": record["id"],
                        "claim_sha256": _claim_hash(record, claims.get(record["id"]), sources),
                        "decision": "uncertain",
                        "note": "Требуется второй проход: проверить смысл, область действия и опору на приведённые источники."}
                       for record in sorted(memory["records"], key=lambda item: item["id"])]}


def _quality(memory, quality):
    if quality is None:
        return {}, None
    _fields(quality, {"format", "schema_version", "checks"}, "quality", optional={"reviewer"})
    _require(quality["format"] == "portable-profile-quality" and
             type(quality["schema_version"]) is int and quality["schema_version"] == 1, "Unsupported profile quality")
    reviewer = _reviewer(quality.get("reviewer"))
    _require(isinstance(quality["checks"], list), "quality.checks must be a list")
    ids = {record["id"] for record in memory["records"]}
    checks = {}
    for check in quality["checks"]:
        _fields(check, {"record_id", "claim_sha256", "decision", "note"}, "quality check", optional={"reviewer"})
        _text(check["record_id"], "quality record ID")
        _require(check["record_id"] in ids and check["record_id"] not in checks, "Unknown or duplicate quality record ID")
        _require(isinstance(check["claim_sha256"], str) and _HASH.fullmatch(check["claim_sha256"]), "Invalid claim fingerprint")
        _require(isinstance(check["decision"], str) and check["decision"] in _QUALITY, "Invalid quality decision")
        _text(check["note"], "quality note")
        if "reviewer" in check:
            _reviewer(check["reviewer"])
        checks[check["record_id"]] = copy.deepcopy(check)
    canonical(quality)
    return checks, copy.deepcopy(reviewer)


def _reviewer(value):
    if value is None:
        return None
    _fields(value, {"kind", "label"}, "quality reviewer")
    _require(isinstance(value["kind"], str) and value["kind"] in {"same_model_second_pass", "separate_agent", "human"}, "Unknown reviewer kind")
    _text(value["label"], "reviewer.label")
    return copy.deepcopy(value)


def _evidence(record, claim, sources):
    if claim is None:
        return "missing", ["Нет аннотации факта: агент должен указать категорию, предмет, свойство и источники."]
    if not claim["evidence"]:
        return "missing", ["Нет цитат для проверки происхождения сведения."]
    declared = {source["id"] for source in record["sources"]}
    reasons = []
    for citation in claim["evidence"]:
        source_id = citation["source_id"]
        if source_id not in declared:
            reasons.append(f"Цитата {source_id} не входит в источники записи.")
        source = sources.get(source_id)
        if source is None:
            reasons.append(f"Текст источника {source_id} не предоставлен.")
        elif citation["quote"] not in source["text"]:
            reasons.append(f"Цитата не найдена дословно в источнике {source_id}.")
    return ("invalid", reasons) if reasons else ("verified", [])


def _issue_id(prefix, payload):
    return prefix + "-" + digest(payload)[:24]


def _subject(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def audit_profile(memory, assessment, quality=None, previous=None, *, semantic_aliases=None, semantic_conflicts=None):
    """Return agent work and focused user questions, never approve records itself."""
    # Session preparation may reuse older decisions, but auditing a pending user
    # decision must not silently restore an earlier active confirmation.
    prepared = prepare_memory(memory)
    if previous is not None:
        validate_memory(previous, allow_stale_review=True)
    sources, claims, coverage = _assessment(prepared, assessment)
    checks, reviewer = _quality(prepared, quality)
    aliases = semantic_aliases or {}
    _require(isinstance(aliases, dict), "Semantic aliases must be a mapping")
    indexed = {r["id"]: r for r in prepared["records"]}
    for alias, target in aliases.items():
        _require(alias in indexed and target in indexed and alias != target and target not in aliases,
                 "Invalid semantic alias")
        _require(indexed[alias]["kind"] == indexed[target]["kind"] and indexed[alias]["scope"] == indexed[target]["scope"],
                 "Semantic aliases must preserve kind and scope")
    previous_by_id = {record["id"]: record for record in previous["records"]} if previous is not None else {}
    records = {record["id"]: record for record in prepared["records"]}
    entries, tasks = [], []

    def task(kind, record_ids, detail, payload, **extra):
        tasks.append({"id": _issue_id("task", {"kind": kind, "records": sorted(record_ids), "payload": payload}),
                      "kind": kind, "record_ids": sorted(record_ids), "detail": detail, **extra})

    for record in prepared["records"]:
        rid = record["id"]
        claim = claims.get(rid)
        claim_hash = _claim_hash(record, claim, sources)
        evidence_status, reasons = _evidence(record, claim, sources)
        check = checks.get(rid)
        quality_status = "missing" if check is None else ("stale" if check["claim_sha256"] != claim_hash else check["decision"])
        human_confirmed = _valid_review(record) and record["review"]["decision"] in {"active", "historical"}
        quality_ready = ((evidence_status == "verified" and quality_status == "supported") or human_confirmed)
        if quality_status == "contradicted":
            quality_ready = False
            reasons.append("Второй проход обнаружил противоречие: " + check["note"])
        elif human_confirmed and quality_status != "supported":
            reasons.append("Сохранено действительное подтверждение неизменённого содержания; повторное интервью не требуется.")
            quality_status = "human_confirmed"
        decision = record["review"]["decision"]
        old = previous_by_id.get(rid)
        change = "unchanged" if old is not None and _semantic(old) == _semantic(record) else ("changed" if old is not None else "new")
        if previous is None and _valid_review(record):
            change = "unchanged"
        entry = {"id": rid, "category": claim["category"] if claim else None,
                 "change": change, "decision": decision, "content_sha256": record_digest(record),
                 "claim_sha256": claim_hash, "quality_status": quality_status,
                 "quality_reviewer": copy.deepcopy((check.get("reviewer") or reviewer) if check else None),
                 "evidence_status": evidence_status, "quality_ready": quality_ready,
                 "can_confirm": quality_ready and decision != "exclude", "reasons": reasons}
        entries.append(entry)
        if decision == "exclude":
            continue
        if claim is None:
            task("annotation", [rid], "Разметить факт по фактически доступным источникам, не придумывая совпадений.", claim_hash)
        elif evidence_status != "verified":
            task("evidence", [rid], "Проверить ссылки и точные цитаты: " + " ".join(reasons), claim_hash)
        if not human_confirmed or quality_status == "contradicted":
            if evidence_status == "verified" and quality_status != "supported":
                task("quality", [rid], f"Проверить по источникам «{record['title']}»: верно ли содержание и к каким ситуациям оно относится." + (" Предыдущая оценка: " + {"supported": "подтверждается источниками", "uncertain": "недостаточно уверенности", "contradicted": "обнаружено противоречие"}[check["decision"]] + ". " + check["note"] if check is not None else " Смысловая проверка этого сведения ещё не выполнена."),
                     {"claim": claim_hash, "status": quality_status})

    by_id = {entry["id"]: entry for entry in entries}
    groups = defaultdict(list)
    for rid, claim in claims.items():
        record = records[rid]
        if rid not in aliases and claim["cardinality"] == "single" and record["review"]["decision"] in {"active", "pending"}:
            # Scope and predicate are explicit identifiers; do not invent equivalence.
            key = (claim["category"], _subject(claim["subject"]), claim["predicate"], record["scope"])
            groups[key].append(rid)
    conflicts, questions = [], []
    conflict_ids = set()
    for key, ids in sorted(groups.items()):
        if len({canonical(records[rid]["value"]) for rid in ids}) <= 1:
            continue
        ids = sorted(ids)
        signature = digest({"group": key, "claims": [(rid, by_id[rid]["claim_sha256"]) for rid in ids]})
        issue_id = "conflict-" + signature[:24]
        question_text = f"В разделе «{CATEGORIES[key[0]]}» найдены разные варианты: " + ", ".join(f"«{title}»" for title in dict.fromkeys(records[rid]["title"] for rid in ids)) + ". Какой актуален или в каких случаях нужен каждый?"
        conflicts.append({"id": issue_id, "record_ids": ids, "question": question_text,
                          "signature": signature, "category": key[0], "scope": key[3]})
        conflict_ids.update(ids)
        for rid in ids:
            by_id[rid]["can_confirm"] = False
            by_id[rid]["reasons"].append("Есть неразрешённый потенциальный конфликт в одной области действия.")
        if all(by_id[rid]["quality_ready"] for rid in ids):
            questions.append({"id": issue_id, "kind": "conflict", "record_ids": ids,
                              "category": key[0], "title": question_text,
                              "why": "Не будем выбирать более новую запись автоматически или превращать оба варианта в действующие правила."})
        else:
            task("conflict", ids, "Сначала проверить опору на источники и корректность группировки конфликтующих фактов; затем задавать пользователю точечный вопрос.", signature)

    # The global model pass can find conflicts across differently named slots.
    # Preserve its finding even if the literal per-record annotations differ.
    for group in semantic_conflicts or []:
        ids = sorted(rid for rid in group["record_ids"] if rid in records and records[rid]["review"]["decision"] in {"active", "pending"})
        if len(ids) < 2 or any(set(c["record_ids"]) == set(ids) for c in conflicts):
            continue
        signature = digest({"group": group["id"], "records": [(rid, by_id[rid]["claim_sha256"]) for rid in ids]})
        issue_id = "semantic-conflict-" + signature[:24]
        category = by_id[ids[0]]["category"]
        question_text = "В общем профиле найдены несовместимые варианты: " + ", ".join("«" + records[rid]["title"] + "»" for rid in ids) + ". Какой актуален или где действует каждый?"
        conflicts.append({"id": issue_id, "record_ids": ids, "question": question_text, "signature": signature,
                          "category": category, "scope": records[ids[0]]["scope"]})
        conflict_ids.update(ids)
        for rid in ids:
            by_id[rid]["can_confirm"] = False
        if all(by_id[rid]["quality_ready"] for rid in ids):
            questions.append({"id": issue_id, "kind": "conflict", "record_ids": ids, "category": category,
                              "title": question_text, "why": group["note"]})
        else:
            task("conflict", ids, "Перепроверить найденное между источниками смысловое противоречие до вопроса пользователю.", signature)

    for category, label in CATEGORIES.items():
        ids = sorted(entry["id"] for entry in entries if entry["category"] == category and
                     entry["decision"] == "pending" and entry["can_confirm"] and entry["id"] not in conflict_ids and entry["id"] not in aliases)
        if ids:
            questions.append({"id": _issue_id("review", {"category": category, "claims": [(rid, by_id[rid]["claim_sha256"]) for rid in ids]}),
                              "kind": "review", "record_ids": ids, "category": category,
                              "title": f"Проверьте сведения в разделе «{label}»: что актуально, что исправить или сохранить как историю?",
                              "why": "Показаны только сведения, которым нужно ваше решение; неизменённые подтверждённые записи не пересматриваются заново."})

    categories = []
    missing_annotations = any(entry["category"] is None and entry["decision"] != "exclude" for entry in entries)
    not_checked = []
    for category, label in CATEGORIES.items():
        declared = coverage.get(category)
        status = declared["status"] if declared else "not_checked"
        ids = sorted(rid for rid, claim in claims.items() if claim["category"] == category)
        source_ids = list(declared["source_ids"]) if declared else []
        note = declared["note"] if declared else "Раздел пока не проверен; это не означает отсутствие таких данных у пользователя."
        if status == "checked" and (not source_ids or any(source_id not in sources or not sources[source_id]["text"].strip() for source_id in source_ids)):
            status = "not_checked"
            note = "Проверка заявлена, но непустые тексты соответствующих источников не предоставлены."
            task("coverage", ids, "Подтвердить охват раздела фактически прочитанными материалами.",
                 {"category": category, "coverage": declared}, category=category)
        if status == "not_checked":
            not_checked.append(category)
        checked_empty = status == "checked" and not ids
        category_report = {"id": category, "label": label, "status": status, "record_ids": ids,
                           "source_ids": source_ids, "note": note, "checked_empty": checked_empty}
        if checked_empty:
            category_report["finding"] = "В предоставленных проверенных фрагментах сведения не найдены; наличие их у пользователя неизвестно."
            if not missing_annotations:
                payload = {"category": category, "coverage": declared,
                           "sources": [sources[source_id] for source_id in sorted(source_ids)]}
                questions.append({"id": _issue_id("gap", payload), "kind": "gap", "record_ids": [],
                                  "category": category,
                                  "title": f"В просмотренных материалах не нашёл сведений для раздела «{label}». Хотите дополнить его или пока пропустить?",
                                  "why": "Это пробел в доступных материалах, а не вывод, что таких данных у вас нет. Ответ необязателен."})
        categories.append(category_report)
    if not_checked:
        task("coverage", [], "Проверить ещё не оценённые разделы по доступным материалам; не превращать отсутствие доступа в анкету для пользователя.", not_checked, categories=not_checked)
    tasks = list({item["id"]: item for item in tasks}.values())
    summary = {"records_total": len(entries), "new": sum(entry["change"] == "new" for entry in entries),
               "changed": sum(entry["change"] == "changed" for entry in entries),
               "unchanged": sum(entry["change"] == "unchanged" for entry in entries),
               **{decision: sum(entry["decision"] == decision for entry in entries) for decision in ("active", "historical", "exclude", "pending")},
               "quality_ready": sum(entry["quality_ready"] for entry in entries),
               "can_confirm": sum(entry["can_confirm"] for entry in entries),
               "conflicts": len(conflicts), "agent_tasks": len(tasks), "questions": len(questions),
               "not_checked_categories": len(not_checked),
               "checked_empty_categories": sum(category["checked_empty"] for category in categories)}
    return {"memory_sha256": digest(prepared), "records": entries, "categories": categories,
            "conflicts": conflicts, "agent_tasks": tasks, "questions": questions, "summary": summary,
            "quality_reviewer": reviewer,
            "limitations": [
                "Проверяются только предоставленные фрагменты; полный охват аккаунта не подтверждён.",
                "Дословная цитата подтверждает наличие текста, но не его смысловую поддержку; quality — аттестация фактического проверяющего, а не выполненный программой модельный проход.",
                "Сопоставление фактов зависит от явных аннотаций предмета, свойства, категории и области; ошибочная разметка может дать ложный конфликт или пропустить настоящий.",
                "Программа не определяет актуальность по давности и не выбирает самую новую запись автоматически.",
            ]}
