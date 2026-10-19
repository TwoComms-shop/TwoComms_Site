"""Exact source silence remains visible after internal ownership is parked."""
from unittest.mock import patch
from datetime import timedelta
from copy import deepcopy

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management import tests_ig_revision_execution as fixtures
from management import tests_ig_r30_semantic_completion as semantic
from management.models import IgBotNotification
from management.services.ig_response_debt import (
    park_manual_revision, record_reply_debt, response_obligation, review_reply_debt,
)
from management.services.ig_technical_debt import _collect_response_obligations
from management.services.ig_daemon_health import (
    technical_debt_alert_decision, revalidate_technical_debt_notification,
)
from management.services.ig_reply_debt_inventory import _revision_delivery_proof


class UnansweredObligationTests(TransactionTestCase):
    reset_sequences = True
    _source = fixtures.RevisionExecutionTests._source
    _collecting_revision = fixtures.RevisionExecutionTests._collecting_revision
    _ready_revision = fixtures.RevisionExecutionTests._ready_revision
    _delivered = semantic.SemanticCompletionTests._delivered

    def test_no_effect_manual_debt_queues_one_exact_handoff_and_stays_unanswered(self):
        source, revision, _token = self._ready_revision()
        with patch("management.services.instagram_bot._provider_http") as provider:
            task = park_manual_revision(revision, "captured_state_exceeds_budget")
            record_reply_debt(revision, "captured_state_exceeds_budget")
        provider.assert_not_called()
        self.assertEqual(IgBotNotification.objects.count(), 1)
        row = IgBotNotification.objects.get()
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.payload["source_message_ids"], [source.pk])
        self.assertEqual(row.payload["manager_task_id"], task.pk)
        observation = response_obligation(revision)
        self.assertTrue(observation["customer_ack_owed"])
        self.assertTrue(observation["substantive_owed"])
        self.assertFalse(observation["manager_informed"])
        self.assertTrue(observation["manager_notification_recorded"])
        self.assertEqual(observation["manager_notification_status"], "pending")
        self.assertTrue(observation["requires_attention"])
        source.refresh_from_db()
        self.assertEqual(source.status, "pending")
        self.assertFalse(revision.delivery_effects.exists())
        with CaptureQueriesContext(connection) as queries:
            cases = _collect_response_obligations(timezone.now(), 100)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))
        self.assertEqual(cases[0]["revision_id"], revision.pk)
        self.assertEqual(cases[0]["sample_ids"], [source.pk])
        self.assertIsNone(technical_debt_alert_decision({"cases": cases, "coverage_complete": True}))

    def test_wrong_topic_notification_cannot_prove_manager_informed(self):
        source, revision, _token = self._ready_revision()
        task = park_manual_revision(revision, "captured_state_exceeds_budget")
        IgBotNotification.objects.create(client=source.client, dedupe_key="wrong-fulfillment-topic",
            event_type="escalation", status="sent", sent_at=timezone.now(), telegram_message_id="501",
            payload={"manager_task_id": task.pk + 1, "revision_id": revision.pk,
                     "source_message_ids": [source.pk], "case_kind": "fulfillment"})
        self.assertFalse(response_obligation(revision)["manager_informed"])
        row = IgBotNotification.objects.exclude(dedupe_key="wrong-fulfillment-topic").get()
        row.status = "sent"
        row.sent_at = timezone.now()
        row.telegram_message_id = "502"
        row.save()
        result = response_obligation(revision)
        self.assertTrue(result["manager_informed"])
        self.assertTrue(result["substantive_owed"])
        self.assertTrue(result["requires_attention"])

    def test_exact_outbox_owns_delivery_stall_and_unknown_without_duplicate_aggregate(self):
        from management.services.ig_lane_health import _job_lane

        _source, revision, _token = self._ready_revision()
        park_manual_revision(revision, "generation_failed")
        row = IgBotNotification.objects.get()
        lane = _job_lane(IgBotNotification, now=timezone.now() + timedelta(minutes=16),
            statuses=("pending", "sending", "unknown", "failed", "dead_letter"), progress_field="sent_at")
        self.assertFalse(lane["healthy"])
        self.assertGreater(lane["attention_total"], 0)
        row.status = "unknown"
        row.save()
        lane = _job_lane(IgBotNotification, now=timezone.now(),
            statuses=("pending", "sending", "unknown", "failed", "dead_letter"), progress_field="sent_at")
        self.assertFalse(lane["healthy"])
        self.assertEqual(lane["counts"]["unknown"], 1)
        self.assertIsNone(technical_debt_alert_decision({"coverage_complete": True,
            "cases": _collect_response_obligations(timezone.now(), 100)}))

    def test_exact_explicit_review_preserves_pending_history_without_false_delivery(self):
        source, revision, _token = self._ready_revision()
        task = park_manual_revision(revision, "generation_failed")
        actor = get_user_model().objects.create_user(username="obligation-reviewer")
        result = review_reply_debt(revision.client_id, task.pk, actor=actor, expected_revision_id=revision.pk)
        self.assertTrue(result["ok"])
        revision.refresh_from_db()
        observed = response_obligation(revision)
        self.assertEqual(observed["disposition"], "reviewed_no_reply")
        self.assertFalse(observed["requires_attention"])
        self.assertTrue(observed["substantive_owed"])
        self.assertEqual(_collect_response_obligations(timezone.now(), 100), [])
        source.refresh_from_db()
        self.assertEqual(source.status, "pending")

    def _legacy_audited_review(self, source_count=1):
        from management.models import AdminAuditLog
        from management.services.ig_revision_execution import prepare_revision

        sources = [self._source(f"legacy-reviewed-{source_count}", f"legacy-reviewed-{source_count}-{index}")
                   for index in range(source_count)]
        _turn, revision = self._collecting_revision(sources)
        self.assertTrue(prepare_revision(revision.pk, lambda **kwargs: None).ready)
        revision.refresh_from_db()
        task = record_reply_debt(revision, "generation_outcome_unresolved")
        actor = get_user_model().objects.create_user(username=f"legacy-review-actor-{source_count}")
        self.assertTrue(review_reply_debt(revision.client_id, task.pk, actor=actor,
            expected_revision_id=revision.pk)["ok"])
        task.refresh_from_db()
        task.manager_context = {key: value for key, value in task.manager_context.items()
                                if key not in {"owner", "disposition"}}
        task.save(update_fields=["manager_context", "updated_at"])
        audit = AdminAuditLog.objects.get(action="ig_reply_debt_reviewed", entity_id=str(task.pk))
        revision.refresh_from_db()
        return sources, revision, task, audit

    def test_legacy_single_and_burst_audited_reviews_without_owner_are_dispositions_only(self):
        for count in (1, 2):
            with self.subTest(source_count=count):
                sources, revision, _task, _audit = self._legacy_audited_review(count)
                with CaptureQueriesContext(connection) as queries:
                    observed = response_obligation(revision)
                self.assertEqual(observed["disposition"], "reviewed_no_reply")
                self.assertFalse(observed["requires_attention"])
                self.assertTrue(observed["customer_ack_owed"])
                self.assertTrue(observed["substantive_owed"])
                self.assertFalse(observed["manager_informed"])
                self.assertFalse(observed["manager_notification_recorded"])
                self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))
                revision.refresh_from_db()
                self.assertIsNone(revision.processed_at)
                for source in sources:
                    source.refresh_from_db()
                    self.assertEqual(source.status, "pending")

    def test_legacy_review_requires_matching_actor_audit_status_and_exact_source_scope(self):
        from management.models import AdminAuditLog

        _sources, revision, _task, audit = self._legacy_audited_review()
        other = get_user_model().objects.create_user(username="wrong-legacy-review-actor")
        for mutation in ("missing", "actor", "payload_actor", "source", "revision", "status", "outcome"):
            with self.subTest(mutation=mutation), transaction.atomic():
                if mutation == "missing":
                    AdminAuditLog.objects.filter(pk=audit.pk).delete()
                elif mutation == "actor":
                    AdminAuditLog.objects.filter(pk=audit.pk).update(actor_id=other.pk)
                else:
                    after = deepcopy(audit.after)
                    key, value = {"payload_actor": ("actor_id", other.pk),
                                  "source": ("source_message_ids", [999999]),
                                  "revision": ("revision_id", revision.pk + 1),
                                  "status": ("status", "completed"),
                                  "outcome": ("outcome", "manager_replied")}[mutation]
                    after[key] = value
                    AdminAuditLog.objects.filter(pk=audit.pk).update(after=after)
                self.assertTrue(response_obligation(revision)["requires_attention"])
                transaction.set_rollback(True)

    def test_physical_sent_does_not_cover_remaining_semantics(self):
        _source, revision, _token = self._delivered(remaining=["cooperation_terms"], disposition="manual")
        effects = list(revision.delivery_effects.all())
        self.assertEqual(_revision_delivery_proof(revision, effects), ("manual", "semantic_reply_incomplete"))
        observed = response_obligation(revision, effects=effects)
        self.assertFalse(observed["customer_ack_owed"])
        self.assertTrue(observed["substantive_owed"])
        self.assertTrue(observed["requires_attention"])

    def test_bare_review_json_cannot_hide_unanswered_source(self):
        source, revision, _token = self._ready_revision()
        task = park_manual_revision(revision, "generation_failed")
        task.status = task.Status.CANCELLED
        task.manager_context = {**task.manager_context, "operator_review": {
            "outcome": "reviewed_no_reply", "revision_id": revision.pk,
            "source_message_ids": [source.pk], "reply_confirmed": False}}
        task.save()
        self.assertTrue(response_obligation(revision)["requires_attention"])
        self.assertEqual(_collect_response_obligations(timezone.now(), 100)[0]["revision_id"], revision.pk)

    def test_valid_transfer_keeps_obligation_until_successor_has_real_disposition(self):
        from management import tests_ig_reply_debt_inventory as inventory
        from management.models import IgClient

        factory = inventory.ReplyDebtInventoryTests()
        customer = IgClient.objects.create(igsid="obligation-paired-transfer")
        source = factory._source(customer)
        predecessor, successor = factory._transfer(customer, source)
        self.assertTrue(response_obligation(predecessor)["requires_attention"])
        task = park_manual_revision(successor, "generation_failed")
        actor = get_user_model().objects.create_user(username="successor-reviewer")
        self.assertTrue(review_reply_debt(customer.pk, task.pk, actor=actor,
            expected_revision_id=successor.pk)["ok"])
        observed = response_obligation(predecessor)
        self.assertEqual(observed["disposition"], "transferred")
        self.assertFalse(observed["requires_attention"])
        self.assertTrue(observed["substantive_owed"])
        self.assertEqual(observed["successor_revision_id"], successor.pk)

    def test_neutral_ack_is_not_substantive_coverage(self):
        _source, revision, _token = self._delivered(remaining=[], disposition="complete")
        revision.action_receipts = {**revision.action_receipts, "provider_safe_reply": {"reply_mode": "neutral_ack"}}
        revision.save(update_fields=["action_receipts", "updated_at"])
        effects = list(revision.delivery_effects.all())
        self.assertEqual(_revision_delivery_proof(revision, effects), ("manual", "neutral_ack_only"))
        self.assertTrue(response_obligation(revision)["substantive_owed"])

    def test_missing_and_empty_semantic_receipts_leave_sent_reply_obligation_open(self):
        _source, revision, _token = self._delivered(remaining=[], disposition="complete")
        valid_receipts = dict(revision.action_receipts)
        effects = list(revision.delivery_effects.all())
        self.assertEqual(response_obligation(revision)["disposition"], "answered")
        self.assertEqual(_revision_delivery_proof(revision, effects), ("coverage", "canonical_delivery_proof"))
        for receipt in (None, {}):
            with self.subTest(receipt=receipt):
                revision.action_receipts = dict(valid_receipts)
                if receipt is None:
                    revision.action_receipts.pop("response_coverage")
                else:
                    revision.action_receipts["response_coverage"] = receipt
                observed = response_obligation(revision)
                self.assertFalse(observed["customer_ack_owed"])
                self.assertTrue(observed["substantive_owed"])
                self.assertTrue(observed["requires_attention"])
                self.assertNotEqual(observed["disposition"], "answered")
                self.assertEqual(_revision_delivery_proof(revision, effects), ("manual", "semantic_coverage_unverified"))

    def test_empty_covered_evidence_cannot_establish_semantic_completion(self):
        from management.services.ig_revision_outbox import _digest

        _source, revision, _token = self._delivered(remaining=[], disposition="complete")
        receipt = dict(revision.action_receipts["response_coverage"])
        receipt["covered"] = []
        receipt.pop("digest")
        receipt["digest"] = _digest(receipt)
        revision.action_receipts["response_coverage"] = receipt
        self.assertTrue(response_obligation(revision)["substantive_owed"])

    def test_observed_incident_delivers_despite_unrelated_incomplete_inventory(self):
        _source, revision, _token = self._ready_revision()
        park_manual_revision(revision, "generation_failed")
        # The production incident predates the durable exact handoff producer.
        IgBotNotification.objects.all().delete()
        snapshot = {"coverage_complete": False, "cases": _collect_response_obligations(timezone.now(), 100)}
        decision = technical_debt_alert_decision(snapshot)
        row = IgBotNotification.objects.create(dedupe_key=decision["dedupe_key"],
            event_type="ig_technical_debt", payload=decision["metadata"])
        self.assertTrue(revalidate_technical_debt_notification(row.pk, snapshot=snapshot))
        self.assertFalse(revalidate_technical_debt_notification(row.pk,
            snapshot={"coverage_complete": False, "cases": []}))
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.audit_events.count(), 0)
        older = {**snapshot, "cases": [{**case, "oldest_age_seconds": 999999} for case in snapshot["cases"]]}
        self.assertEqual(decision["dedupe_key"], technical_debt_alert_decision(older)["dedupe_key"])


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativeGreetingObligationTests(TransactionTestCase):
    from management.tests_ig_revision_live import RevisionLiveTests as _Native

    reset_sequences = True
    setUp = _Native.setUp
    _message = _Native._message
    _prepare = _Native._prepare
    _generate = _Native._generate
    _execute = _Native._execute
    _replace_bundle = _Native._replace_bundle

    def test_native_mixed_greeting_and_price_question_retains_question(self):
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_response_control import parse_structured_response

        self._replace_bundle(["Привіт!", "Яка ціна футболки?"])
        plan = capture_response_plan(self.customer, revision=self.revision)
        coverage = plan.coverage(parse_structured_response({"reply_text": "Вітаю!", "controls": []}))
        self.assertTrue(coverage["remaining"])
        self.assertNotEqual(coverage["disposition"], "complete")
        # Even a source-bound empty-covered complete receipt cannot treat this
        # mixed bundle as the native greeting-only exception.
        from management.services.ig_response_debt import record_response_coverage, response_coverage_complete

        self.assertTrue(record_response_coverage(self.revision.pk, self.token, {
            "plan_digest": plan.digest, "covered": [], "remaining": [], "disposition": "complete"}))
        self.revision.refresh_from_db()
        self.assertFalse(response_coverage_complete(self.revision))
        self.assertTrue(response_obligation(self.revision)["substantive_owed"])

    def test_actual_greeting_sent_with_native_empty_plan_is_semantically_complete(self):
        self._replace_bundle(["Привіт!"])
        self.parsed = {"reply_text": "Вітаю! Чим можемо допомогти?", "controls": []}
        result, generation, transport = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(transport.call_count, 1)
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts["response_coverage"]
        self.assertEqual(receipt["covered"], [])
        self.assertEqual(receipt["remaining"], [])
        self.assertEqual(receipt["disposition"], "complete")
        self.assertTrue(receipt["plan_digest"])
        self.assertEqual(response_obligation(self.revision)["disposition"], "answered")
        self.assertEqual(_revision_delivery_proof(self.revision, list(self.revision.delivery_effects.all())),
            ("coverage", "canonical_delivery_proof"))
        # The greeting exemption still requires the native source-bound receipt.
        original = dict(self.revision.action_receipts)
        for missing in (None, {}):
            with self.subTest(missing=missing):
                self.revision.action_receipts = dict(original)
                if missing is None:
                    self.revision.action_receipts.pop("response_coverage")
                else:
                    self.revision.action_receipts["response_coverage"] = missing
                self.assertTrue(response_obligation(self.revision)["substantive_owed"])
