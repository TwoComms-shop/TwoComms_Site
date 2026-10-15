"""A grant withdrawal creates a claim; only an explicit actual return closes it."""
from decimal import Decimal
from unittest.mock import patch
import datetime as dt
import json

from django.test import override_settings

from finance.models import (
    Account, Company, FundingAllocation, LedgerClassificationEvent,
    ObligationComponentSettlement, ObligationGroup, ObligationSettlement, Transaction,
)
from finance.services import grant_replenishment, ledger_v2, obligations_v2
from finance.services import transactions as txn_service
from finance.tests_grant_reporting import GrantFixture


class GrantReplenishmentFixture(GrantFixture):
    def setUp(self):
        self.account.initial_balance = Decimal('62511')
        self.account.save(update_fields=['initial_balance'])
        self.source.name = 'УВФ'
        self.source.opening_amount = Decimal('62511')
        self.source.received_amount = Decimal('276481')
        self.receipt = self.txn(
            '276481', ttype=Transaction.TYPE_INCOME, kind='grant_inflow', funding_source=self.source,
        )
        self.source.receipt_transaction = self.receipt
        self.source.save(update_fields=['name', 'opening_amount', 'received_amount', 'receipt_transaction'])
        for amount in ('318231', '1130', '226'):
            expense = self.txn(amount, funding_source=self.source)
            self.allocate(expense, amount)
        self.black = Account.objects.create(
            company=self.company, name='Чорна картка', currency='UAH', is_business=False,
        )
        self.withdrawal = self.txn(
            '6000', ttype=Transaction.TYPE_TRANSFER, kind='owner_draw',
            to_account=self.black, to_amount=Decimal('6000'),
        )
        self.group = grant_replenishment.register_withdrawal(
            self.source, self.withdrawal, user=self.user,
        )
        self.account.recalc_balance()
        self.black.recalc_balance()

    def repayment(self, *, transfer=True, **overrides):
        defaults = {
            'amount': Decimal('6000'), 'amount_base': Decimal('6000'),
            'company': self.company, 'currency': 'UAH', 'status': Transaction.STATUS_ACTUAL,
            'date_actual': self.when + dt.timedelta(days=1), 'economic_kind': 'unknown',
        }
        if transfer:
            defaults.update(type=Transaction.TYPE_TRANSFER, account=self.black,
                            to_account=self.account, to_amount=Decimal('6000'))
        else:
            defaults.update(type=Transaction.TYPE_INCOME, account=self.account)
        defaults.update(overrides)
        return Transaction.objects.create(**defaults)

    def bank_facts(self):
        return list(Transaction.objects.order_by('pk').values_list(
            'pk', 'type', 'status', 'amount', 'amount_base', 'currency',
            'account_id', 'to_account_id', 'to_amount', 'date_actual',
        ))


