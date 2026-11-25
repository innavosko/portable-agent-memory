"""One resumable journey, dataset jobs and a single local transactional authority.

SQLite coordinates workers sharing this store. Exported JSON snapshots do not
coordinate separate computers or SaaS sessions; handoff must stop the old owner.
"""
import copy
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from .errors import ValidationError
from .model import canonical, check_no_secret_fields, digest, now, require
from .sources import normalize_sources, resolve_identity, validate_source

STATUSES = {"needs_identity", "needs_input", "needs_choice", "ready", "running",
            "waiting", "deferred", "collected", "failed"}
ANSWER_KEYS = {"scope", "destination", "delivery", "recipient", "skill_packaging", "obsidian", "save_format"}


def _text(value, label):
    require(isinstance(value, str) and bool(value.strip()), f"{label}: nonempty text required")


def _answers(answers):
    require(isinstance(answers, dict) and not set(answers) - ANSWER_KEYS, "Unknown answer key")
    if "scope" in answers:
        require(isinstance(answers["scope"], list) and bool(answers["scope"]) and
                all(x in {"profile", "projects", "skills", "history"} for x in answers["scope"]), "Invalid scope")
    if "destination" in answers:
        route = answers["destination"]
        require(isinstance(route, dict) and route.get("kind") in {"upload", "drive", "folder"}, "Invalid destination")
        _text(route.get("label"), "destination.label")
    if "delivery" in answers:
        require(answers["delivery"] in {"as_ready", "together"}, "Invalid delivery choice")
    if "recipient" in answers:
        require(answers["recipient"] in {"gigaagent", "gigaagent_pro", "chatgpt", "claude", "dots",
                "duet", "codex", "claude_code", "other"}, "Invalid recipient")
    if "skill_packaging" in answers:
        require(answers["skill_packaging"] in {"accept", "decline", "later"}, "Invalid skill packaging choice")
    if "obsidian" in answers:
        choice = answers["obsidian"]
        require(isinstance(choice, dict) and not set(choice) - {"choice", "location"} and
                choice.get("choice") in {"existing", "create", "skip", "later"}, "Invalid Obsidian choice")
        if "location" in choice:
            _text(choice["location"], "obsidian.location")
    if "save_format" in answers:
        require(answers["save_format"] in {"portable_bundle", "markdown", "obsidian_vault"}, "Invalid save format")
    check_no_secret_fields(answers)


def _input(candidate):
    return {key: copy.deepcopy(value) for key, value in candidate.items() if key != "observation_ids"}


def _initial_status(source):
    if not source["selected"]:
        return "deferred"
    if source["identity_status"] != "verified":
        return "needs_identity"
    return {"missing": "needs_input", "choice_required": "needs_choice", "ready": "ready"}[source["input_status"]]


def _sync_jobs(state):
    old = {j["source_id"]: j for j in state["jobs"]}
    updated = []
    for source in state["sources"]:
        job = old.get(source["id"])
        if job is None:
            job = {"id": "job-" + digest(source["id"])[:20], "source_id": source["id"],
                   "owner": state["coordinator"], "status": _initial_status(source),
                   "input": _input(source["inputs"][0]) if len(source["inputs"]) == 1 else None,
                   "input_chosen": False,
                   "attempt": 0, "run_token": None, "result": None, "error": None,
                   "reason": None, "history": []}
        elif not source["selected"]:
            require(job["status"] != "running", "Stop the running owner before deselecting its source")
            job["status"] = "deferred"
        elif job["status"] == "ready" and source["input_status"] == "choice_required" and not job.get("input_chosen", False):
            job.update(status="needs_choice", input=None)
        elif job["status"] in {"needs_identity", "needs_input", "needs_choice", "waiting"}:
            candidate_status = _initial_status(source)
            if job["status"] != "waiting" or candidate_status == "ready":
                job["status"] = candidate_status
                job["input"] = _input(source["inputs"][0]) if len(source["inputs"]) == 1 else None
        updated.append(job)
    kept = {j["id"] for j in updated}
    for job in state["jobs"]:
        require(job["id"] in kept or job["attempt"] == 0,
                "Cannot merge an already collected source implicitly; reconcile its results explicitly")
    state["jobs"] = updated


