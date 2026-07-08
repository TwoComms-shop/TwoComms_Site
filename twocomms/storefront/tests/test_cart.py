"""
Regression tests for the current storefront cart contract.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
from storefront.models import Category, Product, ProductFitOption, PromoCode


class CartViewTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.category = Category.objects.create(name="Test Category", slug="test-category")
        self.product = Product.objects.create(
            title="Test Product",
            slug="test-product",
            category=self.category,
            price=100,
            status="published",
        )

    def set_cart(self, *, qty=2, size="M", fit_option_code="", fit_option_label=""):
        session = self.client.session
        key = f"{self.product.id}:{size}:default"
        if fit_option_code:
            key = f"{key}:{fit_option_code}"
        session["cart"] = {
            key: {
                "product_id": self.product.id,
                "qty": qty,
                "size": size,
                "color_variant_id": None,
                "fit_option_code": fit_option_code,
                "fit_option_label": fit_option_label,
            }
        }
        session.save()
        return next(iter(session["cart"].keys()))

    def create_user(self, username="promo-user"):
        user = User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="StrongPass123!",
        )
        user.userprofile.phone = "+380991234567"
        user.userprofile.save(update_fields=["phone"])
        return user


class ViewCartTests(CartViewTestCase):
    def test_view_empty_cart_exposes_empty_context(self):
        response = self.client.get(reverse("cart"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["items"], [])
        self.assertEqual(response.context["subtotal"], Decimal("0.00"))
        self.assertEqual(response.context["total"], Decimal("0.00"))

    def test_view_cart_with_products_renders_session_items(self):
        cart_key = self.set_cart(qty=2)

        response = self.client.get(reverse("cart"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.product.title)
        self.assertEqual(len(response.context["items"]), 1)
        self.assertEqual(response.context["items"][0]["key"], cart_key)
        self.assertEqual(response.context["items"][0]["qty"], 2)
        self.assertEqual(response.context["subtotal"], Decimal("200.00"))


class AddToCartTests(CartViewTestCase):
    def test_add_product_to_cart_returns_current_json_payload(self):
        response = self.client.post(
            reverse("cart_add"),
            {"product_id": self.product.id, "qty": 2, "size": "L"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["total"], 200.0)
        self.assertEqual(payload["item"]["quantity"], 2)
        self.assertEqual(payload["item"]["size"], "L")
        self.assertIn(f"{self.product.id}:L:default", self.client.session["cart"])

    def test_add_same_product_accumulates_quantity(self):
        self.client.post(reverse("cart_add"), {"product_id": self.product.id, "qty": 1, "size": "M"})

        response = self.client.post(
            reverse("cart_add"),
            {"product_id": self.product.id, "qty": 3, "size": "M"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 4)
        self.assertEqual(self.client.session["cart"][f"{self.product.id}:M:default"]["qty"], 4)

    def test_add_tshirt_fit_option_keeps_cart_lines_separate(self):
        self.product.title = "Футболка Test Product"
        self.product.save(update_fields=["title"])
        ProductFitOption.objects.create(
            product=self.product,
            code="classic",
            label="Класичний",
            is_default=True,
            order=0,
        )
        ProductFitOption.objects.create(
            product=self.product,
            code="oversize",
            label="Оверсайз",
            order=1,
        )

        response_classic = self.client.post(
            reverse("cart_add"),
            {"product_id": self.product.id, "qty": 1, "size": "M", "fit_option": "classic"},
        )
        response_oversize = self.client.post(
            reverse("cart_add"),
            {"product_id": self.product.id, "qty": 1, "size": "M", "fit_option": "oversize"},
        )

        self.assertEqual(response_classic.status_code, 200)
        self.assertEqual(response_oversize.status_code, 200)
        cart = self.client.session["cart"]
        self.assertIn(f"{self.product.id}:M:default:classic", cart)
        self.assertIn(f"{self.product.id}:M:default:oversize", cart)
        self.assertEqual(cart[f"{self.product.id}:M:default:classic"]["fit_option_label"], "Класичний")
        self.assertEqual(cart[f"{self.product.id}:M:default:oversize"]["fit_option_label"], "Оверсайз")
        self.assertEqual(response_oversize.json()["item"]["fit_option_label"], "Оверсайз")

    def test_add_nonexistent_product_returns_404(self):
        response = self.client.post(reverse("cart_add"), {"product_id": 99999, "qty": 1})

        self.assertEqual(response.status_code, 404)

    def test_add_to_cart_caps_quantity(self):
        """W1-13 (NEW-508): qty=999999 не должно раздувать корзину."""
        from storefront.views.cart import MAX_CART_ITEM_QTY

        response = self.client.post(
            reverse("cart_add"),
            {"product_id": self.product.id, "qty": 999999, "size": "M"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.session["cart"][f"{self.product.id}:M:default"]["qty"],
            MAX_CART_ITEM_QTY,
        )

    def test_add_to_cart_caps_accumulated_quantity(self):
        """W1-13: cap работает и при многократном добавлении."""
        from storefront.views.cart import MAX_CART_ITEM_QTY

        self.client.post(
            reverse("cart_add"),
            {"product_id": self.product.id, "qty": MAX_CART_ITEM_QTY, "size": "M"},
        )
        self.client.post(
            reverse("cart_add"),
            {"product_id": self.product.id, "qty": 10, "size": "M"},
        )

        self.assertEqual(
            self.client.session["cart"][f"{self.product.id}:M:default"]["qty"],
            MAX_CART_ITEM_QTY,
        )


class UpdateAndRemoveCartTests(CartViewTestCase):
    def test_update_cart_changes_quantity_for_existing_key(self):
        cart_key = self.set_cart(qty=2)

        response = self.client.post(reverse("update_cart"), {"cart_key": cart_key, "qty": 5})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["line_total"], 500.0)
        self.assertEqual(payload["total"], 500.0)
        self.assertEqual(self.client.session["cart"][cart_key]["qty"], 5)

    def test_update_cart_resets_pending_monobank_invoice(self):
        cart_key = self.set_cart(qty=2)
        session = self.client.session
        session["monobank_invoice_id"] = "inv-stale"
        session["monobank_pending_order_id"] = 123
        session.save()

        response = self.client.post(reverse("update_cart"), {"cart_key": cart_key, "qty": 3})

        self.assertEqual(response.status_code, 200)
        session = self.client.session
        self.assertNotIn("monobank_invoice_id", session)
        self.assertNotIn("monobank_pending_order_id", session)

    def test_update_cart_caps_quantity(self):
        """W1-13 (NEW-508): верхний cap в update_cart."""
        from storefront.views.cart import MAX_CART_ITEM_QTY

        cart_key = self.set_cart(qty=2)

        response = self.client.post(
            reverse("update_cart"), {"cart_key": cart_key, "qty": 999999}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["cart"][cart_key]["qty"], MAX_CART_ITEM_QTY)

    def test_update_cart_rejects_missing_key(self):
        self.set_cart()

        response = self.client.post(reverse("update_cart"), {"cart_key": "missing", "qty": 1})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])

    def test_remove_from_cart_deletes_item_by_exact_key(self):
        cart_key = self.set_cart()

        response = self.client.post(reverse("cart_remove"), {"key": cart_key})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["removed"], [cart_key])
        self.assertEqual(self.client.session["cart"], {})

    def test_remove_from_cart_keeps_ok_response_for_missing_item(self):
        self.set_cart()

        response = self.client.post(reverse("cart_remove"), {"key": "missing"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["removed"], [])

    def test_remove_from_cart_does_not_delete_variants_when_exact_key_is_stale(self):
        current_key = self.set_cart(size="M")
        response = self.client.post(reverse("cart_remove"), {"key": f"{self.product.id}:L:default"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["removed"], [])
        self.assertIn(current_key, self.client.session["cart"])

    def test_remove_from_cart_resets_pending_monobank_invoice_when_removed(self):
        cart_key = self.set_cart(qty=2)
        session = self.client.session
        session["monobank_invoice_id"] = "inv-stale"
        session["monobank_pending_order_id"] = 123
        session.save()

        response = self.client.post(reverse("cart_remove"), {"key": cart_key})

        self.assertEqual(response.status_code, 200)
        session = self.client.session
        self.assertNotIn("monobank_invoice_id", session)
        self.assertNotIn("monobank_pending_order_id", session)


class CartUtilityEndpointTests(CartViewTestCase):
    def test_clear_cart_ajax_empties_cart_and_promo_session(self):
        self.set_cart()
        session = self.client.session
        session["promo_code_id"] = 1
        session["promo_code_data"] = {"code": "SAVE10"}
        session.save()

        response = self.client.post(
            reverse("clean_cart"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.client.session["cart"], {})
        self.assertNotIn("promo_code_id", self.client.session)
        self.assertNotIn("promo_code_data", self.client.session)

    def test_get_cart_count_sums_current_qty_values(self):
        self.set_cart(qty=3)

        response = self.client.get(reverse("get_cart_count"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cart_count"], 3)

    def test_get_cart_count_includes_custom_print_cart(self):
        self.set_cart(qty=3)
        session = self.client.session
        session[SESSION_CUSTOM_CART_KEY] = {
            "custom:1": {"quantity": 2},
            "custom:2": {"quantity": "bad"},
        }
        session.save()

        response = self.client.get(reverse("get_cart_count"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cart_count"], 6)

    def test_cart_summary_does_not_mutate_session_for_missing_products(self):
        session = self.client.session
        session["cart"] = {
            "999:M:default": {
                "product_id": 999,
                "qty": 1,
                "size": "M",
                "color_variant_id": None,
            }
        }
        session["monobank_invoice_id"] = "inv-stale"
        session["monobank_pending_order_id"] = 123
        session.save()

        response = self.client.get(reverse("cart_summary"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 0)
        self.assertEqual(response.json()["total"], 0.0)
        session = self.client.session
        self.assertIn("999:M:default", session["cart"])
        self.assertEqual(session["monobank_invoice_id"], "inv-stale")
        self.assertEqual(session["monobank_pending_order_id"], 123)

    def test_main_js_guards_global_add_to_cart_double_clicks(self):
        js_path = "twocomms/twocomms_django_theme/static/js/main.js"
        with open(js_path, encoding="utf-8") as handle:
            js = handle.read()

        self.assertIn("btn.dataset.addToCartPending", js)
        self.assertIn("delete btn.dataset.addToCartPending", js)

    def test_cart_items_api_exposes_fit_label(self):
        self.set_cart(qty=1, fit_option_code="classic", fit_option_label="Класичний")

        response = self.client.get(reverse("cart_items_api"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"][0]["fit_option_code"], "classic")
        self.assertEqual(payload["items"][0]["fit_option_label"], "Класичний")


class PromoCodeTests(CartViewTestCase):
    def test_apply_valid_promo_code_for_authenticated_user(self):
        self.set_cart(qty=2)
        user = self.create_user()
        self.client.force_login(user)
        promo = PromoCode.objects.create(
            code="SAVE10",
            discount_type="percentage",
            discount_value=Decimal("10.00"),
            is_active=True,
        )

        response = self.client.post(reverse("apply_promo_code"), {"promo_code": "save10"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["discount"], 20.0)
        self.assertEqual(payload["total"], 180.0)
        self.assertEqual(self.client.session["promo_code_id"], promo.id)

    def test_remove_promo_code_clears_session_and_restores_total(self):
        self.set_cart(qty=2)
        session = self.client.session
        session["promo_code_id"] = 123
        session["promo_code_data"] = {"code": "SAVE10", "discount": 20.0}
        session.save()

        response = self.client.post(reverse("remove_promo_code"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["discount"], 0.0)
        self.assertEqual(payload["total"], 200.0)
        self.assertNotIn("promo_code_id", self.client.session)
        self.assertNotIn("promo_code_data", self.client.session)
