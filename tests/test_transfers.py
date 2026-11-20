import copy
import unittest

from portable_memory.errors import ValidationError
from portable_memory.transfers import apply_event, create_transfer, next_actions, validate_transfer


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.caps = {
            "exporter": {"email_read": True, "drive_read": True, "download": True, "notify": True},
            "receiver": {"drive_read": True, "download": True, "notify": True},
        }
        self.card = create_transfer("exporter", "receiver", "email", "Export mailbox", "exporter", self.caps,
                                    transfer_id="transfer-example")
        self.counter = 0

    def event(self, kind, actor="exporter", **data):
        self.counter += 1
        return {"id": f"event-{self.counter}", "type": kind, "actor": actor, "data": data}

    def apply(self, kind, actor="exporter", **data):
        event = self.event(kind, actor, **data)
        self.card = apply_event(self.card, event, expected_revision=self.card["revision"])
        return event

    def ready(self):
        self.apply("access_checked", channel="email", available=True, evidence="Mailbox read succeeded")
        return self.apply("source_ready", evidence="Export completion message identified")

    def prepared(self):
        self.apply("package_ready", sha256="b" * 64, origin="reviewed_profile", evidence="Validated reviewed profile ZIP")

    def test_ready_email_is_not_download_or_import(self):
        self.ready()
        self.assertEqual(self.card["state"], "source_ready")
        self.assertIsNone(self.card["download_sha256"])
        self.assertIsNone(self.card["package_sha256"])
        self.assertIsNone(self.card["receipt"])
        self.apply("notified", evidence="Host returned notification receipt")
        self.assertEqual(self.card["state"], "source_ready")

    def test_only_one_watch_owner_and_notification(self):
        self.ready()
        with self.assertRaises(ValidationError):
            self.apply("notified", actor="receiver", evidence="Attempted duplicate notification")
        self.apply("notified", evidence="Host receipt one")
        with self.assertRaises(ValidationError):
            self.apply("notified", evidence="Host receipt two")

    def test_raw_source_retrieval_has_one_owner(self):
        self.ready()
        self.apply("access_checked", actor="receiver", channel="upload", available=True,
                   evidence="Recipient can read an uploaded source")
        with self.assertRaises(ValidationError):
            self.apply("downloaded", actor="receiver", evidence="Attempted second retrieval",
                       sha256="a" * 64, method="user_upload")
        actions = next_actions(self.card, "receiver")
        self.assertTrue(any("without a duplicate download" in action for action in actions))
        self.assertFalse(any(action.startswith("Retrieve") for action in actions))
        self.apply("handoff", owner="receiver", mode="manual", evidence="Exporter stopped retrieval and handed over latest card")
        self.apply("downloaded", actor="receiver", evidence="Uploaded source read after handoff",
                   sha256="a" * 64, method="user_upload")
        self.assertEqual(self.card["state"], "downloaded")

    def test_stale_revision_rejected_without_mutation(self):
        before = copy.deepcopy(self.card)
        event = self.event("access_checked", channel="email", available=True, evidence="Read succeeded")
        with self.assertRaises(ValidationError):
            apply_event(self.card, event, expected_revision=9)
        self.assertEqual(self.card, before)

    def test_exact_event_replay_is_noop_changed_replay_rejected(self):
        event = self.ready()
        replay = apply_event(self.card, event, expected_revision=1)
        self.assertEqual(replay, self.card)
        replay["state"] = "cancelled"
        self.assertNotEqual(replay, self.card)
        changed = copy.deepcopy(event)
        changed["data"]["evidence"] = "A different message"
        with self.assertRaises(ValidationError):
            apply_event(self.card, changed, expected_revision=self.card["revision"])

    def test_background_requires_runner_and_notification(self):
        with self.assertRaises(ValidationError):
            create_transfer("exporter", "receiver", "email", "Mailbox", "exporter", self.caps, mode="background")
        self.caps["exporter"]["background"] = True
        background = create_transfer("exporter", "receiver", "email", "Mailbox", "exporter", self.caps, mode="background")
        self.assertTrue(any("does not start" in action for action in next_actions(background, "exporter")))
        self.caps["exporter"]["notify"] = False
        with self.assertRaises(ValidationError):
            create_transfer("exporter", "receiver", "email", "Mailbox", "exporter", self.caps, mode="background")

    def test_receiver_cannot_inherit_source_connection(self):
        self.prepared()
        with self.assertRaises(ValidationError):
            self.apply("data_verified", actor="receiver", sha256="b" * 64, evidence="File checked")
        with self.assertRaises(ValidationError):
            self.apply("access_checked", actor="receiver", channel="email", available=True, evidence="Exporter had email")
        self.apply("access_checked", actor="receiver", channel="upload", available=True, evidence="Client uploaded file")
        with self.assertRaises(ValidationError):
            self.apply("data_verified", actor="receiver", sha256="c" * 64, evidence="Wrong file checked")
        self.apply("data_verified", actor="receiver", sha256="b" * 64, evidence="ZIP integrity check passed")
        self.assertEqual(self.card["state"], "data_verified")
        self.assertIsNone(self.card["receipt"])

    def test_handoff_is_explicit_owner_and_capability_checked(self):
        with self.assertRaises(ValidationError):
            self.apply("handoff", actor="receiver", owner="receiver", mode="manual", evidence="Take over")
        with self.assertRaises(ValidationError):
            self.apply("handoff", owner="receiver", mode="session", evidence="Receiver lacks email")
        self.apply("handoff", owner="receiver", mode="manual", evidence="Owner stopped and supplied latest card")
        self.assertEqual(self.card["owner"], "receiver")
        with self.assertRaises(ValidationError):
            self.apply("source_ready", evidence="Old watcher attempts update")
        self.apply("source_ready", actor="receiver", evidence="Client supplied readiness confirmation")

    def test_full_archive_flow_with_actual_result_type(self):
        self.ready()
        self.apply("downloaded", evidence="Retrieved archive", sha256="a" * 64, method="tool")
        self.apply("package_ready", evidence="Review complete, package assembled", sha256="b" * 64, origin="downloaded_source")
        self.apply("rerouted", destination_kind="drive", destination_locator="Shared prepared memory folder", evidence="Client selected shared Drive folder")
        self.apply("access_checked", actor="receiver", channel="drive", available=True, evidence="Receiver read the chosen folder")
        self.apply("data_verified", actor="receiver", evidence="Package validator succeeded", sha256="b" * 64)
        self.apply("imported", actor="receiver", receipt_kind="portable_workspace", evidence="Written memory.json and read back")
        self.assertEqual(self.card["state"], "imported")
        self.assertEqual(self.card["receipt"]["receipt_kind"], "portable_workspace")
        self.assertNotEqual(self.card["receipt"]["receipt_kind"], "native_memory")
        validate_transfer(self.card)

    def test_destination_route_has_independent_access(self):
        self.prepared()
        self.apply("access_checked", actor="receiver", channel="drive", available=True,
                   evidence="Receiver can read a Drive folder")
        with self.assertRaises(ValidationError):
            self.apply("data_verified", actor="receiver", sha256="b" * 64, evidence="Destination is still upload")
        self.apply("rerouted", actor="receiver", destination_kind="drive", destination_locator="Chosen package folder",
                   evidence="Client explicitly selected this shared folder")
        self.assertNotIn("receiver", self.card["access"])
        with self.assertRaises(ValidationError):
            self.apply("data_verified", actor="receiver", sha256="b" * 64, evidence="Previous access invalidated")
        self.apply("access_checked", actor="receiver", channel="drive", available=True,
                   evidence="Receiver read the exact new destination folder")
        self.apply("data_verified", actor="receiver", sha256="b" * 64, evidence="Retrieved package digest validated")
        with self.assertRaises(ValidationError):
            self.apply("rerouted", destination_kind="upload", destination_locator="A new attachment",
                       evidence="Too late to change verified destination")

    def test_destination_is_explicit_without_delivery_claim(self):
        card = create_transfer("exporter", "receiver", "email", "Mailbox", "exporter", self.caps,
                               destination_kind="drive", destination_locator="Memory package folder")
        self.assertEqual(card["source_kind"], "email")
        self.assertEqual(card["destination_kind"], "drive")
        self.card = card
        self.prepared()
        self.assertEqual(self.card["state"], "package_ready")
        self.assertIsNone(self.card["receipt"])
        self.assertTrue(any("does not confirm delivery" in action for action in next_actions(self.card, "receiver")))

    def test_cannot_import_or_claim_archive_before_download(self):
        self.ready()
        with self.assertRaises(ValidationError):
            self.apply("package_ready", evidence="Archive still on server", sha256="b" * 64, origin="downloaded_source")
        with self.assertRaises(ValidationError):
            self.apply("imported", actor="receiver", receipt_kind="native_memory", evidence="Premature")

    def test_manual_upload_requires_actual_upload_access(self):
        self.ready()
        with self.assertRaises(ValidationError):
            self.apply("downloaded", evidence="Missing attachment", sha256="a" * 64, method="user_upload")
        self.apply("access_checked", channel="upload", available=True, evidence="Client attached downloaded ZIP")
        self.apply("downloaded", evidence="Attachment read", sha256="a" * 64, method="user_upload")
        self.assertEqual(self.card["state"], "downloaded")

    def test_rejected_capability_and_secret_values(self):
        self.caps["exporter"]["background"] = "yes"
        with self.assertRaises(ValidationError):
            create_transfer("exporter", "receiver", "email", "Mailbox", "exporter", self.caps)
        self.caps["exporter"]["background"] = False
        for locator in ("https://example.test/download?signature=secret", "Bearer secret"):
            with self.assertRaises(ValidationError):
                create_transfer("exporter", "receiver", "email", locator, "exporter", self.caps)

    def test_other_agent_does_not_start_duplicate_watch(self):
        actions = next_actions(self.card, "receiver")
        self.assertTrue(any("do not start a duplicate" in action for action in actions))
        self.assertTrue(any("your own access" in action for action in actions))

    def test_malformed_json_values_raise_validation_errors(self):
        for actor in ([], {}, 7, None):
            event = self.event("source_ready", evidence="Invalid actor")
            event["actor"] = actor
            with self.assertRaises(ValidationError):
                apply_event(self.card, event, expected_revision=0)
        with self.assertRaises(ValidationError):
            self.apply("access_checked", channel=[], available=True, evidence="Invalid channel")
        with self.assertRaises(ValidationError):
            self.apply("handoff", owner=[], mode="manual", evidence="Invalid owner")


if __name__ == "__main__":
    unittest.main()
