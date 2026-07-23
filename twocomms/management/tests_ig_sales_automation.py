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
    def test_followup_stops_only_for_verified_payment(self):
        from management.services import bot_followups

        forged = IgClient.get_or_create_for_sender("fu_forged_paid")
        forged.stage = IgClient.Stage.PAID
        forged.save(update_fields=["stage", "updated_at"])
        verified = IgClient.get_or_create_for_sender("fu_verified_paid")
        IgDeal.objects.create(
            client=verified,
            status=IgDeal.Status.PAID,
            payment_status="paid",
            paid_at=timezone.now(),
        )

        self.assertEqual(bot_followups._client_allows_followup(forged), (True, ""))
        self.assertEqual(
            bot_followups._client_allows_followup(verified),
            (False, "already_converted"),
        )

    def test_followup_does_not_restart_automatically_after_reversal(self):
        from management.services import bot_followups

        client = IgClient.get_or_create_for_sender("fu_reversed_payment")
        IgDeal.objects.create(
            client=client,
            status=IgDeal.Status.PAID,
            payment_status="reversed",
            payment_truth=IgDeal.PaymentTruth.REVERSED,
            paid_at=timezone.now(),
            paid_amount=Decimal("950"),
            refunded_amount=Decimal("950"),
        )

        self.assertEqual(
            bot_followups._client_allows_followup(client),
            (False, "payment_reversed"),
        )

    def test_transient_followup_failure_is_backed_off_and_not_hot_looped(self):
        from management.models import IgFollowUpTask, InstagramBotSettings
        from management.services import bot_followups

        now = datetime(2026, 7, 9, 14, 0, tzinfo=KYIV)
        client = IgClient.get_or_create_for_sender("fu_retry")
        client.last_message_at = now
        client.stage = IgClient.Stage.QUALIFYING
        client.save(update_fields=["last_message_at", "stage", "updated_at"])
        task = IgFollowUpTask.objects.create(
            client=client,
            due_at=now,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason="retryable_provider_error",
        )

        with patch(
            "management.services.instagram_bot.send_text",
            return_value=(False, "transient", "temporary provider failure"),
        ) as send_text:
            self.assertEqual(
                bot_followups.process_due_followups(
                    InstagramBotSettings.load(), now=now, limit=1
                ),
                0,
            )
            task.refresh_from_db()
            self.assertEqual(task.status, IgFollowUpTask.Status.PENDING)
            self.assertEqual(task.attempt_count, 1)
            self.assertGreater(task.next_attempt_at, now)
            self.assertEqual(task.due_at, task.next_attempt_at)

            # The daemon can run again immediately, but the task is not eligible
            # until its persisted retry timestamp.
            self.assertEqual(
                bot_followups.process_due_followups(
                    InstagramBotSettings.load(), now=now, limit=1
                ),
                0,
            )
            send_text.assert_called_once()

    def test_followup_retries_after_backoff_and_marks_sent(self):
        from management.models import IgFollowUpTask, InstagramBotMessage, InstagramBotSettings
        from management.services import bot_followups

        now = datetime(2026, 7, 9, 14, 0, tzinfo=KYIV)
        client = IgClient.get_or_create_for_sender("fu_retry_recovery")
        client.last_message_at = now
        client.stage = IgClient.Stage.QUALIFYING
        client.save(update_fields=["last_message_at", "stage", "updated_at"])
        task = IgFollowUpTask.objects.create(
            client=client,
            due_at=now,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason="retryable_provider_error",
        )
        retry_at = now + timedelta(minutes=5)
        task.next_attempt_at = retry_at
        task.due_at = retry_at
        task.attempt_count = 1
        task.save(update_fields=["next_attempt_at", "due_at", "attempt_count", "updated_at"])

        with patch(
            "management.services.instagram_bot.send_text",
            return_value=(True, "", ""),
        ):
            self.assertEqual(
                bot_followups.process_due_followups(
                    InstagramBotSettings.load(), now=retry_at, limit=1
                ),
                1,
            )

        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SENT)
        self.assertIsNone(task.next_attempt_at)
        self.assertTrue(
            InstagramBotMessage.objects.filter(
                client=client, source="followup", role=InstagramBotMessage.Role.MODEL
            ).exists()
        )

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

        bot_payments.apply_payment_status(
            deal, "success",
            payload={"status": "success", "amount": 90000, "finalAmount": 90000},
        )
        self.assertFalse(IgFollowUpTask.objects.filter(client=client, status="pending").exists())

    def test_previous_paid_order_does_not_block_new_deal_payment_followup(self):
        from management.models import IgFollowUpTask, IgPaymentProjection
        from management.services import bot_followups

        client = IgClient.get_or_create_for_sender("fu_repeat_buyer")
        old = IgDeal.objects.create(
            client=client,
            status=IgDeal.Status.PAID,
            payment_truth=IgDeal.PaymentTruth.CONFIRMED,
            paid_at=timezone.now(),
            amount=Decimal("900"),
        )
        IgPaymentProjection.objects.create(
            deal=old,
            client=client,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount=Decimal("900"),
            paid_at=timezone.now(),
        )
        current = IgDeal.objects.create(
            client=client,
            status=IgDeal.Status.AWAITING_PAYMENT,
            payment_truth=IgDeal.PaymentTruth.PENDING,
            amount=Decimal("950"),
        )
        IgPaymentProjection.objects.create(
            deal=current,
            client=client,
            truth=IgDeal.PaymentTruth.PENDING,
        )

        task = bot_followups.schedule_payment_followup(current)

        self.assertIsNotNone(task)
        self.assertEqual(task.deal_id, current.id)
        self.assertEqual(task.kind, IgFollowUpTask.Kind.PAYMENT)


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
        IgDeal.objects.create(
            client=paid,
            status=IgDeal.Status.PAID,
            payment_status="paid",
            paid_at=timezone.now(),
        )

        data = self.client.get(reverse("management_bot_stats_api")).json()

        self.assertTrue(data["success"])
        # Hidden clients are intentionally excluded from sales analytics.
        self.assertEqual(data["totals"]["conversations"], 2)
        self.assertGreaterEqual(data["stages"].get(IgClient.Stage.PAID, 0), 1)
        self.assertGreaterEqual(data["objections"].get("price_objection", 0), 1)

    def test_paid_views_and_stats_ignore_stage_without_verified_payment(self):
        forged = IgClient.get_or_create_for_sender("api_forged_paid")
        forged.stage = IgClient.Stage.PAID
        forged.save(update_fields=["stage", "updated_at"])
        verified = IgClient.get_or_create_for_sender("api_verified_paid")
        IgDeal.objects.create(
            client=verified,
            status=IgDeal.Status.PAID,
            payment_status="prepaid",
            paid_at=timezone.now(),
        )

        paid_rows = self.client.get(
            reverse("management_bot_clients_api") + "?view=paid"
        ).json()["clients"]
        paid_ids = {row["id"] for row in paid_rows}
        stats = self.client.get(reverse("management_bot_stats_api")).json()

        self.assertNotIn(forged.id, paid_ids)
        self.assertIn(verified.id, paid_ids)
        self.assertEqual(stats["totals"]["paid"], 1)

        active_rows = self.client.get(
            reverse("management_bot_clients_api") + "?view=active"
        ).json()["clients"]
        forged_row = next(item for item in active_rows if item["id"] == forged.id)
        self.assertEqual(forged_row["stage"], "unverified")
        self.assertEqual(forged_row["stage_raw"], IgClient.Stage.PAID)
        self.assertEqual(forged_row["payment_truth"], "unverified")
        self.assertEqual(stats["stages"].get("unverified"), 1)
        self.assertNotIn(IgClient.Stage.PAID, stats["stages"])

    def test_active_filter_does_not_combine_truth_across_different_deals(self):
        split = IgClient.get_or_create_for_sender("api_split_deal_truth")
        IgDeal.objects.create(
            client=split,
            status=IgDeal.Status.PAID,
            payment_status="unpaid",
            paid_at=timezone.now(),
        )
        IgDeal.objects.create(
            client=split,
            status=IgDeal.Status.DRAFT,
            payment_status="paid",
            paid_at=timezone.now(),
        )

        active_ids = {
            item["id"]
            for item in self.client.get(
                reverse("management_bot_clients_api") + "?view=active"
            ).json()["clients"]
        }
        self.assertIn(split.id, active_ids)

    def test_date_filtered_paid_metrics_use_payment_date(self):
        old_paid = IgClient.get_or_create_for_sender("api_old_payment_event")
        old_paid.last_message_at = timezone.now()
        old_paid.ad_id = "ad-old-payment"
        old_paid.save(update_fields=["last_message_at", "ad_id", "updated_at"])
        IgDeal.objects.create(
            client=old_paid,
            status=IgDeal.Status.PAID,
            payment_status="paid",
            paid_at=timezone.now() - timedelta(days=60),
            amount=Decimal("900"),
        )

        data = self.client.get(
            reverse("management_bot_stats_api") + "?days=7"
        ).json()
        ad_row = next(row for row in data["ads"] if row["ad_id"] == "ad-old-payment")

        self.assertEqual(data["totals"]["paid"], 0)
        self.assertEqual(ad_row["paid"], 0)
        self.assertEqual(ad_row["revenue"], "0")

    def test_payment_status_card_uses_verified_truth_not_latest_unpaid_attempt(self):
        verified = IgClient.get_or_create_for_sender("api_paid_then_retry")
        IgDeal.objects.create(
            client=verified,
            status=IgDeal.Status.PAID,
            payment_status="paid",
            paid_at=timezone.now(),
        )
        IgDeal.objects.create(
            client=verified,
            status=IgDeal.Status.AWAITING_PAYMENT,
            payment_status="unpaid",
        )

        rows = self.client.get(
            reverse("management_bot_clients_api") + "?view=paid"
        ).json()["clients"]
        row = next(item for item in rows if item["id"] == verified.id)

        self.assertEqual(row["payment_status"], "paid")

    def test_reversed_payment_is_explicit_and_not_rendered_as_paid(self):
        reversed_client = IgClient.get_or_create_for_sender("api_reversed_payment")
        reversed_client.stage = IgClient.Stage.PAID
        reversed_client.save(update_fields=["stage", "updated_at"])
        IgDeal.objects.create(
            client=reversed_client,
            status=IgDeal.Status.PAID,
            payment_status="reversed",
            payment_truth=IgDeal.PaymentTruth.REVERSED,
            paid_at=timezone.now(),
            paid_amount=Decimal("950"),
            refunded_amount=Decimal("950"),
            payment_truth_updated_at=timezone.now(),
        )

        active_rows = self.client.get(
            reverse("management_bot_clients_api") + "?view=active"
        ).json()["clients"]
        row = next(item for item in active_rows if item["id"] == reversed_client.id)
        paid_ids = {
            item["id"]
            for item in self.client.get(
                reverse("management_bot_clients_api") + "?view=paid"
            ).json()["clients"]
        }

        self.assertEqual(row["stage"], "payment_reversed")
        self.assertEqual(row["payment_truth"], IgDeal.PaymentTruth.REVERSED)
        self.assertEqual(row["payment_status"], "reversed")
        self.assertNotIn(reversed_client.id, paid_ids)
        stats = self.client.get(reverse("management_bot_stats_api")).json()
        self.assertEqual(stats["totals"]["paid"], 0)

    def test_stats_date_range_excludes_old_inactive_conversations(self):
        self.active.last_message_at = timezone.now()
        self.active.save(update_fields=["last_message_at", "updated_at"])
        old = IgClient.get_or_create_for_sender("api_old_stats")
        old.last_message_at = timezone.now() - timedelta(days=60)
        old.save(update_fields=["last_message_at", "updated_at"])

        data = self.client.get(
            reverse("management_bot_stats_api") + "?days=7"
        ).json()

        self.assertEqual(data["range_days"], 7)
        self.assertTrue(data["range_from"])
        self.assertEqual(data["totals"]["conversations"], 1)

    def test_hide_moves_client_out_of_active_queue_and_statistics(self):
        from management.models import IgFollowUpTask
        from management.services import instagram_bot

        IgFollowUpTask.objects.create(
            client=self.active,
            due_at=timezone.now() + timedelta(hours=1),
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason="waiting_for_reply",
        )
        r = self.client.post(
            reverse("management_bot_client_hide_api", args=[self.active.id]),
            {"reason": "noise"},
        )
        self.assertEqual(r.status_code, 200)
        self.active.refresh_from_db()
        self.assertIsNotNone(self.active.hidden_at)
        self.assertTrue(instagram_bot._client_blocked(self.active))
        self.assertFalse(
            IgFollowUpTask.objects.filter(
                client=self.active, status=IgFollowUpTask.Status.PENDING
            ).exists()
        )

        active_ids = {
            item["id"]
            for item in self.client.get(reverse("management_bot_clients_api") + "?view=active").json()["clients"]
        }
        hidden_ids = {
            item["id"]
            for item in self.client.get(reverse("management_bot_clients_api") + "?view=hidden").json()["clients"]
        }
        self.assertNotIn(self.active.id, active_ids)
        self.assertIn(self.active.id, hidden_ids)

        stats = self.client.get(reverse("management_bot_stats_api")).json()
        self.assertEqual(stats["totals"]["conversations"], 0)
        self.assertEqual(stats["totals"]["qualified"], 0)
        self.assertEqual(stats["totals"]["hidden"], 2)
        self.assertEqual(stats["stages"], {})

    def test_hide_finalizes_already_queued_inbound_messages(self):
        queued = InstagramBotMessage.objects.create(
            sender_id=self.active.igsid,
            client=self.active,
            role=InstagramBotMessage.Role.USER,
            text="не обробляйте після приховування",
            status=InstagramBotMessage.Status.PENDING,
        )

        response = self.client.post(
            reverse("management_bot_client_hide_api", args=[self.active.id]),
            {"reason": "manual"},
        )

        self.assertEqual(response.status_code, 200)
        queued.refresh_from_db()
        self.assertEqual(queued.status, InstagramBotMessage.Status.DONE)
        self.assertIsNotNone(queued.processed_at)

    def test_hide_never_reports_success_while_client_automation_is_active(self):
        self.active.automation_lease_token = "active-automation"
        self.active.automation_lease_until = timezone.now() + timedelta(minutes=2)
        self.active.save(update_fields=[
            "automation_lease_token", "automation_lease_until", "updated_at",
        ])

        response = self.client.post(
            reverse("management_bot_client_hide_api", args=[self.active.id]),
            {"reason": "noise"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["success"])
        self.assertTrue(response.json()["retryable"])
        self.active.refresh_from_db()
        self.assertIsNone(self.active.hidden_at)

    def test_hide_conflicts_with_an_inflight_followup_send(self):
        """A successful Hide must never race with an already-picked follow-up."""
        from management.models import IgFollowUpTask, InstagramBotSettings
        from management.services import bot_followups

        now = timezone.now()
        IgFollowUpTask.objects.create(
            client=self.active,
            due_at=now,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason="waiting_for_reply",
        )
        hide_responses = []

        def send_while_hiding(*_args, **_kwargs):
            hide_responses.append(self.client.post(
                reverse("management_bot_client_hide_api", args=[self.active.id]),
                {"reason": "manual"},
            ))
            return True, "", ""

        with patch(
            "management.services.bot_followups.next_allowed_send_at", return_value=now
        ), patch(
            "management.services.instagram_bot.send_text", side_effect=send_while_hiding
        ):
            self.assertEqual(
                bot_followups.process_due_followups(
                    InstagramBotSettings.load(), now=now, limit=1
                ),
                1,
            )

        self.assertEqual(len(hide_responses), 1)
        self.assertEqual(hide_responses[0].status_code, 409)
        self.active.refresh_from_db()
        self.assertIsNone(self.active.hidden_at)

    def test_hide_after_vision_lease_expiry_stops_follow_on_processing(self):
        from management.models import InstagramBotSettings
        from management.services import instagram_bot

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.ai_enabled = True
        settings.save(update_fields=["is_enabled", "ai_enabled"])
        self.active.profile_fetched_at = timezone.now()
        self.active.save(update_fields=["profile_fetched_at", "updated_at"])
        row = InstagramBotMessage.objects.create(
            sender_id=self.active.igsid,
            client=self.active,
            role=InstagramBotMessage.Role.USER,
            text="що на фото?",
            attachments="[]",
            status=InstagramBotMessage.Status.PROCESSING,
            processing_started_at=timezone.now(),
        )
        hide_responses = []

        def vision_then_hide(*_args, **_kwargs):
            IgClient.objects.filter(pk=self.active.pk).update(
                automation_lease_until=timezone.now() - timedelta(seconds=1)
            )
            hide_responses.append(self.client.post(
                reverse("management_bot_client_hide_api", args=[self.active.id]),
                {"reason": "manual"},
            ))
            return {"product_id": None, "confidence": 0.0, "reason": "none"}

        with patch(
            "management.services.instagram_bot.send_sender_action"
        ), patch(
            "management.services.instagram_bot._collect_images",
            return_value=[("image/jpeg", b"x")],
        ), patch(
            "management.services.instagram_bot._match_allowed", return_value=True
        ), patch(
            "management.services.bot_vision.match", side_effect=vision_then_hide
        ), patch(
            "management.services.instagram_bot._maybe_pin_from_match"
        ) as pin_match, patch(
            "management.services.instagram_bot.gemini_generate"
        ) as generate_reply, patch(
            "management.services.instagram_bot.send_text"
        ) as send_text:
            handled = instagram_bot._process_one(settings, row)

        self.assertFalse(handled)
        self.assertEqual(hide_responses[0].status_code, 200)
        pin_match.assert_not_called()
        generate_reply.assert_not_called()
        send_text.assert_not_called()

    def test_unhide_returns_client_to_active_queue(self):
        self.client.post(
            reverse("management_bot_client_hide_api", args=[self.active.id]),
            {"reason": "noise"},
        )

        r = self.client.post(reverse("management_bot_client_unhide_api", args=[self.active.id]))
        self.assertEqual(r.status_code, 200)
        self.active.refresh_from_db()
        self.assertIsNone(self.active.hidden_at)
        active_ids = {
            item["id"]
            for item in self.client.get(reverse("management_bot_clients_api") + "?view=active").json()["clients"]
        }
        self.assertIn(self.active.id, active_ids)

    def test_pause_resume_and_mark_lost_actions_change_client_state(self):
        from management.models import IgFollowUpTask

        pause = self.client.post(reverse("management_bot_client_pause_api", args=[self.active.id]))
        self.assertEqual(pause.status_code, 200)
        self.active.refresh_from_db()
        self.assertTrue(self.active.bot_paused)

        resume = self.client.post(reverse("management_bot_client_resume_api", args=[self.active.id]))
        self.assertEqual(resume.status_code, 200)
        self.active.refresh_from_db()
        self.assertFalse(self.active.bot_paused)

        IgFollowUpTask.objects.create(
            client=self.active,
            due_at=timezone.now() + timedelta(hours=1),
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason="waiting_for_reply",
        )
        lost = self.client.post(
            reverse("management_bot_client_mark_lost_api", args=[self.active.id]),
            {"reason": "manual_lost"},
        )
        self.assertEqual(lost.status_code, 200)
        self.active.refresh_from_db()
        self.assertEqual(self.active.stage, IgClient.Stage.COLD)
        self.assertFalse(
            IgFollowUpTask.objects.filter(
                client=self.active, status=IgFollowUpTask.Status.PENDING
            ).exists()
        )

    def test_bot_page_has_ukrainian_action_labels_and_visible_action_feedback(self):
        html = self.client.get(reverse("management_bot")).content.decode("utf-8")

        for label in (
            "Активні",
            "Приховані",
            "Зупинити бота",
            "Відновити бота",
            "Приховати",
            "Повернути до активних",
            "Позначити як втрачено",
        ):
            self.assertIn(label, html)
        self.assertIn("async function runClientAction", html)
        self.assertIn("Клієнта приховано", html)
        self.assertIn("Не вдалося виконати дію", html)
        for label in (
            "Статистика продажів IG Direct",
            "Діалоги",
            "Кваліфіковані",
            "Товар визначено",
            "Заплановані контакти",
            "Повернення після нагадування",
            "Воронка продажів",
            "Частка від діалогів",
            "Ефективність реклами",
            "Заперечення клієнтів",
            "Сьогодні",
            "7 днів",
            "30 днів",
            "Увесь час",
        ):
            self.assertIn(label, html)
        for english_label in (
            "Conversations",
            "Qualified",
            "Product matched",
            "Pending follow-ups",
            "Ad/ref performance",
            "Objections",
        ):
            self.assertNotIn(english_label, html)
