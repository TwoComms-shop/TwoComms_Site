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
