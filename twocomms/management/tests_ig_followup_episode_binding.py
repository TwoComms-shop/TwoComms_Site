"""Offline source/episode binding through the existing revision scheduler."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_followups as fixtures
from management.models import (
    IgCommercialEpisode, IgCommerceSelectionSession, IgDeal, IgFollowUpTask,
    IgFunnelResetAudit, InstagramBotMessage,
)
from management.services import ig_revision_followups as followups
from management.services.ig_turn_intent import build_turn_intent, revalidate_followup_intent


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class FollowupEpisodeBindingTests(TransactionTestCase):
    reset_sequences = True
    _payload = fixtures.RevisionNormalFollowupTests._payload
    _plan = fixtures.RevisionNormalFollowupTests._plan
    _sent = fixtures.RevisionNormalFollowupTests._sent
    _kwargs = fixtures.RevisionNormalFollowupTests._kwargs
    _schedule = fixtures.RevisionNormalFollowupTests._schedule

    def setUp(self):
        original = InstagramBotMessage.objects.create

        def source_boundary(**kwargs):
            if kwargs.get("mid") == "revision-delivery-source":
                if self._testMethodName == "test_gift_recipient_is_captured_and_changed_line_is_rejected":
                    kwargs["text"] = "Для друга. Розмір L. Яка ціна?"
                if self._testMethodName == "test_late_imported_event_does_not_become_current_intent":
                    self.opening = original(**{**kwargs, "mid": "episode-opening-source", "text": "Нове замовлення",
                        "provider_created_at": timezone.now() - timedelta(hours=1)})
                    kwargs["provider_created_at"] = timezone.now() - timedelta(hours=2)
            return original(**kwargs)

        with patch.object(InstagramBotMessage.objects, "create", side_effect=source_boundary):
            fixtures.RevisionNormalFollowupTests.setUp(self)

    def _capture(self):
        self.client_row.refresh_from_db()
        return followups.capture_followup_commerce_binding(self.client_row, self.revision,
            build_turn_intent(self.client_row, self.revision))

    def _historical_paid(self):
        deal = IgDeal.objects.create(client=self.client_row, status="paid", payment_truth="confirmed", amount=900)
        IgCommercialEpisode.objects.create(client=self.client_row, sequence=2, open_slot=None,
            materialization_key=f"historical-paid:{deal.pk}", deal=deal, state="fulfilled")
        return deal

    def test_returning_paid_customer_current_new_purchase_can_schedule_once(self):
        historical = self._historical_paid()
        self._sent()
        result = self._schedule()
        self.assertTrue(result.ready, result.reason)
        self.assertEqual(result.reason, "normal_followup_scheduled")
        task = IgFollowUpTask.objects.get(pk=result.task_id)
        self.assertIsNone(task.deal_id)
        self.assertEqual(task.event_payload["commerce_binding"]["episode_id"], self.episode.pk)
        self.assertNotEqual(task.event_payload["commerce_binding"]["deal_id"], historical.pk)
        self.assertTrue(self._schedule().replayed)
        self.assertEqual(IgFollowUpTask.objects.filter(event_key=task.event_key).count(), 1)

    def test_multiple_deals_do_not_replace_bound_deal_and_unproven_lines_fail_closed(self):
        current = IgDeal.objects.create(client=self.client_row, status="draft", amount=1090)
        self.episode.deal = current
        self.episode.save(update_fields=["deal"])
        self._historical_paid()
        binding, deal, reason = self._capture()
        self.assertEqual(reason, "")
        self.assertEqual(deal.pk, current.pk)
        self.assertEqual(binding["deal_id"], current.pk)
        IgCommerceSelectionSession.objects.create(client=self.client_row, commercial_episode=self.episode,
            generation=1, active_index=0, lines=[{"line_id": "duplicate", "size": "L"},
                {"line_id": "duplicate", "size": "M"}])
        self._sent()
        blocked = self._schedule()
        self.assertEqual(blocked.reason, "followup_selection_unproven")
        self.assertEqual(blocked.task_id, 0)

    def test_gift_recipient_is_captured_and_changed_line_is_rejected(self):
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import parse_turn
        apply_turn(self.client_row, self.source, parse_turn(self.source.text), reply_payload={})
        binding, _deal, reason = self._capture()
        self.assertEqual(reason, "")
        self.assertEqual(binding["recipient_id"], "friend")
        self.assertTrue(binding["line_id"])
        session = IgCommerceSelectionSession.objects.get(pk=binding["selection_session_id"])
        session.lines = [{**row, "recipient_id": "self"} for row in session.lines]
        session.save(update_fields=["lines"])
        self.assertEqual(followups.followup_commerce_binding_reason(self.client_row, binding), "followup_selection_unproven")

    def test_missing_or_foreign_current_episode_never_guesses_latest_deal(self):
        self._historical_paid()
        self.client_row.current_commercial_episode = None
        self.client_row.save(update_fields=["current_commercial_episode"])
        self._sent()
        result = self._schedule()
        self.assertEqual(result.reason, "followup_episode_missing")
        self.assertEqual(result.task_id, 0)
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_current_episode_bound_deal_is_persisted_and_changed_episode_blocks_dispatch(self):
        current = IgDeal.objects.create(client=self.client_row, status="draft", amount=1090)
        self.episode.deal = current
        self.episode.save(update_fields=["deal"])
        self._sent()
        result = self._schedule()
        self.assertEqual(result.reason, "normal_followup_scheduled")
        task = IgFollowUpTask.objects.get(pk=result.task_id)
        self.assertEqual(task.deal_id, current.pk)
        self.episode.open_slot = None
        self.episode.save(update_fields=["open_slot"])
        replacement = IgCommercialEpisode.objects.create(client=self.client_row, sequence=2,
            materialization_key="new-followup-episode", opened_watermark_message_id=self.source.pk)
        self.client_row.current_commercial_episode = replacement
        self.client_row.save(update_fields=["current_commercial_episode"])
        task.client = self.client_row
        self.assertEqual(revalidate_followup_intent(task), "followup_commerce_binding_changed")

    def test_reset_invalidates_source_binding_before_new_schedule(self):
        IgFunnelResetAudit.objects.create(client=self.client_row, reset_after_message_id=self.source.pk,
            reason="followup reset boundary")
        binding, deal, reason = self._capture()
        self.assertFalse(binding)
        self.assertIsNone(deal)
        self.assertIn(reason, {"followup_intent_binding_missing", "followup_source_before_episode"})
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_late_imported_event_does_not_become_current_intent(self):
        self.episode.opened_watermark_message_id = self.opening.pk
        self.episode.save(update_fields=["opened_watermark_message_id"])
        binding, deal, reason = self._capture()
        self.assertFalse(binding)
        self.assertIsNone(deal)
        self.assertEqual(reason, "followup_historical_source")

    def test_unpaid_intended_order_does_not_count_as_conversion(self):
        from orders.models import Order
        order = Order.objects.create(full_name="Fixture recipient", phone="", city="", np_office="",
            payment_status="unpaid", total_sum=1090)
        self.episode.intended_order = order
        self.episode.save(update_fields=["intended_order"])
        self._sent()
        result = self._schedule()
        self.assertEqual(result.reason, "normal_followup_scheduled")

    def test_current_order_terminal_negative_truth_blocks_without_direct_deal(self):
        from orders.models import Order
        from management.models import IgPaymentProjection
        order = Order.objects.create(full_name="Fixture recipient", phone="", city="", np_office="",
            payment_status="unpaid", total_sum=1090)
        deal = IgDeal.objects.create(client=self.client_row, order=order, amount=1090)
        IgPaymentProjection.objects.create(client=self.client_row, deal=deal, truth="reversed")
        self.episode.intended_order = order
        self.episode.save(update_fields=["intended_order"])
        self.assertIsNone(self.episode.deal_id)
        self._sent()
        result = self._schedule()
        self.assertEqual(result.reason, "payment_reversed")
        self.assertEqual(result.task_id, 0)

    def test_actual_first_inquiry_intake_binds_empty_session_before_exact_choice(self):
        from django.db import transaction
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        product = self.client_row.current_product
        self.episode.open_slot = None
        self.episode.save(update_fields=["open_slot"])
        self.client_row.current_commercial_episode = None
        self.client_row.current_product = None
        self.client_row.save(update_fields=["current_commercial_episode", "current_product"])
        with transaction.atomic():
            admitted = reduce_inbound_commerce_source(self.client_row, self.source,
                expected_provider_namespace="instagram_login:owner-1")
        self.assertTrue(admitted.ready, admitted.reason)
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.current_commercial_episode.opened_watermark_message_id, 0)
        self.client_row.current_product = product
        self.client_row.save(update_fields=["current_product"])
        self._sent()
        result = self._schedule()
        self.assertEqual(result.reason, "normal_followup_scheduled")
        binding = IgFollowUpTask.objects.get(pk=result.task_id).event_payload["commerce_binding"]
        self.assertEqual(binding["episode_opening_source_id"], self.source.pk)
        self.assertEqual(binding["line_id"], "")
        self.assertEqual(binding["recipient_id"], "")
        self.assertTrue(binding["selection_session_id"])

    def test_native_episode_switch_waits_for_schedule_then_invalidates_queued_basis(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from django.db import close_old_connections, connection, connections, transaction
        if connection.vendor != "mysql":
            self.skipTest("Native InnoDB client mutex required")
        self._sent()
        replacement = IgCommercialEpisode.objects.create(client=self.client_row, sequence=2, open_slot=None,
            materialization_key="concurrent-followup-episode", opened_watermark_message_id=self.source.pk)
        scheduled, switch_waiting = Event(), Event()
        update_next = fixtures.policy._update_client_next

        def wait_after_insert(client):
            scheduled.set()
            if not switch_waiting.wait(5):
                raise AssertionError("Episode mutation did not reach the client mutex")
            return update_next(client)

        def schedule():
            close_old_connections()
            try:
                return self._schedule()
            finally:
                close_old_connections()

        def switch():
            close_old_connections()
            try:
                self.assertTrue(scheduled.wait(5))
                def observe(execute, sql, params, many, context):
                    if self.client_row._meta.db_table in sql and "FOR UPDATE" in sql.upper():
                        switch_waiting.set()
                    return execute(sql, params, many, context)
                with connections["default"].execute_wrapper(observe), transaction.atomic():
                    client = type(self.client_row).objects.select_for_update().get(pk=self.client_row.pk)
                    IgCommercialEpisode.objects.filter(pk=self.episode.pk).update(open_slot=None)
                    IgCommercialEpisode.objects.filter(pk=replacement.pk).update(open_slot=1)
                    client.current_commercial_episode = replacement
                    client.save(update_fields=["current_commercial_episode"])
            finally:
                close_old_connections()

        with patch.object(fixtures.policy, "_update_client_next", side_effect=wait_after_insert):
            with ThreadPoolExecutor(max_workers=2) as workers:
                first, second = workers.submit(schedule), workers.submit(switch)
                result = first.result(timeout=10)
                second.result(timeout=10)
        self.assertEqual(result.reason, "normal_followup_scheduled")
        task = IgFollowUpTask.objects.select_related("client", "deal").get(pk=result.task_id)
        self.assertEqual(revalidate_followup_intent(task), "followup_commerce_binding_changed")
        self.assertEqual(IgFollowUpTask.objects.filter(event_key=task.event_key).count(), 1)