def _reconcile_source_jobs(state, previous_sources):
    """Refining identity changes source IDs, never the existing client's choices."""
    current_ids = {s["id"] for s in state["sources"]}
    previous = {s["id"]: s for s in previous_sources}
    for job in list(state["jobs"]):
        if job["source_id"] in current_ids:
            continue
        old_source = previous[job["source_id"]]
        replacements = [s for s in state["sources"]
                        if set(s["observation_ids"]) & set(old_source["observation_ids"])]
        require(len(replacements) == 1, "Identity refinement must resolve to one source")
        replacement = replacements[0]
        existing = next((j for j in state["jobs"] if j["source_id"] == replacement["id"]), None)
        if existing is None:
            job["source_id"] = replacement["id"]
        else:
            require(job["attempt"] == 0, "Cannot implicitly merge an already attempted job")
            state["jobs"].remove(job)


def create_migration(config):
    require(isinstance(config, dict), "Config must be an object")
    coordinator = config.get("coordinator")
    _text(coordinator, "coordinator")
    actors = config.get("actors", [coordinator])
    require(isinstance(actors, list) and actors and all(isinstance(a, str) and a.strip() for a in actors), "Invalid actors")
    require(len(set(actors)) == len(actors) and coordinator in actors, "Coordinator must be a registered actor")
    answers = copy.deepcopy(config.get("known_answers", {}))
    _answers(answers)
    state = {"format": "portable-memory-migration", "schema_version": 1,
             "id": config.get("id") or str(uuid.uuid4()), "revision": 0,
             "coordinator": coordinator, "actors": actors, "answers": answers,
             "sources": normalize_sources(config.get("sources", [])), "jobs": [],
             "shown_questions": [], "events": [], "created_at": now()}
    _sync_jobs(state)
    validate_migration(state)
    return state


def validate_migration(state):
    require(isinstance(state, dict) and state.get("format") == "portable-memory-migration" and
            type(state.get("schema_version")) is int and state["schema_version"] == 1, "Unsupported migration")
    _text(state.get("id"), "migration.id")
    require(isinstance(state.get("actors"), list) and state.get("coordinator") in state["actors"], "Invalid coordinator")
    require(all(isinstance(x, str) and x for x in state["actors"]) and len(set(state["actors"])) == len(state["actors"]), "Invalid actors")
    _answers(state.get("answers"))
    require(isinstance(state.get("sources"), list) and isinstance(state.get("jobs"), list), "Missing sources or jobs")
    source_ids = []
    observations = []
    for source in state["sources"]:
        validate_source(source)
        source_ids.append(source["id"])
        observations.extend(source["observations"])
    require(len(set(source_ids)) == len(source_ids), "Duplicate source ids")
    require(normalize_sources(observations) == state["sources"], "Source inventory is inconsistent")
    require(len(state["jobs"]) == len(source_ids), "Every source must have one job")
    jobs_seen, job_sources = set(), set()
    for job in state["jobs"]:
        require(isinstance(job, dict), "Invalid job")
        _text(job.get("id"), "job.id")
        require(job["id"] not in jobs_seen and job.get("source_id") in source_ids and job["source_id"] not in job_sources, "Duplicate or unknown job source")
        jobs_seen.add(job["id"]); job_sources.add(job["source_id"])
        require(job.get("owner") in state["actors"] and job.get("status") in STATUSES, "Invalid job owner or state")
        require(type(job.get("attempt")) is int and job["attempt"] >= 0 and isinstance(job.get("history"), list), "Invalid job attempts")
        source = next(s for s in state["sources"] if s["id"] == job["source_id"])
        if job["input"] is not None:
            require(job["input"] in [_input(x) for x in source["inputs"]], "Job input not in its source inventory")
        if job["status"] in {"ready", "running", "collected"}:
            require(source["identity_status"] == "verified" and source["selected"] and job["input"] is not None, "Job lacks verified selected input")
        if job["status"] == "running":
            _text(job.get("run_token"), "run_token")
        else:
            require(job.get("run_token") is None, "Only running jobs may hold a claim")
        if job["status"] == "collected":
            require(isinstance(job.get("result"), dict), "Collected job requires a result")
    require(isinstance(state.get("events"), list) and type(state.get("revision")) is int and state["revision"] == len(state["events"]), "Invalid event revision")
    require(len({e["id"] for e in state["events"]}) == len(state["events"]), "Duplicate event ids")
    require(isinstance(state.get("shown_questions"), list) and all(isinstance(x, str) for x in state["shown_questions"]), "Invalid question ledger")
    canonical(state)


