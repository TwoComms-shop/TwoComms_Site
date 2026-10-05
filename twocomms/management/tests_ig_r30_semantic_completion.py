"""SENT transport receipts must retain uncovered customer obligations."""

from unittest.mock import patch

from django.test import TransactionTestCase
from django.utils import timezone

from management import tests_ig_revision_execution as fixtures
from management.models import IgFollowUpTask, IgRevisionDeliveryEffect, InstagramBotMessage
from management.services.ig_response_debt import record_response_coverage, response_coverage
from management.services.ig_revision_execution import (
    complete_revision_from_effects, finalization_due_ids,
    finalize_sent_revision_effects,
)
from management.services.ig_revision_outbox import _digest


class SemanticCompletionTests(TransactionTestCase):
    reset_sequences = True
    _collecting_revision = fixtures.RevisionExecutionTests._collecting_revision
    _ready_revision = fixtures.RevisionExecutionTests._ready_revision

    def _source(self, sender, mid, text="Хочу замовити футболку розмір L"):
        return fixtures.RevisionExecutionTests._source(self, sender, mid, text)

    def _delivered(self, *, remaining, disposition):
        source, revision, token = self._ready_revision()
        self.assertTrue(record_response_coverage(revision.pk, token, {
            "covered": ["size_choice"], "remaining": remaining,
            "disposition": disposition,
            "next_selector": "product" if disposition == "waiting_on_customer" else "",
            "plan_digest": "a" * 64,
        }))
        payload = {"recipient": {"id": source.sender_id}, "message": {
            "text": "Розмір L враховано. Яку модель футболки обираєте?"}}
        IgRevisionDeliveryEffect.objects.create(
            revision=revision, source_message=source,
            effect_key=f"semantic-part-{revision.pk}", actor="bot", purpose="normal_reply",
            group="substantive_text", kind="text", order_index=0, part_index=0, part_count=1,
            plan_digest="a" * 64, payload=payload, payload_digest=_digest(payload),
            recipient_igsid=source.sender_id, provider_namespace=source.provider_namespace,
            settings_id_snapshot=1, settings_permission_epoch=0,
            client_permission_epoch=revision.permission_epoch,
            revision_snapshot_digest=revision.snapshot_digest,
            publication_id=1, publication_version=1, publication_hash="b" * 64,
            authority_context_digest="c" * 64, state="sent",
            provider_message_id=f"synthetic-receipt-{revision.pk}", terminal_at=timezone.now(),
        )
        revision.refresh_from_db()
        return source, revision, token

    def _finalize(self, revision, token):
        # Receipt/history presentation has its own integration suite. Here the
        # real durable aggregate and semantic disposition run without HTTP.
        with (
            patch("management.services.ig_revision_live._project_sent_history"),
            patch("management.services.ig_revision_outbox.project_legacy_message"),
            patch("management.services.instagram_bot._provider_http") as send,
            patch("management.services.call_ai_analysis.gemini_generate_text") as generate,
        ):
            result = finalize_sent_revision_effects(revision.pk, execution_token=token)
        send.assert_not_called()
        generate.assert_not_called()
        return result

    def test_low_level_completion_cannot_clear_uncovered_purchase(self):
        source, revision, token = self._delivered(remaining=["purchase"], disposition="manual")
        result = complete_revision_from_effects(revision.pk, token)
        self.assertFalse(result.completed)
        self.assertEqual(result.reason, "semantic_reply_incomplete")
        source.refresh_from_db()
        self.assertEqual(source.status, InstagramBotMessage.Status.PENDING)

    def test_selector_question_waits_customer_without_retry_or_manager_alert(self):
        source, revision, token = self._delivered(remaining=["product_choice"], disposition="waiting_on_customer")
        result = self._finalize(revision, token)
        self.assertFalse(result.completed)
        self.assertEqual(result.reason, "waiting_on_customer")
        revision.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(revision.action_receipts["response_debt"]["owner"], "customer")
        self.assertIsNone(revision.recovery_due_at)
        self.assertEqual(source.status, InstagramBotMessage.Status.PENDING)
        self.assertFalse(IgFollowUpTask.objects.filter(reason="revision_case:execution_debt").exists())
        self.assertNotIn(revision.pk, finalization_due_ids())
        receipt = revision.action_receipts["semantic_delivery_finalized"]
        again = self._finalize(revision, "")
        self.assertEqual(again.reason, "waiting_on_customer")
        revision.refresh_from_db()
        self.assertEqual(revision.action_receipts["semantic_delivery_finalized"], receipt)

    def test_incomplete_answer_has_explicit_manager_owner_and_pending_source(self):
        source, revision, token = self._delivered(remaining=["purchase"], disposition="manual")
        result = self._finalize(revision, token)
        self.assertFalse(result.completed)
        self.assertEqual(result.reason, "semantic_reply_incomplete")
        revision.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(revision.action_receipts["response_debt"]["owner"], "manager")
        self.assertEqual(revision.action_receipts["response_debt"]["disposition"], "manager_reply_uncovered")
        self.assertEqual(source.status, InstagramBotMessage.Status.PENDING)
        self.assertNotIn(revision.pk, finalization_due_ids())

    def test_complete_coverage_acknowledges_original_source(self):
        source, revision, token = self._delivered(remaining=[], disposition="complete")
        result = self._finalize(revision, token)
        self.assertTrue(result.completed, result.reason)
        self.assertEqual(result.reason, "receipt_finalized")
        source.refresh_from_db()
        self.assertEqual(source.status, InstagramBotMessage.Status.DONE)

    def test_tampered_read_coverage_fails_closed_despite_sent(self):
        _source, revision, _token = self._delivered(remaining=[], disposition="complete")
        receipts = dict(revision.action_receipts)
        receipts["response_coverage"] = {**receipts["response_coverage"], "digest": "f" * 64}
        # Simulate a corrupt read without bypassing append-only persistence.
        revision.action_receipts = receipts
        self.assertEqual(response_coverage(revision), {
            "remaining": ["coverage_unverified"], "disposition": "manual",
        })
