"""QA23: a native product decision is not an event-policy continuation."""
from django.test import Client, TestCase, override_settings

from management.bot_access import VIEW_IG_CONVERSATION_PII_PERMISSION
from management.models import IgFollowUpTask
from management import tests_ig_product_inquiry_presentation as inquiry_tests
from management.tests_ig_clients_ui import _operator
from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(
    ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    IG_TURN_CONTEXT_MODE="unified", IG_CLIENT_STATE_PROMPT_TOKENS=20000,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, SITE_BASE_URL="https://twocomms.test",
)
class ContextActionProjectionTests(_RestockFixture, TestCase):
    case = inquiry_tests.ProductInquiryPresentationTests.case
    endpoint = inquiry_tests.ProductInquiryPresentationTests.endpoint
    detail_row = inquiry_tests.ProductInquiryPresentationTests.detail_row

    def setUp(self):
        super().setUp()
        self.operator = _operator("qa23-context-viewer", VIEW_IG_CONVERSATION_PII_PERMISSION)
        self.client = Client(HTTP_HOST="management.twocomms.shop")
        self.client.force_login(self.operator)

    def test_native_product_reviews_do_not_offer_generic_policy_continuation(self):
        from unittest.mock import patch

        with patch("management.services.instagram_bot._provider_http") as provider:
            for question, reason in (
                ("Хочу оливкову футболку", "product_configuration_review"),
                ("Які люверси на цьому худі?", "product_information_review"),
            ):
                with self.subTest(reason=reason):
                    task, _ = self.case(question, reason)
                    row = self.detail_row(task)
                    self.assertEqual(row["continue_url"], "")
                    self.assertEqual(row["status_label"], "Потребує перевірки")
                    self.assertIn(question, row["message_text"])
                    task.refresh_from_db()
                    self.assertEqual(task.manager_approval_status, "pending")
                    self.assertEqual(task.status, "skipped")
            provider.assert_not_called()

    def test_existing_event_policy_keeps_its_explicit_continuation(self):
        task = IgFollowUpTask.objects.create(
            client=self.client_row, due_at=self.now, status="completed", kind="payment",
            trigger="event", reason="invoice_expired", event_key="qa23-invoice-policy",
            event_occurred_at=self.now, policy_started_at=self.now, policy_version="test-v1",
            event_payload={"event": "invoice_expired"},
        )
        row = self.detail_row(task)
        self.assertIn(f"/{task.pk}/", row["continue_url"])
        self.assertTrue(row["continue_url"])
