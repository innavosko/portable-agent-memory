"""Exercise the complete multi-source journey using only fictional files."""
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from portable_memory.migration import (MigrationStore, collect_migration,
                                       create_migration, migration_view,
                                       render_migration_view)
from portable_memory.model import empty_memory

ROOT = Path(__file__).resolve().parents[1]


class MigrationJourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.history = self.root / "history.json"
        self.history.write_text(json.dumps([{
            "id": "fictional-chat", "title": "Fictional project", "current_node": "m1",
            "mapping": {"m1": {"parent": None, "message": {
                "id": "m1", "author": {"role": "user"},
                "content": {"parts": ["Keep the project summary concise."]}}}}}]), encoding="utf-8")
        self.profile = self.root / "profile.json"
        self.profile.write_bytes((ROOT / "examples" / "profile.json").read_bytes())
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "overview.md").write_text("# Fictional project\nA reusable meeting summary.\n", encoding="utf-8")
        (self.project / ".env").write_text("TEST_ONLY=excluded\n", encoding="utf-8")

    def observation(self, oid, dataset, *, product="ChatGPT", interface="web", input=None):
        return {"id": oid, "label": dataset, "kind": dataset,
                "identity": {"namespace": "fictional-provider", "account_id": "fictional-account",
                             "workspace_id": "fictional-workspace", "resource_id": dataset},
                "identity_evidence": "Confirmed in this synthetic test fixture only",
                "surface": {"agent_id": product, "product": product, "interface": interface},
                "selected": True, "input": input}

    def config(self):
        archive = {"kind": "archive", "path": str(self.history)}
        folder = {"kind": "folder", "path": str(self.project)}
        return {"coordinator": "export-agent", "actors": ["export-agent", "import-agent"],
                "known_answers": {"scope": ["profile", "projects", "history"],
                                  "destination": {"kind": "upload", "label": "Chosen receiving chat"},
                                  "delivery": "as_ready"},
                "sources": [
                    self.observation("chat-web", "chat-history", input=archive),
                    self.observation("chat-desktop", "chat-history", interface="desktop", input=archive),
                    self.observation("claude-web", "claude-history", product="Claude"),
                    self.observation("claude-desktop", "claude-history", product="Claude", interface="desktop"),
                    self.observation("code", "project", product="Claude Code", interface="cli", input=folder),
                    self.observation("codex", "project", product="Codex", interface="desktop", input=folder),
                    self.observation("profile", "profile", input={"kind": "memory", "path": str(self.profile)})]}

    def store(self, config=None):
        return MigrationStore.create(self.root / "migration.sqlite", create_migration(config or self.config()))

    def event(self, store, kind, data):
        return store.event({"id": str(uuid.uuid4()), "actor": "export-agent", "type": kind, "data": data},
                           expected_revision=store.load()["revision"])

    @staticmethod
    def job(store, kind):
        state = store.load()
        source = next(s for s in state["sources"] if s["kind"] == kind)
        return next(j for j in state["jobs"] if j["source_id"] == source["id"])

    def collect(self, store):
        return collect_migration(store, self.root / "collected", max_workers=3)

    def test_one_journey_collects_ready_sources_then_only_late_archive(self):
        store = self.store()
        self.assertEqual(len(store.load()["jobs"]), 4)
        waiting = self.job(store, "claude-history")
        self.event(store, "wait", {"job_id": waiting["id"], "reason": "Archive requested; still preparing"})
        first = self.collect(store)
        self.assertEqual(len(first["outcomes"]), 3)
        self.assertTrue(all(o["ok"] for o in first["outcomes"]))
        self.assertEqual(first["view"]["counts"]["waiting"], 1)
        self.assertEqual(first["view"]["counts"]["collected"], 3)
        self.assertIsNone(first["view"]["question"])
        self.assertIn("Собрано источников: 3 из 4", render_migration_view(first["view"]))
        completed = {j["id"]: j["result"] for j in store.load()["jobs"] if j["result"]}
        self.assertEqual(self.collect(store)["outcomes"], [])
        self.event(store, "provide_input", {"source_id": waiting["source_id"],
                   "input": {"kind": "archive", "path": str(self.history)}, "evidence": "Client supplied this file for the waiting dataset"})
        late = self.collect(store)
        self.assertEqual([o["job_id"] for o in late["outcomes"]], [waiting["id"]])
        self.assertEqual(late["view"]["counts"]["collected"], 4)
        for job in store.load()["jobs"]:
            self.assertEqual(job["attempt"], 1)
            if job["id"] in completed:
                self.assertEqual(job["result"], completed[job["id"]])

    def test_collection_preserves_review_and_filters_project_credentials(self):
        store = self.store()
        self.collect(store)
        profile = self.job(store, "profile")["result"]
        self.assertEqual(Path(profile["report"]["data_path"]).read_bytes(), self.profile.read_bytes())
        self.assertFalse(profile["report"]["records_modified"])
        project = self.job(store, "project")["result"]
        files = [p.name for p in Path(project["report"]["data_path"]).iterdir()]
        self.assertEqual(files, ["overview.md"])
        self.assertFalse(project["report"]["native_memory_applied"])

    def test_changed_same_path_creates_new_attempt_without_overwriting_old(self):
        store = self.store()
        self.collect(store)
        old = copy.deepcopy(self.job(store, "profile"))
        self.profile.write_text(json.dumps(empty_memory()), encoding="utf-8")
        result = self.collect(store)
        self.assertEqual([o["job_id"] for o in result["outcomes"]], [old["id"]])
        new = self.job(store, "profile")
        self.assertEqual(new["attempt"], 2)
        self.assertEqual(new["history"][0]["result"], old["result"])
        self.assertTrue(Path(old["result"]["report"]["data_path"]).is_file())
        self.assertNotEqual(new["result"]["input_sha256"], old["result"]["input_sha256"])

    def test_identical_provided_copies_are_collected_once_without_extra_choice(self):
        other = self.root / "copy.json"
        shutil.copyfile(self.history, other)
        config = self.config()
        config["sources"][1]["input"]["path"] = str(other)
        store = self.store(config)
        # Both observations in config() shared their input; make truly different candidates.
        original = self.observation("additional-copy", "chat-history", input={"kind": "archive", "path": str(self.history)})
        self.event(store, "register_sources", {"observations": [original]})
        self.assertEqual(self.job(store, "chat-history")["status"], "needs_choice")
        result = self.collect(store)
        history = self.job(store, "chat-history")
        self.assertEqual(history["status"], "collected")
        self.assertEqual(history["attempt"], 1)
        self.assertEqual(sum(o["job_id"] == history["id"] for o in result["outcomes"]), 1)

    def test_missing_input_file_does_not_block_other_jobs(self):
        self.history.unlink()
        store = self.store()
        result = self.collect(store)
        self.assertEqual(result["view"]["counts"]["failed"], 1)
        self.assertEqual(result["view"]["counts"]["collected"], 2)
        self.assertEqual(result["view"]["counts"]["needs_input"], 1)
        self.assertEqual(self.collect(store)["outcomes"], [])

    def test_saved_question_is_not_repeated_and_deferred_source_stays_deferred(self):
        store = self.store()
        view = migration_view(store.load())
        self.assertTrue(view["question"]["id"].startswith("files:"))
        self.event(store, "question_shown", {"question_id": view["question"]["id"]})
        view = migration_view(store.load())
        self.assertIsNone(view["question"])
        self.assertIsNotNone(view["awaiting_answer"])
        waiting = self.job(store, "claude-history")
        self.event(store, "defer", {"job_id": waiting["id"], "reason": "Client wants to continue next week"})
        self.collect(store)
        resumed = MigrationStore(self.root / "migration.sqlite")
        self.assertEqual(self.collect(resumed)["outcomes"], [])
        self.assertIsNone(migration_view(resumed.load())["question"])
        self.assertEqual(self.job(resumed, "claude-history")["status"], "deferred")

    def test_snapshot_resume_preserves_answers_and_completed_jobs(self):
        store = self.store()
        self.collect(store)
        snapshot = json.loads(json.dumps(store.load()))
        resumed = MigrationStore.create(self.root / "resumed.sqlite", snapshot)
        self.assertEqual(self.collect(resumed)["outcomes"], [])
        self.assertEqual(resumed.load()["answers"], snapshot["answers"])
        job = self.job(resumed, "profile")
        Path(job["result"]["output"]).rename(self.root / "temporarily-unavailable")
        self.assertEqual(migration_view(resumed.load())["missing_outputs"], [job["id"]])

    def test_different_versions_remain_a_choice_and_do_not_block_other_sources(self):
        different = self.root / "other.json"
        different.write_text("[]", encoding="utf-8")
        config = self.config()
        config["sources"].append(self.observation("other-version", "chat-history",
                                                input={"kind": "archive", "path": str(different)}))
        store = self.store(config)
        result = self.collect(store)
        self.assertEqual(result["view"]["counts"]["needs_choice"], 1)
        self.assertEqual(result["view"]["counts"]["collected"], 2)
        self.assertTrue(result["view"]["question"]["id"].startswith("input:"))

    def test_cli_resolves_config_paths_and_resumes_exported_state(self):
        config = self.config()
        config["sources"] = [self.observation("profile", "profile", input={"kind": "memory", "path": "profile.json"})]
        cfg = self.root / "config.json"
        cfg.write_text(json.dumps(config), encoding="utf-8")
        db = self.root / "cli.sqlite"

        def cli(*args):
            result = subprocess.run([sys.executable, "-B", "-m", "portable_memory", *map(str, args)],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        created = json.loads(cli("migration-create", "--config", cfg, "--store", db))
        self.assertEqual(created["jobs"][0]["input"]["path"], str(self.profile.resolve()))
        collected = json.loads(cli("migration-collect", "--store", db, "--out", self.root / "cli-output"))
        self.assertEqual(collected["view"]["counts"]["collected"], 1)
        snapshot = self.root / "state.json"
        cli("migration-export-state", "--store", db, "--out", snapshot)
        second = self.root / "cli-resumed.sqlite"
        cli("migration-resume", "--state", snapshot, "--store", second)
        self.assertIn("Собрано источников: 1 из 1", cli("migration-view", "--store", second, "--format", "markdown"))
        self.assertEqual(json.loads(cli("migration-collect", "--store", second, "--out", self.root / "cli-output"))["outcomes"], [])


if __name__ == "__main__":
    unittest.main()
