import copy
import unittest

from portable_memory.errors import ValidationError
from portable_memory.sources import normalize_sources, resolve_identity, validate_source


def identity(namespace="openai", account="account-a", workspace="personal", resource="history"):
    return {"namespace": namespace, "account_id": account, "workspace_id": workspace, "resource_id": resource}


def observation(oid, *, product="ChatGPT", interface="web", dataset=None, kind="history", path="provided/history.zip", input_kind="archive", label="My history", evidence="Account and workspace identifiers confirmed by user", selected=True):
    return {"id": oid, "label": label, "kind": kind, "identity": dataset,
            "identity_evidence": evidence if dataset is not None else None,
            "surface": {"agent_id": f"{product}-{interface}", "product": product, "interface": interface},
            "selected": selected, "input": {"kind": input_kind, "path": path} if path is not None else None}


class SourcesTests(unittest.TestCase):
    def test_four_products_six_surfaces_are_four_datasets(self):
        values = [
            observation("openai-web", dataset=identity()),
            observation("openai-desktop", dataset=identity(), interface="desktop"),
            observation("claude-web", product="Claude", dataset=identity("anthropic")),
            observation("claude-desktop", product="Claude", dataset=identity("anthropic"), interface="desktop"),
            observation("giga-web", product="GigaAgent", dataset=identity("giga")),
            observation("gemini-web", product="Gemini", dataset=identity("google")),
        ]
        sources = normalize_sources(values)
        self.assertEqual(len(sources), 4)
        self.assertEqual(sorted(len(source["surfaces"]) for source in sources), [1, 1, 2, 2])
        self.assertEqual(sum(len(source["observation_ids"]) for source in sources), 6)
        self.assertTrue(all(source["collection_ready"] for source in sources))
        for source in sources:
            validate_source(source)

    def test_common_repo_seen_by_two_agents_is_one_dataset(self):
        shared = identity("filesystem", "owner-a", "device-123", "repo-canonical-42")
        values = [observation("cc-repo", product="Claude Code", interface="cli", kind="project", dataset=shared, input_kind="folder", path="C:/work/repo"),
                  observation("codex-repo", product="Codex", interface="desktop", kind="project", dataset=shared, input_kind="folder", path="C:/work/repo")]
        source = normalize_sources(values)[0]
        self.assertEqual(len(normalize_sources(values)), 1)
        self.assertEqual(len(source["surfaces"]), 2)
        self.assertEqual(len(source["inputs"]), 1)
        self.assertEqual(source["inputs"][0]["observation_ids"], ["cc-repo", "codex-repo"])

    def test_same_email_title_path_without_identity_does_not_merge(self):
        values = [observation("unknown-a", label="person@example.invalid", dataset=None),
                  observation("unknown-b", label="person@example.invalid", dataset=None)]
        sources = normalize_sources(values)
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(source["identity_status"] == "unresolved" for source in sources))
        self.assertTrue(all(not source["collection_ready"] for source in sources))
        self.assertTrue(all("identity_unresolved" in source["collection_blockers"] for source in sources))

    def test_partial_identity_and_no_evidence_are_unresolved(self):
        incomplete = observation("partial", dataset={"namespace": "openai", "account_id": "a"})
        unsupported = observation("no-evidence", dataset=identity(), evidence=None)
        sources = normalize_sources([incomplete, unsupported])
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(source["identity"] is None for source in sources))
        self.assertTrue(all(not source["collection_ready"] for source in sources))

    def test_account_workspace_kind_and_namespace_are_isolated(self):
        values = [observation("a", dataset=identity()),
                  observation("b", dataset=identity(account="other")),
                  observation("c", dataset=identity(workspace="team")),
                  observation("d", dataset=identity(), kind="profile", input_kind="memory"),
                  observation("e", dataset=identity(namespace="anthropic")),
                  observation("f", dataset=identity(resource="another-resource"))]
        self.assertEqual(len(normalize_sources(values)), 6)

    def test_exact_re_registration_order_and_label_changes_are_stable(self):
        one = observation("one", dataset=identity())
        two = observation("two", dataset=identity(), interface="desktop")
        baseline = normalize_sources([one, two])
        self.assertEqual(baseline, normalize_sources([two, one, one, two]))
        renamed = copy.deepcopy(one)
        renamed["label"] = "A nicer title"
        changed = normalize_sources([renamed, two])
        self.assertEqual(changed[0]["id"], baseline[0]["id"])
        unresolved = observation("unknown")
        renamed_unknown = copy.deepcopy(unresolved)
        renamed_unknown["label"] = "Another label"
        self.assertEqual(normalize_sources([unresolved])[0]["id"], normalize_sources([renamed_unknown])[0]["id"])

    def test_repeated_observation_conflicting_identity_is_rejected(self):
        one = observation("same-id", dataset=identity())
        conflicting = copy.deepcopy(one)
        conflicting["identity"]["workspace_id"] = "other-workspace"
        with self.assertRaises(ValidationError):
            normalize_sources([one, conflicting])
        conflicting = copy.deepcopy(one)
        conflicting["kind"] = "profile"
        with self.assertRaises(ValidationError):
            normalize_sources([one, conflicting])

    def test_different_inputs_are_candidates_not_arbitrarily_selected(self):
        one = observation("one", dataset=identity(), path="first.zip")
        two = observation("two", dataset=identity(), path="second.zip", interface="desktop")
        source = normalize_sources([one, two])[0]
        self.assertEqual(source["input_status"], "choice_required")
        self.assertIsNone(source["input"])
        self.assertEqual({item["path"] for item in source["inputs"]}, {"first.zip", "second.zip"})
        self.assertFalse(source["collection_ready"])

    def test_changed_input_for_same_observation_is_preserved(self):
        one = observation("one", dataset=identity(), path="first.zip")
        changed = copy.deepcopy(one)
        changed["input"]["path"] = "second.zip"
        source = normalize_sources([one, changed])[0]
        self.assertEqual(source["observation_ids"], ["one"])
        self.assertEqual(len(source["observations"]), 2)
        self.assertEqual(len(source["inputs"]), 2)

    def test_missing_input_and_unselected_source_cannot_collect(self):
        missing = normalize_sources([observation("missing", dataset=identity(), path=None)])[0]
        self.assertEqual(missing["input_status"], "missing")
        self.assertFalse(missing["collection_ready"])
        not_selected = normalize_sources([observation("no", dataset=identity(), selected=False)])[0]
        self.assertFalse(not_selected["collection_ready"])
        self.assertIn("not_selected", not_selected["collection_blockers"])

    def test_resolve_merges_with_known_dataset_preserving_observations(self):
        known = observation("known", dataset=identity(), path="provided.zip")
        unknown = observation("unknown", dataset=None, interface="desktop", path="provided.zip")
        sources = normalize_sources([known, unknown])
        unresolved = next(source for source in sources if source["identity_status"] == "unresolved")
        result = resolve_identity(sources, unresolved["id"], identity(), "User confirmed the same account and workspace")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["observation_ids"], ["known", "unknown"])
        self.assertEqual(len(result[0]["surfaces"]), 2)
        self.assertTrue(result[0]["collection_ready"])
        self.assertEqual(result[0]["id"], normalize_sources([known])[0]["id"])
        self.assertEqual(result, resolve_identity(result, result[0]["id"], identity(), "Confirmed again"))

    def test_resolving_with_different_inputs_still_requires_choice(self):
        sources = normalize_sources([observation("known", dataset=identity(), path="one.zip"),
                                     observation("unknown", path="two.zip")])
        unresolved = next(source for source in sources if source["identity_status"] == "unresolved")
        result = resolve_identity(sources, unresolved["id"], identity(), "Confirmed identifiers")
        self.assertEqual(result[0]["input_status"], "choice_required")
        self.assertFalse(result[0]["collection_ready"])

    def test_resolution_requires_evidence_and_cannot_reassign_known_identity(self):
        sources = normalize_sources([observation("known", dataset=identity())])
        with self.assertRaises(ValidationError):
            resolve_identity(sources, sources[0]["id"], identity(), "")
        with self.assertRaises(ValidationError):
            resolve_identity(sources, sources[0]["id"], identity(account="another"), "New claim")
        partial_sources = normalize_sources([observation("partial", dataset={"namespace": "openai"})])
        with self.assertRaises(ValidationError):
            resolve_identity(partial_sources, partial_sources[0]["id"], identity(namespace="anthropic"), "Conflicting namespace")

    def test_validate_rejects_forged_readiness_or_id(self):
        source = normalize_sources([observation("unknown")])[0]
        validate_source(source)
        forged = copy.deepcopy(source)
        forged["collection_ready"] = True
        with self.assertRaises(ValidationError):
            validate_source(forged)
        forged = copy.deepcopy(source)
        forged["id"] = "some-other-id"
        with self.assertRaises(ValidationError):
            validate_source(forged)

    def test_no_caller_data_is_mutated(self):
        observations = [observation("unknown")]
        before = copy.deepcopy(observations)
        sources = normalize_sources(observations)
        sources_before = copy.deepcopy(sources)
        resolve_identity(sources, sources[0]["id"], identity(), "Clarified")
        self.assertEqual(observations, before)
        self.assertEqual(sources, sources_before)

    def test_folder_includes_are_preserved_and_equivalent_selections_deduplicate(self):
        one = observation("one", dataset=identity("filesystem"), input_kind="folder", path="C:/repo", kind="project")
        one["input"]["include"] = ["assets\\result.xlsx", "CONTEXT.md"]
        two = copy.deepcopy(one)
        two["id"] = "two"
        two["input"]["include"] = ["CONTEXT.md", "assets/result.xlsx", "CONTEXT.md"]
        source = normalize_sources([one, two])[0]
        self.assertEqual(source["input"]["include"], ["CONTEXT.md", "assets/result.xlsx"])
        self.assertEqual(len(source["inputs"]), 1)
        validate_source(source)
        two["input"]["include"] = ["OTHER.md"]
        source = normalize_sources([one, two])[0]
        self.assertEqual(source["input_status"], "choice_required")
        self.assertEqual(len(source["inputs"]), 2)

    def test_include_is_explicit_and_cannot_escape_folder(self):
        value = observation("folder", dataset=identity("filesystem"), input_kind="folder", path="C:/repo", kind="project")
        value["input"]["include"] = []
        self.assertEqual(normalize_sources([value])[0]["input"]["include"], [])
        for include in [None, ["../private"], ["/absolute"], ["C:/elsewhere"], ["."]]:
            with self.subTest(include=include):
                value["input"]["include"] = include
                with self.assertRaises(ValidationError):
                    normalize_sources([value])
        value = observation("archive", dataset=identity())
        value["input"]["include"] = ["a.txt"]
        with self.assertRaises(ValidationError):
            normalize_sources([value])


if __name__ == "__main__":
    unittest.main()
