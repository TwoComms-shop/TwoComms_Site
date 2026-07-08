from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from .bot_access import META_REVIEWER_GROUP_NAME


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
        self.assertNotContains(response, "custom_direct_token")
        self.assertNotContains(response, "custom_gemini_key")

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

    def test_meta_reviewer_gets_safe_read_only_bot_page(self):
        self._login_meta_reviewer()

        response = self.client.get(
            "/bot/",
            HTTP_HOST="management.twocomms.shop",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DIRECT_BOT Reviewer Access")
        self.assertContains(response, "Meta Bot Reviewer Access")
        self.assertContains(response, "2120980214971807")
        self.assertContains(response, "https://www.instagram.com/twocomms/")
        self.assertContains(response, "https://management.twocomms.shop/privacy-policy/")
        self.assertContains(response, "https://management.twocomms.shop/terms-of-service/")
        self.assertContains(response, "https://management.twocomms.shop/data-deletion/")
        self.assertNotContains(response, "custom_direct_token")
        self.assertNotContains(response, "custom_gemini_key")
        self.assertNotContains(response, "allowed_senders")
        self.assertNotContains(response, "Клієнти")
        self.assertNotContains(response, "Запустити")
        self.assertNotContains(response, "Зупинити")

    def test_meta_reviewer_cannot_call_admin_bot_apis(self):
        self._login_meta_reviewer()

        for path in (
            "/bot/api/start/",
            "/bot/api/stop/",
            "/bot/api/settings/",
            "/bot/api/clients/",
            "/bot/api/kb/",
        ):
            with self.subTest(path=path):
                method = self.client.get if path.endswith(("clients/", "kb/")) else self.client.post
                response = method(
                    path,
                    HTTP_HOST="management.twocomms.shop",
                    secure=True,
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                )
                self.assertEqual(response.status_code, 403)
