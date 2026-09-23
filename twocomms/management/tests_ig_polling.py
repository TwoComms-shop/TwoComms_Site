import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase
from django.core.cache import cache
from django.utils import timezone as django_timezone

from management.models import IgClient, IgPollCursor, InstagramBotMessage, InstagramBotSettings
from management.services import instagram_bot as bot


def _message(mid: str, minute: int, sender: str = "customer") -> dict:
    return {
        "id": mid,
        "message": mid,
        "from": {"id": sender},
        "created_time": f"2026-07-09T14:{minute:02d}:00+0000",
        "attachments": [{"type": "image", "payload": {"url": f"https://cdn/{mid}.jpg"}}],
    }


class PollCursorTests(TestCase):
    def setUp(self):
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.receive_via_poll = True
        self.settings.ig_user_id = "page"
        self.settings.reply_after = None
        self.settings.last_started_at = None
        self.settings.save(update_fields=[
            "is_enabled", "receive_via_poll", "ig_user_id", "reply_after", "last_started_at",
        ])

    def test_processes_all_messages_in_provider_order_and_persists_cursor(self):
        messages = [_message("m3", 3), _message("m2", 2), _message("m1", 1)]
        seen = []

        def enqueue(_settings, **kwargs):
            seen.append(kwargs)
            return True

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "get_conv_ids_cached", return_value=["conv-1"]), \
             patch.object(bot, "_http", return_value=(200, json.dumps({"messages": {"data": messages}}))), \
             patch.object(bot, "enqueue_inbound", side_effect=enqueue):
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["enqueued"], 3)
        self.assertEqual([item["mid"] for item in seen], ["m1", "m2", "m3"])
        self.assertEqual(seen[0]["attachments"], ["https://cdn/m1.jpg"])
        cursor = IgPollCursor.objects.get(conversation_id="conv-1")
        self.assertEqual(cursor.last_message_id, "m3")
        self.assertEqual(cursor.last_message_at, datetime(2026, 7, 9, 14, 3, tzinfo=timezone.utc))

    def test_second_poll_does_not_reenqueue_messages_before_cursor(self):
        messages = [_message("m3", 3), _message("m2", 2), _message("m1", 1)]
        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "get_conv_ids_cached", return_value=["conv-2"]), \
             patch.object(bot, "_http", return_value=(200, json.dumps({"messages": {"data": messages}}))), \
             patch.object(bot, "enqueue_inbound", return_value=True) as enqueue:
            bot.poll_ingest(self.settings)
            enqueue.reset_mock()
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["enqueued"], 0)
        enqueue.assert_not_called()

    def test_empty_provider_message_is_observed_without_blocking_cursor(self):
        message = {
            **_message("m-empty", 4),
            "message": "",
            "attachments": [],
            "to": {"data": [{"id": "page"}]},
        }

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "get_conv_ids_cached", return_value=["conv-empty"]), \
             patch.object(
                 bot,
                 "_http",
                 return_value=(200, json.dumps({"messages": {"data": [message]}})),
             ), \
             patch.object(bot, "enqueue_inbound") as enqueue:
            result = bot.poll_ingest(self.settings)

        self.assertFalse(result["degraded"])
        enqueue.assert_not_called()
        row = InstagramBotMessage.objects.get(mid="m-empty")
        self.assertEqual(row.source, "poll_history")
        self.assertEqual(row.text, "(медіа)")
        self.assertEqual(
            IgPollCursor.objects.get(conversation_id="conv-empty").last_message_id,
            "m-empty",
        )

    def test_poll_history_preserves_repost_kind_and_provider_identity(self):
        message = {
            "id": "m-repost-history",
            "message": "",
            "from": {"id": "poll-repost-user"},
            "to": {"data": [{"id": "page"}]},
            "created_time": "2026-07-09T14:06:00+0000",
            "attachments": [{
                "type": "ig_post",
                "ig_post_media_id": "post-media-1",
                "id": "post-object-1",
                "payload": {"url": "https://cdn/repost.jpg"},
            }],
        }

        self.assertTrue(bot._persist_polled_message(self.settings, message, observed_only=True))
        row = InstagramBotMessage.objects.get(mid="m-repost-history")
        self.assertEqual(row.text, "(поширений допис)")
        self.assertEqual(row.attachment_media[0]["media_type"], "ig_post")
        self.assertEqual(row.attachment_media[0]["provider_media_id"], "post-media-1")
        self.assertEqual(row.attachment_media[0]["provenance"], bot.MEDIA_PROVENANCE_HISTORICAL)
        self.assertEqual(row.attachment_media[0]["status"], bot.MEDIA_STATUS_METADATA_ONLY)

    def test_instagram_login_attachment_envelope_is_enqueued(self):
        message = {
            **_message("m-image", 5),
            "attachments": {
                "data": [
                    {
                        "image_data": {
                            "url": "https://cdn.example/image.jpg",
                            "preview_url": "https://cdn.example/preview.jpg",
                            "width": 1080,
                            "height": 1080,
                        }
                    }
                ],
                "paging": {},
            },
            "to": {"data": [{"id": "page"}]},
        }
        seen = []

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "get_conv_ids_cached", return_value=["conv-image"]), \
             patch.object(
                 bot,
                 "_http",
                 return_value=(200, json.dumps({"messages": {"data": [message]}})),
             ), \
             patch.object(
                 bot,
                 "enqueue_inbound",
                 side_effect=lambda _settings, **kwargs: seen.append(kwargs) or True,
             ):
            result = bot.poll_ingest(self.settings)

        self.assertFalse(result["degraded"])
        self.assertEqual(seen[0]["attachments"], ["https://cdn.example/image.jpg"])
        self.assertEqual(
            IgPollCursor.objects.get(conversation_id="conv-image").last_message_id,
            "m-image",
        )

    def test_follows_paging_until_all_messages_are_seen(self):
        first = {"messages": {"data": [_message("m4", 4), _message("m3", 3)], "paging": {"next": f"{bot.GRAPH}/next"}}}
        second = {"messages": {"data": [_message("m2", 2), _message("m1", 1)]}}
        seen = []

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "get_conv_ids_cached", return_value=["conv-3"]), \
             patch.object(bot, "_http", side_effect=[(200, json.dumps(first)), (200, json.dumps(second))]) as http, \
             patch.object(bot, "enqueue_inbound", side_effect=lambda _s, **kwargs: seen.append(kwargs) or True):
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["enqueued"], 4)
        self.assertEqual([item["mid"] for item in seen], ["m1", "m2", "m3", "m4"])
        self.assertEqual(http.call_count, 2)

    def test_instagram_login_reads_only_the_latest_twenty_message_window(self):
        IgPollCursor.objects.create(
            conversation_id="conv-instagram-window",
            participant_igsid="customer",
        )
        recent = django_timezone.now() - timedelta(minutes=5)
        first = {
            "messages": {
                "data": [
                    {
                        **_message("m2", 2),
                        "created_time": recent.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                    },
                    {
                        **_message("m1", 1),
                        "created_time": (recent - timedelta(minutes=1)).strftime(
                            "%Y-%m-%dT%H:%M:%S+0000"
                        ),
                    },
                ],
                "paging": {"next": f"{bot.INSTAGRAM_GRAPH}/old-message-page"},
            }
        }
        seen = []

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(bot, "get_page_token", return_value="PT"), patch.object(
            bot,
            "get_conv_ids_cached",
            return_value=["conv-instagram-window"],
        ), patch.object(
            bot,
            "_http",
            return_value=(200, json.dumps(first)),
        ) as http, patch.object(
            bot,
            "enqueue_inbound",
            side_effect=lambda _settings, **kwargs: seen.append(kwargs) or True,
        ):
            result = bot.poll_ingest(self.settings)

        self.assertFalse(result["degraded"])
        self.assertEqual(http.call_count, 1)
        fields = parse_qs(urlsplit(http.call_args.args[0]).query)["fields"][0]
        self.assertIn("messages.limit(20)", fields)
        self.assertEqual([item["mid"] for item in seen], ["m1", "m2"])
        self.assertEqual(
            IgPollCursor.objects.get(
                conversation_id="conv-instagram-window"
            ).last_message_id,
            "m2",
        )

    def test_instagram_login_locally_caps_oversized_provider_window_at_twenty(self):
        messages = [_message(f"m{index:02d}", index) for index in range(21)]

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ):
            validated, _next = bot._validate_polled_page(
                {"messages": {"data": messages}},
                self.settings,
            )

        self.assertEqual(len(validated), bot.POLL_INSTAGRAM_MESSAGE_LIMIT)
        self.assertEqual(
            [item["id"] for item in validated],
            [item["id"] for item in messages[:20]],
        )

    def test_instagram_login_old_unseen_message_is_history_only(self):
        IgPollCursor.objects.create(
            conversation_id="conv-old-instagram",
            participant_igsid="customer",
        )
        old = django_timezone.now() - timedelta(days=2)
        message = {
            **_message("m-old-instagram", 1),
            "created_time": old.strftime("%Y-%m-%dT%H:%M:%S+0000"),
        }

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(bot, "get_page_token", return_value="PT"), patch.object(
            bot,
            "get_conv_ids_cached",
            return_value=["conv-old-instagram"],
        ), patch.object(
            bot,
            "_http",
            return_value=(
                200,
                json.dumps({"messages": {"data": [message]}}),
            ),
        ), patch.object(bot, "enqueue_inbound") as enqueue:
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["enqueued"], 0)
        enqueue.assert_not_called()
        row = InstagramBotMessage.objects.get(mid="m-old-instagram")
        self.assertEqual(row.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(row.source, "poll_history")
        self.assertEqual(
            IgPollCursor.objects.get(
                conversation_id="conv-old-instagram"
            ).last_message_id,
            "m-old-instagram",
        )


class ConversationDiscoveryTests(TestCase):
    def setUp(self):
        self.settings = InstagramBotSettings.load()
        self.settings.page_id = "page"
        self.settings.is_enabled = True
        self.settings.receive_via_poll = True
        self.settings.conversation_discovery_ids = []
        self.settings.conversation_discovery_cursor = ""
        self.settings.save(update_fields=[
            "page_id",
            "is_enabled",
            "receive_via_poll",
            "conversation_discovery_ids",
            "conversation_discovery_cursor",
        ])
        cache.delete(bot._conv_cache_key(self.settings))
        cache.delete(f"ig_bot_conv_refresh:{self.settings.page_id}")

    def tearDown(self):
        cache.delete(bot._conv_cache_key(self.settings))
        cache.delete(f"ig_bot_conv_refresh:{self.settings.page_id}")

    def test_refresh_follows_pages_deduplicates_and_paces_requests(self):
        first = {
            "data": [{"id": "c1"}, {"id": "c2"}],
            "paging": {
                "cursors": {"after": "CURSOR1"},
                "next": f"{bot.GRAPH}/page/conversations?platform=instagram&fields=id&limit=2&after=CURSOR1",
            },
        }
        second = {"data": [{"id": "c2"}, {"id": "c3"}]}
        with patch.object(bot, "_http", side_effect=[(200, json.dumps(first)), (200, json.dumps(second))]) as http, \
             patch.object(bot.time, "sleep") as sleep:
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["c1", "c2", "c3"])
        self.assertEqual(http.call_count, 2)
        self.assertEqual(
            http.call_args_list[0].args[0],
            f"{bot.GRAPH}/page/conversations?platform=instagram&fields=id&limit=50",
        )
        self.assertEqual(
            http.call_args_list[0].kwargs["headers"]["Authorization"],
            "Bearer PT",
        )
        sleep.assert_called_once_with(0.5)

    def test_instagram_login_discovery_syncs_participant_and_hidden_exclusion(self):
        hidden = bot.IgClient.get_or_create_for_sender("123456789")
        hidden.hidden_at = django_timezone.now()
        hidden.hidden_reason = "manual"
        hidden.save(update_fields=["hidden_at", "hidden_reason", "updated_at"])
        payload = {
            "data": [
                {
                    "id": "conv-hidden",
                    "updated_time": "2026-07-30T10:29:26+0000",
                    "participants": {
                        "data": [
                            {"id": self.settings.ig_user_id, "username": "twocomms"},
                            {"id": hidden.igsid, "username": "hidden"},
                        ]
                    },
                }
            ]
        }

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(
            bot,
            "_http",
            return_value=(200, json.dumps(payload)),
        ) as http:
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["conv-hidden"])
        query = parse_qs(urlsplit(http.call_args.args[0]).query)
        self.assertEqual(query["limit"], ["50"])
        self.assertEqual(query["fields"], ["id,participants,updated_time"])
        cursor = IgPollCursor.objects.get(conversation_id="conv-hidden")
        self.assertEqual(cursor.participant_igsid, hidden.igsid)
        self.assertIsNotNone(cursor.provider_updated_at)
        self.assertIsNotNone(cursor.excluded_at)
        self.assertEqual(cursor.excluded_reason, "client_hidden")

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(bot, "get_page_token", return_value="PT"), patch.object(
            bot,
            "get_conv_ids_cached",
            return_value=["conv-hidden"],
        ), patch.object(bot, "_http") as message_http:
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["conversations_excluded"], 1)
        self.assertEqual(result["conversations_checked"], 0)
        message_http.assert_not_called()

    def test_instagram_login_discovery_missing_participants_is_fail_closed(self):
        payload = {
            "data": [
                {
                    "id": "conv-missing-participants",
                    "updated_time": "2026-07-30T10:29:26+0000",
                }
            ]
        }

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(
            bot,
            "_http",
            return_value=(200, json.dumps(payload)),
        ):
            bot.refresh_conv_ids(self.settings, "PT")

        cursor = IgPollCursor.objects.get(
            conversation_id="conv-missing-participants"
        )
        self.assertIsNotNone(cursor.excluded_at)
        self.assertEqual(cursor.excluded_reason, "ambiguous_participants")

    def test_instagram_login_malformed_participants_clears_stale_mapping(self):
        IgPollCursor.objects.create(
            conversation_id="conv-malformed-participants",
            participant_igsid="stale-user",
        )
        payload = {
            "data": [
                {
                    "id": "conv-malformed-participants",
                    "updated_time": "2026-07-30T10:29:26+0000",
                    "participants": {"data": "invalid"},
                }
            ]
        }

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(
            bot,
            "_http",
            return_value=(200, json.dumps(payload)),
        ):
            bot.refresh_conv_ids(self.settings, "PT")

        cursor = IgPollCursor.objects.get(
            conversation_id="conv-malformed-participants"
        )
        self.assertEqual(cursor.participant_igsid, "")
        self.assertIsNotNone(cursor.excluded_at)
        self.assertEqual(cursor.excluded_reason, "ambiguous_participants")

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(bot, "get_page_token", return_value="PT"), patch.object(
            bot,
            "get_conv_ids_cached",
            return_value=["conv-malformed-participants"],
        ), patch.object(bot, "_http") as message_http:
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["conversations_excluded"], 1)
        self.assertEqual(result["conversations_checked"], 0)
        message_http.assert_not_called()

    def test_discovery_sync_locks_client_before_cursor_update(self):
        client = IgClient.get_or_create_for_sender("lock-order-user")
        IgPollCursor.objects.create(
            conversation_id="conv-lock-order",
            participant_igsid=client.igsid,
        )
        conversation = {
            "id": "conv-lock-order",
            "updated_time": "2026-07-30T10:29:26+0000",
            "participants": {
                "data": [
                    {"id": self.settings.ig_user_id},
                    {"id": client.igsid},
                ]
            },
        }
        lock_order = []
        client_lock = IgClient.objects.select_for_update
        cursor_lock = IgPollCursor.objects.select_for_update

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(
            IgClient.objects,
            "select_for_update",
            side_effect=lambda: lock_order.append("client") or client_lock(),
        ), patch.object(
            IgPollCursor.objects,
            "select_for_update",
            side_effect=lambda: lock_order.append("cursor") or cursor_lock(),
        ):
            bot._sync_discovered_conversation_state(self.settings, [conversation])

        self.assertIn("client", lock_order)
        self.assertIn("cursor", lock_order)
        self.assertLess(lock_order.index("client"), lock_order.index("cursor"))

    def test_instagram_login_unmapped_cursor_never_fetches_history(self):
        IgPollCursor.objects.create(conversation_id="conv-unmapped")

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(bot, "get_page_token", return_value="PT"), patch.object(
            bot,
            "get_conv_ids_cached",
            return_value=["conv-unmapped"],
        ), patch.object(bot, "_http") as message_http:
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["conversations_unmapped"], 1)
        self.assertEqual(result["conversations_checked"], 0)
        message_http.assert_not_called()

    def test_failed_later_page_keeps_snapshot_and_publishes_validated_ids(self):
        cache.set(bot._conv_cache_key(self.settings), ["old-1", "old-2"], 3600)
        self.addCleanup(cache.delete, "ig_bot_ingress_refresh_degraded:page")
        first = {
            "data": [{"id": "new-1"}],
            "paging": {
                "cursors": {"after": "CURSOR1"},
                "next": f"{bot.GRAPH}/page/conversations?platform=instagram&fields=id&limit=2&after=CURSOR1",
            },
        }
        with patch.object(bot, "_http", side_effect=[(200, json.dumps(first)), (429, "quota")]), \
             patch.object(bot.time, "sleep"):
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["new-1", "old-1", "old-2"])
        self.assertEqual(
            cache.get(bot._conv_cache_key(self.settings)),
            ["new-1", "old-1", "old-2"],
        )
        self.settings.refresh_from_db()
        self.assertEqual(
            self.settings.conversation_discovery_ids,
            ["new-1", "old-1", "old-2"],
        )
        self.assertEqual(self.settings.conversation_discovery_cursor, "CURSOR1")
        self.assertEqual(
            cache.get("ig_bot_ingress_refresh_degraded:page")["state"],
            "conversation_refresh_failed",
        )

    def test_complete_refresh_clears_only_conversation_refresh_degradation(self):
        refresh_key = "ig_bot_ingress_refresh_degraded:page"
        poll_key = "ig_bot_ingress_poll_degraded:page"
        cache.set(refresh_key, {"state": "conversation_refresh_failed"}, 600)
        cache.set(poll_key, {"state": "message_poll_failed"}, 600)
        self.addCleanup(cache.delete, refresh_key)
        self.addCleanup(cache.delete, poll_key)

        with patch.object(bot, "_http", return_value=(200, json.dumps({"data": []}))):
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, [])
        self.assertIsNone(cache.get(refresh_key))
        self.assertEqual(cache.get(poll_key)["state"], "message_poll_failed")

    def test_malformed_first_page_does_not_publish_partial_or_invalid_cache(self):
        cache.set(bot._conv_cache_key(self.settings), ["old"], 3600)
        with patch.object(bot, "_http", return_value=(200, "[]")):
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["old"])
        self.assertEqual(cache.get(bot._conv_cache_key(self.settings)), ["old"])

    def test_invalid_next_url_keeps_snapshot(self):
        cache.set(bot._conv_cache_key(self.settings), ["old"], 3600)
        first = {"data": [{"id": "new"}], "paging": {"next": "https://evil.example/steal"}}
        with patch.object(bot, "_http", return_value=(200, json.dumps(first))):
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["old"])

    def test_invalid_id_shape_keeps_snapshot(self):
        cache.set(bot._conv_cache_key(self.settings), ["old"], 3600)
        with patch.object(bot, "_http", return_value=(200, json.dumps({"data": [{"id": []}]}))):
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["old"])

    def test_malformed_paging_shape_keeps_snapshot(self):
        cache.set(bot._conv_cache_key(self.settings), ["old"], 3600)
        with patch.object(bot, "_http", return_value=(200, json.dumps({"data": [], "paging": "oops"}))):
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["old"])

    def test_refresh_lock_prevents_overlapping_provider_calls(self):
        cache.set(bot._conv_cache_key(self.settings), ["old"], 3600)
        self.settings.conversation_discovery_lease_token = "busy"
        self.settings.conversation_discovery_lease_expires_at = (
            django_timezone.now() + timedelta(seconds=300)
        )
        self.settings.save(update_fields=[
            "conversation_discovery_lease_token",
            "conversation_discovery_lease_expires_at",
        ])
        with patch.object(bot, "_http") as http:
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["old"])
        http.assert_not_called()

    def test_hot_read_merges_durable_and_cached_conversations(self):
        self.settings.conversation_discovery_ids = ["db-a", "shared"]
        self.settings.save(update_fields=["conversation_discovery_ids"])
        cache.set(bot._conv_cache_key(self.settings), ["shared", "cache-c"], 3600)

        ids = bot.get_conv_ids_cached(self.settings)

        self.assertEqual(ids, ["db-a", "shared", "cache-c"])
        self.assertEqual(cache.get(bot._conv_cache_key(self.settings)), ids)

    def test_empty_completed_cache_is_a_valid_snapshot(self):
        cache.set(bot._conv_cache_key(self.settings), [], 3600)

        self.assertEqual(bot.get_conv_ids_cached(self.settings), [])
        self.assertEqual(cache.get(bot._conv_cache_key(self.settings)), [])

    def test_failed_first_page_repairs_cache_from_durable_snapshot(self):
        self.settings.conversation_discovery_ids = ["db-a", "db-b"]
        self.settings.save(update_fields=["conversation_discovery_ids"])
        cache.set(bot._conv_cache_key(self.settings), ["db-a"], 3600)

        with patch.object(bot, "_http", return_value=(504, "timeout")):
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["db-a", "db-b"])
        self.assertEqual(cache.get(bot._conv_cache_key(self.settings)), ids)

    def test_database_lease_allows_only_one_refresh_owner(self):
        first = bot._claim_conversation_discovery_lease(self.settings)
        stale = InstagramBotSettings.objects.get(pk=self.settings.pk)
        second = bot._claim_conversation_discovery_lease(stale)

        self.assertTrue(first)
        self.assertIsNone(second)

        bot._release_conversation_discovery_lease(self.settings.pk, first)
        third = bot._claim_conversation_discovery_lease(stale)
        self.assertTrue(third)
        bot._release_conversation_discovery_lease(self.settings.pk, third)

    def test_expired_database_lease_can_be_reclaimed_without_stale_release(self):
        self.settings.conversation_discovery_lease_token = "expired-owner"
        self.settings.conversation_discovery_lease_expires_at = (
            django_timezone.now() - timedelta(seconds=1)
        )
        self.settings.save(update_fields=[
            "conversation_discovery_lease_token",
            "conversation_discovery_lease_expires_at",
        ])

        new_owner = bot._claim_conversation_discovery_lease(self.settings)
        self.assertTrue(new_owner)
        bot._release_conversation_discovery_lease(
            self.settings.pk,
            "expired-owner",
        )
        self.settings.refresh_from_db()
        self.assertEqual(
            self.settings.conversation_discovery_lease_token,
            new_owner,
        )
        bot._release_conversation_discovery_lease(self.settings.pk, new_owner)

    def test_refresh_persists_safe_cursor_and_continues_next_cycle(self):
        first = {
            "data": [{"id": "c1"}, {"id": "c2"}],
            "paging": {
                "cursors": {"after": "CURSOR1"},
                "next": f"{bot.GRAPH}/page/conversations?platform=instagram&fields=id&limit=2&after=CURSOR1",
            },
        }
        second = {"data": [{"id": "c3"}]}
        with patch.object(bot, "CONV_DISCOVERY_PAGES_PER_REFRESH", 1), \
             patch.object(bot, "_http", return_value=(200, json.dumps(first))) as http:
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["c1", "c2"])
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.conversation_discovery_ids, ["c1", "c2"])
        self.assertEqual(self.settings.conversation_discovery_cursor, "CURSOR1")
        self.assertNotIn("CURSOR1", http.call_args_list[0].args[0])

        with patch.object(bot, "CONV_DISCOVERY_PAGES_PER_REFRESH", 1), \
             patch.object(bot, "_http", return_value=(200, json.dumps(second))) as http:
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["c1", "c2", "c3"])
        self.assertIn("after=CURSOR1", http.call_args_list[0].args[0])
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.conversation_discovery_ids, ["c1", "c2", "c3"])
        self.assertEqual(self.settings.conversation_discovery_cursor, "")
        self.assertEqual(cache.get(bot._conv_cache_key(self.settings)), ["c1", "c2", "c3"])

    def test_completed_multislice_scan_prunes_stale_conversations(self):
        self.settings.conversation_discovery_page_id = "page"
        self.settings.conversation_discovery_ids = ["stale"]
        self.settings.save(update_fields=[
            "conversation_discovery_page_id",
            "conversation_discovery_ids",
        ])
        first = {
            "data": [{"id": "new-1"}],
            "paging": {
                "cursors": {"after": "CURSOR1"},
                "next": f"{bot.GRAPH}/page/conversations?platform=instagram&fields=id&limit=2&after=CURSOR1",
            },
        }
        second = {"data": [{"id": "new-2"}]}

        with patch.object(bot, "CONV_DISCOVERY_PAGES_PER_REFRESH", 1), \
             patch.object(bot, "_http", return_value=(200, json.dumps(first))):
            self.assertEqual(
                bot.refresh_conv_ids(self.settings, "PT"),
                ["new-1", "stale"],
            )
        with patch.object(bot, "CONV_DISCOVERY_PAGES_PER_REFRESH", 1), \
             patch.object(bot, "_http", return_value=(200, json.dumps(second))):
            self.assertEqual(
                bot.refresh_conv_ids(self.settings, "PT"),
                ["new-1", "new-2"],
            )

        self.settings.refresh_from_db()
        self.assertEqual(
            self.settings.conversation_discovery_ids,
            ["new-1", "new-2"],
        )
        self.assertEqual(self.settings.conversation_discovery_scan_ids, [])
        self.assertTrue(self.settings.conversation_discovery_completed_at)

    def test_page_switch_never_restores_previous_account_discovery(self):
        self.settings.conversation_discovery_page_id = "old-page"
        self.settings.conversation_discovery_ids = ["old-conversation"]
        self.settings.conversation_discovery_cursor = "OLD-CURSOR"
        self.settings.conversation_discovery_scan_ids = ["old-conversation"]
        self.settings.save(update_fields=[
            "conversation_discovery_page_id",
            "conversation_discovery_ids",
            "conversation_discovery_cursor",
            "conversation_discovery_scan_ids",
        ])
        cache.set(bot._conv_cache_key(self.settings), ["old-conversation"], 3600)
        self.settings.page_id = "new-page"
        self.settings.save(update_fields=["page_id"])

        with patch.object(
            bot,
            "_http",
            return_value=(200, json.dumps({"data": [{"id": "new-conversation"}]})),
        ):
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["new-conversation"])
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.conversation_discovery_page_id, "new-page")
        self.assertEqual(
            self.settings.conversation_discovery_ids,
            ["new-conversation"],
        )

    def test_cross_slice_cursor_cycle_is_rejected(self):
        first = {
            "data": [{"id": "c1"}],
            "paging": {
                "cursors": {"after": "CURSOR1"},
                "next": f"{bot.GRAPH}/page/conversations?platform=instagram&fields=id&limit=2&after=CURSOR1",
            },
        }
        repeated = {
            "data": [{"id": "c2"}],
            "paging": {
                "cursors": {"after": "CURSOR1"},
                "next": f"{bot.GRAPH}/page/conversations?platform=instagram&fields=id&limit=2&after=CURSOR1",
            },
        }
        with patch.object(bot, "CONV_DISCOVERY_PAGES_PER_REFRESH", 1), \
             patch.object(bot, "_http", return_value=(200, json.dumps(first))):
            self.assertEqual(bot.refresh_conv_ids(self.settings, "PT"), ["c1"])
        with patch.object(bot, "CONV_DISCOVERY_PAGES_PER_REFRESH", 1), \
             patch.object(bot, "_http", return_value=(200, json.dumps(repeated))):
            self.assertEqual(bot.refresh_conv_ids(self.settings, "PT"), ["c1"])

        self.settings.refresh_from_db()
        self.assertEqual(self.settings.conversation_discovery_cursor, "CURSOR1")
        self.assertEqual(
            cache.get("ig_bot_ingress_refresh_degraded:page")["state"],
            "conversation_refresh_failed",
        )

    def test_new_discovery_is_not_starved_by_full_stale_snapshot(self):
        old_ids = [f"old-{index}" for index in range(bot.CONV_MAX_IDS)]
        self.settings.conversation_discovery_page_id = "page"
        self.settings.conversation_discovery_ids = old_ids
        self.settings.save(update_fields=[
            "conversation_discovery_page_id",
            "conversation_discovery_ids",
        ])
        first = {
            "data": [{"id": "new-live"}],
            "paging": {
                "cursors": {"after": "CURSOR1"},
                "next": f"{bot.GRAPH}/page/conversations?platform=instagram&fields=id&limit=2&after=CURSOR1",
            },
        }

        with patch.object(bot, "CONV_DISCOVERY_PAGES_PER_REFRESH", 1), \
             patch.object(bot, "_http", return_value=(200, json.dumps(first))):
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(len(ids), bot.CONV_MAX_IDS)
        self.assertEqual(ids[0], "new-live")
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.conversation_discovery_scan_ids, ["new-live"])
        self.assertEqual(self.settings.conversation_discovery_cursor, "CURSOR1")

    def test_page_budget_covers_configured_conversation_cap(self):
        self.assertGreaterEqual(
            bot.CONV_MAX_PAGES * bot.CONV_PAGE_LIMIT,
            bot.CONV_MAX_IDS,
        )

    def test_discovery_status_exposes_incomplete_scan_without_credentials(self):
        now = django_timezone.now()
        self.settings.conversation_discovery_page_id = "page"
        self.settings.conversation_discovery_ids = ["c1", "c2"]
        self.settings.conversation_discovery_scan_ids = ["c2"]
        self.settings.conversation_discovery_cursor = "CURSOR1"
        self.settings.conversation_discovery_pages_seen = 3
        self.settings.conversation_discovery_updated_at = now - timedelta(seconds=7)
        self.settings.save(update_fields=[
            "conversation_discovery_page_id",
            "conversation_discovery_ids",
            "conversation_discovery_scan_ids",
            "conversation_discovery_cursor",
            "conversation_discovery_pages_seen",
            "conversation_discovery_updated_at",
        ])

        status = bot.conversation_discovery_status(self.settings, now=now)

        self.assertEqual(status["state"], "in_progress")
        self.assertEqual(status["conversation_count"], 2)
        self.assertEqual(status["scan_count"], 1)
        self.assertEqual(status["pages_seen"], 3)
        self.assertEqual(status["updated_age_seconds"], 7.0)
        self.assertNotIn("cursor", status)

    def test_partial_refresh_publishes_validated_ids_and_keeps_cursor(self):
        self.settings.conversation_discovery_ids = ["old"]
        self.settings.save(update_fields=["conversation_discovery_ids"])
        cache.set(bot._conv_cache_key(self.settings), ["old"], 3600)
        first = {
            "data": [{"id": "old"}, {"id": "new"}],
            "paging": {
                "cursors": {"after": "CURSOR2"},
                "next": f"{bot.GRAPH}/page/conversations?platform=instagram&fields=id&limit=2&after=CURSOR2",
            },
        }
        with patch.object(bot, "CONV_DISCOVERY_PAGES_PER_REFRESH", 2), \
             patch.object(bot, "_http", side_effect=[(200, json.dumps(first)), (504, "timeout")]), \
             patch.object(bot.time, "sleep"):
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["old", "new"])
        self.assertEqual(cache.get(bot._conv_cache_key(self.settings)), ["old", "new"])
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.conversation_discovery_ids, ["old", "new"])
        self.assertEqual(self.settings.conversation_discovery_cursor, "CURSOR2")
        self.assertEqual(
            cache.get("ig_bot_ingress_refresh_degraded:page")["state"],
            "conversation_refresh_failed",
        )

    def test_malformed_paging_cursor_keeps_snapshot_without_secret_url_persistence(self):
        self.settings.conversation_discovery_ids = ["old"]
        self.settings.save(update_fields=["conversation_discovery_ids"])
        cache.set(bot._conv_cache_key(self.settings), ["old"], 3600)
        first = {
            "data": [{"id": "new"}],
            "paging": {
                "cursors": {"after": "CURSOR&access_token=SECRET"},
                "next": f"{bot.GRAPH}/page/conversations?platform=instagram&fields=id&limit=2&after=CURSOR&access_token=SECRET",
            },
        }
        with patch.object(bot, "_http", return_value=(200, json.dumps(first))):
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["old"])
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.conversation_discovery_ids, ["old"])
        self.assertEqual(self.settings.conversation_discovery_cursor, "")

    def test_cold_cache_defers_to_background_refresher_without_blocking_poll(self):
        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "get_conv_ids_cached", return_value=None), \
             patch.object(bot, "refresh_conv_ids") as refresh:
            result = bot.poll_ingest(self.settings)

        self.assertTrue(result["refresh_pending"])
        refresh.assert_not_called()

    def test_cold_cache_restores_durable_snapshot_and_polls_conversation(self):
        self.settings.conversation_discovery_ids = ["conv-cold"]
        self.settings.save(update_fields=["conversation_discovery_ids"])
        messages = {"messages": {"data": [_message("cold-live", 1)]}}

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "_http", return_value=(200, json.dumps(messages))) as http, \
             patch.object(bot, "enqueue_inbound", return_value=True) as enqueue:
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["conversations"], 1)
        self.assertEqual(result["conversations_checked"], 1)
        self.assertEqual(result["requests_used"], 1)
        self.assertEqual(result["enqueued"], 1)
        self.assertEqual(http.call_count, 1)
        enqueue.assert_called_once()


