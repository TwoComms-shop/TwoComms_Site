"""Regression tests for abandoned-checkout capture validation."""

from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from orders.models import CheckoutCapture
from storefront.models import Category, Product


class CheckoutCaptureTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client.raise_request_exception = False
        self.url = reverse("checkout_capture")

    def post_json(self, payload, **extra):
        return self.client.post(
            self.url,
            data=payload,
            content_type="application/json",
            **extra,
        )

    def assert_rejected(self, response, error):
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"ok": False, "error": error})

    def set_cart(self, product, *, qty=2):
        session = self.client.session
        cart = {
            f"{product.id}:M:default": {
                "product_id": product.id,
                "qty": qty,
                "size": "M",
                "color_variant_id": None,
            }
        }
        session["cart"] = cart
        session.save()
        return cart

    def create_product(self):
        category = Category.objects.create(
            name="Capture Category",
            slug="capture-category",
        )
        return Product.objects.create(
            title="Capture Product",
            slug="capture-product",
            category=category,
            price=125,
            status="published",
        )

    def test_empty_json_without_cart_rejects_without_persisting_session(self):
        response = self.post_json({})

        self.assert_rejected(response, "contact_required")
        self.assertEqual(CheckoutCapture.objects.count(), 0)
        self.assertEqual(Session.objects.count(), 0)

    def test_empty_json_with_cart_rejects_without_changing_cart(self):
        product = self.create_product()
        cart = self.set_cart(product)
        session_key = self.client.session.session_key

        response = self.post_json({})

        self.assert_rejected(response, "contact_required")
        self.assertEqual(CheckoutCapture.objects.count(), 0)
        self.assertEqual(self.client.session.session_key, session_key)
        self.assertEqual(self.client.session["cart"], cart)

    def test_invalid_contact_rejects_before_malformed_cart_normalization(self):
        session = self.client.session
        session["cart"] = ["malformed-cart"]
        session.save()

        response = self.post_json({"full_name": "Buyer"})

        self.assert_rejected(response, "contact_required")
        self.assertEqual(self.client.session["cart"], ["malformed-cart"])
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_whitespace_only_fields_are_rejected(self):
        response = self.post_json(
            {"full_name": "  ", "phone": "\t", "email": "\n"}
        )

        self.assert_rejected(response, "contact_required")
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_full_name_only_is_rejected(self):
        response = self.post_json({"full_name": "Buyer Without Contact"})

        self.assert_rejected(response, "contact_required")
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_invalid_email_only_is_rejected(self):
        response = self.post_json({"email": "not-an-email"})

        self.assert_rejected(response, "contact_required")
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_invalid_phone_only_json_is_rejected(self):
        response = self.post_json({"phone": "x"})

        self.assert_rejected(response, "contact_required")
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_invalid_phone_only_form_is_rejected(self):
        response = self.client.post(self.url, {"phone": "abc"})

        self.assert_rejected(response, "contact_required")
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_non_string_contact_fields_are_rejected_without_server_error(self):
        response = self.post_json(
            {"full_name": 123, "phone": ["+380501112233"], "email": {}}
        )

        self.assert_rejected(response, "contact_required")
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_malformed_json_is_rejected(self):
        response = self.client.generic(
            "POST",
            self.url,
            data=b'{"phone":',
            content_type="application/json",
        )

        self.assert_rejected(response, "invalid_payload")
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_json_list_is_rejected(self):
        response = self.post_json([{"phone": "+380501112233"}])

        self.assert_rejected(response, "invalid_payload")
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_json_string_is_rejected(self):
        response = self.client.generic(
            "POST",
            self.url,
            data=b'"+380501112233"',
            content_type="application/json",
        )

        self.assert_rejected(response, "invalid_payload")
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_json_null_is_rejected(self):
        response = self.client.generic(
            "POST",
            self.url,
            data=b"null",
            content_type="application/json",
        )

        self.assert_rejected(response, "invalid_payload")
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_invalid_request_does_not_touch_existing_capture(self):
        session = self.client.session
        session["marker"] = "keep"
        session.save()
        notified_at = timezone.now()
        recovery_at = notified_at.replace(microsecond=0)
        capture = CheckoutCapture.objects.create(
            session_key=session.session_key,
            full_name="Existing Buyer",
            phone="+380501111111",
            email="existing@example.com",
            cart_snapshot={"existing": {"product_id": 987, "qty": 3}},
            cart_total=Decimal("456.78"),
            converted=True,
            admin_notified_at=notified_at,
            recovery_sent_at=recovery_at,
        )
        before = CheckoutCapture.objects.filter(pk=capture.pk).values().get()

        response = self.post_json({"full_name": "Rejected Update"})

        self.assert_rejected(response, "contact_required")
        after = CheckoutCapture.objects.filter(pk=capture.pk).values().get()
        self.assertEqual(after, before)

    def test_phone_only_json_creates_capture(self):
        response = self.post_json({"phone": "  +380501112233  "})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        capture = CheckoutCapture.objects.get()
        self.assertEqual(capture.phone, "+380501112233")
        self.assertEqual(capture.email, "")
        self.assertEqual(capture.full_name, "")

    def test_email_only_json_creates_capture(self):
        response = self.post_json({"email": "  buyer@example.com  "})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        capture = CheckoutCapture.objects.get()
        self.assertEqual(capture.email, "buyer@example.com")
        self.assertEqual(capture.phone, "")

    def test_form_encoded_phone_and_name_remain_supported(self):
        response = self.client.post(
            self.url,
            {"phone": "+380671112233", "full_name": "Form Buyer"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        capture = CheckoutCapture.objects.get()
        self.assertEqual(capture.phone, "+380671112233")
        self.assertEqual(capture.full_name, "Form Buyer")

    def test_valid_update_preserves_nonblank_fields_on_active_capture(self):
        session = self.client.session
        session["marker"] = "keep"
        session.save()
        capture = CheckoutCapture.objects.create(
            session_key=session.session_key,
            full_name="Existing Buyer",
            phone="+380501111111",
            email="existing@example.com",
            converted=False,
        )

        response = self.post_json({"phone": "+380672222222"})

        self.assertEqual(response.status_code, 200)
        capture.refresh_from_db()
        self.assertEqual(capture.full_name, "Existing Buyer")
        self.assertEqual(capture.phone, "+380672222222")
        self.assertEqual(capture.email, "existing@example.com")
        self.assertFalse(capture.converted)

    def test_valid_request_does_not_touch_converted_capture(self):
        product = self.create_product()
        self.set_cart(product)
        session = self.client.session
        notified_at = timezone.now()
        recovery_at = notified_at.replace(microsecond=0)
        capture = CheckoutCapture.objects.create(
            session_key=session.session_key,
            full_name="Converted Buyer",
            phone="+380501111111",
            email="converted@example.com",
            cart_snapshot={"original": {"product_id": 987, "qty": 3}},
            cart_total=Decimal("456.78"),
            converted=True,
            admin_notified_at=notified_at,
            recovery_sent_at=recovery_at,
        )
        before = CheckoutCapture.objects.filter(pk=capture.pk).values().get()

        response = self.post_json(
            {
                "full_name": "Late Beacon",
                "phone": "+380672222222",
                "email": "late@example.com",
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        after = CheckoutCapture.objects.filter(pk=capture.pk).values().get()
        self.assertEqual(after, before)

    def test_valid_capture_attaches_validated_cart_and_total(self):
        product = self.create_product()
        cart = self.set_cart(product, qty=2)

        response = self.post_json({"phone": "+380501112233"})

        self.assertEqual(response.status_code, 200)
        capture = CheckoutCapture.objects.get()
        self.assertEqual(capture.cart_snapshot, cart)
        self.assertEqual(capture.cart_total, Decimal("250"))

    def test_authenticated_phone_capture_binds_user_and_fills_user_email(self):
        user = User.objects.create_user(
            username="capture-buyer",
            email="account@example.com",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.post_json({"phone": "+380501112233"})

        self.assertEqual(response.status_code, 200)
        capture = CheckoutCapture.objects.get()
        self.assertEqual(capture.user, user)
        self.assertEqual(capture.email, "account@example.com")

    def test_authenticated_name_capture_uses_valid_account_email(self):
        user = User.objects.create_user(
            username="named-account-buyer",
            email="named@example.com",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.post_json({"full_name": "Named Account Buyer"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        capture = CheckoutCapture.objects.get()
        self.assertEqual(capture.full_name, "Named Account Buyer")
        self.assertEqual(capture.email, "named@example.com")
        self.assertEqual(capture.user, user)

    def test_authenticated_empty_payload_does_not_use_account_email(self):
        user = User.objects.create_user(
            username="empty-account-buyer",
            email="empty@example.com",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.post_json({})

        self.assert_rejected(response, "contact_required")
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_authenticated_name_capture_rejects_invalid_account_email(self):
        user = User.objects.create_user(
            username="invalid-email-account-buyer",
            email="not-an-email",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.post_json({"full_name": "Invalid Email Buyer"})

        self.assert_rejected(response, "contact_required")
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    def test_cross_site_request_remains_forbidden(self):
        response = self.post_json(
            {"phone": "+380501112233"},
            HTTP_SEC_FETCH_SITE="cross-site",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"ok": False})
        self.assertEqual(CheckoutCapture.objects.count(), 0)

    @override_settings(RATELIMIT_ENABLE=True)
    def test_rate_limited_request_remains_too_many_requests(self):
        for _index in range(30):
            response = self.post_json(
                {"phone": "+380501112233"},
                REMOTE_ADDR="192.0.2.51",
            )
            self.assertEqual(response.status_code, 200)

        response = self.post_json(
            {"phone": "+380501112233"},
            REMOTE_ADDR="192.0.2.51",
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"ok": False})
        self.assertEqual(CheckoutCapture.objects.count(), 1)
