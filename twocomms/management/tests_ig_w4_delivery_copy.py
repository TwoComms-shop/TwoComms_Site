"""Regression tests for W4 customer-facing payment and shipment copy."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.test import SimpleTestCase, TestCase

from management.models import IgClient, IgDeal
from management.services import bot_orders
from management.services import instagram_bot as bot
from management.services.ig_order_fulfillment import _message
from management.services.ig_commercial_episodes import ensure_episode_for_deal
from orders.models import Order, PaymentAttempt


class ShipmentCopyTests(SimpleTestCase):
    def setUp(self):
        self.order = SimpleNamespace(order_number="TWC-W4-01", pk=1)

    def test_tracking_copy_reports_creation_without_inventing_transit_or_shipment(self):
        expected = {
            "uk": ("створено ттн", "отримання залежить від перевізника"),
            "ru": ("создана ттн", "получения зависит от перевозчика"),
            "en": ("has been created", "arrival date depends on the carrier"),
        }
        for kind in ("ttn_assigned", "exchange_shipped"):
            for locale, phrases in expected.items():
                with self.subTest(kind=kind, locale=locale):
                    text = _message(kind, locale, self.order, "20400000000000", exchange_size="M").lower()
                    for phrase in phrases:
                        self.assertIn(phrase, text)
                    self.assertIn("https://novaposhta.ua/tracking/?cargo_number=20400000000000", text)
                    for forbidden in ("1-3", "1–3", "вже в дорозі", "уже в пути", "on its way", "fully paid", "оплачен"):
                        self.assertNotIn(forbidden, text)
                    if kind == "exchange_shipped":
                        self.assertIn({"uk": "розмір m", "ru": "размер m", "en": "size m"}[locale], text)
                    else:
                        for forbidden in ("розмір", "размер", "size", "exchange"):
                            self.assertNotIn(forbidden, text)

    def test_exchange_without_size_does_not_invent_a_size(self):
        for locale in ("uk", "ru", "en"):
            with self.subTest(locale=locale):
                text = _message("exchange_shipped", locale, self.order, "20400000000002").lower()
                for forbidden in ("розмір", "размер", "size", "1-3", "1–3", "paid", "оплат"):
                    self.assertNotIn(forbidden, text)
                self.assertIn("20400000000002", text)


class OfferCopyTests(SimpleTestCase):
    def test_order_summary_is_localized_without_losing_commercial_facts(self):
        summary = {
            "items": [
                {
                    "title": "Reality Bends",
                    "color_label": "Термохром",
                    "fit_label": "Оверсайз",
                    "size": "M",
                    "quantity": 2,
                    "option_values": {"material": "thermo"},
                    "option_labels": {"material": "Термохром"},
                    "unit_price": "1450.00",
                    "line_total": "2900.00",
                }
            ],
            "quoted_total": "2900.00",
        }

        expected = {
            "uk": ("Замовлення", "Термохром", "Оверсайз", "розмір M", "2 шт.", "1450 грн/шт", "2900 грн", "скрин"),
            "ru": ("Заказ", "Термохром", "Оверсайз", "размер M", "2 шт.", "1450 грн/шт", "2900 грн", "скрин"),
            "en": ("Order", "Термохром", "Оверсайз", "size M", "2 pcs", "1450 UAH/pc", "2900 UAH", "screenshot"),
        }
        for locale, phrases in expected.items():
            with self.subTest(locale=locale):
                text = bot._checkout_offer_details(locale, summary)
                for phrase in phrases:
                    self.assertIn(phrase, text)

    def test_empty_summary_does_not_invent_order_details(self):
        self.assertEqual(bot._checkout_offer_details("uk", {}), "")


class CheckoutProposalSummaryTests(SimpleTestCase):
    @patch("management.models.IgCheckoutAccessToken.issue")
    @patch("management.services.bot_orders.create_checkout_proposal")
    def test_link_result_exposes_only_frozen_proposal_facts(self, create_proposal, issue):
        item = SimpleNamespace(
            product_title="Reality Bends",
            color_label="Термохром",
            fit_code="oversize",
            fit_label="Оверсайз",
            size="M",
            quantity=2,
            option_values={"fit": "oversize", "material": "thermo"},
            option_labels={"fit": "Оверсайз", "material": "Термохром"},
            unit_price=Decimal("1450.00"),
            line_total=Decimal("2900.00"),
        )
        proposal = SimpleNamespace(
            pk=101,
            public_id=uuid.uuid4(),
            quoted_total=Decimal("2900.00"),
            items=SimpleNamespace(all=lambda: [item]),
        )
        create_proposal.return_value = proposal
        issue.return_value = (
            "w4-frozen-token",
            SimpleNamespace(expires_at=SimpleNamespace(isoformat=lambda: "2026-08-02T12:00:00+00:00")),
        )

        result = bot_orders.create_checkout_proposal_link(
            SimpleNamespace(),
            item_specs=[{"product_id": 1, "size": "M", "qty": 2}],
        )

        self.assertEqual(
            result["order_summary"],
            {
                "items": [
                    {
                        "title": "Reality Bends",
                        "color_label": "Термохром",
                        "fit_code": "oversize",
                        "fit_label": "Оверсайз",
                        "size": "M",
                        "quantity": 2,
                        "option_values": {"fit": "oversize", "material": "thermo"},
                        "option_labels": {"fit": "Оверсайз", "material": "Термохром"},
                        "unit_price": "1450.00",
                        "line_total": "2900.00",
                    }
                ],
                "quoted_total": "2900.00",
            },
        )
        self.assertEqual(result["proposal_pk"], 101)


class FinalizeOfferCopyTests(TestCase):
    def setUp(self):
        self.client_record = IgClient.get_or_create_for_sender("w4-offer-copy")
        self.client_record.intent = IgClient.Intent.PAYMENT
        self.client_record.stage = IgClient.Stage.CHECKOUT
        self.client_record.language = "uk"
        self.client_record.save(
            update_fields=["intent", "stage", "language", "updated_at"]
        )
        # A real current unpaid purchase owner is mandatory. CRM stage alone
        # cannot authorize a payment link; use the native episode producer.
        deal = IgDeal.objects.create(client=self.client_record, status=IgDeal.Status.QUOTED,
            amount=Decimal("1580.00"), requested_payment_amount=Decimal("1580.00"))
        ensure_episode_for_deal(deal)
        self.client_record.refresh_from_db()

    @patch("management.services.instagram_bot.notify_manager")
    @patch("management.services.bot_orders.create_checkout_proposal_link")
    def test_successful_offer_includes_exact_order_summary(self, create_link, _notify):
        create_link.return_value = {
            "ok": True,
            "invoice_url": "https://twocomms.shop/offer/a/w4-token/",
            "proposal_url": "https://twocomms.shop/offer/a/w4-token/",
            "order_summary": {
                "items": [
                    {"title": "Reality Bends", "size": "M", "quantity": 2}
                ],
                "quoted_total": str(Decimal("1580.00")),
            },
        }

        text = bot.finalize_paylink(
            "Готово, зараз надішлю посилання на оплату.",
            {"paylink": "full", "product": 1},
            self.client_record,
            self.client_record.igsid,
        )

        self.assertIn("Reality Bends", text)
        self.assertIn("розмір M", text)
        self.assertIn("1580 грн", text)
        self.assertIn("Monobank", text)
        self.assertIn("нічого надсилати не треба", text)
        create_link.assert_called_once()
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())

    @patch("management.services.instagram_bot.notify_manager")
    @patch("management.services.bot_orders.create_checkout_proposal_link")
    def test_offer_copy_is_not_repeated_when_model_already_used_it(
        self, create_link, _notify
    ):
        create_link.return_value = {
            "ok": True,
            "invoice_url": "https://twocomms.shop/offer/a/w4-repeat/",
        }

        text = bot.finalize_paylink(
            "Перевірте товари в персональній пропозиції TwoComms. "
            "Зараз надішлю посилання на оплату.",
            {"paylink": "full", "product": 1},
            self.client_record,
            self.client_record.igsid,
        )

        self.assertEqual(text.count("Перевірте товари"), 1)

    @patch("management.services.instagram_bot.notify_manager")
    @patch("management.services.bot_orders.create_checkout_proposal_link")
    def test_failed_offer_uses_the_client_language(self, create_link, _notify):
        create_link.return_value = {"ok": False, "error": "provider_error"}
        self.client_record.language = "ru"
        self.client_record.save(update_fields=["language", "updated_at"])

        text = bot.finalize_paylink(
            "Вот ссылка на оплату.",
            {"paylink": "full", "product": 1},
            self.client_record,
            self.client_record.igsid,
        )

        self.assertIn("Уточню детали", text)
        self.assertNotIn("Уточню деталі", text)
