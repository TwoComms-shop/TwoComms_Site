"""Task 4 — закріплення товару діалогу (IgClient.current_product).

Мета: коли товар уже визначено ([PRODUCT:id] від моделі або впевнений матчинг),
посилання на оплату формується саме на нього — БЕЗ повторного вгадування моделлю.
Після створення замовлення пін скидається (щоб наступна покупка почалась чисто).
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from management.models import IgClient, IgDeal, IgDealItem
from management.services import bot_orders
from management.services import instagram_bot as bot


def _pub(title, slug, price=950):
    from storefront.models import Category, Product, ProductStatus

    cat, _ = Category.objects.get_or_create(name="Кат", slug="cp-cat")
    return Product.objects.create(
        title=title, slug=slug, category=cat, price=price, status=ProductStatus.PUBLISHED
    )


class CurrentProductResolverTests(TestCase):
    @patch("management.services.bot_orders.gemini_generate_text")
    def test_pinned_product_used_without_model_call(self, mock_gen):
        p = _pub("Худі Pin", "cp-hoodie", 2155)
        c = IgClient.get_or_create_for_sender("cp1")
        c.current_product = p
        c.save(update_fields=["current_product"])
        got = bot_orders.resolve_product_for_payment(c, None)
        self.assertIsNotNone(got)
        self.assertEqual(got.id, p.id)
        mock_gen.assert_not_called()

    def test_explicit_id_beats_pin(self):
        pinned = _pub("Худі Pinned", "cp-pinned", 2155)
        explicit = _pub("Футболка Explicit", "cp-expl", 950)
        c = IgClient.get_or_create_for_sender("cp1b")
        c.current_product = pinned
        c.save(update_fields=["current_product"])
        got = bot_orders.resolve_product_for_payment(c, explicit.id)
        self.assertEqual(got.id, explicit.id)


class PinProductTests(TestCase):
    def test_pin_sets_current_product(self):
        p = _pub("Футболка Pin", "cp-tee", 950)
        c = IgClient.get_or_create_for_sender("cp2")
        bot_orders.pin_product(c, p.id)
        c.refresh_from_db()
        self.assertEqual(c.current_product_id, p.id)

    def test_pin_ignores_unpublished(self):
        from storefront.models import Category, Product, ProductStatus

        cat, _ = Category.objects.get_or_create(name="Кат", slug="cp-cat")
        p = Product.objects.create(
            title="Чернетка", slug="cp-draft", category=cat, price=10, status=ProductStatus.DRAFT
        )
        c = IgClient.get_or_create_for_sender("cp3")
        bot_orders.pin_product(c, p.id)
        c.refresh_from_db()
        self.assertIsNone(c.current_product_id)

    def test_pin_none_is_safe(self):
        c = IgClient.get_or_create_for_sender("cp3b")
        bot_orders.pin_product(c, None)  # не падає
        c.refresh_from_db()
        self.assertIsNone(c.current_product_id)


class ResetCurrentProductOnOrderTests(TestCase):
    def test_order_creation_resets_pin(self):
        from orders.services.order_builder import create_order_from_deal

        p = _pub("Лонгслів Pin", "cp-long", 1650)
        c = IgClient.get_or_create_for_sender("cp4")
        c.current_product = p
        c.save(update_fields=["current_product"])
        deal = IgDeal.objects.create(
            client=c,
            pay_type=IgDeal.PayType.ONLINE_FULL,
            status=IgDeal.Status.PAID,
            payment_status="paid",
            paid_at=timezone.now(),
        )
        IgDealItem.objects.create(
            deal=deal, product=p, title=p.title, qty=1, unit_price=Decimal("1650")
        )
        deal.recalc_total()
        deal.np_full_name = "Іван Тест"
        deal.np_phone = "0501234567"
        deal.np_city = "Київ"
        deal.np_office = "Відділення 1"
        deal.save()
        create_order_from_deal(deal)
        c.refresh_from_db()
        self.assertIsNone(c.current_product_id)


class MaybePinFromMatchTests(TestCase):
    def test_high_confidence_pins(self):
        p = _pub("Худі Матч", "cp-mh", 2155)
        c = IgClient.get_or_create_for_sender("cp5")
        bot._maybe_pin_from_match(c, {"product_id": p.id, "confidence": 0.9})
        c.refresh_from_db()
        self.assertEqual(c.current_product_id, p.id)

    def test_low_confidence_no_pin(self):
        p = _pub("Худі Матч2", "cp-mh2", 2155)
        c = IgClient.get_or_create_for_sender("cp6")
        bot._maybe_pin_from_match(c, {"product_id": p.id, "confidence": 0.2})
        c.refresh_from_db()
        self.assertIsNone(c.current_product_id)
