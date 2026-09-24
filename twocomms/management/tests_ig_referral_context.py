from django.test import SimpleTestCase, TestCase

from management.models import IgClient, InstagramBotSettings
from management.services import instagram_bot as bot


class ReferralContextExtractionTests(SimpleTestCase):
    def test_message_referral_is_authoritative_and_merges_event_and_postback_context(self):
        payload = {
            "entry": [{
                "messaging": [{
                    "sender": {"id": "ref-user"},
                    "message": {
                        "mid": "ref-mid",
                        "text": "цікавить",
                        "referral": {
                            "ref": "message-campaign",
                            "ad_id": "message-ad",
                            "ads_context_data": {"post_id": "post-123"},
                        },
                    },
                    "referral": {
                        "ref": "event-campaign",
                        "source": "ADS",
                        "ads_context_data": {"ad_title": "Hoodie"},
                    },
                    "postback": {
                        "referral": {
                            "ad_id": "postback-ad",
                            "ads_context_data": {"video_url": "https://cdn/video"},
                        },
                    },
                }]
            }]
        }

        sender, _recipient, _message, referral = list(bot._iter_events(payload))[0]

        self.assertEqual(sender, "ref-user")
        self.assertEqual(referral["ref"], "message-campaign")
        self.assertEqual(referral["ad_id"], "message-ad")
        self.assertEqual(referral["source"], "ADS")
        self.assertEqual(referral["ads_context_data"]["post_id"], "post-123")
        self.assertEqual(referral["ads_context_data"]["ad_title"], "Hoodie")
        self.assertEqual(referral["ads_context_data"]["video_url"], "https://cdn/video")

    def test_changes_message_referral_is_extracted(self):
        payload = {
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {
                        "sender": {"id": "change-ref-user"},
                        "message": {
                            "mid": "change-ref-mid",
                            "text": "hello",
                            "referral": {
                                "ref": "change-campaign",
                                "ads_context_data": {"post_id": "post-456"},
                            },
                        },
                    },
                }]
            }]
        }

        _sender, _recipient, _message, referral = list(bot._iter_events(payload))[0]

        self.assertEqual(referral["ref"], "change-campaign")
        self.assertEqual(referral["ads_context_data"]["post_id"], "post-456")


class ReferralContextPersistenceTests(TestCase):
    def test_full_nested_referral_payload_is_preserved_on_client(self):
        ref = {
            "ref": "message-campaign",
            "ad_id": "message-ad",
            "source": "ADS",
            "type": "OPEN_THREAD",
            "ads_context_data": {
                "post_id": "post-789",
                "ad_title": "Hoodie",
            },
        }

        bot._apply_referral("ref-persist-user", ref)

        client = IgClient.objects.get(igsid="ref-persist-user")
        self.assertEqual(client.referral_payload, ref)
        self.assertEqual(
            client.referral_payload["ads_context_data"]["post_id"],
            "post-789",
        )

    def test_nested_referral_flows_from_webhook_to_client_payload(self):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.allowed_senders = ""
        settings.save()
        payload = {
            "entry": [{
                "messaging": [{
                    "sender": {"id": "ref-webhook-user"},
                    "message": {
                        "mid": "ref-webhook-mid",
                        "text": "ціна?",
                        "referral": {
                            "ref": "nested-campaign",
                            "ad_id": "nested-ad",
                            "ads_context_data": {"post_id": "post-999"},
                        },
                    },
                }]
            }]
        }

        self.assertEqual(bot.handle_webhook_payload(settings, payload), 1)
        client = IgClient.objects.get(igsid="ref-webhook-user")
        self.assertEqual(client.referral_payload["ref"], "nested-campaign")
        self.assertEqual(
            client.referral_payload["ads_context_data"]["post_id"],
            "post-999",
        )

    def test_later_referral_keeps_bounded_previous_touch_history(self):
        bot._apply_referral("multi-touch-user", {
            "ref": "first-campaign",
            "ad_id": "first-ad",
            "ads_context_data": {"post_id": "first-post"},
        })
        bot._apply_referral("multi-touch-user", {
            "ref": "second-campaign",
            "ad_id": "second-ad",
            "ads_context_data": {"post_id": "second-post"},
        })

        client = IgClient.objects.get(igsid="multi-touch-user")
        self.assertEqual(client.referral_payload["ref"], "second-campaign")
        self.assertEqual(
            client.referral_payload["_touch_history"][0]["ads_context_data"]["post_id"],
            "first-post",
        )
