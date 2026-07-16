"""
W1-3 / W1-9 / W1-12 — безопасность вебхуков Monobank.

Проверяет:
- отклонение webhook без/с невалидной подписью X-Sign (400, статус не меняется)
- реальную ECDSA-проверку подписи (ключ приходит base64-PEM)
- pull-verify: paid ТОЛЬКО по подтверждению invoice/status API
- сверку суммы: недоплата -> checking, НЕ paid
- monobank_return без pull-подтверждения не переводит заказ в paid
- дропшип-вебхук: те же требования подписи
"""

import base64
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from orders.models import Order


def _make_ec_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    # Monobank отдаёт ключ base64-кодированным PEM
    return private_key, base64.b64encode(pem).decode()


def _sign(private_key, body: bytes) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    signature = private_key.sign(body, ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode()


class MonobankWebhookSecurityTests(TestCase):
    def setUp(self):
        self.order = Order.objects.create(
            full_name='Webhook Buyer',
            phone='+380501112233',
            city='Київ',
            np_office='Відділення №1',
            pay_type='online_full',
            status='new',
            payment_status='unpaid',
            total_sum=Decimal('260'),
            payment_invoice_id='inv-test-123',
        )
        self.webhook_url = reverse('monobank_webhook')
        self.payload = json.dumps({'invoiceId': 'inv-test-123', 'status': 'success'}).encode()

    def post_webhook(self, body=None, x_sign=None):
        headers = {}
        if x_sign is not None:
            headers['HTTP_X_SIGN'] = x_sign
        return self.client.post(
            self.webhook_url,
            data=body if body is not None else self.payload,
            content_type='application/json',
            secure=True,
            **headers,
        )

    def test_webhook_without_signature_returns_400(self):
        response = self.post_webhook()

        self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'unpaid')

    def test_webhook_with_wrong_key_signature_returns_400(self):
        attacker_key, _ = _make_ec_keypair()
        _, merchant_pub_b64 = _make_ec_keypair()

        with patch(
            'storefront.views.monobank._get_monobank_public_key',
            return_value=merchant_pub_b64,
        ):
            response = self.post_webhook(x_sign=_sign(attacker_key, self.payload))

        self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'unpaid')

    def test_webhook_valid_signature_and_pull_success_marks_paid(self):
        private_key, pub_b64 = _make_ec_keypair()

        with patch(
            'storefront.views.monobank._get_monobank_public_key',
            return_value=pub_b64,
        ), patch(
            'storefront.views.monobank._monobank_api_request',
            return_value={'status': 'success', 'amount': 26000, 'paidAmount': 26000},
        ):
            response = self.post_webhook(x_sign=_sign(private_key, self.payload))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'paid')

    def test_webhook_underpaid_amount_goes_to_checking_not_paid(self):
        """W1-12: частичная оплата НЕ должна помечать заказ полностью оплаченным."""
        private_key, pub_b64 = _make_ec_keypair()

        with patch(
            'storefront.views.monobank._get_monobank_public_key',
            return_value=pub_b64,
        ), patch(
            'storefront.views.monobank._monobank_api_request',
            return_value={'status': 'success', 'amount': 26000, 'paidAmount': 100},
        ):
            response = self.post_webhook(x_sign=_sign(private_key, self.payload))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'checking')

    def test_webhook_duplicate_delivery_is_idempotent(self):
        """
        W0-4б (CB-024): повторная доставка того же success-вебхука не должна
        повторно диспатчить side-effects (purchase-событие, Telegram) —
        ретейл-путь `_apply_monobank_status` идемпотентен через
        `payment_status != old_payment_status`.
        """
        from storefront.models import UserAction

        private_key, pub_b64 = _make_ec_keypair()

        with patch(
            'storefront.views.monobank._get_monobank_public_key',
            return_value=pub_b64,
        ), patch(
            'storefront.views.monobank._monobank_api_request',
            return_value={'status': 'success', 'amount': 26000, 'paidAmount': 26000},
        ), patch(
            'storefront.views.monobank._dispatch_post_payment_events'
        ) as mock_dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                first = self.post_webhook(x_sign=_sign(private_key, self.payload))
            self.assertEqual(first.status_code, 200)
            self.assertEqual(mock_dispatch.call_count, 1)

            # Повторная доставка того же вебхука
            with self.captureOnCommitCallbacks(execute=True):
                second = self.post_webhook(x_sign=_sign(private_key, self.payload))
            self.assertEqual(second.status_code, 200)
            # Статус уже paid → post-payment dispatcher не запускається вдруге.
            self.assertEqual(mock_dispatch.call_count, 1)

        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'paid')
        # Purchase-событие записано максимум один раз
        self.assertLessEqual(
            UserAction.objects.filter(action_type='purchase', order_id=self.order.id).count(),
            1,
        )

    def test_webhook_body_status_ignored_when_pull_says_failure(self):
        """Body говорит success, но pull-истина failure — верим pull."""
        private_key, pub_b64 = _make_ec_keypair()

        with patch(
            'storefront.views.monobank._get_monobank_public_key',
            return_value=pub_b64,
        ), patch(
            'storefront.views.monobank._monobank_api_request',
            return_value={'status': 'failure'},
        ):
            response = self.post_webhook(x_sign=_sign(private_key, self.payload))

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'unpaid')


