"""Actual delivered clarification in the bounded admin/Gemini capture."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from management import tests_ig_payment_reminder_dialog as dialog_tests
from management.services.ig_admin_state_capture import current_admin_state
from management.services.ig_client_state_card import render_client_state_prompt
from management.services.ig_payment_reminder_context import SLOT, payment_reminder_manifest


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test",
    IG_PAYMENT_REMINDERS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativePaymentReminderTimeAdminTests(TransactionTestCase):
    reset_sequences = True
    quoted = dialog_tests.NativePaymentReminderTimeDialogTests.quoted
    source = dialog_tests.NativePaymentReminderTimeDialogTests.source
    revision = dialog_tests.NativePaymentReminderTimeDialogTests.revision
    execute_source = dialog_tests.NativePaymentReminderTimeDialogTests.execute_source
    clarification = dialog_tests.NativePaymentReminderTimeDialogTests.clarification
    pay = dialog_tests.NativePaymentReminderTimeDialogTests.pay

    def setUp(self):
        dialog_tests.NativePaymentReminderTimeDialogTests.setUp(self)

    def test_delivered_time_question_is_visible_unscheduled_in_admin_and_prompt(self):
        self.clarification()
        now = self.now + timedelta(seconds=3)
        with patch("django.utils.timezone.now", return_value=now), \
            patch("management.services.instagram_bot._provider_http") as provider, \
            patch("management.services.call_ai_analysis.gemini_generate_text") as gemini:
            capture = current_admin_state(self.client_row.pk, now=now)
        provider.assert_not_called()
        gemini.assert_not_called()
        self.assertEqual(capture.status, "captured", capture.as_dict())
        self.assertLessEqual(capture.read_queries, 96)
        slot = capture.state.as_dict()["slots"][SLOT]
        self.assertEqual(slot["status"], "confirmed", {"slot": slot, "capture": capture.as_dict()})
        self.assertEqual(slot["value"]["state"], "awaiting_time")
        self.assertFalse(slot["value"]["business_agreement"])
        self.assertIsNone(slot["value"]["scheduled_for"])
        prompt = render_client_state_prompt(capture.state, budget=2400)
        self.assertFalse(prompt.oversized, prompt.as_dict())
        self.assertIn(SLOT, prompt.included)
        self.assertEqual(payment_reminder_manifest(slot)["scheduled_for_epoch"], 0)

    def test_payment_during_final_receipt_read_cannot_leave_time_question_actionable(self):
        from management.services import ig_payment_reminder_dialog as dialog
        from management.services.ig_commerce_projection import capture_current_selection_lines
        self.clarification()
        now = self.now + timedelta(seconds=3)
        cart = capture_current_selection_lines(self.client_row.pk, now=now)
        original, checks = dialog._prompt_effect, []

        def payment_after_snapshot(row, **kwargs):
            checks.append(row.pk)
            if len(checks) == 2:
                with patch("management.services.ig_lifecycle.dispatch_lifecycle_event", return_value="pending"):
                    self.pay()  # Real settlement, with its unrelated outgoing job held in this fixture.
            return original(row, **kwargs)

        with patch.object(dialog, "_prompt_effect", side_effect=payment_after_snapshot), \
            patch("management.services.instagram_bot._provider_http") as provider:
            result = dialog.projection(self.client_row.pk, now=now, current_source_cart=cart)
        provider.assert_not_called()
        self.assertGreaterEqual(len(checks), 2)
        self.assertIsNone(result)
