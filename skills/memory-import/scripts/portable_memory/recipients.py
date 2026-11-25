"""Recipient profiles and a conservative GigaAgent Markdown adapter.

The host performs semantic extraction/review and native memory tool calls. This
module validates their explicit inputs and prepares files/plans; it does not
contact an agent, infer facts, or report an unperformed memory write as success.
"""
import copy
import hashlib
import html
import re
from collections import Counter
from datetime import datetime

from .common_profile import validate_export_review
from .model import canonical, digest, now, record_digest, require


LIMITS = {"user": 20, "feedback": 30, "project": 100, "reference": 40}
PRIORITIES = {"high": 0, "medium": 1, "low": 2}
NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,98}[A-Za-z0-9])?$")
READY = "Готово к импорту"
SECTIONS = {
    "clarify": "Требует уточнения — не импортировать",
    "temporary": "Временный контекст — не импортировать",
    "reserve": "Справочное приложение и резерв — не импортировать автоматически",
}
FIELDS = ("memory_type", "description", "priority", "observed_at", "evidence")
PROVENANCE_SUFFIX = "\nИсточник: пользовательский импорт."
MAX_GIGA_PREVIEW_BYTES = 50_000_000


def recipient_catalog():
    """Declared intake profiles, not a capability probe or integration claim."""
    general = [("chatgpt", "ChatGPT"), ("claude", "Claude"), ("dots", "Dots"),
               ("duet", "Duet"), ("codex", "Codex"), ("claude_code", "Claude Code")]
    profiles = [{"id": rid, "label": label, "intake": "check_host_tools",
                 "native_setup": "requires_available_tools_and_verification"}
                for rid, label in general]
    profiles += [
        {"id": "gigaagent", "label": "ГигаАгент", "intake": "client-memory-export.md",
         "accepted_extensions": [".md"], "archives_supported": False, "skill_installation_supported": False,
         "memory_limits": dict(LIMITS), "export_content_chars": 450,
         "save_content_chars_including_final_newline": 500, "description_chars": 200,
         "native_tools": ["memory_list", "memory_read", "memory_save"],
         "native_setup": "tool_calls_required_no_zip_restore",
         "basis": "User-supplied extract-instruction.md and import-instruction.md"},
        {"id": "gigaagent_pro", "label": "ГигаАгент Про (ex-Ouroboros)",
         "intake": "user_reports_any_files", "preferred_transfer": "portable_package_and_selected_originals",
         "native_setup": "file_intake_does_not_prove_memory_write_or_skill_installation",
         "basis": "User-described flexible file intake; verify current host tools"},
        {"id": "other", "label": "Другой агент", "intake": "check_host_tools",
         "native_setup": "requires_available_tools_and_verification"},
    ]
    return {"format": "portable-memory-recipients", "schema_version": 1, "recipients": profiles}


def _line(value, label, *, empty=False):
    require(isinstance(value, str) and (empty or bool(value.strip())), label + ": text required")
    require(not any(ord(char) < 32 or ord(char) == 127 for char in value), label + ": single line required")
    return value


def _escape(value):
    # Data cannot create Markdown sections, links, code fences or raw HTML.
    return "".join("&#" + str(ord(char)) + ";" if char in "\\`*_{}[]()#+-.!|" else html.escape(char, quote=True)
                   for char in str(value))


def _safe_name(value):
    value = re.sub(r"[^A-Za-z0-9_-]", "-", value).strip("-_")[:100].rstrip("-_")
    return value or "fact"


def _projection_hash(projection):
    return digest({key: value for key, value in projection.items() if key != "quality"})


def _review_hash(review):
    # CommonStore emits a new receipt timestamp on each read. The evidence,
    # semantic conclusions and confirmed content, not that emission time, bind
    # the projection to the review.
    return digest({key: value for key, value in review.items() if key != "created_at"})


def _quality_template(input_hash):
    return {"input_sha256": input_hash, "reviewer": {"kind": "same_model_second_pass", "label": ""},
            "decision": "pending", "note": ""}


def _validate_quality(quality, input_hash):
    require(isinstance(quality, dict) and quality.get("input_sha256") == input_hash,
            "Projection/comparison quality check is missing or stale")
    require(quality.get("decision") == "supported", "A separate semantic verification pass is required")
    reviewer = quality.get("reviewer")
    require(isinstance(reviewer, dict) and reviewer.get("kind") in {"separate_agent", "same_model_second_pass", "human"},
            "Label the actual reviewer; Python does not perform semantic review")
    _line(reviewer.get("label"), "reviewer.label")
    _line(quality.get("note"), "quality.note")


