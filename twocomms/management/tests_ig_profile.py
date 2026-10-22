"""Тести Phase 3 / Task 11 — підвантаження профілю IG-клієнта (Graph API).

При першому контакті бот тягне name/username/profile_pic через Graph і кладе в
картку, щоб у CRM було видно, хто саме пише (з аватаркою).
"""
from datetime import timedelta
import os
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from management.models import IgClient, InstagramBotSettings
from management.services import instagram_bot as bot


class _ConfiguredProviderMixin:
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "legacy_page"}))
        cache.clear()
        self.addCleanup(cache.clear)
        settings_obj = InstagramBotSettings.load()
        settings_obj.page_id = "synthetic-profile-owner"
        settings_obj.ig_user_id = "synthetic-profile-owner"
        settings_obj.save(update_fields=["page_id", "ig_user_id"])


class FetchProfileTests(_ConfiguredProviderMixin, TestCase):
    @patch("management.services.instagram_bot.get_page_token")
    @patch("management.services.instagram_bot._http")
    def test_fetch_parses_profile(self, mock_http, mock_pt):
        mock_pt.return_value = "PT"
        mock_http.return_value = (
            200, '{"name":"Іван","username":"ivan","profile_pic":"https://cdn/a.jpg"}'
        )
        prof = bot.fetch_ig_profile(InstagramBotSettings.load(), "u1")
        self.assertEqual(prof["name"], "Іван")
        self.assertEqual(prof["username"], "ivan")
        self.assertEqual(prof["profile_pic"], "https://cdn/a.jpg")

    @patch("management.services.instagram_bot.get_page_token")
    def test_fetch_empty_without_token(self, mock_pt):
        mock_pt.return_value = ""
        self.assertEqual(bot.fetch_ig_profile(InstagramBotSettings.load(), "u1"), {})


