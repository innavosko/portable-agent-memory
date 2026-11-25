"""Progressive conversation prompts; no account access, user consent or uploads.

The host presents notices and questions and records actual answers through the
migration store. Generating this view never means a notice was shown or accepted.
"""
import copy

from .migration import migration_view, validate_migration
from .model import digest, require


def _question(key, title, why, options, suffix=""):
    return {"id": "journey:" + key + suffix, "answer_key": key,
            "title": title, "why": why, "options": options}


def journey_view(state, actor=None):
    validate_migration(state)
    actor = actor or state["coordinator"]
    require(actor in state["actors"], "Unknown actor")
    base = migration_view(state, actor)
    answers = state["answers"]
    selected = [s for s in state["sources"] if s["selected"]]
    unresolved = any(s["identity_status"] != "verified" for s in selected)
    mode = "unresolved" if not selected or unresolved else "single_source" if len(selected) == 1 else "coordinator"
    role_key = digest({"coordinator": state["coordinator"], "sources": [
        {"id": s["id"], "identity_status": s["identity_status"]} for s in selected]})[:16]
    roles = {
        "single_source": "Источник один. Я соберу и проверю доступные данные здесь за один проход, затем подготовлю экспорт. Если обнаружатся противоречия или пробелы, уточню только их.",
        "coordinator": "Я — ведущий агент: соберу материалы выбранных источников, сведу общий профиль, проверю смысловые дубли и противоречия. Устанавливать навык во все источники необязательно; их файлы можно передать мне. После добавления источника повторю общую проверку, сохранив прежние согласования.",
        "unresolved": "Помогу перенести память. С одним источником соберу экспорт здесь; с несколькими стану ведущим агентом и сведу их данные. Сначала уточним состав: браузер и приложение с общей историей могут быть одним источником."
    }
    notices = [{"id": "journey:role:" + role_key, "kind": "role", "message": roles[mode]}]
    location_map = [{"source_id": s["id"], "label": s["label"], "interfaces": copy.deepcopy(s["surfaces"]),
                     "locations": copy.deepcopy(s.get("locations", [])), "inventory_checked": "locations" in s}
                    for s in selected]
    if any(surface["interface"] in {"desktop", "cli"} for s in selected for surface in s["surfaces"]):
        notices.append({"id": "journey:locations:" + digest(location_map)[:16], "kind": "source_locations", "message":
            "Отдельно учитываю приложение, место хранения и доступ к данным. У десктоп-агента могут быть история аккаунта, локальные файлы и файлы в облаке. Общую историю не дублирую; дополнительные файлы учитываю отдельно. Непроверенный доступ отмечаю как неизвестный."})
    if not answers.get("scope"):
        notices.append({"id": "journey:start", "kind": "start_offer", "message":
            "Предлагаю начать перенос: соберу сведения о вас, правила работы и выбранные материалы, покажу результат и подготовлю его для нового агента."})
    target = answers.get("recipient")
    if target == "gigaagent":
        notices.append({"id": "journey:target:gigaagent", "kind": "target", "message":
            "Для ГигаАгента подготовлю client-memory-export.md: отдельные записи user, feedback, project и reference в его лимитах. Полные беседы, навыки и процедуры сохраню отдельно, если они выбраны; загрузка этих файлов не означает их установку в ГигаАгент."})
    elif target == "gigaagent_pro":
        notices.append({"id": "journey:target:gigaagent_pro", "kind": "target", "message":
            "ГигаАгент Про (ex-Ouroboros) принимает разные файлы. Подготовлю выбранные исходники, память, инструкции и навыки; получатель отдельно проверит доступные способы их применения."})
    questions = []
    if "scope" not in answers:
        questions.append(_question("scope", "Что переносим?", "Выберите объём; остальные шаги подстрою под него.", [
            {"label": "Только профиль и правила", "value": ["profile"]},
            {"label": "Профиль, важные работы и навыки", "value": ["profile", "projects", "skills"]},
            {"label": "Добавить полную историю", "value": ["profile", "projects", "skills", "history"]}]))
    if not selected:
        questions.append(_question("sources", "Из каких агентов или проектов собираем данные?", "Назовите источники; известные аккаунты и файлы повторно перечислять не нужно.", []))
    unchecked_desktop = [s for s in selected if "locations" not in s and any(
        surface["interface"] in {"desktop", "cli"} for surface in s["surfaces"])]
    if unchecked_desktop:
        questions.append(_question("source_locations", "Для десктоп-агентов переносим только историю аккаунта или также файлы на компьютере и в облаке?", "Уточню только неизвестные места: " + ", ".join(s["label"] for s in unchecked_desktop) + ". Уже названные папки и источники использую без повторного вопроса.",
                                   ["Только история аккаунта", "Добавить локальные папки", "Добавить облачные файлы", "Указать и локальные, и облачные файлы"],
                                   ":" + digest([s["id"] for s in unchecked_desktop])[:16]))
    if "recipient" not in answers:
        questions.append(_question("recipient", "Какого агента настраиваем?", "От этого зависит формат экспорта и способ записи памяти.", [
            {"label": "ГигаАгент", "value": "gigaagent"},
            {"label": "ГигаАгент Про (ex-Ouroboros)", "value": "gigaagent_pro"},
            {"label": "Другой агент — укажите какой", "value": "other"}]))
    scope = answers.get("scope", [])
    if scope and ("projects" in scope or "skills" in scope or "history" in scope) and "skill_packaging" not in answers:
        questions.append(_question("skill_packaging", "Предложить, какие полезные работы и беседы превратить в навыки?", "Покажу найденные примеры и пользу; вы выберете, что сохранить. Сначала нужен отбор, затем создание и проверка навыка.", [
            {"label": "Да, предложи кандидатов", "value": "accept"},
            {"label": "Сохранить материалы без новых навыков", "value": "decline"},
            {"label": "Вернуться позже", "value": "later"}]))
    if "obsidian" not in answers:
        questions.append(_question("obsidian", "Вы уже храните память или заметки в Obsidian?", "Можно использовать существующие заметки или подготовить папку Markdown со связями. Obsidian необязателен; платная синхронизация не нужна для локальных файлов.", [
            {"label": "Да, использовать существующее хранилище", "value": {"choice": "existing"}},
            {"label": "Нет, хочу папку для Obsidian", "value": {"choice": "create"}},
            {"label": "Продолжить без Obsidian", "value": {"choice": "skip"}},
            {"label": "Решить позже", "value": {"choice": "later"}}]))
    if "destination" not in answers:
        questions.append(_question("destination", "Куда сохранить готовый набор?", "Предлагаю папку «Перенос памяти». Укажите папку на компьютере, доступный облачный диск или выберите скачивание из чата. Доступ к облаку проверю отдельно.", [
            {"label": "Скачать из чата", "value": {"kind": "upload", "label": "Скачать из текущего чата"}},
            {"label": "Папка на компьютере — укажите путь", "kind": "folder"},
            {"label": "Google Диск, Яндекс Диск, Облако Mail, OneDrive или другое облако — укажите папку", "kind": "drive"}]))
    format_invalid = target == "gigaagent" and answers.get("save_format") not in {None, "markdown"}
    if "save_format" not in answers or format_invalid:
        options = [{"label": "Markdown для ГигаАгента; остальные материалы отдельно", "value": "markdown"}] if target == "gigaagent" else [
            {"label": "Полный набор: ZIP и читаемые Markdown-файлы", "value": "portable_bundle"},
            {"label": "Markdown-документы", "value": "markdown"}]
        if target != "gigaagent" and answers.get("obsidian", {}).get("choice") in {"existing", "create"}:
            options.append({"label": "Папка Markdown со связями для Obsidian", "value": "obsidian_vault"})
        questions.append(_question("save_format", "Как сохранить результат?", "Выбранный формат должен подходить получателю. Полные исходники и навыки не сжимаются до краткого профиля.", options, ":" + (target or "unknown")))
    # Source questions and partial-delivery choices still use the shared job state.
    legacy = base["question"] or base["awaiting_answer"]
    if legacy and legacy["id"] not in {"scope", "sources", "destination"}:
        questions.append(legacy)
    if actor != state["coordinator"]:
        questions, notices = [], []
    shown = set(state["shown_questions"])
    pending = next((q for q in questions if q["id"] in shown), None)
    question = None if pending else (questions[0] if questions else None)
    ready = bool(target and "scope" in answers and "destination" in answers and "save_format" in answers and not format_invalid)
    return {**base, "coordinator": state["coordinator"], "role": mode,
            "known_choices": copy.deepcopy(answers), "notices": [n for n in notices if n["id"] not in shown],
            "source_locations": location_map,
            "question": question, "awaiting_answer": pending, "export_choices_ready": ready,
            "recommendation": "client-memory-export.md" if target == "gigaagent" else "Перенос памяти/",
            "semantic_review_required": True, "native_memory_applied": False}


def render_journey_view(view):
    lines = [n["message"] for n in view["notices"]]
    lines += [view["summary"]]
    if view["question"]:
        question = view["question"]
        lines += ["", question["title"], question["why"]]
        for option in question["options"]:
            lines.append("- " + (option["label"] if isinstance(option, dict) else option))
    elif view["awaiting_answer"]:
        lines += ["", "Жду ответа на предыдущий вопрос; доступные материалы можно обрабатывать дальше."]
    return "\n\n".join(lines) + "\n"
