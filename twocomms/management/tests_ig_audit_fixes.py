"""Аудит-фікси IG-бота (Task 2): echo-гонка, неправильний товар у paylink,
дубль замовлення, safety-net створення замовлення.
"""
import json
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from management.models import IgClient, IgDeal, IgDealItem, InstagramBotSettings
from management.services import bot_orders
from management.services import instagram_bot as bot
from storefront.models import UserAction


class EchoChunkAndScopeTests(TestCase):
    """Bug A: echo приходить по чанках і має бути привʼязаний до отримувача."""

    def setUp(self):
        cache.clear()

    def test_marked_chunk_not_treated_as_manager(self):
        IgClient.get_or_create_for_sender("rcpt1")
        bot._mark_bot_sent("rcpt1", "Перша частина відповіді")
        bot._handle_echo("rcpt1", "Перша частина відповіді")
        c = IgClient.objects.get(igsid="rcpt1")
        self.assertFalse(c.bot_paused)
        self.assertFalse(c.manager_takeover)

    def test_unmarked_text_triggers_takeover(self):
        bot._handle_echo("rcpt2", "Вітаю, це Олег, менеджер")
        c = IgClient.objects.get(igsid="rcpt2")
        self.assertTrue(c.bot_paused)
        self.assertTrue(c.manager_takeover)

    def test_recipient_scoped_no_cross_client_false_negative(self):
        # бот написав "Привіт" клієнту A; менеджер пише те саме "Привіт" клієнту B
        bot._mark_bot_sent("A", "Привіт")
        bot._handle_echo("B", "Привіт")  # для B це менеджер, не власне відлуння
        self.assertTrue(IgClient.objects.get(igsid="B").bot_paused)

    @patch("management.services.instagram_bot.get_page_token", return_value="PT")
    @patch("management.services.instagram_bot._http", return_value=(200, "{}"))
    def test_send_text_marks_each_chunk(self, mock_http, mock_pt):
        IgClient.get_or_create_for_sender("rcptX")
        s = InstagramBotSettings.load()
        long_text = ("Абзац один. " * 60) + "\n" + ("Абзац два. " * 60)
        parts = bot._split_for_send(long_text)
        self.assertGreater(len(parts), 1)  # реально кілька чанків
        ok, kind, hint = bot.send_text(s, "rcptX", long_text)
        self.assertTrue(ok)
        # echo КОЖНОГО чанка не має спричинити перехоплення
        for part in parts:
            bot._handle_echo("rcptX", part)
        self.assertFalse(IgClient.objects.get(igsid="rcptX").bot_paused)


