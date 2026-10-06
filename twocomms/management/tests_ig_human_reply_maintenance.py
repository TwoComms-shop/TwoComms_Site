"""Real store/receipt/maintenance tests; physical transport is forbidden."""
from datetime import timedelta
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from management.ig_bot_models import HumanReplyCommand, IgFunnelResetAudit
from management.ig_human_reply_models import HumanReplyPart
from management.models import AdminAuditLog, IgClient, IgFollowUpTask, InstagramBotMessage, InstagramBotSettings
from management.services.ig_human_reply import create_human_reply_command, resolve_unknown_human_reply
from management.services.ig_human_reply_delivery import claim_next_human_part, record_human_part_started, settle_human_part
from management.services.ig_human_reply_transport import project_human_part_receipt
from management.services.ig_human_reply_maintenance import (
    RECEIPT_AUDIT_ACTION, close_planned_human_reconciliation, maintain_human_reply_delivery,
)


class HumanReplyMaintenanceTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.namespace = "instagram_login:maintenance-owner"
        self.actor = get_user_model().objects.create_superuser(username="human-maintenance", password="x")
        InstagramBotSettings.objects.create(pk=1, ig_user_id="maintenance-owner", page_id="maintenance-owner", is_enabled=True)
        self.customer = IgClient.objects.create(igsid="human-maintenance-customer")
        self.source = self.inbound(self.customer)
        env = patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        env.start()
        self.addCleanup(env.stop)
        self.http = patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("maintenance provider HTTP forbidden")).start()
        self.send = patch("management.services.instagram_bot.send_text", side_effect=AssertionError("maintenance customer send forbidden")).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.http.assert_not_called()
        self.send.assert_not_called()
        super().tearDown()

    def inbound(self, customer):
        return InstagramBotMessage.objects.create(client=customer, sender_id=customer.igsid, role="user",
            source="webhook", provider_namespace=self.namespace, mid="maintenance-source-" + uuid.uuid4().hex,
            text="Підкажіть розмір", status="done", provider_created_at=self.now - timedelta(minutes=2))

    def command(self, text="Вітаю!", *, customer=None, source=None):
        return create_human_reply_command((customer or self.customer).pk, actor=self.actor, text=text,
            context_message_id=(source or self.source).pk, now=self.now).command

    def claim(self, command, *, started=True):
        claim = claim_next_human_part(command.pk, now=self.now)
        self.assertTrue(claim.ready, claim.reason)
        if started:
            result = record_human_part_started(claim.part.pk, claim.token, now=self.now)
            self.assertTrue(result.ready, result.reason)
        return claim

    def expire(self, command):
        result = maintain_human_reply_delivery(now=self.now + timedelta(minutes=2))
        command.refresh_from_db()
        return result

    def receipt(self, claim, mid):
        result = settle_human_part(claim.part.pk, claim.token, provider_namespace=self.namespace,
            http_status=200, provider_message_id=mid, now=self.now + timedelta(minutes=3))
        self.assertTrue(result.ready, result.reason)
        return result

    def task(self, command):
        return IgFollowUpTask.objects.get(event_key=f"human-reply-unknown:{command.pk}")

    def test_unstarted_expiry_only_reclaims_for_user_retry(self):
        command = self.command()
        claim = self.claim(command, started=False)
        result = self.expire(command)
        claim.part.refresh_from_db()
        self.assertEqual(result["reclaimed"], 1)
        self.assertEqual((command.state, claim.part.state), ("pending", "planned"))
        self.assertEqual(claim.part.claim_token, "")
        self.assertIsNone(claim.part.provider_started_at)
        self.assertFalse(IgFollowUpTask.objects.filter(reason="human_reply:delivery_unknown").exists())
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_started_expiry_creates_one_unknown_case_and_never_retries(self):
        command = self.command("A" * 1500)
        claim = self.claim(command)
        result = self.expire(command)
        original = self.task(command)
        replay = maintain_human_reply_delivery(now=self.now + timedelta(minutes=5))
        claim.part.refresh_from_db()
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(command.state, "unknown")
        self.assertEqual(claim.part.claim_token, claim.token)
        self.assertEqual(list(command.delivery_parts.order_by("ordinal").values_list("state", flat=True)), ["unknown", "cancelled"])
        self.assertEqual(self.task(command).pk, original.pk)
        self.assertEqual(replay["expired_seen"], 0)
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_late_exact_receipt_projects_and_closes_case_once_without_manager_approval(self):
        command = self.command()
        claim = self.claim(command)
        self.expire(command)
        task = self.task(command)
        original_payload = task.event_payload
        self.receipt(claim, "maintenance-exact-mid")
        result = maintain_human_reply_delivery(now=self.now + timedelta(minutes=4))
        replay = maintain_human_reply_delivery(now=self.now + timedelta(minutes=5))
        task.refresh_from_db()
        self.assertEqual((result["projected"], result["cases_closed"]), (1, 1))
        self.assertEqual((replay["projected"], replay["cases_closed"]), (0, 0))
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.event_payload, original_payload)
        self.assertEqual(task.manager_approval_status, "pending")
        self.assertIsNone(task.manager_approval_actor_id)
        self.assertNotIn("resolution", task.manager_context)
        self.assertEqual(task.manager_context["receipt_resolution"]["parts"][0]["provider_message_id"], "maintenance-exact-mid")
        self.assertEqual(AdminAuditLog.objects.filter(action=RECEIPT_AUDIT_ACTION).count(), 1)
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)
        self.assertTrue(close_planned_human_reconciliation(command.pk)["idempotent"])

    def test_all_multipart_receipts_required_and_cancelled_tail_never_resumes(self):
        command = self.command("A" * 2500)
        first = self.claim(command)
        self.receipt(first, "maintenance-first-mid")
        project_human_part_receipt(first.part.pk)
        second = self.claim(command)
        self.expire(command)
        original_payload = self.task(command).event_payload
        self.receipt(second, "maintenance-second-mid")
        result = maintain_human_reply_delivery(now=self.now + timedelta(minutes=4))
        command.refresh_from_db()
        task = self.task(command)
        self.assertEqual(command.state, "unknown")
        self.assertEqual(task.status, "skipped")
        self.assertEqual(task.event_payload, original_payload)
        self.assertEqual(task.manager_context["latest_delivery"]["provider_message_ids"],
                         ["maintenance-first-mid", "maintenance-second-mid"])
        self.assertEqual(result["cases_closed"], 0)
        self.assertEqual(list(command.delivery_parts.order_by("ordinal").values_list("state", flat=True)), ["sent", "sent", "cancelled"])
        self.assertEqual(close_planned_human_reconciliation(command.pk)["reason"], "human_receipts_incomplete")
        self.assertFalse(AdminAuditLog.objects.filter(action=RECEIPT_AUDIT_ACTION).exists())

    def test_matching_transcripts_are_required_even_when_all_parts_are_sent(self):
        command = self.command()
        claim = self.claim(command)
        self.expire(command)
        self.receipt(claim, "receipt-no-transcript")
        blocked = close_planned_human_reconciliation(command.pk)
        self.assertEqual(blocked["reason"], "human_transcript_unconfirmed")
        self.assertEqual(self.task(command).status, "skipped")
        project_human_part_receipt(claim.part.pk)
        message = InstagramBotMessage.objects.get(role="manager")
        message.text = "Different text"
        message.save(update_fields=["text"])
        self.assertEqual(close_planned_human_reconciliation(command.pk)["reason"], "human_transcript_unconfirmed")

    def test_operator_assertion_never_creates_mid_and_is_preserved_after_late_receipt(self):
        command = self.command()
        claim = self.claim(command)
        self.expire(command)
        task = self.task(command)
        resolved = resolve_unknown_human_reply(task.pk, client_id=self.customer.pk, actor=self.actor,
            outcome="delivered", note="Reviewed Meta Inbox", now=self.now + timedelta(minutes=3))
        self.assertTrue(resolved["ok"])
        claim.part.refresh_from_db()
        self.assertEqual(claim.part.state, "unknown")
        self.assertIsNone(claim.part.provider_message_id)
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        task.refresh_from_db()
        original = task.manager_context["resolution"]
        self.receipt(claim, "operator-later-exact")
        project_human_part_receipt(claim.part.pk)
        result = close_planned_human_reconciliation(command.pk)
        task.refresh_from_db()
        self.assertTrue(result["operator_resolution_preserved"])
        self.assertEqual(task.manager_context["resolution"], original)
        self.assertNotIn("receipt_resolution", task.manager_context)
        self.assertFalse(AdminAuditLog.objects.filter(action=RECEIPT_AUDIT_ACTION).exists())

    def test_changed_case_operation_cannot_close_from_neighbour_receipts(self):
        command = self.command()
        claim = self.claim(command)
        self.expire(command)
        self.receipt(claim, "mismatched-case-mid")
        project_human_part_receipt(claim.part.pk)
        task = self.task(command)
        task.manager_context["operation_id"] = str(uuid.uuid4())
        task.save(update_fields=["manager_context"])
        self.assertEqual(close_planned_human_reconciliation(command.pk)["reason"], "human_reconciliation_scope_changed")
        self.assertEqual(self.task(command).status, "skipped")

    def test_erasure_blocks_transcript_and_closure_but_keeps_exact_part_receipt(self):
        command = self.command()
        claim = self.claim(command)
        self.expire(command)
        self.customer.privacy_erasure_started_at = timezone.now()
        self.customer.save(update_fields=["privacy_erasure_started_at"])
        self.receipt(claim, "erasure-exact-mid")
        result = maintain_human_reply_delivery(now=self.now + timedelta(minutes=4))
        claim.part.refresh_from_db()
        self.assertEqual(claim.part.provider_message_id, "erasure-exact-mid")
        self.assertEqual(result["cases_closed"], 0)
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        self.assertEqual(self.task(command).status, "skipped")

    def test_historical_exact_receipt_after_reset_and_actor_deletion_reconciles_without_send(self):
        command = self.command()
        claim = self.claim(command)
        self.expire(command)
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=self.source.pk, reason="after human start")
        self.actor.delete()
        self.receipt(claim, "historical-exact-mid")
        result = maintain_human_reply_delivery(now=self.now + timedelta(minutes=4))
        self.assertEqual(result["cases_closed"], 1)
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)
        self.assertIsNone(self.task(command).manager_approval_actor_id)

    def test_expired_part_scan_is_capped_at_twenty_five(self):
        parts = []
        for number in range(26):
            customer = IgClient.objects.create(igsid=f"maintenance-bounded-{number}")
            command = self.command(customer=customer, source=self.inbound(customer))
            parts.append(self.claim(command, started=False).part.pk)
        result = maintain_human_reply_delivery(now=self.now + timedelta(minutes=2), limit=1000)
        self.assertEqual(result["expired_seen"], 25)
        self.assertEqual(HumanReplyPart.objects.filter(pk__in=parts, state="claimed").count(), 1)

    def test_wrong_namespace_receipt_remains_unknown_and_cannot_resolve_case(self):
        command = self.command()
        claim = self.claim(command)
        self.expire(command)
        denied = settle_human_part(claim.part.pk, claim.token, provider_namespace="instagram_login:other",
            http_status=200, provider_message_id="wrong-owner", now=self.now + timedelta(minutes=3))
        self.assertFalse(denied.ready)
        result = maintain_human_reply_delivery(now=self.now + timedelta(minutes=4))
        claim.part.refresh_from_db()
        self.assertEqual(claim.part.state, "unknown")
        self.assertEqual(result["cases_closed"], 0)
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
