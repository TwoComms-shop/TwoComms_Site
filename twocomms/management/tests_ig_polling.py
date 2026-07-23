import json
from datetime import datetime, timezone
from unittest.mock import patch

from django.test import TestCase
from django.core.cache import cache

from management.models import IgPollCursor, InstagramBotSettings
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


class ConversationDiscoveryTests(TestCase):
    def setUp(self):
        self.settings = InstagramBotSettings.load()
        self.settings.page_id = "page"
        self.settings.is_enabled = True
        self.settings.receive_via_poll = True
        self.settings.save(update_fields=["page_id", "is_enabled", "receive_via_poll"])
        cache.delete(bot._conv_cache_key(self.settings))

    def tearDown(self):
        cache.delete(bot._conv_cache_key(self.settings))

    def test_refresh_follows_pages_deduplicates_and_paces_requests(self):
        first = {"data": [{"id": "c1"}, {"id": "c2"}], "paging": {"next": f"{bot.GRAPH}/next-1"}}
        second = {"data": [{"id": "c2"}, {"id": "c3"}]}
        with patch.object(bot, "_http", side_effect=[(200, json.dumps(first)), (200, json.dumps(second))]) as http, \
             patch.object(bot.time, "sleep") as sleep:
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["c1", "c2", "c3"])
        self.assertEqual(http.call_count, 2)
        self.assertEqual(http.call_args_list[0].args[0], f"{bot.GRAPH}/page/conversations?platform=instagram&fields=id&limit=100&access_token=PT")
        sleep.assert_called_once_with(0.5)

    def test_failed_later_page_keeps_last_complete_snapshot(self):
        cache.set(bot._conv_cache_key(self.settings), ["old-1", "old-2"], 3600)
        first = {"data": [{"id": "new-1"}], "paging": {"next": f"{bot.GRAPH}/next-1"}}
        with patch.object(bot, "_http", side_effect=[(200, json.dumps(first)), (429, "quota")]), \
             patch.object(bot.time, "sleep"):
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["old-1", "old-2"])
        self.assertEqual(cache.get(bot._conv_cache_key(self.settings)), ["old-1", "old-2"])

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
        lock_key = f"ig_bot_conv_refresh:{self.settings.page_id}"
        cache.set(lock_key, "busy", 300)
        with patch.object(bot, "_http") as http:
            ids = bot.refresh_conv_ids(self.settings, "PT")

        self.assertEqual(ids, ["old"])
        http.assert_not_called()

    def test_cold_cache_does_not_block_poll_worker(self):
        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "get_conv_ids_cached", return_value=None), \
             patch.object(bot, "refresh_conv_ids") as refresh:
            result = bot.poll_ingest(self.settings)

        self.assertTrue(result["refresh_pending"])
        refresh.assert_not_called()


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

        self.assertEqual(result["enqueued"], 0)
        self.assertEqual(http.call_count, 2)
        enqueue.assert_not_called()
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

    def test_invalid_cached_conversation_ids_request_refresh_without_http(self):
        cache.set(bot._conv_cache_key(self.settings), [123, "conv-valid"], 3600)

        with patch.object(bot, "get_page_token", return_value="PT"), \
             patch.object(bot, "_http") as http:
            result = bot.poll_ingest(self.settings)

        self.assertTrue(result["refresh_pending"])
        self.assertEqual(result["conversations"], 0)
        http.assert_not_called()

    def test_failed_later_message_page_does_not_enqueue_or_advance_cursor(self):
        self._cache_conversations("conv-partial")
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

        self.assertEqual(result["enqueued"], 0)
        enqueue.assert_not_called()
        self.assertEqual(
            IgPollCursor.objects.get(conversation_id="conv-partial").last_message_id,
            "",
        )

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
