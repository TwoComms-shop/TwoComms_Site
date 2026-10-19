from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from management.models import (
    IgClient, IgCommercialEpisode, IgDeal, IgFollowUpTask, IgFunnelStepEvent,
    IgPaymentConfirmationReview, IgPaymentEvent, IgPaymentProjection,
    IgPaymentReviewDecision, InstagramBotMessage,
)
from management.services.bot_payment_truth import confirmed_purchase_units
from management.services.ig_conversion_cohorts import (
    ATTRIBUTION_KEY, OBSERVATION_KEY, build_conversion_cohort_report,
    client_purchase_metrics,
    record_commercial_inquiry_observation_in_transaction,
)
from management.services.ig_funnel_analytics import build_funnel_analytics, record_client_step_event
from orders.models import Order


class ConversionMetricsTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.entry = self.now - timedelta(days=20)
        self.lead = IgClient.get_or_create_for_sender("metrics-client")

    def inquiry(self, client=None, *, at=None, history="verified_new", classification="retail", ad_id="A", evidence=None, status="failed"):
        client = client or self.lead
        row = InstagramBotMessage.objects.create(
            client=client, sender_id=client.igsid, role="user", text="Ціна?",
            provider_created_at=at or self.entry, status=status,
        )
        with transaction.atomic():
            event = record_commercial_inquiry_observation_in_transaction(
                client, source_message_id=row.pk, classification=classification,
                history_status=history,
                evidence=evidence if evidence is not None else {"history_complete": True, "identity_evidence_ref": "account-history:test", "classification_basis": "own_commercial_request"},
                attribution={"quality": "known", "ad_id": ad_id, "source": "ads", "source_message_ids": [row.pk]},
            )
        return event

    def order(self, client=None, *, key="one", status="prepaid", amount="1000.00"):
        client = client or self.lead
        order = Order.objects.create(order_number=f"MET-{key}", full_name="Metrics", phone="380501112233", total_sum=amount, payment_status=status)
        deal = IgDeal.objects.create(client=client, order=order, amount=amount)
        return order, deal

    def manager_payment(self, order, *, amount="200", at=None, key="one", scope="prepayment", client=None):
        client = client or self.lead
        review = IgPaymentConfirmationReview.objects.create(client=client, order=order, status="confirmed", dedupe_key=f"metrics-review:{key}")
        with patch("django.utils.timezone.now", return_value=at or self.entry + timedelta(days=1)):
            IgPaymentReviewDecision.objects.create(
                client=client, review=review, decision="manager_verified", verification_source="manager",
                verification_scope=scope, confirmed_amount=amount, actor_source="management_user", actor_external_id="1",
            )
        return review

    def report(self, **kwargs):
        return build_conversion_cohort_report(observed_at=self.now, **kwargs)

    def test_three_links_no_payment_then_200_balance_next_order_counts_zero_one_one_two(self):
        event = self.inquiry()
        for number in range(3):
            record_client_step_event(self.lead, event_type="paylink_issued", event_key=f"three-links:{number}", occurred_at=self.entry + timedelta(hours=number))
        self.assertEqual(len(confirmed_purchase_units(self.lead)), 0)
        self.assertEqual(self.report()["primary"]["paid"], 0)
        order, _deal = self.order()
        self.manager_payment(order)
        self.assertEqual(len(confirmed_purchase_units(self.lead)), 1)
        self.assertEqual(self.report()["primary"]["paid"], 1)
        self.manager_payment(order, amount="1000", scope="full_payment", key="balance", at=self.entry + timedelta(days=2))
        self.assertEqual(len(confirmed_purchase_units(self.lead)), 1)
        second, _deal = self.order(key="two", status="paid")
        self.manager_payment(second, amount="1000", scope="full_payment", key="two", at=self.entry + timedelta(days=3))
        self.assertEqual(len(confirmed_purchase_units(self.lead)), 2)
        self.assertEqual(self.report()["primary"]["paid"], 1)
        self.assertEqual(IgFunnelStepEvent.objects.filter(episode=event.episode, event_type="paylink_issued").count(), 3)

    def test_matured_unknown_repeat_failed_and_censored_denominators(self):
        self.inquiry()
        unknown = IgClient.get_or_create_for_sender("metrics-unknown")
        self.inquiry(unknown, history="unknown")
        repeat = IgClient.get_or_create_for_sender("metrics-repeat")
        self.inquiry(repeat, history="repeat")
        pending = IgClient.get_or_create_for_sender("metrics-pending")
        self.inquiry(pending, at=self.now - timedelta(days=2))
        hidden = IgClient.get_or_create_for_sender("metrics-hidden")
        hidden.hidden_at = self.now
        hidden.save(update_fields=["hidden_at", "updated_at"])
        self.inquiry(hidden)
        report = self.report()
        self.assertEqual(report["primary"]["entered"], 3)
        self.assertEqual(report["primary"]["matured"], 2)
        self.assertEqual(report["primary"]["right_censored"], 1)
        self.assertEqual(report["segments"]["unknown"]["entered"], 1)
        self.assertEqual(report["segments"]["repeat"]["entered"], 1)
        self.assertEqual(report["primary"]["paid"], 0)
        self.assertEqual(report["primary"]["rate_pct"], 0)
        self.assertIsNotNone(report["primary"]["wilson_95_pct"])

    def test_verified_new_requires_explicit_history_proof(self):
        self.inquiry(evidence={})
        report = self.report()
        self.assertEqual(report["primary"]["entered"], 0)
        self.assertEqual(report["segments"]["unknown"]["entered"], 1)
        self.assertIsNone(report["primary"]["rate_pct"])

    def test_first_commercial_entry_not_repeated_message_or_ad_touch(self):
        first = self.inquiry(ad_id="A")
        self.inquiry(at=self.entry + timedelta(days=5), ad_id="B")
        self.lead.ad_id = "C"
        self.lead.save(update_fields=["ad_id", "updated_at"])
        report = self.report()
        self.assertEqual(report["primary"]["entered"], 1)
        self.assertEqual(first.evidence[ATTRIBUTION_KEY]["ad_id"], "A")
        self.assertEqual(self.report(since=self.entry + timedelta(days=1))["primary"]["entered"], 0)

    def test_service_ugc_spam_excluded_but_later_commercial_included(self):
        for index, kind in enumerate(("service", "ugc", "collaboration", "spam", "unknown")):
            client = IgClient.get_or_create_for_sender(f"metrics-noncommerce-{index}")
            self.inquiry(client, classification=kind)
        self.inquiry(classification="service")
        self.inquiry(at=self.entry + timedelta(days=1))
        report = self.report()
        self.assertEqual(report["primary"]["entered"], 1)
        self.assertEqual(report["coverage"]["unclassified"], 1)
        self.assertEqual(report["coverage"]["excluded_noncommercial"], 5)

    def test_receipt_unknown_amount_partial_less_200_do_not_qualify(self):
        self.inquiry()
        order, _deal = self.order(status="checking")
        IgPaymentConfirmationReview.objects.create(client=self.lead, order=order, status="pending", dedupe_key="receipt-image", evidence={"screenshot": True})
        self.assertEqual(self.report()["primary"]["paid"], 0)
        self.manager_payment(order, amount=None)
        self.assertEqual(self.report()["primary"]["paid"], 0)
        self.manager_payment(order, amount="150", key="under200")
        self.assertEqual(self.report()["primary"]["paid"], 0)

    def test_full_payment_less_200_qualifies_and_period_is_entry_not_outcome_cutoff(self):
        self.inquiry()
        order, _deal = self.order(status="paid", amount="100")
        self.manager_payment(order, amount="100", scope="full_payment")
        report = self.report(since=self.entry - timedelta(seconds=1), until=self.entry + timedelta(days=1))
        self.assertEqual(report["primary"]["paid"], 1)

    def test_seven_fourteen_thirty_late_and_right_censor(self):
        self.inquiry(at=self.now - timedelta(days=35))
        order, _deal = self.order()
        self.manager_payment(order, at=self.now - timedelta(days=15))
        report = self.report()
        self.assertEqual(report["primary"]["paid"], 0)
        self.assertEqual(report["primary"]["late_paid"], 1)
        self.assertEqual(report["segments"]["verified_new"]["windows"]["30"]["paid"], 1)

    def test_payment_source_is_frozen_before_mutable_ad_changes_and_direct_return(self):
        entry = self.inquiry(ad_id="A")
        self.inquiry(at=self.entry + timedelta(hours=1), ad_id="B")
        with transaction.atomic():
            direct = InstagramBotMessage.objects.create(client=self.lead, sender_id=self.lead.igsid, role="user", provider_created_at=self.entry + timedelta(hours=2))
            record_commercial_inquiry_observation_in_transaction(self.lead, source_message_id=direct.pk, classification="retail", attribution={"quality": "known", "source": "direct", "source_message_ids": [direct.pk]})
        payment = record_client_step_event(self.lead, event_type="payment_confirmed", event_key="frozen-payment", occurred_at=self.entry + timedelta(hours=3), actor="provider")
        self.assertEqual(payment.evidence[ATTRIBUTION_KEY]["ad_id"], "B")
        self.lead.ad_id = "C"
        self.lead.save(update_fields=["ad_id", "updated_at"])
        payment.refresh_from_db()
        self.assertEqual(entry.evidence[ATTRIBUTION_KEY]["ad_id"], "A")
        self.assertEqual(payment.evidence[ATTRIBUTION_KEY]["ad_id"], "B")
        with self.assertRaises(ValueError):
            IgFunnelStepEvent.objects.filter(pk=payment.pk).update(evidence={})

    def test_refund_preserves_recorded_gross_and_separates_current_retention(self):
        inquiry = self.inquiry()
        order, deal = self.order(status="unpaid")
        inquiry.episode.deal = deal
        inquiry.episode.intended_order = order
        inquiry.episode.save(update_fields=["deal", "intended_order", "updated_at"])
        provider = IgPaymentEvent.objects.create(client=self.lead, deal=deal, event_key="metrics-provider", provider_status="success", amount_valid=True, gross_amount="200", payload_digest="x")
        record_client_step_event(self.lead, event_type="payment_confirmed", event_key="metrics-gross", occurred_at=self.entry + timedelta(days=1), actor="provider", evidence={"provider_event_id": provider.pk})
        IgPaymentProjection.objects.create(client=self.lead, deal=deal, truth="refunded", gross_amount="200", refunded_amount="200", paid_at=self.entry + timedelta(days=1), last_event=provider)
        report = self.report()
        self.assertEqual(report["primary"]["paid"], 1)
        self.assertEqual(report["segments"]["verified_new"]["gross_paid_clients"], 1)
        self.assertEqual(report["segments"]["verified_new"]["retained_paid_clients"], 0)
        self.assertEqual(report["segments"]["verified_new"]["refund_or_cancelled_clients"], 1)

    def test_no_observations_is_not_measured(self):
        report = self.report()
        self.assertEqual(report["measurement_status"], "not_measured")
        self.assertIsNone(report["primary"]["rate_pct"])

    def test_discount_order_created_is_not_paid_and_followup_unknown_is_null(self):
        record_client_step_event(self.lead, event_type="discount_offered", event_key="metrics-offer", occurred_at=self.entry)
        record_client_step_event(self.lead, event_type="order_created", event_key="metrics-created", occurred_at=self.entry + timedelta(hours=1))
        IgFollowUpTask.objects.create(client=self.lead, status="sent", sent_at=self.entry, due_at=self.entry, kind="qualification")
        report = build_funnel_analytics(until=self.now)
        self.assertEqual(report["discounts"]["bought_after_offer"], 0)
        self.assertEqual(report["discounts"]["orders_created"], 1)
        self.assertIsNone(report["followup_effectiveness"][0]["bought"])
        self.assertIsNone(report["followup_effectiveness"][0]["recovered"])
        self.assertEqual(report["followup_effectiveness"][0]["measurement_status"], "not_measured")

    def test_actual_received_200_is_not_full_invoice_ltv(self):
        self.inquiry()
        order, _deal = self.order(amount="1000")
        self.manager_payment(order, amount="200")
        summary = client_purchase_metrics(self.lead)
        self.assertEqual(summary["observed_net_received"], "200.00")
        self.assertIsNone(summary["goods_revenue"])
        self.assertEqual(summary["confirmed_purchase_count"], 1)

    def test_unpaid_order_and_consultation_number_are_not_purchase_number(self):
        from management.services.ig_commercial_episodes import client_episode_payload

        inquiry = self.inquiry()
        order, _deal = self.order(status="unpaid")
        inquiry.episode.intended_order = order
        inquiry.episode.save(update_fields=["intended_order", "updated_at"])
        payload = client_episode_payload(self.lead)
        self.assertEqual(payload["physical_order_count"], 1)
        self.assertEqual(payload["confirmed_purchase_count"], 0)
        self.assertFalse(payload["items"][0]["confirmed_purchase"])
        self.assertIsNone(payload["items"][0]["purchase_ordinal"])

    def test_paid_ordinals_follow_payment_time_not_order_creation(self):
        from management.services.ig_commercial_episodes import client_episode_payload

        inquiry = self.inquiry()
        first, _deal = self.order(key="created-first")
        second, _deal = self.order(key="created-second")
        inquiry.episode.intended_order = first
        inquiry.episode.save(update_fields=["intended_order", "updated_at"])
        IgCommercialEpisode.objects.create(client=self.lead, sequence=2, open_slot=None, materialization_key="ordinal-two", intended_order=second)
        self.manager_payment(first, key="paid-second", at=self.entry + timedelta(days=3))
        self.manager_payment(second, key="paid-first", at=self.entry + timedelta(days=1))
        items = client_episode_payload(self.lead)["items"]
        by_order = {item["order"]["id"]: item for item in items}
        self.assertEqual(by_order[first.pk]["purchase_ordinal"], 2)
        self.assertEqual(by_order[second.pk]["purchase_ordinal"], 1)

    def test_refined_classification_appends_without_losing_original_observation(self):
        unknown = self.inquiry(classification="unknown")
        with transaction.atomic():
            refined = record_commercial_inquiry_observation_in_transaction(
                self.lead, source_message_id=unknown.evidence[OBSERVATION_KEY]["source_message_id"],
                classification="retail", history_status="unknown",
            )
        self.assertNotEqual(unknown.pk, refined.pk)
        self.assertEqual(self.report()["segments"]["unknown"]["entered"], 1)
        unknown.refresh_from_db()
        self.assertEqual(unknown.evidence[OBSERVATION_KEY]["classification"], "unknown")
        self.assertEqual(refined.evidence[ATTRIBUTION_KEY]["ad_id"], "A")

    def test_discount_same_real_order_across_episodes_is_one_paid_outcome(self):
        inquiry = self.inquiry()
        order, deal = self.order()
        inquiry.episode.deal = deal
        inquiry.episode.save(update_fields=["deal", "updated_at"])
        duplicate_deal = IgDeal.objects.create(client=self.lead, order=order, amount="1000")
        duplicate = IgCommercialEpisode.objects.create(client=self.lead, sequence=2, open_slot=None, materialization_key="discount-duplicate", deal=duplicate_deal)
        for episode in (inquiry.episode, duplicate):
            IgFunnelStepEvent.objects.create(episode=episode, event_type="discount_offered", event_key=f"same-order-offer:{episode.pk}", occurred_at=self.entry)
            IgFunnelStepEvent.objects.create(episode=episode, event_type="payment_confirmed", event_key=f"same-order-paid:{episode.pk}", occurred_at=self.entry + timedelta(hours=1))
        report = build_funnel_analytics(until=self.now)
        self.assertEqual(report["discounts"]["bought_after_offer"], 1)

    def test_gross_manager_confirmation_survives_later_cancelled_review(self):
        self.inquiry()
        order, _deal = self.order(status="unpaid")
        review = self.manager_payment(order)
        decision = review.decisions.get()
        # Real writer records this same immutable before/after confirmation.
        with patch("django.utils.timezone.now", return_value=decision.created_at):
            IgPaymentReviewDecision.objects.create(client=self.lead, review=review, decision="manager_verified", verification_source="manager", verification_scope="prepayment", confirmed_amount="200", actor_source="management_user", actor_external_id="1", review_status_after="confirmed")
        review.status = "cancelled"
        review.save(update_fields=["status", "updated_at"])
        report = self.report()
        self.assertEqual(report["primary"]["paid"], 1)
        self.assertEqual(report["segments"]["verified_new"]["retained_paid_clients"], 0)

    def test_real_reply_and_verified_manager_action_hybrid_but_alert_is_not(self):
        self.inquiry()
        order, _deal = self.order()
        self.manager_payment(order)
        record_client_step_event(self.lead, event_type="bot_replied_first", event_key="metrics-actual-bot", occurred_at=self.entry + timedelta(hours=1), actor="bot", evidence={"provider_confirmed": True, "reply_message_id": 1})
        report = self.report()
        self.assertEqual(report["segments"]["verified_new"]["dimensions"]["participation"]["hybrid"]["paid"], 1)
        self.assertEqual(report["segments"]["verified_new"]["applied_discount_coverage"]["unknown"], 1)

    def test_inquiry_rejects_foreign_source_message(self):
        foreign = IgClient.get_or_create_for_sender("metrics-foreign")
        row = InstagramBotMessage.objects.create(client=foreign, sender_id=foreign.igsid, role="user")
        with transaction.atomic(), self.assertRaises(ValueError):
            record_commercial_inquiry_observation_in_transaction(self.lead, source_message_id=row.pk, classification="retail")

    def test_existing_manager_sync_records_one_confirmed_payment_and_frozen_source(self):
        from management.services.ig_commercial_episodes import sync_episode_payment

        inquiry = self.inquiry(ad_id="A")
        order, deal = self.order()
        inquiry.episode.deal = deal
        inquiry.episode.intended_order = order
        inquiry.episode.save(update_fields=["deal", "intended_order", "updated_at"])
        review = self.manager_payment(order)
        sync_episode_payment(review=review)
        sync_episode_payment(review=review)
        events = IgFunnelStepEvent.objects.filter(event_type="payment_confirmed")
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().evidence[ATTRIBUTION_KEY]["ad_id"], "A")
        self.inquiry(at=self.entry + timedelta(days=2), ad_id="B")
        sync_episode_payment(review=review)
        self.assertEqual(events.get().evidence[ATTRIBUTION_KEY]["ad_id"], "A")
        self.assertEqual(self.report()["primary"]["paid"], 1)

    def test_commercial_observations_do_not_add_stage_entry_events(self):
        self.inquiry()
        self.inquiry(at=self.entry + timedelta(days=1))
        report = build_funnel_analytics(until=self.now)
        self.assertEqual(report["steps"][0]["entered"], 0)
        self.assertEqual(self.report()["primary"]["entered"], 1)

    def test_acquisition_a_and_purchase_b_are_reported_as_distinct_immutable_roles(self):
        from management.services.ig_commercial_episodes import sync_episode_payment

        inquiry = self.inquiry(ad_id="A")
        self.inquiry(at=self.entry + timedelta(hours=1), ad_id="B")
        order, deal = self.order()
        inquiry.episode.deal = deal
        inquiry.episode.intended_order = order
        inquiry.episode.save(update_fields=["deal", "intended_order", "updated_at"])
        review = self.manager_payment(order)
        sync_episode_payment(review=review)
        self.lead.ad_id = "C"
        self.lead.save(update_fields=["ad_id", "updated_at"])
        segment = self.report()["segments"]["verified_new"]
        self.assertEqual(segment["acquisition_sources"][0]["ad_id"], "A")
        self.assertEqual(segment["purchase_sources"][0]["ad_id"], "B")
