from decimal import Decimal
import datetime as dt
import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (
    Account, Company, Counterparty, ObligationComponent, ObligationGroup,
    ObligationSettlement, Transaction,
)
from .services import obligations_v2, recurring


@override_settings(ALLOWED_HOSTS=['testserver', 'fin.twocomms.shop'], SECURE_SSL_REDIRECT=False)
class ComponentPaymentTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('component-payments', '', 'x')
        self.company = Company.objects.create(name='Component payments', base_currency='UAH')
        self.account = Account.objects.create(company=self.company, name='monobank black', currency='UAH')
        self.counterparty = Counterparty.objects.create(company=self.company, name='Влада мама')
        self.group = ObligationGroup.objects.create(
            company=self.company, title='Квартира', counterparty=self.counterparty,
        )
        self.rent = ObligationComponent.objects.create(
            group=self.group, name='Квартплата', fixed_amount=Decimal('4000'),
        )
        self.utilities = ObligationComponent.objects.create(group=self.group, name='Коммунальные')
        self.client.force_login(self.user)

    def payment(self, amount):
        return Transaction.objects.create(
            company=self.company, type=Transaction.TYPE_EXPENSE, status=Transaction.STATUS_ACTUAL,
            amount=Decimal(amount), amount_base=Decimal(amount), currency='UAH',
            account=self.account, date_actual=timezone.now(), comment='Олена Д.',
        )

    def settle(self, component, payment, amount=None, period='2026-10', planned=None):
        return self.client.post(
            f'/api/v2/obligation-components/{component.id}/settle-existing/',
            data=json.dumps({'transaction_id': payment.id, 'amount': amount or str(payment.amount),
                             'period_key': period, 'planned_txn_id': planned.id if planned else None}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )

    def test_unknown_utilities_close_at_actual_amount_without_changing_template_or_balance(self):
        payment = self.payment('1337.17')
        self.account.refresh_from_db()
        balance = self.account.current_balance
        response = self.settle(self.utilities, payment)
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()['component']
        self.assertEqual(Decimal(row['paid']), Decimal('1337.17'))
        self.assertEqual(Decimal(row['remaining']), Decimal('0'))
        self.assertFalse(row['needs_amount'])
        self.utilities.refresh_from_db()
        self.assertIsNone(self.utilities.fixed_amount)
        self.assertIsNone(self.utilities.forecast_amount)
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, balance)
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal('1337.17'))
        self.assertEqual(payment.comment, 'Олена Д.')
        self.assertEqual(payment.settlements.get().period_key, '2026-10')

    def test_rent_and_utilities_are_independent_and_repeat_with_different_amounts(self):
        for period, utility_amount in [('2026-10', '1337.17'), ('2026-11', '2108.43')]:
            with self.subTest(period=period):
                self.assertEqual(self.settle(self.rent, self.payment('4000'), period=period).status_code, 200)
                summary = obligations_v2.group_summary(self.group, period_key=period)
                self.assertEqual(summary['status'], 'partial')
                self.assertTrue(summary['components'][1]['needs_amount'])
                response = self.settle(self.utilities, self.payment(utility_amount), period=period)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(obligations_v2.group_summary(self.group, period_key=period)['status'], 'paid')
        october = obligations_v2.component_summary(self.utilities, period_key='2026-10')
        self.assertEqual(october['paid'], Decimal('1337.17'))
        december = obligations_v2.component_summary(self.utilities, period_key='2026-12')
        self.assertEqual(december['paid'], Decimal('0'))
        self.assertTrue(december['needs_amount'])

    def test_forecast_does_not_cap_actual_variable_bill(self):
        self.utilities.forecast_amount = Decimal('1000')
        self.utilities.save(update_fields=['forecast_amount'])
        response = self.settle(self.utilities, self.payment('1337.17'))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Decimal(response.json()['component']['planned']), Decimal('1337.17'))
        self.utilities.refresh_from_db()
        self.assertEqual(self.utilities.forecast_amount, Decimal('1000'))

    def test_fixed_rent_keeps_cap_and_partial_payment(self):
        too_large = self.settle(self.rent, self.payment('4000.01'))
        self.assertEqual(too_large.status_code, 400)
        self.assertFalse(ObligationSettlement.objects.exists())
        first = self.settle(self.rent, self.payment('1500'))
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(Decimal(first.json()['component']['remaining']), Decimal('2500'))
        second = self.settle(self.rent, self.payment('2500'))
        self.assertEqual(second.status_code, 200, second.content)

    def test_payment_cannot_be_reused_or_overallocated(self):
        payment = self.payment('1337.17')
        too_large = self.settle(self.utilities, payment, amount='1337.18')
        self.assertEqual(too_large.status_code, 400)
        self.assertFalse(ObligationSettlement.objects.exists())
        self.assertEqual(self.settle(self.utilities, payment).status_code, 200)
        duplicate = self.settle(self.utilities, payment)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(payment.settlements.count(), 1)

    def test_invalid_period_and_nonpositive_amount_leave_no_links(self):
        payment = self.payment('1337.17')
        for amount, period in [('0', '2026-10'), ('-1', '2026-10'), ('1337.17', '2026-13')]:
            with self.subTest(amount=amount, period=period):
                self.assertEqual(self.settle(self.utilities, payment, amount=amount, period=period).status_code, 400)
        self.assertFalse(ObligationSettlement.objects.exists())

    def test_closed_variable_component_does_not_accept_another_payment_in_same_month(self):
        self.assertEqual(self.settle(self.utilities, self.payment('1337.17')).status_code, 200)
        extra = self.payment('100')
        response = self.settle(self.utilities, extra)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(extra.settlements.exists())

    def test_group_and_context_use_requested_period(self):
        self.assertEqual(self.settle(self.rent, self.payment('4000')).status_code, 200)
        response = self.client.get(
            f'/api/v2/obligation-groups/{self.group.id}/?period_key=2026-11', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(response.json()['group']['components'][0]['remaining']), Decimal('4000'))
        context = self.client.get(
            f'/api/v2/obligation-components/{self.utilities.id}/payment-context/?period_key=2026-11',
            HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(context.status_code, 200)
        self.assertTrue(context.json()['component']['needs_amount'])
        self.assertEqual(context.json()['component']['period_key'], '2026-11')

    def test_unknown_only_group_is_not_reported_paid(self):
        self.rent.is_active = False
        self.rent.save(update_fields=['is_active'])
        self.assertEqual(obligations_v2.group_summary(self.group)['status'], 'planned')

    def test_complete_components_close_only_selected_planned_month(self):
        plan = Transaction.objects.create(
            company=self.company, type='expense', status=Transaction.STATUS_PLANNED,
            amount=Decimal('2500'), amount_base=Decimal('2500'), currency='UAH', account=self.account,
            counterparty=self.counterparty, comment='Комуналка квартиры',
            date_actual=timezone.make_aware(dt.datetime(2026, 10, 5, 12)),
        )
        rule = recurring.create_rule_from_transaction(plan, user=self.user, frequency='monthly', end_mode='never')
        for period, utility_amount in [('2026-10', '1337.17'), ('2026-11', '2108.43')]:
            with self.subTest(period=period):
                occurrence = rule.transactions.get(status='planned', date_actual__month=int(period[5:]))
                response = self.settle(self.utilities, self.payment(utility_amount), period=period, planned=plan)
                self.assertEqual(response.status_code, 200, response.content)
                occurrence.refresh_from_db()
                self.assertEqual(occurrence.status, Transaction.STATUS_PLANNED)
                response = self.settle(self.rent, self.payment('4000'), period=period, planned=plan)
                self.assertEqual(response.status_code, 200, response.content)
                occurrence.refresh_from_db()
                self.assertEqual(occurrence.status, Transaction.STATUS_CANCELLED)
                self.assertEqual(occurrence.planned_settlements.count(), 2)
        rule.refresh_from_db()
        self.assertEqual(rule.template_amount, Decimal('2500'))
        self.assertTrue(rule.transactions.filter(status='planned', date_actual__month=12).exists())
        self.assertEqual(Transaction.objects.filter(status='actual').count(), 4)

    def test_unrelated_planned_operation_is_not_closed(self):
        other = Counterparty.objects.create(company=self.company, name='Інший отримувач')
        plan = Transaction.objects.create(
            company=self.company, type='expense', status='planned', amount=Decimal('2500'),
            amount_base=Decimal('2500'), currency='UAH', account=self.account,
            counterparty=other, date_actual=timezone.now(),
        )
        response = self.settle(self.utilities, self.payment('1337.17'), planned=plan)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ObligationSettlement.objects.exists())
        plan.refresh_from_db()
        self.assertEqual(plan.status, 'planned')
