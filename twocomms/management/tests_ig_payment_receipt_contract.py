"""Receipt discovery, money provenance and current-episode stage contracts."""
from decimal import Decimal
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from management.services.ig_payment_review import (
    _merge_review_media, _persist_review_media, extract_payment_review_evidence,
    resolve_review_payment_amount,
)


class ReceiptEvidenceContractTests(SimpleTestCase):
    def test_notification_material_binds_accepted_cart_and_reported_facts_excluding_retry_noise(self):
        from management.services.ig_payment_review import _payment_notification_material
        media = {"message_id": 4, "source_part_id": "part", "content_hash": "a" * 64, "role": "receipt",
            "receipt_inspection": {"schema_version": "ig-receipt-inspection-v1", "state": "inspected",
                "source_message_id": 4, "source_part_id": "part", "content_hash": "a" * 64,
                "role": "receipt", "confidence": 0.95, "provider_model": "synthetic-model", "request_id": "synthetic-request",
                "receipt_facts": {"amount": "970.00", "currency": "UAH", "payment_status": "pending"}}}
        item = {"size": "L", "unit_price": "850", "source_message_id": 1, "acceptance_message_id": 2}
        review = SimpleNamespace(pk=1, watermark_message_id=4, deal=None,
            evidence={"media": [media], "order_draft": {"items": [item], "quoted_total": "970.00"}})
        baseline = _payment_notification_material(review)["material_digest"]
        review.watermark_message_id = 20
        item["unit_price"] = "850.00"
        media.update(url="https://example.invalid/rotating", receipt_retry_count=5, uncertainties=["retry_only"])
        self.assertEqual(_payment_notification_material(review)["material_digest"], baseline)
        item["size"] = "M"
        self.assertNotEqual(_payment_notification_material(review)["material_digest"], baseline)
        item["size"] = "L"
        media["receipt_inspection"]["receipt_facts"]["payment_status"] = "completed"
        self.assertNotEqual(_payment_notification_material(review)["material_digest"], baseline)

    def test_manager_paid_statement_requires_positive_unambiguous_completed_full_payment(self):
        from management.services.ig_payment_review import _manager_full_payment_statement
        self.assertEqual(_manager_full_payment_statement("Оплачено"), (True, None))
        self.assertEqual(_manager_full_payment_statement("Оплату отримали, 970 грн"), (True, Decimal("970.00")))
        self.assertEqual(_manager_full_payment_statement("I checked the statement, payment received"), (True, None))
        self.assertEqual(_manager_full_payment_statement("Я перевірив квитанцію, оплату отримали 970 грн"), (True, Decimal("970.00")))
        for text in ("Оплачено, потрібно перевірити", "Оплату отримали, треба звірити", "Оплачено, нужна проверка", "Оплачено, платіж на перевірці", "Payment received, requires verification", "Payment received under review"):
            with self.subTest(text=text):
                self.assertFalse(_manager_full_payment_statement(text)[0])
        for text in ("Не оплачено", "Ні, оплату отримали", "No payment received", "Payment hasn't been confirmed", "We didn't check, payment received", "Оплачено?", "Буде оплачено", "Якщо оплачено", "Если оплачено", "If payment received", "Maybe payment received", "Може оплачено", "На квитанції написано оплачено", "На чеку сплачено", "On the receipt payment received", "Клієнт каже payment received", "Customer says payment received", "According to client, payment received", "Bank says payment received", "Payment received is still pending review", "Передоплату отримали 200 грн", "Оплачено 850 грн або 970 грн", 'Клієнт написав «оплачено»', "Оплачено 970 USD", "Оплачено 120", "Дякуємо, скинемо ТТН"):
            with self.subTest(text=text):
                self.assertFalse(_manager_full_payment_statement(text)[0])
    def test_standalone_completed_assertions_require_review_without_marking_paid(self):
        for text in ("Оплатив 970 грн", "Оплатила 970 грн", "Сплатила 970 грн", "Перевів 970 грн", "Я оплатил 970 грн", "Я вже оплатила, чек у вкладенні"):
            with self.subTest(text=text):
                result = extract_payment_review_evidence([{"id": 1, "role": "user", "text": text}])
                self.assertTrue(result["needs_review"])
                self.assertFalse(result["provider_confirmed"])

    def test_questions_negations_and_payment_links_are_not_completed_assertions(self):
        for text in ("Я не оплатив 970 грн", "Оплатив?", "Я оплачу 970 грн", "Надішліть чек", "Чек ще не надіслав", "Де квитанція?", "Дайте посилання на оплату", "Як оплатити на IBAN?", "Не вкладайте чек, це подарунок", "Чек не вкладайте"):
            with self.subTest(text=text):
                result = extract_payment_review_evidence([{"id": 1, "role": "user", "text": text}])
                self.assertFalse(result["needs_review"])

    def test_receipt_link_is_review_evidence_and_never_a_checkout_link(self):
        result = extract_payment_review_evidence([{
            "id": 1, "role": "user", "text": "Ось чек, посилання https://example.invalid/receipt.pdf",
        }])
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["media"][0]["type"], "receipt_link")
        self.assertFalse(result["media"][0]["catalog_match_allowed"])

    def test_accepted_manager_configuration_and_shipping_split_survive_intervening_acks(self):
        result = extract_payment_review_evidence([
            {"id": 1, "role": "manager", "text": "1 футболка L оверсайз біла TWOCOMMS 1654"},
            {"id": 2, "role": "user", "text": "Так"},
            {"id": 3, "role": "manager", "text": "850 грн + 120 доставка = 970 грн"},
            {"id": 4, "role": "user", "text": "Добре"},
            {"id": 5, "role": "manager", "text": "Пакет окремо"},
            {"id": 6, "role": "user", "text": "Так"},
            {"id": 7, "role": "user", "text": "", "media": [{"url": "https://example.invalid/receipt", "type": "image"}]},
        ])
        self.assertTrue(result["needs_review"])
        draft = result["order_draft"]
        self.assertEqual(draft["quoted_total"], "850.00")
        self.assertEqual(draft["delivery_amount"], "120.00")
        self.assertEqual(draft["payable_total"], "970.00")
        self.assertEqual(draft["items"][0]["size"], "L")
        self.assertEqual(draft["items"][0]["unit_price"], "850.00")
        self.assertEqual(result["message_ids"], [7])
        from management.services.ig_payment_review import _alert_text
        review = SimpleNamespace(pk=11, evidence=result)
        alert = _alert_text(review, SimpleNamespace(pk=12))
        self.assertIn("Сума: 970.00", alert)
        self.assertIn("Товар: 850.00 грн", alert)
        self.assertIn("Доставка: 120.00 грн", alert)
        self.assertIn("Статус: provider_unconfirmed", alert)
        # A component total detached from the actual agreement source must not
        # be presented as the reviewed shipping split.
        with patch.dict(draft, {"amount_source_message_id": 999}):
            unrelated = _alert_text(review, SimpleNamespace(pk=12))
        self.assertNotIn("Товар:", unrelated)
        self.assertNotIn("Доставка:", unrelated)

    def test_manager_instructions_and_thanks_never_create_payment_evidence(self):
        result = extract_payment_review_evidence([
            {"id": 1, "role": "manager", "text": "Оплата на IBAN, сума 970 грн. Надішліть чек."},
            {"id": 2, "role": "manager", "text": "Дякуємо, по відправленню скинемо вам ТТН"},
        ])
        self.assertFalse(result["needs_review"])
        self.assertEqual(result["manager_confirmation_observations"], [])

    def test_explicit_manager_confirmation_is_a_source_observation_requiring_human_approval(self):
        result = extract_payment_review_evidence([
            {"id": 1, "role": "user", "text": "Оплатив 970 грн"},
            {"id": 2, "role": "manager", "text": "Оплату отримали, 970 грн"},
            {"id": 3, "role": "user", "text": "Оплату підтверджено, 970 грн"},
        ])
        observations = result["manager_confirmation_observations"]
        self.assertEqual([row["message_id"] for row in observations], [2])
        self.assertTrue(observations[0]["requires_human_review"])
        self.assertFalse(observations[0]["authoritative_for_fulfillment"])

    def test_all_private_receipt_references_survive_mixed_media_beyond_first_eight(self):
        media = [{"url": f"https://example.invalid/{i}", "role": "product"} for i in range(10)]
        media.append({"url": "https://example.invalid/receipt", "role": "receipt", "provenance": "live_webhook", "status": "owned", "storage_name": "private/blob", "source_part_id": "part-11", "content_hash": "a" * 64})
        saved = _persist_review_media(media)
        self.assertEqual(len(saved), 11)
        self.assertEqual(saved[-1]["storage_name"], "private/blob")
        self.assertEqual(saved[-1]["source_part_id"], "part-11")

    def test_media_merge_uses_stable_part_instead_of_rotating_signed_url(self):
        old = {"source_part_id": "part", "url": "https://example.invalid/old", "status": "owned", "provenance": "live_webhook", "storage_name": "blob"}
        new = {**old, "url": "https://example.invalid/new", "receipt_facts": {"amount": "970.00"}}
        rows = _merge_review_media([old], [new])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["receipt_facts"]["amount"], "970.00")

    def test_full_payment_contract_uses_payable_total_without_turning_delivery_into_unit_price(self):
        review = SimpleNamespace(pk=1, watermark_message_id=7, deal=None, evidence={"order_draft": {
            "quoted_total": "850.00", "merchandise_total": "850.00", "delivery_amount": "120.00", "payable_total": "970.00",
            "items": [{"unit_price": "850.00"}],
        }})
        contract = resolve_review_payment_amount(review, verification_scope="full_payment", confirmed_amount="970.00")
        self.assertEqual(contract["order_total"], Decimal("970.00"))
        self.assertEqual(review.evidence["order_draft"]["items"][0]["unit_price"], "850.00")
        with self.assertRaises(ValueError):
            resolve_review_payment_amount(review, verification_scope="full_payment", confirmed_amount="850.00")

    def test_receipt_amount_currency_and_transfer_status_remain_reported_findings(self):
        media = {
            "url": "https://example.invalid/receipt", "message_id": 4, "source_message_id": 4,
            "source_part_id": "part", "content_hash": "a" * 64, "role": "receipt",
            "receipt_facts": {"amount": "500.00", "currency": "EUR", "payment_status": "pending"},
            "receipt_inspection": {"schema_version": "ig-receipt-inspection-v1", "state": "inspected",
                "source_message_id": 4, "source_part_id": "part", "content_hash": "a" * 64,
                "role": "receipt", "confidence": 0.95, "provider_model": "synthetic-model", "request_id": "synthetic-request",
                "receipt_facts": {"amount": "500.00", "currency": "EUR", "payment_status": "pending", "recipient_name": "Synthetic recipient"}},
        }
        result = extract_payment_review_evidence([
            {"id": 1, "role": "manager", "text": "1 футболка L оверсайз біла TWOCOMMS 1654"},
            {"id": 2, "role": "user", "text": "Так"},
            {"id": 3, "role": "manager", "text": "850 грн + 120 доставка = 970 грн"},
            {"id": 4, "role": "user", "text": "", "media": [media]},
        ])
        finding = result["receipt_review_findings"][0]
        self.assertEqual(set(finding["reason_codes"]), {"receipt_amount_mismatch", "receipt_currency_mismatch", "receipt_transfer_pending"})
        self.assertTrue(result["needs_review"])
        self.assertFalse(result["provider_confirmed"])
        self.assertEqual(result["reported_payment_amounts"][0]["verification_source"], "receipt_reported")
        self.assertNotIn("500.00", [row["amount"] for row in result["amount_evidence"]])

    def test_unreadable_provider_observation_can_retry_same_owned_source(self):
        from management.services.ig_payment_review import _review_media_needs_owned_retry
        item = {"provenance": "live_webhook", "status": "owned", "storage_name": "private/blob",
            "receipt_inspection": {"state": "deferred", "reason": "receipt_quota_unavailable"}}
        self.assertTrue(_review_media_needs_owned_retry({"media": [item]}))
        item["receipt_inspection"] = {"state": "inspected", "role": "receipt"}
        self.assertFalse(_review_media_needs_owned_retry({"media": [item]}))

    def test_reviewed_shipping_contract_is_checked_against_current_merchandise(self):
        from management.services.ig_order_amounts import order_amounts
        contract = {"merchandise_total": "850.00", "delivery_amount": "120.00", "payable_total": "970.00", "actor_id": 1, "review_id": 2, "decision_id": 3, "evidence_message_ids": [7], "prepaid": True, "payer_type": "Sender"}
        order = SimpleNamespace(total_sum="850.00", discount_amount="0", payment_payload={"instagram_delivery_contract": contract})
        self.assertEqual(order_amounts(order)["payable"], Decimal("970.00"))
        self.assertTrue(order_amounts(order)["delivery_contract_valid"])
        order.discount_amount = "50.00"
        self.assertFalse(order_amounts(order)["delivery_contract_valid"])
        self.assertEqual(order_amounts(order)["delivery"], Decimal("0.00"))


