import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from portable_memory.collection import collect_job, collect_many, fingerprint_input
from portable_memory.errors import ValidationError
from portable_memory.model import empty_memory


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.outputs = self.root / "outputs"

    def job(self, path, kind="folder", job_id="job-one", **input_options):
        return {"id": job_id, "source_id": "source-one", "attempt": 1,
                "input": {"kind": kind, "path": str(path), **input_options}}

    def write(self, relative, content="Example context"):
        target = self.sources / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def memory(self):
        memory = empty_memory()
        memory["records"] = [{"id": "profile-one", "kind": "profile", "scope": "personal",
                              "title": "Preferred language", "value": "Russian", "provenance": "user_statement",
                              "currency": "unknown", "sources": [{"id": "chat-one", "locator": "Provided excerpt", "observed_at": None}],
                              "links": [], "review": {"decision": "pending"}}]
        return self.write("profile.json", json.dumps(memory)), memory

    def test_workers_actually_overlap_and_results_keep_input_order(self):
        barrier = threading.Barrier(3, timeout=5)
        thread_ids, lock = set(), threading.Lock()

        def worker(job, output):
            with lock:
                thread_ids.add(threading.get_ident())
            barrier.wait()
            return {"seen": job["id"]}

        jobs = [{"id": f"job-{i}"} for i in range(3)]
        outcomes = collect_many(jobs, self.outputs, max_workers=3, worker=worker)
        self.assertTrue(all(item["ok"] for item in outcomes), outcomes)
        self.assertEqual(len(thread_ids), 3)
        self.assertEqual([item["job_id"] for item in outcomes], [job["id"] for job in jobs])

    def test_failure_of_one_source_does_not_block_another(self):
        valid, memory = self.memory()
        invalid = self.write("invalid.json", "not json")
        results = collect_many([self.job(invalid, "memory", "bad"), self.job(valid, "memory", "good")], self.outputs)
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[1]["ok"])
        copied = Path(results[1]["result"]["output"]) / "memory.json"
        self.assertEqual(json.loads(copied.read_text(encoding="utf-8")), memory)
        self.assertTrue((self.outputs / "bad" / "attempt-0001" / ".incomplete").exists())

    def test_fast_completion_callback_precedes_slow_completion(self):
        started, fast_observed, release_slow = threading.Event(), threading.Event(), threading.Event()
        caller_thread, observed, release_saw_callback = threading.get_ident(), [], []

        def worker(job, output):
            if job["id"] == "slow":
                started.set()
                if not release_slow.wait(timeout=5):
                    raise TimeoutError("Slow job was not released")
            else:
                if not started.wait(timeout=5):
                    raise TimeoutError("Slow job did not start")
            return {"seen": job["id"]}

        def callback(outcome):
            self.assertEqual(threading.get_ident(), caller_thread)
            observed.append(outcome["job_id"])
            if outcome["job_id"] == "fast":
                self.assertFalse(release_slow.is_set())
                fast_observed.set()

        def release_after_callback():
            release_saw_callback.append(fast_observed.wait(timeout=5))
            release_slow.set()

        releaser = threading.Thread(target=release_after_callback)
        releaser.start()
        try:
            results = collect_many([{"id": "slow"}, {"id": "fast"}], self.outputs,
                                   max_workers=2, worker=worker, on_result=callback)
        finally:
            release_slow.set()
            releaser.join(timeout=5)
        self.assertEqual(release_saw_callback, [True])
        self.assertEqual(observed, ["fast", "slow"])
        self.assertEqual([item["job_id"] for item in results], ["slow", "fast"])
        self.assertTrue(all(item["ok"] for item in results))

    def test_failures_also_receive_callback_and_callback_failure_does_not_hide_others(self):
        received = []

        def worker(job, output):
            if job["id"] == "bad":
                raise ValueError("Synthetic source failure")
            return {"value": "complete"}

        outcomes = collect_many([{"id": "bad"}, {"id": "good"}], self.outputs,
                                worker=worker, on_result=received.append)
        self.assertEqual({item["job_id"] for item in received}, {"bad", "good"})
        self.assertFalse(next(item for item in received if item["job_id"] == "bad")["ok"])
        self.assertFalse(outcomes[0]["ok"])

        callbacks = []

        def broken_callback(outcome):
            callbacks.append(outcome["job_id"])
            if outcome["job_id"] == "bad":
                raise RuntimeError("Synthetic store failure")

        with self.assertRaisesRegex(ValidationError, "result callbacks failed"):
            collect_many([{"id": "bad"}, {"id": "good"}], self.outputs,
                         worker=worker, on_result=broken_callback)
        self.assertEqual(set(callbacks), {"bad", "good"})

    def test_memory_pending_is_copied_without_approval(self):
        source, memory = self.memory()
        before = source.read_bytes()
        result = collect_job(self.job(source, "memory"), self.outputs)
        self.assertEqual((Path(result["output"]) / "memory.json").read_bytes(), before)
        self.assertEqual(result["report"]["review_states"], {"pending": 1})
        self.assertFalse(result["report"]["native_memory_applied"])
        self.assertEqual(source.read_bytes(), before)

    def test_folder_default_collects_context_and_skills_excludes_config(self):
        for name in ("AGENTS.md", "CLAUDE.md", "notes/План.md", ".claude/skills/example/SKILL.md", "README.txt"):
            self.write(name)
        for name in (".env", ".env.example", "config.json", ".claude.json", "settings.yaml", "credentials.txt", "passwords.txt", ".git/history.md", "node_modules/pkg/README.md", "cache/secret.md", "artifact.pdf", "code.py"):
            self.write(name, "Should not be copied")
        result = collect_job(self.job(self.sources), self.outputs)
        snapshot = Path(result["report"]["data_path"])
        paths = sorted(path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*") if path.is_file())
        self.assertEqual(paths, [".claude/skills/example/SKILL.md", "AGENTS.md", "CLAUDE.md", "README.txt", "notes/План.md"])
        self.assertEqual(result["report"]["file_count"], 5)

    def test_explicit_artifact_selection_still_excludes_credentials(self):
        self.write("artifacts/result.pdf", "fake PDF test bytes")
        self.write("artifacts/config.json", "configuration")
        self.write("artifacts/passwords.txt", "not selected")
        self.write("secrets/key.md", "excluded directory")
        result = collect_job(self.job(self.sources, include=["artifacts", "secrets"]), self.outputs)
        self.assertEqual([item["path"] for item in result["report"]["files"]], ["artifacts/result.pdf"])

    def test_fingerprint_uses_same_selection_and_ignores_timestamps(self):
        selected = self.write("context.md", "one")
        ignored = self.write("config.json", "old")
        spec = self.job(self.sources)["input"]
        initial = fingerprint_input(spec)
        ignored.write_text("new", encoding="utf-8")
        os.utime(selected, (1_000_000_000, 1_000_000_000))
        self.assertEqual(fingerprint_input(spec), initial)
        selected.write_text("two", encoding="utf-8")
        self.assertNotEqual(fingerprint_input(spec), initial)
        before_add = fingerprint_input(spec)
        self.write("extra.md")
        self.assertNotEqual(fingerprint_input(spec), before_add)

    def test_claimed_snapshot_change_is_rejected_before_output(self):
        source, _ = self.memory()
        job = self.job(source, "memory")
        job["input_sha256"] = fingerprint_input(job["input"])
        source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(ValidationError):
            collect_job(job, self.outputs)
        self.assertFalse(self.outputs.exists())

    def test_attempts_never_overwrite_and_retry_is_new_directory(self):
        source, _ = self.memory()
        job = self.job(source, "memory")
        first = collect_job(job, self.outputs)
        with self.assertRaises(ValidationError):
            collect_job(job, self.outputs)
        retry = copy.deepcopy(job)
        retry["attempt"] = 2
        second = collect_job(retry, self.outputs)
        self.assertNotEqual(first["output"], second["output"])
        self.assertTrue((Path(first["output"]) / "memory.json").exists())

    def test_traversal_and_output_inside_source_rejected(self):
        self.write("context.md")
        for include in (["../outside.md"], ["/absolute"], ["C:\\private"], ["a/../../private"]):
            with self.assertRaises(ValidationError):
                fingerprint_input(self.job(self.sources, include=include)["input"])
        with self.assertRaises(ValidationError):
            collect_job(self.job(self.sources), self.sources / "new-output")

    def test_symlink_source_is_rejected(self):
        real = self.write("real.md")
        linked = self.sources / "linked.md"
        try:
            linked.symlink_to(real)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable on this Windows account: {exc}")
        with self.assertRaises(ValidationError):
            fingerprint_input(self.job(self.sources)["input"])

    def test_resource_limits_and_worker_bounds(self):
        self.write("one.md")
        self.write("two.md")
        with patch("portable_memory.collection.MAX_FILES", 1):
            with self.assertRaises(ValidationError):
                fingerprint_input(self.job(self.sources)["input"])
        for limit in (0, 9, True, 1.5):
            with self.assertRaises(ValidationError):
                collect_many([], self.outputs, max_workers=limit)

    def test_provided_archive_ingested_in_independent_attempt(self):
        archive = [{"uuid": "chat-one", "name": "Example", "chat_messages": [{"uuid": "message-one", "sender": "human", "text": "Hello"}]}]
        source = self.write("conversations.json", json.dumps(archive))
        result = collect_job(self.job(source, "archive"), self.outputs)
        self.assertEqual(result["report"]["conversation_count"], 1)
        self.assertTrue((Path(result["report"]["data_path"]) / "conversations.json").is_file())
        self.assertFalse((Path(result["output"]) / ".incomplete").exists())


if __name__ == "__main__":
    unittest.main()
