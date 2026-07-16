from datetime import timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from orders.models import Order
from storefront.models import SiteSession, UTMSession, UserAction


@override_settings(META_FBC_ATTRIBUTION_WINDOW_DAYS=7)
class BackfillFbcOrderAttributionCommandTests(TestCase):
    GUARD_NAMES = (
        'expect_groups',
        'expect_orders',
        'expect_stale',
        'expect_no_key',
        'expect_invalid',
        'expect_conflicting',
        'expect_create_sessions',
        'expect_reuse_sessions',
    )

    def _fbc(self, created_at, click_id='meta-click'):
        return f'fb.1.{int(created_at.timestamp() * 1000)}.{click_id}'

    def _order(
        self,
        *,
        key=None,
        external_key=None,
        fbc=None,
        fbclid=None,
        gclid=None,
        ttclid=None,
        created=None,
        source='web',
        **overrides,
    ):
        payload = overrides.pop('payment_payload', {})
        payload = dict(payload) if isinstance(payload, dict) else payload
        if isinstance(payload, dict):
            tracking = dict(payload.get('tracking') or {})
            if external_key is not None:
                tracking['external_id'] = f'session:{external_key}'
            for name, value in (
                ('fbc', fbc),
                ('fbclid', fbclid),
                ('gclid', gclid),
                ('ttclid', ttclid),
            ):
                if value is not None:
                    tracking[name] = value
            if tracking:
                payload['tracking'] = tracking

        data = {
            'full_name': 'Historical FBC Buyer',
            'phone': '+380991112233',
            'city': 'Kyiv',
            'np_office': 'Branch 1',
            'pay_type': 'prepay_200',
            'status': 'done',
            'source': source,
            'session_key': key,
            'payment_status': 'paid',
            'total_sum': '1200.00',
            'payment_payload': payload,
        }
        data.update(overrides)
        order = Order.objects.create(**data)
        if created is not None:
            Order.objects.filter(pk=order.pk).update(created=created)
            order.refresh_from_db()
        return order

    def _fresh_order(self, *, key='a' * 32, click_id='meta-click', **overrides):
        created = overrides.pop('created', timezone.now())
        fbc = overrides.pop('fbc', self._fbc(created - timedelta(days=2), click_id))
        return self._order(key=key, fbc=fbc, created=created, **overrides)

    def _apply(
        self,
        *,
        groups,
        orders,
        stale=0,
        no_key=0,
        invalid=0,
        conflicting=0,
        create_sessions=None,
        reuse_sessions=0,
    ):
        if create_sessions is None:
            create_sessions = groups
        output = StringIO()
        call_command(
            'backfill_fbc_order_attribution',
            apply=True,
            expect_groups=groups,
            expect_orders=orders,
            expect_stale=stale,
            expect_no_key=no_key,
            expect_invalid=invalid,
            expect_conflicting=conflicting,
            expect_create_sessions=create_sessions,
            expect_reuse_sessions=reuse_sessions,
            stdout=output,
        )
        return output.getvalue()

    def test_dry_run_reports_aggregates_only_and_does_not_write(self):
        key = 'a' * 32
        order = self._fresh_order(key=key, click_id='private-click-value')
        original_payload = order.payment_payload
        output = StringIO()

        call_command('backfill_fbc_order_attribution', stdout=output)

        order.refresh_from_db()
        report = output.getvalue()
        self.assertIn('scanned=1', report)
        self.assertIn('eligible=1', report)
        self.assertIn('linkable_groups=1', report)
        self.assertIn('linkable_orders=1', report)
        self.assertIn('create_sessions=1', report)
        self.assertIn('reuse_sessions=0', report)
        self.assertIn('dry_run=True', report)
        self.assertNotIn(key, report)
        self.assertNotIn('private-click-value', report)
        self.assertNotIn(order.order_number, report)
        self.assertFalse(UTMSession.objects.exists())
        self.assertIsNone(order.utm_session_id)
        self.assertEqual(order.payment_payload, original_payload)

    def test_apply_requires_every_exact_guard_and_rejects_drift(self):
        order = self._fresh_order()

        with self.assertRaises(CommandError):
            call_command('backfill_fbc_order_attribution', apply=True, stdout=StringIO())
        for missing_guard in self.GUARD_NAMES:
            kwargs = {
                'apply': True,
                'expect_groups': 1,
                'expect_orders': 1,
                'expect_stale': 0,
                'expect_no_key': 0,
                'expect_invalid': 0,
                'expect_conflicting': 0,
                'expect_create_sessions': 1,
                'expect_reuse_sessions': 0,
                'stdout': StringIO(),
            }
            kwargs.pop(missing_guard)
            with self.assertRaises(CommandError):
                call_command('backfill_fbc_order_attribution', **kwargs)
        with self.assertRaises(CommandError):
            self._apply(groups=1, orders=99)

        order.refresh_from_db()
        self.assertIsNone(order.utm_session_id)
        self.assertFalse(UTMSession.objects.exists())

    def test_apply_creates_one_session_and_only_links_allowed_fields(self):
        key = 'b' * 32
        site = SiteSession.objects.create(session_key=key)
        order = self._fresh_order(
            key=key,
            external_key=key,
            click_id='validated-click',
            payment_payload={'keep': {'nested': 'value'}},
        )
        original_payload = order.payment_payload
        original_session_key = order.session_key
        click_time = timezone.datetime.fromtimestamp(
            int(order.payment_payload['tracking']['fbc'].split('.')[2]) / 1000,
            tz=dt_timezone.utc,
        )

        report = self._apply(groups=1, orders=1)

        order.refresh_from_db()
        utm = UTMSession.objects.get()
        self.assertIn('updated_orders=1', report)
        self.assertIn('created_sessions=1', report)
        self.assertEqual(order.utm_session_id, utm.pk)
        self.assertEqual(
            (
                order.utm_source,
                order.utm_medium,
                order.utm_campaign,
                order.utm_content,
                order.utm_term,
            ),
            ('facebook', 'paid_social', None, None, None),
        )
        self.assertEqual(order.session_key, original_session_key)
        self.assertEqual(order.payment_payload, original_payload)
        self.assertEqual(utm.session_id, site.pk)
        self.assertEqual(utm.session_key, key)
        self.assertEqual(utm.utm_source, 'facebook')
        self.assertEqual(utm.utm_medium, 'paid_social')
        self.assertEqual(utm.fbc, order.payment_payload['tracking']['fbc'])
        self.assertEqual(utm.fbclid, 'validated-click')
        self.assertIsNone(utm.fbp)
        self.assertIsNone(utm.gclid)
        self.assertIsNone(utm.ttclid)
        self.assertIsNone(utm.utm_campaign)
        self.assertIsNone(utm.utm_content)
        self.assertIsNone(utm.utm_term)
        self.assertEqual(utm.first_seen, click_time)
        self.assertEqual(utm.last_seen, click_time)
        self.assertFalse(UserAction.objects.exists())

    def test_identical_same_key_orders_share_one_session(self):
        key = 'c' * 32
        created = timezone.now()
        fbc = self._fbc(created - timedelta(days=1), 'shared-click')
        first = self._order(key=key, fbc=fbc, created=created)
        second = self._order(external_key=key, fbc=fbc, created=created + timedelta(hours=1))

        report = self._apply(groups=1, orders=2)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIn('linkable_groups=1', report)
        self.assertIn('linkable_orders=2', report)
        self.assertEqual(UTMSession.objects.count(), 1)
        self.assertEqual(first.utm_session_id, second.utm_session_id)

    def test_same_key_with_different_fbc_skips_entire_group(self):
        conflict_key = 'd' * 32
        safe = self._fresh_order(key='e' * 32, click_id='safe-click')
        first = self._fresh_order(key=conflict_key, click_id='first-click')
        second = self._fresh_order(key=conflict_key, click_id='second-click')

        report = self._apply(groups=1, orders=1, conflicting=2)

        safe.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIn('eligible=3', report)
        self.assertIn('conflicting=2', report)
        self.assertIsNotNone(safe.utm_session_id)
        self.assertIsNone(first.utm_session_id)
        self.assertIsNone(second.utm_session_id)

    def test_invalid_or_stale_member_poisons_every_resolvable_key_group(self):
        now = timezone.now()
        stale_key = '7' * 32
        future_key = '8' * 32
        malformed_key = '9' * 32
        mismatch_order_key = 'a' * 32
        mismatch_external_key = 'b' * 32
        fresh_orders = [
            self._fresh_order(key=stale_key, click_id='fresh-stale-peer'),
            self._fresh_order(key=future_key, click_id='fresh-future-peer'),
            self._fresh_order(key=malformed_key, click_id='fresh-malformed-peer'),
            self._fresh_order(key=mismatch_order_key, click_id='fresh-order-key-peer'),
            self._fresh_order(key=mismatch_external_key, click_id='fresh-external-key-peer'),
        ]
        self._order(
            key=stale_key,
            fbc=self._fbc(now - timedelta(days=8), 'stale-click'),
            created=now,
        )
        self._order(
            key=future_key,
            fbc=self._fbc(now + timedelta(minutes=10), 'future-click'),
            created=now,
        )
        self._order(key=malformed_key, fbc='fb.1.bad.click', created=now)
        self._fresh_order(
            key=mismatch_order_key,
            external_key=mismatch_external_key,
            click_id='mismatched-key-click',
        )

        report = self._apply(
            groups=0,
            orders=0,
            stale=1,
            invalid=3,
            conflicting=5,
        )

        self.assertIn('stale=1', report)
        self.assertIn('invalid=3', report)
        self.assertIn('conflicting=5', report)
        for order in fresh_orders:
            order.refresh_from_db()
            self.assertIsNone(order.utm_session_id)
        self.assertFalse(UTMSession.objects.exists())

    def test_residual_categories_cover_stale_future_malformed_fbp_and_keys(self):
        now = timezone.now()
        stale = self._order(
            key='f' * 32,
            fbc=self._fbc(now - timedelta(days=8), 'stale-click'),
            created=now,
        )
        future = self._order(
            key='g' * 32,
            fbc=self._fbc(now + timedelta(minutes=10), 'future-click'),
            created=now,
        )
        malformed = self._order(key='h' * 32, fbc='fb.1.bad.click')
        fbp_only = self._order(
            key='i' * 32,
            payment_payload={'tracking': {'fbp': 'fb.1.1700000000000.browser-id'}},
        )
        invalid_key = self._fresh_order(key='not-a-valid-session-key')
        mismatch = self._fresh_order(key='j' * 32, external_key='k' * 32)
        no_key = self._fresh_order(key=None)

        report = self._apply(groups=0, orders=0, stale=1, no_key=1, invalid=5)

        self.assertIn('scanned=7', report)
        self.assertIn('stale=1', report)
        self.assertIn('no_key=1', report)
        self.assertIn('invalid=5', report)
        self.assertIn('eligible=3', report)
        for order in (stale, future, malformed, fbp_only, invalid_key, mismatch, no_key):
            order.refresh_from_db()
            self.assertIsNone(order.utm_session_id)
        self.assertFalse(UTMSession.objects.exists())

    def test_raw_click_id_conflicts_skip_order(self):
        created = timezone.now()
        fbc = self._fbc(created - timedelta(days=1), 'fbc-click')
        matching = self._order(
            key='m' * 32,
            fbc=fbc,
            fbclid='fbc-click',
            created=created,
        )
        fb_mismatch = self._order(
            key='n' * 32,
            fbc=fbc,
            fbclid='different-click',
            created=created,
        )
        google = self._order(
            key='o' * 32,
            fbc=fbc,
            gclid='google-click',
            created=created,
        )
        tiktok = self._order(
            key='p' * 32,
            fbc=fbc,
            ttclid='tiktok-click',
            created=created,
        )

        self._apply(groups=1, orders=1, conflicting=3)

        matching.refresh_from_db()
        self.assertIsNotNone(matching.utm_session_id)
        for order in (fb_mismatch, google, tiktok):
            order.refresh_from_db()
            self.assertIsNone(order.utm_session_id)

    def test_reuses_only_non_conflicting_utm_session(self):
        reusable_key = 'q' * 32
        conflicting_key = 'r' * 32
        reusable = self._fresh_order(key=reusable_key, click_id='reuse-click')
        conflict = self._fresh_order(key=conflicting_key, click_id='new-click')
        reusable_utm = UTMSession.objects.create(session_key=reusable_key)
        conflicting_utm = UTMSession.objects.create(
            session_key=conflicting_key,
            utm_source='newsletter',
            utm_medium='email',
        )

        report = self._apply(
            groups=1,
            orders=1,
            conflicting=1,
            create_sessions=0,
            reuse_sessions=1,
        )

        reusable.refresh_from_db()
        conflict.refresh_from_db()
        reusable_utm.refresh_from_db()
        conflicting_utm.refresh_from_db()
        self.assertIn('create_sessions=0', report)
        self.assertIn('reuse_sessions=1', report)
        self.assertEqual(reusable.utm_session_id, reusable_utm.pk)
        self.assertEqual(reusable_utm.utm_source, 'facebook')
        self.assertIsNone(conflict.utm_session_id)
        self.assertEqual(conflicting_utm.utm_source, 'newsletter')

    def test_create_to_reuse_drift_aborts_before_mutating_existing_session(self):
        key = 'c' * 32
        order = self._fresh_order(key=key, click_id='guarded-click')
        existing = UTMSession.objects.create(session_key=key)
        original_timestamps = (existing.first_seen, existing.last_seen)

        with self.assertRaises(CommandError):
            self._apply(
                groups=1,
                orders=1,
                create_sessions=1,
                reuse_sessions=0,
            )

        order.refresh_from_db()
        existing.refresh_from_db()
        self.assertIsNone(order.utm_session_id)
        self.assertIsNone(existing.utm_source)
        self.assertIsNone(existing.fbc)
        self.assertEqual((existing.first_seen, existing.last_seen), original_timestamps)

    def test_reuse_is_blocked_when_out_of_scope_order_already_uses_session(self):
        key = 'd' * 32
        existing = UTMSession.objects.create(session_key=key)
        candidate = self._fresh_order(key=key, click_id='candidate-click')
        outside = self._fresh_order(
            key='e' * 32,
            source='manual',
            utm_session=existing,
            utm_source='manual-source',
        )

        report = self._apply(groups=0, orders=0, conflicting=1)

        candidate.refresh_from_db()
        outside.refresh_from_db()
        existing.refresh_from_db()
        self.assertIn('conflicting=1', report)
        self.assertIsNone(candidate.utm_session_id)
        self.assertEqual(outside.utm_session_id, existing.pk)
        self.assertEqual(outside.utm_source, 'manual-source')
        self.assertIsNone(existing.utm_source)

    def test_reused_session_preserves_existing_timestamps(self):
        key = 'f' * 32
        order = self._fresh_order(key=key, click_id='reuse-timestamp-click')
        existing = UTMSession.objects.create(session_key=key)
        first_seen = timezone.now() - timedelta(days=30)
        last_seen = timezone.now() - timedelta(days=20)
        UTMSession.objects.filter(pk=existing.pk).update(
            first_seen=first_seen,
            last_seen=last_seen,
        )

        self._apply(
            groups=1,
            orders=1,
            create_sessions=0,
            reuse_sessions=1,
        )

        order.refresh_from_db()
        existing.refresh_from_db()
        self.assertEqual(order.utm_session_id, existing.pk)
        self.assertEqual(existing.first_seen, first_seen)
        self.assertEqual(existing.last_seen, last_seen)

    def test_site_session_and_user_action_linkage_conflicts_skip_groups(self):
        site_conflict_key = 's' * 32
        action_conflict_key = 't' * 32
        other_site = SiteSession.objects.create(session_key='u' * 32)
        site = SiteSession.objects.create(session_key=site_conflict_key)
        UTMSession.objects.create(
            session_key='v' * 32,
            session=site,
            utm_source='google',
            utm_medium='cpc',
        )
        site_conflict = self._fresh_order(key=site_conflict_key)
        action_conflict = self._fresh_order(key=action_conflict_key)
        action = UserAction.objects.create(
            action_type='purchase',
            order_id=action_conflict.pk,
            site_session=other_site,
        )

        report = self._apply(groups=0, orders=0, conflicting=2)

        self.assertIn('conflicting=2', report)
        site_conflict.refresh_from_db()
        action_conflict.refresh_from_db()
        action.refresh_from_db()
        self.assertIsNone(site_conflict.utm_session_id)
        self.assertIsNone(action_conflict.utm_session_id)
        self.assertEqual(action.site_session_id, other_site.pk)
        self.assertIsNone(action.utm_session_id)

    def test_site_session_utm_with_different_key_is_conflicting(self):
        key = '5' * 32
        order = self._fresh_order(key=key, click_id='same-evidence')
        site = SiteSession.objects.create(session_key=key)
        tracking = order.payment_payload['tracking']
        existing = UTMSession.objects.create(
            session_key='6' * 32,
            session=site,
            utm_source='facebook',
            utm_medium='paid_social',
            fbc=tracking['fbc'],
            fbclid='same-evidence',
        )

        report = self._apply(groups=0, orders=0, conflicting=1)

        order.refresh_from_db()
        existing.refresh_from_db()
        self.assertIn('conflicting=1', report)
        self.assertIsNone(order.utm_session_id)
        self.assertEqual(existing.session_key, '6' * 32)

    def test_existing_order_attribution_and_manual_orders_are_out_of_scope(self):
        existing_utm = UTMSession.objects.create(session_key='w' * 32)
        attributed = self._fresh_order(key='x' * 32, utm_session=existing_utm)
        raw_attributed = self._fresh_order(key='y' * 32, utm_source='partner')
        manual = self._fresh_order(key='z' * 32, source='manual')
        output = StringIO()

        call_command('backfill_fbc_order_attribution', stdout=output)

        self.assertIn('scanned=0', output.getvalue())
        attributed.refresh_from_db()
        raw_attributed.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(attributed.utm_session_id, existing_utm.pk)
        self.assertEqual(raw_attributed.utm_source, 'partner')
        self.assertIsNone(manual.utm_session_id)
        self.assertEqual(UTMSession.objects.count(), 1)

    def test_write_failure_rolls_back_session_and_all_order_updates(self):
        first = self._fresh_order(key='1' * 32, click_id='first-safe')
        second = self._fresh_order(key='2' * 32, click_id='second-safe')

        with patch(
            'storefront.management.commands.backfill_fbc_order_attribution.Order.objects.bulk_update',
            side_effect=RuntimeError('write failed'),
        ):
            with self.assertRaises(RuntimeError):
                self._apply(groups=2, orders=2)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.utm_session_id)
        self.assertIsNone(second.utm_session_id)
        self.assertFalse(UTMSession.objects.exists())

    def test_second_apply_is_idempotent_while_residuals_remain_guarded(self):
        safe = self._fresh_order(key='3' * 32)
        stale_created = timezone.now()
        stale = self._order(
            key='4' * 32,
            fbc=self._fbc(stale_created - timedelta(days=8)),
            created=stale_created,
        )
        self._apply(groups=1, orders=1, stale=1)

        report = self._apply(groups=0, orders=0, stale=1)

        safe.refresh_from_db()
        stale.refresh_from_db()
        self.assertIn('updated_orders=0', report)
        self.assertIn('created_sessions=0', report)
        self.assertEqual(UTMSession.objects.count(), 1)
        self.assertIsNotNone(safe.utm_session_id)
        self.assertIsNone(stale.utm_session_id)