class GrantReplenishmentTests(GrantReplenishmentFixture):
    def test_protected_withdrawal_and_return_reject_bank_edits_and_deletion(self):
        payment = self.repayment()
        grant_replenishment.confirm_return(self.group, payment, user=self.user)
        for txn in (self.withdrawal, payment):
            txn.refresh_from_db()
            changes = (
                {'amount': Decimal('5999')}, {'status': Transaction.STATUS_CANCELLED},
                {'account': self.account if txn.account_id == self.black.pk else self.black},
                {'date_actual': txn.date_actual + dt.timedelta(days=1)},
                {'excluded_from_reports': True},
            )
            before = self.bank_facts()
            for fields in changes:
                with self.subTest(txn=txn.pk, fields=fields):
                    with self.assertRaises(ValueError):
                        txn_service.update_transaction(txn, user=self.user, **fields)
                    self.assertEqual(self.bank_facts(), before)
            with self.assertRaises(ValueError):
                txn_service.delete_transaction(txn, user=self.user)
            self.assertTrue(Transaction.objects.filter(pk=txn.pk).exists())
        self.assertEqual(ledger_v2.funding_summary(self.source)['available'], Decimal('19405'))

    def test_protected_withdrawal_and_return_cannot_be_split_into_unprotected_children(self):
        payment = self.repayment()
        grant_replenishment.confirm_return(self.group, payment, user=self.user)
        before = self.bank_facts()
        for txn in (self.withdrawal, payment):
            txn.refresh_from_db()
            with self.subTest(txn=txn.pk):
                with self.assertRaises(ValueError):
                    txn_service.split_transaction(
                        txn, user=self.user, parts=[{'amount': '3000'}, {'amount': '3000'}],
                    )
                self.assertFalse(txn.children.exists())
                txn.refresh_from_db()
                self.assertFalse(txn.is_split)
                self.assertFalse(txn.excluded_from_reports)
                self.assertEqual(txn.amount, Decimal('6000'))
        self.assertEqual(self.bank_facts(), before)
        self.assertEqual(ledger_v2.funding_summary(self.source)['available'], Decimal('19405'))

    def test_generic_allocations_cannot_reduce_or_replace_protected_withdrawal_and_return(self):
        before_available = ledger_v2.funding_summary(self.source)['available']
        with self.assertRaises(ValueError):
            self.allocate(self.withdrawal, '1', allocation_type='reserved', replace=True)
        self.assertEqual(self.withdrawal.funding_allocations.get().amount, Decimal('6000'))
        self.assertEqual(ledger_v2.funding_summary(self.source)['available'], before_available)
        self.assertEqual(obligations_v2.group_summary(self.group)['remaining'], Decimal('6000'))
        payment = self.repayment()
        grant_replenishment.confirm_return(self.group, payment, user=self.user)
        for txn in (self.withdrawal, payment):
            for kind in ('received', 'spent', 'reserved'):
                with self.subTest(txn=txn.pk, kind=kind):
                    with self.assertRaises(ValueError):
                        self.allocate(txn, '1', allocation_type=kind, replace=True)
        self.assertEqual(payment.funding_allocations.get().amount, Decimal('6000'))
        self.assertEqual(ledger_v2.funding_summary(self.source)['available'], Decimal('19405'))

    def test_descriptive_edits_allow_minute_precision_date_without_losing_original_seconds(self):
        payment = self.repayment(date_actual=self.when + dt.timedelta(days=1, seconds=37))
        grant_replenishment.confirm_return(self.group, payment, user=self.user)
        payment.refresh_from_db()
        original_date = payment.date_actual
        category = self.company.categories.create(name='Примітка до повернення', type='both')
        txn_service.update_transaction(
            payment, user=self.user, comment='Уточнено примітку', category=category,
            date_actual=payment.date_actual.replace(second=0, microsecond=0),
        )
        payment.refresh_from_db()
        self.assertEqual(payment.comment, 'Уточнено примітку')
        self.assertEqual(payment.category_id, category.pk)
        self.assertEqual(payment.date_actual, original_date)
        self.assertEqual(payment.amount, Decimal('6000'))
        self.assertEqual(ledger_v2.funding_summary(self.source)['available'], Decimal('19405'))

    def test_protected_meaning_and_program_cannot_be_reclassified(self):
        payment = self.repayment()
        grant_replenishment.confirm_return(self.group, payment, user=self.user)
        other = self.source_with_budget()
        for txn, expected_kind in ((self.withdrawal, 'owner_draw'), (payment, 'internal_transfer')):
            for kind, source in (('sale', self.source), (expected_kind, other), (expected_kind, None)):
                with self.subTest(txn=txn.pk, kind=kind, source=source):
                    with self.assertRaises(ValueError):
                        ledger_v2.classify_transaction(txn, economic_kind=kind, funding_source=source)
            txn.refresh_from_db()
            self.assertEqual(txn.economic_kind, expected_kind)
            self.assertEqual(txn.funding_source_id, self.source.pk)

    def test_opening_receipt_expenses_and_withdrawal_reconcile_to_actual_balance(self):
        summary = ledger_v2.funding_summary(self.source)
        self.assertEqual(summary['opening'], Decimal('62511'))
        self.assertEqual(summary['received'], Decimal('276481'))
        self.assertEqual(summary['spent'], Decimal('319587'))
        self.assertEqual(summary['reserved'], Decimal('0'))
        self.assertEqual(summary['withdrawn'], Decimal('6000'))
        self.assertEqual(summary['returned'], Decimal('0'))
        self.assertEqual(summary['to_replenish'], Decimal('6000'))
        self.assertEqual(summary['available'], Decimal('13405'))
        self.account.refresh_from_db()
        self.assertEqual(self.account.current_balance, Decimal('13405'))
        self.assertIsNone(self.group.due_day)
        self.assertIsNone(self.group.recurrence_rule_id)
        self.assertEqual(self.group.forecast_policy['kind'], 'grant_replenishment')
        self.assertEqual(self.group.components.get().fixed_amount, Decimal('6000'))

    def test_registration_is_idempotent(self):
        duplicate = grant_replenishment.register_withdrawal(self.source, self.withdrawal, user=self.user)
        self.assertEqual(duplicate.pk, self.group.pk)
        self.assertEqual(ObligationGroup.objects.count(), 1)
        self.assertEqual(self.group.components.count(), 1)
        self.assertEqual(self.withdrawal.funding_allocations.filter(allocation_type='reserved').count(), 1)
        self.assertEqual(ledger_v2.funding_summary(self.source)['withdrawn'], Decimal('6000'))

    def test_actual_candidate_does_not_automatically_close_claim(self):
        payment = self.repayment()
        before = self.bank_facts()
        rows = grant_replenishment.pending_rows(self.company, source=self.source)
        self.assertEqual([row['id'] for row in rows], [self.group.pk])
        self.assertEqual([candidate.pk for candidate in rows[0]['candidates']], [payment.pk])
        summary = obligations_v2.group_summary(self.group, period_key='2026-12')
        self.assertEqual(summary['status'], 'planned')
        self.assertEqual(summary['remaining'], Decimal('6000'))
        self.assertFalse(payment.settlements.exists())
        self.assertEqual(ledger_v2.funding_summary(self.source)['returned'], Decimal('0'))
        self.assertEqual(self.bank_facts(), before)

    def test_confirmed_transfer_restores_only_available_money_and_keeps_original_receipt(self):
        payment = self.repayment()
        self.account.recalc_balance()
        self.black.recalc_balance()
        before = self.bank_facts()
        balances = list(Account.objects.order_by('pk').values_list('pk', 'current_balance'))
        grant_replenishment.confirm_return(self.group, payment, user=self.user)
        summary = ledger_v2.funding_summary(self.source)
        self.assertEqual(summary['available'], Decimal('19405'))
        self.assertEqual(summary['received'], Decimal('276481'))
        self.assertEqual(summary['opening'], Decimal('62511'))
        self.assertEqual(summary['spent'], Decimal('319587'))
        self.assertEqual(summary['withdrawn'], Decimal('6000'))
        self.assertEqual(summary['returned'], Decimal('6000'))
        self.assertEqual(summary['to_replenish'], Decimal('0'))
        self.assertEqual(self.bank_facts(), before)
        self.assertEqual(list(Account.objects.order_by('pk').values_list('pk', 'current_balance')), balances)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.amount, Decimal('276481'))
        self.assertEqual(self.receipt.economic_kind, 'grant_inflow')
        payment.refresh_from_db()
        self.assertEqual(payment.economic_kind, 'internal_transfer')
        self.assertEqual(payment.funding_source_id, self.source.pk)
        self.assertEqual(payment.funding_allocations.get(allocation_type='returned').amount, Decimal('6000'))
        self.assertEqual(payment.settlements.get().amount, Decimal('6000'))

    def test_confirmed_income_is_an_internal_return_and_never_new_grant_income(self):
        payment = self.repayment(transfer=False)
        grant_replenishment.confirm_return(self.group, payment, user=self.user)
        payment.refresh_from_db()
        self.assertEqual(payment.type, Transaction.TYPE_INCOME)
        self.assertEqual(payment.economic_kind, 'internal_transfer')
        for report in self.reports():
            self.assert_report_cost(report, '0')
            self.assertEqual(report['targeted_in'], Decimal('276481'))
        included_pnl, included_cash = self.reports(include_grants='1')
        self.assertEqual(included_pnl['income'], Decimal('276481'))
        self.assertEqual(included_cash['cash_in'], Decimal('276481'))

    def test_explicit_return_may_come_from_another_own_account(self):
        other_account = Account.objects.create(company=self.company, name='Інша власна', currency='UAH')
        payment = self.repayment(account=other_account)
        grant_replenishment.confirm_return(self.group, payment, user=self.user)
        self.assertEqual(ledger_v2.funding_summary(self.source)['returned'], Decimal('6000'))
        self.assertEqual(ledger_v2.funding_summary(self.source)['available'], Decimal('19405'))

    def test_grant_withdrawal_is_not_own_cost_before_or_after_return(self):
        for report in self.reports():
            self.assert_report_cost(report, '0')
            self.assertEqual(report['owner_drawn'], Decimal('0'))
        payment = self.repayment()
        grant_replenishment.confirm_return(self.group, payment, user=self.user)
        for report in self.reports():
            self.assert_report_cost(report, '0')
            self.assertEqual(report['owner_drawn'], Decimal('0'))

    def test_repeated_confirmation_has_one_settlement_allocation_and_audit_event(self):
        payment = self.repayment()
        grant_replenishment.confirm_return(self.group, payment, user=self.user)
        count_events = LedgerClassificationEvent.objects.count()
        for _ in range(2):
            result = grant_replenishment.confirm_return(self.group, payment, user=self.user)
            self.assertEqual(result.status, 'paid')
        self.assertEqual(ObligationSettlement.objects.count(), 1)
        self.assertEqual(ObligationComponentSettlement.objects.count(), 1)
        self.assertEqual(FundingAllocation.objects.filter(allocation_type='returned').count(), 1)
        self.assertEqual(LedgerClassificationEvent.objects.count(), count_events)
        self.assertEqual(grant_replenishment.pending_rows(self.company), [])

    def test_paid_claim_does_not_reopen_in_later_billing_month(self):
        payment = self.repayment()
        grant_replenishment.confirm_return(self.group, payment, user=self.user)
        for month in ('2026-06', '2026-07', '2027-01'):
            with self.subTest(month=month):
                summary = obligations_v2.group_summary(self.group, period_key=month)
                self.assertEqual(summary['status'], 'paid')
                self.assertEqual(summary['remaining'], Decimal('0'))
                self.assertEqual(summary['components'][0]['paid'], Decimal('6000'))
        self.group.refresh_from_db()
        self.assertEqual(self.group.status, 'paid')

    def test_paid_claim_rejects_a_second_distinct_payment(self):
        first, second = self.repayment(), self.repayment(transfer=False)
        grant_replenishment.confirm_return(self.group, first, user=self.user)
        with self.assertRaises(ValueError):
            grant_replenishment.confirm_return(self.group, second, user=self.user)
        self.assertFalse(second.settlements.exists())
        self.assertFalse(second.funding_allocations.exists())
        self.assertEqual(ledger_v2.funding_summary(self.source)['returned'], Decimal('6000'))

    def test_invalid_receipts_leave_claim_allocations_and_classification_unchanged(self):
        other_company = Company.objects.create(name='Foreign finance', base_currency='UAH')
        invalid = {
            'wrong_destination': self.repayment(to_account=self.black),
            'wrong_amount': self.repayment(to_amount=Decimal('5999.99')),
            'before_withdrawal': self.repayment(date_actual=self.when - dt.timedelta(days=1)),
            'wrong_company': self.repayment(company=other_company),
            'wrong_currency': self.repayment(currency='USD'),
            'planned': self.repayment(status=Transaction.STATUS_PLANNED),
            'new_grant_income': self.repayment(transfer=False, economic_kind='grant_inflow'),
            'wrong_income_destination': self.repayment(transfer=False, account=self.black),
            'wrong_income_amount': self.repayment(transfer=False, amount=Decimal('6000.01')),
        }
        before = self.bank_facts()
        for reason, payment in invalid.items():
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    grant_replenishment.confirm_return(self.group, payment, user=self.user)
                self.group.refresh_from_db()
                self.assertEqual(self.group.status, 'planned')
                self.assertFalse(payment.settlements.exists())
                self.assertFalse(payment.funding_allocations.exists())
                self.assertEqual(ledger_v2.funding_summary(self.source)['to_replenish'], Decimal('6000'))
        self.assertEqual(self.bank_facts(), before)

    def test_failure_after_settlement_creation_rolls_back_whole_confirmation(self):
        payment = self.repayment()
        event_count = LedgerClassificationEvent.objects.count()
        with patch('finance.services.grant_replenishment.obligations_v2.allocate_settlement',
                   side_effect=ValueError('allocation rejected')):
            with self.assertRaisesMessage(ValueError, 'allocation rejected'):
                grant_replenishment.confirm_return(self.group, payment, user=self.user)
        self.assertFalse(ObligationSettlement.objects.exists())
        self.assertFalse(FundingAllocation.objects.filter(allocation_type='returned').exists())
        self.assertEqual(LedgerClassificationEvent.objects.count(), event_count)
        self.group.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(self.group.status, 'planned')
        self.assertEqual(payment.economic_kind, 'unknown')