def _job(state, job_id):
    matches = [j for j in state["jobs"] if j["id"] == job_id]
    require(len(matches) == 1, "Unknown job")
    return matches[0]


def _source(state, source_id):
    matches = [s for s in state["sources"] if s["id"] == source_id]
    require(len(matches) == 1, "Unknown source")
    return matches[0]


def _record_event(state, event):
    state["events"].append({"id": event["id"], "sha256": digest(event),
                            "actor": event["actor"], "type": event["type"], "at": now()})
    state["revision"] += 1


def _reset(job, status):
    if job.get("result") is not None or job.get("error") is not None:
        job["history"].append({"attempt": job["attempt"], "result": job["result"], "error": job["error"]})
    job.update(status=status, result=None, error=None, run_token=None, reason=None)


def apply_migration_event(state, event, *, expected_revision):
    validate_migration(state)
    require(isinstance(event, dict) and set(event) == {"id", "actor", "type", "data"}, "Invalid event envelope")
    _text(event["id"], "event.id")
    require(event["actor"] in state["actors"] and isinstance(event["data"], dict), "Unknown actor or event data")
    for previous in state["events"]:
        if previous["id"] == event["id"]:
            require(previous["sha256"] == digest(event), "Event id reused for different content")
            return copy.deepcopy(state)
    require(type(expected_revision) is int and expected_revision == state["revision"], "Stale migration; reload the current store")
    updated = copy.deepcopy(state)
    actor, kind, data = event["actor"], event["type"], event["data"]
    if kind in {"answer", "register_sources", "resolve_identity", "choose_input", "provide_input", "select_source", "question_shown", "journey_shown"}:
        require(actor == state["coordinator"], "The coordinator records client choices and the shared inventory")
    if kind == "answer":
        require(data.get("key") in ANSWER_KEYS and "value" in data, "Invalid answer")
        updated["answers"][data["key"]] = copy.deepcopy(data["value"])
        _answers(updated["answers"])
    elif kind == "register_sources":
        previous_sources = updated["sources"]
        observations = [o for s in updated["sources"] for o in s["observations"]]
        require(isinstance(data.get("observations"), list), "observations required")
        updated["sources"] = normalize_sources(observations + data["observations"])
        # Discovering another surface is not a new client selection. Preserve an
        # existing selection until an explicit select_source event changes it.
        preserved = []
        for source in updated["sources"]:
            prior = next((s for s in previous_sources if s["id"] == source["id"]), None)
            if prior is None:
                matches = [s for s in previous_sources if set(s["observation_ids"]) & set(source["observation_ids"])]
                selected = all(s["selected"] for s in matches) if matches else source["selected"]
            else:
                selected = prior["selected"]
            for observation in source["observations"]:
                preserved.append({**observation, "selected": selected})
        updated["sources"] = normalize_sources(preserved)
        _reconcile_source_jobs(updated, previous_sources)
        _sync_jobs(updated)
    elif kind == "resolve_identity":
        previous_sources = updated["sources"]
        updated["sources"] = resolve_identity(updated["sources"], data.get("source_id"), data.get("identity"), data.get("evidence"))
        _reconcile_source_jobs(updated, previous_sources)
        _sync_jobs(updated)
    elif kind in {"choose_input", "provide_input"}:
        source = _source(updated, data.get("source_id"))
        job = next(j for j in updated["jobs"] if j["source_id"] == source["id"])
        require(job["status"] != "running", "Stop or finish the current owner before changing its input")
        require(source["identity_status"] == "verified" and source["selected"], "Resolve and select the source first")
        if kind == "provide_input":
            _text(data.get("evidence"), "input evidence")
            observation = copy.deepcopy(source["observations"][0])
            observation.update(id="input-" + digest(event["id"])[:24], input=copy.deepcopy(data.get("input")))
            observation["identity"] = copy.deepcopy(source["identity"])
            observation["identity_evidence"] = "; ".join(x["evidence"] for x in source["identity_evidence"])
            observation["selected"] = True
            require(observation["input"] is not None, "A provided input is required")
            all_obs = [o for s in updated["sources"] for o in s["observations"]] + [observation]
            updated["sources"] = normalize_sources(all_obs)
            source = _source(updated, source["id"])
            chosen = next(x for x in source["inputs"] if observation["id"] in x["observation_ids"])
        else:
            index = data.get("index")
            require(type(index) is int and 0 <= index < len(source["inputs"]), "Invalid input index")
            chosen = source["inputs"][index]
        new_input = _input(chosen)
        changed_snapshot = False
        if job["status"] == "collected" and new_input == job["input"]:
            from .collection import fingerprint_input
            try:
                changed_snapshot = fingerprint_input(new_input) != job["result"].get("input_sha256")
            except (ValidationError, OSError):
                changed_snapshot = True
        if new_input != job["input"] or changed_snapshot or job["status"] not in {"collected", "ready"}:
            job["input"] = new_input
            _reset(job, "ready")
        job["input_chosen"] = True
    elif kind == "select_source":
        source = _source(updated, data.get("source_id"))
        require(type(data.get("selected")) is bool, "selected must be boolean")
        observations = [copy.deepcopy(o) for s in updated["sources"] for o in s["observations"]]
        for observation in observations:
            if observation["id"] in source["observation_ids"]:
                observation["selected"] = data["selected"]
        updated["sources"] = normalize_sources(observations)
        _sync_jobs(updated)
        if data["selected"]:
            job = next(j for j in updated["jobs"] if j["source_id"] == source["id"])
            if job["status"] == "deferred":
                resolved = _source(updated, source["id"])
                job["status"] = "collected" if job["result"] else (
                    "ready" if job["input"] and resolved["identity_status"] == "verified" and job.get("input_chosen")
                    else _initial_status(resolved))
    elif kind in {"assign", "defer", "wait", "retry", "release"}:
        job = _job(updated, data.get("job_id"))
        require(actor in {updated["coordinator"], job["owner"]}, "Only coordinator or job owner may change this job")
        if kind == "assign":
            require(job["status"] != "running", "A running job must finish or be explicitly released before handoff")
            require(data.get("owner") in state["actors"], "Unknown new owner")
            job["owner"] = data["owner"]
        elif kind in {"defer", "wait"}:
            require(job["status"] != "running", "Stop the running operation before deferring it")
            _text(data.get("reason"), "reason")
            job["status"] = "deferred" if kind == "defer" else "waiting"
            job["reason"] = data["reason"]
        elif kind == "release":
            require(job["status"] == "running", "No running claim to release")
            _text(data.get("reason"), "Confirm why the previous worker has stopped")
            _reset(job, "failed")
            job["error"] = "Previous worker explicitly released: " + data["reason"]
        else:
            require(job["status"] in {"failed", "deferred", "waiting", "collected"}, "Job is not retryable")
            source = _source(updated, job["source_id"])
            require(source["selected"], "Select the source before retrying")
            if not job.get("input_chosen"):
                job["input"] = _input(source["inputs"][0]) if len(source["inputs"]) == 1 else None
            _reset(job, "ready" if job["input"] and source["identity_status"] == "verified" else _initial_status(source))
    elif kind == "journey_shown":
        from .guidance import journey_view
        view = journey_view(state)
        items = list(view["notices"])
        if view["question"]:
            items.append(view["question"])
        item_id = data.get("item_id")
        require(any(item["id"] == item_id for item in items), "Journey item is stale or already shown")
        updated["shown_questions"].append(item_id)
    elif kind == "question_shown":
        question = migration_view(state)["question"]
        require(question is not None and data.get("question_id") == question["id"], "Question is stale or already shown")
        updated["shown_questions"].append(question["id"])
    else:
        raise ValidationError("Unsupported migration event")
    _record_event(updated, event)
    validate_migration(updated)
    return updated


