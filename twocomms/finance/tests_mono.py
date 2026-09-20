"""Тести інтеграції Monobank: шифрування токена, мапінг виписки, антидублі,
звірка балансу, бізнес/особисте, перетворення внутрішніх переказів, безпека
вебхука. Зовнішній API мокаємо — реальні запити до monobank не йдуть.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, Client

from finance.models import Account, ClassificationReview, IntegrationConnection, Transaction, get_default_company
from finance.services import crypto, mono as mono_service
from finance.services import mono_api

User = get_user_model()


def _item(_id, amount, t=1714000000, desc='op', hold=False, balance=None, mcc=None,
          counter_iban=None, counter_name=None):
    d = {'id': _id, 'time': t, 'amount': amount, 'description': desc, 'hold': hold}
    if balance is not None:
        d['balance'] = balance
    if mcc is not None:
        d['mcc'] = mcc
    if counter_iban is not None:
        d['counterIban'] = counter_iban
    if counter_name is not None:
        d['counterName'] = counter_name
    return d


# IBAN-и для тестів власних переказів.
IBAN_BLACK = 'UA000000000000000000000000001'
IBAN_FOP = 'UA000000000000000000000000002'
IBAN_FOREIGN = 'UA999999999999999999999999999'


class CryptoTests(TestCase):
    def test_encrypt_roundtrip(self):
        token = 'fake-test-token-DO-NOT-USE-0000000000000'
        enc = crypto.encrypt(token)
        self.assertNotIn(token, enc)  # не зберігаємо у відкритому вигляді
        self.assertEqual(crypto.decrypt(enc), token)

    def test_fingerprint_stable_and_masking(self):
        token = 'secrettoken123456'
        self.assertEqual(crypto.fingerprint(token), crypto.fingerprint(token))
        self.assertNotEqual(crypto.fingerprint(token), crypto.fingerprint('other'))
        self.assertTrue(crypto.mask(token).endswith(token[-4:]))
        self.assertNotIn(token[:6], crypto.mask(token))

    def test_decrypt_garbage_raises(self):
        with self.assertRaises(crypto.TokenCryptoError):
            crypto.decrypt('not-a-valid-token')


class MonoApiHelperTests(TestCase):
    def test_iso_alpha(self):
        self.assertEqual(mono_api.iso_alpha(980), 'UAH')
        self.assertEqual(mono_api.iso_alpha(840), 'USD')
        self.assertEqual(mono_api.iso_alpha(99999), 'UAH')  # фолбек

    def test_account_business_detection(self):
        fop = mono_api.MonoAccount('a', 's', 0, 0, 'fop', 980, [], 'UA1')
        card = mono_api.MonoAccount('b', 's', 0, 0, 'black', 980, ['1234'], 'UA2')
        self.assertTrue(mono_service._is_business_account(fop))
        self.assertFalse(mono_service._is_business_account(card))


class MonoSyncTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('fin_m', 'm@x.com', 'x')
        self.company = get_default_company()
        self.conn = IntegrationConnection.objects.create(
            company=self.company, provider='monobank', status='success',
            connection_method='token')
        self.conn.set_token('TESTTOKEN1234567890')
        self.conn.save()
        self.acc = Account.objects.create(
            company=self.company, name='mono black', currency='UAH',
            integration=self.conn, external_account_id='acc-1',
            external_kind='card', is_business=False, iban=IBAN_BLACK)

    def test_import_maps_amount_and_type(self):
        items = [_item('m1', 50000), _item('m2', -20000)]  # +500 / -200 грн
        res = mono_service.import_statement(self.acc, items, user=self.user, apply_rules=False)
        self.assertEqual(res['created'], 2)
        inc = Transaction.objects.get(external_id='mono:m1')
        exp = Transaction.objects.get(external_id='mono:m2')
        self.assertEqual(inc.type, Transaction.TYPE_INCOME)
        self.assertEqual(inc.amount, Decimal('500.00'))
        self.assertEqual(exp.type, Transaction.TYPE_EXPENSE)
        self.assertEqual(exp.amount, Decimal('200.00'))

    def test_dedup_by_external_id(self):
        items = [_item('m1', 50000)]
        mono_service.import_statement(self.acc, items, user=self.user, apply_rules=False)
        res = mono_service.import_statement(self.acc, items, user=self.user, apply_rules=False)
        self.assertEqual(res['created'], 0)
        self.assertEqual(res['skipped'], 1)
        self.assertEqual(Transaction.objects.filter(external_id='mono:m1').count(), 1)

    def test_auto_categorize_by_mcc(self):
        """Витрата з MCC автоматично отримує відповідну категорію."""
        from finance.models import Category
        # 5411 = супермаркети → 'Продукти'. apply_rules=False, щоб перевірити суто MCC.
        mono_service.import_statement(
            self.acc, [_item('g1', -25000, mcc=5411)], user=self.user, apply_rules=False)
        exp = Transaction.objects.get(external_id='mono:g1')
        self.assertIsNotNone(exp.category)
        self.assertEqual(exp.category.name, 'Продукти')
        # Категорія створена один раз — повторний імпорт не дублює.
        mono_service.import_statement(
            self.acc, [_item('g2', -13000, mcc=5411)], user=self.user, apply_rules=False)
        self.assertEqual(Category.objects.filter(company=self.company, name='Продукти').count(), 1)

    def test_auto_categorize_skips_unknown_mcc(self):
        """Невідомий MCC ('other') не нав'язує категорію."""
        mono_service.import_statement(
            self.acc, [_item('u1', -5000, mcc=9999)], user=self.user, apply_rules=False)
        exp = Transaction.objects.get(external_id='mono:u1')
        self.assertIsNone(exp.category)

    def test_auto_categorize_skips_income(self):
        """Доходи не категоризуються за MCC (лише витрати)."""
        mono_service.import_statement(
            self.acc, [_item('i9', 50000, mcc=5411)], user=self.user, apply_rules=False)
        inc = Transaction.objects.get(external_id='mono:i9')
        self.assertIsNone(inc.category)

    def test_terminal_income_creates_review_marker_without_changing_history(self):
        mono_service.import_statement(
            self.acc, [_item('terminal-review', 50000, desc='Термінал mono')],
            user=self.user, apply_rules=False,
        )
        txn = Transaction.objects.get(external_id='mono:terminal-review')
        review = ClassificationReview.objects.get(transaction=txn, status='pending')
        self.assertEqual(review.proposal['kind'], 'terminal_cash_decision')
        self.assertEqual(txn.type, Transaction.TYPE_INCOME)
        self.assertEqual(txn.economic_kind, 'unknown')

    def test_hold_imports_as_actual_and_counts_in_balance(self):
        """Hold-операції (заблоковані кошти) — фактичні: входять у баланс та звіти.

        Monobank блокує/знімає кошти одразу, тож такі операції мають враховуватись
        в аналітиці. Прапор hold зберігається в external_data; нульові — пропускаємо.
        """
        items = [_item('h1', 10000, hold=True), _item('z1', 0)]
        res = mono_service.import_statement(self.acc, items, user=self.user, apply_rules=False)
        self.assertEqual(res['created'], 1)
        txn = Transaction.objects.get(external_id='mono:h1')
        self.assertEqual(txn.status, Transaction.STATUS_ACTUAL)
        self.assertTrue(txn.external_data.get('hold'))
        # +100 грн дохід відображено у балансі одразу.
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.current_balance, Decimal('100.00'))

    def test_settled_hold_updates_existing_transaction_to_actual(self):
        mono_service.import_statement(
            self.acc, [_item('h1', -8000, hold=True, balance=100000)],
            user=self.user, apply_rules=False)
        mono_service.import_statement(
            self.acc, [_item('h1', -8000, hold=False, balance=92000)],
            user=self.user, apply_rules=False)
        self.assertEqual(Transaction.objects.filter(external_id='mono:h1').count(), 1)
        txn = Transaction.objects.get(external_id='mono:h1')
        self.assertEqual(txn.status, Transaction.STATUS_ACTUAL)
        self.assertFalse(txn.external_data.get('hold', False))
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.current_balance, Decimal('-80.00'))

    def test_business_inherited_from_account(self):
        self.acc.is_business = True
        self.acc.save()
        mono_service.import_statement(self.acc, [_item('b1', 50000)],
                                     user=self.user, apply_rules=False)
        self.assertTrue(Transaction.objects.get(external_id='mono:b1').is_business)

    def test_rich_data_and_mcc_captured(self):
        item = _item('r1', -15000, mcc=5411, balance=84000)  # супермаркет
        item['cashbackAmount'] = 150
        item['operationAmount'] = -15000
        item['currencyCode'] = 980
        mono_service.import_statement(self.acc, [item], user=self.user, apply_rules=False)
        txn = Transaction.objects.get(external_id='mono:r1')
        self.assertEqual(txn.mcc, 5411)
        self.assertEqual(txn.external_data.get('cashback_amount'), 150)
        self.assertEqual(txn.external_data.get('provider'), 'monobank')
        from finance.services import mcc as mcc_mod
        self.assertEqual(mcc_mod.group_for_mcc(5411), 'groceries')
        self.assertEqual(mcc_mod.group_for_mcc(99999), 'other')

    def test_reconcile_balance_backcalc(self):
        # Імпортуємо рух на +500, але банк каже баланс 1500 → initial має дотягнутись.
        mono_service.import_statement(self.acc, [_item('m1', 50000)],
                                     user=self.user, apply_rules=False)
        mono_service._reconcile_balance(self.acc, 150000)  # 1500 грн
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.current_balance, Decimal('1500.00'))

    def test_internal_transfer_reconciliation(self):
        other = Account.objects.create(
            company=self.company, name='mono fop', currency='UAH',
            integration=self.conn, external_account_id='acc-2',
            external_kind='fop', is_business=True, iban=IBAN_FOP)
        # ФОП -200 (витрата на ВЛАСНУ картку: counterIban = IBAN black) і картка
        # +200 (дохід) у близький час → внутрішній переказ.
        mono_service.import_statement(
            other, [_item('e1', -20000, t=1714000000, counter_iban=IBAN_BLACK)],
            user=self.user, apply_rules=False)
        mono_service.import_statement(self.acc, [_item('i1', 20000, t=1714000100)],
                                     user=self.user, apply_rules=False)
        matched = mono_service.reconcile_internal_transfers(self.company, user=self.user)
        self.assertEqual(matched, 1)
        expense = Transaction.objects.get(external_id='mono:e1')
        self.assertEqual(expense.type, Transaction.TYPE_TRANSFER)
        self.assertEqual(expense.to_account, self.acc)
        self.assertFalse(expense.is_business)
        self.assertFalse(Transaction.objects.filter(external_id='mono:i1').exists())

    def test_transfer_to_foreign_card_stays_expense(self):
        """Переказ на ЧУЖУ картку (counterIban не власний) лишається витратою."""
        other = Account.objects.create(
            company=self.company, name='mono fop', currency='UAH',
            integration=self.conn, external_account_id='acc-2',
            external_kind='fop', is_business=True, iban=IBAN_FOP)
        # Витрата на чужу картку + випадковий дохід на ФОП у той самий час/суму.
        mono_service.import_statement(
            self.acc, [_item('e1', -20000, t=1714000000, counter_iban=IBAN_FOREIGN,
                             counter_name='414960****7321')],
            user=self.user, apply_rules=False)
        mono_service.import_statement(other, [_item('i1', 20000, t=1714000100)],
                                     user=self.user, apply_rules=False)
        matched = mono_service.reconcile_internal_transfers(self.company, user=self.user)
        self.assertEqual(matched, 0)  # НЕ зведено
        expense = Transaction.objects.get(external_id='mono:e1')
        self.assertEqual(expense.type, Transaction.TYPE_EXPENSE)  # лишилась витратою
        # Випадковий дохід НЕ видалено.
        self.assertTrue(Transaction.objects.filter(external_id='mono:i1').exists())

    def test_consumed_internal_transfer_income_is_not_reimported(self):
        other = Account.objects.create(
            company=self.company, name='mono fop', currency='UAH',
            integration=self.conn, external_account_id='acc-2',
            external_kind='fop', is_business=True, iban=IBAN_FOP)
        expense_item = _item('e1', -100000, t=1716921500, counter_iban=IBAN_BLACK)
        income_item = _item('i1', 100000, t=1716921500, counter_iban=IBAN_FOP)

        mono_service.import_statement(other, [expense_item], user=self.user, apply_rules=False)
        mono_service.import_statement(self.acc, [income_item], user=self.user, apply_rules=False)
        mono_service.reconcile_internal_transfers(self.company, user=self.user)

        res = mono_service.import_statement(self.acc, [income_item], user=self.user, apply_rules=False)

        self.assertEqual(res['created'], 0)
        self.assertEqual(res['skipped'], 1)
        self.assertFalse(Transaction.objects.filter(external_id='mono:i1').exists())
        transfer = Transaction.objects.get(external_id='mono:e1')
        self.assertIn('mono:i1', transfer.external_data.get('consumed_external_ids', []))

    def test_reconcile_removes_income_duplicate_for_existing_transfer(self):
        from finance.services import transactions as txn_service

        other = Account.objects.create(
            company=self.company, name='mono fop', currency='UAH',
            integration=self.conn, external_account_id='acc-2',
            external_kind='fop', is_business=True, iban=IBAN_FOP)
        transfer = txn_service.create_transaction(
            user=self.user, type=Transaction.TYPE_TRANSFER, amount=Decimal('1000'),
            account=other, to_account=self.acc, to_amount=Decimal('1000'),
            source='integration', external_id='mono:e1',
            date_actual=dt.datetime.fromtimestamp(1717095780, tz=dt.timezone.utc),
        )
        txn_service.create_transaction(
            user=self.user, type=Transaction.TYPE_INCOME, amount=Decimal('1000'),
            account=self.acc, source='integration', external_id='mono:i1',
            date_actual=dt.datetime.fromtimestamp(1717095780, tz=dt.timezone.utc),
            external_data={'counter_iban': IBAN_FOP},
        )

        matched = mono_service.reconcile_internal_transfers(self.company, user=self.user)

        self.assertEqual(matched, 1)
        self.assertFalse(Transaction.objects.filter(external_id='mono:i1').exists())
        transfer.refresh_from_db()
        self.assertEqual(transfer.type, Transaction.TYPE_TRANSFER)
        self.assertIn('mono:i1', transfer.external_data.get('consumed_external_ids', []))

    def test_income_without_iban_absorbed_into_existing_transfer(self):
        """РЕГРЕСІЯ (скрін: дубль перекладу): переказ ФОП→black створено першим,
        а вхідний дохід на black прийшов пізнішим cron-прогоном БЕЗ counterIban
        (типовий P2P на особисту картку). Має поглинутись у переказ, а не
        створити зелений дохід-дубль."""
        from finance.services import transactions as txn_service

        fop = Account.objects.create(
            company=self.company, name='mono fop', currency='UAH',
            integration=self.conn, external_account_id='acc-2',
            external_kind='fop', is_business=True, iban=IBAN_FOP)
        # 1) Переказ уже зведено раніше (income тоді ще не імпортувався) — тож
        #    matched_income_external_id відсутній.
        txn_service.create_transaction(
            user=self.user, type=Transaction.TYPE_TRANSFER, amount=Decimal('1000'),
            account=fop, to_account=self.acc, to_amount=Decimal('1000'),
            source='integration', external_id='mono:e1',
            date_actual=dt.datetime.fromtimestamp(1717095780, tz=dt.timezone.utc),
        )
        # 2) Пізніше cron тягне вхідний item на black БЕЗ counterIban/counterName.
        income_item = _item('i1', 100000, t=1717095790)  # 1000 грн, без реквізитів
        res = mono_service.import_statement(self.acc, [income_item],
                                            user=self.user, apply_rules=False)

        # Дохід НЕ створено — поглинуто у наявний переказ.
        self.assertEqual(res['created'], 0)
        self.assertEqual(res['skipped'], 1)
        self.assertFalse(Transaction.objects.filter(external_id='mono:i1').exists())
        transfer = Transaction.objects.get(external_id='mono:e1')
        self.assertEqual(transfer.type, Transaction.TYPE_TRANSFER)
        self.assertIn('mono:i1', transfer.external_data.get('consumed_external_ids', []))

    def test_two_distinct_incomes_not_both_absorbed_by_one_transfer(self):
        """Один переказ поглинає РІВНО один дохід. Другий легітимний дохід тієї
        ж суми лишається доходом (захист від помилкового видалення)."""
        from finance.services import transactions as txn_service

        fop = Account.objects.create(
            company=self.company, name='mono fop', currency='UAH',
            integration=self.conn, external_account_id='acc-2',
            external_kind='fop', is_business=True, iban=IBAN_FOP)
        txn_service.create_transaction(
            user=self.user, type=Transaction.TYPE_TRANSFER, amount=Decimal('1000'),
            account=fop, to_account=self.acc, to_amount=Decimal('1000'),
            source='integration', external_id='mono:e1',
            date_actual=dt.datetime.fromtimestamp(1717095780, tz=dt.timezone.utc),
        )
        # Перший дохід → поглинається.
        mono_service.import_statement(
            self.acc, [_item('i1', 100000, t=1717095790)],
            user=self.user, apply_rules=False)
        # Другий дохід тієї ж суми (інший item) → НЕ поглинається тим же переказом.
        res = mono_service.import_statement(
            self.acc, [_item('i2', 100000, t=1717096000)],
            user=self.user, apply_rules=False)
        self.assertEqual(res['created'], 1)
        self.assertTrue(Transaction.objects.filter(external_id='mono:i2').exists())

    def test_webhook_reconciles_internal_transfer_pair(self):
        Account.objects.create(
            company=self.company, name='mono fop', currency='UAH',
            integration=self.conn, external_account_id='acc-2',
            external_kind='fop', is_business=True, iban=IBAN_FOP)
        mono_service.process_webhook(
            self.conn,
            {'type': 'StatementItem',
             'data': {'account': 'acc-2',
                      'statementItem': _item('e1', -100000, t=1716921500,
                                             counter_iban=IBAN_BLACK)}},
            user=self.user,
        )
        mono_service.process_webhook(
            self.conn,
            {'type': 'StatementItem',
             'data': {'account': 'acc-1',
                      'statementItem': _item('i1', 100000, t=1716921500,
                                             counter_iban=IBAN_FOP)}},
            user=self.user,
        )

        transfer = Transaction.objects.get(external_id='mono:e1')
        self.assertEqual(transfer.type, Transaction.TYPE_TRANSFER)
        self.assertEqual(transfer.to_account, self.acc)
        self.assertFalse(Transaction.objects.filter(external_id='mono:i1').exists())

    def test_process_webhook_creates_txn(self):
        payload = {'type': 'StatementItem',
                   'data': {'account': 'acc-1',
                            'statementItem': _item('w1', 33000, balance=33000)}}
        res = mono_service.process_webhook(self.conn, payload, user=self.user)
        self.assertTrue(res['ok'])
        self.assertEqual(Transaction.objects.filter(external_id='mono:w1').count(), 1)

    def test_sync_account_pulls_statement_and_sets_balance(self):
        # Мокаємо statement (одна операція) і accounts (баланс) — без мережі.
        import datetime as _dt
        ma = mono_api.MonoAccount('acc-1', 's', 99900, 0, 'black', 980, ['1234'], 'UA1')
        with mock.patch.object(mono_api.MonoClient, 'statement',
                               return_value=[_item('s1', 12300)]) as m_stmt, \
             mock.patch.object(mono_api.MonoClient, 'accounts', return_value=[ma]):
            res = mono_service.sync_account(self.acc, user=self.user,
                                            apply_rules=False, max_windows=1)
        self.assertTrue(m_stmt.called)
        self.assertGreaterEqual(res['created'], 1)
        self.acc.refresh_from_db()
        # Баланс зведено до значення банку (999.00), а не лише до руху операцій.
        self.assertEqual(self.acc.current_balance, Decimal('999.00'))
        # Курсор backfill збережено в meta для наступного прогону.
        self.conn.refresh_from_db()
        self.assertTrue(any(k.startswith('backfill_until_') for k in self.conn.meta))

    def test_sync_account_rate_limit_soft_stop(self):
        with mock.patch.object(mono_api.MonoClient, 'statement',
                               side_effect=mono_api.MonoRateLimitError('429')), \
             mock.patch.object(mono_api.MonoClient, 'accounts', return_value=[]):
            res = mono_service.sync_account(self.acc, user=self.user,
                                            apply_rules=False, max_windows=2)
        self.assertTrue(res['rate_limited'])
        # last_sync_at не оновлюється при rate-limit (щоб добрати наступного разу).



class MonoConnectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('fin_c', 'c@x.com', 'x')
        self.company = get_default_company()

    def test_connect_with_token_validates_and_encrypts(self):
        # connect_with_token робить реальний client.client_info() — мокаємо його.
        fake = {'name': 'Іван', 'clientId': 'cid', 'accounts': [], 'jars': []}
        with mock.patch.object(mono_api.MonoClient, 'client_info', return_value=fake):
            conn = mono_service.connect_with_token('RAWTOKEN1234567', user=self.user)
        self.assertEqual(conn.status, 'success')
        self.assertEqual(conn.client_name, 'Іван')
        self.assertEqual(conn.external_client_id, 'cid')
        self.assertTrue(conn.encrypted_token)
        self.assertNotIn('RAWTOKEN', conn.encrypted_token)
        self.assertEqual(conn.get_token(), 'RAWTOKEN1234567')
        self.assertTrue(conn.webhook_secret)
        # client-info кешується в meta для подальших discover/link без зайвих запитів.
        self.assertIn('client_info', conn.meta)


class MonoWebhookSecurityTests(TestCase):
    FIN_HOST = 'fin.twocomms.shop'

    def setUp(self):
        self.company = get_default_company()
        self.conn = IntegrationConnection.objects.create(
            company=self.company, provider='monobank', status='success',
            webhook_secret='goodsecret')
        self.client = Client()

    def test_webhook_rejects_bad_secret(self):
        from django.test import override_settings
        url = f'/hooks/mono/{self.conn.id}/wrongsecret/'
        with override_settings(ALLOWED_HOSTS=['fin.twocomms.shop', 'testserver']):
            resp = self.client.post(url, data='{}', content_type='application/json',
                                    HTTP_HOST=self.FIN_HOST)
        self.assertEqual(resp.status_code, 403)

    def test_webhook_validation_get_accepts_good_secret(self):
        from django.test import override_settings
        url = f'/hooks/mono/{self.conn.id}/goodsecret/'
        with override_settings(ALLOWED_HOSTS=['fin.twocomms.shop', 'testserver']):
            resp = self.client.get(url, HTTP_HOST=self.FIN_HOST)
        self.assertEqual(resp.status_code, 200)

    def test_webhook_accepts_good_secret(self):
        from django.test import override_settings
        url = f'/hooks/mono/{self.conn.id}/goodsecret/'
        with override_settings(ALLOWED_HOSTS=['fin.twocomms.shop', 'testserver']):
            resp = self.client.post(url, data='{"data":{}}', content_type='application/json',
                                    HTTP_HOST=self.FIN_HOST)
        self.assertEqual(resp.status_code, 200)


