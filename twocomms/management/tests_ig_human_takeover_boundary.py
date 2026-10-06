"""Human takeover cleanup, rollback and the shared physical permission edge."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import skipUnless
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management.models import (
    AdminAuditLog, HumanReplyCommand, IgAiReplyRecoveryJob, IgClient,
    IgCommerceSelectionSession, IgCommerceTurnDecision, IgCustomerTurn,
    IgCustomerTurnRevision, IgFollowUpTask, IgRevisionDeliveryEffect,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services import ig_human_reply as human, ig_permission_transitions as transitions
from management.services.ig_reply_boundary import (
    ReplyBoundaryTimeout, capture_reply_permission, customer_send_boundary, pause_reply_boundary,
)
from management.services.instagram_bot import ingress_provider_namespace


class _HumanFixture:
    def setUp(self):
        super().setUp()
        directory = TemporaryDirectory(prefix="twc-human-takeover-")
        self.addCleanup(directory.cleanup)
        self.permission_lock = str(Path(directory.name)/"permission.lock")
        boundary_patch = patch("management.services.ig_reply_boundary.pause_reply_boundary",
            side_effect=lambda **kwargs: pause_reply_boundary(lock_path=self.permission_lock, **kwargs))
        boundary_patch.start()
        self.addCleanup(boundary_patch.stop)
        self.actor = get_user_model().objects.create_superuser(
            username="takeover-boundary-actor", email="takeover@example.test", password="x")
        self.settings_row = InstagramBotSettings.load()
        self.settings_row.is_enabled = True
        self.settings_row.allowed_senders = ""
        self.settings_row.ig_user_id = "999999"
        self.settings_row.page_id = "999999"
        self.settings_row.save(update_fields=["is_enabled", "allowed_senders", "ig_user_id", "page_id"])
        self.customer = IgClient.objects.create(igsid="123456789", reply_permission_epoch=7)
        self.at = timezone.now()
        self.context = self.message("current", text="Підкажіть ціну", status="pending")
        self.namespace = ingress_provider_namespace(self.settings_row)

    def message(self, label, *, client=None, **overrides):
        client = client or self.customer
        values = dict(client=client, sender_id=client.igsid, role="user", text=label,
            mid=f"takeover-{label}", status="done", provider_created_at=self.at-timedelta(minutes=2))
        values.update(overrides)
        return InstagramBotMessage.objects.create(**values)

    def create(self, **overrides):
        values = dict(actor=self.actor, text="Зараз підкажу.", context_message_id=self.context.pk)
        values.update(overrides)
        return human.create_human_reply_command(self.customer.pk, **values)


@override_settings(IG_MEMORY_GENERATION_ENABLED=False, IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=False)
class HumanTakeoverBoundaryTests(_HumanFixture, TestCase):
    @override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False)
    def test_http_api_reports_retryable_transition_failure_without_dispatch(self):
        from django.urls import reverse
        self.client.force_login(self.actor)
        url = reverse("management_bot_client_human_reply_api", args=[self.customer.pk])
        for code in ("takeover_boundary_busy", "takeover_cleanup_failed", "takeover_transition_failed"):
            with self.subTest(code=code), patch.object(human, "create_human_reply_command",
                    side_effect=human.HumanReplyRejected(code)), patch.object(human, "dispatch_human_reply_command") as dispatch:
                response = self.client.post(url, {"text": "Перевірена відповідь менеджера."}, content_type="application/json")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"success": False, "code": code, "retryable": True})
                dispatch.assert_not_called()

    def test_takeover_cancels_only_exact_unstarted_work_and_preserves_receipt_owned_rows(self):
        pending = self.context
        processing = self.message("processing", status="processing")
        sending = self.message("sending", status="processing", send_state="sending")
        unknown = self.message("unknown", status="failed", send_state="unknown")
        sent = self.message("sent", send_state="sent")
        self.context = sent  # Current UI context, independent of older work.
        jobs = [IgAiReplyRecoveryJob.objects.create(client=self.customer, source_message=row,
            dedupe_key=f"takeover-recovery-{row.pk}", status=status, sending_started_at=started)
            for row, status, started in ((pending, "pending", None), (processing, "processing", None),
                                        (sending, "sending", self.at))]
        uncertain_reply = self.message("uncertain-reply", role="model", send_state="unknown")
        protected = IgAiReplyRecoveryJob.objects.create(client=self.customer, source_message=unknown,
            reply_message=uncertain_reply, dedupe_key="takeover-protected", status="pending")
        session = IgCommerceSelectionSession.objects.create(client=self.customer, generation=1)
        decision = IgCommerceTurnDecision.objects.create(source_message=pending, session=session,
            delivery_required=True, delivery_state="pending")
        crossed = IgCommerceTurnDecision.objects.create(source_message=unknown, session=session,
            delivery_required=True, delivery_state="unknown", attempts=1, delivery_started_at=self.at)
        followup = IgFollowUpTask.objects.create(client=self.customer, due_at=self.at+timedelta(hours=1),
                                                kind=IgFollowUpTask.Kind.QUALIFICATION)
        manager = IgFollowUpTask.objects.create(client=self.customer, due_at=self.at,
                                               kind=IgFollowUpTask.Kind.MANAGER_TASK)
        other = IgClient.objects.create(igsid="987654321")
        foreign = self.message("foreign", client=other, status="pending")
        self.customer.next_followup_at = self.at+timedelta(hours=1)
        self.customer.save(update_fields=["next_followup_at"])
        with patch("management.services.instagram_bot._provider_http") as http:
            result = self.create()
        http.assert_not_called()
        for row in (pending, processing, sending, unknown, sent, *jobs, protected, uncertain_reply,
                    decision, crossed, followup, manager, foreign, self.customer):
            row.refresh_from_db()
        self.assertEqual([pending.status, processing.status], ["done", "done"])
        self.assertEqual((sending.status, sending.send_state), ("processing", "sending"))
        self.assertEqual((unknown.status, unknown.send_state), ("failed", "unknown"))
        self.assertEqual(sent.send_state, "sent")
        self.assertEqual([job.status for job in jobs], ["cancelled", "cancelled", "sending"])
        self.assertEqual(protected.status, "pending")
        self.assertEqual(uncertain_reply.send_state, "unknown")
        self.assertEqual(decision.delivery_state, "not_required")
        self.assertEqual(crossed.delivery_state, "unknown")
        self.assertEqual((followup.status, manager.status, foreign.status), ("cancelled", "pending", "pending"))
        self.assertIsNone(self.customer.next_followup_at)
        self.assertEqual(self.customer.reply_permission_epoch, 8)
        self.assertEqual(result.command.state, "pending")
        audit = AdminAuditLog.objects.get(action="ig_bot.human_reply_command_created")
        self.assertEqual(audit.before, dict(bot_paused=False, manager_takeover=False, permission_epoch=7))

    def test_cleanup_exception_after_partial_writes_rolls_back_entire_transition(self):
        task = IgFollowUpTask.objects.create(client=self.customer, due_at=self.at,
                                            kind=IgFollowUpTask.Kind.QUALIFICATION)
        original = transitions.cancel_client_unstarted_automation
        def fail_after_cleanup(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("private backend failure detail")
        with patch.object(transitions, "cancel_client_unstarted_automation", side_effect=fail_after_cleanup), patch(
            "management.services.instagram_bot._provider_http") as http:
            with self.assertRaises(human.HumanReplyRejected) as caught:
                self.create()
        self.assertEqual(caught.exception.code, "takeover_cleanup_failed")
        self.assertNotIn("private", str(caught.exception))
        http.assert_not_called()
        self.customer.refresh_from_db(); self.context.refresh_from_db(); task.refresh_from_db()
        self.assertFalse(self.customer.bot_paused or self.customer.manager_takeover)
        self.assertEqual(self.customer.reply_permission_epoch, 7)
        self.assertEqual((self.context.status, task.status), ("pending", "pending"))
        self.assertFalse(HumanReplyCommand.objects.exists())
        self.assertFalse(AdminAuditLog.objects.filter(action="ig_bot.human_reply_command_created").exists())

    def test_command_database_and_audit_integrity_failures_are_finite_and_atomic(self):
        targets = (("management.services.ig_human_reply.HumanReplyCommand.objects.create", OperationalError),
                   ("management.services.ig_human_reply.AdminAuditLog.objects.create", IntegrityError))
        for target, error in targets:
            with self.subTest(target=target), patch(target, side_effect=error("sensitive DB detail")), patch(
                "management.services.instagram_bot._provider_http") as http:
                with self.assertRaises(human.HumanReplyRejected) as caught:
                    self.create()
            self.assertEqual(caught.exception.code, "takeover_transition_failed")
            http.assert_not_called()
            self.customer.refresh_from_db(); self.context.refresh_from_db()
            self.assertFalse(self.customer.bot_paused or self.customer.manager_takeover)
            self.assertEqual((self.customer.reply_permission_epoch, self.context.status), (7, "pending"))
            self.assertFalse(HumanReplyCommand.objects.exists())

    def test_busy_boundary_rejects_before_client_locks_or_cleanup_and_duplicate_is_read_only(self):
        operation = uuid.uuid4()
        first = self.create(operation_id=operation)
        # Existing operation is immutable historical recovery, even when a new
        # transition cannot acquire the barrier. No second ownership transition.
        with patch("management.services.ig_reply_boundary.pause_reply_boundary", side_effect=ReplyBoundaryTimeout()), patch.object(
            transitions, "cancel_client_unstarted_automation") as cleanup:
            repeated = self.create(operation_id=operation)
            self.assertTrue(repeated.idempotent)
            self.assertEqual(repeated.command.pk, first.command.pk)
            with self.assertRaises(human.HumanReplyRejected) as caught:
                self.create(operation_id=uuid.uuid4())
        cleanup.assert_not_called()
        self.assertEqual(caught.exception.code, "takeover_boundary_busy")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.reply_permission_epoch, 8)
        self.assertEqual(HumanReplyCommand.objects.count(), 1)

    def test_already_paused_new_command_cleans_pending_work_without_new_epoch_or_false_audit(self):
        self.customer.bot_paused = True
        self.customer.manager_takeover = True
        self.customer.paused_reason = "manager_takeover"
        self.customer.paused_at = self.at
        self.customer.save(update_fields=["bot_paused", "manager_takeover", "paused_reason", "paused_at"])
        task = IgFollowUpTask.objects.create(client=self.customer, due_at=self.at,
                                            kind=IgFollowUpTask.Kind.QUALIFICATION)
        self.create()
        self.customer.refresh_from_db(); task.refresh_from_db()
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(self.customer.reply_permission_epoch, 7)
        audit = AdminAuditLog.objects.get(action="ig_bot.human_reply_command_created")
        self.assertEqual(audit.before, dict(bot_paused=True, manager_takeover=True, permission_epoch=7))

    def test_takeover_invalidates_old_bot_permission_but_human_send_keeps_manager_path(self):
        from management.services import instagram_bot as bot
        old_permission = capture_reply_permission(self.settings_row.pk, self.customer.pk)
        command = self.create().command
        with TemporaryDirectory() as directory, customer_send_boundary(self.settings_row.pk, self.customer.pk,
            old_permission, lock_path=str(Path(directory)/"edge.lock")) as allowed:
            self.assertFalse(allowed)
        with patch.object(bot, "_provider_account_id", return_value="999999"), patch.object(
            bot, "get_page_token", return_value="test-human-token"), patch.object(bot, "_provider_http",
            return_value=(200, '{"message_id":"exact-human-receipt"}')) as http:
            delivered = human.dispatch_human_reply_command(command.pk)
        self.assertEqual(delivered.state, "sent")
        self.assertEqual(delivered.reply_message.role, "manager")
        self.assertEqual(delivered.provider_message_ids, ["exact-human-receipt"])
        self.assertEqual(http.call_count, 1)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.bot_paused and self.customer.manager_takeover)

    def test_late_exact_started_receipt_after_takeover_remains_truth_without_new_send(self):
        from management.services.ig_revision_outbox import finish_effect
        turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.context,
            window_started_at=self.at, window_deadline=self.at+timedelta(seconds=1))
        revision = IgCustomerTurnRevision.objects.create(client=self.customer, turn=turn, revision=1,
            state="claimed", quiet_started_at=self.at, quiet_deadline=self.at,
            quiet_cap_at=self.at, overall_deadline=self.at+timedelta(seconds=60),
            permission_epoch=7, snapshot_digest="a"*64)
        effect = IgRevisionDeliveryEffect.objects.create(revision=revision, source_message=self.context,
            effect_key="late-after-human-takeover", group="substantive_text", kind="text",
            order_index=0, part_index=0, part_count=1, plan_digest="b"*64,
            payload={"recipient":{"id":self.customer.igsid},"message":{"text":"Earlier reply"}},
            payload_digest="c"*64, recipient_igsid=self.customer.igsid, provider_namespace=self.namespace,
            settings_id_snapshot=self.settings_row.pk, settings_permission_epoch=0, client_permission_epoch=7,
            revision_snapshot_digest="a"*64, publication_id=1, publication_version=1,
            publication_hash="d"*64, authority_context_digest="e"*64,
            state="provider_started", claim_token="receipt-owner", provider_started_at=self.at)
        self.create()
        with patch("management.services.instagram_bot._provider_http") as http:
            result = finish_effect(effect.pk, "receipt-owner", provider_namespace=self.namespace,
                http_status=200, provider_message_id="late-exact-receipt")
        http.assert_not_called()
        self.assertTrue(result.changed)
        effect.refresh_from_db(); self.customer.refresh_from_db()
        self.assertEqual((effect.state, effect.provider_message_id), ("sent", "late-exact-receipt"))
        self.assertTrue(self.customer.bot_paused and self.customer.manager_takeover)
        self.assertEqual(self.customer.reply_permission_epoch, 8)
        self.assertEqual(IgRevisionDeliveryEffect.objects.count(), 1)


@skipUnless(connection.vendor == "mysql", "requires isolated native MariaDB permission-edge ordering")
@override_settings(IG_MEMORY_GENERATION_ENABLED=False, IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=False)
class HumanTakeoverNativeBoundaryTests(_HumanFixture, TransactionTestCase):
    def test_bot_physical_edge_completes_before_takeover_commit_and_old_permission_never_sends_again(self):
        from management.services import instagram_bot as bot
        entered, release, takeover_waiting = Event(), Event(), Event()
        old_permission = capture_reply_permission(self.settings_row.pk, self.customer.pk)
        with TemporaryDirectory() as directory:
            lock_path = str(Path(directory)/"human-bot-edge.lock")
            @contextmanager
            def held_bot_edge():
                with customer_send_boundary(self.settings_row.pk, self.customer.pk, old_permission,
                                            lock_path=lock_path) as allowed:
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("test release missing")
                    yield allowed
            @contextmanager
            def takeover_edge(**kwargs):
                takeover_waiting.set()
                with pause_reply_boundary(lock_path=lock_path, **kwargs):
                    yield
            def send_old_bot():
                close_old_connections()
                try:
                    return bot.send_text(self.settings_row, self.customer.igsid, "Earlier bot reply",
                        permission_boundary_factory=held_bot_edge, return_receipt=True)
                finally:
                    close_old_connections()
            def take_over():
                close_old_connections()
                try:
                    return self.create().command.pk
                finally:
                    close_old_connections()
            with patch.object(bot, "_provider_account_id", return_value="999999"), patch.object(
                bot, "get_page_token", return_value="test-token"), patch.object(bot, "_provider_http",
                return_value=(200, '{"message_id":"old-bot-exact"}')) as http, patch(
                "management.services.ig_reply_boundary.pause_reply_boundary", side_effect=takeover_edge), patch.object(
                transitions, "WEB_LOCK_TIMEOUT_SECONDS", 1.0), ThreadPoolExecutor(max_workers=2) as workers:
                prior = workers.submit(send_old_bot)
                self.assertTrue(entered.wait(5))
                takeover = workers.submit(take_over)
                self.assertTrue(takeover_waiting.wait(5))
                self.customer.refresh_from_db()
                self.assertEqual(self.customer.reply_permission_epoch, 7)
                self.assertFalse(self.customer.bot_paused)
                self.assertFalse(HumanReplyCommand.objects.exists())
                release.set()
                self.assertTrue(prior.result(timeout=5).ok)
                command_id = takeover.result(timeout=5)
                with customer_send_boundary(self.settings_row.pk, self.customer.pk, old_permission,
                                            lock_path=lock_path) as allowed:
                    self.assertFalse(allowed)
            self.assertEqual(http.call_count, 1)
            self.customer.refresh_from_db()
            command = HumanReplyCommand.objects.get(pk=command_id)
            self.assertTrue(self.customer.bot_paused and self.customer.manager_takeover)
            self.assertEqual((self.customer.reply_permission_epoch, command.permission_epoch), (8, 8))
            self.assertEqual(command.state, "pending")
