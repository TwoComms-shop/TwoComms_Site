from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.urls import reverse


THEME_ROOT = Path(__file__).resolve().parents[2] / "twocomms_django_theme"
BASE_TEMPLATE = THEME_ROOT / "templates" / "base.html"
LANGUAGE_JS = THEME_ROOT / "static" / "js" / "language-suggestion.js"
LANGUAGE_CSS = THEME_ROOT / "static" / "css" / "language-suggestion.css"


class LanguageSuggestionStaticTests(SimpleTestCase):
    def test_controller_has_human_only_delayed_locale_gate(self):
        source = LANGUAGE_JS.read_text(encoding="utf-8")
        self.assertIn("navigator.webdriver", source)
        self.assertIn("setTimeout(scheduleVisible, 7000)", source)
        self.assertIn("twocomms_language_suggestion_v3", source)
        self.assertIn("value.version === 3 && value.decision", source)

    def test_controller_localizes_stay_action_and_preserves_current_url(self):
        source = LANGUAGE_JS.read_text(encoding="utf-8")
        self.assertIn("Остаться на русском", source)
        self.assertIn("Stay in English", source)
        self.assertIn("next.value = window.location.href", source)
        self.assertIn('name="language" value="uk"', BASE_TEMPLATE.read_text(encoding="utf-8"))

    def test_styles_cover_mobile_and_reduced_motion(self):
        source = LANGUAGE_CSS.read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 520px)", source)
        self.assertIn("prefers-reduced-motion: reduce", source)
        self.assertIn("env(safe-area-inset-bottom)", source)


class LanguageSuggestionTemplateTests(TestCase):
    def test_home_contains_inert_mount_and_existing_seo_alternates(self):
        response = self.client.get(reverse("home"), HTTP_HOST="twocomms.shop")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn('id="language-suggestion"', html)
        self.assertIn("data-nosnippet", html)
        self.assertIn("language-suggestion.css", html)
        self.assertIn("language-suggestion.js", html)
        self.assertIn('hreflang="uk-UA"', html)
        self.assertIn('hreflang="x-default"', html)
        self.assertIn('aria-hidden="true"', html)
