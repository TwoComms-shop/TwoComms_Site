"""Live input precedes finite background slices without starving their queues."""
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixtures
from management.management.commands import run_instagram_bot as runner
from management.models import IgCustomerTurnRevision, IgWebhookInboxEvent, InstagramBotMessage, InstagramBotSettings


QUEUE_PATHS = (
    "management.services.ig_fulfillment_delivery.process_due_fulfillment_updates",
    "management.services.ig_split_delivery.process_due_split_offers",
    "management.services.ig_assisted_partial_delivery.process_due_assisted_partial_offers",
    "management.services.ig_split_order_binding.reconcile_paid_shipping_plans",
    "management.services.ig_restock_subscriptions.reconcile_subscriptions",
    "management.services.ig_restock_subscriptions.process_due_restock_notifications",
    "management.services.ig_payment_reminders.process_due_payment_reminders",
    "management.services.bot_followups.process_due_followups",
)


class ActiveDialogueSchedulingTests(SimpleTestCase):
    def setUp(self):
        runner.reset_followup_slices()
        self.addCleanup(runner.reset_followup_slices)
        self.clock = 0.0
        self.active = False
        self.calls = []
        self.after_queue = lambda: None
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(runner.time, "monotonic", side_effect=lambda: self.clock))
        stack.enter_context(patch.object(runner, "_active_customer_input", side_effect=lambda _settings: self.active))
        for index, path in enumerate(QUEUE_PATHS):
            def work(*args, queue_index=index, **kwargs):
                self.assertEqual(kwargs["limit"], 10 if queue_index == 3 else 1)
                self.calls.append(queue_index)
                self.after_queue()
            stack.enter_context(patch(path, side_effect=work))
        self.settings = InstagramBotSettings(pk=1, is_enabled=True)

    def test_new_input_during_first_background_send_prevents_next_queue_start(self):
        self.after_queue = lambda: setattr(self, "active", True)
        runner._drain_customer_followups(self.settings)
        self.assertEqual(self.calls, [0])
        runner._drain_customer_followups(self.settings)
        self.assertEqual(self.calls, [0])

    def test_slow_single_queue_yields_and_next_tick_rotates_instead_of_repeating_it(self):
        self.after_queue = lambda: setattr(self, "clock", self.clock + 3)
        runner._drain_customer_followups(self.settings)
        self.assertEqual(self.calls, [0])
        runner._drain_customer_followups(self.settings)
        self.assertEqual(self.calls, [0, 1])

    def test_continuous_dialogue_defers_batches_but_each_background_queue_gets_a_finite_slice(self):
        self.active = True
        runner._drain_customer_followups(self.settings)
        self.clock = runner.FOLLOWUP_FAIR_WAIT_SECONDS - 1
        runner._drain_customer_followups(self.settings)
        self.assertEqual(self.calls, [])
        for index in range(len(QUEUE_PATHS)):
            self.clock = runner.FOLLOWUP_FAIR_WAIT_SECONDS * (index + 1)
            runner._drain_customer_followups(self.settings)
            self.assertEqual(self.calls, list(range(index + 1)))
            runner._drain_customer_followups(self.settings)
            self.assertEqual(self.calls, list(range(index + 1)))

    def test_idle_fast_queues_all_progress_with_single_item_limits(self):
        runner._drain_customer_followups(self.settings)
        self.assertEqual(self.calls, list(range(len(QUEUE_PATHS))))


