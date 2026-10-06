"""Operator API proof: receipt availability never confers payment authority."""
import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.ig_bot_models import IgBotNotification, IgDeal, IgPaymentConfirmationReview, IgPaymentReviewDecision
from management.models import IgClient, InstagramBotMessage, InstagramBotSettings
from management.services.ig_commercial_episodes import ensure_episode_for_deal
from management.services.ig_payment_review import record_review_decision
from orders.models import Order


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False)
class PaymentWorkspaceContractTests(TestCase):
    def setUp(self):
        self.operator = get_user_model().objects.create_superuser("receipt-workspace", "receipt@example.test", "x")
        self.client.force_login(self.operator)
        self.customer = IgClient.get_or_create_for_sender("receipt-workspace-customer")
        from management.services.instagram_bot import ingress_provider_namespace
        configuration, _ = InstagramBotSettings.objects.update_or_create(pk=1, defaults={"page_id": "receipt-test", "ig_user_id": "receipt-test"})
        self.namespace = ingress_provider_namespace(configuration)

    def _message(self, *, customer=None, index=1, deadline=None, text="Чек", role="user", media=True):
        customer = customer or self.customer
        part_id = "mp1_" + f"{index:032x}"
        part = {
            "source_part_id": part_id, "url": f"https://lookaside.fbsbx.com/receipt-{index}?signature=PRIVATE",
            "status": "owned", "private_storage": True, "mime": "image/jpeg",
            "storage_name": f"ig_message_media/private/{index}.jpg", "content_hash": hashlib.sha256(b"private").hexdigest(),
        }
        message = InstagramBotMessage.objects.create(
            client=customer, sender_id=customer.igsid, role=role, text=text, status="done",
            source="webhook", provider_namespace=self.namespace, mid=f"receipt-mid-{customer.pk}-{index}",
            media_capture_eligible=True,
            private_media_state="active", private_media_delete_after=deadline or timezone.now() + timedelta(hours=1),
            attachment_media=[part] if media else [],
        )
        return message, {**part, "message_id": message.pk, "role": "receipt"}

    def _inspected(self, source, media, *, amount="970.00", currency="UAH"):
        from management.services.ig_receipt_inspection import SCHEMA_VERSION
        facts = {"amount": amount, "currency": currency, "payment_status": "completed"}
        inspection = {
            "schema_version": SCHEMA_VERSION, "state": "inspected", "role": "receipt", "confidence": 0.96,
            "source_message_id": source.pk, "source_part_id": media["source_part_id"], "content_hash": media["content_hash"],
            "provider_model": "approved-test-model", "request_id": "workspace-receipt", "receipt_facts": facts,
        }
        part = {**source.attachment_media[0], "receipt_inspection": inspection, "receipt_facts": facts}
        source.attachment_media = [part]
        source.save(update_fields=["attachment_media"])
        return {**media, "receipt_inspection": inspection, "receipt_facts": facts, "provider_namespace": self.namespace}

    def _review(self, evidence=None, **kwargs):
        return IgPaymentConfirmationReview.objects.create(
            client=self.customer, dedupe_key=f"workspace:{IgPaymentConfirmationReview.objects.count()}",
            evidence=evidence or {}, **kwargs,
        )

    def _card(self, review):
        response = self.client.get(reverse("management_bot_orders_workspace_api"), {"review": review.pk})
        self.assertEqual(response.status_code, 200)
        return response.json()["items"][0]

    def test_owned_receipt_has_guarded_identity_and_no_transport_secrets_in_either_api(self):
        message, media = self._message()
        review = self._review({"media": [media], "messages": [{"message_id": message.pk, "quote": "Чек", "media": [media]}], "order_draft": {"media": [media]}})
        card = self._card(review)
        receipt = card["media"]["receipts"][0]
        self.assertEqual(receipt["source_part_id"], media["source_part_id"])
        self.assertEqual(receipt["availability"], "private_preview")
        self.assertEqual(receipt["preview_url"], reverse("management_bot_private_media_preview", args=[message.pk, media["source_part_id"]]))
        legacy = self.client.get(reverse("management_bot_payment_reviews_api"), {"id": review.pk})
        self.assertEqual(legacy.status_code, 200)
        for payload in (card, legacy.json()):
            serialized = json.dumps(payload)
            for secret in ("signature=", "lookaside.fbsbx.com", "ig_message_media/private", media["content_hash"]):
                self.assertNotIn(secret, serialized)

    def test_expired_and_foreign_receipts_remain_visible_without_fallback_url(self):
        _, expired = self._message(index=2, deadline=timezone.now() - timedelta(minutes=1))
        foreign = IgClient.get_or_create_for_sender("receipt-foreign")
        _, foreign_media = self._message(customer=foreign, index=3)
        card = self._card(self._review({"media": [expired, foreign_media]}))
        self.assertEqual([row["availability"] for row in card["media"]["receipts"]], ["expired", "unavailable"])
        for row in card["media"]["receipts"]:
            self.assertNotIn("preview_url", row)
            self.assertNotIn("url", row)

    def test_accepted_garment_photo_is_one_agreed_reference_not_a_second_receipt(self):
        from management.services.ig_conversation_agreement import _proof, _row
        shirt, shirt_media = self._message(index=30, role="manager", text="Погоджена біла L oversize")
        shirt.source = "echo"
        shirt.save(update_fields=["source"])
        _, example = self._message(index=31)
        example["role"] = "product"
        receipt, receipt_media = self._message(index=32)
        receipt_media = self._inspected(receipt, receipt_media)
        agreement = {"schema": "conversation-agreement.v1", "source_message_ids": [shirt.pk],
            "items": [{"title": "Agreed shirt", "qty": 1,
            "accepted_reference_message_ids": [shirt.pk]}],
            "evidence": {str(shirt.pk): _proof(_row(shirt))}}
        review = self._review({"media": [example, shirt_media, receipt_media],
            "order_draft": {"agreement": agreement}})
        groups = self._card(review)["media"]
        self.assertEqual([row["message_id"] for row in groups["agreed_products"]], [shirt.pk])
        self.assertEqual(groups["agreed_products"][0]["role"], "agreed_reference")
        self.assertEqual([row["message_id"] for row in groups["receipts"]], [receipt.pk])
        self.assertEqual([row["message_id"] for row in groups["products"]], [example["message_id"]])
        self.assertEqual(groups["unknown"], [])
        shirt.text = "Інше непогоджене фото"
        shirt.save(update_fields=["text"])
        self.assertEqual(self._card(review)["media"]["agreed_products"], [])

    def test_late_receipt_is_recovered_from_old_review_context_and_deduplicated(self):
        context = []
        products = []
        for index in range(10, 19):
            message, part = self._message(index=index)
            part["role"] = "product"
            products.append(part)
            context.append({"message_id": message.pk, "media": [part]})
        receipt_message, receipt = self._message(index=19)
        context.append({"message_id": receipt_message.pk, "media": [receipt]})
        review = self._review({"media": products[:8], "order_draft": {"context_messages": context}, "messages": [context[-1]]})
        receipts = self._card(review)["media"]["receipts"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["message_id"], receipt_message.pk)
        self.assertEqual(receipts[0]["availability"], "private_preview")

    def test_receipt_reported_amount_and_shipping_never_mark_payment_confirmed(self):
        source, media = self._message(index=20)
        media = self._inspected(source, media)
        review = self._review({"media": [media], "order_draft": {"merchandise_total": "850.00", "quoted_total": "850.00", "delivery_total": "120.00", "payable_total": "970.00"}})
        payment = self._card(review)["payment"]
        self.assertEqual(payment["merchandise_total"], "850.00")
        self.assertEqual(payment["delivery_total"], "120.00")
        self.assertEqual(payment["payable_total"], "970.00")
        self.assertEqual(payment["reported_payment_amounts"][0]["amount"], "970.00")
        self.assertEqual(payment["reported_payment_amounts"][0]["currency"], "UAH")
        self.assertFalse(payment["authoritative_for_fulfillment"])
        self.assertEqual(payment["confirmed_paid_amount"], "0.00")

    def test_unreadable_receipt_currency_is_not_filled_from_agreed_order_currency(self):
        source, media = self._message(index=25)
        media = self._inspected(source, media, currency="")
        review = self._review({"media": [media], "order_draft": {
            "currency": "UAH", "merchandise_total": "850.00", "quoted_total": "850.00",
            "delivery_total": "120.00", "payable_total": "970.00",
        }})
        card = self._card(review)
        self.assertEqual(card["payment"]["currency"], "UAH")
        self.assertEqual(card["payment"]["reported_payment_amounts"][0]["amount"], "970.00")
        self.assertEqual(card["payment"]["reported_payment_amounts"][0]["currency"], "")
        self.assertEqual(card["media"]["receipts"][0]["receipt_currency"], "")
        self.assertFalse(card["payment"]["authoritative_for_fulfillment"])

    def test_changed_or_expired_part_cannot_display_cached_ocr_facts_or_findings(self):
        for index, change in [(21, "hash"), (22, "expiry"), (23, "part_expiry"), (24, "namespace"), (26, "unknown_retention"), (27, "sibling_expiry")]:
            with self.subTest(change=change):
                source, media = self._message(index=index)
                media = self._inspected(source, media)
                evidence = {
                    "media": [media], "reported_payment_amounts": [{"message_id": source.pk, "source_part_id": media["source_part_id"], "amount": "970.00", "currency": "UAH", "verification_source": "receipt_reported"}],
                    "receipt_review_findings": [{"message_id": source.pk, "source_part_id": media["source_part_id"], "reason_codes": ["receipt_amount_mismatch"]}],
                }
                review = self._review(evidence)
                before = self._card(review)
                self.assertEqual(before["media"]["receipts"][0]["receipt_reported_amount"], "970.00")
                if change == "hash":
                    source.attachment_media[0]["content_hash"] = "b" * 64
                elif change == "expiry":
                    source.private_media_delete_after = timezone.now() - timedelta(seconds=1)
                elif change == "part_expiry":
                    source.attachment_media[0]["delete_after"] = (timezone.now() - timedelta(seconds=1)).isoformat()
                elif change == "unknown_retention":
                    source.private_media_delete_after = None
                elif change == "sibling_expiry":
                    source.attachment_media.append({
                        "source_part_id": "mp1_" + "e" * 32, "private_storage": True,
                        "delete_after": (timezone.now() - timedelta(seconds=1)).isoformat(),
                    })
                else:
                    source.provider_namespace = "different-account"
                source.save(update_fields=["attachment_media", "private_media_delete_after", "provider_namespace"])
                card = self._card(review)
                receipt = card["media"]["receipts"][0]
                self.assertEqual(receipt["receipt_reported_amount"], "")
                self.assertEqual(receipt["receipt_inspection_state"], "unavailable")
                self.assertEqual(card["payment"]["reported_payment_amounts"], [])
                self.assertEqual(card["payment"]["receipt_review_findings"], [])
                self.assertNotIn("preview_url", receipt)

    def test_notification_main_receipt_is_separate_from_review_and_uncertain_delivery(self):
        review = self._review()
        notification = IgBotNotification.objects.create(
            client=self.customer, dedupe_key=review.dedupe_key, event_type="payment_review", status="unknown",
            payload={"main_delivery_message_id": "124", "text": "SECRET", "token": "SECRET"},
            attempts=1, failure_kind="media_ambiguous_transport",
        )
        state = self._card(review)["notification"]
        self.assertTrue(state["accepted_by_telegram"])
        self.assertTrue(state["delivery_uncertain"])
        self.assertEqual(state["manager_review_status"], "pending")
        self.assertEqual(state["telegram_message_id"], "124")
        self.assertNotIn("SECRET", json.dumps(state))
        notification.payload = {}
        notification.save(update_fields=["payload"])
        self.assertFalse(self._card(review)["notification"]["accepted_by_telegram"])

    def test_deferred_material_update_does_not_claim_current_report_delivery(self):
        review = self._review()
        notification = IgBotNotification.objects.create(
            client=self.customer, dedupe_key=review.dedupe_key, event_type="payment_review",
            status="unknown", payload={"main_delivery_message_id": "124"},
        )
        review.evidence["deferred_payment_notification"] = {
            "schema": "payment-notification-deferred.v1", "notification_id": notification.pk,
            "material_digest": "a" * 64, "delivery_state": "unknown",
        }
        review.save(update_fields=["evidence"])
        state = self._card(review)["notification"]
        self.assertTrue(state["material_update_deferred"])
        self.assertTrue(state["delivery_uncertain"])
        self.assertTrue(state["accepted_by_telegram"])
        review.evidence["deferred_payment_notification"]["notification_id"] += 1
        review.save(update_fields=["evidence"])
        self.assertFalse(self._card(review)["notification"]["material_update_deferred"])

    def test_linked_order_preserves_actual_status_and_ttn(self):
        order = Order.objects.create(order_number="WORKSPACE-TTN", full_name="Client", phone="380501234567", city="Kyiv", np_office="1", total_sum=Decimal("970.00"), status="ship", payment_status="paid", tracking_number="20400000000001")
        review = self._review(order=order)
        card = self._card(review)
        self.assertEqual(card["order"]["status"], "ship")
        self.assertEqual(card["order"]["tracking_number"], "20400000000001")
        self.assertEqual(card["approval"]["state"], "pending")
        self.assertFalse(card["payment"]["authoritative_for_fulfillment"])

    def test_approval_reports_manual_completion_without_claiming_order_creation(self):
        review = self._review({"order_draft": {"quoted_total": "970.00"}})
        response = self.client.post(reverse("management_bot_payment_review_action_api", args=[review.pk]), {
            "action": "manager_verify", "verification_scope": "full_payment",
            "confirmed_amount": "970.00", "order_total_amount": "970.00",
        })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload["order_id"])
        self.assertEqual(payload["order_creation"]["status"], "needs_manual_completion")
        self.assertTrue(payload["order_creation"]["missing_fields"])
        self.assertEqual(payload["next_action"], "resolve_order")
        refreshed = self._card(review)
        self.assertEqual(refreshed["order_creation"]["status"], "needs_manual_completion")
        self.assertEqual(refreshed["order_creation"]["missing_fields"], payload["order_creation"]["missing_fields"])

    def test_new_current_episode_does_not_borrow_historical_manager_confirmation(self):
        historical = self._review({"order_draft": {"quoted_total": "970.00"}})
        record_review_decision(historical, actor=self.operator, decision="manager_verified", verification_scope="full_payment", confirmed_amount="970.00", order_total_amount="970.00")
        self.customer.refresh_from_db()
        old = self.customer.commercial_episodes.filter(open_slot=1).first()
        if old:
            old.open_slot = None
            old.closed_at = timezone.now()
            old.save(update_fields=["open_slot", "closed_at"])
        deal = IgDeal.objects.create(client=self.customer, status=IgDeal.Status.DRAFT)
        episode = ensure_episode_for_deal(deal)
        self.customer.current_commercial_episode = episode
        self.customer.save(update_fields=["current_commercial_episode"])
        response = self.client.get(reverse("management_bot_client_detail_api", args=[self.customer.pk]))
        self.assertEqual(response.status_code, 200)
        payment = response.json()["payment"]
        self.assertEqual(payment["episode_id"], episode.pk)
        self.assertEqual(payment["manager_truth"], "")
        self.assertFalse(payment["authoritative_for_fulfillment"])

    def test_legacy_actor_with_known_amount_requires_source_verification_not_amount_edit(self):
        review = self._review({"order_draft": {"quoted_total": "970.00"}}, status="confirmed")
        IgPaymentReviewDecision.objects.create(
            review=review, client=self.customer, decision="manager_verified", verification_scope="full_payment",
            confirmed_amount=Decimal("970.00"), order_total_amount=Decimal("970.00"),
            actor_source="legacy_import", actor_external_id="legacy-import", review_status_after="confirmed",
        )
        card = self._card(review)
        self.assertFalse(card["payment"]["authoritative_for_fulfillment"])
        self.assertEqual(card["approval"]["state"], "payment_unverified")
        self.assertFalse(card["approval"]["can_clarify_amount"])
        self.assertFalse(card["approval"]["can_link_existing"])

    def test_cart_confirmation_requires_owned_unchanged_acceptance_sources(self):
        from management.services.ig_conversation_agreement import extract_conversation_agreement

        seller, _ = self._message(index=30, role="manager", media=False, text="Біла футболка TWOCOMMS 1654, оверсайз, розмір L, 1 шт.")
        acceptance, _ = self._message(index=31, media=False, text="Так, беру")
        agreement = extract_conversation_agreement([seller, acceptance])
        self.assertTrue(agreement["items"])
        review = self._review({"agreement": agreement, "order_draft": {"items": agreement["items"]}})
        self.assertTrue(self._card(review)["draft"]["agreement"]["customer_confirmed"])
        acceptance.text = "Ні, не беру"
        acceptance.save(update_fields=["text"])
        self.assertFalse(self._card(review)["draft"]["agreement"]["customer_confirmed"])
