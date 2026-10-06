import datetime
import hashlib
import json
import re
from copy import copy
from decimal import Decimal
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import (
    IgClient,
    IgCommercialEpisode,
    IgConversationAnalysisJob,
    IgConversationAnalysisResult,
    IgConversationAnalysisSnapshot,
    IgMemoryFact,
    IgMemoryFactEvidence,
    IgMemoryHead,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services import ig_analysis_v2 as analysis_v2
from management.services import ig_typed_memory as memory


SHADOW = {
    "IG_ANALYSIS_MATERIALITY_MODE": "shadow",
    "IG_ANALYSIS_V2_MODE": "shadow",
    "IG_ANALYSIS_V2_EXTENDED_PROMPT": True,
    "IG_TYPED_MEMORY_MODE": "shadow_compare",
}


def _memory_trigger_rows(names):
    """Inspect the actual installed database, with no cross-vendor SQL."""
    placeholders = ", ".join(["%s"] * len(names))
    with connection.cursor() as cursor:
        if connection.vendor == "mysql":
            cursor.execute(
                "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION "
                "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
                f"AND TRIGGER_NAME IN ({placeholders})", list(names),
            )
            return {name: (table, timing.upper(), event.upper()) for name, table, timing, event in cursor.fetchall()}
        if connection.vendor == "sqlite":
            cursor.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger' "
                f"AND name IN ({placeholders})", list(names),
            )
            result = {}
            for name, table, sql in cursor.fetchall():
                match = re.search(r"\b(BEFORE|AFTER)\s+(INSERT|UPDATE|DELETE)\b", sql or "", re.I)
                result[name] = (table, match.group(1).upper(), match.group(2).upper()) if match else (table, "", "")
            return result
    raise AssertionError(f"Unsupported physical memory guard backend: {connection.vendor}")


def _unmigrated_sqlite_profile():
    modules = getattr(settings, "MIGRATION_MODULES", {})
    return connection.vendor == "sqlite" and "management" in modules and modules["management"] is None


