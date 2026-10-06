"""Actual transcript DTO keeps private previews inside lifecycle/owner fences."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from management import bot_views
from management.models import IgClient, InstagramBotMessage
from management.services.ig_private_media_lifecycle import RETENTION_POLICY_VERSION


def owned_part(now, *, index=0, due=None):
    due = now + timedelta(hours=1) if due is None else due
    part = {"source_part_id": "mp1_" + ("a" if index == 0 else "c") * 32,
        "original_index": index, "content_hash": "b" * 64,
        "capture_state": "owned", "status": "owned", "private_storage": True,
        "storage_name": "ig_message_media/private-never-export/customer.jpg",
        "mime": "image/jpeg", "url": "https://lookaside.fbsbx.com/private?token=never-export",
        "delete_after": due.isoformat()}
    if due == now + timedelta(hours=1):
        part["retention_policy"] = {"version": RETENTION_POLICY_VERSION,
            "retention_seconds": 3600, "captured_at": now.isoformat(),
            "delete_after": due.isoformat(), "source_part_id": part["source_part_id"],
            "content_hash": part["content_hash"]}
    return part


class MediaLifecycleCallerGuardTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.message = SimpleNamespace(pk=7, attachment_media=[owned_part(self.now)],
            private_media_state="active", private_media_delete_after=None,
            turn_intelligence_artifact={}, attachments="", role="user", source="")

    def rows(self, **changes):
        args = {"owner_verified": True, "erasure_started": False, "now": self.now}
        args.update(changes)
        return bot_views._message_media_rows(self.message, [], **args)

    def assert_unreadable(self, rows):
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["preview_url"], "")
            self.assertEqual(row["public_url"], "")
            self.assertFalse(row["media_lifecycle"]["lifecycle"]["readable"])

    def test_future_owned_exact_due_allows_only_guarded_url_without_file_or_provider_reads(self):
        before = deepcopy(self.message.attachment_media)
        with patch("management.services.ig_private_media.private_media_storage") as storage:
            row = self.rows()[0]
        storage.assert_not_called()
        expected = reverse("management_bot_private_media_preview", args=[7, before[0]["source_part_id"]], urlconf="management.urls")
        self.assertEqual(row["preview_url"], expected)
        self.assertEqual(row["public_url"], expected)
        lifecycle = row["media_lifecycle"]["lifecycle"]
        self.assertEqual(lifecycle["deletion_due"], before[0]["delete_after"])
        self.assertEqual(lifecycle["policy"]["retention_seconds"], 3600)
        self.assertEqual(self.message.attachment_media, before)
        for private in (before[0]["storage_name"], before[0]["url"], "never-export"):
            self.assertNotIn(private, json.dumps(row))

    def test_exact_expiry_and_unknown_due_never_issue_url(self):
        self.assert_unreadable(self.rows(now=self.now + timedelta(hours=1)))
        for due in (None, "", "unknown", self.now.replace(tzinfo=None).isoformat()):
            self.message.attachment_media[0]["delete_after"] = due
            self.assert_unreadable(self.rows())

    def test_owner_erasure_and_default_caller_are_fail_closed(self):
        for kwargs in ({"owner_verified": False}, {"owner_verified": "true"}, {"erasure_started": True}):
            self.assert_unreadable(self.rows(**kwargs))
        self.assert_unreadable(bot_views._message_media_rows(self.message, []))

    def test_earliest_private_sibling_expires_entire_message_without_new_timer(self):
        sibling = owned_part(self.now, index=1, due=self.now)
        self.message.attachment_media.append(sibling)
        self.assert_unreadable(self.rows())
        self.assertEqual(self.rows()[0]["media_lifecycle"]["lifecycle"]["deletion_due"], self.now.isoformat())

    def test_delete_failed_retry_is_not_an_active_preview(self):
        self.message.private_media_state = "delete_failed"
        self.message.private_media_delete_after = self.now + timedelta(minutes=1)
        rows = self.rows()
        self.assert_unreadable(rows)
        lifecycle = rows[0]["media_lifecycle"]["lifecycle"]
        self.assertEqual(lifecycle["state"], "delete_failed")
        self.assertEqual(lifecycle["retry_due"], self.message.private_media_delete_after.isoformat())

    def test_outgoing_catalog_image_does_not_acquire_customer_retention_gate(self):
        self.message.attachment_media = []
        self.message.role = "model"; self.message.source = "catalog_media"
        self.message.attachments = json.dumps(["https://twocomms.shop/media/catalog/product.jpg"])
        rows = bot_views._message_media_rows(self.message, [])
        self.assertEqual(rows[0]["public_url"], "https://twocomms.shop/media/catalog/product.jpg")
        self.assertEqual(rows[0]["role"], "product")
        self.assertNotIn("media_lifecycle", rows[0])


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False)
class MediaLifecycleClientApiTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.admin = get_user_model().objects.create_superuser("media-dto-admin", "media@example.test", "x")
        self.client.force_login(self.admin)
        self.customer = IgClient.objects.create(igsid="private-media-customer", display_name="PRIVATE CUSTOMER NAME")
        self.message = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", text="PRIVATE customer text", mid="media-dto-source", status="done",
            private_media_state="active", attachment_media=[owned_part(self.now)])
        self.url = reverse("management_bot_client_detail_api", args=[self.customer.pk])

    def history_get(self):
        return self.client.get(self.url, {"before_id": self.message.pk + 1})

    def test_actual_scoped_history_get_passes_owner_clock_and_is_select_only(self):
        before = deepcopy(self.message.attachment_media)
        with patch.object(bot_views.timezone, "now", return_value=self.now), CaptureQueriesContext(connection) as queries, patch(
            "management.services.ig_private_media.private_media_storage") as storage, patch(
            "management.services.bot_memory.gemini_generate_text") as generation:
            response = self.history_get()
        self.assertEqual(response.status_code, 200)
        row = response.json()["messages"][0]["media"][0]
        self.assertTrue(row["media_lifecycle"]["lifecycle"]["readable"])
        self.assertTrue(row["preview_url"].startswith("/bot/private-media/"))
        self.assertEqual(row["public_url"], row["preview_url"])
        self.assertTrue(all(item["sql"].lstrip().upper().startswith("SELECT") for item in queries))
        storage.assert_not_called(); generation.assert_not_called()
        self.assertIn("no-store", response["Cache-Control"])
        self.message.refresh_from_db(); self.assertEqual(self.message.attachment_media, before)
        for private in (before[0]["storage_name"], before[0]["url"], "never-export"):
            self.assertNotIn(private, response.content.decode())

    def test_hidden_owner_denies_guarded_url_on_actual_history_get(self):
        self.customer.hidden_at = self.now
        self.customer.save(update_fields=["hidden_at"])
        response = self.history_get()
        self.assertEqual(response.status_code, 200)
        row = response.json()["messages"][0]["media"][0]
        self.assertEqual(row["preview_url"], "")
        self.assertEqual(row["public_url"], "")
        self.assertEqual(row["media_lifecycle"]["lifecycle"]["reason"], "owner_unverified")

    def test_expired_actual_history_does_not_leak_legacy_provider_attachment_fallback(self):
        provider_url = self.message.attachment_media[0]["url"]
        self.message.attachments = json.dumps([provider_url])
        # The captured attachment ages naturally; no acquisition proof or
        # expiry field is relaxed to make the fixture readable.
        self.message.save(update_fields=["attachments"])
        with patch.object(bot_views.timezone, "now", return_value=self.now + timedelta(hours=1)):
            response = self.history_get()
        self.assertEqual(response.status_code, 200)
        row = response.json()["messages"][0]["media"][0]
        self.assertEqual(row["media_lifecycle"]["lifecycle"]["state"], "expired")
        self.assertEqual(row["preview_url"], "")
        self.assertEqual(row["public_url"], "")
        for private in (provider_url, "lookaside.fbsbx.com", "never-export", "ig_message_media/private-never-export"):
            self.assertNotIn(private, response.content.decode())

    def test_erasing_get_returns_410_before_transcript_projection_without_private_values_or_producers(self):
        self.customer.privacy_erasure_started_at = self.now
        self.customer.save(update_fields=["privacy_erasure_started_at"])
        with CaptureQueriesContext(connection) as queries, patch.object(bot_views, "_message_media_rows") as project, patch(
            "management.services.ig_commerce_projection.captured_selection_for") as selection, patch(
            "management.services.bot_memory.gemini_generate_text") as generation:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json(), {"success": False, "code": "client_erasing"})
        self.assertIn("no-store", response["Cache-Control"])
        project.assert_not_called(); selection.assert_not_called(); generation.assert_not_called()
        self.assertTrue(all(item["sql"].lstrip().upper().startswith("SELECT") for item in queries))
        for private in ("PRIVATE", self.customer.igsid, "storage_name", "lookaside", "never-export", "messages"):
            self.assertNotIn(private, response.content.decode())
