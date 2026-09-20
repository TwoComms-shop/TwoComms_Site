"""Confirm historical actual operations from the dedicated grant account."""
from django.core.management.base import BaseCommand, CommandError

from finance.models import Account, FundingSource, get_default_company
from finance.services.ledger_v2 import confirm_grant_account_history


class Command(BaseCommand):
    help = 'Класифікує фактичну історію рахунку «Грантова» як операції УВФ.'

    def add_arguments(self, parser):
        parser.add_argument('--account-id', type=int, default=None)
        parser.add_argument('--source-id', type=int, default=None)
        parser.add_argument('--apply', action='store_true', help='Застосувати підтвердження')

    def handle(self, *args, **options):
        company = get_default_company()
        account = (company.accounts.filter(id=options['account_id']).first()
                   if options['account_id'] else company.accounts.filter(name__iexact='Грантова').first())
        source = (company.funding_sources.filter(id=options['source_id']).first()
                  if options['source_id'] else company.funding_sources.filter(name__iexact='УВФ').first())
        if account is None:
            raise CommandError('Рахунок «Грантова» не знайдено')
        if source is None:
            raise CommandError('Джерело «УВФ» не знайдено')
        rows = list(account.transactions.filter(status='actual').order_by('date_actual', 'id'))
        self.stdout.write(f'Рахунок: {account.name} ({account.id}); джерело: {source.name} ({source.id})')
        self.stdout.write(f'Фактичних операцій: {len(rows)}')
        if not options['apply']:
            self.stdout.write('Перевірка завершена. Для застосування додайте --apply.')
            return
        changed = confirm_grant_account_history(account, source)
        self.stdout.write(self.style.SUCCESS(f'Підтверджено операцій: {len(changed)}; майбутні залишено для review.'))