class SendApiErrorClassificationTests(TestCase):
    def test_advanced_access_subcode_is_explicit(self):
        body = json.dumps({
            "error": {
                "code": 200,
                "error_subcode": bot.ADVANCED_ACCESS_SUBCODE,
                "message": "App does not have advanced access for Instagram messages.",
            }
        })

        kind, hint = bot._classify_send_error(403, body)

        self.assertEqual(kind, "permanent")
        self.assertIn("нерольового", hint)
        self.assertIn("Advanced Access", hint)
        self.assertIn("instagram_manage_messages", hint)

    def test_messaging_window_subcode_is_explicit(self):
        body = json.dumps({
            "error": {
                "code": 10,
                "error_subcode": bot.MESSAGING_WINDOW_CLOSED_SUBCODE,
                "message": "Messaging window closed.",
            }
        })

        kind, hint = bot._classify_send_error(400, body)

        self.assertEqual(kind, "permanent")
        self.assertIn("24-годинне", hint)

    @patch("management.services.instagram_bot.get_page_token", return_value="PT")
    @patch("management.services.instagram_bot._http")
    def test_send_text_persists_permanent_error_for_dashboard(self, mock_http, _mock_pt):
        mock_http.return_value = (
            403,
            json.dumps({
                "error": {
                    "code": 200,
                    "error_subcode": bot.ADVANCED_ACCESS_SUBCODE,
                    "message": "App does not have advanced access for Instagram messages.",
                }
            }),
        )
        s = InstagramBotSettings.load()
        s.last_error = ""
        s.save(update_fields=["last_error"])

        ok, kind, hint = bot.send_text(s, "nonrole1", "Привіт")

        self.assertFalse(ok)
        self.assertEqual(kind, "permanent")
        self.assertIn("Advanced Access", hint)
        s.refresh_from_db()
        self.assertIn("Meta Send API", s.last_error)
        self.assertIn("Advanced Access", s.last_error)

    @patch("management.services.instagram_bot.get_page_token", return_value="PT")
    @patch("management.services.instagram_bot._http", return_value=(503, "provider overloaded"))
    def test_transient_provider_result_is_unknown_not_retryable(self, _mock_http, _mock_pt):
        ok, kind, hint = bot.send_text(InstagramBotSettings.load(), "uncertain-recipient", "Привіт")

        self.assertFalse(ok)
        self.assertEqual(kind, "unknown")
        self.assertIn("не підтверджено", hint)

    @patch("management.services.instagram_bot.get_page_token", return_value="PT")
    @patch("management.services.instagram_bot._http")
    def test_permanent_send_block_is_persisted_on_the_affected_client(self, mock_http, _mock_pt):
        client = IgClient.get_or_create_for_sender("delivery-blocked-client")
        mock_http.return_value = (
            403,
            json.dumps({
                "error": {
                    "code": 200,
                    "error_subcode": bot.ADVANCED_ACCESS_SUBCODE,
                    "message": "App does not have advanced access for Instagram messages.",
                }
            }),
        )

        bot.send_text(InstagramBotSettings.load(), client.igsid, "Привіт")

        client.refresh_from_db()
        self.assertEqual(getattr(client, "delivery_status", ""), "advanced_access")
        self.assertIn("Advanced Access", getattr(client, "delivery_error", ""))

    @patch("management.services.instagram_bot.get_page_token", return_value="PT")
    @patch("management.services.instagram_bot._http", return_value=(200, "{}"))
    def test_successful_send_clears_client_delivery_block(self, _mock_http, _mock_pt):
        client = IgClient.get_or_create_for_sender("delivery-cleared-client")
        setattr(client, "delivery_status", "advanced_access")
        setattr(client, "delivery_error", "попередня причина")
        client.save()

        bot.send_text(InstagramBotSettings.load(), client.igsid, "Привіт")

        client.refresh_from_db()
        self.assertEqual(getattr(client, "delivery_status", "advanced_access"), "")
        self.assertEqual(getattr(client, "delivery_error", "попередня причина"), "")

    @patch("management.services.instagram_bot.get_page_token", return_value="PT")
    @patch("management.services.instagram_bot._http")
    def test_graph_551_requires_message_requests_check_without_claiming_it_is_confirmed(self, mock_http, _mock_pt):
        client = IgClient.get_or_create_for_sender("delivery-request-check-client")
        mock_http.return_value = (
            400,
            json.dumps({"error": {"code": 551, "message": "Recipient unavailable."}}),
        )

        bot.send_text(InstagramBotSettings.load(), client.igsid, "Привіт")

        client.refresh_from_db()
        self.assertEqual(getattr(client, "delivery_status", ""), "message_request_check")

    @patch("management.services.instagram_bot.log")
    @patch("management.services.instagram_bot.get_page_token", return_value="PT")
    @patch("management.services.instagram_bot._http")
    def test_send_error_log_never_contains_raw_graph_response(self, mock_http, _mock_pt, mock_log):
        raw_marker = "raw-meta-response-marker"
        mock_http.return_value = (
            403,
            json.dumps({"error": {"code": 200, "message": raw_marker}}),
        )

        bot.send_text(InstagramBotSettings.load(), "untracked-recipient", "Привіт")

        logged = "\n".join(str(call.args) for call in mock_log.call_args_list)
        self.assertNotIn(raw_marker, logged)


