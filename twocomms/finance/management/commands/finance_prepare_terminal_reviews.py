"""Prepare review markers for previously imported terminal top-ups."""
from django.core.management.base import BaseCommand

from finance.models import Transaction, get_default_company
from finance.services.ledger_v2 import ensure_terminal_cash_decision_review, terminal_cash_evidence


class Command(BaseCommand):
    help = 'Готує пропозиції для поповнень через Mono/City24 без зміни історії'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Створити review-маркери')

    def handle(self, *args, **options):
        company = get_default_company()
        candidates = Transaction.objects.filter(
            company=company, status=Transaction.STATUS_ACTUAL,
            type=Transaction.TYPE_INCOME, economic_kind='unknown',
        ).select_related('account').order_by('date_actual', 'id')
        count = created = 0
        for txn in candidates:
            if not terminal_cash_evidence(txn)['is_candidate']:
                continue
            count += 1
            if options['apply']:
                before = txn.classification_reviews.filter(status='pending').count()
                ensure_terminal_cash_decision_review(txn)
                after = txn.classification_reviews.filter(status='pending').count()
                created += int(after > before)
            self.stdout.write(
                f'#{txn.id}: {txn.date_actual:%d.%m.%Y} {txn.amount} {txn.currency} · {txn.comment[:80]}'
            )
        mode = 'Створено' if options['apply'] else 'Знайдено'
        self.stdout.write(self.style.SUCCESS(f'{mode}: {created if options["apply"] else count} з {count} кандидатів.'))
