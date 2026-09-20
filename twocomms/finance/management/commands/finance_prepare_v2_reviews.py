"""Prepare explicit grant sources and review proposals for known bank inflows."""
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand
from django.db import transaction

from finance.models import ClassificationReview, FundingSource, Transaction, get_default_company


class Command(BaseCommand):
    help = 'Готовит отдельные источники УВФ и Людина в біді без автоклассификации истории'

    SOURCES = (
        {
            'name': 'УВФ',
            'amount': Decimal('276481'),
            'date': '2026-06-05',
            'transaction_amount': Decimal('276481'),
            'note': 'Входящий грантовый платеж на счет Грантова',
        },
        {
            'name': 'Людина в біді',
            'amount': Decimal('216000'),
            'date': '2026-09-08',
            'transaction_amount': Decimal('216000'),
            'note': 'Входящий грантовый платеж на monobank white',
        },
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Записать источники и review-предложения')

    def handle(self, *args, **options):
        company = get_default_company()
        for spec in self.SOURCES:
            start = datetime.fromisoformat(spec['date']).replace(tzinfo=timezone.utc)
            txn = Transaction.objects.filter(
                company=company,
                type=Transaction.TYPE_INCOME,
                amount=spec['transaction_amount'],
                date_actual__gte=start,
                date_actual__lt=start + timedelta(days=1),
            ).order_by('id').first()
            source = FundingSource.objects.filter(company=company, name=spec['name']).first()
            if not options['apply']:
                self.stdout.write(
                    f"{spec['name']}: source={'существует' if source else 'будет создан'}, "
                    f"transaction={txn.id if txn else 'не найден'}"
                )
                continue
            if not txn:
                self.stdout.write(self.style.WARNING(f"{spec['name']}: операция не найдена, пропуск"))
                continue
            with transaction.atomic():
                source, _ = FundingSource.objects.get_or_create(
                    company=company,
                    name=spec['name'],
                    defaults={
                        'source_type': 'grant',
                        'received_amount': spec['amount'],
                        'received_at': spec['date'],
                        'notes': spec['note'],
                    },
                )
                review, created = ClassificationReview.objects.get_or_create(
                    company=company,
                    transaction=txn,
                    status='pending',
                    defaults={
                        'proposal': {
                            'ownership_scope': 'business',
                            'economic_kind': 'grant_inflow',
                            'funding_source_id': source.id,
                            'note': spec['note'],
                        },
                        'reason': f"Входящий платеж {txn.amount} похож на грант «{spec['name']}».",
                        'impact': {
                            'pnl': '0',
                            'cashflow': 'targeted_inflow',
                            'funding_source': spec['name'],
                        },
                        'confidence': Decimal('95'),
                    },
                )
            state = 'создано' if created else 'уже было'
            self.stdout.write(self.style.SUCCESS(
                f"{spec['name']}: источник id={source.id}, review id={review.id} ({state}); история не изменена"
            ))
