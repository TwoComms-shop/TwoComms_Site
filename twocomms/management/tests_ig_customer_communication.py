"""New wording cannot change original signed delivery/choice evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.test import SimpleTestCase, override_settings

from management.services import ig_assisted_partial as partial
from management.services import ig_fulfillment_delivery as delivery
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_split_shipping as split


@override_settings(IG_FULFILLMENT_HMAC_KEY="customer-copy-test-key")
class CustomerCommunicationTests(SimpleTestCase):
    def split_data(self, locale):
        parcels = [{"line_ids": ["one"], "payer": "customer", "customer_delivery_cap": "80.00"}]
        return {"request": {"locale": locale, "lines": [{"line_id": "one", "label": "Hoodie"}]},
            "decision": {"lines": [{"line_id": "one", "preparation": None, "dispatch": None}]},
            "options": {"together": {"parcels": deepcopy(parcels)}, "separate": {"parcels": deepcopy(parcels)}}}

    def signed(self, row, kind, payload):
        row.key_id = next(iter(reviews._keys()))
        row.envelope = {"version": 1, "purpose": split.PURPOSE, "columns": row.columns, **payload}
        row.envelope_hmac = split._sign(kind, row.envelope, row.key_id)
        return row

    def native_split(self, *, new):
        data = self.split_data("en")
        if new:
            data.update(copy_version=2, rendered_copy=split._template_offer(data, split.CUSTOMER_COPY))
        offer = self.signed(SimpleNamespace(pk=uuid.uuid4(), columns={"kind": "offer"}, client_id=7, task_id=8), "offer", data)
        task = SimpleNamespace(client_id=7, reason=split.ORIGIN, trigger="event", event_key=f"split-shipping:{offer.pk}",
            status="sent", attempt_count=1, sent_at=datetime(2026, 10, 14, tzinfo=timezone.utc), claim_token="", claim_until=None,
            provider_message_id="mid.native-test", event_payload={"offer_id": str(offer.pk)})
        task.message_text = split.render_offer(offer)[0]
        receipt = self.signed(SimpleNamespace(columns={"kind": "receipt"}, task=task, task_id=8, offer_id=offer.pk,
            native_mid=task.provider_message_id), "receipt", {"body_digest": split._body_digest(offer),
                "offer_digest": offer.envelope_hmac, "delivered_at": task.sent_at.isoformat()})
        return offer, receipt

    def test_legacy_native_receipt_and_choice_survive_new_template_changes(self):
        with patch.object(reviews, "_columns", side_effect=lambda row: row.columns):
            offer, receipt = self.native_split(new=False)
            text, buttons = split.render_offer(offer)
            self.assertIn("do not guarantee arrival", text)
            with patch.object(split, "CUSTOMER_COPY", {}):
                self.assertEqual(split.render_offer(offer)[0], text)
                self.assertEqual(split.encode_choice(offer, "together"), buttons[0].payload)
                self.assertTrue(split._native_receipt(offer, receipt))

    def test_new_signed_native_body_is_frozen_and_tampering_invalidates_choice(self):
        with patch.object(reviews, "_columns", side_effect=lambda row: row.columns):
            offer, receipt = self.native_split(new=True)
            text, buttons = split.render_offer(offer)
            self.assertIn("paid separately to the carrier", text)
            self.assertIn("agreed maximum charges", text)
            self.assertNotIn("do not guarantee", text)
            with patch.object(split, "CUSTOMER_COPY", {}):
                self.assertEqual(split.render_offer(offer)[0], text)
                self.assertEqual(split.encode_choice(offer, "together"), buttons[0].payload)
                self.assertTrue(split._native_receipt(offer, receipt))
            offer.envelope["rendered_copy"]["text"] += " altered"
            self.assertEqual(split.render_offer(offer), ("", ()))
            with self.assertRaises(split.SplitShippingError):
                split.encode_choice(offer, "together")

    def task_projection(self, locale):
        request_id, decision_id = str(uuid.uuid4()), str(uuid.uuid4())
        task = SimpleNamespace(pk=1, client_id=7, event_key="update:1", manager_context={},
            event_payload={"request_id": request_id, "decision_id": decision_id,
                "request_digest": "original-request", "decision_digest": "original-decision"})
        projection = {"current_request": {"id": request_id, "locale": locale,
            "lines": [{"line_id": "one", "label": "Hoodie"}], "customer_requirements": [{"date_status": "resolved"}]},
            "decision": {"id": decision_id, "lines": [{"line_id": "one", "availability": "available"}]}}
        return task, projection

    def test_old_fulfillment_task_keeps_original_copy_and_new_signed_task_keeps_its_body(self):
        for locale in ("uk", "ru", "en"):
            with self.subTest(locale=locale):
                task, projection = self.task_projection(locale)
                old = delivery.render_manager_update(projection)
                self.assertEqual(delivery.render_task_update(task, projection), old)
                self.assertIn(delivery.COPY[locale]["arrival"], old)
                new = delivery.render_manager_update(projection, copy_version=2)
                self.assertIn(delivery.CUSTOMER_COPY[locale]["arrival"], new)
                self.assertNotEqual(old, new)
                delivery._freeze_task_update(task, projection, new)
                with patch.object(delivery, "CUSTOMER_COPY", {}):
                    self.assertEqual(delivery.render_task_update(task, projection), new)
                task.event_payload = {**task.event_payload, "decision_digest": "different-decision"}
                self.assertEqual(delivery.render_task_update(task, projection), "")

    def test_new_fulfillment_copy_rejects_body_and_owner_tampering(self):
        task, projection = self.task_projection("en")
        new = delivery.render_manager_update(projection, copy_version=2)
        delivery._freeze_task_update(task, projection, new)
        frozen = deepcopy(task.manager_context)
        task.manager_context[delivery.CUSTOMER_COPY_KEY]["body"]["text"] += " changed"
        self.assertEqual(delivery.render_task_update(task, projection), "")
        task.manager_context = frozen
        task.client_id = 9
        self.assertEqual(delivery.render_task_update(task, projection), "")

    def test_plain_availability_does_not_add_an_unrelated_arrival_notice(self):
        task, projection = self.task_projection("en")
        projection["current_request"]["customer_requirements"] = []
        self.assertNotIn("arrival", delivery.render_manager_update(projection, copy_version=2))

    def test_partial_new_template_and_signed_old_copy_are_independent(self):
        data = {"locale": "en", "kind": "partial", "task_id": 1, "labels": {"one": "Hoodie", "two": "T-shirt"},
            "quote": {"selected_line_ids": ["one"], "deferred_line_ids": ["two"], "total": "850.00",
                "delivery_scope": "now", "shipping_terms": {scope: {"payer": "customer", "customer_maximum": "80.00"}
                    for scope in ("now", "later", "together")}}}
        data["rendered_copy"] = partial._template_copy(data)
        self.assertIn("paid separately to the carrier", data["rendered_copy"]["text"])
        self.assertIn("agreed maximum charges", data["rendered_copy"]["text"])
        # The partial renderer reads the signed manifest's stored physical copy,
        # including an older authored sentence, rather than today's template.
        data["rendered_copy"]["text"] = "An original already-delivered card body."
        signed = partial._signed("partial_card", data)
        with patch.object(partial, "COPY", {}):
            original = partial._verified("partial_card", signed)
            self.assertEqual(partial._render(original, signed["key_id"])[0], "An original already-delivered card body.")
        signed["manifest"]["rendered_copy"]["text"] += " changed"
        self.assertIsNone(partial._verified("partial_card", signed))