class MonoCronCommandTests(TestCase):
    def setUp(self):
        self.company = get_default_company()
        self.conn = IntegrationConnection.objects.create(
            company=self.company, provider='monobank', status='success',
            connection_method='token')
        self.conn.set_token('TESTTOKEN1234567890')
        self.conn.save()
        self.acc_1 = Account.objects.create(
            company=self.company, name='mono one', currency='UAH',
            integration=self.conn, external_account_id='acc-1',
            external_kind='card', is_business=False)
        self.acc_2 = Account.objects.create(
            company=self.company, name='mono two', currency='UAH',
            integration=self.conn, external_account_id='acc-2',
            external_kind='card', is_business=False)

    def test_default_cron_sync_rotates_one_account_without_client_info(self):
        out = StringIO()
        with mock.patch.object(mono_api.MonoClient, 'client_info',
                               side_effect=AssertionError('client-info should not be called')), \
             mock.patch.object(mono_api.MonoClient, 'accounts',
                               side_effect=AssertionError('accounts should not be called')), \
             mock.patch.object(mono_api.MonoClient, 'statement', return_value=[]) as m_stmt:
            call_command('finance_mono_sync', stdout=out)
            self.assertEqual(m_stmt.call_count, 1)
            self.assertEqual(m_stmt.call_args.args[0], 'acc-1')
            self.conn.refresh_from_db()
            self.assertEqual(self.conn.meta.get('mono_sync_next_account_index'), 1)

            call_command('finance_mono_sync', stdout=out)
            self.assertEqual(m_stmt.call_count, 2)
            self.assertEqual(m_stmt.call_args.args[0], 'acc-2')


