"""Prepare editable composite obligation templates.

The command is dry-run by default.  ``--apply`` only creates/updates the new
v2 groups and components; it never changes imported transactions or old plans.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from finance.models import Counterparty, get_default_company
from finance.models_finance_v2 import ObligationComponent, ObligationGroup


class Command(BaseCommand):
    help = 'Готовит группы Налоги, Квартира и Офис для composite obligations v2'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Записать шаблоны в базу')

    def handle(self, *args, **options):
        company = get_default_company()
        cp_qs = Counterparty.objects.filter(company=company)
        tax_cp = cp_qs.filter(type='government').filter(name__iregex=r'подат|налог').first()
        home_cp = cp_qs.filter(name__iregex=r'влад').first()
        office_cp = cp_qs.filter(name__iregex=r'виктор|владимир|викторов').first()
        specs = [
            ('Налоги', tax_cp, [
                ('Военный сбор', Decimal('865'), 'Військовий збір за місяць'),
                ('Единый налог', Decimal('1729'), 'Єдиний податок за місяць'),
            ]),
            ('Квартира Влада мамы', home_cp, [
                ('Квартплата', Decimal('4000'), 'rent'),
                ('Коммунальные', None, 'utilities'),
            ]),
            ('Офис Виктора Викторовича', office_cp, [
                ('Аренда', Decimal('15000'), 'office rent'),
                ('Коммунальный аванс', Decimal('21000'), 'office utilities prepayment'),
            ]),
        ]
        for title, cp, components in specs:
            group = ObligationGroup.objects.filter(company=company, title=title).first()
            if not options['apply']:
                state = 'существует' if group else 'будет создана'
                self.stdout.write(f'{title}: {state}, контрагент={cp.name if cp else "не найден"}')
                continue
            group, _ = ObligationGroup.objects.get_or_create(
                company=company, title=title,
                defaults={'counterparty': cp, 'type': 'expense', 'role': 'charge'},
            )
            if cp and group.counterparty_id != cp.id:
                group.counterparty = cp
                group.save(update_fields=['counterparty'])
            for name, amount, purpose in components:
                component, _ = ObligationComponent.objects.get_or_create(
                    group=group, name=name,
                    defaults={'fixed_amount': amount, 'payment_purpose_template': purpose},
                )
                if amount is not None and component.fixed_amount != amount:
                    component.fixed_amount = amount
                    component.save(update_fields=['fixed_amount'])
                if purpose and component.payment_purpose_template in {'', 'military tax', 'unified tax'}:
                    component.payment_purpose_template = purpose
                    component.save(update_fields=['payment_purpose_template'])
            self.stdout.write(self.style.SUCCESS(f'готово: {title} (id={group.id})'))
        if not options['apply']:
            self.stdout.write('Dry-run: для записи добавьте --apply. История операций не меняется.')
