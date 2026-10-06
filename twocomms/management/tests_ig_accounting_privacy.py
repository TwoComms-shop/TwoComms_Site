"""Fenced JSON erasure preserves existing accounting and its economic truth."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_gemini_accounting_shadow as fixtures
from management import tests_ig_revision_live as live_fixture
from management.models import GeminiQuotaState, GeminiRequest, GeminiRequestAttempt, IgClient, InstagramBotMessage
from management.services import gemini_accounting_runtime as runtime
from management.services.ig_accounting_privacy import AccountingPrivacyError, scrub_client_accounting_context
from management.services.ig_request_manifest import capture_dispatch_context, capture_request_context
from management.services.ig_turn_lineage import turn_lineage
from management.tests_ig_request_context_manifest import policy


class _PrivacyFixture:
    def setUp(self):
        fixtures.seed_shadow_profiles()
        self.case = live_fixture.RevisionLiveTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case._prepare()
        self.body = b'{"contents": []}'
        self.context = capture_request_context(payload={"contents": []}, metadata={
            "revision_id": self.case.revision.pk, "client_id": self.case.customer.pk,
            "source_message_ids": [self.case.source.pk], "bundle_digest": self.case.revision.snapshot_digest,
            "builder_version": "ig-turn-intelligence.v1", "effective_mode": "unified",
            "selected_block_ids": ["history"],
        })
        with runtime.revision_request_execution(self.case.revision.pk, self.case.token,
            settings_id=self.case.settings.pk, settings_permission_epoch=self.case.settings.reply_permission_epoch), turn_lineage(
                lane="live", client_id=self.case.customer.pk, source_message_id=self.case.source.pk,
                logical_turn_id=f"ig-revision:{self.case.revision.pk}"):
            self.observer = runtime.begin_request(request_id=None, role="chat", reasoning_task="customer_chat",
                candidate_plan=fixtures._raw_plan("gemini-3.5-flash-lite"), deadline_seconds=30,
                request_policy_manifest={**policy(), "request_context": self.context})
        self.assertTrue(self.observer.enabled, self.observer.block_reason)
        self.boundary = self.observer.attempt(key_name="GEMINI_API", model="gemini-3.5-flash-lite")
        self.boundary.prepare_dispatch_manifest(self.body)
        # This is a local admission fixture. No HTTP method is invoked.
        with patch("management.services.call_ai_analysis.requests.post") as provider:
            self.assertTrue(self.boundary.before_provider(serialized_bytes=len(self.body)))
        provider.assert_not_called()
        self.graph = GeminiRequest.objects.get(pk=self.observer.graph_id)
        self.attempt = GeminiRequestAttempt.objects.get(pk=self.boundary.attempt_id)

    def fence(self, client=None):
        client = client or self.case.customer
        IgClient.objects.filter(pk=client.pk).update(privacy_erasure_started_at=timezone.now())

    def scrub(self, **kwargs):
        return scrub_client_accounting_context([self.case.customer.pk], **kwargs)


@override_settings(**fixtures.SHADOW, IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class AccountingPrivacyTests(_PrivacyFixture, TransactionTestCase):
    def test_fence_required_and_foreign_frozen_ids_cannot_expand_scope(self):
        with self.assertRaises(AccountingPrivacyError) as error:
            self.scrub()
        self.assertEqual(error.exception.code, "privacy_fence_required")
        self.fence()
        foreign = IgClient.objects.create(igsid="synthetic-foreign-erasure-owner")
        source = InstagramBotMessage.objects.create(client=foreign, sender_id=foreign.igsid,
            mid="synthetic-foreign-source", role="user", text="synthetic private foreign text")
        with self.assertRaises(AccountingPrivacyError) as error:
            self.scrub(frozen_message_ids=[source.pk])
        self.assertEqual(error.exception.code, "privacy_source_scope_mismatch")
        self.graph.refresh_from_db()
        self.assertIn("request_context", self.graph.policy_manifest)

    def test_idempotent_scrub_changes_only_extensions_and_preserves_economics(self):
        self.fence()
        graph_before = GeminiRequest.objects.values().get(pk=self.graph.pk)
        attempt_before = GeminiRequestAttempt.objects.values().get(pk=self.attempt.pk)
        quotas_before = list(GeminiQuotaState.objects.order_by("pk").values())
        counts = self.scrub(frozen_message_ids=[self.case.source.pk])
        self.assertEqual(counts, {"clients": 1, "request_contexts": 1, "dispatch_manifests": 1})
        graph_after = GeminiRequest.objects.values().get(pk=self.graph.pk)
        attempt_after = GeminiRequestAttempt.objects.values().get(pk=self.attempt.pk)
        expected_graph = {**graph_before, "policy_manifest": policy()}
        expected_attempt = {**attempt_before, "dispatch_manifest": {}}
        self.assertEqual(graph_after, expected_graph)
        self.assertEqual(attempt_after, expected_attempt)
        self.assertEqual(quotas_before, list(GeminiQuotaState.objects.order_by("pk").values()))
        self.assertEqual(self.scrub(), {"clients": 1, "request_contexts": 0, "dispatch_manifests": 0})
        self.assertTrue(GeminiRequest.objects.filter(pk=self.graph.pk).exists())
        self.assertTrue(GeminiRequestAttempt.objects.filter(pk=self.attempt.pk).exists())

    def test_legacy_and_foreign_graphs_are_preserved(self):
        legacy = GeminiRequest.objects.create(request_id="synthetic-legacy-ledger",
            client_id=self.case.customer.pk, policy_manifest=policy())
        foreign = IgClient.objects.create(igsid="synthetic-foreign-context-owner")
        foreign_context = capture_request_context(payload={"contents": []}, metadata={"client_id": foreign.pk})
        other = GeminiRequest.objects.create(request_id="synthetic-foreign-ledger", client_id=foreign.pk,
            policy_manifest={**policy(), "request_context": foreign_context})
        other_attempt = GeminiRequestAttempt.objects.create(request_graph=other, request_id=other.request_id,
            client_id=foreign.pk, role="chat", key_name="synthetic", model="gemini-3.5-flash-lite",
            attempt_index=1, candidate_index=1, outcome="failed", fsm_state="failed",
            dispatch_manifest=capture_dispatch_context(payload=self.body, context=foreign_context,
                attempt_index=1, model="gemini-3.5-flash-lite"))
        other_before = deepcopy(other.policy_manifest)
        dispatch_before = deepcopy(other_attempt.dispatch_manifest)
        self.fence()
        self.scrub()
        legacy.refresh_from_db()
        other.refresh_from_db()
        other_attempt.refresh_from_db()
        self.assertEqual(legacy.policy_manifest, policy())
        self.assertEqual(other.policy_manifest, other_before)
        self.assertEqual(other_attempt.dispatch_manifest, dispatch_before)

    def test_cutoff_limits_graphs_but_includes_later_attempts_of_frozen_graph(self):
        cutoff = self.graph.created_at + timedelta(microseconds=1)
        self.assertGreater(self.attempt.created_at, cutoff)
        future = GeminiRequest.objects.create(request_id="synthetic-later-ledger", client_id=self.case.customer.pk,
            policy_manifest={**policy(), "request_context": capture_request_context(
                payload={"contents": []}, metadata={"client_id": self.case.customer.pk})})
        self.fence()
        self.assertEqual(self.scrub(cutoff_at=cutoff), {"clients": 1, "request_contexts": 1, "dispatch_manifests": 1})
        future.refresh_from_db()
        self.assertIn("request_context", future.policy_manifest)

    def test_outer_rollback_restores_extensions_without_economic_change(self):
        self.fence()
        before_graph = deepcopy(self.graph.policy_manifest)
        before_attempt = deepcopy(self.attempt.dispatch_manifest)
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.scrub()
                raise RuntimeError("synthetic transaction rollback")
        self.graph.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual(self.graph.policy_manifest, before_graph)
        self.assertEqual(self.attempt.dispatch_manifest, before_attempt)

    def test_direct_owner_erasure_scrubs_extensions_created_after_claim_cutoff(self):
        from management.bot_views import _delete_direct_bot_records
        cutoff = self.graph.created_at - timedelta(seconds=1)
        owner_id = self.case.customer.pk
        with patch("management.services.call_ai_analysis.requests.post") as provider:
            result = _delete_direct_bot_records(
                exact_client_ids=[owner_id], frozen_message_ids=[self.case.source.pk],
                frozen_sender_ids=[self.case.customer.igsid], frozen_cutoff_at=cutoff,
            )
        provider.assert_not_called()
        self.assertGreater(result["clients"], 0)
        self.assertFalse(IgClient.objects.filter(pk=owner_id).exists())
        self.graph.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertNotIn("request_context", self.graph.policy_manifest)
        self.assertEqual(self.attempt.dispatch_manifest, {})

    def test_cached_observer_cannot_restore_context_or_start_new_dispatch(self):
        original = deepcopy(self.attempt.dispatch_manifest)
        self.fence()
        self.scrub()
        new_boundary = self.observer.attempt(key_name="GEMINI_API", model="gemini-3.5-flash-lite")
        new_boundary.prepare_dispatch_manifest(self.body)
        with patch("management.services.call_ai_analysis.requests.post") as provider:
            self.assertFalse(new_boundary.before_provider(serialized_bytes=len(self.body)))
            # Already-needed late accounting settles through existing records;
            # customer context cannot be repopulated by that settlement.
            self.boundary.succeeded({"promptTokenCount": 17, "totalTokenCount": 21})
        provider.assert_not_called()
        self.attempt.refresh_from_db()
        self.graph.refresh_from_db()
        self.assertEqual(self.attempt.dispatch_manifest, {})
        self.assertNotIn("request_context", self.graph.policy_manifest)
        self.assertEqual(self.attempt.total_tokens, 21)
        self.assertEqual(GeminiRequestAttempt.objects.filter(request_graph=self.graph).count(), 1)
        self.attempt.dispatch_manifest = original
        with self.assertRaises(ValidationError):
            self.attempt.save(update_fields=["dispatch_manifest"])

    def test_strict_scope_and_cutoff_have_finite_errors(self):
        for owners, kwargs in (([True], {}), (["1"], {}), ([], {"frozen_message_ids": [1]}),
                              ([self.case.customer.pk], {"cutoff_at": timezone.now().replace(tzinfo=None)})):
            with self.subTest(owners=owners, kwargs=kwargs), self.assertRaises(AccountingPrivacyError) as error:
                scrub_client_accounting_context(owners, **kwargs)
            self.assertTrue(error.exception.code.startswith("privacy_"))
        self.assertEqual(scrub_client_accounting_context([]), {"clients": 0, "request_contexts": 0, "dispatch_manifests": 0})


@skipUnless(connection.vendor == "mysql", "MariaDB/InnoDB contention contract")
@override_settings(**fixtures.SHADOW, IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativeAccountingPrivacyContentionTests(_PrivacyFixture, TransactionTestCase):
    def test_two_scrubbers_serialize_and_preserve_economic_rows(self):
        self.fence()
        owner_id = self.case.customer.pk
        gate = Barrier(2)

        def worker():
            close_old_connections()
            try:
                gate.wait(timeout=10)
                return scrub_client_accounting_context([owner_id])
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(sum(row["request_contexts"] for row in results), 1)
        self.assertEqual(sum(row["dispatch_manifests"] for row in results), 1)
        self.assertEqual(GeminiRequest.objects.filter(pk=self.graph.pk).count(), 1)
        self.assertEqual(GeminiRequestAttempt.objects.filter(pk=self.attempt.pk).count(), 1)