@override_settings(ALLOWED_HOSTS=['testserver', 'fin.twocomms.shop'], SECURE_SSL_REDIRECT=False)
class GrantReplenishmentViewTests(GrantReplenishmentFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_planned_and_grants_show_pending_claim_and_candidate_without_auto_confirmation(self):
        payment = self.repayment()
        before = self.bank_facts()
        for path in ('/planned/', '/grants/'):
            with self.subTest(path=path):
                response = self.client.get(path, HTTP_HOST='fin.twocomms.shop')
                self.assertContains(response, self.group.title)
                self.assertContains(response, f'/api/v2/grant-replenishments/{self.group.pk}/confirm/')
                self.assertContains(response, f'value="{payment.pk}"')
                self.assertContains(response, 'Підтвердити повернення')
        self.group.refresh_from_db()
        self.assertEqual(self.group.status, 'planned')
        self.assertFalse(payment.settlements.exists())
        self.assertEqual(self.bank_facts(), before)

    def test_form_confirmation_redirects_and_closes_claim(self):
        payment = self.repayment()
        response = self.client.post(
            f'/api/v2/grant-replenishments/{self.group.pk}/confirm/',
            data={'transaction_id': payment.pk, 'next': '/grants/'}, HTTP_HOST='fin.twocomms.shop',
        )
        self.assertRedirects(response, '/grants/', fetch_redirect_response=False)
        self.group.refresh_from_db()
        self.assertEqual(self.group.status, 'paid')
        self.assertEqual(ledger_v2.funding_summary(self.source)['available'], Decimal('19405'))

    def test_form_invalid_return_is_400_and_keeps_claim_open(self):
        payment = self.repayment(to_amount=Decimal('1'))
        response = self.client.post(
            f'/api/v2/grant-replenishments/{self.group.pk}/confirm/',
            data={'transaction_id': payment.pk, 'next': '/planned/'}, HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(response.status_code, 400)
        self.group.refresh_from_db()
        self.assertEqual(self.group.status, 'planned')
        self.assertFalse(payment.settlements.exists())

    def test_confirmation_never_redirects_to_arbitrary_external_destination(self):
        payment = self.repayment()
        response = self.client.post(
            f'/api/v2/grant-replenishments/{self.group.pk}/confirm/',
            data={'transaction_id': payment.pk, 'next': 'https://example.test/'},
            HTTP_HOST='fin.twocomms.shop',
        )
        self.assertRedirects(response, '/planned/', fetch_redirect_response=False)

    def test_generic_funding_endpoint_cannot_forge_returned_allocation(self):
        payment = self.repayment()
        response = self.client.post(
            f'/api/v2/funding/{self.source.pk}/allocate/',
            data=json.dumps({'transaction_id': payment.pk, 'amount': '6000', 'allocation_type': 'returned'}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(payment.funding_allocations.exists())
        self.assertEqual(ledger_v2.funding_summary(self.source)['to_replenish'], Decimal('6000'))

    def test_generic_settlement_components_cannot_close_grant_claim(self):
        unrelated = self.repayment(transfer=False, account=self.black)
        settlement = ObligationSettlement.objects.create(
            company=self.company, payment=unrelated, amount=Decimal('6000'), currency='UAH',
            period_key='2026-06',
        )
        component = self.group.components.get()
        response = self.client.post(
            f'/api/v2/settlements/{settlement.pk}/components/',
            data=json.dumps({'allocations': {str(component.pk): '6000'}}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(component.settlements.exists())
        self.assertEqual(obligations_v2.group_summary(self.group)['remaining'], Decimal('6000'))
        self.assertEqual(ledger_v2.funding_summary(self.source)['returned'], Decimal('0'))

    def test_generic_settle_existing_cannot_close_grant_claim_even_with_valid_candidate(self):
        payment = self.repayment(transfer=False)
        component = self.group.components.get()
        response = self.client.post(
            f'/api/v2/obligation-components/{component.pk}/settle-existing/',
            data=json.dumps({'transaction_id': payment.pk, 'amount': '6000', 'period_key': '2026-06'}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(payment.settlements.exists())
        self.assertFalse(component.settlements.exists())
        self.assertEqual(ledger_v2.funding_summary(self.source)['to_replenish'], Decimal('6000'))


class GrantAllocationBoundaryTests(GrantFixture):
    def test_generic_allocator_accepts_only_received_spent_and_reserved(self):
        income = self.txn('100', ttype=Transaction.TYPE_INCOME)
        for kind in ('returned', 'withdrawn', 'unknown', ''):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    self.allocate(income, '100', allocation_type=kind)
        self.assertFalse(income.funding_allocations.exists())
        self.allocate(income, '100', allocation_type='received')
        expense = self.txn('100')
        self.allocate(expense, '50', allocation_type='spent')
        self.allocate(expense, '50', allocation_type='reserved')

    def test_received_requires_actual_income_and_spent_requires_actual_expense(self):
        expense = self.txn('100')
        income = self.txn('100', ttype=Transaction.TYPE_INCOME)
        planned_income = self.txn('100', ttype=Transaction.TYPE_INCOME)
        planned_income.status = Transaction.STATUS_PLANNED
        planned_income.save(update_fields=['status'])
        planned_expense = self.txn('100')
        planned_expense.status = Transaction.STATUS_PLANNED
        planned_expense.save(update_fields=['status'])
        for txn, kind in ((expense, 'received'), (planned_income, 'received'),
                          (income, 'spent'), (planned_expense, 'spent')):
            with self.subTest(txn=txn.pk, kind=kind):
                with self.assertRaises(ValueError):
                    self.allocate(txn, '100', allocation_type=kind)
                self.assertFalse(txn.funding_allocations.exists())

    def test_reserved_and_spent_share_one_program_budget(self):
        limited = self.source_with_budget('100')
        spent, reserved = self.txn('100'), self.txn('100')
        self.allocate(spent, '70', source=limited)
        self.allocate(reserved, '30', source=limited, allocation_type='reserved')
        with self.assertRaises(ValueError):
            self.allocate(reserved, '30.01', source=limited, allocation_type='reserved', replace=True)
        with self.assertRaises(ValueError):
            self.allocate(spent, '70.01', source=limited, replace=True)
        self.assertEqual(ledger_v2.funding_summary(limited)['available'], Decimal('0'))
