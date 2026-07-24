from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from management.models import (
    IgClient,
    IgConversationAnalysisSnapshot,
    IgFollowUpTask,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services.ig_opt_out_backfill import reconcile_opt_out_backfill


class IgOptOutBackfillTests(TestCase):
    def setUp(self):
        self.client = IgClient.objects.create(igsid="opt-out-backfill-client")
        settings = InstagramBotSettings.load()
        settings.opt_out_backfill_cursor = 0
        settings.save(update_fields=["opt_out_backfill_cursor"])

    def message(self, text, *, created_at=None):
        message = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text=text,
            status=InstagramBotMessage.Status.DONE,
        )
        if created_at:
            InstagramBotMessage.objects.filter(pk=message.pk).update(created_at=created_at)
            message.created_at = created_at
        return message

    @patch("management.services.ig_opt_out_backfill.cancel_pending")
    def test_backfill_sets_durable_stop_and_cancels_followups(self, cancel_pending):
        message = self.message("STOP")
        task = IgFollowUpTask.objects.create(
            client=self.client,
            due_at=timezone.now() + timedelta(days=1),
        )

        result = reconcile_opt_out_backfill(limit=10, now=timezone.now())

        self.assertEqual(result["updated"], 1)
        self.client.refresh_from_db()
        self.assertEqual(self.client.opt_out_message_id, message.pk)
        self.assertIsNotNone(self.client.opted_out_at)
        self.assertTrue(self.client.bot_paused)
        self.assertEqual(self.client.paused_reason, "opt_out")
        cancel_pending.assert_called_once_with(self.client, reason="opt_out")
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.PENDING)

    def test_opt_in_after_legacy_stop_is_not_overwritten(self):
        old = timezone.now() - timedelta(days=2)
        self.message("unsubscribe", created_at=old)
        self.client.opted_in_at = timezone.now() - timedelta(days=1)
        self.client.save(update_fields=["opted_in_at"])

        result = reconcile_opt_out_backfill(limit=10, now=timezone.now())

        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["already_opted_in"], 1)
        self.client.refresh_from_db()
        self.assertIsNone(self.client.opted_out_at)

    def test_ambiguous_snapshot_is_reported_without_mutation(self):
        IgConversationAnalysisSnapshot.objects.create(
            client=self.client,
            dedupe_key="ambiguous-opt-out-snapshot",
            score_band=IgConversationAnalysisSnapshot.Band.LOST,
            interaction_type=IgConversationAnalysisSnapshot.InteractionType.OPT_OUT,
            analysis_model="rules",
        )

        result = reconcile_opt_out_backfill(limit=10, now=timezone.now())

        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["ambiguous"], 1)
        self.client.refresh_from_db()
        self.assertIsNone(self.client.opted_out_at)

    def test_cursor_and_repeat_are_idempotent(self):
        self.message("STOP")

        first = reconcile_opt_out_backfill(limit=10, now=timezone.now())
        second = reconcile_opt_out_backfill(limit=10, now=timezone.now())

        self.assertEqual(first["updated"], 1)
        self.assertEqual(second["updated"], 0)
        self.assertGreaterEqual(second["skipped_existing"], 1)
