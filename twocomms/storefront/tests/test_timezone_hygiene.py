import ast
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase
from django.utils import timezone

from orders.dropshipper_views import _current_reporting_year_month


def _naive_datetime_now_lines(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    datetime_class_aliases = set()
    datetime_module_aliases = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "datetime":
                    datetime_module_aliases.add(alias.asname or "datetime")
        elif isinstance(node, ast.ImportFrom) and node.module == "datetime":
            for alias in node.names:
                if alias.name == "datetime":
                    datetime_class_aliases.add(alias.asname or "datetime")

    offender_lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "now":
            continue

        owner = node.func.value
        direct_class_call = (
            isinstance(owner, ast.Name)
            and owner.id in datetime_class_aliases
        )
        module_class_call = (
            isinstance(owner, ast.Attribute)
            and owner.attr == "datetime"
            and isinstance(owner.value, ast.Name)
            and owner.value.id in datetime_module_aliases
        )
        if direct_class_call or module_class_call:
            offender_lines.append(node.lineno)

    return offender_lines


class TimezoneHygieneTests(SimpleTestCase):
    def test_dropshipper_reporting_month_uses_active_local_timezone(self):
        utc_new_year_boundary = datetime(2026, 12, 31, 22, 30, tzinfo=UTC)

        with (
            timezone.override("Europe/Kyiv"),
            patch("django.utils.timezone.now", return_value=utc_new_year_boundary),
        ):
            reporting_period = _current_reporting_year_month()

        self.assertEqual(reporting_period, (2027, 1))

    def test_no_naive_datetime_now_in_known_reporting_paths(self):
        root = Path(settings.BASE_DIR)
        paths = [
            root / "storefront" / "views" / "promo.py",
            root / "storefront" / "recommendations.py",
            root / "storefront" / "utm_api_views.py",
            root / "orders" / "dropshipper_views.py",
        ]

        offenders = {
            str(path.relative_to(root)): _naive_datetime_now_lines(path)
            for path in paths
            if _naive_datetime_now_lines(path)
        }

        self.assertEqual(offenders, {})
