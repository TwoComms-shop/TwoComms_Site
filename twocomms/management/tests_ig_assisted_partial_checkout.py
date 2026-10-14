"""Native delivered choice -> selected quote; no mocked purchase authority."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
import uuid

from django.db import DatabaseError, transaction
from django.test import TransactionTestCase, override_settings

from management.ig_assisted_commerce_models import IgAssistedCommerceExecution as Execution
from management.models import IgCheckoutAccessToken, IgCheckoutProposal, IgCheckoutInventoryReservation
from management.services import ig_assisted_checkout as checkout
from management.services import ig_assisted_commerce as commerce
from management.tests_ig_assisted_partial_api import PartialCommerceFixture
from orders.models import Order, PaymentAttempt
from storefront.models import Product


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="off",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeSelectedAssistedCheckoutTests(PartialCommerceFixture, TransactionTestCase):
    def _selected_case(self):
        with override_settings(ROOT_URLCONF="twocomms.urls_management"):
            return self.selected_case()

    def _prepare_selection(self, case, *, operation=None, selection=None, **changes):
        request, decision = case["request"], case["decision"]
        arguments = {"actor": self.manager, "request_id": request.pk, "decision_id": decision.pk,
            "expected_request_digest": request.envelope_hmac, "expected_decision_digest": decision.envelope_hmac,
            "operation_id": operation or uuid.uuid4(), "revision_id": case["revision"].pk,
            "settings_id": self.settings.pk,
            "verified_selection": case["selection"] if selection is None else selection, **changes}
        with patch("django.utils.timezone.now", return_value=self.now), \
                patch("management.services.instagram_bot._provider_http") as provider:
            result = checkout.prepare_assisted_checkout(self.client_row.pk, **arguments)
        provider.assert_not_called()
        return result

    def test_selected_checkout_quotes_only_exact_ready_whole_lines_with_original_identity(self):
        case = self._selected_case()
        cap = case["selection"]
        before_products = Product.objects.count()
        result = self._prepare_selection(case)
        self.assertTrue(result["ready"], result)
        execution = Execution.objects.get(proposal_id=result["proposal_id"])
        body = execution.envelope["snapshot"]
        self.assertEqual(execution.scope, "partial")
        self.assertEqual(body["schema"], checkout.SELECTED_EXECUTION_SCHEMA)
        self.assertNotIn("quote_snapshot", body)
        self.assertNotIn("source_cart_binding", body)
        revision = result["proposal"].revisions.get(pk=body["checkout_revision_id"])
        self.assertEqual(body["quote_snapshot_digest"], commerce._digest(revision.snapshot))
        self.assertEqual(body["binding_digest"], commerce._digest(revision.snapshot["source_cart_binding"]))
        self.assertLessEqual(len(commerce._json(body).encode("utf-8")), 256 * 1024)
        self.assertEqual(body["selected_line_ids"], list(cap.selected_line_ids))
        self.assertEqual(body["deferred_line_ids"], list(cap.deferred_line_ids))
        self.assertTrue(body["deferred_line_ids"])
        self.assertEqual(execution.quoted_total, cap.approved_total)
        self.assertEqual(execution.quoted_custom_quantity, cap.approved_custom_quantity)
        self.assertEqual(execution.purchase_source_id, case["source"].pk)
        self.assertEqual(execution.choice_source_id, case["source"].pk)
        self.assertTrue(result["proposal"].assisted_checkout_v2)
        items = list(result["proposal"].items.order_by("position"))
        self.assertEqual([item.position for item in items], list(range(len(cap.lines))))
        self.assertEqual([row["line_id"] for row in body["rows"]], list(cap.selected_line_ids))
        self.assertTrue(set(body["selected_line_ids"]).isdisjoint(body["deferred_line_ids"]))
        self.assertEqual(Product.objects.count(), before_products)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())

    def test_raw_selected_evidence_cannot_authorize_checkout(self):
        from management.services.ig_assisted_scopes import selection_evidence
        case = self._selected_case()
        result = self._prepare_selection(case, selection=deepcopy(selection_evidence(case["selection"])))
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "assisted_selection_requires_native_producer")
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_default_full_path_cannot_turn_mixed_ready_wait_approval_into_invoice(self):
        case = self._selected_case()
        request, decision = case["request"], case["decision"]
        with patch("django.utils.timezone.now", return_value=self.now):
            result = checkout.prepare_assisted_checkout(self.client_row.pk, actor=self.manager,
                request_id=request.pk, decision_id=decision.pk,
                expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac,
                operation_id=uuid.uuid4(), revision_id=case["revision"].pk, settings_id=self.settings.pk)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "assisted_full_ready_required")
        self.assertFalse(Execution.objects.exists())

    def test_lost_prepare_response_replays_exact_same_native_token_and_proposal(self):
        case = self._selected_case()
        operation = uuid.uuid4()
        first = self._prepare_selection(case, operation=operation)
        self.assertTrue(first["ready"], first)
        replay = self._prepare_selection(case, operation=operation)
        self.assertTrue(replay["ready"], replay)
        self.assertTrue(replay["replayed"])
        self.assertIsNone(replay["token"])
        self.assertEqual((replay["proposal_id"], replay["checkout_url"]),
            (first["proposal_id"], first["checkout_url"]))
        self.assertEqual((Execution.objects.count(), IgCheckoutProposal.objects.count(),
            IgCheckoutAccessToken.objects.count()), (1, 1, 1))
        self.assertNotIn(first["token"], str(Execution.objects.get().envelope))

    def test_management_urlconf_prepares_real_public_token_entry_url(self):
        case = self._selected_case()
        with override_settings(ROOT_URLCONF="twocomms.urls_management", SITE_BASE_URL="https://twocomms.shop"):
            result = self._prepare_selection(case)
        self.assertTrue(result["ready"], result)
        self.assertTrue(result["checkout_url"].startswith("https://twocomms.shop/"))
        self.assertIn(result["token"], result["checkout_url"])

    def test_replay_recovers_from_original_historical_selection_without_current_reader(self):
        from management.services.ig_assisted_scopes import verified_selection_from_evidence
        case = self._selected_case()
        operation = uuid.uuid4()
        first = self._prepare_selection(case, operation=operation)
        self.assertTrue(first["ready"], first)
        execution = Execution.objects.get(proposal_id=first["proposal_id"])
        historical = verified_selection_from_evidence(execution.request, execution.decision,
            execution, historical=True)
        with patch("management.services.ig_assisted_scopes.intention_coverage",
                side_effect=AssertionError("Replay must not reacquire purchase capacity")):
            replay = self._prepare_selection(case, operation=operation, selection=historical)
        self.assertTrue(replay["ready"], replay)
        self.assertEqual(replay["checkout_url"], first["checkout_url"])

    def test_revoked_partial_token_cannot_be_revived_by_duplicate_choice(self):
        case = self._selected_case()
        operation = uuid.uuid4()
        first = self._prepare_selection(case, operation=operation)
        self.assertTrue(first["ready"], first)
        IgCheckoutAccessToken.objects.filter(proposal_id=first["proposal_id"]).update(revoked_at=self.now)
        replay = self._prepare_selection(case, operation=operation)
        self.assertFalse(replay["ready"])
        self.assertEqual(replay["reason"], "assisted_access_token_unavailable")
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)

    def test_consumed_choice_never_creates_second_execution_or_payment_under_another_operation(self):
        case = self._selected_case()
        first = self._prepare_selection(case)
        self.assertTrue(first["ready"], first)
        second = self._prepare_selection(case)
        self.assertFalse(second["ready"])
        self.assertEqual((Execution.objects.count(), IgCheckoutProposal.objects.count(),
            IgCheckoutAccessToken.objects.count()), (1, 1, 1))
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_historical_selection_validation_is_a_coverage_recursion_leaf(self):
        case = self._selected_case()
        prepared = self._prepare_selection(case)
        self.assertTrue(prepared["ready"], prepared)
        execution = Execution.objects.get(proposal_id=prepared["proposal_id"])
        later = self.now + timedelta(days=2)
        with patch("management.services.ig_assisted_scopes.intention_coverage",
                side_effect=AssertionError("History must not depend on current coverage")):
            result = checkout.validate_assisted_execution_history(execution, now=later)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["selection"]["selected_line_ids"], list(case["selection"].selected_line_ids))
        self.assertEqual(result["selection"]["shipping_terms"], case["selection"].shipping_terms)
        self.assertEqual(result["selection"]["shipping_scope"], "now")

    def test_referenced_native_quote_tamper_fails_in_fresh_and_historical_modes(self):
        from management.tests_ig_assisted_commerce_models import NativeAssistedCommerceModelsTests
        case = self._selected_case()
        prepared = self._prepare_selection(case)
        self.assertTrue(prepared["ready"], prepared)
        execution = Execution.objects.get(proposal_id=prepared["proposal_id"])
        quote_revision = prepared["proposal"].revisions.get(
            pk=execution.envelope["snapshot"]["checkout_revision_id"])
        changed = deepcopy(quote_revision.snapshot)
        changed["items"][0]["unit_price"] = "9999.00"
        with self.assertRaisesMessage(ValueError, "IgCheckoutRevision is append-only"):
            type(quote_revision).objects.filter(pk=quote_revision.pk).update(snapshot=changed)
        with self.assertRaisesMessage(DatabaseError, "IgCheckoutRevision is append-only"), transaction.atomic():
            NativeAssistedCommerceModelsTests.raw_update(self, quote_revision, snapshot=changed)
        quote_revision.refresh_from_db()
        self.assertNotEqual(quote_revision.snapshot, changed)

        # Both native write boundaries rejected corruption. Separately model
        # a damaged storage read of this actual produced revision; all signed
        # Execution/Decision evidence and production guards remain untouched.
        damaged = deepcopy(quote_revision)
        damaged.snapshot = changed
        manager = type(quote_revision).objects
        native_filter = manager.filter
        exact_owner = {"pk": quote_revision.pk, "proposal_id": prepared["proposal_id"],
            "revision": execution.envelope["snapshot"]["proposal_revision"]}
        damaged_reads = []

        def native_revision_read(*args, **kwargs):
            query = native_filter(*args, **kwargs)
            if not args and kwargs == exact_owner:
                damaged_reads.append(kwargs)
                query.first = lambda: damaged
            return query

        with patch.object(manager, "filter", side_effect=native_revision_read):
            for fresh in (True, False):
                checked = checkout.validate_assisted_proposal(prepared["proposal"], now=self.now, fresh=fresh)
                self.assertFalse(checked["ready"])
                self.assertEqual(checked["reason"], "assisted_execution_quote_reference_changed")
        self.assertEqual(len(damaged_reads), 2)
        intact = checkout.validate_assisted_proposal(prepared["proposal"], now=self.now, fresh=False)
        self.assertTrue(intact["ready"], intact)
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())

    def test_current_offer_uses_actual_selected_choice_and_prepared_owned_capture(self):
        case = self._selected_case()
        before = (Execution.objects.count(), IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count())
        offered = checkout.current_assisted_offer(self.client_row, revision=case["revision"], now=self.now)
        self.assertTrue(offered["ready"], offered)
        self.assertTrue(offered["can_prepare_checkout"], offered)
        self.assertEqual(offered["verified_selection"].scope_digest, case["selection"].scope_digest)
        self.assertEqual((Execution.objects.count(), IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), before)
        prepared = self._prepare_selection(case)
        self.assertTrue(prepared["ready"], prepared)
        offered = checkout.current_assisted_offer(self.client_row, revision=case["revision"], now=self.now)
        self.assertTrue(offered["ready"], offered)
        self.assertFalse(offered["can_prepare_checkout"])
        self.assertEqual(offered["source_cart_binding"], prepared["source_cart_binding"])
        self.assertEqual(offered["current_capture"], prepared["current_capture"])

    def test_wait_for_all_button_does_not_authorize_invoice_or_restock_subscription(self):
        from management.models import IgRestockSubscription
        with override_settings(ROOT_URLCONF="twocomms.urls_management"):
            case = self.selected_case(action="together")
        self.assertIsNone(case["selection"])
        offered = checkout.current_assisted_offer(self.client_row, revision=case["revision"], now=self.now)
        self.assertFalse(offered["can_prepare_checkout"])
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(IgRestockSubscription.objects.exists())

    def test_actual_button_payload_tamper_blocks_new_invoice_but_preserves_original_evidence(self):
        case = self._selected_case()
        prepared = self._prepare_selection(case)
        self.assertTrue(prepared["ready"], prepared)
        source = case["source"]
        source.quick_reply_payload = ""
        source.save(update_fields=["quick_reply_payload"])
        fresh = checkout.validate_assisted_proposal(prepared["proposal"], now=self.now, fresh=True)
        history = checkout.validate_assisted_proposal(prepared["proposal"], now=self.now, fresh=False)
        self.assertFalse(fresh["ready"])
        self.assertTrue(history["ready"], history)
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())

    def _assert_native_choice_locale(self, language, stale_profile):
        from management.services.ig_assisted_partial import selection_reply_language
        self.client_row.language = stale_profile
        self.client_row.save(update_fields=["language"])
        actual_source = self.canonical
        names = {"uk": "Ukrainian", "ru": "Russian", "en": "English"}

        def own_language_source(text, **kwargs):
            source = actual_source(f"{text}. Please reply in {names[language]}.", **kwargs)
            # Canonical inbound processing legitimately synchronizes profile
            # language. Stage a stale profile after that producer, before the
            # card reader; the source remains the actual language authority.
            self.client_row.language = stale_profile
            self.client_row.save(update_fields=["language"])
            return source

        # Only the customer's actual inbound text is varied; native capture,
        # card delivery, receipt and choice authority run normally.
        with patch.object(self, "canonical", side_effect=own_language_source):
            case = self._selected_case()
        self.assertEqual(selection_reply_language(case["selection"]), language)
        self.client_row.language = stale_profile
        self.client_row.save(update_fields=["language"])
        result = self._prepare_selection(case)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["proposal"].locale, language)
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.language, stale_profile)

    def test_native_ukrainian_choice_owns_hosted_locale_with_stale_english_profile(self):
        self._assert_native_choice_locale("uk", "en")

    def test_native_russian_choice_owns_hosted_locale_with_stale_ukrainian_profile(self):
        self._assert_native_choice_locale("ru", "uk")

    def test_native_english_choice_owns_hosted_locale_with_stale_russian_profile(self):
        self._assert_native_choice_locale("en", "ru")

    def test_settlement_admission_requires_canonical_transaction(self):
        denied = checkout.admit_assisted_settlement(None, now=self.now)
        self.assertFalse(denied["ready"])
        self.assertEqual(denied["reason"], "assisted_order_transaction_required")
        with transaction.atomic():
            denied = checkout.admit_assisted_settlement(None, now=self.now)
        self.assertFalse(denied["ready"])
        self.assertEqual(denied["reason"], "assisted_attempt_native_row_required")


from management.tests_ig_assisted_partial_sources import PartialCommerceFixture as ContinuationCommerceFixture


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="off",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeSelectedContinuationHistoryTests(ContinuationCommerceFixture, TransactionTestCase):
    def test_paid_first_order_remains_original_when_unpaid_remainder_has_new_owner_and_request(self):
        case = self.selected_case()
        with override_settings(ROOT_URLCONF="twocomms.urls"):
            prepared = self.prepare_selection(case["selection"])
        order = self.pay_prepared(prepared)
        order.refresh_from_db()
        frozen_payload = deepcopy(order.payment_payload)
        frozen_items = list(order.items.order_by("pk").values())
        remainder, _adoption, _task, _source, _revision, _token = self.resume_case(case["request"])
        self.assertEqual(remainder.intention_id, case["request"].intention_id)
        self.assertNotEqual(remainder.pk, case["request"].pk)
        with patch("management.services.ig_assisted_scopes.intention_coverage",
                side_effect=AssertionError("Paid history must not read newest scope coverage")):
            checked = checkout.validate_assisted_order(order, now=self.now)
        self.assertTrue(checked["ready"], checked)
        self.assertEqual(checked["selection"]["selected_line_ids"], list(case["selection"].selected_line_ids))
        self.assertEqual(checked["selection"]["deferred_line_ids"], list(case["selection"].deferred_line_ids))
        order.refresh_from_db()
        self.assertEqual(order.payment_payload, frozen_payload)
        self.assertEqual(list(order.items.order_by("pk").values()), frozen_items)
        self.assertEqual((Execution.objects.count(), Order.objects.count()), (1, 1))

    def test_fresh_remainder_checkout_has_bounded_signed_quote_refs_and_two_disjoint_paid_orders(self):
        first = self.selected_case()
        first_prepared = self.prepare_selection(first["selection"])
        first_order = self.pay_prepared(first_prepared)
        first_order.refresh_from_db()
        frozen_first = deepcopy(first_order.payment_payload)
        second = self.paid_remainder_case(first["request"])
        second_order = second["order"]
        executions = list(Execution.objects.order_by("created_at", "pk"))
        self.assertEqual(len(executions), 2)
        self.assertEqual(executions[0].intention_id, executions[1].intention_id)
        ids = [set(execution.envelope["snapshot"]["selected_line_ids"]) for execution in executions]
        self.assertTrue(ids[0].isdisjoint(ids[1]))
        self.assertEqual(ids[0] | ids[1], set(first["selection"].coverage["original_quantities"]))
        for execution, order in zip(executions, (first_order, second_order), strict=True):
            body = execution.envelope["snapshot"]
            self.assertEqual(body["schema"], checkout.SELECTED_EXECUTION_SCHEMA)
            self.assertNotIn("quote_snapshot", body)
            self.assertNotIn("source_cart_binding", body)
            self.assertLessEqual(len(commerce._json(body).encode("utf-8")), 256 * 1024)
            quote_revision = execution.proposal.revisions.get(pk=body["checkout_revision_id"])
            self.assertEqual(body["quote_snapshot_digest"], commerce._digest(quote_revision.snapshot))
            self.assertEqual(body["binding_digest"], commerce._digest(quote_revision.snapshot["source_cart_binding"]))
            checked = checkout.validate_assisted_order(order, now=self.now)
            self.assertTrue(checked["ready"], checked)
            self.assertEqual({row["line_id"] for row in checked["item_map"]}, set(body["selected_line_ids"]))
            self.assertEqual(order.items.count(), len(body["selected_line_ids"]))
        first_order.refresh_from_db()
        self.assertEqual(first_order.payment_payload, frozen_first)
        self.assertEqual((Order.objects.count(), PaymentAttempt.objects.count()), (2, 2))
