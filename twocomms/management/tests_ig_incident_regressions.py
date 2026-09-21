import inspect
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from management.models import IgBotNotification, InstagramBotSettings
from management.services import instagram_bot as bot


class AlternateStoryEnvelopeTests(SimpleTestCase):
    def test_unproven_direct_story_change_shape_is_ignored(self):
        payload = {
            "entry": [{"changes": [{
                "field": "story_mentions",
                "value": {
                    "sender": {"id": "story-user"},
                    "timestamp": 1785000000000,
                    "mid": "story-change-mid",
                    "text": "дивіться сторіс",
                    "attachments": [{
                        "type": "story_mention",
                        "id": "story-object-1",
                        "payload": {"url": "https://cdn/story.jpg"},
                    }],
                },
            }]}]}
        events = list(bot._iter_events(payload))
        self.assertEqual(events, [])

    def test_story_change_without_message_identity_is_ignored(self):
        payload = {"entry": [{"changes": [{
            "field": "story_mentions",
            "value": {"sender": {"id": "story-user"}, "story_id": "s1"},
        }]}]}
        self.assertEqual(list(bot._iter_events(payload)), [])


class IncidentContractTests(SimpleTestCase):
    def test_detail_keeps_id_cursor_and_render_order_aligned(self):
        from management import bot_views

        source = inspect.getsource(bot_views.bot_client_detail_api)
        self.assertIn('c.messages.order_by("-id")[:300]', source)
        self.assertIn('c.messages.filter(id__lt=before_id).order_by("-id")[:100]', source)
        self.assertIn('c.messages.filter(id__gt=after_id).order_by("id")[:100]', source)

    def test_conversation_template_has_layout_settling_and_anchor_paths(self):
        from pathlib import Path

        template = Path(__file__).parent / "templates" / "management" / "bot.html"
        text = template.read_text()
        self.assertIn("scrollConversationToBottom", text)
        self.assertIn("preserveConversationAnchor", text)
        self.assertIn("document.documentElement.scrollHeight", text)


class ImmediateAlertThrottleTests(TestCase):
    def test_immediate_delivery_uses_flow_gate_and_defers_when_denied(self):
        with patch(
            "management.services.instagram_bot._deliver_manager_notification",
            return_value=True,
        ) as deliver, patch(
            "management.services.ig_alerts.throttle_gate",
            return_value=(False, 17),
        ) as gate:
            self.assertTrue(bot.notify_manager("bounded", dedupe_key="bounded-alert"))
        gate.assert_called_once()
        deliver.assert_not_called()
        row = IgBotNotification.objects.get(dedupe_key="bounded-alert")
        self.assertEqual(row.status, IgBotNotification.Status.PENDING)
        self.assertIsNotNone(row.next_attempt_at)