class ConversationMessagePaginationSafetyTests(TestCase):
    def setUp(self):
        self.settings = InstagramBotSettings.load()
        self.settings.page_id = "page"
        self.settings.ig_user_id = "page"
        self.settings.is_enabled = True
        self.settings.receive_via_poll = True
        self.settings.reply_after = None
        self.settings.last_started_at = None
        self.settings.save(update_fields=[
            "page_id",
            "ig_user_id",
            "is_enabled",
            "receive_via_poll",
            "reply_after",
            "last_started_at",
        ])
        cache.delete(bot._conv_cache_key(self.settings))

    def tearDown(self):
        cache.delete(bot._conv_cache_key(self.settings))
        cache.delete(f"ig_bot_poll_offset:{self.settings.page_id}")

    def _cache_conversations(self, *ids):
        cache.set(bot._conv_cache_key(self.settings), list(ids), 3600)

    def test_malformed_messages_shape_does_not_crash_or_advance_cursor(self):
        self._cache_conversations("conv-malformed")
        payload = {"messages": "oops"}

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "_http", return_value=(200, json.dumps(payload))), \
             patch.object(bot, "enqueue_inbound") as enqueue:
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["enqueued"], 0)
        enqueue.assert_not_called()
        cursor = IgPollCursor.objects.get(conversation_id="conv-malformed")
        self.assertEqual(cursor.last_message_id, "")
        self.assertIsNone(cursor.last_message_at)

    def test_untrusted_next_url_is_not_requested_or_partially_published(self):
        self._cache_conversations("conv-hostile")
        first = {
            "messages": {
                "data": [_message("m1", 1)],
                "paging": {"next": "https://evil.example/steal"},
            }
        }

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "_http", return_value=(200, json.dumps(first))) as http, \
             patch.object(bot, "enqueue_inbound") as enqueue:
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["enqueued"], 0)
        self.assertEqual(http.call_count, 1)
        enqueue.assert_not_called()
        self.assertEqual(
            IgPollCursor.objects.get(conversation_id="conv-hostile").last_message_id,
            "",
        )

    def test_repeated_next_url_is_detected_without_partial_cursor_advance(self):
        self._cache_conversations("conv-cycle")
        loop_url = f"{bot.GRAPH}/loop"
        first = {
            "messages": {
                "data": [_message("m2", 2)],
                "paging": {"next": loop_url},
            }
        }
        repeated = {
            "messages": {
                "data": [_message("m1", 1)],
                "paging": {"next": loop_url},
            }
        }

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "_http", side_effect=[
                 (200, json.dumps(first)),
                 (200, json.dumps(repeated)),
             ]) as http, \
             patch.object(bot, "enqueue_inbound") as enqueue:
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["enqueued"], 2)
        self.assertEqual(http.call_count, 2)
        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(
            IgPollCursor.objects.get(conversation_id="conv-cycle").last_message_id,
            "",
        )

    def test_non_string_message_id_is_rejected_without_enqueue(self):
        self._cache_conversations("conv-bad-mid")
        payload = {"messages": {"data": [{**_message("m1", 1), "id": 123}]}}

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "_http", return_value=(200, json.dumps(payload))), \
             patch.object(bot, "enqueue_inbound") as enqueue:
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["enqueued"], 0)
        self.assertTrue(result["degraded"])
        enqueue.assert_not_called()
        self.assertEqual(
            IgPollCursor.objects.get(conversation_id="conv-bad-mid").last_message_id,
            "",
        )

    def test_malformed_message_fields_do_not_advance_cursor(self):
        malformed_messages = [
            {key: value for key, value in _message("m-time", 1).items() if key != "created_time"},
            {**_message("m-sender", 1), "from": {"id": 123}},
            {**_message("m-text", 1), "message": 123},
            {
                **_message("m-attachment", 1),
                "attachments": [{"type": "image", "payload": "oops"}],
            },
        ]

        for index, message in enumerate(malformed_messages):
            with self.subTest(index=index):
                conversation_id = f"conv-malformed-field-{index}"
                self._cache_conversations(conversation_id)
                with patch.object(bot, "get_page_token", return_value="PT"), \
                     patch.object(
                         bot,
                         "_http",
                         return_value=(200, json.dumps({"messages": {"data": [message]}})),
                     ), \
                     patch.object(bot, "enqueue_inbound") as enqueue:
                    result = bot.poll_ingest(self.settings)

                self.assertEqual(result["enqueued"], 0)
                enqueue.assert_not_called()
                cursor = IgPollCursor.objects.get(conversation_id=conversation_id)
                self.assertEqual(cursor.last_message_id, "")
                self.assertIsNone(cursor.last_message_at)

    def test_invalid_cached_conversation_ids_request_refresh_without_message_fetch(self):
        cache.set(bot._conv_cache_key(self.settings), [123, "conv-valid"], 3600)

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "refresh_conv_ids") as refresh, \
             patch.object(bot, "_fetch_polled_conversation") as fetch:
            result = bot.poll_ingest(self.settings)

        self.assertTrue(result["refresh_pending"])
        self.assertEqual(result["conversations"], 0)
        refresh.assert_not_called()
        fetch.assert_not_called()

    def test_failed_later_message_page_does_not_enqueue_or_advance_cursor(self):
        self._cache_conversations("conv-partial")
        self.addCleanup(cache.delete, "ig_bot_ingress_poll_degraded:page")
        first = {
            "messages": {
                "data": [_message("m2", 2)],
                "paging": {"next": f"{bot.GRAPH}/next-message-page"},
            }
        }

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "_http", side_effect=[
                 (200, json.dumps(first)),
                 (503, "temporary"),
             ]), \
             patch.object(bot, "enqueue_inbound") as enqueue:
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["enqueued"], 1)
        self.assertTrue(result["degraded"])
        self.assertEqual(
            cache.get("ig_bot_ingress_poll_degraded:page")["state"],
            "message_poll_failed",
        )
        enqueue.assert_called_once()
        self.assertEqual(
            IgPollCursor.objects.get(conversation_id="conv-partial").last_message_id,
            "",
        )

    def test_complete_poll_clears_only_message_poll_degradation(self):
        self._cache_conversations("conv-complete")
        refresh_key = "ig_bot_ingress_refresh_degraded:page"
        poll_key = "ig_bot_ingress_poll_degraded:page"
        cache.set(refresh_key, {"state": "conversation_refresh_failed"}, 600)
        cache.set(poll_key, {"state": "message_poll_failed"}, 600)
        self.addCleanup(cache.delete, refresh_key)
        self.addCleanup(cache.delete, poll_key)

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "_http", return_value=(200, json.dumps({"messages": {"data": []}}))):
            result = bot.poll_ingest(self.settings)

        self.assertFalse(result["degraded"])
        self.assertEqual(bot.POLL_MESSAGE_TIMEOUT, 12)
        self.assertIsNone(cache.get(poll_key))
        self.assertEqual(cache.get(refresh_key)["state"], "conversation_refresh_failed")

    def test_instagram_login_unchanged_conversation_skips_provider_io(self):
        self._cache_conversations("conv-unchanged")
        provider_updated_at = django_timezone.now()
        IgPollCursor.objects.create(
            conversation_id="conv-unchanged",
            participant_igsid="123456789",
            provider_updated_at=provider_updated_at,
            synced_provider_updated_at=provider_updated_at,
            last_message_id="m-current",
            last_message_at=provider_updated_at,
        )

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(bot, "get_page_token", return_value="PT"), patch.object(
            bot,
            "get_conv_ids_cached",
            return_value=["conv-unchanged"],
        ), patch.object(
            bot,
            "_http",
        ) as http:
            result = bot.poll_ingest(self.settings)

        self.assertFalse(result["degraded"])
        self.assertEqual(result["conversations_unchanged"], 1)
        self.assertEqual(result["conversations_checked"], 0)
        http.assert_not_called()

    def test_instagram_login_403_is_isolated_and_backed_off(self):
        self._cache_conversations("conv-inaccessible")
        IgPollCursor.objects.create(
            conversation_id="conv-inaccessible",
            participant_igsid="123456789",
            provider_updated_at=django_timezone.now(),
        )

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(bot, "get_page_token", return_value="PT"), patch.object(
            bot,
            "get_conv_ids_cached",
            return_value=["conv-inaccessible"],
        ), patch.object(
            bot,
            "_http",
            return_value=(403, json.dumps({"error": {"code": 100}})),
        ) as http:
            first = bot.poll_ingest(self.settings)
            second = bot.poll_ingest(self.settings)

        self.assertFalse(first["degraded"])
        self.assertEqual(first["conversations_checked"], 1)
        self.assertEqual(second["conversations_deferred"], 1)
        self.assertEqual(second["conversations_checked"], 0)
        self.assertEqual(http.call_count, 1)
        cursor = IgPollCursor.objects.get(conversation_id="conv-inaccessible")
        self.assertEqual(cursor.failure_count, 1)
        self.assertEqual(cursor.last_error, "http_403")
        self.assertIsNotNone(cursor.next_attempt_at)

    def test_instagram_login_permission_code_is_immediately_degraded(self):
        self._cache_conversations("conv-permission")
        IgPollCursor.objects.create(
            conversation_id="conv-permission",
            participant_igsid="123456789",
            provider_updated_at=django_timezone.now(),
        )

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(
            bot,
            "get_conv_ids_cached",
            return_value=["conv-permission"],
        ), patch.object(bot, "get_page_token", return_value="PT"), patch.object(
            bot,
            "_http",
            return_value=(403, json.dumps({"error": {"code": 200}})),
        ):
            result = bot.poll_ingest(self.settings)

        self.assertTrue(result["degraded"])
        self.assertEqual(
            cache.get(
                "ig_bot_ingress_poll_degraded:"
                f"{bot.INSTAGRAM_LOGIN_TRANSPORT}:{self.settings.ig_user_id}"
            )["reason"],
            "meta_permission_error",
        )

    def test_all_inaccessible_conversations_mark_poll_degraded(self):
        self._cache_conversations("conv-inaccessible-a", "conv-inaccessible-b")
        for conversation_id in ("conv-inaccessible-a", "conv-inaccessible-b"):
            IgPollCursor.objects.create(
                conversation_id=conversation_id,
                participant_igsid=f"user-{conversation_id[-1]}",
                provider_updated_at=django_timezone.now(),
            )

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(
            bot,
            "get_conv_ids_cached",
            return_value=["conv-inaccessible-a", "conv-inaccessible-b"],
        ), patch.object(bot, "get_page_token", return_value="PT"), patch.object(
            bot,
            "_http",
            return_value=(403, json.dumps({"error": {"code": 100}})),
        ):
            result = bot.poll_ingest(self.settings)

        self.assertTrue(result["degraded"])
        self.assertEqual(
            cache.get(
                "ig_bot_ingress_poll_degraded:"
                f"{bot.INSTAGRAM_LOGIN_TRANSPORT}:{self.settings.ig_user_id}"
            )["reason"],
            "all_conversations_inaccessible",
        )

    def test_isolated_403_does_not_clear_existing_poll_degradation(self):
        self._cache_conversations("conv-still-inaccessible")
        IgPollCursor.objects.create(
            conversation_id="conv-still-inaccessible",
            participant_igsid="123456789",
            provider_updated_at=django_timezone.now(),
        )
        poll_key = (
            "ig_bot_ingress_poll_degraded:"
            f"{bot.INSTAGRAM_LOGIN_TRANSPORT}:{self.settings.ig_user_id}"
        )
        cache.set(
            poll_key,
            {"state": "message_poll_failed", "reason": "http_503", "at": 1},
            600,
        )
        self.addCleanup(cache.delete, poll_key)

        with patch.object(
            bot,
            "provider_transport",
            return_value=bot.INSTAGRAM_LOGIN_TRANSPORT,
        ), patch.object(
            bot,
            "get_conv_ids_cached",
            return_value=["conv-still-inaccessible"],
        ), patch.object(bot, "get_page_token", return_value="PT"), patch.object(
            bot,
            "_http",
            return_value=(403, json.dumps({"error": {"code": 100}})),
        ):
            result = bot.poll_ingest(self.settings)

        self.assertFalse(result["degraded"], result)
        self.assertEqual(cache.get(poll_key)["reason"], "http_503")

    def test_global_request_budget_rotates_conversations_fairly(self):
        self._cache_conversations("conv-a", "conv-b")
        requested_urls = []

        def http(url, **_kwargs):
            requested_urls.append(url)
            cid = "a" if "/conv-a?" in url else "b"
            return 200, json.dumps({"messages": {"data": [_message(f"m-{cid}", 1)]}})

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "POLL_MAX_REQUESTS", 1, create=True), \
             patch.object(bot, "POLL_MAX_SECONDS", 60, create=True), \
             patch.object(bot, "_http", side_effect=http), \
             patch.object(bot, "enqueue_inbound", return_value=True):
            first = bot.poll_ingest(self.settings)
            second = bot.poll_ingest(self.settings)

        self.assertTrue(first["budget_exhausted"])
        self.assertTrue(second["budget_exhausted"])
        self.assertEqual(first["conversations_checked"], 1)
        self.assertEqual(second["conversations_checked"], 1)
        self.assertEqual(len(requested_urls), 2)
        self.assertIn("/conv-a?", requested_urls[0])
        self.assertIn("/conv-b?", requested_urls[1])

    def test_incomplete_budgeted_cycle_does_not_clear_poll_degradation(self):
        self._cache_conversations("conv-a", "conv-b")
        poll_key = "ig_bot_ingress_poll_degraded:page"
        cache.set(
            poll_key,
            {"state": "message_poll_failed", "reason": "http_503"},
            600,
        )
        self.addCleanup(cache.delete, poll_key)

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "POLL_MAX_REQUESTS", 1, create=True), \
             patch.object(bot, "POLL_MAX_SECONDS", 60, create=True), \
             patch.object(
                 bot,
                 "_http",
                 return_value=(200, json.dumps({"messages": {"data": []}})),
             ):
            result = bot.poll_ingest(self.settings)

        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(cache.get(poll_key)["state"], "message_poll_failed")

    def test_advanced_access_failure_is_visible_without_storing_graph_body(self):
        self._cache_conversations("conv-permission")
        body = json.dumps({
            "error": {
                "code": 200,
                "error_subcode": bot.ADVANCED_ACCESS_SUBCODE,
                "message": "App does not have Advanced Access to instagram_manage_messages permission",
            }
        })

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "_http", return_value=(403, body)):
            result = bot.poll_ingest(self.settings)

        self.assertTrue(result["degraded"])
        evidence = cache.get("ig_bot_ingress_poll_degraded:page")
        self.assertEqual(evidence["reason"], "meta_advanced_access")
        self.assertNotIn("Advanced Access", json.dumps(evidence))

    def test_advanced_access_phrase_without_exact_subcode_is_generic_permission_error(self):
        body = json.dumps({
            "error": {
                "code": 200,
                "message": "App does not have Advanced Access to instagram_manage_messages permission",
            }
        })

        self.assertEqual(
            bot._classify_poll_provider_failure(403, body),
            "meta_permission_error",
        )

    def test_history_messages_are_persisted_without_reply_queue_or_page_side_effects(self):
        self._cache_conversations("conv-history")
        self.settings.reply_after = datetime(2026, 7, 10, 17, 0, tzinfo=timezone.utc)
        self.settings.save(update_fields=["reply_after"])
        messages = [
            _message("old-user", 1, sender="customer"),
            _message("old-page", 2, sender="page"),
        ]
        messages[1]["to"] = {"data": [{"id": "customer"}]}
        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "_http", return_value=(200, json.dumps({"messages": {"data": messages}}))), \
             patch.object(bot, "enqueue_inbound") as enqueue, \
             patch(
                 "management.services.bot_conversation_analysis.schedule_analysis"
             ) as schedule_analysis:
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["enqueued"], 0)
        enqueue.assert_not_called()
        rows = {
            row.mid: row
            for row in InstagramBotMessage.objects.filter(
                mid__in=["old-user", "old-page"]
            )
        }
        self.assertEqual(rows["old-user"].role, InstagramBotMessage.Role.USER)
        self.assertEqual(rows["old-page"].role, InstagramBotMessage.Role.MANAGER)
        client = bot.IgClient.objects.get(igsid="customer")
        self.assertFalse(client.manager_takeover)
        self.assertFalse(client.bot_paused)
        self.assertEqual(schedule_analysis.call_count, 2)

    def test_duplicate_history_backfill_does_not_reschedule_analysis(self):
        message = _message("history-dedupe", 1)

        with patch(
            "management.services.bot_conversation_analysis.schedule_analysis"
        ) as schedule_analysis:
            self.assertTrue(
                bot._persist_polled_message(self.settings, message, observed_only=True)
            )
            self.assertTrue(
                bot._persist_polled_message(self.settings, message, observed_only=True)
            )

        self.assertEqual(
            InstagramBotMessage.objects.filter(mid="history-dedupe").count(),
            1,
        )
        schedule_analysis.assert_called_once()

    def test_duplicate_poll_enrichment_schedules_analysis_after_media_is_added(self):
        message = _message("history-media-enrichment", 1)
        self.assertTrue(
            bot._persist_polled_message(self.settings, message, observed_only=True)
        )
        enriched = dict(message)
        enriched["attachments"] = [{
            "type": "image",
            "payload": {"url": "https://lookaside.example/poll.jpg"},
        }]

        with patch(
            "management.services.bot_conversation_analysis.schedule_analysis"
        ) as schedule_analysis:
            self.assertTrue(
                bot._persist_polled_message(
                    self.settings,
                    enriched,
                    observed_only=True,
                )
            )

        self.assertEqual(schedule_analysis.call_count, 1)
        row = InstagramBotMessage.objects.get(mid="history-media-enrichment")
        self.assertEqual(row.attachment_media[0]["status"], "metadata_only")

    def test_page_cap_queues_validated_live_rows_without_cursor_advance(self):
        self._cache_conversations("conv-long")
        first_page = {
            "messages": {
                "data": [_message("history-1", 1)],
                "paging": {"next": f"{bot.GRAPH}/next-history"},
            }
        }
        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "POLL_MESSAGE_MAX_PAGES", 1), \
             patch.object(bot, "_http", return_value=(200, json.dumps(first_page))), \
             patch.object(bot, "_persist_polled_message") as persist, \
             patch.object(bot, "enqueue_inbound", return_value=True) as enqueue:
            result = bot.poll_ingest(self.settings)

        self.assertTrue(result["degraded"])
        persist.assert_not_called()
        enqueue.assert_called_once()
        cursor = IgPollCursor.objects.get(conversation_id="conv-long")
        self.assertEqual(cursor.last_message_id, "")

    def test_page_side_history_is_manager_evidence_with_provider_time(self):
        message = _message("manager-history", 1, sender="page")
        message["to"] = {"data": [{"id": "customer"}]}

        self.assertTrue(bot._persist_polled_message(self.settings, message, observed_only=True))

        row = InstagramBotMessage.objects.get(mid="manager-history")
        self.assertEqual(row.role, InstagramBotMessage.Role.MANAGER)
        self.assertEqual(row.sender_id, "customer")
        self.assertEqual(
            row.provider_created_at,
            bot._parse_ig_time(message["created_time"]),
        )

    def test_page_side_without_valid_recipient_does_not_advance_cursor(self):
        self._cache_conversations("conv-page-no-recipient")
        message = _message("page-no-recipient", 1, sender="page")
        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(
                 bot,
                 "_http",
                 return_value=(200, json.dumps({"messages": {"data": [message]}})),
             ):
            result = bot.poll_ingest(self.settings)

        self.assertTrue(result["degraded"])
        cursor = IgPollCursor.objects.get(conversation_id="conv-page-no-recipient")
        self.assertEqual(cursor.last_message_id, "")

    def test_live_page_side_message_starts_manager_takeover(self):
        self._cache_conversations("conv-live-manager")
        self.settings.allowed_senders = ""
        self.settings.reply_after = datetime(2026, 7, 9, 14, 0, tzinfo=timezone.utc)
        self.settings.save(update_fields=["allowed_senders", "reply_after"])
        message = _message("manager-live", 1, sender="page")
        message["to"] = {"data": [{"id": "customer-live-manager"}]}

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(
                 bot,
                 "_http",
                 return_value=(200, json.dumps({"messages": {"data": [message]}})),
             ), \
             patch("management.services.instagram_bot.notify_manager", return_value=True):
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["enqueued"], 0)
        client = bot.IgClient.objects.get(igsid="customer-live-manager")
        self.assertTrue(client.manager_takeover)
        self.assertTrue(client.bot_paused)
        row = InstagramBotMessage.objects.get(mid="manager-live")
        self.assertEqual(row.role, InstagramBotMessage.Role.MANAGER)
        self.assertEqual(row.provider_created_at, bot._parse_ig_time(message["created_time"]))

    def test_message_id_longer_than_mariadb_column_is_rejected(self):
        self._cache_conversations("conv-long-mid")
        payload = {"messages": {"data": [_message("m" * 256, 1)]}}
        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "_http", return_value=(200, json.dumps(payload))):
            result = bot.poll_ingest(self.settings)

        self.assertTrue(result["degraded"])
        self.assertFalse(InstagramBotMessage.objects.exists())

    def test_incomplete_then_complete_poll_queues_live_message_exactly_once(self):
        self._cache_conversations("conv-live-retry")
        self.settings.allowed_senders = ""
        self.settings.reply_after = datetime(2026, 7, 9, 14, 0, tzinfo=timezone.utc)
        self.settings.save(update_fields=["allowed_senders", "reply_after"])
        first_page = {
            "messages": {
                "data": [_message("live-2", 2)],
                "paging": {"next": f"{bot.GRAPH}/next-live"},
            }
        }
        last_page = {"messages": {"data": [_message("history-1", 0)]}}

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "_http", side_effect=[
                 (200, json.dumps(first_page)),
                 (503, "temporary"),
                 (200, json.dumps(first_page)),
                 (200, json.dumps(last_page)),
             ]), \
             patch("management.services.bot_sales_classifier.classify_message", return_value={}), \
             patch("management.services.bot_followups.schedule_after_inbound"):
            first = bot.poll_ingest(self.settings)
            second = bot.poll_ingest(self.settings)

        self.assertEqual(first["enqueued"], 1)
        self.assertEqual(second["enqueued"], 0)
        rows = InstagramBotMessage.objects.filter(mid="live-2")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().status, InstagramBotMessage.Status.PENDING)
        cursor = IgPollCursor.objects.get(conversation_id="conv-live-retry")
        self.assertEqual(cursor.last_message_id, "live-2")

    def test_incomplete_poll_keeps_pre_boundary_customer_message_observed(self):
        self._cache_conversations("conv-old-retry")
        self.settings.reply_after = datetime(2026, 7, 9, 14, 2, tzinfo=timezone.utc)
        self.settings.save(update_fields=["reply_after"])
        first_page = {
            "messages": {
                "data": [_message("history-1", 1)],
                "paging": {"next": f"{bot.GRAPH}/next-history"},
            }
        }

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "POLL_MESSAGE_MAX_PAGES", 1), \
             patch.object(bot, "_http", return_value=(200, json.dumps(first_page))):
            result = bot.poll_ingest(self.settings)

        self.assertEqual(result["enqueued"], 0)
        row = InstagramBotMessage.objects.get(mid="history-1")
        self.assertEqual(row.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(row.source, "poll_history")
