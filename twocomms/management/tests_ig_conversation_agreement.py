"""Anonymous source-contract cases; no providers or receipt verification."""
from copy import deepcopy
from datetime import timedelta
import json
import os
from unittest.mock import patch
import uuid

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.services.ig_conversation_agreement import extract_conversation_agreement


def message(pk, role, text, **extra):
    return {"id": pk, "role": role, "text": text, "source": "webhook", **extra}


class ConversationAgreementTests(SimpleTestCase):
    def extract(self, offer, answer="Так", **seller):
        return extract_conversation_agreement([
            message(10, "manager", offer, **seller), message(11, "user", answer),
        ])

    def test_accepted_offsite_configuration_keeps_source_identity(self):
        result = self.extract("L отверсаз білу TwoComms SamplePrint42")
        item = result["items"][0]
        self.assertIsNone(item["product_id"])
        self.assertEqual(item["title"], "TwoComms SamplePrint42")
        self.assertEqual((item["size"], item["fit"], item["color"]), ("L", "oversize", "white"))
        self.assertEqual((item["source_message_id"], item["acceptance_message_id"]), (10, 11))
        self.assertEqual(item["identity_kind"], "offsite_named")
        self.assertIsNone(item["qty"])
        self.assertEqual(result["source_message_ids"], [10, 11])
        self.assertEqual(len(result["evidence"]["10"]["source_digest"]), 64)

    def test_multilingual_size_color_fit_normalization(self):
        cases = [
            ("Футболка SamplePrint42 біла л оверсайз", "white", "L", "oversize"),
            ("Футболка SamplePrint42 білого L oversize", "white", "L", "oversize"),
            ("Футболка SamplePrint42 белая л оверсайз", "white", "L", "oversize"),
            ("Футболка SamplePrint42 чёрная L oversize", "black", "L", "oversize"),
            ("Футболка SamplePrint42 чорна л оверсайз", "black", "L", "oversize"),
            ("tshirt SamplePrint42 white L oversized", "white", "L", "oversize"),
            ("tshirt SamplePrint42 black XL classic", "black", "XL", "classic"),
        ]
        for offer, color, size, fit in cases:
            with self.subTest(offer=offer):
                item = self.extract(offer)["items"][0]
                self.assertEqual((item["color"], item["size"], item["fit"]), (color, size, fit))

    def test_configuration_is_not_a_claim_about_catalogue_or_payment(self):
        result = self.extract("Футболка SamplePrint42 біла L oversize")
        self.assertEqual(result["items"][0]["configuration_authority"], "customer_confirmed_seller_offer")
        self.assertIn("catalog_product_not_identified", result["uncertainty_reasons"])
        self.assertEqual(result["payable_total"], "")
        self.assertNotIn("paid", result)

    def test_missing_configuration_fields_stay_unknown(self):
        item = self.extract("Модель: SamplePrint42, розмір L")["items"][0]
        self.assertEqual(item["title"], "SamplePrint42")
        self.assertEqual(item["color"], "")
        self.assertEqual(item["fit"], "")
        self.assertEqual(item["garment_type"], "")
        self.assertIsNone(item["qty"])

    def test_explicit_singular_customer_request_supplies_type_and_quantity(self):
        result = extract_conversation_agreement([
            message(8, "user", "Хочу на подарунок футболку товаришу"),
            message(10, "manager", "L отверсаз білу TwoComms SamplePrint42"),
            message(11, "user", "Так"),
        ])
        item = result["items"][0]
        self.assertEqual((item["garment_type"], item["qty"]), ("tshirt", 1))
        self.assertEqual((item["garment_source_message_id"], item["quantity_source_message_id"]), (8, 8))
        self.assertEqual(item["quantity_inference"], "singular_garment")
        self.assertIn(8, result["source_message_ids"])

    def test_affirmative_garment_clause_survives_unrelated_negation_and_questions(self):
        for request in ("Вітаю, хочу на подарунок футболку другу, але він не любить яскраві пакунки. Чи є доставка?",
                        "Хочу футболку. Не потрібна подарункова коробка. Коли відправка?",
                        "I want a t-shirt, but no gift wrapping. Is shipping available?",
                        "Хочу 2 футболки, але не потрібна подарункова коробка"):
            with self.subTest(request=request):
                result = extract_conversation_agreement([
                    message(8, "user", request), message(10, "manager", "L oversize white TwoComms SamplePrint42"),
                    message(11, "user", "Так"), message(12, "manager", "850 грн + 120 доставка = 970 грн"),
                ])
                item = result["items"][0]
                self.assertEqual(item["garment_type"], "tshirt")
                self.assertEqual(item["qty"], 2 if "2 футболки" in request else 1)
                self.assertEqual((item["garment_source_message_id"], item["quantity_source_message_id"]), (8, 8))
                self.assertEqual(item["unit_price"], None if item["qty"] == 2 else "850.00")

    def test_question_negated_reported_and_alternative_requests_cannot_supply_quantity(self):
        for request in ("Хочу футболку?", "Не хочу футболку", "Хочу футболку або худі",
                        "I want a t-shirt or hoodie", "Хочу футболку і худі", 'Він написав: "хочу футболку"'):
            with self.subTest(request=request):
                item = extract_conversation_agreement([
                    message(8, "user", request), message(10, "manager", "L oversize white TwoComms SamplePrint42"),
                    message(11, "user", "Так"),
                ])["items"][0]
                self.assertEqual(item["garment_type"], "")
                self.assertIsNone(item["qty"])
        item = extract_conversation_agreement([
            message(8, "user", "Хочу футболку, ще футболку для друга"),
            message(10, "manager", "L oversize white TwoComms SamplePrint42"), message(11, "user", "Так"),
        ])["items"][0]
        self.assertIsNone(item["qty"])

    def test_owned_mime_only_photo_and_confirmation_question_complete_custom_item(self):
        result = extract_conversation_agreement([
            message(8, "user", "Вітаю, хочу на подарунок футболку другу, але він не любить яскраві пакунки"),
            message(9, "manager", "Сума (850 грн)"),
            message(10, "manager", "L отверсаз білу TwoComms SamplePrint42"),
            message(11, "manager", "Саме цей принт", attachments=json.dumps(["https://example.invalid/source-image"]),
                attachment_media=[{"mime": "image/jpeg", "source_part_id": "synthetic-owned-image", "content_hash": "a" * 64,
                    "status": "owned", "private_storage": True}]),
            message(12, "manager", "Все вірно?"), message(13, "user", "Так"),
            message(14, "manager", "850 грн + 120 доставка = 970 грн"),
        ])
        item = result["items"][0]
        self.assertEqual((item["garment_type"], item["qty"], item["unit_price"]), ("tshirt", 1, "850.00"))
        self.assertEqual(item["reference_message_ids"], [11])
        self.assertEqual(item["accepted_reference_message_ids"], [11])
        self.assertEqual(item["price_evidence_message_ids"], [14])
        self.assertEqual(item["acceptance_message_id"], 13)
        self.assertIsNone(item["product_id"])
        self.assertIn("media_binding_digest", result["evidence"]["11"])

    def test_accepted_seller_reference_excludes_prior_alternatives_and_late_receipt(self):
        rows = [message(7, "user", "", attachment_media=[{"mime": "image/jpeg", "content_hash": "a" * 64}]),
            message(10, "manager", "Футболка SamplePrint42 L oversize white"),
            message(11, "manager", "Саме цей принт", attachment_media=[{"mime": "image/jpeg", "content_hash": "b" * 64}]),
            message(12, "user", "Так"),
            message(13, "user", "", attachment_media=[{"mime": "image/jpeg", "content_hash": "c" * 64}])]
        before = extract_conversation_agreement(rows)
        self.assertEqual(before["items"][0]["reference_message_ids"], [7, 11])
        self.assertEqual(before["items"][0]["accepted_reference_message_ids"], [11])
        self.assertNotIn(13, before["source_message_ids"])
        rows[-1]["attachment_media"][0].update(role="receipt", payment_evidence=True)
        after = extract_conversation_agreement(rows)
        self.assertEqual(after["items"], before["items"])
        self.assertEqual(after["evidence"], before["evidence"])

    def test_old_customer_type_and_quantity_do_not_cross_a_reset(self):
        result = extract_conversation_agreement([
            message(8, "user", "Хочу футболку"), message(9, "user", "Хочу інший товар"),
            message(10, "manager", "L oversize white TwoComms SamplePrint42"), message(11, "user", "Так"),
        ])
        item = result["items"][0]
        self.assertEqual(item["garment_type"], "")
        self.assertIsNone(item["qty"])

    def test_ordinary_seller_remark_or_unanchored_preferences_are_not_offers(self):
        for text in ("Добре, L", "Біла L oversize", "У нас є футболки", "Ваше повідомлення про футболку"):
            with self.subTest(text=text):
                self.assertEqual(self.extract(text)["items"], [])

    def test_negation_question_alternatives_and_reports_cannot_accept(self):
        for answer in ("Не согласен", "Нет, не оформляем", "Да?", "Так, але M", "Так, тільки за 700 грн", "Він сказав так", '"Так"', "Так, беру іншу футболку"):
            with self.subTest(answer=answer):
                self.assertEqual(self.extract("Футболка SamplePrint42 біла L oversize", answer)["items"], [])
        for offer in ("Футболка SamplePrint42 L або M", "Футболка SamplePrint42 не біла L", "Футболка SamplePrint42 біла L?", 'Футболка SamplePrint42 "L"'):
            with self.subTest(offer=offer):
                self.assertEqual(self.extract(offer)["items"], [])

    def test_customer_restatement_must_match_offered_configuration(self):
        self.assertEqual(self.extract("Футболка SamplePrint42 біла L oversize", "Так, беру чорну M")["items"], [])
        self.assertEqual(self.extract("Футболка SamplePrint42 біла L oversize", "Так, беру білу L")["items"][0]["size"], "L")
        self.assertEqual(self.extract("Футболка SamplePrint42 біла L oversize", "Так, беру")["items"][0]["size"], "L")
        self.assertEqual(self.extract("Футболка SamplePrint42 біла L oversize", "Так, беру худі")["items"], [])

    def test_sent_bot_offer_requires_delivery_proof(self):
        for metadata in ({}, {"status": "done", "send_state": "unknown", "provider_message_id": "fake-mid"}):
            result = extract_conversation_agreement([
                message(10, "model", "Футболка SamplePrint42 біла L oversize", **metadata), message(11, "user", "Так"),
            ])
            self.assertEqual(result["items"], [])
        result = extract_conversation_agreement([
            message(10, "model", "Футболка SamplePrint42 біла L oversize", status="done", send_state="sent", provider_message_id="fake-mid"),
            message(11, "user", "Так"),
        ])
        self.assertEqual(len(result["items"]), 1)

    def test_failed_manager_offer_cannot_supply_configuration(self):
        self.assertEqual(self.extract("Футболка SamplePrint42 біла L oversize", status="failed")["items"], [])

    def test_screenshot_reference_between_offer_and_confirmation(self):
        result = extract_conversation_agreement([
            message(10, "manager", "Футболка SamplePrint42 біла L oversize"),
            message(11, "user", "", media=[{"type": "image", "role": "product"}]),
            message(12, "user", "Так"),
        ])
        self.assertEqual(result["items"][0]["reference_message_ids"], [11])
        self.assertIn("reference_identity_unverified", result["uncertainty_reasons"])
        self.assertIsNone(result["items"][0]["product_id"])
        self.assertEqual(len(result["evidence"]["11"]["media_binding_digest"]), 64)

    def test_delivered_manager_photo_between_offer_and_customer_confirmation(self):
        result = extract_conversation_agreement([
            message(10, "manager", "Футболка SamplePrint42 біла L oversize"),
            message(11, "manager", "", status="done", send_state="sent", provider_message_id="synthetic-photo-mid",
                attachment_media=[{"media_type": "image", "source_part_id": "synthetic-photo-part", "content_hash": "a" * 64}]),
            message(12, "user", "Так"),
        ])
        self.assertEqual(result["items"][0]["reference_message_ids"], [11])
        self.assertIn(11, result["source_message_ids"])
        self.assertEqual(result["items"][0]["accepted_reference_message_ids"], [11])
        self.assertEqual(result["items"][0]["acceptance_message_id"], 12)
        self.assertIsNone(result["items"][0]["product_id"])

    def test_receipt_image_never_becomes_product_reference(self):
        result = extract_conversation_agreement([
            message(10, "manager", "Футболка SamplePrint42 біла L oversize"),
            message(11, "user", "", media=[{"type": "image", "role": "receipt", "payment_evidence": True}]),
            message(12, "user", "Так"),
        ])
        self.assertEqual(result["items"][0]["reference_message_ids"], [])

    def test_merchandise_delivery_and_payable_are_separate(self):
        result = extract_conversation_agreement([
            message(10, "manager", "Футболка SamplePrint42 біла L oversize, 1 шт"),
            message(11, "user", "Так"), message(12, "manager", "850 грн + 120 доставка = 970 грн"),
        ])
        self.assertEqual((result["merchandise_total"], result["delivery_total"], result["payable_total"]), ("850.00", "120.00", "970.00"))
        self.assertEqual(result["quoted_total"], "850.00")
        self.assertEqual(result["items"][0]["unit_price"], "850.00")
        self.assertEqual(result["amounts"]["authority"], "seller_instruction")
        self.assertIsNone(result["amounts"]["acceptance_message_id"])

    def test_incorrect_or_mixed_currency_equation_is_unknown(self):
        for text, reason in (("850 грн + 120 доставка = 980 грн", "amount_arithmetic_mismatch"),
                             ("850 USD + 120 доставка = 970 грн", "amount_currency_conflict")):
            with self.subTest(text=text):
                result = extract_conversation_agreement([message(10, "manager", text)])
                self.assertEqual(result["payable_total"], "")
                self.assertIn(reason, result["uncertainty_reasons"])

    def test_quote_expiry_only_comes_from_explicit_timezone_bound_source(self):
        result = extract_conversation_agreement([message(10, "manager", "Ціна 850 грн, діє до 2099-01-01T12:00:00+02:00")])
        self.assertEqual(result["amounts"]["expires_at"], "2099-01-01T12:00:00+02:00")
        absent = extract_conversation_agreement([message(10, "manager", "Ціна 850 грн")])
        self.assertNotIn("expires_at", absent["amounts"])
        unknown = extract_conversation_agreement([message(10, "manager", "Ціна 850 грн, діє до завтра")])
        self.assertIn("quote_expiry_unknown", unknown["uncertainty_reasons"])

    def test_ibans_are_instructions_and_do_not_verify_customer_payment(self):
        fake_iban = "UA" + "1" * 27
        result = extract_conversation_agreement([
            message(10, "manager", f"IBAN {fake_iban}\nОтримувач: Приклад Отримувача\nДо сплати 850 грн"),
            message(11, "user", "Оплатив 850 грн"),
        ])
        self.assertEqual(result["payment_instruction"]["iban"], fake_iban)
        self.assertEqual(result["payment_instruction"]["authority"], "seller_instruction")
        self.assertEqual(result["payable_total"], "850.00")
        self.assertNotIn("paid", result)

    def test_delivery_contact_and_gift_request_keep_customer_sources(self):
        result = extract_conversation_agreement([
            message(10, "user", "Місто: Приклад\nВідділення: 1\nПІБ: Приклад Отримувача\nТелефон: +380000000000"),
            message(11, "user", "Це подарунок, не вкладайте чек у посилку"),
        ])
        self.assertEqual(result["shipping"]["city"], "Приклад")
        self.assertEqual(result["shipping"]["field_evidence"]["city"]["source_message_id"], 10)
        self.assertTrue(result["packaging"]["exclude_receipt"])
        self.assertTrue(result["packaging"]["gift"])
        self.assertEqual(result["packaging"]["source_message_id"], 11)

    def test_structured_unlabelled_contact_requires_customer_phone(self):
        result = extract_conversation_agreement([
            message(10, "user", "Приклад Отримувача\n+380000000000\nм. Приклад\nВідділення №1"),
            message(11, "user", "Чек не вкладайте, це подарунок"),
        ])
        self.assertEqual(result["shipping"]["full_name"], "Приклад Отримувача")
        self.assertEqual(result["shipping"]["city"], "Приклад")
        self.assertEqual(result["shipping"]["office"], "Відділення №1")
        self.assertTrue(result["packaging"]["exclude_receipt"])
        unproven = extract_conversation_agreement([message(10, "user", "Приклад Отримувача")])
        self.assertEqual(unproven["shipping"], {})

    def test_inline_phone_name_and_comma_np_city_have_field_source_proof(self):
        result = extract_conversation_agreement([
            message(10, "user", "0630000000 Приклад Отримувача\nМісто Прикладу, НП №4"),
        ])
        self.assertEqual(result["shipping"]["full_name"], "Приклад Отримувача")
        self.assertEqual(result["shipping"]["city"], "Місто Прикладу")
        self.assertEqual(result["shipping"]["office"], "Відділення №4")
        self.assertEqual(result["shipping"]["field_evidence"]["full_name"]["source_message_id"], 10)

    def test_negative_reminder_to_include_receipt_does_not_mean_exclude(self):
        result = extract_conversation_agreement([message(10, "user", "Це подарунок, не забудьте вкласти чек")])
        self.assertFalse(result["packaging"].get("exclude_receipt", False))

    def test_reset_and_counteroffer_end_old_confirmation_anchor(self):
        result = extract_conversation_agreement([
            message(10, "manager", "Футболка SamplePrint42 біла L oversize"), message(11, "user", "Так"),
            message(12, "user", "Хочу іншу футболку"), message(13, "user", "Так"),
        ])
        self.assertEqual(result["items"], [])
        self.assertIn("customer_selection_reset", result["uncertainty_reasons"])
        result = extract_conversation_agreement([
            message(10, "manager", "Футболка SamplePrint42 біла L oversize"), message(11, "user", "А M є?"), message(12, "user", "Так"),
        ])
        self.assertEqual(result["items"], [])

    def test_accepted_configuration_withdrawal_does_not_revive_old_offer(self):
        result = extract_conversation_agreement([
            message(10, "manager", "Футболка SamplePrint42 біла L oversize"), message(11, "user", "Так"),
            message(12, "user", "Не хочу цей розмір"), message(13, "user", "Так"),
        ])
        self.assertEqual(result["items"], [])

    def test_invalid_source_identity_or_order_abstains(self):
        for rows in ([message(11, "manager", "Футболка SamplePrint42 біла L"), message(10, "user", "Так")],
                     [message(10, "manager", "Футболка SamplePrint42 біла L"), message(10, "user", "Так")],
                     [{"role": "manager", "text": "Футболка SamplePrint42 біла L"}, message(10, "user", "Так")]):
            with self.subTest(rows=rows):
                result = extract_conversation_agreement(rows)
                self.assertEqual(result["items"], [])
                self.assertIn("transcript_identity_or_order_invalid", result["uncertainty_reasons"])


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class ConversationAgreementPersistenceTests(TestCase):
    def setUp(self):
        from management.models import IgClient, IgCommercialEpisode, InstagramBotMessage
        self.now = timezone.now()
        self.customer = IgClient.objects.create(igsid="synthetic-agreement-customer")
        self.episode = IgCommercialEpisode.objects.create(client=self.customer, sequence=1,
            materialization_key="synthetic-agreement-episode")
        self.customer.current_commercial_episode = self.episode
        self.customer.save(update_fields=["current_commercial_episode"])
        from management.services.ig_conversation_routes import conversation_route_reset_floor
        self.reset_floor = conversation_route_reset_floor(self.customer.pk)
        self.rows = []
        for index, (role, text) in enumerate((
            ("user", "Хочу футболку"),
            ("manager", "L білу оверсайз TwoComms SamplePrint42"),
            ("user", "Так"),
            ("manager", "850 грн + 120 доставка = 970 грн"),
        )):
            self.rows.append(InstagramBotMessage.objects.create(client=self.customer,
                sender_id=self.customer.igsid, provider_namespace="instagram_login:synthetic-agreement-owner",
                role=role, source="echo" if role == "manager" else "webhook", status="done",
                send_state="sent" if role == "manager" else "", provider_message_id=f"synthetic-agreement-{index}",
                mid=f"synthetic-agreement-{index}", provider_created_at=self.now, text=text))

    def persist(self):
        from management.services.ig_conversation_agreement import persist_conversation_agreement
        return persist_conversation_agreement(self.customer, self.rows, watermark=self.rows[-1].pk)

    def read(self, **extra):
        from management.services.ig_conversation_agreement import read_conversation_agreement
        arguments = {"episode_id": self.episode.pk, "source_namespace": "instagram_login:synthetic-agreement-owner",
            "reset_floor": self.reset_floor, "watermark": {"message_id": self.rows[-1].pk, "event_at": self.now.isoformat()}}
        arguments.update(extra)
        return read_conversation_agreement(self.customer, **arguments)

    def test_persist_and_read_reverify_sources_without_catalogue_or_payment_effects(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        result = self.persist()
        self.assertTrue(result["persisted"])
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.current_size, self.customer.current_color, self.customer.current_qty), ("L", "white", 1))
        self.assertIsNone(self.customer.current_product_id)
        with CaptureQueriesContext(connection) as queries:
            captured = self.read()
        self.assertEqual(captured["reason"], "")
        self.assertEqual(captured["agreement"]["merchandise_total"], "850.00")
        self.assertEqual(captured["agreement"]["payable_total"], "970.00")
        self.assertEqual(len(captured["source_rows"]), 4)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))
        self.assertFalse(self.customer.deals.exists())

    def test_stored_payload_cannot_forge_fact_with_valid_source_refs(self):
        self.persist()
        context = deepcopy(self.customer.sales_context)
        context["conversation_agreement"]["items"][0]["size"] = "XL"
        self.customer.sales_context = context
        self.customer.save(update_fields=["sales_context"])
        self.assertEqual(self.read()["reason"], "conversation_agreement_projection_changed")

    def test_changed_source_and_changed_namespace_invalidate_read(self):
        from management.models import InstagramBotMessage
        self.persist()
        self.assertEqual(self.read(source_namespace="instagram_login:foreign-owner")["reason"], "conversation_agreement_scope_mismatch")
        InstagramBotMessage.objects.filter(pk=self.rows[1].pk).update(text="Different source text")
        self.assertEqual(self.read()["reason"], "conversation_agreement_source_changed")

    def legacy_projection(self):
        from management.services.ig_conversation_agreement import agreement_projection_digest
        self.persist()
        context = deepcopy(self.customer.sales_context)
        item = context["conversation_agreement"]["items"][0]
        item.update(garment_type="", qty=None, unit_price=None, price_evidence_message_ids=[])
        for key in ("garment_source_message_id", "quantity_source_message_id", "quantity_inference", "price_authority"):
            item.pop(key, None)
        self.customer.sales_context = context
        self.customer.save(update_fields=["sales_context"])
        return agreement_projection_digest(context["conversation_agreement"])

    def reproject(self, expected_digest, rows=None):
        from management.services.ig_conversation_agreement import reproject_conversation_agreement
        rows = self.rows if rows is None else rows
        with patch("management.services.ig_admin_state_capture._namespace", return_value="instagram_login:synthetic-agreement-owner"):
            return reproject_conversation_agreement(self.customer, rows, watermark=rows[-1].pk,
                expected_agreement_digest=expected_digest)

    def test_explicit_reprojection_restores_current_source_facts_and_bounded_metadata(self):
        expected = self.legacy_projection()
        self.assertEqual(self.read()["reason"], "conversation_agreement_projection_changed")
        self.assertEqual(self.persist()["reason"], "agreement_retained_conversation_agreement_projection_changed")
        result = self.reproject(expected)
        self.assertTrue(result["persisted"], result)
        self.assertEqual(result["reason"], "agreement_reprojected")
        item = result["agreement"]["items"][0]
        self.assertEqual((item["garment_type"], item["qty"], item["unit_price"]), ("tshirt", 1, "850.00"))
        self.assertEqual(self.read()["reason"], "")
        self.customer.refresh_from_db()
        audit = self.customer.sales_context["conversation_agreement_reprojections"]
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]["prior_digest"], audit[0]["new_digest"]), (expected, result["new_digest"]))
        self.assertEqual(audit[0]["reason"], "explicit_source_reprojection")
        self.assertEqual(audit[0]["source_message_ids"], result["agreement"]["source_message_ids"])
        self.assertNotIn("SamplePrint", json.dumps(audit))

    def test_reprojection_refuses_changed_material_source_before_projection_bypass(self):
        from management.models import InstagramBotMessage
        expected = self.legacy_projection()
        before = deepcopy(self.customer.sales_context)
        InstagramBotMessage.objects.filter(pk=self.rows[1].pk).update(text="A changed source")
        result = self.reproject(expected)
        self.assertEqual(result["reason"], "agreement_reprojection_conversation_agreement_source_changed")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.sales_context, before)

    def test_reprojection_refuses_erasure_started_after_preview(self):
        from management.models import IgClient
        expected = self.legacy_projection()
        before = deepcopy(self.customer.sales_context)
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=self.now)
        self.assertEqual(self.reproject(expected)["reason"], "agreement_reprojection_client_unavailable")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.sales_context, before)

    def test_reprojection_refuses_reset_after_preview(self):
        expected = self.legacy_projection()
        before = deepcopy(self.customer.sales_context)
        with patch("management.services.ig_conversation_routes.conversation_route_reset_floor", return_value=self.rows[-1].pk):
            self.assertEqual(self.reproject(expected)["reason"], "agreement_reprojection_scope_changed")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.sales_context, before)

    def test_reprojection_expected_digest_refuses_a_concurrent_head_change(self):
        from management.models import IgClient
        expected = self.legacy_projection()
        newer = deepcopy(self.customer.sales_context)
        newer["conversation_agreement"]["items"][0]["size"] = "M"
        IgClient.objects.filter(pk=self.customer.pk).update(sales_context=newer)
        self.assertEqual(self.reproject(expected)["reason"], "agreement_reprojection_head_changed")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.sales_context, newer)

    def test_reprojection_rolls_back_removed_head_when_fresh_source_capture_is_refused(self):
        from management.models import InstagramBotMessage
        expected = self.legacy_projection()
        before = deepcopy(self.customer.sales_context)
        foreign = InstagramBotMessage.objects.create(client=self.customer, sender_id="synthetic-foreign-recipient",
            provider_namespace="instagram_login:synthetic-agreement-owner", role="user", source="webhook", status="done",
            mid="synthetic-foreign-refresh-source", text="A source", provider_created_at=self.now)
        result = self.reproject(expected, rows=[*self.rows, foreign])
        self.assertEqual(result["reason"], "agreement_reprojection_agreement_source_changed")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.sales_context, before)

    def test_capture_cannot_borrow_future_agreement_or_source_time(self):
        from datetime import timedelta
        self.persist()
        result = self.read(watermark={"message_id": self.rows[2].pk, "event_at": self.now.isoformat()})
        self.assertEqual(result["reason"], "conversation_agreement_after_capture")
        result = self.read(watermark={"message_id": self.rows[-1].pk, "event_at": (self.now - timedelta(seconds=1)).isoformat()})
        self.assertEqual(result["reason"], "conversation_agreement_source_after_capture")

    def test_privacy_and_reset_floor_invalidate_existing_projection(self):
        self.persist()
        with patch("management.services.ig_conversation_routes.conversation_route_reset_floor", return_value=self.rows[-1].pk):
            self.assertEqual(self.read()["reason"], "conversation_agreement_scope_changed")
        self.customer.privacy_erasure_started_at = self.now
        self.customer.save(update_fields=["privacy_erasure_started_at"])
        self.assertEqual(self.read()["reason"], "conversation_agreement_scope_mismatch")

    def test_persistence_rejects_foreign_owner_and_stale_write(self):
        from management.models import InstagramBotMessage
        self.persist()
        from management.services.ig_conversation_agreement import persist_conversation_agreement
        stale = persist_conversation_agreement(self.customer, self.rows[:-1], watermark=self.rows[-2].pk)
        self.assertEqual(stale["reason"], "agreement_source_stale")
        InstagramBotMessage.objects.filter(pk=self.rows[1].pk).update(sender_id="foreign-recipient")
        self.assertEqual(self.persist()["reason"], "agreement_source_changed")

    def test_explicit_expired_quote_is_not_checkout_authority(self):
        self.rows[-1].text += ", діє до 2000-01-01T12:00:00+02:00"
        self.rows[-1].save(update_fields=["text"])
        self.persist()
        self.assertEqual(self.read()["reason"], "conversation_agreement_quote_expired")

    def prepare_initial_agreement(self):
        self.customer.current_commercial_episode = None
        self.customer.save(update_fields=["current_commercial_episode"])
        self.episode.delete()
        self.persist()

    def test_owned_first_episode_transfer_preserves_all_source_proof(self):
        from management.models import IgCommercialEpisode
        from management.services.ig_conversation_agreement import initial_agreement_transfer_sources, persist_conversation_agreement
        self.prepare_initial_agreement()
        with patch("management.services.ig_admin_state_capture._namespace", return_value="instagram_login:synthetic-agreement-owner"):
            transfer = initial_agreement_transfer_sources(self.customer)
        self.assertEqual(transfer["reason"], "")
        self.assertEqual(transfer["source_floor"], self.rows[0].pk)
        self.episode = IgCommercialEpisode.objects.create(client=self.customer, sequence=1,
            materialization_key="synthetic-first-transfer", opened_watermark_message_id=transfer["source_floor"])
        self.customer.current_commercial_episode = self.episode
        self.customer.save(update_fields=["current_commercial_episode"])
        rebound = persist_conversation_agreement(self.customer, transfer["messages"], watermark=self.rows[-1].pk)
        self.assertTrue(rebound["persisted"])
        self.assertEqual(self.read()["reason"], "")
        self.assertEqual(rebound["agreement"]["scope"]["episode_id"], self.episode.pk)
        self.assertEqual(rebound["agreement"]["items"], transfer["agreement"]["items"])

    def test_first_episode_transfer_cannot_borrow_prior_sale_history(self):
        from management.services.ig_conversation_agreement import initial_agreement_transfer_sources
        self.customer.current_commercial_episode = None
        self.customer.save(update_fields=["current_commercial_episode"])
        result = initial_agreement_transfer_sources(self.customer)
        self.assertEqual(result["reason"], "initial_agreement_prior_commercial_history")

    def test_first_episode_transfer_requires_observation_of_latest_owned_source(self):
        from management.models import InstagramBotMessage
        from management.services.ig_conversation_agreement import initial_agreement_transfer_sources
        self.prepare_initial_agreement()
        InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace="instagram_login:synthetic-agreement-owner", role="user", source="webhook", status="done",
            mid="synthetic-transfer-newer-source", text="Не хочу цей розмір", provider_created_at=self.now)
        with patch("management.services.ig_admin_state_capture._namespace", return_value="instagram_login:synthetic-agreement-owner"):
            result = initial_agreement_transfer_sources(self.customer)
        self.assertEqual(result["reason"], "initial_agreement_observation_stale")

    def assert_audited_size_projection_preserved(self, operation, value, *, invalid_proof=False):
        from django.contrib.auth import get_user_model
        from management.models import IgCommerceSelectionTransition, InstagramBotMessage, InstagramBotSettings
        from management.services.ig_admin_state_capture import current_admin_state
        from management.services.ig_commerce_projection import source_preferences_for
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import parse_turn
        from management.services.ig_selection_corrections import save_size_correction
        actor = get_user_model().objects.create_superuser("synthetic-size-operator", "operator@example.invalid", "fixture")
        InstagramBotSettings.objects.update_or_create(pk=1, defaults={"ig_user_id": "synthetic-agreement-owner"})
        source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace="instagram_login:synthetic-agreement-owner", role="user", source="webhook", status="done",
            mid="synthetic-canonical-size", text="Хочу футболку розмір L", provider_created_at=self.now)
        decision = apply_turn(self.customer, source, parse_turn(source.text), reply_payload={})
        with patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"}):
            context = current_admin_state(self.customer.pk, now=self.now + timedelta(seconds=1)).state.as_dict()["boundary"]["size_correction_context"]
            self.assertTrue(context["available"], context)
            correction = save_size_correction(self.customer.pk, actor=actor, operation_id=uuid.uuid4(),
                expected_selection_revision=context["context"]["selection_revision"],
                expected_context_digest=context["context_digest"], operation=operation, value=value,
                now=self.now + timedelta(seconds=1))
            self.customer.refresh_from_db()
            decision.session.refresh_from_db()
            before_session = deepcopy(decision.session.snapshot())
            canonical = deepcopy(source_preferences_for(self.customer))
            if operation == "set":
                self.assertEqual(canonical["values"]["size"], "M")
                self.assertEqual(canonical["evidence"]["size"]["authority"], "audited_correction")
            else:
                self.assertIn("size", canonical["cleared"])
            if invalid_proof:
                self.customer.current_size = value or ""
                self.customer.save(update_fields=["current_size"])
                event = IgCommerceSelectionTransition.objects.get(pk=correction.transition_id)
                effects = deepcopy(event.effects)
                effects["manager_correction"]["input_digest"] = "a" * 64
                # Simulate storage corruption using the upstream correction
                # regression pattern; keep the append-only ORM guard intact.
                from django.db import connection
                table = connection.ops.quote_name(IgCommerceSelectionTransition._meta.db_table)
                if connection.vendor == "mysql":
                    # MariaDB also enforces append-only receipts with triggers.
                    # Validate the malformed copy and simulate unavailable
                    # source projection without mutating the protected ledger.
                    from management.services.ig_selection_corrections import validated_correction_receipt
                    corrupted = deepcopy(event)
                    corrupted.effects = effects
                    receipt_context = event.effects["manager_correction"]["context"]
                    scope = {**receipt_context["scope"], "reset_id": receipt_context["reset_id"]}
                    self.assertIsNone(validated_correction_receipt(corrupted, scope=scope,
                        namespace=receipt_context["source_namespace"]))
                    unavailable = patch("management.services.ig_selection_corrections.validated_correction_receipt", return_value=None)
                    unavailable.start()
                    self.addCleanup(unavailable.stop)
                else:
                    with connection.cursor() as cursor:
                        cursor.execute(f"UPDATE {table} SET effects=%s WHERE id=%s", [json.dumps(effects), event.pk])
                canonical = source_preferences_for(self.customer)
                self.assertEqual(canonical, {})
            before_size = self.customer.current_size
            result = self.persist()
            self.assertTrue(result["persisted"], result)
            self.customer.refresh_from_db()
            decision.session.refresh_from_db()
            self.assertIsNone(self.customer.current_product_id)
            self.assertEqual(self.customer.current_size, before_size)
            self.assertEqual(decision.session.snapshot(), before_session)
            self.assertEqual(source_preferences_for(self.customer), canonical)
            self.assertEqual(result["agreement"]["items"][0]["size"], "L")

    def test_agreement_does_not_overwrite_audited_canonical_size(self):
        self.assert_audited_size_projection_preserved("set", "M")

    def test_agreement_does_not_revive_audited_canonical_size_clear(self):
        self.assert_audited_size_projection_preserved("clear", None)

    def test_invalid_canonical_correction_proof_cannot_revive_historical_size(self):
        self.assert_audited_size_projection_preserved("set", "M", invalid_proof=True)

    def noise(self, count=100):
        from management.models import InstagramBotMessage
        return [InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace="instagram_login:synthetic-agreement-owner", role="user", source="webhook", status="done",
            mid=f"synthetic-noise-{index}", text=f"Дякую за пояснення {index}",
            provider_created_at=self.now + timedelta(seconds=index + 1)) for index in range(count)]

    def test_material_agreement_survives_more_than_eighty_irrelevant_turns(self):
        from management.models import InstagramBotMessage
        from management.services.ig_conversation_agreement import persist_conversation_agreement
        contact = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace="instagram_login:synthetic-agreement-owner", role="user", source="webhook", status="done",
            mid="synthetic-durable-contact", text="0630000000 Приклад Отримувача\nМісто Прикладу, НП №4", provider_created_at=self.now)
        self.rows.append(contact)
        original = deepcopy(self.persist()["agreement"])
        newest = self.noise()
        result = persist_conversation_agreement(self.customer, newest[-80:], watermark=newest[-1].pk)
        self.assertTrue(result["persisted"], result)
        for key in ("items", "amounts", "shipping"):
            self.assertEqual(result["agreement"][key], original[key])
        self.assertEqual(result["agreement"]["watermark_message_id"], newest[-1].pk)
        self.assertLessEqual(len(result["agreement"]["transcript_message_ids"]), 160)
        captured = self.read(watermark={"message_id": newest[-1].pk, "event_at": newest[-1].provider_created_at.isoformat()})
        self.assertEqual(captured["reason"], "")

    def test_new_counteroffer_reset_and_withdrawal_do_not_revive_old_agreement(self):
        from management.models import InstagramBotMessage
        from management.services.ig_conversation_agreement import persist_conversation_agreement
        original = deepcopy(self.persist()["agreement"])
        newest = self.noise()
        for index, text in enumerate(("Так, тільки за 700 грн", "Хочу іншу футболку", "Не хочу білу футболку")):
            with self.subTest(text=text):
                self.customer.sales_context = {"conversation_agreement": deepcopy(original)}
                self.customer.save(update_fields=["sales_context"])
                terminal = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
                    provider_namespace="instagram_login:synthetic-agreement-owner", role="user", source="webhook", status="done",
                    mid=f"synthetic-terminal-{index}", text=text, provider_created_at=self.now + timedelta(seconds=101 + index))
                result = persist_conversation_agreement(self.customer, newest[-79:] + [terminal], watermark=terminal.pk)
                self.assertTrue(result["persisted"], result)
                self.assertEqual(result["agreement"]["items"], [])
                self.assertEqual(result["agreement"]["amounts"], {})

    def test_changed_material_source_is_not_carried_past_history_window(self):
        from management.models import InstagramBotMessage
        from management.services.ig_conversation_agreement import persist_conversation_agreement
        self.persist()
        previous = deepcopy(self.customer.sales_context)
        newest = self.noise()
        InstagramBotMessage.objects.filter(pk=self.rows[1].pk).update(text="Different source")
        result = persist_conversation_agreement(self.customer, newest[-80:], watermark=newest[-1].pk)
        self.assertFalse(result["persisted"])
        self.assertTrue(result["requires_human_review"])
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.sales_context, previous)

    def test_empty_and_all_below_floor_inputs_preserve_previous_head(self):
        from management.services.ig_conversation_agreement import persist_conversation_agreement
        self.persist()
        previous = deepcopy(self.customer.sales_context)
        empty = persist_conversation_agreement(self.customer, [], watermark=self.rows[-1].pk)
        self.assertFalse(empty["persisted"])
        with patch("management.services.ig_conversation_routes.conversation_route_reset_floor", return_value=self.rows[-1].pk + 1):
            filtered = self.persist()
        self.assertFalse(filtered["persisted"])
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.sales_context, previous)

    def test_fresh_source_quote_can_replace_expired_quote_without_bypassing_read_guards(self):
        from management.models import InstagramBotMessage
        from management.services.ig_conversation_agreement import persist_conversation_agreement
        self.rows[-1].text += ", діє до 2000-01-01T12:00:00+02:00"
        self.rows[-1].save(update_fields=["text"])
        self.persist()
        self.assertEqual(self.read()["reason"], "conversation_agreement_quote_expired")
        fresh = []
        for index, (role, text) in enumerate((("manager", "900 грн + 120 доставка = 1020 грн"), ("user", "Так"))):
            fresh.append(InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
                provider_namespace="instagram_login:synthetic-agreement-owner", role=role,
                source="echo" if role == "manager" else "webhook", status="done",
                send_state="sent" if role == "manager" else "", provider_message_id=f"synthetic-fresh-quote-{index}",
                mid=f"synthetic-fresh-quote-{index}", text=text, provider_created_at=self.now))
        result = persist_conversation_agreement(self.customer, fresh, watermark=fresh[-1].pk)
        self.assertTrue(result["persisted"], result)
        self.assertEqual(result["agreement"]["amounts"]["payable_total"], "1020.00")
        self.assertEqual(result["agreement"]["amounts"]["acceptance_message_id"], fresh[-1].pk)
        self.assertNotIn("expires_at", result["agreement"]["amounts"])
        self.assertEqual(self.read(watermark={"message_id": fresh[-1].pk, "event_at": self.now.isoformat()})["reason"], "")

    def test_owned_manager_reference_bytes_bind_empty_text_without_binding_analysis_annotations(self):
        from management.models import InstagramBotMessage
        added = []
        for index, (role, text) in enumerate((("manager", "Футболка SamplePrint42 біла L oversize"), ("manager", ""), ("user", "Так"))):
            added.append(InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
                provider_namespace="instagram_login:synthetic-agreement-owner", role=role,
                source="echo" if role == "manager" else "webhook", status="done",
                send_state="sent" if role == "manager" else "", provider_message_id=f"synthetic-manager-photo-{index}",
                mid=f"synthetic-manager-photo-{index}", text=text, provider_created_at=self.now,
                attachment_media=[{"mime": "image/jpeg", "source_part_id": "synthetic-owned-reference",
                    "content_hash": "a" * 64, "status": "owned", "private_storage": True,
                    "url": "https://example.invalid/image?signature=fixture"}] if index == 1 else []))
        self.rows.extend(added)
        result = self.persist()
        self.assertTrue(result["persisted"], result)
        photo = added[1]
        self.assertEqual(result["agreement"]["items"][0]["reference_message_ids"], [photo.pk])
        proof = result["agreement"]["evidence"][str(photo.pk)]
        self.assertEqual(len(proof["media_binding_digest"]), 64)
        self.assertNotIn("signature", json.dumps(proof))
        self.assertEqual(self.read()["reason"], "")
        annotated = deepcopy(photo.attachment_media)
        annotated[0].update(role="product", confidence=0.99, receipt_inspection={"status": "not_receipt"})
        InstagramBotMessage.objects.filter(pk=photo.pk).update(attachment_media=annotated)
        self.assertEqual(self.read()["reason"], "")
        annotated[0]["content_hash"] = "b" * 64
        InstagramBotMessage.objects.filter(pk=photo.pk).update(attachment_media=annotated)
        self.assertEqual(self.read()["reason"], "conversation_agreement_source_changed")

    def test_late_captionless_receipt_annotation_does_not_change_accepted_design_proof(self):
        from management.models import InstagramBotMessage
        late = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace="instagram_login:synthetic-agreement-owner", role="user", source="webhook", status="done",
            mid="synthetic-late-receipt", text="", provider_created_at=self.now,
            attachment_media=[{"media_type": "image", "source_part_id": "synthetic-late-part", "content_hash": "c" * 64,
                "status": "owned", "role": "unknown"}])
        self.rows.append(late)
        result = self.persist()
        self.assertTrue(result["persisted"], result)
        self.assertNotIn(late.pk, result["agreement"]["items"][0]["reference_message_ids"])
        self.assertNotIn(late.pk, result["agreement"]["source_message_ids"])
        self.assertNotIn(str(late.pk), result["agreement"]["evidence"])
        annotated = deepcopy(late.attachment_media)
        annotated[0].update(role="receipt", payment_evidence=True, confidence=0.99,
            receipt_inspection={"status": "observed", "amount": "970.00"})
        InstagramBotMessage.objects.filter(pk=late.pk).update(attachment_media=annotated)
        self.assertEqual(self.read()["reason"], "")
        self.assertEqual(self.read()["agreement"]["items"], result["agreement"]["items"])
