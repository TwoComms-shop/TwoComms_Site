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

    def test_ig_post_repost_is_retained_as_media(self):
        msg = {"attachments": [{"type": "ig_post", "payload": {"url": "https://cdn/post.jpg"}}]}
        self.assertEqual(bot._extract_media_urls(msg), ["https://cdn/post.jpg"])

    def test_story_mention(self):
        msg = {"attachments": [{"type": "story_mention", "payload": {"url": "https://cdn/sm.jpg"}}]}
        self.assertEqual(bot._extract_media_urls(msg), ["https://cdn/sm.jpg"])

    def test_story_reply(self):
        msg = {"reply_to": {"story": {"url": "https://cdn/s.jpg", "id": "1"}}}
        self.assertEqual(bot._extract_media_urls(msg), ["https://cdn/s.jpg"])

    def test_story_permalink_is_review_context_not_capture_url(self):
        msg = {
            "reply_to": {
                "story": {
                    "url": "https://www.instagram.com/stories/example/123456/",
                    "id": "story-1",
                }
            }
        }
        self.assertEqual(bot._extract_media_urls(msg), [])
        metadata = bot._provider_attachment_metadata(msg)
        self.assertEqual(metadata[0]["media_type"], "story")
        self.assertEqual(metadata[0]["status"], bot.MEDIA_STATUS_METADATA_ONLY)
        self.assertEqual(metadata[0]["provenance"], bot.MEDIA_PROVENANCE_HISTORICAL)
        self.assertFalse(metadata[0].get("capture_eligible", True))

    def test_repost_permalink_is_review_context_not_capture_url(self):
        msg = {
            "attachments": [{
                "type": "ig_post",
                "payload": {"url": "https://www.instagram.com/p/ABC123/"},
            }],
        }
        self.assertEqual(bot._extract_media_urls(msg), [])
        metadata = bot._provider_attachment_metadata(msg)
        self.assertEqual(metadata[0]["media_type"], "ig_post")
        self.assertEqual(metadata[0]["status"], bot.MEDIA_STATUS_METADATA_ONLY)
        self.assertFalse(metadata[0].get("capture_eligible", True))

    def test_reel_permalink_is_review_context_not_capture_url(self):
        msg = {
            "attachments": [{
                "type": "ig_reel",
                "payload": {"url": "https://www.instagram.com/reel/XYZ789/"},
            }],
        }
        self.assertEqual(bot._extract_media_urls(msg), [])

    def test_plain_image_still_works(self):
        msg = {"attachments": [{"type": "image", "payload": {"url": "https://cdn/i.jpg"}}]}
        self.assertEqual(bot._extract_media_urls(msg), ["https://cdn/i.jpg"])

    def test_dedupe_and_cap_three(self):
        msg = {
            "attachments": [
                {"type": "image", "payload": {"url": "https://cdn/u1"}},
                {"type": "image", "payload": {"url": "https://cdn/u1"}},  # дубль
                {"type": "image", "payload": {"url": "https://cdn/u2"}},
                {"type": "image", "payload": {"url": "https://cdn/u3"}},
                {"type": "image", "payload": {"url": "https://cdn/u4"}},  # понад ліміт
            ]
        }
        self.assertEqual(
            bot._extract_media_urls(msg),
            ["https://cdn/u1", "https://cdn/u2", "https://cdn/u3"],
        )

    def test_empty_when_no_media(self):
        self.assertEqual(bot._extract_media_urls({"text": "привіт"}), [])

    def test_provider_story_provenance_is_preserved_separately_from_url(self):
        msg = {
            "mid": "story-mid",
            "attachments": [{
                "type": "story_mention",
                "id": "story-object-1",
                "payload": {
                    "url": "https://cdn/story.jpg",
                    "media_id": "media-1",
                    "target": {"username": "twocomms"},
                },
            }],
        }

        media = bot._provider_attachment_metadata(msg)

        self.assertEqual(media[0]["provider_object_key"], "story_mention:story-object-1")
        self.assertEqual(media[0]["provider_media_id"], "media-1")
        self.assertEqual(media[0]["target_username"], "twocomms")
        self.assertTrue(media[0]["provider_native_mention"])
        self.assertEqual(media[0]["provenance"], "live_webhook")

    def test_story_mention_requires_provider_mid_media_identity_and_explicit_brand_target(self):
        cases = {
            "missing_mid": {
                "attachments": [{
                    "type": "story_mention",
                    "id": "story-object-1",
                    "payload": {
                        "url": "https://cdn/story.jpg",
                        "media_id": "media-1",
                        "target": {"username": "twocomms"},
                    },
                }],
            },
            "missing_provider_media_identity": {
                "mid": "story-mid",
                "attachments": [{
                    "type": "story_mention",
                    "id": "arbitrary-object-id",
                    "payload": {
                        "url": "https://cdn/story.jpg",
                        "target": {"username": "twocomms"},
                    },
                }],
            },
            "arbitrary_payload_object_id": {
                "mid": "story-mid",
                "attachments": [{
                    "type": "story_mention",
                    "payload": {
                        "url": "https://cdn/story.jpg",
                        "id": "arbitrary-object-id",
                        "target": {"username": "twocomms"},
                    },
                }],
            },
        }

        for name, msg in cases.items():
            with self.subTest(name=name):
                media = bot._provider_attachment_metadata(msg)

                self.assertFalse(media[0]["provider_native_mention"])
                self.assertEqual(media[0]["target_username"], "")

    def test_story_mention_rejects_wrong_or_missing_target_and_attachment_username(self):
        base = {
            "mid": "story-mid",
            "attachments": [{
                "type": "story_mention",
                "id": "story-object-1",
                "payload": {
                    "url": "https://cdn/story.jpg",
                    "media_id": "media-1",
                },
            }],
        }
        cases = {
            "missing_target": base,
            "wrong_target": {
                **base,
                "attachments": [{
                    **base["attachments"][0],
                    "payload": {
                        **base["attachments"][0]["payload"],
                        "target": {"username": "another_brand"},
                    },
                }],
            },
            "attachment_only_username": {
                **base,
                "attachments": [{
                    **base["attachments"][0],
                    "username": "twocomms",
                }],
            },
        }

        for name, msg in cases.items():
            with self.subTest(name=name):
                media = bot._provider_attachment_metadata(msg)

                self.assertFalse(media[0]["provider_native_mention"])
                self.assertEqual(media[0]["target_username"], "")

    def test_generic_story_share_and_reply_are_never_native_mentions(self):
        messages = (
            {
                "mid": "story-mid",
                "attachments": [{
                    "type": "story",
                    "id": "story-object-1",
                    "payload": {
                        "url": "https://cdn/story.jpg",
                        "media_id": "media-1",
                        "target": {"username": "twocomms"},
                    },
                }],
            },
            {
                "mid": "share-mid",
                "attachments": [{
                    "type": "share",
                    "id": "shared-object-1",
                    "payload": {
                        "url": "https://cdn/shared.jpg",
                        "media_id": "media-1",
                        "target": {"username": "twocomms"},
                    },
                }],
            },
            {
                "mid": "reply-mid",
                "reply_to": {
                    "story": {
                        "id": "story-object-1",
                        "media_id": "media-1",
                        "url": "https://cdn/story.jpg",
                        "target": {"username": "twocomms"},
                    }
                },
            },
        )

        for msg in messages:
            with self.subTest(msg=msg):
                media = bot._provider_attachment_metadata(msg)

                self.assertFalse(media[0]["provider_native_mention"])
                self.assertEqual(media[0]["target_username"], "")

    def test_generic_share_fields_cannot_forge_provider_native_brand_mention(self):
        msg = {
            "mid": "share-mid",
            "attachments": [{
                "type": "share",
                "id": "shared-object-1",
                "username": "twocomms",
                "payload": {
                    "url": "https://cdn/shared.jpg",
                    "target": {"username": "twocomms"},
                },
            }],
        }

        media = bot._provider_attachment_metadata(msg)

        self.assertFalse(media[0]["provider_native_mention"])
        self.assertEqual(media[0]["target_username"], "")

    def test_provider_native_share_with_post_media_id_preserves_repost_provenance(self):
        """A Meta-native repost is eligible when its typed object identity is present."""
        msg = {
            "mid": "share-mid-native",
            "attachments": [{
                "type": "share",
                "payload": {
                    "url": "https://cdn/shared.jpg",
                    "ig_post_media_id": "post-media-1",
                    "target": {"username": "@TwoComms"},
                },
            }],
        }

        media = bot._provider_attachment_metadata(msg)

        self.assertTrue(media[0]["provider_native_mention"])
        self.assertEqual(media[0]["media_type"], "share")
        self.assertEqual(media[0]["provider_media_id"], "post-media-1")
        self.assertEqual(media[0]["provider_object_key"], "share:post-media-1")
        self.assertEqual(media[0]["target_username"], "twocomms")

    def test_story_mention_missing_target_cannot_be_inferred_from_event_type(self):
        msg = {
            "mid": "story-mid",
            "attachments": [{
                "type": "story_mention",
                "id": "story-object-1",
                "username": "attacker-controlled-name",
                "payload": {"url": "https://cdn/story.jpg"},
            }],
        }

        media = bot._provider_attachment_metadata(msg)

        self.assertFalse(media[0]["provider_native_mention"])
        self.assertEqual(media[0]["target_username"], "")

    def test_story_mention_without_provider_event_identity_is_not_native(self):
        msg = {
            "attachments": [{
                "type": "story_mention",
                "id": "story-object-1",
                "payload": {"url": "https://cdn/story.jpg"},
            }],
        }

        media = bot._provider_attachment_metadata(msg)

        self.assertFalse(media[0]["provider_native_mention"])
        self.assertEqual(media[0]["target_username"], "")

    def test_normalized_provider_object_key_cannot_forge_native_identity(self):
        """Only provider fields from the raw attachment may establish identity."""
        msg = {
            "mid": "story-mid",
            "attachments": [{
                "type": "story_mention",
                # ``provider_object_key`` is our normalized storage field, not
                # a Meta attachment field.  It must not be accepted as a
                # substitute for an object id supplied by the provider.
                "provider_object_key": "forged-object",
                "payload": {
                    "url": "https://cdn/story.jpg",
                    "media_id": "media-1",
                    "target": {"username": "twocomms"},
                },
            }],
        }

        media = bot._provider_attachment_metadata(msg)

        self.assertFalse(media[0]["provider_native_mention"])
        self.assertEqual(media[0]["provider_object_key"], "")

    def test_reply_to_story_is_context_not_provider_native_mention(self):
        msg = {
            "mid": "reply-mid",
            "reply_to": {
                "story": {
                    "id": "story-object-1",
                    "url": "https://cdn/story.jpg",
                }
            },
        }

        media = bot._provider_attachment_metadata(msg)

        self.assertFalse(media[0]["provider_native_mention"])
        self.assertEqual(media[0]["target_username"], "")


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

    def test_enqueues_textless_ig_post_repost_with_media(self):
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "ig-post-user"},
            "message": {"mid": "ig-post-mid", "attachments": [{
                "type": "ig_post",
                "payload": {
                    "url": "https://cdn/repost.jpg",
                    "ig_post_media_id": "media-123",
                    "title": "Репост",
                },
            }]},
        }]}]}

        self.assertEqual(bot.handle_webhook_payload(self.s, payload), 1)
        self.assertEqual(bot.handle_webhook_payload(self.s, payload), 0)
        msg = InstagramBotMessage.objects.get(mid="ig-post-mid")
        self.assertEqual(msg.text, "(поширений допис)")
        self.assertEqual(json.loads(msg.attachments), ["https://cdn/repost.jpg"])
        self.assertEqual(msg.attachment_media[0]["media_type"], "ig_post")
        self.assertEqual(msg.attachment_media[0]["provider_media_id"], "media-123")

    def test_textless_story_permalink_is_persisted_as_context_only(self):
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "story-context-user"},
            "message": {
                "mid": "story-context-mid",
                "reply_to": {"story": {
                    "url": "https://www.instagram.com/stories/example/123456/",
                    "id": "story-1",
                }},
            },
        }]}]}

        self.assertEqual(bot.handle_webhook_payload(self.s, payload), 1)
        row = InstagramBotMessage.objects.get(mid="story-context-mid")
        self.assertEqual(row.text, "(сторіс)")
        self.assertEqual(row.attachments, "")
        self.assertEqual(row.attachment_media[0]["media_type"], "story")
        self.assertEqual(row.attachment_media[0]["status"], bot.MEDIA_STATUS_METADATA_ONLY)
        self.assertFalse(row.attachment_media[0].get("capture_eligible", True))

    def test_textless_repost_without_url_keeps_provider_identity(self):
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "repost-context-user"},
            "message": {
                "mid": "repost-context-mid",
                "attachments": [{
                    "type": "ig_post",
                    "id": "post-object-1",
                    "ig_post_media_id": "post-media-1",
                }],
            },
        }]}]}

        self.assertEqual(bot.handle_webhook_payload(self.s, payload), 1)
        row = InstagramBotMessage.objects.get(mid="repost-context-mid")
        self.assertEqual(row.text, "(поширений допис)")
        self.assertEqual(row.attachment_media[0]["provider_object_key"], "ig_post:post-object-1")
        self.assertEqual(row.attachment_media[0]["provider_media_id"], "post-media-1")
        self.assertTrue(row.attachment_media[0]["url_metadata_expired"])

    def test_manager_echo_retains_story_media_type(self):
        bot._handle_echo(
            "echo-story-user",
            "",
            attachments=[{
                "url": "https://www.instagram.com/stories/example/123456/",
                "type": "story",
                "provider_object_key": "story:story-1",
            }],
            mid="echo-story-mid",
            persistence_only=True,
            provider_namespace=bot.ingress_provider_namespace(self.s),
        )
        row = InstagramBotMessage.objects.get(mid="echo-story-mid")
        self.assertEqual(row.role, InstagramBotMessage.Role.MANAGER)
        self.assertEqual(row.attachments, "[]")
        self.assertEqual(row.attachment_media[0]["media_type"], "story")
        self.assertEqual(row.attachment_media[0]["status"], bot.MEDIA_STATUS_METADATA_ONLY)

    def test_story_mention_keeps_provider_native_metadata(self):
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "ugc-webhook"},
            "message": {
                "mid": "ugc-story-mid",
                "text": "дивіться",
                "attachments": [{
                    "type": "story_mention",
                    "id": "story-object-1",
                    "payload": {
                        "url": "https://cdn/story.jpg",
                        "media_id": "media-1",
                        "target": {"username": "twocomms"},
                    },
                }],
            },
        }]}]}

        self.assertEqual(bot.handle_webhook_payload(self.s, payload), 1)
        row = InstagramBotMessage.objects.get(mid="ugc-story-mid")
        self.assertEqual(row.attachment_media[0]["provider_object_key"], "story_mention:story-object-1")

    def test_enqueues_messages_change_shape(self):
        payload = {"entry": [{"changes": [{
            "field": "messages",
            "value": {
                "sender": {"id": "u10"},
                "recipient": {"id": "page"},
                "timestamp": 1785000000000,
                "message": {"mid": "changes-mid", "text": "Привіт"},
            },
        }]}]}

        self.assertEqual(bot.handle_webhook_payload(self.s, payload), 1)
        message = InstagramBotMessage.objects.get(mid="changes-mid")
        self.assertEqual(message.sender_id, "u10")
        self.assertIsNotNone(message.provider_created_at)

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

    def test_rejects_sender_longer_than_mariadb_column(self):
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "u" * 65},
            "message": {"mid": "safe-mid", "text": "hello"},
        }]}]}

        self.assertEqual(bot.handle_webhook_payload(self.s, payload), 0)
        self.assertFalse(InstagramBotMessage.objects.exists())

    def test_rejects_message_id_longer_than_mariadb_column(self):
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "safe-user"},
            "message": {"mid": "m" * 256, "text": "hello"},
        }]}]}

        self.assertEqual(bot.handle_webhook_payload(self.s, payload), 0)
        self.assertFalse(InstagramBotMessage.objects.exists())
