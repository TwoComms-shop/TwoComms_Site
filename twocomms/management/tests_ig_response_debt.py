from datetime import timedelta

from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from management.models import IgClient, IgCustomerTurn, IgCustomerTurnRevision, IgFollowUpTask, InstagramBotMessage
from management.services.ig_response_debt import (
    park_manual_revision, record_reply_debt, reply_debt_payload,
    resolve_delivered_reply_debt, with_reply_debt,
)


class ResponseDebtTests(TestCase):
    def setUp(self):
        self.client_row = IgClient.objects.create(igsid="reply-debt-owner")
        self.source = InstagramBotMessage.objects.create(client=self.client_row, role="user", text="Оверсайз", status="pending")
        self.now = timezone.now()
        turn = IgCustomerTurn.objects.create(client=self.client_row, primary_source_message=self.source,
            window_started_at=self.now, window_deadline=self.now)
        self.revision = IgCustomerTurnRevision.objects.create(client=self.client_row, turn=turn, revision=1,
            quiet_started_at=self.now, quiet_deadline=self.now, quiet_cap_at=self.now,
            overall_deadline=self.now-timedelta(minutes=2), state="claimed", claim_token="expired-worker",
            claimed_at=self.now-timedelta(minutes=3), lease_until=self.now-timedelta(minutes=2), recovery_state="manual")

    def test_manual_debt_is_idempotent_and_releases_lease_without_fake_reply(self):
        with transaction.atomic():
            first = park_manual_revision(self.revision, "provider_candidates_exhausted")
            second = park_manual_revision(self.revision, "provider_candidates_exhausted")
        self.assertEqual(first.pk, second.pk)
        self.revision.refresh_from_db(); self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")
        self.assertIsNone(self.revision.processed_at)
        self.assertEqual(self.revision.claim_token, "")
        self.assertIsNone(self.revision.lease_until)
        self.assertEqual(self.revision.action_receipts["response_debt"]["owner"], "manager")

    def test_list_and_detail_agree_without_per_row_queries(self):
        record_reply_debt(self.revision, "generation_failed")
        annotated = with_reply_debt(IgClient.objects.all()).get(pk=self.client_row.pk)
        with self.assertNumQueries(0):
            payload = reply_debt_payload(annotated)
        self.assertTrue(payload["required"])
        self.assertEqual(payload["revision_id"], self.revision.pk)
        self.assertEqual(payload, reply_debt_payload(self.client_row))

    def test_unrelated_manager_case_does_not_become_reply_debt(self):
        IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now,
            kind="manager_task", reason="prize_review:pending", event_key="prize-case")
        self.assertEqual(reply_debt_payload(self.client_row), {"required": False})

    def test_exact_delivery_resolution_keeps_other_cases(self):
        task = record_reply_debt(self.revision, "generation_failed")
        other = IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now,
            kind="manager_task", reason="revision_case:execution_debt", event_key="ig-revision-debt:other", status="skipped")
        resolve_delivered_reply_debt(self.revision)
        task.refresh_from_db(); other.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.COMPLETED)
        self.assertEqual(other.status, IgFollowUpTask.Status.SKIPPED)
        self.assertTrue(reply_debt_payload(self.client_row)["required"])

    def test_completed_case_not_reopened_by_duplicate_finalization(self):
        task = record_reply_debt(self.revision, "generation_failed")
        resolve_delivered_reply_debt(self.revision)
        record_reply_debt(self.revision, "generation_failed")
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.COMPLETED)
        self.assertFalse(reply_debt_payload(self.client_row)["required"])

    def test_existing_client_list_annotation_includes_reply_debt(self):
        from management.bot_views import _with_latest_interaction

        record_reply_debt(self.revision, "generation_failed")
        row = _with_latest_interaction(IgClient.objects.all()).get(pk=self.client_row.pk)
        self.assertTrue(row.has_manager_action)
        self.assertTrue(row.has_reply_debt)

    @override_settings(ROOT_URLCONF="twocomms.urls_management")
    def test_authorized_detail_and_incremental_poll_include_debt(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        admin = get_user_model().objects.create_superuser("reply-debt-admin", password="fixture")
        self.client.force_login(admin)
        record_reply_debt(self.revision, "generation_failed")
        url = reverse("management_bot_client_detail_api", args=[self.client_row.pk])
        full = self.client.get(url)
        self.assertEqual(full.status_code, 200)
        self.assertTrue(full.json()["client"]["response_debt"]["required"])
        incremental = self.client.get(url, {"after_id": self.source.pk})
        self.assertEqual(incremental.status_code, 200)
        self.assertTrue(incremental.json()["response_debt"]["required"])
        resolve_delivered_reply_debt(self.revision)
        self.assertFalse(self.client.get(url, {"after_id": self.source.pk}).json()["response_debt"]["required"])

    @override_settings(ROOT_URLCONF="twocomms.urls_management")
    def test_reply_debt_does_not_bypass_conversation_permission(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        staff = get_user_model().objects.create_user("reply-debt-no-pii", password="fixture", is_staff=True)
        self.client.force_login(staff)
        record_reply_debt(self.revision, "generation_failed")
        url = reverse("management_bot_client_detail_api", args=[self.client_row.pk])
        response = self.client.get(url, {"after_id": self.source.pk})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("response_debt", response.json())
