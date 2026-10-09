"""W4 regression tests for manager-verified UGC promo rewards."""

import inspect

from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.models import (
    IgClient,
    IgConversationAnalysisSnapshot,
    IgDeal,
    IgOrderCustomerEvent,
    IgPaymentProjection,
    IgPostSaleCase,
    IgUgcReward,
    IgUgcRewardDelivery,
    InstagramBotMessage,
)
from management.services.ig_order_assignments import link_order_to_client
from management.services.ig_order_fulfillment import _message
from management.services.ig_ugc_rewards import (
    UgcRewardConflict,
    award_ugc_reward,
    process_external_ugc_reward_delivery,
    process_linked_ugc_reward_lifecycle_job,
)
from orders.models import Order
from storefront.models import PromoCode, PromoCodeGuestUsage


class DeliveredReviewCopyTests(SimpleTestCase):
    def test_review_copy_asks_about_the_order_before_the_verified_reward(self):
        order = type("OrderStub", (), {"order_number": "TWC-UGC-01", "pk": 1})()
        expected = {
            "uk": ("якістю", "посадкою", "після перевірки", "10%"),
            "ru": ("качеством", "посадкой", "после проверки", "10%"),
            "en": ("quality", "fit", "after we verify", "10%"),
        }

        for locale, phrases in expected.items():
            with self.subTest(locale=locale):
                text = _message("delivered_review", locale, order, "").lower()
                for phrase in phrases:
                    self.assertIn(phrase, text)
                self.assertLess(text.index(phrases[0]), text.index("10%"))

    def test_delivery_processing_reconciles_lifecycle_before_locking_delivery(self):
        source = inspect.getsource(process_external_ugc_reward_delivery)

        reward_id_lookup = 'values_list("reward_id", flat=True)'
        reconcile_call = "_reconcile_locked_ugc_reward_lifecycle("
        delivery_lock = "IgUgcRewardDelivery.objects.select_for_update()"
        self.assertIn(reward_id_lookup, source)
        self.assertLess(source.index(reward_id_lookup), source.index(reconcile_call))
        self.assertLess(source.index(reconcile_call), source.index(delivery_lock))

    def test_order_truth_callback_defers_unsupported_database_alias(self):
        from management.services.ig_order_truth import _publish_instagram_order_truth

        with patch(
            "management.services.ig_commercial_episodes.sync_episode_fulfillment"
        ) as sync_episode:
            _publish_instagram_order_truth(77, using="replica")

        sync_episode.assert_not_called()


