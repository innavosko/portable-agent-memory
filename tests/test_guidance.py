"""Observable journey behavior and persistence; only fictional sources."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from portable_memory.errors import ValidationError
from portable_memory.guidance import journey_view
from portable_memory.migration import MigrationStore, apply_migration_event, create_migration


def source(oid="web", resource="history", interface="web", verified=True):
    return {"id": oid, "label": resource, "kind": "history",
            "identity": {"namespace": "test", "account_id": "fictional", "workspace_id": "personal", "resource_id": resource} if verified else None,
            "identity_evidence": "Fictional identity confirmed in test" if verified else None,
            "surface": {"agent_id": "agent", "product": "ChatGPT", "interface": interface},
            "selected": True, "input": None}


class GuidedJourneyTests(unittest.TestCase):
    def state(self, answers=None, sources=None):
        return create_migration({"coordinator": "main", "actors": ["main", "source-agent"],
                                 "known_answers": answers or {}, "sources": sources if sources is not None else [source()]})

    def event(self, state, kind, data, actor="main"):
        return apply_migration_event(state, {"id": str(uuid.uuid4()), "actor": actor, "type": kind, "data": data}, expected_revision=state["revision"])

    def answer(self, state, key, value):
        return self.event(state, "answer", {"key": key, "value": value})

    def test_same_history_web_and_desktop_is_one_source(self):
        view = journey_view(self.state(sources=[source(), source("desktop", interface="desktop")]))
        self.assertEqual(view["role"], "single_source")
        self.assertEqual(len(view["sources"]), 1)

    def test_multiple_sources_and_unknown_identity_have_distinct_roles(self):
        self.assertEqual(journey_view(self.state(sources=[source(), source("work", "work-history")]))["role"], "coordinator")
        self.assertEqual(journey_view(self.state(sources=[source(verified=False)]))["role"], "unresolved")
        self.assertEqual(journey_view(self.state(sources=[]))["role"], "unresolved")

    def test_view_does_not_claim_shown_or_approve_any_action(self):
        state = self.state()
        before = copy.deepcopy(state)
        view = journey_view(state)
        self.assertEqual(state, before)
        self.assertFalse(view["export_choices_ready"])
        self.assertFalse(view["native_memory_applied"])
        self.assertEqual(view["question"]["answer_key"], "scope")
        self.assertEqual({n["kind"] for n in view["notices"]}, {"role", "start_offer"})

    def test_role_notice_reappears_only_when_source_map_changes(self):
        state = self.state({"scope": ["profile"]})
        role = journey_view(state)["notices"][0]
        state = self.event(state, "journey_shown", {"item_id": role["id"]})
        self.assertFalse(any(n["kind"] == "role" for n in journey_view(state)["notices"]))
        state = self.event(state, "register_sources", {"observations": [source("work", "work-history")]})
        self.assertEqual(journey_view(state)["role"], "coordinator")
        self.assertTrue(any(n["kind"] == "role" for n in journey_view(state)["notices"]))
        self.assertEqual(state["answers"]["scope"], ["profile"])

    def test_offer_decline_and_obsidian_choice_survive_resume(self):
        state = self.state({"scope": ["profile", "projects"], "recipient": "gigaagent"})
        self.assertEqual(journey_view(state)["question"]["answer_key"], "skill_packaging")
        state = self.answer(state, "skill_packaging", "decline")
        self.assertEqual(journey_view(state)["question"]["answer_key"], "obsidian")
        state = self.answer(state, "obsidian", {"choice": "skip"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            MigrationStore.create(path, state)
            restored = MigrationStore(path).load()
        view = journey_view(restored)
        self.assertEqual(view["question"]["answer_key"], "destination")
        self.assertEqual(view["known_choices"]["skill_packaging"], "decline")

    def test_explicit_profile_only_does_not_expand_scope_to_skills(self):
        state = self.state({"scope": ["profile"], "recipient": "gigaagent"})
        self.assertEqual(journey_view(state)["question"]["answer_key"], "obsidian")

    def test_pending_question_not_asked_again_or_replaced_with_another(self):
        state = self.state({"scope": ["profile", "projects"], "recipient": "claude"})
        q = journey_view(state)["question"]
        state = self.event(state, "journey_shown", {"item_id": q["id"]})
        view = journey_view(state)
        self.assertIsNone(view["question"])
        self.assertEqual(view["awaiting_answer"]["id"], q["id"])
        state = self.answer(state, "skill_packaging", "later")
        self.assertEqual(journey_view(state)["question"]["answer_key"], "obsidian")

    def test_gigaagent_requires_compatible_format_after_target_change(self):
        answers = {"scope": ["profile"], "recipient": "gigaagent_pro", "obsidian": {"choice": "skip"},
                   "destination": {"kind": "upload", "label": "Download"}, "save_format": "portable_bundle"}
        state = self.state(answers)
        self.assertTrue(journey_view(state)["export_choices_ready"])
        state = self.answer(state, "recipient", "gigaagent")
        view = journey_view(state)
        self.assertFalse(view["export_choices_ready"])
        self.assertEqual(view["question"]["answer_key"], "save_format")
        self.assertEqual([o["value"] for o in view["question"]["options"]], ["markdown"])
        state = self.answer(state, "save_format", "markdown")
        self.assertTrue(journey_view(state)["export_choices_ready"])
        self.assertTrue(journey_view(state)["semantic_review_required"])

    def test_only_coordinator_records_current_notices_and_answers(self):
        state = self.state()
        item = journey_view(state)["notices"][0]["id"]
        with self.assertRaises(ValidationError):
            self.event(state, "journey_shown", {"item_id": item}, actor="source-agent")
        with self.assertRaises(ValidationError):
            self.event(state, "journey_shown", {"item_id": "invented"})
        self.assertEqual(journey_view(state, "source-agent")["notices"], [])
        self.assertIsNone(journey_view(state, "source-agent")["question"])

    def test_invalid_choices_are_rejected(self):
        for key, value in [("recipient", "madeup"), ("obsidian", {"choice": "automatic_connection"}),
                           ("save_format", "install_everything"), ("skill_packaging", "auto")]:
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.answer(self.state(), key, value)

    def test_cli_shows_guided_role_and_one_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.sqlite"
            MigrationStore.create(path, self.state())
            result = subprocess.run([sys.executable, "-B", "-m", "portable_memory", "journey-view", "--store", str(path)],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        view = json.loads(result.stdout)
        self.assertEqual(view["role"], "single_source")
        self.assertEqual(view["question"]["answer_key"], "scope")

    def test_desktop_can_have_account_local_and_cloud_sources_without_inferred_access(self):
        account = source("desktop", interface="desktop")
        account["locations"] = [{"kind": "account", "label": "History in account", "access": "unknown"}]
        local = source("project-local", "local-project", "desktop")
        local["locations"] = [{"kind": "local", "label": "Provided project folder", "access": "available", "evidence": "Fictional file read"}]
        cloud = source("project-cloud", "cloud-project", "desktop")
        cloud["locations"] = [{"kind": "cloud", "label": "User-named cloud folder", "access": "unknown"}]
        state = self.state({"scope": ["profile"], "recipient": "gigaagent"}, [account, local, cloud])
        view = journey_view(state)
        self.assertEqual(len(view["sources"]), 3)
        self.assertEqual({i["locations"][0]["kind"] for i in view["source_locations"]}, {"account", "local", "cloud"})
        self.assertEqual(view["question"]["answer_key"], "obsidian")
        self.assertTrue(all(not s["collection_ready"] for s in state["sources"]))

    def test_location_choice_is_explicit_and_does_not_duplicate_known_history(self):
        state = self.state({"scope": ["profile"]}, [source(), source("desktop", interface="desktop")])
        q = journey_view(state)["question"]
        self.assertEqual(q["answer_key"], "source_locations")
        state = self.event(state, "journey_shown", {"item_id": q["id"]})
        obs = source("desktop", interface="desktop")
        obs["locations"] = [{"kind": "account", "label": "Only shared account history", "access": "unknown"}]
        state = self.event(state, "register_sources", {"observations": [obs]})
        self.assertEqual(len(state["sources"]), 1)
        self.assertEqual(journey_view(state)["question"]["answer_key"], "recipient")

    def test_source_location_known_access_requires_evidence(self):
        obs = source()
        obs["locations"] = [{"kind": "cloud", "label": "Some folder", "access": "available"}]
        with self.assertRaises(ValidationError):
            self.state(sources=[obs])

    def test_declined_obsidian_is_not_offered_again_as_output_format(self):
        state = self.state({"scope": ["profile"], "recipient": "gigaagent_pro", "obsidian": {"choice": "skip"},
                            "destination": {"kind": "upload", "label": "Download"}})
        question = journey_view(state)["question"]
        self.assertEqual(question["answer_key"], "save_format")
        self.assertNotIn("obsidian_vault", [item["value"] for item in question["options"]])


if __name__ == "__main__":
    unittest.main()
