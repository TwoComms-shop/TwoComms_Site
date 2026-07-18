"""
Regression tests for the current storefront cart contract.
"""

from __future__ import annotations

import json
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.translation import override

from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
from storefront.models import Category, Product, ProductFitOption, PromoCode
from storefront.views.utils import MAX_CART_ITEM_QTY as SESSION_MAX_CART_ITEM_QTY
from storefront.views.utils import MAX_CART_ITEMS
from storefront.views.utils import normalize_cart_session


class CartSessionNormalizationTests(SimpleTestCase):
    def test_normalizer_keeps_typed_bounded_rows_and_drops_invalid_ids(self):
        raw_cart = {
            "valid": {
                "product_id": "12",
                "color_variant_id": "34",
                "qty": "999",
                "size": " M ",
            },
            "bad-product": {"product_id": "abc", "qty": 1},
            "bad-variant": {"product_id": 12, "color_variant_id": -5, "qty": 1},
            "bad-shape": "not-a-row",
        }

        cleaned, changed = normalize_cart_session(raw_cart)

        self.assertTrue(changed)
        self.assertEqual(
            cleaned,
            {
                "valid": {
                    "product_id": 12,
                    "color_variant_id": 34,
                    "qty": SESSION_MAX_CART_ITEM_QTY,
                    "size": "M",
                    "option_values": {},
                    "option_labels": {},
                }
            },
        )

    def test_normalizer_rewrites_even_empty_non_dict_sessions(self):
        self.assertEqual(normalize_cart_session([]), ({}, True))
        self.assertEqual(normalize_cart_session(None), ({}, True))

    def test_normalizer_caps_number_of_session_rows(self):
        raw_cart = {
            f"line-{index}": {"product_id": index + 1, "qty": 1}
            for index in range(5)
        }

        cleaned, changed = normalize_cart_session(raw_cart, max_items=2)

        self.assertTrue(changed)
        self.assertEqual(list(cleaned), ["line-0", "line-1"])


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

    def test_cart_and_mini_drop_malformed_rows_without_losing_valid_items(self):
        valid_key = f"{self.product.id}:M:default"
        session = self.client.session
        session["cart"] = {
            valid_key: {
                "product_id": str(self.product.id),
                "qty": "2",
                "size": "M",
                "color_variant_id": None,
            },
            "bad:M:default": {
                "product_id": "abc",
                "qty": "broken",
                "size": "M",
                "color_variant_id": "also-bad",
            },
        }
        session.save()

        cart_response = self.client.get(reverse("cart"))
        mini_response = self.client.get(reverse("cart_mini"))

        self.assertEqual(cart_response.status_code, 200)
        self.assertEqual(mini_response.status_code, 200)
        self.assertContains(cart_response, self.product.title)
        self.assertEqual(list(self.client.session["cart"]), [valid_key])
        self.assertEqual(self.client.session["cart"][valid_key]["product_id"], self.product.id)

    def test_cart_drops_variant_owned_by_another_product(self):
        from productcolors.models import Color, ProductColorVariant

        other_product = Product.objects.create(
            title="Foreign Variant Product",
            slug="foreign-variant-product",
            category=self.category,
            price=200,
            status="published",
        )
        color = Color.objects.create(name="Foreign Session", primary_hex="#654321")
        foreign_variant = ProductColorVariant.objects.create(
            product=other_product,
            color=color,
            is_default=True,
        )
        session = self.client.session
        session["cart"] = {
            f"{self.product.id}:M:{foreign_variant.id}": {
                "product_id": self.product.id,
                "qty": 1,
                "size": "M",
                "color_variant_id": foreign_variant.id,
            }
        }
        session.save()

        response = self.client.get(reverse("cart"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session["cart"], {})
        self.assertEqual(response.context["items"], [])


class AddToCartTests(CartViewTestCase):
    def set_full_cart(self, *, include_target=False):
        session = self.client.session
        cart = {
            f"legacy-{index}": {
                "product_id": self.product.id,
                "qty": 1,
                "size": "M",
                "color_variant_id": None,
            }
            for index in range(MAX_CART_ITEMS - int(include_target))
        }
        if include_target:
            cart[f"{self.product.id}:L:default"] = {
                "product_id": self.product.id,
                "qty": 1,
                "size": "L",
                "color_variant_id": None,
            }
        session["cart"] = cart
        session.save()

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

        def cart_key(fit_code):
            options = json.dumps(
                {"fit": fit_code},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            return f"{self.product.id}:M:default:{fit_code}:{options}"

        classic_key = cart_key("classic")
        oversize_key = cart_key("oversize")
        self.assertEqual(set(cart), {classic_key, oversize_key})
        for key, code, label in (
            (classic_key, "classic", "Класичний"),
            (oversize_key, "oversize", "Оверсайз"),
        ):
            with self.subTest(code=code):
                self.assertEqual(cart[key]["fit_option_code"], code)
                self.assertEqual(cart[key]["fit_option_label"], label)
                self.assertEqual(cart[key]["option_values"], {"fit": code})
                self.assertEqual(cart[key]["option_labels"], {"Посадка": label})
        self.assertEqual(response_oversize.json()["item"]["fit_option_label"], "Оверсайз")

    def test_add_nonexistent_product_returns_404(self):
        response = self.client.post(reverse("cart_add"), {"product_id": 99999, "qty": 1})

        self.assertEqual(response.status_code, 404)

    def test_add_rejects_malformed_product_id_before_orm_lookup(self):
        response = self.client.post(
            reverse("cart_add"),
            {"product_id": "abc", "qty": 1, "size": "M"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    def test_add_rejects_new_line_when_cart_is_at_row_cap(self):
        self.set_full_cart()

        response = self.client.post(
            reverse("cart_add"),
            {"product_id": self.product.id, "qty": 1, "size": "L"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(len(self.client.session["cart"]), MAX_CART_ITEMS)

    def test_add_at_row_cap_still_updates_existing_line(self):
        self.set_full_cart(include_target=True)

        response = self.client.post(
            reverse("cart_add"),
            {"product_id": self.product.id, "qty": 2, "size": "L"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.client.session["cart"]), MAX_CART_ITEMS)
        self.assertEqual(
            self.client.session["cart"][f"{self.product.id}:L:default"]["qty"],
            3,
        )

    def test_add_rejects_color_variant_owned_by_another_product(self):
        from productcolors.models import Color, ProductColorVariant

        other_product = Product.objects.create(
            title="Other Product",
            slug="other-product",
            category=self.category,
            price=200,
            status="published",
        )
        color = Color.objects.create(name="Foreign", primary_hex="#123456")
        foreign_variant = ProductColorVariant.objects.create(
            product=other_product,
            color=color,
            is_default=True,
        )

        response = self.client.post(
            reverse("cart_add"),
            {
                "product_id": self.product.id,
                "color_variant_id": foreign_variant.id,
                "qty": 1,
                "size": "M",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(self.client.session.get("cart", {}), {})

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
    def set_foreign_variant_cart(self):
        from productcolors.models import Color, ProductColorVariant

        suffix = Product.objects.count()
        other_product = Product.objects.create(
            title="Foreign API Product",
            slug=f"foreign-api-product-{suffix}",
            category=self.category,
            price=200,
            status="published",
        )
        color = Color.objects.create(name=f"Foreign API {suffix}", primary_hex="#ABCDEF")
        variant = ProductColorVariant.objects.create(
            product=other_product,
            color=color,
            is_default=True,
        )
        key = f"{self.product.id}:M:{variant.id}"
        session = self.client.session
        session["cart"] = {
            key: {
                "product_id": self.product.id,
                "qty": 2,
                "size": "M",
                "color_variant_id": variant.id,
            }
        }
        session.save()
        return key

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

    def test_cart_page_exposes_localized_clear_endpoint(self):
        self.set_cart()

        for locale in ("uk", "ru", "en"):
            with self.subTest(locale=locale), override(locale):
                response = self.client.get(reverse("cart"))

                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response,
                    f'data-cart-clear-url="{reverse("clean_cart")}"',
                )

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
        js_path = settings.BASE_DIR / "twocomms_django_theme" / "static" / "js" / "main.js"
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

    def test_summary_and_items_api_drop_foreign_variant_rows(self):
        self.set_foreign_variant_cart()

        summary = self.client.get(reverse("cart_summary"))

        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.json()["count"], 0)
        self.assertEqual(self.client.session["cart"], {})

        self.set_foreign_variant_cart()
        items = self.client.get(reverse("cart_items_api"))

        self.assertEqual(items.status_code, 200)
        self.assertEqual(items.json()["items"], [])
        self.assertEqual(self.client.session["cart"], {})

    def test_update_drops_preexisting_foreign_variant_row(self):
        key = self.set_foreign_variant_cart()

        response = self.client.post(reverse("update_cart"), {"cart_key": key, "qty": 3})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.session["cart"], {})


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
