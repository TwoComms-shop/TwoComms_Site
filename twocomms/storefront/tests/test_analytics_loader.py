from pathlib import Path

from django.test import SimpleTestCase


class AnalyticsLoaderRegressionTests(SimpleTestCase):
    @staticmethod
    def _loader_source():
        loader_path = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme"
            / "static"
            / "js"
            / "analytics-loader.js"
        )
        return loader_path.read_text(encoding="utf-8")

    def test_bfcache_restore_uses_defined_pixel_initializer(self):
        source = self._loader_source()

        self.assertNotIn("initializePixelsImmediately()", source)
        self.assertIn("function handleBFCacherestore(event)", source)
        self.assertIn("initializePixelsDeferred();", source)

    def test_paid_landing_loads_tiktok_on_low_device(self):
        source = self._loader_source()

        self.assertIn(
            "var isPaidLanding = /[?&](gclid|fbclid|ttclid|wbraid|gbraid|msclkid|utm_source|utm_medium|utm_campaign)=/i.test(win.location.search);",
            source,
        )
        self.assertIn("if (!isLowDevice || isPaidLanding) {", source)
        self.assertIn(
            "if (isPaidLanding) {\n    initializePixelsDeferred();\n  } else {",
            source,
        )

    def test_base_template_uses_current_analytics_loader_version(self):
        template_path = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme"
            / "templates"
            / "base.html"
        )
        source = template_path.read_text(encoding="utf-8")

        self.assertIn("analytics-loader.js' %}?v=9", source)
