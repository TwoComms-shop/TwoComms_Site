from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from orders.models import Order
from storefront.models import SiteSession, UTMSession, UserAction


class ReconcilePurchaseActionsCommandTests(TestCase):
    def _order(self, *, source='web', payment_status='paid', **overrides):
        data = {
            'full_name': 'Reconcile Buyer',
            'phone': '+380991112233',
            'city': 'Київ',
            'np_office': 'Відділення №1',
            'pay_type': 'online_full',
            'status': 'new',
            'source': source,
            'payment_status': payment_status,
            'total_sum': Decimal('1200.00'),
        }
        data.update(overrides)
        return Order.objects.create(**data)

    def test_dry_run_reports_missing_without_writing(self):
        self._order(source='web', payment_status='paid')
        output = StringIO()

        call_command('reconcile_purchase_actions', stdout=output)

        self.assertIn('missing=1', output.getvalue())
        self.assertIn('dry_run=True', output.getvalue())
        self.assertFalse(UserAction.objects.filter(action_type='purchase').exists())

    def test_apply_backfills_only_trusted_orders_and_is_idempotent(self):
        site_session = SiteSession.objects.create(session_key='reconcile-session')
        utm_session = UTMSession.objects.create(
            session=site_session,
            session_key='reconcile-session',
            utm_source='instagram',
        )
        web_order = self._order(
            source='web',
            payment_status='paid',
            session_key='reconcile-session',
            utm_session=utm_session,
        )
        occurred_at = timezone.now() - timedelta(days=30)
        web_order.payment_payload = {
            'history': [
                {
                    'status': 'success',
                    'received_at': occurred_at.isoformat(),
                },
            ],
        }
        web_order.save(update_fields=['payment_payload'])

        ig_order = self._order(
            source='manual',
            payment_status='prepaid',
            pay_type='prepay_200',
            payment_provider='monobank',
            payment_invoice_id='ig-invoice-1',
        )
        unverified_manual = self._order(source='manual', payment_status='paid')
        free_manual = self._order(
            source='manual',
            payment_status='paid',
            payment_payload={'manual_payment_preset': 'free'},
        )
        unpaid_web = self._order(source='web', payment_status='unpaid')

        first_output = StringIO()
        call_command('reconcile_purchase_actions', apply=True, stdout=first_output)

        self.assertIn('created=2', first_output.getvalue())
        self.assertTrue(
            UserAction.objects.filter(action_type='purchase', order_id=web_order.pk).exists()
        )
        self.assertTrue(
            UserAction.objects.filter(action_type='purchase', order_id=ig_order.pk).exists()
        )
        self.assertFalse(
            UserAction.objects.filter(action_type='purchase', order_id=unverified_manual.pk).exists()
        )
        self.assertFalse(
            UserAction.objects.filter(action_type='purchase', order_id=free_manual.pk).exists()
        )
        self.assertFalse(
            UserAction.objects.filter(action_type='purchase', order_id=unpaid_web.pk).exists()
        )

        action = UserAction.objects.get(action_type='purchase', order_id=web_order.pk)
        self.assertLess(action.timestamp, timezone.now() - timedelta(days=29))
        utm_session.refresh_from_db()
        self.assertTrue(utm_session.is_converted)
        self.assertEqual(utm_session.conversion_type, 'purchase')
        self.assertLess(utm_session.converted_at, timezone.now() - timedelta(days=29))

        second_output = StringIO()
        call_command('reconcile_purchase_actions', apply=True, stdout=second_output)
        self.assertIn('created=0', second_output.getvalue())
        self.assertEqual(UserAction.objects.filter(action_type='purchase').count(), 2)

    def test_apply_uses_earliest_success_callback_time(self):
        earliest = timezone.now() - timedelta(days=45)
        repeated = timezone.now() - timedelta(days=5)
        order = self._order(
            source='web',
            payment_status='paid',
            payment_payload={
                'history': [
                    {'status': 'success', 'received_at': earliest.isoformat()},
                    {'status': 'success', 'received_at': repeated.isoformat()},
                ],
            },
        )

        call_command('reconcile_purchase_actions', apply=True, stdout=StringIO())

        action = UserAction.objects.get(action_type='purchase', order_id=order.pk)
        self.assertAlmostEqual(
            action.timestamp.timestamp(),
            earliest.timestamp(),
            delta=1,
        )
