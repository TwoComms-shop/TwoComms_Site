import base64
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from .bot_access import META_REVIEWER_GROUP_NAME
from .models import (
    BotDataDeletionRequest,
    IgClient,
    InstagramBotMessage,
    InstagramBotRawEvent,
    InstagramBotSettings,
)


@override_settings(
    ALLOWED_HOSTS=["testserver", "management.twocomms.shop"],
    ROOT_URLCONF="twocomms.urls_management",
)
class InstagramBotPrivacyPolicyTests(TestCase):
    def _login_meta_reviewer(self):
        user = get_user_model().objects.create_user(
            username="meta_reviewer_direct_bot",
            email="meta-reviewer@twocomms.shop",
            password="test-reviewer-password",
        )
        group = Group.objects.create(name=META_REVIEWER_GROUP_NAME)
        user.groups.add(group)
        self.client.force_login(user)
        return user

    def test_privacy_policy_is_public_without_login_redirect(self):
        response = self.client.get(
            "/privacy-policy/",
            HTTP_HOST="management.twocomms.shop",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("/login/", response.get("Location", ""))
        self.assertContains(response, "Privacy Policy for the twocomms Instagram Direct Bot")
        self.assertContains(response, "DIRECT_BOT")
        self.assertContains(response, "2120980214971807")
        self.assertContains(response, "https://www.instagram.com/twocomms/")
        self.assertContains(response, "Gemini 3.1 Flash")
        self.assertContains(response, "data deletion")
        self.assertContains(response, "cooperation@twocomms.shop")

    def test_bot_privacy_policy_alias_is_public(self):
        response = self.client.get(
            "/bot/privacy-policy/",
            HTTP_HOST="management.twocomms.shop",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Public policy URL")

    def test_terms_of_service_is_public_without_login_redirect(self):
        response = self.client.get(
            "/terms-of-service/",
            HTTP_HOST="management.twocomms.shop",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("/login/", response.get("Location", ""))
        self.assertContains(response, "Terms of Service for the twocomms Instagram Direct Bot")
        self.assertContains(response, "DIRECT_BOT")
        self.assertContains(response, "2120980214971807")
        self.assertContains(response, "https://management.twocomms.shop/data-deletion/")

    def test_data_deletion_instructions_are_public_without_login_redirect(self):
        response = self.client.get(
            "/data-deletion/",
            HTTP_HOST="management.twocomms.shop",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("/login/", response.get("Location", ""))
        self.assertContains(response, "User Data Deletion Instructions for DIRECT_BOT")
        self.assertContains(response, "Data Deletion Instructions URL")
        self.assertContains(response, "Data Deletion Callback URL")
        self.assertContains(response, "Delete DIRECT_BOT data")
        self.assertContains(response, "DIRECT_BOT data deletion request")
        self.assertContains(response, "cooperation@twocomms.shop")

    def test_bot_terms_and_data_deletion_aliases_are_public(self):
        for path, expected in (
            ("/bot/terms-of-service/", "Public Terms URL"),
            ("/bot/data-deletion/", "Public Data Deletion URL"),
        ):
            with self.subTest(path=path):
                response = self.client.get(
                    path,
                    HTTP_HOST="management.twocomms.shop",
                    secure=True,
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected)

    def test_app_review_info_is_public_without_exposing_controls(self):
        response = self.client.get(
            "/app-review/",
            HTTP_HOST="management.twocomms.shop",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("/login/", response.get("Location", ""))
        self.assertContains(response, "DIRECT_BOT App Review Information")
        self.assertContains(response, "Why the admin dashboard is not public")
        self.assertContains(response, "Reviewer testing flow")
        self.assertContains(response, "Recommended App Review notes")
        self.assertContains(response, "Screencast checklist")
        self.assertContains(response, "https://management.twocomms.shop/privacy-policy/")
        self.assertContains(response, "https://management.twocomms.shop/terms-of-service/")
        self.assertContains(response, "https://management.twocomms.shop/data-deletion/")
        self.assertContains(response, "https://management.twocomms.shop/data-deletion/request/")
        self.assertNotContains(response, "custom_direct_token")
        self.assertNotContains(response, "custom_gemini_key")

    def test_data_deletion_form_deletes_matching_direct_bot_records(self):
        client = IgClient.objects.create(igsid="123456789", username="delete_me")
        InstagramBotMessage.objects.create(
            sender_id="123456789",
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="please delete this",
            mid="mid-delete-me",
        )
        InstagramBotRawEvent.objects.create(sender_id="123456789", payload='{"text":"delete"}')

        response = self.client.post(
            "/data-deletion/submit/",
            {"identifier": "https://www.instagram.com/delete_me/"},
            HTTP_HOST="management.twocomms.shop",
            secure=True,
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deletion Request Status")
        deletion_request = BotDataDeletionRequest.objects.get()
        self.assertEqual(deletion_request.status, BotDataDeletionRequest.Status.COMPLETED)
        self.assertEqual(deletion_request.deleted_clients_count, 1)
        self.assertEqual(deletion_request.deleted_messages_count, 1)
        self.assertEqual(deletion_request.deleted_raw_events_count, 1)
        self.assertFalse(IgClient.objects.filter(igsid="123456789").exists())
        self.assertFalse(InstagramBotMessage.objects.filter(sender_id="123456789").exists())
        self.assertFalse(InstagramBotRawEvent.objects.filter(sender_id="123456789").exists())

    def test_data_deletion_callback_returns_meta_required_json(self):
        payload = {"user_id": "meta-user-123", "algorithm": "HMAC-SHA256"}
        encoded_payload = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        signed_request = "ignoredsig." + encoded_payload

        response = self.client.post(
            "/data-deletion/request/",
            {"signed_request": signed_request},
            HTTP_HOST="management.twocomms.shop",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("confirmation_code", data)
        self.assertTrue(data["url"].startswith("https://management.twocomms.shop/data-deletion/status/"))
        deletion_request = BotDataDeletionRequest.objects.get(confirmation_code=data["confirmation_code"])
        self.assertEqual(deletion_request.source, BotDataDeletionRequest.Source.META_CALLBACK)
        self.assertEqual(deletion_request.meta_user_id, "meta-user-123")

    def test_public_bot_dashboard_and_controls_remain_protected(self):
        dashboard = self.client.get(
            "/bot/",
            HTTP_HOST="management.twocomms.shop",
            secure=True,
        )
        self.assertEqual(dashboard.status_code, 302)
        self.assertIn("/login/", dashboard["Location"])

        for path in ("/bot/api/start/", "/bot/api/stop/"):
            with self.subTest(path=path):
                response = self.client.post(
                    path,
                    HTTP_HOST="management.twocomms.shop",
                    secure=True,
                )
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login/", response["Location"])

    def test_meta_reviewer_home_redirects_directly_to_bot(self):
        self._login_meta_reviewer()

        response = self.client.get(
            "/",
            HTTP_HOST="management.twocomms.shop",
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("management_bot"))

    def test_meta_reviewer_gets_working_bot_only_page_without_secrets(self):
        self._login_meta_reviewer()

        response = self.client.get(
            "/bot/",
            HTTP_HOST="management.twocomms.shop",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Інстаграм-бот")
        self.assertContains(response, "Meta reviewer mode")
        self.assertContains(response, "Meta Bot Reviewer")
        self.assertContains(response, "Запустити")
        self.assertContains(response, "Зупинити")
        self.assertContains(response, "Налаштування")
        self.assertContains(response, "Клієнти")
        self.assertContains(response, "is-disabled")
        self.assertNotContains(response, "custom_direct_token")
        self.assertNotContains(response, "custom_gemini_key")
        self.assertNotContains(response, "allowed_senders")
        self.assertNotContains(response, "Системний промпт")
        self.assertNotContains(response, "Інструкції, посилання та реклама")

    def test_meta_reviewer_can_use_bot_demo_apis_but_not_kb_admin_api(self):
        self._login_meta_reviewer()

        with patch("management.bot_views.bot.start_bot"), patch("management.bot_views.bot.stop_bot"):
            for path in ("/bot/api/start/", "/bot/api/stop/"):
                with self.subTest(path=path):
                    response = self.client.post(
                        path,
                        HTTP_HOST="management.twocomms.shop",
                        secure=True,
                        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                    )
                    self.assertEqual(response.status_code, 200)

        for path in ("/bot/api/status/", "/bot/api/clients/"):
            with self.subTest(path=path):
                response = self.client.get(
                    path,
                    HTTP_HOST="management.twocomms.shop",
                    secure=True,
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                )
                self.assertEqual(response.status_code, 200)

        response = self.client.get(
            "/bot/api/kb/",
            HTTP_HOST="management.twocomms.shop",
            secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 403)

    def test_meta_reviewer_settings_save_cannot_change_secret_fields(self):
        self._login_meta_reviewer()
        settings_obj = InstagramBotSettings.load()
        settings_obj.custom_direct_token = "keep-direct-secret"
        settings_obj.custom_gemini_key = "keep-gemini-secret"
        settings_obj.system_prompt = "keep-system-prompt"
        settings_obj.allowed_senders = "keep-sender"
        settings_obj.save()

        response = self.client.post(
            "/bot/api/settings/",
            {
                "ai_enabled": "on",
                "receive_via_poll": "on",
                "gemini_model": "gemini-2.5-flash",
                "custom_direct_token": "leaked-change",
                "custom_gemini_key": "leaked-change",
                "system_prompt": "changed",
                "allowed_senders": "changed",
            },
            HTTP_HOST="management.twocomms.shop",
            secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        settings_obj.refresh_from_db()
        self.assertTrue(settings_obj.ai_enabled)
        self.assertTrue(settings_obj.receive_via_poll)
        self.assertEqual(settings_obj.gemini_model, "gemini-2.5-flash")
        self.assertEqual(settings_obj.custom_direct_token, "keep-direct-secret")
        self.assertEqual(settings_obj.custom_gemini_key, "keep-gemini-secret")
        self.assertEqual(settings_obj.system_prompt, "keep-system-prompt")
        self.assertEqual(settings_obj.allowed_senders, "keep-sender")
