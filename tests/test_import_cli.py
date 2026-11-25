"""CLI receiving flow with authored fictional data, never real host operations."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from portable_memory.model import empty_memory
from portable_memory.package import json_bytes, load_json
from scripts.synthetic_fixture_audit import export_synthetic_package
from test_setup import memory_of


ROOT = Path(__file__).resolve().parents[1]


class ImportCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pm-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "selected-context.zip"
        self.memory = memory_of(("preference", "answer-language", "active", "Fictional preference: Russian"))
        export_synthetic_package(self.memory, self.archive)
        self.config = self.save("setup-delivery.json", {
            "recipient": {"agent_id": "fictional-receiver", "workspace_id": "fictional-workspace"},
            "setup_scope": ["profile"],
        })

    def save(self, name, data):
        path = self.root / name
        path.write_bytes(json_bytes(data))
        return path

    def cli(self, *args, expected=0):
        completed = subprocess.run([sys.executable, "-B", "-m", "portable_memory", *map(str, args)],
                                   cwd=ROOT, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(expected, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout if expected == 0 else completed.stderr)

    def prepare(self, *, current=None, origin=None, setup=True):
        plan = self.root / "plan.json"
        arguments = ["plan", self.archive, "--out", plan]
        if current is not None:
            arguments += ["--current", self.save("current.json", current)]
        if origin:
            arguments += ["--current-source", origin]
        self.cli(*arguments)
        workspace = self.root / "received"
        arguments = ["apply", self.archive, "--plan", plan, "--out", workspace, "--accept"]
        if setup:
            arguments += ["--setup-delivery", self.config]
        result = self.cli(*arguments)
        self.assertEqual("not_applied", result["native_memory"])
        self.assertTrue((workspace / "IMPORT_REPORT.md").is_file())
        self.assertTrue((workspace / "NEXT_STEPS.md").is_file())
        return workspace, result

    def capability(self, state):
        event = self.save("capability.json", {
            "id": "fictional-native-capability", "actor": "agent", "type": "capability",
            "data": {"name": "native_memory", "available": True,
                     "evidence": "Fictional test capability; no real native API was called."},
        })
        next_state = self.root / "setup-r1.json"
        self.cli("setup-event", state, "--event", event, "--revision", 0, "--out", next_state)
        return next_state

    def verification(self, state, *, expected=0):
        event = self.save("result.json", {
            "id": "fictional-native-result", "actor": "agent", "type": "step_result",
            "data": {"step_id": "profile", "status": "verified",
                     "evidence": "Fictional fixture describing an exact approved operation.",
                     "read_back": "Fictional fixture readback matches; no actual host action occurred."},
        })
        destination = self.root / "setup-r2.json"
        self.cli("setup-event", state, "--event", event, "--revision", 1,
                 "--out", destination, expected=expected)
        return destination

    def test_unseen_receiver_memory_blocks_native_completion_even_with_host_result(self):
        workspace, result = self.prepare()
        saved = load_json(workspace / "memory.json")
        self.assertTrue(saved["metadata"]["recipient_memory_required"])
        self.assertNotIn("Fictional preference: Russian", (workspace / "ACTIVE_PROFILE.md").read_text(encoding="utf-8"))
        self.assertIn("Fictional preference: Russian", (workspace / "PROFILE.md").read_text(encoding="utf-8"))
        state = self.capability(workspace / "setup.json")
        blocked = self.verification(state, expected=2)
        self.assertFalse(blocked.exists())

    def test_explicit_fictional_snapshot_initializes_setup_and_requires_readback(self):
        workspace, result = self.prepare(current=empty_memory())
        view = self.cli("setup-view", workspace / "setup.json")
        self.assertEqual("needs_setup", view["status"])
        self.assertEqual("pending", view["items"][0]["status"])
        state = self.capability(workspace / "setup.json")
        finished = self.verification(state)
        view = self.cli("setup-view", finished)
        self.assertEqual("completed_within_requested_scope", view["status"])
        self.assertEqual("host_attestation_for_native_steps", view["verification"])

    def test_previous_workspace_is_not_a_fresh_receiver_snapshot(self):
        workspace, result = self.prepare(current=empty_memory(), origin="previous_portable_workspace")
        self.assertTrue(load_json(workspace / "memory.json")["metadata"]["recipient_memory_required"])
        self.assertNotEqual("completed_within_requested_scope", self.cli("setup-view", workspace / "setup.json")["status"])

    def test_no_setup_selection_still_produces_actionable_incomplete_receipt(self):
        workspace, result = self.prepare(current=empty_memory(), setup=False)
        self.assertTrue(result["setup_required"])
        self.assertFalse((workspace / "setup.json").exists())
        self.assertIn("profile", result["pending_categories"])


if __name__ == "__main__":
    unittest.main()
