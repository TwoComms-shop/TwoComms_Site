"""Offline consumers exercise real publications and the same captured request."""
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management.models import IgClient, InstagramBotMessage, InstagramBotSettings
from management.services import bot_conversation_analysis as analysis
from management.services import ig_memory_producer as producer
from management.services.ig_analysis_lane import owner_scope
from management.services.ig_memory_timeline import VERSION
from management.services.ig_turn_capture import _memory
from management.services.ig_turn_intelligence import SCOPE_KEYS, build_turn_context, capture_digest


class AnalysisMemoryPayloadTests(SimpleTestCase):
    def test_historical_truth_is_explicit_unknown_and_never_false_unpaid(self):
        payload = analysis._analysis_prompt_payload(watermark=12,
            transcript=[{"message_id": 12, "role": "user", "text": "Для себе.",
                "event_at": "2026-10-08T12:00:00+00:00"}],
            truth_state={"verified_payment": True, "order_truth": [{"order_id": 999}]},
            memory={"text": "2026-09-01: На подарунок.", "reason": "historical_as_of"},
            memory_metadata={"selected": True, "head_version": 4}, truth_current=False)
        self.assertIsNone(payload["verified_payment"])
        self.assertEqual(payload["truth_state"], {"status": "unknown", "reason": "historical_truth_unavailable"})
        self.assertNotIn("999", json.dumps(payload))
        self.assertEqual(payload["conversation_scope"], "fresh_delta")
        self.assertIn("no recipient, language, availability", payload["historical_memory"]["guidance"])

    def test_fresh_truth_and_omission_are_preserved_in_actual_payload(self):
        truth = {"verified_payment": False, "order_truth": []}
        payload = analysis._analysis_prompt_payload(watermark=12, transcript=[], truth_state=truth,
            memory={"text": "", "reason": "timeline_delta_count_budget"},
            memory_metadata={"selected": False, "omission_reason": "timeline_delta_count_budget"})
        self.assertEqual(payload["truth_state"], truth)
        self.assertIs(payload["verified_payment"], False)
        self.assertEqual(payload["memory_snapshot"]["omission_reason"], "timeline_delta_count_budget")
        self.assertEqual(payload["conversation_scope"], "bounded_recent_history")

    def test_public_memory_dto_rejects_malformed_proofs_as_bounded_omissions(self):
        from management.tests_ig_turn_intelligence import capture, scoped
        for proof in (["not-a-proof"], "not-a-proof", {"version": "captured-memory.timeline.v2", "capture": []}):
            with self.subTest(proof=proof):
                args = capture("Тепер для себе.")
                args["memory"] = scoped(args, text="Untrusted", reason="current", provenance=proof)
                context = build_turn_context(**args)
                self.assertIsNone(context.memory_note)
                self.assertFalse(context.metadata["memory_snapshot"]["selected"])
                self.assertEqual(context.metadata["memory_snapshot"]["source_message_ids"], ())

    def test_dedicated_memory_render_exclusion_preserves_admin_snapshot_and_choices(self):
        from management.tests_ig_client_state_card import CapturedClientStateCardTests
        from management.services.ig_client_state_card import client_state_admin_payload, render_client_state_prompt
        fixture = CapturedClientStateCardTests(methodName="runTest")
        fixture.setUp()
        narrative = {"text": "2026-09-01: Хочу подарунок.", "scope": fixture.scope,
            "source_refs": [{"kind": "message", "id": 20}], "provenance": {"version": "fixture"}}
        state = fixture.capture({"source_selection": fixture.selection(), "narrative": narrative})
        before = state.as_dict()
        self.assertIn("Хочу подарунок", render_client_state_prompt(state, budget=4000).text)
        render = render_client_state_prompt(state, budget=4000, excluded_slots=("context.narrative",))
        self.assertNotIn("Хочу подарунок", render.text)
        self.assertNotIn("context.narrative", render.included)
        self.assertIn(("context.narrative", "dedicated_memory_module"), render.omitted)
        self.assertIn("choice.size", render.included)
        self.assertEqual(state.as_dict(), before)
        self.assertEqual(client_state_admin_payload(state)["slots"]["context.narrative"]["value"], narrative["text"])
        self.assertEqual(render.capture_digest, state.digest)
        for invalid in ("context.narrative", ("choice.size",), ("payment.current",), ("unknown",)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                render_client_state_prompt(state, budget=4000, excluded_slots=invalid)


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_MEMORY_TIMELINE_ENABLED=True,
    GEMINI_ACCOUNTING_V2_MODE="shadow", GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2020-01-01T08:00:00+00:00",
    GEMINI_NONLIVE_ADMISSION_MODE="enforce")
