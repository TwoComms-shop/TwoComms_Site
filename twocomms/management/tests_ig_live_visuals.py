from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from management.services import instagram_bot as bot


class LegacyPresenceBoundaryTests(SimpleTestCase):
    """Transport/race coverage lives in tests_ig_presence; retain send fences."""

    def test_cleanup_scheduling_precedes_send_without_a_transport_wait(self):
        events = []
        with patch.object(bot, "_stop_typing_indicator", side_effect=lambda *_: events.append("stop")):
            marker = bot._mark_sending_after_typing_off(None, None, True, lambda: events.append("marker"))
            result = bot._send_with_typing_off(None, None, False, lambda: events.append("send") or "sent")
        self.assertIsNone(marker)
        self.assertEqual(result, "sent")
        self.assertEqual(events, ["stop", "marker", "stop", "send"])

    @patch.object(bot, "_stop_typing_indicator")
    @patch.object(bot, "_renew_client_automation_lease", return_value=False)
    def test_lease_loss_cancels_presence(self, _lease, stop):
        state = bot._wait_for_typing_window(None, None, "lease", None, "reply", typing_started_at=None)
        self.assertEqual(state, "lease_lost")
        stop.assert_called_once()

    @patch.object(bot, "_stop_typing_indicator")
    @patch.object(bot, "_renew_client_automation_lease", return_value=True)
    @patch.object(bot, "_reply_permission_is_current", return_value=False)
    def test_permission_loss_cancels_presence_without_delay(self, _permission, _lease, stop):
        with patch.object(bot.time, "sleep") as sleep:
            state = bot._wait_for_typing_window(None, None, "lease", None, "reply", typing_started_at=100)
        self.assertEqual(state, "permission_denied")
        stop.assert_called_once()
        sleep.assert_not_called()


class TypingPermissionTransitionTests(TestCase):
    def setUp(self):
        from django.utils import timezone

        self.settings = bot.InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.ai_enabled = False
        self.settings.trigger_text = "hello"
        self.settings.reply_text = "A reply"
        self.settings.save(update_fields=[
            "is_enabled", "ai_enabled", "trigger_text", "reply_text",
        ])
        self.row = bot.InstagramBotMessage.objects.create(
            sender_id="permission-transition-customer",
            role=bot.InstagramBotMessage.Role.USER,
            text="hello",
            status=bot.InstagramBotMessage.Status.PROCESSING,
            processing_started_at=timezone.now(),
        )

    @patch(
        "management.services.instagram_bot.send_text",
    )
    @patch(
        "management.services.instagram_bot.gemini_generate",
        return_value="A reply",
    )
    @patch(
        "management.services.instagram_bot._reply_permission_is_current",
        return_value=False,
    )
    @patch("management.services.instagram_bot.time.sleep")
    @patch("management.services.instagram_bot.send_sender_action")
    def test_permission_change_at_final_boundary_finishes_claim_without_send(
        self, sender_action, _sleep, _permission, _gemini, send_text
    ):
        sender_action.side_effect = lambda _settings, _sender_id, action: (
            bot.SenderActionResult(True, 200, "delivered", action)
        )
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])

        result = bot._process_one_inside_reply_boundary(
            self.settings,
            self.row,
            lease_token="",
            permission=SimpleNamespace(settings_epoch=1, client_epoch=None),
        )

        self.assertFalse(result)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, bot.InstagramBotMessage.Status.DONE)
        self.assertIsNotNone(self.row.processed_at)
        send_text.assert_not_called()

    @patch("management.services.instagram_bot.send_text")
    @patch("management.services.instagram_bot._wait_for_typing_window")
    def test_lease_loss_keeps_lease_helpers_requeue_result(self, wait, send_text):
        def lose_lease(*_args, **_kwargs):
            bot._requeue_for_active_lease(self.row)
            return "lease_lost"

        wait.side_effect = lose_lease

        result = bot._process_one_inside_reply_boundary(
            self.settings,
            self.row,
            lease_token="",
            permission=None,
        )

        self.assertFalse(result)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, bot.InstagramBotMessage.Status.PENDING)
        self.assertIsNone(self.row.processing_started_at)
        send_text.assert_not_called()
