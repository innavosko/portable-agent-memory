import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from portable_memory.archives import ingest_archive
from portable_memory.errors import ValidationError


def chatgpt():
    return {"id": "conversation-one", "title": "Проект и правила", "current_node": "b", "mapping": {
        "root": {"parent": None, "message": None},
        "a": {"parent": "root", "message": {"id": "ma", "author": {"role": "user"}, "content": {"content_type": "text", "parts": ["Моя почта: test@example.invalid"]}}},
        "b": {"parent": "a", "message": {"id": "mb", "author": {"role": "assistant"}, "content": {"parts": ["Первый ответ"]}}},
        "c": {"parent": "a", "message": {"id": "mc", "author": {"role": "assistant"}, "content": {"parts": ["Другой ответ", {"content_type": "image_asset_pointer", "asset_pointer": "file-service://fake"}]}}},
    }}


def claude():
    return {"uuid": "claude-one", "name": "Работа", "chat_messages": [
        {"uuid": "c1", "sender": "human", "text": "Привет", "content": [{"type": "text", "text": "Привет"}], "attachments": [{"file_name": "work.pdf"}]},
        {"uuid": "c2", "sender": "assistant", "text": "Ответ", "content": []},
    ]}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "выгрузка"

    def write_json(self, data, name="export.json"):
        path = self.root / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")
        return path

    def make_zip(self, members):
        path = self.root / "export.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in members:
                info = zipfile.ZipInfo(name)
                info.filename = name  # Preserve hostile spellings on Windows too.
                archive.writestr(info, content if isinstance(content, (str, bytes)) else json.dumps(content))
        return path

    def data(self):
        return json.loads((self.output / "conversations.json").read_text(encoding="utf-8"))["conversations"]

    def test_chatgpt_preserves_all_branches_and_raw(self):
        raw = chatgpt()
        report = ingest_archive(self.write_json([raw]), self.output)
        self.assertEqual(report["message_count"], 3)
        self.assertFalse(report["profile_extracted"])
        data = self.data()[0]
        self.assertEqual(data["raw"], raw)
        self.assertEqual([m["on_current_branch"] for m in data["messages"]], [True, True, False])
        self.assertEqual(data["messages"][2]["parent_id"], "a")
        self.assertIn("Другой ответ", data["messages"][2]["text"])
        self.assertTrue(any("non-text" in warning for warning in report["warnings"]))
        markdown = next((self.output / "conversations").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("> Моя почта", markdown)

    def test_claude_attachments_and_duplicate_text(self):
        report = ingest_archive(self.write_json({"conversations": [claude()]}), self.output)
        messages = self.data()[0]["messages"]
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["text"], "Привет")
        self.assertEqual(messages[1]["text"], "Ответ")
        self.assertTrue(any("attachment" in warning for warning in report["warnings"]))
        self.assertFalse(report["attachments_fetched"])

    def test_multipart_zip_and_unprocessed_inventory(self):
        source = self.make_zip([("account/conversations-1.json", [chatgpt()]), ("account/conversations-2.json", [claude()]), ("file.pdf", b"not a real PDF")])
        report = ingest_archive(source, self.output)
        self.assertEqual(report["conversation_count"], 2)
        self.assertEqual(report["providers"], ["chatgpt", "claude"])
        self.assertEqual(report["unprocessed_members"], ["file.pdf"])
        self.assertFalse((self.output / "file.pdf").exists())
        self.assertEqual(len(report["source_sha256"]), 64)

    def test_duplicate_conversation_ids_preserved(self):
        original, revised = chatgpt(), chatgpt()
        revised["title"] = "Revised"
        report = ingest_archive(self.write_json([original, revised]), self.output)
        self.assertEqual(report["conversation_count"], 2)
        self.assertEqual(len({item["id"] for item in self.data()}), 2)
        self.assertTrue(any("duplicate" in warning for warning in report["warnings"]))

    def test_unsafe_paths_rejected_even_when_not_json(self):
        for name in ["../escape.txt", "/absolute.txt", "C:/file.txt", "a\\b.txt", "a/../b.txt"]:
            with self.subTest(name=name):
                source = self.make_zip([("conversations.json", [chatgpt()]), (name, "x")])
                with self.assertRaises(ValidationError):
                    ingest_archive(source, self.output)
                self.assertFalse(self.output.exists())

    def test_case_duplicate_path_rejected(self):
        source = self.make_zip([("conversations.json", [chatgpt()]), ("CONVERSATIONS.JSON", [chatgpt()])])
        with self.assertRaises(ValidationError):
            ingest_archive(source, self.output)

    def test_symlink_rejected(self):
        path = self.root / "export.zip"
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("conversations.json", json.dumps([chatgpt()]))
            archive.writestr(info, "../../outside")
        with self.assertRaises(ValidationError):
            ingest_archive(path, self.output)

    def test_limits_checked_before_writing(self):
        source = self.write_json([chatgpt()])
        with self.assertRaises(ValidationError):
            ingest_archive(source, self.output, max_file_bytes=10)
        self.assertFalse(self.output.exists())
        source = self.make_zip([("conversations.json", [chatgpt()]), ("padding", "x" * 100)])
        with self.assertRaises(ValidationError):
            ingest_archive(source, self.output, max_members=1)

    def test_zip_expanded_limit(self):
        source = self.root / "export.zip"
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("conversations.json", json.dumps([chatgpt()]))
            archive.writestr("padding", "x" * 100_000)
        with self.assertRaises(ValidationError):
            ingest_archive(source, self.output, max_file_bytes=10_000, max_total_bytes=20_000)

    def test_unsupported_and_malformed_fail_without_partial_result(self):
        for data in [{"unknown": []}, ["bad"], [{"chat_messages": "bad"}], [{"mapping": []}], [], [{"something": "else"}]]:
            with self.subTest(data=data):
                with self.assertRaises(ValidationError):
                    ingest_archive(self.write_json(data), self.output)
                self.assertFalse(self.output.exists())
        source = self.root / "bad.json"
        source.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValidationError):
            ingest_archive(source, self.output)

    def test_refuse_overwrite(self):
        source = self.write_json([claude()])
        ingest_archive(source, self.output)
        before = (self.output / "conversations.json").read_bytes()
        with self.assertRaises(ValidationError):
            ingest_archive(source, self.output)
        self.assertEqual(before, (self.output / "conversations.json").read_bytes())

    def test_cyclic_branch_warns_without_hang(self):
        value = chatgpt()
        value["mapping"]["a"]["parent"] = "b"
        report = ingest_archive(self.write_json(value), self.output)
        self.assertTrue(any("cyclic" in warning for warning in report["warnings"]))
        self.assertEqual(report["message_count"], 3)


if __name__ == "__main__":
    unittest.main()
