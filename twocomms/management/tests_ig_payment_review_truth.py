import json
import os
import subprocess
import sys
import tempfile
import textwrap
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import DatabaseError, connection, transaction
from django.db.models.fields import NOT_PROVIDED
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.bot_access import (
    MANAGE_IG_PAYMENTS_PERMISSION,
    VIEW_IG_CONVERSATION_PII_PERMISSION,
)


class InstagramLegacyPaymentResolutionModelContractTests(SimpleTestCase):
    def test_historical_fulfillment_contract_preserves_resolution_and_total_provenance(self):
        from management.ig_bot_models import IgPaymentConfirmationReview, IgPaymentReviewDecision

        self.assertEqual(
            [choice[0] for choice in IgPaymentConfirmationReview.ResolutionKind.choices],
            [
                "",
                "historical_paid_archived",
            ],
        )
        self.assertEqual(
            [choice[0] for choice in IgPaymentConfirmationReview.ResolutionOutcome.choices],
            [
                "already_received",
                "already_delivered",
                "completed_unknown",
            ],
        )
        self.assertEqual(
            IgPaymentReviewDecision.VerificationScope.HISTORICAL_FULFILLED,
            "historical_fulfilled",
        )

        order_total_amount = IgPaymentReviewDecision._meta.get_field("order_total_amount")
        self.assertTrue(order_total_amount.null)
        self.assertTrue(order_total_amount.blank)
        self.assertEqual(order_total_amount.max_digits, 12)
        self.assertEqual(order_total_amount.decimal_places, 2)

        order_total_source = IgPaymentReviewDecision._meta.get_field("order_total_source")
        self.assertEqual(order_total_source.max_length, 48)
        self.assertTrue(order_total_source.blank)
        self.assertEqual(order_total_source.default, "")

        resolution_outcome = IgPaymentConfirmationReview._meta.get_field("resolution_outcome")
        self.assertEqual(resolution_outcome.max_length, 32)
        self.assertTrue(resolution_outcome.null)
        self.assertTrue(resolution_outcome.blank)
        self.assertIs(resolution_outcome.default, NOT_PROVIDED)


