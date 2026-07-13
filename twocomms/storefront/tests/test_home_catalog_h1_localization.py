import re

from django.core.cache import cache, caches
from django.test import TestCase


class HomeCatalogH1LocalizationTests(TestCase):
    def setUp(self):
        cache.clear()
        caches["fragments"].clear()

    def _h1_text(self, path):
        response = self.client.get(path, secure=True, HTTP_HOST="twocomms.shop")
        self.assertEqual(response.status_code, 200)
        match = re.search(
            r"<h1\b[^>]*>(.*?)</h1>",
            response.content.decode("utf-8"),
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        return " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split())

    def test_home_h1_is_localized_for_russian_and_english(self):
        self.assertEqual(
            self._h1_text("/ru/"),
            "TwoComms — украинский streetwear с кодом продолжения",
        )
        self.assertEqual(
            self._h1_text("/en/"),
            "TwoComms — Ukrainian streetwear built to carry the story forward",
        )

    def test_catalog_h1_is_localized_for_russian_and_english(self):
        self.assertEqual(
            self._h1_text("/ru/catalog/"),
            "Каталог одежды TwoComms",
        )
        self.assertEqual(
            self._h1_text("/en/catalog/"),
            "TwoComms clothing catalog",
        )
