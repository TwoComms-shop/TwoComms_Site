"""Expired reply recovery must remain reachable behind old ingress shadows."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixtures
from management import tests_ig_revision_recovery as recovery_fixtures
from management.models import (
    IgClient, IgCustomerTurn, IgCustomerTurnRevision, IgFollowUpTask,
    IgTurnMessage, InstagramBotMessage,
)
from management.services.ig_revision_commerce import INGRESS_SOURCE_PRODUCER
from management.services.ig_revision_execution import (
    due_revision_ids, expired_revision_debt_ids, record_expired_revision_debt,
)
from management.services.ig_turn_revisions import create_collecting_revision


@override_settings(
    IG_REVISION_EXECUTION_ENABLED=True,
    IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00",
    GOOGLE_INDEXING_ENABLED=False,
)
class RevisionDebtStarvationTests(TransactionTestCase):
    setUp = recovery_fixtures.RevisionRecoveryTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _successful_proposal = recovery_fixtures.RevisionRecoveryTests._successful_proposal

    def _collecting(self, suffix, *, status="pending", source_owned=True, now=None):
        now = now or timezone.now() - timedelta(days=2)
        client = IgClient.objects.create(igsid=f"debt-shadow-{suffix}")
        source = InstagramBotMessage.objects.create(
            client=client, sender_id=client.igsid, role="user", source="webhook",
            provider_namespace="instagram_login:owner-1", mid=f"debt-shadow-{suffix}",
            text="Яка ціна?", status="pending", provider_created_at=now,
        )
        turn = IgCustomerTurn.objects.create(
            client=client, primary_source_message=source,
            window_started_at=now, window_deadline=now,
        )
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        revision = create_collecting_revision(turn, [source], now=now, bypass_quiet=True).revision
        updates = {"active_slot": None, "created_at": now}
        if source_owned:
            revision.action_receipts = {"commerce_source_observations": {"producer": INGRESS_SOURCE_PRODUCER}}
            revision.save(update_fields=["action_receipts"])
        IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(**updates)
        InstagramBotMessage.objects.filter(pk=source.pk).update(status=status)
        revision.refresh_from_db()
        return source, revision

    def test_one_worker_pass_reaches_saved_proposal_after_twenty_answered_ingress_shadows(self):
        from management.services.ig_revision_live import process_pending_revisions

        self._successful_proposal()
        shadows = [self._collecting(str(index), status="done") for index in range(20)]
        source_ids = [source.pk for source, _revision in shadows] + [self.source.pk]
        before = list(InstagramBotMessage.objects.filter(pk__in=source_ids).order_by("pk").values_list("pk", "status", "attempts", "send_state"))
        proposal_digest = self.revision.generation_proposal_digest
        now = timezone.now() + timedelta(minutes=31)
        self.assertEqual(expired_revision_debt_ids(now=now, limit=15), [self.revision.pk])

        with (
            patch("management.services.ig_revision_live.timezone.now", return_value=now),
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("saved proposal must not regenerate")) as generate,
            patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("expired provider horizon must not send")) as http,
        ):
            process_pending_revisions(self.settings, max_items=15)
            self.revision.refresh_from_db()
            self.assertEqual(self.revision.recovery_state, "manual")
            self.assertEqual(self.revision.recovery_code, "provider_horizon_exhausted")
            process_pending_revisions(self.settings, max_items=15)

        generate.assert_not_called()
        http.assert_not_called()
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.recovery_state, "manual")
        self.assertEqual(self.revision.recovery_code, "provider_horizon_exhausted")
        self.assertEqual(self.revision.generation_proposal_digest, proposal_digest)
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertEqual(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{self.revision.pk}").count(), 1)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual(before, list(InstagramBotMessage.objects.filter(pk__in=source_ids).order_by("pk").values_list("pk", "status", "attempts", "send_state")))
        self.assertEqual(list(IgCustomerTurnRevision.objects.filter(pk__in=[revision.pk for _, revision in shadows]).values_list("state", flat=True)), ["collecting"] * 20)

    def test_unsealed_source_receipts_require_exact_pending_unsent_source_set(self):
        now = timezone.now()
        invalid = []
        for kind in ("answered", "sending", "count", "zero_count", "terminal"):
            source, revision = self._collecting(kind)
            if kind == "answered":
                InstagramBotMessage.objects.filter(pk=source.pk).update(status="done")
            elif kind == "sending":
                InstagramBotMessage.objects.filter(pk=source.pk).update(send_state="sent")
            elif kind == "count":
                IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(source_count=2)
            elif kind == "zero_count":
                IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(source_count=0)
            else:
                IgCustomerTurn.objects.filter(pk=revision.turn_id).update(claim_state="processed", terminal_reason="replied")
            invalid.append(revision.pk)
            self.assertEqual(record_expired_revision_debt(revision.pk, now=now), "legacy_shadow_not_owed")
        selected = expired_revision_debt_ids(now=now, limit=50)
        self.assertFalse(set(invalid).intersection(selected))
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_pending_source_owner_survives_cutover_and_rollback_and_records_debt_once(self):
        source, revision = self._collecting("pending-owner")
        cutoff = timezone.now() - timedelta(days=1)
        now = timezone.now()
        self.assertIn(revision.pk, expired_revision_debt_ids(now=now, cutover_at=cutoff))
        self.assertIn(revision.pk, expired_revision_debt_ids(now=now, cutover_at=cutoff, owned_only=True))
        self.assertEqual(record_expired_revision_debt(revision.pk, now=now, cutover_at=cutoff), "preparation_expired")
        self.assertNotIn(revision.pk, expired_revision_debt_ids(now=now, cutover_at=cutoff))
        record_expired_revision_debt(revision.pk, now=now, cutover_at=cutoff)
        self.assertEqual(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{revision.pk}").count(), 1)
        source.refresh_from_db()
        self.assertEqual(source.status, "pending")

    def test_old_unowned_shadow_is_excluded_but_fresh_pending_debt_is_preserved(self):
        _old_source, old = self._collecting("old-unowned", source_owned=False)
        _fresh_source, fresh = self._collecting("fresh-unowned", source_owned=False, now=timezone.now() - timedelta(minutes=5))
        cutoff = timezone.now() - timedelta(days=1)
        ids = expired_revision_debt_ids(cutover_at=cutoff)
        self.assertNotIn(old.pk, ids)
        self.assertIn(fresh.pk, ids)

    def test_expired_unsealed_claim_retains_pending_debt_across_cutover_and_rollback(self):
        source, revision = self._collecting("claimed-before-cutover", source_owned=False)
        now = timezone.now()
        cutoff = now - timedelta(days=1)
        IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(
            state="claimed", claim_token="crashed-preparation", lease_until=now - timedelta(minutes=1),
        )
        self.assertIn(revision.pk, expired_revision_debt_ids(now=now, cutover_at=cutoff, owned_only=True))
        self.assertEqual(record_expired_revision_debt(revision.pk, now=now, cutover_at=cutoff), "preparation_expired")
        self.assertNotIn(revision.pk, expired_revision_debt_ids(now=now, cutover_at=cutoff, owned_only=True))
        source.refresh_from_db()
        self.assertEqual(source.status, "pending")

    def test_actual_expired_claim_precedes_a_full_batch_of_valid_collecting_debts(self):
        for index in range(20):
            self._collecting(f"owed-{index}")
        self._successful_proposal()
        now = timezone.now() + timedelta(minutes=31)
        self.assertEqual(expired_revision_debt_ids(now=now, limit=1), [self.revision.pk])
        # The ordinary preparation selector has a deadline fence; these old
        # collecting rows cannot re-enter the generation queue either.
        self.assertEqual(due_revision_ids(now=now), [])
