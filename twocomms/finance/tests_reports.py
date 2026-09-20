"""Тести Блоку 6: звіти Cash Flow, P&L, дебіторка, баланс, рендер сторінок."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from finance.models import Account, FundingSource, Transaction, get_default_company
from finance.services import reports as rep
from finance.services import reports_debt as repd
from finance.services import transactions as txn_service

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['fin.twocomms.shop', 'testserver'])
class ReportsTests(TestCase):
    FIN_HOST = 'fin.twocomms.shop'

    def setUp(self):
        self.user = User.objects.create_superuser('fin_r', 'r@x.com', 'x')
        self.company = get_default_company()
        self.acc = Account.objects.create(company=self.company, name='Банк', currency='UAH',
                                           initial_balance=Decimal('0'), current_balance=Decimal('0'))
        now = timezone.now()
        txn_service.create_transaction(user=self.user, type=Transaction.TYPE_INCOME,
                                       amount=Decimal('1000'), account=self.acc, date_actual=now)
        txn_service.create_transaction(user=self.user, type=Transaction.TYPE_EXPENSE,
                                       amount=Decimal('400'), account=self.acc, date_actual=now)
        # Переказ не має впливати на P&L.
        acc2 = Account.objects.create(company=self.company, name='Каса', currency='UAH')
        txn_service.create_transaction(user=self.user, type=Transaction.TYPE_TRANSFER,
                                       amount=Decimal('200'), account=self.acc, to_account=acc2,
                                       to_amount=Decimal('200'), date_actual=now)

    def test_cash_flow_excludes_transfers(self):
        data = rep.cash_flow(self.company, {'period': 'year'})
        self.assertEqual(data['cash_in'], Decimal('1000'))
        self.assertEqual(data['cash_out'], Decimal('400'))
        self.assertEqual(data['net'], Decimal('600'))

    def test_cash_flow_separates_grant_from_operating_series(self):
        source = FundingSource.objects.create(
            company=self.company, name='Людина в біді', received_amount=Decimal('216000'),
        )
        grant = txn_service.create_transaction(
            user=self.user, type=Transaction.TYPE_INCOME, amount=Decimal('216000'),
            account=self.acc, date_actual=timezone.now(), is_business=True,
        )
        grant.economic_kind = 'grant_inflow'
        grant.funding_source = source
        grant.save(update_fields=['economic_kind', 'funding_source'])
        data = rep.cash_flow(self.company, {'period': 'year'})
        self.assertEqual(data['cash_in'], Decimal('1000'))
        self.assertEqual(data['targeted_in'], Decimal('216000'))
        self.assertEqual(sum(bucket['in'] for bucket in data['series']), 1000.0)

    def test_pnl_profit(self):
        data = rep.pnl(self.company, {'period': 'year'})
        self.assertEqual(data['income'], Decimal('1000'))
        self.assertEqual(data['expenses'], Decimal('400'))
        self.assertEqual(data['profit'], Decimal('600'))

    def test_receivables_from_planned(self):
        txn_service.create_transaction(user=self.user, type=Transaction.TYPE_INCOME,
                                       amount=Decimal('750'), account=self.acc,
                                       status=Transaction.STATUS_PLANNED,
                                       date_actual=timezone.now() + dt.timedelta(days=5))
        data = repd.receivables(self.company)
        self.assertEqual(data['total'], Decimal('750'))

    def test_balance_sheet_balanced(self):
        data = repd.balance_sheet(self.company)
        # Активи (гроші 600 + дебіторка) мають дорівнювати пасивам (капітал 0 + прибуток 600).
        self.assertTrue(data['balanced'])

    def test_report_pages_render(self):
        self.client.force_login(self.user)
        for kind in ('cashflow', 'pnl', 'receivables', 'payables', 'statement',
                     'forecast', 'owner_drawings', 'personal_expenses',
                     'projects', 'audit', 'balance', 'plan_fact', 'metrics'):
            resp = self.client.get(f'/analytic/report/{kind}/', HTTP_HOST=self.FIN_HOST)
            self.assertEqual(resp.status_code, 200, f'{kind} → {resp.status_code}')

    def test_analytics_report_insight_icons_have_scoped_sizes(self):
        css_path = settings.BASE_DIR / 'static' / 'css' / 'finance-fixes.css'
        css = css_path.read_text(encoding='utf-8')

        for expected in (
            '.fin-insight-card__head',
            '.fin-insight-card__head svg',
            'width: 18px',
            'height: 18px',
            'flex: 0 0 18px',
        ):
            self.assertIn(expected, css)

        for template_name in (
            'personal_expenses.html',
            'owner_drawings.html',
            'forecast.html',
        ):
            template_path = (
                Path(settings.BASE_DIR)
                / 'finance'
                / 'templates'
                / 'finance'
                / 'reports'
                / template_name
            )
            html = template_path.read_text(encoding='utf-8')
            self.assertIn('class="fin-insight-card__head"', html)
            self.assertNotIn('<style>', html)

    def test_export_xlsx(self):
        self.client.force_login(self.user)
        resp = self.client.get('/analytic/report/projects/export/?period=year', HTTP_HOST=self.FIN_HOST)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheet', resp['Content-Type'])