def giga_projection_quality_template(projection):
    return _quality_template(_projection_hash(projection))


def giga_comparison_quality_template(comparisons):
    require(isinstance(comparisons, dict) and comparisons.get("format") == "gigaagent-import-comparison",
            "Invalid GigaAgent comparison")
    return _quality_template(_projection_hash(comparisons))


def giga_projection_template(memory, review):
    """Blank target facts bound to the existing checked common profile.

    Hosts must classify/atomize/word each fact and verify the projection. Never
    flatten arbitrary project text, workflows or skills into active user facts.
    """
    validate_export_review(memory, review)
    aliases = memory["metadata"]["common_profile"]["aliases"]
    entries = []
    for record in memory["records"]:
        if record["review"]["decision"] == "exclude" or record["id"] in aliases:
            continue
        entries.append({"source_record_ids": [record["id"]],
                        "source_record_hashes": {record["id"]: record_digest(record)},
                        "disposition": "draft", "reason": "",
                        "record": {"name": _safe_name(record["id"]), "memory_type": "",
                                   "description": "", "priority": "medium", "observed_at": "неизвестно",
                                   "evidence": "", "content": ""}})
    result = {"format": "gigaagent-projection", "schema_version": 1,
              "source_memory_sha256": digest(memory), "source_review_sha256": _review_hash(review),
              "metadata": {"source_agent": review["coordinator"], "exported_at": now(), "subject": "",
                           "available_sources": "; ".join(memory["coverage"]["read"]) or "Указаны в исходном пакете",
                           "coverage_limits": "Только предоставленные источники; недоступные чаты не проверены."},
              "entries": entries}
    result["quality"] = giga_projection_quality_template(result)
    return result


def _validate_fact(record, *, constrained=True):
    require(isinstance(record, dict), "GigaAgent fact must be an object")
    require(set(record) == {"name", *FIELDS, "content"}, "Unexpected or missing GigaAgent fact fields")
    require(isinstance(record["name"], str) and NAME.fullmatch(record["name"]), "Invalid semantic fact name")
    for key in (*FIELDS, "content"):
        _line(record[key], "fact." + key)
    require(record["memory_type"] in LIMITS, "Only user/feedback/project/reference are importable; never knowledge")
    require(record["priority"] in PRIORITIES, "Invalid priority")
    if constrained:
        require(len(record["description"]) <= 200, "description exceeds 200 characters")
        require(len(record["content"]) <= 450, "export content exceeds 450 characters")
    require(len(record["content"]) <= 100000, "Fact is too large; split the source explicitly")


