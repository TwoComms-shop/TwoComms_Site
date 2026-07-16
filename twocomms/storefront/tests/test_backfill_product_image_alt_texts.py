from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from storefront.models import Category, Product, ProductImage


class BackfillProductImageAltTextsCommandTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name='Худі', slug='hoodies-alt')
        self.product = Product.objects.create(
            title='Худі TwoComms Свобода',
            slug='hoodie-alt-test',
            category=category,
            price=1200,
            status='published',
        )
        self.first = ProductImage.objects.create(
            product=self.product,
            image='products/extra/first.jpg',
            order=0,
        )
        self.existing = ProductImage.objects.create(
            product=self.product,
            image='products/extra/existing.jpg',
            order=1,
            alt_text='Ручний опис спинки худі',
        )
        self.third = ProductImage.objects.create(
            product=self.product,
            image='products/extra/third.jpg',
            order=2,
            alt_text='',
        )

    def _apply(self, **overrides):
        options = {
            'apply': True,
            'expect_images': 2,
            'stdout': StringIO(),
        }
        options.update(overrides)
        call_command('backfill_product_image_alt_texts', **options)
        return options['stdout'].getvalue()

    def test_dry_run_reports_candidates_without_writing(self):
        output = StringIO()
        call_command('backfill_product_image_alt_texts', stdout=output)

        self.first.refresh_from_db()
        self.assertIsNone(self.first.alt_text)
        self.assertIn('images=2', output.getvalue())
        self.assertIn('dry_run=True', output.getvalue())

    def test_apply_updates_only_empty_alts_using_gallery_positions(self):
        output = self._apply()

        self.first.refresh_from_db()
        self.existing.refresh_from_db()
        self.third.refresh_from_db()
        self.assertEqual(self.first.alt_text, 'Худі TwoComms Свобода - фото 1 у галереї TwoComms')
        self.assertEqual(self.existing.alt_text, 'Ручний опис спинки худі')
        self.assertEqual(self.third.alt_text, 'Худі TwoComms Свобода - фото 3 у галереї TwoComms')
        self.assertIn('updated_images=2', output)

    def test_apply_requires_exact_guard_and_is_idempotent(self):
        with self.assertRaises(CommandError):
            call_command('backfill_product_image_alt_texts', apply=True, stdout=StringIO())
        with self.assertRaises(CommandError):
            self._apply(expect_images=99)

        self._apply()
        output = StringIO()
        call_command(
            'backfill_product_image_alt_texts',
            apply=True,
            expect_images=0,
            stdout=output,
        )
        self.assertIn('updated_images=0', output.getvalue())

    def test_apply_rolls_back_all_rows_when_save_fails(self):
        with patch.object(
            ProductImage.objects,
            'bulk_update',
            side_effect=RuntimeError('write failed'),
        ):
            with self.assertRaises(RuntimeError):
                self._apply()

        self.first.refresh_from_db()
        self.third.refresh_from_db()
        self.assertIsNone(self.first.alt_text)
        self.assertEqual(self.third.alt_text, '')