class ActiveDialogueServiceLaneTests(SimpleTestCase):
    def setUp(self):
        runner.reset_service_lanes()
        self.addCleanup(runner.reset_service_lanes)
        self.active = False
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(runner, "_active_customer_input", side_effect=lambda _settings: self.active))
        self.notification = stack.enter_context(patch.object(runner.bot, "drain_manager_notifications"))
        self.profiles = stack.enter_context(patch.object(runner.bot, "refresh_profiles_batch"))
        self.profile_cooldown = stack.enter_context(patch.object(runner.cache, "add", return_value=True))
        self.guard = stack.enter_context(patch.object(runner, "_reclaim_lease_guard"))
        self.privacy = stack.enter_context(patch.object(runner, "_private_media_cleanup_tick"))
        self.settings = InstagramBotSettings(pk=1, is_enabled=True)

    def test_external_service_lanes_use_one_item_and_keep_privacy_and_db_work(self):
        runner._run_service_lanes(self.settings)
        self.notification.assert_called_once_with(limit=1)
        self.profiles.assert_not_called()
        self.guard.assert_called_once()
        self.privacy.assert_called_once()

    def test_input_arriving_during_notification_skips_profiles_without_consuming_cooldown(self):
        self.notification.side_effect = lambda **_kwargs: setattr(self, "active", True)
        runner._run_service_lanes(self.settings)
        self.notification.assert_called_once_with(limit=1)
        self.profiles.assert_not_called()
        self.profile_cooldown.assert_not_called()
        self.guard.assert_called_once()
        self.privacy.assert_called_once()

    def test_continuous_input_keeps_notification_progress_and_privacy_work(self):
        self.active = True
        for _ in range(3):
            runner._SERVICE_LANES.begin_cycle()
            runner._run_service_lanes(self.settings)
        self.assertEqual(self.notification.call_count, 3)
        self.assertEqual(self.privacy.call_count, 3)
        self.profiles.assert_not_called()


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class ActiveDialogueInputProbeTests(TransactionTestCase):
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare

    def _settle_fixture(self):
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(state="processed", active_slot=None)
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(status="done")

    def test_quiet_yet_unexpired_bundle_already_defers_background_without_provider_io(self):
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(quiet_deadline=timezone.now() + timedelta(seconds=2))
        with patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("probe must be DB-only")) as http:
            self.assertTrue(runner._active_customer_input(self.settings))
        http.assert_not_called()

    def test_confirmed_reply_leaves_no_active_signal_and_new_message_immediately_restores_it(self):
        self._settle_fixture()
        self.assertFalse(runner._active_customer_input(self.settings))
        self._message("А який розмір?", "new-active-dialogue")
        self.assertTrue(runner._active_customer_input(self.settings))

    def test_manual_owed_sources_are_not_mistaken_for_runnable_new_input(self):
        self._prepare()
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(
            recovery_state="manual", recovery_code="operator_handled", claim_token="", lease_until=None,
        )
        self.assertFalse(runner._active_customer_input(self.settings))

    def test_ready_inbox_ingress_defers_work_but_other_accounts_and_future_retries_do_not(self):
        self._settle_fixture()
        event = IgWebhookInboxEvent.objects.create(
            namespace="instagram_login:owner-1", event_key="new-customer-input", customer_igsid=self.customer.igsid,
            decision="accepted", payload={}, payload_digest="a" * 64,
            next_attempt_at=timezone.now() + timedelta(minutes=1),
        )
        self.assertFalse(runner._active_customer_input(self.settings))
        IgWebhookInboxEvent.objects.filter(pk=event.pk).update(next_attempt_at=None)
        self.assertTrue(runner._active_customer_input(self.settings))
        self.settings.ig_user_id = "other-owner"
        self.assertFalse(runner._active_customer_input(self.settings))

    def test_permanent_blocked_ingress_does_not_become_an_endless_active_dialogue(self):
        self._settle_fixture()
        IgWebhookInboxEvent.objects.create(
            namespace="instagram_login:owner-1", event_key="permanently-blocked", customer_igsid=self.customer.igsid,
            decision="blocked", reason="materialization_retry_exhausted", payload={}, payload_digest="b" * 64,
        )
        self.assertFalse(runner._active_customer_input(self.settings))

    @override_settings(IG_REVISION_EXECUTION_ENABLED=True, IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00")
    def test_ownerless_legacy_pending_is_not_active_when_only_canonical_queue_runs(self):
        self._settle_fixture()
        self._message("Старе питання без власника", "unowned-legacy")
        self.assertFalse(runner._active_customer_input(self.settings))