class ReceiptPersistenceContractTests(TestCase):
    def setUp(self):
        from management.ig_bot_models import IgClient
        self.client = IgClient.get_or_create_for_sender("synthetic-receipt-contract")
        self.actor = get_user_model().objects.create_user(username="synthetic_receipt_reviewer", is_staff=True)

    def test_generic_owned_receipt_is_discovered_before_needs_review_and_stays_pending(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.models import InstagramBotMessage
        from management.services.ig_payment_review import create_payment_review
        media = {"url": "https://example.invalid/receipt", "type": "image", "mime": "image/jpeg", "private_storage": True, "provenance": "live_webhook", "status": "owned", "storage_name": "private/test", "source_part_id": "part", "content_hash": "a" * 64}
        source = InstagramBotMessage.objects.create(client=self.client, sender_id=self.client.igsid, role="user", text="", source="webhook", mid="synthetic-mid", media_capture_eligible=True, attachment_media=[media])
        calls = []
        def inspect(rows, **kwargs):
            calls.append(kwargs)
            return [{**row, "role": "receipt", "receipt_inspection": {"state": "inspected", "schema_version": "ig-receipt-inspection-v1", "source_message_id": source.pk, "source_part_id": "part", "content_hash": "a" * 64, "role": "receipt", "confidence": 0.95, "provider_model": "synthetic-model", "request_id": "synthetic-request", "receipt_facts": {"amount": "970.00", "currency": "UAH", "payment_status": "completed", "recipient_name": "Synthetic recipient"}}} for row in rows]
        with patch("management.services.ig_receipt_inspection.inspect_receipt_media", side_effect=inspect), patch("management.services.instagram_bot.notify_manager"), patch("management.services.ig_payment_review._raw_media_by_mid", return_value={}):
            review = create_payment_review(self.client, watermark=source.pk, messages=[{"id": source.pk, "role": "user", "text": "", "media": [media]}])
        self.assertIsNotNone(review)
        self.assertEqual(review.status, IgPaymentConfirmationReview.Status.PENDING)
        self.assertFalse(review.decisions.exists())
        self.assertEqual(review.evidence["reported_payment_amounts"][0]["amount"], "970.00")
        self.assertTrue(calls[0]["allow_provider"])

    def test_historical_review_approval_cannot_change_current_new_purchase_stage(self):
        from management.ig_bot_models import IgCommercialEpisode, IgPaymentConfirmationReview
        from management.services.ig_payment_review import record_review_decision
        review = IgPaymentConfirmationReview.objects.create(client=self.client, dedupe_key="synthetic-old-receipt", watermark_message_id=2, evidence={"order_draft": {"quoted_total": "850.00"}})
        IgCommercialEpisode.objects.create(client=self.client, sequence=1, materialization_key="synthetic-old", primary_payment_review=review, open_slot=None)
        current = IgCommercialEpisode.objects.create(client=self.client, sequence=2, materialization_key="synthetic-current", open_slot=1, opened_watermark_message_id=10)
        self.client.current_commercial_episode = current
        self.client.stage = "payment_pending"
        self.client.save(update_fields=["current_commercial_episode", "stage"])
        with patch("management.services.bot_conversation_analysis.schedule_client_truth_analysis"), patch("orders.services.ig_review_order_builder.create_order_from_payment_review", return_value={"status": "needs_manual_completion", "order": None}):
            approved = record_review_decision(review, actor=self.actor, decision="manager_verified", verification_scope="full_payment", confirmed_amount="850.00")
        self.client.refresh_from_db()
        self.assertEqual(approved.status, "confirmed")
        self.assertEqual(self.client.stage, "payment_pending")
        self.assertEqual(approved.decisions.get().stage_after, "payment_pending")

    def test_rejection_does_not_regress_a_completed_customer(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.services.ig_payment_review import record_review_decision
        self.client.stage = "done"
        self.client.save(update_fields=["stage"])
        review = IgPaymentConfirmationReview.objects.create(client=self.client, dedupe_key="synthetic-completed", watermark_message_id=2)
        with patch("management.services.bot_conversation_analysis.schedule_client_truth_analysis"):
            record_review_decision(review, actor=self.actor, decision="manager_rejected", reason_code="receipt_unreadable")
        self.client.refresh_from_db()
        self.assertEqual(self.client.stage, "done")


from management.tests_ig_human_reply_delivery import _HumanStoreFixture


class AuthenticatedManagerPaymentStatementTests(_HumanStoreFixture):
    def setUp(self):
        super().setUp()
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.services.ig_commercial_episodes import ensure_episode_for_review
        self.source.text = "Я оплатив 970 грн"
        self.source.save(update_fields=["text"])
        self.review = IgPaymentConfirmationReview.objects.create(
            client=self.customer, dedupe_key="authenticated-manager-statement",
            watermark_message_id=self.source.pk, evidence={"order_draft": {"quoted_total": "970.00"}},
        )
        self.episode = ensure_episode_for_review(self.review)
        self.schedule = patch("management.services.bot_conversation_analysis.schedule_client_truth_analysis")
        self.schedule.start()
        self.addCleanup(self.schedule.stop)
        self.builder = patch("orders.services.ig_review_order_builder.create_order_from_payment_review", return_value={"status": "needs_manual_completion", "order": None})
        self.builder_mock = self.builder.start()
        self.addCleanup(self.builder.stop)

    def sent_manager_source(self, text):
        from management.services.ig_human_reply_transport import project_human_part_receipt
        command, _rows, claim = self.started(text)
        self.confirm(claim)
        command.refresh_from_db()
        projected = project_human_part_receipt(claim.part.pk)
        self.assertIsNotNone(projected.message)
        return projected.message, command, claim.part

    def apply(self, source):
        from management.services.ig_payment_review import apply_authenticated_manager_payment_confirmation
        return apply_authenticated_manager_payment_confirmation(source.pk)

    def test_canonical_sent_manager_statement_records_exact_payment_with_real_actor(self):
        source, command, part = self.sent_manager_source("Оплату отримали, 970 грн")
        approved = self.apply(source)
        self.assertEqual(approved.status, "confirmed")
        decision = approved.decisions.get()
        self.assertEqual(decision.actor_id, self.actor.pk)
        self.assertEqual(decision.confirmed_amount, Decimal("970.00"))
        self.assertEqual(decision.verification_scope, "full_payment")
        self.assertEqual(decision.reason_code, "authenticated_manager_chat_confirmation")
        self.assertIn(f"source_message:{source.pk}", decision.reason_text)
        self.builder_mock.assert_called_once()
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.stage, "paid")
        self.assertFalse(self.customer.payment_projections.exists())
        self.assertIsNone(self.apply(source))
        self.assertEqual(approved.decisions.count(), 1)

    def test_bare_paid_statement_uses_known_current_full_amount_only(self):
        source, _, _ = self.sent_manager_source("Оплачено")
        approved = self.apply(source)
        self.assertEqual(approved.decisions.get().confirmed_amount, Decimal("970.00"))

    def test_plain_external_manager_echo_has_no_authenticated_payment_authority(self):
        from management.models import InstagramBotMessage
        source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace=self.namespace, role="manager", source="webhook", text="Оплачено, 970 грн", status="done")
        self.assertIsNone(self.apply(source))
        self.assertFalse(self.review.decisions.exists())

    def assert_legacy_paid_receipt_has_no_current_payment_authority(self, *, delete_actor=False):
        from management.ig_bot_models import HumanReplyCommand, IgClient, IgPaymentConfirmationReview, IgPaymentReviewDecision
        from management.ig_human_reply_models import HumanReplyPart
        from management.models import InstagramBotMessage
        from management.services.ig_legacy_human_receipts import find_legacy_human_receipt
        from management.tests_ig_legacy_human_receipts import LegacyHumanReceiptReadTests
        from orders.models import Order

        command, source = LegacyHumanReceiptReadTests.receipt(
            self, ids=["synthetic-legacy-paid-mid"], text="Оплату отримали, 970 грн",
        )
        # The reader admits an already retained namespace and one exact MID.
        # These satisfy the adapter's outer source checks, so the absence of a
        # canonical modern part must still deny current financial authority.
        source.provider_namespace = self.namespace
        source.save(update_fields=["provider_namespace"])
        if delete_actor:
            self.actor.delete()
            command.refresh_from_db()
            self.assertIsNone(command.actor_id)
        else:
            self.assertEqual(command.actor_id, self.actor.pk)
            self.assertTrue(self.actor.is_superuser)
        self.assertFalse(HumanReplyPart.objects.filter(command=command).exists())

        before_client = IgClient.objects.values().get(pk=self.customer.pk)
        before_review = IgPaymentConfirmationReview.objects.values().get(pk=self.review.pk)
        before_command = HumanReplyCommand.objects.values().get(pk=command.pk)
        before_source = InstagramBotMessage.objects.values().get(pk=source.pk)
        counts = {model: model.objects.count() for model in (
            Order, IgPaymentReviewDecision, InstagramBotMessage, HumanReplyPart,
        )}
        proof = find_legacy_human_receipt(
            client_id=self.customer.pk, namespace=self.namespace, recipient=self.customer.igsid,
            mid=source.provider_message_id, command_id=command.pk, manager_message_id=source.pk,
        )
        self.assertTrue(proof.accepted, proof.reason)
        self.assertEqual(proof.reason, "exact_legacy_human_provider_receipt")
        self.assertIsNone(self.apply(source))
        self.builder_mock.assert_not_called()
        self.transport_mock.assert_not_called()
        self.assertFalse(self.review.decisions.exists())
        for model, count in counts.items():
            self.assertEqual(model.objects.count(), count, model.__name__)
        self.assertEqual(IgClient.objects.values().get(pk=self.customer.pk), before_client)
        self.assertEqual(IgPaymentConfirmationReview.objects.values().get(pk=self.review.pk), before_review)
        self.assertEqual(HumanReplyCommand.objects.values().get(pk=command.pk), before_command)
        self.assertEqual(InstagramBotMessage.objects.values().get(pk=source.pk), before_source)

    def test_accepted_legacy_whole_command_paid_receipt_cannot_confirm_current_payment(self):
        self.assert_legacy_paid_receipt_has_no_current_payment_authority()

    def test_accepted_legacy_paid_receipt_with_deleted_actor_cannot_confirm_current_payment(self):
        self.assert_legacy_paid_receipt_has_no_current_payment_authority(delete_actor=True)


    def test_operator_without_payment_capability_cannot_confirm_through_chat(self):
        from django.contrib.auth.models import Permission
        source, command, _ = self.sent_manager_source("Оплачено, 970 грн")
        self.actor.is_superuser = False
        self.actor.save(update_fields=["is_superuser"])
        self.actor.user_permissions.add(Permission.objects.get(codename="operate_ig_bot"), Permission.objects.get(codename="view_ig_conversation_pii"))
        self.assertIsNone(self.apply(source))
        self.assertFalse(self.review.decisions.exists())

    def test_questions_negations_deposits_wrong_amount_and_reported_words_cannot_verify(self):
        for index, text in enumerate(("Не оплачено", "Оплачено?", "Передоплату отримали 200 грн", "Оплачено 850 грн", 'Клієнт написав «оплачено»', "No payment received", "Ні, оплату отримали", "Якщо оплачено", "If payment received", "На квитанції написано оплачено", "Клієнт каже payment received", "Customer says payment received", "We didn't check, payment received", "Maybe payment received")):
            with self.subTest(text=text):
                # One independent command keeps the exact existing source;
                # distinct provider receipts are required by the human ledger.
                from management.services.ig_human_reply_transport import project_human_part_receipt
                command, _, claim = self.started(text)
                self.confirm(claim, mid=f"synthetic-paid-negative-{index}")
                source = project_human_part_receipt(claim.part.pk).message
                self.assertIsNone(self.apply(source))
        self.assertFalse(self.review.decisions.exists())

    def test_changed_reset_source_or_queue_claim_cannot_apply_old_statement(self):
        from management.services.ig_payment_review import apply_authenticated_manager_payment_confirmation
        from management.ig_bot_models import IgFunnelResetAudit
        source, _, _ = self.sent_manager_source("Оплачено 970 грн")
        self.assertIsNone(apply_authenticated_manager_payment_confirmation(source.pk, observation_guard=lambda: "claim_lost"))
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=source.pk, reason="synthetic reset")
        self.assertIsNone(self.apply(source))
        self.assertFalse(self.review.decisions.exists())

