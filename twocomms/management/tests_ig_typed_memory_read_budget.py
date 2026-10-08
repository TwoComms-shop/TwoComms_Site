"""Bounded reads with real signed assertion chains and unchanged SQL guards."""
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import IgMemoryFact, IgMemoryFactEvidence, IgMemoryHead
from management.services import ig_typed_memory as memory
from management import tests_ig_typed_memory as fixtures
from management.tests_support import AnalysisPrivacyCleanupMixin


@override_settings(**fixtures.SHADOW)
class TypedMemoryReadBudgetTests(AnalysisPrivacyCleanupMixin, TestCase):
    _next_language_result = fixtures.TypedMemoryRuntimeTests._next_language_result

    def setUp(self):
        fixtures.TypedMemoryRuntimeTests.setUp(self)
        self.assertEqual(memory.publish_analysis_memory(self.result.pk).created_facts, 3)

    def append_assertion_fixture(self, head, revision):
        """Skip quadratic publisher scanning only while constructing test history.

        Every immutable source/result/fact/evidence and head transition still passes
        its real model and database guard; no validator or trigger is patched.
        """
        result = self._next_language_result(
            language="ru" if revision % 2 == 0 else "uk",
            suffix=f"read-budget-{revision}",
        )
        candidate = next(row for row in memory.candidates_from_result(result)
                         if row.fact_key == "observed_language")
        key_id, _ = memory._keyring()
        values = memory._fact_payload(result, candidate, slot_key=head.slot_key,
                                      supersedes_id=head.current_fact_id, key_id=key_id)
        _, signature = memory._mac("management.typed-memory.fact.v1",
                                   memory._fact_hmac_payload(values), key_id=key_id)
        fact = IgMemoryFact(**values, integrity_hmac=signature)
        fact.source_result = result
        fact.supersedes = head.current_fact
        fact.save(force_insert=True)
        for ordinal, message_id in enumerate(candidate.evidence_ids, 1):
            payload = {"fact_id": fact.pk, "ordinal": ordinal, "message_id": message_id,
                       "source_role": "user", "claim_code": "language",
                       "integrity_key_id": key_id}
            _, signature = memory._mac("management.typed-memory.evidence.v1", payload,
                                       key_id=key_id)
            IgMemoryFactEvidence.objects.create(**payload, evidence_hmac=signature)
        head.current_fact = fact
        head.revision = revision
        head.projected_at = timezone.now()
        payload = {name: getattr(head, name) for name in memory._head_hmac_payload({})}
        _, head.projection_hmac = memory._mac("management.typed-memory.head.v1", payload,
                                             key_id=key_id)
        head.save()
        return fact

    def read_with_queries(self):
        with CaptureQueriesContext(connection) as queries:
            read = memory.read_typed_memory(self.client_row.pk, episode_id=self.episode.pk,
                                            line_id="line:primary")
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT")
                            for row in queries))
        return read, list(queries)

    def test_multiple_slots_share_two_chain_queries_and_have_no_provider_effect(self):
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider:
            read, queries = self.read_with_queries()
        self.assertEqual(read["status"], memory.MEMORY_READ_OK)
        self.assertEqual(len(read["facts"]), 3)
        # Client, current episode, reset, heads/current evidence, chains/evidence.
        self.assertLessEqual(len(queries), 7)
        provider.assert_not_called()

    def test_full_512_assertion_chain_keeps_query_budget_and_validates_old_evidence(self):
        _, short_queries = self.read_with_queries()
        head = IgMemoryHead.objects.select_related("current_fact").get(
            fact_key="observed_language")
        old_fact_id = head.current_fact_id
        for revision in range(2, memory.MAX_CHAIN_DEPTH + 1):
            self.append_assertion_fixture(head, revision)
        read, long_queries = self.read_with_queries()
        self.assertEqual(read["status"], memory.MEMORY_READ_OK)
        self.assertEqual(len(long_queries), len(short_queries))
        self.assertEqual(next(row for row in read["facts"]
                              if row["fact_key"] == "observed_language")["revision"], 512)
        actual = memory.evidence_integrity_valid
        with patch.object(memory, "evidence_integrity_valid",
                          side_effect=lambda row: row.fact_id != old_fact_id and actual(row)):
            rejected, _ = self.read_with_queries()
        self.assertEqual(rejected["status"], memory.MEMORY_READ_INVALID)
        self.assertEqual(rejected["facts"], [])
        # Batching cannot become a disguised reset of the existing depth guard.
        result = self._next_language_result(language="en", suffix="depth-513-rejected")
        self.assertEqual(memory.publish_analysis_memory(result.pk).status,
                         "chain_depth_exhausted")
        head.refresh_from_db()
        self.assertEqual(head.revision, 512)

    def test_separate_reads_revalidate_retired_keys_and_old_chain_signatures(self):
        read, _ = self.read_with_queries()
        self.assertEqual(read["status"], memory.MEMORY_READ_OK)
        with self.settings(IG_TYPED_MEMORY_HMAC_ACTIVE_KEY_ID="tmk_retired",
                           IG_TYPED_MEMORY_HMAC_KEYRING={
                               "tmk_retired": "read-budget-retirement-secret-000001"}):
            rejected, _ = self.read_with_queries()
        self.assertEqual(rejected["status"], memory.MEMORY_READ_INVALID)
        read, _ = self.read_with_queries()
        self.assertEqual(read["status"], memory.MEMORY_READ_OK)

    def test_overflow_sentinel_is_per_slot_and_does_not_hide_sibling_chains(self):
        head = IgMemoryHead.objects.select_related("current_fact").get(
            fact_key="observed_language")
        for revision in range(2, 7):
            self.append_assertion_fixture(head, revision)
        heads = list(IgMemoryHead.objects.select_related("current_fact").order_by("slot_key"))
        # Use a smaller reader budget with legal rows under unchanged physical
        # guards: materialization must fetch budget+1 per slot, not a global cap.
        with patch.object(memory, "MAX_CHAIN_DEPTH", 1):
            memory._materialize_memory_read_chains(heads)
            for captured in heads:
                bundle = captured._typed_memory_read_chain
                if captured.fact_key == "observed_language":
                    self.assertEqual(len(bundle.facts), 2)
                    self.assertFalse(memory.memory_chain_valid(captured))
                else:
                    self.assertEqual(len(bundle.facts), 1)
                    self.assertTrue(memory.memory_chain_valid(captured))

    def test_materialized_bundle_rejects_changed_head_slot_limit_or_keyring(self):
        heads = list(IgMemoryHead.objects.select_related("current_fact").order_by("slot_key"))
        memory._materialize_memory_read_chains(heads)
        head, sibling = heads[:2]
        self.assertTrue(memory.memory_chain_valid(head))
        own_bundle = head._typed_memory_read_chain
        head._typed_memory_read_chain = sibling._typed_memory_read_chain
        self.assertFalse(memory.memory_chain_valid(head))
        head._typed_memory_read_chain = own_bundle
        with patch.object(memory, "MAX_CHAIN_DEPTH", memory.MAX_CHAIN_DEPTH + 1):
            self.assertFalse(memory.memory_chain_valid(head))
        active, ring = memory._keyring()
        retained = {key: value.decode() for key, value in ring.items()}
        retained["tmk_budget_rotated"] = "read-budget-rotation-secret-000000001"
        with self.settings(IG_TYPED_MEMORY_HMAC_ACTIVE_KEY_ID="tmk_budget_rotated",
                           IG_TYPED_MEMORY_HMAC_KEYRING=retained):
            self.assertTrue(memory.head_integrity_valid(head))
            self.assertFalse(memory.memory_chain_valid(head))
        # A newly signed changed head still cannot consume an old bundle.
        head.projected_at += timedelta(microseconds=1)
        payload = {name: getattr(head, name) for name in memory._head_hmac_payload({})}
        _, head.projection_hmac = memory._mac("management.typed-memory.head.v1", payload,
                                             key_id=active)
        self.assertTrue(memory.head_integrity_valid(head))
        with CaptureQueriesContext(connection) as queries:
            self.assertFalse(memory.memory_chain_valid(head))
        self.assertEqual(len(queries), 0)