@override_settings(
    ROOT_URLCONF="twocomms.urls_management",
    ALLOWED_HOSTS=["testserver", "management.twocomms.shop"],
)
class UgcRewardTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_superuser(
            username="ugc-reviewer",
            email="ugc-reviewer@example.com",
            password="test-password",
        )
        self.ig_client = IgClient.get_or_create_for_sender("ugc-client")
        self.delivered_at = timezone.now() - timedelta(hours=1)
        self.order = Order.objects.create(
            order_number="TWC-UGC-02",
            full_name="UGC Buyer",
            phone="380501112233",
            city="Kyiv",
            np_office="Branch 1",
            total_sum=Decimal("1000.00"),
            payment_status="paid",
            status="done",
            tracking_number="20450000000002",
            tracking_status_code=9,
            tracking_provider_event_at=self.delivered_at,
            tracking_terminal_at=self.delivered_at,
        )
        self.assignment = link_order_to_client(
            self.order,
            client=self.ig_client,
            actor=self.actor,
        )
        self.evidence = InstagramBotMessage.objects.create(
            sender_id=self.ig_client.igsid,
            client=self.ig_client,
            role=InstagramBotMessage.Role.USER,
            text="Відмітила вас у сторіс",
            attachments="https://cdn.example/story-proof.jpg",
            status=InstagramBotMessage.Status.DONE,
            provider_created_at=self.delivered_at + timedelta(minutes=1),
        )

    def _open_service_case(
        self,
        suffix,
        *,
        order=None,
        case_type=IgPostSaleCase.CaseType.RETURN,
        status=IgPostSaleCase.Status.OPEN,
    ):
        complaint = InstagramBotMessage.objects.create(
            sender_id=self.ig_client.igsid,
            client=self.ig_client,
            role=InstagramBotMessage.Role.USER,
            text="Хочу повернути замовлення",
            source="webhook",
            mid=f"ugc-delivered-open-service-case-{suffix}",
            provider_created_at=timezone.now(),
        )
        return IgPostSaleCase.objects.create(
            client=self.ig_client,
            order=order,
            source_message=complaint,
            case_type=case_type,
            status=status,
        )

    def test_lifecycle_worker_locks_client_order_promo_reward_before_job(self):
        import re
        from django.db import connection
        from management.ig_bot_models import IgUgcRewardLifecycleJob

        if not connection.features.has_select_for_update:
            self.skipTest("Actual lifecycle FOR UPDATE ordering requires the native database")
        reward = self._award()
        job = IgUgcRewardLifecycleJob.objects.create(client_id=self.ig_client.pk,
            order_id=self.order.pk, source="order_truth")
        observed = []

        def observe(execute, sql, params, many, context):
            if "for update" in sql.lower():
                table = re.search(r'\bFROM\s+[`"]?(\w+)', sql, re.IGNORECASE)
                if table:
                    observed.append(table.group(1))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(observe), patch(
            "management.services.instagram_bot.send_text",
            side_effect=AssertionError("Lifecycle reconciliation must not deliver a reward"),
        ) as send:
            result = process_linked_ugc_reward_lifecycle_job(job.pk)
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(result["selected"], 1)
        self.assertEqual(observed[:5], [IgClient._meta.db_table, Order._meta.db_table,
            PromoCode._meta.db_table, IgUgcReward._meta.db_table, IgUgcRewardLifecycleJob._meta.db_table])
        self.assertFalse(IgUgcRewardLifecycleJob.objects.filter(pk=job.pk).exists())
        reward.promo_code.refresh_from_db()
        self.assertEqual(reward.promo_code.discount_value, Decimal("10.00"))
        send.assert_not_called()

    def _award(self):
        reward, created = award_ugc_reward(
            client=self.ig_client,
            order=self.order,
            actor=self.actor,
            evidence_message_id=self.evidence.pk,
            review_note="Verified post-delivery story evidence",
        )
        self.assertTrue(created)
        return reward

    def _assert_reward_lifecycle(self, reward, *, state, reason, promo_active):
        reward.refresh_from_db()
        reward.promo_code.refresh_from_db()
        reward.delivery.refresh_from_db()
        self.assertEqual(getattr(reward, "lifecycle_state", None), state)
        self.assertEqual(getattr(reward, "lifecycle_reason", None), reason)
        self.assertEqual(reward.promo_code.is_active, promo_active)

    def test_verified_evidence_issues_one_bounded_single_use_code(self):
        before = timezone.now()

        reward, created = award_ugc_reward(
            client=self.ig_client,
            order=self.order,
            actor=self.actor,
            evidence_message_id=self.evidence.pk,
            review_note="Story mark checked in Direct",
        )

        self.assertTrue(created)
        self.assertEqual(reward.assignment_id, self.assignment.id)
        self.assertEqual(reward.assignment_version, self.assignment.version)
        self.assertEqual(reward.reviewed_by_id, self.actor.id)
        self.assertIsNotNone(reward.reviewed_at)
        self.assertEqual(reward.evidence_message_id, self.evidence.id)
        self.assertEqual(reward.review_note, "Story mark checked in Direct")
        self.assertEqual(reward.lifecycle_state, "active")
        self.assertEqual(reward.lifecycle_reason, "")
        self.assertIsNotNone(reward.lifecycle_updated_at)
        promo = reward.promo_code
        self.assertEqual(promo.discount_type, "percentage")
        self.assertEqual(promo.discount_value, Decimal("10.00"))
        self.assertEqual(promo.max_uses, 1)
        self.assertFalse(promo.one_time_per_user)
        self.assertTrue(promo.guest_redeemable)
        self.assertTrue(promo.is_guest_ugc_capability())
        self.assertTrue(promo.is_active)
        self.assertGreaterEqual(promo.valid_from, before)
        self.assertGreaterEqual(promo.valid_until, before + timedelta(days=89))
        self.assertLessEqual(promo.valid_until, before + timedelta(days=91))
        delivery = IgUgcRewardDelivery.objects.get(reward=reward)
        self.assertIn(promo.code, delivery.message_snapshot)
        self.assertIn("90", delivery.message_snapshot)

    def test_delivered_order_reward_requires_a_non_blank_manager_reason(self):
        with self.assertRaisesMessage(
            UgcRewardConflict,
            "Додайте причину підтвердження UGC.",
        ):
            award_ugc_reward(
                client=self.ig_client,
                order=self.order,
                actor=self.actor,
                evidence_message_id=self.evidence.pk,
                review_note=" \t\r\n\u00a0 ",
            )

        self.assertFalse(IgUgcReward.objects.exists())
        self.assertFalse(PromoCode.objects.exists())

    def test_manual_reward_enqueues_outbox_without_synchronous_customer_send(self):
        outbound_roles = (
            InstagramBotMessage.Role.MODEL,
            InstagramBotMessage.Role.MANAGER,
        )
        outbound_before = InstagramBotMessage.objects.filter(
            client=self.ig_client,
            role__in=outbound_roles,
        ).count()
        customer_events_before = IgOrderCustomerEvent.objects.filter(
            client=self.ig_client,
        ).count()

        award_ugc_reward(
            client=self.ig_client,
            order=self.order,
            actor=self.actor,
            evidence_message_id=self.evidence.pk,
            review_note="Verified post-delivery story evidence",
        )

        self.assertEqual(
            InstagramBotMessage.objects.filter(
                client=self.ig_client,
                role__in=outbound_roles,
            ).count(),
            outbound_before,
        )
        self.assertEqual(
            IgOrderCustomerEvent.objects.filter(client=self.ig_client).count(),
            customer_events_before,
        )
        self.assertEqual(
            IgUgcRewardDelivery.objects.filter(client=self.ig_client).count(),
            1,
        )

    def test_order_linked_outbox_failure_rolls_back_reward_promo_and_lifetime(self):
        from unittest.mock import patch

        from management.ig_bot_models import IgUgcRewardLifetime

        with patch.object(
            IgUgcRewardDelivery.objects,
            "create",
            side_effect=RuntimeError("forced order-linked outbox failure"),
        ), self.assertRaisesRegex(RuntimeError, "forced order-linked outbox failure"):
            award_ugc_reward(
                client=self.ig_client,
                order=self.order,
                actor=self.actor,
                evidence_message_id=self.evidence.pk,
                review_note="Verified post-delivery story evidence",
            )

        self.assertFalse(IgUgcReward.objects.exists())
        self.assertFalse(IgUgcRewardLifetime.objects.exists())
        self.assertFalse(IgUgcRewardDelivery.objects.exists())
        self.assertFalse(PromoCode.objects.exists())

    def test_open_service_case_blocks_delivered_order_award_without_side_effects(self):
        from management.ig_bot_models import IgUgcRewardLifetime

        self._open_service_case("new-award")

        with self.assertRaisesRegex(UgcRewardConflict, "активне звернення"):
            award_ugc_reward(
                client=self.ig_client,
                order=self.order,
                actor=self.actor,
                evidence_message_id=self.evidence.pk,
            )

        self.assertFalse(IgUgcReward.objects.exists())
        self.assertFalse(PromoCode.objects.exists())
        self.assertFalse(IgUgcRewardDelivery.objects.exists())
        self.assertFalse(IgUgcRewardLifetime.objects.exists())

    def test_existing_reward_replay_survives_late_open_service_case(self):
        from management.ig_bot_models import IgUgcRewardLifetime

        first, first_created = award_ugc_reward(
            client=self.ig_client,
            order=self.order,
            actor=self.actor,
            evidence_message_id=self.evidence.pk,
            review_note="Verified post-delivery story evidence",
        )
        self._open_service_case("idempotent-replay")

        try:
            replay, replay_created = award_ugc_reward(
                client=self.ig_client,
                order=self.order,
                actor=self.actor,
                evidence_message_id=self.evidence.pk,
            )
        except UgcRewardConflict as exc:
            self.fail(f"Idempotent reward replay was blocked: {exc}")

        self.assertTrue(first_created)
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(replay.promo_code_id, first.promo_code_id)
        self.assertEqual(IgUgcReward.objects.count(), 1)
        self.assertEqual(PromoCode.objects.count(), 1)
        self.assertEqual(IgUgcRewardDelivery.objects.count(), 1)
        self.assertEqual(IgUgcRewardLifetime.objects.count(), 1)

    def test_open_source_return_holds_sent_unused_linked_reward(self):
        reward = self._award()
        delivery = reward.delivery
        delivery.state = IgUgcRewardDelivery.State.SENT
        delivery.provider_message_ids = ["mid-ugc-linked-sent"]
        delivery.completed_at = timezone.now()
        delivery.save(
            update_fields=["state", "provider_message_ids", "completed_at", "updated_at"]
        )

        with self.captureOnCommitCallbacks(execute=True):
            self._open_service_case("linked-sent-hold", order=self.order)

        self._assert_reward_lifecycle(
            reward,
            state="held",
            reason="service_case_open",
            promo_active=False,
        )
        self.assertEqual(reward.promo_code.current_uses, 0)
        self.assertEqual(reward.delivery.state, IgUgcRewardDelivery.State.SENT)
        self.assertEqual(reward.delivery.provider_message_ids, ["mid-ugc-linked-sent"])

    def test_pending_linked_delivery_stays_waiting_while_source_case_is_open(self):
        from unittest.mock import patch

        reward = self._award()
        with self.captureOnCommitCallbacks(execute=True):
            self._open_service_case("linked-pending-hold", order=self.order)

        with patch("management.services.instagram_bot.send_text") as send:
            state = process_external_ugc_reward_delivery(reward.delivery.pk)

        self.assertEqual(state, IgUgcRewardDelivery.State.WAITING_WINDOW)
        self._assert_reward_lifecycle(
            reward,
            state="held",
            reason="service_case_open",
            promo_active=False,
        )
        self.assertEqual(reward.delivery.last_error, "service_case_open")
        send.assert_not_called()

    def test_completed_exchange_reactivates_held_unused_linked_reward(self):
        reward = self._award()
        with self.captureOnCommitCallbacks(execute=True):
            case = self._open_service_case(
                "linked-exchange-reopen",
                order=self.order,
                case_type=IgPostSaleCase.CaseType.EXCHANGE,
            )
        self._assert_reward_lifecycle(
            reward,
            state="held",
            reason="service_case_open",
            promo_active=False,
        )

        case.status = IgPostSaleCase.Status.COMPLETED
        case.resolved_at = timezone.now()
        with self.captureOnCommitCallbacks(execute=True):
            case.save(update_fields=["status", "resolved_at", "updated_at"])

        self._assert_reward_lifecycle(
            reward,
            state="active",
            reason="",
            promo_active=True,
        )
        self.assertEqual(reward.delivery.state, IgUgcRewardDelivery.State.PENDING)

    def test_completed_source_return_revokes_unused_linked_reward(self):
        reward = self._award()
        with self.captureOnCommitCallbacks(execute=True):
            case = self._open_service_case("linked-return-revoke", order=self.order)

        case.status = IgPostSaleCase.Status.COMPLETED
        case.resolved_at = timezone.now()
        with self.captureOnCommitCallbacks(execute=True):
            case.save(update_fields=["status", "resolved_at", "updated_at"])

        self._assert_reward_lifecycle(
            reward,
            state="revoked",
            reason="source_order_returned",
            promo_active=False,
        )
        self.assertEqual(reward.promo_code.current_uses, 0)
        self.assertEqual(reward.delivery.state, IgUgcRewardDelivery.State.FAILED)
        self.assertEqual(reward.delivery.last_error, "source_order_returned")
        self.assertIsNotNone(reward.delivery.completed_at)

    def test_source_order_cancellation_hook_revokes_unused_linked_reward(self):
        reward = self._award()
        self.order.status = "cancelled"

        with self.captureOnCommitCallbacks(execute=True):
            self.order.save(update_fields=["status", "updated"])

        self._assert_reward_lifecycle(
            reward,
            state="revoked",
            reason="source_order_cancelled",
            promo_active=False,
        )
        self.assertEqual(reward.promo_code.current_uses, 0)

        self.order.status = "done"
        with self.captureOnCommitCallbacks(execute=True):
            self.order.save(update_fields=["status", "updated"])
        self._assert_reward_lifecycle(
            reward,
            state="revoked",
            reason="source_order_cancelled",
            promo_active=False,
        )

    def test_full_refund_projection_hook_revokes_unused_linked_reward(self):
        reward = self._award()
        deal = IgDeal.objects.create(
            client=self.ig_client,
            order=self.order,
            amount=self.order.total_sum,
            status=IgDeal.Status.ORDER_CREATED,
            payment_truth=IgDeal.PaymentTruth.CONFIRMED,
            paid_amount=self.order.total_sum,
        )

        with self.captureOnCommitCallbacks(execute=True):
            IgPaymentProjection.objects.create(
                deal=deal,
                client=self.ig_client,
                truth=IgDeal.PaymentTruth.REFUNDED,
                gross_amount=self.order.total_sum,
                refunded_amount=self.order.total_sum,
            )

        self._assert_reward_lifecycle(
            reward,
            state="revoked",
            reason="source_order_refunded",
            promo_active=False,
        )
        self.assertEqual(reward.promo_code.current_uses, 0)

    def test_failed_refund_hook_is_recovered_from_a_durable_event_queue(self):
        from management.services.ig_follow_reconcile import (
            reconcile_follow_intelligence_once,
        )

        reward = self._award()
        reward.delivery.state = IgUgcRewardDelivery.State.SENT
        reward.delivery.completed_at = timezone.now()
        reward.delivery.provider_message_ids = ["ugc-provider-receipt"]
        reward.delivery.save(update_fields=[
            "state",
            "completed_at",
            "provider_message_ids",
            "updated_at",
        ])
        deal = IgDeal.objects.create(
            client=self.ig_client,
            order=self.order,
            amount=self.order.total_sum,
            status=IgDeal.Status.ORDER_CREATED,
            payment_truth=IgDeal.PaymentTruth.CONFIRMED,
            paid_amount=self.order.total_sum,
        )

        with patch(
            "management.services.ig_ugc_rewards.reconcile_linked_ugc_rewards",
            side_effect=RuntimeError("synthetic lifecycle hook failure"),
        ), self.captureOnCommitCallbacks(execute=True):
            IgPaymentProjection.objects.create(
                deal=deal,
                client=self.ig_client,
                truth=IgDeal.PaymentTruth.REFUNDED,
                gross_amount=self.order.total_sum,
                refunded_amount=self.order.total_sum,
            )

        reward.refresh_from_db()
        self.assertEqual(reward.lifecycle_state, "active")

        counts = reconcile_follow_intelligence_once(
            limit=10,
            now=timezone.now() + timedelta(minutes=10),
        )

        self._assert_reward_lifecycle(
            reward,
            state="revoked",
            reason="source_order_refunded",
            promo_active=False,
        )
        self.assertEqual(reward.delivery.state, IgUgcRewardDelivery.State.SENT)
        self.assertEqual(counts.get("ugc_lifecycle_selected"), 1)
        replay = reconcile_follow_intelligence_once(
            limit=10,
            now=timezone.now() + timedelta(minutes=20),
        )
        self.assertEqual(replay.get("ugc_lifecycle_selected"), 0)

    def test_redeemed_code_is_never_restored_by_partial_or_full_refund(self):
        reward = self._award()
        reward.promo_code.current_uses = 1
        reward.promo_code.save(update_fields=["current_uses", "updated_at"])
        redemption_order = Order.objects.create(
            order_number="TWC-UGC-REDEMPTION",
            full_name="UGC repeat buyer",
            phone="380501112277",
            city="Kyiv",
            np_office="Branch 7",
            total_sum=Decimal("900.00"),
            discount_amount=Decimal("100.00"),
            promo_code=reward.promo_code,
            payment_status="paid",
            status="new",
        )
        PromoCodeGuestUsage.objects.create(
            promo_code=reward.promo_code,
            reservation_key="ugc-redeemed-lifecycle",
            order=redemption_order,
            state=PromoCodeGuestUsage.State.CONSUMED,
            consumed_at=timezone.now(),
        )
        deal = IgDeal.objects.create(
            client=self.ig_client,
            order=self.order,
            amount=self.order.total_sum,
            status=IgDeal.Status.ORDER_CREATED,
            payment_truth=IgDeal.PaymentTruth.CONFIRMED,
            paid_amount=self.order.total_sum,
        )
        with self.captureOnCommitCallbacks(execute=True):
            projection = IgPaymentProjection.objects.create(
                deal=deal,
                client=self.ig_client,
                truth=IgDeal.PaymentTruth.PARTIALLY_REFUNDED,
                gross_amount=self.order.total_sum,
                refunded_amount=Decimal("100.00"),
            )

        self._assert_reward_lifecycle(
            reward,
            state="active",
            reason="",
            promo_active=True,
        )
        self.assertEqual(reward.promo_code.current_uses, 1)

        projection.truth = IgDeal.PaymentTruth.REFUNDED
        projection.refunded_amount = self.order.total_sum
        with self.captureOnCommitCallbacks(execute=True):
            projection.save(update_fields=["truth", "refunded_amount", "updated_at"])

        self._assert_reward_lifecycle(
            reward,
            state="active",
            reason="",
            promo_active=True,
        )
        self.assertEqual(reward.promo_code.current_uses, 1)

    def test_full_refund_revokes_reserved_code_without_releasing_capacity(self):
        reward = self._award()
        reward.promo_code.current_uses = 1
        reward.promo_code.save(update_fields=["current_uses", "updated_at"])
        reservation = PromoCodeGuestUsage.objects.create(
            promo_code=reward.promo_code,
            reservation_key="ugc-reserved-before-source-refund",
            state=PromoCodeGuestUsage.State.RESERVED,
        )
        deal = IgDeal.objects.create(
            client=self.ig_client,
            order=self.order,
            amount=self.order.total_sum,
            status=IgDeal.Status.ORDER_CREATED,
            payment_truth=IgDeal.PaymentTruth.CONFIRMED,
            paid_amount=self.order.total_sum,
        )

        with self.captureOnCommitCallbacks(execute=True):
            IgPaymentProjection.objects.create(
                deal=deal,
                client=self.ig_client,
                truth=IgDeal.PaymentTruth.REFUNDED,
                gross_amount=self.order.total_sum,
                refunded_amount=self.order.total_sum,
            )

        self._assert_reward_lifecycle(
            reward,
            state="revoked",
            reason="source_order_refunded",
            promo_active=False,
        )
        reservation.refresh_from_db()
        self.assertEqual(reward.promo_code.current_uses, 1)
        self.assertEqual(reservation.state, PromoCodeGuestUsage.State.RESERVED)

    def test_late_paid_exact_reservation_consumes_without_reactivating_revoked_reward(self):
        from orders.models import PaymentAttempt
        from orders.promo_reservations import (
            consume_payment_attempt_promo,
            reserve_promo_for_checkout,
        )

        reward = self._award()
        reservation = reserve_promo_for_checkout(
            code=reward.promo_code.code,
            user=None,
            total_amount=Decimal("900.00"),
        )
        guest_usage = PromoCodeGuestUsage.objects.get(promo_code=reward.promo_code)
        attempt = PaymentAttempt.objects.create(
            fingerprint="ugc-revoked-late-paid-reservation",
            user=None,
            full_name="Late paid UGC buyer",
            phone="380501112266",
            city="Kyiv",
            np_office="Branch 6",
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.PROCESSING,
            cart_snapshot={},
            gross_amount=Decimal("900.00"),
            discount_amount=Decimal("90.00"),
            payable_amount=Decimal("810.00"),
            payment_amount=Decimal("810.00"),
            promo_code=reward.promo_code,
            monobank_invoice_id="ugc-revoked-late-paid-invoice",
            event_state=reservation.event_state,
        )
        deal = IgDeal.objects.create(
            client=self.ig_client,
            order=self.order,
            amount=self.order.total_sum,
            status=IgDeal.Status.ORDER_CREATED,
            payment_truth=IgDeal.PaymentTruth.CONFIRMED,
            paid_amount=self.order.total_sum,
        )
        with self.captureOnCommitCallbacks(execute=True):
            IgPaymentProjection.objects.create(
                deal=deal,
                client=self.ig_client,
                truth=IgDeal.PaymentTruth.REFUNDED,
                gross_amount=self.order.total_sum,
                refunded_amount=self.order.total_sum,
            )
        self._assert_reward_lifecycle(
            reward,
            state="revoked",
            reason="source_order_refunded",
            promo_active=False,
        )

        paid_order = Order.objects.create(
            order_number="TWC-UGC-REVOKED-PAID",
            full_name="Late paid UGC buyer",
            phone="380501112266",
            city="Kyiv",
            np_office="Branch 6",
            total_sum=Decimal("900.00"),
            discount_amount=Decimal("90.00"),
            promo_code=reward.promo_code,
            payment_status="paid",
            status="new",
        )
        self.assertTrue(consume_payment_attempt_promo(attempt, order=paid_order))

        guest_usage.refresh_from_db()
        self.assertEqual(guest_usage.state, PromoCodeGuestUsage.State.CONSUMED)
        self.assertEqual(guest_usage.order_id, paid_order.pk)
        self._assert_reward_lifecycle(
            reward,
            state="revoked",
            reason="source_order_refunded",
            promo_active=False,
        )

    def test_support_snapshot_holds_unused_linked_reward(self):
        reward = self._award()

        with self.captureOnCommitCallbacks(execute=True):
            IgConversationAnalysisSnapshot.objects.create(
                client=self.ig_client,
                dedupe_key="ugc-linked-support-hold",
                score_band=IgConversationAnalysisSnapshot.Band.PAID,
                interaction_type=(
                    IgConversationAnalysisSnapshot.InteractionType.SUPPORT_COMPLAINT
                ),
            )

        self._assert_reward_lifecycle(
            reward,
            state="held",
            reason="service_case_open",
            promo_active=False,
        )

    def test_unrelated_return_does_not_change_external_ugc_reward(self):
        promo = PromoCode.objects.create(
            code="UGCEXTERNAL1",
            promo_type="regular",
            discount_type="percentage",
            discount_value=Decimal("10.00"),
            max_uses=1,
            one_time_per_user=False,
            guest_redeemable=True,
            valid_from=timezone.now(),
            valid_until=timezone.now() + timedelta(days=90),
            is_active=True,
        )
        external = IgUgcReward.objects.create(
            client=self.ig_client,
            order=None,
            assignment=None,
            assignment_version=0,
            evidence_type=IgUgcReward.EvidenceType.STORY_MENTION,
            evidence_fingerprint="external-unrelated-return".ljust(64, "0"),
            promo_code=promo,
            reviewed_by=self.actor,
            reward_path="external_ugc",
            decision_source="manager",
        )
        IgUgcRewardDelivery.objects.create(
            reward=external,
            client=self.ig_client,
            message_snapshot="external reward",
        )
        unrelated = Order.objects.create(
            order_number="TWC-UGC-OTHER-RETURN",
            full_name="Other purchase",
            phone="380501112299",
            city="Kyiv",
            np_office="Branch 9",
            total_sum=Decimal("800.00"),
            payment_status="paid",
            status="done",
        )

        with self.captureOnCommitCallbacks(execute=True):
            self._open_service_case("external-unrelated", order=unrelated)

        self._assert_reward_lifecycle(
            external,
            state="active",
            reason="",
            promo_active=True,
        )

    def test_unrelated_return_does_not_hold_linked_reward_for_another_order(self):
        reward = self._award()
        unrelated = Order.objects.create(
            order_number="TWC-UGC-LINKED-RET",
            full_name="Other linked purchase",
            phone="380501112288",
            city="Kyiv",
            np_office="Branch 8",
            total_sum=Decimal("850.00"),
            payment_status="paid",
            status="done",
        )

        with self.captureOnCommitCallbacks(execute=True):
            self._open_service_case("linked-unrelated", order=unrelated)

        self._assert_reward_lifecycle(
            reward,
            state="active",
            reason="",
            promo_active=True,
        )

    def test_same_evidence_is_idempotent_and_never_creates_a_second_code(self):
        first, first_created = award_ugc_reward(
            client=self.ig_client,
            order=self.order,
            actor=self.actor,
            evidence_message_id=self.evidence.pk,
            review_note="Verified post-delivery story evidence",
        )

        second, second_created = award_ugc_reward(
            client=self.ig_client,
            order=self.order,
            actor=self.actor,
            evidence_message_id=self.evidence.pk,
            review_note="Verified post-delivery story evidence",
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(PromoCode.objects.count(), 1)
        self.assertEqual(IgUgcReward.objects.count(), 1)

    def test_same_instagram_url_ignores_tracking_query_for_idempotency(self):
        first, first_created = award_ugc_reward(
            client=self.ig_client,
            order=self.order,
            actor=self.actor,
            evidence_url="https://www.instagram.com/p/UGC123/?utm_source=share",
            review_note="Verified post-delivery Instagram evidence",
        )

        second, second_created = award_ugc_reward(
            client=self.ig_client,
            order=self.order,
            actor=self.actor,
            evidence_url="https://www.instagram.com/p/UGC123/?igsh=tracking-token",
            review_note="Verified post-delivery Instagram evidence",
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(PromoCode.objects.count(), 1)

    def test_different_evidence_cannot_issue_a_second_code_for_the_same_order(self):
        award_ugc_reward(
            client=self.ig_client,
            order=self.order,
            actor=self.actor,
            evidence_message_id=self.evidence.pk,
            review_note="Verified post-delivery story evidence",
        )

        with self.assertRaises(UgcRewardConflict):
            award_ugc_reward(
                client=self.ig_client,
                order=self.order,
                actor=self.actor,
                evidence_url="https://www.instagram.com/stories/twocomms/123456789/",
            )

        self.assertEqual(PromoCode.objects.count(), 1)

    def test_unassigned_order_is_rejected(self):
        other_order = Order.objects.create(
            order_number="TWC-UGC-UNASSIGNED",
            full_name="Other Buyer",
            phone="380501112244",
            city="Kyiv",
            np_office="Branch 2",
            total_sum=Decimal("900.00"),
            payment_status="paid",
            status="done",
        )

        with self.assertRaises(UgcRewardConflict):
            award_ugc_reward(
                client=self.ig_client,
                order=other_order,
                actor=self.actor,
                evidence_message_id=self.evidence.pk,
            )

    def test_reward_is_rejected_until_the_assigned_order_is_done(self):
        for status in ("new", "prep", "ship", "cancelled"):
            with self.subTest(status=status):
                self.order.status = status
                self.order.save(update_fields=["status"])

                with self.assertRaisesMessage(
                    UgcRewardConflict,
                    "Нагороду можна видати лише після підтвердженого отримання замовлення.",
                ):
                    award_ugc_reward(
                        client=self.ig_client,
                        order=self.order,
                        actor=self.actor,
                        evidence_message_id=self.evidence.pk,
                    )

        self.assertFalse(IgUgcReward.objects.exists())
        self.assertFalse(PromoCode.objects.exists())

    def test_manual_done_without_carrier_delivery_is_rejected(self):
        self.order.tracking_status_code = 7
        self.order.tracking_terminal_at = None
        self.order.save(
            update_fields=["tracking_status_code", "tracking_terminal_at"]
        )

        with self.assertRaisesMessage(
            UgcRewardConflict,
            "Нагороду можна видати лише після підтвердженого отримання замовлення.",
        ):
            award_ugc_reward(
                client=self.ig_client,
                order=self.order,
                actor=self.actor,
                evidence_message_id=self.evidence.pk,
            )

        self.assertFalse(IgUgcReward.objects.exists())
        self.assertFalse(PromoCode.objects.exists())

    def test_direct_evidence_before_provider_delivery_event_is_rejected(self):
        self.evidence.provider_created_at = self.delivered_at - timedelta(seconds=1)
        self.evidence.save(update_fields=["provider_created_at"])

        with self.assertRaisesMessage(
            UgcRewardConflict,
            "Доказ Direct має бути створений після підтвердженого отримання замовлення.",
        ):
            award_ugc_reward(
                client=self.ig_client,
                order=self.order,
                actor=self.actor,
                evidence_message_id=self.evidence.pk,
            )

        self.assertFalse(IgUgcReward.objects.exists())
        self.assertFalse(PromoCode.objects.exists())

    def test_provider_delivery_time_wins_over_delayed_terminal_polling_time(self):
        provider_delivery_at = timezone.now() - timedelta(hours=2)
        self.order.tracking_provider_event_at = provider_delivery_at
        self.order.tracking_terminal_at = timezone.now()
        self.order.save(
            update_fields=["tracking_provider_event_at", "tracking_terminal_at"]
        )
        self.evidence.provider_created_at = provider_delivery_at + timedelta(minutes=5)
        self.evidence.save(update_fields=["provider_created_at"])

        reward, created = award_ugc_reward(
            client=self.ig_client,
            order=self.order,
            actor=self.actor,
            evidence_message_id=self.evidence.pk,
            review_note="Verified post-delivery story evidence",
        )

        self.assertTrue(created)
        self.assertEqual(reward.evidence_message_id, self.evidence.pk)

    def test_staff_endpoint_is_idempotent_and_client_detail_exposes_reward(self):
        self.client.force_login(self.actor)
        url = reverse(
            "management_bot_client_ugc_reward_api",
            args=[self.ig_client.pk],
        )
        payload = {
            "order_id": self.order.pk,
            "evidence_message_id": self.evidence.pk,
            "review_note": "Checked",
        }

        first = self.client.post(url, payload)
        second = self.client.post(url, payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_json = first.json()
        second_json = second.json()
        self.assertTrue(first_json["created"])
        self.assertFalse(second_json["created"])
        self.assertEqual(
            first_json["reward"]["promo_code"],
            second_json["reward"]["promo_code"],
        )

        detail = self.client.get(
            reverse("management_bot_client_detail_api", args=[self.ig_client.pk])
        )
        self.assertEqual(detail.status_code, 200)
        ugc = detail.json()["ugc_rewards"]
        self.assertFalse(ugc["reward_eligible"])
        self.assertEqual(ugc["eligibility_reason"], "already_rewarded")
        self.assertEqual(ugc["items"][0]["order_id"], self.order.id)
        self.assertFalse(ugc["items"][0]["reward_eligible"])
        self.assertEqual(
            ugc["items"][0]["eligibility_reason"],
            "already_rewarded",
        )
        self.assertEqual(
            ugc["items"][0]["promo_code"],
            first_json["reward"]["promo_code"],
        )
        self.assertEqual(ugc["award_url"], url)

    def test_staff_endpoint_rejects_blank_delivered_reward_reason(self):
        self.client.force_login(self.actor)
        response = self.client.post(
            reverse(
                "management_bot_client_ugc_reward_api",
                args=[self.ig_client.pk],
            ),
            {
                "order_id": self.order.pk,
                "evidence_message_id": self.evidence.pk,
                "review_note": " \t\r\n\u00a0 ",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Додайте причину підтвердження UGC.")
        self.assertFalse(IgUgcReward.objects.exists())
        self.assertFalse(PromoCode.objects.exists())

    def test_detail_marks_delivered_unrewarded_order_as_reward_eligible(self):
        self.client.force_login(self.actor)

        detail = self.client.get(
            reverse("management_bot_client_detail_api", args=[self.ig_client.pk])
        )

        self.assertEqual(detail.status_code, 200)
        payload = detail.json()
        self.assertTrue(payload["ugc_rewards"]["reward_eligible"])
        self.assertEqual(
            payload["ugc_rewards"]["eligibility_reason"],
            "delivered_order_eligible",
        )
        assignment = payload["orders"]["assignments"][0]
        self.assertTrue(assignment["reward_eligible"])
        self.assertEqual(
            assignment["eligibility_reason"],
            "delivered_order_eligible",
        )

    def test_dashboard_contains_the_manager_ugc_action(self):
        self.client.force_login(self.actor)

        response = self.client.get(reverse("management_bot"))

        self.assertContains(response, "renderUgcRewards")
        self.assertContains(response, "Підтвердити UGC і видати -10%")
        self.assertContains(response, "item.order.status==='done'")

    def test_identical_ugc_lifecycle_signals_are_deduplicated_before_commit(self):
        from management.ig_bot_models import IgUgcRewardLifecycleJob
        from management.services.ig_order_truth import _schedule_ugc_reward_event

        self._award()

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            with transaction.atomic():
                first_id = _schedule_ugc_reward_event(
                    order_id=self.order.pk,
                    source="order_truth",
                )
                second_id = _schedule_ugc_reward_event(
                    order_id=self.order.pk,
                    source="payment_projection",
                )

        self.assertEqual(first_id, second_id)
        self.assertEqual(IgUgcRewardLifecycleJob.objects.count(), 1)
        self.assertEqual(len(callbacks), 1)
        job = IgUgcRewardLifecycleJob.objects.get(pk=first_id)
        self.assertEqual(job.source, "order_truth")

    def test_payment_projection_lookup_uses_signal_database_alias(self):
        from management.services.ig_order_truth import (
            publish_ugc_reward_payment_truth,
        )

        manager = Mock()
        manager.using.return_value.filter.return_value.values_list.return_value.first.return_value = (
            self.order.pk
        )
        instance = type("ProjectionStub", (), {"pk": 321})()

        with patch.object(IgPaymentProjection, "objects", manager), patch(
            "management.services.ig_order_truth._schedule_ugc_reward_event"
        ) as schedule:
            publish_ugc_reward_payment_truth(
                IgPaymentProjection,
                instance,
                using="replica",
            )

        manager.using.assert_called_once_with("replica")
        schedule.assert_called_once_with(
            order_id=self.order.pk,
            source="payment_projection",
            using="replica",
        )

    def test_lifecycle_callback_preserves_database_alias(self):
        from management.services.ig_order_truth import _schedule_ugc_reward_event

        self._award()

        with patch(
            "management.services.ig_order_truth._reconcile_ugc_reward_event"
        ) as reconcile, patch(
            "management.services.ig_order_truth.transaction.on_commit"
        ) as on_commit:
            job_id = _schedule_ugc_reward_event(
                order_id=self.order.pk,
                source="order_truth",
                using="default",
            )
            callback = on_commit.call_args.args[0]
            callback()

        reconcile.assert_called_once_with(job_id, using="default")

    def test_lifecycle_callback_processes_the_signal_database_alias(self):
        from management.services.ig_order_truth import _reconcile_ugc_reward_event

        expected = {"state": "done", "selected": 1}
        with patch(
            "management.services.ig_ugc_rewards."
            "process_linked_ugc_reward_lifecycle_job",
            return_value=expected,
        ) as process:
            result = _reconcile_ugc_reward_event(321, using="replica")

        process.assert_called_once_with(321, using="replica")
        self.assertEqual(result, expected)

    def test_stale_numeric_lifecycle_targets_complete_without_retries(self):
        from management.ig_bot_models import IgUgcRewardLifecycleJob

        stale_targets = (
            {"order_id": 900_000_001, "client_id": None},
            {"order_id": None, "client_id": 900_000_002},
        )
        for target in stale_targets:
            with self.subTest(target=target):
                job = IgUgcRewardLifecycleJob.objects.create(
                    source="stale_target",
                    **target,
                )

                result = process_linked_ugc_reward_lifecycle_job(job.pk)

                self.assertEqual(result["state"], "done")
                self.assertEqual(result["selected"], 0)
                self.assertFalse(
                    IgUgcRewardLifecycleJob.objects.filter(pk=job.pk).exists()
                )
