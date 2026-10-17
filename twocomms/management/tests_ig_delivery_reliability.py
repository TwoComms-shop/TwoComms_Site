"""Physical delivery fences for edited sources and proven zero-I/O guards."""
from contextlib import contextmanager
import os
from unittest.mock import Mock, patch

from django.test import TransactionTestCase, override_settings

from management import tests_ig_revision_delivery as fixtures
from management import tests_ig_revision_live as live_fixtures
from management.models import InstagramBotMessage
from management.services.ig_revision_delivery import ProviderPartResult, drain_group
from management.services.ig_revision_transport import ProviderPartPreflight, RevisionProviderTransport


@override_settings(SITE_BASE_URL="https://twocomms.test")
class DeliveryReliabilityTests(TransactionTestCase):
    reset_sequences = True
    setUp = fixtures.RevisionDeliveryTests.setUp
    _payload = fixtures.RevisionDeliveryTests._payload
    _plan = fixtures.RevisionDeliveryTests._plan

    def _text_plan(self, parts=2):
        self._plan([{"group": "substantive_text", "kind": "text", "payload": self._payload(str(index))}
                    for index in range(parts)])

    def _drain(self, send):
        return drain_group(self.revision.pk, self.revision_token, "substantive_text", send)

    def test_same_source_edit_before_delivery_cancels_without_http(self):
        self._text_plan()
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="Changed request")
        send = Mock()
        result = self._drain(send)
        send.assert_not_called()
        self.assertEqual(result.reason, "revision_sources_changed")
        self.assertEqual(set(self.revision.delivery_effects.values_list("state", flat=True)), {"cancelled"})

    def test_edit_between_split_parts_retains_sent_part_and_never_resends(self):
        self._text_plan()

        def send(_payload):
            InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="Changed during delivery")
            return ProviderPartResult("instagram_login:owner-1", http_status=200, provider_message_id="first-sent")

        callback = Mock(side_effect=send)
        first = self._drain(callback)
        self.assertEqual(callback.call_count, 1)
        self.assertEqual(first.reason, "revision_sources_changed")
        effects = list(self.revision.delivery_effects.order_by("part_index"))
        self.assertEqual([row.state for row in effects], ["sent", "cancelled"])
        self.assertEqual(effects[0].provider_message_id, "first-sent")
        replay = Mock()
        self._drain(replay)
        replay.assert_not_called()

    def test_bound_inspection_projection_does_not_change_source_identity(self):
        from management.tests_ig_revision_outbox import RevisionOutboxTests

        self.turn = self.revision.turn
        self.revision.delete()
        part = {"source_part_id": "mp1_" + "7" * 32, "original_index": 0,
                "type": "image", "status": "failed", "capture_terminal": True}
        self.source.attachment_media = [part]
        self.source.save(update_fields=["attachment_media"])
        self.revision, self.revision_token = RevisionOutboxTests._claim_revision(self)
        self._text_plan(parts=1)
        part["inspection"] = {"version": "ig-media-inspection-v1", "state": "inspected",
            "source_part_id": part["source_part_id"], "revision_id": self.revision.pk,
            "request_id": "bound-request", "content_hash": "a" * 64}
        self.source.attachment_media = [part]
        self.source.save(update_fields=["attachment_media"])
        callback = Mock(return_value=ProviderPartResult("instagram_login:owner-1", http_status=200,
                                                       provider_message_id="inspection-safe"))
        self.assertEqual(self._drain(callback).state, "sent")
        self.assertEqual(callback.call_count, 1)

    def test_source_edit_after_start_marker_is_proven_zero_dispatch(self):
        from management.services.ig_revision_live import _drain_effects
        from management.services.ig_revision_outbox import mark_provider_started

        self.settings.ig_user_id = "owner-1"
        self.settings.save(update_fields=["ig_user_id"])
        self._text_plan(parts=1)

        def start_then_edit(*args, **kwargs):
            started = mark_provider_started(*args, **kwargs)
            self.assertEqual(started.reason, "provider_started")
            InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="Edited after start CAS")
            return started

        with patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"}), \
                patch("management.services.ig_revision_delivery.mark_provider_started", side_effect=start_then_edit), \
                patch("management.services.instagram_bot._provider_http") as http:
            _drain_effects(self.revision, self.revision_token, self.settings, "fake-token")
        http.assert_not_called()
        effect = self.revision.delivery_effects.get()
        self.assertEqual(effect.state, "definite_failed")
        self.assertEqual(effect.failure_code, "revision_sources_changed")

    def _assert_business_guard_denial(self, target, denied, expected):
        from management.services.ig_revision_live import _drain_effects

        self._text_plan(parts=1)
        physical = Mock()
        native = RevisionProviderTransport(
            lambda _payload: ProviderPartPreflight(True, payload_kind="text",
                provider_namespace="instagram_login:owner-1", recipient=self.client_row.igsid), physical)
        guard = patch(target, side_effect=denied) if denied else patch(target, return_value="fulfillment_review_changed")
        with guard, patch("management.services.ig_revision_transport.build_provider_part_callback", return_value=native):
            _drain_effects(self.revision, self.revision_token, self.settings, "fake-token")
        physical.assert_not_called()
        effect = self.revision.delivery_effects.get()
        self.assertEqual(effect.state, "definite_failed")
        self.assertEqual(effect.failure_code, expected)

    def test_payment_guard_denial_is_definite_zero_dispatch(self):
        from management.services.ig_payment_reminders import ReminderBoundary

        @contextmanager
        def payment_denied(_revision_id):
            yield ReminderBoundary(False, "invitation_expired")

        self._assert_business_guard_denial(
            "management.services.ig_payment_reminders.provider_boundary_for_revision", payment_denied,
            "payment_reminder_scope_changed")

    def test_restock_guard_denial_is_definite_zero_dispatch(self):
        from management.services.ig_restock_subscriptions import RestockBoundary

        @contextmanager
        def restock_denied(_revision_id):
            yield RestockBoundary(False, "target_changed")

        self._assert_business_guard_denial(
            "management.services.ig_restock_subscriptions.provider_boundary_for_revision", restock_denied,
            "restock_subscription_scope_changed")

    def test_fulfillment_guard_denial_is_definite_zero_dispatch(self):
        self._assert_business_guard_denial(
            "management.services.ig_fulfillment_context.validate_retained_fulfillment", None,
            "fulfillment_scope_changed")

    def test_arbitrary_transport_cannot_claim_business_zero_dispatch(self):
        self._text_plan(parts=1)
        result = self._drain(lambda _payload: ProviderPartResult("instagram_login:owner-1",
            outcome="known_not_dispatched", explicit_rejection_code="fulfillment_scope_changed"))
        self.assertEqual(result.state, "unknown")


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class PreparationCaptureReliabilityTests(TransactionTestCase):
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message

    def test_capture_progress_stops_after_its_exact_preparation_head_is_lost(self):
        from management.models import IgCustomerTurnRevision
        from management.services.ig_revision_live import capture_revision_source
        from management.services.ig_turn_revisions import claim_revision_preparation

        claim = claim_revision_preparation(self.revision.pk)
        self.assertTrue(claim.token, claim.reason)

        def capture(_source, **kwargs):
            self.assertTrue(kwargs["on_progress"]())
            IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(active_slot=None, state="superseded")
            self.assertFalse(kwargs["on_progress"]())

        with patch("management.services.instagram_bot._capture_message_media", side_effect=capture) as mocked:
            capture_revision_source(message_id=self.source.pk, deadline_at=claim.media_prepare_deadline,
                remaining_seconds=5, revision_id=self.revision.pk, preparation_token=claim.token)
        mocked.assert_called_once()
