import importlib
from unittest.mock import Mock, call

from django.apps import apps
from django.test import SimpleTestCase, TestCase

from storefront.management.commands.fix_seo_text_data import (
    _trim_to_word_boundary,
)


class CategorySeoTitleTrimTests(SimpleTestCase):
    def test_trim_drops_dangling_connector_words(self):
        cases = {
            "Футболки TwoComms — стрітвеар та мілітарі-принти від українського бренду": (
                "Футболки TwoComms — стрітвеар та мілітарі-принти"
            ),
            "Худі TwoComms — теплі толстовки зі стрітвеар-принтами та свободною посадкою": (
                "Худі TwoComms — теплі толстовки зі стрітвеар-принтами"
            ),
            "Лонгсліви TwoComms — лаконічний стрітвеар з рукавами на кожен день": (
                "Лонгсліви TwoComms — лаконічний стрітвеар з рукавами"
            ),
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(_trim_to_word_boundary(source, 60), expected)


class CategorySeoTitleRepairMigrationTests(TestCase):
    def test_repairs_only_exact_damaged_values_and_is_reversible(self):
        from storefront.models import Category

        migration = importlib.import_module(
            "storefront.migrations.0081_repair_truncated_category_seo_titles"
        )
        broken, repaired = migration.TITLE_REPAIRS["tshirts"]
        category = Category.objects.create(
            name="Футболки",
            slug="tshirts",
            seo_title=broken,
            seo_title_uk=broken,
        )

        migration.repair_titles(apps, None)
        category.refresh_from_db()
        self.assertEqual(category.seo_title, repaired)
        self.assertEqual(category.seo_title_uk, repaired)

        migration.reverse_repairs(apps, None)
        category.refresh_from_db()
        self.assertEqual(category.seo_title, broken)
        self.assertEqual(category.seo_title_uk, broken)

    def test_updates_are_guarded_by_exact_damaged_values(self):
        migration = importlib.import_module(
            "storefront.migrations.0081_repair_truncated_category_seo_titles"
        )
        category_model = Mock()
        queryset = category_model.objects.filter.return_value
        historical_apps = Mock()
        historical_apps.get_model.return_value = category_model

        migration.repair_titles(historical_apps, None)

        expected_filters = []
        expected_updates = []
        for slug, (broken, repaired) in migration.TITLE_REPAIRS.items():
            expected_filters.extend(
                [
                    call(slug=slug, seo_title=broken),
                    call(slug=slug, seo_title_uk=broken),
                ]
            )
            expected_updates.extend(
                [call(seo_title=repaired), call(seo_title_uk=repaired)]
            )
        self.assertEqual(category_model.objects.filter.call_args_list, expected_filters)
        self.assertEqual(queryset.update.call_args_list, expected_updates)
