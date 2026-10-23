"""Full operator reset: new context, retained financial/permission evidence."""
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import OperationalError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.models import (
    IgClient, IgCommercialEpisode, IgCommerceSelectionSession,
    IgConversationAnalysisJob, IgConversationAnalysisSnapshot,
    IgCustomerTurn, IgCustomerTurnRevision, IgFunnelResetAudit,
    IgRevisionDeliveryEffect, IgWebhookInboxEvent,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_funnel_reset import (
    current_message_floor, pre_reset_source_is_historical, reset_funnel,
)
from management.tests_ig_buyer_truth import BuyerTruthTestMixin
from management.tests_ig_hosted_settlement import HostedSettlementFixture
from management.tests_support import AnalysisPrivacyCleanupMixin


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
                   ALLOWED_HOSTS=["testserver"], IG_PROVIDER_TRANSPORT="instagram_login")
class FreshConversationResetTests(BuyerTruthTestMixin, TestCase):
    def setUp(self):
        self.transport = patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        self.transport.start()
        self.addCleanup(self.transport.stop)
        self.manager = get_user_model().objects.create_superuser(
            username="fresh-reset-operator", email="operator@example.invalid", password="x",
        )
        self.client.force_login(self.manager)
        self.settings = InstagramBotSettings.objects.create(pk=1, ig_user_id="reset-owner", is_enabled=True)
        self.customer = IgClient.objects.create(
            igsid="fresh-reset-customer", language="ru", intent=IgClient.Intent.SIZE,
            current_size="XL", current_color="black", memory_summary="Old preferences",
            sales_context={"_profile": {"sizes": ["XL"]}, "shipping_city": "Old city"},
            ad_ref="old-ref", ad_id="old-ad", ad_source="ad", ad_title="Old title",
            ad_creative_url="https://example.invalid/old", referral_payload={"ref": "old"},
            phone="+380500000000", phone_normalized="380500000000",
            bot_paused=True, manager_takeover=True, reply_permission_epoch=9,
            purchases_count=2, total_spent=Decimal("4000.00"),
            last_user_message_at=timezone.now()-timedelta(days=2),
        )
        self.message = InstagramBotMessage.objects.create(
            client=self.customer, sender_id=self.customer.igsid, mid="reset-old-source",
            provider_namespace="instagram_login:reset-owner", text="Old size XL",
            role=InstagramBotMessage.Role.USER, status=InstagramBotMessage.Status.PENDING,
        )
        self.episode = IgCommercialEpisode.objects.create(
            client=self.customer, sequence=1, open_slot=1, materialization_key="reset-old-episode",
            opened_watermark_message_id=self.message.pk,
        )
        self.customer.current_commercial_episode = self.episode
        self.customer.save(update_fields=["current_commercial_episode"])
        self.session = IgCommerceSelectionSession.objects.create(
            client=self.customer, commercial_episode=self.episode, generation=1, open_slot=1,
            lines=[{"line_id": "old-line", "size": "XL", "quantity": 3}],
        )

    def reset(self, **kwargs):
        return reset_funnel(client_id=self.customer.pk, actor=self.manager,
                            mode="fresh_conversation", **kwargs)

    def inbox(self, *, timestamp=None, text="Old question", namespace=None, echo=False):
        namespace = namespace or "instagram_login:reset-owner"
        owner = namespace.split(":", 1)[1]
        event = {"sender": {"id": self.customer.igsid}, "recipient": {"id": owner},
                 "message": {"mid": f"inbox-{IgWebhookInboxEvent.objects.count()}", "text": text}}
        if timestamp is not None:
            event["timestamp"] = int(timestamp.timestamp()*1000)
        if echo:
            event["message"]["is_echo"] = True
        payload = {"object": "instagram", "entry": [{"id": owner, "messaging": [event]}]}
        from management.services.ig_webhook_inbox import _canonical

        digest = hashlib.sha256(_canonical(payload)).hexdigest()
        return IgWebhookInboxEvent.objects.create(namespace=namespace, owner_id=owner,
            customer_igsid=self.customer.igsid, event_key=event["message"]["mid"],
            decision=IgWebhookInboxEvent.Decision.ACCEPTED, payload=payload, payload_digest=digest)

    def test_full_reset_clears_profile_referral_and_cart_preserving_permissions_and_clocks(self):
        before_clock = self.customer.last_user_message_at
        self.customer.opted_out_at = timezone.now()-timedelta(days=3)
        self.customer.save(update_fields=["opted_out_at"])
        result = self.reset(expected_permission_epoch=9)
        self.assertTrue(result["ok"])
        self.customer.refresh_from_db()
        self.session.refresh_from_db()
        self.episode.refresh_from_db()
        self.message.refresh_from_db()
        self.assertEqual(self.customer.sales_context, {})
        self.assertEqual(self.customer.memory_summary, "")
        self.assertEqual(self.customer.language, "")
        self.assertEqual(self.customer.current_size, "")
        for field in ("ad_ref", "ad_id", "ad_source", "ad_title", "ad_creative_url"):
            self.assertEqual(getattr(self.customer, field), "")
        self.assertEqual(self.customer.referral_payload, {})
        self.assertEqual(self.customer.phone, "")
        self.assertEqual(self.customer.phone_normalized, "")
        self.assertTrue(self.customer.bot_paused)
        self.assertTrue(self.customer.manager_takeover)
        self.assertIsNotNone(self.customer.opted_out_at)
        self.assertEqual(self.customer.last_user_message_at, before_clock)
        self.assertEqual(self.customer.purchases_count, 2)
        self.assertEqual(self.customer.total_spent, Decimal("4000.00"))
        self.assertEqual(self.customer.reply_permission_epoch, 10)
        self.assertIsNone(self.session.open_slot)
        self.assertEqual(self.session.lines[0]["line_id"], "old-line")
        self.assertIsNone(self.episode.open_slot)
        self.assertNotEqual(self.customer.current_commercial_episode_id, self.episode.pk)
        fresh = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        self.assertEqual(fresh.commercial_episode_id, self.customer.current_commercial_episode_id)
        self.assertEqual(fresh.lines, [])
        self.assertEqual(fresh.generation, 2)
        self.assertEqual(self.message.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(current_message_floor(self.customer), self.message.pk+1)
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.customer).count(), 1)

    def test_full_reset_of_real_confirmed_buyer_opens_new_episode_without_rewriting_payment(self):
        from management.models import IgPaymentConfirmationReview
        from management.services.bot_payment_truth import client_has_confirmed_purchase

        order = self._order("RESET-HISTORY-1")
        review = self._review(self.customer, dedupe="reset-payment-review",
                              status=IgPaymentConfirmationReview.Status.CONFIRMED, order=order)
        decision = self._manager_decision(review)
        self.assertTrue(client_has_confirmed_purchase(self.customer))
        result = self.reset()
        self.customer.refresh_from_db()
        review.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(result["stage"], IgClient.Stage.NEW)
        self.assertTrue(client_has_confirmed_purchase(self.customer))
        self.assertEqual(review.status, IgPaymentConfirmationReview.Status.CONFIRMED)
        self.assertEqual(order.payment_status, "paid")
        self.assertEqual(order.total_sum, Decimal("2100.00"))
        self.assertEqual(review.decisions.get().pk, decision.pk)
        self.assertNotEqual(self.customer.current_commercial_episode_id, self.episode.pk)

    def test_old_lightweight_mode_keeps_profile_referral_and_buyer_episode(self):
        with patch("management.services.ig_funnel_reset.client_has_confirmed_purchase", return_value=True):
            result = reset_funnel(client_id=self.customer.pk, actor=self.manager)
        self.customer.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(result["stage"], IgClient.Stage.PAID)
        self.assertIn("_profile", self.customer.sales_context)
        self.assertEqual(self.customer.ad_ref, "old-ref")
        self.assertEqual(self.customer.phone, "+380500000000")
        self.assertEqual(self.customer.current_commercial_episode_id, self.episode.pk)
        self.assertEqual(self.session.open_slot, 1)
        self.assertFalse(IgFunnelResetAudit.objects.get().previous_state.get("reset_mode"))

    def test_confirmation_and_epoch_compare_and_swap_are_required_by_api(self):
        url = reverse("management_bot_client_reset_funnel_api", args=[self.customer.pk])
        data = {"confirm_reset": "1", "mode": "fresh_conversation", "expected_permission_epoch": "9"}
        self.assertEqual(self.client.post(url, data).status_code, 409)
        data["confirm_fresh_conversation"] = "1"
        data["expected_permission_epoch"] = "8"
        self.assertEqual(self.client.post(url, data).status_code, 409)
        self.assertFalse(IgFunnelResetAudit.objects.exists())
        data["expected_permission_epoch"] = "9"
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "fresh_conversation")
        self.assertEqual(response.json()["reply_permission_epoch"], 10)
        self.assertIn("retired_pending_messages", response.json())
        self.assertEqual(self.client.post(url, data).status_code, 409)
        self.assertEqual(IgFunnelResetAudit.objects.count(), 1)

    def test_preaccepted_unknown_clock_is_archived_raw_without_source(self):
        row = self.inbox()
        original = (row.payload, row.payload_digest)
        with patch("management.services.instagram_bot.handle_webhook_payload", side_effect=AssertionError("must not materialize")):
            result = self.reset()
        row.refresh_from_db()
        self.assertEqual(result["archived_inbox_events"], 1)
        self.assertIsNotNone(row.processed_at)
        self.assertEqual(row.reason, "fresh_conversation_reset_history")
        self.assertEqual((row.payload, row.payload_digest), original)
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.customer).count(), 1)
        boundary = IgFunnelResetAudit.objects.get().previous_state["reset_boundary"]
        self.assertEqual(boundary["schema"], "fresh-conversation-reset.v1")
        self.assertEqual(boundary["inbox_highwater"], row.pk)

    def test_late_old_event_is_archived_but_new_unknown_and_foreign_owner_are_not(self):
        from management.services.ig_webhook_inbox import _archive_historical_customer_event

        old_time = timezone.now()-timedelta(days=1)
        self.reset()
        late = self.inbox(timestamp=old_time)
        unknown = self.inbox()
        future = self.inbox(timestamp=timezone.now()+timedelta(seconds=1))
        foreign = self.inbox(timestamp=old_time, namespace="instagram_login:other-owner")
        for row, expected in ((late, True), (unknown, False), (future, False), (foreign, False)):
            self.assertEqual(_archive_historical_customer_event(row, namespace="instagram_login:reset-owner",
                owner_id="reset-owner", now=timezone.now()), expected)
        self.assertFalse(pre_reset_source_is_historical(self.customer, provider_created_at=old_time,
                                                        provider_namespace="instagram_login:other-owner"))

    def test_drain_archives_old_replay_before_native_dispatch(self):
        from management.services.ig_webhook_inbox import drain_webhook_inbox

        self.reset()
        row = self.inbox(timestamp=timezone.now()-timedelta(days=1))
        with patch("management.services.instagram_bot.handle_webhook_payload", side_effect=AssertionError("no replay")):
            self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
            self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 0)
        row.refresh_from_db()
        self.assertEqual(row.reason, "fresh_conversation_reset_history")
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.customer).count(), 1)

    def test_reset_does_not_archive_echo_or_other_customer(self):
        echo = self.inbox(echo=True)
        other = self.inbox()
        other.customer_igsid = "other-customer"
        other.save(update_fields=["customer_igsid"])
        result = self.reset()
        self.assertEqual(result["archived_inbox_events"], 0)
        echo.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNone(echo.processed_at)
        self.assertIsNone(other.processed_at)

    def test_old_analyses_are_not_current_in_either_selector_or_card_prefetch(self):
        from management.services.ig_analysis_materiality import current_analysis_snapshot
        from management.bot_views import _client_card

        snapshot = IgConversationAnalysisSnapshot.objects.create(client=self.customer,
            last_analyzed_message=self.message, commercial_episode=self.episode,
            dedupe_key="reset-old-analysis", score_band="qualified", interaction_type="size_fit_question")
        self.reset()
        self.customer.refresh_from_db()
        self.customer._latest_customer_analysis = [snapshot]
        self.customer._latest_analysis = [snapshot]
        for mode in ("off", "shadow"):
            with override_settings(IG_ANALYSIS_MATERIALITY_MODE=mode, IG_ANALYSIS_FRESHNESS_SELECTOR=True):
                self.assertIsNone(current_analysis_snapshot(self.customer))
                self.assertIsNone(current_analysis_snapshot(self.customer, candidates=[snapshot]))
                self.assertNotEqual(_client_card(self.customer)["interaction_type"], "size_fit_question")

    def test_unstarted_effects_and_recovery_retire_but_sent_unknown_receipts_survive(self):
        now = timezone.now()
        turn = IgCustomerTurn.objects.create(client=self.customer, episode=self.episode,
            primary_source_message=self.message, window_started_at=now, window_deadline=now,
            claim_state=IgCustomerTurn.ClaimState.CLAIMED, claim_token="old-turn")
        revision = IgCustomerTurnRevision.objects.create(client=self.customer, turn=turn, revision=1,
            state=IgCustomerTurnRevision.State.CLAIMED, quiet_started_at=now, quiet_deadline=now,
            quiet_cap_at=now, overall_deadline=now, claim_token="old-revision", recovery_state="pending",
            recovery_due_at=now, action_receipts={"source_actions": {"kept": True}})
        states = ("planned", "claimed", "provider_started", "sent", "unknown")
        for i, state in enumerate(states):
            IgRevisionDeliveryEffect.objects.create(revision=revision, source_message=self.message,
                effect_key=f"reset-effect-{i}", group="substantive_text", kind="text", order_index=i,
                part_index=i, part_count=5, plan_digest="a"*64, payload={"text": str(i)},
                payload_digest="b"*64, recipient_igsid=self.customer.igsid,
                provider_namespace="instagram_login:reset-owner", settings_id_snapshot=1,
                settings_permission_epoch=0, client_permission_epoch=9, revision_snapshot_digest="c"*64,
                publication_id=1, publication_version=1, publication_hash="d"*64,
                authority_context_digest="e"*64, state=state,
                provider_message_id="provider-sent" if state=="sent" else "")
        job = IgConversationAnalysisJob.objects.create(client=self.customer, watermark_message_id=self.message.pk,
            due_at=now, next_attempt_at=now, status=IgConversationAnalysisJob.Status.PROCESSING, lease_token="old")
        result = self.reset()
        revision.refresh_from_db()
        turn.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(result["cancelled_delivery_effects"], 2)
        self.assertEqual(list(revision.delivery_effects.order_by("order_index").values_list("state", flat=True)),
                         ["cancelled", "cancelled", "provider_started", "sent", "unknown"])
        self.assertEqual(revision.state, "superseded")
        self.assertIsNone(revision.active_slot)
        self.assertIsNone(revision.recovery_due_at)
        self.assertEqual(revision.action_receipts, {"source_actions": {"kept": True}})
        self.assertEqual(turn.terminal_reason, "superseded")
        self.assertEqual(job.status, "skipped")
        self.assertEqual(job.lease_token, "")

    def test_terminal_revision_active_head_also_releases_without_rewriting_receipts(self):
        now = timezone.now()
        turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.message,
            window_started_at=now, window_deadline=now, claim_state="processed", terminal_reason="send_unknown")
        revision = IgCustomerTurnRevision.objects.create(client=self.customer, turn=turn, revision=1,
            state="processed", quiet_started_at=now, quiet_deadline=now, quiet_cap_at=now,
            overall_deadline=now, recovery_state="pending", recovery_due_at=now)
        self.reset()
        revision.refresh_from_db()
        turn.refresh_from_db()
        self.assertEqual(revision.state, "processed")
        self.assertIsNone(revision.active_slot)
        self.assertEqual(turn.terminal_reason, "send_unknown")

    def test_preaccepted_old_stop_withdraws_consent_without_context_or_clock_changes(self):
        from management.models import IgFollowUpTask, InstagramBotLog

        before_clock = self.customer.last_user_message_at
        old_time = timezone.now()-timedelta(days=1)
        row = self.inbox(timestamp=old_time, text="stop")
        task = IgFollowUpTask.objects.create(client=self.customer, due_at=timezone.now(), kind="qualification")
        with patch("management.services.instagram_bot._enqueue_memory_source_event", side_effect=AssertionError("no memory")):
            result = self.reset()
        self.customer.refresh_from_db()
        task.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(self.customer.paused_reason, "opt_out")
        self.assertTrue(self.customer.bot_paused)
        self.assertEqual(self.customer.last_user_message_at, before_clock)
        self.assertAlmostEqual(self.customer.opted_out_at.timestamp(), old_time.timestamp(), places=2)
        self.assertEqual(result["reply_permission_epoch"], self.customer.reply_permission_epoch)
        self.assertGreater(self.customer.reply_permission_epoch, 10)
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(row.reason, "fresh_conversation_reset_history")
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.customer).count(), 1)
        receipt = json.loads(InstagramBotLog.objects.get(event="fresh_reset_historical_opt_out").detail)
        self.assertEqual(receipt["inbox_event_id"], row.pk)
        self.assertEqual(receipt["payload_digest"], row.payload_digest)
        self.assertEqual(receipt["outcome"], "opt_out_retained")
        self.assertNotIn("stop", json.dumps(receipt))

    def test_full_reset_retires_automatic_followup_but_retains_manager_service_task(self):
        from management.models import IgFollowUpTask

        automatic = IgFollowUpTask.objects.create(client=self.customer, due_at=timezone.now(), kind="qualification")
        service = IgFollowUpTask.objects.create(client=self.customer, due_at=timezone.now(),
            kind="manager_task", reason="existing_paid_order_service")
        result = self.reset()
        automatic.refresh_from_db()
        service.refresh_from_db()
        self.assertEqual(result["cancelled_followups"], 1)
        self.assertEqual(automatic.status, "cancelled")
        self.assertEqual(service.status, "pending")

    def test_old_stop_does_not_override_newer_explicit_opt_in(self):
        self.customer.opted_in_at = timezone.now()-timedelta(hours=1)
        self.customer.bot_paused = False
        self.customer.manager_takeover = False
        self.customer.save(update_fields=["opted_in_at", "bot_paused", "manager_takeover"])
        self.inbox(timestamp=timezone.now()-timedelta(days=1), text="stop")
        self.reset()
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.opted_out_at)
        self.assertFalse(self.customer.bot_paused)
        self.assertEqual(self.customer.reply_permission_epoch, 10)

    def test_unknown_clock_preaccepted_stop_conservatively_withdraws_consent(self):
        self.inbox(text="stop")
        self.reset()
        self.customer.refresh_from_db()
        self.assertIsNotNone(self.customer.opted_out_at)
        self.assertEqual(self.customer.paused_reason, "opt_out")

    def test_late_old_start_never_resumes_and_known_old_stop_keeps_newer_withdrawal(self):
        from management.services.ig_funnel_reset import handle_historical_reset_ingress

        newer_stop = timezone.now()-timedelta(hours=1)
        self.customer.opted_out_at = newer_stop
        self.customer.save(update_fields=["opted_out_at"])
        self.reset()
        kwargs = dict(client=self.customer, provider_created_at=timezone.now()-timedelta(days=1),
                      provider_namespace="instagram_login:reset-owner")
        self.assertTrue(handle_historical_reset_ingress(text="start", mid="old-start", **kwargs))
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.bot_paused)
        self.assertEqual(self.customer.opted_out_at, newer_stop)
        self.assertTrue(handle_historical_reset_ingress(text="stop", mid="old-stop", **kwargs))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.opted_out_at, newer_stop)
        self.assertEqual(IgFunnelResetAudit.objects.count(), 1)
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.customer).count(), 1)

    def test_future_general_and_mixed_wishlist_messages_have_only_new_history(self):
        from management.services.instagram_bot import _build_history

        self.reset()
        new = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role=InstagramBotMessage.Role.USER, text="Привіт, що у вас є? Хочу худі та футболку, яка тканина?",
            provider_created_at=timezone.now(), mid="new-general-mixed")
        self.assertEqual(_build_history(self.customer.igsid), [{"role": "user", "text": new.text}])

    def test_empty_transcript_full_reset_also_hides_unwatermarked_legacy_analysis(self):
        from management.services.ig_analysis_materiality import current_analysis_snapshot

        empty = IgClient.objects.create(igsid="reset-empty-customer")
        snapshot = IgConversationAnalysisSnapshot.objects.create(client=empty,
            dedupe_key="reset-empty-analysis", score_band="qualified", interaction_type="size_fit_question")
        reset_funnel(client_id=empty.pk, actor=self.manager, mode="fresh_conversation")
        with override_settings(IG_ANALYSIS_MATERIALITY_MODE="off"):
            self.assertIsNone(current_analysis_snapshot(empty))
            self.assertIsNone(current_analysis_snapshot(empty, candidates=[snapshot]))

    def test_actual_enqueue_archives_old_context_and_stop_without_source_memory_or_clock_effects(self):
        from management.services.instagram_bot import enqueue_inbound

        self.reset()
        before_clock = self.customer.last_user_message_at
        old_time = timezone.now()-timedelta(days=1)
        with patch("management.services.instagram_bot._enqueue_memory_source_event", side_effect=AssertionError("no memory")), \
             patch("management.services.instagram_bot._observe_conversion_inbound", side_effect=AssertionError("no conversation")):
            for text in ("Який розмір?", "start", "stop"):
                self.assertFalse(enqueue_inbound(self.settings, sender_id=self.customer.igsid,
                    text=text, mid=f"old-native-{len(text)}", received_at=old_time, persistence_only=True))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.last_user_message_at, before_clock)
        self.assertEqual(self.customer.paused_reason, "opt_out")
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.customer).count(), 1)

    def test_actual_enqueue_accepts_new_general_mixed_question_once_in_fresh_episode(self):
        from management.services.instagram_bot import enqueue_inbound, _build_history

        self.customer.bot_paused = False
        self.customer.manager_takeover = False
        self.customer.save(update_fields=["bot_paused", "manager_takeover"])
        self.reset()
        text = "Вітаю, які є худі та футболки і яка тканина?"
        args = dict(sender_id=self.customer.igsid, text=text, mid="new-native-mixed",
                    received_at=timezone.now()+timedelta(milliseconds=1), persistence_only=True)
        with patch("management.services.instagram_bot.gemini_generate", side_effect=AssertionError("no model")), \
             patch("management.services.instagram_bot.send_text", side_effect=AssertionError("no send")):
            self.assertTrue(enqueue_inbound(self.settings, **args))
            self.assertFalse(enqueue_inbound(self.settings, **args))
        new = InstagramBotMessage.objects.get(mid="new-native-mixed")
        self.customer.refresh_from_db()
        self.assertEqual(new.status, InstagramBotMessage.Status.PENDING)
        self.assertEqual(new.primary_customer_turn.episode_id, self.customer.current_commercial_episode_id)
        self.assertEqual(_build_history(self.customer.igsid), [{"role": "user", "text": text}])
        self.assertNotIn("_profile", self.customer.sales_context)

    def test_actual_enqueue_known_old_history_mid_cannot_promote_without_new_provider_clock(self):
        from management.services.instagram_bot import enqueue_inbound

        self.customer.bot_paused = False
        self.customer.manager_takeover = False
        self.customer.save(update_fields=["bot_paused", "manager_takeover"])
        self.message.source = "manual_refresh"
        self.message.provider_created_at = timezone.now()-timedelta(days=1)
        self.message.save(update_fields=["source", "provider_created_at"])
        self.reset()
        with patch("management.services.instagram_bot._promote_manual_refresh_message", side_effect=AssertionError("no promotion")):
            self.assertFalse(enqueue_inbound(self.settings, sender_id=self.customer.igsid,
                text=self.message.text, mid=self.message.mid, persistence_only=True))
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.customer).count(), 1)

    def test_native_old_referral_is_fenced_before_attribution_mutation(self):
        from management.services.instagram_bot import handle_webhook_payload

        row = self.inbox(timestamp=timezone.now()-timedelta(days=1))
        row.payload["entry"][0]["messaging"][0]["referral"] = {
            "ref": "old-referral-must-not-return", "ad_id": "old-ad-must-not-return",
        }
        self.reset()
        with patch("management.services.instagram_bot._apply_referral", side_effect=AssertionError("no old attribution")) as referral:
            self.assertEqual(handle_webhook_payload(self.settings, row.payload, persistence_only=True), 0)
        referral.assert_not_called()
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.ad_ref, "")

    def test_reset_between_inbox_precheck_and_native_handler_keeps_unknown_clock_new_mid_archived(self):
        from management.services import instagram_bot as bot
        from management.services.ig_webhook_inbox import drain_webhook_inbox

        row = self.inbox(text="Old raw event without a provider clock")
        original_payload = row.payload
        native = bot.handle_webhook_payload
        original_clock = self.customer.last_user_message_at
        resets = []

        def reset_then_handle(*args, **kwargs):
            # SQLite has no SKIP LOCKED row ownership. Suppress the reset's
            # eager archive to reproduce a row already held by the drainer.
            with patch("management.services.ig_webhook_inbox.archive_pre_reset_inbox", return_value=0):
                resets.append(self.reset())
            return native(*args, **kwargs)

        with patch("management.services.instagram_bot.handle_webhook_payload", side_effect=reset_then_handle):
            self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
        self.assertEqual(len(resets), 1)
        self.customer.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(self.customer.last_user_message_at, original_clock)
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.customer).count(), 1)
        self.assertEqual(row.reason, "fresh_conversation_reset_history")
        self.assertEqual(row.payload, original_payload)

    def test_reset_between_inbox_precheck_and_handler_does_not_restore_old_referral(self):
        from management.services import instagram_bot as bot
        from management.services.ig_webhook_inbox import drain_webhook_inbox

        row = self.inbox(timestamp=timezone.now()-timedelta(days=1))
        event = row.payload["entry"][0]["messaging"][0]
        event["referral"] = {"ref": "old-race-ref", "ad_id": "old-race-ad"}
        # This receipt's canonical digest is the accepted source identity.
        from management.services.ig_webhook_inbox import _canonical
        row.payload_digest = hashlib.sha256(_canonical(row.payload)).hexdigest()
        row.save(update_fields=["payload", "payload_digest"])
        native = bot.handle_webhook_payload

        def reset_then_handle(*args, **kwargs):
            with patch("management.services.ig_webhook_inbox.archive_pre_reset_inbox", return_value=0):
                self.reset()
            return native(*args, **kwargs)

        with patch("management.services.instagram_bot.handle_webhook_payload", side_effect=reset_then_handle):
            self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.ad_ref, "")
        self.assertEqual(self.customer.ad_id, "")
        self.assertEqual(self.customer.referral_payload, {})

    def test_native_inbox_identity_rejects_modified_payload_and_wrong_owner_before_mutation(self):
        from copy import deepcopy
        from management.services.instagram_bot import handle_webhook_payload

        row = self.inbox()
        altered = deepcopy(row.payload)
        altered["entry"][0]["messaging"][0]["message"]["text"] = "Borrowed receipt text"
        foreign = self.inbox(namespace="instagram_login:foreign-reset-owner")
        with patch("management.services.instagram_bot._apply_referral", side_effect=AssertionError("no attribution")), \
             patch("management.services.instagram_bot.enqueue_inbound", side_effect=AssertionError("no source")):
            for payload, event_id in ((altered, row.pk), (row.payload, foreign.pk), (foreign.payload, foreign.pk)):
                with self.assertRaisesMessage(ValueError, "inbox_source_unproven"):
                    handle_webhook_payload(self.settings, payload, persistence_only=True, _inbox_event_id=event_id)
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.customer).count(), 1)

    def test_new_post_reset_raw_event_keeps_its_own_live_referral_and_dedupes(self):
        from management.services.ig_webhook_inbox import _canonical, drain_webhook_inbox

        self.reset()
        row = self.inbox(timestamp=timezone.now()+timedelta(seconds=1), text="Нове питання про худі")
        row.payload["entry"][0]["messaging"][0]["referral"] = {"ref": "new-live-ref", "ad_id": "new-live-ad"}
        row.payload_digest = hashlib.sha256(_canonical(row.payload)).hexdigest()
        row.save(update_fields=["payload", "payload_digest"])
        mid = row.payload["entry"][0]["messaging"][0]["message"]["mid"]
        self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
        self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 0)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.ad_ref, "new-live-ref")
        self.assertEqual(self.customer.ad_id, "new-live-ad")
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.customer, mid=mid).count(), 1)

    def test_precheck_to_native_materialization_excludes_another_client_lock_owner(self):
        import threading
        from management.services import instagram_bot as bot
        from management.services import ig_webhook_inbox as inbox
        from management.services.ig_commercial_episodes import commercial_episode_client_lock

        self.inbox(text="A live unknown-clock inbound")
        native_archive = inbox._archive_historical_customer_event
        native_handler = bot.handle_webhook_payload
        attempting = threading.Event()
        entered = threading.Event()
        worker = None
        checks = []

        def competing_owner():
            # Only contend for the real local client lock; no second-thread
            # database work or claim of MariaDB transaction behavior.
            attempting.set()
            with commercial_episode_client_lock(self.customer.pk):
                entered.set()

        def checked_archive(*args, **kwargs):
            nonlocal worker
            result = native_archive(*args, **kwargs)
            if worker is None:
                worker = threading.Thread(target=competing_owner, daemon=True)
                worker.start()
                self.assertTrue(attempting.wait(timeout=1))
                self.assertFalse(entered.wait(timeout=0.1))
            checks.append("archive")
            return result

        def checked_native(*args, **kwargs):
            self.assertFalse(entered.is_set())
            checks.append("native")
            return native_handler(*args, **kwargs)

        try:
            with patch.object(inbox, "_archive_historical_customer_event", side_effect=checked_archive), \
                 patch.object(bot, "handle_webhook_payload", side_effect=checked_native):
                self.assertEqual(inbox.drain_webhook_inbox(self.settings, limit=1), 1)
            self.assertTrue(entered.wait(timeout=1))
            self.assertEqual(checks, ["archive", "native", "archive"])
        finally:
            if worker is not None:
                worker.join(timeout=1)

    def test_archived_stop_postback_retains_consent_but_start_postback_does_not_resume(self):
        from management.services.ig_webhook_inbox import _canonical, drain_webhook_inbox

        stop = self.inbox(timestamp=timezone.now()-timedelta(days=1))
        stop_event = stop.payload["entry"][0]["messaging"][0]
        stop_event["postback"] = {"mid": stop_event["message"]["mid"], "title": "stop", "payload": "stop"}
        del stop_event["message"]
        stop.payload_digest = hashlib.sha256(_canonical(stop.payload)).hexdigest()
        stop.save(update_fields=["payload", "payload_digest"])
        self.reset()
        self.customer.refresh_from_db()
        original_clock = self.customer.last_user_message_at
        self.assertEqual(self.customer.paused_reason, "opt_out")
        self.assertIsNotNone(self.customer.opted_out_at)
        start = self.inbox(timestamp=timezone.now()-timedelta(days=1))
        start_event = start.payload["entry"][0]["messaging"][0]
        start_event["postback"] = {"mid": start_event["message"]["mid"], "title": "start", "payload": "start"}
        del start_event["message"]
        start.payload_digest = hashlib.sha256(_canonical(start.payload)).hexdigest()
        start.save(update_fields=["payload", "payload_digest"])
        self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.bot_paused)
        self.assertEqual(self.customer.paused_reason, "opt_out")
        self.assertIsNone(self.customer.opted_in_at)
        self.assertEqual(self.customer.last_user_message_at, original_clock)
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.customer).count(), 1)

    def test_archive_clock_uses_native_seconds_format_without_treating_new_inbound_as_old(self):
        from management.services.ig_webhook_inbox import _canonical, drain_webhook_inbox

        self.reset()
        row = self.inbox(timestamp=timezone.now()+timedelta(seconds=1), text="Новий запит")
        event = row.payload["entry"][0]["messaging"][0]
        event["timestamp"] = int(event["timestamp"] / 1000)
        row.payload_digest = hashlib.sha256(_canonical(row.payload)).hexdigest()
        row.save(update_fields=["payload", "payload_digest"])
        self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
        row.refresh_from_db()
        self.assertNotEqual(row.reason, "fresh_conversation_reset_history")
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.customer,
            mid=event["message"]["mid"]).count(), 1)


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
                   ALLOWED_HOSTS=["testserver"], IG_ASSISTED_CHECKOUT_V2="enforced",
                   IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class FreshResetCheckoutLinksTests(HostedSettlementFixture, TestCase):
    def reset(self):
        with patch("storefront.views.monobank._monobank_api_request", side_effect=AssertionError("reset must not contact bank")):
            return reset_funnel(client_id=self.client_row.pk, actor=None, mode="fresh_conversation")

    def token(self, proposal):
        from management.models import IgCheckoutAccessToken

        raw, token = IgCheckoutAccessToken.issue(proposal=proposal)
        url = reverse("ig_checkout_token_entry", kwargs={"token": raw})
        self.assertEqual(self.client.get(url).status_code, 302)
        return url, token

    def test_full_reset_revokes_unpaid_url_and_existing_browser_grant_before_payment(self):
        from management.models import IgCheckoutInventoryReservation

        proposal = self.proposal()
        url, token = self.token(proposal)
        hold = IgCheckoutInventoryReservation.objects.create(proposal=proposal, product=self.product,
            item=proposal.items.first(), quantity=1, reservation_fingerprint="fresh-reset-unstarted-hold",
            expires_at=timezone.now()+timedelta(hours=1))
        result = self.reset()
        with patch("storefront.views.monobank._monobank_api_request", side_effect=AssertionError("no old payment")) as bank:
            self.assertEqual(self.client.get(url).status_code, 410)
            self.assertEqual(self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}),
                {"expected_revision": proposal.revision, "expected_digest": proposal.items_digest},
                HTTP_ACCEPT="application/json").status_code, 404)
        bank.assert_not_called()
        proposal.refresh_from_db()
        token.refresh_from_db()
        hold.refresh_from_db()
        self.assertEqual(proposal.status, "revoked")
        self.assertIsNotNone(token.revoked_at)
        self.assertEqual(hold.state, "released")
        self.assertEqual(result["retired_checkout_proposals"], 1)
        self.assertFalse(proposal.deal.__class__.objects.get(pk=proposal.deal_id).active_checkout_proposal_id)

    def test_lightweight_reset_keeps_existing_checkout_token_contract(self):
        proposal = self.proposal()
        url, token = self.token(proposal)
        reset_funnel(client_id=self.client_row.pk, actor=None)
        token.refresh_from_db()
        self.assertIsNone(token.revoked_at)
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_new_fresh_episode_can_issue_a_new_offer_without_reusing_old_url(self):
        from management.services.ig_checkout_readiness import _active_deal_state

        old = self.proposal()
        old_url, _token = self.token(old)
        self.reset()
        self.client_row.refresh_from_db()
        self.assertIsNone(_active_deal_state(self.client_row)["deal_id"])
        new = self.proposal()
        new_url, _token = self.token(new)
        self.assertNotEqual(new.pk, old.pk)
        self.assertEqual(new.commercial_episode_id, self.client_row.current_commercial_episode_id)
        self.assertNotEqual(new_url, old_url)
        self.assertEqual(self.client.get(old_url).status_code, 410)
        self.assertEqual(self.client.get(new_url).status_code, 302)

    def test_provider_started_invoice_is_preserved_for_review_but_local_link_is_revoked(self):
        from management.models import IgCheckoutInvoiceGeneration, IgCheckoutInventoryReservation

        proposal = self.proposal()
        attempt = self.invoice(proposal, "fresh-reset-existing-bank-invoice")
        generation = IgCheckoutInvoiceGeneration.objects.get(payment_attempt=attempt)
        url, token = self.token(proposal)
        hold = IgCheckoutInventoryReservation.objects.create(proposal=proposal, invoice_generation=generation,
            product=self.product, item=proposal.items.first(), quantity=1,
            reservation_fingerprint="fresh-reset-protected-bank-hold", expires_at=timezone.now()+timedelta(hours=1))
        before = (attempt.status, attempt.monobank_invoice_id, attempt.invoice_url, attempt.event_state,
                  generation.state, generation.provider_request_digest)
        result = self.reset()
        attempt.refresh_from_db()
        generation.refresh_from_db()
        proposal.refresh_from_db()
        hold.refresh_from_db()
        self.assertEqual((attempt.status, attempt.monobank_invoice_id, attempt.invoice_url, attempt.event_state,
                          generation.state, generation.provider_request_digest), before)
        self.assertEqual(hold.state, "active")
        self.assertEqual(proposal.status, "manager_review")
        self.assertEqual(result["checkout_payment_review"], 1)
        self.assertEqual(self.client.get(url).status_code, 410)
        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)

    def test_paid_receipt_and_order_remain_accessible_as_history(self):
        from management.models import IgPaymentEvent, IgPaymentProjection

        proposal, attempt, order = self.paid(invoice_id="fresh-reset-paid-history")
        url, token = self.token(proposal)
        before_events = list(IgPaymentEvent.objects.values_list("pk", flat=True))
        before_truth = IgPaymentProjection.objects.get(deal=proposal.deal).truth
        result = self.reset()
        proposal.refresh_from_db()
        attempt.refresh_from_db()
        order.refresh_from_db()
        token.refresh_from_db()
        self.assertEqual(proposal.status, "paid")
        self.assertIsNone(token.revoked_at)
        self.assertEqual(order.payment_status, "paid")
        self.assertEqual(attempt.order_id, order.pk)
        self.assertEqual(list(IgPaymentEvent.objects.values_list("pk", flat=True)), before_events)
        self.assertEqual(IgPaymentProjection.objects.get(deal=proposal.deal).truth, before_truth)
        self.assertEqual(result["preserved_paid_checkout_proposals"], 1)
        opened = self.client.get(url)
        self.assertEqual(opened.status_code, 302)
        self.assertEqual(self.client.get(opened["Location"]).status_code, 200)

    def test_pre_dispatch_generation_uses_native_cancellation_proof_and_cannot_dispatch_after_reset(self):
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory
        from management.tests_ig_checkout_generation_runtime import CheckoutGenerationRuntimeTests
        from management.services.ig_checkout_generation import _prepare_generation, _persist_provider_dispatch_evidence
        from management.services.ig_checkout_payment import CheckoutPaymentError

        proposal = self.proposal()
        CheckoutGenerationRuntimeTests._open(self, proposal)
        request = RequestFactory().post("/fresh-reset-pre-dispatch/")
        SessionMiddleware(lambda _request: None).process_request(request)
        request.session.save()
        request.user = AnonymousUser()
        _locked, generation, attempt, _values, reused = _prepare_generation(proposal, request=request,
            payload=CheckoutGenerationRuntimeTests._delivery_payload(self))
        self.assertFalse(reused)
        self.assertFalse(generation.provider_request_digest)
        self.assertFalse(attempt.monobank_invoice_id)
        result = self.reset()
        generation.refresh_from_db()
        attempt.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(result["cancelled_checkout_attempts"], 1)
        self.assertEqual(generation.state, "cancelled")
        self.assertEqual(attempt.status, "cancelled")
        self.assertEqual(attempt.event_state["provider_boundary"]["state"], "cancelled_pre_dispatch")
        self.assertEqual(proposal.status, "revoked")
        with self.assertRaises(CheckoutPaymentError):
            _persist_provider_dispatch_evidence(attempt.pk, {"amount": 90000})

    def test_legacy_bank_evidence_on_deal_without_attempt_stays_review_not_released(self):
        from management.models import IgCheckoutInventoryReservation

        proposal = self.proposal()
        proposal.deal.invoice_id = "old-legacy-bank-invoice"
        proposal.deal.invoice_url = "https://pay.example/old-legacy-bank-invoice"
        proposal.deal.save(update_fields=["invoice_id", "invoice_url"])
        url, _token = self.token(proposal)
        hold = IgCheckoutInventoryReservation.objects.create(proposal=proposal, product=self.product,
            item=proposal.items.first(), quantity=1, reservation_fingerprint="fresh-reset-legacy-bank-hold",
            expires_at=timezone.now()+timedelta(hours=1))
        result = self.reset()
        proposal.refresh_from_db()
        hold.refresh_from_db()
        self.assertEqual(proposal.status, "manager_review")
        self.assertEqual(result["checkout_payment_review"], 1)
        self.assertEqual(hold.state, "active")
        self.assertEqual(self.client.get(url).status_code, 410)

    def test_unpaid_proposal_cannot_borrow_deal_paid_history_to_keep_old_token(self):
        proposal = self.proposal()
        url, token = self.token(proposal)
        proposal.deal.paid_amount = Decimal("900.00")
        proposal.deal.payment_truth = "confirmed"
        proposal.deal.save(update_fields=["paid_amount", "payment_truth"])
        result = self.reset()
        proposal.refresh_from_db()
        proposal.deal.refresh_from_db()
        token.refresh_from_db()
        self.assertEqual(proposal.status, "manager_review")
        self.assertEqual(proposal.deal.paid_amount, Decimal("900.00"))
        self.assertEqual(proposal.deal.payment_truth, "confirmed")
        self.assertIsNotNone(token.revoked_at)
        self.assertEqual(result["checkout_payment_review"], 1)
        self.assertEqual(self.client.get(url).status_code, 410)

    def test_orphan_legacy_unpaid_bank_link_is_reported_without_rewriting_its_money_record(self):
        from management.models import IgDeal

        deal = IgDeal.objects.create(client=self.client_row, invoice_id="legacy-orphan-reset",
            invoice_url="https://pay.example/legacy-orphan-reset", amount=Decimal("900.00"))
        result = self.reset()
        deal.refresh_from_db()
        self.assertEqual(result["checkout_payment_review"], 1)
        self.assertEqual(deal.invoice_id, "legacy-orphan-reset")
        self.assertEqual(deal.invoice_url, "https://pay.example/legacy-orphan-reset")
        self.assertEqual(deal.payment_truth, "unverified")

    def test_already_expired_proposal_history_is_not_reopened_by_full_reset(self):
        proposal = self.proposal()
        url, token = self.token(proposal)
        proposal.status = "expired"
        proposal.save(update_fields=["status", "updated_at"])
        self.reset()
        proposal.refresh_from_db()
        token.refresh_from_db()
        self.assertEqual(proposal.status, "expired")
        self.assertIsNotNone(token.revoked_at)
        self.assertEqual(self.client.get(url).status_code, 410)

    def test_financial_busy_rolls_back_every_earlier_context_token_and_hold_change(self):
        from management.models import IgCheckoutInventoryReservation, IgDeal
        from management.services import ig_funnel_reset as reset_service

        first = self.proposal()
        _url, first_token = self.token(first)
        hold = IgCheckoutInventoryReservation.objects.create(proposal=first, product=self.product,
            item=first.items.first(), quantity=1, reservation_fingerprint="fresh-reset-rollback-hold",
            expires_at=timezone.now()+timedelta(hours=1))
        reset_funnel(client_id=self.client_row.pk, actor=None)  # Real older, separate purchase cycle.
        self.client_row.refresh_from_db()
        self.proposal()
        self.client_row.sales_context = {"reset_busy_sentinel": "keep"}
        self.client_row.save(update_fields=["sales_context"])
        before = IgClient.objects.filter(pk=self.client_row.pk).values(
            "reply_permission_epoch", "sales_context", "current_commercial_episode_id", "stage").get()
        audit_count = IgFunnelResetAudit.objects.count()
        locks = reset_service._reset_financial_rows
        deals = 0

        def busy_on_second_financial_parent(queryset):
            nonlocal deals
            if queryset.model is IgDeal:
                deals += 1
                if deals == 2:
                    raise OperationalError(1205, "NOWAIT financial row is busy")
            return locks(queryset)

        with patch.object(reset_service, "_reset_financial_rows", side_effect=busy_on_second_financial_parent):
            result = self.reset()
        self.assertEqual((result["ok"], result["status"], result["code"]), (False, 409, "reset_busy"))
        self.assertEqual(deals, 2)
        first.refresh_from_db()
        first_token.refresh_from_db()
        hold.refresh_from_db()
        self.assertEqual(first.status, "viewed")
        self.assertIsNone(first_token.revoked_at)
        self.assertEqual(hold.state, "active")
        self.assertEqual(IgFunnelResetAudit.objects.count(), audit_count)
        self.assertEqual(IgClient.objects.filter(pk=self.client_row.pk).values(*before).get(), before)

    def test_unrelated_database_error_is_not_reported_as_retryable_reset_busy(self):
        with patch("management.services.ig_funnel_reset._reset_financial_rows",
                   side_effect=OperationalError(1054, "unknown column")):
            with self.assertRaises(OperationalError):
                self.reset()

    @override_settings(ROOT_URLCONF="twocomms.urls_management")
    def test_api_returns_explicit_busy_409_without_advancing_epoch(self):
        operator = get_user_model().objects.create_superuser("busy-reset-operator", "busy@example.invalid", "x")
        self.client.force_login(operator)
        before = self.client_row.reply_permission_epoch
        with patch("management.services.ig_funnel_reset._reset_financial_rows",
                   side_effect=OperationalError(3572, "NOWAIT lock unavailable")):
            response = self.client.post(reverse("management_bot_client_reset_funnel_api", args=[self.client_row.pk]),
                {"confirm_reset": "1", "mode": "fresh_conversation", "confirm_fresh_conversation": "1",
                 "expected_permission_epoch": before})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "reset_busy")
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.reply_permission_epoch, before)
        self.assertFalse(IgFunnelResetAudit.objects.filter(client=self.client_row).exists())