class InstagramPaymentDecisionTests(TestCase):
    def _require_db_append_only_trigger(self):
        """Skip raw-SQL trigger assertions when the runner disables migrations."""
        trigger_name = "ig_paydec_no_update"
        with connection.cursor() as cursor:
            if connection.vendor == "sqlite":
                cursor.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=%s",
                    [trigger_name],
                )
            elif connection.vendor in {"mysql", "mariadb"}:
                # SHOW TRIGGERS LIKE filters table names in MariaDB. Looking
                # up a trigger name there silently skipped the native gate.
                cursor.execute("SELECT 1 FROM information_schema.TRIGGERS "
                    "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=%s", [trigger_name])
            else:
                return
            if cursor.fetchone() is None:
                self.skipTest("append-only triggers are unavailable in migration-disabled test settings")

    def setUp(self):
        from management.ig_bot_models import IgClient, IgDeal, IgPaymentConfirmationReview, IgPaymentProjection

        self.client = IgClient.get_or_create_for_sender(
            "manager-verified-client",
            defaults={"display_name": "Клієнт перевірки"},
        )
        self.client.stage = IgClient.Stage.PAYMENT_PENDING
        self.client.save(update_fields=["stage", "updated_at"])
        self.deal = IgDeal.objects.create(client=self.client, amount="2100.00")
        self.projection = IgPaymentProjection.objects.create(
            client=self.client,
            deal=self.deal,
            truth=IgDeal.PaymentTruth.UNVERIFIED,
        )
        self.review = IgPaymentConfirmationReview.objects.create(
            client=self.client,
            deal=self.deal,
            dedupe_key="manager-verified-review",
            evidence={"order_draft": {"quoted_total": "2100"}},
            watermark_message_id=42,
        )
        self.actor = get_user_model().objects.create_user(
            username="reviewer",
            password="test-password",
            is_staff=True,
        )

    @patch("management.services.bot_conversation_analysis.schedule_client_truth_analysis")
    def test_manager_verification_is_source_qualified_and_keeps_provider_truth(self, schedule):
        from management.ig_bot_models import IgClientStageEvent, IgPaymentReviewDecision
        from management.services.ig_payment_review import record_review_decision

        with self.captureOnCommitCallbacks(execute=True):
            result = record_review_decision(
                self.review,
                actor=self.actor,
                decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
            )

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.manual_payment_truth, "manager_verified")
        decision = IgPaymentReviewDecision.objects.get(review=self.review)
        self.assertEqual(decision.decision, IgPaymentReviewDecision.Decision.MANAGER_VERIFIED)
        self.assertEqual(decision.verification_scope, "full_payment")
        self.assertEqual(decision.confirmed_amount, Decimal("2100.00"))
        self.assertEqual(decision.currency, "UAH")
        self.assertEqual(decision.amount_source, "deal_requested_amount")
        self.assertEqual(decision.evidence_watermark_message_id, 42)
        self.projection.refresh_from_db()
        self.assertEqual(self.projection.truth, "unverified")
        self.client.refresh_from_db()
        self.assertEqual(self.client.stage, "paid")
        self.assertTrue(IgClientStageEvent.objects.filter(
            client=self.client,
            from_stage="payment_pending",
            to_stage="paid",
            reason="payment_review_manager_verified",
        ).exists())
        schedule.assert_called_once_with(self.client, trigger="manager_payment_decision")

    def test_dynamic_prepayment_decision_keeps_exact_amount_and_evidence(self):
        from management.ig_bot_models import IgDeal, IgPaymentReviewDecision
        from management.services.ig_payment_review import record_review_decision

        self.deal.pay_type = IgDeal.PayType.PREPAYMENT
        self.deal.requested_payment_amount = Decimal("500.00")
        self.deal.requested_payment_evidence_ids = [101, 102]
        self.deal.save(update_fields=[
            "pay_type",
            "requested_payment_amount",
            "requested_payment_evidence_ids",
            "updated_at",
        ])

        record_review_decision(
            self.review,
            actor=self.actor,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
        )

        decision = IgPaymentReviewDecision.objects.get(review=self.review)
        self.assertEqual(decision.verification_scope, "prepayment")
        self.assertEqual(decision.confirmed_amount, Decimal("500.00"))
        self.assertEqual(decision.amount_source, "deal_requested_amount")
        self.assertEqual(decision.amount_evidence_message_ids, [101, 102])

    def test_full_payment_rejects_amount_that_differs_from_negotiated_total(self):
        from management.services.ig_payment_review import record_review_decision

        with self.assertRaisesMessage(ValueError, "повною вартістю"):
            record_review_decision(
                self.review,
                actor=self.actor,
                decision="manager_verified",
                verification_scope="full_payment",
                confirmed_amount="500.00",
            )

        self.assertFalse(self.review.decisions.exists())

    def test_manager_verification_without_any_amount_fails_closed(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.services.ig_payment_review import record_review_decision

        review = IgPaymentConfirmationReview.objects.create(
            client=self.client,
            dedupe_key="manager-verified-no-amount",
            evidence={},
        )

        with self.assertRaisesMessage(ValueError, "Сума підтвердженого платежу"):
            record_review_decision(
                review,
                actor=self.actor,
                decision="manager_verified",
                verification_scope="payment_claim",
            )

        self.assertFalse(review.decisions.exists())

    def test_unknown_order_total_cannot_become_fulfillment_authority(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.services.ig_payment_review import record_review_decision

        review = IgPaymentConfirmationReview.objects.create(
            client=self.client,
            dedupe_key="manager-verified-unknown-total",
            evidence={
                "amount_evidence": [
                    {"kind": "payment_evidence", "amount": "500", "message_id": 150},
                ],
            },
        )

        with self.assertRaisesMessage(ValueError, "Повна вартість замовлення"):
            record_review_decision(
                review,
                actor=self.actor,
                decision="manager_verified",
                verification_scope="prepayment",
                confirmed_amount="500.00",
            )

        self.assertFalse(review.decisions.exists())

    @patch("management.services.bot_conversation_analysis.schedule_client_truth_analysis")
    def test_manager_supplied_total_is_stored_separately_from_paid_amount(self, schedule):
        from management.ig_bot_models import (
            IgCommercialEpisode,
            IgPaymentConfirmationReview,
            IgPaymentReviewDecision,
        )
        from management.services.ig_payment_review import record_review_decision

        review = IgPaymentConfirmationReview.objects.create(
            client=self.client,
            dedupe_key="manager-supplied-total",
            evidence={"amount_evidence": [{"kind": "payment_evidence", "amount": "1550"}]},
        )

        with self.captureOnCommitCallbacks(execute=True):
            record_review_decision(
                review,
                actor=self.actor,
                decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
                verification_scope=IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT,
                order_total_amount="1550.00",
                confirmed_amount="1550.00",
            )

        decision = IgPaymentReviewDecision.objects.get(review=review)
        self.assertEqual(decision.order_total_amount, Decimal("1550.00"))
        self.assertEqual(decision.order_total_source, "manager_input")
        self.assertEqual(decision.confirmed_amount, Decimal("1550.00"))
        episode = IgCommercialEpisode.objects.get(primary_payment_review=review)
        self.assertEqual(episode.payment_snapshot["order_total"], "1550.00")
        self.assertEqual(episode.payment_snapshot["negotiated_order_total_source"], "manager_input")
        schedule.assert_called_once_with(self.client, trigger="manager_payment_decision")

    @patch(
        "management.services.bot_conversation_analysis.schedule_client_truth_analysis",
        side_effect=RuntimeError("analysis queue unavailable"),
    )
    def test_analysis_enqueue_failure_does_not_rollback_manager_decision(self, schedule):
        from management.services.ig_payment_review import record_review_decision

        with self.captureOnCommitCallbacks(execute=True):
            record_review_decision(
                self.review,
                actor=self.actor,
                decision="manager_verified",
            )

        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "confirmed")
        self.assertEqual(self.review.decisions.count(), 1)
        schedule.assert_called_once_with(self.client, trigger="manager_payment_decision")

    def test_exact_payment_claim_without_order_total_does_not_mark_client_paid(self):
        from management.ig_bot_models import IgPaymentConfirmationReview, IgPaymentReviewDecision
        from management.services.ig_payment_review import record_review_decision

        review = IgPaymentConfirmationReview.objects.create(
            client=self.client,
            dedupe_key="manager-payment-claim-only",
            evidence={
                "amount_evidence": [
                    {"kind": "payment_evidence", "amount": "500", "message_id": 151},
                ],
            },
        )

        record_review_decision(
            review,
            actor=self.actor,
            decision="manager_verified",
            verification_scope="payment_claim",
            confirmed_amount="500.00",
        )

        decision = IgPaymentReviewDecision.objects.get(review=review)
        self.client.refresh_from_db()
        self.assertEqual(decision.confirmed_amount, Decimal("500.00"))
        self.assertEqual(decision.verification_scope, "payment_claim")
        self.assertEqual(self.client.stage, "payment_pending")

    def test_rejection_requires_reason_and_returns_to_checkout(self):
        from management.services.ig_payment_review import record_review_decision

        with self.assertRaisesMessage(ValueError, "Код причини"):
            record_review_decision(
                self.review,
                actor=self.actor,
                decision="manager_rejected",
            )

        result = record_review_decision(
            self.review,
            actor=self.actor,
            decision="manager_rejected",
            reason_code="amount_mismatch",
            reason_text="Сума на чеку не збігається з домовленістю.",
        )

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.manual_payment_truth, "manager_rejected")
        self.assertEqual(result.cancellation_reason, "Сума на чеку не збігається з домовленістю.")
        self.client.refresh_from_db()
        self.assertEqual(self.client.stage, "checkout")

    def test_terminal_decision_replay_does_not_create_a_second_decision(self):
        from management.ig_bot_models import IgPaymentReviewDecision
        from management.services.ig_payment_review import record_review_decision

        first = record_review_decision(
            self.review,
            actor=self.actor,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
        )
        second = record_review_decision(
            self.review,
            actor=self.actor,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(IgPaymentReviewDecision.objects.filter(review=self.review).count(), 1)

    def test_decision_history_is_append_only(self):
        from management.ig_bot_models import IgPaymentReviewDecision
        from management.services.ig_payment_review import record_review_decision

        record_review_decision(
            self.review,
            actor=self.actor,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
        )
        decision = IgPaymentReviewDecision.objects.get(review=self.review)
        decision.reason_text = "Змінений заднім числом доказ"

        with self.assertRaisesMessage(ValueError, "append-only"):
            decision.save()
        with self.assertRaisesMessage(ValueError, "append-only"):
            decision.delete()
        with self.assertRaisesMessage(ValueError, "append-only"):
            IgPaymentReviewDecision.objects.filter(pk=decision.pk).update(
                reason_text="Переписано через QuerySet"
            )
        with self.assertRaisesMessage(ValueError, "append-only"):
            IgPaymentReviewDecision.objects.filter(pk=decision.pk).delete()

    def test_database_trigger_rejects_raw_decision_update(self):
        from management.ig_bot_models import IgPaymentReviewDecision
        from management.services.ig_payment_review import record_review_decision

        self._require_db_append_only_trigger()

        record_review_decision(
            self.review,
            actor=self.actor,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
        )
        decision = IgPaymentReviewDecision.objects.get(review=self.review)

        with self.assertRaisesMessage(DatabaseError, "append-only"):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE management_igpaymentreviewdecision "
                        "SET reason_text = %s WHERE id = %s",
                        ["Переписано напряму", decision.pk],
                    )

    def test_database_trigger_rejects_raw_decision_delete(self):
        from management.ig_bot_models import IgPaymentReviewDecision
        from management.services.ig_payment_review import record_review_decision

        self._require_db_append_only_trigger()

        record_review_decision(
            self.review,
            actor=self.actor,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
        )
        decision = IgPaymentReviewDecision.objects.get(review=self.review)

        with self.assertRaisesMessage(DatabaseError, "append-only"):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM management_igpaymentreviewdecision WHERE id = %s",
                        [decision.pk],
                    )

    def test_fresh_hidden_state_blocks_decision_inside_transaction(self):
        from management.ig_bot_models import IgClient, IgPaymentReviewDecision
        from management.services.ig_payment_review import record_review_decision

        IgClient.objects.filter(pk=self.client.pk).update(hidden_at=timezone.now())

        with self.assertRaisesMessage(ValueError, "Прихований клієнт"):
            record_review_decision(
                self.review,
                actor=self.actor,
                decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
            )
        self.assertFalse(IgPaymentReviewDecision.objects.filter(review=self.review).exists())

    def test_rejection_requires_structured_reason_code_not_only_free_text(self):
        from management.services.ig_payment_review import record_review_decision

        with self.assertRaisesMessage(ValueError, "Код причини"):
            record_review_decision(
                self.review,
                actor=self.actor,
                decision="manager_rejected",
                reason_text="Вільний текст без класифікації.",
            )

    def test_decision_requires_management_or_telegram_actor(self):
        from management.services.ig_payment_review import record_review_decision

        with self.assertRaisesMessage(ValueError, "Автор рішення"):
            record_review_decision(
                self.review,
                actor=None,
                decision="manager_verified",
            )

    def test_prepayment_deal_derives_prepayment_verification_scope(self):
        from management.ig_bot_models import IgDeal, IgPaymentReviewDecision
        from management.services.ig_payment_review import record_review_decision

        self.deal.pay_type = IgDeal.PayType.PREPAY_200
        self.deal.save(update_fields=["pay_type", "updated_at"])

        record_review_decision(
            self.review,
            actor=self.actor,
            decision="manager_verified",
        )

        decision = IgPaymentReviewDecision.objects.get(review=self.review)
        self.assertEqual(decision.verification_scope, "prepayment")

    def test_telegram_origin_wins_over_linked_management_actor(self):
        from management.ig_bot_models import IgPaymentReviewDecision
        from management.services.ig_payment_review import record_review_decision

        record_review_decision(
            self.review,
            actor=self.actor,
            decision="manager_verified",
            telegram_decision={
                "telegram_user_id": "777",
                "telegram_username": "owner",
            },
        )

        decision = IgPaymentReviewDecision.objects.get(review=self.review)
        self.assertEqual(decision.actor, self.actor)
        self.assertEqual(decision.actor_source, "telegram_user")
        self.assertEqual(decision.actor_external_id, "777")

    @patch("management.services.bot_conversation_analysis.schedule_client_truth_analysis")
    def test_reanalysis_schedule_failure_does_not_rollback_decision(self, schedule):
        from management.ig_bot_models import IgPaymentReviewDecision
        from management.services.ig_payment_review import record_review_decision

        schedule.side_effect = RuntimeError("analysis queue unavailable")

        with self.captureOnCommitCallbacks(execute=True):
            record_review_decision(
                self.review,
                actor=self.actor,
                decision="manager_verified",
            )

        self.review.refresh_from_db()
        self.client.refresh_from_db()
        self.assertEqual(self.review.status, "confirmed")
        self.assertEqual(self.client.stage, "paid")
        self.assertTrue(IgPaymentReviewDecision.objects.filter(review=self.review).exists())

    @patch("management.ig_bot_models.IgClientStageEvent.objects.create")
    def test_stage_event_failure_rolls_back_review_and_decision(self, create_stage_event):
        from management.ig_bot_models import IgPaymentReviewDecision
        from management.services.ig_payment_review import record_review_decision

        create_stage_event.side_effect = RuntimeError("stage ledger unavailable")

        with self.assertRaisesMessage(RuntimeError, "stage ledger unavailable"):
            record_review_decision(
                self.review,
                actor=self.actor,
                decision="manager_verified",
            )

        self.review.refresh_from_db()
        self.client.refresh_from_db()
        self.assertEqual(self.review.status, "pending")
        self.assertEqual(self.client.stage, "payment_pending")
        self.assertFalse(IgPaymentReviewDecision.objects.filter(review=self.review).exists())


