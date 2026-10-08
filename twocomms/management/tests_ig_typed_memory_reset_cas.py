"""Reset selection must not revoke an assertion accepted after that selection."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import (
    IgClient, IgFunnelResetAudit, IgMemoryFact, IgMemoryFactEvidence, IgMemoryHead,
)
from management.services import ig_typed_memory as memory
from management import tests_ig_typed_memory as fixtures
from management.tests_support import AnalysisPrivacyCleanupMixin


class ResetCasFixture:
    _next_language_result = fixtures.TypedMemoryRuntimeTests._next_language_result

    def setUp(self):
        fixtures.TypedMemoryRuntimeTests.setUp(self)
        self.assertEqual(memory.publish_analysis_memory(self.result.pk).status, "published")

    def memory_rows(self):
        return (
            list(IgMemoryFact.objects.order_by("id").values()),
            list(IgMemoryFactEvidence.objects.order_by("id").values()),
            list(IgMemoryHead.objects.order_by("id").values()),
        )

    def reset(self, boundary=None):
        return IgFunnelResetAudit.objects.create(
            client=self.client_row,
            reset_after_message_id=self.message.pk if boundary is None else boundary,
            reason="typed-memory reset CAS regression",
        )

    def selected_head(self):
        return IgMemoryHead.objects.select_related("current_fact").get(
            fact_key="objection_observed",
        )

    def reset_kwargs(self, head, audit):
        return {
            "operation": IgMemoryFact.Operation.INVALIDATE,
            "source_event_digest": memory._sha({
                "head_id": head.pk, "reset_id": audit.pk,
                "reset_after_message_id": audit.reset_after_message_id,
            }),
            "reason_code": "reset_boundary",
            "expected_current_fact_id": head.current_fact_id,
            "expected_revision": head.revision,
            "expected_client_id": self.client_row.pk,
            "reset_after_message_id": audit.reset_after_message_id,
            "expected_reset_id": audit.pk,
        }

    def assert_no_memory_write(self, before, captured):
        self.assertEqual(self.memory_rows(), before)
        self.assertFalse(any(
            query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            for query in captured.captured_queries
        ))

    def assert_new_assertion_survives(self, selected, newer, report):
        selected.refresh_from_db()
        self.assertEqual(selected.state, IgMemoryHead.State.ACTIVE)
        self.assertEqual(selected.revision, 2)
        self.assertEqual(selected.current_fact.source_result_id, newer.pk)
        self.assertTrue(memory.memory_chain_valid(selected))
        # The unchanged deferred slot still requires invalidation. Protecting
        # the corrected slot cannot silently lose the other selected slot.
        deferred = IgMemoryHead.objects.get(fact_key="deferred_intent")
        self.assertEqual((deferred.state, deferred.revision), ("invalidated", 2))
        self.assertTrue(memory.memory_chain_valid(deferred))
        self.assertEqual((report["considered"], report["invalidated"], report["stale"]), (2, 1, 1))


@override_settings(**fixtures.SHADOW)
class TypedMemoryResetCasTests(ResetCasFixture, TestCase):
    def test_expected_fact_and_revision_are_both_required_for_exact_cas(self):
        head = self.selected_head()
        before = self.memory_rows()
        for fact_id, revision in (
            (head.current_fact_id + 100000, head.revision),
            (head.current_fact_id, head.revision + 1),
        ):
            with self.subTest(fact_id=fact_id, revision=revision):
                with CaptureQueriesContext(connection) as captured:
                    outcome = memory.append_memory_tombstone(
                        head.pk, operation="invalidate", source_event_digest="a" * 64,
                        reason_code="explicit_retraction",
                        expected_current_fact_id=fact_id, expected_revision=revision,
                    )
                self.assertEqual(outcome.status, "stale")
                self.assert_no_memory_write(before, captured)

    def test_exact_cas_appends_once_and_duplicate_event_does_not_advance(self):
        head = self.selected_head()
        kwargs = {
            "operation": "invalidate", "source_event_digest": "b" * 64,
            "reason_code": "explicit_retraction",
            "expected_current_fact_id": head.current_fact_id,
            "expected_revision": head.revision,
        }
        first = memory.append_memory_tombstone(head.pk, **kwargs)
        self.assertEqual((first.status, first.created_facts, first.advanced_heads), ("published", 1, 1))
        head.refresh_from_db()
        self.assertEqual((head.state, head.revision), ("invalidated", 2))
        self.assertTrue(memory.memory_chain_valid(head))
        before = self.memory_rows()
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(memory.append_memory_tombstone(head.pk, **kwargs).status, "stale")
            direct = {key: value for key, value in kwargs.items() if not key.startswith("expected_")}
            self.assertEqual(memory.append_memory_tombstone(head.pk, **direct).status, "not_active")
        self.assert_no_memory_write(before, captured)

    def test_exact_current_reset_invalidates_only_once(self):
        head = self.selected_head()
        audit = self.reset()
        old_fact_id = head.current_fact_id
        kwargs = self.reset_kwargs(head, audit)
        outcome = memory.append_memory_tombstone(head.pk, **kwargs)
        self.assertEqual(outcome.status, "published")
        head.refresh_from_db()
        self.assertEqual((head.state, head.revision), ("invalidated", 2))
        self.assertEqual(head.current_fact.supersedes_id, old_fact_id)
        self.assertTrue(IgMemoryFact.objects.filter(pk=old_fact_id).exists())
        self.assertTrue(memory.memory_chain_valid(head))
        before = self.memory_rows()
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(memory.append_memory_tombstone(head.pk, **kwargs).status, "stale")
        self.assert_no_memory_write(before, captured)

    def test_newer_reset_rejects_old_selected_reset_even_if_head_is_unchanged(self):
        head = self.selected_head()
        first = self.reset()
        kwargs = self.reset_kwargs(head, first)
        newer = self._next_language_result(suffix="newer-reset")
        self.reset(newer.watermark_message_id)
        before = self.memory_rows()
        with CaptureQueriesContext(connection) as captured:
            outcome = memory.append_memory_tombstone(head.pk, **kwargs)
        self.assertEqual(outcome.status, "stale")
        self.assert_no_memory_write(before, captured)

    def test_erasure_after_selection_fences_reset_without_memory_write(self):
        head = self.selected_head()
        audit = self.reset()
        kwargs = self.reset_kwargs(head, audit)
        IgClient.objects.filter(pk=self.client_row.pk).update(
            privacy_erasure_started_at=timezone.now(),
        )
        before = self.memory_rows()
        with CaptureQueriesContext(connection) as captured:
            outcome = memory.append_memory_tombstone(head.pk, **kwargs)
        self.assertEqual(outcome.status, "privacy_fenced")
        self.assert_no_memory_write(before, captured)

    def test_forged_client_or_reset_boundary_cannot_invalidate_current_head(self):
        head = self.selected_head()
        audit = self.reset()
        before = self.memory_rows()
        for change in (
            {"expected_client_id": self.client_row.pk + 100000},
            {"reset_after_message_id": audit.reset_after_message_id + 1},
            {"expected_reset_id": audit.pk + 100000},
        ):
            with self.subTest(change=change):
                kwargs = {**self.reset_kwargs(head, audit), **change}
                with CaptureQueriesContext(connection) as captured:
                    outcome = memory.append_memory_tombstone(head.pk, **kwargs)
                self.assertEqual(outcome.status, "stale")
                self.assert_no_memory_write(before, captured)

    def test_locked_reset_predicate_rejects_post_reset_assertion_without_expected_pair(self):
        audit = self.reset()
        newer = self._next_language_result(suffix="new-source", with_objection=True)
        self.assertEqual(memory.publish_analysis_memory(newer.pk).status, "published")
        head = self.selected_head()
        kwargs = self.reset_kwargs(head, audit)
        kwargs.pop("expected_current_fact_id")
        kwargs.pop("expected_revision")
        before = self.memory_rows()
        with CaptureQueriesContext(connection) as captured:
            outcome = memory.append_memory_tombstone(head.pk, **kwargs)
        self.assertEqual(outcome.status, "stale")
        self.assert_no_memory_write(before, captured)

    def test_client_language_scope_cannot_be_reset_by_episode_boundary(self):
        audit = self.reset()
        head = IgMemoryHead.objects.get(fact_key="observed_language")
        before = self.memory_rows()
        with CaptureQueriesContext(connection) as captured:
            outcome = memory.append_memory_tombstone(head.pk, **self.reset_kwargs(head, audit))
        self.assertEqual(outcome.status, "stale")
        self.assert_no_memory_write(before, captured)

    def test_newer_reset_with_identical_boundary_rejects_old_reset_identity(self):
        head = self.selected_head()
        audit = self.reset()
        kwargs = self.reset_kwargs(head, audit)
        self.reset(audit.reset_after_message_id)
        before = self.memory_rows()
        with CaptureQueriesContext(connection) as captured:
            outcome = memory.append_memory_tombstone(head.pk, **kwargs)
        self.assertEqual(outcome.status, "stale")
        self.assert_no_memory_write(before, captured)

    def test_batch_without_matching_durable_reset_cannot_invalidate(self):
        before = self.memory_rows()
        with CaptureQueriesContext(connection) as captured:
            report = memory.invalidate_memory_for_reset(
                client_id=self.client_row.pk, reset_after_message_id=self.message.pk,
            )
        self.assertEqual((report["considered"], report["invalidated"], report["stale"]), (0, 0, 1))
        self.assert_no_memory_write(before, captured)

    def test_current_reset_batch_invalidates_episode_slots_and_replays_without_rows(self):
        audit = self.reset()
        old_facts, old_evidence, _heads = self.memory_rows()
        report = memory.invalidate_memory_for_reset(
            client_id=self.client_row.pk, reset_after_message_id=audit.reset_after_message_id,
        )
        self.assertEqual((report["considered"], report["invalidated"], report["stale"]), (2, 2, 0))
        self.assertEqual(
            list(IgMemoryFact.objects.filter(pk__in=[row["id"] for row in old_facts]).order_by("id").values()),
            old_facts,
        )
        self.assertEqual(list(IgMemoryFactEvidence.objects.order_by("id").values()), old_evidence)
        language = IgMemoryHead.objects.get(fact_key="observed_language")
        self.assertEqual((language.state, language.revision), ("active", 1))
        self.assertTrue(all(memory.memory_chain_valid(head) for head in IgMemoryHead.objects.all()))
        before = self.memory_rows()
        with CaptureQueriesContext(connection) as captured:
            replay = memory.invalidate_memory_for_reset(
                client_id=self.client_row.pk, reset_after_message_id=audit.reset_after_message_id,
            )
        self.assertEqual((replay["considered"], replay["invalidated"], replay["stale"]), (0, 0, 0))
        self.assert_no_memory_write(before, captured)

    def assert_reset_selector_interleaving(self, *, reconcile):
        audit = self.reset()
        selected = self.selected_head()
        newer = self._next_language_result(suffix="selected-race", with_objection=True)
        real_append = memory.append_memory_tombstone

        def after_selection(head_or_id, **kwargs):
            if getattr(head_or_id, "pk", head_or_id) == selected.pk:
                self.assertEqual(memory.publish_analysis_memory(newer.pk).status, "published")
            return real_append(head_or_id, **kwargs)

        with patch.object(memory, "append_memory_tombstone", side_effect=after_selection):
            report = (
                memory.reconcile_reset_tombstones(limit=10) if reconcile
                else memory.invalidate_memory_for_reset(
                    client_id=self.client_row.pk,
                    reset_after_message_id=audit.reset_after_message_id,
                )
            )
        self.assert_new_assertion_survives(selected, newer, report)

    def test_direct_reset_preserves_assertion_published_after_selection(self):
        self.assert_reset_selector_interleaving(reconcile=False)

    def test_reconcile_reset_preserves_assertion_published_after_selection(self):
        self.assert_reset_selector_interleaving(reconcile=True)


@skipUnless(connection.vendor == "mysql", "Disposable MariaDB reset-selection race")
@override_settings(**fixtures.SHADOW)
class TypedMemoryResetCasMariaTests(ResetCasFixture, AnalysisPrivacyCleanupMixin, TransactionTestCase):
    def setUp(self):
        self.assertRegex(str(connection.settings_dict.get("NAME") or ""), r"^test_twocomms_[A-Za-z0-9_]+$")
        super().setUp()

    def test_reset_selection_and_post_reset_publisher_on_two_native_connections(self):
        audit = self.reset()
        selected = self.selected_head()
        newer = self._next_language_result(suffix="native-selected-race", with_objection=True)
        selected_before_publish = Barrier(2)
        publish_committed = Barrier(2)
        real_append = memory.append_memory_tombstone

        def after_selection(head_or_id, **kwargs):
            if getattr(head_or_id, "pk", head_or_id) == selected.pk:
                selected_before_publish.wait(timeout=15)
                publish_committed.wait(timeout=15)
            return real_append(head_or_id, **kwargs)

        def reset_worker():
            close_old_connections()
            try:
                return memory.invalidate_memory_for_reset(
                    client_id=self.client_row.pk,
                    reset_after_message_id=audit.reset_after_message_id,
                )
            finally:
                close_old_connections()

        def publish_worker():
            close_old_connections()
            try:
                selected_before_publish.wait(timeout=15)
                outcome = memory.publish_analysis_memory(newer.pk)
                publish_committed.wait(timeout=15)
                return outcome
            finally:
                close_old_connections()

        with patch.object(memory, "append_memory_tombstone", side_effect=after_selection):
            with ThreadPoolExecutor(max_workers=2) as pool:
                resetting = pool.submit(reset_worker)
                publishing = pool.submit(publish_worker)
                report = resetting.result(timeout=30)
                outcome = publishing.result(timeout=30)
        self.assertEqual(outcome.status, "published")
        self.assert_new_assertion_survives(selected, newer, report)