class TimelineConsumerCaptureTests(TransactionTestCase):
    def setUp(self):
        InstagramBotSettings.objects.create(pk=1, is_enabled=True, allowed_senders="")
        self.client = IgClient.objects.create(igsid="timeline-consumer")
        self.clock = timezone.now()
        self.owner_context = owner_scope(now=self.clock, lease_seconds=3600)
        self.owner_context.__enter__()
        self.addCleanup(self.owner_context.__exit__, None, None, None)
        self.ordinal = 0

    def source(self, text, *, days=0):
        self.ordinal += 1
        return InstagramBotMessage.objects.create(client=self.client, sender_id=self.client.igsid,
            role="user", source="poll", text=text, mid=f"timeline-consumer-{self.ordinal}",
            provider_namespace="instagram_login:consumer-owner", status="done",
            provider_created_at=self.clock + timedelta(days=days, microseconds=self.ordinal))

    def publish(self, source, *, topic="gift"):
        result = producer.enqueue_memory_source(source.pk, now=self.clock)
        self.assertTrue(result.queued, result.reason)
        claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=4))
        self.assertIsNotNone(claim)
        result = producer.publish_memory_result(claim, {"version": VERSION,
            "events": [{"source_message_id": source.pk, "quote": source.text, "topic": topic}]},
            now=self.clock + timedelta(seconds=5))
        self.assertTrue(result.published, result.reason)
        self.client.refresh_from_db()

    def captured(self, source, *, context_chars=12000):
        sealed = [{"message_id": source.pk, "role": "user", "text": source.text,
            "client_id": self.client.pk, "source_namespace": source.provider_namespace,
            "source_digest": producer._digest(producer._source_payload(source, source.provider_namespace)),
            "provider_created_at": source.provider_created_at.isoformat()}]
        vector = {"message_id": source.pk, "event_at": source.provider_created_at.isoformat()}
        boundary = dict(client_id=self.client.pk, revision_id=900, source_namespace=source.provider_namespace,
            source_ids=[source.pk], source_digests={str(source.pk): sealed[0]["source_digest"]},
            sealed_sources_digest=capture_digest(sealed), watermark=vector, source_watermark=vector,
            reset_id=None, reset_floor=1, erasure_epoch="", permission_epoch=1,
            publication={"id": 1, "version": 1, "hash": "a" * 64}, episode_id=None, order_id=None,
            line_id="current-line", recipient_id="self")
        memory = _memory(self.client, boundary, sealed_sources=sealed)
        context = build_turn_context(boundary=boundary, sources=sealed, captured_at=self.clock,
            memory=memory, routing_policy={"mode": "unified", "context_chars": context_chars})
        return context, memory

    def test_dated_past_gift_survives_current_self_request_without_selection_authority(self):
        gift = self.source("Хочу футболку на подарунок.", days=-20)
        self.publish(gift)
        current = self.source("Тепер хочу для себе, розмір L.")
        context, memory = self.captured(current)
        self.assertEqual(memory["reason"], "historical_as_of")
        self.assertIn("Хочу футболку на подарунок.", context.memory_note)
        self.assertIn(gift.provider_created_at.isoformat(), context.memory_note)
        self.assertIn("HISTORICAL AS OF", context.memory_note)
        self.assertFalse(context.facts.product_or_recipient_switch)
        narrative = context.components["narrative"]
        self.assertEqual(narrative["provenance"]["timeline"]["events"][0]["scope"], None)
        self.assertEqual(narrative["scope"]["recipient_id"], "self")
        self.assertEqual(context.metadata["memory_snapshot"]["delta_message_ids"], (current.pk,))
        self.assertTrue(context.metadata["memory_snapshot"]["selected"])
        self.assertNotIn("подарунок", json.dumps(dict(context.metadata["memory_snapshot"]), default=str))

    def test_complete_delta_not_in_recent_history_is_present_in_live_memory_module(self):
        gift = self.source("Шукаю подарунок.", days=-20)
        self.publish(gift)
        first = self.source("Підкажіть, чи є ця модель?", days=-2)
        current = self.source("Тепер потрібен розмір L.")
        context, _memory_read = self.captured(current)
        self.assertIn(first.text, context.memory_note)
        self.assertIn(first.provider_created_at.isoformat(), context.memory_note)
        self.assertNotIn(current.text, context.memory_note)  # Already in sealed current sources.

    def test_context_budget_omits_entire_checkpoint_and_delta(self):
        gift = self.source("Шукаю подарунок.", days=-20)
        self.publish(gift)
        current = self.source("Тепер для себе.")
        context, _memory_read = self.captured(current, context_chars=20)
        self.assertIsNone(context.memory_note)
        self.assertNotIn("narrative", context.components)
        self.assertFalse(context.metadata["memory_snapshot"]["selected"])
        self.assertEqual(context.metadata["memory_snapshot"]["omission_reason"], "context_budget_exceeded")

    def test_background_uses_one_snapshot_and_only_its_dated_complete_gap(self):
        gift = self.source("Шукаю подарунок.", days=-20)
        self.publish(gift)
        correction = self.source("Це вже для себе.", days=-2)
        current = self.source("Чи є розмір L?")
        with patch.object(producer, "read_memory_timeline", wraps=producer.read_memory_timeline) as reader:
            memory, metadata, rows = analysis._capture_analysis_memory(self.client, current.pk)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual([row.pk for row in rows], [correction.pk, current.pk])
        self.assertEqual(metadata["delta_message_ids"], [correction.pk, current.pk])
        transcript, _by_id, _media = analysis._conversation(self.client.pk, current.pk, message_rows=rows)
        self.assertEqual(transcript[0]["event_at"], correction.provider_created_at.isoformat())
        payload = analysis._analysis_prompt_payload(watermark=current.pk, transcript=transcript,
            truth_state={"verified_payment": False, "order_truth": []}, memory=memory, memory_metadata=metadata)
        self.assertIn(gift.text, payload["historical_memory"]["text"])
        self.assertNotIn(gift.pk, [row["message_id"] for row in payload["conversation"]])
        self.assertEqual(payload["memory_snapshot"]["read_digest"], memory["provenance"]["read_digest"])

    def test_historical_job_cannot_borrow_later_head(self):
        old = self.source("Колись хотів подарунок.", days=-20)
        self.publish(old)
        old_job = self.source("Чи є розмір L?", days=-2)
        later = self.source("Тепер для себе.")
        self.publish(later, topic="self_purchase")
        memory, metadata, rows = analysis._capture_analysis_memory(self.client, old_job.pk)
        self.assertEqual(memory["reason"], "timeline_head_after_sealed_watermark")
        self.assertFalse(metadata["selected"])
        self.assertEqual(memory["text"], "")
        self.assertIsNone(rows)

    def test_delta_overflow_is_named_omission_not_recent_window_claim(self):
        gift = self.source("Шукаю подарунок.", days=-20)
        self.publish(gift)
        for index in range(25):
            current = self.source(f"Питання номер {index}.")
        context, memory = self.captured(current)
        self.assertEqual(memory["reason"], "timeline_delta_count_budget")
        self.assertIsNone(context.memory_note)
        self.assertFalse(context.metadata["memory_snapshot"]["selected"])

    def test_real_background_provider_payload_contains_dated_snapshot_and_gap_once(self):
        gift = self.source("Шукаю подарунок.", days=-20)
        self.publish(gift)
        current = self.source("Чи є розмір L?")
        analysis.schedule_analysis(self.client, current, now=self.clock - timedelta(minutes=1), delay_seconds=0)
        claim = analysis._claim_due(self.clock)
        self.assertIsNotNone(claim)
        with patch.object(producer, "read_memory_timeline", wraps=producer.read_memory_timeline) as reader, patch.object(
                analysis, "gemini_generate_json", return_value={"parsed": {
                    "interaction_type": "product_interest", "score_band": "exploring",
                    "purchase_probability": 0.4, "confidence": 0.6,
                    "evidence": [{"message_id": current.pk, "quote": current.text, "claim": "Питання про розмір"}],
                    "uncertainties": []}, "model": "offline-memory-fixture", "meta": {}}) as provider:
            outcome = analysis._process_claim(*claim, self.clock)
        self.assertEqual(outcome, "done")
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(provider.call_count, 1)
        payload = json.loads(provider.call_args.args[1])
        self.assertIn(gift.text, payload["historical_memory"]["text"])
        self.assertIn(gift.provider_created_at.isoformat(), payload["historical_memory"]["text"])
        self.assertEqual(payload["conversation"][0]["event_at"], current.provider_created_at.isoformat())
        self.assertEqual(payload["memory_snapshot"]["delta_message_ids"], [current.pk])
        self.assertEqual(payload["conversation_scope"], "fresh_delta")

    def test_late_accepted_input_is_coalesced_before_any_provider_call(self):
        old = self.source("Чи є розмір L?", days=-2)
        analysis.schedule_analysis(self.client, old, now=self.clock - timedelta(minutes=1), delay_seconds=0)
        claim = analysis._claim_due(self.clock)
        self.assertIsNotNone(claim)
        later = self.source("Тепер потрібен XL.")
        with patch.object(analysis, "gemini_generate_json") as provider:
            outcome = analysis._process_claim(*claim, self.clock)
        self.assertEqual(outcome, "superseded")
        provider.assert_not_called()
        job = analysis.IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.watermark_message_id, later.pk)
        self.assertEqual(job.status, job.Status.PENDING)
        self.assertEqual(job.last_error, "analysis_watermark_changed")
        self.assertEqual(job.claimed_watermark_message_id, 0)
        self.assertEqual(job.attempts, 0)

    def test_late_older_provider_observation_does_not_move_job_event_backwards(self):
        current = self.source("Чи є розмір L?")
        analysis.schedule_analysis(self.client, current, now=self.clock - timedelta(minutes=1), delay_seconds=0)
        claim = analysis._claim_due(self.clock)
        self.assertIsNotNone(claim)
        older = self.source("Раніше шукав подарунок.", days=-2)
        job, watermark, revision, token = claim
        self.assertFalse(analysis._supersede_analysis_before_provider(job, self.client, watermark,
            revision, token, now=self.clock))
        job.refresh_from_db()
        self.assertEqual(job.watermark_message_id, current.pk)
        self.assertEqual(job.status, job.Status.PROCESSING)
        self.assertNotIn(older.pk, [row.pk for row in analysis._analysis_message_rows(self.client.pk, current.pk)])

    def test_inverted_ingest_job_is_terminal_skip_before_provider_or_snapshot(self):
        from management.models import IgConversationAnalysisEvent, IgConversationAnalysisSnapshot
        self.source("Чи є розмір L?")
        older_event = self.source("Раніше шукав подарунок.", days=-2)
        analysis.schedule_analysis(self.client, older_event, now=self.clock - timedelta(minutes=1), delay_seconds=0)
        claim = analysis._claim_due(self.clock)
        self.assertIsNotNone(claim)
        self.assertFalse(analysis._analysis_truth_is_current(self.client, older_event.pk))
        with patch.object(analysis, "gemini_generate_json") as provider:
            outcome = analysis._process_claim(*claim, self.clock)
        self.assertEqual(outcome, "skipped")
        provider.assert_not_called()
        self.assertFalse(IgConversationAnalysisSnapshot.objects.filter(client=self.client).exists())
        self.assertFalse(IgConversationAnalysisEvent.objects.filter(client=self.client).exists())
        job = analysis.IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.status, job.Status.SKIPPED)
        self.assertEqual(job.skip_reason, "historical_event_boundary")
        self.assertEqual(job.claimed_watermark_message_id, 0)

    def test_source_event_changed_during_provider_cannot_publish_snapshot_or_repeat_event(self):
        from management.models import IgConversationAnalysisEvent, IgConversationAnalysisSnapshot
        prior = self.source("Попереднє питання.", days=-2)
        current = self.source("Хочу ще одну футболку.")
        analysis.schedule_analysis(self.client, current, now=self.clock - timedelta(minutes=1), delay_seconds=0)
        claim = analysis._claim_due(self.clock)
        self.assertIsNotNone(claim)
        def changed_during_provider(*args, **kwargs):
            InstagramBotMessage.objects.filter(pk=current.pk).update(
                provider_created_at=prior.provider_created_at - timedelta(days=1))
            return {"parsed": {"interaction_type": "high_intent", "score_band": "checkout",
                "repeat_intent": {"kind": "explicit_more", "confidence": 0.95,
                    "evidence_message_ids": [current.pk]},
                "evidence": [{"message_id": current.pk, "quote": current.text, "claim": "Повторна покупка"}]},
                "model": "offline-source-change", "meta": {}}
        with patch.object(analysis, "gemini_generate_json", side_effect=changed_during_provider) as provider:
            outcome = analysis._process_claim(*claim, self.clock)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(outcome, "skipped")
        self.assertFalse(IgConversationAnalysisSnapshot.objects.filter(client=self.client).exists())
        self.assertFalse(IgConversationAnalysisEvent.objects.filter(client=self.client).exists())
        job = analysis.IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.status, job.Status.SKIPPED)
        self.assertEqual(job.skip_reason, "historical_event_boundary")

    def test_legacy_import_job_uses_fresh_owned_event_anchor_without_import_evidence(self):
        from management.models import IgConversationAnalysisEvent, IgConversationAnalysisSnapshot
        fresh = self.source("Чи є розмір L?")
        imported = InstagramBotMessage.objects.create(client=self.client, sender_id=self.client.igsid,
            role="user", source="manual_refresh", text="Хочу ще одну футболку на подарунок.",
            provider_namespace=fresh.provider_namespace, status="done",
            attachments=json.dumps(["https://lookaside.example/historical-context-only.jpg"]),
            provider_created_at=self.clock - timedelta(days=30))
        analysis.schedule_analysis(self.client, imported, trigger="manual_refresh",
            now=self.clock, delay_seconds=0)
        claim = analysis._claim_due(self.clock)
        self.assertIsNotNone(claim)
        anchor = analysis._analysis_source_boundary(self.client, imported.pk)
        self.assertEqual(anchor["message_id"], fresh.pk)
        self.assertTrue(analysis._analysis_truth_is_current(self.client, imported.pk, expected_boundary=anchor))
        with patch.object(analysis, "gemini_generate_json", return_value={"parsed": {
                "interaction_type": "product_interest", "score_band": "exploring",
                "evidence": [{"message_id": fresh.pk, "quote": fresh.text, "claim": "Питання про розмір"}],
                "repeat_intent": {"kind": "explicit_more", "confidence": 0.95,
                    "evidence_message_ids": [imported.pk]}}, "model": "offline-anchor-fixture", "meta": {}}) as provider, patch(
                "management.services.instagram_bot.download_image") as historical_download:
            outcome = analysis._process_claim(*claim, self.clock)
        self.assertEqual(outcome, "done")
        self.assertEqual(provider.call_count, 1)
        historical_download.assert_not_called()
        self.assertEqual(provider.call_args.kwargs["images"], [])
        self.assertEqual(provider.call_args.kwargs["image_labels"], [])
        payload = json.loads(provider.call_args.args[1])
        self.assertEqual(payload["watermark_message_id"], imported.pk)
        self.assertEqual(payload["conversation_event_boundary"]["message_id"], fresh.pk)
        self.assertEqual(payload["conversation_event_boundary"]["event_at"], fresh.provider_created_at.isoformat())
        fresh_item = next(row for row in payload["conversation"] if row["message_id"] == fresh.pk)
        self.assertTrue(fresh_item["evidence_eligible"])
        old_item = next(row for row in payload["conversation"] if row["message_id"] == imported.pk)
        self.assertFalse(old_item["evidence_eligible"])
        self.assertNotIn("media", old_item)
        self.assertEqual(old_item["analysis_scope"], "historical_context")
        snapshot = IgConversationAnalysisSnapshot.objects.get(client=self.client)
        self.assertEqual(snapshot.last_analyzed_message_id, imported.pk)
        self.assertFalse(snapshot.repeat_intent)
        self.assertFalse(IgConversationAnalysisEvent.objects.filter(client=self.client).exists())
        job = analysis.IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.watermark_message_id, imported.pk)
        self.assertEqual(job.status, job.Status.DONE)

    def test_legacy_import_anchor_cannot_cross_sender_or_namespace(self):
        fresh = self.source("Чи є розмір L?")
        imported = InstagramBotMessage.objects.create(client=self.client, sender_id=self.client.igsid,
            role="user", source="import", text="Старе питання.",
            provider_namespace="instagram_login:foreign-owner", status="done",
            provider_created_at=self.clock - timedelta(days=30))
        self.assertIsNone(analysis._analysis_source_boundary(self.client, imported.pk))
        InstagramBotMessage.objects.filter(pk=imported.pk).update(
            provider_namespace=fresh.provider_namespace, sender_id="foreign-sender")
        self.assertIsNone(analysis._analysis_source_boundary(self.client, imported.pk))

    def test_prior_episode_gap_is_dated_context_without_current_repeat_evidence(self):
        from management.models import IgCommercialEpisode
        gift = self.source("Шукаю подарунок.", days=-20)
        self.publish(gift)
        older = self.source("Хочу ще одну футболку.", days=-2)
        current = self.source("Дякую.")
        episode = IgCommercialEpisode.objects.create(client=self.client, sequence=1,
            materialization_key="timeline-current-episode", opened_watermark_message_id=current.pk)
        self.client.current_commercial_episode = episode
        self.client.save(update_fields=["current_commercial_episode"])
        memory, _metadata, rows = analysis._capture_analysis_memory(self.client, current.pk)
        self.assertEqual(memory["reason"], "historical_as_of")
        transcript, by_id, _media = analysis._conversation(self.client.pk, current.pk, message_rows=rows)
        historical = next(row for row in transcript if row["message_id"] == older.pk)
        self.assertFalse(historical["evidence_eligible"])
        self.assertEqual(historical["analysis_scope"], "historical_context")
        self.assertNotIn(older.pk, by_id)
        normalized = analysis._normalize({"interaction_type": "high_intent", "score_band": "high_intent",
            "repeat_intent": {"kind": "explicit_more", "confidence": 0.9,
                "evidence_message_ids": [older.pk]},
            "evidence": [{"message_id": older.pk, "quote": older.text, "claim": "Повторна покупка"}]},
            by_id, verified_payment=True)
        self.assertEqual(normalized["repeat_intent"], {})
        self.assertEqual(normalized["evidence"], [])


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_MEMORY_TIMELINE_ENABLED=True,
    IG_TURN_CONTEXT_MODE="unified", IG_CLIENT_STATE_PROMPT_TOKENS=2400,
    GEMINI_ACCOUNTING_V2_MODE="shadow", GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2020-01-01T08:00:00+00:00",
    GEMINI_NONLIVE_ADMISSION_MODE="enforce")
