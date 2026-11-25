"""Receiver handoff tests use only fictional local profiles and installers."""
import hashlib
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from portable_memory.common_profile import common_result, create_common
from portable_memory.completion import preflight_handoff, prepare_handoff
from portable_memory.errors import ValidationError
from portable_memory.package import export_package
from test_common_profile import COORDINATOR, confirmed, memory_of, record, reviewed


def native_record(name="concise", kind="feedback", content="Пользователь предпочитает краткие ответы."):
    return (f"### {name}\n- memory_type: {kind}\n- description: Вымышленная запись для проверки\n"
            "- priority: high\n- observed_at: неизвестно\n- evidence: Вымышленное сообщение пользователя\n\n"
            f"**content**\n{content}\n\n")


def native_document(*records):
    return ("# Экспорт памяти о пользователе\n\n## Сведения об экспорте\n"
            "- Исходный агент: Вымышленный источник теста\n- Дата экспорта: 2026-11-24\n"
            "- О ком экспорт: Вымышленный пользователь\n- Доступные источники: Тестовые записи\n"
            "- Ограничения полноты: Только тестовые записи\n\n## Готово к импорту\n\n" + "".join(records) +
            "## Справочное приложение и резерв — не импортировать автоматически\n\n"
            "Эта архивная справка не является записью для импорта.\n\n## Итог\n"
            f"Готовых тестовых записей: {len(records)}. Личная память не использовалась.\n")


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "handoff"
        self.instruction = self.write("source-instruction.md", "# Импорт\nПрочитай все файлы; проверь и сохрани доступными инструментами.")

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def profile(self, name="client-memory-export.md", *records):
        return self.write(name, native_document(*(records or (native_record(),))))

    def installer(self, *, extra=None, skill_name="memory-import"):
        path = self.root / "installer.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("memory-import/SKILL.md", f"---\nname: {skill_name}\ndescription: Fictional test\n---\n# Import")
            archive.writestr("memory-import/scripts/memory_tool.py", "# Fictional entry point\n")
            archive.writestr("memory-import/scripts/portable_memory/__init__.py", "")
            archive.writestr("memory-import/scripts/portable_memory/cli.py", "# Fictional runtime\n")
            if extra is not None:
                archive.writestr(*extra)
        return path

    def package(self, name="memory-export.zip"):
        source = confirmed(memory_of(record("concise", "Пользователь предпочитает краткие рабочие ответы.")))
        state = reviewed(create_common(COORDINATOR, previous=source), predicates={"concise": "response-length"})
        checked = common_result(state)
        path = self.root / name
        export_package(checked["memory"], path, review=checked["review"])
        return path

    def test_giga_copies_actual_profile_instruction_and_exact_next_steps(self):
        source = self.profile()
        before = source.read_bytes()
        report = prepare_handoff("gigaagent", [source], self.out, instruction=self.instruction)
        self.assertEqual((self.out / source.name).read_bytes(), before)
        self.assertEqual((self.out / "import-instruction.md").read_bytes(), self.instruction.read_bytes())
        self.assertEqual(report["native_memory"], "not_written")
        self.assertEqual(report["delivery_scope"], "gigaagent_upload_only")
        self.assertFalse(report["full_export_complete"])
        self.assertEqual(report["skill_installation"], "not_performed")
        self.assertEqual(report["gigaagent_counts"], {"feedback": 1})
        guide = Path(report["guide"]).read_text(encoding="utf-8")
        self.assertIn("client-memory-export.md", guide)
        self.assertIn("полностью прочитай", guide)
        self.assertIn("Не ограничивайся пересказом", guide)
        self.assertIn("Markdown не нужно распаковывать", guide)
        self.assertIn("ГигаАгент не принимает архивы и навыки", guide)
        self.assertNotIn("memory-import.zip", guide)
        self.assertEqual({".md"}, {path.suffix for path in self.out.iterdir()})
        self.assertIsNone(report["manifest"])
        self.assertEqual(["import-instruction.md", source.name], report["upload_files"])
        self.assertEqual(source.read_bytes(), before)
        for item in report["files"]:
            self.assertEqual(item["sha256"], hashlib.sha256((self.out / item["name"]).read_bytes()).hexdigest())

    def test_giga_requires_instruction_before_creating_output(self):
        with self.assertRaisesRegex(ValidationError, "requires import-instruction"):
            prepare_handoff("gigaagent", [self.profile()], self.out)
        self.assertFalse(self.out.exists())

    def test_preflight_checks_assets_and_destination_without_creating_output(self):
        report = preflight_handoff("gigaagent", self.out, instruction=self.instruction)
        self.assertEqual(report["status"], "ready_for_export")
        self.assertFalse(self.out.exists())
        self.assertEqual(report["assets"][0]["name"], "import-instruction.md")
        with self.assertRaisesRegex(ValidationError, "actual memory-import.zip"):
            preflight_handoff("chatgpt", self.out)
        self.out.mkdir()
        with self.assertRaisesRegex(ValidationError, "already exists"):
            preflight_handoff("other", self.out)

    def test_giga_rejects_arbitrary_md_empty_ready_and_zip(self):
        bad = [self.write("notes.md", "# User\nA free-form profile"),
               self.write("reserve.md", native_document()), self.installer()]
        bad += [self.write("SKILL.md", "---\nname: test-skill\ndescription: Test\n---\n# Instructions"),
                self.write("profile.json", "{}"),
                self.write("incomplete-profile.md", "# Экспорт памяти о пользователе\n\n## Готово к импорту\n" + native_record())]
        for source in bad:
            with self.subTest(source=source.name), self.assertRaises(ValidationError):
                prepare_handoff("gigaagent", [source], self.out, instruction=self.instruction)
            self.assertFalse(self.out.exists())

    def test_giga_multiple_files_share_limits_and_duplicate_names(self):
        a = self.profile("one.md", native_record("same"))
        b = self.profile("two.md", native_record("SAME", content="Другая запись."))
        with self.assertRaisesRegex(ValidationError, "Duplicate profile record name"):
            prepare_handoff("gigaagent", [a, b], self.out, instruction=self.instruction)
        a = self.profile("one.md", *(native_record(f"user-{i}", "user", f"Вымышленный факт номер {i}.") for i in range(20)))
        b = self.profile("two.md", native_record("user-last", "user", "Дополнительный вымышленный факт."))
        with self.assertRaisesRegex(ValidationError, "Combined GigaAgent user limit"):
            prepare_handoff("gigaagent", [a, b], self.out, instruction=self.instruction)
        self.assertFalse(self.out.exists())

    def test_giga_exact_repeated_content_across_files_is_not_silently_merged(self):
        a = self.profile("one.md", native_record("first"))
        b = self.profile("two.md", native_record("second", content="Пользователь  предпочитает краткие ответы."))
        with self.assertRaisesRegex(ValidationError, "Identical profile record"):
            prepare_handoff("gigaagent", [a, b], self.out, instruction=self.instruction)
        self.assertFalse(self.out.exists())

    def test_giga_multiple_valid_files_keep_actual_names_and_counts(self):
        files = [self.profile("Личные сведения.md", native_record("person", "user", "Вымышленный факт.")),
                 self.profile("Правила.md", native_record())]
        report = prepare_handoff("gigaagent", files, self.out, instruction=self.instruction)
        guide = Path(report["guide"]).read_text(encoding="utf-8")
        self.assertIn("Личные сведения.md", guide)
        self.assertIn("Правила.md", guide)
        self.assertEqual(report["gigaagent_counts"], {"user": 1, "feedback": 1})

    def test_chatgpt_and_claude_include_verified_actual_installer_and_portable_data(self):
        package, installer = self.package(), self.installer()
        for target in ("chatgpt", "claude"):
            with self.subTest(target=target):
                out = self.root / target
                report = prepare_handoff(target, [package], out, import_skill=installer)
                self.assertEqual((out / "memory-import.zip").read_bytes(), installer.read_bytes())
                self.assertEqual((out / package.name).read_bytes(), package.read_bytes())
                self.assertEqual(report["files"][0]["record_count"], 1)
                guide = Path(report["guide"]).read_text(encoding="utf-8")
                self.assertIn("memory-export.zip", guide)
                self.assertIn("memory-import.zip", guide)
                if target == "chatgpt":
                    self.assertIn("обычный чат сама по себе не устанавливает", guide)
                    self.assertIn("ручной запуск", guide)
                    self.assertIn("полный memory-import.zip", guide)
                    self.assertIn("справочные файлы", guide)
                else:
                    self.assertIn("Customize → Skills", guide)

    def test_portable_receiver_rejects_missing_installer_and_arbitrary_data(self):
        package = self.package()
        with self.assertRaisesRegex(ValidationError, "actual memory-import.zip"):
            prepare_handoff("claude", [package], self.out)
        with self.assertRaises(ValidationError):
            prepare_handoff("chatgpt", [self.profile()], self.out, import_skill=self.installer())
        self.assertFalse(self.out.exists())

    def test_installer_rejects_wrong_role_traversal_and_symlink(self):
        package = self.package()
        for path in (self.installer(skill_name="memory-export"),):
            with self.assertRaisesRegex(ValidationError, "must declare"):
                prepare_handoff("claude", [package], self.out, import_skill=path)
        for name in ("../evil.py", "memory-import/../evil.py", "memory-import/SKILL.MD"):
            installer = self.installer(extra=(name, "malicious"))
            with self.subTest(name=name), self.assertRaises(ValidationError):
                prepare_handoff("claude", [package], self.out, import_skill=installer)
        link = zipfile.ZipInfo("memory-import/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        installer = self.installer(extra=(link, "target"))
        with self.assertRaisesRegex(ValidationError, "symbolic links"):
            prepare_handoff("claude", [package], self.out, import_skill=installer)
        self.assertFalse(self.out.exists())

    def test_generic_target_receives_portable_guide_without_claiming_skill_installation(self):
        report = prepare_handoff("other", [self.package()], self.out)
        self.assertFalse((self.out / "memory-import.zip").exists())
        self.assertIn("Совместимость", Path(report["guide"]).read_text(encoding="utf-8"))
        self.assertEqual(report["receiver_capabilities"], "not_live_verified")

    def test_pro_uses_menu_route_and_real_files_without_claiming_native_support(self):
        source = self.write("work.md", "# Вымышленная работа\nСодержание работы.")
        report = prepare_handoff("gigaagent_pro", [source], self.out)
        guide = Path(report["guide"]).read_text(encoding="utf-8")
        self.assertIn("Amberkeep carry memory", guide)
        self.assertIn("Меню", guide)
        self.assertIn("не проверялось", guide)
        self.assertNotIn("memory-import.zip", guide)
        self.assertEqual((self.out / source.name).read_bytes(), source.read_bytes())
        self.assertEqual(report["files"][0]["validation"], "copied_without_native_format_claim")

    def test_pro_rejects_malformed_portable_zip_and_unsafe_zip_members(self):
        bad = self.root / "bad.zip"
        for members in ({"manifest.json": "{}"}, {"../escaped.txt": "bad"}):
            with zipfile.ZipFile(bad, "w") as archive:
                for name, data in members.items():
                    archive.writestr(name, data)
            with self.assertRaises(ValidationError):
                prepare_handoff("gigaagent_pro", [bad], self.out)
        self.assertFalse(self.out.exists())

    def test_output_is_never_overwritten(self):
        self.out.mkdir()
        sentinel = self.out / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "already exists"):
            prepare_handoff("gigaagent", [self.profile()], self.out, instruction=self.instruction)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(self.out.iterdir()), [sentinel])

    def test_delivery_reserved_and_case_colliding_names_are_rejected(self):
        for name in ("NEXT_STEPS.md", "import-instruction.md", "handoff-manifest.json"):
            with self.subTest(name=name), self.assertRaisesRegex(ValidationError, "Reserved or colliding"):
                prepare_handoff("gigaagent_pro", [self.write(name, "actual data")], self.out)
        a = self.write("a/Profile.md", "one")
        b = self.write("b/PROFILE.MD", "two")
        with self.assertRaisesRegex(ValidationError, "Reserved or colliding"):
            prepare_handoff("gigaagent_pro", [a, b], self.out)
        self.assertFalse(self.out.exists())

    def test_missing_or_empty_source_never_creates_output(self):
        for files in ([], [self.root / "missing.md"], [self.root], [self.write("empty.md", "")]):
            with self.subTest(files=files), self.assertRaises(ValidationError):
                prepare_handoff("gigaagent_pro", files, self.out)
            self.assertFalse(self.out.exists())

    def test_manifest_lists_only_actual_files_with_hashes(self):
        report = prepare_handoff("gigaagent_pro", [self.profile()], self.out)
        manifest = json.loads(Path(report["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual({item["name"] for item in manifest["files"]},
                         {path.name for path in self.out.iterdir()} - {"handoff-manifest.json"})
        self.assertEqual(manifest["native_memory"], "not_written")

    def test_valid_portable_archive_may_exceed_individual_file_limit(self):
        package = self.package()
        installer = self.installer()
        self.assertGreater(package.stat().st_size, 256)
        with patch("portable_memory.completion.MAX_FILE", 256):
            report = prepare_handoff("claude", [package], self.out, import_skill=installer)
        self.assertEqual(report["files"][0]["validation"], "package_and_common_profile_review_checked")
        self.assertEqual((self.out / package.name).read_bytes(), package.read_bytes())

    def test_data_archive_budget_excludes_installer_and_generated_artifacts(self):
        package, installer = self.package(), self.installer()
        size = package.stat().st_size
        # The real valid export fits exactly the mocked whole-archive budget.
        # Installer/guide/manifest are bounded separately, not charged to it.
        with patch("portable_memory.completion.MAX_TOTAL", size - 128), \
                patch("portable_memory.completion.ARCHIVE_OVERHEAD", 128):
            report = prepare_handoff("claude", [package], self.out, import_skill=installer)
        self.assertGreater(sum(path.stat().st_size for path in self.out.iterdir()), size)
        self.assertEqual(report["native_memory"], "not_written")

    def test_data_archive_budget_still_rejects_oversize_or_aggregate_inputs(self):
        package = self.package()
        size = package.stat().st_size
        with patch("portable_memory.completion.MAX_TOTAL", size - 129), \
                patch("portable_memory.completion.ARCHIVE_OVERHEAD", 128):
            with self.assertRaisesRegex(ValidationError, "Source file is too large"):
                prepare_handoff("other", [package], self.out)
        second = self.root / "second.zip"
        second.write_bytes(package.read_bytes())
        with patch("portable_memory.completion.MAX_TOTAL", size - 128), \
                patch("portable_memory.completion.ARCHIVE_OVERHEAD", 128):
            with self.assertRaisesRegex(ValidationError, "Handoff data files are too large"):
                prepare_handoff("other", [package, second], self.out)
        self.assertFalse(self.out.exists())

    def test_opaque_files_keep_individual_file_limit(self):
        source = self.write("notes.md", "a" * 129)
        with patch("portable_memory.completion.MAX_FILE", 128):
            with self.assertRaisesRegex(ValidationError, "Source file is too large"):
                prepare_handoff("gigaagent_pro", [source], self.out)
        self.assertFalse(self.out.exists())

    def test_pro_portable_archive_uses_portable_limits_including_manifest(self):
        package = self.package()
        # An opaque archive accepts at most this many members; portable packages
        # include their manifest and are validated by package.read_package.
        with patch("portable_memory.completion.MAX_FILES", 5), \
                patch("portable_memory.completion.MAX_FILE", 256):
            report = prepare_handoff("gigaagent_pro", [package], self.out)
        self.assertEqual(report["files"][0]["kind"], "portable_memory")

    def test_installer_original_zip_names_cannot_hide_nul_suffixes(self):
        installer = self.installer(extra=("memory-import/safeXsuffix.txt", "extra"))
        data = installer.read_bytes().replace(b"safeXsuffix.txt", b"safe\x00suffix.txt")
        installer.write_bytes(data)
        with self.assertRaisesRegex(ValidationError, "Unsafe filename"):
            preflight_handoff("claude", self.out, import_skill=installer)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