def _questions(state):
    if "scope" not in state["answers"]:
        return [{"id": "scope", "title": "Что хотите сохранить в новом агенте?",
                 "why": "Выберем нужный объём и не будем собирать лишнее.",
                 "options": ["Профиль и правила", "Профиль, важные работы и навыки", "Добавить исходную историю"]}]
    if not state["sources"]:
        return [{"id": "sources", "title": "Из каких сервисов или проектов переносим данные?",
                 "why": "Составлю карту источников; приложения с общей историей учтём один раз.",
                 "options": ["ChatGPT и Claude", "Добавить проекты Claude Code и Codex"]}]
    selected = {s["id"]: s for s in state["sources"] if s["selected"]}
    questions = []
    for job in state["jobs"]:
        source = selected.get(job["source_id"])
        if source is None:
            continue
        label = source["label"]
        if job["status"] == "needs_identity":
            questions.append({"id": "identity:" + source["id"],
                              "title": f"Уточним, к какому аккаунту и пространству относится «{label}»?",
                              "why": "Это позволит распознать повторный источник и сохранить личное и рабочее отдельно.",
                              "options": ["Уточнить источник", "Отложить этот источник"]})
        elif job["status"] == "needs_choice":
            questions.append({"id": "input:" + source["id"] + ":" + digest(source["inputs"])[:12],
                              "title": f"Для «{label}» есть несколько файлов. Какой используем?",
                              "why": "Сохраним выбранную версию и не обработаем один набор дважды.",
                              "options": [str(Path(x["path"]).name) for x in source["inputs"]] + ["Выбрать позже"]})
    missing = [j for j in state["jobs"] if j["source_id"] in selected and j["status"] == "needs_input"]
    if missing:
        labels = ", ".join(selected[j["source_id"]]["label"] for j in missing)
        questions.append({"id": "files:" + digest(sorted(j["id"] for j in missing))[:16],
                          "title": f"Материалы для {labels} уже готовы или ещё ожидаются?",
                          "why": "Готовое обработаем сейчас; остальные источники смогут подождать.",
                          "options": ["Передать готовые файлы", "Архивы ещё готовятся", "Займёмся ими позже"]})
    if not questions and any(j["status"] == "collected" for j in state["jobs"]) and "destination" not in state["answers"]:
        questions.append({"id": "destination", "title": "Как удобнее передать проверенную память получателю?",
                          "why": "Материалы уже собраны; теперь подготовим подходящий способ передачи.",
                          "options": ["Файлом в чат", "Через выбранную папку"]})
    if (not questions and any(j["status"] == "collected" for j in state["jobs"]) and
            any(j["status"] in {"waiting", "needs_input", "failed", "running"} for j in state["jobs"]) and
            "delivery" not in state["answers"]):
        questions.append({"id": "delivery", "title": "Передать готовую часть после проверки или дождаться остальных?",
                          "why": "Можно начать пользоваться подтверждённым профилем раньше.",
                          "options": ["Готовое — по мере проверки", "Дождаться всех выбранных источников"]})
    return questions


