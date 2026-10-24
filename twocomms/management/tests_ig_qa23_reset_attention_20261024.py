"""Fresh reset archives old conversation alerts without inventing an answer."""
from copy import deepcopy
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixtures
from management import tests_ig_revision_preference_fallback as fallback_fixtures
from management.models import (
    GeminiRequest, IgClient, IgFollowUpTask, IgFunnelResetAudit, IgPaymentConfirmationReview,
    IgRevisionDeliveryEffect, InstagramBotMessage,
)
from management.services.ig_funnel_reset import _archive_fresh_reply_attention, reset_funnel
from management.services.ig_response_debt import park_manual_revision, record_reply_debt, reply_debt_payload, with_reply_debt
from management.services.ig_revision_holding import confirmed_holding_effect_ids


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False,
                   INDEXNOW_ENABLED=False)
class FreshResetAttentionTests(TransactionTestCase):
    reset_sequences = True
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _execute = live_fixtures.RevisionLiveTests._execute
    _generate = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._generate
    _fit_fixture = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._fit_fixture
    _record = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._record

    def _ordinary_debt(self):
        self._prepare()
        return record_reply_debt(self.revision, "generation_failed")

    def _missing_manifest_debt(self, *, refresh=False):
        from management.services.ig_revision_live import RevisionGenerationBoundary
        from management.services.ig_revision_outbox import PublicationBinding
        from management.services.ig_revision_provider_execution import ProviderContinuation
        from management.services.ig_revision_recovery import record_generation_admission
        from management.services.ig_turn_revisions import create_refresh_successor

        self._prepare()
        self.economic_root_revision = self.revision
        if refresh:
            result = create_refresh_successor(self.revision.pk, self.token, reason="publication_changed")
            self.assertTrue(result.created, result.reason)
            self.revision = result.revision
            self._prepare()
        publication = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        authority = RevisionGenerationBoundary(self.revision, self.token, self.settings, publication).baseline
        # The retained historical case predates atomic manifest freezing.
        # Reproduce that old admission boundary; the task and its debt receipt
        # still come from the ordinary native service, never task rewrites.
        with patch("management.services.ig_revision_provider_execution.freeze_revision_provider_manifest_locked",
                   return_value=ProviderContinuation(ready=True)):
            admission = record_generation_admission(self.revision.pk, self.token,
                settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
                publication=publication, authority=authority)
        self.assertTrue(admission.ready, admission.reason)
        self.revision.refresh_from_db()
        return park_manual_revision(self.revision, "provider_manifest_missing")

    def _holding_debt(self, *, historical_case=True):
        self.assertFalse(self._fit_fixture(source_texts=["Сколько стоит эта футболка?"]).ready)
        result, _generate, http = self._execute()
        self.assertEqual(result.reasons, ("technical_holding_sent",))
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        task = IgFollowUpTask.objects.get(event_key=f"ig-revision-debt:{self.revision.pk}")
        if not historical_case:
            return task
        # Reproduce the retained historical case representation: that version
        # created its debt after the holding receipt. Current admission creates
        # the case before dispatch, so rebuild only this disposable task row.
        task.delete()
        task = record_reply_debt(self.revision, "technical_holding_sent",
                                 effects=list(self.revision.delivery_effects.order_by("order_index", "id")))
        self.assertEqual(task.manager_context["disposition"], "reconcile_delivery")
        self.assertEqual(confirmed_holding_effect_ids(self.revision), tuple(task.event_payload["effect_ids"]))
        return task

    def _reset(self, *, mode="fresh_conversation"):
        with patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("reset cannot send")):
            result = reset_funnel(client_id=self.customer.pk, actor=None, mode=mode)
        self.assertTrue(result["ok"], result)
        self.customer.refresh_from_db()
        return result

    def _assert_current_attention(self, required):
        self.assertEqual(reply_debt_payload(IgClient.objects.get(pk=self.customer.pk))["required"], required)
        self.assertEqual(with_reply_debt(IgClient.objects.filter(pk=self.customer.pk)).get().has_reply_debt, required)

    def test_full_reset_archives_old_reply_alert_without_claiming_a_delivered_answer(self):
        task = self._ordinary_debt()
        original = deepcopy(task.event_payload)
        self._assert_current_attention(True)
        result = self._reset()
        task.refresh_from_db()
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(task.event_payload, original)
        archive = task.manager_context["fresh_reset_archive"]
        self.assertFalse(archive["reply_confirmed"])
        self.assertEqual(archive["outcome"], "archived_by_fresh_reset")
        self.assertEqual(result["archived_reply_attention_task_ids"], [task.pk])
        self.assertEqual(result["archived_reply_attention_count"], 1)
        audit = IgFunnelResetAudit.objects.get(pk=result["audit_id"])
        self.assertEqual(audit.previous_state["reply_attention_retirement"]["archived_reply_attention_task_ids"], [task.pk])
        self._assert_current_attention(False)
        self.assertTrue(InstagramBotMessage.objects.filter(pk=self.source.pk).exists())

    def test_actual_policy_failure_confirmed_holding_is_archived_but_sent_history_is_unchanged(self):
        task = self._holding_debt()
        payload, context = deepcopy(task.event_payload), deepcopy(task.manager_context)
        effects = list(IgRevisionDeliveryEffect.objects.filter(revision=self.revision).values())
        transcript = list(InstagramBotMessage.objects.filter(client=self.customer).values())
        receipts = deepcopy(self.revision.action_receipts)
        result = self._reset()
        task.refresh_from_db()
        self.revision.refresh_from_db()
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(task.event_payload, payload)
        self.assertEqual({k: v for k, v in task.manager_context.items() if k != "fresh_reset_archive"}, context)
        self.assertEqual(self.revision.action_receipts, receipts)
        self.assertEqual(list(IgRevisionDeliveryEffect.objects.filter(revision=self.revision).values()), effects)
        after = list(InstagramBotMessage.objects.filter(client=self.customer).values())
        self.assertEqual([(row["id"], row["text"], row["send_state"], row["provider_message_id"]) for row in after],
                         [(row["id"], row["text"], row["send_state"], row["provider_message_id"]) for row in transcript])
        self.assertEqual(result["archived_reply_attention_task_ids"], [task.pk])
        self._assert_current_attention(False)

    def test_native_missing_manifest_crash_gap_archives_without_reply_or_provider_evidence(self):
        task = self._missing_manifest_debt()
        payload, receipts = deepcopy(task.event_payload), deepcopy(self.revision.action_receipts)
        transcript_fields = ("id", "text", "send_state", "provider_message_id", "delivery_provider_message_ids")
        transcript = list(InstagramBotMessage.objects.filter(client=self.customer).values(*transcript_fields))
        self.assertIn("generation_admission", receipts)
        self.assertIn("response_debt", receipts)
        self.assertNotIn("provider_execution_manifest", receipts)
        self.assertFalse(GeminiRequest.objects.exists())
        result = self._reset()
        task.refresh_from_db()
        self.revision.refresh_from_db()
        self.assertEqual(result["archived_reply_attention_task_ids"], [task.pk])
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(task.event_payload, payload)
        self.assertFalse(task.manager_context["fresh_reset_archive"]["reply_confirmed"])
        self.assertEqual(self.revision.action_receipts, receipts)
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertEqual(list(InstagramBotMessage.objects.filter(client=self.customer).values(*transcript_fields)), transcript)
        self._assert_current_attention(False)
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)

    def test_unstarted_refresh_family_can_archive_its_exact_native_crash_gap(self):
        task = self._missing_manifest_debt(refresh=True)
        self.assertEqual(self._reset()["archived_reply_attention_task_ids"], [task.pk])
        task.refresh_from_db()
        self.assertFalse(task.manager_context["fresh_reset_archive"]["reply_confirmed"])
        self._assert_current_attention(False)

    def test_second_native_reset_archives_retained_crash_gap_without_rewriting_first_audit(self):
        task = self._missing_manifest_debt()
        # Preserve the already completed old implementation's missed archive,
        # while exercising the ordinary reset transaction and retirement.
        with patch("management.services.ig_funnel_reset._missing_manifest_attention_is_unstarted", return_value=False):
            first = self._reset()
        first_audit = deepcopy(IgFunnelResetAudit.objects.get(pk=first["audit_id"]).previous_state)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.recovery_code, "fresh_conversation_reset")
        self.assertEqual(self.revision.state, "superseded")
        self._assert_current_attention(True)
        second = self._reset()
        self.assertEqual(second["archived_reply_attention_task_ids"], [task.pk])
        self.assertEqual(second["reply_permission_epoch"], first["reply_permission_epoch"] + 1)
        self.assertEqual(IgFunnelResetAudit.objects.get(pk=first["audit_id"]).previous_state, first_audit)
        task.refresh_from_db()
        self.assertFalse(task.manager_context["fresh_reset_archive"]["reply_confirmed"])
        self._assert_current_attention(False)

    def test_missing_manifest_reason_without_native_task_receipt_keeps_attention(self):
        self._prepare()
        task = record_reply_debt(self.revision, "provider_manifest_missing")
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self._assert_current_attention(True)

    def test_missing_manifest_native_task_receipt_must_match_exact_task(self):
        from django.db.models.query import QuerySet
        task = self._missing_manifest_debt()
        receipts = deepcopy(self.revision.action_receipts)
        receipts["response_debt"]["task_id"] = task.pk + 1
        # Isolated corruption bypasses the append-only model guard deliberately.
        QuerySet.update(type(self.revision).objects.filter(pk=self.revision.pk), action_receipts=receipts)
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_missing_manifest_any_graph_identity_keeps_attention_even_before_provider_start(self):
        from django.db.models.query import QuerySet
        task = self._missing_manifest_debt(refresh=True)
        for identity in ("logical_turn_id", "source_execution_key", "source_message_id"):
            with self.subTest(identity=identity):
                graph = GeminiRequest.objects.create(request_id=f"reset-gap-{identity}", lane="live",
                    task_class="simple_live", accounting_mode="shadow")
                value = self.source.pk if identity == "source_message_id" else f"ig-revision:{self.economic_root_revision.pk}"
                # A corrupt identity fixture must not disappear from the guard.
                QuerySet.update(GeminiRequest.objects.filter(pk=graph.pk), **{identity: value})
                self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
                task.refresh_from_db()
                self.assertEqual(task.status, "skipped")
                graph.delete()

    def test_missing_manifest_raw_family_manifest_or_reference_keeps_attention(self):
        from django.db.models.query import QuerySet
        task = self._missing_manifest_debt(refresh=True)
        root = self.economic_root_revision
        root.refresh_from_db()
        original = deepcopy(root.action_receipts)
        for key, value in (("provider_execution_manifest", {}),
                           ("provider_execution_reference", {"root_revision_id": self.revision.pk})):
            with self.subTest(key=key):
                QuerySet.update(type(root).objects.filter(pk=root.pk), action_receipts={**original, key: value})
                self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
                task.refresh_from_db()
                self.assertEqual(task.status, "skipped")
        QuerySet.update(type(root).objects.filter(pk=root.pk), action_receipts=original)

    def test_missing_manifest_invalid_family_keeps_attention(self):
        from django.db.models.query import QuerySet
        task = self._missing_manifest_debt(refresh=True)
        QuerySet.update(type(self.revision).objects.filter(pk=self.revision.pk), permission_epoch=999)
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_missing_manifest_corrupted_root_snapshot_keeps_attention(self):
        from django.db.models.query import QuerySet
        task = self._missing_manifest_debt(refresh=True)
        root = self.economic_root_revision
        snapshot = deepcopy(root.bundle_snapshot)
        snapshot["sources"][0]["text"] = "Corrupted historical source"
        QuerySet.update(type(root).objects.filter(pk=root.pk), bundle_snapshot=snapshot)
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_missing_manifest_unknown_ancestor_delivery_keeps_attention(self):
        task = self._missing_manifest_debt(refresh=True)
        root = self.economic_root_revision
        effect = IgRevisionDeliveryEffect.objects.create(revision=root, source_message=self.source,
            effect_key="reset-gap-unknown-ancestor", group="substantive_text", kind="text",
            order_index=0, part_index=0, part_count=1, plan_digest="a"*64,
            payload={"text": "Uncertain old reply"}, payload_digest="b"*64,
            recipient_igsid=self.customer.igsid, provider_namespace=self.source.provider_namespace,
            settings_id_snapshot=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            client_permission_epoch=root.permission_epoch, revision_snapshot_digest=root.snapshot_digest,
            publication_id=self.publication.pk, publication_version=self.publication.version,
            publication_hash=self.publication.snapshot_hash, authority_context_digest="c"*64, state="unknown")
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        effect.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self.assertEqual(effect.state, "unknown")

    def test_missing_manifest_family_source_after_reset_boundary_keeps_attention(self):
        task = self._missing_manifest_debt()
        with transaction.atomic():
            result = _archive_fresh_reply_attention(self.customer,
                boundary=self.source.pk-1, actor=None, now=timezone.now())
        self.assertEqual(result["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_missing_manifest_payment_evidence_keeps_attention(self):
        task = self._missing_manifest_debt()
        IgPaymentConfirmationReview.objects.create(client=self.customer,
            dedupe_key="missing-manifest-payment", watermark_message_id=self.source.pk)
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_unknown_holding_delivery_keeps_its_reconciliation_alert(self):
        task = self._holding_debt()
        IgRevisionDeliveryEffect.objects.filter(revision=self.revision).update(state="unknown")
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self.assertNotIn("fresh_reset_archive", task.manager_context)
        self.assertEqual(self.revision.delivery_effects.get().state, "unknown")
        self._assert_current_attention(True)

    def test_current_pre_dispatch_holding_case_uses_its_exact_native_task_admission(self):
        task = self._holding_debt(historical_case=False)
        original = deepcopy(task.event_payload)
        self.assertEqual(original["effect_ids"], [])
        self.assertEqual(self.revision.action_receipts["technical_holding"]["task_id"], task.pk)
        self.assertTrue(confirmed_holding_effect_ids(self.revision))
        self.assertEqual(self._reset()["archived_reply_attention_task_ids"], [task.pk])
        task.refresh_from_db()
        self.assertEqual(task.event_payload, original)
        self.assertFalse(task.manager_context["fresh_reset_archive"]["reply_confirmed"])
        self._assert_current_attention(False)

    def test_current_holding_with_an_extra_unknown_effect_keeps_attention(self):
        task = self._holding_debt(historical_case=False)
        effect = self.revision.delivery_effects.get()
        effect.pk = None
        effect.effect_key += ":unknown-sibling"
        effect.order_index = 1
        effect.group = "uncertain_sibling"
        effect.state = "unknown"
        effect.provider_message_id = ""
        effect.save(force_insert=True)
        self.assertEqual(confirmed_holding_effect_ids(self.revision), ())
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self._assert_current_attention(True)

    def test_empty_effect_manifest_requires_the_original_native_holding_task_identity(self):
        original = self._holding_debt(historical_case=False)
        original.delete()
        replacement = record_reply_debt(self.revision, "provider_candidates_exhausted")
        self.assertNotEqual(replacement.pk, self.revision.action_receipts["technical_holding"]["task_id"])
        self.assertTrue(confirmed_holding_effect_ids(self.revision))
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        replacement.refresh_from_db()
        self.assertEqual(replacement.status, "skipped")

    def test_changed_source_content_cannot_archive_a_previously_captured_alert(self):
        task = self._ordinary_debt()
        self.source.text = "Changed source after capture"
        self.source.save(update_fields=["text"])
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_unresolved_payment_coverage_keeps_its_manager_alert(self):
        task = self._ordinary_debt()
        self.revision.action_receipts = {**self.revision.action_receipts,
            "response_coverage": {"payment_verification": "unresolved"}}
        self.revision.save(update_fields=["action_receipts", "updated_at"])
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_sent_holding_without_native_delivery_receipt_keeps_attention(self):
        task = self._holding_debt()
        IgRevisionDeliveryEffect.objects.filter(revision=self.revision).update(provider_message_id="")
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self._assert_current_attention(True)

    def test_payment_evidence_and_independent_service_tasks_survive_full_reset(self):
        task = self._ordinary_debt()
        payment = IgPaymentConfirmationReview.objects.create(client=self.customer,
            dedupe_key="fresh-attention-payment", watermark_message_id=self.source.pk)
        services = [IgFollowUpTask.objects.create(client=self.customer, kind="manager_task",
                    due_at=timezone.now(), reason=reason) for reason in
                    ("existing_paid_order_service", "human_reply:delivery_unknown", "post_sale:review")]
        result = self._reset()
        self.assertEqual(result["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self.assertEqual(payment.status, "pending")
        for service in services:
            service.refresh_from_db()
            self.assertEqual(service.status, "pending")

    def test_source_ids_after_the_captured_reset_boundary_cannot_be_archived(self):
        task = self._ordinary_debt()
        with transaction.atomic():
            result = _archive_fresh_reply_attention(self.customer, boundary=self.source.pk-1, actor=None, now=timezone.now())
        self.assertEqual(result["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_foreign_revision_binding_and_manual_disposition_cannot_be_archived(self):
        task = self._ordinary_debt()
        other = IgClient.objects.create(igsid="fresh-attention-other")
        IgFollowUpTask.objects.filter(pk=task.pk).update(client=other)
        with transaction.atomic():
            result = _archive_fresh_reply_attention(other, boundary=self.source.pk, actor=None, now=timezone.now())
        self.assertEqual(result["archived_reply_attention_count"], 0)
        IgFollowUpTask.objects.filter(pk=task.pk).update(client=self.customer,
            manager_context={**task.manager_context, "disposition": "manual_service_review"})
        self.assertEqual(self._reset()["archived_reply_attention_count"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_legacy_funnel_reset_keeps_existing_reply_attention_contract(self):
        task = self._ordinary_debt()
        result = self._reset(mode="funnel")
        self.assertNotIn("archived_reply_attention_count", result)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self._assert_current_attention(True)

    def test_next_new_customer_source_can_create_independent_current_attention(self):
        task = self._ordinary_debt()
        self._reset()
        self._assert_current_attention(False)
        source = self._message("New independent question", "fresh-attention-new")
        from management.services.ig_turn_revisions import create_collecting_revision
        from management.models import IgCustomerTurn, IgTurnMessage
        turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        successor = create_collecting_revision(turn, [source], bypass_quiet=True).revision
        current = record_reply_debt(successor, "generation_failed")
        self.assertNotEqual(current.pk, task.pk)
        self._assert_current_attention(True)
        self.assertEqual(reply_debt_payload(IgClient.objects.get(pk=self.customer.pk))["source_message_ids"], [source.pk])
