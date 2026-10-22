"""Task 5 — локалізація аватарки IG-клієнта.

Проблема: profile_pic від Instagram — підписаний тимчасовий CDN-URL, що
протухає й має hotlink-захист, тож у CRM пізніше показує «битий» аватар.
Фікс: качаємо байти й зберігаємо у себе (media), у CRM віддаємо локальний URL.
"""
from unittest.mock import patch

from django.core.files.storage import default_storage
from django.test import TestCase, override_settings

from management.models import IgClient, InstagramBotSettings
from management.services import instagram_bot as bot


class LocalizeAvatarTests(TestCase):
    def tearDown(self):
        for name in ("ig_avatars/av1.jpg", "ig_avatars/avl1.jpg"):
            if default_storage.exists(name):
                default_storage.delete(name)

    @patch("management.services.instagram_bot.download_image")
    def test_localize_stores_file_and_returns_local_url(self, mock_dl):
        mock_dl.return_value = ("image/jpeg", b"\xff\xd8\xff\xe0fakejpegbytes")
        url = bot._localize_avatar("avl1", "https://cdn.instagram/expiring.jpg")
        self.assertTrue(url)
        self.assertIn("ig_avatars", url)
        self.assertTrue(default_storage.exists("ig_avatars/avl1.jpg"))

    @patch("management.services.instagram_bot.download_image")
    def test_localize_returns_empty_on_failed_download(self, mock_dl):
        mock_dl.return_value = None
        url = bot._localize_avatar("avl1", "https://cdn.instagram/expiring.jpg")
        self.assertEqual(url, "")


class EnsureProfileLocalizesTests(TestCase):
    def tearDown(self):
        if default_storage.exists("ig_avatars/av1.jpg"):
            default_storage.delete("ig_avatars/av1.jpg")

    @patch("management.services.instagram_bot.download_image")
    @patch("management.services.instagram_bot.fetch_ig_profile")
    def test_ensure_profile_sets_avatar_local(self, mock_fetch, mock_dl):
        mock_fetch.return_value = {
            "name": "Іван", "username": "ivan", "profile_pic": "https://cdn/a.jpg",
        }
        mock_dl.return_value = ("image/jpeg", b"\xff\xd8\xff\xe0jpeg")
        s = InstagramBotSettings.load()
        c = IgClient.get_or_create_for_sender("av1")
        ok = bot.ensure_profile(s, c)
        self.assertTrue(ok)
        c.refresh_from_db()
        self.assertTrue(c.avatar_local)
        self.assertIn("ig_avatars", c.avatar_local)

    @patch("management.services.instagram_bot.download_image")
    @patch("management.services.instagram_bot.fetch_ig_profile")
    def test_ensure_profile_does_not_fetch_for_an_erased_client(self, mock_fetch, mock_dl):
        s = InstagramBotSettings.load()
        c = IgClient.get_or_create_for_sender("erased-avatar")
        from django.utils import timezone

        c.privacy_erasure_started_at = timezone.now()
        c.save(update_fields=["privacy_erasure_started_at", "updated_at"])

        self.assertFalse(bot.ensure_profile(s, c))
        mock_fetch.assert_not_called()
        mock_dl.assert_not_called()


@override_settings(ROOT_URLCONF="twocomms.urls_management")
class ClientCardUsesLocalAvatarTests(TestCase):
    def test_card_prefers_local_avatar(self):
        from management.bot_views import _client_card

        c = IgClient.get_or_create_for_sender("av2")
        c.profile_pic_url = "https://cdn.instagram/expiring.jpg"
        c.avatar_local = "/media/ig_avatars/av2.jpg"
        c.save(update_fields=["profile_pic_url", "avatar_local"])
        card = _client_card(c)
        self.assertEqual(card["avatar"], "/media/ig_avatars/av2.jpg")
