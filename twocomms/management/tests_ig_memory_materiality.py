"""Pure classification and real signed publisher re-observation contracts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import fields
from datetime import datetime, timedelta, timezone as utc_timezone
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_typed_memory as fixtures
from management.models import (
    IgClient, IgConversationAnalysisResult, IgMemoryFact, IgMemoryFactEvidence,
    IgMemoryHead, InstagramBotMessage,
)
from management.services import ig_analysis_v2 as analysis_v2
from management.services import ig_memory_materiality as materiality
from management.services import ig_typed_memory as memory
from management.tests_support import AnalysisPrivacyCleanupMixin


class MemoryMaterialityClassificationTests(SimpleTestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 7, tzinfo=utc_timezone.utc)
        self.old = {
            "id": 11, "client_id": 7, "scope": "client",
            "commercial_episode_id": None, "line_id": "", "order_id": None,
            "post_sale_case_id": None, "fact_key": "observed_language",
            "schema_version": "typed-memory.v1", "operation": "assert",
            "producer_policy_version": "typed-memory-projector.v1",
            "producer": "analysis_v2", "source_role": "user",
            "closure_method": "analysis_assertion", "typed_value": {"code": "uk"},
            "confidence": None, "observed_at": self.now - timedelta(days=1),
            "valid_until": None, "source_watermark_message_id": 20,
            "sensitivity": "low", "retention_class": "client",
        }
        self.candidate = dict(self.old, observed_at=self.now, source_watermark_message_id=30)
        self.head = {field: self.old[field] for field in materiality.SCOPE_FIELDS}
        self.head.update(current_fact_id=11, revision=512, state="active")
        self.boundary = {
            "client_id": 7, "reset_floor": 1, "watermark_message_id": 30,
            "episode_id": 9, "line_id": "line:primary", "hidden": False,
            "erasure_started": False, "source_evidence_valid": True,
        }

    def decision(self):
        return materiality.classify_memory_candidate(
            self.candidate, head=self.head, current_fact=self.old,
            boundary=self.boundary, now=self.now,
        ).action

    def test_new_observation_does_not_change_semantics_or_depth(self):
        original = deepcopy((self.old, self.candidate, self.head, self.boundary))
        self.assertEqual(self.decision(), "reobserve")
        self.assertEqual((self.old, self.candidate, self.head, self.boundary), original)

    def test_genuine_value_change_requires_strict_publication(self):
        self.candidate["typed_value"] = {"code": "ru"}
        self.assertEqual(self.decision(), "append")

    def test_malformed_values_never_raise_or_classify_as_unchanged(self):
        for field, value in (
            ("fact_key", []), ("producer_policy_version", {}),
            ("typed_value", {"code": []}), ("client_id", True),
            ("observed_at", "today"), ("confidence", Decimal("NaN")),
            ("sensitivity", "personal_preference"),
        ):
            with self.subTest(field=field):
                candidate = dict(self.candidate, **{field: value})
                self.assertEqual(materiality.classify_memory_candidate(
                    candidate, head=self.head, current_fact=self.old,
                    boundary=self.boundary, now=self.now,
                ).action, "append")

    def test_reset_future_source_inactive_or_unverified_requires_strict_path(self):
        for row, field, value in (
            (self.boundary, "reset_floor", 21),
            (self.boundary, "watermark_message_id", 19),
            (self.boundary, "source_evidence_valid", False),
            (self.boundary, "hidden", True),
            (self.boundary, "erasure_started", True),
            (self.head, "state", "expired"),
        ):
            previous = row[field]
            row[field] = value
            self.assertEqual(self.decision(), "append")
            row[field] = previous

    def _date(self):
        for row in (self.old, self.candidate, self.head):
            row.update(scope="episode", commercial_episode_id=9,
                fact_key="deferred_intent")
        for row in (self.old, self.candidate):
            row.update(typed_value={"kind": "date", "condition_code": "customer_date"},
                sensitivity="personal_preference", retention_class="until_date",
                valid_until=self.now + timedelta(hours=1))

    def test_date_same_until_reobserves_but_changed_until_is_material(self):
        self._date()
        self.assertEqual(self.decision(), "reobserve")
        self.candidate["valid_until"] += timedelta(hours=1)
        self.assertEqual(self.decision(), "append")

    def test_exact_expiry_or_invalid_expiry_cannot_reobserve(self):
        self._date()
        self.now = self.old["valid_until"]
        self.assertEqual(self.decision(), "append")
        self.candidate["valid_until"] = "today"
        self.assertEqual(self.decision(), "append")

    def test_episode_line_and_fact_source_identity_cannot_exchange_authority(self):
        self._date()
        self.boundary["episode_id"] = 10
        self.assertEqual(self.decision(), "append")
        self.boundary["episode_id"] = 9
        self.head["current_fact_id"] = 12
        self.assertEqual(self.decision(), "append")

    def test_objection_enum_matches_model_allowlist(self):
        from management.models import IgObjection
        self.assertEqual(materiality.OBJECTION_TYPES, set(IgObjection.Type.values))

    def test_public_publish_outcome_shape_stays_compatible(self):
        self.assertEqual(tuple(field.name for field in fields(memory.PublishOutcome)), (
            "status", "result_id", "created_facts", "advanced_heads", "unchanged_heads",
        ))

    @override_settings(
        IG_TYPED_MEMORY_HMAC_ACTIVE_KEY_ID="tmk_golden",
        IG_TYPED_MEMORY_HMAC_KEYRING={"tmk_golden": "materiality-golden-test-secret-00001"},
    )
    def test_v1_fact_and_head_mac_bytes_remain_compatible(self):
        values = {
            "record_key": "memory-fact:" + "a" * 64, "client_id": 7,
            "scope": "client", "typed_value": {"code": "uk"},
            "observed_at": datetime(2026, 10, 7, tzinfo=utc_timezone.utc),
        }
        for domain, payload, expected in (
            ("management.typed-memory.fact.v1", memory._fact_hmac_payload(values),
             "33a16123f30c6e02ccfe1a5e86d311e6de282600932d8df5f025863fbbb678c7"),
            ("management.typed-memory.head.v1", memory._head_hmac_payload(values),
             "b29fcee65938352bbfb865f995051e702e13aa43f2a5f05b331e49c4254177fe"),
        ):
            self.assertEqual(memory._mac(domain, payload)[1], expected)


@override_settings(**fixtures.SHADOW)
class MemoryMaterialityPublisherTests(TestCase):
    _next_language_result = fixtures.TypedMemoryRuntimeTests._next_language_result

    def setUp(self):
        fixtures.TypedMemoryRuntimeTests.setUp(self)
        self.assertEqual(memory.publish_analysis_memory(self.result.pk).status, "published")

    def memory_snapshot(self):
        return tuple(list(model.objects.order_by("pk").values()) for model in (
            IgMemoryFact, IgMemoryFactEvidence, IgMemoryHead,
        ))

    def newer(self, *, suffix="same", language="uk", confidence=Decimal("0.9000"),
              objection="price", deferred_until=None, deferred=False):
        original = IgConversationAnalysisResult.save
        def save(result, *args, **kwargs):
            result.active_objection_type = objection
            result.active_objection_confidence = confidence
            if deferred:
                result.deferred_kind = "date" if deferred_until is not None else "payday"
                result.deferred_condition_code = "customer_date" if deferred_until is not None else "payday"
                result.deferred_until = deferred_until
                for row in result.evidence_manifest:
                    row["claim_codes"] = sorted(set(row["claim_codes"]) | {"deferred_intent"})
            result.result_digest = analysis_v2.result_digest_for_instance(result)
            return original(result, *args, **kwargs)
        with patch.object(IgConversationAnalysisResult, "save", save):
            return self._next_language_result(language=language, suffix=suffix, with_objection=True)

    def test_all_same_values_retain_every_authority_field_and_new_result(self):
        before = self.memory_snapshot()
        newer = self.newer(deferred=True)
        outcome = memory.publish_analysis_memory(newer.pk)
        self.assertEqual((outcome.status, outcome.created_facts, outcome.advanced_heads,
                          outcome.unchanged_heads), ("published", 0, 0, 3))
        self.assertEqual(self.memory_snapshot(), before)
        self.assertNotEqual(newer.language_evidence_message_ids, self.result.language_evidence_message_ids)
        self.assertTrue(IgConversationAnalysisResult.objects.filter(pk=newer.pk).exists())
        self.assertEqual(memory.publish_analysis_memory(newer.pk).unchanged_heads, 3)
        self.assertEqual(self.memory_snapshot(), before)

    def test_changed_language_appends_only_changed_slot(self):
        old = IgMemoryHead.objects.get(fact_key="observed_language").current_fact_id
        outcome = memory.publish_analysis_memory(self.newer(language="ru").pk)
        self.assertEqual((outcome.created_facts, outcome.advanced_heads, outcome.unchanged_heads), (1, 1, 1))
        head = IgMemoryHead.objects.select_related("current_fact").get(fact_key="observed_language")
        self.assertEqual((head.revision, head.current_fact.supersedes_id), (2, old))
        self.assertTrue(memory.memory_chain_valid(head))

    def test_same_value_at_append_depth_limit_survives_but_change_stays_blocked(self):
        before = self.memory_snapshot()
        newer = self.newer()
        with patch.object(memory, "MAX_CHAIN_DEPTH", 1):
            outcome = memory.publish_analysis_memory(newer.pk)
        self.assertEqual((outcome.created_facts, outcome.unchanged_heads), (0, 2))
        self.assertEqual(self.memory_snapshot(), before)
        changed = self.newer(language="ru", suffix="changed")
        with patch.object(memory, "MAX_CHAIN_DEPTH", 1):
            outcome = memory.publish_analysis_memory(changed.pk)
        self.assertEqual(outcome.status, "chain_depth_exhausted")
        self.assertEqual(self.memory_snapshot(), before)

    def test_reset_old_source_requires_new_assertion(self):
        newer = self.newer()
        with patch("management.services.ig_funnel_reset.current_message_floor",
                   return_value=newer.watermark_message_id):
            outcome = memory.publish_analysis_memory(newer.pk)
        self.assertEqual((outcome.created_facts, outcome.advanced_heads), (2, 2))
        self.assertEqual(IgMemoryHead.objects.get(fact_key="observed_language").current_fact.source_result_id, newer.pk)

    def test_invalidated_head_requires_new_assertion_from_fresh_source(self):
        head = IgMemoryHead.objects.get(fact_key="observed_language")
        self.assertEqual(memory.append_memory_tombstone(
            head, operation=IgMemoryFact.Operation.INVALIDATE,
            source_event_digest="7" * 64, reason_code="explicit_retraction",
        ).status, "published")
        newer = self.newer()
        outcome = memory.publish_analysis_memory(newer.pk)
        self.assertEqual((outcome.created_facts, outcome.advanced_heads, outcome.unchanged_heads), (1, 1, 1))
        head.refresh_from_db()
        self.assertEqual(head.current_fact.source_result_id, newer.pk)

    def test_retained_evidence_no_longer_owned_requires_new_assertion(self):
        other = IgClient.objects.create(igsid="materiality-other")
        InstagramBotMessage.objects.filter(pk=self.message.pk).update(client_id=other.pk)
        newer = self.newer()
        outcome = memory.publish_analysis_memory(newer.pk)
        self.assertEqual((outcome.created_facts, outcome.advanced_heads), (2, 2))

    def test_new_foreign_evidence_rejects_all_candidates(self):
        before = self.memory_snapshot()
        newer = self.newer()
        other = IgClient.objects.create(igsid="materiality-foreign")
        InstagramBotMessage.objects.filter(pk=newer.watermark_message_id).update(client_id=other.pk)
        self.assertEqual(memory.publish_analysis_memory(newer.pk).status, "invalid_evidence")
        self.assertEqual(self.memory_snapshot(), before)

    def test_integrity_failure_cannot_be_hidden_by_equal_value(self):
        before = self.memory_snapshot()
        newer = self.newer()
        with patch.object(memory, "fact_integrity_valid", return_value=False):
            self.assertEqual(memory.publish_analysis_memory(newer.pk).status, "integrity_error")
        self.assertEqual(self.memory_snapshot(), before)

    def test_stale_result_keeps_existing_rejection(self):
        before = self.memory_snapshot()
        self.newer()
        self.assertEqual(memory.publish_analysis_memory(self.result.pk).status, "stale")
        self.assertEqual(self.memory_snapshot(), before)

    def test_key_rotation_does_not_resign_or_refresh_unchanged_fact(self):
        before = self.memory_snapshot()
        newer = self.newer()
        active, ring = memory._keyring()
        retained = {key: value.decode() for key, value in ring.items()}
        retained["tmk_materiality_new"] = "materiality-rotated-test-secret-00001"
        with self.settings(IG_TYPED_MEMORY_HMAC_KEYRING=retained,
                           IG_TYPED_MEMORY_HMAC_ACTIVE_KEY_ID="tmk_materiality_new"):
            self.assertEqual(memory.publish_analysis_memory(newer.pk).unchanged_heads, 2)
        self.assertEqual(self.memory_snapshot(), before)
        with self.settings(IG_TYPED_MEMORY_HMAC_KEYRING={
            "tmk_materiality_new": retained["tmk_materiality_new"]},
            IG_TYPED_MEMORY_HMAC_ACTIVE_KEY_ID="tmk_materiality_new"):
            self.assertEqual(memory.publish_analysis_memory(newer.pk).status, "integrity_error")
        self.assertEqual(self.memory_snapshot(), before)

    def test_later_write_failure_rolls_back_prior_changed_slot(self):
        before = self.memory_snapshot()
        newer = self.newer(language="ru", objection="thinking")
        old_language_fact_id = IgMemoryHead.objects.get(
            fact_key="observed_language",
        ).current_fact_id
        original = memory._mac
        def sign(domain, payload, **kwargs):
            if (
                domain == "management.typed-memory.head.v1"
                and payload["fact_key"] == "observed_language"
                and payload["current_fact_id"] != old_language_fact_id
            ):
                raise ValueError("synthetic later-slot signing failure")
            return original(domain, payload, **kwargs)
        with patch.object(memory, "_mac", side_effect=sign):
            self.assertEqual(memory.publish_analysis_memory(newer.pk).status, "conflict")
        self.assertEqual(self.memory_snapshot(), before)

    def test_date_reobservation_preserves_ttl_and_changed_date_appends(self):
        expiry = timezone.now() + timedelta(days=1)
        first = self.newer(deferred=True, deferred_until=expiry, suffix="date-initial")
        self.assertEqual(memory.publish_analysis_memory(first.pk).status, "published")
        before = self.memory_snapshot()
        same = self.newer(deferred=True, deferred_until=expiry, suffix="date-same")
        outcome = memory.publish_analysis_memory(same.pk)
        self.assertEqual((outcome.created_facts, outcome.unchanged_heads), (0, 3))
        self.assertEqual(self.memory_snapshot(), before)
        changed = self.newer(deferred=True, deferred_until=expiry + timedelta(days=1), suffix="date-changed")
        self.assertEqual(memory.publish_analysis_memory(changed.pk).created_facts, 1)

    def test_exact_expiry_requires_existing_append_path_even_without_sweep(self):
        expiry = timezone.now() + timedelta(days=1)
        first = self.newer(deferred=True, deferred_until=expiry, suffix="expired-initial")
        memory.publish_analysis_memory(first.pk)
        old = IgMemoryHead.objects.get(fact_key="deferred_intent").current_fact_id
        same = self.newer(deferred=True, deferred_until=expiry, suffix="expired-same")
        with patch.object(memory.timezone, "now", return_value=expiry):
            outcome = memory.publish_analysis_memory(same.pk)
        self.assertEqual((outcome.created_facts, outcome.unchanged_heads), (1, 2))
        head = IgMemoryHead.objects.get(fact_key="deferred_intent")
        self.assertNotEqual(head.current_fact_id, old)
        self.assertEqual(head.current_fact.valid_until, expiry)
        read = memory.read_typed_memory(self.client_row, episode_id=self.episode.pk,
                                        line_id="line:primary", now=expiry)
        self.assertFalse(any(fact["fact_key"] == "deferred_intent" for fact in read["facts"]))


@skipUnless(connection.vendor == "mysql", "Disposable MariaDB-only materiality race")
@override_settings(**fixtures.SHADOW)
class MemoryMaterialityNativeTests(AnalysisPrivacyCleanupMixin, TransactionTestCase):
    _next_language_result = fixtures.TypedMemoryRuntimeTests._next_language_result
    newer = MemoryMaterialityPublisherTests.newer

    def setUp(self):
        self.assertRegex(str(connection.settings_dict.get("NAME") or ""),
                         r"^test_twocomms_[A-Za-z0-9_]+$")
        fixtures.TypedMemoryRuntimeTests.setUp(self)
        self.assertEqual(memory.publish_analysis_memory(self.result.pk).status, "published")

    def test_two_fresh_result_publishers_reobserve_without_head_write(self):
        newer = self.newer(deferred=True)
        before = MemoryMaterialityPublisherTests.memory_snapshot(self)
        barrier = Barrier(2)
        def worker(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return memory.publish_analysis_memory(newer.pk)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(worker, (1, 2)))
        self.assertEqual([(row.status, row.created_facts, row.unchanged_heads)
                          for row in outcomes], [("published", 0, 3)] * 2)
        self.assertEqual(MemoryMaterialityPublisherTests.memory_snapshot(self), before)
