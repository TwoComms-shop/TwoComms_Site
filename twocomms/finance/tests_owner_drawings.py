"""Тести для Owner's Drawings (вивід на особисте) — БЛОК 1."""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from finance.models import Category, Company, Account, Transaction
from finance.services import mono, reports


class OwnerDrawingsTestCase(TestCase):
    """Тести для функціональності виведення на особисте."""

    def setUp(self):
        """Створення тестових даних."""
        self.company = Company.objects.create(name='Test Company', base_currency='UAH')

        # Створення системної категорії "Вивід на особисте"
        self.owner_cat = Category.objects.create(
            company=self.company,
            name='Вивід на особисте',
            type='both',
            color='#9333ea',
            icon='arrow-down-to-line',
            is_system=True,
        )

        # Рахунки
        self.fop_account = Account.objects.create(
            company=self.company,
            name='ФОП рахунок',
            type='bank',
            currency='UAH',
            is_business=True,
            initial_balance=Decimal('100000'),
            iban='UA000000000000000000000000010',
        )

        self.personal_account = Account.objects.create(
            company=self.company,
            name='Особиста картка',
            type='card',
            currency='UAH',
            is_business=False,
            initial_balance=Decimal('0'),
            iban='UA000000000000000000000000011',
        )

    def test_reconcile_with_exact_match(self):
        """Тест злиття переказів з точним збігом сум."""
        # Створюємо витрату на ФОП (переказ на ВЛАСНУ особисту картку)
        expense = Transaction.objects.create(
            company=self.company,
            type=Transaction.TYPE_EXPENSE,
            status=Transaction.STATUS_ACTUAL,
            amount=Decimal('5000'),
            currency='UAH',
            account=self.fop_account,
            date_actual=timezone.now(),
            source='integration',
            external_id='mono:exp1',
            external_data={'counter_iban': self.personal_account.iban},
        )

        # Створюємо дохід на особистій картці
        income = Transaction.objects.create(
            company=self.company,
            type=Transaction.TYPE_INCOME,
            status=Transaction.STATUS_ACTUAL,
            amount=Decimal('5000'),
            currency='UAH',
            account=self.personal_account,
            date_actual=timezone.now(),
            source='integration',
            external_id='mono:inc1',
        )

        # Запускаємо reconcile
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.create(username='test')

        matched = mono.reconcile_internal_transfers(self.company, user=user)

        # Перевіряємо результат
        self.assertEqual(matched, 1)

        # Витрата має перетворитись на переказ
        expense.refresh_from_db()
        self.assertEqual(expense.type, Transaction.TYPE_TRANSFER)
        self.assertEqual(expense.to_account, self.personal_account)
        self.assertFalse(expense.is_business)

        # Дохід має бути видалений
        self.assertFalse(Transaction.objects.filter(id=income.id).exists())

    def test_reconcile_with_commission(self):
        """Тест злиття переказів з комісією (різниця в сумі)."""
        # Витрата 5000 грн (переказ на ВЛАСНУ особисту картку)
        expense = Transaction.objects.create(
            company=self.company,
            type=Transaction.TYPE_EXPENSE,
            status=Transaction.STATUS_ACTUAL,
            amount=Decimal('5000'),
            currency='UAH',
            account=self.fop_account,
            date_actual=timezone.now(),
            source='integration',
            external_id='mono:exp2',
            external_data={'counter_iban': self.personal_account.iban},
        )

        # Дохід 4950 грн (комісія 50 грн = 1%)
        income = Transaction.objects.create(
            company=self.company,
            type=Transaction.TYPE_INCOME,
            status=Transaction.STATUS_ACTUAL,
            amount=Decimal('4950'),
            currency='UAH',
            account=self.personal_account,
            date_actual=timezone.now(),
            source='integration',
            external_id='mono:inc2',
        )

        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.create(username='test2')

        # Запускаємо reconcile з толерантністю 2%
        matched = mono.reconcile_internal_transfers(
            self.company, user=user, tolerance_percent=2.0
        )

        # Має знайти пару навіть з різницею
        self.assertEqual(matched, 1)

        expense.refresh_from_db()
        self.assertEqual(expense.type, Transaction.TYPE_TRANSFER)
        self.assertEqual(expense.to_amount, Decimal('4950'))

    def test_owner_drawings_report(self):
        """Тест звіту по виведенням власника."""
        # Створюємо переказ з категорією "Вивід на особисте"
        transfer = Transaction.objects.create(
            company=self.company,
            type=Transaction.TYPE_TRANSFER,
            status=Transaction.STATUS_ACTUAL,
            amount=Decimal('10000'),
            currency='UAH',
            account=self.fop_account,
            to_account=self.personal_account,
            to_amount=Decimal('10000'),
            date_actual=timezone.now(),
            category=self.owner_cat,
            is_business=False,
        )

        # Створюємо бізнес-дохід
        Transaction.objects.create(
            company=self.company,
            type=Transaction.TYPE_INCOME,
            status=Transaction.STATUS_ACTUAL,
            amount=Decimal('50000'),
            amount_base=Decimal('50000'),
            currency='UAH',
            account=self.fop_account,
            date_actual=timezone.now(),
            is_business=True,
        )

        # Створюємо бізнес-витрату
        Transaction.objects.create(
            company=self.company,
            type=Transaction.TYPE_EXPENSE,
            status=Transaction.STATUS_ACTUAL,
            amount=Decimal('30000'),
            amount_base=Decimal('30000'),
            currency='UAH',
            account=self.fop_account,
            date_actual=timezone.now(),
            is_business=True,
        )

        # Генеруємо звіт (period='all' — стійко до меж місяця)
        data = reports.owner_drawings_report(self.company, {'period': 'all'})

        # Перевіряємо результати
        self.assertEqual(data['total_withdrawn'], Decimal('10000'))
        self.assertEqual(data['business_profit'], Decimal('20000'))  # 50000 - 30000
        self.assertEqual(data['withdrawal_percent'], 50.0)  # 10000 / 20000 * 100
        self.assertEqual(len(data['transfers']), 1)

    def test_owner_drawings_excluded_from_pnl(self):
        """Тест що виведення не впливають на P&L."""
        # Створюємо переказ (вивід на особисте)
        Transaction.objects.create(
            company=self.company,
            type=Transaction.TYPE_TRANSFER,
            status=Transaction.STATUS_ACTUAL,
            amount=Decimal('10000'),
            currency='UAH',
            account=self.fop_account,
            to_account=self.personal_account,
            to_amount=Decimal('10000'),
            date_actual=timezone.now(),
            category=self.owner_cat,
            is_business=False,
        )

        # Бізнес-операції
        Transaction.objects.create(
            company=self.company,
            type=Transaction.TYPE_INCOME,
            status=Transaction.STATUS_ACTUAL,
            amount=Decimal('50000'),
            amount_base=Decimal('50000'),
            currency='UAH',
            account=self.fop_account,
            date_actual=timezone.now(),
            is_business=True,
        )

        Transaction.objects.create(
            company=self.company,
            type=Transaction.TYPE_EXPENSE,
            status=Transaction.STATUS_ACTUAL,
            amount=Decimal('30000'),
            amount_base=Decimal('30000'),
            currency='UAH',
            account=self.fop_account,
            date_actual=timezone.now(),
            is_business=True,
        )

        # P&L звіт
        pnl_data = reports.pnl(self.company, {'period': 'all'})

        # Вивід показується як окрема витратна категорія і входить у підсумок.
        self.assertEqual(pnl_data['profit'], Decimal('10000'))
        self.assertEqual(pnl_data['income'], Decimal('50000'))
        self.assertEqual(pnl_data['expenses'], Decimal('40000'))
        self.assertEqual(pnl_data['owner_drawn'], Decimal('10000'))
        self.assertTrue(any(row['name'] == 'Вивід на особисте'
                            and row['total'] == 10000.0
                            for row in pnl_data['expense_by_category']))
