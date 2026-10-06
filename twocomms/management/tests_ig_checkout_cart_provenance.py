"""Frozen source-line parity survives price expansion and real order conversion."""
from copy import deepcopy
from decimal import Decimal
import hashlib
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from management.models import IgCheckoutProposal, IgCheckoutProposalItem, IgCheckoutRevision, IgClient, IgCommercialEpisode, IgDeal
from management.services import ig_checkout_cart_provenance as lineage
from management.services import ig_revision_cart_binding as source_contract
from management.services.ig_checkout_payment import CheckoutPaymentError, _revalidate_frozen_proposal, _snapshot
from orders.models import Order, OrderItem, PaymentAttempt
from orders.payment_attempts import PaymentAttemptConversionError, materialize_payment_attempt
from storefront.models import Category, Product


def ready_binding(owner, items):
    scope = {"client_id": owner["client_id"], "episode_id": owner["episode_id"],
        "order_id": None, "reset_id": None, "reset_floor": 1, "source_namespace": "ig:shop:1"}
    lines, readiness = [], {}
    for index, item in enumerate(items):
        line_id, recipient = f"line-{index}", "self" if index == 0 else f"friend-{index}"
        choices = {"product_id": item["product_id"], "quantity": item["qty"], "size": item["size"],
            "fit_option_code": item["fit_option_code"], "option_values": item.get("option_values", {})}
        proof = {"source_message_id": 20 + index, "source_digest": "a" * 64, "transition_id": 50 + index}
        selection = {"schema": "source-selection.v1", "scope": {**scope,
            "line_id": line_id, "recipient_id": recipient, "session_id": 11, "generation": 2, "revision": 4},
            "fields": {key: {"value": value, "status": "confirmed", "authority": "customer_source", "source": deepcopy(proof)}
                for key, value in choices.items()}}
        lines.append({"line_id": line_id, "recipient_id": recipient, "index": index, "source_selection": selection})
        readiness[line_id] = {"scope": {**scope, "line_id": line_id, "recipient_id": recipient},
            "capture_digest": "b" * 64, "readiness": {"applicability_known": True, "can_issue_link": True, "missing": [],
                "product": {"id": item["product_id"]}, "quantity": item["qty"], "size": {"selected": item["size"]},
                "fit": {"selected": item["fit_option_code"]}, "color": {"selected_variant_id": item.get("color_variant_id")},
                "options": {"selected": item.get("option_values", {})}}}
    captured = {"schema": "source-selections.v1", "status": "captured", "scope": scope,
        "session_id": 11, "generation": 2, "selection_revision": 4, "active_line_id": "line-0", "active_index": 0,
        "capture_digest": "b" * 64, "lines": lines}
    source = source_contract.build_cart_binding(captured_cart=captured, expected_scope=scope)
    quoted = source_contract.bind_quote_lines(binding=source, quoted_items=items, readiness_by_line=readiness)
    return source_contract.frozen_cart_binding_payload(quoted)


def original_items(product_id=37):
    return [{"proposal_item_id": 101, "position": 0, "product_id": product_id, "qty": 3, "size": "M",
        "fit_option_code": "regular", "color_variant_id": None, "option_values": {}, "unit_price": "100.00", "line_total": "260.00"},
        {"proposal_item_id": 102, "position": 1, "product_id": product_id, "qty": 1, "size": "L",
        "fit_option_code": "regular", "color_variant_id": None, "option_values": {}, "unit_price": "50.00", "line_total": "50.00"}]


def expanded_items(items):
    return [{**items[0], "qty": 1, "unit_price": "100.00", "line_total": "100.00"},
        {**items[0], "qty": 2, "unit_price": "80.00", "line_total": "160.00"}, deepcopy(items[1])]


