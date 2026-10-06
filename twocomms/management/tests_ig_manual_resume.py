from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.models import (
    IgAiReplyRecoveryJob,
    IgClient,
    IgCommerceSelectionSession,
    IgCommerceTurnDecision,
    IgCustomerTurn,
    IgFollowUpTask,
    InstagramBotLog,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services.ig_customer_turns import ensure_turn_for_inbound
from management.services.ig_manual_resume import (
    ManualResumeRejected,
    resume_client_automation,
)
from management.services.ig_permission_transitions import (
    cancel_client_unstarted_automation,
)


MGMT = override_settings(
    ROOT_URLCONF="twocomms.urls_management",
    SECURE_SSL_REDIRECT=False,
)


@MGMT
class ManualResumeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "resume-manager", password="x", is_staff=True
        )
        self.client.force_login(self.user)
        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.save(update_fields=["is_enabled"])

    def paused_client(self, suffix="main"):
        row = IgClient.get_or_create_for_sender(f"manual-resume-{suffix}")
        row.bot_paused = True
        row.manager_takeover = True
        row.paused_reason = "manager_takeover"
        row.paused_at = timezone.now() - timedelta(hours=13)
        row.reply_permission_epoch = 4
        row.save(update_fields=[
            "bot_paused",
            "manager_takeover",
            "paused_reason",
            "paused_at",
            "reply_permission_epoch",
            "updated_at",
        ])
        return row

    def observed_turn(self, client, text, mid, *, at):
        source = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text=text,
            mid=mid,
            status=InstagramBotMessage.Status.DONE,
            processed_at=at,
            provider_created_at=at,
        )
        attachment = ensure_turn_for_inbound(source, now=at)
        turn = attachment.turn
        IgCustomerTurn.objects.filter(pk=turn.pk).update(
            claim_state=IgCustomerTurn.ClaimState.PROCESSED,
            processed_at=at,
            terminal_reason=IgCustomerTurn.TerminalReason.NO_REPLY_NEEDED,
        )
        turn.refresh_from_db()
        return source, turn

    def test_resume_identifies_latest_unanswered_without_rewriting_history(self):
        client = self.paused_client()
        now = timezone.now()
        older, older_turn = self.observed_turn(
            client, "Яка ціна футболки?", "resume-older", at=now - timedelta(minutes=3)
        )
        latest, latest_turn = self.observed_turn(
            client, "А чорна є у розмірі L?", "resume-latest", at=now - timedelta(minutes=1)
        )

        result = resume_client_automation(client.pk, actor=self.user)

        client.refresh_from_db()
        older.refresh_from_db()
        latest.refresh_from_db()
        older_turn.refresh_from_db()
        latest_turn.refresh_from_db()
        self.assertTrue(result.changed)
        self.assertEqual(result.permission_epoch, 5)
        self.assertFalse(result.successor_created)
        self.assertIsNone(result.successor_turn_id)
        self.assertIsNone(result.successor_source_message_id)
        self.assertEqual(result.unresolved_turn_id, latest_turn.pk)
        self.assertEqual(result.unresolved_source_message_id, latest.pk)
        self.assertEqual(result.successor_reason, "successor_revision_unavailable")
        self.assertFalse(client.bot_paused)
        self.assertFalse(client.manager_takeover)
        self.assertEqual(older.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(older_turn.claim_state, IgCustomerTurn.ClaimState.PROCESSED)
        self.assertEqual(latest.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(latest_turn.claim_state, IgCustomerTurn.ClaimState.PROCESSED)
        self.assertEqual(
            InstagramBotMessage.objects.filter(
                client=client, status=InstagramBotMessage.Status.PENDING
            ).count(),
            0,
        )
        self.assertTrue(
            InstagramBotLog.objects.filter(
                event="manual_resume",
                detail__contains=f"client={client.pk}; user={self.user.pk}; epoch=4->5",
            ).exists()
        )

    def test_manager_answer_after_latest_customer_turn_creates_no_successor(self):
        client = self.paused_client("answered")
        source, turn = self.observed_turn(
            client,
            "Підкажіть, чи є XL?",
            "resume-answered-source",
            at=timezone.now() - timedelta(minutes=2),
        )
        InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.MANAGER,
            text="Так, XL є в наявності.",
            status=InstagramBotMessage.Status.DONE,
        )

        result = resume_client_automation(client.pk, actor=self.user)

        source.refresh_from_db()
        turn.refresh_from_db()
        self.assertEqual(result.successor_reason, "manager_answered")
        self.assertIsNone(result.successor_turn_id)
        self.assertEqual(source.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(turn.claim_state, IgCustomerTurn.ClaimState.PROCESSED)

    def test_repeated_resume_does_not_advance_epoch_or_duplicate_work(self):
        client = self.paused_client("repeat")
        source, turn = self.observed_turn(
            client,
            "Яка ціна?",
            "resume-repeat-source",
            at=timezone.now() - timedelta(minutes=1),
        )

        first = resume_client_automation(client.pk, actor=self.user)
        second = resume_client_automation(client.pk, actor=self.user)

        client.refresh_from_db()
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(second.successor_reason, "already_resumed")
        self.assertEqual(client.reply_permission_epoch, 5)
        self.assertEqual(
            InstagramBotMessage.objects.filter(
                client=client, status=InstagramBotMessage.Status.PENDING
            ).count(),
            0,
        )
        self.assertEqual(IgCustomerTurn.objects.filter(pk=turn.pk).count(), 1)
        self.assertEqual(InstagramBotMessage.objects.filter(pk=source.pk).count(), 1)

    def test_resume_boolean_never_records_consent(self):
        # Exercise the opt-out contract as an explicitly authorized operator.
        # A generic staff principal correctly stops at the capability boundary.
        from django.contrib.auth.models import Permission
        self.user.user_permissions.add(*(
            Permission.objects.get(content_type__app_label="management", codename=code)
            for code in ("operate_ig_bot", "view_ig_conversation_pii")
        ))
        client = self.paused_client("optout")
        client.opted_out_at = timezone.now()
        client.save(update_fields=["opted_out_at", "updated_at"])
        epoch = client.reply_permission_epoch

        response = self.client.post(
            reverse("management_bot_client_resume_api", args=[client.pk]),
            {"confirm_opt_in": "1"},
        )

        client.refresh_from_db()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "active_opt_out")
        self.assertTrue(response.json()["requires_verified_reconsent"])
        self.assertTrue(client.bot_paused)
        self.assertTrue(client.manager_takeover)
        self.assertEqual(client.reply_permission_epoch, epoch)
        self.assertIsNone(client.opted_in_at)
        self.assertIsNone(client.opted_in_by_id)

    def test_existing_valid_consent_is_preserved(self):
        client = self.paused_client("valid-consent")
        opted_out_at = timezone.now() - timedelta(days=2)
        opted_in_at = timezone.now() - timedelta(days=1)
        client.opted_out_at = opted_out_at
        client.opted_in_at = opted_in_at
        client.opted_in_by = self.user
        client.save(update_fields=[
            "opted_out_at", "opted_in_at", "opted_in_by", "updated_at",
        ])

        result = resume_client_automation(client.pk, actor=self.user)

        client.refresh_from_db()
        self.assertTrue(result.changed)
        self.assertEqual(client.opted_out_at, opted_out_at)
        self.assertEqual(client.opted_in_at, opted_in_at)
        self.assertEqual(client.opted_in_by_id, self.user.pk)

    def test_hidden_erasure_and_blocked_clients_are_unchanged(self):
        states = (
            ("hidden", "hidden_at", timezone.now(), "client_hidden"),
            (
                "erasure",
                "privacy_erasure_started_at",
                timezone.now(),
                "privacy_erasure_active",
            ),
            ("blocked", "is_blocked", True, "client_blocked"),
        )
        for suffix, field, value, code in states:
            with self.subTest(state=suffix):
                client = self.paused_client(suffix)
                setattr(client, field, value)
                client.save(update_fields=[field, "updated_at"])
                before = (
                    client.bot_paused,
                    client.manager_takeover,
                    client.reply_permission_epoch,
                    client.paused_reason,
                )
                with self.assertRaises(ManualResumeRejected) as raised:
                    resume_client_automation(client.pk, actor=self.user)
                client.refresh_from_db()
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(
                    (
                        client.bot_paused,
                        client.manager_takeover,
                        client.reply_permission_epoch,
                        client.paused_reason,
                    ),
                    before,
                )

    def test_unstarted_automation_is_cancelled_without_touching_sending_or_unknown(self):
        client = self.paused_client("cancel")
        now = timezone.now()
        pending = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="pending",
            mid="resume-cancel-pending",
            status=InstagramBotMessage.Status.PENDING,
        )
        sending = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="sending",
            mid="resume-cancel-sending",
            status=InstagramBotMessage.Status.PROCESSING,
            send_state="sending",
        )
        unknown = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="unknown",
            mid="resume-cancel-unknown",
            status=InstagramBotMessage.Status.FAILED,
            send_state="unknown",
        )
        processing = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="processing before provider boundary",
            mid="resume-cancel-processing",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        pending_recovery = IgAiReplyRecoveryJob.objects.create(
            source_message=pending,
            client=client,
            dedupe_key="resume-cancel-recovery-pending",
            status=IgAiReplyRecoveryJob.Status.PENDING,
        )
        sending_recovery = IgAiReplyRecoveryJob.objects.create(
            source_message=sending,
            client=client,
            dedupe_key="resume-cancel-recovery-sending",
            status=IgAiReplyRecoveryJob.Status.SENDING,
            sending_started_at=now,
        )
        processing_recovery = IgAiReplyRecoveryJob.objects.create(
            source_message=processing,
            client=client,
            dedupe_key="resume-cancel-recovery-processing",
            status=IgAiReplyRecoveryJob.Status.PROCESSING,
        )
        unknown_recovery_source = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="unknown recovery source",
            mid="resume-cancel-recovery-unknown-source",
            status=InstagramBotMessage.Status.DONE,
        )
        unknown_reply = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.MODEL,
            text="possibly delivered",
            status=InstagramBotMessage.Status.DONE,
            send_state="unknown",
        )
        unknown_recovery = IgAiReplyRecoveryJob.objects.create(
            source_message=unknown_recovery_source,
            client=client,
            reply_message=unknown_reply,
            dedupe_key="resume-cancel-recovery-unknown",
            status=IgAiReplyRecoveryJob.Status.PENDING,
        )
        commerce_source = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="commerce pending",
            mid="resume-cancel-commerce-pending",
            status=InstagramBotMessage.Status.DONE,
        )
        unknown_commerce_source = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="commerce unknown",
            mid="resume-cancel-commerce-unknown",
            status=InstagramBotMessage.Status.DONE,
        )
        session = IgCommerceSelectionSession.objects.create(
            client=client, generation=1
        )
        pending_decision = IgCommerceTurnDecision.objects.create(
            source_message=commerce_source,
            session=session,
            delivery_required=True,
            delivery_state=IgCommerceTurnDecision.DeliveryState.PENDING,
        )
        unknown_decision = IgCommerceTurnDecision.objects.create(
            source_message=unknown_commerce_source,
            session=session,
            delivery_required=True,
            delivery_state=IgCommerceTurnDecision.DeliveryState.UNKNOWN,
            attempts=1,
            delivery_started_at=now,
        )
        normal_followup = IgFollowUpTask.objects.create(
            client=client,
            due_at=now + timedelta(hours=1),
            kind=IgFollowUpTask.Kind.QUALIFICATION,
        )
        manager_task = IgFollowUpTask.objects.create(
            client=client,
            due_at=now + timedelta(hours=1),
            kind=IgFollowUpTask.Kind.MANAGER_TASK,
        )

        cancelled = cancel_client_unstarted_automation(
            client, reason="test_pause", now=now, nowait=False
        )

        pending.refresh_from_db()
        sending.refresh_from_db()
        unknown.refresh_from_db()
        processing.refresh_from_db()
        pending_recovery.refresh_from_db()
        sending_recovery.refresh_from_db()
        processing_recovery.refresh_from_db()
        unknown_recovery.refresh_from_db()
        unknown_reply.refresh_from_db()
        pending_decision.refresh_from_db()
        unknown_decision.refresh_from_db()
        normal_followup.refresh_from_db()
        manager_task.refresh_from_db()
        self.assertEqual(cancelled["inbound_rows"], 2)
        self.assertEqual(cancelled["recovery_jobs"], 2)
        self.assertEqual(cancelled["commerce_decisions"], 1)
        self.assertEqual(pending.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(sending.status, InstagramBotMessage.Status.PROCESSING)
        self.assertEqual(sending.send_state, "sending")
        self.assertEqual(unknown.status, InstagramBotMessage.Status.FAILED)
        self.assertEqual(unknown.send_state, "unknown")
        self.assertEqual(processing.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(pending_recovery.status, IgAiReplyRecoveryJob.Status.CANCELLED)
        self.assertEqual(sending_recovery.status, IgAiReplyRecoveryJob.Status.SENDING)
        self.assertEqual(processing_recovery.status, IgAiReplyRecoveryJob.Status.CANCELLED)
        self.assertEqual(unknown_recovery.status, IgAiReplyRecoveryJob.Status.PENDING)
        self.assertEqual(unknown_reply.send_state, "unknown")
        self.assertEqual(
            pending_decision.delivery_state,
            IgCommerceTurnDecision.DeliveryState.NOT_REQUIRED,
        )
        self.assertEqual(
            unknown_decision.delivery_state,
            IgCommerceTurnDecision.DeliveryState.UNKNOWN,
        )
        self.assertEqual(normal_followup.status, IgFollowUpTask.Status.CANCELLED)
        self.assertEqual(manager_task.status, IgFollowUpTask.Status.PENDING)

    def test_unknown_outbound_suppresses_successor(self):
        client = self.paused_client("unknown-outbound")
        source, turn = self.observed_turn(
            client,
            "Чи є синя?",
            "resume-unknown-source",
            at=timezone.now() - timedelta(minutes=1),
        )
        InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.MODEL,
            text="Так, є.",
            status=InstagramBotMessage.Status.DONE,
            send_state="unknown",
        )

        result = resume_client_automation(client.pk, actor=self.user)

        source.refresh_from_db()
        turn.refresh_from_db()
        self.assertEqual(result.successor_reason, "delivery_reconciliation_required")
        self.assertIsNone(result.successor_turn_id)
        self.assertEqual(source.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(turn.claim_state, IgCustomerTurn.ClaimState.PROCESSED)
