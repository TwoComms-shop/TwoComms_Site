from datetime import timedelta
import os
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import (
    IgClient, IgCustomerTurn, IgCustomerTurnRevision, IgFollowUpTask,
    IgTurnMessage, InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_revision_execution import (
    due_revision_ids, expired_revision_debt_ids, record_expired_revision_debt,
)
from management.services.ig_revision_live import revision_execution_enabled
from management.services.ig_revision_rollout import (
    parse_cutover, revision_execution_rollout,
)
from management.services.ig_turn_revisions import create_collecting_revision


PAST_CUTOVER = "2000-01-01T00:00:00+00:00"


class RevisionRolloutTests(TransactionTestCase):
    reset_sequences = True

    def _revision(self, name, *, now=None, status="pending", turn_state="open"):
        now = now or timezone.now()
        client = IgClient.objects.create(igsid=name)
        source = InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            provider_namespace="instagram_login:owner-1",
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            text="question",
            mid=f"{name}-source",
            status=status,
        )
        turn = IgCustomerTurn.objects.create(
            client=client,
            primary_source_message=source,
            window_started_at=now,
            window_deadline=now,
            claim_state=turn_state,
            claimed_at=(now - timedelta(hours=1)) if turn_state == "claimed" else None,
        )
        IgTurnMessage.objects.create(
            turn=turn, message=source, ordinal=1, role="user"
        )
        revision = create_collecting_revision(
            turn, [source], now=now, bypass_quiet=True
        ).revision
        return client, source, turn, revision

    def test_cutover_parser_requires_aware_iso_and_gate_fails_closed(self):
        now = timezone.now()
        self.assertIsNone(parse_cutover(""))
        self.assertIsNone(parse_cutover("2026-09-08T12:00:00"))
        self.assertIsNone(parse_cutover("not-a-date"))
        missing = revision_execution_rollout(
            now=now, flag_value=True, cutover_value=""
        )
        invalid = revision_execution_rollout(
            now=now, flag_value=True, cutover_value="not-a-date"
        )
        future = revision_execution_rollout(
            now=now,
            flag_value=True,
            cutover_value=(now + timedelta(seconds=1)).isoformat(),
        )
        enabled = revision_execution_rollout(
            now=now,
            flag_value=True,
            cutover_value=(now - timedelta(seconds=1)).isoformat(),
        )
        self.assertEqual(missing.reason, "cutover_missing_or_invalid")
        self.assertFalse(invalid.enabled)
        self.assertEqual(future.reason, "cutover_pending")
        self.assertTrue(enabled.enabled)

    @override_settings(IG_REVISION_EXECUTION_ENABLED=True)
    def test_requested_flag_without_cutover_does_not_enable_execution(self):
        with patch.dict(os.environ, {"IG_REVISION_EXECUTION_CUTOVER_AT": ""}):
            self.assertFalse(revision_execution_enabled())

    def test_cutover_excludes_old_shadow_but_preserves_owned_revision(self):
        now = timezone.now()
        cutoff = now - timedelta(minutes=1)
        _client, _source, _turn, revision = self._revision("rollout-due", now=now)
        IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(
            created_at=cutoff - timedelta(seconds=1)
        )
        self.assertNotIn(
            revision.pk, due_revision_ids(now=now, cutover_at=cutoff)
        )
        IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(
            media_prepare_deadline=now
        )
        self.assertIn(
            revision.pk, due_revision_ids(now=now, cutover_at=cutoff)
        )

    def test_expired_old_or_terminal_shadow_cannot_create_rollout_debt(self):
        now = timezone.now()
        cutoff = now - timedelta(minutes=1)
        created = cutoff - timedelta(minutes=1)
        _client, source, _turn, old = self._revision(
            "rollout-old-debt", now=now - timedelta(minutes=5)
        )
        IgCustomerTurnRevision.objects.filter(pk=old.pk).update(created_at=created)
        self.assertNotIn(
            old.pk,
            expired_revision_debt_ids(now=now, cutover_at=cutoff),
        )

        _client, terminal_source, _turn, terminal = self._revision(
            "rollout-terminal-debt", now=now - timedelta(minutes=5)
        )
        IgCustomerTurnRevision.objects.filter(pk=terminal.pk).update(
            created_at=cutoff + timedelta(seconds=1)
        )
        InstagramBotMessage.objects.filter(pk=terminal_source.pk).update(status="done")
        self.assertNotIn(
            terminal.pk,
            expired_revision_debt_ids(now=now, cutover_at=cutoff),
        )
        self.assertEqual(
            record_expired_revision_debt(
                terminal.pk, now=now, cutover_at=cutoff
            ),
            "legacy_shadow_not_owed",
        )
        self.assertFalse(IgFollowUpTask.objects.exists())

    @override_settings(
        IG_REVISION_EXECUTION_ENABLED=True,
        IG_REVISION_EXECUTION_CUTOVER_AT=PAST_CUTOVER,
    )
    def test_enabled_worker_keeps_hygiene_legacy_only(self):
        from management.services import instagram_bot as bot

        now = timezone.now()
        _client, _source, legacy_turn, _revision = self._revision(
            "rollout-legacy-claim", now=now, status="done", turn_state="claimed"
        )
        _client, _source, owned_turn, owned_revision = self._revision(
            "rollout-owned-claim", now=now, status="done", turn_state="claimed"
        )
        IgCustomerTurnRevision.objects.filter(pk=owned_revision.pk).update(
            media_prepare_deadline=now
        )
        settings_row = InstagramBotSettings.objects.create(pk=1, is_enabled=True)
        with (
            patch.object(bot, "_maybe_purge_expired_private_media"),
            patch.object(bot, "_send_rate_limit_backoff_active", return_value=False),
            patch.object(bot, "reclaim_stale_processing", return_value=0) as reclaim,
            patch(
                "management.services.ig_revision_live.process_revision_finalizations",
                return_value=0,
            ),
            patch(
                "management.services.ig_revision_live.process_pending_revisions",
                return_value=0,
            ),
        ):
            self.assertEqual(bot.process_pending(settings_row, max_items=1), 0)
        reclaim.assert_called_once_with()
        legacy_turn.refresh_from_db()
        owned_turn.refresh_from_db()
        self.assertEqual(legacy_turn.claim_state, IgCustomerTurn.ClaimState.PROCESSED)
        self.assertEqual(owned_turn.claim_state, IgCustomerTurn.ClaimState.CLAIMED)
