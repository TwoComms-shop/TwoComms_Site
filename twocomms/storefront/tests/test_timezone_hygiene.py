from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class TimezoneHygieneTests(SimpleTestCase):
    def test_no_naive_datetime_now_in_known_reporting_paths(self):
        root = Path(settings.BASE_DIR)
        paths = [
            root / "storefront" / "views" / "promo.py",
            root / "storefront" / "recommendations.py",
            root / "storefront" / "utm_api_views.py",
        ]

        offenders = [
            str(path.relative_to(root))
            for path in paths
            if "datetime.now(" in path.read_text(encoding="utf-8")
        ]

        self.assertEqual(offenders, [])
