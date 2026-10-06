import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone


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
                {"id": 238, "role": "user", "text": "Я оплатила, ось чек", "attachments": "receipt.jpg"},
                {"id": 237, "role": "manager", "text": "Оплата на рахунок ФОП. Сума: 2100 грн"},
            ]
        )
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["message_ids"], [238])

    def test_product_post_and_receipt_are_separate_media_roles(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence([
            {
                "id": 235,
                "role": "user",
                "text": "Принт ось цей",
                "media": [{"url": "https://cdn/product.jpg", "type": "ig_post", "title": "Біла футболка 1654"}],
            },
            {"id": 237, "role": "manager", "text": "Оплата на рахунок ФОП. Сума: 2100 грн"},
            {
                "id": 238,
                "role": "user",
                "text": "(зображення)",
                "media": [{"url": "https://cdn/receipt.jpg", "type": "image"}],
            },
        ])
        self.assertEqual(
            [(item["url"], item["role"]) for item in result["media"]],
            [("https://cdn/product.jpg", "product"), ("https://cdn/receipt.jpg", "payment_candidate")],
        )
        self.assertEqual(result["evidence"][0]["media"][0]["role"], "payment_candidate")

    @patch("management.services.ig_payment_review._raw_media_by_mid", return_value={
        "mid-product": [{"url": "https://cdn/product.jpg", "type": "ig_post", "raw_event_id": 437}],
    })
    def test_raw_event_media_is_recovered_when_normalized_attachment_is_empty(self, _raw):
        from management.services.ig_payment_review import _augment_messages_with_raw_media

        client = SimpleNamespace(igsid="1735898131060065")
        rows = _augment_messages_with_raw_media(client, [{"id": 235, "mid": "mid-product", "attachments": ""}])
        self.assertEqual(rows[0]["media"][0]["type"], "ig_post")
        self.assertIn("product.jpg", rows[0]["attachments"])

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
        delivery = result["order_draft"]["delivery"]
        self.assertEqual({key: value for key, value in delivery.items() if key != "office"}, {
            "full_name": "Ніколаєнко Яна",
            "phone": "0502034719",
            "city": "Харків",
        })
        self.assertEqual(delivery["office"].casefold().replace("№", "").replace(" ", ""), "поштомат21586")

    def test_customer_prepayment_context_is_bounded_to_the_next_unlabelled_image(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence([
            {"id": 1, "role": "user", "text": "По повній передоплаті"},
            {"id": 2, "role": "user", "text": "Добре"},
            {
                "id": 3,
                "role": "user",
                "text": "(зображення)",
                "attachments": '["https://cdn.example/later-product.jpg"]',
            },
        ])

        self.assertFalse(result["needs_review"])
        self.assertEqual(result["media"][0]["role"], "other")

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

    def test_manager_alert_keeps_only_typed_total_and_review_reference(self):
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
        self.assertIn("Review ID: 42", text)
        self.assertIn("Сума: 2100.00", text)
        self.assertIn("management.twocomms.shop/bot/?payment_review=42", text)
        self.assertNotIn("Яна", text)
        self.assertNotIn("yana", text)
        self.assertNotIn("1735898131060065", text)
        self.assertNotIn("Базова футболка", text)
        self.assertNotIn("catalog_product_not_identified", text)

    def test_single_line_conversation_total_prefills_negotiated_unit_price(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence([
            {"id": 1, "role": "user", "text": "Беру базову S за 790 грн"},
            {"id": 2, "role": "user", "text": "Я вже оплатила, ось чек", "attachments": "receipt.jpg"},
        ])
        self.assertEqual(result["order_draft"]["items"][0]["unit_price"], "790.00")

    def test_multi_line_total_requires_manual_price_allocation(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence([
            {"id": 1, "role": "user", "text": "Базова S та oversize XS, разом 2100 грн"},
            {"id": 2, "role": "user", "text": "Я вже оплатила, ось чек", "attachments": "receipt.jpg"},
        ])
        self.assertIn("conversation_price_allocation_required", result["order_draft"]["uncertainty_reasons"])

    def test_prepaid_receipt_amount_does_not_replace_agreed_order_total(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence([
            {"id": 1, "role": "user", "text": "Базова S та oversize XS"},
            {"id": 2, "role": "manager", "text": "Сума замовлення разом 2100 грн"},
            {"id": 3, "role": "user", "text": "Так, погоджуюсь"},
            {"id": 4, "role": "user", "text": "Оплатила передоплату 200 грн, ось чек", "attachments": "receipt.jpg"},
        ])
        self.assertEqual(result["order_draft"]["quoted_total"], "2100")
        self.assertEqual(
            [(item["amount"], item["kind"]) for item in result["amount_evidence"]],
            [("2100", "order_total"), ("200", "payment_evidence")],
        )

    def test_multiple_amounts_in_one_message_keep_independent_meanings(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence([
            {
                "id": 237,
                "role": "manager",
                "text": "Сума замовлення 2100 грн, передоплата 200 грн на рахунок ФОП",
            },
        ])

        self.assertEqual(result["order_draft"]["quoted_total"], "2100")
        self.assertEqual(
            [(item["amount"], item["kind"]) for item in result["amount_evidence"]],
            [("2100", "order_total"), ("200", "payment_evidence")],
        )

    def test_completed_transfer_amount_does_not_replace_agreed_order_total(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence([
            {"id": 1, "role": "manager", "text": "Ціна футболки 790 грн"},
            {"id": 2, "role": "user", "text": "Так, оформлюйте"},
            {"id": 3, "role": "user", "text": "Переказ зроблено, сума 200 грн"},
        ])

        self.assertTrue(result["needs_review"])
        self.assertEqual(Decimal(result["order_draft"]["quoted_total"]), Decimal("790.00"))
        self.assertEqual(
            [(item["amount"], item["kind"]) for item in result["amount_evidence"]],
            [("790", "unit_price"), ("200", "payment_evidence")],
        )

    def test_manager_alert_has_review_button_and_redacted_media_counts(self):
        from management.services.ig_payment_review import _alert_text, _review_keyboard

        review = SimpleNamespace(
            pk=42,
            evidence={
                "order_draft": {"quoted_total": "2100", "items": [{"title": "Базова футболка", "size": "S", "qty": 1}]},
                "media": [{"role": "product"}, {"role": "receipt"}],
                "catalog_match": {"status": "matched", "title": "Футболка «Харків Вокзальна»", "confidence": 0.94, "url": "https://twocomms.shop/product/kharkiv/"},
            },
        )
        client = SimpleNamespace(display_name="Яна", username="yana", igsid="1735898131060065")
        text = _alert_text(review, client)
        self.assertIn("RECEIPT_EVIDENCE: 1", text)
        self.assertIn("UNRESOLVED_MEDIA: 0", text)
        self.assertNotIn("Яна", text)
        self.assertNotIn("yana", text)
        self.assertNotIn("1735898131060065", text)
        self.assertNotIn("Футболка «Харків Вокзальна»", text)
        self.assertNotIn("https://twocomms.shop/product/kharkiv/", text)
        keyboard = _review_keyboard(review)
        self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "Підтвердити оплату 2100.00 грн")
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "igpay:confirm:42")
        self.assertEqual(keyboard["inline_keyboard"][0][1]["callback_data"], "igpay:cancel:42")
        self.assertEqual(keyboard["inline_keyboard"][1][0]["text"], "Відкрити перевірку")
        self.assertEqual(keyboard["inline_keyboard"][2][0]["text"], "Товар: Футболка «Харків Вокзальна»")

    def test_telegram_keyboard_requires_unambiguous_amount_for_direct_confirm(self):
        from management.services.ig_payment_review import _review_keyboard

        review = SimpleNamespace(pk=43, evidence={})
        keyboard = _review_keyboard(review)
        callback_buttons = [
            button
            for row in keyboard["inline_keyboard"]
            for button in row
            if button.get("callback_data", "").startswith("igpay:confirm:")
        ]

        self.assertEqual(callback_buttons, [])
        self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "Відкрити перевірку")

    def test_confirmation_candidate_has_stable_evidence_digest(self):
        from management.services.ig_payment_review import resolve_review_payment_amount

        review = SimpleNamespace(
            pk=44,
            watermark_message_id=91,
            evidence={
                "amount_evidence": [
                    {"kind": "payment_evidence", "amount": "500", "message_id": 91},
                ],
                "order_draft": {"quoted_total": "2100", "currency": "UAH"},
            },
        )

        first = resolve_review_payment_amount(review)
        second = resolve_review_payment_amount(review)

        self.assertEqual(first["amount"], Decimal("500.00"))
        self.assertEqual(first["scope"], "prepayment")
        self.assertEqual(first["source"], "review_payment_evidence")
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(len(first["digest"]), 64)

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

    def test_waiting_verbs_do_not_count_as_receipt_evidence(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence(
            [
                {"id": 227, "role": "user", "text": "Чекаю розмірну сітку"},
                {"id": 234, "role": "user", "text": "Почекаємо"},
            ]
        )
        self.assertFalse(result["needs_review"])

    def test_confirmation_transition_is_idempotent_and_cancel_is_terminal(self):
        from management.services.ig_payment_review import next_review_status

        self.assertEqual(next_review_status("pending", "confirm"), "confirmed")
        self.assertEqual(next_review_status("confirmed", "confirm"), "confirmed")
        self.assertEqual(next_review_status("confirmed", "cancel"), "cancelled")
        self.assertEqual(next_review_status("cancelled", "confirm"), "cancelled")


class PaymentReviewEpisodeScopeTests(TestCase):
    def _backdate_legacy_review(self, review):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.services.ig_payment_review import LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF

        created_at = LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF - timedelta(seconds=1)
        IgPaymentConfirmationReview.objects.filter(pk=review.pk).update(
            created_at=created_at,
        )
        review.created_at = created_at
        return review

    def test_claim_anchor_prefers_receipt_source_id_over_temporary_url(self):
        from management.services.ig_payment_review import payment_review_claim_anchor

        first = {
            "evidence": [{
                "message_id": 238,
                "role": "user",
                "quote": "Я оплатила, ось чек",
                "media": [{
                    "role": "payment_candidate",
                    "message_id": 238,
                    "url": "https://cdn.test/receipt.jpg?expires=one",
                }],
            }],
        }
        refreshed_url = {
            "evidence": [{
                **first["evidence"][0],
                "media": [{
                    "role": "payment_candidate",
                    "message_id": 238,
                    "url": "https://cdn.test/receipt.jpg?expires=two",
                }],
            }],
        }

        self.assertEqual(
            payment_review_claim_anchor(first),
            payment_review_claim_anchor(refreshed_url),
        )

    def test_claim_anchor_ignores_rotating_attachment_url_when_message_id_known(self):
        from management.services.ig_payment_review import payment_review_claim_anchor

        first = {
            "evidence": [{
                "message_id": 238,
                "role": "user",
                "quote": "Я оплатила, ось чек",
                "attachments": "https://cdn.test/receipt.jpg?expires=one",
            }],
        }
        refreshed_url = {
            "evidence": [{
                **first["evidence"][0],
                "attachments": "https://cdn.test/receipt.jpg?expires=two",
            }],
        }

        self.assertEqual(
            payment_review_claim_anchor(first),
            payment_review_claim_anchor(refreshed_url),
        )

    def test_payment_fingerprint_ignores_legacy_amount_kind_drift(self):
        from management.services.ig_payment_review import payment_review_fingerprint

        evidence = {
            "amount_evidence": [
                {"amount": "2100", "message_id": 237, "kind": None},
            ],
            "media": [
                {"role": "receipt", "message_id": 238, "url": "https://cdn.test/receipt.jpg"},
            ],
            "order_draft": {
                "quoted_total": "2100",
                "currency": "UAH",
                "items": [
                    {"product_id": 111, "fit": "classic", "size": "S", "qty": 1},
                    {"product_id": 111, "fit": "oversize", "size": "XS", "qty": 1},
                ],
                "delivery": {"city": "Харків", "office": "Вокзальна"},
            },
        }
        reclassified = {
            **evidence,
            "amount_evidence": [
                {"amount": "2100", "message_id": 237, "kind": "order_total"},
            ],
        }

        self.assertEqual(
            payment_review_fingerprint(evidence),
            payment_review_fingerprint(reclassified),
        )

    def test_receipt_without_catalog_context_still_has_stable_fingerprint(self):
        from management.services.ig_payment_review import payment_review_fingerprint

        evidence = {
            "amount_evidence": [{"amount": "1760", "message_id": 1133}],
            "media": [
                {
                    "role": "receipt",
                    "message_id": 1136,
                    "url": "https://cdn.test/receipt-1136.jpg",
                }
            ],
            "order_draft": {
                "quoted_total": "",
                "currency": "UAH",
                "items": [],
                "delivery": {},
            },
        }

        self.assertTrue(payment_review_fingerprint(evidence))
        self.assertEqual(
            payment_review_fingerprint(evidence),
            payment_review_fingerprint({**evidence, "watermark_message_id": 1141}),
        )

    def test_duplicate_reconciliation_is_idempotent_after_canonicalization(self):
        from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
        from management.services.ig_payment_review import reconcile_duplicate_payment_review
        from orders.models import Order

        client = IgClient.get_or_create_for_sender("review-reconciliation-idempotent")
        order = Order.objects.create(
            full_name="Review Client",
            phone="380501112233",
            city="Київ",
            np_office="Відділення 1",
            total_sum=Decimal("2100.00"),
            payment_status="paid",
        )
        evidence = {
            "claim_anchor": "b" * 64,
            "amount_evidence": [{"amount": "2100", "message_id": 237}],
            "media": [{"role": "receipt", "message_id": 238, "url": "https://cdn.test/receipt.jpg"}],
            "order_draft": {
                "quoted_total": "2100",
                "currency": "UAH",
                "items": [{"product_id": 111, "fit": "classic", "size": "S", "qty": 1}],
                "delivery": {"city": "Київ", "office": "Відділення 1"},
            },
        }
        canonical = IgPaymentConfirmationReview.objects.create(
            client=client,
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
            order=order,
            evidence=evidence,
            dedupe_key="canonical-reconciliation-idempotent",
            watermark_message_id=242,
        )
        duplicate = IgPaymentConfirmationReview.objects.create(
            client=client,
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
            evidence=evidence,
            dedupe_key="duplicate-reconciliation-idempotent",
            watermark_message_id=238,
        )

        self.assertEqual(reconcile_duplicate_payment_review(duplicate).pk, canonical.pk)
        canonical.refresh_from_db()
        self.assertEqual(canonical.status, IgPaymentConfirmationReview.Status.CONFIRMED)
        self.assertIsNone(canonical.superseded_by_id)
        self.assertEqual(reconcile_duplicate_payment_review(canonical), None)
        duplicate.refresh_from_db()
        self.assertEqual(duplicate.superseded_by_id, canonical.pk)

    def test_later_watermark_reuses_strictly_identical_payment_review(self):
        from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
        from management.services.ig_payment_review import create_payment_review

        client = IgClient.get_or_create_for_sender("review-stable-payment-fingerprint")
        messages = [
            {"id": 233, "role": "user", "text": "Беру базову S за 790 грн"},
            {"id": 237, "role": "manager", "text": "До сплати 790 грн"},
            {
                "id": 238,
                "role": "user",
                "text": "Я оплатила, ось чек",
                "media": [{"url": "https://cdn.test/receipt-238.jpg", "type": "image"}],
            },
        ]
        with (
            patch(
                "management.services.ig_payment_review._persist_review_media",
                side_effect=lambda rows: rows,
            ),
            patch(
                "management.services.ig_payment_review._resolve_payment_media_candidates",
                side_effect=lambda rows: rows,
            ),
            patch("management.services.instagram_bot.notify_manager"),
            patch("management.services.ig_commercial_episodes.ensure_episode_for_review"),
        ):
            first = create_payment_review(client, watermark=238, messages=messages)
            replay = create_payment_review(client, watermark=242, messages=messages)

        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(IgPaymentConfirmationReview.objects.filter(client=client).count(), 1)

    @patch.dict(
        "os.environ",
        {
            "MANAGEMENT_TG_BOT_TOKEN": "test-token",
            "MANAGEMENT_TG_ADMIN_CHAT_ID": "777",
        },
        clear=False,
    )
    @patch(
        "management.services.instagram_bot._http",
        return_value=(200, json.dumps({"ok": True, "result": {"message_id": 91}})),
    )
    def test_payment_review_keeps_receipt_media_in_crm_without_telegram_photo(self, http):
        from management.ig_bot_models import (
            IgBotNotification,
            IgClient,
            IgPaymentConfirmationReview,
        )
        from management.services.ig_payment_review import create_payment_review

        client = IgClient.get_or_create_for_sender("review-private-receipt-boundary")
        receipt_url = "https://cdn.test/private-receipt-marker.jpg"
        messages = [
            {"id": 233, "role": "user", "text": "Беру базову S за 790 грн"},
            {"id": 237, "role": "manager", "text": "До сплати 790 грн"},
            {
                "id": 238,
                "role": "user",
                "text": "Я оплатила, ось чек",
                "media": [{"url": receipt_url, "type": "image"}],
            },
        ]
        with (
            patch(
                "management.services.ig_payment_review._persist_review_media",
                side_effect=lambda rows: rows,
            ),
            patch(
                "management.services.ig_payment_review._resolve_payment_media_candidates",
                side_effect=lambda rows: rows,
            ),
            patch("management.services.ig_commercial_episodes.ensure_episode_for_review"),
        ):
            review = create_payment_review(client, watermark=238, messages=messages)

        self.assertIsInstance(review, IgPaymentConfirmationReview)
        self.assertEqual(review.evidence["media"][0]["url"], receipt_url)
        notification = IgBotNotification.objects.get(dedupe_key=review.dedupe_key)
        self.assertNotIn("media", notification.payload)
        self.assertNotIn(receipt_url, json.dumps(notification.payload, ensure_ascii=False))
        self.assertFalse(any(call.args[0].endswith("/sendPhoto") for call in http.call_args_list))

    def test_later_context_reuses_receipt_review_without_reprocessing_media(self):
        from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
        from management.services.ig_payment_review import create_payment_review

        client = IgClient.get_or_create_for_sender("review-stable-claim-anchor")
        claim_messages = [
            {"id": 233, "role": "user", "text": "Беру базову S"},
            {"id": 237, "role": "manager", "text": "До сплати 790 грн"},
            {
                "id": 238,
                "role": "user",
                "text": "Я оплатила, ось чек",
                "media": [{"url": "https://cdn.test/receipt-238.jpg", "type": "image"}],
            },
        ]
        later_context = [
            *claim_messages,
            {"id": 242, "role": "manager", "text": "Доставка Новою поштою"},
            {"id": 243, "role": "user", "text": "Київ, відділення 1"},
        ]
        with (
            patch(
                "management.services.ig_payment_review._persist_review_media",
                side_effect=lambda rows: rows,
            ) as persist_media,
            patch(
                "management.services.ig_payment_review._resolve_payment_media_candidates",
                side_effect=lambda rows: rows,
            ) as resolve_media,
            patch("management.services.instagram_bot.notify_manager"),
            patch("management.services.ig_commercial_episodes.ensure_episode_for_review"),
        ):
            first = create_payment_review(client, watermark=238, messages=claim_messages)
            replay = create_payment_review(client, watermark=243, messages=later_context)

        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(IgPaymentConfirmationReview.objects.filter(client=client).count(), 1)
        self.assertEqual(persist_media.call_count, 1)
        self.assertEqual(resolve_media.call_count, 1)

    def test_receipt_added_after_payment_claim_reuses_pending_episode_review(self):
        from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
        from management.services.ig_payment_review import create_payment_review

        client = IgClient.get_or_create_for_sender("review-payment-claim-then-receipt")
        claim_messages = [
            {"id": 237, "role": "manager", "text": "До сплати 790 грн"},
            {"id": 238, "role": "user", "text": "Я оплатила 790 грн"},
        ]
        receipt_messages = [
            *claim_messages,
            {
                "id": 239,
                "role": "user",
                "text": "Ось чек",
                "media": [{"url": "https://cdn.test/receipt-239.jpg", "type": "image"}],
            },
        ]
        with (
            patch(
                "management.services.ig_payment_review._persist_review_media",
                side_effect=lambda rows: rows,
            ) as persist_media,
            patch(
                "management.services.ig_payment_review._resolve_payment_media_candidates",
                side_effect=lambda rows: rows,
            ) as resolve_media,
            patch("management.services.instagram_bot.notify_manager"),
            patch("management.services.ig_commercial_episodes.ensure_episode_for_review"),
        ):
            first = create_payment_review(client, watermark=238, messages=claim_messages)
            receipt = create_payment_review(client, watermark=239, messages=receipt_messages)

        self.assertEqual(receipt.pk, first.pk)
        self.assertEqual(IgPaymentConfirmationReview.objects.filter(client=client).count(), 1)
        self.assertEqual(persist_media.call_count, 2)
        self.assertEqual(resolve_media.call_count, 2)

    def test_pending_orderless_duplicate_is_superseded_without_deleting_audit(self):
        from management.ig_bot_models import (
            IgBotNotification,
            IgBotNotificationAudit,
            IgClient,
            IgPaymentConfirmationReview,
        )
        from management.services.ig_payment_review import reconcile_duplicate_payment_review

        client = IgClient.get_or_create_for_sender("review-pending-legacy-duplicate")
        evidence = {
            "claim_anchor": "c" * 64,
            "amount_evidence": [{"amount": "1760", "message_id": 1133}],
            "media": [
                {
                    "role": "receipt",
                    "message_id": 1136,
                    "url": "https://cdn.test/legacy-receipt.jpg",
                }
            ],
            "order_draft": {
                "quoted_total": "1760",
                "currency": "UAH",
                "items": [],
                "delivery": {},
            },
        }
        canonical = IgPaymentConfirmationReview.objects.create(
            client=client,
            evidence=evidence,
            dedupe_key="pending-legacy-canonical",
            watermark_message_id=1136,
        )
        duplicate = IgPaymentConfirmationReview.objects.create(
            client=client,
            evidence={
                **evidence,
                "order_draft": {**evidence["order_draft"], "delivery": {"city": "Київ"}},
            },
            dedupe_key="pending-legacy-duplicate",
            watermark_message_id=1141,
        )
        notification = IgBotNotification.objects.create(
            client=client,
            event_type="payment_review",
            dedupe_key=duplicate.dedupe_key,
        )

        result = reconcile_duplicate_payment_review(duplicate)

        self.assertEqual(result.pk, canonical.pk)
        duplicate.refresh_from_db()
        self.assertEqual(duplicate.status, IgPaymentConfirmationReview.Status.SUPERSEDED)
        self.assertEqual(duplicate.superseded_by_id, canonical.pk)
        notification.refresh_from_db()
        self.assertEqual(notification.status, IgBotNotification.Status.RESOLVED)
        self.assertTrue(
            IgBotNotificationAudit.objects.filter(
                notification=notification,
                action="supersede_duplicate",
            ).exists()
        )
        self.assertEqual(
            IgPaymentConfirmationReview.objects.filter(client=client).count(),
            2,
        )

    def test_distinct_receipt_after_closed_episode_creates_new_review(self):
        from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
        from management.services.ig_payment_review import create_payment_review

        client = IgClient.get_or_create_for_sender("review-repeat-payment-claim")
        first_messages = [
            {"id": 1, "role": "manager", "text": "До сплати 790 грн"},
            {
                "id": 2,
                "role": "user",
                "text": "Я оплатила, ось чек",
                "media": [{"url": "https://cdn.test/first-receipt.jpg", "type": "image"}],
            },
        ]
        second_messages = [
            {"id": 10, "role": "manager", "text": "До сплати 850 грн"},
            {
                "id": 11,
                "role": "user",
                "text": "Оплатила повторне замовлення, ось чек",
                "media": [{"url": "https://cdn.test/second-receipt.jpg", "type": "image"}],
            },
        ]
        with (
            patch(
                "management.services.ig_payment_review._persist_review_media",
                side_effect=lambda rows: rows,
            ),
            patch(
                "management.services.ig_payment_review._resolve_payment_media_candidates",
                side_effect=lambda rows: rows,
            ),
            patch("management.services.instagram_bot.notify_manager"),
        ):
            first = create_payment_review(client, watermark=2, messages=first_messages)
            episode = first.commercial_episode
            episode.open_slot = None
            episode.closed_at = timezone.now()
            episode.save(update_fields=["open_slot", "closed_at", "updated_at"])
            second = create_payment_review(client, watermark=11, messages=second_messages)

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(IgPaymentConfirmationReview.objects.filter(client=client).count(), 2)

    def test_replay_of_a_cancelled_claim_never_reopens_or_reprocesses_the_receipt(self):
        from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
        from orders.models import Order
        from management.services.ig_payment_review import (
            create_payment_review,
            reconcile_duplicate_payment_review,
        )

        client = IgClient.get_or_create_for_sender("review-terminal-canonical")
        messages = [
            {"id": 10, "role": "manager", "text": "До сплати 790 грн"},
            {
                "id": 11,
                "role": "user",
                "text": "Я оплатила, ось чек",
                "media": [{"url": "https://cdn.test/terminal-receipt.jpg", "type": "image"}],
            },
        ]
        with (
            patch(
                "management.services.ig_payment_review._persist_review_media",
                side_effect=lambda rows: rows,
            ) as persist_media,
            patch(
                "management.services.ig_payment_review._resolve_payment_media_candidates",
                side_effect=lambda rows: rows,
            ) as resolve_media,
            patch("management.services.instagram_bot.notify_manager"),
            patch("management.services.ig_commercial_episodes.ensure_episode_for_review"),
        ):
            canonical = create_payment_review(client, watermark=11, messages=messages)
            canonical.status = IgPaymentConfirmationReview.Status.CONFIRMED
            canonical.order = Order.objects.create(
                full_name="Cancelled canonical",
                phone="0500000011",
                city="Київ",
                np_office="1",
                total_sum=Decimal("790.00"),
            )
            canonical.save(update_fields=["status", "order", "updated_at"])
            duplicate = IgPaymentConfirmationReview.objects.create(
                client=client,
                status=IgPaymentConfirmationReview.Status.CONFIRMED,
                evidence=canonical.evidence,
                dedupe_key="terminal-canonical-duplicate",
                watermark_message_id=12,
            )
            reconcile_duplicate_payment_review(duplicate)
            canonical.status = IgPaymentConfirmationReview.Status.CANCELLED
            canonical.save(update_fields=["status", "updated_at"])

            replay = create_payment_review(client, watermark=13, messages=messages)

        duplicate.refresh_from_db()
        self.assertEqual(duplicate.status, IgPaymentConfirmationReview.Status.SUPERSEDED)
        self.assertEqual(replay.pk, canonical.pk)
        self.assertEqual(replay.status, IgPaymentConfirmationReview.Status.CANCELLED)
        self.assertEqual(IgPaymentConfirmationReview.objects.filter(client=client).count(), 2)
        self.assertEqual(persist_media.call_count, 1)
        self.assertEqual(resolve_media.call_count, 1)

    def test_historical_paid_archive_requires_verified_review_and_closes_episode(self):
        from django.contrib.auth import get_user_model

        from management.ig_bot_models import (
            IgBotNotification,
            IgClient,
            IgCommercialEpisode,
            IgPaymentConfirmationReview,
        )
        from management.services.ig_payment_review import (
            archive_historical_paid_review,
            record_review_decision,
        )

        actor = get_user_model().objects.create_user(
            "historical-paid-actor",
            password="x",
            is_staff=True,
        )
        client = IgClient.get_or_create_for_sender("historical-paid-client")
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="historical-paid-review",
            evidence={"order_draft": {"quoted_total": "1760.00"}},
            watermark_message_id=1136,
        )
        IgBotNotification.objects.create(
            client=client,
            event_type="payment_review",
            dedupe_key=review.dedupe_key,
            status=IgBotNotification.Status.SENT,
        )
        record_review_decision(
            review,
            actor=actor,
            decision="manager_verified",
            verification_scope="full_payment",
            confirmed_amount=Decimal("1760.00"),
        )

        archived = archive_historical_paid_review(
            review,
            actor=actor,
            reason="Historical fulfilled sale confirmed by operator",
        )

        archived.refresh_from_db()
        client.refresh_from_db()
        episode = IgCommercialEpisode.objects.get(primary_payment_review=review)
        notification = IgBotNotification.objects.get(dedupe_key=review.dedupe_key)
        self.assertEqual(
            archived.resolution_kind,
            IgPaymentConfirmationReview.ResolutionKind.HISTORICAL_PAID_ARCHIVED,
        )
        self.assertEqual(archived.resolved_by_id, actor.pk)
        self.assertIsNotNone(archived.resolved_at)
        self.assertEqual(client.stage, IgClient.Stage.PAID)
        self.assertEqual(episode.state, IgCommercialEpisode.State.FULFILLED)
        self.assertEqual(episode.outcome, "historical_paid_archived")
        self.assertIsNone(episode.open_slot)
        self.assertEqual(notification.status, IgBotNotification.Status.RESOLVED)

    def test_historical_completion_records_known_amount_without_order_side_effects(self):
        from django.contrib.auth import get_user_model

        from management.ig_bot_models import (
            IgBotNotification,
            IgClient,
            IgCommercialEpisode,
            IgMetaEventLog,
            IgOrderCustomerEvent,
            IgPaymentConfirmationReview,
            IgPaymentReviewDecision,
        )
        from management.services.ig_payment_review import (
            is_legacy_historical_payment_review,
            resolve_historical_paid_review,
        )
        from orders.models import Order

        actor = get_user_model().objects.create_user(
            "historical-completion-known-actor",
            password="x",
            is_staff=True,
        )
        client = IgClient.get_or_create_for_sender("historical-completion-known-client")
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="historical-completion-known-review",
            evidence={"amount_evidence": [{"kind": "payment_evidence", "amount": "1760"}]},
        )
        self._backdate_legacy_review(review)
        self.assertTrue(is_legacy_historical_payment_review(review))
        notification = IgBotNotification.objects.create(
            client=client,
            event_type="payment_review",
            dedupe_key=review.dedupe_key,
            status=IgBotNotification.Status.SENT,
        )
        baseline = {
            "orders": Order.objects.count(),
            "meta": IgMetaEventLog.objects.count(),
            "customer_events": IgOrderCustomerEvent.objects.count(),
        }

        resolved = resolve_historical_paid_review(
            review,
            actor=actor,
            outcome="already_received",
            reason="Historical sale confirmed by owner",
            confirmed_amount="1760.00",
        )

        resolved.refresh_from_db()
        client.refresh_from_db()
        notification.refresh_from_db()
        decision = IgPaymentReviewDecision.objects.get(review=review)
        episode = IgCommercialEpisode.objects.get(primary_payment_review=review)
        self.assertEqual(resolved.status, IgPaymentConfirmationReview.Status.CONFIRMED)
        self.assertEqual(
            resolved.resolution_kind,
            IgPaymentConfirmationReview.ResolutionKind.HISTORICAL_PAID_ARCHIVED,
        )
        self.assertEqual(resolved.resolution_outcome, "already_received")
        self.assertEqual(decision.verification_scope, "historical_fulfilled")
        self.assertEqual(decision.confirmed_amount, Decimal("1760.00"))
        self.assertIsNone(decision.order_total_amount)
        self.assertEqual(client.stage, "done")
        self.assertEqual(episode.state, IgCommercialEpisode.State.FULFILLED)
        self.assertEqual(notification.status, IgBotNotification.Status.RESOLVED)
        self.assertEqual(Order.objects.count(), baseline["orders"])
        self.assertEqual(IgMetaEventLog.objects.count(), baseline["meta"])
        self.assertEqual(IgOrderCustomerEvent.objects.count(), baseline["customer_events"])

        replay = resolve_historical_paid_review(
            review,
            actor=actor,
            outcome="already_received",
            reason="Historical sale confirmed by owner",
            confirmed_amount="1760.00",
        )
        self.assertEqual(replay.pk, resolved.pk)
        self.assertEqual(IgPaymentReviewDecision.objects.filter(review=review).count(), 1)
        with self.assertRaisesMessage(ValueError, "іншим результатом"):
            resolve_historical_paid_review(
                review,
                actor=actor,
                outcome="already_delivered",
                reason="Historical sale confirmed by owner",
                confirmed_amount="1760.00",
            )

    def test_historical_completion_rejects_fresh_pending_review(self):
        from django.contrib.auth import get_user_model

        from management.ig_bot_models import (
            IgClient,
            IgPaymentConfirmationReview,
            IgPaymentReviewDecision,
        )
        from management.services.ig_payment_review import (
            is_legacy_historical_payment_review,
            resolve_historical_paid_review,
        )

        actor = get_user_model().objects.create_user(
            "historical-completion-fresh-actor",
            password="x",
            is_staff=True,
        )
        client = IgClient.get_or_create_for_sender("historical-completion-fresh-client")
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="historical-completion-fresh-review",
        )

        self.assertFalse(is_legacy_historical_payment_review(review))
        with self.assertRaisesMessage(ValueError, "межі legacy-імпорту"):
            resolve_historical_paid_review(
                review,
                actor=actor,
                outcome="already_received",
                reason="This is a current pending review",
                amount_unrecoverable=True,
            )

        review.refresh_from_db()
        self.assertEqual(review.status, IgPaymentConfirmationReview.Status.PENDING)
        self.assertFalse(IgPaymentReviewDecision.objects.filter(review=review).exists())

    def test_historical_completion_keeps_unrecoverable_amount_null(self):
        from django.contrib.auth import get_user_model

        from management.ig_bot_models import (
            IgClient,
            IgCommercialEpisodeEvent,
            IgPaymentConfirmationReview,
            IgPaymentReviewDecision,
        )
        from management.services.ig_payment_review import resolve_historical_paid_review

        actor = get_user_model().objects.create_user(
            "historical-completion-unknown-actor",
            password="x",
            is_staff=True,
        )
        client = IgClient.get_or_create_for_sender("historical-completion-unknown-client")
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="historical-completion-unknown-review",
            evidence={},
        )
        self._backdate_legacy_review(review)

        resolved = resolve_historical_paid_review(
            review,
            actor=actor,
            outcome="completed_unknown",
            reason="Historical amount cannot be recovered",
            amount_unrecoverable=True,
        )

        decision = IgPaymentReviewDecision.objects.get(review=review)
        self.assertEqual(resolved.resolution_outcome, "completed_unknown")
        self.assertIsNone(decision.confirmed_amount)
        self.assertIsNone(decision.order_total_amount)
        self.assertEqual(decision.amount_source, "historical_amount_unrecoverable")
        event = IgCommercialEpisodeEvent.objects.get(event_type="historical_paid_archived")
        self.assertIsNone(event.evidence["confirmed_amount"])

    def test_historical_completion_does_not_close_newer_open_episode_for_zero_watermark_review(self):
        from django.contrib.auth import get_user_model

        from management.ig_bot_models import (
            IgClient,
            IgCommercialEpisode,
            IgPaymentConfirmationReview,
        )
        from management.services.ig_commercial_episodes import start_repeat_episode
        from management.services.ig_payment_review import resolve_historical_paid_review

        actor = get_user_model().objects.create_user(
            "historical-completion-current-episode-actor",
            password="x",
            is_staff=True,
        )
        client = IgClient.get_or_create_for_sender("historical-completion-current-episode-client")
        client.stage = IgClient.Stage.CHECKOUT
        client.save(update_fields=["stage", "updated_at"])
        current = start_repeat_episode(
            client,
            repeat_kind=IgCommercialEpisode.RepeatKind.REORDER,
            evidence_message_ids=[5001],
            confidence=Decimal("0.97"),
            analysis_model="gemini-test",
            analysis_prompt_version="repeat-v1",
        )
        client.refresh_from_db()
        expected_current_stage = client.stage
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="historical-completion-current-episode-review",
            watermark_message_id=0,
        )
        self._backdate_legacy_review(review)

        resolved = resolve_historical_paid_review(
            review,
            actor=actor,
            outcome="already_received",
            reason="An old sale was paid and received before the current conversation.",
            confirmed_amount="1760.00",
        )

        client.refresh_from_db()
        current.refresh_from_db()
        historical = resolved.commercial_episode
        self.assertEqual(client.stage, expected_current_stage)
        self.assertEqual(client.current_commercial_episode_id, current.pk)
        self.assertEqual(current.open_slot, 1)
        self.assertEqual(current.state, IgCommercialEpisode.State.ACTIVE)
        self.assertNotEqual(historical.pk, current.pk)
        self.assertIsNone(historical.open_slot)
        self.assertEqual(historical.state, IgCommercialEpisode.State.FULFILLED)

    def test_historical_completion_detaches_legacy_review_from_newer_open_episode(self):
        from django.contrib.auth import get_user_model

        from management.ig_bot_models import (
            IgClient,
            IgCommercialEpisode,
            IgPaymentConfirmationReview,
        )
        from management.services.ig_commercial_episodes import (
            start_repeat_episode,
            sync_episode_payment,
        )
        from management.services.ig_payment_review import resolve_historical_paid_review

        actor = get_user_model().objects.create_user(
            "historical-completion-polluted-episode-actor",
            password="x",
            is_staff=True,
        )
        client = IgClient.get_or_create_for_sender("historical-completion-polluted-episode-client")
        client.stage = IgClient.Stage.CHECKOUT
        client.save(update_fields=["stage", "updated_at"])
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="historical-completion-polluted-episode-review",
            watermark_message_id=0,
        )
        self._backdate_legacy_review(review)
        current = start_repeat_episode(
            client,
            repeat_kind=IgCommercialEpisode.RepeatKind.REORDER,
            evidence_message_ids=[5002],
            confidence=Decimal("0.97"),
            analysis_model="gemini-test",
            analysis_prompt_version="repeat-v1",
        )
        polluted = sync_episode_payment(review=review)
        self.assertEqual(polluted.pk, current.pk)
        current.refresh_from_db()
        self.assertEqual(current.primary_payment_review_id, review.pk)
        self.assertEqual(current.payment_snapshot["review_id"], review.pk)
        client.refresh_from_db()
        expected_current_stage = client.stage

        resolved = resolve_historical_paid_review(
            review,
            actor=actor,
            outcome="already_received",
            reason="An old sale was paid and received before the current conversation.",
            confirmed_amount="1760.00",
        )

        client.refresh_from_db()
        current.refresh_from_db()
        historical = resolved.commercial_episode
        self.assertEqual(client.stage, expected_current_stage)
        self.assertEqual(client.current_commercial_episode_id, current.pk)
        self.assertEqual(current.open_slot, 1)
        self.assertEqual(current.state, IgCommercialEpisode.State.ACTIVE)
        self.assertIsNone(current.primary_payment_review_id)
        self.assertIsNone(current.payment_snapshot["review_id"])
        self.assertIsNone(current.payment_snapshot["manager_decision_id"])
        self.assertNotEqual(historical.pk, current.pk)
        self.assertIsNone(historical.open_slot)
        self.assertEqual(historical.state, IgCommercialEpisode.State.FULFILLED)

    def test_historical_completion_rejects_non_staff_missing_reason_and_linked_order(self):
        from django.contrib.auth import get_user_model

        from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
        from management.services.ig_payment_review import resolve_historical_paid_review
        from orders.models import Order

        actor = get_user_model().objects.create_user(
            "historical-completion-nonstaff-actor",
            password="x",
        )
        client = IgClient.get_or_create_for_sender("historical-completion-rejected-client")
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="historical-completion-rejected-review",
        )
        self._backdate_legacy_review(review)
        with self.assertRaisesMessage(ValueError, "лише менеджер"):
            resolve_historical_paid_review(
                review,
                actor=actor,
                outcome="already_received",
                reason="Historical sale confirmed",
                amount_unrecoverable=True,
            )

        actor.is_staff = True
        actor.save(update_fields=["is_staff"])
        invalid_review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="historical-completion-invalid-amount-review",
        )
        self._backdate_legacy_review(invalid_review)
        with self.assertRaisesMessage(ValueError, "додатним числом"):
            resolve_historical_paid_review(
                invalid_review,
                actor=actor,
                outcome="already_received",
                reason="Historical sale confirmed",
                confirmed_amount="not-money",
                amount_unrecoverable=True,
            )

        with self.assertRaisesMessage(ValueError, "обов'язкова"):
            resolve_historical_paid_review(
                review,
                actor=actor,
                outcome="already_received",
                reason="",
                amount_unrecoverable=True,
            )

        review.order = Order.objects.create(
            full_name="Legacy customer",
            phone="0500000011",
            city="Київ",
            np_office="1",
            total_sum=Decimal("1.00"),
        )
        review.save(update_fields=["order", "updated_at"])
        with self.assertRaisesMessage(ValueError, "пов'язане замовлення"):
            resolve_historical_paid_review(
                review,
                actor=actor,
                outcome="already_received",
                reason="Historical sale confirmed",
                amount_unrecoverable=True,
            )

    def test_historical_completion_rejects_hidden_client_and_provider_reversal(self):
        from django.contrib.auth import get_user_model

        from management.ig_bot_models import (
            IgClient,
            IgDeal,
            IgPaymentConfirmationReview,
            IgPaymentProjection,
        )
        from management.services.ig_payment_review import resolve_historical_paid_review

        actor = get_user_model().objects.create_user(
            "historical-completion-conflict-actor",
            password="x",
            is_staff=True,
        )
        hidden_client = IgClient.get_or_create_for_sender("historical-completion-hidden-client")
        hidden_client.hidden_at = timezone.now()
        hidden_client.save(update_fields=["hidden_at", "updated_at"])
        hidden_review = IgPaymentConfirmationReview.objects.create(
            client=hidden_client,
            dedupe_key="historical-completion-hidden-review",
        )
        self._backdate_legacy_review(hidden_review)
        with self.assertRaisesMessage(ValueError, "Прихований клієнт"):
            resolve_historical_paid_review(
                hidden_review,
                actor=actor,
                outcome="already_received",
                reason="Historical sale confirmed",
                amount_unrecoverable=True,
            )

        client = IgClient.get_or_create_for_sender("historical-completion-reversed-client")
        deal = IgDeal.objects.create(client=client, amount=Decimal("1760.00"))
        IgPaymentProjection.objects.create(
            client=client,
            deal=deal,
            truth=IgDeal.PaymentTruth.REVERSED,
        )
        reversed_review = IgPaymentConfirmationReview.objects.create(
            client=client,
            deal=deal,
            dedupe_key="historical-completion-reversed-review",
        )
        self._backdate_legacy_review(reversed_review)
        with self.assertRaisesMessage(ValueError, "provider-оплати"):
            resolve_historical_paid_review(
                reversed_review,
                actor=actor,
                outcome="already_received",
                reason="Historical sale confirmed",
                confirmed_amount="1760.00",
            )

    def test_historical_paid_archive_rejects_deal_with_active_checkout_proposal(self):
        from django.contrib.auth import get_user_model

        from management.ig_bot_models import (
            IgCheckoutProposal,
            IgClient,
            IgDeal,
            IgPaymentConfirmationReview,
        )
        from management.services.ig_payment_review import (
            archive_historical_paid_review,
            record_review_decision,
        )

        actor = get_user_model().objects.create_user(
            "historical-paid-checkout-actor",
            password="x",
            is_staff=True,
        )
        client = IgClient.get_or_create_for_sender("historical-paid-checkout-client")
        deal = IgDeal.objects.create(
            client=client,
            status=IgDeal.Status.QUOTED,
            amount=Decimal("1760.00"),
            requested_payment_amount=Decimal("1760.00"),
        )
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            deal=deal,
            dedupe_key="historical-paid-checkout-review",
            evidence={"order_draft": {"quoted_total": "1760.00"}},
            watermark_message_id=1137,
        )
        record_review_decision(
            review,
            actor=actor,
            decision="manager_verified",
            verification_scope="full_payment",
            confirmed_amount=Decimal("1760.00"),
        )
        IgCheckoutProposal.objects.create_current(
            deal=deal,
            catalog_total=Decimal("1760.00"),
            quoted_total=Decimal("1760.00"),
            requested_payment_amount=Decimal("1760.00"),
            items_digest="a" * 64,
        )

        with self.assertRaisesMessage(ValueError, "активну checkout-пропозицію"):
            archive_historical_paid_review(
                review,
                actor=actor,
                reason="Must not archive an active checkout",
            )

        review.refresh_from_db()
        self.assertEqual(review.resolution_kind, "")

    def test_different_receipt_remains_a_separate_payment_review(self):
        from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
        from management.services.ig_payment_review import create_payment_review

        client = IgClient.get_or_create_for_sender("review-distinct-payment-fingerprint")

        def messages(receipt_id, receipt_url):
            return [
                {"id": 233, "role": "user", "text": "Беру базову S за 790 грн"},
                {"id": 237, "role": "manager", "text": "До сплати 790 грн"},
                {
                    "id": receipt_id,
                    "role": "user",
                    "text": "Я оплатила, ось чек",
                    "media": [{"url": receipt_url, "type": "image"}],
                },
            ]

        with (
            patch(
                "management.services.ig_payment_review._persist_review_media",
                side_effect=lambda rows: rows,
            ),
            patch(
                "management.services.ig_payment_review._resolve_payment_media_candidates",
                side_effect=lambda rows: rows,
            ),
            patch("management.services.instagram_bot.notify_manager"),
            patch("management.services.ig_commercial_episodes.ensure_episode_for_review"),
        ):
            first = create_payment_review(
                client,
                watermark=238,
                messages=messages(238, "https://cdn.test/receipt-238.jpg"),
            )
            second = create_payment_review(
                client,
                watermark=250,
                messages=messages(250, "https://cdn.test/receipt-250.jpg"),
            )

        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(IgPaymentConfirmationReview.objects.filter(client=client).count(), 2)

    def test_repeat_episode_without_deal_never_reuses_old_same_product_deal(self):
        from management.ig_bot_models import IgClient, IgCommercialEpisode, IgDeal, IgDealItem
        from management.services.ig_commercial_episodes import (
            ensure_episode_for_deal,
            start_repeat_episode,
        )
        from management.services.ig_payment_review import _select_review_deal
        from storefront.models import Category, Product, ProductStatus

        category = Category.objects.create(name="Футболки review", slug="review-episode")
        product = Product.objects.create(
            title="Футболка review",
            slug="review-episode-product",
            category=category,
            price=Decimal("790.00"),
            status=ProductStatus.PUBLISHED,
        )
        client = IgClient.get_or_create_for_sender("review-repeat-episode-scope")
        old_deal = IgDeal.objects.create(
            client=client,
            amount=Decimal("790.00"),
            status=IgDeal.Status.AWAITING_PAYMENT,
        )
        IgDealItem.objects.create(
            deal=old_deal,
            product=product,
            title=product.title,
            qty=1,
            unit_price=Decimal("790.00"),
        )
        old_episode = ensure_episode_for_deal(old_deal)
        old_episode.state = IgCommercialEpisode.State.LOST
        old_episode.open_slot = None
        old_episode.save(update_fields=["state", "open_slot", "updated_at"])
        client.current_commercial_episode = None
        client.save(update_fields=["current_commercial_episode", "updated_at"])
        repeat = start_repeat_episode(
            client,
            repeat_kind=IgCommercialEpisode.RepeatKind.REORDER,
            evidence_message_ids=[9901],
            confidence=Decimal("0.97"),
            analysis_model="gemini-test",
            analysis_prompt_version="repeat-scope-v1",
        )

        selected = _select_review_deal(
            client,
            [{"status": "matched", "product_id": product.pk}],
        )

        self.assertIsNone(repeat.deal_id)
        self.assertIsNone(selected)

    def test_new_episode_review_does_not_rematerialize_old_payment_claim(self):
        from management.ig_bot_models import IgClient, IgCommercialEpisode
        from management.models import InstagramBotMessage
        from management.services.ig_commercial_episodes import start_repeat_episode
        from management.services.ig_payment_review import create_payment_review

        client = IgClient.get_or_create_for_sender("review-message-episode-scope")
        InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Я оплатив 790 грн, ось чек",
        )
        repeat_message = InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Хочу замовити ще одну футболку",
        )
        start_repeat_episode(
            client,
            repeat_kind=IgCommercialEpisode.RepeatKind.REORDER,
            evidence_message_ids=[repeat_message.pk],
            confidence=Decimal("0.98"),
            analysis_model="gemini-test",
            analysis_prompt_version="repeat-review-scope-v1",
        )
        current_message = InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Потрібен чорний колір",
        )

        review = create_payment_review(client, watermark=current_message.pk)

        self.assertIsNone(review)


