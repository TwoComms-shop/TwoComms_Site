"""Sales automation upgrade tests for the Instagram Direct bot."""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.models import (
    IgClient,
    IgDeal,
    IgDealItem,
    InstagramBotMessage,
)

User = get_user_model()
MGMT = override_settings(
    ROOT_URLCONF="twocomms.urls_management",
    ALLOWED_HOSTS=["testserver", "management.twocomms.shop", "localhost", "127.0.0.1"],
    SECURE_SSL_REDIRECT=False,
)
KYIV = ZoneInfo("Europe/Kyiv")


class SalesClassifierTests(TestCase):
    def test_detects_language_intent_objection_and_custom_print(self):
        from management.models import IgConversationSignal
        from management.services import bot_sales_classifier

        client = IgClient.get_or_create_for_sender("sales_cls_1")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Скільки ціна? Хочу кастомний принт на подарунок, але дорого",
        )

        result = bot_sales_classifier.classify_message(client, message=message)

        client.refresh_from_db()
        self.assertEqual(client.language, "uk")
        self.assertEqual(client.intent, IgClient.Intent.CUSTOM_PRINT)
        self.assertEqual(client.primary_objection, IgClient.Objection.PRICE)
        self.assertGreaterEqual(client.buying_readiness, 50)
        self.assertTrue(result["signals"])
        self.assertTrue(
            IgConversationSignal.objects.filter(
                client=client,
                signal_type=IgConversationSignal.Type.CUSTOM_PRINT,
            ).exists()
        )
        self.assertTrue(client.sales_context.get("gift"))

    def test_stop_or_no_buy_closes_automation_without_rescue_discount(self):
        from management.models import IgFollowUpTask
        from management.services import bot_sales_classifier, bot_followups

        client = IgClient.get_or_create_for_sender("sales_cls_stop")
        client.stage = IgClient.Stage.PRODUCT_MATCHED
        client.discount_offered_percent = 5
        client.last_message_at = timezone.now()
        client.save()
        IgFollowUpTask.objects.create(
            client=client,
            due_at=timezone.now(),
            kind=IgFollowUpTask.Kind.RESCUE,
            reason="rescue",
            discount_percent=5,
        )
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Нет, не буду покупать, больше не пишите",
        )

        bot_sales_classifier.classify_message(client, message=message)
        bot_followups.cancel_pending(client, reason="client_no_buy")

        client.refresh_from_db()
        self.assertEqual(client.stage, IgClient.Stage.COLD)
        self.assertEqual(client.lost_reason, "no_buy")
        self.assertFalse(IgFollowUpTask.objects.filter(client=client, status="pending").exists())


class FollowUpPolicyTests(TestCase):
    def test_quiet_hours_move_due_time_to_next_10_kyiv(self):
        from management.models import IgFollowUpTask
        from management.services import bot_followups

        client = IgClient.get_or_create_for_sender("fu_quiet")
        now = datetime(2026, 7, 9, 18, 30, tzinfo=KYIV)
        client.last_message_at = now
        client.save()

        task = bot_followups.schedule_followup(
            client,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            delay=timedelta(hours=2),
            reason="qualification_unanswered",
            now=now,
        )

        due_local = task.due_at.astimezone(KYIV)
        self.assertEqual(due_local.hour, 10)
        self.assertEqual(due_local.minute, 0)
        self.assertEqual(due_local.date().isoformat(), "2026-07-10")
        self.assertEqual(task.meta_window_deadline, now + timedelta(hours=23))

    def test_discount_ladder_starts_at_5_and_caps_at_10(self):
        from management.services import bot_followups

        client = IgClient.get_or_create_for_sender("fu_discount")
        client.stage = IgClient.Stage.PRODUCT_MATCHED
        client.followup_level = 1
        client.discount_offered_percent = 0
        client.save()

        self.assertEqual(bot_followups.next_discount_percent(client), 5)
        client.discount_offered_percent = 5
        client.save()
        self.assertEqual(bot_followups.next_discount_percent(client, explicit_negotiation=True), 10)
        client.discount_offered_percent = 10
        client.save()
        self.assertEqual(bot_followups.next_discount_percent(client, explicit_negotiation=True), 0)

    def test_payment_link_creates_followup_and_paid_cancels_it(self):
        from management.models import IgFollowUpTask
        from management.services import bot_payments

        client = IgClient.get_or_create_for_sender("fu_payment")
        deal = IgDeal.objects.create(
            client=client,
            pay_type=IgDeal.PayType.ONLINE_FULL,
            status=IgDeal.Status.DRAFT,
            amount=Decimal("900"),
        )
        IgDealItem.objects.create(deal=deal, title="Тестова футболка", qty=1, unit_price=Decimal("900"))
        deal.recalc_total()

        with patch("storefront.views.monobank._monobank_api_request") as mock_api:
            mock_api.return_value = {"invoiceId": "inv_fu", "pageUrl": "https://pay/fu"}
            res = bot_payments.create_payment_link(deal)
        self.assertTrue(res["ok"])
        self.assertTrue(
            IgFollowUpTask.objects.filter(
                client=client,
                kind=IgFollowUpTask.Kind.PAYMENT,
                status=IgFollowUpTask.Status.PENDING,
            ).exists()
        )

        bot_payments.apply_payment_status(deal, "success")
        self.assertFalse(IgFollowUpTask.objects.filter(client=client, status="pending").exists())