MGMT = override_settings(
    ROOT_URLCONF="twocomms.urls_management",
    SECURE_SSL_REDIRECT=False,
    SITE_BASE_URL="https://shop.example",
)


@MGMT
class InstagramPaymentDecisionApiTests(TestCase):
    def setUp(self):
        from management.ig_bot_models import (
            IgClient,
            IgDeal,
            IgPaymentConfirmationReview,
            IgPaymentProjection,
        )

        self.actor = get_user_model().objects.create_user(
            username="payment-api-reviewer",
            password="test-password",
            is_staff=True,
        )
        self.actor.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="management",
            codename__in={
                MANAGE_IG_PAYMENTS_PERMISSION.split(".", 1)[1],
                VIEW_IG_CONVERSATION_PII_PERMISSION.split(".", 1)[1],
            },
        ))
        self.client.force_login(self.actor)
        self.ig_client = IgClient.get_or_create_for_sender("payment-api-client")
        self.ig_client.stage = IgClient.Stage.PAYMENT_PENDING
        self.ig_client.save(update_fields=["stage", "updated_at"])
        self.deal = IgDeal.objects.create(client=self.ig_client, amount="2100.00")
        self.projection = IgPaymentProjection.objects.create(
            client=self.ig_client,
            deal=self.deal,
            truth=IgDeal.PaymentTruth.UNVERIFIED,
        )
        self.review = IgPaymentConfirmationReview.objects.create(
            client=self.ig_client,
            deal=self.deal,
            dedupe_key="payment-api-review",
            evidence={"order_draft": {"quoted_total": "2100"}},
            watermark_message_id=77,
        )
        self.action_url = reverse(
            "management_bot_payment_review_action_api",
            args=[self.review.pk],
        )

    @patch("management.services.bot_conversation_analysis.schedule_client_truth_analysis")
    def test_manager_verify_returns_source_qualified_truth(self, schedule):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.action_url,
                {"action": "manager_verify", "verification_scope": "full_payment"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "confirmed")
        self.assertEqual(payload["payment"]["manager_truth"], "manager_verified")
        self.assertEqual(payload["payment"]["provider_truth"], "unverified")
        self.assertEqual(payload["payment"]["verification_source"], "manager")
        self.assertEqual(payload["decision"]["verification_scope"], "full_payment")
        self.assertEqual(payload["decision"]["confirmed_amount"], "2100.00")
        self.assertEqual(payload["payment"]["confirmed_paid_amount"], "2100.00")
        self.assertEqual(payload["payment"]["remaining_amount"], "0.00")
        self.assertEqual(payload["next_action"], "resolve_order")
        self.assertEqual(payload["order_url"], "")
        self.assertTrue(payload["order_resolution"]["required"])
        self.assertEqual(
            payload["order_resolution"]["create_new"]["url"],
            f"https://shop.example/admin-panel/orders/manual/create/?ig_payment_review={self.review.pk}",
        )
        self.assertEqual(
            payload["order_resolution"]["link_existing"]["action"],
            "link_order",
        )
        self.projection.refresh_from_db()
        self.assertEqual(self.projection.truth, "unverified")
        schedule.assert_called_once()

    def test_manager_verify_api_accepts_dynamic_prepayment_amount(self):
        from management.ig_bot_models import IgDeal, IgPaymentReviewDecision

        self.deal.pay_type = IgDeal.PayType.PREPAYMENT
        self.deal.requested_payment_amount = Decimal("500.00")
        self.deal.requested_payment_evidence_ids = [501]
        self.deal.save(update_fields=[
            "pay_type",
            "requested_payment_amount",
            "requested_payment_evidence_ids",
            "updated_at",
        ])

        response = self.client.post(
            self.action_url,
            {
                "action": "manager_verify",
                "verification_scope": "prepayment",
                "confirmed_amount": "500.00",
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        decision = IgPaymentReviewDecision.objects.get(review=self.review)
        self.assertEqual(decision.confirmed_amount, Decimal("500.00"))
        self.assertEqual(response.json()["payment"]["order_total"], "2100.00")
        self.assertEqual(response.json()["payment"]["requested_payment_amount"], "500.00")
        self.assertEqual(response.json()["payment"]["confirmed_paid_amount"], "500.00")
        self.assertEqual(response.json()["payment"]["remaining_amount"], "1600.00")

    @patch("management.services.bot_conversation_analysis.schedule_client_truth_analysis")
    def test_manager_verify_api_accepts_missing_total_when_manager_supplies_it(self, schedule):
        from management.ig_bot_models import IgPaymentConfirmationReview, IgPaymentReviewDecision

        review = IgPaymentConfirmationReview.objects.create(
            client=self.ig_client,
            dedupe_key="payment-api-supplied-total",
            evidence={"amount_evidence": [{"kind": "payment_evidence", "amount": "1550"}]},
        )
        action_url = reverse(
            "management_bot_payment_review_action_api",
            args=[review.pk],
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                action_url,
                {
                    "action": "manager_verify",
                    "verification_scope": "full_payment",
                    "order_total_amount": "1550.00",
                    "confirmed_amount": "1550.00",
                },
            )

        self.assertEqual(response.status_code, 200, response.content)
        decision = IgPaymentReviewDecision.objects.get(review=review)
        self.assertEqual(decision.order_total_amount, Decimal("1550.00"))
        self.assertEqual(response.json()["payment"]["order_total"], "1550.00")
        self.assertEqual(response.json()["decision"]["order_total_source"], "manager_input")
        schedule.assert_called_once()

    def test_historical_paid_fulfilled_api_archives_unknown_amount_without_order_actions(self):
        from management.ig_bot_models import IgPaymentConfirmationReview, IgPaymentReviewDecision

        review = IgPaymentConfirmationReview.objects.create(
            client=self.ig_client,
            dedupe_key="payment-api-historical-unknown",
            evidence={},
        )
        from management.services.ig_payment_review import LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF

        IgPaymentConfirmationReview.objects.filter(pk=review.pk).update(
            created_at=LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF - timedelta(seconds=1),
        )
        action_url = reverse(
            "management_bot_payment_review_action_api",
            args=[review.pk],
        )
        response = self.client.post(
            action_url,
            {
                "action": "historical_paid_fulfilled",
                "outcome": "already_received",
                "reason": "Historical sale confirmed by owner",
                "amount_unrecoverable": "true",
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["next_action"], "historical_completed")
        self.assertEqual(payload["resolution_outcome"], "already_received")
        self.assertFalse(payload["order_resolution"]["required"])
        self.assertEqual(payload["order_resolution"]["create_new"]["url"], "")
        decision = IgPaymentReviewDecision.objects.get(review=review)
        self.assertEqual(decision.verification_scope, "historical_fulfilled")
        self.assertIsNone(decision.confirmed_amount)

    def test_historical_paid_fulfilled_api_rejects_fresh_pending_review(self):
        from management.ig_bot_models import IgPaymentConfirmationReview, IgPaymentReviewDecision

        review = IgPaymentConfirmationReview.objects.create(
            client=self.ig_client,
            dedupe_key="payment-api-historical-fresh",
            evidence={},
        )

        response = self.client.post(
            reverse("management_bot_payment_review_action_api", args=[review.pk]),
            {
                "action": "historical_paid_fulfilled",
                "outcome": "already_received",
                "reason": "Current pending sale",
                "amount_unrecoverable": "true",
            },
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("межі legacy-імпорту", response.json()["error"])
        review.refresh_from_db()
        self.assertEqual(review.status, IgPaymentConfirmationReview.Status.PENDING)
        self.assertFalse(IgPaymentReviewDecision.objects.filter(review=review).exists())

    def test_historical_paid_fulfilled_api_rejects_regular_user(self):
        from management.ig_bot_models import IgPaymentConfirmationReview

        review = IgPaymentConfirmationReview.objects.create(
            client=self.ig_client,
            dedupe_key="payment-api-historical-regular-user",
        )
        regular = get_user_model().objects.create_user(
            username="historical-regular-user",
            password="test-password",
        )
        self.client.force_login(regular)
        response = self.client.post(
            reverse("management_bot_payment_review_action_api", args=[review.pk]),
            {
                "action": "historical_paid_fulfilled",
                "outcome": "already_received",
                "reason": "Historical sale confirmed by owner",
                "amount_unrecoverable": "true",
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_provider_and_manager_amount_conflict_requires_reconciliation(self):
        from management.ig_bot_models import IgDeal

        self.projection.truth = IgDeal.PaymentTruth.CONFIRMED
        self.projection.gross_amount = Decimal("500.00")
        self.projection.refunded_amount = Decimal("0.00")
        self.projection.save(update_fields=["truth", "gross_amount", "refunded_amount", "updated_at"])

        response = self.client.post(
            self.action_url,
            {
                "action": "manager_verify",
                "verification_scope": "full_payment",
                "confirmed_amount": "2100.00",
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        payment = response.json()["payment"]
        self.assertEqual(payment["provider_confirmed_amount"], "500.00")
        self.assertEqual(payment["manager_confirmed_amount"], "2100.00")
        self.assertTrue(payment["needs_reconciliation"])
        self.assertEqual(payment["confirmed_paid_amount"], "")
        self.assertEqual(payment["remaining_amount"], "")
        self.assertFalse(payment["authoritative_for_fulfillment"])

    @patch("management.services.bot_conversation_analysis.schedule_client_truth_analysis")
    def test_legacy_confirmed_review_can_append_exact_amount_clarification(self, schedule):
        from management.ig_bot_models import (
            IgPaymentConfirmationReview,
            IgPaymentReviewDecision,
        )

        self.review.status = IgPaymentConfirmationReview.Status.CONFIRMED
        self.review.evidence = {
            "order_draft": {
                "quoted_total": "2100",
                "amount_source_message_id": 237,
                "items": [
                    {"title": "Базова футболка", "qty": 1, "unit_price": None},
                    {"title": "Оверсайз", "qty": 1, "unit_price": None},
                ],
                "uncertainty_reasons": ["conversation_price_allocation_required"],
            },
            "amount_evidence": [
                {
                    "message_id": 237,
                    "role": "manager",
                    "amount": "2100",
                    "kind": "order_total",
                    "quote": "Сума: 2100 грн",
                }
            ],
        }
        self.review.save(update_fields=["status", "evidence", "updated_at"])
        IgPaymentReviewDecision.objects.create(
            review=self.review,
            client=self.ig_client,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
            verification_source="manager",
            verification_scope=IgPaymentReviewDecision.VerificationScope.PAYMENT_CLAIM,
            confirmed_amount=None,
            amount_source="",
            amount_evidence_message_ids=[],
            evidence_watermark_message_id=238,
            actor=self.actor,
            actor_source=IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
            actor_external_id=str(self.actor.pk),
            review_status_before=IgPaymentConfirmationReview.Status.PENDING,
            review_status_after=IgPaymentConfirmationReview.Status.CONFIRMED,
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.action_url,
                {
                    "action": "clarify_amount",
                    "verification_scope": "full_payment",
                    "confirmed_amount": "2100.00",
                },
            )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["next_action"], "resolve_order")
        self.assertTrue(payload["order_resolution"]["required"])
        self.assertEqual(payload["payment"]["confirmed_paid_amount"], "2100.00")
        self.assertEqual(IgPaymentReviewDecision.objects.filter(review=self.review).count(), 2)
        clarified = IgPaymentReviewDecision.objects.filter(review=self.review).order_by("-id").first()
        self.assertEqual(clarified.confirmed_amount, Decimal("2100.00"))
        self.assertEqual(clarified.amount_source, "manager_input")
        self.assertEqual(clarified.amount_evidence_message_ids, [237])
        self.assertEqual(clarified.review_status_before, "confirmed")
        self.assertEqual(clarified.review_status_after, "confirmed")
        schedule.assert_called_once()

    def test_amount_clarification_cannot_replace_an_authoritative_decision(self):
        from management.services.ig_payment_review import record_review_decision

        record_review_decision(
            self.review,
            actor=self.actor,
            decision="manager_verified",
            verification_scope="full_payment",
            confirmed_amount="2100.00",
        )

        response = self.client.post(
            self.action_url,
            {
                "action": "clarify_amount",
                "verification_scope": "full_payment",
                "confirmed_amount": "2000.00",
            },
        )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("вже містить точну суму", response.json()["error"])
        self.assertEqual(self.review.decisions.count(), 1)

    def test_amount_clarification_cannot_change_a_review_with_linked_order(self):
        from management.ig_bot_models import (
            IgPaymentConfirmationReview,
            IgPaymentReviewDecision,
        )
        from orders.models import Order

        order = Order.objects.create(
            full_name="Яна",
            phone="380502034719",
            total_sum=Decimal("2100.00"),
            payment_status="paid",
        )
        self.review.status = IgPaymentConfirmationReview.Status.CONFIRMED
        self.review.order = order
        self.review.save(update_fields=["status", "order", "updated_at"])
        IgPaymentReviewDecision.objects.create(
            review=self.review,
            client=self.ig_client,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
            verification_source="manager",
            verification_scope=IgPaymentReviewDecision.VerificationScope.PAYMENT_CLAIM,
            confirmed_amount=None,
            actor=self.actor,
            actor_source=IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
            actor_external_id=str(self.actor.pk),
            review_status_before=IgPaymentConfirmationReview.Status.PENDING,
            review_status_after=IgPaymentConfirmationReview.Status.CONFIRMED,
        )

        response = self.client.post(
            self.action_url,
            {
                "action": "clarify_amount",
                "verification_scope": "full_payment",
                "confirmed_amount": "2100.00",
            },
        )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("замовлення вже прив’язано", response.json()["error"])
        self.assertEqual(self.review.decisions.count(), 1)

    def test_manager_reject_requires_structured_reason(self):
        response = self.client.post(self.action_url, {"action": "manager_reject"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Причина", response.json()["error"])
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "pending")

    def test_invalid_verification_scope_is_rejected(self):
        response = self.client.post(
            self.action_url,
            {"action": "manager_verify", "verification_scope": "everything"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Обсяг перевірки", response.json()["error"])

    def test_manager_reject_exposes_reason_in_review_api(self):
        response = self.client.post(
            self.action_url,
            {
                "action": "manager_reject",
                "reason_code": "amount_mismatch",
                "reason_text": "Сума на чеку не збігається з домовленістю.",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["payment"]["manager_truth"], "manager_rejected")
        self.assertEqual(payload["decision"]["reason_code"], "amount_mismatch")
        self.assertEqual(
            payload["decision"]["reason_text"],
            "Сума на чеку не збігається з домовленістю.",
        )
        rows = self.client.get(
            reverse("management_bot_payment_reviews_api") + f"?id={self.review.pk}"
        ).json()["items"]
        self.assertEqual(rows[0]["manual_payment_truth"], "manager_rejected")
        self.assertEqual(rows[0]["latest_decision"]["reason_code"], "amount_mismatch")

    def test_hidden_client_cannot_be_decided(self):
        self.ig_client.hidden_at = timezone.now()
        self.ig_client.save(update_fields=["hidden_at", "updated_at"])

        response = self.client.post(self.action_url, {"action": "manager_verify"})

        self.assertEqual(response.status_code, 409)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "pending")

    def test_opposite_terminal_action_returns_conflict(self):
        from management.ig_bot_models import IgPaymentReviewDecision

        confirmed = self.client.post(self.action_url, {"action": "manager_verify"})
        rejected = self.client.post(
            self.action_url,
            {
                "action": "manager_reject",
                "reason_code": "wrong_receipt",
                "reason_text": "Інший платіж.",
            },
        )

        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json()["current_decision"], "manager_verified")
        self.assertEqual(IgPaymentReviewDecision.objects.filter(review=self.review).count(), 1)

    def test_legacy_terminal_review_without_decision_cannot_create_order(self):
        self.review.status = "confirmed"
        self.review.save(update_fields=["status", "updated_at"])

        rows = self.client.get(
            reverse("management_bot_payment_reviews_api") + f"?id={self.review.pk}"
        ).json()["items"]
        response = self.client.post(self.action_url, {"action": "manager_verify"})

        self.assertEqual(rows[0]["manual_payment_truth"], "")
        self.assertEqual(rows[0]["order_url"], "")
        self.assertEqual(response.status_code, 409)
        self.assertIn("журнал", response.json()["error"])


class InstagramPaymentDecisionMigrationTests(SimpleTestCase):
    def test_backfills_source_qualified_decisions_for_legacy_terminal_reviews(self):
        script = textwrap.dedent(
            """
            import json
            import os
            import sys

            # The parent test runner may export ``DJANGO_SETTINGS_MODULE`` as
            # ``test_settings`` (which disables migrations).  This subprocess
            # intentionally exercises the real migration graph.
            os.environ["DJANGO_SETTINGS_MODULE"] = "twocomms.settings"
            from django.conf import settings

            settings.DATABASES["default"] = {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": sys.argv[1],
            }

            import django
            django.setup()

            from django.db import connection
            from django.db.migrations.executor import MigrationExecutor
            from django.utils import timezone

            migrate_from = ("management", "0102_ig_payment_review_order")
            migrate_to = ("management", "0103_ig_payment_review_truth")
            executor = MigrationExecutor(connection)
            executor.migrate([migrate_from])
            executor = MigrationExecutor(connection)
            old_apps = executor.loader.project_state([migrate_from]).apps

            User = old_apps.get_model("auth", "User")
            Client = old_apps.get_model("management", "IgClient")
            Deal = old_apps.get_model("management", "IgDeal")
            Review = old_apps.get_model("management", "IgPaymentConfirmationReview")

            actor = User.objects.create(username="legacy-payment-reviewer")
            confirmed_client = Client.objects.create(
                igsid="legacy-confirmed-client", stage="payment_pending"
            )
            confirmed_deal = Deal.objects.create(
                client=confirmed_client, pay_type="online_full", amount="2100.00"
            )
            confirmed_at = timezone.now() - timezone.timedelta(days=2)
            confirmed_review_id = Review.objects.create(
                client=confirmed_client,
                deal=confirmed_deal,
                dedupe_key="legacy-confirmed-review",
                status="confirmed",
                evidence={
                    "telegram_decision": {
                        "telegram_user_id": "777",
                        "telegram_username": "owner",
                    }
                },
                watermark_message_id=42,
                confirmed_by=actor,
                confirmed_at=confirmed_at,
            ).pk

            cancelled_client = Client.objects.create(
                igsid="legacy-cancelled-client", stage="checkout"
            )
            cancelled_deal = Deal.objects.create(
                client=cancelled_client, pay_type="prepay_200", amount="2100.00"
            )
            cancelled_at = timezone.now() - timezone.timedelta(days=1)
            cancelled_review_id = Review.objects.create(
                client=cancelled_client,
                deal=cancelled_deal,
                dedupe_key="legacy-cancelled-review",
                status="cancelled",
                watermark_message_id=84,
                cancelled_by=actor,
                cancelled_at=cancelled_at,
                cancellation_reason="",
            ).pk

            legacy_client = Client.objects.create(
                igsid="legacy-unattributed-client", stage="payment_pending"
            )
            unattributed_review_id = Review.objects.create(
                client=legacy_client,
                dedupe_key="legacy-unattributed-review",
                status="confirmed",
            ).pk

            executor = MigrationExecutor(connection)
            executor.migrate([migrate_to])
            executor = MigrationExecutor(connection)
            new_apps = executor.loader.project_state([migrate_to]).apps
            Decision = new_apps.get_model("management", "IgPaymentReviewDecision")

            confirmed = Decision.objects.get(review_id=confirmed_review_id)
            cancelled = Decision.objects.get(review_id=cancelled_review_id)
            unattributed = Decision.objects.get(review_id=unattributed_review_id)
            print("MIGRATION_RESULT=" + json.dumps({
                "confirmed": {
                    "decision": confirmed.decision,
                    "scope": confirmed.verification_scope,
                    "actor_source": confirmed.actor_source,
                    "actor_external_id": confirmed.actor_external_id,
                    "actor_label": confirmed.actor_label,
                    "watermark": confirmed.evidence_watermark_message_id,
                    "timestamp_preserved": confirmed.created_at == confirmed_at,
                },
                "cancelled": {
                    "decision": cancelled.decision,
                    "scope": cancelled.verification_scope,
                    "actor_source": cancelled.actor_source,
                    "reason_code": cancelled.reason_code,
                    "reason_text": cancelled.reason_text,
                    "watermark": cancelled.evidence_watermark_message_id,
                    "timestamp_preserved": cancelled.created_at == cancelled_at,
                },
                "unattributed": {
                    "decision": unattributed.decision,
                    "scope": unattributed.verification_scope,
                    "actor_source": unattributed.actor_source,
                    "actor_external_id": unattributed.actor_external_id,
                    "review_id": unattributed_review_id,
                },
            }, sort_keys=True))
            """
        )
        project_root = os.path.dirname(os.path.dirname(__file__))
        env = os.environ.copy()
        for key in ("DB_ENGINE", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT"):
            env.pop(key, None)
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, (project_root, env.get("PYTHONPATH", "")))
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "migration.sqlite3")
            result = subprocess.run(
                [sys.executable, "-c", script, database_path],
                cwd=project_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        marker = next(
            line for line in result.stdout.splitlines() if line.startswith("MIGRATION_RESULT=")
        )
        payload = json.loads(marker.removeprefix("MIGRATION_RESULT="))
        self.assertEqual(
            payload["confirmed"],
            {
                "decision": "manager_verified",
                "scope": "full_payment",
                "actor_source": "telegram_user",
                "actor_external_id": "777",
                "actor_label": "owner",
                "watermark": 42,
                "timestamp_preserved": True,
            },
        )
        self.assertEqual(
            payload["cancelled"],
            {
                "decision": "manager_rejected",
                "scope": "prepayment",
                "actor_source": "management_user",
                "reason_code": "legacy_cancelled",
                "reason_text": "legacy_cancelled",
                "watermark": 84,
                "timestamp_preserved": True,
            },
        )
        self.assertEqual(
            payload["unattributed"],
            {
                "decision": "evidence_accepted_provider_unverified",
                "scope": "payment_claim",
                "actor_source": "legacy_import",
                "actor_external_id": f"review:{payload['unattributed']['review_id']}",
                "review_id": payload["unattributed"]["review_id"],
            },
        )