class PaylinkProductTests(TestCase):
    """Bug B: paylink має бути на ПРАВИЛЬНИЙ товар, навіть якщо є стара чернетка."""

    def setUp(self):
        from storefront.models import Category, Product, ProductStatus

        cat = Category.objects.create(name="Одяг", slug="odiah-pl")
        self.p1 = Product.objects.create(title="Стара футболка", slug="old-tee", category=cat, price=600, status=ProductStatus.PUBLISHED)
        self.p2 = Product.objects.create(title="Худі Kharkiv", slug="hk-pl", category=cat, price=950, status=ProductStatus.PUBLISHED)
        self.c = IgClient.get_or_create_for_sender("pl1")
        old = IgDeal.objects.create(client=self.c, pay_type=IgDeal.PayType.ONLINE_FULL)
        IgDealItem.objects.create(deal=old, product=self.p1, title=self.p1.title, qty=1, unit_price=Decimal("600"))
        old.recalc_total()

    @patch("management.services.bot_orders.create_payment_link")
    def test_new_product_not_charged_as_old(self, mock_link):
        mock_link.return_value = {"ok": True, "invoice_url": "https://pay/x", "invoice_id": "x"}
        bot_orders.create_deal_and_link(self.c, pay_type="full", product_id=self.p2.id)
        billed_deal = mock_link.call_args.args[0]
        titles = [it.title for it in billed_deal.items.all()]
        self.assertIn("Худі Kharkiv", titles)
        self.assertNotIn("Стара футболка", titles)
        self.assertEqual(billed_deal.amount, Decimal("950"))


class SafetyNetTests(TestCase):
    """Bug D: створення замовлення без тегу [ORDER]."""

    def _paid_deal(self, igsid, with_np):
        c = IgClient.get_or_create_for_sender(igsid)
        d = IgDeal.objects.create(
            client=c, pay_type=IgDeal.PayType.ONLINE_FULL,
            status=IgDeal.Status.PAID, payment_status="paid",
            np_full_name=("Іван" if with_np else ""), np_phone=("0931112233" if with_np else ""),
            np_city=("Київ" if with_np else ""), np_office=("Відд 1" if with_np else ""),
        )
        IgDealItem.objects.create(deal=d, title="Худі", qty=1, unit_price=Decimal("950"))
        d.recalc_total()
        return c, d

    @patch("management.services.bot_orders.notify_manager")
    def test_fulfill_ready_paid_deals_creates_order(self, _n):
        c, d = self._paid_deal("sn1", with_np=True)
        self.assertEqual(bot_orders.fulfill_ready_paid_deals(), 1)
        d.refresh_from_db()
        self.assertIsNotNone(d.order_id)

    @patch("management.services.bot_orders.notify_manager")
    def test_fulfill_skips_without_np(self, _n):
        c, d = self._paid_deal("sn2", with_np=False)
        self.assertEqual(bot_orders.fulfill_ready_paid_deals(), 0)

    @patch("management.services.bot_orders.notify_manager")
    def test_safety_net_heals_missing_purchase_for_existing_order(self, _n):
        c, deal = self._paid_deal("sn-heal", with_np=True)
        order = bot_orders.create_order_from_deal(deal)
        UserAction.objects.filter(
            action_type='purchase',
            order_id=order.pk,
        ).delete()

        self.assertEqual(bot_orders.fulfill_ready_paid_deals(), 0)

        self.assertTrue(
            UserAction.objects.filter(
                action_type='purchase',
                order_id=order.pk,
            ).exists()
        )

    def test_looks_like_contact_info(self):
        self.assertTrue(bot._looks_like_contact_info("0931112233"))
        self.assertTrue(bot._looks_like_contact_info("Київ, відділення 5, Іван"))
        self.assertFalse(bot._looks_like_contact_info("а скільки коштує?"))