class MonobankStatusPersistenceTests(TestCase):
    def setUp(self):
        self.order = Order.objects.create(
            full_name='Persistence Buyer',
            phone='+380501112233',
            city='Київ',
            np_office='Відділення №1',
            pay_type='online_full',
            status='new',
            payment_status='paid',
            total_sum=Decimal('260'),
            payment_invoice_id='inv-persist-1',
        )

    def test_duplicate_success_save_error_does_not_fallback_to_full_save(self):
        from storefront.views.utils import _record_monobank_status_locked

        with patch.object(self.order, 'save', side_effect=RuntimeError('db down')) as save_mock:
            with self.assertRaises(RuntimeError):
                _record_monobank_status_locked(
                    self.order,
                    {'status': 'success', 'invoiceId': 'inv-persist-1'},
                    source='test',
                )

        self.assertEqual(save_mock.call_count, 1)
        self.assertEqual(save_mock.call_args.kwargs, {'update_fields': ['payment_payload']})

    def test_status_transition_save_error_does_not_fallback_to_full_save(self):
        from storefront.views.utils import _record_monobank_status_locked

        self.order.payment_status = 'unpaid'

        with patch.object(self.order, 'save', side_effect=RuntimeError('db down')) as save_mock:
            with self.assertRaises(RuntimeError):
                _record_monobank_status_locked(
                    self.order,
                    {'status': 'success', 'invoiceId': 'inv-persist-1'},
                    source='test',
                )

        self.assertEqual(save_mock.call_count, 1)
        self.assertEqual(
            save_mock.call_args.kwargs,
            {'update_fields': ['payment_payload', 'payment_status']},
        )

    def test_pending_status_save_error_does_not_fallback_to_full_save(self):
        from storefront.views.utils import _record_monobank_status_locked

        with patch.object(self.order, 'save', side_effect=RuntimeError('db down')) as save_mock:
            with self.assertRaises(RuntimeError):
                _record_monobank_status_locked(
                    self.order,
                    {'status': 'processing', 'invoiceId': 'inv-persist-1'},
                    source='test',
                )

        self.assertEqual(save_mock.call_count, 1)
        self.assertEqual(
            save_mock.call_args.kwargs,
            {'update_fields': ['payment_payload', 'payment_status']},
        )


