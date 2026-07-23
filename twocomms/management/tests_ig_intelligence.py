from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from management.models import IgClient, IgConversationAnalysisSnapshot, IgDeal, InstagramBotMessage
from management.services.bot_sales_classifier import classify_message


class ConversationIntelligenceSnapshotTests(TestCase):
    def setUp(self):
        self.client = IgClient.objects.create(igsid="intelligence-user")

    def message(self, text, role=InstagramBotMessage.Role.USER):
        return InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=role,
            text=text,
            status=InstagramBotMessage.Status.DONE,
        )

    def test_user_message_persists_evidence_bound_snapshot(self):
        message = self.message("Який розмір M і чи є чорний?")

        result = classify_message(self.client, message=message)

        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(result["analysis_snapshot_id"], snapshot.id)
        self.assertEqual(snapshot.score_band, "exploring")
        self.assertEqual(snapshot.purchase_probability, Decimal("0.28"))
        self.assertGreaterEqual(snapshot.confidence, Decimal("0.55"))
        self.assertEqual(snapshot.analysis_model, "rules")
        self.assertEqual(snapshot.rules_version, "2026-07-23.v1")
        self.assertEqual(snapshot.evidence[0]["source_role"], "user")
        self.assertIn("product", snapshot.uncertainties)

    def test_payment_signal_is_high_intent_but_not_paid(self):
        message = self.message("Дайте посилання на оплату")

        classify_message(self.client, message=message)

        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(snapshot.score_band, "high_intent")
        self.assertEqual(snapshot.interaction_type, "high_intent")
        self.assertLess(snapshot.purchase_probability, Decimal("1.00"))
        self.assertIn("payment_unverified", snapshot.uncertainties)

    def test_manager_evidence_is_labeled_and_deduplicated(self):
        message = self.message("Підкажемо клієнту розмір", InstagramBotMessage.Role.MANAGER)

        classify_message(self.client, message=message)
        classify_message(self.client, message=message)

        self.assertEqual(self.client.analysis_snapshots.count(), 1)
        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(snapshot.evidence[0]["source_role"], "manager")
        self.assertTrue(snapshot.evidence[0]["manager_evidence"])
        self.client.refresh_from_db()
        self.assertEqual(self.client.buying_readiness, 0)
        self.assertEqual(snapshot.purchase_probability, Decimal("0.00"))

    def test_explicit_no_buy_is_lost_not_high_probability(self):
        message = self.message("Більше не пишіть, купувати не буду")

        classify_message(self.client, message=message)

        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(snapshot.score_band, "lost")
        self.assertEqual(snapshot.interaction_type, "opt_out")
        self.assertEqual(snapshot.purchase_probability, Decimal("0.00"))

    def test_new_snapshot_fks_are_cross_engine_safe(self):
        from management.models import IgConversationAnalysisSnapshot

        self.assertFalse(IgConversationAnalysisSnapshot._meta.get_field("client").db_constraint)
        self.assertFalse(
            IgConversationAnalysisSnapshot._meta.get_field("last_analyzed_message").db_constraint
        )

    def test_reaction_only_is_not_product_intent(self):
        message = self.message("🔥")

        result = classify_message(self.client, message=message)

        self.client.refresh_from_db()
        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(result["interaction_type"], "reaction_only")
        self.assertEqual(snapshot.interaction_type, "reaction_only")
        self.assertEqual(self.client.buying_readiness, 0)
        self.assertEqual(self.client.intent, IgClient.Intent.UNKNOWN)

    @patch("management.services.instagram_bot.send_text")
    @patch("management.services.instagram_bot.gemini_generate")
    @patch("management.services.instagram_bot.send_sender_action")
    def test_reaction_only_is_stored_without_auto_reply(
        self, send_sender_action, gemini_generate, send_text
    ):
        from management.models import InstagramBotSettings
        from management.services import instagram_bot

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.ai_enabled = True
        settings.allowed_senders = ""
        settings.save(update_fields=["is_enabled", "ai_enabled", "allowed_senders"])
        message = self.message("❤️")
        message.status = InstagramBotMessage.Status.PROCESSING
        message.processing_started_at = message.created_at
        message.save(update_fields=["status", "processing_started_at"])

        self.assertTrue(instagram_bot._process_one_unlocked(settings, message))

        message.refresh_from_db()
        self.assertEqual(message.status, InstagramBotMessage.Status.DONE)
        gemini_generate.assert_not_called()
        send_text.assert_not_called()
        send_sender_action.assert_not_called()

    @patch("management.services.bot_followups.schedule_after_inbound")
    def test_reaction_only_does_not_schedule_followup(self, schedule_after_inbound):
        from management.models import InstagramBotSettings
        from management.services import instagram_bot

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.allowed_senders = ""
        settings.save(update_fields=["is_enabled", "allowed_senders"])

        self.assertTrue(
            instagram_bot.enqueue_inbound(
                settings,
                sender_id="reaction-ingress",
                text="🔥",
                mid="reaction-ingress-mid",
            )
        )

        schedule_after_inbound.assert_not_called()

    @patch("management.services.bot_followups.schedule_after_inbound")
    @patch("management.services.instagram_bot.send_text")
    @patch("management.services.instagram_bot.gemini_generate")
    @patch("management.services.instagram_bot.send_sender_action")
    def test_global_reply_pause_stores_and_analyzes_without_reply_backlog(
        self, sender_action, gemini, send_text, schedule_followup
    ):
        from management.models import InstagramBotSettings
        from management.services import instagram_bot

        settings = InstagramBotSettings.load()
        settings.is_enabled = False
        settings.allowed_senders = ""
        settings.save(update_fields=["is_enabled", "allowed_senders"])

        self.assertTrue(
            instagram_bot.enqueue_inbound(
                settings,
                sender_id=self.client.igsid,
                text="Хочу чорну футболку розміру M",
                mid="global-paused-observation",
            )
        )

        message = InstagramBotMessage.objects.get(mid="global-paused-observation")
        self.assertEqual(message.status, InstagramBotMessage.Status.DONE)
        self.assertIsNotNone(message.processed_at)
        self.assertEqual(message.analysis_snapshots.count(), 1)
        self.assertEqual(instagram_bot.process_pending(settings), 0)
        sender_action.assert_not_called()
        gemini.assert_not_called()
        send_text.assert_not_called()
        schedule_followup.assert_not_called()

    @patch("management.services.bot_followups.schedule_after_inbound")
    def test_paused_client_is_observed_without_followup_or_pending_reply(self, schedule_followup):
        from management.models import InstagramBotSettings
        from management.services import instagram_bot

        self.client.bot_paused = True
        self.client.paused_reason = "manager_takeover"
        self.client.save(update_fields=["bot_paused", "paused_reason", "updated_at"])
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.allowed_senders = ""
        settings.save(update_fields=["is_enabled", "allowed_senders"])

        self.assertTrue(
            instagram_bot.enqueue_inbound(
                settings,
                sender_id=self.client.igsid,
                text="Ціна зависока, я подумаю",
                mid="client-paused-observation",
            )
        )

        message = InstagramBotMessage.objects.get(mid="client-paused-observation")
        self.assertEqual(message.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(message.analysis_snapshots.count(), 1)
        schedule_followup.assert_not_called()

    @patch("management.services.instagram_bot.send_text")
    @patch("management.services.instagram_bot.gemini_generate")
    def test_stop_converts_pending_rows_so_resume_never_replies_to_old_backlog(
        self, gemini, send_text
    ):
        from management.models import InstagramBotSettings
        from management.services import instagram_bot

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.allowed_senders = ""
        settings.save(update_fields=["is_enabled", "allowed_senders"])
        self.assertTrue(
            instagram_bot.enqueue_inbound(
                settings,
                sender_id=self.client.igsid,
                text="Скільки коштує?",
                mid="pre-stop-pending",
            )
        )
        self.assertEqual(
            InstagramBotMessage.objects.get(mid="pre-stop-pending").status,
            InstagramBotMessage.Status.PENDING,
        )

        instagram_bot.stop_bot()
        instagram_bot.start_bot()
        self.assertEqual(instagram_bot.process_pending(), 0)
        message = InstagramBotMessage.objects.get(mid="pre-stop-pending")
        self.assertEqual(message.status, InstagramBotMessage.Status.DONE)
        gemini.assert_not_called()
        send_text.assert_not_called()

    def test_disabled_poll_message_stays_observed_when_resume_wins_before_ingress(self):
        from datetime import timedelta

        from management.models import InstagramBotSettings
        from management.services import instagram_bot

        settings = InstagramBotSettings.load()
        settings.is_enabled = False
        settings.allowed_senders = ""
        settings.save(update_fields=["is_enabled", "allowed_senders"])
        provider_created_at = timezone.now() - timedelta(seconds=5)
        instagram_bot.start_bot()

        self.assertTrue(
            instagram_bot.enqueue_inbound(
                settings,
                sender_id=self.client.igsid,
                text="Повідомлення під час паузи",
                mid="disabled-poll-before-resume",
                source="poll",
                received_at=provider_created_at,
            )
        )

        message = InstagramBotMessage.objects.get(mid="disabled-poll-before-resume")
        self.assertEqual(message.status, InstagramBotMessage.Status.DONE)

    def test_manager_takeover_closes_existing_reply_and_followup_backlog(self):
        from datetime import timedelta

        from management.models import IgFollowUpTask, InstagramBotSettings
        from management.services import instagram_bot

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.allowed_senders = ""
        settings.save(update_fields=["is_enabled", "allowed_senders"])
        self.assertTrue(
            instagram_bot.enqueue_inbound(
                settings,
                sender_id=self.client.igsid,
                text="Підкажіть ціну",
                mid="takeover-existing-pending",
            )
        )
        task = IgFollowUpTask.objects.create(
            client=self.client,
            due_at=timezone.now() + timedelta(hours=1),
            kind=IgFollowUpTask.Kind.QUALIFICATION,
        )

        with patch("management.services.instagram_bot.notify_manager"):
            instagram_bot._handle_echo(self.client.igsid, "Вже відповідаю клієнту")

        message = InstagramBotMessage.objects.get(mid="takeover-existing-pending")
        task.refresh_from_db()
        self.assertEqual(message.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(task.status, IgFollowUpTask.Status.CANCELLED)

    def test_plain_greeting_is_information_only_without_readiness_inflation(self):
        message = self.message("Привіт")

        classify_message(self.client, message=message)

        self.client.refresh_from_db()
        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(snapshot.interaction_type, "information_only")
        self.assertEqual(self.client.buying_readiness, 0)

    def test_interaction_taxonomy_contract_is_complete(self):
        self.assertEqual(
            {value for value, _label in IgConversationAnalysisSnapshot.InteractionType.choices},
            {
                "unknown",
                "reaction_only",
                "information_only",
                "product_interest",
                "size_fit_question",
                "custom_print",
                "price_objection",
                "high_intent",
                "payment_pending",
                "paid_order_waiting",
                "no_reply",
                "explicit_no_buy",
                "opt_out",
                "spam_abuse",
                "manager_observation",
            },
        )

    def test_explicit_no_buy_does_not_add_positive_intent_or_readiness(self):
        message = self.message("Не хочу кастомний принт, більше не пишіть")

        result = classify_message(self.client, message=message)

        self.client.refresh_from_db()
        self.assertEqual(result["interaction_type"], "opt_out")
        self.assertEqual(self.client.intent, IgClient.Intent.UNKNOWN)
        self.assertEqual(self.client.buying_readiness, 0)

    def test_paid_customer_opt_out_preserves_verified_stage(self):
        self.client.stage = IgClient.Stage.PAID
        self.client.save(update_fields=["stage", "updated_at"])
        IgDeal.objects.create(
            client=self.client,
            status=IgDeal.Status.PAID,
            payment_status="paid",
            paid_at=timezone.now(),
        )
        message = self.message("Стоп, більше не пишіть")

        result = classify_message(self.client, message=message)

        self.client.refresh_from_db()
        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(result["interaction_type"], "opt_out")
        self.assertEqual(self.client.stage, IgClient.Stage.PAID)
        self.assertEqual(snapshot.score_band, "paid")

    def test_forged_paid_stage_is_not_payment_truth(self):
        self.client.stage = IgClient.Stage.PAID
        self.client.save(update_fields=["stage", "updated_at"])

        classify_message(self.client, text="Дякую", role=InstagramBotMessage.Role.USER)

        snapshot = self.client.analysis_snapshots.get()
        self.assertNotEqual(snapshot.score_band, "paid")
        self.assertNotEqual(snapshot.interaction_type, "paid_order_waiting")

    def test_verified_deal_is_payment_truth_even_if_soft_stage_is_stale(self):
        IgDeal.objects.create(
            client=self.client,
            status=IgDeal.Status.PAID,
            payment_status="paid",
            paid_at=timezone.now(),
        )

        classify_message(self.client, text="Дякую", role=InstagramBotMessage.Role.USER)

        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(snapshot.score_band, "paid")
        self.assertEqual(snapshot.interaction_type, "paid_order_waiting")

    def test_persisted_no_reply_objection_is_classified_as_no_reply(self):
        self.client.primary_objection = IgClient.Objection.NO_REPLY
        self.client.save(update_fields=["primary_objection", "updated_at"])

        result = classify_message(
            self.client,
            text="",
            role=InstagramBotMessage.Role.USER,
        )

        snapshot = self.client.analysis_snapshots.get()
        self.assertEqual(result["interaction_type"], "no_reply")
        self.assertEqual(snapshot.interaction_type, "no_reply")

    def test_terminal_inbound_cancels_existing_followup_immediately(self):
        from datetime import timedelta

        from management.models import IgFollowUpTask, InstagramBotSettings
        from management.services import instagram_bot

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.allowed_senders = ""
        settings.save(update_fields=["is_enabled", "allowed_senders"])
        task = IgFollowUpTask.objects.create(
            client=self.client,
            due_at=timezone.now() + timedelta(hours=2),
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason="qualification_unanswered",
        )

        self.assertTrue(
            instagram_bot.enqueue_inbound(
                settings,
                sender_id=self.client.igsid,
                text="Не буду купувати, більше не пишіть",
                mid="terminal-ingress-mid",
            )
        )

        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.CANCELLED)
        self.assertEqual(task.skip_reason, "opt_out")
