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
        first = {"messages": {"data": [_message("m4", 4), _message("m3", 3)], "paging": {"next": "https://graph/next"}}}
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
