"""New capture policy stamps are bound to actual verified local private bytes."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from management.models import IgClient, InstagramBotMessage
from management.services import ig_private_media as private
from management.services import instagram_bot as bot
from management.services.ig_media_recovery import prepared_blob_descriptor
from management.services.ig_private_media_lifecycle import RETENTION_POLICY_VERSION, project_private_media_lifecycle


class NewCaptureRetentionPolicyTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        os.chmod(root, 0o700)
        override = override_settings(IG_PRIVATE_MEDIA_ROOT=str(root))
        override.enable()
        self.addCleanup(override.disable)
        self.customer = IgClient.objects.create(igsid="capture-policy")

    def pending(self):
        url = "https://lookaside.fbsbx.com/verified.jpg"
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", media_capture_eligible=True, attachments=json.dumps([url]),
            attachment_media=[{"url": url, "status": "pending", "provenance": "live_webhook"}])

    def assert_bound_policy(self, row, item, expected):
        policy = item["retention_policy"]
        self.assertEqual(policy["version"], RETENTION_POLICY_VERSION)
        self.assertEqual(policy["retention_seconds"], expected)
        self.assertEqual(policy["source_part_id"], item["source_part_id"])
        self.assertEqual(policy["content_hash"], item["content_hash"])
        self.assertEqual(policy["delete_after"], item["delete_after"])
        self.assertEqual(datetime.fromisoformat(policy["delete_after"]) - datetime.fromisoformat(policy["captured_at"]), timedelta(seconds=expected))
        with private.private_media_storage().open(item["storage_name"], "rb") as handle:
            self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), policy["content_hash"])
        dto = project_private_media_lifecycle(item, message_state=row.private_media_state,
            message_delete_after=row.private_media_delete_after, owner_verified=True)
        self.assertEqual(dto["policy"]["state"], "verified")

    @override_settings(IG_PRIVATE_MEDIA_RETENTION_SECONDS=3 * 86400)
    def test_fresh_verified_capture_stores_one_bound_policy_snapshot(self):
        row = self.pending()
        with patch.object(bot, "download_image", return_value=("image/jpeg", b"verified-private-body")), patch("requests.post") as provider:
            items = bot._capture_message_media(row)
        row.refresh_from_db()
        self.assertEqual(items[0]["status"], "owned")
        self.assert_bound_policy(row, row.attachment_media[0], 3 * 86400)
        provider.assert_not_called()

    @override_settings(IG_PRIVATE_MEDIA_RETENTION_SECONDS=60 * 86400)
    def test_configuration_change_during_finish_cannot_relabel_captured_deadline(self):
        row = self.pending()
        finish = bot._finish_media_capture

        def changing_finish(*args, **kwargs):
            with override_settings(IG_PRIVATE_MEDIA_RETENTION_SECONDS=3600):
                return finish(*args, **kwargs)

        with patch.object(bot, "download_image", return_value=("image/jpeg", b"same-boundary-body")), patch.object(bot, "_finish_media_capture", side_effect=changing_finish):
            bot._capture_message_media(row)
        row.refresh_from_db()
        self.assert_bound_policy(row, row.attachment_media[0], 60 * 86400)

    @override_settings(IG_PRIVATE_MEDIA_RETENTION_SECONDS=3600)
    def test_prepared_verified_blob_recovery_uses_same_policy_owner_without_download(self):
        row = self.pending()
        token, item, use = bot._claim_media_capture(row.pk, row.attachment_media[0]["url"])
        descriptor = prepared_blob_descriptor(storage_name=f"prepared/{row.pk}.jpg", mime_type="image/jpeg", body_bytes=b"prepared-private")
        self.assertIsNotNone(bot._persist_prepared_media_blob(row.pk, item["source_part_id"], token, use, descriptor))
        storage = private.private_media_storage()
        storage.save(descriptor["storage_name"], ContentFile(b"prepared-private"))
        row.refresh_from_db()
        with patch.object(bot, "download_image") as download:
            state, _parts, _retry = bot._resume_prepared_media_blob(row, row.attachment_media[0],
                token=token, use_token=use, private_storage=storage)
        self.assertEqual(state, "finalized")
        download.assert_not_called()
        row.refresh_from_db()
        self.assert_bound_policy(row, row.attachment_media[0], 3600)

    def test_owned_legacy_capture_is_not_backfilled_or_extended_during_replay(self):
        row = self.pending()
        raw = b"legacy-private"
        name = private.private_media_storage().save(f"legacy/{row.pk}.jpg", ContentFile(raw))
        due = timezone.now() + timedelta(hours=2)
        row.private_media_state = "active"
        row.private_media_delete_after = due
        row.attachment_media = bot._normalize_message_media([{**row.attachment_media[0], "status": "owned",
            "private_storage": True, "storage_name": name, "mime": "image/jpeg", "bytes": len(raw),
            "content_hash": hashlib.sha256(raw).hexdigest(), "delete_after": due.isoformat()}], message_scope=row.pk)
        row.save(update_fields=["attachment_media", "private_media_state", "private_media_delete_after"])
        with patch.object(bot, "download_image") as download, patch.object(private, "current_private_media_retention_policy") as policy:
            bot._capture_message_media(row)
        row.refresh_from_db()
        self.assertNotIn("retention_policy", row.attachment_media[0])
        self.assertEqual(row.private_media_delete_after, due)
        download.assert_not_called()
        policy.assert_not_called()

    def test_incoming_metadata_cannot_replace_verified_capture_stamp_or_owned_hash(self):
        row = self.pending()
        with patch.object(bot, "download_image", return_value=("image/jpeg", b"merge-private")):
            bot._capture_message_media(row)
        row.refresh_from_db()
        original = deepcopy(row.attachment_media[0])
        incoming = {**original, "status": "pending", "capture_state": "discovered",
                    "content_hash": "c" * 64, "retention_policy": {"version": "untrusted", "retention_seconds": 90 * 86400}}
        merged = bot._merge_attachment_media(row.attachment_media, [incoming], message_scope=row.pk)
        self.assertEqual(merged[0]["retention_policy"], original["retention_policy"])
        self.assertEqual(merged[0]["content_hash"], original["content_hash"])
