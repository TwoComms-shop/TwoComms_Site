"""Read-time expiry of real signed current typed facts without a sweep."""
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import IgConversationAnalysisResult, IgMemoryFact, IgMemoryFactEvidence, IgMemoryHead
from management.services import ig_analysis_v2 as analysis_v2, ig_typed_memory as memory
from management import tests_ig_typed_memory as fixtures

SHADOW = fixtures.SHADOW


@override_settings(**SHADOW)
class TypedMemoryReadTtlTests(TestCase):
    _next_language_result = fixtures.TypedMemoryRuntimeTests._next_language_result

    def setUp(self):
        self.read_at = timezone.now(); self.expiry = self.read_at + timedelta(hours=1)
        original = IgConversationAnalysisResult.save
        def date_result(result, *args, **kwargs):
            result.deferred_kind = "date"; result.deferred_condition_code = "customer_date"; result.deferred_until = self.expiry
            if getattr(self, "only_date", False):
                result.detected_language = ""; result.language_evidence_message_ids = []
                result.active_objection_type = ""; result.active_objection_confidence = None
            for row in result.evidence_manifest:
                row["claim_codes"] = sorted(set(row["claim_codes"]) | {"deferred_intent"})
            result.result_digest = analysis_v2.result_digest_for_instance(result)
            return original(result, *args, **kwargs)
        self.date_patch = patch.object(IgConversationAnalysisResult, "save", date_result)
        self.date_patch.start(); self.addCleanup(self.date_patch.stop)
        fixtures.TypedMemoryRuntimeTests.setUp(self)
        self.assertEqual(memory.publish_analysis_memory(self.result.pk).status, "published")

    def read(self, now=None):
        return memory.read_typed_memory(self.client_row, episode_id=self.episode.pk,
            line_id="line:primary", now=self.read_at if now is None else now)

    def test_future_date_available_but_exact_expiry_and_after_omit_without_sweep(self):
        self.assertIn("deferred_intent", {row["fact_key"] for row in self.read()["facts"]})
        for at in (self.expiry, self.expiry + timedelta(seconds=1)):
            read = self.read(at)
            self.assertEqual(read["status"], memory.MEMORY_READ_OK)
            self.assertEqual(read["reason"], "valid_until_elapsed")
            self.assertEqual(read["omitted"]["expired"], 1)
            self.assertEqual({row["fact_key"] for row in read["facts"]}, {"observed_language", "objection_observed"})
        self.assertEqual(IgMemoryHead.objects.get(fact_key="deferred_intent").state, "active")

    def test_mixed_expiry_does_not_hide_another_slot_integrity_failure(self):
        with patch.object(memory, "MAX_READ_HEADS", 100):
            # Other scopes do not turn this date into a valid intent.
            read = self.read(self.expiry)
        self.assertFalse(any(row["fact_key"] == "deferred_intent" for row in read["facts"]))
        with patch.object(memory, "memory_chain_valid", side_effect=lambda head: head.fact_key == "deferred_intent"):
            invalid = self.read(self.expiry)
        self.assertEqual(invalid["status"], memory.MEMORY_READ_INVALID)
        self.assertEqual(invalid["facts"], [])

    def test_current_unexpired_supersession_ignores_expired_predecessor(self):
        old = IgMemoryHead.objects.get(fact_key="deferred_intent").current_fact_id
        self.expiry += timedelta(days=1)
        newer = self._next_language_result(language="uk", suffix="ttl-future", with_objection=True)
        self.assertEqual(memory.publish_analysis_memory(newer.pk).status, "published")
        head = IgMemoryHead.objects.select_related("current_fact").get(fact_key="deferred_intent")
        self.assertEqual(head.current_fact.supersedes_id, old)
        read = self.read(self.expiry - timedelta(hours=1))
        intent = next(row for row in read["facts"] if row["fact_key"] == "deferred_intent")
        self.assertEqual(intent["fact_id"], head.current_fact_id)
        self.assertEqual(intent["source_result_id"], newer.pk)

    def test_current_expired_supersession_never_falls_back_to_unexpired_older_fact(self):
        old = IgMemoryHead.objects.get(fact_key="deferred_intent").current_fact_id
        old_expiry = self.expiry
        # Both producer inputs are legitimate future dates. The newer fact
        # expires first; only the read clock advances beyond its deadline.
        self.expiry = self.read_at + timedelta(minutes=10)
        newer = self._next_language_result(language="uk", suffix="ttl-expired", with_objection=True)
        self.assertEqual(memory.publish_analysis_memory(newer.pk).status, "published")
        self.assertNotEqual(IgMemoryHead.objects.get(fact_key="deferred_intent").current_fact_id, old)
        at = self.expiry + timedelta(seconds=1)
        self.assertLess(at, old_expiry)
        self.assertGreater(self.expiry, self.read_at)
        read = self.read(at)
        self.assertEqual(read["omitted"]["expired"], 1)
        self.assertFalse(any(row["fact_key"] == "deferred_intent" for row in read["facts"]))

    def test_expiry_does_not_hide_corruption_or_weaken_keyring_validation(self):
        original = memory.fact_integrity_valid
        with patch.object(memory, "fact_integrity_valid", side_effect=lambda fact: fact.fact_key != "deferred_intent" and original(fact)):
            read = self.read(self.expiry)
        self.assertEqual(read["status"], memory.MEMORY_READ_INVALID); self.assertEqual(read["facts"], [])
        self.assertEqual(read["reason"], "integrity_failure")

    def test_read_expiry_is_select_only_no_tombstones_generation_or_client_mutations(self):
        counts = (IgMemoryFact.objects.count(), IgMemoryFactEvidence.objects.count(), IgMemoryHead.objects.count())
        with CaptureQueriesContext(connection) as queries, patch.object(memory, "expire_due_memory") as sweep, patch(
            "management.services.bot_memory.gemini_generate_text") as generation:
            self.read(self.expiry)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        sweep.assert_not_called(); generation.assert_not_called()
        self.assertEqual(counts, (IgMemoryFact.objects.count(), IgMemoryFactEvidence.objects.count(), IgMemoryHead.objects.count()))

    def test_default_current_clock_captured_once_and_invalid_clock_fails_before_db(self):
        with patch.object(memory.timezone, "now", return_value=self.expiry) as clock:
            read = memory.read_typed_memory(self.client_row, episode_id=self.episode.pk)
        self.assertEqual(clock.call_count, 1); self.assertEqual(read["omitted"]["expired"], 1)
        for invalid in (self.expiry.replace(tzinfo=None), "today", False):
            with CaptureQueriesContext(connection) as queries:
                result = memory.read_typed_memory(self.client_row, now=invalid)
            self.assertEqual(result["reason"], "invalid_read_time"); self.assertEqual(len(queries), 0)


@override_settings(**SHADOW)
class TypedMemoryOnlyDateExpiryTests(TestCase):
    def setUp(self):
        self.only_date = True
        TypedMemoryReadTtlTests.setUp(self)

    def test_all_current_facts_expired_is_finite_stale_with_no_evidence_or_watermark(self):
        read = memory.read_typed_memory(self.client_row, episode_id=self.episode.pk, now=self.expiry)
        self.assertEqual(read["status"], memory.MEMORY_READ_STALE)
        self.assertEqual(read["reason"], "valid_until_elapsed")
        self.assertEqual(read["facts"], []); self.assertEqual(read["evidence_message_ids"], [])
        self.assertEqual(read["source_watermark_message_id"], 0); self.assertEqual(read["omitted"]["expired"], 1)
