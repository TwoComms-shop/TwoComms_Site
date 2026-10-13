"""Owned customer inquiry -> recorded manager case -> authorized detail JSON."""
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from management.bot_access import (
    META_REVIEWER_GROUP_NAME,
    VIEW_IG_CONVERSATION_PII_PERMISSION,
)
from management.models import IgFollowUpTask
from management.services import ig_product_inquiries as inquiries
from management.services.ig_response_plan import capture_response_plan
from management.services.ig_revision_intents import ensure_product_inquiry_review_case
from management.tests_ig_clients_ui import _operator
from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(
    ROOT_URLCONF="twocomms.urls_management",
    SECURE_SSL_REDIRECT=False,
    IG_TURN_CONTEXT_MODE="unified",
    IG_CLIENT_STATE_PROMPT_TOKENS=20000,
    GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False,
    SITE_BASE_URL="https://twocomms.test",
)
class ProductInquiryPresentationTests(_RestockFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.operator = _operator(
            "product-inquiry-presentation-viewer", VIEW_IG_CONVERSATION_PII_PERMISSION,
        )
        self.client = Client(HTTP_HOST="management.twocomms.shop")
        self.client.force_login(self.operator)

    def endpoint(self):
        return reverse("management_bot_client_detail_api", args=[self.client_row.pk])

    def case(self, text, reason):
        source = self.source(text)
        revision, _token = self.revision(source)
        revision.client = self.client_row
        plan = capture_response_plan(self.client_row, revision=revision)
        disposition = inquiries.product_inquiries_for_revision(revision, response_plan=plan)
        self.assertEqual(disposition["status"], "manager_required", disposition)
        self.assertEqual(disposition["reason"], reason)
        with transaction.atomic():
            task, notification = ensure_product_inquiry_review_case(revision, disposition)
        self.assertEqual(task.reason, "revision_case:" + reason)
        self.assertEqual(task.manager_context["product_inquiries"], disposition)
        self.assertIn(source.pk, [row["message_id"] for row in disposition["source_refs"]])
        self.assertEqual(task.status, IgFollowUpTask.Status.SKIPPED)
        self.assertEqual(task.manager_approval_status, IgFollowUpTask.ManagerApprovalStatus.PENDING)
        self.assertFalse(task.manager_context["authority"]["checkout_authorized"])
        self.assertFalse(task.manager_context["authority"]["stock_confirmed"])
        return task, notification

    def detail_row(self, task):
        response = self.client.get(self.endpoint())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("no-store", response.headers["Cache-Control"])
        return next(row for row in response.json()["followups"] if row["id"] == task.pk)

    def test_actual_configuration_and_information_cases_have_human_pending_labels_without_provider_io(self):
        cases = (
            ("Хочу оливкову футболку", "product_configuration_review", "Перевірка параметрів товару"),
            ("Які люверси на цьому худі?", "product_information_review", "Уточнення деталей товару"),
        )
        with patch("management.services.instagram_bot._provider_http") as provider:
            for question, reason, label in cases:
                with self.subTest(reason=reason):
                    task, notification = self.case(question, reason)
                    row = self.detail_row(task)
                    self.assertEqual(row["reason"], task.reason)
                    self.assertEqual(row["reason_label"], label)
                    self.assertEqual(row["status_label"], "Потребує перевірки")
                    self.assertEqual(row["status"], IgFollowUpTask.Status.SKIPPED)
                    self.assertEqual(row["message_text"], task.message_text)
                    self.assertIn("Не замінюйте колір чи розмір", row["message_text"])
                    self.assertEqual(row["provider_message_id"], "")
                    self.assertEqual(row["attempt_count"], 0)
                    self.assertEqual(notification.client_id, self.client_row.pk)
            provider.assert_not_called()

    def test_resolved_native_case_preserves_status_instead_of_displaying_pending_review(self):
        with patch("management.services.instagram_bot._provider_http") as provider:
            task, _notification = self.case("Які люверси на цьому худі?", "product_information_review")
            task.status = IgFollowUpTask.Status.COMPLETED
            task.manager_approval_status = IgFollowUpTask.ManagerApprovalStatus.APPROVED
            task.save(update_fields=["status", "manager_approval_status"])
            row = self.detail_row(task)
            self.assertEqual(row["status_label"], task.get_status_display())
            self.assertEqual(row["reason_label"], "Уточнення деталей товару")
            self.assertEqual(row["message_text"], task.message_text)
            provider.assert_not_called()

    def test_staff_without_pii_permission_and_meta_reviewer_cannot_read_actual_case(self):
        with patch("management.services.instagram_bot._provider_http") as provider:
            task, _notification = self.case("Які люверси на цьому худі?", "product_information_review")
            unprivileged = _operator("product-inquiry-no-capability")
            reviewer = _operator("product-inquiry-meta-reviewer", VIEW_IG_CONVERSATION_PII_PERMISSION)
            reviewer.groups.add(Group.objects.create(name=META_REVIEWER_GROUP_NAME))
            for operator in (unprivileged, reviewer):
                with self.subTest(operator=operator.username):
                    self.client.force_login(operator)
                    response = self.client.get(self.endpoint())
                    self.assertEqual(response.status_code, 403, response.content)
                    self.assertNotIn("followups", response.json())
                    self.assertNotIn(task.message_text.encode(), response.content)
            provider.assert_not_called()

    def test_erasure_fence_hides_actual_inquiry_case_even_from_authorized_viewer(self):
        with patch("management.services.instagram_bot._provider_http") as provider:
            task, _notification = self.case("Які люверси на цьому худі?", "product_information_review")
            self.client_row.privacy_erasure_started_at = self.now
            self.client_row.save(update_fields=["privacy_erasure_started_at"])
            response = self.client.get(self.endpoint())
            self.assertEqual(response.status_code, 410, response.content)
            self.assertEqual(response.json(), {"success": False, "code": "client_erasing"})
            self.assertNotIn(task.message_text.encode(), response.content)
            provider.assert_not_called()