class TypedMemoryRuntimeTests(TestCase):
    def setUp(self):
        self.client_row = IgClient.objects.create(igsid="typed-memory-client")
        self.episode = IgCommercialEpisode.objects.create(
            client=self.client_row,
            sequence=1,
            open_slot=1,
            materialization_key="typed-memory:episode:1",
            opened_watermark_message_id=1,
        )
        self.client_row.current_commercial_episode = self.episode
        self.client_row.save(update_fields=["current_commercial_episode", "updated_at"])
        self.message = InstagramBotMessage.objects.create(
            client=self.client_row,
            sender_id=self.client_row.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Після зарплати хочу футболку, але ціна важлива",
            status=InstagramBotMessage.Status.DONE,
        )
        self.fingerprint = hashlib.sha256(b"typed-memory-current-state").hexdigest()
        self.job = IgConversationAnalysisJob.objects.create(
            client=self.client_row,
            watermark_message_id=self.message.pk,
            analyzed_watermark_message_id=self.message.pk,
            revision=2,
            analyzed_revision=2,
            status=IgConversationAnalysisJob.Status.DONE,
            due_at=timezone.now(),
            next_attempt_at=timezone.now(),
            materiality_episode=self.episode,
            materiality_line_id="line:primary",
            materiality_event_highwater=7,
            analyzed_materiality_event_highwater=7,
            materiality_digest="a" * 64,
            analyzed_materiality_digest="a" * 64,
            authority_digest="b" * 64,
            artifact_digest="c" * 64,
            required_state_fingerprint=self.fingerprint,
        )
        self.snapshot = IgConversationAnalysisSnapshot.objects.create(
            client=self.client_row,
            last_analyzed_message=self.message,
            dedupe_key="typed-memory:snapshot:1",
            score_band=IgConversationAnalysisSnapshot.Band.QUALIFIED,
            interaction_type=IgConversationAnalysisSnapshot.InteractionType.PRICE_OBJECTION,
            commercial_episode=self.episode,
            required_state_fingerprint=self.fingerprint,
            analyzed_at=timezone.now(),
        )
        self.result = IgConversationAnalysisResult(
            result_key="analysis-v2:" + hashlib.sha256(b"typed-memory-result").hexdigest(),
            legacy_snapshot=self.snapshot,
            client=self.client_row,
            commercial_episode=self.episode,
            line_id="line:primary",
            watermark_message_id=self.message.pk,
            job_revision=2,
            materiality_event_highwater=7,
            materiality_digest="a" * 64,
            authority_digest="b" * 64,
            artifact_digest="c" * 64,
            state_correlation=analysis_v2.state_correlation(self.fingerprint),
            result_schema_version=analysis_v2.RESULT_SCHEMA_VERSION,
            normalizer_version=analysis_v2.NORMALIZER_VERSION,
            interaction_type=self.snapshot.interaction_type,
            score_band=self.snapshot.score_band,
            detected_language="uk",
            language_evidence_message_ids=[self.message.pk],
            evidence_manifest=[{
                "message_id": self.message.pk,
                "source_role": "user",
                "claim_codes": ["interaction", "objection", "deferred_intent"],
            }],
            customer_evidence_count=1,
            active_objection_type="price",
            active_objection_confidence=Decimal("0.8000"),
            deferred_kind=IgConversationAnalysisResult.DeferredKind.PAYDAY,
            deferred_condition_code="payday",
            result_digest="",
            analyzed_at=self.snapshot.analyzed_at,
        )
        self.result.result_digest = analysis_v2.result_digest_for_instance(self.result)
        self.result.save(force_insert=True)

    def _next_language_result(
        self, *, language="ru", suffix="next", line_id="line:primary",
        with_objection=False,
    ):
        message = InstagramBotMessage.objects.create(
            client=self.client_row,
            sender_id=self.client_row.igsid,
            role=InstagramBotMessage.Role.USER,
            text="language revision",
            status=InstagramBotMessage.Status.DONE,
        )
        next_revision = int(self.job.revision or 0) + 1
        next_highwater = int(self.job.materiality_event_highwater or 0) + 1
        materiality_digest = hashlib.sha256(
            f"typed-memory:{suffix}:materiality".encode()
        ).hexdigest()
        IgConversationAnalysisJob.objects.filter(pk=self.job.pk).update(
            watermark_message_id=message.pk,
            analyzed_watermark_message_id=message.pk,
            revision=next_revision,
            analyzed_revision=next_revision,
            materiality_event_highwater=next_highwater,
            analyzed_materiality_event_highwater=next_highwater,
            materiality_digest=materiality_digest,
            analyzed_materiality_digest=materiality_digest,
            materiality_line_id=line_id,
        )
        self.job.refresh_from_db()
        snapshot = IgConversationAnalysisSnapshot.objects.create(
            client=self.client_row,
            last_analyzed_message=message,
            dedupe_key=f"typed-memory:{suffix}:snapshot",
            score_band=IgConversationAnalysisSnapshot.Band.COLD,
            interaction_type=IgConversationAnalysisSnapshot.InteractionType.INFORMATION_ONLY,
            commercial_episode=self.episode,
            required_state_fingerprint=self.fingerprint,
            analyzed_at=timezone.now(),
        )
        result = IgConversationAnalysisResult(
            result_key="analysis-v2:" + hashlib.sha256(
                f"typed-memory:{suffix}:result".encode()
            ).hexdigest(),
            legacy_snapshot=snapshot,
            client=self.client_row,
            commercial_episode=self.episode,
            line_id=line_id,
            watermark_message_id=message.pk,
            job_revision=next_revision,
            materiality_event_highwater=next_highwater,
            materiality_digest=materiality_digest,
            authority_digest="b" * 64,
            artifact_digest="c" * 64,
            state_correlation=analysis_v2.state_correlation(self.fingerprint),
            result_schema_version=analysis_v2.RESULT_SCHEMA_VERSION,
            normalizer_version=analysis_v2.NORMALIZER_VERSION,
            interaction_type=snapshot.interaction_type,
            score_band=snapshot.score_band,
            detected_language=language,
            language_evidence_message_ids=[message.pk],
            evidence_manifest=[{
                "message_id": message.pk,
                "source_role": "user",
                "claim_codes": (
                    ["interaction", "objection"]
                    if with_objection else ["interaction"]
                ),
            }],
            customer_evidence_count=1,
            active_objection_type=("price" if with_objection else ""),
            active_objection_confidence=(
                Decimal("0.8000") if with_objection else None
            ),
            result_digest="",
            analyzed_at=snapshot.analyzed_at,
        )
        result.result_digest = analysis_v2.result_digest_for_instance(result)
        result.save(force_insert=True)
        return result

    def _append_direct_tombstone(self, head, *, revision):
        old = head.current_fact
        key_id, _ring = memory._keyring()
        observed_at = head.projected_at + datetime.timedelta(microseconds=1)
        values = {
            "record_key": "",
            "slot_key": old.slot_key,
            "client_id": old.client_id,
            "scope": old.scope,
            "commercial_episode_id": old.commercial_episode_id,
            "line_id": old.line_id,
            "order_id": old.order_id,
            "post_sale_case_id": old.post_sale_case_id,
            "fact_key": old.fact_key,
            "schema_version": memory.SCHEMA_VERSION,
            "operation": IgMemoryFact.Operation.INVALIDATE,
            "typed_value": {},
            "confidence": None,
            "source_role": "system",
            "producer": "deterministic_projector",
            "producer_policy_version": memory.PROJECTOR_VERSION,
            "closure_method": "deterministic_invalidation",
            "source_result_id": None,
            "source_result_digest": "",
            "source_materiality_digest": "",
            "source_state_correlation": "",
            "source_watermark_message_id": 0,
            "source_event_digest": hashlib.sha256(
                f"typed-memory:depth:{revision}".encode()
            ).hexdigest(),
            "expected_evidence_count": 0,
            "supersedes_id": old.pk,
            "reason_code": "reset_boundary",
            "integrity_key_id": key_id,
            "observed_at": observed_at,
            "valid_until": None,
            "sensitivity": old.sensitivity,
            "retention_class": old.retention_class,
        }
        identity = memory._fact_hmac_payload(values)
        identity.pop("record_key", None)
        identity.pop("supersedes_id", None)
        identity.pop("integrity_key_id", None)
        values["record_key"] = "memory-fact:" + memory._sha(identity)
        _kid, signature = memory._mac(
            "management.typed-memory.fact.v1",
            memory._fact_hmac_payload(values),
            key_id=key_id,
        )
        fact = IgMemoryFact(**values, integrity_hmac=signature)
        fact.save(force_insert=True)
        head_values = {
            "slot_key": head.slot_key,
            "client_id": head.client_id,
            "scope": head.scope,
            "commercial_episode_id": head.commercial_episode_id,
            "line_id": head.line_id,
            "order_id": head.order_id,
            "post_sale_case_id": head.post_sale_case_id,
            "fact_key": head.fact_key,
            "schema_version": head.schema_version,
            "current_fact_id": fact.pk,
            "state": IgMemoryHead.State.INVALIDATED,
            "revision": revision,
            "projection_policy_version": memory.PROJECTOR_VERSION,
            "projected_at": observed_at,
            "integrity_key_id": key_id,
        }
        _hkid, head_signature = memory._mac(
            "management.typed-memory.head.v1",
            memory._head_hmac_payload(head_values),
            key_id=key_id,
        )
        head.current_fact = fact
        head.state = IgMemoryHead.State.INVALIDATED
        head.revision = revision
        head.projected_at = observed_at
        head.projection_hmac = head_signature
        head.integrity_key_id = key_id
        head.save()
        return fact

    def test_off_mode_is_absolute_zero_query_and_typed_prompt_is_rejected(self):
        with CaptureQueriesContext(connection) as captured:
            outcome = memory.publish_analysis_memory(self.result.pk)
        self.assertEqual(outcome.status, "off")
        self.assertEqual(len(captured), 0)

        with override_settings(IG_TYPED_MEMORY_MODE="typed_prompt"):
            with CaptureQueriesContext(connection) as captured:
                outcome = memory.publish_analysis_memory(self.result.pk)
            self.assertEqual(outcome.status, "off")
            self.assertEqual(len(captured), 0)

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.gemini_generate_json")
    def test_one_result_projects_three_closed_facts_without_provider(self, provider):
        outcome = memory.publish_analysis_memory(self.result.pk)

        self.assertEqual(outcome.status, "published")
        self.assertEqual(outcome.created_facts, 3)
        self.assertEqual(IgMemoryFact.objects.count(), 3)
        self.assertEqual(IgMemoryHead.objects.count(), 3)
        self.assertEqual(IgMemoryFactEvidence.objects.count(), 3)
        self.assertEqual(
            set(IgMemoryFact.objects.values_list("fact_key", flat=True)),
            {"observed_language", "objection_observed", "deferred_intent"},
        )
        self.assertTrue(all(
            memory.memory_chain_valid(head)
            for head in IgMemoryHead.objects.select_related("current_fact")
        ))
        one_head = IgMemoryHead.objects.get(fact_key="observed_language")
        with CaptureQueriesContext(connection) as captured:
            self.assertTrue(memory.memory_chain_valid(one_head))
        self.assertLessEqual(len(captured), 2)
        provider.assert_not_called()

        repeated = memory.publish_analysis_memory(self.result.pk)
        self.assertEqual(repeated.status, "published")
        self.assertEqual(repeated.created_facts, 0)
        self.assertEqual(repeated.unchanged_heads, 3)
        self.assertEqual(IgMemoryFact.objects.count(), 3)

    @override_settings(**SHADOW)
    def test_late_result_and_forged_evidence_cannot_advance_heads(self):
        IgConversationAnalysisJob.objects.filter(pk=self.job.pk).update(
            materiality_digest="d" * 64,
            materiality_event_highwater=8,
        )
        outcome = memory.publish_analysis_memory(self.result.pk)
        self.assertEqual(outcome.status, "stale")
        self.assertFalse(IgMemoryHead.objects.exists())

        IgConversationAnalysisJob.objects.filter(pk=self.job.pk).update(
            materiality_digest="a" * 64,
            materiality_event_highwater=7,
        )
        installed = _memory_trigger_rows(("ig_anres_no_update",))
        if installed:
            self.assertEqual(installed["ig_anres_no_update"], ("management_igconversationanalysisresult", "BEFORE", "UPDATE"))
            with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE management_igconversationanalysisresult "
                    "SET language_evidence_message_ids=%s WHERE id=%s",
                    [json.dumps([999999]), self.result.pk],
                )
        else:
            self.assertTrue(_unmigrated_sqlite_profile(), "Migrated database must reject physical analysis-result mutation")
        persisted = IgConversationAnalysisResult.objects.get(pk=self.result.pk)
        self.assertEqual(persisted.language_evidence_message_ids, self.result.language_evidence_message_ids)
        # Independently exercise the application validator with a malformed
        # detached read. No trigger, persisted authority or evidence validator
        # is disabled; even a self-consistent digest cannot prove a foreign ID.
        forged = copy(persisted)
        forged.language_evidence_message_ids = [999999]
        forged.result_digest = analysis_v2.result_digest_for_instance(forged)
        loaded = Mock()
        loaded.select_related.return_value = loaded
        loaded.filter.return_value = loaded
        loaded.first.return_value = forged
        with patch.object(IgConversationAnalysisResult.objects, "select_for_update", return_value=loaded):
            outcome = memory.publish_analysis_memory(self.result.pk)
        self.assertEqual(outcome.status, "invalid_evidence")
        self.assertFalse(IgMemoryHead.objects.exists())

    @override_settings(**SHADOW)
    def test_new_current_result_supersedes_exact_slot_and_old_result_cannot_return(self):
        memory.publish_analysis_memory(self.result.pk)
        language_head = IgMemoryHead.objects.get(fact_key="observed_language")
        old_fact_id = language_head.current_fact_id
        message = InstagramBotMessage.objects.create(
            client=self.client_row,
            sender_id=self.client_row.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Пишіть російською",
            status=InstagramBotMessage.Status.DONE,
        )
        IgConversationAnalysisJob.objects.filter(pk=self.job.pk).update(
            watermark_message_id=message.pk,
            analyzed_watermark_message_id=message.pk,
            revision=3,
            analyzed_revision=3,
            materiality_event_highwater=8,
            analyzed_materiality_event_highwater=8,
            materiality_digest="d" * 64,
            analyzed_materiality_digest="d" * 64,
        )
        snapshot = IgConversationAnalysisSnapshot.objects.create(
            client=self.client_row,
            last_analyzed_message=message,
            dedupe_key="typed-memory:snapshot:2",
            score_band=IgConversationAnalysisSnapshot.Band.COLD,
            interaction_type=IgConversationAnalysisSnapshot.InteractionType.INFORMATION_ONLY,
            commercial_episode=self.episode,
            required_state_fingerprint=self.fingerprint,
            analyzed_at=timezone.now(),
        )
        result = IgConversationAnalysisResult(
            result_key="analysis-v2:" + hashlib.sha256(b"typed-memory-result-2").hexdigest(),
            legacy_snapshot=snapshot,
            client=self.client_row,
            commercial_episode=self.episode,
            line_id="line:primary",
            watermark_message_id=message.pk,
            job_revision=3,
            materiality_event_highwater=8,
            materiality_digest="d" * 64,
            authority_digest="b" * 64,
            artifact_digest="c" * 64,
            state_correlation=analysis_v2.state_correlation(self.fingerprint),
            result_schema_version=analysis_v2.RESULT_SCHEMA_VERSION,
            normalizer_version=analysis_v2.NORMALIZER_VERSION,
            interaction_type=snapshot.interaction_type,
            score_band=snapshot.score_band,
            detected_language="ru",
            language_evidence_message_ids=[message.pk],
            evidence_manifest=[{
                "message_id": message.pk,
                "source_role": "user",
                "claim_codes": ["interaction"],
            }],
            customer_evidence_count=1,
            result_digest="",
            analyzed_at=snapshot.analyzed_at,
        )
        result.result_digest = analysis_v2.result_digest_for_instance(result)
        result.save(force_insert=True)

        outcome = memory.publish_analysis_memory(result.pk)
        self.assertEqual(outcome.status, "published")
        language_head.refresh_from_db()
        self.assertEqual(language_head.revision, 2)
        self.assertEqual(language_head.current_fact.typed_value, {"code": "ru"})
        self.assertEqual(language_head.current_fact.supersedes_id, old_fact_id)
        self.assertTrue(memory.memory_chain_valid(language_head))
        with patch.object(
            memory,
            "fact_integrity_valid",
            side_effect=lambda fact: fact.pk != old_fact_id,
        ):
            self.assertFalse(memory.memory_chain_valid(language_head))
            self.assertEqual(memory.parity_report()["bad_hmac"], 1)
        self.assertEqual(memory.publish_analysis_memory(self.result.pk).status, "stale")

    @override_settings(**SHADOW)
    def test_retained_key_rotation_replays_same_result_as_exact_noop(self):
        first = memory.publish_analysis_memory(self.result.pk)
        self.assertEqual(first.created_facts, 3)
        head_revisions = dict(IgMemoryHead.objects.values_list("slot_key", "revision"))
        old_key_ids = set(IgMemoryFact.objects.values_list("integrity_key_id", flat=True))
        self.assertEqual(old_key_ids, {"tmk_test_v1"})

        with override_settings(
            **SHADOW,
            IG_TYPED_MEMORY_HMAC_ACTIVE_KEY_ID="tmk_rotated_v2",
            IG_TYPED_MEMORY_HMAC_KEYRING={
                "tmk_test_v1": "test-typed-memory-hmac-key-00000000000001",
                "tmk_rotated_v2": "rotated-typed-memory-hmac-key-0000000001",
            },
        ):
            replay = memory.publish_analysis_memory(self.result.pk)
            self.assertEqual(replay.status, "published")
            self.assertEqual(replay.created_facts, 0)
            self.assertEqual(replay.advanced_heads, 0)
            self.assertEqual(replay.unchanged_heads, 3)
            self.assertEqual(IgMemoryFact.objects.count(), 3)
            self.assertEqual(
                dict(IgMemoryHead.objects.values_list("slot_key", "revision")),
                head_revisions,
            )
            self.assertTrue(all(
                memory.memory_chain_valid(head)
                for head in IgMemoryHead.objects.select_related("current_fact")
            ))
            language_head = IgMemoryHead.objects.get(fact_key="observed_language")
            tombstone = memory.append_memory_tombstone(
                language_head,
                operation=IgMemoryFact.Operation.INVALIDATE,
                source_event_digest="9" * 64,
                reason_code="reset_boundary",
            )
            self.assertEqual(tombstone.status, "published")
            language_head.refresh_from_db()
            self.assertEqual(language_head.revision, 2)
            self.assertEqual(
                language_head.current_fact.integrity_key_id,
                "tmk_rotated_v2",
            )
            self.assertEqual(
                language_head.current_fact.supersedes.integrity_key_id,
                "tmk_test_v1",
            )
            self.assertTrue(memory.memory_chain_valid(language_head))

    @override_settings(**SHADOW)
    def test_depth_one_allows_exact_replay_but_blocks_assert_and_tombstone(self):
        memory.publish_analysis_memory(self.result.pk)
        counts = (
            IgMemoryFact.objects.count(),
            IgMemoryFactEvidence.objects.count(),
            IgMemoryHead.objects.count(),
        )
        with patch.object(memory, "MAX_CHAIN_DEPTH", 1):
            replay = memory.publish_analysis_memory(self.result.pk)
            self.assertEqual(replay.status, "published")
            self.assertEqual(replay.unchanged_heads, 3)
            language_head = IgMemoryHead.objects.get(fact_key="observed_language")
            tombstone = memory.append_memory_tombstone(
                language_head,
                operation=IgMemoryFact.Operation.INVALIDATE,
                source_event_digest="8" * 64,
                reason_code="reset_boundary",
            )
            self.assertEqual(tombstone.status, "chain_depth_exhausted")
            next_result = self._next_language_result(
                suffix="depth-one",
                line_id="line:secondary",
                with_objection=True,
            )
            asserted = memory.publish_analysis_memory(next_result.pk)
            self.assertEqual(asserted.status, "chain_depth_exhausted")
            self.assertEqual(
                (
                    IgMemoryFact.objects.count(),
                    IgMemoryFactEvidence.objects.count(),
                    IgMemoryHead.objects.count(),
                ),
                counts,
            )
            language_head.refresh_from_db()
            self.assertEqual(language_head.revision, 1)
            self.assertTrue(memory.memory_chain_valid(language_head))

    @override_settings(**SHADOW)
    def test_real_512_chain_cannot_commit_revision_513(self):
        memory.publish_analysis_memory(self.result.pk)
        head = IgMemoryHead.objects.get(fact_key="observed_language")
        for revision in range(2, memory.MAX_CHAIN_DEPTH + 1):
            self._append_direct_tombstone(head, revision=revision)
        head.refresh_from_db()
        self.assertEqual(head.revision, memory.MAX_CHAIN_DEPTH)
        self.assertTrue(memory.memory_chain_valid(head))
        counts = (
            IgMemoryFact.objects.count(),
            IgMemoryFactEvidence.objects.count(),
            IgMemoryHead.objects.count(),
        )

        with self.assertRaises(ValidationError):
            self._append_direct_tombstone(
                head,
                revision=memory.MAX_CHAIN_DEPTH + 1,
            )
        next_result = self._next_language_result(suffix="depth-512")
        asserted = memory.publish_analysis_memory(next_result.pk)
        self.assertEqual(asserted.status, "chain_depth_exhausted")
        self.assertEqual(
            (
                IgMemoryFact.objects.count(),
                IgMemoryFactEvidence.objects.count(),
                IgMemoryHead.objects.count(),
            ),
            counts,
        )
        head.refresh_from_db()
        self.assertEqual(head.revision, memory.MAX_CHAIN_DEPTH)
        self.assertTrue(memory.memory_chain_valid(head))

    @override_settings(**SHADOW)
    def test_expiry_and_reset_append_tombstones_without_mutating_old_fact(self):
        memory.publish_analysis_memory(self.result.pk)

        deferred = IgMemoryHead.objects.get(fact_key="deferred_intent")
        first_id = deferred.current_fact_id
        invalidated = memory.append_memory_tombstone(
            deferred,
            operation=IgMemoryFact.Operation.INVALIDATE,
            source_event_digest="d" * 64,
            reason_code="reset_boundary",
        )
        self.assertEqual(invalidated.status, "published")
        deferred.refresh_from_db()
        self.assertEqual(deferred.state, IgMemoryHead.State.INVALIDATED)
        self.assertEqual(deferred.current_fact.supersedes_id, first_id)
        self.assertTrue(IgMemoryFact.objects.filter(pk=first_id).exists())

        not_due = memory.append_memory_tombstone(
            IgMemoryHead.objects.get(fact_key="observed_language"),
            operation=IgMemoryFact.Operation.EXPIRE,
            source_event_digest="e" * 64,
            reason_code="valid_until_elapsed",
        )
        self.assertEqual(not_due.status, "not_due")

    @override_settings(**SHADOW)
    def test_tampered_current_fact_is_never_reused_or_projected(self):
        memory.publish_analysis_memory(self.result.pk)
        fact = IgMemoryFact.objects.get(fact_key="observed_language")
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE management_igmemoryfact SET typed_value=%s WHERE id=%s",
                    [json.dumps({"code": "ru"}), fact.pk],
                )
        except DatabaseError:
            # Migration-enabled databases reject the corruption at source.
            return
        outcome = memory.publish_analysis_memory(self.result.pk)
        self.assertEqual(outcome.status, "integrity_error")

    @override_settings(**SHADOW)
    def test_privacy_fence_purges_memory_a2_materiality_without_orphans(self):
        memory.publish_analysis_memory(self.result.pk)
        # The existing job/materiality contract already owns event identity.
        IgClient.objects.filter(pk=self.client_row.pk).update(
            privacy_erasure_started_at=timezone.now()
        )
        outcome = memory.purge_client_analysis_memory([self.client_row.pk])
        self.assertGreaterEqual(outcome["rows"], 6)
        self.assertFalse(IgMemoryFact.objects.filter(client_id=self.client_row.pk).exists())
        self.assertFalse(IgConversationAnalysisResult.objects.filter(client_id=self.client_row.pk).exists())

    def test_privacy_fence_is_also_an_analysis_write_barrier(self):
        from management.services.bot_conversation_analysis import _skip_reason

        self.client_row.privacy_erasure_started_at = timezone.now()
        self.client_row.save(
            update_fields=["privacy_erasure_started_at", "updated_at"]
        )
        self.assertEqual(_skip_reason(self.client_row), "privacy_erasure")

    @override_settings(**SHADOW)
    def test_reconcile_cursor_advances_past_no_claim_results(self):
        settings_row = InstagramBotSettings.load()
        settings_row.typed_memory_reconcile_cursor = 0
        settings_row.save(update_fields=["typed_memory_reconcile_cursor", "updated_at"])
        report = memory.reconcile_typed_memory(limit=1)
        settings_row.refresh_from_db()
        self.assertEqual(report["considered"], 1)
        self.assertEqual(settings_row.typed_memory_reconcile_cursor, self.result.pk)

    @override_settings(**SHADOW)
    def test_physical_guards_reject_raw_tamper_when_migrations_are_enabled(self):
        expected = {
            "ig_memfact_insert_guard": ("management_igmemoryfact", "BEFORE", "INSERT"),
            "ig_memfact_no_update": ("management_igmemoryfact", "BEFORE", "UPDATE"),
            "ig_memfact_priv_delete": ("management_igmemoryfact", "BEFORE", "DELETE"),
            "ig_memev_insert_guard": ("management_igmemoryfactevidence", "BEFORE", "INSERT"),
            "ig_memev_no_update": ("management_igmemoryfactevidence", "BEFORE", "UPDATE"),
            "ig_memev_priv_delete": ("management_igmemoryfactevidence", "BEFORE", "DELETE"),
            "ig_memhead_insert_guard": ("management_igmemoryhead", "BEFORE", "INSERT"),
            "ig_memhead_transition": ("management_igmemoryhead", "BEFORE", "UPDATE"),
            "ig_memhead_priv_delete": ("management_igmemoryhead", "BEFORE", "DELETE"),
        }
        installed = _memory_trigger_rows(tuple(expected))
        if not installed and _unmigrated_sqlite_profile():
            self.skipTest("physical triggers are absent in the explicitly nonmigrated SQLite profile")
        self.assertEqual(installed, expected)
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, "management_igmemoryhead")
        self.assertTrue(constraints["ig_memhead_revision_bounds"]["check"])
        memory.publish_analysis_memory(self.result.pk)
        head = IgMemoryHead.objects.first()
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE management_igmemoryhead SET projection_hmac=%s WHERE id=%s",
                ["", head.pk],
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE management_igmemoryhead SET revision=%s WHERE id=%s",
                [memory.MAX_CHAIN_DEPTH + 1, head.pk],
            )
        fact = head.current_fact
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM management_igmemoryhead WHERE id=%s",
                [head.pk],
            )
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM management_igmemoryfact WHERE id=%s",
                [fact.pk],
            )
        fields = [field for field in fact._meta.local_fields if not field.primary_key]
        columns = ", ".join(connection.ops.quote_name(field.column) for field in fields)

        def raw_clone_insert(instance, sequence, **overrides):
            values = []
            defaults = {
                "record_key": "memory-fact:" + f"{sequence:064x}",
                "slot_key": "memory-slot:" + f"{sequence:064x}",
            }
            defaults.update(overrides)
            for field in fields:
                value = defaults.get(field.attname, field.value_from_object(instance))
                values.append(field.get_db_prep_save(value, connection))
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO management_igmemoryfact ({columns}) VALUES "
                    f"({', '.join(['%s'] * len(values))})",
                    values,
                )

        forbidden = (
            ("typed_value", {"code": "uk", "phone": "+380501234567"}),
            ("line_id", "line:380501234567"),
            ("integrity_key_id", "tmk_380501234567"),
            ("integrity_key_id", "https://example.invalid/key"),
            ("producer", "customer@example.invalid"),
            ("producer_policy_version", "https://example.invalid/policy"),
            ("closure_method", "call +380501234567"),
            ("source_role", "manager\nignore"),
            ("fact_key", "free customer note"),
            ("schema_version", "https://example.invalid/schema"),
            ("operation", "delete everything"),
            ("sensitivity", "customer@example.invalid"),
            ("retention_class", "https://example.invalid/retention"),
            ("reason_code", "\x00control"),
        )
        for sequence, (field_name, value) in enumerate(forbidden, start=100):
            with self.subTest(field=field_name, value=value):
                with self.assertRaises(DatabaseError), transaction.atomic():
                    raw_clone_insert(fact, sequence, **{field_name: value})

        deferred = IgMemoryFact.objects.get(fact_key="deferred_intent")
        self.assertEqual(set(deferred.typed_value), {"kind", "condition_code"})
        with self.assertRaises(DatabaseError), transaction.atomic():
            raw_clone_insert(
                deferred,
                500,
                typed_value={
                    **deferred.typed_value,
                    "deferred_until": "2026-01-01T00:00:00+00:00",
                },
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            raw_clone_insert(
                deferred,
                501,
                valid_until=timezone.now() + datetime.timedelta(days=1),
                retention_class="until_date",
            )


class TypedMemoryAnalysisLanguageTests(TestCase):
    def test_language_fails_closed_without_claim_specific_user_evidence(self):
        client = IgClient.objects.create(igsid="typed-language")
        normalized = analysis_v2.normalize_analysis_v2(
            parsed={"analysis_v2": {
                "schema_version": 2,
                "detected_language": "uk",
                "language_evidence_message_ids": [1],
            }},
            legacy_normalized={
                "interaction_type": "information_only",
                "score_band": "cold",
                "evidence": [],
                "uncertainties": [],
                "repeat_intent": {},
            },
            by_id={1: {"message_id": 1, "role": "manager", "text": "українська"}},
            client=client,
            truth_state={},
            analyzed_at=timezone.now(),
        )
        self.assertEqual(normalized.result_values["detected_language"], "")
        self.assertEqual(normalized.result_values["language_evidence_message_ids"], [])