class CheckoutCartProvenancePureTests(SimpleTestCase):
    def setUp(self):
        self.owner = {"proposal_id": "8dafdb30-a6de-4a73-bc4e-72f5b3b72261", "proposal_pk": 9,
            "revision_id": 10, "revision": 2, "client_id": 7, "deal_id": 8, "episode_id": 13}
        self.items = original_items()
        self.binding = ready_binding(self.owner, self.items)
        self.revision = {"items": deepcopy(self.items), "source_cart_binding": self.binding}
        self.cart = expanded_items(self.items)

    def capture(self, **changes):
        args = {"revision_snapshot": self.revision, "owner": self.owner, "proposal_items": self.items}
        args.update(changes)
        return lineage.capture_proposal_provenance(**args)

    def snapshot(self):
        return {"checkout_surface": "instagram_proposal", "proposal_id": self.owner["proposal_id"],
            "cart": deepcopy(self.cart), "source_cart_provenance": lineage.bind_expanded_cart(self.capture(), cart_items=self.cart)}

    def test_exact_ordered_quote_and_expanded_rows_keep_original_recipient_and_are_detached(self):
        original = deepcopy(self.revision)
        snapshot = self.snapshot()
        result = lineage.validate_attempt_provenance(snapshot)
        self.assertEqual([row["quote_position"] for row in result["item_map"]], [0, 0, 1])
        self.assertEqual([row["recipient_id"] for row in result["item_map"]], ["self", "self", "friend-1"])
        self.assertEqual([row["proposal_item_id"] for row in result["item_map"]], [101, 101, 102])
        result["binding"]["lines"][0]["recipient_id"] = "changed"
        self.assertEqual(self.revision, original)
        self.assertEqual(snapshot["source_cart_provenance"]["binding"]["lines"][0]["recipient_id"], "self")

    def test_absence_is_explicit_legacy_unknown_but_present_null_empty_or_partial_is_invalid(self):
        self.assertEqual(self.capture(revision_snapshot={}), lineage.legacy_unknown())
        self.assertEqual(lineage.validate_attempt_provenance({}), lineage.legacy_unknown())
        for raw in (None, {}, [], "model JSON", {**self.binding, "quote_line_map": []}):
            with self.subTest(raw=type(raw).__name__), self.assertRaises(lineage.CartProvenanceError):
                self.capture(revision_snapshot={"items": self.items, "source_cart_binding": raw})

    def test_present_digest_schema_or_unknown_field_cannot_become_legacy(self):
        for changes in ({"schema": "future"}, {"source_binding_digest": "c" * 64}, {"quote_binding_digest": "d" * 64}, {"model_authority": True}):
            with self.subTest(changes=changes), self.assertRaises(lineage.CartProvenanceError):
                self.capture(revision_snapshot={**self.revision, "source_cart_binding": {**self.binding, **changes}})

    def test_foreign_client_episode_or_noncanonical_owner_is_rejected(self):
        for changes in ({"client_id": 8}, {"episode_id": 14}, {"revision_id": None}, {"revision": True}, {"proposal_id": "unscoped"}):
            with self.subTest(changes=changes), self.assertRaises(lineage.CartProvenanceError):
                self.capture(owner={**self.owner, **changes})

    def test_original_proposal_configuration_quantity_order_and_frozen_quote_must_match(self):
        for changes in ({"size": "S"}, {"qty": 4}, {"position": 1}, {"option_values": {"fabric": "silk"}},
                {"line_total": "259.00"}, {"unit_price": "99.00"}):
            changed = deepcopy(self.items); changed[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(lineage.CartProvenanceError):
                self.capture(proposal_items=changed)
        changed = deepcopy(self.revision); changed["items"].reverse()
        with self.assertRaises(lineage.CartProvenanceError):
            self.capture(revision_snapshot=changed)

    def test_expansion_group_totals_missing_extra_foreign_and_reordered_rows_fail_closed(self):
        variants = [self.cart[:-1], self.cart + [self.cart[0]], [self.cart[2], *self.cart[:2]]]
        for change in ({"proposal_item_id": 999}, {"qty": 2}, {"size": "S"}, {"line_total": "101.00"}, {"unit_price": "99.00"}):
            rows = deepcopy(self.cart); rows[0].update(change); variants.append(rows)
        for rows in variants:
            with self.subTest(rows=rows), self.assertRaises(lineage.CartProvenanceError):
                lineage.bind_expanded_cart(self.capture(), cart_items=rows)

    def test_attempt_owner_map_or_proposal_group_tampering_fails_without_current_lookup(self):
        for path in ("owner", "map", "group", "cart", "proposal", "legacy"):
            changed = self.snapshot()
            if path == "owner": changed["source_cart_provenance"]["owner"]["client_id"] = 99
            if path == "map": changed["source_cart_provenance"]["item_map"][1]["recipient_id"] = "friend-1"
            if path == "group": changed["source_cart_provenance"]["proposal_items"][0]["line_total"] = "259.00"
            if path == "cart": changed["cart"][0]["qty"] = 2
            if path == "proposal": changed["proposal_id"] = "a7edf45a-aec2-4cc1-ab41-ecb20b65b88b"
            if path == "legacy": changed["source_cart_provenance"] = {"schema": lineage.SCHEMA, "status": "legacy_unknown"}
            with self.subTest(path=path), self.assertRaises(lineage.CartProvenanceError):
                lineage.validate_attempt_provenance(changed)

    def test_order_item_ids_bind_expanded_rows_without_zipping_two_source_lines_to_three_items(self):
        snapshot = self.snapshot(); provenance = lineage.validate_attempt_provenance(snapshot)
        rows = [{**row, "id": 900 + index, "order_id": 88} for index, row in enumerate(self.cart)]
        result = lineage.bind_order_items(provenance, cart_items=self.cart, order_id=88, order_items=rows)
        self.assertEqual([row["order_item_id"] for row in result["item_map"]], [900, 901, 902])
        self.assertEqual([row["recipient_id"] for row in result["item_map"]], ["self", "self", "friend-1"])
        self.assertIsNone(result["binding"]["scope"]["order_id"])
        self.assertEqual(result["order_id"], 88)
        for change in ({"id": None}, {"id": 901}, {"order_id": 89}, {"qty": 9}, {"unit_price": "1.00"}):
            changed = deepcopy(rows); changed[0].update(change)
            with self.subTest(change=change), self.assertRaises(lineage.CartProvenanceError):
                lineage.bind_order_items(provenance, cart_items=self.cart, order_id=88, order_items=changed)


class CheckoutCartProvenanceIntegrationTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Source cart", slug="source-cart")
        self.product = Product.objects.create(title="Gift shirt", slug="source-cart-shirt", category=category, price=100, status="published")
        self.customer = IgClient.objects.create(igsid="source-cart-customer")
        self.deal = IgDeal.objects.create(client=self.customer, amount=Decimal("310.00"))
        self.episode = IgCommercialEpisode.objects.create(client=self.customer, deal=self.deal, sequence=1,
            materialization_key="source-cart-episode:1", opened_watermark_message_id=1)
        self.proposal = IgCheckoutProposal.objects.create(client=self.customer, deal=self.deal, commercial_episode=self.episode,
            catalog_total=Decimal("310.00"), quoted_total=Decimal("310.00"), requested_payment_amount=Decimal("310.00"),
            items_digest="a" * 64)
        self.items = original_items(self.product.pk)
        for row in self.items:
            item = IgCheckoutProposalItem.objects.create(proposal=self.proposal, product=self.product, product_title=self.product.title,
                quantity=row["qty"], position=row["position"], size=row["size"], fit_code=row["fit_option_code"],
                catalog_unit_price=row["unit_price"], catalog_line_total=row["line_total"],
                quoted_unit_price=row["unit_price"], quoted_line_total=row["line_total"])
            row["proposal_item_id"] = item.pk

    def revision(self, *, binding=True, malformed=False):
        owner = {"proposal_id": str(self.proposal.public_id), "proposal_pk": self.proposal.pk,
            "revision_id": 1, "revision": self.proposal.revision, "client_id": self.customer.pk,
            "deal_id": self.deal.pk, "episode_id": self.episode.pk}
        frozen = deepcopy(self.items)
        frozen[0]["price_parts"] = [{"qty": 1, "unit_price": "100.00"}, {"qty": 2, "unit_price": "80.00"}]
        snapshot = {"items": frozen}
        if binding: snapshot["source_cart_binding"] = {} if malformed else ready_binding(owner, self.items)
        return IgCheckoutRevision.objects.create(proposal=self.proposal, revision=self.proposal.revision,
            digest=self.proposal.items_digest, snapshot=snapshot, source="bot_create")

    def attempt(self, snapshot, *, suffix="one", **kwargs):
        return PaymentAttempt.objects.create(fingerprint="source-cart-" + suffix, full_name="One shipping recipient",
            phone="+380501112233", city="Kyiv", np_office="Branch 1", pay_type="online_full", cart_snapshot=snapshot,
            gross_amount=Decimal("310.00"), payable_amount=Decimal("310.00"), payment_amount=Decimal("310.00"), **kwargs)

    def test_actual_snapshot_and_materializer_keep_every_split_and_exact_order_item_ids(self):
        revision = self.revision()
        snapshot = _snapshot(self.proposal)
        provenance = snapshot["source_cart_provenance"]
        self.assertEqual(provenance["owner"]["revision_id"], revision.pk)
        self.assertEqual([row["quote_position"] for row in provenance["item_map"]], [0, 0, 1])
        attempt = self.attempt(snapshot)
        with patch("storefront.views.monobank._monobank_api_request") as provider:
            order, created = materialize_payment_attempt(attempt.pk, status="success")
            replay, replay_created = materialize_payment_attempt(attempt.pk, status="success")
        provider.assert_not_called(); self.assertTrue(created); self.assertFalse(replay_created)
        self.assertEqual(replay.pk, order.pk)
        persisted = Order.objects.get(pk=order.pk).payment_payload["source_cart_provenance"]
        actual = list(OrderItem.objects.filter(order=order).order_by("id"))
        self.assertEqual([row["order_item_id"] for row in persisted["item_map"]], [item.pk for item in actual])
        self.assertEqual([row["recipient_id"] for row in persisted["item_map"]], ["self", "self", "friend-1"])
        self.assertEqual([item.qty for item in actual], [1, 2, 1])
        self.assertEqual(sum((item.line_total for item in actual), Decimal("0")), Decimal("310.00"))
        self.assertEqual(order.full_name, "One shipping recipient")
        self.assertEqual(persisted["binding"], revision.snapshot["source_cart_binding"])
        self.assertIsNone(persisted["binding"]["scope"]["order_id"])

    def test_malformed_present_revision_fails_before_catalog_recipient_or_provider(self):
        self.revision(malformed=True)
        with patch("management.services.ig_checkout.validate_checkout_items") as catalog, patch(
            "storefront.views.monobank._monobank_api_request") as provider:
            with self.assertRaises(CheckoutPaymentError): _revalidate_frozen_proposal(self.proposal)
            with self.assertRaises(CheckoutPaymentError): _snapshot(self.proposal)
        catalog.assert_not_called(); provider.assert_not_called()
        self.assertEqual(PaymentAttempt.objects.count(), 0); self.assertEqual(Order.objects.count(), 0)

    def test_absent_legacy_revision_or_binding_stays_explicit_unknown_and_convertible(self):
        for has_revision in (False, True):
            if has_revision: self.revision(binding=False)
            snapshot = _snapshot(self.proposal)
            self.assertEqual(snapshot["source_cart_provenance"], lineage.legacy_unknown())
            attempt = self.attempt(snapshot, suffix=str(has_revision))
            order, created = materialize_payment_attempt(attempt.pk, status="success")
            self.assertTrue(created)
            self.assertEqual(order.payment_payload["source_cart_provenance"], lineage.legacy_unknown())

    def test_attempt_map_tamper_rolls_back_before_order_items_or_conversion(self):
        self.revision()
        snapshot = _snapshot(self.proposal)
        snapshot["source_cart_provenance"]["item_map"][1]["recipient_id"] = "foreign"
        attempt = self.attempt(snapshot)
        with self.assertRaises(PaymentAttemptConversionError) as failed:
            materialize_payment_attempt(attempt.pk, status="success")
        self.assertEqual(failed.exception.marker, "source_cart_provenance_invalid")
        self.assertEqual(Order.objects.count(), 0); self.assertEqual(OrderItem.objects.count(), 0)
        attempt.refresh_from_db(); self.assertIsNone(attempt.order_id); self.assertEqual(attempt.payment_history, [])

    def test_order_mapping_failure_rolls_back_bulk_created_economic_graph(self):
        self.revision()
        attempt = self.attempt(_snapshot(self.proposal))
        with patch.object(lineage, "bind_order_items", side_effect=lineage.CartProvenanceError("cart_order_items_unavailable")):
            with self.assertRaises(PaymentAttemptConversionError): materialize_payment_attempt(attempt.pk, status="success")
        self.assertEqual(Order.objects.count(), 0); self.assertEqual(OrderItem.objects.count(), 0)
        attempt.refresh_from_db(); self.assertIsNone(attempt.order_id); self.assertEqual(attempt.payment_history, [])

    def test_later_repeat_episode_never_relabels_original_paid_attempt(self):
        revision = self.revision()
        snapshot = _snapshot(self.proposal)
        attempt = self.attempt(snapshot)
        self.episode.open_slot = None; self.episode.save(update_fields=["open_slot"])
        later = IgCommercialEpisode.objects.create(client=self.customer, sequence=2,
            materialization_key="source-cart-episode:2", opened_watermark_message_id=100)
        self.customer.current_commercial_episode = later
        self.customer.save(update_fields=["current_commercial_episode"])
        with patch("management.services.ig_commerce_projection.capture_current_selection_lines") as current:
            order, created = materialize_payment_attempt(attempt.pk, status="success")
        current.assert_not_called(); self.assertTrue(created)
        stored = order.payment_payload["source_cart_provenance"]
        self.assertEqual(stored["owner"]["episode_id"], self.episode.pk)
        self.assertEqual(stored["owner"]["revision_id"], revision.pk)
        self.assertEqual(stored["binding"], revision.snapshot["source_cart_binding"])

    def test_existing_series_order_without_exact_original_provenance_is_not_rebound(self):
        from orders.checkout_series import existing_series_order_idempotency_key
        self.revision()
        snapshot = _snapshot(self.proposal)
        series = hashlib.sha256(b"source-cart-existing-series").hexdigest()
        order = Order.objects.create(full_name="Earlier winner", phone="+380501112234", city="Kyiv", np_office="Branch 1",
            pay_type="online_full", total_sum=Decimal("310.00"),
            checkout_idempotency_key=existing_series_order_idempotency_key(series), payment_payload={})
        attempt = self.attempt(snapshot, checkout_series_key=series, checkout_generation=1, checkout_winner_claimed=True)
        with self.assertRaises(PaymentAttemptConversionError) as failed:
            materialize_payment_attempt(attempt.pk, status="success")
        self.assertEqual(failed.exception.marker, "source_cart_provenance_invalid")
        attempt.refresh_from_db(); self.assertIsNone(attempt.order_id)
        self.assertEqual(Order.objects.count(), 1)
        order.refresh_from_db(); self.assertNotIn("source_cart_provenance", order.payment_payload)