class MonobankReturnSecurityTests(TestCase):
    def setUp(self):
        self.order = Order.objects.create(
            full_name='Return Buyer',
            phone='+380501112233',
            city='Київ',
            np_office='Відділення №1',
            pay_type='online_full',
            status='new',
            payment_status='unpaid',
            total_sum=Decimal('260'),
            payment_invoice_id='inv-return-1',
        )
        self.return_url = reverse('monobank_return')

    def test_return_without_pull_confirmation_does_not_mark_paid(self):
        """W1-3: убран unsafe fallback `or 'success'` — редирект без
        подтверждения API не переводит заказ в paid."""
        from storefront.views.monobank import MonobankAPIError

        session = self.client.session
        session['monobank_invoice_id'] = 'inv-return-1'
        session['monobank_pending_order_id'] = self.order.id
        session.save()

        with patch(
            'storefront.views.monobank._monobank_api_request',
            side_effect=MonobankAPIError('API unavailable'),
        ):
            response = self.client.get(
                self.return_url, {'invoiceId': 'inv-return-1'}, secure=True
            )

        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'unpaid')

    def test_return_with_pull_success_marks_paid(self):
        session = self.client.session
        session['monobank_invoice_id'] = 'inv-return-1'
        session['monobank_pending_order_id'] = self.order.id
        session.save()

        with patch(
            'storefront.views.monobank._monobank_api_request',
            return_value={'status': 'success', 'amount': 26000, 'paidAmount': 26000},
        ):
            response = self.client.get(
                self.return_url, {'invoiceId': 'inv-return-1'}, secure=True
            )

        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'paid')


class DropshipperMonobankCallbackSecurityTests(TestCase):
    def test_callback_without_signature_returns_400(self):
        """W1-9 (NEW-501): дропшип-вебхук без X-Sign отклоняется."""
        response = self.client.post(
            '/orders/dropshipper/monobank/callback/',
            data=json.dumps({'invoiceId': 'ds-inv-1', 'status': 'success'}),
            content_type='application/json',
            secure=True,
        )

        self.assertEqual(response.status_code, 400)

    def test_callback_with_invalid_signature_returns_400(self):
        attacker_key, _ = _make_ec_keypair()
        _, merchant_pub_b64 = _make_ec_keypair()
        body = json.dumps({'invoiceId': 'ds-inv-1', 'status': 'success'}).encode()

        with patch(
            'storefront.views.monobank._get_monobank_public_key',
            return_value=merchant_pub_b64,
        ):
            response = self.client.post(
                '/orders/dropshipper/monobank/callback/',
                data=body,
                content_type='application/json',
                secure=True,
                HTTP_X_SIGN=_sign(attacker_key, body),
            )

        self.assertEqual(response.status_code, 400)


