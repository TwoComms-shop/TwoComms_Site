"""Receipt work belongs to the existing fenced background worker."""
from contextlib import ExitStack
from unittest.mock import patch

from django.test import SimpleTestCase

from management.management.commands import run_instagram_bot as daemon
from management.models import InstagramBotSettings
from management.services import instagram_bot as bot


class _OneCycleEvent:
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, _seconds):
        self.stopped = True


class PaymentObservationWorkerTests(SimpleTestCase):
    def _worker(self, *, maintenance=False, renewals=True, stop_on_reconcile=False):
        event = _OneCycleEvent()
        order = []
        with ExitStack() as stack:
            for target in (
                "management.management.commands.run_instagram_bot.close_old_connections",
                "management.management.commands.run_instagram_bot.require_database_ready",
                "management.management.commands.run_instagram_bot.task_heartbeat",
                "management.management.commands.run_instagram_bot._memory_background_tick",
                "management.management.commands.run_instagram_bot.bot.log",
                "management.services.ig_analysis_lane.bind_owner",
                "management.services.ig_typed_memory.reconcile_typed_memory",
                "management.services.ig_memory_producer.reconcile_memory_sources",
            ):
                stack.enter_context(patch(target))
            stack.enter_context(patch.object(daemon, "maintenance_status", return_value={"active": maintenance}))
            renew = stack.enter_context(patch("management.services.ig_analysis_lane.renew_owner",
                **({"side_effect": renewals} if isinstance(renewals, list) else {"return_value": renewals})))
            reconcile = stack.enter_context(patch("management.services.bot_conversation_analysis.reconcile_analysis_jobs",
                side_effect=(lambda **kwargs: event.set()) if stop_on_reconcile else None,
                return_value={}))
            analysis = stack.enter_context(patch("management.services.bot_conversation_analysis.process_due_analysis",
                side_effect=lambda **kwargs: order.append("analysis")))
            stack.enter_context(patch("management.services.ig_analysis_events.process_due_analysis_events", return_value={}))
            observations = stack.enter_context(patch("management.services.ig_payment_observation.drain_payment_observations",
                side_effect=lambda **kwargs: order.append("observations")))
            provider = stack.enter_context(patch("management.services.call_ai_analysis.gemini_generate_text"))
            transport = stack.enter_context(patch.object(bot, "_provider_http"))
            settings_load = stack.enter_context(patch.object(bot.InstagramBotSettings, "load",
                return_value=InstagramBotSettings(ai_enabled=False, is_enabled=False)))
            daemon._analysis_worker(event, "payment-owner", 4)
            provider.assert_not_called()
            transport.assert_not_called()
            settings_load.assert_not_called()
        return observations, analysis, reconcile, renew, order, event

    def test_existing_background_owner_observes_one_source_before_analysis_with_replies_disabled(self):
        observations, analysis, _reconcile, renew, order, _event = self._worker()
        observations.assert_called_once_with(max_items=1)
        analysis.assert_called_once_with(limit=1)
        self.assertEqual(order, ["observations", "analysis"])
        self.assertGreaterEqual(renew.call_count, 2)

    def test_maintenance_never_claims_or_probes_receipts(self):
        observations, analysis, reconcile, _renew, _order, _event = self._worker(maintenance=True)
        observations.assert_not_called()
        analysis.assert_not_called()
        reconcile.assert_not_called()

    def test_lost_lane_owner_never_claims_receipts(self):
        observations, analysis, reconcile, _renew, _order, event = self._worker(renewals=False)
        observations.assert_not_called()
        analysis.assert_not_called()
        reconcile.assert_not_called()
        self.assertTrue(event.stopped)

    def test_owner_lost_during_reconcile_never_claims_receipts(self):
        observations, analysis, reconcile, _renew, _order, event = self._worker(renewals=[True, False])
        reconcile.assert_called_once()
        observations.assert_not_called()
        analysis.assert_not_called()
        self.assertTrue(event.stopped)

    def test_stop_during_reconcile_never_claims_receipts(self):
        observations, analysis, reconcile, _renew, _order, _event = self._worker(stop_on_reconcile=True)
        reconcile.assert_called_once()
        observations.assert_not_called()
        analysis.assert_not_called()

    def test_customer_process_pending_does_not_drain_receipt_queue(self):
        settings = InstagramBotSettings(ai_enabled=False, is_enabled=False)
        with patch.object(bot, "_maybe_purge_expired_private_media"), patch(
            "management.services.ig_revision_live.process_revision_finalizations", return_value=0,
        ), patch("management.services.ig_payment_observation.drain_payment_observations") as observations:
            self.assertEqual(bot.process_pending(settings), 0)
        observations.assert_not_called()
