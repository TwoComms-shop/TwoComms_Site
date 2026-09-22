import hashlib
import tempfile
from datetime import timedelta
from pathlib import Path

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

    def _message(self, *, erased=False, wrong_hash=False):
        client = IgClient.objects.create(
            igsid=f"preview-{InstagramBotMessage.objects.count()}",
            privacy_erasure_started_at=timezone.now() if erased else None,
        )
        source_part_id = "mp1_" + "a" * 32
        raw = b"\xff\xd8\xffprivate-preview"
        storage_name = private_media_storage().save(
            f"ig_message_media/preview/{client.pk}.jpg", ContentFile(raw),
        )
        row = InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            role=InstagramBotMessage.Role.USER,
            private_media_state=InstagramBotMessage.PrivateMediaState.ACTIVE,
            private_media_delete_after=timezone.now() + timedelta(hours=1),
            attachment_media=[{
                "source_part_id": source_part_id,
                "status": "owned",
                "private_storage": True,
                "storage_name": storage_name,
                "mime": "image/jpeg",
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
