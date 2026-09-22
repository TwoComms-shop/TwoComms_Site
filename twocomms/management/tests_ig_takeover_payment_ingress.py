from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from management.models import IgClient, InstagramBotMessage, InstagramBotSettings
from management.services import instagram_bot


class ManagerTakeoverPaymentIngressTests(TestCase):
    def setUp(self):
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["is_enabled", "ai_enabled"])
        self.client = IgClient.get_or_create_for_sender("takeover-payment-ingress")
        self.client.manager_takeover = True
        self.client.bot_paused = True
        self.client.paused_reason = "manager_takeover"
        self.client.save(update_fields=["manager_takeover", "bot_paused", "paused_reason", "updated_at"])
        self.row = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role=InstagramBotMessage.Role.USER,
            text="Я оплатила, ось чек",
            source="webhook",
            status=InstagramBotMessage.Status.PROCESSING,
            attachments='["https://example.invalid/receipt.jpg"]',
            media_capture_eligible=True,
            processing_started_at=timezone.now(),
        )

    @patch("management.services.instagram_bot._capture_message_media")
    @patch("management.services.instagram_bot._recover_current_message_media", return_value=[])
    @patch("management.services.bot_sales_classifier.classify_message")
    def test_takeover_suppresses_send_but_runs_customer_evidence_classification(
        self, classify_message, recover_media, capture_media
    ):
        result = instagram_bot._process_one_unlocked(self.settings, self.row)

        self.assertFalse(result)
        capture_media.assert_called_once_with(self.row)
        recover_media.assert_called_once_with(self.row)
        classify_message.assert_called_once()
        self.assertTrue(classify_message.call_args.kwargs["operational_effects"])
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, InstagramBotMessage.Status.DONE)
        self.assertIsNotNone(self.row.processed_at)

    @patch("management.services.instagram_bot._capture_message_media")
    @patch("management.services.instagram_bot._recover_current_message_media", return_value=[])
    @patch("management.services.bot_sales_classifier.classify_message")
    def test_takeover_observation_does_not_depend_on_paused_reason(
        self, classify_message, recover_media, capture_media
    ):
        self.client.paused_reason = ""
        self.client.save(update_fields=["paused_reason", "updated_at"])

        result = instagram_bot._process_one_unlocked(self.settings, self.row)

        self.assertFalse(result)
        capture_media.assert_called_once_with(self.row)
        recover_media.assert_called_once_with(self.row)
        classify_message.assert_called_once()

    @patch("management.services.instagram_bot._capture_message_media", side_effect=RuntimeError("storage unavailable"))
    @patch("management.services.instagram_bot._recover_current_message_media", return_value=[])
    @patch("management.services.bot_sales_classifier.classify_message")
    def test_media_capture_failure_does_not_drop_deterministic_projection(
        self, classify_message, recover_media, capture_media
    ):
        result = instagram_bot._process_one_unlocked(self.settings, self.row)

        self.assertFalse(result)
        capture_media.assert_called_once_with(self.row)
        recover_media.assert_called_once_with(self.row)
        classify_message.assert_called_once()
