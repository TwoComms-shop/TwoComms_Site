"""Source-verified timeline publication and sealed historical reader contracts."""
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_memory_producer as fixtures
from management import tests_ig_memory_provider_gate as provider_fixtures
from management import tests_ig_nonlive_admission as admission_fixtures
from management import tests_gemini_accounting_shadow as accounting_fixtures
from management.models import IgClient, IgCommercialEpisode, IgFunnelResetAudit
from management.services import bot_memory, ig_memory_producer as producer
from management.services.ig_memory_timeline import VERSION as EVENT_VERSION
from management.tests_support import AnalysisPrivacyCleanupMixin


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_MEMORY_TIMELINE_ENABLED=True,
    GEMINI_ACCOUNTING_V2_MODE="shadow", GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2020-01-01T08:00:00+00:00",
    GEMINI_NONLIVE_ADMISSION_MODE="enforce")
class MemoryTimelineProducerTests(AnalysisPrivacyCleanupMixin, TransactionTestCase):
    source = fixtures.CapturedMemoryProducerTests.source
    claim = fixtures.CapturedMemoryProducerTests.claim

    def setUp(self):
        fixtures.CapturedMemoryProducerTests.setUp(self)

    def selection(self, *picks):
        return {"version": EVENT_VERSION, "events": [
            {"source_message_id": row.pk, "quote": quote, "topic": topic}
            for row, quote, topic in picks]}

    def head(self, text="Це подарунок для сестри.", topic="gift"):
        source = self.source(text)
        self.assertTrue(producer.enqueue_memory_source(source.pk, now=self.clock).queued)
        claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=4))
        self.assertIsNotNone(claim)
        outcome = producer.publish_memory_result(claim, self.selection((source, text, topic)),
            now=self.clock + timedelta(seconds=5))
        self.assertTrue(outcome.published, outcome.reason)
        self.client.refresh_from_db()
        return source, claim

    def next_claim(self, source):
        self.assertTrue(producer.enqueue_memory_source(source.pk, now=self.clock + timedelta(seconds=10)).queued)
        claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=14))
        self.assertIsNotNone(claim)
        return claim

    def boundary(self, source):
        return {"client_id": self.client.pk, "source_namespace": source.provider_namespace,
            "reset_id": None, "reset_floor": 1, "erasure_epoch": "",
            "watermark": producer._watermark(source)}

    def test_first_publish_is_exact_dated_source_quote_with_null_scope(self):
        source, claim = self.head()
        self.assertEqual(claim.capture["version"], producer.TIMELINE_VERSION)
        self.assertEqual(set(claim.capture["scope"]), {"client_id", "namespace", "reset_id", "reset_floor", "erasure_at"})
        event = self.client.memory_provenance["timeline"]["events"][0]
        self.assertEqual(event["quote"], source.text)
        self.assertEqual(event["event_at"], source.provider_created_at.isoformat())
        self.assertEqual(event["time_basis"], "provider")
        self.assertIsNone(event["scope"])
        self.assertNotIn("timeline_inputs", self.client.memory_provenance["capture"])
        self.assertIn(source.text, self.client.memory_summary)
        self.assertNotEqual(event["event_at"], self.client.memory_provenance["generated_at"])

    def test_fresh_inbound_keeps_historical_head_and_complete_delta_without_generation(self):
        old, _ = self.head()
        newer = self.source("Тепер для себе, чи є розмір L?")
        producer.enqueue_memory_source(newer.pk, now=self.clock + timedelta(seconds=10))
        before = (self.client.memory_version, deepcopy(self.client.memory_provenance), self.client.memory_summary)
        with patch("management.services.call_ai_analysis.gemini_generate_text") as generate:
            read = producer.read_memory_timeline(self.client, boundary=self.boundary(newer))
        self.assertEqual(read.reason, "historical_as_of")
        self.assertIn(old.text, read.text)
        self.assertEqual([row["source_message_id"] for row in read.provenance["read_delta"]], [newer.pk])
        self.assertEqual(read.provenance["read_delta"][0]["text"], newer.text)
        self.assertEqual(producer.validate_memory_timeline_read(vars(read), self.boundary(newer)), "")
        generate.assert_not_called()
        self.client.refresh_from_db()
        self.assertEqual((self.client.memory_version, self.client.memory_provenance, self.client.memory_summary), before)

    def test_carry_is_verified_and_actual_provider_input_includes_previous_events(self):
        old, _ = self.head()
        newer = self.source("Хочу ще одну футболку для себе.")
        claim = self.next_claim(newer)
        self.assertEqual([row["source_message_id"] for row in claim.capture["previous_events"]], [old.pk])
        payload = bot_memory.build_timeline_payload(claim.capture)
        data = json.loads(payload["contents"][0]["parts"][0]["text"].split("\n\n", 1)[1])
        self.assertEqual(data["retained_events"], claim.capture["previous_events"])
        self.assertEqual([row["source_message_id"] for row in data["delta_sources"]], [newer.pk])
        self.assertEqual({row["message_id"] for row in claim.capture["sources"]}, {old.pk, newer.pk})
        result = producer.publish_memory_result(claim,
            self.selection((old, old.text, "gift"), (newer, newer.text, "self_purchase")),
            now=self.clock + timedelta(seconds=15))
        self.assertTrue(result.published, result.reason)
        self.client.refresh_from_db()
        self.assertEqual(len(self.client.memory_provenance["timeline"]["events"]), 2)

    def test_empty_selection_retains_verified_event_without_timestamp_refresh(self):
        old, _ = self.head()
        event = deepcopy(self.client.memory_provenance["timeline"]["events"][0])
        newer = self.source("Дякую.")
        claim = self.next_claim(newer)
        result = producer.publish_memory_result(claim, self.selection(), now=self.clock + timedelta(seconds=15))
        self.assertTrue(result.published, result.reason)
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_provenance["timeline"]["events"], [event])

    def test_new_selection_is_final_and_retained_drop_is_explicit(self):
        old, _ = self.head()
        newer = self.source("Передумала: купую для себе.")
        claim = self.next_claim(newer)
        result = producer.publish_memory_result(claim, self.selection((newer, newer.text, "correction")),
            now=self.clock + timedelta(seconds=15))
        self.assertTrue(result.published, result.reason)
        self.client.refresh_from_db()
        timeline = self.client.memory_provenance["timeline"]
        self.assertEqual([row["source_message_id"] for row in timeline["events"]], [newer.pk])
        self.assertIn({"source_message_id": old.pk, "reason": "retained_not_selected"}, timeline["coverage"]["omissions"])

    def test_mutable_episode_change_is_not_attached_to_old_event_or_head_scope(self):
        old, _ = self.head()
        episode = IgCommercialEpisode.objects.create(client=self.client, sequence=1,
            materialization_key="timeline:episode:1")
        self.client.current_commercial_episode = episode
        self.client.save(update_fields=["current_commercial_episode"])
        newer = self.source("Зараз для себе.")
        read = producer.read_memory_timeline(self.client, boundary=self.boundary(newer))
        self.assertEqual(read.reason, "historical_as_of")
        self.assertIsNone(read.provenance["timeline"]["events"][0]["scope"])
        self.assertNotIn("episode_id", read.provenance["capture"]["scope"])

    def test_future_head_is_omitted_and_never_searches_older_head(self):
        old, _ = self.head()
        newer = self.source("Для себе.")
        claim = self.next_claim(newer)
        producer.publish_memory_result(claim, self.selection((newer, newer.text, "self_purchase")),
            now=self.clock + timedelta(seconds=15))
        read = producer.read_memory_timeline(self.client, boundary=self.boundary(old))
        self.assertEqual(read.reason, "timeline_head_after_sealed_watermark")
        self.assertFalse(read.text)

    def test_complete_delta_count_and_character_budgets_fail_closed(self):
        self.head()
        for index in range(producer.TIMELINE_DELTA_LIMIT + 1):
            source = self.source(f"Новий запит {index}.")
        read = producer.read_memory_timeline(self.client, boundary=self.boundary(source))
        self.assertEqual(read.reason, "timeline_delta_count_budget")
        self.assertFalse(read.text)

    def test_long_delta_is_not_clipped_into_apparently_complete_read(self):
        self.head()
        source = self.source("А" * (producer.TIMELINE_DELTA_CHARS + 1))
        self.assertEqual(producer.read_memory_timeline(self.client, boundary=self.boundary(source)).reason,
            "timeline_delta_char_budget")

    def test_changed_retained_source_cannot_be_carried_or_read(self):
        source, _ = self.head()
        type(source).objects.filter(pk=source.pk).update(text="Змінене джерело.")
        newer = self.source("Для себе.")
        self.assertEqual(producer.read_memory_timeline(self.client, boundary=self.boundary(newer)).reason,
            "timeline_source_changed")
        producer.enqueue_memory_source(newer.pk, now=self.clock + timedelta(seconds=10))
        self.assertIsNone(producer.claim_memory_job(client_id=self.client.pk,
            now=self.clock + timedelta(seconds=14)))
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_producer_state["last_reason"], "timeline_source_changed")

    def test_invalid_paraphrase_dates_and_foreign_ids_never_publish(self):
        source = self.source("Це подарунок для сестри.")
        producer.enqueue_memory_source(source.pk, now=self.clock)
        claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=4))
        result = producer.publish_memory_result(claim, self.selection((source, "Подарунок мамі.", "gift")),
            now=self.clock + timedelta(seconds=5))
        self.assertFalse(result.published)
        self.assertEqual(result.reason, "quote_not_source_span")
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_version, 0)

    def test_contact_quote_is_an_explicit_omission_not_redacted_authority(self):
        source = self.source("Мій телефон +380501234567.")
        producer.enqueue_memory_source(source.pk, now=self.clock)
        claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=4))
        result = producer.publish_memory_result(claim, self.selection((source, source.text, "recipient")),
            now=self.clock + timedelta(seconds=5))
        self.assertTrue(result.published)
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_provenance["timeline"]["events"], [])
        self.assertNotIn("380501234567", self.client.memory_summary)
        self.assertIn({"source_message_id": source.pk, "reason": "contact_details"},
            self.client.memory_provenance["timeline"]["coverage"]["omissions"])

    def test_source_clamping_cannot_advance_head_past_omitted_correction(self):
        old, _ = self.head()
        before = deepcopy(self.client.memory_provenance)
        source = self.source("А" * (producer.TIMELINE_SOURCE_CHARS + 1) + " Не подарунок, для себе.")
        producer.enqueue_memory_source(source.pk, now=self.clock + timedelta(seconds=10))
        claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=14))
        self.assertIsNone(claim)
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_producer_state["last_reason"], "timeline_delta_char_budget")
        self.assertEqual(self.client.memory_provenance, before)
        read = producer.read_memory_timeline(self.client, boundary=self.boundary(source))
        self.assertEqual(read.reason, "historical_as_of")
        self.assertEqual(read.provenance["read_delta"][0]["text"], source.text)

    def test_legacy_plaintext_is_not_promoted_and_v1_reader_stays_strict(self):
        with self.settings(IG_MEMORY_TIMELINE_ENABLED=False):
            source, _ = fixtures.CapturedMemoryProducerTests.head(self, "old narrative")
            self.client.refresh_from_db()
            self.assertEqual(producer.read_memory_summary(self.client).reason, "current")
        read = producer.read_memory_timeline(self.client, boundary=self.boundary(source))
        self.assertEqual(read.reason, "timeline_provenance_missing")
        self.assertEqual(producer.read_memory_summary(self.client).reason, "current")
        newer = self.source("Для себе.")
        claim = self.next_claim(newer)
        self.assertEqual(claim.capture["previous_events"], [])
        self.assertEqual(claim.capture["coverage"]["previous_head_reason"], "timeline_provenance_missing")

    def test_hidden_and_erasure_fence_enqueue_claim_reader_and_publication(self):
        source, _ = self.head()
        newer = self.source("Для себе.")
        claim = self.next_claim(newer)
        IgClient.objects.filter(pk=self.client.pk).update(hidden_at=self.clock)
        self.assertEqual(producer.enqueue_memory_source(newer.pk).reason, "client_hidden")
        self.assertEqual(producer.read_memory_timeline(self.client, boundary=self.boundary(newer)).reason, "client_hidden")
        self.assertEqual(producer.publish_memory_result(claim, self.selection(),
            now=self.clock + timedelta(seconds=15)).reason, "client_hidden")
        self.assertIsNone(producer.claim_memory_job(client_id=self.client.pk,
            now=self.clock + timedelta(seconds=16)))
        IgClient.objects.filter(pk=self.client.pk).update(hidden_at=None, privacy_erasure_started_at=self.clock)
        self.assertEqual(producer.read_memory_timeline(self.client, boundary=self.boundary(newer)).reason, "client_erasing")

    def test_reset_fences_historical_reader(self):
        source, _ = self.head()
        IgFunnelResetAudit.objects.create(client=self.client, reset_after_message_id=source.pk, reason="timeline reset")
        self.assertEqual(producer.read_memory_timeline(self.client, boundary=self.boundary(source)).reason,
            "timeline_scope_changed")

    def test_local_ingestion_time_is_explicit_and_not_provider_event_time(self):
        source = self.source("Для себе.")
        type(source).objects.filter(pk=source.pk).update(provider_created_at=None)
        source.refresh_from_db()
        producer.enqueue_memory_source(source.pk, now=self.clock)
        claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=4))
        result = producer.publish_memory_result(claim, self.selection((source, source.text, "self_purchase")),
            now=self.clock + timedelta(seconds=5))
        self.assertTrue(result.published, result.reason)
        self.client.refresh_from_db()
        event = self.client.memory_provenance["timeline"]["events"][0]
        self.assertEqual((event["event_at"], event["time_basis"]), (source.created_at.isoformat(), "local_ingest"))

    def test_single_existing_facade_request_has_no_preflight_and_strict_json_output(self):
        source = self.source("Чи є розмір L?")
        producer.enqueue_memory_source(source.pk, now=self.clock)
        with self.settings(IG_MEMORY_GENERATION_ENABLED=True, IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=True), patch(
            "management.services.call_ai_analysis.gemini_generate_text",
            return_value={"parsed": json.dumps(self.selection((source, source.text, "availability_inquiry")))},
        ) as generate:
            result = producer.process_due_memory(now=self.clock + timedelta(seconds=4))
        self.assertEqual(result["published"], 1, result)
        generate.assert_called_once()
        self.assertEqual(generate.call_args.kwargs["reasoning_task"], "memory_summary")
        self.assertIn("pre_dispatch_guard", generate.call_args.kwargs)

    def test_pure_read_validator_rejects_forged_delta_shape_and_source_proofs(self):
        source, _ = self.head()
        newer = self.source("Для себе.")
        read = producer.read_memory_timeline(self.client, boundary=self.boundary(newer))
        self.assertEqual(read.reason, "historical_as_of")
        for section, field, value in (("read_delta", "role", "authority"),
            ("read_delta", "scope", {"episode_id": 999}), ("sources", "message_id", True)):
            view = deepcopy(vars(read))
            if section == "sources":
                view["provenance"]["capture"]["sources"][0][field] = value
            else:
                view["provenance"][section][0][field] = value
            self.assertNotEqual(producer.validate_memory_timeline_read(view, self.boundary(newer)), "")