class IgPaymentTelegramCallbackTests(TestCase):
    @override_settings(MANAGEMENT_BASE_URL="https://management.example")
    @patch.dict("os.environ", {"MANAGEMENT_TG_ADMIN_CHAT_ID": "777", "MANAGEMENT_TG_BOT_TOKEN": "token"}, clear=False)
    @patch("management.views._tg_edit_message")
    @patch("management.views._tg_answer_callback")
    def test_confirm_button_confirms_review_without_management_second_click(self, answer, edit):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.models import IgBotNotification, IgClient
        from management.views import management_bot_webhook

        client = IgClient.get_or_create_for_sender("telegram-review-client")
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="telegram-review-callback",
            evidence={"order_draft": {"quoted_total": "2100"}},
        )
        IgBotNotification.objects.create(
            dedupe_key=review.dedupe_key,
            event_type="payment_review",
            payload={
                "text": "Review",
                "chat_id": "777",
                "media": [],
                "payment_candidate": {
                    "amount": "2100.00",
                    "currency": "UAH",
                    "scope": "full_payment",
                    "source": "review_quoted_total",
                    "evidence_message_ids": [],
                    "digest": "placeholder",
                },
            },
            status=IgBotNotification.Status.SENT,
            telegram_message_id="88",
        )
        request = RequestFactory().post(
            "/management/telegram/webhook/token",
            data=json.dumps({
                "callback_query": {
                    "id": "cb-1",
                    "data": f"igpay:confirm:{review.pk}",
                    "from": {"id": 777, "username": "owner"},
                    "message": {"chat": {"id": 777, "type": "private"}, "message_id": 88, "text": "Review"},
                }
            }),
            content_type="application/json",
        )
        from management.services.ig_payment_review import resolve_review_payment_amount

        notification = IgBotNotification.objects.get(dedupe_key=review.dedupe_key)
        notification.payload["payment_candidate"] = {
            **notification.payload["payment_candidate"],
            "digest": resolve_review_payment_amount(review)["digest"],
        }
        notification.save(update_fields=["payload", "updated_at"])
        response = management_bot_webhook(request, "token")
        self.assertEqual(response.status_code, 200)
        review.refresh_from_db()
        self.assertEqual(review.status, IgPaymentConfirmationReview.Status.CONFIRMED)
        decision = review.decisions.get()
        self.assertEqual(decision.confirmed_amount, Decimal("2100.00"))
        self.assertEqual(decision.amount_source, "review_quoted_total")
        answer.assert_called_once()
        edit.assert_called_once()
        self.assertIsNone(edit.call_args.kwargs["parse_mode"])
        self.assertIn("ig_payment_review=", edit.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["url"])

    @override_settings(MANAGEMENT_BASE_URL="https://management.example")
    @patch.dict("os.environ", {"MANAGEMENT_TG_ADMIN_CHAT_ID": "777", "MANAGEMENT_TG_BOT_TOKEN": "token"}, clear=False)
    @patch("management.views._tg_edit_message")
    @patch("management.views._tg_answer_callback")
    def test_confirm_button_rejects_stale_amount_candidate(self, answer, edit):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.models import IgBotNotification, IgClient
        from management.views import management_bot_webhook

        client = IgClient.get_or_create_for_sender("telegram-stale-candidate")
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="telegram-stale-candidate",
            evidence={"order_draft": {"quoted_total": "2100"}},
        )
        IgBotNotification.objects.create(
            dedupe_key=review.dedupe_key,
            event_type="payment_review",
            payload={
                "text": "Review",
                "chat_id": "777",
                "media": [],
                "payment_candidate": {
                    "amount": "2000.00",
                    "currency": "UAH",
                    "scope": "full_payment",
                    "source": "review_quoted_total",
                    "evidence_message_ids": [],
                    "digest": "stale",
                },
            },
            status=IgBotNotification.Status.SENT,
            telegram_message_id="92",
        )
        request = RequestFactory().post(
            "/management/telegram/webhook/token",
            data=json.dumps({
                "callback_query": {
                    "id": "cb-stale",
                    "data": f"igpay:confirm:{review.pk}",
                    "from": {"id": 777, "username": "owner"},
                    "message": {"chat": {"id": 777, "type": "private"}, "message_id": 92, "text": "Review"},
                }
            }),
            content_type="application/json",
        )

        response = management_bot_webhook(request, "token")

        self.assertEqual(response.status_code, 200)
        review.refresh_from_db()
        self.assertEqual(review.status, IgPaymentConfirmationReview.Status.PENDING)
        answer.assert_called_once_with("token", "cb-stale", "Сума змінилася — відкрийте перевірку")
        edit.assert_not_called()

    @patch.dict("os.environ", {"MANAGEMENT_TG_BOT_TOKEN": "token"}, clear=True)
    @patch("management.views._tg_edit_message")
    @patch("management.views._tg_answer_callback")
    def test_callback_denies_when_admin_chat_allowlist_is_missing(self, answer, edit):
        from management.views import management_bot_webhook

        request = RequestFactory().post(
            "/management/telegram/webhook/token",
            data=json.dumps({
                "callback_query": {
                    "id": "cb-deny",
                    "data": "igpay:confirm:1",
                    "message": {"chat": {"id": 777}, "message_id": 88, "text": "Review"},
                }
            }),
            content_type="application/json",
        )
        response = management_bot_webhook(request, "token")
        self.assertEqual(response.status_code, 200)
        answer.assert_called_once_with("token", "cb-deny", "Доступ не налаштовано")
        edit.assert_not_called()

    @override_settings(MANAGEMENT_BASE_URL="https://management.example")
    @patch.dict("os.environ", {"MANAGEMENT_TG_ADMIN_CHAT_ID": "777", "MANAGEMENT_TG_BOT_TOKEN": "token"}, clear=False)
    @patch("management.views._tg_edit_message")
    @patch("management.views._tg_answer_callback")
    def test_cancel_button_cancels_review_directly(self, answer, edit):
        from management.ig_bot_models import IgPaymentConfirmationReview, IgPaymentReviewDecision
        from management.models import IgBotNotification, IgClient
        from management.views import management_bot_webhook

        client = IgClient.get_or_create_for_sender("telegram-cancel-client")
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="telegram-review-cancel",
            evidence={},
        )
        IgBotNotification.objects.create(
            dedupe_key=review.dedupe_key,
            event_type="payment_review",
            payload={"text": "Review", "chat_id": "777", "media": []},
            status=IgBotNotification.Status.SENT,
            telegram_message_id="89",
        )
        request = RequestFactory().post(
            "/management/telegram/webhook/token",
            data=json.dumps({
                "callback_query": {
                    "id": "cb-cancel",
                    "data": f"igpay:cancel:{review.pk}",
                    "from": {"id": 777, "username": "owner"},
                    "message": {"chat": {"id": 777, "type": "private"}, "message_id": 89, "text": "Review"},
                }
            }),
            content_type="application/json",
        )
        response = management_bot_webhook(request, "token")
        self.assertEqual(response.status_code, 200)
        review.refresh_from_db()
        self.assertEqual(review.status, IgPaymentConfirmationReview.Status.CANCELLED)
        decision = IgPaymentReviewDecision.objects.get(review=review)
        self.assertEqual(decision.reason_code, "telegram_rejected")
        self.assertEqual(decision.verification_scope, "payment_claim")
        self.assertEqual(decision.actor_source, "telegram_user")
        self.assertEqual(decision.actor_external_id, "777")
        answer.assert_called_once()
        edit.assert_called_once()

    @override_settings(MANAGEMENT_BASE_URL="https://management.example")
    @patch.dict("os.environ", {"MANAGEMENT_TG_ADMIN_CHAT_ID": "777", "MANAGEMENT_TG_BOT_TOKEN": "token"}, clear=False)
    @patch("management.views._tg_edit_message", side_effect=[RuntimeError("telegram edit failed"), None])
    @patch("management.views._tg_answer_callback")
    def test_callback_retry_repairs_message_after_decision_committed(self, answer, edit):
        from management.ig_bot_models import IgPaymentConfirmationReview, IgPaymentReviewDecision
        from management.models import IgBotNotification, IgClient
        from management.views import management_bot_webhook

        client = IgClient.get_or_create_for_sender("telegram-retry-client")
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="telegram-review-retry",
            evidence={"order_draft": {"quoted_total": "2100.00", "currency": "UAH"}},
        )
        from management.services.ig_payment_review import resolve_review_payment_amount

        candidate = resolve_review_payment_amount(review)
        notification = IgBotNotification.objects.create(
            dedupe_key=review.dedupe_key,
            event_type="payment_review",
            payload={
                "text": "Review",
                "chat_id": "777",
                "media": [],
                "payment_candidate": {
                    "amount": f"{candidate['amount']:.2f}",
                    "currency": candidate["currency"],
                    "scope": candidate["scope"],
                    "source": candidate["source"],
                    "evidence_message_ids": candidate["evidence_message_ids"],
                    "digest": candidate["digest"],
                },
            },
            status=IgBotNotification.Status.SENT,
            telegram_message_id="90",
        )

        def callback_request(callback_id):
            return RequestFactory().post(
                "/management/telegram/webhook/token",
                data=json.dumps({
                    "callback_query": {
                        "id": callback_id,
                        "data": f"igpay:confirm:{review.pk}",
                        "from": {"id": 777, "username": "owner"},
                        "message": {
                            "chat": {"id": 777, "type": "private"},
                            "message_id": 90,
                            "text": "Review",
                        },
                    }
                }),
                content_type="application/json",
            )

        with self.assertRaisesMessage(RuntimeError, "telegram edit failed"):
            management_bot_webhook(callback_request("cb-retry-1"), "token")

        review.refresh_from_db()
        self.assertEqual(review.status, IgPaymentConfirmationReview.Status.CONFIRMED)
        self.assertEqual(IgPaymentReviewDecision.objects.filter(review=review).count(), 1)

        response = management_bot_webhook(callback_request("cb-retry-2"), "token")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(edit.call_count, 2)
        self.assertEqual(IgPaymentReviewDecision.objects.filter(review=review).count(), 1)
        notification.refresh_from_db()
        self.assertEqual(notification.status, IgBotNotification.Status.RESOLVED)
        answer.assert_called_once_with("token", "cb-retry-2", "Дію вже виконано")


