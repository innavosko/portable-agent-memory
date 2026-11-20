import copy
import unittest

from portable_memory.errors import ValidationError
from portable_memory.storage import (create_route, normalize_location,
                                     provider_catalog, route_view, validate_route)


def verified(provider="google_drive", *, resource=None, read=False, write=False):
    namespace = {"local": "filesystem", "chat_upload": "chat"}.get(provider, provider)
    return {"provider": provider, "label": "Chosen folder", "resource_id": resource or f"{namespace}::owner-123::folder-456",
            "access": "verified", "capabilities": {"read": read, "write": write},
            "evidence": "Synthetic fixture: the corresponding read/write tool operation succeeded for this resource"}


class StorageTests(unittest.TestCase):
    def test_catalog_contains_choices_not_connectors_and_is_immutable(self):
        catalog = provider_catalog()
        self.assertEqual({item["id"] for item in catalog}, {"google_drive", "yandex_disk", "mail_cloud", "sberdisk", "onedrive", "dropbox", "other", "local", "chat_upload"})
        self.assertTrue(all(set(item) == {"id", "label", "url", "note"} for item in catalog))
        self.assertTrue(all(item["url"] is None for item in catalog if item["id"] in {"other", "local", "chat_upload"}))
        catalog[0]["id"] = "mutated"
        self.assertEqual(provider_catalog()[0]["id"], "google_drive")

    def test_selecting_service_does_not_create_capabilities(self):
        place = normalize_location({"provider": "google_drive"})
        self.assertEqual(place["access"], "unknown")
        self.assertEqual(place["capabilities"], {"read": False, "write": False})
        self.assertIsNone(place["resource_id"])
        self.assertIsNone(place["evidence"])
        self.assertEqual(normalize_location(place), place)

    def test_manual_route_between_different_clouds(self):
        route = create_route({"sender": {"provider": "google_drive", "access": "manual"},
                              "recipient": {"provider": "yandex_disk", "access": "manual"}, "copy_mode": "user_copy"})
        view = route_view(route)
        self.assertFalse(view["same_provider"])
        self.assertEqual(view["status"], "manual_transfer_ready")
        self.assertEqual(view["delivery_status"], "not_started")
        self.assertTrue(view["package_creation_allowed"])
        self.assertFalse(view["automatic_transfer"])
        self.assertFalse(view["sender_can_write"])

    def test_unknown_access_and_missing_recipient_do_not_block_package(self):
        route = create_route({"sender": {"provider": "mail_cloud"}, "copy_mode": "user_copy"})
        view = route_view(route)
        self.assertEqual(view["status"], "recipient_not_chosen")
        self.assertTrue(view["package_creation_allowed"])
        self.assertEqual({item["reason"] for item in view["pending"]}, {"choice_pending", "access_pending"})

    def test_same_provider_does_not_prove_shared_access(self):
        config = {"sender": {"provider": "onedrive"}, "recipient": {"provider": "onedrive"}, "copy_mode": "shared_folder"}
        with self.assertRaises(ValidationError):
            create_route(config)
        config["copy_mode"] = "user_copy"
        view = route_view(create_route(config))
        self.assertTrue(view["same_provider"])
        self.assertFalse(view["same_resource"])
        self.assertEqual(view["status"], "access_pending")

    def test_shared_folder_requires_both_separate_verified_directions(self):
        config = {"sender": verified(write=True), "recipient": verified(read=True), "copy_mode": "shared_folder"}
        route = create_route(config)
        view = route_view(route)
        self.assertEqual(view["status"], "shared_folder_ready")
        self.assertTrue(view["same_resource"])
        self.assertTrue(view["sender_can_write"])
        self.assertTrue(view["recipient_can_read"])
        self.assertEqual(view["delivery_status"], "not_started")
        for side, capability in (("sender", "write"), ("recipient", "read")):
            broken = copy.deepcopy(config)
            broken[side]["capabilities"][capability] = False
            with self.subTest(side=side), self.assertRaises(ValidationError):
                create_route(broken)

    def test_different_owners_or_resources_cannot_be_shared_by_title(self):
        for resource in ("google_drive::another-owner::folder-456", "google_drive::owner-123::another-folder"):
            with self.subTest(resource=resource), self.assertRaises(ValidationError):
                create_route({"sender": verified(write=True), "recipient": verified(resource=resource, read=True), "copy_mode": "shared_folder"})

    def test_different_providers_cannot_be_shared_even_with_same_resource_text(self):
        with self.assertRaises(ValidationError):
            create_route({"sender": verified(write=True), "recipient": verified("yandex_disk", read=True), "copy_mode": "shared_folder"})

    def test_verified_requires_evidence_and_qualified_identity(self):
        for mutation in ({"resource_id": None}, {"resource_id": "folder-456"}, {"evidence": None}, {"evidence": ""},
                         {"resource_id": "google_drive::::folder"}, {"resource_id": "onedrive::owner::folder"}):
            with self.subTest(mutation=mutation):
                place = verified(write=True)
                place.update(mutation)
                with self.assertRaises(ValidationError):
                    normalize_location(place)

    def test_official_homepage_does_not_prove_resource_or_access(self):
        place = verified(write=True)
        place["evidence"] = "https://www.google.com/intx/en/drive/"
        with self.assertRaises(ValidationError):
            normalize_location(place)
        place = verified(write=True)
        place["resource_id"] = "https://drive.google.com/"
        with self.assertRaises(ValidationError):
            normalize_location(place)

    def test_signed_urls_and_credentials_are_rejected(self):
        dangerous = ["https://example.invalid/file?X-Amz-Signature=abc", "https://example.invalid/file#access_token=abc",
                     "https://user:password@example.invalid/file", "Bearer abcdef", "access_token=abcdef",
                     "password: secret-value", "https%3A%2F%2Fexample.invalid%2Ffile%3Fsig%3Dabc"]
        for value in dangerous:
            for field in ("label", "resource_id", "evidence"):
                with self.subTest(value=value, field=field):
                    place = {"provider": "other", "label": "Custom service", field: value}
                    with self.assertRaises(ValidationError):
                        normalize_location(place)

    def test_unknown_service_stays_other_and_rambler_is_not_a_storage_alias(self):
        custom = normalize_location({"provider": "other", "label": "Сервис, который пользователь назвал Рамблером"})
        self.assertEqual(custom["provider"], "other")
        self.assertEqual(custom["access"], "unknown")
        self.assertIsNone(custom["resource_id"])
        with self.assertRaises(ValidationError):
            normalize_location({"provider": "rambler", "label": "Рамблер"})
        with self.assertRaises(ValidationError):
            normalize_location({"provider": "other"})

    def test_local_and_chat_manual_choices_and_no_shared_chat(self):
        route = create_route({"sender": {"provider": "local", "access": "manual"},
                              "recipient": {"provider": "chat_upload", "access": "manual"}, "copy_mode": "user_copy"})
        self.assertEqual(route_view(route)["status"], "manual_transfer_ready")
        with self.assertRaises(ValidationError):
            create_route({"sender": verified("chat_upload", write=True), "recipient": verified("chat_upload", read=True), "copy_mode": "shared_folder"})

    def test_same_verified_local_folder_can_be_shared(self):
        route = create_route({"sender": verified("local", write=True), "recipient": verified("local", read=True), "copy_mode": "shared_folder"})
        self.assertTrue(route_view(route)["same_resource"])

    def test_manual_or_unknown_cannot_claim_agent_permissions(self):
        for access in ("manual", "unknown"):
            with self.subTest(access=access), self.assertRaises(ValidationError):
                normalize_location({"provider": "dropbox", "access": access, "capabilities": {"read": True}})

    def test_strict_shapes_types_and_tampering(self):
        for place in ({"provider": "dropbox", "capabilities": {"read": 1}},
                      {"provider": "dropbox", "capabilities": {"send": False}},
                      {"provider": "dropbox", "password": "secret"}):
            with self.subTest(place=place), self.assertRaises(ValidationError):
                normalize_location(place)
        route = create_route({"sender": {"provider": "dropbox"}, "copy_mode": "user_copy"})
        validate_route(route)
        route["schema_version"] = True
        with self.assertRaises(ValidationError):
            validate_route(route)
        route["schema_version"] = 1
        route["sender"]["capabilities"]["write"] = True
        with self.assertRaises(ValidationError):
            route_view(route)

    def test_inputs_are_not_mutated(self):
        config = {"sender": verified(write=True), "recipient": verified(read=True), "copy_mode": "shared_folder"}
        before = copy.deepcopy(config)
        route = create_route(config)
        snapshot = copy.deepcopy(route)
        view = route_view(route)
        view["sender"]["label"] = "Changed display"
        self.assertEqual(config, before)
        self.assertEqual(route, snapshot)


if __name__ == "__main__":
    unittest.main()