@override_settings(ROOT_URLCONF="twocomms.urls_management")
class FirstReviewAgreementScopeTests(TestCase):
    def setUp(self):
        from django.utils import timezone
        from management.models import IgClient, InstagramBotMessage
        from management.services.ig_conversation_agreement import persist_conversation_agreement, _row
        self.customer = IgClient.objects.create(igsid="synthetic-first-review-scope")
        self.actor = get_user_model().objects.create_superuser(username="synthetic-first-review-actor", email="scope@example.invalid", password="test")
        self.namespace = "instagram_login:synthetic-first-review-owner"
        self.rows = []
        for index, (role, text) in enumerate((
            ("user", "Хочу одну футболку"), ("manager", "L білу оверсайз TwoComms SamplePrint42"),
            ("user", "Так"), ("manager", "850 грн + 120 доставка = 970 грн"),
        )):
            self.rows.append(InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
                provider_namespace=self.namespace, role=role, source="echo" if role == "manager" else "webhook",
                status="done", send_state="sent" if role == "manager" else "", provider_message_id=f"scope-source-{index}",
                mid=f"scope-source-{index}", provider_created_at=timezone.now(), text=text))
        result = persist_conversation_agreement(self.customer, self.rows, watermark=self.rows[-1].pk)
        self.assertTrue(result["persisted"])
        self.draft = extract_payment_review_evidence([_row(row) for row in self.rows])["order_draft"]

    def test_first_approved_review_transfers_only_reverified_initial_agreement_and_card_keeps_acceptance(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.services.ig_payment_review import record_review_decision
        from management.services.ig_conversation_agreement import read_conversation_agreement
        from management.services.ig_conversation_routes import conversation_route_reset_floor
        from management.bot_views import _payment_review_workspace_payload
        review = IgPaymentConfirmationReview.objects.create(client=self.customer, dedupe_key="synthetic-first-scope-review",
            watermark_message_id=self.rows[-1].pk, evidence={"order_draft": self.draft, "agreement": self.draft["agreement"]})
        with patch("management.services.ig_admin_state_capture._namespace", return_value=self.namespace), patch("management.services.bot_conversation_analysis.schedule_client_truth_analysis"), patch("orders.services.ig_review_order_builder.create_order_from_payment_review", return_value={"status": "needs_manual_completion", "order": None}):
            approved = record_review_decision(review, actor=self.actor, decision="manager_verified", verification_scope="full_payment", confirmed_amount="970.00")
        self.customer.refresh_from_db()
        episode = self.customer.current_commercial_episode
        self.assertEqual(episode.opened_watermark_message_id, self.rows[0].pk)
        read = read_conversation_agreement(self.customer, episode_id=episode.pk, source_namespace=self.namespace,
            reset_floor=conversation_route_reset_floor(self.customer.pk))
        self.assertEqual(read["reason"], "")
        self.assertEqual(read["agreement"]["items"][0]["size"], "L")
        self.assertEqual(episode.events.filter(event_type="agreement_scope_transfer").count(), 1)
        self.assertTrue(_payment_review_workspace_payload(approved)["draft"]["agreement"]["customer_confirmed"])

    def test_old_commercial_history_cannot_lower_a_new_review_floor_or_transfer_old_agreement(self):
        from management.ig_bot_models import IgCommercialEpisode, IgPaymentConfirmationReview
        from management.services.ig_commercial_episodes import ensure_episode_for_review
        IgCommercialEpisode.objects.create(client=self.customer, sequence=1, materialization_key="synthetic-old-scope", open_slot=None)
        review = IgPaymentConfirmationReview.objects.create(client=self.customer, dedupe_key="synthetic-repeat-review",
            watermark_message_id=self.rows[-1].pk, evidence={"order_draft": self.draft})
        with patch("management.services.ig_admin_state_capture._namespace", return_value=self.namespace):
            episode = ensure_episode_for_review(review)
        self.assertEqual(episode.opened_watermark_message_id, self.rows[-1].pk)
        self.assertFalse(episode.events.filter(event_type="agreement_scope_transfer").exists())


class PaymentReviewLockOrderTests(SimpleTestCase):
    def test_episode_barrier_and_client_lock_precede_review_lock(self):
        from django.db import transaction
        from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
        from management.services.ig_payment_review import _payment_review_mutation, _lock_payment_review
        original_atomic = transaction.atomic
        original_get_connection = transaction.get_connection
        connection = original_get_connection()
        original_atomic_state = (
            connection.in_atomic_block, tuple(connection.atomic_blocks),
            tuple(connection.savepoint_ids), connection.needs_rollback,
        )
        events = []
        original = SimpleNamespace(pk=7, client_id=91)
        locked_client = SimpleNamespace(pk=91)
        locked_review = SimpleNamespace(pk=7, client_id=91)
        identity = Mock()
        identity.values_list.return_value.first.return_value = 91
        identity.select_related.return_value.first.return_value = locked_review
        clients = Mock()
        clients.filter.return_value.first.return_value = locked_client
        reviews = Mock()
        reviews.filter.return_value.select_related.return_value.first.return_value = locked_review

        @contextmanager
        def barrier(client_id):
            self.assertEqual(client_id, 91)
            events.append("barrier_enter")
            yield
            events.append("barrier_exit")

        @contextmanager
        def atomic():
            events.append("atomic_enter")
            yield
            events.append("atomic_exit")

        def lock_client():
            events.append("client_lock")
            return clients

        def lock_review():
            events.append("review_lock")
            return reviews

        with patch.object(IgPaymentConfirmationReview.objects, "filter", return_value=identity), patch.object(IgPaymentConfirmationReview.objects, "select_for_update", side_effect=lock_review), patch.object(IgClient.objects, "select_for_update", side_effect=lock_client), patch("management.services.ig_commercial_episodes.commercial_episode_client_lock", side_effect=barrier), patch("management.services.ig_payment_review.transaction.atomic", side_effect=atomic), patch("management.services.ig_payment_review.transaction.get_connection", return_value=SimpleNamespace(in_atomic_block=True)):
            with _payment_review_mutation(original):
                result = _lock_payment_review(original, related=("client", "deal"))
        self.assertEqual(events, ["barrier_enter", "atomic_enter", "client_lock", "review_lock", "atomic_exit", "barrier_exit"])
        self.assertIs(result.client, locked_client)
        reviews.filter.assert_called_once_with(pk=7, client_id=91)
        reviews.filter.return_value.select_related.assert_called_once_with(None)
        identity.select_related.assert_called_once_with("deal")
        self.assertIs(transaction.atomic, original_atomic)
        self.assertIs(transaction.get_connection, original_get_connection)
        self.assertEqual((
            connection.in_atomic_block, tuple(connection.atomic_blocks),
            tuple(connection.savepoint_ids), connection.needs_rollback,
        ), original_atomic_state)

    def test_changed_review_owner_is_rejected_before_acquiring_other_client_locks(self):
        from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
        from management.services.ig_payment_review import _payment_review_mutation
        identity = Mock()
        identity.values_list.return_value.first.return_value = 92
        with patch.object(IgPaymentConfirmationReview.objects, "filter", return_value=identity), patch.object(IgClient.objects, "select_for_update") as clients, patch.object(IgPaymentConfirmationReview.objects, "select_for_update") as reviews:
            with self.assertRaisesMessage(ValueError, "Клієнт перевірки оплати змінився"):
                with _payment_review_mutation(SimpleNamespace(pk=7, client_id=91)):
                    self.fail("Foreign review ownership was admitted")
        clients.assert_not_called()
        reviews.assert_not_called()


class PaymentNotificationMaterialRevisionTests(_HumanStoreFixture):
    """Real notification persistence; stub transport supplies exact receipts."""
    def setUp(self):
        super().setUp()
        self.source.text = "Я оплатив 970 грн"
        self.source.save(update_fields=["text"])
        self.price(120)
        self.http = patch("management.services.instagram_bot._http", side_effect=AssertionError("queue performed Telegram IO"))
        self.http_mock = self.http.start()
        self.addCleanup(self.http.stop)
        self.review = self.observe()
        self.customer.refresh_from_db()

    def observe(self):
        from management.services.ig_payment_review import create_payment_review
        from management.models import InstagramBotMessage
        watermark = InstagramBotMessage.objects.filter(client=self.customer).order_by("-pk").values_list("pk", flat=True).first()
        return create_payment_review(self.customer, watermark=watermark, allow_provider=False)

    def notification(self):
        from management.ig_bot_models import IgBotNotification
        return IgBotNotification.objects.get(dedupe_key=self.review.dedupe_key)

    def finish(self, *, status="sent", message_id="101"):
        from management.services.instagram_bot import _finish_notification
        row = self.notification()
        row.status = "sending"
        row.attempts += 1
        row.save(update_fields=["status", "attempts"])
        with self.captureOnCommitCallbacks(execute=True):
            _finish_notification(row.dedupe_key, status=status, message_id=message_id)
        return self.notification()

    def price(self, delivery):
        from management.models import InstagramBotMessage
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace=self.namespace, role="manager", source="echo", status="done", send_state="sent",
            provider_message_id=f"notification-price-{delivery}",
            text=f"850 грн + {delivery} доставка = {850 + delivery} грн")

    def test_sent_claim_receipt_revision_once_then_replay_and_noise_never_resend(self):
        from management.models import InstagramBotMessage
        row = self.finish()
        old_candidate = row.payload["payment_candidate"]
        media = {"url": "https://example.invalid/private-receipt", "type": "image", "role": "receipt",
            "source_part_id": "mp1_" + "a" * 32, "content_hash": "b" * 64,
            "storage_name": "synthetic/private-receipt", "status": "owned", "private_storage": True,
            "provenance": "live_webhook", "mime": "image/jpeg"}
        receipt = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace=self.namespace, role="user", source="webhook", status="pending", text="Ось чек",
            mid="notification-receipt", provider_message_id="notification-receipt", media_capture_eligible=True,
            private_media_state="active", attachment_media=[media])
        review = self.observe()
        self.assertEqual(review.pk, self.review.pk)
        row = self.notification()
        self.assertEqual((row.status, row.attempts, row.telegram_message_id), ("pending", 1, ""))
        self.assertNotIn("main_delivery_message_id", row.payload)
        self.assertNotIn("media", row.payload)
        self.assertNotIn(media["storage_name"], json.dumps(row.payload))
        self.assertEqual(row.payload["payment_notification_revision"]["version"], 2)
        historical = json.loads(row.audit_events.get(action="payment_material_revision").note)
        self.assertEqual(historical["telegram_message_id"], "101")
        self.assertEqual(historical["candidate_digest"], old_candidate["digest"])
        self.assertTrue(any(item.get("message_id") == receipt.pk for item in review.evidence["media"]))
        self.finish(message_id="102")
        self.observe()
        self.inbound("Дякую")
        self.observe()
        row = self.notification()
        self.assertEqual((row.status, row.attempts, row.telegram_message_id), ("sent", 2, "102"))
        self.assertEqual(row.audit_events.count(), 1)
        self.http_mock.assert_not_called()

    def test_changed_payable_refreshes_early_provider_free_branch(self):
        self.finish()
        self.price(150)
        with patch("management.services.ig_payment_review._resolve_payment_media_candidates") as classifier:
            review = self.observe()
        classifier.assert_not_called()
        row = self.notification()
        self.assertEqual(review.pk, self.review.pk)
        self.assertEqual((row.status, row.attempts), ("pending", 1))
        self.assertIn("Сума: 1000.00", row.payload["text"])
        self.assertEqual(row.payload["payment_candidate"]["order_total"], "1000.00")

    def test_unknown_sending_payloads_unchanged_and_latest_material_marked(self):
        for index, state in enumerate(("unknown", "sending")):
            with self.subTest(state=state):
                row = self.notification()
                row.status = state
                row.telegram_message_id = "retained-original"
                row.save(update_fields=["status", "telegram_message_id"])
                previous = row.payload
                self.price(150 + index * 10)
                self.observe()
                row = self.notification()
                self.review.refresh_from_db()
                self.assertEqual((row.status, row.payload, row.telegram_message_id), (state, previous, "retained-original"))
                marker = self.review.evidence["deferred_payment_notification"]
                self.assertEqual((marker["notification_id"], marker["delivery_state"]), (row.pk, state))
                self.assertFalse(row.audit_events.exists())

    def test_known_finish_queues_deferred_once_and_exact_notification_is_required(self):
        from management.services.ig_payment_review import refresh_deferred_payment_review_notification
        row = self.notification()
        row.status = "sending"
        row.save(update_fields=["status"])
        self.price(150)
        self.observe()
        self.assertFalse(refresh_deferred_payment_review_notification(self.review.pk, notification_id=row.pk + 1))
        queued = self.finish(message_id="201")
        self.assertEqual((queued.status, queued.attempts), ("pending", 1))
        self.assertEqual(queued.payload["payment_notification_revision"]["prior_telegram_message_id"], "201")
        self.assertFalse(refresh_deferred_payment_review_notification(self.review.pk, notification_id=row.pk))
        self.review.refresh_from_db()
        self.assertNotIn("deferred_payment_notification", self.review.evidence)
        self.assertEqual(queued.audit_events.count(), 1)

    def test_real_finish_hook_known_sent_only_and_exact_row_bound(self):
        with patch("management.services.ig_payment_review.refresh_deferred_payment_review_notification") as hook:
            row = self.finish()
            hook.assert_called_once_with(self.review.pk, notification_id=row.pk)
            hook.reset_mock()
            self.finish(status="unknown", message_id="")
            hook.assert_not_called()

    def test_deferred_terminal_privacy_stale_namespace_and_missing_receipt_fail_closed(self):
        from django.utils import timezone
        from management.services.ig_payment_review import refresh_deferred_payment_review_notification
        row = self.notification()
        row.status = "sending"
        row.save(update_fields=["status"])
        price = self.price(150)
        self.observe()
        row.status = "sent"
        row.telegram_message_id = "201"
        row.save(update_fields=["status", "telegram_message_id"])
        for model, field, invalid, valid in (
            (self.review, "status", "cancelled", "pending"),
            (self.customer, "privacy_erasure_started_at", timezone.now(), None),
            (self.customer, "current_commercial_episode_id", None, self.customer.current_commercial_episode_id),
            (self.review, "watermark_message_id", self.source.pk, price.pk),
            (price, "provider_namespace", "instagram_login:foreign-owner", self.namespace),
        ):
            with self.subTest(field=field):
                setattr(model, field, invalid)
                model.save(update_fields=[field])
                self.assertFalse(refresh_deferred_payment_review_notification(self.review.pk, notification_id=row.pk))
                setattr(model, field, valid)
                model.save(update_fields=[field])
        for bad_id in ("", "garbage", "0"):
            row.telegram_message_id = bad_id
            row.save(update_fields=["telegram_message_id"])
            self.assertFalse(refresh_deferred_payment_review_notification(self.review.pk, notification_id=row.pk))
        row.refresh_from_db()
        self.assertEqual(row.status, "sent")
        self.assertFalse(row.audit_events.exists())
        from management.ig_bot_models import IgFunnelResetAudit
        row.telegram_message_id = "201"
        row.save(update_fields=["telegram_message_id"])
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=price.pk, reason="synthetic reset")
        self.assertFalse(refresh_deferred_payment_review_notification(self.review.pk, notification_id=row.pk))

    def test_malformed_revision_candidate_and_deferred_schema_never_rearm(self):
        from copy import deepcopy
        from management.services.ig_payment_review import refresh_deferred_payment_review_notification
        row = self.notification()
        row.status = "sending"
        row.save(update_fields=["status"])
        self.price(150)
        self.observe()
        row.status, row.telegram_message_id = "sent", "301"
        row.save(update_fields=["status", "telegram_message_id"])
        original = deepcopy(row.payload)
        for field, invalid in (("payment_candidate", []), ("payment_notification_revision", []),
            ("payment_notification_revision", {"version": "garbage"}),
            ("payment_notification_revision", {**original["payment_notification_revision"], "version": True})):
            with self.subTest(field=field, invalid=invalid):
                row.payload = {**original, field: invalid}
                row.save(update_fields=["payload"])
                self.assertFalse(refresh_deferred_payment_review_notification(self.review.pk, notification_id=row.pk))
        row.payload = original
        row.save(update_fields=["payload"])
        self.review.refresh_from_db()
        self.review.evidence["deferred_payment_notification"]["schema"] = "foreign-schema"
        self.review.save(update_fields=["evidence"])
        self.assertFalse(refresh_deferred_payment_review_notification(self.review.pk, notification_id=row.pk))
        row.refresh_from_db()
        self.assertEqual(row.status, "sent")
        self.assertFalse(row.audit_events.exists())