@override_settings(**admission_fixtures.ENFORCE, IG_MEMORY_TIMELINE_ENABLED=True,
    IG_MEMORY_GENERATION_ENABLED=True, IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=True)
class MemoryTimelineProviderTransportTests(AnalysisPrivacyCleanupMixin, TransactionTestCase):
    """Exercise the real v2 payload/facade/accounting; mock only physical POST."""
    source = fixtures.CapturedMemoryProducerTests.source
    _profile = admission_fixtures.NonliveFinalAdmissionTests._profile
    _facade = admission_fixtures.NonliveFinalAdmissionTests._facade

    def setUp(self):
        provider_fixtures.MemoryProviderGateTests.setUp(self)
        from management.services import call_ai_analysis as ai
        ai.gemini_keys.clear_model_overload()
        self.addCleanup(ai.gemini_keys.clear_model_overload)
        self.event_source = self.source("Це подарунок для сестри.")
        self.assertTrue(producer.enqueue_memory_source(self.event_source.pk, now=self.clock).queued)
        self.selection = {"version": EVENT_VERSION, "events": [{
            "source_message_id": self.event_source.pk,
            "quote": self.event_source.text, "topic": "gift",
        }]}

    def response(self):
        return accounting_fixtures._Response(payload={
            "candidates": [{"finishReason": "STOP", "content": {
                "parts": [{"text": json.dumps(self.selection, ensure_ascii=False)}],
            }}],
            "usageMetadata": {"promptTokenCount": 110, "candidatesTokenCount": 30,
                "totalTokenCount": 140},
        })

    def test_actual_v2_facade_post_publishes_exact_dated_head_and_one_quota_dispatch(self):
        from management.models import GeminiRequest, GeminiQuotaState
        with self._facade(response=self.response()) as (post, cancel):
            result = producer.process_due_memory(now=self.clock + timedelta(seconds=4))
        self.assertEqual((result["claimed"], result["published"], result["failed"]), (1, 1, 0), result)
        post.assert_called_once()
        cancel.assert_not_called()
        sent = json.loads(post.call_args.kwargs["data"])
        text = sent["contents"][0]["parts"][0]["text"]
        self.assertIn(EVENT_VERSION, text)
        self.assertIn(self.event_source.text, text)
        self.assertEqual(sent["generationConfig"]["responseMimeType"], "application/json")
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_version, 1)
        proof = self.client.memory_provenance
        self.assertEqual(proof["version"], producer.TIMELINE_VERSION)
        event = proof["timeline"]["events"][0]
        self.assertEqual((event["source_message_id"], event["quote"], event["event_at"], event["time_basis"]),
            (self.event_source.pk, self.event_source.text, self.event_source.provider_created_at.isoformat(), "provider"))
        self.assertIsNone(event["scope"])
        read = producer.read_memory_timeline(self.client, boundary={
            "client_id": self.client.pk, "source_namespace": self.event_source.provider_namespace,
            "reset_id": None, "reset_floor": 1, "erasure_epoch": "",
            "watermark": producer._watermark(self.event_source),
        })
        self.assertEqual(read.reason, "historical_as_of")
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.accounting_mode, "enforced")
        self.assertEqual(graph.attempts.filter(provider_started_at__isnull=False).count(), 1)
        self.assertEqual(GeminiQuotaState.objects.get().rpd_dispatched, 1)

    def test_v2_source_erasure_at_final_admission_is_zero_post_head_and_spend(self):
        from management.models import GeminiRequest, GeminiRequestAttempt, GeminiQuotaState
        from management.services import gemini_accounting_runtime as runtime
        validate = runtime.RequestObserver._validate_boundary
        def erase_after_planning(observer, boundary):
            admitted = validate(observer, boundary)
            IgClient.objects.filter(pk=self.client.pk).update(privacy_erasure_started_at=timezone.now())
            return admitted
        with self._facade(response=self.response()) as (post, _cancel), patch.object(
            runtime.RequestObserver, "_validate_boundary", erase_after_planning,
        ):
            result = producer.process_due_memory(now=self.clock + timedelta(seconds=4))
        self.assertEqual((result["claimed"], result["published"]), (1, 0), result)
        post.assert_not_called()
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_version, 0)
        self.assertFalse(self.client.memory_summary)
        self.assertFalse(self.client.memory_provenance)
        rows = GeminiRequestAttempt.objects.filter(request_graph=GeminiRequest.objects.get())
        self.assertFalse(rows.filter(provider_started_at__isnull=False).exists())
        self.assertTrue(rows.filter(failure_kind="source_admission_denied").exists())
        self.assertFalse(GeminiQuotaState.objects.filter(rpd_dispatched__gt=0).exists())
