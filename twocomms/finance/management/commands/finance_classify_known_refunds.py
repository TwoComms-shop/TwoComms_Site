from django.core.management.base import BaseCommand

from finance.models import Transaction, get_default_company
from finance.services import ledger_v2


class Command(BaseCommand):
    help = 'Classify only deterministic Viktor rent/utility refunds; dry-run by default.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Застосувати класифікацію')

    def handle(self, *args, **options):
        company = get_default_company()
        rows = Transaction.objects.filter(
            company=company, status=Transaction.STATUS_ACTUAL,
            type=Transaction.TYPE_INCOME, economic_kind='unknown',
        ).select_related('recurrence_rule', 'counterparty', 'category', 'account')
        matched = [txn for txn in rows if ledger_v2.is_known_rent_refund(txn)]
        mode = 'Застосовано' if options['apply'] else 'Підготовлено (dry-run)'
        self.stdout.write(f'{mode}: {len(matched)} повернень')
        for txn in matched:
            if options['apply']:
                ledger_v2.classify_known_rent_refund(txn)
            self.stdout.write(f'#{txn.id} {txn.amount} {txn.comment}')
