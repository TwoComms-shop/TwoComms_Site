"""Тести Phase 0 / Task 1 — сире логування вебхуків Instagram-бота.

Мета: бачити реальний формат вхідних подій (шери постів, story_mention,
відповіді на сторис, рекламний referral, echo менеджера) на акаунті, а не
здогадуватись. record_raw_event() зберігає повний payload + витягнуті ознаки.
"""
import json

from django.test import TestCase

from management.models import InstagramBotRawEvent
from management.services import instagram_bot as bot


class RecordRawEventTests(TestCase):
    def test_extracts_share_attachment_and_referral(self):
        payload = {
            "entry": [{
                "messaging": [{
                    "sender": {"id": "777"},
                    "message": {
                        "attachments": [
                            {"type": "share", "payload": {"url": "https://cdn/x.jpg"}}
                        ]
                    },
                    "referral": {
                        "ref": "summer", "ad_id": "123", "source": "ADS",
                        "ads_context_data": {"ad_title": "Hoodie Kharkiv"},
                    },
                }]
            }]
        }
        ev = bot.record_raw_event(payload)
        self.assertEqual(ev.sender_id, "777")
        self.assertIn("share", ev.attachment_types)
        self.assertTrue(ev.has_referral)
        self.assertFalse(ev.has_echo)
        self.assertEqual(InstagramBotRawEvent.objects.count(), 1)
        self.assertEqual(
            json.loads(ev.payload)["entry"][0]["messaging"][0]["sender"]["id"], "777"
        )

    def test_detects_echo(self):
        payload = {
            "entry": [{
                "messaging": [{
                    "sender": {"id": "1"},
                    "message": {"is_echo": True, "text": "manager reply"},
                }]
            }]
        }
        ev = bot.record_raw_event(payload)
        self.assertTrue(ev.has_echo)
        self.assertFalse(ev.has_referral)
        self.assertEqual(ev.attachment_types, "")

    def test_changes_field_attachments(self):
        # IG інколи доставляє через entry[].changes[].value (а не messaging[]).
        payload = {
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {
                        "sender": {"id": "55"},
                        "message": {
                            "attachments": [
                                {"type": "story_mention", "payload": {"url": "u"}}
                            ]
                        },
                    },
                }]
            }]
        }
        ev = bot.record_raw_event(payload)
        self.assertEqual(ev.sender_id, "55")
        self.assertIn("story_mention", ev.attachment_types)

    def test_records_ignored_event_kinds_and_unknown_fields(self):
        payload = {
            "entry": [{
                "messaging": [
                    {"postback": {"title": "start"}, "future": True},
                    {"reaction": {"emoji": "❤️"}},
                    {"message": {"mid": "m1", "is_deleted": True}},
                ],
                "changes": [{"field": "future_field", "value": {}}],
            }]
        }
        ev = bot.record_raw_event(payload)
        self.assertIn("delete=1", ev.note)
        self.assertIn("postback=1", ev.note)
        self.assertIn("reaction=1", ev.note)
        self.assertIn("unknown_change=1", ev.note)
        self.assertIn("unknown_fields=1", ev.note)

    def test_keeps_rows_trimmed(self):
        # Не накопичуємо нескінченно — найстаріші підрізаються.
        for i in range(5):
            bot.record_raw_event({"entry": [{"messaging": [{"sender": {"id": str(i)}}]}]})
        # 5 подій збережено (ліміт значно більший)
        self.assertEqual(InstagramBotRawEvent.objects.count(), 5)
