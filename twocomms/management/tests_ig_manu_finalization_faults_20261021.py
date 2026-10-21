"""SENT proof failures become finite operator debt, never another send."""
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management import tests_ig_revision_delivery as delivery_fixtures
from management import tests_ig_revision_execution as execution_fixtures
from management.models import IgBotNotification, IgCustomerTurnRevision, IgFollowUpTask
from management.services import ig_revision_execution as execution
from management.services.ig_revision_delivery import ProviderPartResult, drain_group
from management.services.ig_revision_finalization_faults import (
    FAULT_KEY, finalization_fault_blocks_execution, finalization_fault_disposition,
)
from management.services.ig_revision_outbox import _digest, plan_revision_effects


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class ManuFinalizationFaultTests(TransactionTestCase):
    reset_sequences = True
    setUp = delivery_fixtures.RevisionDeliveryTests.setUp
    _source = execution_fixtures.RevisionExecutionTests._source
    _collecting_revision = execution_fixtures.RevisionExecutionTests._collecting_revision
    _ready_revision = execution_fixtures.RevisionExecutionTests._ready_revision

    def _inject_historical_fault(self, row, field):
        # Production ORM guards correctly forbid these changes. Deliberately
        # inject legacy/corrupt persisted evidence to exercise quarantine only.
        quote = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {quote(row._meta.db_table)} SET {quote(row._meta.get_field(field).column)} = %s WHERE id = %s",
                [getattr(row, field), row.pk])

    def _sent(self, label, *, parts=1, unknown_last=False):
        source, revision, token = self._ready_revision(label)
        result = plan_revision_effects(revision.pk, token, source_message_id=source.pk,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding, authority_context_digest="e" * 64,
            effects=[{"group": "substantive_text", "kind": "text", "payload": {
                "recipient": {"id": source.sender_id}, "message": {"text": f"Answer {index}"}}}
                for index in range(parts)])
        self.assertTrue(result.created, result.reasons)
        calls = []

        def transport(_payload):
            calls.append(1)
            if unknown_last and len(calls) == parts:
                return ProviderPartResult(source.provider_namespace, outcome="timeout")
            return ProviderPartResult(source.provider_namespace, 200, f"receipt-{revision.pk}-{len(calls)}")

        drain_group(revision.pk, token, "substantive_text", transport)
        self.assertEqual(len(calls), parts)
        IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(lease_until=None)
        revision.refresh_from_db()
        source.refresh_from_db()
        return source, revision, list(revision.delivery_effects.order_by("order_index", "id"))

    def _finalize(self, revision):
        with patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("no transport")), \
             patch("management.services.instagram_bot.gemini_generate", side_effect=AssertionError("no generation")):
            return execution.finalize_sent_revision_effects(revision.pk)

    def _fault_receipt(self, revision):
        return IgFollowUpTask.objects.get(event_key=f"ig-revision-debt:{revision.pk}").manager_context[FAULT_KEY]

    def test_fault_is_once_only_hashed_source_bound_and_does_not_rewrite_ledgers(self):
        source, revision, effects = self._sent("poison")
        effects[0].payload_digest = "0" * 64
        self._inject_historical_fault(effects[0], "payload_digest")
        before = list(revision.delivery_effects.values())
        before_source = (source.status, source.send_state, source.attempts)
        result = self._finalize(revision)
        self.assertEqual(result.reason, "delivery_receipt_proof_invalid")
        revision.refresh_from_db()
        receipt = self._fault_receipt(revision)
        self.assertEqual(receipt["source_message_ids"], [source.pk])
        self.assertEqual(receipt["effect_ids"], [effects[0].pk])
        self.assertEqual(receipt["digest"], _digest({key: value for key, value in receipt.items() if key != "digest"}))
        self.assertEqual(finalization_fault_disposition(revision), result.reason)
        self.assertTrue(finalization_fault_blocks_execution(revision))
        self.assertEqual(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{revision.pk}").count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(payload__revision_id=revision.pk).count(), 1)
        before_revision = revision.action_receipts
        with CaptureQueriesContext(connection) as queries:
            self._finalize(revision)
            self.assertNotIn(revision.pk, execution.finalization_due_ids(limit=1))
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for query in queries))
        revision.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(revision.action_receipts, before_revision)
        self.assertEqual(list(revision.delivery_effects.values()), before)
        self.assertEqual(source.status, "pending")
        self.assertEqual((source.status, source.send_state, source.attempts), before_source)

    def test_expired_debt_checks_whole_proof_instead_of_accepting_all_sent(self):
        source, revision, effects = self._sent("expired")
        effects[0].recipient_igsid = "different-recipient"
        self._inject_historical_fault(effects[0], "recipient_igsid")
        now = timezone.now()
        IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(overall_deadline=now - timedelta(seconds=1))
        self.assertEqual(execution.record_expired_revision_debt(revision.pk, now=now), "delivery_receipt_proof_invalid")
        self.assertTrue(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{revision.pk}").exists())
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(execution.record_expired_revision_debt(revision.pk, now=now), "finalization_proof_quarantined")
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for query in queries))
        source.refresh_from_db()
        self.assertEqual(source.status, "pending")

    def test_changed_valid_proof_reenters_finalization_only_without_resending(self):
        source, revision, effects = self._sent("repair")
        original = effects[0].payload_digest
        effects[0].payload_digest = "0" * 64
        self._inject_historical_fault(effects[0], "payload_digest")
        self._finalize(revision)
        effects[0].payload_digest = original
        self._inject_historical_fault(effects[0], "payload_digest")
        revision.refresh_from_db()
        self.assertEqual(finalization_fault_disposition(revision), "")
        self.assertTrue(finalization_fault_blocks_execution(revision))
        self.assertIn(revision.pk, execution.finalization_due_ids(limit=2))
        result = self._finalize(revision)
        self.assertTrue(result.completed, result.reason)
        source.refresh_from_db()
        self.assertEqual(source.status, "done")
        self.assertEqual(effects[0].provider_message_id, f"receipt-{revision.pk}-1")

    def test_changed_invalid_proof_records_new_exact_cause_and_history_once(self):
        _source, revision, effects = self._sent("history", parts=2)
        effects[0].payload_digest = "0" * 64
        self._inject_historical_fault(effects[0], "payload_digest")
        self._finalize(revision)
        revision.refresh_from_db()
        previous = self._fault_receipt(revision)
        effects[1].provider_message_id = effects[0].provider_message_id
        self._inject_historical_fault(effects[1], "provider_message_id")
        result = self._finalize(revision)
        self.assertEqual(result.reason, "delivery_provider_receipt_duplicate")
        revision.refresh_from_db()
        receipt = self._fault_receipt(revision)
        self.assertEqual(receipt["previous_digest"], previous["digest"])
        self.assertEqual(receipt["history"][-1]["code"], previous["code"])
        self.assertEqual(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{revision.pk}").count(), 1)

    def test_more_than_limit_poison_rows_cannot_hide_healthy_sent_receipt(self):
        poison = []
        for index in range(5):
            _source, revision, effects = self._sent(f"poison-{index}")
            effects[0].payload_digest = "0" * 64
            self._inject_historical_fault(effects[0], "payload_digest")
            poison.append(revision.pk)
        healthy_source, healthy, _ = self._sent("healthy")
        for _ in range(4):
            for revision_id in execution.finalization_due_ids(limit=2):
                self._finalize(IgCustomerTurnRevision.objects.get(pk=revision_id))
        healthy.refresh_from_db()
        healthy_source.refresh_from_db()
        self.assertEqual(healthy.state, "processed")
        self.assertEqual(healthy_source.status, "done")
        self.assertEqual(IgFollowUpTask.objects.filter(event_key__in=[f"ig-revision-debt:{pk}" for pk in poison]).count(), 5)
        self.assertEqual(execution.finalization_due_ids(limit=2), [])

    def test_partial_sent_unknown_fault_keeps_unknown_and_sent_immutable(self):
        source, revision, effects = self._sent("unknown", parts=2, unknown_last=True)
        effects[0].payload_digest = "0" * 64
        self._inject_historical_fault(effects[0], "payload_digest")
        before = list(revision.delivery_effects.values())
        self._finalize(revision)
        self.assertEqual(list(revision.delivery_effects.values()), before)
        self.assertEqual([row["state"] for row in before], ["sent", "unknown"])
        source.refresh_from_db()
        self.assertEqual(source.status, "pending")

    def test_active_lease_and_erasure_do_not_create_quarantine_cases(self):
        _source, revision, effects = self._sent("active")
        effects[0].payload_digest = "0" * 64
        self._inject_historical_fault(effects[0], "payload_digest")
        IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(lease_until=timezone.now() + timedelta(minutes=1))
        self.assertEqual(self._finalize(revision).reason, "finalization_lease_active")
        revision.client.privacy_erasure_started_at = timezone.now()
        revision.client.save(update_fields=["privacy_erasure_started_at"])
        self.assertEqual(self._finalize(revision).reason, "erasure_private_projection_suppressed")
        self.assertFalse(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{revision.pk}").exists())

    def test_source_graph_scope_is_checked_and_hashed(self):
        _source, revision, _effects = self._sent("source-scope")
        source_row = revision.sources.first()
        source_row.source_namespace = "instagram_login:another-owner"
        self._inject_historical_fault(source_row, "source_namespace")
        self.assertEqual(self._finalize(revision).reason, "delivery_receipt_scope_invalid")

    def test_plan_and_part_proof_faults_have_finite_native_dispositions(self):
        for field, value, expected in (
            ("plan_digest", "", "delivery_plan_proof_invalid"),
            ("part_count", 2, "delivery_parts_proof_invalid"),
        ):
            with self.subTest(field=field):
                source, revision, effects = self._sent(f"bad-{field}")
                setattr(effects[0], field, value)
                self._inject_historical_fault(effects[0], field)
                before = list(revision.delivery_effects.values())
                self.assertEqual(self._finalize(revision).reason, expected)
                self.assertEqual(self._fault_receipt(revision)["code"], expected)
                self.assertEqual(list(revision.delivery_effects.values()), before)
                source.refresh_from_db()
                self.assertEqual(source.status, "pending")

    def test_malformed_quarantine_remains_execution_blocked_until_proof_revalidation(self):
        _source, revision, effects = self._sent("malformed")
        effects[0].payload_digest = "0" * 64
        self._inject_historical_fault(effects[0], "payload_digest")
        self._finalize(revision)
        task = IgFollowUpTask.objects.get(event_key=f"ig-revision-debt:{revision.pk}")
        task.manager_context[FAULT_KEY] = {"version": "invalid", "code": "unknown"}
        task.save(update_fields=["manager_context"])
        self.assertTrue(finalization_fault_blocks_execution(revision))
        self.assertEqual(finalization_fault_disposition(revision), "")
        self.assertIn(revision.pk, execution.finalization_due_ids(limit=2))
        self.assertEqual(self._finalize(revision).reason, "delivery_receipt_proof_invalid")
        self.assertEqual(self._fault_receipt(revision)["code"], "delivery_receipt_proof_invalid")
