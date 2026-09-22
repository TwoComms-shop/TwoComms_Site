"""Тести push-сервісу фінансів (звіти + відправка з моком pywebpush)."""
from __future__ import annotations

from decimal import Decimal
import json
from unittest.mock import patch
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Account, Transaction, get_default_company
from .models_settings import PushSubscription, NotificationLog
from .services import push as push_service

User = get_user_model()


@override_settings(
    WEB_PUSH_ENABLED=True,
    WEB_PUSH_VAPID_PUBLIC_KEY='test-pub',
    WEB_PUSH_VAPID_PRIVATE_KEY='test-priv',
    WEB_PUSH_VAPID_SUBJECT='mailto:test@twocomms.shop',
    SITE_BASE_URL='https://fin.twocomms.shop',
    ALLOWED_HOSTS=['fin.twocomms.shop', 'testserver'],
)
class PushServiceTests(TestCase):
    def setUp(self):
        self.company = get_default_company()
        self.user = User.objects.create_superuser('push_admin', 'a@a.com', 'pass12345')
        self.acc = Account.objects.create(
            company=self.company, name='ФОП', currency='UAH', is_business=True)
        Transaction.objects.create(
            company=self.company, type=Transaction.TYPE_INCOME,
            status=Transaction.STATUS_ACTUAL, amount=Decimal('1500'),
            amount_base=Decimal('1500'), currency='UAH', account=self.acc,
            date_actual=timezone.now(), is_business=True)
        self.sub = PushSubscription.objects.create(
            user=self.user, endpoint='https://push.example/abc',
            p256dh='p256dh-key', auth='auth-key', is_active=True)

    def test_daily_report_content(self):
        r = push_service.build_daily_report(self.company)
        self.assertIn('title', r)
        self.assertIn('body', r)
        self.assertIn('баланс', r['body'].lower())
        data = r['data']
        self.assertEqual(data['kind'], 'daily')
        for key in ('income', 'expense', 'profit', 'balance', 'debts_unpaid', 'tips'):
            self.assertIn(key, data)

    def test_debts_report_none_without_debts(self):
        # Без планових боргів/інвойсів — звіту немає (не турбуємо).
        self.assertIsNone(push_service.build_debts_report(self.company))

    def test_debts_report_with_unpaid(self):
        Transaction.objects.create(
            company=self.company, type=Transaction.TYPE_EXPENSE,
            status=Transaction.STATUS_PLANNED, amount=Decimal('5000'),
            amount_base=Decimal('5000'), currency='UAH', account=self.acc,
            date_actual=timezone.now() + timezone.timedelta(days=2), is_business=True)
        r = push_service.build_debts_report(self.company)
        self.assertIsNotNone(r)
        self.assertEqual(r['data']['kind'], 'debts')
        self.assertGreaterEqual(r['data']['payable']['count'], 1)

    def test_dedup_keys_stable(self):
        import datetime as dt
        day = dt.date(2026, 6, 8)
        self.assertEqual(push_service.dedup_daily(day), 'daily:2026-06-08')
        self.assertEqual(push_service.dedup_debts(day), 'debts:2026-06-08')
        self.assertTrue(push_service.dedup_weekly(day).startswith('weekly:2026-W'))

    @patch('finance.services.push.webpush')
    def test_send_stores_dedup_key_and_log_id(self, mocked):
        mocked.return_value = SimpleNamespace(status_code=201)
        res = push_service.send_to_user(
            self.user, 'T', 'B', notification_type='daily', dedup_key='daily:2026-06-08')
        self.assertIn('log_id', res)
        log = NotificationLog.objects.get(id=res['log_id'])
        self.assertEqual(log.dedup_key, 'daily:2026-06-08')
        self.assertTrue(log.success)

    @patch('finance.services.push.webpush')
    def test_transaction_push_uses_finance_origin_and_context(self, mocked):
        mocked.return_value = SimpleNamespace(status_code=201)
        result = push_service.send_to_user(
            self.user, 'Класифікація', 'Перевірте операцію',
            url='/payments/?transaction=42', notification_type='custom',
            report_data={'kind': 'classification_review', 'transaction_id': 42,
                         'suggested_kind': 'sale'},
            actions=[{'action': 'open', 'title': 'Відкрити'}],
        )
        payload = json.loads(mocked.call_args.kwargs['data'])
        self.assertEqual(result['log_id'], payload['notification_id'])
        self.assertTrue(payload['url'].startswith('https://fin.twocomms.shop/payments/'))
        self.assertIn('transaction=42', payload['url'])
        self.assertEqual(payload['transaction_id'], 42)
        self.assertEqual(payload['action_urls']['confirm'].split('classification_action=')[1], 'confirm')

    def test_notification_detail_and_ack_api(self):
        from django.test import Client
        log = NotificationLog.objects.create(
            user=self.user, notification_type='daily', title='Звіт', body='тіло',
            success=True, dedup_key='daily:x', report_data={'kind': 'daily', 'income': 1.0})
        c = Client()
        c.force_login(self.user)
        r = c.get(f'/api/notifications/{log.id}/', HTTP_HOST='fin.twocomms.shop', secure=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['report']['kind'], 'daily')
        r2 = c.post(f'/api/notifications/{log.id}/ack/', HTTP_HOST='fin.twocomms.shop', secure=True)
        self.assertEqual(r2.status_code, 200)
        log.refresh_from_db()
        self.assertTrue(log.acknowledged)

    def test_weekly_report_content(self):
        r = push_service.build_weekly_report(self.company)
        self.assertIn('здоров', r['body'].lower())
        self.assertIn('health_score', r['data'])

    def test_planned_reminder_report(self):
        # Без планових — None.
        self.assertIsNone(push_service.build_planned_reminder_report(self.company))
        # З плановим на завтра — є нагадування.
        Transaction.objects.create(
            company=self.company, type=Transaction.TYPE_EXPENSE,
            status=Transaction.STATUS_PLANNED, amount=Decimal('3000'),
            amount_base=Decimal('3000'), currency='UAH', account=self.acc,
            date_actual=timezone.now() + timezone.timedelta(hours=20), is_business=True)
        r = push_service.build_planned_reminder_report(self.company)
        self.assertIsNotNone(r)
        self.assertIn('платеж', r['body'].lower())

    def test_settings_save_load_new_fields(self):
        from django.test import Client
        import json as _json
        c = Client()
        c.force_login(self.user)
        c.post('/api/settings/save/',
               data=_json.dumps({'push_enabled': True, 'push_planned_reminders': True,
                                 'push_large_txn': True, 'push_large_txn_threshold': 25000}),
               content_type='application/json', HTTP_HOST='fin.twocomms.shop', secure=True)
        r = c.get('/api/settings/get/', HTTP_HOST='fin.twocomms.shop', secure=True)
        body = r.json()
        self.assertTrue(body['push_planned_reminders'])
        self.assertTrue(body['push_large_txn'])
        self.assertEqual(body['push_large_txn_threshold'], 25000.0)

    @patch('finance.services.push.webpush')
    def test_send_to_user_success(self, mocked):
        mocked.return_value = SimpleNamespace(status_code=201)
        res = push_service.send_to_user(self.user, 'Тест', 'Тіло', url='/health/')
        self.assertTrue(res['ok'])
        self.assertEqual(res['sent'], 1)
        self.assertTrue(NotificationLog.objects.filter(user=self.user).exists())

    @patch('finance.services.push.webpush')
    def test_send_deactivates_gone_subscription(self, mocked):
        from finance.services.push import WebPushException
        resp = SimpleNamespace(status_code=410)
        mocked.side_effect = WebPushException('gone', response=resp)
        res = push_service.send_to_user(self.user, 'Тест', 'Тіло')
        self.assertEqual(res['failed'], 1)
        self.sub.refresh_from_db()
        self.assertFalse(self.sub.is_active)

    def test_test_push_api(self):
        from django.test import Client
        c = Client()
        c.force_login(self.user)
        with patch('finance.services.push.webpush') as mocked:
            mocked.return_value = SimpleNamespace(status_code=201)
            r = c.post('/api/push/test/', HTTP_HOST='fin.twocomms.shop', secure=True)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['success'])

    @override_settings(WEB_PUSH_ENABLED=False)
    def test_not_configured(self):
        self.assertFalse(push_service.is_configured())
        res = push_service.send_to_user(self.user, 'x', 'y')
        self.assertFalse(res['ok'])
