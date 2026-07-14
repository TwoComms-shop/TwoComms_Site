from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from orders.models import Order
from storefront.models import SiteSession, UTMSession


class NormalizeAiAttributionCommandTests(TestCase):
    def setUp(self):
        self.alias_site = SiteSession.objects.create(
            session_key='alias-session',
            first_touch_data={
                'utm_source': 'chatgpt.com',
                'utm_campaign': 'keep-campaign',
            },
        )
        self.alias_utm = UTMSession.objects.create(
            session=self.alias_site,
            session_key='alias-session',
            utm_source='chatgpt.com',
            utm_campaign='keep-campaign',
        )
        self.canonical_utm = UTMSession.objects.create(
            session_key='canonical-session',
            utm_source='chatgpt',
        )
        self.explicit_medium_utm = UTMSession.objects.create(
            session_key='explicit-medium-session',
            utm_source='chat.openai.com',
            utm_medium='referral',
        )
        self.instagram_utm = UTMSession.objects.create(
            session_key='instagram-session',
            utm_source='ig',
            utm_medium='social',
        )
        self.referrer_site = SiteSession.objects.create(
            session_key='referrer-session',
            first_touch_data={
                'referrer': 'https://chatgpt.com/c/example',
                'landing_path': '/catalog/',
            },
        )
        self.order = Order.objects.create(
            full_name='Historical AI Buyer',
            phone='+380501112233',
            city='Київ',
            np_office='Відділення №1',
            pay_type='cod',
            payment_status='unpaid',
            total_sum=130,
            source='web',
            utm_session=self.alias_utm,
            utm_source='chatgpt.com',
            utm_campaign='keep-campaign',
        )

    def _apply(self, **overrides):
        options = {
            'apply': True,
            'expect_utm_sessions': 3,
            'expect_site_sessions': 2,
            'expect_orders': 1,
            'stdout': StringIO(),
        }
        options.update(overrides)
        call_command('normalize_ai_attribution', **options)
        return options['stdout'].getvalue()

    def test_dry_run_reports_exact_candidates_without_writing(self):
        output = StringIO()

        call_command('normalize_ai_attribution', stdout=output)

        self.alias_utm.refresh_from_db()
        self.alias_site.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.alias_utm.utm_source, 'chatgpt.com')
        self.assertIsNone(self.alias_utm.utm_medium)
        self.assertEqual(self.alias_site.first_touch_data['utm_source'], 'chatgpt.com')
        self.assertEqual(self.order.utm_source, 'chatgpt.com')
        self.assertIn('utm_sessions=3', output.getvalue())
        self.assertIn('site_sessions=2', output.getvalue())
        self.assertIn('orders=1', output.getvalue())
        self.assertIn('conflicts=0', output.getvalue())
        self.assertIn('dry_run=True', output.getvalue())

    def test_apply_fails_closed_when_expected_counts_drift(self):
        with self.assertRaises(CommandError):
            self._apply(expect_utm_sessions=99)

        self.alias_utm.refresh_from_db()
        self.alias_site.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.alias_utm.utm_source, 'chatgpt.com')
        self.assertEqual(self.alias_site.first_touch_data['utm_source'], 'chatgpt.com')
        self.assertEqual(self.order.utm_source, 'chatgpt.com')

    def test_apply_is_narrow_and_idempotent(self):
        original_utm_count = UTMSession.objects.count()
        original_site_count = SiteSession.objects.count()
        original_order_id = self.order.pk
        original_utm_id = self.alias_utm.pk
        original_utm_times = (self.alias_utm.first_seen, self.alias_utm.last_seen)
        original_site_times = (self.alias_site.first_seen, self.alias_site.last_seen)

        output = self._apply()

        self.alias_utm.refresh_from_db()
        self.canonical_utm.refresh_from_db()
        self.explicit_medium_utm.refresh_from_db()
        self.instagram_utm.refresh_from_db()
        self.alias_site.refresh_from_db()
        self.referrer_site.refresh_from_db()
        self.order.refresh_from_db()

        self.assertEqual((self.alias_utm.utm_source, self.alias_utm.utm_medium), ('chatgpt', 'ai'))
        self.assertEqual(self.alias_utm.utm_campaign, 'keep-campaign')
        self.assertEqual((self.canonical_utm.utm_source, self.canonical_utm.utm_medium), ('chatgpt', 'ai'))
        self.assertEqual(
            (self.explicit_medium_utm.utm_source, self.explicit_medium_utm.utm_medium),
            ('chatgpt', 'referral'),
        )
        self.assertEqual((self.instagram_utm.utm_source, self.instagram_utm.utm_medium), ('ig', 'social'))
        self.assertEqual(self.alias_site.first_touch_data['utm_source'], 'chatgpt')
        self.assertEqual(self.alias_site.first_touch_data['utm_medium'], 'ai')
        self.assertEqual(self.alias_site.first_touch_data['utm_campaign'], 'keep-campaign')
        self.assertEqual(self.referrer_site.first_touch_data['utm_source'], 'chatgpt')
        self.assertEqual(self.referrer_site.first_touch_data['utm_medium'], 'ai')
        self.assertEqual(self.referrer_site.first_touch_data['landing_path'], '/catalog/')
        self.assertEqual((self.order.utm_source, self.order.utm_medium), ('chatgpt', 'ai'))
        self.assertEqual(self.order.utm_campaign, 'keep-campaign')
        self.assertEqual(self.order.pk, original_order_id)
        self.assertEqual(self.order.utm_session_id, original_utm_id)
        self.assertEqual((self.alias_utm.first_seen, self.alias_utm.last_seen), original_utm_times)
        self.assertEqual((self.alias_site.first_seen, self.alias_site.last_seen), original_site_times)
        self.assertEqual(UTMSession.objects.count(), original_utm_count)
        self.assertEqual(SiteSession.objects.count(), original_site_count)
        self.assertIn('updated_utm_sessions=3', output)
        self.assertIn('updated_site_sessions=2', output)
        self.assertIn('updated_orders=1', output)

        second_output = StringIO()
        call_command(
            'normalize_ai_attribution',
            apply=True,
            expect_utm_sessions=0,
            expect_site_sessions=0,
            expect_orders=0,
            stdout=second_output,
        )
        self.assertIn('updated_utm_sessions=0', second_output.getvalue())
        self.assertIn('updated_site_sessions=0', second_output.getvalue())
        self.assertIn('updated_orders=0', second_output.getvalue())

    def test_conflicting_linked_order_aborts_all_changes(self):
        self.order.utm_source = 'instagram'
        self.order.save(update_fields=['utm_source'])

        output = StringIO()
        call_command('normalize_ai_attribution', stdout=output)
        self.assertIn('conflicts=1', output.getvalue())

        with self.assertRaises(CommandError):
            self._apply()

        self.alias_utm.refresh_from_db()
        self.alias_site.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.alias_utm.utm_source, 'chatgpt.com')
        self.assertEqual(self.alias_site.first_touch_data['utm_source'], 'chatgpt.com')
        self.assertEqual(self.order.utm_source, 'instagram')

    def test_conflicting_linked_order_medium_aborts_all_changes(self):
        self.order.utm_medium = 'referral'
        self.order.save(update_fields=['utm_medium'])

        output = StringIO()
        call_command('normalize_ai_attribution', stdout=output)
        self.assertIn('conflicts=1', output.getvalue())

        with self.assertRaises(CommandError):
            self._apply()

        self.alias_utm.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual((self.alias_utm.utm_source, self.alias_utm.utm_medium), ('chatgpt.com', None))
        self.assertEqual((self.order.utm_source, self.order.utm_medium), ('chatgpt.com', 'referral'))

    def test_apply_rejects_values_that_changed_after_planning(self):
        from storefront.management.commands import normalize_ai_attribution as command_module

        collect_plan = command_module._collect_plan
        call_count = 0

        def collect_stale_plan():
            nonlocal call_count
            plan = collect_plan()
            if call_count == 0:
                UTMSession.objects.filter(pk=self.alias_utm.pk).update(
                    utm_source='instagram',
                    utm_medium='social',
                )
            call_count += 1
            return plan

        with patch.object(command_module, '_collect_plan', side_effect=collect_stale_plan):
            with self.assertRaises(CommandError):
                self._apply()

        self.alias_utm.refresh_from_db()
        self.alias_site.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual((self.alias_utm.utm_source, self.alias_utm.utm_medium), ('chatgpt.com', None))
        self.assertEqual(self.alias_site.first_touch_data['utm_source'], 'chatgpt.com')
        self.assertEqual(self.order.utm_source, 'chatgpt.com')

    def test_apply_rolls_back_all_models_when_a_save_fails(self):
        with patch.object(SiteSession, 'save', side_effect=RuntimeError('storage unavailable')):
            with self.assertRaises(RuntimeError):
                self._apply()

        self.alias_utm.refresh_from_db()
        self.alias_site.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual((self.alias_utm.utm_source, self.alias_utm.utm_medium), ('chatgpt.com', None))
        self.assertEqual(self.alias_site.first_touch_data['utm_source'], 'chatgpt.com')
        self.assertEqual((self.order.utm_source, self.order.utm_medium), ('chatgpt.com', None))
