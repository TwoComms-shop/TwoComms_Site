"""A completed human answer is health ownership only with exact native proof."""
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management import tests_ig_manager_wait_lifecycle as fixtures
from management.models import AdminAuditLog, IgFollowUpTask, IgTurnRevisionSource
from management.services.ig_lane_health import _manual_owned, _revision_lane
from management.services.ig_response_debt import record_reply_debt


@override_settings(GOOGLE_INDEXING_ENABLED=False,
    IG_REVISION_EXECUTION_ENABLED=True, IG_REVISION_EXECUTION_CUTOVER_AT="2026-01-01T00:00:00+00:00")
class HumanSettlementHealthTests(TransactionTestCase):
    setUp = fixtures.ManagerWaitLifecycleTests.setUp
    _physical_http = fixtures.ManagerWaitLifecycleTests._physical_http
    _revision = fixtures.ManagerWaitLifecycleTests._revision
    _new_source = fixtures.ManagerWaitLifecycleTests._new_source
    _send = fixtures.ManagerWaitLifecycleTests._send

    def settled(self):
        revision = self._revision()
        task = record_reply_debt(revision, "generation_semantic_rejected")
        command = self._send()
        self.assertEqual(command.state, "sent")
        revision.refresh_from_db(); task.refresh_from_db()
        self.assertEqual(task.status, "completed")
        self.assertEqual(revision.recovery_code, "manager_reply_confirmed")
        return revision, task, command

    def assert_health(self, revision, *, owned):
        with patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("health provider IO")):
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(_manual_owned(revision), owned)
                for limit in (None, 1):
                    lane = _revision_lane(now=timezone.now(), settings_row=self.settings,
                        allowed=set(), include_progress=False, observation_limit=limit)
                    self.assertEqual(lane["attention_total"], 0 if owned else 1, lane)
                    self.assertEqual(lane["counts"]["manual"], 1 if owned else 0, lane)
                    self.assertEqual(lane["counts"]["attention"], 0 if owned else 1, lane)
            self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))

    def test_actual_confirmed_human_settlement_is_owned_without_bot_delivery(self):
        revision, task, command = self.settled()
        self.assertFalse(revision.delivery_effects.exists())
        self.assertEqual(task.manager_context["reply_resolution"]["command_id"], command.pk)
        self.assert_health(revision, owned=True)

    def test_multipart_native_settlement_requires_all_physical_parts_sent(self):
        from management.ig_human_reply_models import HumanReplyPart
        from management.services.ig_human_reply import create_human_reply_command, dispatch_human_reply_command

        revision = self._revision()
        task = record_reply_debt(revision, "generation_semantic_rejected")
        def physical(*args, **kwargs):
            self.provider_mid = f"multipart-{self.http.call_count}"
            return self._physical_http(*args, **kwargs)
        self.http.side_effect = physical
        accepted = create_human_reply_command(self.customer.pk, actor=self.actor, text="A" * 1500)
        command = dispatch_human_reply_command(accepted.command.pk)
        self.assertEqual(command.state, "sent")
        self.assertEqual(len(command.provider_message_ids), 2)
        revision.refresh_from_db(); task.refresh_from_db()
        self.assertEqual(task.status, "completed")
        self.assert_health(revision, owned=True)
        HumanReplyPart.objects.filter(command=command, ordinal=1).update(state="unknown")
        self.assert_health(revision, owned=False)

    def test_forged_completion_without_native_settlement_audit_stays_attention(self):
        revision, task, _command = self.settled()
        AdminAuditLog.objects.filter(action="ig_reply_debt_manager_replied").delete()
        self.assert_health(revision, owned=False)

    def test_unknown_command_cannot_inherit_old_resolution(self):
        revision, _task, command = self.settled()
        type(command).objects.filter(pk=command.pk).update(state="unknown")
        self.assert_health(revision, owned=False)

    def test_unrelated_source_command_does_not_resolve_this_revision(self):
        revision, _task, command = self.settled()
        type(command).objects.filter(pk=command.pk).update(context_message=self._new_source())
        self.assert_health(revision, owned=False)

    def test_wrong_physical_recipient_does_not_resolve_this_revision(self):
        revision, _task, command = self.settled()
        type(command).objects.filter(pk=command.pk).update(recipient_igsid="unrelated-recipient")
        self.assert_health(revision, owned=False)

    def test_revision_source_binding_must_still_match_physical_context(self):
        revision, _task, _command = self.settled()
        IgTurnRevisionSource.objects.create(revision=revision, message=self._new_source(),
            ordinal=2, role="user", source_digest="additional-source")
        self.assert_health(revision, owned=False)

    def test_forged_resolution_mid_does_not_match_audit_or_command(self):
        revision, task, _command = self.settled()
        context = task.manager_context
        context["reply_resolution"]["provider_message_ids"] = ["invented-mid"]
        IgFollowUpTask.objects.filter(pk=task.pk).update(manager_context=context)
        self.assert_health(revision, owned=False)