def build_giga_export(memory, review, projection):
    """Render a reviewed target projection; excess facts remain in reserve."""
    validate_export_review(memory, review)
    require(isinstance(projection, dict) and projection.get("format") == "gigaagent-projection"
            and projection.get("schema_version") == 1, "Invalid GigaAgent projection")
    require(projection.get("source_memory_sha256") == digest(memory)
            and projection.get("source_review_sha256") == _review_hash(review), "Projection source changed; project and review again")
    _validate_quality(projection.get("quality"), _projection_hash(projection))
    metadata = projection.get("metadata")
    require(isinstance(metadata, dict) and set(metadata) == {"source_agent", "exported_at", "subject", "available_sources", "coverage_limits"},
            "Export metadata must describe subject, sources and coverage")
    for key, value in metadata.items():
        _line(value, "metadata." + key)
    records = {r["id"]: r for r in memory["records"] if r["review"]["decision"] != "exclude"}
    aliases = memory["metadata"]["common_profile"]["aliases"]
    expected = set(records) - set(aliases)
    entries = projection.get("entries")
    require(isinstance(entries, list), "Projection entries required")
    seen, covered, classified = set(), set(), []
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {"source_record_ids", "source_record_hashes", "disposition", "reason", "record"},
                "Invalid projection entry")
        ids = entry["source_record_ids"]
        require(isinstance(ids, list) and ids and all(isinstance(rid, str) and rid in expected for rid in ids)
                and len(set(ids)) == len(ids), "Projection must reference actual canonical records")
        require(entry["source_record_hashes"] == {rid: record_digest(records[rid]) for rid in ids}, "Stale projection record binding")
        covered.update(ids)
        disposition = entry["disposition"]
        require(disposition in {"ready", *SECTIONS}, "Classify draft facts before export")
        _line(entry["reason"], "projection.reason")
        fact = entry["record"]
        _validate_fact(fact, constrained=False)
        require(fact["name"].casefold() not in seen, "Fact names must be unique")
        seen.add(fact["name"].casefold())
        if disposition == "ready":
            require(all(records[rid]["review"]["decision"] == "active" and records[rid]["currency"] == "current" for rid in ids),
                    "Historical/pending facts cannot become ready through a target projection")
            require(all(records[rid]["kind"] not in {"workflow", "skill", "automation", "artifact", "idea"} for rid in ids),
                    "Procedures, skills, artifacts and tasks belong in a reference appendix, not disguised native facts")
        reason = entry["reason"]
        if disposition == "ready" and (len(fact["description"]) > 200 or len(fact["content"]) > 450):
            disposition, reason = "reserve", "Превышен размер записи; нужно содержательно сократить или разделить, текст сохранён."
        classified.append({"record": copy.deepcopy(fact), "disposition": disposition, "reason": reason, "source_record_ids": ids})
    require(covered == expected, "Every canonical source record must be accounted for, including reserve/uncertainty")
    counts = Counter()
    classified.sort(key=lambda item: PRIORITIES[item["record"]["priority"]])
    for item in classified:
        category = item["record"]["memory_type"]
        if item["disposition"] == "ready":
            if counts[category] >= LIMITS[category]:
                item.update(disposition="reserve", reason="Превышен предел категории; запись сохранена в резерве.")
            else:
                counts[category] += 1
    labels = {"source_agent": "Исходный агент", "exported_at": "Дата экспорта", "subject": "О ком экспорт",
              "available_sources": "Доступные источники", "coverage_limits": "Ограничения полноты"}
    lines = ["# Экспорт памяти о пользователе", "", "## Сведения об экспорте"]
    lines.extend("- " + labels[key] + ": " + _escape(value) for key, value in metadata.items())
    lines += ["", "## " + READY, ""]
    for item in classified:
        if item["disposition"] == "ready":
            lines.extend(_render_fact(item["record"]))
    for disposition, title in SECTIONS.items():
        selected = [item for item in classified if item["disposition"] == disposition]
        if not selected:
            continue
        lines += ["## " + title, ""]
        for item in selected:
            lines.extend(_render_fact(item["record"]))
            lines += ["Причина: " + _escape(item["reason"]), ""]
    lines += ["## Итог", "", "; ".join(f"{category}: {counts[category]}" for category in LIMITS) + ".",
              "Приложения и резерв не импортируются автоматически. Процедуры не являются knowledge принимающего агента.", ""]
    markdown = "\n".join(lines)
    # Round-trip the importable section so escaping never changes data boundaries.
    parsed = parse_giga_export(markdown)
    require(parsed["records"] == [item["record"] for item in classified if item["disposition"] == "ready"],
            "Markdown round-trip changed ready facts")
    report = {"target": "gigaagent", "filename": "client-memory-export.md", "ready_counts": dict(counts),
              "items": [{"name": i["record"]["name"], "disposition": i["disposition"], "reason": i["reason"],
                         "source_record_ids": i["source_record_ids"]} for i in classified],
              "projection_sha256": _projection_hash(projection), "native_memory": "not_written",
              "limitations": ["Meaning and privacy exclusions were checked by the named host reviewer, not inferred by Python.",
                              "Actual receiving capacity includes its existing and archived memory; import must check it again."]}
    return {"markdown": markdown, "report": report}


def _preview_next_action(item, entry):
    disposition = item["disposition"]
    if disposition == "clarify":
        return ("Проверить доступные источники и прежние согласования. Если они не снимают неопределённость, "
                "задать только необходимый вопрос. После уточнения заново проверить изменённые сведения.")
    if disposition == "temporary":
        return ("Сохранить как контекст для продолжения работы с известной датой и статусом; "
                "не превращать разовую задачу или временное состояние в постоянное правило пользователя.")
    record = entry["record"]
    if entry["disposition"] == "ready" and (len(record["description"]) > 200 or len(record["content"]) > 450):
        return ("Содержательно сократить запись или разделить на самостоятельные подтверждённые факты. "
                "Сохранить условия, даты и основания, повторно проверить смысл; не обрезать текст автоматически.")
    if entry["disposition"] == "ready":
        return ("Проверить смысловые дубли и приоритет записей этой категории. Выбрать наиболее полезные "
                "подтверждённые факты в пределах лимита, сохранить остальное отдельно; не менять категорию ради обхода лимита.")
    return ("Сохранить исходный материал отдельно в выбранном пользователем хранилище. Проверить, можно ли "
            "выделить из него самостоятельные сведения о пользователе и заново проверить их происхождение и смысл. "
            "Процедуры и навыки не выдавать за факты; наличие резервной копии подтвердить только после её создания.")


