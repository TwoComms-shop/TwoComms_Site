"""Тести Phase 1 / Task 5 — мульти-тип витяг вкладень + рекламний referral.

Головний фікс: раніше ловився ЛИШЕ attachments[].type=='image', тож пересланий
пост/reels/відповідь на сторіс відкидались і бот їх не «бачив». Тепер беремо
share/ig_reel/story_mention/story/video/file payload.url + reply_to.story.url,
а referral з реклами зберігаємо в картку клієнта.
"""
import json

from django.test import SimpleTestCase, TestCase

from management.models import IgClient, InstagramBotMessage, InstagramBotSettings
from management.services import instagram_bot as bot


class ExtractMediaUrlsTests(SimpleTestCase):
    def test_share_attachment(self):
        msg = {"attachments": [{"type": "share", "payload": {"url": "https://cdn/p.jpg"}}]}
        self.assertEqual(bot._extract_media_urls(msg), ["https://cdn/p.jpg"])

    def test_ig_reel(self):
        msg = {"attachments": [{"type": "ig_reel", "payload": {"url": "https://cdn/r.jpg"}}]}
        self.assertEqual(bot._extract_media_urls(msg), ["https://cdn/r.jpg"])

    def test_story_mention(self):
        msg = {"attachments": [{"type": "story_mention", "payload": {"url": "https://cdn/sm.jpg"}}]}
        self.assertEqual(bot._extract_media_urls(msg), ["https://cdn/sm.jpg"])

    def test_story_reply(self):
        msg = {"reply_to": {"story": {"url": "https://cdn/s.jpg", "id": "1"}}}
        self.assertEqual(bot._extract_media_urls(msg), ["https://cdn/s.jpg"])

    def test_plain_image_still_works(self):
        msg = {"attachments": [{"type": "image", "payload": {"url": "https://cdn/i.jpg"}}]}
        self.assertEqual(bot._extract_media_urls(msg), ["https://cdn/i.jpg"])

    def test_dedupe_and_cap_three(self):
        msg = {
            "attachments": [
                {"type": "image", "payload": {"url": "u1"}},
                {"type": "image", "payload": {"url": "u1"}},  # дубль
                {"type": "image", "payload": {"url": "u2"}},
                {"type": "image", "payload": {"url": "u3"}},
                {"type": "image", "payload": {"url": "u4"}},  # понад ліміт
            ]
        }
        self.assertEqual(bot._extract_media_urls(msg), ["u1", "u2", "u3"])

    def test_empty_when_no_media(self):
        self.assertEqual(bot._extract_media_urls({"text": "привіт"}), [])


class WebhookShapeSafetyTests(SimpleTestCase):
    def test_iter_events_ignores_non_object_envelopes(self):
        payload = {"entry": ["extension", {"messaging": "not-a-list", "changes": "bad"}]}
        self.assertEqual(list(bot._iter_events(payload)), [])

    def test_summary_counts_ignored_valid_event_kinds_and_unknown_fields(self):
        payload = {
            "entry": [{
                "messaging": [
                    {"sender": {"id": "u1"}, "postback": {"title": "start"}, "future": 1},
                    {"sender": {"id": "u1"}, "reaction": {"emoji": "❤️"}},
                    {"sender": {"id": "u1"}, "message": {"mid": "m1", "text": "hi"}},
                ],
                "changes": [{"field": "message_reactions", "value": {}}, {"field": "future_field", "value": {}}],
            }]
        }
        summary = bot._webhook_observation_summary(payload)
        self.assertIn("message=1", summary)
        self.assertIn("postback=1", summary)
        self.assertIn("reaction=2", summary)
        self.assertIn("unknown_change=1", summary)
        self.assertIn("unknown_fields=1", summary)


class ApplyReferralTests(TestCase):
    def test_referral_written_to_client(self):
        ref = {
            "ref": "summer", "ad_id": "999", "source": "ADS",
            "ads_context_data": {"ad_title": "Hoodie Kharkiv", "photo_url": "https://cdn/ad.jpg"},
        }
        bot._apply_referral("u1", ref)
        c = IgClient.objects.get(igsid="u1")
        self.assertEqual(c.ad_id, "999")
        self.assertEqual(c.ad_source, "ADS")
        self.assertEqual(c.ad_title, "Hoodie Kharkiv")
        self.assertEqual(c.ad_creative_url, "https://cdn/ad.jpg")
        self.assertEqual(c.referral_payload.get("ref"), "summer")


class HandleWebhookPayloadTests(TestCase):
    def setUp(self):
        s = InstagramBotSettings.load()
        s.is_enabled = True
        s.allowed_senders = ""  # дозволяємо всім (для тесту)
        s.save()
        self.s = s

    def test_enqueues_shared_post_with_media(self):
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "u9"},
            "message": {"mid": "mm1", "attachments": [
                {"type": "share", "payload": {"url": "https://cdn/post.jpg"}}
            ]},
        }]}]}
        n = bot.handle_webhook_payload(self.s, payload)
        self.assertEqual(n, 1)
        msg = InstagramBotMessage.objects.get(mid="mm1")
        self.assertEqual(json.loads(msg.attachments), ["https://cdn/post.jpg"])

    def test_skips_echo(self):
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "u9"},
            "message": {"mid": "e1", "is_echo": True, "text": "manager"},
        }]}]}
        self.assertEqual(bot.handle_webhook_payload(self.s, payload), 0)

    def test_referral_stored_on_contact(self):
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "u9"},
            "message": {"mid": "r1", "text": "скільки?"},
            "referral": {"ref": "x", "ad_id": "42", "source": "ADS",
                         "ads_context_data": {"ad_title": "Tee"}},
        }]}]}
        bot.handle_webhook_payload(self.s, payload)
        c = IgClient.objects.get(igsid="u9")
        self.assertEqual(c.ad_id, "42")
        self.assertEqual(c.ad_title, "Tee")