def migration_view(state, actor=None):
    validate_migration(state)
    actor = actor or state["coordinator"]
    require(actor in state["actors"], "Unknown actor")
    selected_ids = {s["id"] for s in state["sources"] if s["selected"]}
    selected_jobs = [j for j in state["jobs"] if j["source_id"] in selected_ids]
    counts = {status: sum(j["status"] == status for j in selected_jobs) for status in sorted(STATUSES)}
    ready = [j["id"] for j in selected_jobs if j["status"] == "ready" and j["owner"] == actor]
    missing_outputs = [j["id"] for j in selected_jobs if j["status"] == "collected" and
                       not Path(j["result"].get("output", "__missing__")).is_dir()]
    questions = _questions(state) if actor == state["coordinator"] else []
    pending = next((q for q in questions if q["id"] in state["shown_questions"]), None)
    question = None if pending else (questions[0] if questions else None)
    summary = f"Собрано источников: {counts['collected']} из {len(selected_jobs)}."
    if counts["running"]:
        summary += f" Обрабатываются: {counts['running']}."
    if counts["waiting"]:
        summary += f" Ожидаются: {counts['waiting']}."
    if counts["deferred"]:
        summary += f" Отложены вами: {counts['deferred']}."
    if counts["failed"]:
        summary += f" Нужна помощь с получением: {counts['failed']}."
    summary += " Собранные данные ещё нужно проверить и согласовать перед переносом."
    return {"migration_id": state["id"], "revision": state["revision"], "summary": summary,
            "counts": counts, "sources": [{"id": s["id"], "label": s["label"], "kind": s["kind"],
                                            "selected": s["selected"], "identity_status": s["identity_status"],
                                            "surfaces": s["surfaces"], "observation_count": len(s["observation_ids"])} for s in state["sources"]],
            "jobs": copy.deepcopy(state["jobs"]), "question": question, "awaiting_answer": pending,
            "automatic_actions": ([{"action": "collect", "job_ids": ready}] if ready and "scope" in state["answers"] else []),
            "missing_outputs": missing_outputs,
            "limitations": ["Collection is not native-memory import.",
                            "Only workers using this local store are coordinated; separate SaaS snapshots are not synchronized."]}