class PostPaymentEventsDeferralTests(TestCase):
    """W2-7 (AN-011/DB-009): внешние отправки — ПОСЛЕ commit, вне row-lock."""

    def setUp(self):
        self.order = Order.objects.create(
            full_name='Deferred Buyer',
            phone='+380501112244',
            city='Київ',
            np_office='Відділення №1',
            pay_type='online_full',
            status='new',
            payment_status='unpaid',
            total_sum=Decimal('260'),
            payment_invoice_id='inv-defer-1',
        )

    def test_external_sends_deferred_until_commit(self):
        from storefront.views.utils import _record_monobank_status

        with patch('storefront.views.utils._send_post_payment_events') as mock_send:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                _record_monobank_status(
                    self.order, {'status': 'success'}, source='webhook'
                )
            # Внутри транзакции внешние отправки НЕ выполнялись
            mock_send.assert_not_called()
            # ...но callback зарегистрирован и выполняется после commit
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            mock_send.assert_called_once()
            args = mock_send.call_args[0]
            self.assertEqual(args[0], self.order.pk)

        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'paid')

    def test_no_dispatch_when_status_unchanged(self):
        from storefront.views.utils import _record_monobank_status
        from storefront.models import UserAction

        self.order.payment_status = 'paid'
        self.order.save(update_fields=['payment_status'])

        with patch('storefront.views.utils._send_post_payment_events') as mock_send:
            with self.captureOnCommitCallbacks(execute=True):
                _record_monobank_status(
                    self.order, {'status': 'success'}, source='webhook'
                )
            mock_send.assert_not_called()
        self.assertEqual(
            UserAction.objects.filter(action_type='purchase', order_id=self.order.pk).count(),
            1,
        )

    def test_retail_duplicate_success_heals_missing_purchase_without_dispatch(self):
        from storefront.models import UserAction
        from storefront.views.monobank import _apply_monobank_status

        self.order.payment_status = 'paid'
        self.order.save(update_fields=['payment_status'])

        with patch(
            'storefront.views.monobank._dispatch_post_payment_events',
        ) as mock_dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                _apply_monobank_status(
                    self.order,
                    'success',
                    payload={'status': 'success'},
                    source='webhook',
                )

        mock_dispatch.assert_not_called()
        self.assertEqual(
            UserAction.objects.filter(action_type='purchase', order_id=self.order.pk).count(),
            1,
        )

    def test_retail_status_helper_uses_shared_dispatcher_after_commit_once(self):
        from storefront.views.monobank import _apply_monobank_status

        with patch(
            'storefront.views.monobank._dispatch_post_payment_events',
        ) as mock_dispatch:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                _apply_monobank_status(
                    self.order,
                    'success',
                    payload={'status': 'success'},
                    source='webhook',
                )

            mock_dispatch.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            mock_dispatch.assert_called_once_with(
                self.order.pk,
                'unpaid',
                'online_full',
            )

            with self.captureOnCommitCallbacks(execute=False) as duplicate_callbacks:
                _apply_monobank_status(
                    self.order,
                    'success',
                    payload={'status': 'success'},
                    source='webhook',
                )
            self.assertEqual(duplicate_callbacks, [])

    def test_shared_dispatcher_sends_idempotent_receipt_outside_status_lock(self):
        from types import SimpleNamespace
        from storefront.views.utils import _send_post_payment_events

        self.order.email = 'buyer@example.com'
        self.order.payment_status = 'paid'
        self.order.save(update_fields=['email', 'payment_status'])

        with patch(
            'orders.telegram_notifications.TelegramNotifier'
        ), patch(
            'orders.facebook_conversions_service.get_facebook_conversions_service',
            return_value=SimpleNamespace(enabled=False),
        ), patch(
            'orders.tiktok_events_service.get_tiktok_events_service',
            return_value=SimpleNamespace(enabled=False),
        ), patch(
            'orders.email_receipt.send_order_receipt_email'
        ) as mock_receipt:
            _send_post_payment_events(
                self.order.pk,
                'unpaid',
                'online_full',
            )

        mock_receipt.assert_called_once()
        self.assertEqual(mock_receipt.call_args.args[0].pk, self.order.pk)

    def _send_with_telegram_result(self, delivered):
        from types import SimpleNamespace
        from storefront.views.utils import _send_post_payment_events

        self.order.email = 'buyer@example.com'
        self.order.payment_status = 'paid'
        self.order.save(update_fields=['email', 'payment_status'])

        with patch(
            'orders.telegram_notifications.TelegramNotifier'
        ) as notifier_class, patch(
            'orders.facebook_conversions_service.get_facebook_conversions_service',
            return_value=SimpleNamespace(enabled=False),
        ), patch(
            'orders.tiktok_events_service.get_tiktok_events_service',
            return_value=SimpleNamespace(enabled=False),
        ), patch(
            'orders.email_receipt.send_order_receipt_email'
        ) as receipt:
            notifier_class.return_value.send_new_order_notification.return_value = delivered
            _send_post_payment_events(
                self.order.pk,
                'unpaid',
                'online_full',
            )

        self.order.refresh_from_db()
        return receipt

    def test_shared_dispatcher_does_not_persist_telegram_flag_after_false(self):
        receipt = self._send_with_telegram_result(False)

        notifications = (self.order.payment_payload or {}).get('telegram_notifications', {})
        self.assertFalse(notifications.get('order_notification_sent', False))
        receipt.assert_called_once()

    def test_shared_dispatcher_persists_telegram_flag_after_true(self):
        receipt = self._send_with_telegram_result(True)

        notifications = (self.order.payment_payload or {}).get('telegram_notifications', {})
        self.assertTrue(notifications.get('order_notification_sent'))
        receipt.assert_called_once()
