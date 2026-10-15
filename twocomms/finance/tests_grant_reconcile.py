"""Reviewed grant repairs are dry-run first and never rewrite bank facts."""
import json
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError

from finance.models import (
    Account, AuditLog, FundingAllocation, FundingSource, LedgerClassification,
    LedgerClassificationEvent, ObligationGroup, Transaction,
)
from finance.services import ledger_v2
from finance.tests_grant_reporting import GrantFixture


class GrantReconcileTests(GrantFixture):
    def setUp(self):
        self.account.initial_balance = Decimal('62511')
        self.account.save(update_fields=['initial_balance'])
        self.uwf = FundingSource.objects.create(
            company=self.company, name='УВФ', received_amount=Decimal('276481'),
        )
        self.receipt = self.txn(
            '276481', ttype=Transaction.TYPE_INCOME, kind='grant_inflow', funding_source=self.uwf,
        )
        self.uwf.receipt_transaction = self.receipt
        self.uwf.save(update_fields=['receipt_transaction'])
        self.double_tagged = self.txn('154299', funding_source=self.uwf)
        other_equipment = self.txn('163932', funding_source=self.uwf)
        for txn in (self.double_tagged, other_equipment):
            # Existing reviewed history may exceed an uncorrected opening budget.
            FundingAllocation.objects.create(
                funding_source=self.uwf, transaction=txn, amount=txn.amount, allocation_type='spent',
            )
        self.duplicate = FundingAllocation.objects.create(
            funding_source=self.source, transaction=self.double_tagged,
            amount=Decimal('154299'), allocation_type='spent',
        )
        self.supplier_expenses = [self.txn('1130'), self.txn('226')]
        self.black = Account.objects.create(company=self.company, name='Чорна', currency='UAH')
        self.withdrawal = self.txn(
            '6000', ttype=Transaction.TYPE_TRANSFER, kind='owner_draw',
            to_account=self.black, to_amount=Decimal('6000'),
        )
        pension = Account.objects.create(company=self.company, name='Пенсійна', currency='UAH')
        self.split = self.txn('225870.85', account=pension, funding_source=self.source)
        FundingAllocation.objects.create(
            funding_source=self.source, transaction=self.split,
            amount=Decimal('225870.85'), allocation_type='spent',
        )
        for account in self.company.accounts.all():
            account.recalc_balance()

    def run_command(self, *, apply=False, opening='62511'):
        output = StringIO()
        arguments = [
            '--source-id', str(self.uwf.pk), '--opening-amount', opening,
            '--withdrawal-id', str(self.withdrawal.pk),
            '--split-transaction-id', str(self.split.pk), '--split-source-id', str(self.source.pk),
            '--grant-amount', '216000', '--remove-duplicate-allocation-id', str(self.duplicate.pk),
        ]
        for expense in self.supplier_expenses:
            arguments.extend(('--spent-transaction-id', str(expense.pk)))
        if apply:
            arguments.append('--apply')
        call_command('finance_reconcile_grant_splits', *arguments, stdout=output)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def bank_facts(self):
        return {
            'transactions': list(Transaction.objects.order_by('pk').values_list(
                'pk', 'type', 'status', 'amount', 'amount_base', 'currency', 'account_id',
                'to_account_id', 'to_amount', 'date_actual', 'comment', 'external_id',
            )),
            'balances': list(Account.objects.order_by('pk').values_list('pk', 'initial_balance', 'current_balance')),
        }

    def complete_state(self):
        return {
            'bank': self.bank_facts(),
            'sources': list(FundingSource.objects.order_by('pk').values()),
            'allocations': list(FundingAllocation.objects.order_by('pk').values()),
            'classifications': list(LedgerClassification.objects.order_by('pk').values()),
            'events': list(LedgerClassificationEvent.objects.order_by('pk').values()),
            'groups': list(ObligationGroup.objects.order_by('pk').values()),
            'audit': list(AuditLog.objects.order_by('pk').values()),
        }

    def test_default_dry_run_prints_exact_plan_without_any_writes(self):
        before = self.complete_state()
        rows = self.run_command()
        self.assertEqual(self.complete_state(), before)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['apply'])
        self.assertEqual(rows[0]['plan']['opening_amount'], '62511')
        self.assertEqual(rows[0]['plan']['grant_amount'], '216000')
        self.assertEqual(rows[0]['plan']['own_amount'], '9870.85')
        self.assertEqual(rows[0]['plan']['remove_duplicate_allocation'], self.duplicate.pk)
        self.assertEqual(set(rows[0]['plan']['spent_transactions']), {txn.pk for txn in self.supplier_expenses})

    def test_apply_repairs_only_reviewed_funding_and_preserves_bank_ledger(self):
        bank_before = self.bank_facts()
        rows = self.run_command(apply=True)
        self.assertEqual(self.bank_facts(), bank_before)
        self.assertTrue(rows[-1]['after']['bank_ledger_unchanged'])
        self.uwf.refresh_from_db()
        self.assertEqual(self.uwf.opening_amount, Decimal('62511'))
        self.assertEqual(ledger_v2.funding_summary(self.uwf)['available'], Decimal('13405'))
        self.assertEqual(ledger_v2.funding_summary(self.uwf)['spent'], Decimal('319587'))
        self.assertEqual(ledger_v2.funding_summary(self.uwf)['received'], Decimal('276481'))
        self.assertEqual(ledger_v2.funding_summary(self.source)['spent'], Decimal('216000'))
        self.assertFalse(FundingAllocation.objects.filter(pk=self.duplicate.pk).exists())
        self.assertTrue(self.double_tagged.funding_allocations.filter(funding_source=self.uwf, amount=154299).exists())
        for expense in self.supplier_expenses:
            expense.refresh_from_db()
            self.assertEqual(expense.funding_source_id, self.uwf.pk)
            self.assertEqual(expense.funding_allocations.get().amount, expense.amount)
        self.assertEqual(self.split.funding_allocations.get().amount, Decimal('216000'))
        group = ObligationGroup.objects.get(forecast_policy__kind='grant_replenishment')
        self.assertEqual(group.forecast_policy['withdrawal_id'], self.withdrawal.pk)
        self.assertEqual(group.components.get().fixed_amount, Decimal('6000'))
        self.assertEqual(AuditLog.objects.filter(action='grant_reconcile').count(), 1)
        for report in self.reports():
            self.assert_report_cost(report, '9870.85')

    def test_second_apply_keeps_same_split_claim_and_balances(self):
        self.run_command(apply=True)
        before_bank = self.bank_facts()
        before_allocations = list(FundingAllocation.objects.order_by('transaction_id', 'funding_source_id').values_list(
            'transaction_id', 'funding_source_id', 'allocation_type', 'amount',
        ))
        before_group = ObligationGroup.objects.get().pk
        self.run_command(apply=True)
        self.assertEqual(self.bank_facts(), before_bank)
        self.assertEqual(list(FundingAllocation.objects.order_by('transaction_id', 'funding_source_id').values_list(
            'transaction_id', 'funding_source_id', 'allocation_type', 'amount',
        )), before_allocations)
        self.assertEqual(ObligationGroup.objects.get().pk, before_group)
        self.uwf.refresh_from_db()
        self.assertEqual(ledger_v2.funding_summary(self.uwf)['available'], Decimal('13405'))
        self.assertEqual(ledger_v2.funding_summary(self.source)['spent'], Decimal('216000'))

    def test_opening_must_match_reviewed_account_initial_balance(self):
        before = self.complete_state()
        with self.assertRaises(CommandError):
            self.run_command(apply=True, opening='62510')
        self.assertEqual(self.complete_state(), before)

    def test_late_failure_rolls_back_opening_duplicate_removal_and_allocations(self):
        before = self.complete_state()
        with patch('finance.management.commands.finance_reconcile_grant_splits.grant_replenishment.register_withdrawal',
                   side_effect=ValueError('withdrawal rejected')):
            with self.assertRaisesMessage(ValueError, 'withdrawal rejected'):
                self.run_command(apply=True)
        self.assertEqual(self.complete_state(), before)
