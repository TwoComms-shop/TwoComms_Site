"""Apply explicitly reviewed grant attribution corrections, without bank mutations."""
from decimal import Decimal
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from finance.models import AuditLog, FundingAllocation, FundingSource, Transaction
from finance.services import grant_replenishment, ledger_v2


class Command(BaseCommand):
    help = 'Dry-run by default: reconcile reviewed opening grant funds, splits and temporary withdrawal.'

    def add_arguments(self, parser):
        parser.add_argument('--source-id', type=int, required=True)
        parser.add_argument('--opening-amount', required=True)
        parser.add_argument('--spent-transaction-id', type=int, action='append', default=[])
        parser.add_argument('--withdrawal-id', type=int, required=True)
        parser.add_argument('--split-transaction-id', type=int, required=True)
        parser.add_argument('--split-source-id', type=int, required=True)
        parser.add_argument('--grant-amount', required=True)
        parser.add_argument('--remove-duplicate-allocation-id', type=int, required=True)
        parser.add_argument('--apply', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        source = FundingSource.objects.select_for_update().get(pk=options['source_id'])
        split_source = FundingSource.objects.select_for_update().get(pk=options['split_source_id'], company=source.company)
        split = Transaction.objects.get(pk=options['split_transaction_id'], company=source.company, funding_source=split_source)
        withdrawal = Transaction.objects.get(pk=options['withdrawal_id'], company=source.company)
        expenses = list(Transaction.objects.filter(pk__in=options['spent_transaction_id'], company=source.company))
        if len(expenses) != len(set(options['spent_transaction_id'])):
            raise CommandError('Not all reviewed expense transactions were found')
        opening = Decimal(options['opening_amount'])
        granted = Decimal(options['grant_amount'])
        if not opening.is_finite() or opening < 0 or not granted.is_finite() or not 0 < granted <= split.amount:
            raise CommandError('Invalid reviewed opening or grant amount')
        duplicate = FundingAllocation.objects.filter(pk=options['remove_duplicate_allocation_id']).first()
        if duplicate and not (
            duplicate.allocation_type == 'spent' and duplicate.funding_source_id == split_source.id
            and duplicate.transaction.funding_source_id == source.id
            and duplicate.transaction.amount == duplicate.amount
            and duplicate.transaction.funding_allocations.filter(
                funding_source=source, allocation_type='spent', amount=duplicate.amount).exists()
        ):
            raise CommandError('Allocation does not match the reviewed full duplicate')
        account_id = source.receipt_transaction.account_id if source.receipt_transaction_id else None
        if account_id is None or opening != source.receipt_transaction.account.initial_balance:
            raise CommandError('Reviewed grant opening does not match the bank account opening')
        for expense in expenses:
            if expense.type != 'expense' or expense.status != 'actual' or expense.account_id != account_id:
                raise CommandError('Reviewed expense is not an actual payment from this grant account')
        bank_fields = ('id', 'type', 'status', 'amount', 'amount_base', 'account_id', 'to_account_id',
                       'to_amount', 'date_actual', 'external_id', 'comment')
        bank_before = list(Transaction.objects.filter(company=source.company).order_by('id').values_list(*bank_fields))
        balances_before = list(source.company.accounts.order_by('id').values_list('id', 'current_balance'))
        before = {'source': {k: str(v) for k, v in ledger_v2.funding_summary(source).items()},
                  'split_source': {k: str(v) for k, v in ledger_v2.funding_summary(split_source).items()},
                  'duplicate': ({'id': duplicate.id, 'amount': str(duplicate.amount),
                                 'transaction_id': duplicate.transaction_id} if duplicate else None)}
        plan = {'opening_amount': str(opening), 'spent_transactions': [t.id for t in expenses],
                'withdrawal': withdrawal.id, 'split_transaction': split.id,
                'grant_amount': str(granted), 'own_amount': str(split.amount - granted),
                'remove_duplicate_allocation': duplicate.id if duplicate else None}
        self.stdout.write(json.dumps({'apply': options['apply'], 'plan': plan, 'before': before}, ensure_ascii=False))
        if not options['apply']:
            return
        source.opening_amount = opening
        source.save(update_fields=['opening_amount'])
        if duplicate:
            duplicate.delete()
        for expense in expenses:
            ledger_v2.classify_transaction(
                expense, economic_kind='operating_expense', ownership_scope='business', funding_source=source,
                note='Підтверджено власником: витрата з окремого рахунку УВФ',
            )
            ledger_v2.allocate_funding(source=source, txn=expense, amount=expense.amount, replace=True)
        ledger_v2.sync_grant_allocation(
            split, source=split_source, amount=granted,
            note='Підтверджено власником: грантова частина та власне співфінансування з пенсійного рахунку',
        )
        group = grant_replenishment.register_withdrawal(source, withdrawal)
        bank_after = list(Transaction.objects.filter(company=source.company).order_by('id').values_list(*bank_fields))
        balances_after = list(source.company.accounts.order_by('id').values_list('id', 'current_balance'))
        if bank_before != bank_after or balances_before != balances_after:
            raise CommandError('Bank ledger or account balances changed; all corrections rolled back')
        after = {'source': {k: str(v) for k, v in ledger_v2.funding_summary(source).items()},
                 'split_source': {k: str(v) for k, v in ledger_v2.funding_summary(split_source).items()},
                 'replenishment_group_id': group.id, 'bank_ledger_unchanged': True}
        AuditLog.objects.create(
            company=source.company, action='grant_reconcile', entity_type='FundingSource', entity_id=str(source.id),
            summary='Підтверджений початковий грантовий залишок, усунення дубля, співфінансування та повернення виведення',
            before=before, after=after, source='management',
        )
        self.stdout.write(json.dumps({'after': after}, ensure_ascii=False))
