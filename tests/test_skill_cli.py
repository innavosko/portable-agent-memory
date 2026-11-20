"""A supplied catalog is not an account crawler; original skills remain data."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from portable_memory.package import load_json, read_package
from scripts.synthetic_fixture_audit import save_fixture_store

ROOT = Path(__file__).resolve().parents[1]


class SkillCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skill = self.root / "provided" / "custom-review"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: custom-review\ndescription: Review the supplied fictional notes.\n---\n\nUse references/format.md.\n", encoding="utf-8")
        (self.skill / "references").mkdir()
        (self.skill / "references" / "format.md").write_text("Fictional expected sections", encoding="utf-8")
        (self.skill / "scripts").mkdir()
        (self.skill / "scripts" / "example.py").write_text("raise RuntimeError('Never run transported code')\n", encoding="utf-8")
        (self.skill / "agents").mkdir()
        (self.skill / "agents" / "openai.yaml").write_text("policy:\n  allow_implicit_invocation: false\n", encoding="utf-8")
        self.source = {"product": "fictional-agent", "account_id": "example-account", "workspace_id": "example-space",
                       "observed_at": "2026-11-20", "evidence": "Fictional explicit folder selection", "catalog_complete": False}
        self.source_file = self.save("source.json", self.source)

    def save(self, name, value):
        file = self.root / name
        file.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8-sig")
        return file

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-B", "-m", "portable_memory", *map(str, args)],
                                cwd=ROOT, text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(expected, result.returncode, result.stderr or result.stdout)
        return json.loads(result.stdout if expected == 0 else result.stderr)

    def discover(self):
        file = self.root / "catalog.json"
        summary = self.cli("skill-discover", self.root / "provided", "--source", self.source_file, "--out", file)
        self.assertTrue(summary["selection_required"])
        catalog = load_json(file)
        self.assertFalse(catalog["items"][0]["selected"])
        return catalog

    def selected(self):
        catalog = self.discover()
        item = catalog["items"][0]
        item.update({"selected": True, "origin": "user_created", "enabled": False})
        item["locator"] = {"kind": "folder", "path": "provided/custom-review"}
        return self.save("selected.json", catalog)

    def test_original_bundle_roundtrip_preserves_yaml_scripts_and_disabled_state(self):
        collected = self.root / "collected"
        catalog = self.selected()
        self.cli("skill-collect", catalog, "--out", collected, "--accept")
        memory = load_json(collected / "memory.json")
        self.assertEqual("pending", memory["records"][0]["review"]["decision"])
        self.assertFalse(memory["records"][0]["value"]["enabled"])
        self.assertEqual("user_created", memory["records"][0]["value"]["origin"])
        package = self.root / "export.zip"
        common_store = self.root / "common-profile.sqlite"
        audited = save_fixture_store(common_store, memory)
        audited_memory = self.save("audited-memory.json", audited["memory"])
        self.cli("export", audited_memory, "--common-store", common_store,
                 "--assets", collected / "assets", "--out", package)
        data = read_package(package)
        for relative in ("agents/openai.yaml", "scripts/example.py", "references/format.md", "SKILL.md"):
            matches = [raw for name, raw in data["files"].items() if name.startswith("assets/skills/") and name.endswith(relative)]
            self.assertEqual([(self.skill / relative).read_bytes()], matches)
        plan = self.root / "plan.json"
        self.cli("plan", package, "--out", plan)
        self.assertEqual("defer", load_json(plan)["items"][0]["action"])
        self.cli("apply", package, "--plan", plan, "--out", self.root / "workspace", "--accept")
        self.assertEqual([], load_json(self.root / "workspace" / "memory.json")["records"])
        self.assertEqual("not_installed", load_json(self.root / "workspace" / "receipt.json")["skills"])

    def test_copy_requires_acceptance_and_existing_output_is_preserved(self):
        catalog = self.selected()
        output = self.root / "collected"
        self.cli("skill-collect", catalog, "--out", output, expected=2)
        self.assertFalse(output.exists())
        self.cli("skill-collect", catalog, "--out", output, "--accept")
        original = (output / "memory.json").read_bytes()
        self.cli("skill-collect", catalog, "--out", output, "--accept", expected=2)
        self.assertEqual(original, (output / "memory.json").read_bytes())

    def test_provided_zip_and_unavailable_catalog_entry_are_both_reported(self):
        catalog = self.discover()
        archive = self.root / "skill.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            for path in self.skill.rglob("*"):
                if path.is_file():
                    handle.writestr("wrapper/" + path.relative_to(self.skill).as_posix(), path.read_bytes())
        catalog["items"][0].update({"selected": True, "locator": {"kind": "zip", "path": "skill.zip"}})
        catalog["items"].append({"id": "not-readable", "name": "Fictional opaque skill", "version": None,
            "origin": "platform", "enabled": True, "selected": True, "source_ref": "fictional-item-id",
            "locator": {"kind": "unavailable", "reason": "The supplied catalog has no downloadable original"}})
        selected = self.save("selected.json", catalog)
        output = self.root / "collected"
        self.cli("skill-collect", selected, "--out", output, "--accept")
        memory = load_json(output / "memory.json")
        self.assertEqual(2, len(memory["records"]))
        unavailable = next(r for r in memory["records"] if r["title"] == "Fictional opaque skill")
        self.assertEqual("pending", unavailable["review"]["decision"])
        self.assertIn("unavailable", json.dumps(unavailable))
        self.assertTrue((output / "inventory-report.json").is_file())

    def test_discovery_does_not_overwrite_an_existing_catalog(self):
        self.discover()
        file = self.root / "catalog.json"
        original = file.read_bytes()
        self.cli("skill-discover", self.root / "provided", "--source", self.source_file, "--out", file, expected=2)
        self.assertEqual(original, file.read_bytes())


if __name__ == "__main__":
    unittest.main()
