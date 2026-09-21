from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from management.models import IgClient, IgConversationAnalysisJob, IgWorkerLaneState
from management.services.ig_analysis_lane import (
    acquire_owner,
    claim_admission,
    freeze_claims,
    owner_claim_admission,
    recover_frozen_lane,
    release_owner,
    renew_owner,
)


class AnalysisLaneFenceTests(TestCase):
    def test_unscoped_admission_does_not_create_implicit_manual_owner(self):
        self.assertIsNone(owner_claim_admission(now=timezone.now()))
        state = IgWorkerLaneState.objects.get_or_create(lane_key="conversation_analysis")[0]
        self.assertEqual(state.owner_kind, "")
        self.assertEqual(state.owner_token, "")

    def test_one_shot_boundary_binds_and_releases_a_manual_owner_explicitly(self):
        from management.services.bot_conversation_analysis import _analysis_owner_scope
        from management.services.ig_analysis_lane import current_owner

        with _analysis_owner_scope(now=timezone.now()) as owner:
            self.assertEqual(owner["owner_kind"], "manual")
            self.assertEqual(current_owner()["owner_token"], owner["owner_token"])
        self.assertIsNone(current_owner())

    def test_only_one_active_owner_and_generation_changes_on_takeover(self):
        now = timezone.now()
        first = acquire_owner(owner_kind="daemon", owner_token="a", now=now)
        self.assertIsNotNone(first)
        self.assertIsNone(acquire_owner(owner_kind="manual", owner_token="b", now=now))
        release_owner(owner_token="a", generation=first["generation"], now=now)
        second = acquire_owner(owner_kind="manual", owner_token="b", now=now)
        self.assertEqual(second["generation"], first["generation"] + 1)

    def test_freeze_blocks_claims_and_old_owner_cannot_release_new_generation(self):
        now = timezone.now()
        owner = acquire_owner(owner_kind="daemon", owner_token="a", now=now)
        self.assertTrue(claim_admission(owner_token="a", generation=owner["generation"], now=now))
        self.assertTrue(freeze_claims(owner_token="a", generation=owner["generation"], reason="stalled", now=now))
        self.assertFalse(claim_admission(owner_token="a", generation=owner["generation"], now=now))
        release_owner(owner_token="a", generation=owner["generation"], now=now)
        replacement = recover_frozen_lane(
            evidence={"source": "supervisor", "state": "child_exited"},
            owner_kind="manual", owner_token="b", now=now + timedelta(seconds=901),
        )
        self.assertIsNotNone(replacement)
        self.assertFalse(release_owner(owner_token="a", generation=owner["generation"], now=now + timedelta(seconds=92)))
        state = IgWorkerLaneState.objects.get(lane_key="conversation_analysis")
        self.assertEqual(state.owner_token, "b")
        self.assertEqual(state.freeze_generation, owner["generation"])

    def test_renew_requires_exact_generation(self):
        owner = acquire_owner(owner_kind="daemon", owner_token="a")
        self.assertFalse(renew_owner(owner_token="a", generation=owner["generation"] + 1))
        self.assertTrue(renew_owner(owner_token="a", generation=owner["generation"]))

    def test_expired_owner_cannot_revive_or_renew_frozen_generation(self):
        now = timezone.now()
        owner = acquire_owner(owner_kind="daemon", owner_token="expired", lease_seconds=1, now=now)
        self.assertFalse(renew_owner(
            owner_token="expired", generation=owner["generation"],
            now=now + timedelta(seconds=2),
        ))
        replacement = acquire_owner(
            owner_kind="manual", owner_token="replacement", now=now + timedelta(seconds=2),
        )
        self.assertIsNotNone(replacement)
        self.assertFalse(renew_owner(
            owner_token="expired", generation=owner["generation"],
            now=now + timedelta(seconds=3),
        ))
        self.assertTrue(freeze_claims(
            owner_token="replacement", generation=replacement["generation"],
            reason="test", now=now + timedelta(seconds=2),
        ))
        self.assertFalse(renew_owner(
            owner_token="replacement", generation=replacement["generation"],
            now=now + timedelta(seconds=3),
        ))

    def test_frozen_lane_requires_cooperative_recovery_before_replacement(self):
        now = timezone.now()
        owner = acquire_owner(owner_kind="daemon", owner_token="a", lease_seconds=1, now=now)
        freeze_claims(owner_token="a", generation=owner["generation"], reason="stall", now=now)
        replacement = acquire_owner(owner_kind="manual", owner_token="b", now=now + timedelta(seconds=2))
        self.assertIsNone(replacement)
        state = IgWorkerLaneState.objects.get(lane_key="conversation_analysis")
        self.assertTrue(state.claim_frozen)
        self.assertEqual(state.freeze_generation, owner["generation"])

    def test_cooperative_recovery_requires_evidence_and_reclaims_only_old_generation(self):
        now = timezone.now()
        owner = acquire_owner(owner_kind="daemon", owner_token="old", lease_seconds=1, now=now)
        self.assertTrue(freeze_claims(
            owner_token=owner["owner_token"],
            generation=owner["generation"],
            reason="stalled",
            now=now,
        ))
        client = IgClient.objects.create(igsid="analysis-lane-recovery")
        old_job = IgConversationAnalysisJob.objects.create(
            client=client,
            status=IgConversationAnalysisJob.Status.PROCESSING,
            lease_token="old-job",
            lease_until=now - timedelta(seconds=1),
            claim_generation=owner["generation"],
            due_at=now,
            next_attempt_at=now,
        )
        future_job = IgConversationAnalysisJob.objects.create(
            client=IgClient.objects.create(igsid="analysis-lane-future"),
            status=IgConversationAnalysisJob.Status.PROCESSING,
            lease_token="future-job",
            lease_until=now - timedelta(seconds=1),
            claim_generation=owner["generation"] + 1,
            due_at=now,
            next_attempt_at=now,
        )
        self.assertIsNone(recover_frozen_lane(evidence=None, now=now + timedelta(seconds=901)))
        recovered = recover_frozen_lane(
            evidence={"source": "supervisor", "state": "stalled"},
            owner_kind="daemon",
            owner_token="new",
            now=now + timedelta(seconds=901),
        )
        self.assertEqual(recovered["reclaimed"], 1)
        self.assertEqual(recovered["previous_generation"], owner["generation"])
        old_job.refresh_from_db()
        future_job.refresh_from_db()
        self.assertEqual(old_job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(old_job.lease_token, "")
        self.assertEqual(future_job.status, IgConversationAnalysisJob.Status.PROCESSING)
        self.assertEqual(future_job.lease_token, "future-job")
        state = IgWorkerLaneState.objects.get(lane_key="conversation_analysis")
        self.assertFalse(state.claim_frozen)
        self.assertEqual(state.owner_token, "new")