@skipUnless(connection.vendor in {"mysql", "mariadb"} and connection.features.has_select_for_update_nowait,
            "Requires isolated MariaDB/MySQL NOWAIT row locks; SQLite is not concurrency proof")
@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
                   ALLOWED_HOSTS=["testserver"], IG_ASSISTED_CHECKOUT_V2="enforced",
                   IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class FreshResetFinancialLockRaceTests(HostedSettlementFixture, AnalysisPrivacyCleanupMixin, TransactionTestCase):
    def test_standard_callback_holding_deal_causes_atomic_busy_reset_then_can_settle(self):
        import threading
        import time
        from management.models import IgDeal
        from management.services.ig_checkout_generation import _lock_generation_graph
        from storefront.views.monobank import _apply_payment_attempt_status

        proposal = self.proposal()
        attempt = self.invoice(proposal, "fresh-reset-mariadb-lock-race")
        from management.models import IgCheckoutAccessToken
        _raw, token = IgCheckoutAccessToken.issue(proposal=proposal)
        self.client_row.refresh_from_db()
        self.client_row.sales_context = {"reset_busy_sentinel": "keep"}
        self.client_row.save(update_fields=["sales_context"])
        before = IgClient.objects.filter(pk=self.client_row.pk).values(
            "reply_permission_epoch", "sales_context", "current_commercial_episode_id", "stage").get()
        graph_locked, reset_returned = threading.Event(), threading.Event()
        errors, settlements = [], []

        def callback():
            close_old_connections()
            try:
                with transaction.atomic():
                    # Pause after the native graph's first Deal lock, before
                    # its joined Proposal/Client lock. Owning Client too would
                    # only exercise reset's initial NOWAIT, not the inversion.
                    IgDeal.objects.select_for_update().get(pk=proposal.deal_id)
                    graph_locked.set()
                    if not reset_returned.wait(timeout=5):
                        raise AssertionError("reset waited behind financial owner")
                    graph = _lock_generation_graph(attempt.pk)
                    if graph is None:
                        raise AssertionError("missing native invoice graph")
                    settlements.append(_apply_payment_attempt_status(graph[3], "success",
                        payload=self.payload(graph[3]), source="provider_pull"))
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        worker = threading.Thread(target=callback, daemon=True)
        worker.start()
        try:
            self.assertTrue(graph_locked.wait(timeout=5))
            started = time.monotonic()
            result = reset_funnel(client_id=self.client_row.pk, actor=None, mode="fresh_conversation",
                                  expected_permission_epoch=before["reply_permission_epoch"])
            self.assertLess(time.monotonic()-started, 2)
            self.assertEqual((result["ok"], result["status"], result["code"]), (False, 409, "reset_busy"))
            self.assertEqual(IgClient.objects.filter(pk=self.client_row.pk).values(*before).get(), before)
            token.refresh_from_db()
            self.assertIsNone(token.revoked_at)
            self.assertFalse(IgFunnelResetAudit.objects.filter(client=self.client_row).exists())
        finally:
            reset_returned.set()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(settlements), 1)
        order, created = settlements[0]
        self.assertTrue(created)
        self.assertIsNotNone(order)
        attempt.refresh_from_db()
        proposal.refresh_from_db()
        token.refresh_from_db()
        self.assertEqual(attempt.order_id, order.pk)
        self.assertEqual(proposal.status, "paid")
        self.assertIsNone(token.revoked_at)
