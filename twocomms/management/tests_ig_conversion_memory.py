"""Offline regressions for finite catch-up and explicit durable communication."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management import tests_ig_memory_producer as fixtures
from management import tests_ig_memory_timeline_producer as timeline_fixtures
from management.models import IgCommercialEpisode, IgFunnelResetAudit, InstagramBotMessage
from management.services import ig_memory_producer as producer
from management.services import ig_communication_preferences as preferences
from management.services import ig_memory_timeline_consumers as useful
from management.services.bot_memory import build_timeline_payload


class ExplicitCommunicationParserTests(SimpleTestCase):
    def test_finite_direct_requests_and_temporary_scope(self):
        for text, expected in (
            ("Пишіть коротко, будь ласка.", [("verbosity", "short", "persistent")]),
            ("Поясніть докладніше, будь ласка.", [("verbosity", "detailed", "persistent")]),
            ("Зараз коротко", [("verbosity", "short", "current_turn")]),
            ("Пишіть без емодзі.", [("emoji", "omit", "persistent")]),
            ("Коротко, без емодзі", [("verbosity", "short", "persistent"), ("emoji", "omit", "persistent")]),
            ("Кратко и без эмодзи", [("verbosity", "short", "persistent"), ("emoji", "omit", "persistent")]),
            ("Короткі відповіді, без емодзі", [("verbosity", "short", "persistent"), ("emoji", "omit", "persistent")]),
            ("Краткие ответы и без эмодзи", [("verbosity", "short", "persistent"), ("emoji", "omit", "persistent")]),
            ("Детальні відповіді", [("verbosity", "detailed", "persistent")]),
            ("Подробные ответы", [("verbosity", "detailed", "persistent")]),
            ("Зараз коротко та без емодзі", [("verbosity", "short", "current_turn"), ("emoji", "omit", "current_turn")]),
            ("Please reply in detail and without emojis.", [("verbosity", "detailed", "persistent"), ("emoji", "omit", "persistent")]),
        ):
            with self.subTest(text=text):
                self.assertEqual([(row["slot"], row["value"], row["scope"])
                    for row in preferences.explicit_preferences(text)], expected)

    def test_quotes_reports_topics_and_negation_are_not_client_preferences(self):
        for text in ('Менеджер каже: пишіть коротко.', 'Вона просила: пишіть без емодзі.',
                     '«Пишіть коротко»', '> Please reply briefly',
                     'Пишіть на принті без емодзі.', 'Please do not answer briefly.',
                     'Я пишу коротко.', 'Я пишу коротко, без емодзі.', 'I write brief answers.',
                     '«Коротко, без емодзі»', '> Кратко и без эмодзи',
                     'Раніше: коротко, без емодзі.', 'Раньше: кратко и без эмодзи.',
                     'Принт: коротко, без емодзі.', 'The manager asked to reply briefly.'):
            with self.subTest(text=text):
                self.assertEqual(preferences.explicit_preferences(text), [])


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_MEMORY_TIMELINE_ENABLED=True,
    GEMINI_ACCOUNTING_V2_MODE="shadow", GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2020-01-01T08:00:00+00:00",
    GEMINI_NONLIVE_ADMISSION_MODE="enforce")
class ConversionMemoryTests(TransactionTestCase):
    source = fixtures.CapturedMemoryProducerTests.source
    claim = fixtures.CapturedMemoryProducerTests.claim
    setUp = fixtures.CapturedMemoryProducerTests.setUp
    head = timeline_fixtures.MemoryTimelineProducerTests.head
    selection = timeline_fixtures.MemoryTimelineProducerTests.selection
    boundary = timeline_fixtures.MemoryTimelineProducerTests.boundary

    def enqueue(self, text):
        source = self.source(text)
        self.assertTrue(producer.enqueue_memory_source(source.pk, now=self.clock).queued)
        return source

    def sealed(self, row):
        return {"message_id": row.pk, "text": row.text, "role": row.role,
            "source_namespace": row.provider_namespace,
            "provider_created_at": row.provider_created_at.isoformat() if row.provider_created_at else "",
            "observed_created_at": row.created_at.isoformat()}

    def read_preferences(self, source, *, current=False):
        return preferences.capture_communication_preferences(self.client,
            boundary=self.boundary(source), sealed_sources=[self.sealed(source)] if current else [])

    def policy_capture(self, source):
        from management.services.ig_captured_policy_inputs import capture_policy_inputs
        from management.services.ig_turn_intelligence import capture_digest
        sources = [{**self.sealed(source), "source_digest": "a" * 64}]
        boundary = {**self.boundary(source), "episode_id": None, "order_id": None,
            "line_id": "", "recipient_id": "self", "source_ids": [source.pk],
            "source_digests": {str(source.pk): "a" * 64},
            "sealed_sources_digest": capture_digest(sources), "history_digests": {}}
        return capture_policy_inputs(self.client.pk, boundary=boundary, sources=sources,
            history=(), response_plan=SimpleNamespace(choices={}), captured_at=self.clock + timedelta(minutes=1))

    def test_existing_head_55_delta_sources_make_finite_ordered_progress(self):
        self.head()
        sources = [self.enqueue(f"Повідомлення {index}.") for index in range(55)]
        seen, checkpoints = [], 0
        for index in range(3):
            at = self.clock + timedelta(seconds=10 + index * 2)
            claim = producer.claim_memory_job(client_id=self.client.pk, now=at)
            self.assertIsNotNone(claim)
            seen.extend(row["message_id"] for row in claim.capture["sources"] if row["message_id"] in {row.pk for row in sources})
            self.assertLessEqual(claim.capture["coverage"]["processed_source_count"], 24)
            self.assertEqual(claim.capture["coverage"]["older_omitted_minimum"], 0)
            outcome = producer.publish_memory_result(claim, self.selection(), now=at + timedelta(seconds=1))
            self.assertTrue(outcome.published, outcome.reason)
            checkpoints += outcome.reason == "catchup_checkpoint"
            self.client.refresh_from_db()
            self.assertEqual(self.client.memory_attempts, 0)
        self.assertEqual(seen, [row.pk for row in sources])
        self.assertEqual(checkpoints, 2)
        self.assertIsNone(self.client.memory_due_at)
        self.assertEqual(self.client.memory_producer_state["consumed"], producer._watermark(sources[-1]))
        read = producer.read_memory_timeline(self.client, boundary=self.boundary(sources[-1]))
        self.assertEqual(read.reason, "historical_as_of")
        self.assertEqual(read.provenance["read_delta"], [])

    def test_aggregate_character_pages_and_2001_character_source_do_not_stall(self):
        self.head()
        sources = [self.enqueue("А" * 2001 + "."), self.enqueue("Б" * 8000 + "."), self.enqueue("Для себе.")]
        # The total is below 12k; every body reaches the admitted generation.
        claim = self.claim()
        self.assertIsNotNone(claim)
        self.assertEqual([row["text"] for row in claim.capture["timeline_inputs"] if row["source_message_id"] in {row.pk for row in sources}],
            [row.text for row in sources])
        self.assertTrue(producer.publish_memory_result(claim,
            self.selection((sources[-1], sources[-1].text, "correction")), now=self.clock + timedelta(seconds=5)).published)

    def test_checkpoint_progress_follows_provider_clock_even_when_ids_are_reversed(self):
        self.head()
        sources = [self.source(f"Деталь {index}.", at=self.clock + timedelta(microseconds=index))
            for index in range(31, 1, -1)]
        latest = self.source("Новий запит.", at=self.clock + timedelta(microseconds=40))
        producer.enqueue_memory_source(latest.pk, now=self.clock)
        expected = [row.pk for row in sorted([*sources, latest], key=producer._event_key)]
        seen = []
        for index in range(2):
            at = self.clock + timedelta(seconds=10 + index * 2)
            claim = producer.claim_memory_job(client_id=self.client.pk, now=at)
            self.assertIsNotNone(claim)
            seen.extend(row["message_id"] for row in claim.capture["sources"] if row["message_id"] in expected)
            self.assertTrue(producer.publish_memory_result(claim, self.selection(), now=at + timedelta(seconds=1)).published)
        self.assertEqual(seen, expected)
        self.assertEqual(producer.read_memory_timeline(self.client, boundary=self.boundary(latest)).reason, "historical_as_of")

    def test_large_single_source_has_overlapping_windows_and_no_readable_partial(self):
        self.head()
        text = ("Стара деталь. " * 2100) + "Передумала: тепер для себе."
        source = self.enqueue(text)
        previous_offset, pages, found_correction = 0, 0, False
        while pages < 6:
            at = self.clock + timedelta(seconds=10 + pages * 2)
            claim = producer.claim_memory_job(client_id=self.client.pk, now=at)
            self.assertIsNotNone(claim)
            dto = next(item for item in claim.capture["timeline_inputs"] if item["source_message_id"] == source.pk)
            self.assertLessEqual(len(dto["text"]), 12000)
            payload = json.loads(build_timeline_payload(claim.capture)["contents"][0]["parts"][0]["text"].split("\n\n", 1)[1])
            self.assertIn(source.pk, [row["source_message_id"] for row in payload["delta_sources"]])
            cursor = claim.capture["source_cursor"]
            if cursor:
                self.assertGreater(cursor["offset"], previous_offset)
                previous_offset = cursor["offset"]
            correction = "Передумала: тепер для себе."
            chosen = self.selection((source, correction, "correction")) if correction in dto["text"] else self.selection()
            found_correction |= correction in dto["text"]
            outcome = producer.publish_memory_result(claim, chosen, now=at + timedelta(seconds=1))
            self.assertTrue(outcome.published, outcome.reason)
            self.client.refresh_from_db()
            read = producer.read_memory_timeline(self.client, boundary=self.boundary(source))
            pages += 1
            if not cursor:
                self.assertEqual(read.reason, "historical_as_of")
                self.assertIn(correction, read.text)
                break
            self.assertEqual(read.reason, "timeline_source_catchup_pending")
            self.assertFalse(read.text)
            self.assertFalse(claim.capture["coverage"]["complete"])
        self.assertTrue(found_correction)
        self.assertEqual(pages, 3)
        self.assertIsNone(self.client.memory_due_at)

    def test_segment_cut_cannot_manufacture_complete_sentence_or_pick_unseen_tail(self):
        self.head()
        source = self.enqueue("А" * 12001 + ". Не подарунок, для себе.")
        claim = self.claim()
        result = producer.publish_memory_result(claim,
            self.selection((source, "Не подарунок, для себе.", "correction")), now=self.clock + timedelta(seconds=5))
        self.assertEqual(result.reason, "quote_not_source_span")
        self.assertFalse(result.published)

    def test_source_mutation_and_reset_fence_continuation(self):
        self.head()
        source = self.enqueue("А" * 24001)
        claim = self.claim()
        self.assertTrue(producer.publish_memory_result(claim, self.selection(), now=self.clock + timedelta(seconds=5)).published)
        InstagramBotMessage.objects.filter(pk=source.pk).update(text="Змінено.")
        self.assertIsNone(producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=6)))
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_producer_state["last_reason"], "timeline_source_changed")
        IgFunnelResetAudit.objects.create(client=self.client, reset_after_message_id=source.pk, reason="reset")
        self.assertIsNone(producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=100)))

    def test_capture_blockage_is_visible_in_drain_counters(self):
        self.head()
        source = self.enqueue("Новий запит.")
        InstagramBotMessage.objects.filter(pk=source.pk - 1).update(text="Changed source.")
        with patch.object(producer, "_automatic_admission_reason", return_value=""), patch.object(producer, "_background_admission_reason", return_value=""):
            result = producer.process_due_memory(now=self.clock + timedelta(seconds=10), generate=lambda _claim: self.selection())
        self.assertEqual(result["capture_blocked"], 1)
        self.assertEqual(result["reason"], "timeline_source_changed")
        self.assertEqual(result["published"], 0)
        with patch.object(producer, "_automatic_admission_reason", return_value=""), patch.object(producer, "_background_admission_reason", return_value=""):
            deferred = producer.process_due_memory(now=self.clock + timedelta(seconds=11), generate=lambda _claim: self.selection())
        self.assertEqual(deferred["reason"], "pending_backoff")
        self.assertEqual(deferred["backoff_count_minimum"], 1)

    def test_shared_historical_fit_recipient_dto_reuses_frozen_read_without_queries(self):
        source, _ = self.head("Для сестри: розмір M, ширина 52 см.", topic="measurements")
        memory = producer.read_memory_timeline(self.client, boundary=self.boundary(source))
        with self.assertNumQueries(0):
            dto = useful.capture_useful_memory(self.client, boundary=self.boundary(source), captured_memory=memory)
        self.assertFalse(dto["current_selection_authority"])
        observation = dto["observations"][0]
        self.assertEqual(observation["quote"], source.text)
        self.assertEqual(set(observation["kinds"]), {"measurement", "size_history", "recipient_history"})
        self.assertEqual(observation["source_ref"]["message_id"], source.pk)
        self.assertEqual(observation["applicability"], "reconfirm_for_current_recipient_and_garment")
        self.assertNotIn("selected_size", json.dumps(dto))
        self.assertIn("never choose a new order size", useful.render_useful_memory(dto))
        foreign = {**self.boundary(source), "source_namespace": "foreign:owner"}
        self.assertEqual(useful.capture_useful_memory(self.client, boundary=foreign, captured_memory=memory)["observations"], [])

    def test_historical_report_is_not_promoted_and_current_correction_remains_separate(self):
        old, _ = self.head("Менеджер сказала: розмір M.", topic="size_history")
        source = self.enqueue("Для себе хочу розмір L.")
        memory = producer.read_memory_timeline(self.client, boundary=self.boundary(source))
        dto = useful.capture_useful_memory(self.client, boundary=self.boundary(source), captured_memory=memory)
        self.assertEqual(dto["observations"], [])
        self.assertEqual(memory.provenance["read_delta"][0]["source_message_id"], source.pk)

    def test_explicit_preferences_survive_history_and_temporary_override_does_not_erase(self):
        old = self.enqueue("Пишіть коротко. Пишіть без емодзі.")
        for index in range(80):
            source = self.enqueue(f"Дякую {index}.")
        durable = self.read_preferences(source)
        self.assertEqual(durable["slots"]["verbosity"]["value"], "short")
        self.assertEqual(durable["slots"]["emoji"]["value"], "omit")
        self.assertEqual(durable["slots"]["verbosity"]["source_message_id"], old.pk)
        temporary = self.enqueue("Зараз поясніть докладніше.")
        turn = self.read_preferences(temporary, current=True)
        self.assertEqual(turn["slots"]["verbosity"]["value"], "detailed")
        self.assertEqual(turn["slots"]["verbosity"]["scope"], "current_turn")
        later = self.enqueue("Яка ціна?")
        restored = self.read_preferences(later, current=True)
        self.assertEqual(restored["slots"]["verbosity"]["value"], "short")
        self.assertIn("necessary information", preferences.render_communication_preferences(restored))

    def test_bare_combined_style_request_survives_into_next_actual_policy_capture(self):
        self.enqueue("Коротко, без емодзі")
        current = self.enqueue("Які є кольори?")
        captured = self.policy_capture(current)
        self.assertEqual(captured.communication_preferences["slots"]["verbosity"]["value"], "short")
        self.assertEqual(captured.communication_preferences["slots"]["emoji"]["value"], "omit")
        self.assertIn("Keep the reply concise", captured.automation_note)
        self.assertIn("Do not use emoji", captured.automation_note)

    def test_preference_capture_reuses_validated_owner_and_has_zero_read_empty_path(self):
        source = self.enqueue("Яка ціна?")
        self.client.refresh_from_db()
        with self.assertNumQueries(0):
            empty = preferences.capture_communication_preferences(self.client,
                boundary=self.boundary(source), sealed_sources=[self.sealed(source)], validated_owner=self.client)
        self.assertEqual(empty["slots"], {})
        source = self.enqueue("Пишіть коротко.")
        self.client.refresh_from_db()
        with self.assertNumQueries(1):
            dto = preferences.capture_communication_preferences(self.client,
                boundary=self.boundary(source), sealed_sources=[self.sealed(source)], validated_owner=self.client)
        self.assertEqual(dto["slots"]["verbosity"]["value"], "short")

    def test_persistent_supersession_own_source_only_and_episode_independent(self):
        old = self.enqueue("Пишіть коротко.")
        manager = self.source("Пишіть докладніше.", role="manager", source="manager")
        producer.enqueue_memory_source(manager.pk, now=self.clock)
        quote = self.enqueue('Менеджер каже: "Пишіть докладніше".')
        self.assertEqual(self.read_preferences(quote)["slots"]["verbosity"]["value"], "short")
        episode = IgCommercialEpisode.objects.create(client=self.client, sequence=1, materialization_key="mem-pref-episode")
        self.client.current_commercial_episode = episode
        self.client.save(update_fields=["current_commercial_episode"])
        latest = self.enqueue("Поясніть докладніше, будь ласка.")
        result = self.read_preferences(latest)
        self.assertEqual(result["slots"]["verbosity"]["value"], "detailed")
        self.assertEqual(result["slots"]["verbosity"]["supersedes_source_message_id"], old.pk)

    def test_delayed_older_own_preference_reaches_next_actual_policy_capture_without_rewind(self):
        head, _ = self.head("Яка ціна?", topic="purchase_inquiry")
        self.client.refresh_from_db()
        before = deepcopy(self.client.memory_producer_state)
        head_before = (self.client.memory_version, deepcopy(self.client.memory_provenance))
        delayed = self.source("Пишіть без емодзі.", at=head.provider_created_at - timedelta(seconds=1))
        result = producer.enqueue_memory_source(delayed.pk, now=self.clock + timedelta(seconds=6))
        self.assertFalse(result.queued)
        self.assertEqual(result.reason, "source_already_consumed_or_historical")
        self.client.refresh_from_db()
        for field in ("dirty", "dirty_digest", "consumed", "last_reason"):
            self.assertEqual(self.client.memory_producer_state[field], before[field])
        self.assertIsNone(self.client.memory_due_at)
        self.assertEqual((self.client.memory_version, self.client.memory_provenance), head_before)
        current = self.enqueue("Які є кольори?")
        captured = self.policy_capture(current)
        slot = captured.communication_preferences["slots"]["emoji"]
        self.assertEqual(slot["value"], "omit")
        self.assertEqual(slot["source_message_id"], delayed.pk)
        self.assertIn("Do not use emoji", captured.automation_note)

    def test_late_preference_updates_its_slot_clock_but_never_beats_newer_explicit_choice(self):
        self.enqueue("Пишіть без емодзі.")
        newer = self.source("Яка ціна?", at=self.clock + timedelta(seconds=10))
        producer.enqueue_memory_source(newer.pk, now=self.clock)
        middle = self.source("Можна емодзі.", at=self.clock + timedelta(seconds=5))
        self.assertFalse(producer.enqueue_memory_source(middle.pk, now=self.clock).queued)
        latest = self.source("Ще одне питання.", at=self.clock + timedelta(seconds=11))
        producer.enqueue_memory_source(latest.pk, now=self.clock)
        self.assertEqual(self.policy_capture(latest).communication_preferences["slots"]["emoji"]["value"], "allow")
        final = self.source("Пишіть без емодзі.", at=self.clock + timedelta(seconds=12))
        producer.enqueue_memory_source(final.pk, now=self.clock)
        stale = self.source("Можна емодзі.", at=self.clock + timedelta(seconds=7))
        self.assertFalse(producer.enqueue_memory_source(stale.pk, now=self.clock).queued)
        current = self.source("Підкажіть ціну.", at=self.clock + timedelta(seconds=13))
        producer.enqueue_memory_source(current.pk, now=self.clock)
        slot = self.policy_capture(current).communication_preferences["slots"]["emoji"]
        self.assertEqual(slot["value"], "omit")
        self.assertEqual(slot["source_message_id"], final.pk)

    def test_newer_unenqueued_event_blocks_narrative_but_not_valid_older_preference(self):
        newer = self.source("Яка ціна?", at=self.clock + timedelta(seconds=10))
        delayed = self.source("Пишіть без емодзі.", at=self.clock + timedelta(seconds=5))
        result = producer.enqueue_memory_source(delayed.pk, now=self.clock)
        self.assertEqual(result.reason, "historical_event_rewind")
        self.client.refresh_from_db()
        self.assertNotIn("dirty", self.client.memory_producer_state)
        self.assertEqual(self.client.memory_version, 0)
        current = self.source("Які є кольори?", at=self.clock + timedelta(seconds=11))
        producer.enqueue_memory_source(current.pk, now=self.clock)
        slot = self.policy_capture(current).communication_preferences["slots"]["emoji"]
        self.assertEqual(slot["source_message_id"], delayed.pk)
        self.assertEqual(slot["value"], "omit")

    def test_delayed_preference_persistence_preserves_reset_and_erasure_admission(self):
        self.head("Яка ціна?", topic="purchase_inquiry")
        delayed = self.source("Пишіть без емодзі.", at=self.clock - timedelta(seconds=1))
        self.client.refresh_from_db()
        before = deepcopy(self.client.memory_producer_state)
        IgFunnelResetAudit.objects.create(client=self.client, reset_after_message_id=delayed.pk, reason="reset")
        self.assertEqual(producer.enqueue_memory_source(delayed.pk, now=self.clock).reason, "source_before_reset")
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_producer_state, before)
        after_reset = self.source("Пишіть без емодзі.", at=self.clock - timedelta(seconds=2))
        self.client.privacy_erasure_started_at = self.clock
        self.client.save(update_fields=["privacy_erasure_started_at"])
        self.assertEqual(producer.enqueue_memory_source(after_reset.pk, now=self.clock).reason, "client_erasing")
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_producer_state, before)

    def test_preference_source_edit_erasure_foreign_namespace_reset_and_future_are_omitted(self):
        old = self.enqueue("Пишіть коротко.")
        boundary = self.boundary(old)
        latest = self.enqueue("Поясніть докладніше.")
        self.assertEqual(preferences.capture_communication_preferences(self.client, boundary=boundary)["slots"], {})
        wrong = {**self.boundary(latest), "source_namespace": "foreign:owner"}
        self.assertEqual(preferences.capture_communication_preferences(self.client, boundary=wrong)["slots"], {})
        InstagramBotMessage.objects.filter(pk=latest.pk).update(text="Edited source.")
        self.assertEqual(self.read_preferences(latest)["slots"], {})
        self.assertEqual(self.read_preferences(latest, current=True)["slots"], {})
        IgFunnelResetAudit.objects.create(client=self.client, reset_after_message_id=latest.pk, reason="reset")
        self.assertEqual(self.read_preferences(latest)["slots"], {})
        self.client.privacy_erasure_started_at = self.clock
        self.client.save(update_fields=["privacy_erasure_started_at"])
        self.assertEqual(self.read_preferences(latest)["slots"], {})
