from django.test import TestCase, override_settings


@override_settings(
    ALLOWED_HOSTS=["testserver", "management.twocomms.shop"],
    ROOT_URLCONF="twocomms.urls_management",
)
class InstagramBotPrivacyPolicyTests(TestCase):
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
