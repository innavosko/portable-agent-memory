"""Synthetic originals only: discovery, completeness, portability and ZIP safety."""
import copy
import json
import os
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from portable_memory import skill_inventory as skills
from portable_memory.errors import ValidationError
from portable_memory.model import validate_memory


class SkillInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = {"product": "fictional-agent", "account_id": "owner-1", "workspace_id": "personal",
                       "observed_at": "2026-11-20T10:00:00Z", "evidence": "Host returned this explicit synthetic catalog.",
                       "catalog_complete": False}

    def folder(self, name="sample", files=None):
        root = self.root / name
        entries = {"SKILL.md": "---\nname: Пример\n---\nВымышленный навык.\n",
                   "scripts/run.py": "raise RuntimeError('MUST NOT EXECUTE')\n",
                   "references/правила.md": "Вход → проверка → результат.\n",
                   "assets/template.bin": b"\x00\x01\xff",
                   "agents/openai.yaml": "interface:\n  display_name: Example\n",
                   "pyproject.toml": "[project]\nname = 'example'\nversion = '1.0'\n",
                   "package-lock.json": '{"lockfileVersion": 3}',
                   "requirements.txt": "example-package==1.2.3\n",
                   "config.json": '{"format": "table", "locale": "ru"}'}
        if files is not None:
            entries = files
        for path, data in entries.items():
            file = root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return root

    def item(self, path=None, sid="example", **overrides):
        item = {"id": sid, "name": "Одинаковое имя", "version": "1.2", "origin": "user_created",
                "enabled": False, "selected": True, "source_ref": "host:skills:" + sid,
                "locator": {"kind": "folder", "path": str(path or self.folder())},
                "dependencies": ["Требуется инструмент чтения таблиц", "requirements.txt describes runtime libraries"]}
        item.update(overrides)
        return item

    def catalog(self, *items):
        return {"format": "portable-skill-catalog", "schema_version": 1,
                "source": copy.deepcopy(self.source), "items": list(items)}

    def collect(self, catalog, name="result"):
        target = self.root / name
        report = skills.collect_skills(catalog, target, accepted=True)
        memory = json.loads((target / "memory.json").read_text(encoding="utf-8"))
        return report, memory, target

    def zip(self, members, name="input.zip"):
        path = self.root / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member, data in members:
                if isinstance(member, str):
                    original = member
                    member = zipfile.ZipInfo(original)
                    # Preserve hostile bytes: ZipInfo normalizes backslashes on
                    # Windows and truncates NUL when constructed normally.
                    member.filename = original
                archive.writestr(member, data)
        return path

    def test_discovery_is_readonly_shallow_unselected_and_does_not_infer_metadata(self):
        container = self.root / "chosen"
        self.folder("chosen/one")
        self.folder("chosen/two")
        self.folder("chosen/nested/deep")
        before = sorted(str(p) for p in container.rglob("*"))
        catalog = skills.discover_skills(container, self.source)
        self.assertEqual([item["name"] for item in catalog["items"]], ["one", "two"])
        self.assertEqual(before, sorted(str(p) for p in container.rglob("*")))
        for item in catalog["items"]:
            self.assertFalse(item["selected"])
            self.assertEqual(item["origin"], "unknown")
            self.assertIsNone(item["version"])
            self.assertIsNone(item["enabled"])
        single = skills.discover_skills(container / "one", self.source)
        self.assertEqual(single["items"][0]["id"], catalog["items"][0]["id"])
        single["items"][0]["selected"] = True
        report, memory, _ = self.collect(single)
        self.assertNotIn(str(container), json.dumps(report, ensure_ascii=False))
        self.assertNotIn(str(container), json.dumps(memory, ensure_ascii=False))

    def test_discovery_ids_ignore_observation_changes_but_isolate_accounts_workspaces(self):
        folder = self.folder()
        original = skills.discover_skills(folder, self.source)["items"][0]["id"]
        observed = {**self.source, "observed_at": "2027-01-01", "evidence": "Another actual read", "catalog_complete": True}
        self.assertEqual(original, skills.discover_skills(folder, observed)["items"][0]["id"])
        for field in ("account_id", "workspace_id", "product"):
            changed = {**self.source, field: "different"}
            self.assertNotEqual(original, skills.discover_skills(folder, changed)["items"][0]["id"])
        unknown = {**self.source, "account_id": None, "workspace_id": None}
        self.assertEqual(skills.discover_skills(folder, unknown)["source"], unknown)

    def test_discovery_rejects_home_and_drive_root(self):
        for root in (Path.home(), Path(self.root.anchor)):
            with self.subTest(root=root), self.assertRaises(ValidationError):
                skills.discover_skills(root, self.source)

    def test_original_scripts_configs_locks_binary_unicode_are_preserved_without_execution(self):
        original = self.folder()
        catalog = self.catalog(self.item(original))
        untouched = copy.deepcopy(catalog)
        report, memory, target = self.collect(catalog)
        self.assertEqual(catalog, untouched)
        self.assertEqual(report["items"][0]["status"], "full_snapshot")
        for file in original.rglob("*"):
            if file.is_file():
                self.assertEqual(file.read_bytes(), (target / "assets/skills/example" / file.relative_to(original)).read_bytes())
        record = memory["records"][0]
        validate_memory(memory)
        self.assertEqual(record["kind"], "skill")
        self.assertEqual(record["review"], {"decision": "pending"})
        self.assertEqual(record["currency"], "unknown")
        self.assertEqual(record["value"]["enabled"], False)
        self.assertEqual(record["value"]["enabled_semantics"], "source_preference_only")
        self.assertEqual(record["value"]["origin"], "user_created")
        self.assertEqual(record["value"]["dependencies"], catalog["items"][0]["dependencies"])
        self.assertEqual(record["value"]["dependency_check"], "not_verified")
        self.assertEqual(record["value"]["installation_status"], "not_installed")
        self.assertEqual(record["value"]["snapshot_sha256"], report["items"][0]["snapshot_sha256"])
        self.assertFalse((target / ".incomplete").exists())
        self.assertTrue((target / "inventory-report.md").is_file())

    def test_credentials_caches_and_environments_are_explicitly_partial_not_silent(self):
        folder = self.folder(files={"SKILL.md": "A", "agents/openai.yaml": "name: example",
                                    ".env": "do-not-copy", "credentials.json": "do-not-copy",
                                    ".git/config": "do-not-copy", "node_modules/pkg/index.js": "do-not-copy",
                                    ".venv/lib/data.py": "do-not-copy", "__pycache__/data.pyc": b"no"})
        report, memory, target = self.collect(self.catalog(self.item(folder)))
        self.assertEqual(report["items"][0]["status"], "partial")
        self.assertEqual(len(report["items"][0]["excluded"]), 6)
        self.assertEqual(set(report["items"][0]["files"]), {"SKILL.md", "agents/openai.yaml"})
        self.assertEqual(len(memory["coverage"]["excluded"]), 6)
        self.assertEqual(memory["records"][0]["value"]["status"], "partial")
        self.assertFalse((target / "assets/skills/example/.env").exists())

    def test_unavailable_selected_skills_remain_in_memory_and_report(self):
        missing = self.item(self.root / "missing", sid="missing")
        unavailable = self.item(self.root / "ignored", sid="locked", locator={"kind": "unavailable", "reason": "Platform did not return original files"})
        skipped = self.item(self.root / "do-not-read", sid="not-selected", selected=False)
        report, memory, target = self.collect(self.catalog(missing, unavailable, skipped))
        self.assertEqual(report["summary"]["unavailable"], 2)
        self.assertEqual(report["summary"]["not_selected"], 1)
        self.assertEqual({r["id"] for r in memory["records"]}, {"missing", "locked"})
        self.assertTrue(all(r["value"]["status"] == "unavailable" and r["value"]["snapshot_sha256"] is None for r in memory["records"]))
        self.assertTrue((target / "assets/skills").is_dir())
        self.assertFalse(any((target / "assets/skills").iterdir()))

    def test_same_named_skills_are_separate_and_duplicate_ids_rejected(self):
        left = self.item(self.folder("left"), sid="left")
        right = self.item(self.folder("right"), sid="right", origin="third_party")
        report, memory, _ = self.collect(self.catalog(left, right))
        self.assertEqual(len(memory["records"]), 2)
        self.assertEqual(report["summary"]["full_snapshot"], 2)
        self.assertNotEqual(memory["records"][0]["scope"], memory["records"][1]["scope"])
        for rid in ("left", "LEFT"):
            duplicate = {**right, "id": rid}
            with self.subTest(rid=rid), self.assertRaises(ValidationError):
                skills.validate_catalog(self.catalog(left, duplicate))

    def test_duplicate_confirmed_source_version_is_rejected_even_under_different_ids(self):
        first = self.item()
        alias = {**first, "id": "seen-in-another-agent"}
        with self.assertRaisesRegex(ValidationError, "Repeated confirmed skill source/version"):
            skills.validate_catalog(self.catalog(first, alias))
        # New versions remain distinct candidates for explicit review.
        skills.validate_catalog(self.catalog(first, {**alias, "version": "2.0"}))
        # Titles and origin are never a deduplication key.
        skills.validate_catalog(self.catalog(first, {**alias, "source_ref": "host:skills:separate", "origin": "third_party"}))
        # Unknown owner/workspace does not establish shared identity.
        for unknown in ("account_id", "workspace_id"):
            catalog = self.catalog(first, alias)
            catalog["source"][unknown] = None
            skills.validate_catalog(catalog)

    def test_collection_requires_explicit_acceptance_selection_and_new_destination(self):
        item = self.item()
        catalog = self.catalog(item)
        output = self.root / "never-created"
        for acceptance in (False, 1, None):
            with self.subTest(acceptance=acceptance), self.assertRaises(ValidationError):
                skills.collect_skills(catalog, output, accepted=acceptance)
        self.assertFalse(output.exists())
        with self.assertRaises(ValidationError):
            skills.collect_skills(self.catalog({**item, "selected": False}), output, accepted=True)
        self.collect(catalog)
        with self.assertRaises(ValidationError):
            skills.collect_skills(catalog, self.root / "result", accepted=True)

    def test_output_cannot_be_inside_source_and_failed_write_stays_incomplete(self):
        folder = self.folder()
        catalog = self.catalog(self.item(folder))
        with self.assertRaises(ValidationError):
            skills.collect_skills(catalog, folder / "output", accepted=True)
        write_new = skills.write_new

        def fail_memory(path, data):
            if Path(path).name == "memory.json":
                raise OSError("synthetic disk failure")
            return write_new(path, data)

        with patch.object(skills, "write_new", side_effect=fail_memory), self.assertRaises(OSError):
            self.collect(catalog)
        self.assertTrue((self.root / "result/.incomplete").is_file())
        with self.assertRaises(ValidationError):
            self.collect(catalog)

    def test_zip_root_and_single_wrapper_preserve_files_and_equal_folder_snapshot(self):
        folder = self.folder()
        files = [(p.relative_to(folder).as_posix(), p.read_bytes()) for p in folder.rglob("*") if p.is_file()]
        base_report, _, _ = self.collect(self.catalog(self.item(folder)), "folder-result")
        for index, prefix in enumerate(("", "example/")):
            members = [(prefix + name, raw) for name, raw in files]
            if prefix:
                members.insert(0, (prefix, b""))
            zip_path = self.zip(members, f"source{index}.zip")
            item = self.item(folder, locator={"kind": "zip", "path": str(zip_path)})
            report, _, target = self.collect(self.catalog(item), f"zip-result-{index}")
            self.assertEqual(report["items"][0]["snapshot_sha256"], base_report["items"][0]["snapshot_sha256"])
            self.assertEqual((target / "assets/skills/example/agents/openai.yaml").read_bytes(), (folder / "agents/openai.yaml").read_bytes())

    def test_changed_file_changes_snapshot_without_changing_discovered_identity(self):
        folder = self.folder()
        before = skills.discover_skills(folder, self.source)
        before["items"][0]["selected"] = True
        first, _, _ = self.collect(before, "first")
        (folder / "SKILL.md").write_text("Changed content", encoding="utf-8")
        after = skills.discover_skills(folder, self.source)
        after["items"][0]["selected"] = True
        second, _, _ = self.collect(after, "second")
        self.assertEqual(before["items"][0]["id"], after["items"][0]["id"])
        self.assertNotEqual(first["items"][0]["snapshot_sha256"], second["items"][0]["snapshot_sha256"])

    def test_zip_unsafe_names_duplicates_unicode_collisions_and_file_parents_rejected(self):
        invalid = ["../escape", "/absolute", "C:/absolute", "C:\\absolute", "a\\b", "NUL.txt", "trailing.", "bad\x00name"]
        cases = [[("SKILL.md", "ok"), (name, "bad")] for name in invalid]
        cases += [[("SKILL.md", "one"), ("SKILL.md", "two")],
                  [("SKILL.md", "one"), ("skill.md", "two")],
                  [("SKILL.md", "one"), ("é.txt", "two"), ("e\u0301.txt", "three")],
                  [("SKILL.md", "one"), ("scripts", "file"), ("scripts/run.py", "bad")],
                  [("SKILL.md", "one"), ("scripts/run.py", "bad"), ("scripts", "file")]]
        for index, members in enumerate(cases):
            with self.subTest(index=index):
                path = self.zip(members, f"bad-{index}.zip")
                item = self.item(self.root / "unused", locator={"kind": "zip", "path": str(path)})
                with self.assertRaises(ValidationError):
                    self.collect(self.catalog(item), f"rejected-{index}")
                self.assertFalse((self.root / f"rejected-{index}").exists())

    def test_zip_symlinks_and_multiple_skill_roots_rejected(self):
        link = zipfile.ZipInfo("linked.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = [[("SKILL.md", "ok"), (link, "../outside")],
                 [("a/SKILL.md", "one"), ("b/SKILL.md", "two")],
                 [("a/SKILL.md", "one"), ("unrelated.txt", "extra")],
                 [("nested/deep/SKILL.md", "too deep")]]
        for index, members in enumerate(cases):
            path = self.zip(members, f"invalid-roots-{index}.zip")
            item = self.item(self.root / "unused", locator={"kind": "zip", "path": str(path)})
            with self.subTest(index=index), self.assertRaises(ValidationError):
                self.collect(self.catalog(item), f"rejected-roots-{index}")

    def test_zip_excluded_credentials_mark_partial_and_missing_skill_is_not_silently_accepted(self):
        path = self.zip([("SKILL.md", "ok"), ("agents/platform.yaml", "name: example"), ("auth.json", "secret")])
        item = self.item(self.root / "unused", locator={"kind": "zip", "path": str(path)})
        report, _, target = self.collect(self.catalog(item))
        self.assertEqual(report["items"][0]["status"], "partial")
        self.assertFalse((target / "assets/skills/example/auth.json").exists())
        folder = self.folder("no-skill", {"README.md": "not a skill"})
        with self.assertRaises(ValidationError):
            self.collect(self.catalog(self.item(folder)), "missing-skill")

    def test_malformed_zip_and_resource_limits_fail_before_output(self):
        bad = self.root / "broken.zip"
        bad.write_bytes(b"not a zip")
        item = self.item(self.root / "unused", locator={"kind": "zip", "path": str(bad)})
        with self.assertRaises(ValidationError):
            self.collect(self.catalog(item), "bad-result")
        self.assertFalse((self.root / "bad-result").exists())
        folder = self.folder()
        for limit, value in (("MAX_FILE_BYTES", 1), ("MAX_TOTAL_BYTES", 1), ("MAX_FILES", 1), ("MAX_ENTRIES", 1)):
            with self.subTest(limit=limit), patch.object(skills, limit, value), self.assertRaises(ValidationError):
                self.collect(self.catalog(self.item(folder)), "limited")
            self.assertFalse((self.root / "limited").exists())
        zipped = self.zip([("SKILL.md", "x" * 1000)])
        item = self.item(folder, locator={"kind": "zip", "path": str(zipped)})
        with patch.object(skills, "MAX_TOTAL_BYTES", 100), self.assertRaises(ValidationError):
            self.collect(self.catalog(item), "expanded")

    def test_schema_does_not_accept_forged_selection_unknown_fields_or_unsafe_ids(self):
        item = self.item()
        for field, value in (("selected", 1), ("enabled", "yes"), ("origin", "guessed"),
                             ("version", 42), ("id", "../escape"), ("dependencies", "anything")):
            bad = self.catalog({**item, field: value})
            with self.subTest(field=field), self.assertRaises(ValidationError):
                skills.validate_catalog(bad)
        bad = self.catalog(item)
        bad["source"]["catalog_complete"] = 1
        with self.assertRaises(ValidationError):
            skills.validate_catalog(bad)
        bad = self.catalog({**item, "automatically_installed": True})
        with self.assertRaises(ValidationError):
            skills.validate_catalog(bad)

    def test_folders_reject_symlink_files_if_host_allows_them(self):
        folder = self.folder()
        try:
            os.symlink(folder / "SKILL.md", folder / "shortcut.md")
        except OSError as exc:
            self.skipTest(f"Host does not permit creating symlinks: {exc}")
        with self.assertRaises(ValidationError):
            self.collect(self.catalog(self.item(folder)))

    @unittest.skipUnless(os.name == "nt", "Windows junction check")
    def test_discovery_and_collection_reject_real_junctions(self):
        folder = self.folder()
        selected = self.root / "selected"
        selected.mkdir()
        junction = selected / "redirected"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(folder)], capture_output=True)
        if result.returncode:
            self.skipTest("Host does not permit creating junctions")
        self.addCleanup(lambda: os.rmdir(junction) if junction.exists() else None)
        with self.assertRaises(ValidationError):
            skills.discover_skills(selected, self.source)
        with self.assertRaises(ValidationError):
            self.collect(self.catalog(self.item(junction)))


if __name__ == "__main__":
    unittest.main()
