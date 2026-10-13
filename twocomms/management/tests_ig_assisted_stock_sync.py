"""Real assisted approval -> explicit recount -> ordinary finite checkout."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
import uuid

from django.test import TransactionTestCase, override_settings
from django.db import transaction

from management.ig_assisted_commerce_models import IgAssistedCommerceStockSync as Sync
from management.models import IgCheckoutProposal, IgCheckoutInventoryReservation as Reservation
from management.services import ig_assisted_commerce as commerce
from management.services import ig_assisted_stock_sync as sync
from management.services import ig_fulfillment_stock_sync as inventory
from management.services.ig_checkout import build_assisted_quote, CheckoutConfigurationError, create_or_update_proposal
from management.tests_ig_assisted_commerce import AssistedCommerceFixture
from management.tests_ig_fulfillment_stock_sync import NativeFulfillmentStockSyncTests


class AssistedStockSyncFixture(AssistedCommerceFixture):
    """Reusable native catalog/custom source and manager-ledger producers."""
    def approved_catalog_case(self, *, mixed=False, basis="warehouse_checked"):
        if mixed:
            self.canonical(f"добавьте {self.products[1].title}, количество 1, размер L, цвет purple, крій classic")
        request, revision = self.request_case(custom=False)
        items = commerce._editable_items(request.envelope["snapshot"])
        for item in items:
            item.update(availability="ready", checked_quantity=item["quantity"], supply_basis=basis,
                source_location="Менеджер фізично перевірив точні речі")
            if item["item_kind"] == "custom_apparel":
                item.update(title="Фіолетове худі", unit_price="1500.00")
            item["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed",
                "answer": "Менеджер перевірив: " + str(question["requested_value"])}
                for question in request.envelope["snapshot"]["questions"] if question["line_id"] == item["line_id"]]
        draft, _ = self.record(request, items=items)
        decision, _ = self.record(request, status="approved", latest=draft)
        return request, draft, decision, revision

    def preview(self, request, decision):
        value = sync.projection(request.client_id, actor=self.manager, request_id=request.pk,
            decision_id=decision.pk, now=self.now)
        self.assertTrue(value["eligible"], value)
        return value

    @staticmethod
    def counts(preview, *, total=None):
        return {pool["pool_id"]: {"total": pool["minimum_total"] if total is None else total,
            **({"size_counts": {key: size["minimum_total"] if total is None else total
                for key, size in pool["size_counts"].items()}} if pool["source"] == "catalog_variant" else {})}
            for pool in preview["pools"]}

    def sync(self, request, decision, preview, *, counts=None, operation_id=None, **changes):
        return sync.apply_stock_sync(request.pk, actor=self.manager, decision_id=decision.pk,
            expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac,
            expected_preview_digest=preview["preview_digest"], operation_id=operation_id or uuid.uuid4(),
            counts_by_pool=self.counts(preview) if counts is None else counts, now=self.now, **changes)

    def warehouse(self):
        return NativeFulfillmentStockSyncTests.warehouse(self)

    def foreign_hold(self, **kwargs):
        return NativeFulfillmentStockSyncTests.foreign_hold(self, **kwargs)


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False, ROOT_URLCONF="twocomms.urls")
class NativeAssistedStockSyncTests(AssistedStockSyncFixture, TransactionTestCase):
    def test_actual_zero_stock_known_catalog_stays_catalog_with_observation_only_price(self):
        request, _revision = self.request_case(custom=False)
        row = request.envelope["snapshot"]["lines"][0]
        spec = row["catalog_item_spec"]
        self.assertEqual((spec["product_id"], spec["color_variant_id"], spec["size"]),
            (self.product.pk, self.variant.pk, "M"))
        self.assertIs(row["physical_authority"], False)
        self.assertIs(row["price_authority"], False)
        item = commerce._editable_items(request.envelope["snapshot"])[0]
        self.assertEqual(item["item_kind"], "catalog_apparel")
        self.assertEqual(item["unit_price"], "790.00")
        self.assertFalse(Sync.objects.exists())

    def test_actual_unsupported_color_and_size_have_no_catalog_configuration_hint(self):
        for text in ("цвет purple, размер M", "цвет black, размер 3XL"):
            with self.subTest(text=text):
                source = self.canonical(text)
                revision, _token = self.revision(source)
                with transaction.atomic():
                    snapshot = commerce._capture(self.client_row, revision.pk, self.settings.pk, self.now)
                self.assertIsNone(snapshot["lines"][0]["catalog_item_spec"])
        self.assertFalse(Sync.objects.exists())

    def test_zero_accounting_requires_explicit_count_then_ordinary_catalog_quote(self):
        request, _draft, decision, _revision = self.approved_catalog_case()
        original = deepcopy(request.envelope["snapshot"])
        with self.assertRaises(CheckoutConfigurationError):
            build_assisted_quote(client=self.client_row, verified_decision=self.verified(request, decision))
        preview = self.preview(request, decision)
        self.assertTrue(preview["requires_sync"])
        self.assertEqual(preview["pools"][0]["before_total"], 0)
        self.assertFalse(self.sync(request, decision, preview, counts={})["ready"])
        with patch("management.services.instagram_bot._provider_http") as provider:
            result = self.sync(request, decision, preview)
        provider.assert_not_called()
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["status"], "stock_ready")
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(Reservation.objects.exists())
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (1, 1))
        request.refresh_from_db()
        self.assertEqual(original, request.envelope["snapshot"])
        quote = build_assisted_quote(client=self.client_row, verified_decision=self.verified(request, decision))
        self.assertEqual(quote.quoted_total, decision.approved_total)
        receipt = Sync.objects.get(pk=result["sync_id"])
        receipt.refresh_from_db()
        self.assertTrue(commerce.verify_assisted_evidence(receipt, "stock_sync"))

    def test_mixed_recount_repairs_only_catalog_then_real_full_purchase_reserves_finite_catalog(self):
        request, _draft, decision, _revision = self.approved_catalog_case(mixed=True)
        preview = self.preview(request, decision)
        self.assertEqual(len(preview["pools"]), 1)
        self.assertEqual(preview["pools"][0]["line_ids"], [decision.envelope["snapshot"]["items"][0]["line_id"]])
        result = self.sync(request, decision, preview)
        self.assertTrue(result["ready"], result)
        self.now += timedelta(seconds=1)
        buy = self.canonical("Хочу купити всі товари в кошику", at=self.now)
        self.revision(buy)
        from management.services.ig_assisted_checkout import prepare_assisted_checkout
        with patch("django.utils.timezone.now", return_value=self.now), \
                patch("management.services.instagram_bot._provider_http") as provider:
            prepared = prepare_assisted_checkout(self.client_row.pk, actor=self.manager, request_id=request.pk,
                decision_id=decision.pk, expected_request_digest=request.envelope_hmac,
                expected_decision_digest=decision.envelope_hmac, operation_id=uuid.uuid4())
        provider.assert_not_called()
        self.assertTrue(prepared["ready"], prepared)
        proposal = IgCheckoutProposal.objects.get(pk=prepared["proposal_id"])
        self.assertEqual(list(proposal.items.values_list("product_id", flat=True)), [self.product.pk, None])
        hold = proposal.inventory_reservations.get(state=Reservation.State.ACTIVE)
        self.assertEqual((hold.quantity, hold.allocation_source), (1, "catalog_variant"))
        self.assertIsNone(proposal.payment_attempt_id)
        self.assertIsNone(proposal.winner_invoice_generation_id)

    def test_checked_warehouse_recount_records_explicit_movement_without_invented_variant_stock(self):
        stock = self.warehouse()
        request, _draft, decision, _revision = self.approved_catalog_case()
        preview = self.preview(request, decision)
        self.assertEqual(preview["pools"][0]["source"], "warehouse")
        result = self.sync(request, decision, preview, counts=self.counts(preview, total=2))
        self.assertTrue(result["ready"], result)
        stock.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(stock.quantity, 2)
        self.assertEqual(self.variant.stock, 0)
        self.assertEqual(len(Sync.objects.get(pk=result["sync_id"]).envelope["snapshot"]["movement_ids"]), 1)

    def test_count_inputs_cannot_invent_increment_omit_size_or_understate_checked_pool(self):
        request, _draft, decision, _revision = self.approved_catalog_case()
        preview = self.preview(request, decision)
        key = preview["pools"][0]["pool_id"]
        for counts in ({}, {key: {"total": True, "size_counts": {}}}, {key: {"total": 1}},
                {key: {"total": 1, "size_counts": {}}}, self.counts(preview, total=0),
                {key: {"total": 1, "size_counts": {str(self.rule.pk): 1}, "increment": 1}}):
            with self.subTest(counts=counts):
                self.assertFalse(self.sync(request, decision, preview, counts=counts)["ready"])
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        self.assertFalse(Sync.objects.exists())

    def test_foreign_active_holds_are_protected_in_exact_size_and_pool_counts(self):
        _proposal, hold = self.foreign_hold()
        self.variant.stock, self.rule.stock = 0, 0
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        request, _draft, decision, _revision = self.approved_catalog_case()
        preview = self.preview(request, decision)
        pool = preview["pools"][0]
        self.assertEqual((pool["protected"], pool["minimum_total"]), (1, 2))
        self.assertEqual(pool["size_counts"][str(self.rule.pk)]["minimum_total"], 2)
        self.assertFalse(self.sync(request, decision, preview, counts=self.counts(preview, total=1))["ready"])
        hold.refresh_from_db()
        self.assertEqual(hold.state, Reservation.State.ACTIVE)
        self.assertTrue(self.sync(request, decision, preview)["ready"])

    def test_foreign_paid_warehouse_commitment_cannot_be_recounted_away(self):
        stock = self.warehouse()
        _proposal, hold = self.foreign_hold(warehouse_stock=stock, paid=True)
        stock.quantity = 0
        stock.save(update_fields=["quantity"])
        request, _draft, decision, _revision = self.approved_catalog_case()
        preview = self.preview(request, decision)
        self.assertEqual((preview["pools"][0]["protected"], preview["pools"][0]["minimum_total"]), (1, 2))
        result = self.sync(request, decision, preview, counts=self.counts(preview, total=1))
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["reason"], "physical_total_below_protected_and_requested")
        hold.refresh_from_db()
        self.assertEqual(hold.state, Reservation.State.PAID_COMMITTED)
        self.assertFalse(Sync.objects.exists())

    def test_preview_count_race_rolls_back_without_receipt(self):
        request, _draft, decision, _revision = self.approved_catalog_case()
        preview = self.preview(request, decision)
        self.variant.stock = 2
        self.variant.save(update_fields=["stock"])
        result = self.sync(request, decision, preview)
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["reason"], "preview_changed")
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.stock, 0)
        self.assertFalse(Sync.objects.exists())

    def test_catalog_price_change_after_write_rolls_back_transactional_count_and_receipt(self):
        request, _draft, decision, _revision = self.approved_catalog_case()
        preview = self.preview(request, decision)
        write = inventory._write_counts
        def changed_metadata(*args, **kwargs):
            result = write(*args, **kwargs)
            type(self.variant).objects.filter(pk=self.variant.pk).update(price_override=999)
            return result
        with patch.object(inventory, "_write_counts", side_effect=changed_metadata):
            result = self.sync(request, decision, preview)
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["reason"], "catalog_metadata_changed")
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        self.assertFalse(Sync.objects.exists())

    def test_real_source_change_after_write_rolls_back_transactional_stock(self):
        request, _draft, decision, _revision = self.approved_catalog_case()
        preview = self.preview(request, decision)
        write = inventory._write_counts
        def changed_source(*args, **kwargs):
            result = write(*args, **kwargs)
            type(request.source).objects.filter(pk=request.source_id).update(text="інша кількість 4")
            return result
        with patch.object(inventory, "_write_counts", side_effect=changed_source):
            result = self.sync(request, decision, preview)
        self.assertFalse(result["ready"], result)
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        self.assertFalse(Sync.objects.exists())

    def test_operation_replay_is_exact_native_receipt_and_never_another_recount(self):
        request, _draft, decision, _revision = self.approved_catalog_case()
        preview, operation = self.preview(request, decision), uuid.uuid4()
        first = self.sync(request, decision, preview, operation_id=operation)
        replay = self.sync(request, decision, preview, operation_id=operation)
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(first["sync_id"], replay["sync_id"])
        self.assertEqual(replay["operation_id"], str(operation))
        conflicted = self.sync(request, decision, preview, operation_id=operation, counts=self.counts(preview, total=2))
        self.assertEqual(conflicted["reason"], "operation_conflict")
        readback = sync.projection(request.client_id, actor=self.manager, request_id=request.pk, decision_id=decision.pk, now=self.now)
        for key in ("operation_id", "request_id", "request_digest", "decision_id", "decision_digest", "preview_digest", "proof_digest"):
            self.assertEqual(readback[key], replay[key])
        self.assertEqual(Sync.objects.count(), 1)

    def test_revoked_original_approver_cannot_authorize_recount_by_another_allowed_manager(self):
        request, _draft, decision, _revision = self.approved_catalog_case()
        preview = self.preview(request, decision)
        from django.contrib.auth import get_user_model
        another = get_user_model().objects.create_superuser("other-recount-manager", password="test")
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"])
        self.manager = another
        result = self.sync(request, decision, preview)
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["reason"], "actor_forbidden")
        self.assertFalse(Sync.objects.exists())
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 0)
        decision.refresh_from_db()
        self.assertTrue(commerce.verify_assisted_evidence(decision, "decision"))
