import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "twocomms/twocomms_django_theme/static/js/language-suggestion.js"
BASE = REPO_ROOT / "twocomms/twocomms_django_theme/templates/base.html"
HEADER = REPO_ROOT / "twocomms/twocomms_django_theme/templates/partials/header.html"


class LanguageSuggestionSourceTests(unittest.TestCase):
    def test_ukrainian_exits_before_prompt_setup(self):
        source = SOURCE.read_text(encoding="utf-8")
        gate = "if (htmlLanguage === 'uk' || (htmlLanguage !== 'ru' && htmlLanguage !== 'en')) return;"
        self.assertIn(gate, source)
        self.assertLess(source.index(gate), source.index("storageKey"))

    def test_russian_and_english_copy_are_localized(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("Switch to Ukrainian?", source)
        self.assertIn("Перейти на украинский?", source)
        self.assertIn("Stay in English", source)
        self.assertIn("Остаться на русском", source)

    def test_decision_is_stored_once_for_the_browser(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("twocomms_language_suggestion_v3", source)
        self.assertIn("twocomms_language_suggestion_v1", source)
        self.assertIn("twocomms_language_suggestion_v2", source)
        self.assertIn("value.version === 3 && value.decision", source)
        self.assertIn("decision: { state: state, language: htmlLanguage, ts: Date.now() }", source)
        self.assertIn("hasLegacyDecision()", source)
        self.assertIn("if ((store && store.decision) || hasLegacyDecision() || !storageEnabled) return;", source)

    def test_switch_button_has_no_hardcoded_ukrainian_copy(self):
        template = BASE.read_text(encoding="utf-8")
        self.assertIn("data-language-suggestion-switch", template)
        self.assertIn("language_switch_links as language_suggestion_links", template)
        self.assertNotIn("next.value = window.location.href", SOURCE.read_text(encoding="utf-8"))
        self.assertNotIn(
            'class="language-suggestion__button language-suggestion__button--primary">Перейти українською',
            template,
        )

    def test_mobile_language_links_are_outside_fragment_cache(self):
        template = HEADER.read_text(encoding="utf-8")
        mobile_language = template.index('class="mobile-nav-language')
        self.assertLess(template.index("{% endcache %}"), mobile_language)


if __name__ == "__main__":
    unittest.main()