class BusinessScopeAndMccFilterTests(TestCase):
    def setUp(self):
        from finance.services import transactions as txn_service
        self.user = User.objects.create_superuser('fin_f', 'f@x.com', 'x')
        self.company = get_default_company()
        self.acc = Account.objects.create(company=self.company, name='card', currency='UAH')
        self.t_biz = txn_service.create_transaction(
            user=self.user, type=Transaction.TYPE_EXPENSE, amount=Decimal('100'),
            account=self.acc, is_business=True, mcc=5411)  # продукти
        self.t_pers = txn_service.create_transaction(
            user=self.user, type=Transaction.TYPE_EXPENSE, amount=Decimal('50'),
            account=self.acc, is_business=False, mcc=5541)  # паливо

    def test_scope_filter(self):
        from finance.services import filters
        biz = filters.filter_transactions(self.company, {'scope': 'business'})
        self.assertIn(self.t_biz, biz)
        self.assertNotIn(self.t_pers, biz)
        pers = filters.filter_transactions(self.company, {'scope': 'personal'})
        self.assertIn(self.t_pers, pers)
        self.assertNotIn(self.t_biz, pers)

    def test_mcc_group_filter(self):
        from finance.services import filters
        groceries = filters.filter_transactions(self.company, {'mcc_group': 'groceries'})
        self.assertIn(self.t_biz, groceries)
        self.assertNotIn(self.t_pers, groceries)

    def test_bulk_set_business_zero_is_false(self):
        # Регресія: bool('0') == True; перевіряємо, що '0' робить is_business=False.
        from finance.views.payments import transactions_bulk_api
        from django.test import RequestFactory
        import json as _json
        rf = RequestFactory()
        req = rf.post('/api/transactions/bulk/',
                      data=_json.dumps({'action': 'set_business',
                                        'ids': f'{self.t_biz.id}', 'value': '0'}),
                      content_type='application/json')
        req.user = self.user
        resp = transactions_bulk_api(req)
        self.assertEqual(resp.status_code, 200)
        self.t_biz.refresh_from_db()
        self.assertFalse(self.t_biz.is_business)
