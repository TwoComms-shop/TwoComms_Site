from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from storefront.models import SiteSession, UTMSession, UserAction


class BackfillClickIdAttributionCommandTests(TestCase):
    def setUp(self):
        self.facebook_site = SiteSession.objects.create(
            session_key='facebook-click-session',
            visitor_id='visitor-facebook',
            ip_address='203.0.113.10',
            user_agent='Audit Browser',
            first_touch_data={
                'fbclid': 'fb-click-123',
                'landing_path': '/catalog/hoodies/',
                'referrer': 'https://facebook.com/',
            },
        )
        self.facebook_actions = [
            UserAction.objects.create(
                site_session=self.facebook_site,
                action_type='product_view',
                product_id=101,
            ),
            UserAction.objects.create(
                site_session=self.facebook_site,
                action_type='add_to_cart',
                product_id=101,
            ),
        ]

        self.google_site = SiteSession.objects.create(
            session_key='google-click-session',
            first_touch_data={'gclid': 'google-click-456'},
        )
        self.google_utm = UTMSession.objects.create(
            session_key=self.google_site.session_key,
            utm_source='newsletter',
            utm_medium='email',
            gclid='google-click-456',
        )
        self.google_action = UserAction.objects.create(
            site_session=self.google_site,
            action_type='custom_print_start',
        )
        self.already_linked_action = UserAction.objects.create(
            site_session=self.facebook_site,
            utm_session=self.google_utm,
            action_type='click',
        )

        self.organic_site = SiteSession.objects.create(
            session_key='organic-session',
            first_touch_data={'landing_path': '/catalog/'},
        )
        self.organic_action = UserAction.objects.create(
            site_session=self.organic_site,
            action_type='product_view',
            product_id=202,
        )

        self.ambiguous_site = SiteSession.objects.create(
            session_key='ambiguous-click-session',
            first_touch_data={'fbclid': 'fb-ambiguous', 'gclid': 'g-ambiguous'},
        )
        self.ambiguous_action = UserAction.objects.create(
            site_session=self.ambiguous_site,
            action_type='product_view',
            product_id=303,
        )

    def _apply(self, **overrides):
        options = {
            'apply': True,
            'expect_sessions': 2,
            'expect_actions': 3,
            'stdout': StringIO(),
        }
        options.update(overrides)
        call_command('backfill_click_id_attribution', **options)
        return options['stdout'].getvalue()

    def test_dry_run_reports_exact_candidates_without_writing(self):
        output = StringIO()

        call_command('backfill_click_id_attribution', stdout=output)

        self.assertFalse(UTMSession.objects.filter(session=self.facebook_site).exists())
        self.assertIsNone(UserAction.objects.get(pk=self.facebook_actions[0].pk).utm_session_id)
        self.assertIn('sessions=2', output.getvalue())
        self.assertIn('actions=3', output.getvalue())
        self.assertIn('create_sessions=1', output.getvalue())
        self.assertIn('reuse_sessions=1', output.getvalue())
        self.assertIn('ambiguous_sessions=1', output.getvalue())
        self.assertIn('dry_run=True', output.getvalue())

    def test_apply_creates_or_reuses_sessions_and_links_only_null_actions(self):
        output = self._apply()

        facebook_utm = UTMSession.objects.get(session=self.facebook_site)
        self.assertEqual(facebook_utm.session_key, self.facebook_site.session_key)
        self.assertEqual((facebook_utm.utm_source, facebook_utm.utm_medium), ('facebook', 'paid_social'))
        self.assertEqual(facebook_utm.fbclid, 'fb-click-123')
        self.assertEqual(facebook_utm.visitor_id, 'visitor-facebook')
        self.assertEqual(facebook_utm.ip_address, '203.0.113.10')
        self.assertEqual(facebook_utm.user_agent, 'Audit Browser')
        self.assertEqual(facebook_utm.landing_page, '/catalog/hoodies/')
        self.assertEqual(facebook_utm.referrer, 'https://facebook.com/')

        self.google_utm.refresh_from_db()
        self.assertEqual(self.google_utm.session_id, self.google_site.pk)
        self.assertEqual((self.google_utm.utm_source, self.google_utm.utm_medium), ('newsletter', 'email'))
        self.google_action.refresh_from_db()
        self.assertEqual(self.google_action.utm_session_id, self.google_utm.pk)

        for action in self.facebook_actions:
            action.refresh_from_db()
            self.assertEqual(action.utm_session_id, facebook_utm.pk)
        self.already_linked_action.refresh_from_db()
        self.assertEqual(self.already_linked_action.utm_session_id, self.google_utm.pk)
        self.organic_action.refresh_from_db()
        self.ambiguous_action.refresh_from_db()
        self.assertIsNone(self.organic_action.utm_session_id)
        self.assertIsNone(self.ambiguous_action.utm_session_id)
        self.assertIn('updated_sessions=2', output)
        self.assertIn('updated_actions=3', output)

    def test_apply_requires_exact_expectation_guards(self):
        with self.assertRaises(CommandError):
            call_command('backfill_click_id_attribution', apply=True, stdout=StringIO())
        with self.assertRaises(CommandError):
            self._apply(expect_actions=99)

        self.assertFalse(UTMSession.objects.filter(session=self.facebook_site).exists())
        self.assertIsNone(UserAction.objects.get(pk=self.facebook_actions[0].pk).utm_session_id)

    def test_second_apply_is_idempotent(self):
        self._apply()

        output = StringIO()
        call_command(
            'backfill_click_id_attribution',
            apply=True,
            expect_sessions=0,
            expect_actions=0,
            stdout=output,
        )

        self.assertIn('updated_sessions=0', output.getvalue())
        self.assertIn('updated_actions=0', output.getvalue())

    def test_apply_rolls_back_session_and_action_writes_together(self):
        with patch.object(UserAction.objects, 'bulk_update', side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):
                self._apply()

        self.assertFalse(UTMSession.objects.filter(session=self.facebook_site).exists())
        self.assertIsNone(self.google_utm.session_id)
        self.assertIsNone(UserAction.objects.get(pk=self.facebook_actions[0].pk).utm_session_id)
        self.assertIsNone(UserAction.objects.get(pk=self.google_action.pk).utm_session_id)
