"""Э2.11 — дублирующее медиа-вложение с новой подписью URL даёт одну CRM-строку."""
from django.test import TestCase
from django.utils import timezone

from management.models import InstagramBotMessage, InstagramBotSettings
from management.services import instagram_bot as bot


class SyntheticKeyIgnoresUrlSignatureTests(TestCase):
    """Подписанные media URL одноразовые — они не идентичность вложения."""

    def setUp(self):
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.ig_user_id = "brand-account"
        self.settings.save(update_fields=["is_enabled", "ig_user_id"])
        self.received_at = timezone.now()

    def _key(self, attachments, metadata=None):
        return bot._synthetic_inbound_event_key(
            sender_id="sender-1",
            text="ось фото",
            attachments=attachments,
            received_at=self.received_at,
            attachment_metadata=metadata,
        )

    def test_same_provider_object_id_gives_one_identity(self):
        first = self._key(
            ["https://cdn.example.com/m/1.jpg?sig=AAA"],
            [{"provider_object_id": "obj-42"}],
        )
        second = self._key(
            ["https://cdn.example.com/m/1.jpg?sig=BBB"],
            [{"provider_object_id": "obj-42"}],
        )
        self.assertEqual(first, second)

    def test_same_url_path_with_a_new_signature_gives_one_identity(self):
        first = self._key(["https://cdn.example.com/m/1.jpg?sig=AAA&exp=1"])
        second = self._key(["https://cdn.example.com/m/1.jpg?sig=BBB&exp=2"])
        self.assertEqual(first, second)

    def test_different_attachments_stay_different(self):
        first = self._key(["https://cdn.example.com/m/1.jpg?sig=AAA"])
        second = self._key(["https://cdn.example.com/m/2.jpg?sig=AAA"])
        self.assertNotEqual(first, second)

    def test_different_provider_objects_stay_different(self):
        first = self._key(["https://cdn.example.com/m/1.jpg"], [{"object_id": "a"}])
        second = self._key(["https://cdn.example.com/m/1.jpg"], [{"object_id": "b"}])
        self.assertNotEqual(first, second)

    def test_non_url_attachment_value_is_not_an_identity(self):
        # `_attachment_urls` пропускает только реальные URL, поэтому мусорное
        # значение не становится частью идентичности вложения.
        self.assertEqual(bot._stable_attachment_identity(["not-a-url"], None), ())

    def test_duplicate_signed_attachment_creates_one_pending_row(self):
        for signature in ("AAA", "BBB"):
            bot.enqueue_inbound(
                self.settings,
                sender_id="991234567890",
                text="ось фото",
                mid="",
                # Exercise the trusted webhook persistence adapter. Polling
                # requires its separately authenticated native source proof.
                source="webhook",
                attachments=[f"https://cdn.example.com/m/1.jpg?sig={signature}"],
                attachment_metadata=[{"provider_object_id": "obj-77"}],
                received_at=self.received_at,
                persistence_only=True,
            )

        rows = InstagramBotMessage.objects.filter(
            sender_id="991234567890", role=InstagramBotMessage.Role.USER
        )
        self.assertEqual(rows.count(), 1, "два подписи одного объекта — одна строка")