class EnsureProfileTests(_ConfiguredProviderMixin, TestCase):
    def tearDown(self):
        settings_obj = InstagramBotSettings.load()
        cache.delete(f"ig_profile_global_error:{settings_obj.page_id or 'unknown'}")

    @patch("management.services.instagram_bot.ensure_profile")
    @patch("management.services.instagram_bot.get_page_token", return_value="PT")
    def test_refresh_batch_is_bounded_and_skips_hidden_clients(self, _token, ensure):
        first = IgClient.get_or_create_for_sender("batch-1")
        second = IgClient.get_or_create_for_sender("batch-2")
        hidden = IgClient.get_or_create_for_sender("batch-hidden")
        hidden.hidden_at = timezone.now()
        hidden.save(update_fields=["hidden_at"])
        ensure.side_effect = lambda _s, client, force=False: client.pk == first.pk

        result = bot.refresh_profiles_batch(InstagramBotSettings.load(), limit=1)

        self.assertEqual(
            result,
            {"checked": 1, "updated": 1, "failed": 0, "state": "ok"},
        )
        self.assertEqual(ensure.call_count, 1)
        self.assertEqual(ensure.call_args.args[1].pk, first.pk)

    @patch("management.services.instagram_bot._localize_avatar")
    @patch("management.services.instagram_bot.fetch_ig_profile")
    def test_ensure_stores_profile(self, mock_fetch, mock_local):
        mock_fetch.return_value = {"name": "Іван", "username": "ivan", "profile_pic": "https://cdn/a.jpg"}
        mock_local.return_value = "/media/ig_avatars/u2.jpg"
        c = IgClient.get_or_create_for_sender("u2")
        self.assertTrue(bot.ensure_profile(InstagramBotSettings.load(), c))
        c.refresh_from_db()
        self.assertEqual(c.display_name, "Іван")
        self.assertEqual(c.username, "ivan")
        self.assertEqual(c.profile_pic_url, "https://cdn/a.jpg")
        self.assertEqual(c.avatar_local, "/media/ig_avatars/u2.jpg")
        self.assertIsNotNone(c.profile_fetched_at)

    @patch("management.services.instagram_bot.fetch_ig_profile")
    def test_ensure_skips_when_fresh_and_has_local_avatar(self, mock_fetch):
        c = IgClient.get_or_create_for_sender("u3")
        c.profile_fetched_at = timezone.now()
        c.avatar_local = "/media/ig_avatars/u3.jpg"
        c.save()
        self.assertFalse(bot.ensure_profile(InstagramBotSettings.load(), c))
        self.assertEqual(mock_fetch.call_count, 0)

    @patch("management.services.instagram_bot._localize_avatar")
    @patch("management.services.instagram_bot.fetch_ig_profile")
    def test_ensure_refetches_legacy_without_local_avatar(self, mock_fetch, mock_local):
        """Легасі-картка з profile_fetched_at, але без avatar_local — оновлюємо."""
        mock_fetch.return_value = {"name": "Оля", "username": "olya", "profile_pic": "https://cdn/b.jpg"}
        mock_local.return_value = "/media/ig_avatars/u4.jpg"
        c = IgClient.get_or_create_for_sender("u4")
        c.profile_fetched_at = timezone.now()
        c.save(update_fields=["profile_fetched_at"])
        self.assertTrue(bot.ensure_profile(InstagramBotSettings.load(), c))
        self.assertEqual(mock_fetch.call_count, 1)

    @patch("management.services.instagram_bot.get_page_token", return_value="")
    def test_refresh_batch_reports_missing_token(self, _token):
        result = bot.refresh_profiles_batch(InstagramBotSettings.load(), limit=5)

        self.assertEqual(
            result,
            {"checked": 0, "updated": 0, "failed": 0, "state": "no_token"},
        )

    @patch("management.services.instagram_bot._http")
    @patch("management.services.instagram_bot.get_page_token", return_value="PT")
    def test_permission_error_stops_profile_batch_after_one_graph_call(self, _token, http):
        http.return_value = (
            403,
            '{"error":{"code":200,"error_subcode":2534048,"message":"denied"}}',
        )
        for index in range(3):
            IgClient.get_or_create_for_sender(f"denied-{index}")

        result = bot.refresh_profiles_batch(InstagramBotSettings.load(), limit=3)

        self.assertEqual(http.call_count, 1)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["state"], "permission_denied")

    @patch("management.services.instagram_bot.ensure_profile", return_value=False)
    @patch("management.services.instagram_bot.get_page_token", return_value="PT")
    def test_failed_profile_is_persistently_backed_off_before_next_batch(
        self, _token, ensure
    ):
        first = IgClient.get_or_create_for_sender("retry-first")
        second = IgClient.get_or_create_for_sender("retry-second")

        first_result = bot.refresh_profiles_batch(InstagramBotSettings.load(), limit=1)
        second_result = bot.refresh_profiles_batch(InstagramBotSettings.load(), limit=1)

        self.assertEqual(first_result["failed"], 1)
        self.assertEqual(second_result["failed"], 1)
        self.assertEqual(
            [call.args[1].pk for call in ensure.call_args_list],
            [first.pk, second.pk],
        )
        first.refresh_from_db()
        self.assertEqual(first.profile_sync_failures, 1)
        self.assertGreater(first.profile_sync_next_at, timezone.now())

    @patch("management.services.instagram_bot.ensure_profile", return_value=True)
    @patch("management.services.instagram_bot.get_page_token", return_value="PT")
    def test_force_refresh_can_override_client_backoff(self, _token, ensure):
        client = IgClient.get_or_create_for_sender("retry-force")
        client.profile_sync_next_at = timezone.now() + timedelta(hours=1)
        client.save(update_fields=["profile_sync_next_at"])

        result = bot.refresh_profiles_batch(
            InstagramBotSettings.load(), limit=1, force=True
        )

        self.assertEqual(result["updated"], 1)
        ensure.assert_called_once()
