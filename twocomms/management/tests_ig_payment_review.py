from django.test import SimpleTestCase


class IgPaymentReviewRulesTests(SimpleTestCase):
    def test_customer_payment_statement_is_review_evidence_not_provider_paid(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence(
            [
                {"id": 10, "role": "user", "text": "Я вже оплатила, чек у вкладенні"},
                {"id": 11, "role": "manager", "text": "Добре, перевірю"},
            ]
        )
        self.assertTrue(result["needs_review"])
        self.assertFalse(result["provider_confirmed"])
        self.assertEqual(result["message_ids"], [10])

    def test_reaction_and_payment_link_do_not_create_review(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence(
            [
                {"id": 10, "role": "user", "text": "🔥"},
                {"id": 11, "role": "model", "text": "Ось посилання на оплату"},
            ]
        )
        self.assertFalse(result["needs_review"])

    def test_confirmation_transition_is_idempotent_and_cancel_is_terminal(self):
        from management.services.ig_payment_review import next_review_status

        self.assertEqual(next_review_status("pending", "confirm"), "confirmed")
        self.assertEqual(next_review_status("confirmed", "confirm"), "confirmed")
        self.assertEqual(next_review_status("confirmed", "cancel"), "cancelled")
        self.assertEqual(next_review_status("cancelled", "confirm"), "cancelled")
