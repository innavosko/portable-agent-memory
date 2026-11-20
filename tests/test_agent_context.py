"""Synthetic context only: exact values, scoped links and honest source coverage."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portable_memory import agent_context as context
from portable_memory.errors import ValidationError
from portable_memory.model import validate_memory


class AgentContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = {"platform": "dots", "account_id": "synthetic-owner", "workspace_id": "synthetic-workspace",
                       "observed_at": "2026-11-20T10:00:00Z", "evidence": "Fictional host returned these selected context sections."}

    def item(self, sid="one", kind="rule", value="  Исходный текст.\n\nНе сокращать: А → Б.\n", **extra):
        result = {"id": sid, "title": "Одинаковое название", "kind": kind, "scope": "global",
                  "value": value, "source_ref": "host:context:" + sid}
        result.update(extra)
        return result

    def category(self, cid, items=None, status="provided", **extra):
        items = list(items or [])
        result = {"id": cid, "status": status, "evidence": "Host returned the specified source/check result.",
                  "source_refs": list(dict.fromkeys(item["source_ref"] for item in items)) or ["host:check:" + cid],
                  "items": items, "excluded_by_user": status == "excluded"}
        result.update(extra)
        return result

    def snapshot(self, *categories):
        return {"format": "portable-agent-context", "schema_version": 1, "source": copy.deepcopy(self.source),
                "categories": list(categories)}

    def collect(self, snapshot, name="result"):
        folder = self.root / name
        report = context.collect_context(snapshot, folder, accepted=True)
        memory = json.loads((folder / "memory.json").read_text(encoding="utf-8"))
        return report, memory, folder

    def test_template_has_all_fixed_categories_pending_and_never_assumes_platform_access(self):
        for platform in ("dots", "duet"):
            source = {k: v for k, v in self.source.items() if k != "platform"}
            original = copy.deepcopy(source)
            template = context.context_template(platform, source)
            self.assertEqual(source, original)
            self.assertEqual(template["source"]["platform"], platform)
            self.assertEqual([category["id"] for category in template["categories"]], list(context.CATEGORIES))
            self.assertEqual(len(template["categories"]), 15)
            self.assertTrue(all(category["status"] == "pending" and not category["items"] for category in template["categories"]))
        with self.assertRaises(ValidationError):
            context.context_template("duet", self.source)
        with self.assertRaises(ValidationError):
            context.context_template("invented-platform", self.source)

    def test_missing_categories_remain_pending_and_source_snapshot_is_not_filled_in(self):
        snapshot = self.snapshot(self.category("agent_instructions", [self.item()]))
        untouched = copy.deepcopy(snapshot)
        report, memory, folder = self.collect(snapshot)
        self.assertEqual(snapshot, untouched)
        self.assertEqual(json.loads((folder / "assets/context/source.json").read_text(encoding="utf-8")), snapshot)
        self.assertEqual(len(report["categories"]), 15)
        self.assertEqual(report["summary"]["provided"], 1)
        self.assertEqual(report["summary"]["pending"], 14)
        self.assertEqual(len(report["unmet_categories"]), 14)
        self.assertFalse(report["summary"]["all_categories_assessed"])
        self.assertFalse(report["summary"]["complete_account_export"])
        self.assertTrue(all(category["supplied"] == (category["id"] == "agent_instructions") for category in report["categories"]))
        self.assertEqual(memory["records"][0]["value"], self.item()["value"])

    def test_exact_instruction_values_whitespace_and_structured_rules_are_preserved(self):
        rule = self.item("account-rule", value="Строка 1.\r\n  Строка 2.\n\nНе выполнять print('source only').\n")
        structured = self.item("agent-rule", value={"condition": "если совещание", "calendar_rules": ["показать проект", "не менять без согласования"],
                                                   "format": {"headings": 12, "body": 10}, "enabled": False, "exceptions": []})
        snapshot = self.snapshot(self.category("account_instructions", [rule]), self.category("agent_rules", [structured]))
        report, memory, folder = self.collect(snapshot)
        validate_memory(memory)
        records = {record["source_item_id"]: record for record in memory["records"]}
        for item in (rule, structured):
            record = records[item["id"]]
            self.assertEqual(record["value"], item["value"])
            self.assertEqual(record["scope"], item["scope"])
            self.assertEqual(record["review"], {"decision": "pending"})
            self.assertEqual(record["currency"], "unknown")
            self.assertEqual(record["provenance"], "source")
            self.assertEqual(record["sources"][0]["locator"], item["source_ref"])
        self.assertEqual(report["summary"]["user_review_pending"], 2)
        self.assertTrue((folder / "context-report.md").is_file())
        self.assertFalse((folder / ".incomplete").exists())

    def test_project_rules_require_explicit_project_scope_and_link_only_to_actual_project(self):
        project = self.item("project-1", "project", {"context": "Исследование", "file_refs": ["drive:file-1"]}, scope="project:project-1")
        rule = self.item("instructions-1", project_id="project-1", scope="project:project-1", value="Заголовки только этого проекта — 20 пт.")
        other = self.item("instructions-2", project_id="project-2", scope="project:project-2", value="Не применять правила другого проекта.")
        report, memory, _ = self.collect(self.snapshot(self.category("project_instructions", [rule, other]),
                                                       self.category("projects", [project])))
        records = {record["source_item_id"]: record for record in memory["records"]}
        self.assertEqual(records["instructions-1"]["links"], [{"target": records["project-1"]["id"], "relation": "belongs_to", "basis": "source"}])
        self.assertEqual(records["instructions-1"]["source_project_id"], "project-1")
        self.assertEqual(records["instructions-2"]["links"], [])
        self.assertEqual(report["unresolved_projects"][0]["source_project_id"], "project-2")
        for bad in ({k: v for k, v in rule.items() if k != "project_id"}, {**rule, "scope": "global"},
                    {**rule, "scope": "project:wrong-project"}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                context.validate_context(self.snapshot(self.category("project_instructions", [bad])))

    def test_two_same_named_projects_keep_separate_canonical_links(self):
        projects = [self.item(sid, "project", "Original project context", scope="project:" + sid) for sid in ("a", "b")]
        rules = [self.item("rule-" + sid, project_id=sid, scope="project:" + sid) for sid in ("a", "b")]
        _, memory, _ = self.collect(self.snapshot(self.category("projects", projects), self.category("project_instructions", rules)))
        records = {record["source_item_id"]: record for record in memory["records"]}
        self.assertEqual(records["rule-a"]["links"][0]["target"], records["a"]["id"])
        self.assertEqual(records["rule-b"]["links"][0]["target"], records["b"]["id"])
        self.assertNotEqual(records["a"]["id"], records["b"]["id"])

    def test_responsibilities_tasks_channels_and_accounts_preserve_actual_details(self):
        data = {
            "responsibilities": ("workflow", {"assigned": "Готовить сводку заявок", "rules": ["отменённые не считать"]}),
            "active_tasks": ("workflow", {"task": "Проверить договор", "status": "in_progress", "deadline": "2026-12-01"}),
            "recurring_tasks": ("automation", {"schedule": "Каждый понедельник 09:30", "timezone": "Europe/Moscow", "status": "paused", "destination": "team-channel:1"}),
            "integrations": ("integration", {"account": "owner@example.invalid", "scopes": ["calendar.read"], "rules": ["Не переносить события без согласования"]}),
            "channels_and_destinations": ("resource", {"channel": "team-channel:1", "destination": "drive:folder-9", "delivery_rule": "Приложить PDF"}),
        }
        snapshot = self.snapshot(*(self.category(cid, [self.item(cid, kind, value)]) for cid, (kind, value) in data.items()))
        report, memory, _ = self.collect(snapshot)
        for record in memory["records"]:
            self.assertEqual(record["value"], data[record["source_category"]][1])
            self.assertEqual(record["review"]["decision"], "pending")
        self.assertEqual(report["summary"]["records"], 5)
        self.assertFalse(report["summary"]["complete_account_export"])
        self.assertTrue(any("not restored" in text for text in report["limitations"]))

    def test_skill_declarations_and_file_references_do_not_claim_original_files(self):
        categories = [self.category("skills", [self.item("skill-1", "skill", {"name": "Existing", "enabled": False, "originals": "not supplied"})]),
                      self.category("artifacts", [self.item("artifact-1", "artifact", {"url": "https://example.invalid/original", "name": "Result"})]),
                      self.category("conversations", [self.item("conversation-1", "resource", {"messages": [{"role": "user", "text": "Оригинальный текст"}]})])]
        report, memory, folder = self.collect(self.snapshot(*categories))
        self.assertEqual({r["kind"] for r in memory["records"]}, {"skill", "artifact", "resource"})
        self.assertEqual(sorted(p.relative_to(folder / "assets").as_posix() for p in (folder / "assets").rglob("*") if p.is_file()), ["context/source.json"])
        self.assertTrue(any("do not include original skill files" in text for text in report["limitations"]))

    def test_coverage_unavailable_not_found_excluded_are_distinct_and_need_evidence(self):
        snapshot = self.snapshot(self.category("saved_memory", status="unavailable"),
                                 self.category("agent_notes", status="not_found"),
                                 self.category("conversations", status="excluded", evidence="User explicitly chose not to transfer conversations", source_refs=[]))
        report, memory, _ = self.collect(snapshot)
        self.assertEqual(memory["records"], [])
        self.assertEqual(report["summary"]["unavailable"], 1)
        self.assertEqual(report["summary"]["not_found"], 1)
        self.assertEqual(report["summary"]["excluded"], 1)
        self.assertIn("saved_memory", report["unmet_categories"])
        self.assertNotIn("agent_notes", report["unmet_categories"])
        self.assertNotIn("conversations", report["unmet_categories"])
        for status in ("provided", "unavailable", "not_found", "excluded"):
            items = [self.item()] if status == "provided" else []
            bad = self.category("agent_instructions", items, status=status, evidence=None)
            with self.subTest(status=status), self.assertRaises(ValidationError):
                context.validate_context(self.snapshot(bad))
        for status in ("unavailable", "not_found"):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                context.validate_context(self.snapshot(self.category("saved_memory", status=status, source_refs=[])))

    def test_exclusion_requires_user_choice_and_cannot_carry_hidden_items_into_original(self):
        for category in (self.category("agent_notes", status="excluded", excluded_by_user=False),
                         self.category("agent_notes", [self.item(kind="idea")], status="excluded"),
                         self.category("agent_notes", [self.item(kind="idea")], excluded_by_user=True)):
            with self.subTest(category=category), self.assertRaises(ValidationError):
                context.validate_context(self.snapshot(category))

    def test_all_categories_observed_does_not_claim_complete_account_or_restoration(self):
        categories = [self.category(cid, status="not_found") for cid in context.CATEGORIES]
        report, memory, _ = self.collect(self.snapshot(*categories))
        self.assertTrue(report["summary"]["all_categories_assessed"])
        self.assertTrue(report["summary"]["all_requested_categories_available"])
        self.assertFalse(report["summary"]["complete_account_export"])
        self.assertEqual(report["summary"]["records"], 0)
        self.assertEqual(report["unmet_categories"], [])
        self.assertEqual(len(memory["coverage"]["read"]), 15)

    def test_stable_ids_ignore_known_source_observation_and_content_changes(self):
        first = self.snapshot(self.category("agent_rules", [self.item()]))
        _, before, _ = self.collect(first, "first")
        changed = copy.deepcopy(first)
        changed["source"]["observed_at"] = "2027-01-01"
        changed["source"]["evidence"] = "New actual source read"
        changed["categories"][0]["items"][0]["value"] = "Changed original rule"
        _, after, _ = self.collect(changed, "after")
        self.assertEqual(before["records"][0]["id"], after["records"][0]["id"])
        self.assertNotEqual(before["records"][0]["value"], after["records"][0]["value"])
        self.assertEqual(after["records"][0]["review"]["decision"], "pending")

    def test_source_account_workspace_and_platform_are_isolated_without_alias_inference(self):
        snapshot = self.snapshot(self.category("agent_rules", [self.item()]))
        _, original, _ = self.collect(snapshot, "first")
        original_id = original["records"][0]["id"]
        for index, (field, value) in enumerate((('account_id', 'different'), ('workspace_id', 'different'), ('platform', 'duet'))):
            changed = copy.deepcopy(snapshot)
            changed["source"][field] = value
            _, memory, _ = self.collect(changed, f"changed-{index}")
            self.assertNotEqual(original_id, memory["records"][0]["id"])

    def test_unknown_source_identity_uses_snapshot_identity_without_false_merging(self):
        snapshot = self.snapshot(self.category("agent_rules", [self.item()]))
        snapshot["source"]["account_id"] = None
        first_report, first, _ = self.collect(snapshot, "first")
        _, same, _ = self.collect(snapshot, "same")
        self.assertTrue(first_report["identity_unresolved"])
        self.assertEqual(first["records"][0]["id"], same["records"][0]["id"])
        changed = copy.deepcopy(snapshot)
        changed["source"]["evidence"] = "Another unknown source observation"
        _, other, _ = self.collect(changed, "other")
        self.assertNotEqual(first["records"][0]["id"], other["records"][0]["id"])

    def test_item_ids_can_be_opaque_platform_ids_and_same_ids_in_different_categories_remain_separate(self):
        item = self.item("source:opaque/id:1")
        report, memory, _ = self.collect(self.snapshot(self.category("agent_rules", [item]), self.category("agent_instructions", [item])))
        self.assertEqual(report["summary"]["records"], 2)
        self.assertNotEqual(memory["records"][0]["id"], memory["records"][1]["id"])
        self.assertEqual(memory["records"][0]["source_item_id"], item["id"])

    def test_item_sources_observation_dates_and_mapping_are_validated(self):
        item = self.item(observed_at=None)
        _, memory, _ = self.collect(self.snapshot(self.category("agent_rules", [item])))
        self.assertIsNone(memory["records"][0]["sources"][0]["observed_at"])
        for bad in ({**item, "source_ref": "not-in-category"}, {**item, "kind": "skill"}, {**item, "value": None},
                    {**item, "observed_at": 42}, {**item, "review": {"decision": "active"}}):
            category = self.category("agent_rules", [item])
            category["items"] = [bad]
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                context.validate_context(self.snapshot(category))

    def test_duplicate_categories_items_and_ambiguous_project_aliases_rejected(self):
        rule = self.category("agent_rules", [self.item()])
        cases = [self.snapshot(rule, rule), self.snapshot(self.category("agent_rules", [self.item(), self.item()])),
                 self.snapshot(self.category("projects", [self.item("one", "project", project_id="shared"),
                                                           self.item("two", "project", project_id="shared")]))]
        for snapshot in cases:
            with self.subTest(snapshot=snapshot), self.assertRaises(ValidationError):
                context.validate_context(snapshot)

    def test_credential_fields_rejected_without_stripping_valid_account_rules(self):
        for key in ("access_token", "refresh-token", "password", "api_key", "client_secret", "authorization"):
            snapshot = self.snapshot(self.category("integrations", [self.item("integration", "integration", {"account": "owner@example.invalid", key: "synthetic-secret"})]))
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.collect(snapshot, "must-not-exist")
        self.assertFalse((self.root / "must-not-exist").exists())
        valid = self.snapshot(self.category("integrations", [self.item("integration", "integration", {"account": "owner@example.invalid", "scopes": ["read"], "rules": ["Попросить подключить аккаунт заново"]})]))
        context.validate_context(valid)

    def test_explicit_acceptance_new_output_and_incomplete_marker(self):
        snapshot = self.snapshot(self.category("agent_rules", [self.item()]))
        for accepted in (False, 1, None):
            with self.subTest(accepted=accepted), self.assertRaises(ValidationError):
                context.collect_context(snapshot, self.root / "absent", accepted=accepted)
        self.assertFalse((self.root / "absent").exists())
        self.collect(snapshot)
        with self.assertRaises(ValidationError):
            self.collect(snapshot)
        original = context.write_new

        def fail_memory(path, data):
            if Path(path).name == "memory.json":
                raise OSError("synthetic interrupted write")
            return original(path, data)

        with patch.object(context, "write_new", side_effect=fail_memory), self.assertRaises(OSError):
            self.collect(snapshot, "partial")
        self.assertTrue((self.root / "partial/.incomplete").exists())
        with self.assertRaises(ValidationError):
            self.collect(snapshot, "partial")

    def test_input_item_count_and_output_limits_fail_before_creating_destination(self):
        snapshot = self.snapshot(self.category("agent_rules", [self.item()]))
        for field, value in (("MAX_INPUT_BYTES", 1), ("MAX_TOTAL_BYTES", 1), ("MAX_ITEMS", 0)):
            with self.subTest(field=field), patch.object(context, field, value), self.assertRaises(ValidationError):
                self.collect(snapshot, "limited")
            self.assertFalse((self.root / "limited").exists())


if __name__ == "__main__":
    unittest.main()
