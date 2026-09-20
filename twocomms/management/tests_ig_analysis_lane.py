from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from management.models import IgWorkerLaneState
from management.services.ig_analysis_lane import (
    acquire_owner,
    claim_admission,
    freeze_claims,
    release_owner,
    renew_owner,
)


class AnalysisLaneFenceTests(TestCase):
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
        replacement = acquire_owner(owner_kind="manual", owner_token="b", now=now + timedelta(seconds=91))
        self.assertIsNotNone(replacement)
        self.assertFalse(release_owner(owner_token="a", generation=owner["generation"], now=now + timedelta(seconds=92)))
        state = IgWorkerLaneState.objects.get(lane_key="conversation_analysis")
        self.assertEqual(state.owner_token, "b")
        self.assertEqual(state.freeze_generation, owner["generation"])

    def test_renew_requires_exact_generation(self):
        owner = acquire_owner(owner_kind="daemon", owner_token="a")
        self.assertFalse(renew_owner(owner_token="a", generation=owner["generation"] + 1))
        self.assertTrue(renew_owner(owner_token="a", generation=owner["generation"]))

    def test_expired_owner_can_be_replaced_but_freeze_provenance_remains(self):
        now = timezone.now()
        owner = acquire_owner(owner_kind="daemon", owner_token="a", lease_seconds=1, now=now)
        freeze_claims(owner_token="a", generation=owner["generation"], reason="stall", now=now)
        replacement = acquire_owner(owner_kind="manual", owner_token="b", now=now + timedelta(seconds=2))
        state = IgWorkerLaneState.objects.get(lane_key="conversation_analysis")
        self.assertEqual(replacement["generation"], owner["generation"] + 1)
        self.assertTrue(state.claim_frozen)
        self.assertEqual(state.freeze_generation, owner["generation"])
