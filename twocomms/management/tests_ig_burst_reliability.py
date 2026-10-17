"""Real ingress and worker clocks preserve every still-owed burst source."""
from copy import deepcopy
from datetime import timedelta
import os
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixtures
from management import tests_ig_revision_recovery as recovery_fixtures
from management.models import GeminiRequest, GeminiRequestAttempt, IgClient, IgCustomerTurnRevision, InstagramBotMessage, InstagramBotSettings
from management.services.ig_customer_turns import ensure_turn_for_inbound
from management.services.ig_revision_burst_budget import economic_root
from management.services.ig_revision_execution import due_revision_ids, prepare_revision
from management.services.ig_revision_source_coverage import validate_source_transfer
from management.services.ig_turn_revisions import claim_revision_preparation


ROLLOUT = {
    "IG_REVISION_EXECUTION_ENABLED": True,
    "IG_REVISION_EXECUTION_CUTOVER_AT": "2000-01-01T00:00:00+00:00",
    "GOOGLE_INDEXING_ENABLED": False,
    "IG_ANALYSIS_V2_MODE": "off",
}


@override_settings(**ROLLOUT)
class BurstIngressPreparationTests(TransactionTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = timezone.now()
        self.settings = InstagramBotSettings.objects.create(
            pk=1, is_enabled=True, ai_enabled=True, ig_user_id="owner-1", allowed_senders="",
        )
        self.customer = IgClient.objects.create(igsid="17840000000999", language="uk")
        self.sequence = 0

    def incoming(self, text="Яка ціна?", *, seconds=0, media=None):
        self.sequence += 1
        row = InstagramBotMessage.objects.create(
            client=self.customer, sender_id=self.customer.igsid, mid=f"burst-clock-{self.sequence}",
            role="user", source="webhook", status="pending", text=text,
            provider_namespace="instagram_login:owner-1", provider_created_at=self.now,
            attachment_media=media or [],
        )
        attached = ensure_turn_for_inbound(row, now=self.now + timedelta(seconds=seconds))
        return row, IgCustomerTurnRevision.objects.get(pk=attached.revision_id)

    def enqueue(self, text, mid, *, media=None):
        from management.services import instagram_bot as bot

        with patch.object(bot, "_provider_http") as http, patch.object(bot, "_capture_message_media") as capture:
            self.assertTrue(bot.enqueue_inbound(
                self.settings, sender_id=self.customer.igsid, text=text, mid=mid, source="webhook",
                attachment_metadata=media or [], received_at=self.now, persistence_only=True,
            ))
        http.assert_not_called()
        capture.assert_not_called()
        return InstagramBotMessage.objects.get(mid=mid)

    def test_actual_enqueue_neutral_photo_then_question_keeps_original_photo_and_observation(self):
        first = self.enqueue("Дякую", "real-neutral-photo", media=[{
            "type": "image", "url": "https://cdn.invalid/photo.jpg", "provider_object_id": "photo-1",
        }])
        root = first.revision_sources.get().revision
        observation_key = f"commerce_observation:{first.pk}"
        self.assertIn(observation_key, root.action_receipts)
        original = deepcopy(root.action_receipts[observation_key])
        second = self.enqueue("А яка ціна цього худі?", "real-next-question")
        successor = IgCustomerTurnRevision.objects.get(client=self.customer, active_slot=1)
        self.assertEqual(list(successor.sources.order_by("ordinal").values_list("message_id", flat=True)), [first.pk, second.pk])
        self.assertTrue(validate_source_transfer(successor))
        self.assertEqual(successor.action_receipts[observation_key], original)
        self.assertEqual(successor.sources.get(message=first).discovered_media, root.sources.get().discovered_media)
        first.refresh_from_db()
        self.assertEqual(first.status, "pending")
        self.assertEqual(GeminiRequest.objects.count(), 0)
        # Duplicate webhook materialization cannot fork another successor.
        from management.services import instagram_bot as bot
        self.assertFalse(bot.enqueue_inbound(self.settings, sender_id=self.customer.igsid,
            text=second.text, mid=second.mid, source="webhook", persistence_only=True))
        self.assertEqual(IgCustomerTurnRevision.objects.filter(client=self.customer).count(), 2)

    def test_forged_neutral_observation_cannot_transfer_old_source(self):
        from management.services.ig_revision_commerce import INGRESS_SOURCE_PRODUCER

        first, root = self.incoming("Дякую")
        root.action_receipts = {
            "commerce_source_observations": {
                "schema": "revision-commerce-observations.v1", "producer": INGRESS_SOURCE_PRODUCER,
            },
            f"commerce_observation:{first.pk}": {
                "schema": "commerce-source-observation.v1", "classification": "neutral", "reason": "neutral_source",
                "client_id": self.customer.pk, "source_message_id": first.pk, "source_digest": "f" * 64,
                "source_namespace": first.provider_namespace, "reset_floor": 1,
                "event_at": first.provider_created_at.isoformat(),
            },
        }
        root.save(update_fields=["action_receipts"])
        second, successor = self.incoming("Розмір L", seconds=1)
        self.assertEqual(list(successor.sources.values_list("message_id", flat=True)), [second.pk])
        self.assertNotIn("source_transfer_in", successor.action_receipts)
        self.assertEqual(list(root.sources.values_list("message_id", flat=True)), [first.pk])

    def test_actual_returning_customer_photo_and_caption_share_observed_sources(self):
        from management.models import IgCommercialEpisode

        episode = IgCommercialEpisode.objects.create(
            client=self.customer, sequence=1, open_slot=None, state="fulfilled",
            materialization_key="burst-fulfilled", closed_at=self.now - timedelta(days=1),
        )
        self.customer.current_commercial_episode = episode
        self.customer.save(update_fields=["current_commercial_episode"])
        first = self.enqueue("", "aftercare-photo", media=[{
            "type": "image", "url": "https://cdn.invalid/received.jpg", "provider_object_id": "received-1",
        }])
        root = first.revision_sources.get().revision
        self.assertEqual(root.action_receipts[f"commerce_observation:{first.pk}"]["reason"], "post_purchase_observation")
        second = self.enqueue("Тут шов розійшовся, що робити?", "aftercare-caption")
        successor = IgCustomerTurnRevision.objects.get(client=self.customer, active_slot=1)
        self.assertTrue(validate_source_transfer(successor))
        self.assertEqual(list(successor.sources.order_by("ordinal").values_list("message_id", flat=True)), [first.pk, second.pk])
        self.assertEqual(IgCommercialEpisode.objects.filter(client=self.customer).count(), 1)
        episode.refresh_from_db()
        self.assertEqual((episode.state, episode.open_slot), ("fulfilled", None))

    def test_quick_pending_capture_revisits_fixed_deadline_and_seals_without_another_fetch(self):
        source, revision = self.incoming(media=[{
            "type": "image", "url": "https://cdn.invalid/pending.jpg", "status": "unavailable",
            "capture_retryable": True, "capture_terminal": False,
        }])
        due = revision.quiet_deadline
        with patch("django.utils.timezone.now", return_value=due):
            first = prepare_revision(revision.pk, lambda **kwargs: None)
        self.assertEqual(first.reason, "media_pending")
        revision.refresh_from_db()
        fixed_deadline = revision.media_prepare_deadline
        self.assertEqual(revision.lease_until, fixed_deadline)
        self.assertIn(revision.pk, due_revision_ids(now=fixed_deadline))
        with patch("django.utils.timezone.now", return_value=fixed_deadline):
            capture_calls = []
            second = prepare_revision(revision.pk, lambda **kwargs: capture_calls.append(kwargs))
        self.assertTrue(second.ready, second.reason)
        self.assertEqual(capture_calls, [])
        revision.refresh_from_db()
        self.assertEqual(revision.media_prepare_deadline, fixed_deadline)
        part = second.snapshot["sources"][0]["media_parts"][0]
        self.assertEqual((part["capture_outcome"], part["reason"]), ("unavailable", "media_prepare_deadline"))
        self.assertEqual(GeminiRequest.objects.count(), 0)

    def test_crashed_preparation_reclaim_does_not_renew_media_budget(self):
        _source, revision = self.incoming()
        first = claim_revision_preparation(revision.pk, now=revision.quiet_deadline)
        second = claim_revision_preparation(revision.pk, now=first.revision.lease_until + timedelta(microseconds=1))
        self.assertTrue(second.token, second.reason)
        self.assertNotEqual(first.token, second.token)
        self.assertEqual(second.media_prepare_deadline, first.media_prepare_deadline)

    def test_preparation_stops_before_next_source_when_inbound_supersedes_it(self):
        first, _root = self.incoming()
        second, revision = self.incoming("Чорне худі", seconds=0.1)
        calls = []
        fresh = []
        due = revision.quiet_deadline

        def capture(**kwargs):
            calls.append(kwargs["message_id"])
            row, successor = self.incoming("Розмір L", seconds=(due - self.now).total_seconds())
            fresh.append((row, successor))

        with patch("django.utils.timezone.now", return_value=due):
            result = prepare_revision(revision.pk, capture)
        self.assertEqual(result.reason, "preparation_claim_stale")
        self.assertEqual(calls, [first.pk])
        row, successor = fresh[0]
        self.assertEqual(list(successor.sources.order_by("ordinal").values_list("message_id", flat=True)), [first.pk, second.pk, row.pk])
        self.assertTrue(validate_source_transfer(successor))


@override_settings(**ROLLOUT)
class BurstUnsentProposalTests(TransactionTestCase):
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _generate = live_fixtures.RevisionLiveTests._generate
    _execute = live_fixtures.RevisionLiveTests._execute
    _successful_proposal = recovery_fixtures.RevisionRecoveryTests._successful_proposal

    def test_extra_source_does_not_refund_exhausted_won_provider_budget(self):
        from management.services.ig_revision_provider_execution import inspect_revision_provider_execution

        self._prepare()
        self._successful_proposal()
        root = self.revision
        graph = GeminiRequest.objects.get()
        GeminiRequestAttempt.objects.filter(pk=graph.winner_attempt_id).update(
            provider_started_at=graph.created_at, dispatch_pacific_day=graph.created_at.date(),
        )
        for index in range(2, 9):
            GeminiRequestAttempt.objects.create(
                request_graph=graph, request_id=graph.request_id, role="chat", key_name="GEMINI_API",
                model="gemini-3.7-flash", attempt_index=index, candidate_index=index,
                provider_started_at=graph.created_at, dispatch_pacific_day=graph.created_at.date(),
                fsm_state="failed", outcome="transient", logical_turn_id=graph.logical_turn_id,
                client_id=graph.client_id, source_message_id=graph.source_message_id, lane="live", accounting_mode="shadow",
            )
        fresh = self._message("Чи є розмір L?", "after-exhausted-winner")
        attached = ensure_turn_for_inbound(fresh, now=root.quiet_started_at + timedelta(seconds=2))
        successor = IgCustomerTurnRevision.objects.get(pk=attached.revision_id)
        self.assertTrue(validate_source_transfer(successor))
        continuation = inspect_revision_provider_execution(successor)
        self.assertEqual(continuation.root_revision_id, root.pk)
        self.assertEqual((continuation.http_remaining, continuation.reason), (0, "provider_dispatch_budget"))
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.assertEqual(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).count(), 8)

    def test_extra_fragment_after_won_unsent_proposal_carries_sources_and_existing_budget(self):
        from management.services.ig_revision_provider_execution import inspect_revision_provider_execution

        self._prepare()
        self._successful_proposal()
        root = self.revision
        graph = GeminiRequest.objects.get()
        GeminiRequestAttempt.objects.filter(pk=graph.winner_attempt_id).update(
            provider_started_at=graph.created_at, dispatch_pacific_day=graph.created_at.date(),
        )
        root.refresh_from_db()
        self.assertFalse(root.delivery_effects.exists())
        self.assertEqual(inspect_revision_provider_execution(root).http_remaining, 7)
        fresh = self._message("І ще підкажіть, які є розміри?", "after-won-proposal")
        from management.services.ig_revision_source_coverage import plan_source_transfer
        plans = []

        def inspect_transfer(*args, **kwargs):
            plan = plan_source_transfer(*args, **kwargs)
            plans.append(plan.reason)
            return plan

        with patch("management.services.ig_revision_source_coverage.plan_source_transfer", side_effect=inspect_transfer):
            attached = ensure_turn_for_inbound(fresh, now=root.quiet_started_at + timedelta(seconds=2))
        successor = IgCustomerTurnRevision.objects.get(pk=attached.revision_id)
        self.assertEqual(list(successor.sources.order_by("ordinal").values_list("message_id", flat=True)), [self.source.pk, fresh.pk], (plans, root.action_receipts))
        self.assertTrue(validate_source_transfer(successor))
        self.assertEqual(economic_root(successor).pk, root.pk)
        self.assertEqual(inspect_revision_provider_execution(successor).http_remaining, 7)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.assertFalse(successor.generation_proposal_digest)
        root.refresh_from_db()
        self.assertEqual(root.state, "superseded")
        self.revision = successor
        prepared = prepare_revision(successor.pk, lambda **kwargs: None, now=successor.quiet_deadline)
        self.assertTrue(prepared.ready, prepared.reason)
        self.token = prepared.execution_token
        self.revision.refresh_from_db()
        result, generation, send = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((generation.call_count, send.call_count), (1, 1))
        self.revision.refresh_from_db()
        self.assertEqual([row["message_id"] for row in self.revision.generation_proposal["sources"]], [self.source.pk, fresh.pk])