def render_migration_view(view):
    labels = {s["id"]: s["label"] for s in view["sources"]}
    names = {"needs_identity": "нужно уточнить источник", "needs_input": "нужен файл",
             "needs_choice": "нужно выбрать версию", "ready": "можно обработать", "running": "обрабатывается",
             "waiting": "ожидаем", "deferred": "отложено", "collected": "собрано, ждёт проверки", "failed": "нужно повторить получение"}
    lines = [view["summary"], ""]
    for job in view["jobs"]:
        lines.append(f"- {labels[job['source_id']]} — {names[job['status']]}")
    if view["automatic_actions"]:
        lines += ["", "Могу продолжить обработку уже доступных материалов."]
    if view["question"]:
        lines += ["", view["question"]["title"], view["question"]["why"]]
    elif view["awaiting_answer"]:
        lines += ["", "Предыдущий вопрос сохранён; повторно отвечать на уже согласованное не нужно."]
    if view["missing_outputs"]:
        lines += ["", "Часть ранее собранных файлов недоступна в этой среде; их нужно передать вместе с состоянием."]
    return "\n".join(lines) + "\n"


class MigrationStore:
    """Transactional coordination on one filesystem. Do not share a live DB via sync drives."""
    def __init__(self, path):
        self.path = Path(path)
        require(self.path.is_file(), "Migration store not found")

    @classmethod
    def create(cls, path, state):
        validate_migration(state)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb"):
            pass
        with closing(sqlite3.connect(path, timeout=15)) as connection:
            with connection:
                connection.execute("CREATE TABLE migration (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
                connection.execute("INSERT INTO migration VALUES(1, ?)", (canonical(state).decode("utf-8"),))
        return cls(path)

    def _transaction(self, transform=None):
        with closing(sqlite3.connect(self.path, timeout=15)) as connection:
            with connection:
                if transform:
                    connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT body FROM migration WHERE id=1").fetchone()
                require(row is not None, "Invalid migration database")
                state = json.loads(row[0])
                validate_migration(state)
                if transform:
                    state = transform(state)
                    validate_migration(state)
                    connection.execute("UPDATE migration SET body=? WHERE id=1", (canonical(state).decode("utf-8"),))
                return state

    def load(self):
        return self._transaction()

    def event(self, event, *, expected_revision):
        return self._transaction(lambda state: apply_migration_event(state, event, expected_revision=expected_revision))

    def claim_ready(self, actor, *, limit=4):
        from .collection import fingerprint_input
        require(type(limit) is int and 1 <= limit <= 8, "Worker limit must be 1..8")
        claimed = []
        def claim(state):
            require(actor in state["actors"], "Unknown actor")
            require("scope" in state["answers"], "Agree the desired scope before collection")
            attempted = []
            for job in state["jobs"]:
                if job["status"] == "ready" and job["owner"] == actor and len(attempted) < limit:
                    attempted.append(job["id"])
                    try:
                        snapshot_hash = fingerprint_input(job["input"])
                    except (ValidationError, OSError) as exc:
                        job.update(status="failed", error=str(exc), attempt=job["attempt"] + 1)
                        continue
                    job.update(status="running", attempt=job["attempt"] + 1, run_token=str(uuid.uuid4()))
                    job["input_sha256"] = snapshot_hash
                    claimed.append(copy.deepcopy(job))
            if attempted:
                _record_event(state, {"id": str(uuid.uuid4()), "actor": actor, "type": "jobs_claimed",
                                      "data": {"jobs": [j["id"] for j in claimed], "examined": attempted}})
            return state
        self._transaction(claim)
        return claimed

    def finish(self, claim, outcome):
        def finish_job(state):
            job = _job(state, claim["id"])
            require(job["status"] == "running" and job["run_token"] == claim["run_token"] and job["owner"] == claim["owner"],
                    "Collection claim is no longer current")
            require(isinstance(outcome, dict) and type(outcome.get("ok")) is bool, "Invalid outcome")
            require(outcome.get("job_id") == job["id"], "Outcome belongs to another job")
            if outcome["ok"]:
                result = outcome.get("result")
                require(isinstance(result, dict) and Path(result.get("output", "__missing__")).is_dir(), "Collected output unavailable")
                output = Path(result["output"])
                require(output.parent.name == job["id"] and output.name == f"attempt-{job['attempt']:04d}", "Outcome directory belongs to another attempt")
                require(not (output / ".incomplete").exists(), "Collection did not finish writing its output")
                require(result.get("kind") == job["input"]["kind"] and result.get("input_sha256") == job.get("input_sha256"), "Outcome does not match the claimed snapshot")
                job.update(status="collected", result=copy.deepcopy(result), error=None, run_token=None)
            else:
                job.update(status="failed", result=None, error=str(outcome.get("error", "Collection failed")), run_token=None)
            _record_event(state, {"id": "finish-" + claim["run_token"], "actor": claim["owner"],
                                  "type": "collection_finished", "data": {"job_id": job["id"], "ok": outcome["ok"]}})
            return state
        return self._transaction(finish_job)


def collect_migration(store, output_root, *, actor=None, max_workers=4):
    from .collection import collect_many, fingerprint_input
    actor = actor or store.load()["coordinator"]
    outcomes = []
    state = store.load()
    # Compare only already selected, explicitly supplied inputs. No account scans.
    for job in state["jobs"]:
        if job["owner"] == actor and job["status"] == "collected":
            try:
                changed = fingerprint_input(job["input"]) != job["result"].get("input_sha256")
            except (ValidationError, OSError):
                changed = False  # The existing collected snapshot may still be usable.
            if changed:
                latest = store.load()
                store.event({"id": str(uuid.uuid4()), "actor": actor, "type": "retry", "data": {"job_id": job["id"]}}, expected_revision=latest["revision"])
    # Identical supplied copies need no extra client question. Versions that differ do.
    if actor == state["coordinator"]:
        for source in store.load()["sources"]:
            job = next(j for j in store.load()["jobs"] if j["source_id"] == source["id"])
            if job["status"] != "needs_choice":
                continue
            try:
                hashes = {(item["kind"], fingerprint_input(_input(item))) for item in source["inputs"]}
            except (ValidationError, OSError):
                continue
            if len(hashes) == 1:
                latest = store.load()
                store.event({"id": str(uuid.uuid4()), "actor": actor, "type": "choose_input",
                             "data": {"source_id": source["id"], "index": 0, "reason": "Provided copies have identical content"}}, expected_revision=latest["revision"])
    # Drain runnable jobs in bounded batches; waiting/deferred jobs never block.
    while True:
        claims = store.claim_ready(actor, limit=max_workers)
        if not claims:
            if any(j["status"] == "ready" and j["owner"] == actor for j in store.load()["jobs"]):
                continue
            break
        by_id = {claim["id"]: claim for claim in claims}
        def completed(outcome):
            store.finish(by_id[outcome["job_id"]], outcome)
        batch = collect_many(claims, output_root, max_workers=max_workers, on_result=completed)
        outcomes.extend(batch)
    return {"outcomes": outcomes, "view": migration_view(store.load(), actor)}
