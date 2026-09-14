"""Provider-free fairness regressions for runnable live reply demand."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from management.models import (
    IgClient, IgConversationAnalysisJob, IgCustomerTurn, IgTurnMessage,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services import bot_conversation_analysis as analysis
from management.services.ig_turn_revisions import (
    create_collecting_revision, claim_revision_preparation, seal_revision,
    claim_sealed_revision,
)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False)
class AnalysisFairnessTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.settings = InstagramBotSettings.objects.create(pk=1, is_enabled=True, ai_enabled=True)
        self.client = IgClient.objects.create(igsid="fairness-live")
        self.sequence = 0

    def message(self, *, age=0, status="pending", client=None):
        self.sequence += 1
        client = self.client if client is None else client
        source = InstagramBotMessage.objects.create(
            client=client, sender_id=client.igsid, role="user", source="webhook",
            text="Підкажіть модель", mid=f"fairness-{self.sequence}",
            provider_namespace="instagram_login:fixture", status=status,
        )
        InstagramBotMessage.objects.filter(pk=source.pk).update(created_at=self.now - timedelta(seconds=age))
        source.refresh_from_db()
        return source

    def revision(self, source, *, overall_deadline=None):
        turn = IgCustomerTurn.objects.create(
            client=self.client, primary_source_message=source,
            window_started_at=self.now, window_deadline=self.now,
        )
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        return create_collecting_revision(turn, [source], now=self.now, bypass_quiet=True,
                                          overall_deadline=overall_deadline).revision

    def job(self, *, age=0, attempts=0):
        customer = IgClient.objects.create(igsid=f"fairness-analysis-{self.sequence}-{age}")
        return IgConversationAnalysisJob.objects.create(
            client=customer, watermark_message_id=1, revision=1, attempts=attempts,
            due_at=self.now - timedelta(seconds=age), next_attempt_at=self.now,
        )

    def test_old_pending_and_orphan_do_not_starve_another_analysis_job(self):
        old = self.message(age=86400)
        orphan = InstagramBotMessage.objects.create(
            sender_id="orphan-fixture", role="user", text="Hello", status="pending",
        )
        job = self.job(age=60)
        self.assertFalse(analysis._customer_reply_work_waiting())
        with patch.object(analysis, "_process_claim", return_value="done") as process:
            counts = analysis.process_due_analysis(limit=1, now=self.now)
        self.assertEqual(counts["done"], 1)
        self.assertEqual(process.call_args.args[0].pk, job.pk)
        old.refresh_from_db()
        orphan.refresh_from_db()
        self.assertEqual((old.status, orphan.status), ("pending", "pending"))

    def test_recent_legacy_pending_retains_priority_and_bounded_next_opportunity(self):
        self.message(age=2)
        job = self.job(age=60, attempts=2)
        self.assertTrue(analysis._customer_reply_work_waiting())
        with patch.object(analysis, "_process_claim") as process:
            analysis.process_due_analysis(limit=1, now=self.now)
        process.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.attempts, 2)
        self.assertEqual(job.last_error, "deferred_for_live_reply")
        self.assertEqual(job.next_attempt_at, self.now + timedelta(seconds=15))
        self.assertEqual(job.due_at, self.now - timedelta(seconds=60))

    def test_stale_processing_without_start_time_is_not_runnable(self):
        source = self.message(age=86400, status="processing")
        self.assertFalse(analysis._customer_reply_work_waiting())
        source.processing_started_at = self.now
        source.save(update_fields=["processing_started_at"])
        self.assertTrue(analysis._customer_reply_work_waiting())

    def test_manual_canonical_pending_has_no_global_live_priority(self):
        source = self.message()
        revision = self.revision(source, overall_deadline=self.now - timedelta(seconds=1))
        revision.media_prepare_deadline = self.now - timedelta(seconds=1)
        revision.recovery_state = "manual"
        revision.recovery_code = "provider_candidates_exhausted"
        revision.save(update_fields=["media_prepare_deadline", "recovery_state", "recovery_code"])
        self.assertFalse(analysis._customer_reply_work_waiting())
        source.refresh_from_db()
        self.assertEqual(source.status, "pending")

    def test_valid_preparation_and_claim_are_prioritized_even_after_legacy_age(self):
        source = self.message(age=86400)
        revision = self.revision(source)
        claim = claim_revision_preparation(revision.pk, now=self.now)
        self.assertTrue(claim.token)
        self.assertTrue(analysis._customer_reply_work_waiting())
        sealed = seal_revision(revision.pk, claim.token, now=self.now)
        self.assertTrue(sealed.sealed, sealed.reason)
        claimed = claim_sealed_revision(revision.pk, now=self.now)
        self.assertTrue(claimed.token)
        self.assertTrue(analysis._customer_reply_work_waiting())

    def test_due_collecting_head_obeys_rollout_and_current_permission(self):
        source = self.message(age=86400)
        self.revision(source)
        self.assertFalse(analysis._customer_reply_work_waiting())
        with override_settings(IG_REVISION_EXECUTION_ENABLED=True,
                               IG_REVISION_EXECUTION_CUTOVER_AT=(self.now - timedelta(days=2)).isoformat()):
            self.assertTrue(analysis._customer_reply_work_waiting())
            self.client.manager_takeover = True
            self.client.save(update_fields=["manager_takeover"])
            self.assertFalse(analysis._customer_reply_work_waiting())

    def test_waiting_recovery_without_current_provider_proof_is_not_global_demand(self):
        source = self.message()
        revision = self.revision(source)
        preparing = claim_revision_preparation(revision.pk, now=self.now)
        self.assertTrue(seal_revision(revision.pk, preparing.token, now=self.now).sealed)
        claimed = claim_sealed_revision(revision.pk, now=self.now)
        self.assertTrue(claimed.token)
        revision.refresh_from_db()
        revision.recovery_state = "waiting"
        revision.recovery_due_at = self.now
        revision.save(update_fields=["recovery_state", "recovery_due_at"])
        self.assertFalse(analysis._customer_reply_work_waiting())

    def test_paused_takeover_erasure_optout_and_spam_have_no_global_priority(self):
        self.message()
        restrictions = {
            "bot_paused": True, "manager_takeover": True,
            "privacy_erasure_started_at": self.now, "opted_out_at": self.now,
            "stage": IgClient.Stage.SPAM, "hidden_at": self.now, "is_blocked": True,
        }
        for field, value in restrictions.items():
            original = getattr(self.client, field)
            setattr(self.client, field, value)
            self.client.save(update_fields=[field])
            self.assertFalse(analysis._customer_reply_work_waiting(), field)
            setattr(self.client, field, original)
            self.client.save(update_fields=[field])

    def test_aged_job_gets_one_background_slot_when_management_capacity_allows(self):
        self.message()
        job = self.job(age=3600)
        with patch.object(analysis, "_analysis_capacity_available", return_value=True), patch.object(
            analysis, "_process_claim", return_value="done"
        ) as process:
            counts = analysis.process_due_analysis(limit=10, now=self.now)
        self.assertEqual(counts["done"], 1)
        self.assertEqual(process.call_count, 1)
        self.assertEqual(process.call_args.args[0].pk, job.pk)

    def test_aged_job_without_capacity_is_explicitly_deferred_without_dispatch(self):
        self.message()
        job = self.job(age=3600)
        with patch.object(analysis, "_analysis_capacity_available", return_value=False), patch.object(
            analysis, "gemini_generate_json"
        ) as provider:
            analysis.process_due_analysis(limit=1, now=self.now)
        provider.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.last_error, "deferred_for_analysis_capacity")
        self.assertEqual(job.attempts, 0)
        self.assertGreater(job.next_attempt_at, self.now)

    def test_deferred_claim_preserves_prior_provider_failures(self):
        job = self.job(age=60, attempts=2)
        claimed, _watermark, _revision, token = analysis._claim_due(self.now)
        self.assertEqual(claimed.attempts, 3)
        self.assertTrue(analysis._defer_claim_for_customer_reply(job.pk, token, now=self.now))
        job.refresh_from_db()
        self.assertEqual(job.attempts, 2)

    def test_capacity_preflight_uses_existing_management_task_pool(self):
        from management.services import gemini_keys
        with patch.object(gemini_keys, "task_model_chain", return_value=["verified-test-model"]) as chain, patch.object(
            gemini_keys, "iter_attempts", return_value=iter([("BACKGROUND", "not-printed", "verified-test-model")])
        ) as candidates:
            self.assertTrue(analysis._analysis_capacity_available())
        chain.assert_called_once_with("management", "conversation_reanalysis")
        candidates.assert_called_once_with("management", model_chain_override=["verified-test-model"])