class FalseHistoricalPurchaseCorrectionTests(TestCase):
    def test_correction_supersedes_false_truth_and_preserves_current_funnel(self):
        from django.contrib.auth import get_user_model

        from management.ig_bot_models import (
            IgCommercialEpisode,
            IgCommercialEpisodeEvent,
            IgDeal,
            IgPaymentConfirmationReview,
            IgPaymentReviewDecision,
        )
        from management.models import IgClient
        from management.services.bot_payment_truth import (
            client_has_confirmed_purchase,
            historical_purchase_confirmation,
            recalculate_client_payment_aggregates,
        )
        from management.services.ig_payment_review import (
            correct_false_historical_purchase,
        )

        actor = get_user_model().objects.create_user(
            "false-historical-correction-actor",
            password="x",
            is_staff=True,
        )
        client = IgClient.get_or_create_for_sender("false-historical-correction-client")
        client.stage = IgClient.Stage.CHECKOUT
        client.intent = IgClient.Intent.PAYMENT
        client.save(update_fields=["stage", "intent", "updated_at"])
        client.set_stage(
            IgClient.Stage.DONE,
            reason="historical_payment_review_completed",
        )
        review = IgPaymentConfirmationReview.objects.create(
            client=client,
            dedupe_key="false-historical-correction-review",
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
            watermark_message_id=17,
            confirmed_by=actor,
            confirmed_at=timezone.now(),
            resolution_kind=(
                IgPaymentConfirmationReview.ResolutionKind.HISTORICAL_PAID_ARCHIVED
            ),
            resolution_outcome=(
                IgPaymentConfirmationReview.ResolutionOutcome.ALREADY_RECEIVED
            ),
            resolution_note="Original false classification",
            resolved_at=timezone.now(),
            resolved_by=actor,
        )
        original_decision = IgPaymentReviewDecision.objects.create(
            review=review,
            client=client,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
            verification_source="manager",
            verification_scope=(
                IgPaymentReviewDecision.VerificationScope.HISTORICAL_FULFILLED
            ),
            currency="UAH",
            amount_source="historical_amount_unrecoverable",
            reason_code="historical_paid_fulfilled",
            reason_text="Original classification",
            evidence_watermark_message_id=review.watermark_message_id,
            review_status_before=IgPaymentConfirmationReview.Status.PENDING,
            review_status_after=IgPaymentConfirmationReview.Status.CONFIRMED,
            stage_before=client.stage,
            stage_after=client.stage,
            actor=actor,
            actor_source=IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
            actor_external_id=str(actor.pk),
            actor_label=actor.get_username(),
        )
        historical_episode = IgCommercialEpisode.objects.create(
            client=client,
            sequence=1,
            open_slot=None,
            materialization_key="false-history-episode",
            state=IgCommercialEpisode.State.ACTIVE,
            primary_payment_review=review,
            payment_snapshot={"review_id": review.pk, "decision_id": original_decision.pk},
        )
        current_deal = IgDeal.objects.create(
            client=client,
            status=IgDeal.Status.QUOTED,
            amount=Decimal("788.00"),
        )
        current_episode = IgCommercialEpisode.objects.create(
            client=client,
            sequence=2,
            open_slot=1,
            materialization_key="current-real-prospect-episode",
            state=IgCommercialEpisode.State.ACTIVE,
            deal=current_deal,
        )
        client.current_commercial_episode = current_episode
        client.save(update_fields=["current_commercial_episode", "updated_at"])
        recalculate_client_payment_aggregates(client)
        self.assertTrue(client_has_confirmed_purchase(client))

        corrected = correct_false_historical_purchase(
            review,
            actor=actor,
            reason="Owner confirmed that no historical purchase occurred",
        )

        corrected.refresh_from_db()
        historical_episode.refresh_from_db()
        current_episode.refresh_from_db()
        client.refresh_from_db()
        decisions = list(review.decisions.order_by("id"))
        self.assertEqual(len(decisions), 2)
        self.assertEqual(decisions[0].pk, original_decision.pk)
        self.assertEqual(
            decisions[1].decision,
            IgPaymentReviewDecision.Decision.MANAGER_REJECTED,
        )
        self.assertEqual(
            decisions[1].reason_code,
            "false_historical_purchase_corrected",
        )
        self.assertEqual(
            corrected.status,
            IgPaymentConfirmationReview.Status.SUPERSEDED,
        )
        self.assertEqual(
            corrected.resolution_kind,
            IgPaymentConfirmationReview.ResolutionKind.NONE,
        )
        self.assertIsNone(corrected.resolution_outcome)
        self.assertIsNotNone(corrected.superseded_at)
        self.assertIsNone(historical_episode.primary_payment_review_id)
        self.assertEqual(
            historical_episode.state,
            IgCommercialEpisode.State.CANCELLED,
        )
        self.assertEqual(
            historical_episode.outcome,
            "historical_purchase_corrected",
        )
        self.assertIsNone(historical_episode.payment_snapshot["review_id"])
        event = IgCommercialEpisodeEvent.objects.get(
            event_type="historical_purchase_corrected"
        )
        self.assertEqual(event.episode_id, historical_episode.pk)
        self.assertEqual(
            event.evidence["reason_code"],
            "false_historical_purchase_corrected",
        )
        self.assertEqual(current_episode.state, IgCommercialEpisode.State.ACTIVE)
        self.assertEqual(client.current_commercial_episode_id, current_episode.pk)
        self.assertEqual(client.stage, IgClient.Stage.CHECKOUT)
        self.assertEqual(client.purchases_count, 0)
        self.assertFalse((client.conversion_flags or {}).get("is_buyer"))
        self.assertFalse(client_has_confirmed_purchase(client))
        self.assertFalse(historical_purchase_confirmation(client)["confirmed"])
        from management.services.instagram_bot import build_prompt_snapshot
        from management.tests_ig_policy_helpers import ensure_test_instruction_publication

        ensure_test_instruction_publication()
        prompt = build_prompt_snapshot(client)
        self.assertNotIn("Завершено (100% воронки)", prompt)

        # Reproduce the old daemon-start migration reconciler: it reopened the
        # corrected deal episode and materialized the retired review separately.
        historical_episode.state = IgCommercialEpisode.State.ACTIVE
        historical_episode.outcome = ""
        historical_episode.closed_at = None
        historical_episode.save(update_fields=[
            "state", "outcome", "closed_at", "updated_at",
        ])
        resurrected = IgCommercialEpisode.objects.create(
            client=client,
            sequence=3,
            open_slot=None,
            materialization_key="retired-review-rematerialized",
            state=IgCommercialEpisode.State.LOST,
            outcome="superseded_duplicate_payment_review",
            primary_payment_review=review,
        )

        replay = correct_false_historical_purchase(
            corrected,
            actor=actor,
            reason="Owner confirmed that no historical purchase occurred",
        )
        historical_episode.refresh_from_db()
        resurrected.refresh_from_db()
        current_episode.refresh_from_db()
        client.refresh_from_db()
        self.assertEqual(replay.pk, corrected.pk)
        self.assertEqual(review.decisions.count(), 2)
        self.assertEqual(historical_episode.state, IgCommercialEpisode.State.CANCELLED)
        self.assertEqual(resurrected.state, IgCommercialEpisode.State.CANCELLED)
        self.assertIsNone(resurrected.primary_payment_review_id)
        self.assertEqual(client.current_commercial_episode_id, current_episode.pk)
        self.assertEqual(client.stage, IgClient.Stage.CHECKOUT)
        self.assertEqual(
            IgCommercialEpisodeEvent.objects.filter(
                event_type="historical_purchase_corrected",
                evidence__review_id=review.pk,
            ).count(),
            2,
        )

        from django.core.management import call_command

        call_command("reconcile_ig_commercial_episodes", passes=3)
        call_command("reconcile_ig_commercial_episodes", passes=3)
        self.assertFalse(
            IgCommercialEpisode.objects.filter(
                primary_payment_review=review,
            ).exists()
        )
        self.assertEqual(
            IgCommercialEpisode.objects.filter(client=client).count(),
            3,
        )
