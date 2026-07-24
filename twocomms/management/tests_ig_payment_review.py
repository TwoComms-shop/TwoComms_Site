from django.test import SimpleTestCase


class IgPaymentReviewRulesTests(SimpleTestCase):
    def test_manager_payment_instructions_do_not_count_as_customer_payment(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence(
            [
                {
                    "id": 237,
                    "role": "manager",
                    "text": "Оплата на рахунок ФОП. Сума: 2100 грн",
                },
            ]
        )
        self.assertFalse(result["needs_review"])
        self.assertEqual(result["evidence"], [])

    def test_receipt_and_negotiated_order_draft_keep_roles_and_uncertainty(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence(
            [
                {
                    "id": 233,
                    "role": "user",
                    "text": "Мені потрібно 2 футболки: 1. Базова s 2. Оверсайз xs. Принт однаковий",
                },
                {
                    "id": 237,
                    "role": "manager",
                    "text": "Оплата на рахунок ФОП. Сума: 2100 грн",
                },
                {
                    "id": 238,
                    "role": "user",
                    "text": "(зображення)",
                    "attachments": "[https://lookaside.fbsbx.com/receipt.jpg]",
                },
            ]
        )
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["message_ids"], [238])
        self.assertEqual(result["order_draft"]["quoted_total"], "2100")
        self.assertEqual(
            [(item["fit"], item["size"], item["qty"]) for item in result["order_draft"]["items"]],
            [("classic", "S", 1), ("oversize", "XS", 1)],
        )
        self.assertIn("catalog_product_not_identified", result["order_draft"]["uncertainty_reasons"])
        self.assertEqual(result["amount_evidence"][0]["role"], "manager")
        self.assertEqual(result["order_draft"]["delivery"], {
            "full_name": "",
            "phone": "",
            "city": "",
            "office": "",
        })

    def test_receipt_before_manager_payment_context_is_still_reviewed(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence(
            [
                {"id": 238, "role": "user", "text": "(зображення)", "attachments": "receipt.jpg"},
                {"id": 237, "role": "manager", "text": "Оплата на рахунок ФОП. Сума: 2100 грн"},
            ]
        )
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["message_ids"], [238])

    def test_packaging_preference_requires_manager_package_context(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence(
            [
                {"id": 241, "role": "manager", "text": "Футболки в різні зіп пакети чи можна в один?"},
                {"id": 242, "role": "user", "text": "В різні"},
                {"id": 238, "role": "user", "text": "(зображення)", "attachments": "receipt.jpg"},
            ]
        )
        self.assertEqual(result["order_draft"]["packaging_preference"], "Окремі пакети")

    def test_delivery_lines_are_preserved_for_editable_order_draft(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence(
            [
                {
                    "id": 236,
                    "role": "user",
                    "text": "Харків, поштомат 21586\nНіколаєнко Яна\n0502034719\n\nПо повній передоплаті",
                },
                {"id": 238, "role": "user", "text": "(зображення)", "attachments": "receipt.jpg"},
            ]
        )
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["order_draft"]["delivery"], {
            "full_name": "Ніколаєнко Яна",
            "phone": "0502034719",
            "city": "Харків",
            "office": "Поштомат 21586",
        })

    def test_delivery_parser_ignores_short_followup_and_reads_slash_separated_name(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence(
            [
                {
                    "id": 236,
                    "role": "user",
                    "text": "Харків, поштомат 21586 / Ніколаєнко Яна / 0502034719 / По повній передоплаті",
                },
                {"id": 238, "role": "user", "text": "(зображення)", "attachments": "receipt.jpg"},
                {"id": 242, "role": "user", "text": "В різні"},
            ]
        )
        self.assertEqual(result["order_draft"]["delivery"]["full_name"], "Ніколаєнко Яна")

    def test_manager_alert_preserves_conversation_total_and_uncertainty(self):
        from types import SimpleNamespace

        from management.services.ig_payment_review import _alert_text

        review = SimpleNamespace(
            pk=42,
            evidence={
                "order_draft": {
                    "quoted_total": "2100",
                    "currency": "UAH",
                    "items": [{"title": "Базова футболка", "size": "S", "qty": 1}],
                    "uncertainty_reasons": ["catalog_product_not_identified"],
                },
            },
        )
        client = SimpleNamespace(display_name="Яна", username="yana", igsid="1735898131060065")
        text = _alert_text(review, client)
        self.assertIn("2100 грн", text)
        self.assertIn("Базова футболка · S · 1 шт.", text)
        self.assertIn("товар не зіставлено з каталогом", text)
        self.assertIn("management.twocomms.shop/bot/", text)
        self.assertNotIn("ціна сайту", text)

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