def _render_giga_preview(preview):
    lines = ["# Перед экспортом в ГигаАгент", "",
             "Ниже — что подготовлено для долгосрочной памяти, что в неё не войдёт и почему. "
             "Это предварительный обзор; память принимающего агента ещё не изменена.", "", "## Охват экспорта", ""]
    for key, label in (("source_agent", "Исходный агент"), ("exported_at", "Дата подготовленной выгрузки"),
                       ("subject", "О ком экспорт"), ("available_sources", "Указанные доступные источники"),
                       ("coverage_limits", "Ограничения полноты")):
        lines += ["- " + label + ": " + _escape(preview["metadata"][key])]
    for key, label in (("read", "Прочитанные источники по карте охвата"), ("unavailable", "Недоступные источники"),
                       ("excluded", "Исключённые источники")):
        labels = preview["coverage"][key]
        lines += ["- " + label + ": " + ("; ".join(_escape(value) for value in labels) if labels else "не указаны") + "."]
    lines += ["- Записей, исключённых по прежнему решению пользователя: " + str(preview["excluded_by_user_count"]) +
              ". Их содержимое здесь не повторяется.",
              "- Дубликатов, сведённых к общей записи: " + str(preview["deduplicated_alias_count"]) + ".", "",
              "## Подготовлено для импорта", "",
             "; ".join(f"{category}: {preview['ready_counts'].get(category, 0)}" for category in LIMITS) + ".", ""]
    for item in preview["ready"]:
        lines += ["### " + _escape(item["name"]) + " — " + item["memory_type"], "", _escape(item["content"]), "",
                  "Основание: " + _escape(item["evidence"]),
                  "Исходные записи: " + ", ".join(_escape(rid) for rid in item["source_record_ids"]) + ".", ""]
    if not preview["ready"]:
        lines += ["Готовых записей нет. Ничего не будет добавлено ради количества.", ""]
    lines += ["## Не войдёт в долгосрочную память", "",
              "Эти материалы отделены от готовых записей. Справочные разделы Markdown не импортируются автоматически.", ""]
    for item in preview["excluded"]:
        lines += ["### " + _escape(item["name"]) + " — " + item["memory_type"], "", _escape(item["content"]), "",
                  "Раздел: " + SECTIONS[item["disposition"]] + ".",
                  "Причина: " + _escape(item["reason"]),
                  "Что можно сделать: " + _escape(item["next_action"]),
                  "Основание: " + _escape(item["evidence"]),
                  "Исходные записи: " + ", ".join(_escape(rid) for rid in item["source_record_ids"]) + ".", ""]
    if not preview["excluded"]:
        lines += ["Среди подготовленных записей таких материалов нет.", ""]
    if preview["split_sources"]:
        lines += ["## Разделённые исходные записи", ""]
        for source in preview["split_sources"]:
            lines += ["- " + _escape(source["source_record_id"]) + ": " + "; ".join(
                _escape(part["name"]) + " — " + ("готово к импорту" if part["disposition"] == "ready"
                                                   else SECTIONS[part["disposition"]])
                for part in source["parts"]) + "."]
        lines += [""]
    lines += ["## Границы проверки", "", *["- " + text for text in preview["limitations"]], ""]
    return "\n".join(lines)


