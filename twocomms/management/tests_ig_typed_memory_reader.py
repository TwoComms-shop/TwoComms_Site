import json
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.models import IgFunnelResetAudit, IgMemoryHead
from management.services import ig_typed_memory as memory
from management.tests_ig_typed_memory import SHADOW, TypedMemoryRuntimeTests


class TypedMemoryReaderTests(TestCase):
    def setUp(self):
        # Reuse the existing valid Analysis V2 fixture without inheriting its
        # larger projector test suite.
        TypedMemoryRuntimeTests.setUp(self)

    @override_settings(**SHADOW)
    def test_reads_current_scoped_facts_without_customer_text(self):
        self.assertEqual(memory.publish_analysis_memory(self.result.pk).status, "published")

        read = memory.read_typed_memory(
            self.client_row,
            episode_id=self.episode.pk,
            line_id="line:primary",
        )

        self.assertEqual(read["status"], memory.MEMORY_READ_OK)
        self.assertEqual(
            {fact["fact_key"] for fact in read["facts"]},
            {"observed_language", "objection_observed", "deferred_intent"},
        )
        self.assertEqual(read["source_watermark_message_id"], self.message.pk)
        self.assertEqual(read["evidence_message_ids"], [self.message.pk])
        self.assertNotIn("Після зарплати", json.dumps(read, ensure_ascii=False))
        self.assertNotIn("phone", json.dumps(read, ensure_ascii=False))

    @override_settings(**SHADOW)
    def test_scope_isolation_excludes_other_episode_and_line(self):
        self.assertEqual(memory.publish_analysis_memory(self.result.pk).status, "published")

        other_episode = memory.read_typed_memory(
            self.client_row,
            episode_id=self.episode.pk + 1000,
            line_id="line:primary",
        )
        self.assertEqual(other_episode["status"], memory.MEMORY_READ_OK)
        self.assertEqual(
            {fact["fact_key"] for fact in other_episode["facts"]},
            {"observed_language"},
        )

        other_line = memory.read_typed_memory(
            self.client_row,
            episode_id=self.episode.pk,
            line_id="line:secondary",
        )
        self.assertEqual(other_line["status"], memory.MEMORY_READ_OK)
        self.assertEqual(
            {fact["fact_key"] for fact in other_line["facts"]},
            {"observed_language", "deferred_intent"},
        )

    @override_settings(**SHADOW)
    def test_invalid_chain_fails_closed_without_facts(self):
        self.assertEqual(memory.publish_analysis_memory(self.result.pk).status, "published")
        current_fact_id = IgMemoryHead.objects.get(
            fact_key="observed_language",
        ).current_fact_id

        with patch.object(
            memory,
            "fact_integrity_valid",
            side_effect=lambda fact: fact.pk != current_fact_id,
        ):
            read = memory.read_typed_memory(
                self.client_row,
                episode_id=self.episode.pk,
                line_id="line:primary",
            )

        self.assertEqual(read["status"], memory.MEMORY_READ_INVALID)
        self.assertEqual(read["reason"], "integrity_failure")
        self.assertEqual(read["facts"], [])
        self.assertEqual(read["omitted"]["invalid"], 1)

    @override_settings(**SHADOW)
    def test_reset_floor_excludes_old_facts_as_stale(self):
        self.assertEqual(memory.publish_analysis_memory(self.result.pk).status, "published")
        IgFunnelResetAudit.objects.create(
            client=self.client_row,
            reset_after_message_id=self.message.pk,
            reason="reader test reset",
        )

        read = memory.read_typed_memory(
            self.client_row,
            episode_id=self.episode.pk,
            line_id="line:primary",
        )

        self.assertEqual(read["status"], memory.MEMORY_READ_STALE)
        self.assertEqual(read["reason"], "reset_or_freshness_floor")
        self.assertEqual(read["facts"], [])
        self.assertEqual(read["omitted"]["stale"], 3)

    @override_settings(**SHADOW)
    def test_empty_and_unknown_outcomes_are_explicit(self):
        empty = memory.read_typed_memory(
            self.client_row,
            episode_id=self.episode.pk,
            line_id="line:primary",
        )
        self.assertEqual(empty["status"], memory.MEMORY_READ_EMPTY)
        self.assertEqual(empty["facts"], [])

        unknown = memory.read_typed_memory(999999)
        self.assertEqual(unknown["status"], memory.MEMORY_READ_UNKNOWN)
        self.assertEqual(unknown["reason"], "client_unavailable")

    @override_settings(**SHADOW)
    def test_read_has_no_write_queries_or_provider_calls(self):
        self.assertEqual(memory.publish_analysis_memory(self.result.pk).status, "published")
        with patch("management.services.bot_memory.gemini_generate_text") as provider:
            with CaptureQueriesContext(connection) as captured:
                read = memory.read_typed_memory(
                    self.client_row,
                    episode_id=self.episode.pk,
                    line_id="line:primary",
                )

        self.assertEqual(read["status"], memory.MEMORY_READ_OK)
        provider.assert_not_called()
        self.assertFalse(any(
            query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                for query in captured.captured_queries
        ))
