"""Attach verified treasury recipients to the two tax components.

The command only creates or updates recipient cards and component defaults. It
does not edit imported transactions or create settlements. It is dry-run by
default so the IBAN evidence can be reviewed before applying it in production.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from finance.models import Counterparty, ObligationComponent, Transaction, get_default_company
from finance.services.cards import upsert_card


class Command(BaseCommand):
    help = 'Синхронізує підтверджені IBAN Податкової з компонентами податків'

    SPECS = (
        ('Воєнний збір', 'Военный сбор', '865.00', 'Військовий збір за місяць'),
        ('Єдиний податок', 'Единый налог', '1729.00', 'Єдиний податок за місяць'),
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Записати реквізити в базу')

    def handle(self, *args, **options):
        company = get_default_company()
        counterparty = Counterparty.objects.filter(
            company=company, name__iregex=r'подат|налог',
        ).first()
        if not counterparty:
            self.stdout.write(self.style.ERROR('Контрагента «Податкова» не знайдено'))
            return

        for uk_name, legacy_name, amount, purpose in self.SPECS:
            component = ObligationComponent.objects.filter(
                group__company=company, group__title='Налоги',
                name__in=[uk_name, legacy_name],
            ).first()
            if not component:
                self.stdout.write(self.style.WARNING(f'{uk_name}: компонент не знайдено'))
                continue
            txn = Transaction.objects.filter(
                company=company, type=Transaction.TYPE_EXPENSE,
                amount=amount,
            ).exclude(external_data__counter_iban='').order_by('-date_actual', '-id').first()
            iban = (txn.external_data or {}).get('counter_iban', '') if txn else ''
            if not iban:
                self.stdout.write(self.style.WARNING(f'{uk_name}: IBAN у виписці не знайдено'))
                continue
            self.stdout.write(
                f'{uk_name}: {iban} (операція #{txn.id}, {txn.date_actual:%d.%m.%Y}, {amount} грн)'
            )
            if not options['apply']:
                continue
            card = upsert_card(
                counterparty,
                iban=iban,
                bank='Казначейство України',
                label=uk_name,
                make_primary=False,
                when=txn.date_actual,
            )
            component.recipient_card = card
            component.payment_purpose_template = purpose
            component.save(update_fields=['recipient_card', 'payment_purpose_template'])
            self.stdout.write(self.style.SUCCESS(f'  привʼязано картку #{card.id} до компонента #{component.id}'))

        if not options['apply']:
            self.stdout.write('Dry-run: для запису додайте --apply. Операції та їх історія не змінюються.')
