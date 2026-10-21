"""A delivered time question is readable context, never scheduled consent."""
from copy import deepcopy

from django.test import SimpleTestCase

from management.services.ig_payment_reminder_context import SLOT, VERSION, payment_reminder_manifest
from management.services.ig_client_state_card import render_client_state_prompt
from management.services.ig_request_manifest import capture_request_context, sanitize_request_context
from management.services.gemini_accounting_contract import RequestPolicyManifestError
from management import tests_ig_payment_reminder_context as context_tests


class PaymentReminderTimeContextTests(SimpleTestCase):
    slot = context_tests.PaymentReminderContextTests.slot
    state = context_tests.PaymentReminderContextTests.state

    def setUp(self):
        context_tests.PaymentReminderContextTests.setUp(self)
        self.projection.update(status="awaiting_time", due_at=None, due_at_kyiv=None,
            task_id=None, invitation_id=None, prompt_receipt_confirmed=True)
        self.projection["source_ids"].update(answer=None, send=None)
        self.projection["source_times"].update(answer=None, send=None)

    def test_prompt_admin_and_manifest_share_unscheduled_state_without_sql(self):
        slot = self.slot()
        self.assertEqual(slot["status"], "confirmed")
        self.assertEqual(slot["value"]["state"], "awaiting_time")
        self.assertFalse(slot["value"]["business_agreement"])
        self.assertIsNone(slot["value"]["scheduled_for"])
        state = self.state(slot)
        self.assertEqual(state.as_dict()["slots"][SLOT]["value"]["state"], "awaiting_time")
        prompt = render_client_state_prompt(state, budget=2400)
        self.assertIn('"state":"awaiting_time"', prompt.text)
        self.assertIn('"business_agreement":false', prompt.text)
        snapshot = payment_reminder_manifest(slot)
        self.assertEqual(snapshot["scheduled_for_epoch"], 0)
        context = capture_request_context(payload={}, metadata={"payment_reminder_snapshot": snapshot,
            "view_versions": {"payment_reminder_context": VERSION}})
        self.assertEqual(sanitize_request_context(context)["payment_reminder_snapshot"], snapshot)

    def test_unproven_or_scheduled_time_question_is_not_admitted(self):
        for change in ({"prompt_receipt_confirmed": False}, {"due_at": "2026-10-12T12:00:00+00:00"},
                {"task_id": 99}, {"invitation_id": "foreign"}):
            with self.subTest(change=change):
                self.assertEqual(self.slot({**deepcopy(self.projection), **change})["status"], "unknown")
        for role in ("card", "answer", "send"):
            raw = deepcopy(self.projection)
            raw["source_ids"][role] = None if role == "card" else 90
            with self.subTest(role=role):
                self.assertEqual(self.slot(raw)["status"], "unknown")

    def test_manifest_zero_time_is_exclusive_to_verified_time_question(self):
        snapshot = payment_reminder_manifest(self.slot())
        for change in ({"scheduled_for_epoch": 1}, {"source_message_ids": [20]},
                {"status": "accepted"}, {"status": "sent"}, {"source_message_ids": [20, 21, 22]}):
            with self.subTest(change=change), self.assertRaises(RequestPolicyManifestError):
                capture_request_context(payload={}, metadata={"payment_reminder_snapshot": {**snapshot, **change}})
