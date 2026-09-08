import json
import os
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_delivery as delivery_fixtures
from management.models import (
    IgBotNotification, IgClient, IgDeferredEcho, IgPermissionTransitionJob,
    InstagramBotMessage,
)
from management.services.ig_permission_transitions import create_permission_transition
from management.services.ig_revision_echo import (
    acknowledge_historical_manager_echo, acknowledge_manager_echo, observe_revision_echo, pending_manager_echoes,
    reconcile_revision_echoes, revision_echo_blocks,
)
from management.services.ig_revision_outbox import claim_next_effect, finish_effect, mark_provider_started


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class RevisionEchoTests(TransactionTestCase):
    reset_sequences = True
    _payload = delivery_fixtures.RevisionDeliveryTests._payload
    _plan = delivery_fixtures.RevisionDeliveryTests._plan

    def setUp(self):
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        delivery_fixtures.RevisionDeliveryTests.setUp(self)
        self.settings.ig_user_id = "owner-1"
        self.settings.save(update_fields=["ig_user_id"])
        self.namespace = "instagram_login:owner-1"

    def _start(self):
        self._plan([{"group": "substantive_text", "kind": "text", "payload": self._payload("Canonical bot reply")}])
        claim = claim_next_effect(self.revision.pk, self.revision_token, "substantive_text")
        started = mark_provider_started(claim.effect.pk, claim.token, self.revision_token)
        self.assertEqual(started.reason, "provider_started")
        return claim

    def _observe(self, mid="echo-early-mid", **overrides):
        kwargs = dict(settings_id=self.settings.pk, namespace=self.namespace,
                      recipient=self.client_row.igsid, mid=mid, text="Echo text")
        kwargs.update(overrides)
        return observe_revision_echo(**kwargs)

    def _reconcile(self):
        return reconcile_revision_echoes(settings_id=self.settings.pk, client_id=self.client_row.pk, namespace=self.namespace)

    def test_early_echo_waits_then_exact_sent_receipt_proves_own(self):
        claim = self._start()
        observed = self._observe()
        self.assertTrue(observed.accepted, observed.reason)
        self.assertEqual(observed.classification, "waiting_receipt")
        self.assertTrue(revision_echo_blocks(self.client_row.pk, self.namespace))
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        self.client_row.refresh_from_db()
        self.assertFalse(self.client_row.manager_takeover)
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace,
                      http_status=200, provider_message_id="echo-early-mid")
        self._reconcile()
        event = IgDeferredEcho.objects.get(pk=observed.event_id)
        self.assertEqual(event.state, "own")
        self.assertEqual(event.matched_effect_id, claim.effect.pk)
        self.assertFalse(revision_echo_blocks(self.client_row.pk, self.namespace))
        replay = self._observe()
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.classification, "own")
        self.assertEqual(IgDeferredEcho.objects.count(), 1)
        self.assertFalse(IgBotNotification.objects.exists())

    def test_namespace_and_recipient_are_required_for_own_attribution(self):
        claim = self._start()
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace,
                      http_status=200, provider_message_id="scoped-mid")
        wrong_namespace = self._observe("scoped-mid", namespace="instagram_login:other-owner")
        self.assertFalse(wrong_namespace.accepted)
        self.assertEqual(wrong_namespace.reason, "echo_namespace_mismatch")
        other = IgClient.objects.create(igsid="other-echo-recipient")
        wrong_recipient = self._observe("scoped-mid", recipient=other.igsid)
        self.assertNotEqual(wrong_recipient.classification, "own")
        self.assertFalse(wrong_recipient.accepted)
        self.assertEqual(wrong_recipient.reason, "echo_receipt_identity_mismatch")
        self.assertFalse(IgDeferredEcho.objects.exists())
        self.assertFalse(self.client_row.manager_takeover)

    def test_real_manager_echo_after_definite_failure_projects_once_via_exact_job(self):
        claim = self._start()
        media = [{"url": "https://example.test/manager-photo.jpg", "type": "image", "title": "Manager reference"}]
        observed = self._observe("manager-mid", text="", attachments=media)
        self.assertEqual(observed.classification, "waiting_receipt")
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace,
                      http_status=400, transport_outcome="explicit_rejected")
        self._reconcile()
        pending = pending_manager_echoes(client_id=self.client_row.pk, namespace=self.namespace)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["mid"], "manager-mid")
        self.assertEqual(pending[0]["provenance"]["event_id"], observed.event_id)
        message = InstagramBotMessage.objects.create(
            client=self.client_row, sender_id=self.client_row.igsid, mid="manager-mid",
            provider_namespace=self.namespace, role="manager", source="echo", status="done",
            text="(зображення менеджера)", attachments=json.dumps([media[0]["url"]]),
        )
        with patch("management.services.ig_permission_transitions.attempt_permission_transition") as transition:
            job = create_permission_transition(kind=IgPermissionTransitionJob.Kind.MANAGER_TAKEOVER,
                dedupe_key=f"permission:manager_takeover:message:{message.pk}",
                client=self.client_row, settings=self.settings, source_message=message)
            applied = acknowledge_manager_echo(event_id=observed.event_id, settings_id=self.settings.pk,
                manager_message_id=message.pk, permission_transition_id=job.pk)
        transition.assert_not_called()
        self.assertTrue(applied.accepted, applied.reason)
        self.assertEqual(applied.classification, "manager_applied")
        replay = acknowledge_manager_echo(event_id=observed.event_id, settings_id=self.settings.pk,
            manager_message_id=message.pk, permission_transition_id=job.pk)
        self.assertTrue(replay.replayed)
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)
        self.assertEqual(IgPermissionTransitionJob.objects.count(), 1)
        self.client_row.refresh_from_db()
        self.assertFalse(self.client_row.manager_takeover)
        self.assertFalse(revision_echo_blocks(self.client_row.pk, self.namespace))

    def test_unknown_stays_ambiguous_without_fuzzy_text_and_queues_one_safe_alert(self):
        claim = self._start()
        first = self._observe("unknown-one", text="Private customer echo words", historical=True)
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace, transport_outcome="timeout")
        with (
            patch("management.services.instagram_bot._deliver_manager_notification") as send,
            patch("management.services.instagram_bot._handle_echo") as legacy,
            patch("management.services.instagram_bot._provider_http") as http,
        ):
            self._reconcile()
            second = self._observe("unknown-two", text="Private customer echo words")
            self._reconcile()
        send.assert_not_called()
        legacy.assert_not_called()
        http.assert_not_called()
        self.assertEqual(second.classification, "ambiguous")
        self.assertTrue(revision_echo_blocks(self.client_row.pk, self.namespace))
        self.assertEqual(IgDeferredEcho.objects.get(pk=first.event_id).state, "ambiguous")
        notice = IgBotNotification.objects.get()
        text = notice.payload["text"]
        self.assertIn("не вдалося визначити автора повідомлення", text)
        self.assertIn("Відкрийте розмову", text)
        self.assertIn("CRM:", text)
        self.assertNotIn("Private customer echo words", json.dumps(notice.payload))
        self.assertEqual(set(IgDeferredEcho.objects.values_list("notification_id", flat=True)), {notice.pk})
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_erasure_fence_prevents_ingress_reconcile_and_cascades_owned_events(self):
        self._start()
        observed = self._observe()
        self.client_row.privacy_erasure_started_at = timezone.now()
        self.client_row.save(update_fields=["privacy_erasure_started_at"])
        denied = self._observe("echo-after-erasure")
        self.assertFalse(denied.accepted)
        self.assertFalse(denied.retryable)
        self.assertEqual(denied.reason, "echo_erasure_active")
        results = self._reconcile()
        self.assertEqual(results[0].reason, "echo_erasure_active")
        self.assertEqual(IgDeferredEcho.objects.count(), 1)
        self.assertEqual(IgDeferredEcho.objects.get(pk=observed.event_id).state, "waiting_receipt")
        self.assertFalse(IgBotNotification.objects.exists())
        self.client_row.delete()
        self.assertFalse(IgDeferredEcho.objects.exists())

    def test_queue_limit_requires_retry_without_pretending_event_was_stored(self):
        self._start()
        with patch("management.services.ig_revision_echo.MAX_UNRESOLVED", 2):
            self.assertTrue(self._observe("queue-one").accepted)
            self.assertTrue(self._observe("queue-two").accepted)
            overflow = self._observe("queue-three")
        self.assertFalse(overflow.accepted)
        self.assertTrue(overflow.retryable)
        self.assertEqual(overflow.reason, "echo_queue_limit")
        self.assertFalse(IgDeferredEcho.objects.filter(provider_message_id="queue-three").exists())
        self.assertEqual(IgDeferredEcho.objects.count(), 2)
        self.assertEqual(IgBotNotification.objects.count(), 1)

    def test_terminal_pruning_keeps_exact_durable_replay_proof(self):
        claim = self._start()
        early = self._observe("proven-own")
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace, http_status=200, provider_message_id="proven-own")
        self._reconcile()
        with patch("management.services.ig_revision_echo.MAX_RETAINED", 2):
            self.assertTrue(self._observe("manager-one").accepted)
            self.assertTrue(self._observe("manager-two").accepted)
        self.assertFalse(IgDeferredEcho.objects.filter(pk=early.event_id).exists())
        self.assertEqual(self._observe("proven-own").classification, "own")
        self.assertEqual(IgDeferredEcho.objects.count(), 2)

    def test_bounded_metadata_and_immutable_capture(self):
        self.assertFalse(self._observe(text="x" * 4001).accepted)
        self.assertFalse(self._observe(mid="invalid mid").accepted)
        self.assertFalse(IgDeferredEcho.objects.exists())
        event = IgDeferredEcho.objects.get(pk=self._observe().event_id)
        with self.assertRaises(ValueError):
            IgDeferredEcho.objects.filter(pk=event.pk).update(payload={"text": "replacement"})
        # A changed poll URL/body for the same MID cannot overwrite first capture.
        replay = self._observe(text="Different poll representation")
        self.assertTrue(replay.replayed)
        event.refresh_from_db()
        self.assertEqual(event.payload["text"], "Echo text")


    def test_historical_echo_waits_for_receipts_then_records_context_without_takeover(self):
        claim = self._start()
        observed = self._observe("historical-manager-mid", text="Earlier manager context", historical=True)
        self.assertEqual(observed.classification, "waiting_receipt")
        self.assertTrue(revision_echo_blocks(self.client_row.pk, self.namespace))
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace,
                      http_status=200, provider_message_id="different-proven-bot-mid")
        self._reconcile()
        event = IgDeferredEcho.objects.get(pk=observed.event_id)
        self.assertEqual(event.state, "manager_pending")
        self.assertTrue(event.payload["historical"])
        self.assertFalse(revision_echo_blocks(self.client_row.pk, self.namespace))
        self.assertTrue(pending_manager_echoes(client_id=self.client_row.pk, namespace=self.namespace)[0]["historical"])
        live_ack = acknowledge_manager_echo(event_id=event.pk, settings_id=self.settings.pk, manager_message_id=1, permission_transition_id=1)
        self.assertEqual(live_ack.reason, "echo_historical_projection_required")
        message = InstagramBotMessage.objects.create(
            client=self.client_row, sender_id=self.client_row.igsid,
            mid="historical-manager-mid", provider_namespace=self.namespace,
            role="manager", source="poll_history", status="done",
            text="Earlier manager context",
        )
        ack = acknowledge_historical_manager_echo(event_id=event.pk, settings_id=self.settings.pk, manager_message_id=message.pk)
        self.assertTrue(ack.accepted, ack.reason)
        self.assertEqual(ack.classification, "manager_applied")
        self.assertEqual(ack.permission_transition_id, 0)
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
        self.client_row.refresh_from_db()
        self.assertFalse(self.client_row.manager_takeover)
        replay = self._observe("historical-manager-mid", text="Earlier manager context", historical=False)
        self.assertTrue(replay.replayed)
        event.refresh_from_db()
        self.assertTrue(event.payload["historical"])
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
        self.assertEqual(self._observe("different-proven-bot-mid", historical=True).classification, "own")
