from datetime import timedelta
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from orders.models import Order
from storefront.models import SiteSession, UTMSession, UserAction


class ReconcileOrderUtmAttributionCommandTests(TestCase):
    def _utm(self, key, *, source='instagram', **overrides):
        data = {
            'session_key': key,
            'utm_source': source,
            'utm_medium': 'paid_social',
            'utm_campaign': 'historical_campaign',
            'utm_content': 'creative_a',
            'utm_term': 'shirts',
        }
        data.update(overrides)
        return UTMSession.objects.create(**data)

    def _order(self, *, source='web', session_key=None, external_id=None, **overrides):
        payload = overrides.pop('payment_payload', {})
        if external_id is not None:
            payload = dict(payload)
            payload['tracking'] = {
                **(payload.get('tracking') or {}),
                'external_id': external_id,
            }
        data = {
            'full_name': 'Historical Buyer',
            'phone': '+380991112233',
            'city': 'Київ',
            'np_office': 'Відділення №1',
            'pay_type': 'prepay_200',
            'status': 'done',
            'source': source,
            'session_key': session_key,
            'payment_status': 'paid',
            'total_sum': '1200.00',
            'payment_payload': payload,
        }
        data.update(overrides)
        return Order.objects.create(**data)

    def _run_apply(self, *, orders, actions=0, conversions=0):
        output = StringIO()
        call_command(
            'reconcile_order_utm_attribution',
            apply=True,
            expect_orders=orders,
            expect_actions=actions,
            expect_conversions=conversions,
            stdout=output,
        )
        return output.getvalue()

    def test_dry_run_reports_exact_match_without_writing(self):
        key = 'a' * 32
        utm = self._utm(key)
        order = self._order(external_id=f'session:{key}')
        action = UserAction.objects.create(
            action_type='purchase',
            order_id=order.pk,
            metadata={'keep': 'unchanged'},
        )
        original_payload = order.payment_payload
        output = StringIO()

        call_command('reconcile_order_utm_attribution', stdout=output)

        order.refresh_from_db()
        action.refresh_from_db()
        utm.refresh_from_db()
        self.assertIn('recoverable_orders=1', output.getvalue())
        self.assertIn('recoverable_actions=1', output.getvalue())
        self.assertIn('recoverable_conversions=1', output.getvalue())
        self.assertIn('updated_orders=0', output.getvalue())
        self.assertIn('dry_run=True', output.getvalue())
        self.assertIsNone(order.utm_session_id)
        self.assertIsNone(order.session_key)
        self.assertEqual(order.payment_payload, original_payload)
        self.assertIsNone(action.utm_session_id)
        self.assertFalse(utm.is_converted)

    def test_apply_links_exact_external_evidence_without_granting_session_access(self):
        key = 'b' * 32
        utm = self._utm(key, source='ig')
        order = self._order(
            external_id=f'session:{key}',
            payment_payload={'history': [{'status': 'success'}]},
        )
        action = UserAction.objects.create(
            action_type='purchase',
            order_id=order.pk,
            metadata={'source': 'purchase_reconciliation'},
        )
        original_action_time = action.timestamp
        original_payload = order.payment_payload

        output = self._run_apply(orders=1, actions=1, conversions=1)

        order.refresh_from_db()
        action.refresh_from_db()
        utm.refresh_from_db()
        self.assertIn('updated_orders=1', output)
        self.assertIn('updated_actions=1', output)
        self.assertIn('updated_conversions=1', output)
        self.assertEqual(order.utm_session_id, utm.pk)
        self.assertEqual(order.utm_source, 'ig')
        self.assertEqual(order.utm_medium, 'paid_social')
        self.assertEqual(order.utm_campaign, 'historical_campaign')
        self.assertEqual(order.utm_content, 'creative_a')
        self.assertEqual(order.utm_term, 'shirts')
        self.assertIsNone(order.session_key)
        self.assertEqual(order.payment_payload, original_payload)
        self.assertEqual(action.utm_session_id, utm.pk)
        self.assertEqual(action.metadata, {'source': 'purchase_reconciliation'})
        self.assertTrue(utm.is_converted)
        self.assertEqual(utm.conversion_type, 'purchase')
        self.assertEqual(utm.converted_at, original_action_time)

        state = (
            order.utm_session_id,
            order.utm_source,
            action.utm_session_id,
            utm.is_converted,
            utm.conversion_type,
            utm.converted_at,
        )
        second_output = self._run_apply(orders=0, actions=0, conversions=0)
        order.refresh_from_db()
        action.refresh_from_db()
        utm.refresh_from_db()
        self.assertIn('updated_orders=0', second_output)
        self.assertEqual(
            state,
            (
                order.utm_session_id,
                order.utm_source,
                action.utm_session_id,
                utm.is_converted,
                utm.conversion_type,
                utm.converted_at,
            ),
        )

    def test_apply_accepts_exact_order_session_key_without_changing_it(self):
        key = 'c' * 32
        utm = self._utm(key, source='google', utm_medium='cpc')
        order = self._order(
            session_key=key,
            payment_status='checking',
            status='cancelled',
        )

        output = self._run_apply(orders=1)

        order.refresh_from_db()
        utm.refresh_from_db()
        self.assertIn('updated_actions=0', output)
        self.assertEqual(order.session_key, key)
        self.assertEqual(order.utm_session_id, utm.pk)
        self.assertEqual(order.utm_source, 'google')
        self.assertFalse(UserAction.objects.filter(order_id=order.pk).exists())
        self.assertFalse(utm.is_converted)

    def test_apply_fails_closed_when_order_and_external_keys_disagree(self):
        order_key = 'd' * 32
        external_key = 'e' * 32
        self._utm(order_key)
        self._utm(external_key, source='google')
        order = self._order(
            session_key=order_key,
            external_id=f'session:{external_key}',
        )

        with self.assertRaises(CommandError):
            self._run_apply(orders=0)

        order.refresh_from_db()
        self.assertIsNone(order.utm_session_id)
        self.assertIsNone(order.utm_source)

    def test_unverifiable_evidence_is_never_backfilled(self):
        missing_key = 'f' * 32
        no_source_key = 'g' * 32
        future_key = 'h' * 32
        expired_key = 'i' * 32
        self._utm(no_source_key, source='   ')

        future_order = self._order(external_id=f'session:{future_key}')
        self._utm(future_key)

        expired_utm = self._utm(expired_key)
        expired_order = self._order(external_id=f'session:{expired_key}')
        UTMSession.objects.filter(pk=expired_utm.pk).update(
            first_seen=expired_order.created
            - timedelta(seconds=settings.SESSION_COOKIE_AGE + 1),
        )

        orders = [
            self._order(external_id='user:123'),
            self._order(external_id='session:bad key'),
            self._order(external_id=f'session:{missing_key}'),
            self._order(external_id=f'session:{no_source_key}'),
            future_order,
            expired_order,
        ]

        output = self._run_apply(orders=0)

        self.assertIn('no_evidence=1', output)
        self.assertIn('invalid_evidence=1', output)
        self.assertIn('missing_utm=2', output)
        self.assertIn('outside_window=2', output)
        for order in orders:
            order.refresh_from_db()
            self.assertIsNone(order.utm_session_id)
            self.assertIsNone(order.utm_source)

    def test_existing_attribution_and_manual_orders_are_untouched(self):
        key = 'j' * 32
        utm = self._utm(key)
        partially_attributed = self._order(
            external_id=f'session:{key}',
            utm_source='keep_me',
        )
        manual = self._order(
            source='manual',
            external_id=f'session:{key}',
        )

        output = self._run_apply(orders=0)

        self.assertIn('already_attributed=1', output)
        partially_attributed.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(partially_attributed.utm_source, 'keep_me')
        self.assertIsNone(partially_attributed.utm_session_id)
        self.assertIsNone(manual.utm_session_id)
        self.assertEqual(Order.objects.filter(source='web').count(), 1)

    def test_action_attribution_conflict_aborts_all_writes(self):
        order_key = 'k' * 32
        other_key = 'm' * 32
        self._utm(order_key)
        other_utm = self._utm(other_key, source='google')
        order = self._order(external_id=f'session:{order_key}')
        action = UserAction.objects.create(
            action_type='purchase',
            order_id=order.pk,
            utm_session=other_utm,
        )

        with self.assertRaises(CommandError):
            self._run_apply(orders=1)

        order.refresh_from_db()
        action.refresh_from_db()
        self.assertIsNone(order.utm_session_id)
        self.assertEqual(action.utm_session_id, other_utm.pk)

    def test_site_session_conflict_aborts_all_writes(self):
        order_key = 'o' * 32
        other_site = SiteSession.objects.create(session_key='p' * 32)
        self._utm(order_key)
        order = self._order(external_id=f'session:{order_key}')
        action = UserAction.objects.create(
            action_type='purchase',
            order_id=order.pk,
            site_session=other_site,
        )

        with self.assertRaises(CommandError):
            self._run_apply(orders=1, actions=1, conversions=1)

        order.refresh_from_db()
        action.refresh_from_db()
        self.assertIsNone(order.utm_session_id)
        self.assertIsNone(action.utm_session_id)
        self.assertEqual(action.site_session_id, other_site.pk)

    def test_shared_utm_session_gets_one_strongest_conversion_update(self):
        key = 'q' * 32
        utm = self._utm(key)
        lead_order = self._order(external_id=f'session:{key}')
        purchase_order = self._order(external_id=f'session:{key}')
        lead = UserAction.objects.create(
            action_type='lead',
            order_id=lead_order.pk,
        )
        purchase = UserAction.objects.create(
            action_type='purchase',
            order_id=purchase_order.pk,
        )
        lead_time = timezone.now() - timedelta(days=5)
        purchase_time = timezone.now() - timedelta(days=3)
        UserAction.objects.filter(pk=lead.pk).update(timestamp=lead_time)
        UserAction.objects.filter(pk=purchase.pk).update(timestamp=purchase_time)
        lead.refresh_from_db()
        purchase.refresh_from_db()

        output = self._run_apply(orders=2, actions=2, conversions=1)

        utm.refresh_from_db()
        lead.refresh_from_db()
        purchase.refresh_from_db()
        self.assertIn('updated_orders=2', output)
        self.assertIn('updated_actions=2', output)
        self.assertIn('updated_conversions=1', output)
        self.assertEqual(lead.utm_session_id, utm.pk)
        self.assertEqual(purchase.utm_session_id, utm.pk)
        self.assertTrue(utm.is_converted)
        self.assertEqual(utm.conversion_type, 'purchase')
        self.assertEqual(utm.converted_at, purchase_time)

    def test_apply_repairs_existing_same_type_conversion_timestamp(self):
        key = 's' * 32
        utm = self._utm(key)
        wrong_time = timezone.now() - timedelta(days=1)
        UTMSession.objects.filter(pk=utm.pk).update(
            is_converted=True,
            conversion_type='purchase',
            converted_at=wrong_time,
        )
        order = self._order(external_id=f'session:{key}')
        action = UserAction.objects.create(
            action_type='purchase',
            order_id=order.pk,
        )
        canonical_time = timezone.now() - timedelta(days=10)
        UserAction.objects.filter(pk=action.pk).update(timestamp=canonical_time)

        output = self._run_apply(orders=1, actions=1, conversions=1)

        utm.refresh_from_db()
        self.assertIn('updated_conversions=1', output)
        self.assertEqual(utm.conversion_type, 'purchase')
        self.assertEqual(utm.converted_at, canonical_time)

    def test_lead_evidence_cannot_downgrade_existing_purchase(self):
        key = 't' * 32
        utm = self._utm(key)
        purchase_time = timezone.now() - timedelta(days=20)
        UTMSession.objects.filter(pk=utm.pk).update(
            is_converted=True,
            conversion_type='purchase',
            converted_at=purchase_time,
        )
        order = self._order(external_id=f'session:{key}')
        action = UserAction.objects.create(
            action_type='lead',
            order_id=order.pk,
        )

        output = self._run_apply(orders=1, actions=1, conversions=0)

        action.refresh_from_db()
        utm.refresh_from_db()
        self.assertIn('updated_conversions=0', output)
        self.assertEqual(action.utm_session_id, utm.pk)
        self.assertTrue(utm.is_converted)
        self.assertEqual(utm.conversion_type, 'purchase')
        self.assertEqual(utm.converted_at, purchase_time)

    def test_apply_requires_all_expected_count_guards(self):
        key = 'r' * 32
        self._utm(key)
        order = self._order(external_id=f'session:{key}')

        with self.assertRaises(CommandError):
            call_command(
                'reconcile_order_utm_attribution',
                apply=True,
                stdout=StringIO(),
            )

        order.refresh_from_db()
        self.assertIsNone(order.utm_session_id)

    def test_expected_counts_guard_prevents_partial_apply(self):
        key = 'n' * 32
        self._utm(key)
        order = self._order(external_id=f'session:{key}')

        with self.assertRaises(CommandError):
            self._run_apply(orders=2)

        order.refresh_from_db()
        self.assertIsNone(order.utm_session_id)