class TimelineActualLiveConsumerTests(TransactionTestCase):
    def test_actual_live_request_manifest_and_state_reuse_the_validated_snapshot(self):
        from management.tests_ig_turn_context_integration import TurnContextConsumerIntegrationTests
        from management.services.ig_turn_integration import prepare_revision_turn_context

        # Reuse the canonical publication/revision fixture, not a patched seal,
        # state capture, head proof or transport payload assembler.
        fixture_owner = TurnContextConsumerIntegrationTests(methodName="runTest")
        fixture_owner.setUp()
        self.addCleanup(fixture_owner.doCleanups)
        from django.db import transaction
        with transaction.atomic():
            fixture = fixture_owner.fixture(text="Тепер для себе, розмір L.")
        gift_text = "Хочу футболку на подарунок."
        InstagramBotMessage.objects.filter(pk=fixture.past.pk).update(text=gift_text)
        fixture.past.refresh_from_db()
        clock = timezone.now()
        lane = owner_scope(now=clock, lease_seconds=3600)
        lane.__enter__()
        self.addCleanup(lane.__exit__, None, None, None)
        queued = producer.enqueue_memory_source(fixture.source.pk, now=clock)
        self.assertTrue(queued.queued, queued.reason)
        claim = producer.claim_memory_job(client_id=fixture.customer.pk, now=clock + timedelta(seconds=4))
        self.assertIsNotNone(claim)
        result = producer.publish_memory_result(claim, {"version": VERSION,
            "events": [{"source_message_id": fixture.past.pk, "quote": gift_text, "topic": "gift"}]},
            now=clock + timedelta(seconds=5))
        self.assertTrue(result.published, result.reason)
        with patch.object(producer, "read_memory_timeline", wraps=producer.read_memory_timeline) as reader:
            fixture.prepared = prepare_revision_turn_context(fixture.revision,
                generation_boundary=fixture.boundary, collection=fixture.collection,
                settings_row=fixture_owner.settings_row, publication=fixture_owner.publication_binding,
                now=clock + timedelta(seconds=6))
        self.assertEqual(reader.call_count, 1)
        captured = fixture.prepared.context.components["narrative"]
        captured_text = captured["text"]
        manifest_snapshot = deepcopy(fixture.prepared.request_metadata["memory_snapshot"])
        self.assertEqual(fixture.prepared.state.as_dict()["slots"]["context.narrative"]["value"], captured_text)
        # The real request must consume the detached capture after the mutable
        # DB head changes, without trying to read or repair it in a consumer.
        IgClient.objects.filter(pk=fixture.customer.pk).update(memory_summary="LATEST HEAD POISON")
        fixture_owner.requests = []
        from management.services.ig_policy_compiler import compile_policy
        with patch.object(producer, "read_memory_timeline", side_effect=AssertionError("consumer reread")) as reread, patch(
                "management.services.call_ai_analysis.gemini_generate_text", side_effect=fixture_owner.transport) as provider, patch(
                "management.services.ig_policy_compiler.compile_policy", wraps=compile_policy) as compiler:
            fixture_owner.consume(fixture)
        reread.assert_not_called()
        self.assertEqual(provider.call_count, 1)
        payload, manifest, _kwargs = fixture_owner.requests[0]
        self.assertIn(gift_text, str(payload))
        self.assertIn(fixture.past.provider_created_at.isoformat(), str(payload))
        self.assertNotIn("LATEST HEAD POISON", str(payload))
        self.assertEqual(manifest["request_context"]["memory_snapshot"], manifest_snapshot)
        self.assertTrue(manifest_snapshot["selected"])

        # Fit precisely the real mandatory modules, observing the unmodified
        # compiler. Optional memory must disappear as one complete module and
        # cannot leak its head quote through mandatory client-state rendering.
        modules = compiler.call_args.kwargs
        mandatory = [*modules["immutable_authority"], *modules["published_core"], *modules["verified_dynamic_facts"]]
        mandatory_chars = sum(len(module.body) for module in mandatory) + 2 * (len(mandatory) - 1)
        state_before = fixture.prepared.state.as_dict()
        fixture_owner.requests = []
        with self.settings(IG_BOT_POLICY_BUDGET_CHARS=mandatory_chars), patch(
                "management.services.call_ai_analysis.gemini_generate_text", side_effect=fixture_owner.transport) as limited_provider:
            fixture_owner.consume(fixture)
        self.assertEqual(limited_provider.call_count, 1)
        low_payload, low_manifest, _kwargs = fixture_owner.requests[0]
        system_prompt = low_payload["system_instruction"]["parts"][0]["text"]
        self.assertNotIn(gift_text, system_prompt)
        self.assertNotIn("context:memory", low_manifest["selected_ids"])
        self.assertIn({"id": "context:memory", "reason": "budget_exhausted"}, low_manifest["omitted"])
        self.assertIn({"id": "state:context.narrative", "reason": "dedicated_memory_module"}, low_manifest["omitted"])
        self.assertFalse(low_manifest["request_context"]["memory_snapshot"]["selected"])
        self.assertEqual(fixture.prepared.state.as_dict(), state_before)
        self.assertIn(gift_text, state_before["slots"]["context.narrative"]["value"])
