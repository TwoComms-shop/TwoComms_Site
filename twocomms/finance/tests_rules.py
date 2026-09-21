"""Тести Блоку 8: двигун автоправил (умови, дії, застосування до історії)."""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from finance.models import Account, AutomationRule, Category, Transaction, get_default_company
from finance.services import rules_engine
from finance.services import transactions as txn_service

User = get_user_model()


class RulesEngineTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('fin_rule', 'rule@x.com', 'x')
        self.company = get_default_company()
        self.acc = Account.objects.create(company=self.company, name='Банк', currency='UAH',
                                           initial_balance=Decimal('0'), current_balance=Decimal('0'))
        self.cat = Category.objects.create(company=self.company, name='Реклама', type='expense')

    def _rule(self, **kw):
        defaults = dict(company=self.company, name='R', is_enabled=True, priority=100,
                        transaction_type='expense',
                        conditions=[{'field': 'comment', 'operator': 'contains', 'value': 'facebook'}],
                        actions=[{'action': 'set_category', 'value': self.cat.id, 'overwrite': True}])
        defaults.update(kw)
        return AutomationRule.objects.create(**defaults)

    def test_rule_matches_and_applies(self):
        rule = self._rule()
        txn = txn_service.create_transaction(user=self.user, type=Transaction.TYPE_EXPENSE,
                                             amount=Decimal('100'), account=self.acc,
                                             comment='Оплата Facebook Ads')
        applied = rules_engine.apply_rules_to_transaction(txn, user=self.user)
        self.assertIn(rule.id, applied)
        txn.refresh_from_db()
        self.assertEqual(txn.category_id, self.cat.id)

    def test_rule_no_match(self):
        self._rule()
        txn = txn_service.create_transaction(user=self.user, type=Transaction.TYPE_EXPENSE,
                                             amount=Decimal('100'), account=self.acc,
                                             comment='Оренда офісу')
        applied = rules_engine.apply_rules_to_transaction(txn, user=self.user)
        self.assertEqual(applied, [])
        txn.refresh_from_db()
        self.assertIsNone(txn.category_id)

    def test_amount_condition(self):
        rule = self._rule(conditions=[{'field': 'amount', 'operator': 'gt', 'value': '500'}])
        big = txn_service.create_transaction(user=self.user, type=Transaction.TYPE_EXPENSE,
                                             amount=Decimal('600'), account=self.acc, comment='x')
        small = txn_service.create_transaction(user=self.user, type=Transaction.TYPE_EXPENSE,
                                               amount=Decimal('100'), account=self.acc, comment='y')
        self.assertTrue(rules_engine.rule_matches(big, rule))
        self.assertFalse(rules_engine.rule_matches(small, rule))

    def test_provider_condition_reads_import_metadata(self):
        rule = self._rule(
            conditions=[{'field': 'provider', 'operator': 'equals', 'value': 'novapay'}],
        )
        txn = txn_service.create_transaction(
            user=self.user, type=Transaction.TYPE_EXPENSE, amount=Decimal('100'),
            account=self.acc, comment='NovaPay', external_data={'provider': 'novapay'},
        )
        self.assertTrue(rules_engine.rule_matches(txn, rule))

    def test_apply_to_existing(self):
        rule = self._rule()
        for i in range(3):
            txn_service.create_transaction(user=self.user, type=Transaction.TYPE_EXPENSE,
                                           amount=Decimal('50'), account=self.acc,
                                           comment='facebook promo')
        qs = Transaction.objects.filter(company=self.company)
        count = rules_engine.apply_to_existing(rule, qs, user=self.user)
        self.assertEqual(count, 3)

    @override_settings(ALLOWED_HOSTS=['fin.twocomms.shop', 'testserver'])
    def test_rules_page_renders(self):
        self.client.force_login(self.user)
        resp = self.client.get('/rules/', HTTP_HOST='fin.twocomms.shop')
        self.assertEqual(resp.status_code, 200)
