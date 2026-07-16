from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from orders.models import Order
from storefront.models import SiteSession, UTMSession


class NormalizeUtmSourcesCommandTests(TestCase):
    def setUp(self):
        self.site = SiteSession.objects.create(
            session_key='legacy-instagram-session',
            first_touch_data={
                'utm_source': 'IGShopping',
                'utm_medium': 'paid_social',
                'utm_campaign': 'keep-campaign',
            },
        )
        self.utm = UTMSession.objects.create(
            session=self.site,
            session_key=self.site.session_key,
            utm_source='Instagram',
            utm_medium='paid_social',
            utm_campaign='keep-campaign',
        )
        self.order = Order.objects.create(
            full_name='Historical UTM Buyer',
            phone='+380501112233',
            city='Kyiv',
            np_office='Office 1',
            pay_type='cod',
            payment_status='unpaid',
            total_sum=100,
            source='web',
            utm_session=self.utm,
            utm_source='ig',
            utm_medium='paid_social',
            utm_campaign='keep-campaign',
        )
        self.facebook = UTMSession.objects.create(
            session_key='legacy-facebook-session',
            utm_source='fb',
            utm_medium='cpc',
        )
        self.case_only = UTMSession.objects.create(
            session_key='case-only-session',
            utm_source='fb-SiteLink',
            utm_medium='referral',
        )
        self.canonical = UTMSession.objects.create(
            session_key='canonical-session',
            utm_source='instagram',
            utm_medium='social',
        )
        self.unknown = UTMSession.objects.create(
            session_key='numeric-session',
            utm_source='120233970682840302',
        )

    def _apply(self, **overrides):
        options = {
            'apply': True,
            'expect_utm_sessions': 3,
            'expect_site_sessions': 1,
            'expect_orders': 1,
            'stdout': StringIO(),
        }
        options.update(overrides)
        call_command('normalize_utm_sources', **options)
        return options['stdout'].getvalue()

    def test_dry_run_reports_exact_candidates_without_writing(self):
        output = StringIO()

        call_command('normalize_utm_sources', stdout=output)

        self.utm.refresh_from_db()
        self.site.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.utm.utm_source, 'Instagram')
        self.assertEqual(self.site.first_touch_data['utm_source'], 'IGShopping')
        self.assertEqual(self.order.utm_source, 'ig')
        self.assertIn('utm_sessions=3', output.getvalue())
        self.assertIn('site_sessions=1', output.getvalue())
        self.assertIn('orders=1', output.getvalue())
        self.assertIn('conflicts=0', output.getvalue())
        self.assertIn('dry_run=True', output.getvalue())

    def test_apply_normalizes_only_source_and_preserves_other_fields(self):
        output = self._apply()

        self.utm.refresh_from_db()
        self.site.refresh_from_db()
        self.order.refresh_from_db()
        self.facebook.refresh_from_db()
        self.case_only.refresh_from_db()
        self.canonical.refresh_from_db()
        self.unknown.refresh_from_db()
        self.assertEqual((self.utm.utm_source, self.utm.utm_medium), ('instagram', 'paid_social'))
        self.assertEqual(self.utm.utm_campaign, 'keep-campaign')
        self.assertEqual(self.site.first_touch_data['utm_source'], 'instagram')
        self.assertEqual(self.site.first_touch_data['utm_medium'], 'paid_social')
        self.assertEqual(self.site.first_touch_data['utm_campaign'], 'keep-campaign')
        self.assertEqual((self.order.utm_source, self.order.utm_medium), ('instagram', 'paid_social'))
        self.assertEqual(self.order.utm_campaign, 'keep-campaign')
        self.assertEqual(self.facebook.utm_source, 'facebook')
        self.assertEqual(self.case_only.utm_source, 'fb-sitelink')
        self.assertEqual(self.canonical.utm_source, 'instagram')
        self.assertEqual(self.unknown.utm_source, '120233970682840302')
        self.assertIn('updated_utm_sessions=3', output)
        self.assertIn('updated_site_sessions=1', output)
        self.assertIn('updated_orders=1', output)

    def test_apply_requires_guards_and_is_idempotent(self):
        with self.assertRaises(CommandError):
            call_command('normalize_utm_sources', apply=True, stdout=StringIO())
        with self.assertRaises(CommandError):
            self._apply(expect_utm_sessions=99)

        self._apply()
        output = StringIO()
        call_command(
            'normalize_utm_sources',
            apply=True,
            expect_utm_sessions=0,
            expect_site_sessions=0,
            expect_orders=0,
            stdout=output,
        )
        self.assertIn('updated_utm_sessions=0', output.getvalue())
        self.assertIn('updated_site_sessions=0', output.getvalue())
        self.assertIn('updated_orders=0', output.getvalue())

    def test_conflicting_linked_order_aborts_all_updates(self):
        self.order.utm_source = 'facebook'
        self.order.save(update_fields=['utm_source'])

        output = StringIO()
        call_command('normalize_utm_sources', stdout=output)
        self.assertIn('conflicts=1', output.getvalue())
        with self.assertRaises(CommandError):
            self._apply(expect_orders=0)

        self.utm.refresh_from_db()
        self.site.refresh_from_db()
        self.assertEqual(self.utm.utm_source, 'Instagram')
        self.assertEqual(self.site.first_touch_data['utm_source'], 'IGShopping')

    def test_apply_rolls_back_all_models_when_a_save_fails(self):
        with patch.object(SiteSession, 'save', side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):
                self._apply()

        self.utm.refresh_from_db()
        self.site.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.utm.utm_source, 'Instagram')
        self.assertEqual(self.site.first_touch_data['utm_source'], 'IGShopping')
        self.assertEqual(self.order.utm_source, 'ig')