def build_giga_preview(memory, review, projection):
    """Deterministic pre-export review, preserving the exporter's actual gates.

    Semantic splitting/classification is supplied by the host in projection.
    This function reports its validated effective result, including length and
    capacity overflow; it does not infer facts or attest that a user saw a file.
    """
    result = build_giga_export(memory, review, projection)
    entries = {entry["record"]["name"]: entry for entry in projection["entries"]}
    ready, excluded, sources = [], [], {}
    for outcome in result["report"]["items"]:
        entry = entries[outcome["name"]]
        item = {**copy.deepcopy(entry["record"]), "disposition": outcome["disposition"], "reason": outcome["reason"],
                "source_record_ids": list(outcome["source_record_ids"]),
                "source_record_hashes": dict(entry["source_record_hashes"])}
        if item["disposition"] == "ready":
            ready.append(item)
        else:
            item["next_action"] = _preview_next_action(outcome, entry)
            excluded.append(item)
        for rid in item["source_record_ids"]:
            source = sources.setdefault(rid, {"source_record_id": rid,
                                              "source_record_sha256": item["source_record_hashes"][rid], "parts": []})
            source["parts"].append({"name": item["name"], "memory_type": item["memory_type"],
                                    "disposition": item["disposition"]})
    preview = {"format": "gigaagent-export-preview", "schema_version": 1,
               "source_memory_sha256": digest(memory), "source_review_sha256": _review_hash(review),
               "projection_sha256": result["report"]["projection_sha256"],
               "markdown_sha256": hashlib.sha256(result["markdown"].encode("utf-8")).hexdigest(),
               "metadata": copy.deepcopy(projection["metadata"]), "coverage": copy.deepcopy(memory["coverage"]),
               "excluded_by_user_count": sum(record["review"]["decision"] == "exclude" for record in memory["records"]),
               "deduplicated_alias_count": len(memory["metadata"]["common_profile"]["aliases"]),
               "ready_counts": dict(result["report"]["ready_counts"]), "ready": ready, "excluded": excluded,
               "split_sources": [sources[rid] for rid in sorted(sources) if len(sources[rid]["parts"]) > 1],
               "limitations": [
                   "Модель выделяет смыслы, проверяет источники и актуальность. Код проверяет предоставленную разметку, "
                   "связи с исходными записями, наличие проверок и ограничения формата; он не выполняет смысловую проверку сам.",
                   "Цель — сохранить полезные подтверждённые сведения. Лимиты категорий являются верхними границами, а не планом заполнения.",
                   "Полнота ограничена доступными источниками; недоступные материалы не считаются просмотренными.",
                   "При импорте нужно проверить текущую и архивную память ГигаАгента: существующие записи занимают часть лимитов.",
                   "Обзор не подтверждает, что пользователь уже прочитал его, и не записывает память или резервные копии."],
               "native_memory": "not_written"}
    preview["markdown"] = _render_giga_preview(preview)
    require(len(canonical(preview)) <= MAX_GIGA_PREVIEW_BYTES, "GigaAgent preview is too large; split the source explicitly")
    return preview


def validate_giga_preview(preview, memory, review, projection):
    """Reject a missing, edited or stale preview; do not infer user approval."""
    require(isinstance(preview, dict), "A GigaAgent export preview is required before final export")
    expected = build_giga_preview(memory, review, projection)
    require(digest(preview) == digest(expected), "GigaAgent preview is changed or stale; generate and show the current preview")
    return expected


def _render_fact(record):
    return ["### " + record["name"], *["- " + key + ": " + _escape(record[key]) for key in FIELDS],
            "", "**content**", _escape(record["content"]), ""]