class ManagerEchoAnalysisTests(TestCase):
    def test_manager_echo_is_persisted_and_keeps_bot_silent(self):
        from management.models import IgConversationSignal
        from management.services import instagram_bot

        instagram_bot._handle_echo("echo_sales_1", "Менеджер: можемо зробити передоплату 200 грн")

        client = IgClient.objects.get(igsid="echo_sales_1")
        self.assertTrue(client.bot_paused)
        self.assertTrue(client.manager_takeover)
        self.assertTrue(
            InstagramBotMessage.objects.filter(
                client=client,
                role=InstagramBotMessage.Role.MANAGER,
                text__icontains="передоплату",
            ).exists()
        )
        self.assertTrue(
            IgConversationSignal.objects.filter(
                client=client,
                signal_type=IgConversationSignal.Type.MANAGER_TAKEOVER,
            ).exists()
        )


@MGMT
class SalesCockpitApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("sales_adm", password="x", is_staff=True)
        self.client.force_login(self.admin)
        self.active = IgClient.get_or_create_for_sender("api_active")
        self.active.display_name = "Активний"
        self.active.stage = IgClient.Stage.PRODUCT_MATCHED
        self.active.buying_readiness = 72
        self.active.save()
        self.hidden = IgClient.get_or_create_for_sender("api_hidden")
        self.hidden.display_name = "Прихований"
        self.hidden.hidden_at = timezone.now()
        self.hidden.hidden_reason = "spam"
        self.hidden.save()

    def test_default_clients_exclude_hidden_and_hidden_view_includes_hidden(self):
        default_data = self.client.get(reverse("management_bot_clients_api")).json()
        self.assertTrue(any(c["id"] == self.active.id for c in default_data["clients"]))
        self.assertFalse(any(c["id"] == self.hidden.id for c in default_data["clients"]))

        hidden_data = self.client.get(reverse("management_bot_clients_api") + "?view=hidden").json()
        self.assertTrue(any(c["id"] == self.hidden.id for c in hidden_data["clients"]))

    def test_stats_endpoint_reports_conversion_and_objection_breakdown(self):
        from management.models import IgConversationSignal

        IgConversationSignal.objects.create(
            client=self.active,
            signal_type=IgConversationSignal.Type.PRICE_OBJECTION,
            confidence=0.9,
        )
        paid = IgClient.get_or_create_for_sender("api_paid")
        paid.stage = IgClient.Stage.PAID
        paid.save()

        data = self.client.get(reverse("management_bot_stats_api")).json()

        self.assertTrue(data["success"])
        self.assertGreaterEqual(data["totals"]["conversations"], 3)
        self.assertGreaterEqual(data["stages"].get(IgClient.Stage.PAID, 0), 1)
        self.assertGreaterEqual(data["objections"].get("price_objection", 0), 1)

    def test_hide_and_unhide_actions(self):
        r = self.client.post(
            reverse("management_bot_client_hide_api", args=[self.active.id]),
            {"reason": "noise"},
        )
        self.assertEqual(r.status_code, 200)
        self.active.refresh_from_db()
        self.assertIsNotNone(self.active.hidden_at)

        r = self.client.post(reverse("management_bot_client_unhide_api", args=[self.active.id]))
        self.assertEqual(r.status_code, 200)
        self.active.refresh_from_db()
        self.assertIsNone(self.active.hidden_at)
