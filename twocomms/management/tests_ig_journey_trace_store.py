"""Trace persistence rejects stale, foreign and hand-crafted authority claims."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import IgClient, IgCommercialEpisode, IgJourneyTraceSnapshot, InstagramBotMessage
from management.services.ig_journey_trace_contract import normalize_journey_trace
from management.services.ig_journey_trace_store import (
    JourneyTraceStoreConflict, JourneyTraceStoreRejected, record_journey_trace,
)


class JourneyTraceStoreTests(TestCase):
    def setUp(self):
        self.buyer = IgClient.get_or_create_for_sender("trace-store-buyer")
        self.other = IgClient.get_or_create_for_sender("trace-store-other")
        self.message = InstagramBotMessage.objects.create(
            client=self.buyer, sender_id="trace-store-buyer", role="user", text="PRIVATE TEXT хочу худі",
        )
        self.by_id = {self.message.pk: {"message_id": self.message.pk, "role": "user", "text": self.message.text}}
        self.trace = self.normalized()
        self.analyzed_at = timezone.now()

    def normalized(self, *, by_id=None, message=None, target="catalog_discovery"):
        message = message or self.message
        return normalize_journey_trace({"schema_version": 1, "steps": [{
            "from_node": "inbound", "to_node": target, "kind": "progress", "reason_code": "entered",
            "confidence": 0.9, "evidence": [{"message_id": message.pk, "quote": "хочу худі"}],
        }], "current_node": target}, by_id=by_id or self.by_id, watermark=message.pk)

    def record(self, **overrides):
        values = dict(client_id=self.buyer.pk, episode_id=None, watermark=self.message.pk,
                      normalized_trace=self.trace, by_id=self.by_id, prompt_version="journey-trace.v1",
                      analysis_model="gemini-test", analyzed_at=self.analyzed_at)
        values.update(overrides)
        return record_journey_trace(**values)

    def test_valid_source_bound_snapshot_has_digests_no_raw_text_and_idempotent_identity(self):
        with CaptureQueriesContext(connection) as queries:
            saved = self.record()
        selects = [query for query in queries if query["sql"].lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 4)  # client, all sources, newest message, existing key
        self.assertEqual(self.record(analyzed_at=self.analyzed_at + timedelta(seconds=3)).pk, saved.pk)
        self.assertEqual(IgJourneyTraceSnapshot.objects.count(), 1)
        self.assertIsNone(saved.commercial_episode_id)
        self.assertEqual(saved.schema_version, "journey-trace.v1")
        self.assertEqual(saved.trace["authority"], "none")
        self.assertNotIn("PRIVATE", json.dumps(saved.trace))
        encoded = json.dumps(saved.trace, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
        self.assertEqual(saved.trace_digest, hashlib.sha256(encoded).hexdigest())
        changed = self.normalized(target="configured_line")
        with self.assertRaises(JourneyTraceStoreConflict):
            self.record(normalized_trace=changed)

    def test_privacy_missing_client_and_foreign_episode_are_rejected(self):
        foreign_episode = IgCommercialEpisode.objects.create(client=self.other, sequence=1, materialization_key="trace-foreign")
        for overrides in ({"episode_id": foreign_episode.pk}, {"client_id": self.other.pk}, {"client_id": 999999}, {"client_id": True}, {"episode_id": True}):
            with self.subTest(overrides=overrides), self.assertRaises(JourneyTraceStoreRejected):
                self.record(**overrides)
        for field in ("hidden_at", "privacy_erasure_started_at"):
            IgClient.objects.filter(pk=self.buyer.pk).update(**{field: timezone.now()})
            with self.assertRaises(JourneyTraceStoreRejected):
                self.record()
            IgClient.objects.filter(pk=self.buyer.pk).update(**{field: None})
        self.assertFalse(IgJourneyTraceSnapshot.objects.exists())

    def test_cited_source_foreign_missing_truncated_or_role_changed_is_rejected(self):
        shortened = deepcopy(self.by_id)
        shortened[self.message.pk]["text"] = "хочу худі"
        for row in (shortened, {self.message.pk: {**self.by_id[self.message.pk], "role": "manager"}}):
            with self.subTest(row=row), self.assertRaises(JourneyTraceStoreRejected):
                self.record(by_id=row, normalized_trace=self.normalized(by_id=row))
        InstagramBotMessage.objects.filter(pk=self.message.pk).update(client=self.other)
        with self.assertRaises(JourneyTraceStoreRejected):
            self.record()
        InstagramBotMessage.objects.filter(pk=self.message.pk).delete()
        with self.assertRaises(JourneyTraceStoreRejected):
            self.record()
        self.assertFalse(IgJourneyTraceSnapshot.objects.exists())

    def test_watermark_is_latest_owned_message_for_all_three_roles(self):
        for role in ("user", "manager", "model"):
            new = InstagramBotMessage.objects.create(client=self.buyer, sender_id="trace-store-buyer", role=role, text="new")
            with self.subTest(role=role), self.assertRaises(JourneyTraceStoreRejected):
                self.record()
            new.delete()
        InstagramBotMessage.objects.create(client=self.other, sender_id="trace-store-other", role="user", text="new")
        self.assertIsNotNone(self.record().pk)

    def test_handcrafted_payload_extra_keys_digests_and_authority_are_rejected(self):
        variants = [normalize_journey_trace(None, by_id=self.by_id, watermark=self.message.pk)]
        for key, value in (("authority", "payment_confirmed"), ("raw_text", "PRIVATE"), ("watermark", True)):
            trace = deepcopy(self.trace)
            trace[key] = value
            variants.append(trace)
        trace = deepcopy(self.trace)
        trace["steps"][0]["evidence"][0]["source_text_sha256"] = "a" * 64
        variants.append(trace)
        trace = deepcopy(self.trace)
        trace["steps"][0]["evidence"][0]["quote"] = "PRIVATE"
        variants.append(trace)
        for trace in variants:
            with self.subTest(trace=trace), self.assertRaises(JourneyTraceStoreRejected):
                self.record(normalized_trace=trace)
        self.assertFalse(IgJourneyTraceSnapshot.objects.exists())

    def test_model_and_query_mutation_paths_cannot_rewrite_snapshot(self):
        saved = self.record()
        with self.assertRaises(ValidationError):
            saved.save()
        with self.assertRaises(ValidationError):
            saved.delete()
        with self.assertRaises(ValidationError):
            IgJourneyTraceSnapshot.objects.filter(pk=saved.pk).update(trace={})
        with self.assertRaises(ValidationError):
            IgJourneyTraceSnapshot.objects.bulk_update([saved], ["trace"])
        with self.assertRaises(ValidationError):
            IgJourneyTraceSnapshot.objects.filter(pk=saved.pk).delete()
        with self.assertRaises(ValidationError):
            IgJourneyTraceSnapshot.objects.bulk_create([saved], update_conflicts=True, update_fields=["trace"], unique_fields=["snapshot_key"])
        saved.refresh_from_db()
        self.assertEqual(saved.trace, self.trace)
