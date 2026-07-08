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
