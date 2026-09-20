"""Tests for notifications emitted by newly imported Monobank income only."""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from .models import Account, IntegrationConnection, Transaction, get_default_company
from .models_settings import UserSettings
from .services import mono, transaction_alerts


User = get_user_model()


def _incoming_item(item_id, amount, description='Переказ'):
    return {
        'id': item_id,
        'time': 1714000000,
        'amount': amount,
        'description': description,
    }


@override_settings(FIN_HOST='fin.twocomms.shop')
class ImportedIncomingAlertTests(TestCase):
    def setUp(self):
        cache.clear()
        self.company = get_default_company()
        self.user = User.objects.create_superuser('alert_admin', 'alert@example.com', 'pass')
        self.connection = IntegrationConnection.objects.create(
            company=self.company, provider='monobank', status='success', connection_method='token',
        )
        self.account = Account.objects.create(
            company=self.company, name='Mono', currency='UAH', integration=self.connection,
            external_account_id='alert-account', external_kind='card',
        )
        UserSettings.objects.create(
            user=self.user, push_enabled=True, telegram_notifications=True,
        )

    @patch('management.services.notify.send_message', return_value=True)
    @patch('management.services.notify.admin_chat_ids', return_value=['12345'])
    @patch('finance.services.push.send_to_user', return_value={'ok': True, 'sent': 1})
    def test_terminal_import_notifies_once_with_review_url(self, push_send, _chat_ids, telegram_send):
        item = _incoming_item(
            'terminal-1', 123450,
            'Поповнення через термінал Monobank City24',
        )

        with self.captureOnCommitCallbacks(execute=True):
            mono.import_statement(self.account, [item], user=self.user, apply_rules=False)
        txn = Transaction.objects.get(external_id='mono:terminal-1')
        transaction_alerts.notify_new_incoming_classification(txn.id)
        with self.captureOnCommitCallbacks(execute=True):
            mono.import_statement(self.account, [item], user=self.user, apply_rules=False)

        self.assertEqual(push_send.call_count, 1)
        self.assertEqual(telegram_send.call_count, 1)
        self.assertIn('термінал Mono/City24', push_send.call_args.args[2])
        self.assertEqual(push_send.call_args.kwargs['url'], f'/payments/?terminal_review={txn.id}')
        keyboard = telegram_send.call_args.kwargs['reply_markup']
        self.assertEqual(
            keyboard['inline_keyboard'][0][0]['url'],
            f'https://fin.twocomms.shop/payments/?terminal_review={txn.id}',
        )

    @patch('finance.services.push.send_to_user', return_value={'ok': True, 'sent': 1})
    def test_regular_unclassified_income_uses_neutral_finance_link(self, push_send):
        UserSettings.objects.filter(user=self.user).update(telegram_notifications=False)

        with self.captureOnCommitCallbacks(execute=True):
            mono.import_statement(
                self.account, [_incoming_item('incoming-1', 50000)],
                user=self.user, apply_rules=False,
            )

        txn = Transaction.objects.get(external_id='mono:incoming-1')
        self.assertEqual(push_send.call_count, 1)
        self.assertEqual(push_send.call_args.kwargs['url'], f'/payments/?transaction={txn.id}')
        self.assertIn('поки враховане як дохід', push_send.call_args.args[2])
