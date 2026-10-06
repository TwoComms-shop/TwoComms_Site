import hashlib
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.models import AdminAuditLog, IgClient, InstagramBotMessage
from management.services.ig_private_media import private_media_storage


@override_settings(ROOT_URLCONF="twocomms.urls_management")
class PrivateMediaPreviewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "private-preview-admin", "preview@example.test", "x",
        )
        self.client.force_login(self.user)

    def _message(
        self, *, erased=False, wrong_hash=False, role=InstagramBotMessage.Role.USER,
        mime="image/jpeg", raw=b"\xff\xd8\xffprivate-preview", suffix="jpg",
    ):
        client = IgClient.objects.create(
            igsid=f"preview-{InstagramBotMessage.objects.count()}",
            privacy_erasure_started_at=timezone.now() if erased else None,
        )
        source_part_id = "mp1_" + "a" * 32
        storage_name = private_media_storage().save(
            f"ig_message_media/preview/{client.pk}.{suffix}", ContentFile(raw),
        )
        row = InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            role=role,
            private_media_state=InstagramBotMessage.PrivateMediaState.ACTIVE,
            private_media_delete_after=timezone.now() + timedelta(hours=1),
            attachment_media=[{
                "source_part_id": source_part_id,
                "status": "owned",
                "private_storage": True,
                "storage_name": storage_name,
                "mime": mime,
                "content_hash": "0" * 64 if wrong_hash else hashlib.sha256(raw).hexdigest(),
            }],
        )
        return row, source_part_id

    def test_authorized_preview_is_no_store(self):
        with tempfile.TemporaryDirectory() as root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(root).resolve()),
        ):
            row, part_id = self._message()
            response = self.client.get(reverse(
                "management_bot_private_media_preview", args=[row.pk, part_id],
            ))

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(AdminAuditLog.objects.filter(
            actor=self.user, action="ig_private_media.preview", entity_id=str(row.pk),
        ).exists())

    def test_authorized_video_preview_is_inline_and_bounded(self):
        with tempfile.TemporaryDirectory() as root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(root).resolve()),
        ):
            row, part_id = self._message(
                mime="video/mp4", raw=b"video-preview", suffix="mp4",
            )
            response = self.client.get(reverse(
                "management_bot_private_media_preview", args=[row.pk, part_id],
            ))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"].split(";", 1)[0], "video/mp4")

    def test_manager_echo_preview_is_authorized_and_compacted(self):
        with tempfile.TemporaryDirectory() as root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(root).resolve()),
        ):
            row, part_id = self._message(role=InstagramBotMessage.Role.MANAGER)
            response = self.client.get(reverse(
                "management_bot_private_media_preview", args=[row.pk, part_id],
            ))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"].split(";")[0], "image/jpeg")
        self.assertIn("no-store", response["Cache-Control"])

    def test_authorized_audio_preview_preserves_playable_content_type(self):
        with tempfile.TemporaryDirectory() as root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(root).resolve()),
        ):
            row, part_id = self._message(
                mime="audio/ogg", raw=b"OggSprivate-voice", suffix="ogg",
            )
            response = self.client.get(reverse(
                "management_bot_private_media_preview", args=[row.pk, part_id],
            ))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"].split(";", 1)[0], "audio/ogg")
        self.assertEqual(response.content, b"OggSprivate-voice")
        self.assertIn("no-store", response["Cache-Control"])

    def test_erasure_or_digest_change_makes_preview_unavailable(self):
        with tempfile.TemporaryDirectory() as root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(root).resolve()),
        ):
            for erased, wrong_hash in ((True, False), (False, True)):
                with self.subTest(erased=erased, wrong_hash=wrong_hash):
                    row, part_id = self._message(erased=erased, wrong_hash=wrong_hash)
                    response = self.client.get(reverse(
                        "management_bot_private_media_preview", args=[row.pk, part_id],
                    ))
                    self.assertEqual(response.status_code, 404)
                    self.assertIn("no-store", response["Cache-Control"])

    def _assert_expired_preview(self, *, source_deadline=None, part_deadline=None):
        from management import ig_private_media_views as views

        frozen = timezone.now()
        with tempfile.TemporaryDirectory() as root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(root).resolve()),
        ), patch.object(views.timezone, "now", return_value=frozen):
            row, part_id = self._message()
            if source_deadline is not None:
                row.private_media_delete_after = source_deadline(frozen)
            if part_deadline is not None:
                row.attachment_media[0]["delete_after"] = part_deadline(frozen)
            row.save(update_fields=["private_media_delete_after", "attachment_media"])
            with patch.object(views, "acquire_blob_use") as acquire, patch.object(views, "_read_current_bytes") as read:
                response = self.client.get(reverse(
                    "management_bot_private_media_preview", args=[row.pk, part_id],
                ))
            acquire.assert_not_called()
            read.assert_not_called()
            self.assertEqual(response.status_code, 404)
            self.assertIn("no-store", response["Cache-Control"])
            self.assertFalse(AdminAuditLog.objects.filter(
                action="ig_private_media.preview", entity_id=str(row.pk),
            ).exists())

    def test_expired_message_deadline_denies_before_lease_and_bytes(self):
        self._assert_expired_preview(source_deadline=lambda now: now)

    def test_expired_part_deadline_denies_before_lease_and_bytes(self):
        self._assert_expired_preview(part_deadline=lambda now: (now - timedelta(seconds=1)).isoformat())

    def test_malformed_or_naive_part_deadline_denies_before_bytes(self):
        for value in ("not-a-date", "2026-02-30T12:00:00+00:00", "2026-10-06T12:00:00", "", 123):
            with self.subTest(value=value):
                self._assert_expired_preview(part_deadline=lambda now, value=value: value)

    def test_malformed_or_naive_message_deadline_is_not_a_retention_proof(self):
        from management import ig_private_media_views as views

        frozen = timezone.now()
        with tempfile.TemporaryDirectory() as root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(root).resolve()),
        ), patch.object(views.timezone, "now", return_value=frozen):
            row, part_id = self._message()
            for value in (frozen.replace(tzinfo=None) + timedelta(hours=1),
                          "not-a-date", "2026-02-30T12:00:00+00:00", "", 123):
                with self.subTest(value=value):
                    row.private_media_delete_after = value
                    with self.assertRaises(views.PrivateMediaUnavailable):
                        views._safe_part(row, row.client, part_id, use_token=None)

    def test_future_part_deadline_preserves_authorized_preview(self):
        from management import ig_private_media_views as views

        frozen = timezone.now()
        with tempfile.TemporaryDirectory() as root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(root).resolve()),
        ), patch.object(views.timezone, "now", return_value=frozen):
            row, part_id = self._message()
            row.attachment_media[0]["delete_after"] = (frozen + timedelta(minutes=20)).isoformat()
            row.save(update_fields=["attachment_media"])
            response = self.client.get(reverse(
                "management_bot_private_media_preview", args=[row.pk, part_id],
            ))
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response["Cache-Control"])

    def test_deadline_expiring_while_waiting_is_rechecked_before_bytes(self):
        from management import ig_private_media_views as views

        for deadline_owner in ("message", "part"):
            with self.subTest(deadline_owner=deadline_owner), tempfile.TemporaryDirectory() as root, override_settings(
                IG_PRIVATE_MEDIA_ROOT=str(Path(root).resolve()),
            ):
                frozen = timezone.now()
                clock = {"now": frozen}
                acquire_original = views.acquire_blob_use
                with patch.object(views.timezone, "now", side_effect=lambda: clock["now"]):
                    row, part_id = self._message()
                    deadline = frozen + timedelta(seconds=20)
                    if deadline_owner == "message":
                        row.private_media_delete_after = deadline
                    else:
                        row.attachment_media[0]["delete_after"] = deadline.isoformat()
                    row.save(update_fields=["private_media_delete_after", "attachment_media"])

                    def delayed_acquire(message_id, *, seconds):
                        token = acquire_original(message_id, seconds=seconds)
                        clock["now"] = frozen + timedelta(seconds=30)
                        return token

                    with patch.object(views, "acquire_blob_use", side_effect=delayed_acquire), patch.object(views, "_read_current_bytes") as read:
                        response = self.client.get(reverse(
                            "management_bot_private_media_preview", args=[row.pk, part_id],
                        ))
                    read.assert_not_called()
                    self.assertEqual(response.status_code, 404)
                    self.assertIn("no-store", response["Cache-Control"])
                    row.refresh_from_db()
                    self.assertEqual(row.private_media_use_token, "")
                    self.assertIsNone(row.private_media_use_until)
                    self.assertFalse(AdminAuditLog.objects.filter(
                        action="ig_private_media.preview", entity_id=str(row.pk),
                    ).exists())
