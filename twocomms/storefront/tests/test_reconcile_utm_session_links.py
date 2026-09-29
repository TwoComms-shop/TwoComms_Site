from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from storefront.management.commands.reconcile_utm_session_links import _apply_link
from storefront.models import SiteSession, UTMSession


class ReconcileUtmSessionLinksTests(TestCase):
    def _pair(self, key='exact-key', *, age_days=0):
        site = SiteSession.objects.create(session_key=key, first_touch_data={'keep': 'first-touch'})
        utm = UTMSession.objects.create(
            session_key=key, utm_source='instagram', utm_medium='paid_social',
            utm_campaign='launch', fbclid='original-click', is_converted=True,
            conversion_type='purchase', converted_at=timezone.now(),
        )
        if age_days:
            UTMSession.objects.filter(pk=utm.pk).update(first_seen=timezone.now() - timedelta(days=age_days))
            utm.refresh_from_db()
        return site, utm

    def _run(self, **options):
        output = StringIO()
        call_command('reconcile_utm_session_links', stdout=output, **options)
        return output.getvalue()

    def test_dry_run_reports_without_changing_any_row(self):
        site, utm = self._pair()
        snapshot = UTMSession.objects.values().get(pk=utm.pk)
        output = self._run()
        self.assertIn('recoverable=1', output)
        self.assertIn('updated=0', output)
        self.assertIn('dry_run=True', output)
        self.assertEqual(UTMSession.objects.values().get(pk=utm.pk), snapshot)
        self.assertEqual(SiteSession.objects.count(), 1)

    def test_apply_changes_only_fk_and_second_apply_is_noop(self):
        site, utm = self._pair()
        snapshot = UTMSession.objects.values().get(pk=utm.pk)
        output = self._run(apply=True)
        snapshot['session_id'] = site.pk
        self.assertEqual(UTMSession.objects.values().get(pk=utm.pk), snapshot)
        self.assertIn('updated=1', output)
        self.assertIn('dry_run=False', output)
        self.assertIn('updated=0', self._run(apply=True))
        self.assertEqual(UTMSession.objects.count(), 1)
        site.refresh_from_db()
        self.assertEqual(site.first_touch_data, {'keep': 'first-touch'})

    def test_missing_or_empty_session_key_does_not_create_or_infer_session(self):
        SiteSession.objects.create(session_key='different-key', visitor_id='same-visitor')
        UTMSession.objects.create(session_key='missing-key', visitor_id='same-visitor')
        UTMSession.objects.create(session_key='')
        output = self._run(apply=True)
        self.assertIn('missing_site_session=2', output)
        self.assertIn('updated=0', output)
        self.assertEqual(SiteSession.objects.count(), 1)
        self.assertEqual(UTMSession.objects.filter(session__isnull=True).count(), 2)

    def test_occupied_site_and_existing_link_are_not_overwritten(self):
        site, utm = self._pair()
        owner = UTMSession.objects.create(session_key='previous-key', session=site)
        already_linked_site, already_linked_utm = self._pair('already-linked-key')
        UTMSession.objects.filter(pk=already_linked_utm.pk).update(session=already_linked_site)
        output = self._run(apply=True)
        self.assertIn('already_linked_site_session=1', output)
        self.assertIn('updated=0', output)
        utm.refresh_from_db()
        owner.refresh_from_db()
        already_linked_utm.refresh_from_db()
        self.assertIsNone(utm.session_id)
        self.assertEqual(owner.session_id, site.pk)
        self.assertEqual(already_linked_utm.session_id, already_linked_site.pk)

    def test_default_seven_day_window_and_explicit_lookback(self):
        _, recent = self._pair('recent', age_days=6)
        _, older = self._pair('older', age_days=8)
        self.assertIn('updated=1', self._run(apply=True))
        recent.refresh_from_db()
        older.refresh_from_db()
        self.assertIsNotNone(recent.session_id)
        self.assertIsNone(older.session_id)
        self.assertIn('updated=1', self._run(apply=True, days=10))

    def test_small_batches_visit_every_initial_candidate(self):
        for index in range(5):
            self._pair(f'batch-key-{index}')
        output = self._run(apply=True, batch_size=2)
        self.assertIn('scanned=5', output)
        self.assertIn('updated=5', output)
        self.assertFalse(UTMSession.objects.filter(session__isnull=True).exists())

    def test_apply_rechecks_a_link_changed_after_plan(self):
        site, utm = self._pair()
        other_site = SiteSession.objects.create(session_key='other-key')

        def concurrent_link(utm_id, key, site_id):
            UTMSession.objects.filter(pk=utm_id).update(session=other_site)
            return _apply_link(utm_id, key, site_id)

        with patch(
            'storefront.management.commands.reconcile_utm_session_links._apply_link',
            side_effect=concurrent_link,
        ):
            output = self._run(apply=True)
        self.assertIn('changed_before_apply=1', output)
        self.assertIn('updated=0', output)
        utm.refresh_from_db()
        self.assertEqual(utm.session_id, other_site.pk)

    def test_apply_rechecks_site_occupancy(self):
        site, utm = self._pair()
        UTMSession.objects.create(session_key='concurrent-owner', session=site)
        self.assertFalse(_apply_link(utm.pk, utm.session_key, site.pk))
        utm.refresh_from_db()
        self.assertIsNone(utm.session_id)

    def test_invalid_options_fail_before_writes(self):
        _, utm = self._pair()
        for options in ({'days': 0}, {'days': -1}, {'batch_size': 0}, {'batch_size': 2001}):
            with self.subTest(options=options), self.assertRaises(CommandError):
                self._run(apply=True, **options)
        utm.refresh_from_db()
        self.assertIsNone(utm.session_id)
