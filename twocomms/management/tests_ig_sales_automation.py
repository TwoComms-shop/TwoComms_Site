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
from management.services.instagram_bot import ProviderDeliveryReceipt

User = get_user_model()
MGMT = override_settings(
    ROOT_URLCONF="twocomms.urls_management",
    ALLOWED_HOSTS=["testserver", "management.twocomms.shop", "localhost", "127.0.0.1"],
    SECURE_SSL_REDIRECT=False,
)
KYIV = ZoneInfo("Europe/Kyiv")


class SalesClassifierTests(TestCase):
    @patch("management.services.ig_payment_review.create_payment_review")
    def test_manager_message_never_intakes_payment_review(self, create_review):
        from management.services import bot_sales_classifier

        client = IgClient.get_or_create_for_sender("sales_cls_manager_payment_boundary")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.MANAGER,
            text="Оплату підтверджено, очікуємо доставку",
        )

        bot_sales_classifier.classify_message(
            client,
            message=message,
            operational_effects=True,
        )

        create_review.assert_not_called()

    @patch("management.services.ig_payment_review.create_payment_review")
    @patch("management.services.ig_post_sale.open_post_sale_case")
    def test_projection_only_classification_creates_no_operational_cases(
        self,
        open_post_sale_case,
        create_review,
    ):
        from management.services import bot_sales_classifier

        client = IgClient.get_or_create_for_sender("sales_cls_projection_only")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Я оплатила, ось чек",
        )

        bot_sales_classifier.ensure_rule_classification(
            client,
            message,
            operational_effects=False,
            allow_post_sale_effects=False,
        )

        open_post_sale_case.assert_not_called()
        create_review.assert_not_called()

    def test_explicit_purchase_decision_sets_payment_intent(self):
        from management.models import IgConversationSignal
        from management.services import bot_sales_classifier

        client = IgClient.get_or_create_for_sender("sales_cls_purchase_decision")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Беру, давайте оформляти",
        )

        bot_sales_classifier.classify_message(client, message=message)

        client.refresh_from_db()
        self.assertEqual(client.intent, IgClient.Intent.PAYMENT)
        self.assertTrue(
            IgConversationSignal.objects.filter(
                client=client,
                message=message,
                signal_type=IgConversationSignal.Type.CHECKOUT_STARTED,
            ).exists()
        )

    def test_deferred_purchase_phrase_does_not_start_checkout(self):
        from management.models import IgConversationSignal
        from management.services import bot_sales_classifier

        client = IgClient.get_or_create_for_sender("sales_cls_deferred_decision")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Давайте позже",
        )

        bot_sales_classifier.classify_message(client, message=message)

        client.refresh_from_db()
        self.assertNotEqual(client.intent, IgClient.Intent.PAYMENT)
        self.assertFalse(
            IgConversationSignal.objects.filter(
                client=client,
                message=message,
                signal_type=IgConversationSignal.Type.CHECKOUT_STARTED,
            ).exists()
        )

    def test_payment_link_refusal_does_not_start_checkout(self):
        from management.models import IgConversationSignal
        from management.services import bot_sales_classifier

        client = IgClient.get_or_create_for_sender("sales_cls_link_refusal")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Ссылка на оплату не нужна",
        )

        bot_sales_classifier.classify_message(client, message=message)

        client.refresh_from_db()
        self.assertNotEqual(client.intent, IgClient.Intent.PAYMENT)
        self.assertFalse(
            IgConversationSignal.objects.filter(
                client=client,
                message=message,
                signal_type=IgConversationSignal.Type.CHECKOUT_STARTED,
            ).exists()
        )

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

    def test_bare_catalog_print_reference_is_not_custom_print(self):
        from management.models import IgConversationSignal
        from management.services import bot_sales_classifier

        client = IgClient.get_or_create_for_sender("sales_cls_catalog_print")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Принт ось цей, розмір M",
        )

        result = bot_sales_classifier.classify_message(client, message=message)

        client.refresh_from_db()
        self.assertNotEqual(client.intent, IgClient.Intent.CUSTOM_PRINT)
        self.assertNotEqual(result["interaction_type"], "custom_print")
        self.assertFalse(
            IgConversationSignal.objects.filter(
                client=client,
                signal_type=IgConversationSignal.Type.CUSTOM_PRINT,
            ).exists()
        )

    def test_wrong_print_is_support_not_custom_print(self):
        from management.services import bot_sales_classifier

        client = IgClient.get_or_create_for_sender("sales_cls_wrong_print")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Один принт не той відправили",
        )

        result = bot_sales_classifier.classify_message(client, message=message)

        client.refresh_from_db()
        self.assertNotEqual(client.intent, IgClient.Intent.CUSTOM_PRINT)
        self.assertEqual(result["interaction_type"], "support_complaint")

    def test_dtf_supplier_pitch_is_collaboration_not_custom_print(self):
        from management.services import bot_sales_classifier

        client = IgClient.get_or_create_for_sender("sales_cls_dtf_supplier")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Ми постачальник DTF друку, пропонуємо співпрацю",
        )

        result = bot_sales_classifier.classify_message(client, message=message)

        client.refresh_from_db()
        self.assertNotEqual(client.intent, IgClient.Intent.CUSTOM_PRINT)
        self.assertEqual(result["interaction_type"], "collaboration")

    def test_explicit_request_to_change_own_print_remains_custom_print(self):
        from management.services import bot_sales_classifier

        client = IgClient.get_or_create_for_sender("sales_cls_explicit_custom_print")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Можете змінити принт і надрукувати мій власний дизайн?",
        )

        result = bot_sales_classifier.classify_message(client, message=message)

        client.refresh_from_db()
        self.assertEqual(client.intent, IgClient.Intent.CUSTOM_PRINT)
        self.assertEqual(result["interaction_type"], "custom_print")

    def test_neutral_followup_does_not_inherit_legacy_custom_print_intent(self):
        from management.models import IgConversationSignal
        from management.services import bot_sales_classifier

        client = IgClient.get_or_create_for_sender("sales_cls_stale_custom_print")
        client.intent = IgClient.Intent.CUSTOM_PRINT
        client.save(update_fields=["intent", "updated_at"])
        old_message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="legacy false custom print",
        )
        IgConversationSignal.objects.create(
            client=client,
            message=old_message,
            signal_type=IgConversationSignal.Type.CUSTOM_PRINT,
            confidence=0.9,
        )
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Дякую",
        )

        result = bot_sales_classifier.classify_message(client, message=message)

        client.refresh_from_db()
        self.assertEqual(client.intent, IgClient.Intent.UNKNOWN)
        self.assertEqual(result["interaction_type"], "information_only")

    def test_rules_projection_ignores_custom_print_signal_from_older_episode(self):
        from management.models import IgCommercialEpisode, IgConversationSignal
        from management.services import bot_sales_classifier

        client = IgClient.get_or_create_for_sender("rules_old_custom_print_episode")
        old_message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="legacy custom print",
        )
        IgConversationSignal.objects.create(
            client=client,
            message=old_message,
            signal_type=IgConversationSignal.Type.CUSTOM_PRINT,
            confidence=0.9,
        )
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Дякую",
        )
        episode = IgCommercialEpisode.objects.create(
            client=client,
            sequence=1,
            materialization_key="rules-old-custom-print-episode",
            opened_watermark_message_id=message.pk,
        )
        client.current_commercial_episode = episode
        client.intent = IgClient.Intent.UNKNOWN
        client.save(update_fields=[
            "current_commercial_episode", "intent", "updated_at",
        ])

        snapshot = bot_sales_classifier.reconcile_rules_projection(
            client,
            watermark=message.pk,
        )

        self.assertEqual(snapshot.interaction_type, "information_only")

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
    def setUp(self):
        from management.models import InstagramBotSettings

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.save(update_fields=["is_enabled"])

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

    def test_uncertain_transient_followup_failure_is_ambiguous_and_not_retried(self):
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
            self.assertEqual(task.status, IgFollowUpTask.Status.AMBIGUOUS)
            self.assertEqual(task.attempt_count, 1)
            self.assertIsNone(task.next_attempt_at)
            self.assertTrue(
                IgFollowUpTask.objects.filter(
                    delivery_review_for=task,
                    kind=IgFollowUpTask.Kind.MANAGER_TASK,
                    status=IgFollowUpTask.Status.PENDING,
                ).exists()
            )

            # The daemon can run again immediately, but ambiguous delivery is
            # resolved only by the manager and must never cross Meta twice.
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
            return_value=ProviderDeliveryReceipt(True, "", "", "retry-recovery"),
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

    def test_evening_followup_stays_inside_the_approved_window(self):
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
        self.assertEqual(due_local.hour, 20)
        self.assertEqual(due_local.minute, 30)
        self.assertEqual(due_local.date().isoformat(), "2026-07-09")
        self.assertEqual(task.meta_window_deadline, now + timedelta(hours=23))

    def test_discount_ladder_starts_at_5_and_caps_at_10(self):
        from management.services import bot_followups

        client = IgClient.get_or_create_for_sender("fu_discount")
        client.stage = IgClient.Stage.PRODUCT_MATCHED
        client.followup_level = 1
        client.discount_offered_percent = 0
        client.save()
        InstagramBotMessage.objects.create(client=client, sender_id="fu_discount", role="user",
                                           text="Не можу собі дозволити за таку суму", status="done")

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
        self.assertFalse(
            IgFollowUpTask.objects.filter(
                client=client,
                kind=IgFollowUpTask.Kind.PAYMENT,
                status=IgFollowUpTask.Status.PENDING,
            ).exists()
        )
        fulfillment = IgFollowUpTask.objects.get(
            client=client,
            deal=deal,
            kind=IgFollowUpTask.Kind.FULFILLMENT,
            status=IgFollowUpTask.Status.PENDING,
        )
        self.assertEqual(fulfillment.reason, "paid_missing_delivery")
        self.assertEqual(fulfillment.level, 0)

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
        from django.contrib.auth.models import Permission
        from management.bot_access import (
            OPERATE_IG_BOT_PERMISSION,
            VIEW_IG_CONVERSATION_PII_PERMISSION,
        )
        from management.models import InstagramBotSettings

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.save(update_fields=["is_enabled"])
        self.admin = User.objects.create_user("sales_adm", password="x", is_staff=True)
        for capability in (VIEW_IG_CONVERSATION_PII_PERMISSION, OPERATE_IG_BOT_PERMISSION):
            app_label, codename = capability.split(".", 1)
            self.admin.user_permissions.add(Permission.objects.get(
                content_type__app_label=app_label, codename=codename,
            ))
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

    def test_payment_delivery_task_exposes_invoice_message_in_client_detail(self):
        from management.models import IgFollowUpTask

        invoice_message = "Оплата: https://pay.example/invoice/visible-in-card"
        IgFollowUpTask.objects.create(
            client=self.active,
            due_at=timezone.now(),
            status=IgFollowUpTask.Status.SKIPPED,
            kind=IgFollowUpTask.Kind.MANAGER_TASK,
            reason="payment_link_delivery_review",
            message_text=invoice_message,
            skip_reason="meta_link_restriction",
        )

        data = self.client.get(
            reverse("management_bot_client_detail_api", args=[self.active.id])
        ).json()
        task = next(
            item
            for item in data["followups"]
            if item["reason"] == "payment_link_delivery_review"
        )

        self.assertEqual(task["message_text"], invoice_message)

    def test_recent_payment_delivery_task_is_not_hidden_by_followup_limit(self):
        from management.models import IgFollowUpTask

        old_due_at = timezone.now() - timedelta(days=2)
        for index in range(51):
            IgFollowUpTask.objects.create(
                client=self.active,
                due_at=old_due_at,
                status=IgFollowUpTask.Status.SKIPPED,
                kind=IgFollowUpTask.Kind.MANAGER_TASK,
                reason=f"old-review-{index}",
            )
        fresh = IgFollowUpTask.objects.create(
            client=self.active,
            due_at=timezone.now(),
            status=IgFollowUpTask.Status.SKIPPED,
            kind=IgFollowUpTask.Kind.MANAGER_TASK,
            reason="payment_link_delivery_review",
            message_text="Оплата: https://pay.example/invoice/fresh",
        )

        data = self.client.get(
            reverse("management_bot_client_detail_api", args=[self.active.id])
        ).json()

        self.assertEqual(len(data["followups"]), 50)
        self.assertIn(fresh.id, {item["id"] for item in data["followups"]})

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

    def test_stats_accepts_inclusive_custom_local_date_range(self):
        inside = IgClient.get_or_create_for_sender("api_custom_range_inside")
        outside = IgClient.get_or_create_for_sender("api_custom_range_outside")
        inside.last_message_at = timezone.make_aware(
            datetime(2026, 7, 12, 12, 0), KYIV
        )
        outside.last_message_at = timezone.make_aware(
            datetime(2026, 7, 14, 0, 1), KYIV
        )
        inside.save(update_fields=["last_message_at", "updated_at"])
        outside.save(update_fields=["last_message_at", "updated_at"])

        data = self.client.get(
            reverse("management_bot_stats_api")
            + "?date_from=2026-07-12&date_to=2026-07-13"
        ).json()

        self.assertTrue(data["success"])
        self.assertEqual(data["range_mode"], "custom")
        self.assertEqual(data["date_from"], "2026-07-12")
        self.assertEqual(data["date_to"], "2026-07-13")
        self.assertEqual(data["totals"]["conversations"], 1)

    def test_stats_rejects_reversed_custom_date_range(self):
        response = self.client.get(
            reverse("management_bot_stats_api")
            + "?date_from=2026-07-14&date_to=2026-07-12"
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])

    def test_stats_period_applies_to_hidden_and_scheduled_followup_metrics(self):
        from management.models import IgFollowUpTask

        old_hidden = IgClient.get_or_create_for_sender("api_old_hidden_stats")
        old_hidden.hidden_at = timezone.now() - timedelta(days=60)
        old_hidden.save(update_fields=["hidden_at", "updated_at"])
        IgFollowUpTask.objects.create(
            client=self.active,
            due_at=timezone.now() - timedelta(days=2),
            status=IgFollowUpTask.Status.PENDING,
        )
        IgFollowUpTask.objects.create(
            client=self.active,
            due_at=timezone.now() - timedelta(days=60),
            status=IgFollowUpTask.Status.PENDING,
        )

        recent = self.client.get(
            reverse("management_bot_stats_api") + "?days=7"
        ).json()
        all_time = self.client.get(
            reverse("management_bot_stats_api") + "?days=0"
        ).json()

        self.assertEqual(recent["totals"]["hidden"], 1)
        self.assertEqual(recent["totals"]["pending_followups"], 1)
        self.assertEqual(all_time["totals"]["hidden"], 2)
        self.assertEqual(all_time["totals"]["pending_followups"], 2)

    def test_hide_moves_client_out_of_active_queue_and_statistics(self):
        from management.models import IgFollowUpTask, IgPollCursor
        from management.services import instagram_bot

        poll_cursor = IgPollCursor.objects.create(
            conversation_id="active-conversation",
            participant_igsid=self.active.igsid,
        )

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
        poll_cursor.refresh_from_db()
        self.assertIsNotNone(poll_cursor.excluded_at)
        self.assertEqual(poll_cursor.excluded_reason, "client_hidden")
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
            return ProviderDeliveryReceipt(True, "", "", "hide-race")

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

        def capture_media_then_hide(*_args, **_kwargs):
            IgClient.objects.filter(pk=self.active.pk).update(
                automation_lease_until=timezone.now() - timedelta(seconds=1)
            )
            hide_responses.append(self.client.post(
                reverse("management_bot_client_hide_api", args=[self.active.id]),
                {"reason": "manual"},
            ))
            return []

        with patch(
            "management.services.instagram_bot.send_sender_action"
        ), patch(
            # Live image inspection now shares the current media preparation
            # and reply provider path; the old separate vision match is gone.
            "management.services.instagram_bot._capture_message_media",
            side_effect=capture_media_then_hide,
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
        from management.models import IgPollCursor

        poll_cursor = IgPollCursor.objects.create(
            conversation_id="active-conversation",
            participant_igsid=self.active.igsid,
            excluded_at=timezone.now(),
            excluded_reason="client_hidden",
            synced_provider_updated_at=timezone.now(),
        )
        self.client.post(
            reverse("management_bot_client_hide_api", args=[self.active.id]),
            {"reason": "noise"},
        )

        r = self.client.post(reverse("management_bot_client_unhide_api", args=[self.active.id]))
        self.assertEqual(r.status_code, 200)
        self.active.refresh_from_db()
        self.assertIsNone(self.active.hidden_at)
        poll_cursor.refresh_from_db()
        self.assertIsNone(poll_cursor.excluded_at)
        self.assertEqual(poll_cursor.excluded_reason, "")
        self.assertIsNone(poll_cursor.synced_provider_updated_at)
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

    def test_confirmed_funnel_reset_keeps_chat_and_clears_mutable_sales_state(self):
        from management.models import IgFollowUpTask
        from management.services.instagram_bot import _build_history

        self.active.intent = IgClient.Intent.CUSTOM_PRINT
        self.active.language = "ru"
        self.active.primary_objection = IgClient.Objection.PRICE
        self.active.current_size = "XL"
        self.active.current_color = "black"
        self.active.current_qty = 3
        self.active.discount_offered_percent = 10
        self.active.memory_summary = "Стара пам'ять"
        self.active.sales_context = {"legacy": True}
        self.active.bot_paused = True
        self.active.save()
        old_message = InstagramBotMessage.objects.create(
            sender_id=self.active.igsid,
            client=self.active,
            role=InstagramBotMessage.Role.USER,
            text="Старий контекст",
        )
        followup = IgFollowUpTask.objects.create(
            client=self.active,
            due_at=timezone.now() + timedelta(hours=1),
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason="waiting_for_reply",
        )

        unconfirmed = self.client.post(
            f"/bot/api/clients/{self.active.id}/reset-funnel/",
        )
        self.assertEqual(unconfirmed.status_code, 409)

        response = self.client.post(
            f"/bot/api/clients/{self.active.id}/reset-funnel/",
            {"confirm_reset": "1", "reason": "qa_retest"},
        )

        self.assertEqual(response.status_code, 200)
        self.active.refresh_from_db()
        followup.refresh_from_db()
        self.assertEqual(self.active.stage, IgClient.Stage.NEW)
        self.assertEqual(self.active.intent, IgClient.Intent.UNKNOWN)
        self.assertEqual(self.active.language, "")
        self.assertEqual(self.active.primary_objection, IgClient.Objection.NONE)
        self.assertEqual(self.active.buying_readiness, 0)
        self.assertEqual(self.active.current_size, "")
        self.assertEqual(self.active.current_color, "")
        self.assertEqual(self.active.current_qty, 1)
        self.assertEqual(self.active.discount_offered_percent, 0)
        self.assertEqual(self.active.memory_summary, "")
        self.assertEqual(self.active.sales_context, {})
        self.assertTrue(self.active.bot_paused)
        self.assertTrue(InstagramBotMessage.objects.filter(pk=old_message.pk).exists())
        self.assertEqual(followup.status, IgFollowUpTask.Status.CANCELLED)
        from management.models import IgFunnelResetAudit

        audit = IgFunnelResetAudit.objects.get(client=self.active)
        self.assertEqual(audit.reset_after_message_id, old_message.pk)
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual(audit.reason, "qa_retest")

        new_message = InstagramBotMessage.objects.create(
            sender_id=self.active.igsid,
            client=self.active,
            role=InstagramBotMessage.Role.USER,
            text="Новий тест",
        )
        self.assertEqual(
            _build_history(self.active.igsid),
            [{"role": "user", "text": new_message.text}],
        )

    def test_funnel_reset_preserves_verified_payment_stage(self):
        paid = IgClient.get_or_create_for_sender("api_reset_paid")
        paid.stage = IgClient.Stage.ORDER_CREATED
        paid.intent = IgClient.Intent.CUSTOM_PRINT
        paid.save(update_fields=["stage", "intent", "updated_at"])
        IgDeal.objects.create(
            client=paid,
            status=IgDeal.Status.PAID,
            payment_status="paid",
            paid_at=timezone.now(),
        )

        response = self.client.post(
            f"/bot/api/clients/{paid.id}/reset-funnel/",
            {"confirm_reset": "1", "reason": "qa_retest"},
        )

        self.assertEqual(response.status_code, 200)
        paid.refresh_from_db()
        self.assertEqual(paid.stage, IgClient.Stage.PAID)
        self.assertEqual(paid.intent, IgClient.Intent.UNKNOWN)

    def test_hidden_client_cannot_be_reset(self):
        response = self.client.post(
            f"/bot/api/clients/{self.hidden.id}/reset-funnel/",
            {"confirm_reset": "1", "reason": "qa_retest"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["success"])

    def test_bot_page_has_ukrainian_action_labels_and_visible_action_feedback(self):
        html = self.client.get(reverse("management_bot")).content.decode("utf-8")

        for label in (
            "Активні",
            "Приховані",
            "Відповідати самому",
            "Вернути боту",
            "Приховати",
            "Повернути до активних",
            "Позначити як втрачено",
        ):
            self.assertIn(label, html)
        self.assertIn("async function runClientAction", html)
        self.assertIn("Клієнта приховано", html)
        self.assertIn("Не вдалося виконати дію", html)
        self.assertIn("item.message_text", html)
        for label in (
            "Статистика продажів IG Direct",
            "Діалоги",
            "Кваліфіковані",
            "Товар визначено",
            "Заплановані контакти",
            "Контакти в діалогах з оплатою",
            "Шлях до оплати",
            "Поточні етапи",
            "Конверсія завершених",
            "Кампанії",
            "Причини втрат",
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
