"""Fresh source service debt reaches actual promotion owners before analysis."""
from datetime import datetime, timedelta
import json
import os
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_service_complaints as complaint_fixtures
from management.models import IgClient, IgFollowUpTask, InstagramBotMessage, InstagramBotSettings
from management.services import bot_followups, bot_playbooks, ig_follow_cta, ig_ugc_rewards, instagram_bot
from management.services.ig_service_complaints import HOLD_REASON, promotion_service_hold_reason
from reviews.tests import test_purchase_review_incentive as purchase_fixtures


COMPLAINT = "You said shipping was 90 but I paid 120. My first impression is bad."


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class ServicePromotionOwnerTests(purchase_fixtures._PurchaseReviewFixture, TestCase):
    revision = complaint_fixtures.ServiceComplaintSourceGuardTests.revision
    task = complaint_fixtures.ServiceComplaintSourceGuardTests.task

    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        self.settings_row = InstagramBotSettings.load()
        self.settings_row.ig_user_id = "1"
        self.settings_row.save(update_fields=["ig_user_id", "updated_at"])
        self.customer = self.ig_client
        self.customer.language = "en"
        self.customer.primary_objection = "price"
        self.customer.save(update_fields=["language", "primary_objection"])
        self.serial = 0
        self.now = timezone.now()

    def message(self, text=COMPLAINT, **changes):
        self.serial += 1
        values = dict(client=self.customer, sender_id=self.customer.igsid, role="user", source="webhook",
            mid=f"promotion-service-source-{self.serial}", provider_namespace="instagram_login:1",
            provider_created_at=timezone.now(), text=text, status="pending")
        values.update(changes)
        return InstagramBotMessage.objects.create(**values)

    def assert_promotion_hold(self):
        self.assertEqual(promotion_service_hold_reason(self.customer), HOLD_REASON)
        self.assertEqual(ig_ugc_rewards.ugc_service_case_reason(self.customer), HOLD_REASON)
        allowed, reason = bot_followups._client_allows_followup(self.customer, kind="thinking")
        self.assertFalse(allowed)
        self.assertEqual(reason, "service_case_open")
        self.assertTrue(ig_follow_cta._has_post_sale_risk(self.customer, order=self.order))
        tags = bot_playbooks.tags_for_client(self.customer)
        self.assertTrue({"service", "service_complaint"}.issubset(tags))
        self.assertTrue({"sales", "discount", "price"}.isdisjoint(tags))
        guard = instagram_bot.automation_guardrails(self.customer)
        self.assertIn("POST-SALE SERVICE MODE", guard)
        self.assertNotIn("rescue", guard)
        self.assertNotIn("Клієнт уже купив", guard)
        from management.services.ig_post_purchase_invitation import post_purchase_invitation_block_reason

        self.assertEqual(post_purchase_invitation_block_reason(self.customer, self.order), "post_purchase_service_case_open")

    def test_owned_fresh_complaint_before_analysis_blocks_all_promotion_owners(self):
        from management.models import IgConversationAnalysisSnapshot, IgPostSaleCase

        self.message()
        self.assertFalse(IgConversationAnalysisSnapshot.objects.filter(client=self.customer).exists())
        self.assertFalse(IgPostSaleCase.objects.filter(client=self.customer).exists())
        with patch("management.services.instagram_bot._provider_http") as http, patch(
                "management.services.call_ai_analysis.gemini_generate_text") as gemini:
            self.assert_promotion_hold()
        http.assert_not_called()
        gemini.assert_not_called()

    def test_plain_delivery_price_question_and_no_issues_do_not_block_sales(self):
        for text in ("How much does shipping cost: 90 or 120?", "No issues, the shirt is not damaged",
                     "I'm not complaining about shipping", "Thank you, everything is fine"):
            with self.subTest(text=text):
                self.message(text)
                self.assertEqual(promotion_service_hold_reason(self.customer), "")
                self.assertEqual(ig_ugc_rewards.ugc_service_case_reason(self.customer), "")
                self.assertFalse(bot_followups._has_open_service_conversation(self.customer))
                self.assertFalse(ig_follow_cta._has_post_sale_risk(self.customer, order=self.order))
                self.assertIn("sales", bot_playbooks.tags_for_client(self.customer))
                self.assertEqual(instagram_bot.automation_guardrails(self.customer), instagram_bot.SALES_AUTOMATION_GUARDRAILS)

    def test_foreign_namespace_or_manager_complaint_does_not_create_promotion_hold(self):
        self.message(provider_namespace="instagram_login:someone-else")
        self.message(role="manager", sender_id="1")
        self.assertEqual(ig_ugc_rewards.ugc_service_case_reason(self.customer), "")
        self.assertIn("sales", bot_playbooks.tags_for_client(self.customer))

    def test_closing_another_case_does_not_clear_current_source_bound_complaint_debt(self):
        from management.models import IgPostSaleCase

        self.source = self.message()
        self.now = timezone.now()
        pending = self.task(self.revision(), status="pending")
        self.message("Thank you for explaining")
        self.message("We explained the carrier charge", role="manager", sender_id="1")
        IgPostSaleCase.objects.create(client=self.customer, order=self.order,
            source_message=self.evidence, case_type="exchange", status="completed", resolved_at=timezone.now())
        self.assert_promotion_hold()
        pending.refresh_from_db()
        self.assertEqual(pending.status, "pending")

    def test_new_complaint_holds_real_owed_reward_without_new_grant_or_expiry_change(self):
        from management.models import IgUgcReward, IgUgcRewardLifetime

        reward = self._award()
        expiry = reward.promo_code.valid_until
        delivery = reward.deliveries.get(generation=1)
        before = (IgUgcReward.objects.count(), IgUgcRewardLifetime.objects.count(), reward.promo_code.code)
        self.message()
        with patch("management.services.instagram_bot.send_text") as send, patch(
                "management.services.instagram_bot._provider_http") as http:
            state = ig_ugc_rewards.process_external_ugc_reward_delivery(delivery.pk)
        self.assertIn(state, {"failed", "waiting_window"})
        send.assert_not_called()
        http.assert_not_called()
        reward.promo_code.refresh_from_db()
        self.assertEqual(reward.promo_code.valid_until, expiry)
        self.assertEqual((IgUgcReward.objects.count(), IgUgcRewardLifetime.objects.count(), reward.promo_code.code), before)

    def _reward_boundary_race(self, *, partial):
        from management.services.instagram_bot import ProviderDeliveryReceipt

        reward = self._award()
        expiry = reward.promo_code.valid_until
        delivery = reward.deliveries.get(generation=1)
        ids = ("mid.reward-before-service-hold",) if partial else ()
        def transport(_settings, _recipient, text, **kwargs):
            self.assertEqual(text, delivery.message_snapshot)
            factory = kwargs["provider_request_boundary_factory"]
            if partial:
                with factory(delivered_chunk_count=0, provider_message_ids=(), planned_chunk_count=2) as allowed:
                    self.assertTrue(allowed)
                    kwargs["provider_message_callback"](ids[0])
            self.message()
            with factory(delivered_chunk_count=int(partial), provider_message_ids=ids,
                    planned_chunk_count=2 if partial else 1) as allowed:
                self.assertFalse(allowed)
            return ProviderDeliveryReceipt(False, "cancelled", provider_message_ids=ids,
                delivered_chunk_count=int(partial), planned_chunk_count=2 if partial else 1)
        with patch("management.services.instagram_bot.send_text", side_effect=transport) as send, patch(
                "management.services.instagram_bot._provider_http") as http:
            state = ig_ugc_rewards.process_external_ugc_reward_delivery(delivery.pk)
        send.assert_called_once()
        http.assert_not_called()
        self.assertEqual(state, "ambiguous" if partial else "waiting_window")
        delivery.refresh_from_db()
        self.assertEqual(delivery.provider_message_ids, list(ids))
        reward.promo_code.refresh_from_db()
        self.assertEqual(reward.promo_code.valid_until, expiry)
        if partial:
            with patch("management.services.instagram_bot.send_text") as again:
                self.assertEqual(ig_ugc_rewards.process_external_ugc_reward_delivery(delivery.pk), "ambiguous")
            again.assert_not_called()

    def test_reward_claim_then_complaint_blocks_first_socket(self):
        self._reward_boundary_race(partial=False)

    def test_partial_reward_mid_then_complaint_remains_ambiguous_without_replay(self):
        self._reward_boundary_race(partial=True)


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class GenericFollowupServiceBoundaryTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.now = datetime(2026, 10, 9, 11, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
        clock = patch("django.utils.timezone.now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        self.settings_row = InstagramBotSettings.objects.create(pk=1, is_enabled=True, ig_user_id="1")
        self.customer = IgClient.objects.create(igsid="generic-promotion-customer", language="en",
            last_user_message_at=self.now)
        self.message("Thanks for the information")
        self.task_row = IgFollowUpTask.objects.create(client=self.customer, kind="thinking", reason="legacy_sales_check",
            due_at=self.now, message_text="Here is the information about the next purchase. " * 35)

    def message(self, text):
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", status="pending", text=text,
            mid=f"generic-promotion-source-{InstagramBotMessage.objects.count()}",
            provider_namespace="instagram_login:1", provider_created_at=self.now)

    def test_claim_then_new_complaint_blocks_legacy_socket_before_any_http(self):
        real_send = instagram_bot.send_text
        def prepare_then_complaint(*args, **kwargs):
            self.message(COMPLAINT)
            return real_send(*args, **kwargs)
        with patch.object(instagram_bot, "send_text", side_effect=prepare_then_complaint), patch.object(
                instagram_bot, "get_page_token", return_value="fixture-token"), patch.object(instagram_bot, "_provider_http") as http:
            self.assertEqual(bot_followups.process_due_followups(self.settings_row, now=self.now), 0)
        http.assert_not_called()
        self.task_row.refresh_from_db()
        self.assertEqual((self.task_row.status, self.task_row.skip_reason), ("skipped", HOLD_REASON))
        self.assertEqual(self.task_row.provider_message_id, "")

    def test_actual_first_chunk_mid_then_complaint_blocks_second_http_and_replay(self):
        def first_http(*args, **kwargs):
            payload = json.loads(kwargs["data"].decode())
            self.assertEqual(payload["recipient"]["id"], self.customer.igsid)
            self.message(COMPLAINT)
            return 200, '{"message_id":"mid.followup-before-service-hold"}'
        with patch.object(instagram_bot, "get_page_token", return_value="fixture-token"), patch.object(
                instagram_bot, "_provider_http", side_effect=first_http) as http:
            self.assertEqual(bot_followups.process_due_followups(self.settings_row, now=self.now), 0)
        http.assert_called_once()
        self.task_row.refresh_from_db()
        self.assertEqual(self.task_row.status, "ambiguous")
        self.assertEqual(self.task_row.provider_message_id, "mid.followup-before-service-hold")
        self.assertTrue(IgFollowUpTask.objects.filter(delivery_review_for=self.task_row, kind="manager_task").exists())
        with patch.object(instagram_bot, "_provider_http") as again:
            self.assertEqual(bot_followups.process_due_followups(self.settings_row, now=self.now), 0)
        again.assert_not_called()


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class FollowCtaServiceProviderBoundaryTests(TestCase):
    def setUp(self):
        cache.clear()
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = timezone.now()
        self.settings_row = InstagramBotSettings.objects.create(pk=1, is_enabled=True, ig_user_id="1")
        self.customer = IgClient.objects.create(igsid="service-cta-customer", language="en",
            stage="payment_pending", last_user_message_at=self.now)
        from management.models import IgCommercialEpisode, IgFollowState
        from management.services.ig_follow_state import configuration_fingerprint

        self.episode = IgCommercialEpisode.objects.create(client=self.customer, sequence=1,
            materialization_key="service-cta-episode")
        self.customer.current_commercial_episode = self.episode
        self.customer.save(update_fields=["current_commercial_episode"])
        self.source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", status="done", mid="service-cta-inbound",
            provider_namespace="instagram_login:1", provider_created_at=self.now,
            text="Thank you for the information")
        IgFollowState.objects.create(client=self.customer, state="not_following", revision=3,
            source="instagram_login", config_fingerprint=configuration_fingerprint(self.settings_row),
            observed_at=self.now - timedelta(minutes=2), expires_at=self.now + timedelta(hours=1), last_result="known")

    def test_reserved_optional_cta_rechecks_fresh_owned_complaint_at_actual_provider_boundary(self):
        from management.models import IgFollowCtaDecision

        opportunity = ig_follow_cta.evaluate_follow_opportunity(client=self.customer,
            opportunity=IgFollowCtaDecision.Opportunity.PAYMENT, episode=self.episode,
            source_message=self.source, base_text="Thank you, we will keep you informed.", now=self.now)
        self.assertTrue(opportunity.allowed, opportunity.reason_codes)
        decision = ig_follow_cta.prepare_follow_decision(opportunity,
            candidate_text="If you like our approach, we would be happy to see you among our followers.")
        self.assertEqual(decision.state, "prepared", decision.reason_codes)
        authorized = ig_follow_cta.authorize_follow_cta(decision.pk,
            current_base_text=decision.base_text, now=self.now)
        self.assertIsNotNone(authorized)
        self.source.text = COMPLAINT
        self.source.save(update_fields=["text"])
        with patch("management.services.instagram_bot._provider_http") as http:
            with ig_follow_cta.follow_provider_request_boundary(authorized, now=self.now) as allowed:
                self.assertFalse(allowed)
        http.assert_not_called()
        decision.refresh_from_db()
        self.assertEqual(decision.state, "cancelled")
        self.assertEqual(decision.suppression_reason, "post_sale_risk")
        self.assertIsNone(decision.provider_io_started_at)
