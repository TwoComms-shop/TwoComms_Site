from datetime import timedelta
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import (
    AdminAuditLog, IgClient, IgCustomerTurn, IgCustomerTurnRevision,
    IgTurnMessage, InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_manual_resume import resume_client_automation
from management.services.ig_revision_execution import due_revision_ids, prepare_revision
from management.services.ig_turn_revisions import create_collecting_revision, create_refresh_successor


@override_settings(IG_REVISION_EXECUTION_ENABLED=True)
class ManualRevisionIntegrationTests(TransactionTestCase):
    def setUp(self):
        transport = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        transport.start()
        self.addCleanup(transport.stop)
        self.settings = InstagramBotSettings.objects.create(pk=1, is_enabled=True, ig_user_id="manual-owner")
        self.operator = get_user_model().objects.create_superuser(username="manual-operator", password="local-fixture")
        self.customer = IgClient.objects.create(igsid="17840000000801", bot_paused=True, manager_takeover=True, reply_permission_epoch=0)
        at = timezone.now() - timedelta(minutes=3)
        self.source = InstagramBotMessage.objects.create(
            client=self.customer, sender_id=self.customer.igsid, mid="manual-question",
            source="webhook", provider_namespace="instagram_login:manual-owner",
            role="user", text="Підкажіть, який розмір мені обрати?", status="done", provider_created_at=at,
        )
        self.turn = IgCustomerTurn.objects.create(
            client=self.customer, primary_source_message=self.source,
            window_started_at=at, window_deadline=at,
        )
        IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=1, role="user")
        self.old = create_collecting_revision(self.turn, [self.source], now=at).revision
        self.turn.claim_state = self.turn.ClaimState.PROCESSED
        self.turn.terminal_reason = self.turn.TerminalReason.NO_REPLY_NEEDED
        self.turn.save(update_fields=["claim_state", "terminal_reason"])

    def test_one_audited_resume_is_claimable_without_reopening_old_history(self):
        old_deadline = self.old.overall_deadline
        result = resume_client_automation(self.customer.pk, actor=self.operator)
        self.assertTrue(result.successor_created, result.successor_reason)
        self.assertEqual(result.permission_epoch, 1)
        self.assertIn(result.successor_revision_id, due_revision_ids())
        prepared = prepare_revision(result.successor_revision_id, lambda **kwargs: None)
        self.assertTrue(prepared.ready, prepared.reason)
        repeated = resume_client_automation(self.customer.pk, actor=self.operator)
        self.assertFalse(repeated.changed)
        self.assertEqual(repeated.permission_epoch, 1)
        self.assertEqual(IgCustomerTurnRevision.objects.filter(origin="manual_resume").count(), 1)
        self.assertEqual(AdminAuditLog.objects.filter(action="ig_bot.manual_resume").count(), 1)
        self.old.refresh_from_db()
        self.source.refresh_from_db()
        self.turn.refresh_from_db()
        self.assertEqual(self.old.overall_deadline, old_deadline)
        self.assertEqual(self.source.status, "done")
        self.assertEqual(self.turn.terminal_reason, self.turn.TerminalReason.NO_REPLY_NEEDED)

    def test_tampered_audit_cannot_bypass_legacy_terminal_guard(self):
        result = resume_client_automation(self.customer.pk, actor=self.operator)
        self.assertTrue(result.successor_created, result.successor_reason)
        AdminAuditLog.objects.filter(action="ig_bot.manual_resume").update(after={"permission_epoch": 2})
        prepared = prepare_revision(result.successor_revision_id, lambda **kwargs: None)
        self.assertFalse(prepared.ready)
        self.assertEqual(prepared.reason, "legacy_turn_terminal")

    def test_one_policy_refresh_preserves_manual_authorization_and_deadline(self):
        result = resume_client_automation(self.customer.pk, actor=self.operator)
        self.assertTrue(result.successor_created, result.successor_reason)
        prepared = prepare_revision(result.successor_revision_id, lambda **kwargs: None)
        self.assertTrue(prepared.ready, prepared.reason)
        manual = IgCustomerTurnRevision.objects.get(pk=result.successor_revision_id)
        refreshed = create_refresh_successor(manual.pk, prepared.execution_token, reason="publication_changed")
        self.assertTrue(refreshed.created, refreshed.reason)
        self.assertEqual(refreshed.revision.overall_deadline, manual.overall_deadline)
        self.assertEqual(refreshed.revision.action_receipts["manual_resume_authorization"], manual.action_receipts["manual_resume_authorization"])
        self.assertIn(refreshed.revision.pk, due_revision_ids())
        second = prepare_revision(refreshed.revision.pk, lambda **kwargs: None)
        self.assertTrue(second.ready, second.reason)
