from html.parser import HTMLParser

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import translation


class _MetaDescriptionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.description = ""

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta":
            return
        values = dict(attrs)
        if values.get("name", "").lower() == "description":
            self.description = values.get("content", "")


@override_settings(
    NOVA_POSHTA_FALLBACK_ENABLED=False,
    COMPRESS_ENABLED=False,
    COMPRESS_OFFLINE=False,
)
class CommercialMetaDescriptionTests(TestCase):
    def _description(self, route_name, language):
        with translation.override(language):
            response = self.client.get(reverse(route_name), secure=True)

        self.assertEqual(response.status_code, 200)
        parser = _MetaDescriptionParser()
        parser.feed(response.content.decode("utf-8"))
        self.assertTrue(parser.description)
        return parser.description

    def test_audit_outliers_fit_serp_length_without_losing_page_intent(self):
        cases = (
            ("cooperation", "uk", ("дропшипінг", "оптові закупівлі")),
            ("cooperation", "ru", ("дропшиппинг", "оптовые закупки")),
            ("cooperation", "en", ("dropshipping", "wholesale")),
            ("custom_print", "uk", ("DTF-друк", "футболках")),
            ("custom_print", "ru", ("DTF-печать", "футболках")),
            ("custom_print", "en", ("DTF printing", "T-shirts")),
            ("wholesale_page", "uk", ("8 одиниць", "менеджера")),
            ("wholesale_page", "ru", ("8 единиц", "менеджера")),
            ("wholesale_page", "en", ("8 units", "manager")),
            ("catalog", "uk", ("TwoComms", "DTF-друк")),
            ("catalog", "ru", ("TwoComms", "DTF-печать")),
            ("catalog", "en", ("military-inspired", "DTF")),
        )

        for route_name, language, expected_terms in cases:
            with self.subTest(route=route_name, language=language):
                description = self._description(route_name, language)
                self.assertGreaterEqual(len(description), 120)
                self.assertLessEqual(len(description), 160)
                for term in expected_terms:
                    self.assertIn(term, description)
