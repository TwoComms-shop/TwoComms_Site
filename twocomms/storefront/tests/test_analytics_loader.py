from pathlib import Path

from django.test import SimpleTestCase


class AnalyticsLoaderRegressionTests(SimpleTestCase):
    def test_bfcache_restore_uses_defined_pixel_initializer(self):
        loader_path = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme"
            / "static"
            / "js"
            / "analytics-loader.js"
        )
        source = loader_path.read_text(encoding="utf-8")

        self.assertNotIn("initializePixelsImmediately()", source)
        self.assertIn("function handleBFCacherestore(event)", source)
        self.assertIn("initializePixelsDeferred();", source)
