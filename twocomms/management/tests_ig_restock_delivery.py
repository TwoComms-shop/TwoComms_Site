"""Native restock dispatch cannot borrow consent, stock or future capability."""
from datetime import timedelta
import json
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, transaction, OperationalError, IntegrityError
from django.test import TransactionTestCase, override_settings

from management.models import IgClient, IgFollowUpTask, IgCustomerTurnRevision, IgFunnelResetAudit, InstagramBotMessage
from management.ig_restock_subscription_models import IgRestockAvailabilityEvent, IgRestockSubscription
from management.services import ig_restock_subscriptions as restock
from management.services.ig_restock_availability import observe_target, observe_configuration
from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, SITE_BASE_URL="https://twocomms.test")
class NativeRestockDeliveryTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        # This component suite substitutes its manager-admission adapter below.
        # Cross-purpose signed-manager coordination is exercised without this
        # substitution in NativeAvailabilityCoordinationTests.
        self.coordination = patch(
            "management.services.ig_fulfillment_notification_coordination.restock_update_coordination",
            return_value={"reason": ""})
        self.coordination.start()
        self.addCleanup(self.coordination.stop)

    def manager_ready(self, subscription, observation, *, now):
        inv = subscription.accepted_answer.invitation
        configuration = observe_configuration(inv.target, now=now)
        if (observation["target"] != inv.target or configuration["status"] != "valid"
            or configuration["configuration_digest"] != self.approved_configuration_digest):
            return {"ready": False, "reason": "manager_review_stale", "proof": {}}
        return {"ready": True, "reason": "", "proof": {"version": 1, "decision_id": "signed-manager-decision-7",
            "decision_version": 1, "decision_digest": "b" * 64, "request_digest": "a" * 64,
            "subscription_id": str(subscription.pk), "target": inv.target, "checked_quantity": inv.target["quantity"],
            "supply_basis": self.approved_basis, "configuration_digest": self.approved_configuration_digest,
            "inventory_digest": self.approved_inventory_digest}}

    def available(self, *, quantity=2):
        self.variant.stock = quantity
        self.variant.save(update_fields=["stock"])
        self.rule.stock = quantity
        self.rule.save(update_fields=["stock", "updated_at"])

    def event(self, *, accessible_unrecorded=False, unknown_recorded_stock=False):
        inv, subscription = self.accepted()
        if not accessible_unrecorded:
            self.available()
        if unknown_recorded_stock:
            self.rule.stock = None
            self.rule.save(update_fields=["stock", "updated_at"])
        now = self.now + timedelta(seconds=2)
        observation = observe_target(inv.target, now=now).as_dict()
        self.approved_basis = "accessible_unrecorded" if accessible_unrecorded else "warehouse_checked"
        self.approved_inventory_digest = restock._digest({key: observation[key] for key in ("target", "evidence", "status", "reason")})
        configuration = observe_configuration(inv.target, now=now)
        self.assertEqual(configuration["status"], "valid", configuration)
        self.approved_configuration_digest = configuration["configuration_digest"]
        with patch("management.services.ig_fulfillment_reviews.ensure_restock_manager_review", return_value=None), \
                patch.object(restock, "manager_admission", side_effect=self.manager_ready):
            outcome = restock.reconcile_subscriptions(now=now)
        self.assertEqual(outcome["created"], 1, outcome)
        event = IgRestockAvailabilityEvent.objects.get(subscription=subscription)
        return inv, subscription, event.task

    def dispatch(self, task, *, at=None, before_http=None, outcome="sent", approval=None):
        from management.services import instagram_bot as bot
        now = at or self.now + timedelta(seconds=3)
        # Complete only pre-existing customer input; callback-created input must
        # retain its pending state and stop the imminent physical request.
        InstagramBotMessage.objects.filter(client=self.client_row, role="user").update(status="done")
        IgCustomerTurnRevision.objects.filter(client=self.client_row).update(state="processed",
            active_slot=None, claim_token="", lease_until=None)
        physical = []
        def lookup(_settings, **kwargs):
            if before_http:
                before_http()
            return "synthetic-native-restock-token"
        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            if outcome == "unknown":
                raise TimeoutError("request reached provider; native result unknown")
            return 200, json.dumps({"message_id": "native-one-restock-notification"})
        with patch("django.utils.timezone.now", return_value=now), \
                patch.object(bot, "get_page_token", side_effect=lookup), \
                patch.object(bot, "_provider_http", side_effect=provider), \
                patch.object(restock, "manager_admission", side_effect=approval or self.manager_ready):
            count = restock.process_due_restock_notifications(self.settings, now=now, limit=50)
        return count, physical

    def test_inventory_alone_never_creates_customer_task_without_manager_approval(self):
        _inv, subscription = self.accepted()
        self.available()
        with patch("management.services.ig_fulfillment_reviews.ensure_restock_manager_review", return_value=None) as review, \
                patch.object(restock, "manager_admission", return_value={"ready": False, "reason": "manager_review_pending", "proof": {}}):
            outcome = restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))
        review.assert_called_once()
        self.assertEqual(outcome["created"], 0)
        self.assertFalse(IgRestockAvailabilityEvent.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.filter(reason=restock.ORIGIN).exists())
        subscription.refresh_from_db()
        self.assertEqual(subscription.last_reason, "manager_review_pending")

    def test_one_event_one_native_message_does_not_advance_marketing_policy(self):
        inv, subscription, task = self.event()
        original_level = self.client_row.followup_level
        count, physical = self.dispatch(task)
        self.assertEqual((count, len(physical)), (1, 1))
        task.refresh_from_db()
        subscription.refresh_from_db()
        self.client_row.refresh_from_db()
        self.assertEqual((task.status, subscription.state), ("sent", "sent"))
        self.assertEqual(self.client_row.followup_level, original_level)
        self.assertEqual(task.provider_message_id, "native-one-restock-notification")
        self.assertNotIn("quick_replies", physical[0]["message"])
        self.assertIn(inv.envelope["label"], physical[0]["message"]["text"])
        self.assertEqual(IgRestockAvailabilityEvent.objects.count(), 1)
        count, physical = self.dispatch(task, at=self.now + timedelta(minutes=1))
        self.assertEqual((count, physical), (0, []))

    def test_signed_accessible_unrecorded_goods_can_notify_without_minting_stock_or_invoice(self):
        from orders.models import PaymentAttempt
        inv, subscription, task = self.event(accessible_unrecorded=True)
        event = IgRestockAvailabilityEvent.objects.get(subscription=subscription)
        self.assertEqual(event.envelope["availability"]["status"], "unavailable")
        self.assertEqual(event.envelope["manager_decision"]["supply_basis"], "accessible_unrecorded")
        count, physical = self.dispatch(task)
        self.assertEqual((count, len(physical)), (1, 1))
        self.rule.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual((self.rule.stock, self.variant.stock), (0, 0))
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertIn(inv.envelope["label"], physical[0]["message"]["text"])
        self.assertIn("Менеджер", physical[0]["message"]["text"])

    def test_signed_exact_target_manager_ready_can_override_unknown_stock_record(self):
        _inv, subscription, task = self.event(accessible_unrecorded=True, unknown_recorded_stock=True)
        event = IgRestockAvailabilityEvent.objects.get(subscription=subscription)
        self.assertEqual(event.envelope["availability"]["status"], "unknown")
        count, physical = self.dispatch(task)
        self.assertEqual((count, len(physical)), (1, 1))
        self.rule.refresh_from_db()
        self.assertIsNone(self.rule.stock)

    def test_configuration_changed_during_token_lookup_blocks_physical_http(self):
        _inv, _subscription, task = self.event()
        def depleted():
            from storefront.models import ProductFitOption
            fit = ProductFitOption.objects.get(product=self.product, code="classic")
            fit.is_active = False
            fit.save(update_fields=["is_active"])
        count, physical = self.dispatch(task, before_http=depleted)
        self.assertEqual((count, physical), (0, []))
        task.refresh_from_db()
        self.assertNotEqual(task.status, "ambiguous")
        self.assertEqual(task.provider_message_id, "")
        self.assertEqual(task.skip_reason, "manager_review_stale")

    def test_catalogue_recount_does_not_silently_revoke_manager_accessible_goods(self):
        _inv, _subscription, task = self.event(accessible_unrecorded=True)
        count, physical = self.dispatch(task, before_http=lambda: self.available(quantity=3))
        self.assertEqual((count, len(physical)), (1, 1))

    def test_pending_paid_stop_or_complaint_customer_source_before_http_waits(self):
        _inv, _subscription, task = self.event()
        for index, text in enumerate(("Я оплатил", "Стоп, не пишите", "Є скарга на замовлення"), 1):
            with self.subTest(text=text):
                def pending():
                    self.source(text, at=self.now + timedelta(seconds=3))
                count, physical = self.dispatch(task, before_http=pending, at=self.now + timedelta(minutes=20 * index))
                self.assertEqual((count, physical), (0, []))
                task.refresh_from_db()
                self.assertEqual(task.status, "pending")
                self.assertEqual(task.skip_reason, "pending_inbound")

    def test_fresh_manager_denial_at_http_does_not_become_unknown(self):
        _inv, _subscription, task = self.event()
        allowed = {"value": True}
        def approval(subscription, observation, *, now):
            return self.manager_ready(subscription, observation, now=now) if allowed["value"] else {
                "ready": False, "reason": "manager_review_stale", "proof": {}}
        count, physical = self.dispatch(task, before_http=lambda: allowed.update(value=False), approval=approval)
        self.assertEqual((count, physical), (0, []))
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.skip_reason, "manager_review_stale")

    def test_renewed_manager_decision_for_same_target_can_release_one_historical_event(self):
        _inv, _subscription, task = self.event()
        original = IgRestockAvailabilityEvent.objects.get(task=task).envelope["manager_decision"]
        def renewed(subscription, observation, *, now):
            ready = self.manager_ready(subscription, observation, now=now)
            ready["proof"]["decision_id"] = 8
            ready["proof"]["decision_version"] = 2
            return ready
        count, physical = self.dispatch(task, approval=renewed)
        self.assertEqual((count, len(physical)), (1, 1))
        self.assertEqual(IgRestockAvailabilityEvent.objects.get(task=task).envelope["manager_decision"], original)

    def test_expired_or_wrong_target_manager_decision_keeps_waiting_without_http(self):
        _inv, _subscription, task = self.event()
        for index, reason in enumerate(("manager_review_expired", "manager_review_stale", "manager_review_unavailable"), 1):
            with self.subTest(reason=reason):
                count, physical = self.dispatch(task, at=self.now + timedelta(minutes=20 * index),
                    approval=lambda *_args, **_kwargs: {"ready": False, "reason": reason, "proof": {}})
                self.assertEqual((count, physical), (0, []))
                task.refresh_from_db()
                self.assertEqual(task.status, "pending")

    def test_outside_real_user_window_waits_despite_fresh_crm_cache(self):
        _inv, subscription, task = self.event()
        due = self.now + timedelta(hours=24)
        IgClient.objects.filter(pk=self.client_row.pk).update(last_user_message_at=due)
        count, physical = self.dispatch(task, at=due)
        self.assertEqual((count, physical), (0, []))
        task.refresh_from_db()
        self.assertEqual((task.status, task.skip_reason), ("pending", "waiting_window"))
        subscription.refresh_from_db()
        self.assertEqual(subscription.state, "active")
        self.assertIsNone(task.sent_at)

    def test_quiet_hours_hold_existing_event_without_due_time_shift(self):
        _inv, _subscription, task = self.event()
        original_due = task.due_at
        count, physical = self.dispatch(task, at=self.now + timedelta(hours=10))
        self.assertEqual((count, physical), (0, []))
        task.refresh_from_db()
        self.assertEqual((task.status, task.skip_reason, task.due_at), ("pending", "quiet_hours", original_due))

    def test_no_buy_at_last_physical_boundary_sends_nothing(self):
        _inv, _subscription, task = self.event()
        count, physical = self.dispatch(task, before_http=lambda: IgClient.objects.filter(pk=self.client_row.pk).update(primary_objection="no_buy"))
        self.assertEqual((count, physical), (0, []))
        task.refresh_from_db()
        self.assertNotEqual(task.status, "ambiguous")

    def test_pause_at_last_physical_boundary_sends_nothing(self):
        _inv, _subscription, task = self.event()
        count, physical = self.dispatch(task, before_http=lambda: IgClient.objects.filter(pk=self.client_row.pk).update(bot_paused=True))
        self.assertEqual((count, physical), (0, []))

    def test_privacy_erasure_at_last_physical_boundary_sends_nothing(self):
        _inv, _subscription, task = self.event()
        count, physical = self.dispatch(task, before_http=lambda: IgClient.objects.filter(pk=self.client_row.pk).update(privacy_erasure_started_at=self.now))
        self.assertEqual((count, physical), (0, []))
        task.refresh_from_db()
        self.assertEqual(task.provider_message_id, "")

    def test_provider_account_change_denies_even_when_caller_settings_object_is_stale(self):
        from management.models import InstagramBotSettings
        _inv, _subscription, task = self.event()
        count, physical = self.dispatch(task, before_http=lambda: InstagramBotSettings.objects.filter(pk=self.settings.pk).update(ig_user_id="different-restock-owner"))
        self.assertEqual((count, physical), (0, []))
        task.refresh_from_db()
        self.assertNotEqual(task.status, "ambiguous")

    def test_reset_after_acceptance_blocks_task_without_retargeting(self):
        inv, _subscription, task = self.event()
        def reset():
            IgFunnelResetAudit.objects.create(client=self.client_row, reset_after_message_id=inv.request_source_id,
                reason="restock late reset")
        count, physical = self.dispatch(task, before_http=reset)
        self.assertEqual((count, physical), (0, []))

    @skipUnless(connection.vendor == "mysql", "Physical migration guard verification requires disposable native MariaDB")
    def test_ninety_day_subscription_expiry_cannot_be_extended_by_runtime_cache(self):
        inv, subscription, task = self.event()
        expected = OperationalError if connection.vendor == "mysql" else IntegrityError
        with self.assertRaises(expected), transaction.atomic():
            IgRestockSubscription.objects.filter(pk=subscription.pk).update(expires_at=inv.expires_at + timedelta(days=90))
        subscription.refresh_from_db()
        self.assertEqual(subscription.expires_at, inv.expires_at)
        count, physical = self.dispatch(task, at=inv.expires_at + timedelta(seconds=1))
        self.assertEqual((count, physical), (0, []))

    def test_native_mid_is_durable_before_fallible_history_and_recovery_uses_zero_http(self):
        _inv, subscription, task = self.event()
        create = InstagramBotMessage.objects.create
        def fail_history(**kwargs):
            if kwargs.get("role") == "model":
                raise RuntimeError("CRM history failed after native MID")
            return create(**kwargs)
        with patch.object(InstagramBotMessage.objects, "create", side_effect=fail_history):
            count, physical = self.dispatch(task)
        self.assertEqual((count, len(physical)), (1, 1))
        task.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual((task.status, task.provider_message_id, subscription.state),
            ("sent", "native-one-restock-notification", "sent"))
        self.assertIsNone(task.sent_message_id)
        count, physical = self.dispatch(task, at=self.now + timedelta(minutes=1))
        self.assertEqual((count, physical), (0, []))
        task.refresh_from_db()
        self.assertIsNotNone(task.sent_message_id)
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.client_row, role="model",
            provider_message_id=task.provider_message_id).count(), 1)

    def test_started_unknown_native_outcome_is_never_blindly_replayed(self):
        _inv, subscription, task = self.event()
        count, physical = self.dispatch(task, outcome="unknown")
        self.assertEqual((count, len(physical)), (0, 1))
        task.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual((task.status, subscription.state), ("ambiguous", "unknown"))
        count, physical = self.dispatch(task, at=self.now + timedelta(minutes=20))
        self.assertEqual((count, physical), (0, []))

    def test_processing_crash_is_unknown_and_not_requeued(self):
        _inv, subscription, task = self.event()
        IgFollowUpTask.objects.filter(pk=task.pk).update(status="processing", attempt_count=1,
            claim_token="crashed-restock-worker", claim_until=self.now - timedelta(seconds=1))
        count, physical = self.dispatch(task)
        self.assertEqual((count, physical), (0, []))
        task.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual((task.status, subscription.state), ("ambiguous", "unknown"))

    def test_stock_flapping_never_creates_second_event_or_task(self):
        _inv, subscription, task = self.event()
        for quantity in (0, 3, 0, 5):
            self.available(quantity=quantity)
            IgRestockSubscription.objects.filter(pk=subscription.pk).update(next_check_at=self.now)
            with patch("management.services.ig_fulfillment_reviews.ensure_restock_manager_review", return_value=None), \
                    patch.object(restock, "manager_admission", side_effect=self.manager_ready):
                restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))
        self.assertEqual(IgRestockAvailabilityEvent.objects.count(), 1)
        self.assertEqual(IgFollowUpTask.objects.filter(reason=restock.ORIGIN).count(), 1)
        self.assertEqual(IgRestockAvailabilityEvent.objects.get(subscription=subscription).task_id, task.pk)

    def test_generic_followup_drain_cannot_send_or_quiet_shift_agreed_restock(self):
        from management.services.bot_followups import process_due_followups
        _inv, _subscription, task = self.event()
        original = (task.due_at, task.status, task.attempt_count)
        with patch("management.services.instagram_bot._provider_http") as provider:
            process_due_followups(self.settings, now=self.now + timedelta(seconds=3), limit=50)
        provider.assert_not_called()
        task.refresh_from_db()
        self.assertEqual((task.due_at, task.status, task.attempt_count), original)