def parse_giga_export(markdown):
    """Read only the ready section. All document text remains untrusted data."""
    require(isinstance(markdown, str) and len(markdown) <= 2000000, "Invalid or oversized Markdown document")
    lines = markdown.replace("\r\n", "\n").split("\n")
    headings = [(i, line[3:].strip()) for i, line in enumerate(lines) if line.startswith("## ")]
    require(all(title in {"Сведения об экспорте", READY, *SECTIONS.values(), "Итог"} for _, title in headings),
            "Unexpected section in the GigaAgent memory document")
    ready = [i for i, title in headings if title == READY]
    require(len(ready) == 1, "Exactly one 'Готово к импорту' section is required")
    start = ready[0] + 1
    end = next((i for i, _ in headings if i >= start), len(lines))
    blocks, block = [], []
    for line in lines[start:end]:
        if line.startswith("### "):
            if block:
                blocks.append(block)
            block = [line]
        elif block:
            block.append(line)
        else:
            require(not line.strip(), "Unexpected content before the first ready fact")
    if block:
        blocks.append(block)
    records, names = [], set()
    for block in blocks:
        clean = [line for line in block if line.strip()]
        require(len(clean) >= 8 and clean[6] == "**content**", "Each ready block must have five metadata lines and a content paragraph")
        record = {"name": clean[0][4:].strip()}
        for index, key in enumerate(FIELDS, 1):
            prefix = "- " + key + ": "
            require(clean[index].startswith(prefix), "Unexpected or reordered GigaAgent metadata")
            record[key] = html.unescape(clean[index][len(prefix):])
        content_lines = clean[7:]
        require(not any(re.match(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|`{3}|~{3}|>|<!--|\*\*content\*\*)", line)
                        for line in content_lines), "Ready content must be a paragraph, not headings, metadata, lists or fenced code")
        # Markdown soft line wraps do not change the fact's meaning or count as
        # separate native memories. Validate the resulting text after joining.
        record["content"] = html.unescape(" ".join(line.strip() for line in content_lines))
        _validate_fact(record)
        require(record["name"].casefold() not in names, "Duplicate ready fact name")
        names.add(record["name"].casefold())
        records.append(record)
    counts = Counter(record["memory_type"] for record in records)
    require(all(counts[key] <= limit for key, limit in LIMITS.items()), "Ready category exceeds GigaAgent export limit")
    return {"format": "gigaagent-import-document", "schema_version": 1, "records": records,
            "document_sha256": digest(markdown), "semantic_review": "required", "native_memory": "not_written"}


def validate_giga_delivery(markdown):
    """Validate a complete deliverable against the supplied GigaAgent export spec.

    The lower-level parser intentionally remains usable for a ready-section
    excerpt. A delivered profile additionally needs its source/coverage metadata
    and summary. This validates structure, not the truth of natural-language text.
    """
    parsed = parse_giga_export(markdown)
    lines = markdown.replace("\r\n", "\n").split("\n")
    nonempty = [(index, line) for index, line in enumerate(lines) if line.strip()]
    require(nonempty and nonempty[0][1] == "# Экспорт памяти о пользователе",
            "GigaAgent delivery requires the title '# Экспорт памяти о пользователе'")
    heading_lines = [(index, re.fullmatch(r" {0,3}(#{1,2})[ \t]+(.*?)[ \t]*", line))
                     for index, line in enumerate(lines)]
    heading_lines = [(index, match) for index, match in heading_lines if match]
    require(all(lines[index].startswith(match.group(1) + " ") for index, match in heading_lines),
            "Delivery headings must use the supplied Markdown layout")
    titles = [(index, match.group(2)) for index, match in heading_lines if match.group(1) == "#"]
    require(len(titles) == 1, "GigaAgent delivery must contain exactly one document title")
    headings = [(index, match.group(2)) for index, match in heading_lines if match.group(1) == "##"]
    order = ["Сведения об экспорте", READY, *SECTIONS.values(), "Итог"]
    names = [title for _, title in headings]
    require(all(title in order for title in names), "Unexpected section in the GigaAgent delivery")
    require(len(set(names)) == len(names), "Duplicate section in the GigaAgent delivery")
    require(all(title in names for title in (order[0], READY, order[-1])),
            "GigaAgent delivery requires export metadata, ready records and a summary")
    require([order.index(title) for title in names] == sorted(order.index(title) for title in names),
            "GigaAgent delivery sections are out of order")
    metadata_start = headings[0][0] + 1
    metadata_end = headings[1][0]
    labels = {"Исходный агент", "Дата экспорта", "О ком экспорт", "Доступные источники", "Ограничения полноты"}
    metadata = {}
    for line in lines[metadata_start:metadata_end]:
        if not line.strip():
            continue
        match = re.fullmatch(r"- ([^:]+):[ \t]*(.*)", line)
        require(match is not None and match.group(1) in labels,
                "Export metadata must use the five labels from the GigaAgent specification")
        label, value = match.groups()
        require(label not in metadata, f"Duplicate export metadata label: {label}")
        require(bool(html.unescape(value).strip()), f"Empty export metadata: {label}")
        metadata[label] = value
    require(set(metadata) == labels, "GigaAgent delivery requires all five export metadata fields")
    require(any(html.unescape(line).strip() for line in lines[headings[-1][0] + 1:]),
            "GigaAgent delivery summary must not be empty")
    return parsed


def _snapshot(snapshot):
    require(isinstance(snapshot, dict) and snapshot.get("format") == "gigaagent-memory-snapshot"
            and snapshot.get("schema_version") == 1, "Invalid native memory snapshot")
    require(type(snapshot.get("complete")) is bool and type(snapshot.get("include_archived")) is bool,
            "Snapshot must explicitly state completeness and include_archived")
    records = snapshot.get("records")
    require(isinstance(records, list), "Native memory records required")
    names = set()
    for record in records:
        require(isinstance(record, dict) and isinstance(record.get("name"), str) and NAME.fullmatch(record["name"]),
                "Invalid existing memory name")
        require(record["name"].casefold() not in names, "Duplicate existing memory name")
        names.add(record["name"].casefold())
        require(record.get("memory_type") in {*LIMITS, "knowledge"}, "Invalid current memory category")
        for key in ("description", "content"):
            if key in record:
                require(isinstance(record[key], str), "Existing memory text must be text")
    return records


def giga_comparison_template(document, snapshot):
    document = parse_giga_export(document) if isinstance(document, str) else document
    require(isinstance(document, dict) and document.get("format") == "gigaagent-import-document", "Invalid parsed document")
    _snapshot(snapshot)
    result = {"format": "gigaagent-import-comparison", "schema_version": 1,
              "document_sha256": document["document_sha256"], "snapshot_sha256": digest(snapshot),
              "incoming_records_sha256": digest(document["records"]),
              "incoming_and_existing_checked": False,
              "items": [{"name": record["name"], "relation": "defer", "existing_names": [],
                         "note": "Сопоставьте смысл, субъект, актуальность и область применения.", "result": None}
                        for record in document["records"]]}
    result["quality"] = _quality_template(_projection_hash(result))
    return result


def _full_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        return None
    try:
        return value if datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None else None
    except ValueError:
        return None


def _save_payload(record, *, existing=None):
    content = record["content"]
    observed = _full_timestamp(record["observed_at"])
    if observed is None and record["observed_at"] != "неизвестно":
        content += "\nДата сведения: " + record["observed_at"] + "."
    content += PROVENANCE_SUFFIX + "\n"
    if len(content) > 500 or len(record["description"]) > 200:
        return None
    result = {key: record[key] for key in ("name", "memory_type", "description")}
    result["content"] = content
    if observed:
        result["observed_at"] = observed
    if existing is not None:
        stamp = existing.get("updated_at_epoch")
        require(isinstance(stamp, str) and bool(stamp), "Read current updated_at_epoch and preserve its exact string")
        result["expected_updated_at"] = stamp
    return result


def plan_giga_import(document, snapshot, comparisons=None):
    """Prepare individually reviewable native tool arguments, never execute them.

    Without a complete archived-inclusive inventory and a source-bound semantic
    comparison all records remain deferred. An empty or partial list is never
    evidence that the recipient has no memory.
    """
    document = parse_giga_export(document) if isinstance(document, str) else copy.deepcopy(document)
    require(isinstance(document, dict) and document.get("format") == "gigaagent-import-document"
            and document.get("schema_version") == 1, "Invalid parsed document")
    require(isinstance(document.get("records"), list), "Incoming records required")
    for record in document["records"]:
        _validate_fact(record)
    require(len({r["name"].casefold() for r in document["records"]}) == len(document["records"]), "Duplicate incoming name")
    incoming_counts = Counter(r["memory_type"] for r in document["records"])
    require(all(incoming_counts[key] <= limit for key, limit in LIMITS.items()), "Incoming category exceeds GigaAgent export limit")
    current = _snapshot(snapshot)
    by_name = {r["name"]: r for r in current}
    occupancy = Counter(r["memory_type"] for r in current)
    plan = {"format": "gigaagent-import-plan", "schema_version": 1,
            "document_sha256": document["document_sha256"], "snapshot_sha256": digest(snapshot),
            "native_memory": "not_written", "authorization_required": True, "items": [],
            "capacity_including_archived": dict(occupancy),
            "after_each_write": "memory_read and compare; on MEMORY_CONFLICT reread and replan; never retry the whole batch blindly"}
    inventory_ok = snapshot["complete"] and snapshot["include_archived"]
    checks = {}
    if comparisons is not None:
        require(isinstance(comparisons, dict) and comparisons.get("format") == "gigaagent-import-comparison"
                and comparisons.get("schema_version") == 1, "Invalid native comparison")
        require(comparisons.get("document_sha256") == document["document_sha256"]
                and comparisons.get("snapshot_sha256") == digest(snapshot)
                and comparisons.get("incoming_records_sha256") == digest(document["records"]), "Native comparison is stale")
        require(comparisons.get("incoming_and_existing_checked") is True, "Check duplicates and conflicts within incoming data and against current memory")
        _validate_quality(comparisons.get("quality"), _projection_hash(comparisons))
        require(isinstance(comparisons.get("items"), list), "Comparison items required")
        for item in comparisons["items"]:
            require(isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"] not in checks,
                    "Duplicate/invalid comparison item")
            checks[item["name"]] = item
        require(set(checks) == {r["name"] for r in document["records"]}, "Comparison must cover all incoming facts")
    reserved_names = {r["name"].casefold() for r in current}
    updated_names = set()
    for record in sorted(document["records"], key=lambda r: PRIORITIES[r["priority"]]):
        item = {"name": record["name"], "memory_type": record["memory_type"], "action": "defer", "save_arguments": None,
                "reason": "Нужны полный список памяти с архивными записями и смысловая проверка.", "evidence": record["evidence"]}
        plan["items"].append(item)
        if not inventory_ok or comparisons is None:
            continue
        check = checks[record["name"]]
        relation, names = check.get("relation"), check.get("existing_names")
        require(relation in {"new", "duplicate", "merge", "conflict", "unrelated_name_collision", "defer"}, "Unknown comparison relation")
        _line(check.get("note"), "comparison.note")
        require(isinstance(names, list) and all(isinstance(name, str) and name in by_name for name in names)
                and len(names) == len(set(names)), "Unknown existing memory match")
        item["reason"] = check["note"]
        if relation in {"defer", "conflict"}:
            continue
        if relation in {"duplicate", "merge", "unrelated_name_collision"}:
            require(names and all(by_name[name].get("read_verified") is True and isinstance(by_name[name].get("content"), str) for name in names),
                    "Read each matching/colliding current record before deciding")
        if relation == "duplicate":
            item["action"] = "skip"
            continue
        target = copy.deepcopy(record)
        existing = None
        if relation == "merge":
            require(len(names) == 1, "Merge one current record at a time without deleting the others")
            existing = by_name[names[0]]
            if existing.get("archived") is not False:
                item["reason"] = "Запись архивная или её статус неизвестен; проверьте статус и отдельно согласуйте восстановление, не активируйте автоматически."
                continue
            if existing["name"] in updated_names:
                item["reason"] = "Эта запись уже меняется в плане; объедините изменения и перечитайте её перед следующей записью."
                continue
            if existing["memory_type"] != record["memory_type"]:
                item["reason"] = "Нельзя менять категорию существующей записи при импорте."
                continue
            require(isinstance(check.get("result"), dict), "An explicit reviewed merged result is required")
            target = check["result"]
            _validate_fact(target)
            require(target["name"] == existing["name"] and target["memory_type"] == existing["memory_type"], "Merge cannot rename or recategorize current memory")
        else:
            if relation == "new":
                require(not names, "A new fact cannot have unchecked current matches")
            if relation == "unrelated_name_collision":
                require(isinstance(check.get("result"), dict), "Choose a distinct meaningful name for the unrelated fact")
                target = check["result"]
                _validate_fact(target)
                require({k: v for k, v in target.items() if k != "name"} == {k: v for k, v in record.items() if k != "name"},
                        "Name collision resolution must not rewrite the fact")
            if target["name"].casefold() in reserved_names:
                item["reason"] = "Имя уже занято; выберите другое смысловое имя без замены чужого факта."
                continue
            if occupancy[target["memory_type"]] >= LIMITS[target["memory_type"]]:
                item["reason"] = "Недостаточно места в категории; существующая память не вытесняется."
                continue
        payload = _save_payload(target, existing=existing)
        if payload is None:
            item["reason"] = "После сохранения происхождения/даты превышен лимит; нужно согласовать сокращение."
            continue
        item.update(action="update" if existing else "add", save_arguments=payload)
        reserved_names.add(target["name"].casefold())
        if existing:
            updated_names.add(existing["name"])
        else:
            occupancy[target["memory_type"]] += 1
    plan["planned_counts"] = dict(Counter(item["action"] for item in plan["items"]))
    plan["projected_capacity_including_archived"] = dict(occupancy)
    return plan


def verify_giga_readback(plan, observed_records):
    """Compare actual host memory_read results after attempted writes.

    Missing results remain unverified. Tool execution/authenticity is outside
    Python; callers must not substitute the planned arguments for actual reads.
    """
    require(isinstance(plan, dict) and plan.get("format") == "gigaagent-import-plan", "Invalid import plan")
    require(isinstance(observed_records, list), "Actual memory_read results required")
    by_name = {}
    for record in observed_records:
        require(isinstance(record, dict) and isinstance(record.get("name"), str) and record["name"] not in by_name, "Invalid/repeated readback")
        by_name[record["name"]] = record
    results = []
    for item in plan["items"]:
        if item["action"] not in {"add", "update"}:
            continue
        payload = item["save_arguments"]
        actual = by_name.get(payload["name"])
        comparable = {key: value for key, value in payload.items() if key != "expected_updated_at"}
        verified = actual is not None and all(actual.get(key) == value for key, value in comparable.items())
        results.append({"name": payload["name"], "verified": verified,
                        "reason": "matches_actual_readback" if verified else "missing_or_changed_readback"})
    return {"native_memory": "verified_supplied_readbacks" if results and all(r["verified"] for r in results) else "not_fully_verified",
            "records": results, "remaining_deferred": [i["name"] for i in plan["items"] if i["action"] == "defer"],
            "limitation": "The host must supply genuine tool results; this function does not call memory_read."}
