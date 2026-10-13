from datetime import timedelta
from decimal import Decimal
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from management.ig_bot_models import (
    IgClient, IgCommercialEpisode, IgPaymentConfirmationReview, IgPaymentReviewDecision,
)
from management.models import InstagramBotMessage
from management.services.ig_commercial_episodes import (
    client_payment_truth_state, ensure_open_episode_for_locked_client, start_repeat_episode,
)
from management.services.ig_payment_review import (
    _claim_review_context, create_payment_review, extract_payment_review_evidence,
    payment_review_claim_anchor, reconcile_duplicate_payment_review,
)
from orders.models import Order


class AftercareEpisodeClaimTests(TestCase):
    def setUp(self):
        from management.models import InstagramBotSettings

        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        InstagramBotSettings.objects.update_or_create(pk=1, defaults={"ig_user_id": "aftercare-test-business"})
        self.customer = IgClient.get_or_create_for_sender("aftercare-claim-customer")
        self.receipt = self.message("Я оплатив 970 грн, ось чек")
        self.messages = [self.receipt_payload(self.receipt)]
        extracted = extract_payment_review_evidence(self.messages)
        anchor = payment_review_claim_anchor(extracted)
        self.order = Order.objects.create(
            full_name="Test Buyer", phone="0500000011", total_sum=Decimal("970"),
            status="done", payment_status="paid", sale_source="Instagram",
        )
        self.canonical = IgPaymentConfirmationReview.objects.create(
            client=self.customer, order=self.order, status="confirmed",
            evidence=_claim_review_context(extracted, claim_anchor=anchor),
            watermark_message_id=self.receipt.pk, dedupe_key="aftercare-canonical",
        )
        self.actor = get_user_model().objects.create_user("aftercare-manager", is_staff=True)
        self.decision(self.canonical, "970")
        self.completed = IgCommercialEpisode.objects.create(
            client=self.customer, sequence=1, materialization_key="aftercare-completed",
            state="fulfilled", outcome="fulfilled", open_slot=None,
            primary_payment_review=self.canonical, intended_order=self.order,
            closed_at=timezone.now() - timedelta(seconds=1),
        )
        self.customer.stage = IgClient.Stage.DONE
        self.customer.save(update_fields=["stage"])

    def message(self, text, role="user"):
        from management.services.ig_admin_state_capture import _namespace

        return InstagramBotMessage.objects.create(
            client=self.customer, sender_id=self.customer.igsid,
            role=role, source="webhook", text=text, provider_namespace=_namespace(),
        )

    def receipt_payload(self, source):
        return {"id": source.pk, "role": "user", "text": source.text,
                "media": [{"url": "https://cdn.test/receipt.jpg", "type": "image"}]}

    def decision(self, review, amount, scope="full_payment"):
        return IgPaymentReviewDecision.objects.create(
            review=review, client=self.customer, decision="manager_verified",
            verification_scope=scope, confirmed_amount=Decimal(amount),
            actor_source="management_user", actor_external_id=str(self.actor.pk),
        )

    def test_aftercare_observations_and_receipt_replay_never_open_purchase_or_call_provider(self):
        from management.services.ig_funnel_analytics import record_client_step_event

        manager = self.message("Посилку отримано, поділіться враженнями", "manager")
        engaged = record_client_step_event(
            self.customer, event_type="manager_engaged", event_key="aftercare-manager",
            actor="manager", evidence={"manager_message_id": manager.pk},
        )
        complaint = self.message("Доставка коштувала 90, а взяли 120. Поверніть різницю 30 грн")
        with transaction.atomic():
            resolved = ensure_open_episode_for_locked_client(self.customer, materialization_prefix="commerce-source")
        started = record_client_step_event(
            self.customer, event_type="conversation_started", event_key="aftercare-complaint",
            actor="customer", evidence={"message_id": complaint.pk},
        )
        with (patch("management.services.ig_payment_review._inspect_payment_messages") as inspect,
              patch("management.services.ig_payment_review._resolve_payment_media_candidates") as resolve,
              patch("management.services.ig_payment_review._queue_payment_review_notification") as notify):
            replay = create_payment_review(self.customer, watermark=complaint.pk,
                                           messages=self.messages + [{"id": complaint.pk, "role": "user", "text": complaint.text}])
        self.assertEqual((resolved.pk, engaged.episode_id, started.episode_id, replay.pk),
                         (self.completed.pk, self.completed.pk, self.completed.pk, self.canonical.pk))
        inspect.assert_not_called()
        resolve.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(self.customer.commercial_episodes.count(), 1)
        self.assertEqual(self.customer.payment_confirmation_reviews.count(), 1)
        self.customer.refresh_from_db()
        self.completed.refresh_from_db()
        self.assertIsNone(self.customer.current_commercial_episode_id)
        self.assertIsNone(self.completed.open_slot)
        self.assertEqual(self.customer.stage, IgClient.Stage.DONE)

    def repeat(self):
        source = self.message("Хочу замовити ще одну футболку")
        episode = start_repeat_episode(
            self.customer, repeat_kind="explicit_more", evidence_message_ids=[source.pk],
            confidence="1", analysis_model="deterministic_source", analysis_prompt_version="test",
        )
        self.customer.refresh_from_db()
        return episode

    def test_old_claim_in_real_repeat_remains_historical_and_does_not_grant_payment(self):
        episode = self.repeat()
        # Also cover old legacy cycles with a zero floor; identity is global,
        # so source fencing alone is not the duplicate-payment guard.
        episode.opened_watermark_message_id = 0
        episode.save(update_fields=["opened_watermark_message_id"])
        with patch("management.services.ig_payment_review._inspect_payment_messages") as inspect:
            replay = create_payment_review(self.customer, messages=self.messages)
        self.assertEqual(replay.pk, self.canonical.pk)
        inspect.assert_not_called()
        episode.refresh_from_db()
        self.assertIsNone(episode.primary_payment_review_id)
        current = client_payment_truth_state(self.customer)["current_payment_truth"]
        self.assertEqual(current["episode_id"], episode.pk)
        self.assertFalse(current["authoritative_for_fulfillment"])
        self.assertEqual(self.customer.commercial_episodes.count(), 2)

    def test_distinct_receipt_in_real_repeat_is_new_pending_claim(self):
        episode = self.repeat()
        source = self.message("Оплатив 1100 грн за нове замовлення, ось чек")
        with patch("management.services.ig_payment_review._queue_payment_review_notification"):
            fresh = create_payment_review(self.customer, watermark=source.pk,
                                          messages=[self.receipt_payload(source)], allow_provider=False)
        self.assertNotEqual(fresh.pk, self.canonical.pk)
        self.assertEqual(fresh.status, "pending")
        episode.refresh_from_db()
        self.assertEqual(episode.primary_payment_review_id, fresh.pk)
        self.assertEqual(self.customer.commercial_episodes.count(), 2)

    def test_distinct_receipt_after_completion_bootstraps_source_bounded_cycle(self):
        source = self.message("Оплатив нове замовлення, ось чек")
        with patch("management.services.ig_payment_review._queue_payment_review_notification"):
            fresh = create_payment_review(self.customer, messages=[self.receipt_payload(source)], allow_provider=False)
        self.assertEqual(fresh.status, "pending")
        self.assertEqual(fresh.commercial_episode.opened_watermark_message_id, source.pk)
        self.assertEqual(self.customer.commercial_episodes.count(), 2)

    def test_initial_prospect_and_deliberate_reset_can_still_open_an_episode(self):
        prospect = IgClient.get_or_create_for_sender("aftercare-new-prospect")
        with transaction.atomic():
            initial = ensure_open_episode_for_locked_client(prospect, materialization_prefix="ig-funnel")
            reset = ensure_open_episode_for_locked_client(self.customer, materialization_prefix="ig-false-history-correction")
        self.assertEqual(initial.sequence, 1)
        self.assertEqual(reset.sequence, 2)
        self.assertEqual(reset.open_slot, 1)

    def test_real_complaint_reduction_preserves_completed_selection_without_bootstrap(self):
        from management.models import IgCommerceSelectionSession
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source

        self.completed.product_snapshot = [{"size": "M", "qty": 1}]
        self.completed.price_snapshot = {"negotiated_total": "970.00"}
        self.completed.save(update_fields=["product_snapshot", "price_snapshot"])
        session = IgCommerceSelectionSession.objects.create(
            client=self.customer, commercial_episode=self.completed, generation=1,
            state="closed", open_slot=None, lines=[{"size": "M", "qty": 1}],
        )
        before = session.snapshot()
        complaint = self.message("Доставка коштувала 90 а взяли 120. Поверніть різницю 30 грн")
        with transaction.atomic():
            result = reduce_inbound_commerce_source(self.customer, complaint,
                expected_provider_namespace=complaint.provider_namespace)
        self.assertTrue(result.ready, result.reason)
        self.assertEqual(result.reason, "source_observed")
        self.assertEqual(result.observations[0]["reason"], "post_purchase_observation")
        session.refresh_from_db()
        self.completed.refresh_from_db()
        self.assertEqual(session.snapshot(), before)
        self.assertEqual(self.completed.product_snapshot, [{"size": "M", "qty": 1}])
        self.assertEqual(self.completed.price_snapshot, {"negotiated_total": "970.00"})
        self.assertEqual(self.customer.commercial_episodes.count(), 1)
        self.assertEqual(IgCommerceSelectionSession.objects.filter(client=self.customer).count(), 1)

    def test_real_repeat_source_creates_exactly_one_new_cycle_after_completion(self):
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source

        source = self.message("Хочу замовити ще одну футболку")
        with transaction.atomic():
            result = reduce_inbound_commerce_source(self.customer, source,
                expected_provider_namespace=source.provider_namespace)
        self.assertTrue(result.ready, result.reason)
        self.assertEqual(self.customer.commercial_episodes.count(), 2)
        repeat = self.customer.commercial_episodes.get(open_slot=1)
        self.assertEqual(repeat.repeat_evidence_message_ids, [source.pk])
        self.assertIsNone(repeat.intended_order_id)
        self.assertIsNone(repeat.primary_payment_review_id)

    def duplicate(self, episode):
        review = IgPaymentConfirmationReview.objects.create(
            client=self.customer, evidence=self.canonical.evidence,
            dedupe_key="aftercare-duplicate", watermark_message_id=self.receipt.pk,
        )
        episode.primary_payment_review = review
        episode.save(update_fields=["primary_payment_review"])
        return review

    def test_duplicate_repair_preserves_independent_repeat_and_detaches_old_payment(self):
        episode = self.repeat()
        duplicate = self.duplicate(episode)
        repaired = reconcile_duplicate_payment_review(duplicate)
        self.assertEqual(repaired.pk, self.canonical.pk)
        episode.refresh_from_db()
        duplicate.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(duplicate.status, "superseded")
        self.assertEqual(episode.open_slot, 1)
        self.assertEqual(self.customer.current_commercial_episode_id, episode.pk)
        self.assertIsNone(episode.primary_payment_review_id)
        self.assertFalse(episode.payment_snapshot["authoritative_for_fulfillment"])
        self.assertEqual(episode.events.get(event_type="review_superseded").evidence["canonical_review_id"], self.canonical.pk)

    def test_duplicate_orphan_repair_and_terminal_truth_keep_canonical_completed_purchase(self):
        episode = IgCommercialEpisode.objects.create(
            client=self.customer, sequence=2, materialization_key="aftercare-orphan",
        )
        self.customer.current_commercial_episode = episode
        self.customer.save(update_fields=["current_commercial_episode"])
        duplicate = self.duplicate(episode)
        reconcile_duplicate_payment_review(duplicate)
        self.customer.refresh_from_db()
        current = client_payment_truth_state(self.customer)["current_payment_truth"]
        self.assertEqual(current["episode_id"], self.completed.pk)
        self.assertEqual(current["review_id"], self.canonical.pk)
        self.assertTrue(current["authoritative_for_fulfillment"])
        with transaction.atomic():
            aftercare = ensure_open_episode_for_locked_client(self.customer, materialization_prefix="commerce-source")
        self.assertEqual(aftercare.pk, self.completed.pk)
        self.assertEqual(self.customer.commercial_episodes.count(), 2)
        self.assertFalse(self.customer.commercial_episodes.filter(open_slot=1).exists())

    def test_conflicting_confirmed_amounts_block_repair_and_claim_recreation(self):
        duplicate = IgPaymentConfirmationReview.objects.create(
            client=self.customer, evidence=self.canonical.evidence, status="confirmed",
            dedupe_key="aftercare-conflicting-decision",
        )
        self.decision(duplicate, "485", "prepayment")
        self.assertIsNone(reconcile_duplicate_payment_review(duplicate))
        with patch("management.services.ig_payment_review._inspect_payment_messages") as inspect:
            self.assertIsNone(create_payment_review(self.customer, messages=self.messages))
        inspect.assert_not_called()
        duplicate.refresh_from_db()
        self.assertEqual(duplicate.status, "confirmed")
        self.assertEqual(self.customer.payment_confirmation_reviews.count(), 2)

    def test_legacy_unbound_reviews_choose_canonical_and_keep_superseded_in_audit_only(self):
        legacy = IgClient.get_or_create_for_sender("aftercare-legacy-unbound")
        canonical = IgPaymentConfirmationReview.objects.create(
            client=legacy, evidence=self.canonical.evidence, status="confirmed",
            dedupe_key="aftercare-unbound-canonical",
        )
        duplicate = IgPaymentConfirmationReview.objects.create(
            client=legacy, evidence=self.canonical.evidence, status="superseded",
            superseded_by=canonical, dedupe_key="aftercare-unbound-superseded",
        )
        truth = client_payment_truth_state(legacy)
        self.assertEqual(truth["current_payment_truth"]["review_id"], canonical.pk)
        self.assertEqual({row["review_id"] for row in truth["payment_truth"]}, {canonical.pk, duplicate.pk})
        self.assertIsNone(truth["current_payment_truth"]["episode_id"])

    def test_legacy_unbound_reviews_use_latest_eligible_not_first_none_episode_match(self):
        legacy = IgClient.get_or_create_for_sender("aftercare-legacy-multiple")
        first = IgPaymentConfirmationReview.objects.create(
            client=legacy, evidence={"claim_anchor": "a" * 64}, status="confirmed",
            dedupe_key="aftercare-unbound-first",
        )
        latest = IgPaymentConfirmationReview.objects.create(
            client=legacy, evidence={"claim_anchor": "b" * 64}, status="pending",
            dedupe_key="aftercare-unbound-latest",
        )
        current = client_payment_truth_state(legacy)["current_payment_truth"]
        self.assertEqual(current["review_id"], latest.pk)
        self.assertNotEqual(current["review_id"], first.pk)
