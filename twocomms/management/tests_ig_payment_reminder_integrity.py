"""Native physical evidence guards and fenced erasure, independent of adapters."""
from django.db import DatabaseError, connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from django.test.utils import CaptureQueriesContext
from unittest.mock import patch
from datetime import timedelta
from management import tests_ig_payment_reminders as fixtures
from management.ig_payment_reminder_models import (
    IgPaymentReminderInvitation, IgPaymentReminderReceipt, IgPaymentReminderAnswer,
)
from management.services.ig_payment_reminder_privacy import purge_payment_reminder_data


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test",
    IG_PAYMENT_REMINDERS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class ReminderEvidenceIntegrityTests(TestCase):
    for _name, _value in fixtures.RequestedPaymentReminderTests.__dict__.items():
        if callable(_value) and not _name.startswith("test_"):
            locals()[_name] = _value
    native_buttons = staticmethod(fixtures.RequestedPaymentReminderTests.native_buttons)

    def assert_physical_block(self, sql, params):
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql, params)

    def test_invitation_sql_update_and_delete_are_blocked(self):
        invitation, _task, _revision = self.accepted()
        pk = IgPaymentReminderInvitation._meta.pk.get_db_prep_value(invitation.pk, connection)
        self.assert_physical_block("UPDATE management_igpaymentreminderinvitation SET body=%s WHERE id=%s", ["forged", pk])
        self.assert_physical_block("DELETE FROM management_igpaymentreminderinvitation WHERE id=%s", [pk])
        invitation.refresh_from_db()
        self.assertIn("Нагадаю", invitation.body)

    def test_receipt_sql_update_and_delete_are_blocked(self):
        invitation, _task, _revision = self.accepted()
        receipt = IgPaymentReminderReceipt.objects.get(invitation=invitation)
        self.assert_physical_block("UPDATE management_igpaymentreminderreceipt SET provider_message_id=%s WHERE id=%s", ["forged", receipt.pk])
        self.assert_physical_block("DELETE FROM management_igpaymentreminderreceipt WHERE id=%s", [receipt.pk])

    def test_answer_sql_update_and_delete_are_blocked(self):
        _invitation, task, _revision = self.accepted()
        answer = IgPaymentReminderAnswer.objects.get(task=task)
        self.assert_physical_block("UPDATE management_igpaymentreminderanswer SET kind=%s WHERE id=%s", ["revoke", answer.pk])
        self.assert_physical_block("DELETE FROM management_igpaymentreminderanswer WHERE id=%s", [answer.pk])

    def test_erasure_requires_every_exact_committed_owner(self):
        invitation, task, _revision = self.accepted()
        from management.models import IgClient
        other = IgClient.objects.create(igsid="foreign-reminder-erasure")
        with self.assertRaises(ValueError), transaction.atomic():
            purge_payment_reminder_data([self.client_row.pk])
        self.client_row.privacy_erasure_started_at = timezone.now()
        self.client_row.save(update_fields=["privacy_erasure_started_at"])
        with self.assertRaises(ValueError), transaction.atomic():
            purge_payment_reminder_data([self.client_row.pk, other.pk])
        with transaction.atomic():
            purge_payment_reminder_data([self.client_row.pk])
        self.assertFalse(IgPaymentReminderInvitation.objects.filter(pk=invitation.pk).exists())
        self.assertFalse(IgPaymentReminderReceipt.objects.exists())
        self.assertFalse(IgPaymentReminderAnswer.objects.exists())
        task.refresh_from_db()
        self.assertEqual(task.client_id, self.client_row.pk)

    def test_real_current_admin_capture_is_read_only_bounded_and_human_readable(self):
        from management.models import InstagramBotMessage
        from management.services.ig_admin_state_capture import current_admin_state, MAX_READ_QUERIES
        invitation, _revision = self.sent_invitation()
        InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
            role="model", source="bot", status="done", text=invitation.body,
            provider_message_id="reminder-invitation-mid", provider_namespace="instagram_login:owner-1")
        result, answer_source = self.accept(invitation)
        self.assertTrue(result.handled)
        at = self.now + timedelta(seconds=2)
        with patch("django.utils.timezone.now", return_value=at), CaptureQueriesContext(connection) as queries:
            captured = current_admin_state(self.client_row.pk, now=at)
        self.assertEqual(captured.status, "captured", captured.as_dict())
        self.assertLessEqual(captured.read_queries, MAX_READ_QUERIES)
        verbs = {q["sql"].lstrip().split(None, 1)[0].upper() for q in queries}
        self.assertFalse(verbs & {"INSERT", "UPDATE", "DELETE", "REPLACE", "ALTER", "CREATE", "DROP"})
        reminder = captured.state.as_dict()["slots"]["payment.reminder"]
        self.assertEqual(reminder["status"], "confirmed", reminder)
        self.assertEqual(reminder["value"]["state"], "accepted")
        self.assertIn("Київ", reminder["value"]["scheduled_for_label"])
        self.assertEqual(reminder["authority"], "conversation_agreement")
        self.assertEqual(reminder["value"]["native_future_permission"], "unverified")

    def test_generic_queue_retains_legacy_tasks_with_missing_json_origin(self):
        from management.models import IgFollowUpTask
        from management.services.bot_followups import process_due_followups
        _invitation, agreed, _revision = self.accepted()
        legacy = IgFollowUpTask.objects.create(client=self.client_row, deal=self.proposal.deal,
            due_at=agreed.due_at, kind="payment", reason="legacy-payment-no-origin", event_payload={})
        mislabelled = IgFollowUpTask.objects.create(client=self.client_row, deal=self.proposal.deal,
            due_at=agreed.due_at, kind="payment", reason="legacy-mislabelled",
            event_payload={"origin": "agreed_payment_reminder"})
        selected = []
        def capture_claim(task_id, **kwargs):
            selected.append(task_id)
            return None
        with patch("management.services.bot_followups._claim_due_followup", side_effect=capture_claim):
            process_due_followups(self.settings, now=agreed.due_at)
        self.assertIn(legacy.pk, selected)
        self.assertNotIn(agreed.pk, selected)
        self.assertNotIn(mislabelled.pk, selected)

    def test_new_confirmation_replaces_same_clock_reminder_only(self):
        from management.models import IgFollowUpTask
        first, old_task, _revision = self.accepted()
        other = IgFollowUpTask.objects.create(client=self.client_row, deal=self.proposal.deal,
            due_at=old_task.due_at, kind="payment", reason="unrelated-payment")
        second, _revision = self.sent_invitation(text="Нагадай про оплату через 2 години",
            mid="second-reminder-invitation-mid")
        self.assertEqual(first.created_at, second.created_at)
        result, source = self.accept(second)
        self.assertTrue(result.handled, result.reason)
        new_task = IgPaymentReminderAnswer.objects.get(source=source).task
        self.assertIsNotNone(new_task)
        old_task.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(old_task.status, "cancelled")
        self.assertEqual(old_task.skip_reason, "new_reminder_confirmed")
        self.assertEqual(other.status, "pending")
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.client_row,
            reason="agreed_payment_reminder", status="pending").count(), 1)

    def test_new_unprocessed_customer_message_at_http_boundary_blocks_reminder(self):
        _invitation, task, _revision = self.accepted()
        count, requests = self.dispatch(task,
            before_http=lambda: self.source("Оплатив, перевірте будь ласка"))
        self.assertEqual((count, requests), (0, []))
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self.assertIn("pending_inbound", task.skip_reason)
