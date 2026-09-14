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
    def test_browser_debt_survives_switch_and_incomplete_payload_until_explicit_resolution(self):
        from pathlib import Path
        import shutil
        import subprocess

        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required for the browser state regression")
        template = (Path(__file__).parent / "templates/management/bot.html").read_text()
        start = template.index("const replyDebtByClient=new Map();")
        end = template.index("function renderReplyDebt(", start)
        program = template[start:end] + """
const assert=require('node:assert/strict');
const debt={required:true,task_id:100};
assert.equal(reconcileReplyDebt(171,debt),debt);
assert.equal(reconcileReplyDebt(341,{required:false}).required,false);
assert.equal(reconcileReplyDebt(171,undefined),debt);
assert.equal(reconcileReplyDebt(171,{}),debt);
assert.equal(reconcileReplyDebt(171,null),debt);
assert.equal(reconcileReplyDebt(172,undefined),null);
assert.equal(reconcileReplyDebt(171,{required:false}).required,false);
assert.equal(reconcileReplyDebt(171,undefined).required,false);
reconcileReplyDebt(171,debt);
rememberReviewedReplyDebt(171,100,{required:false});
assert.equal(reconcileReplyDebt(171,debt).required,false);
assert.equal(reconcileReplyDebt(341,debt).required,true);
const newDebt={required:true,task_id:101};
assert.equal(reconcileReplyDebt(171,newDebt),newDebt);
assert.equal(reconcileReplyDebt(171,debt),newDebt);
assert.equal(rememberReviewedReplyDebt(171,100,{required:false}),newDebt);
const timedDebt={required:true,task_id:102,observed_at:'2026-09-14T10:00:02Z'};
assert.equal(reconcileReplyDebt(171,timedDebt),timedDebt);
assert.equal(reconcileReplyDebt(171,{required:false,observed_at:'2026-09-14T10:00:01Z'}),timedDebt);
assert.equal(reconcileReplyDebt(171,{required:false,observed_at:'2026-09-14T10:00:02Z'}),timedDebt);
assert.equal(reconcileReplyDebt(171,{required:false}),timedDebt);
assert.equal(reconcileReplyDebt(171,{required:false,observed_at:'2026-09-14T10:00:03Z'}).required,false);
"""
        completed = subprocess.run([node, "-e", program], text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

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
        refreshed = reply_debt_payload(self.client_row)
        self.assertTrue(refreshed.pop("observed_at"))
        self.assertEqual({key: value for key, value in payload.items() if key != "observed_at"}, refreshed)

    def test_unrelated_manager_case_does_not_become_reply_debt(self):
        IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now,
            kind="manager_task", reason="prize_review:pending", event_key="prize-case")
        self.assertFalse(reply_debt_payload(self.client_row)["required"])

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
        self.assertIn("no-store", full["Cache-Control"])
        incremental = self.client.get(url, {"after_id": self.source.pk})
        self.assertEqual(incremental.status_code, 200)
        self.assertTrue(incremental.json()["response_debt"]["required"])
        self.assertIn("no-store", incremental["Cache-Control"])
        # Returning to the same card must read current state, independent of
        # the last polled client and of existing browser/intermediary caches.
        other = IgClient.objects.create(igsid="reply-debt-other")
        other_url = reverse("management_bot_client_detail_api", args=[other.pk])
        self.assertFalse(self.client.get(other_url, {"detail": 1}).json()["client"]["response_debt"]["required"])
        returned = self.client.get(url, {"detail": 1})
        self.assertTrue(returned.json()["client"]["response_debt"]["required"])
        listing = self.client.get(reverse("management_bot_clients_api"))
        self.assertIn("no-store", listing["Cache-Control"])
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

    def _review_actor(self):
        from django.contrib.auth import get_user_model
        return get_user_model().objects.create_superuser("debt-reviewer", password="fixture")

    def test_operator_review_closes_only_alert_and_preserves_delivery_truth(self):
        from management.models import AdminAuditLog
        from management.services.ig_response_debt import review_reply_debt

        actor = self._review_actor()
        task = record_reply_debt(self.revision, "delivery_unknown")
        before_revision = self.revision.action_receipts.copy()
        first = review_reply_debt(self.client_row.pk, task.pk, actor=actor,
                                  expected_revision_id=self.revision.pk)
        again = review_reply_debt(self.client_row.pk, task.pk, actor=actor,
                                  expected_revision_id=self.revision.pk)
        self.assertTrue(first["ok"])
        self.assertTrue(again["idempotent"])
        task.refresh_from_db(); self.source.refresh_from_db(); self.revision.refresh_from_db()
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(task.manager_context["operator_review"]["outcome"], "reviewed_no_reply")
        self.assertFalse(task.manager_context["operator_review"]["reply_confirmed"])
        self.assertEqual(self.source.status, "pending")
        self.assertIsNone(self.revision.processed_at)
        self.assertEqual(self.revision.action_receipts, before_revision)
        self.assertFalse(reply_debt_payload(self.client_row)["required"])
        self.assertEqual(AdminAuditLog.objects.filter(action="ig_reply_debt_reviewed").count(), 1)
        # Recovery of the same failure does not recreate an acknowledged alert.
        self.assertEqual(record_reply_debt(self.revision, "generation_failed").pk, task.pk)
        self.assertFalse(reply_debt_payload(self.client_row)["required"])
        # A different incident remains visible, even for the same client.
        another = IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now,
            event_key="ig-revision-debt:new", kind="manager_task", status="skipped",
            reason="revision_case:execution_debt", event_payload={"revision_id": 999})
        self.assertEqual(reply_debt_payload(self.client_row)["task_id"], another.pk)

    def test_operator_review_rejects_wrong_client_scope_revision_and_other_tasks(self):
        from management.services.ig_response_debt import review_reply_debt
        actor = self._review_actor()
        task = record_reply_debt(self.revision, "generation_failed")
        other = IgClient.objects.create(igsid="review-other")
        self.assertEqual(review_reply_debt(other.pk, task.pk, actor=actor,
            expected_revision_id=self.revision.pk)["status"], 404)
        self.assertEqual(review_reply_debt(self.client_row.pk, task.pk, actor=actor,
            expected_revision_id=self.revision.pk + 1)["status"], 409)
        task.reason = "prize_review:pending"; task.save(update_fields=["reason"])
        self.assertEqual(review_reply_debt(self.client_row.pk, task.pk, actor=actor,
            expected_revision_id=self.revision.pk)["status"], 404)

    @override_settings(ROOT_URLCONF="twocomms.urls_management")
    def test_review_endpoint_requires_write_permission_post_and_csrf(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        from django.urls import reverse
        actor = self._review_actor()
        task = record_reply_debt(self.revision, "generation_failed")
        url = reverse("management_bot_client_reply_debt_review_api", args=[self.client_row.pk, task.pk])
        self.client.force_login(actor)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url).status_code, 400)
        strict = Client(enforce_csrf_checks=True); strict.force_login(actor)
        self.assertEqual(strict.post(url, {"expected_revision_id": self.revision.pk}).status_code, 403)
        staff = get_user_model().objects.create_user("review-unprivileged", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.post(url, {"expected_revision_id": self.revision.pk}).status_code, 403)
        self.client.force_login(actor)
        response = self.client.post(url, {"expected_revision_id": self.revision.pk})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["response_debt"]["required"])
        detail = reverse("management_bot_client_detail_api", args=[self.client_row.pk])
        self.assertFalse(self.client.get(detail).json()["client"]["response_debt"]["required"])
        self.assertFalse(self.client.get(detail, {"after_id": self.source.pk}).json()["response_debt"]["required"])

    def test_review_and_delivery_resolution_do_not_overwrite_each_other(self):
        from management.services.ig_response_debt import review_reply_debt
        actor = self._review_actor()
        task = record_reply_debt(self.revision, "generation_failed")
        resolve_delivered_reply_debt(self.revision)
        result = review_reply_debt(self.client_row.pk, task.pk, actor=actor,
                                   expected_revision_id=self.revision.pk)
        self.assertTrue(result["idempotent"])
        task.refresh_from_db()
        self.assertEqual(task.status, "completed")
        self.assertNotIn("operator_review", task.manager_context)
