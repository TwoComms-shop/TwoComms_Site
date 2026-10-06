"""Hosted receipt continuity through actual webhook and existing repair paths."""
from datetime import timedelta
from decimal import Decimal
import json
from unittest.mock import patch
from unittest import skipUnless

from django.test import TestCase, TransactionTestCase, override_settings
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from management.models import (IgCheckoutAccessToken, IgCheckoutInvoiceGeneration, IgBotNotification,
    IgClient, IgDeal, IgLifecycleEvent, IgPaymentEvent, IgPaymentProjection)
from management.models import InstagramBotMessage
from management.services.ig_checkout import create_or_update_proposal
from management.services.ig_checkout_payment import (bind_verified_payment, project_attempt_settlement,
    project_verified_payment_without_order)
from orders.models import Order, PaymentAttempt
from orders.nova_poshta_checkout import build_city_choice_token, build_warehouse_choice_token
from storefront.models import Category, Product, ProductFitOption
from storefront.views.monobank import _apply_payment_attempt_status


class HostedSettlementFixture:
    def setUp(self):
        super().setUp()
        self.clock = timezone.now() - timedelta(minutes=1)
        self.client_row = IgClient.get_or_create_for_sender("hosted-settlement-tests")
        category = Category.objects.create(name="Hosted settlement", slug="hosted-settlement")
        self.product = Product.objects.create(title="Hosted shirt", slug="hosted-shirt", category=category,
            price=Decimal("900.00"), status="published")
        self.fit = ProductFitOption.objects.create(product=self.product, code="classic", label="Classic",
            is_default=True, is_active=True)
        self.dispatch = patch("management.services.ig_lifecycle.dispatch_lifecycle_event", return_value="pending")
        self.dispatch_mock = self.dispatch.start()
        self.addCleanup(self.dispatch.stop)

    def proposal(self, *, deal=None):
        return create_or_update_proposal(client=self.client_row, pay_type="online_full",
            item_specs=[{"product_id": self.product.pk, "qty": 1, "fit_option_code": self.fit.code, "size": "M"}], deal=deal)

    def invoice(self, proposal, invoice_id, *, payment_choice="online_full"):
        raw, _token = IgCheckoutAccessToken.issue(proposal=proposal)
        opened = self.client.get(reverse("ig_checkout_token_entry", kwargs={"token": raw}))
        self.assertEqual(opened.status_code, 302)
        delivery = {"full_name": "Іван Петренко", "phone": "+380501112233", "email": "buyer@example.com",
            "city": "Київ", "np_settlement_ref": "settlement-ref", "np_city_ref": "city-ref",
            "np_city_token": build_city_choice_token({"label": "Київ", "settlement_ref": "settlement-ref", "city_ref": "city-ref"}),
            "np_office": "Відділення №12", "np_warehouse_ref": "warehouse-ref",
            "np_warehouse_token": build_warehouse_choice_token({"label": "Відділення №12", "ref": "warehouse-ref",
                "kind": "branch", "city_ref": "city-ref"}), "payment_choice": payment_choice}
        with patch("storefront.views.monobank._monobank_api_request", return_value={
            "invoiceId": invoice_id, "pageUrl": f"https://pay.example/{invoice_id}"}):
            response = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}),
                delivery, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        proposal.refresh_from_db()
        return PaymentAttempt.objects.get(pk=proposal.payment_attempt_id)

    def payload(self, attempt, *, status="success", net=90000, seconds=0, **updates):
        result = {"invoiceId": attempt.monobank_invoice_id, "reference": attempt.reference, "ccy": 980,
            "status": status, "amount": int(attempt.payment_amount * 100), "finalAmount": net,
            "modifiedDate": (self.clock + timedelta(seconds=seconds)).isoformat()}
        result.update(updates)
        return result

    def paid(self, invoice_id="settlement-winner"):
        proposal = self.proposal()
        attempt = self.invoice(proposal, invoice_id)
        order, created = _apply_payment_attempt_status(attempt, "success", payload=self.payload(attempt), source="provider_pull")
        self.assertTrue(created)
        self.assertIsNotNone(order)
        proposal.refresh_from_db()
        attempt.refresh_from_db()
        return proposal, attempt, order

    def webhook(self, attempt, payload, *, transport_time="first delivery"):
        with patch("storefront.views.monobank._webhook_signature_ok", return_value=True), patch(
            "storefront.views.monobank._monobank_api_request", return_value=payload) as provider:
            response = self.client.post(reverse("monobank_webhook"), data=json.dumps({
                "invoiceId": attempt.monobank_invoice_id, "status": payload["status"], "occurred_at": transport_time}),
                content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(provider.call_count, 1)
        return response


@override_settings(IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class HostedSettlementTests(HostedSettlementFixture, TestCase):
    def test_webhook_full_reversal_after_conversion_preserves_order_and_blocks_fulfillment(self):
        proposal, attempt, order = self.paid()
        self.webhook(attempt, self.payload(attempt, status="reversed", net=0, seconds=10))
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        order.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual((projection.truth, projection.net_paid_amount), (IgDeal.PaymentTruth.REVERSED, Decimal("0.00")))
        self.assertEqual((order.payment_status, order.status), ("unpaid", "cancelled"))
        self.assertEqual(attempt.order_id, order.pk)
        self.assertEqual(attempt.status, PaymentAttempt.Status.CONVERTED)
        self.assertEqual(Order.objects.count(), 1)

    def test_partial_refund_uses_gross_and_net_without_new_order_or_receipt_send(self):
        proposal, attempt, order = self.paid()
        initial_events = IgLifecycleEvent.objects.filter(kind=IgLifecycleEvent.Kind.PAYMENT_VERIFIED).count()
        self.webhook(attempt, self.payload(attempt, net=65000, seconds=10))
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        self.assertEqual((projection.truth, projection.gross_amount, projection.refunded_amount),
            (IgDeal.PaymentTruth.PARTIALLY_REFUNDED, Decimal("900.00"), Decimal("250.00")))
        self.assertTrue(projection.needs_reconciliation)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(IgLifecycleEvent.objects.filter(kind=IgLifecycleEvent.Kind.PAYMENT_VERIFIED).count(), initial_events)
        self.assertTrue(IgBotNotification.objects.filter(event_type="hosted_settlement_review").exists())

    def test_prepayment_settlement_uses_requested_200_not_order_total_900(self):
        message = InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
            role="user", text="Чи можна 200 грн передоплати, а решту післяплатою?")
        proposal = create_or_update_proposal(client=self.client_row, pay_type="prepayment",
            requested_payment_amount=Decimal("200.00"), evidence={"message_ids": [message.pk]},
            item_specs=[{"product_id": self.product.pk, "qty": 1, "fit_option_code": self.fit.code, "size": "M"}])
        attempt = self.invoice(proposal, "settlement-prepay-200", payment_choice="prepay_200_cod")
        order, _created = _apply_payment_attempt_status(attempt, "success", payload=self.payload(attempt, net=20000), source="provider_pull")
        self.assertEqual(order.payment_status, "prepaid")
        self.assertEqual(order.total_sum, Decimal("900.00"))
        attempt.refresh_from_db()
        result = project_attempt_settlement(attempt.pk, status="success",
            payload=self.payload(attempt, net=10000, seconds=10), source="provider_pull")
        self.assertTrue(result["applied"], result)
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        self.assertEqual((projection.gross_amount, projection.refunded_amount), (Decimal("200.00"), Decimal("100.00")))

    def test_duplicate_webhook_other_transport_timestamp_and_reconcile_share_one_receipt(self):
        proposal, attempt, order = self.paid()
        payload = self.payload(attempt, status="reversed", net=0, seconds=10)
        self.webhook(attempt, payload, transport_time="transport A")
        first = IgPaymentProjection.objects.get(deal=proposal.deal).last_event_id
        count = IgPaymentEvent.objects.count()
        self.webhook(attempt, payload, transport_time="transport B")
        result = project_attempt_settlement(attempt.pk, status="reversed", payload=payload, source="ig_reconcile")
        self.assertEqual(result["reason"], "settlement_receipt_replay")
        self.assertEqual(IgPaymentEvent.objects.count(), count)
        self.assertEqual(IgPaymentProjection.objects.get(deal=proposal.deal).last_event_id, first)
        self.assertEqual(Order.objects.count(), 1)

    def test_same_initial_success_replay_does_not_append_second_payment_receipt(self):
        proposal, attempt, order = self.paid()
        count = IgPaymentEvent.objects.count()
        self.webhook(attempt, self.payload(attempt), transport_time="later retry")
        self.assertEqual(IgPaymentEvent.objects.count(), count)
        self.assertEqual(Order.objects.count(), 1)

    def test_old_success_and_old_refund_never_replace_newer_reversal(self):
        proposal, attempt, order = self.paid()
        project_attempt_settlement(attempt.pk, status="reversed", payload=self.payload(attempt, status="reversed", net=0, seconds=20), source="provider_pull")
        head = IgPaymentProjection.objects.get(deal=proposal.deal).last_event_id
        for status, net, seconds in (("success", 90000, 0), ("success", 70000, 10)):
            result = project_attempt_settlement(attempt.pk, status=status, payload=self.payload(attempt, status=status, net=net, seconds=seconds), source="provider_pull")
            self.assertEqual(result["reason"], "settlement_stale_provider_version")
        self.assertEqual(IgPaymentProjection.objects.get(deal=proposal.deal).last_event_id, head)

    def test_same_provider_version_with_conflicting_money_is_explicit_review(self):
        proposal, attempt, order = self.paid()
        result = project_attempt_settlement(attempt.pk, status="success", payload=self.payload(attempt, net=70000), source="provider_pull")
        self.assertEqual(result["reason"], "settlement_provider_version_conflict")
        self.assertEqual(IgPaymentProjection.objects.get(deal=proposal.deal).net_paid_amount, Decimal("900.00"))

    def test_verified_refund_fact_survives_pause_takeover_and_closed_response_window(self):
        proposal, attempt, order = self.paid("settlement-no-send")
        from management.services.ig_lifecycle import _response_window_open
        expired_user_time = timezone.now() - timedelta(days=2)
        user_source = InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
            role=InstagramBotMessage.Role.USER, status=InstagramBotMessage.Status.DONE, source="webhook",
            mid="settlement-closed-window-user", text="Дякую", provider_created_at=expired_user_time)
        IgClient.objects.filter(pk=self.client_row.pk).update(bot_paused=True, manager_takeover=True,
            last_user_message_at=user_source.provider_created_at, last_message_at=timezone.now())
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.meta_window_anchor, user_source.provider_created_at)
        self.assertFalse(_response_window_open(self.client_row, timezone.now()))
        before = IgLifecycleEvent.objects.count()
        self.webhook(attempt, self.payload(attempt, net=0, seconds=10))
        self.assertEqual(IgPaymentProjection.objects.get(deal=proposal.deal).truth, IgDeal.PaymentTruth.REFUNDED)
        self.assertEqual(IgLifecycleEvent.objects.count(), before)
        self.dispatch_mock.assert_not_called()

    def test_old_receipt_without_authoritative_provider_version_requires_review(self):
        proposal = self.proposal()
        attempt = self.invoice(proposal, "settlement-unknown-version")
        _apply_payment_attempt_status(attempt, "success", payload=self.payload(attempt, modifiedDate=""), source="provider_pull")
        attempt.refresh_from_db()
        result = project_attempt_settlement(attempt.pk, status="reversed",
            payload=self.payload(attempt, status="reversed", net=0, seconds=10), source="provider_pull")
        self.assertEqual(result["reason"], "settlement_prior_version_unknown")
        self.assertEqual(IgPaymentProjection.objects.get(deal=proposal.deal).truth, IgDeal.PaymentTruth.CONFIRMED)
        self.assertTrue(IgBotNotification.objects.filter(event_type="hosted_settlement_review").exists())

    def test_missing_version_and_identity_currency_amount_mismatches_do_not_invent_truth(self):
        proposal, attempt, order = self.paid()
        cases = [({"invoiceId": "foreign"}, "settlement_identity_currency_mismatch"),
            ({"reference": "foreign"}, "settlement_identity_currency_mismatch"),
            ({"ccy": 840}, "settlement_identity_currency_mismatch"),
            ({"modifiedDate": ""}, "settlement_provider_version_unknown"),
            ({"amount": 80000}, "settlement_amount_mismatch"),
            ({"finalAmount": None}, "settlement_amount_missing_or_malformed"),
            ({"paidAmount": 70000}, "settlement_amount_conflict")]
        for changed, reason in cases:
            with self.subTest(reason=reason):
                result = project_attempt_settlement(attempt.pk, status="success", payload=self.payload(attempt, net=70000, seconds=10, **changed), source="provider_pull")
                self.assertEqual(result["reason"], reason)
                self.assertFalse(result["applied"])
        self.assertEqual(IgPaymentProjection.objects.get(deal=proposal.deal).net_paid_amount, Decimal("900.00"))

    def test_database_rollback_keeps_receipt_projection_order_and_review_unchanged(self):
        proposal, attempt, order = self.paid()
        count = IgPaymentEvent.objects.count()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                project_attempt_settlement(attempt.pk, status="reversed", payload=self.payload(attempt, status="reversed", net=0, seconds=10), source="provider_pull")
                raise RuntimeError("rollback payment observation")
        self.assertEqual(IgPaymentEvent.objects.count(), count)
        self.assertEqual(IgPaymentProjection.objects.get(deal=proposal.deal).truth, IgDeal.PaymentTruth.CONFIRMED)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "paid")
        self.assertFalse(IgBotNotification.objects.filter(event_type="payment_reversed_review").exists())

    def test_commit_crash_is_repaired_by_existing_durable_projection_backstop(self):
        from management.services.bot_payments import reconcile_payment_projections
        proposal, attempt, order = self.paid()
        with patch("management.services.bot_payments.reconcile_payment_projection", side_effect=RuntimeError("worker lost")), self.captureOnCommitCallbacks(execute=True):
            project_attempt_settlement(attempt.pk, status="reversed", payload=self.payload(attempt, status="reversed", net=0, seconds=10), source="provider_pull")
        self.assertTrue(IgPaymentProjection.objects.get(deal=proposal.deal).needs_reconciliation)
        self.assertEqual(reconcile_payment_projections(limit=10), 1)
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        self.assertFalse(projection.needs_reconciliation)
        self.assertEqual(projection.truth, IgDeal.PaymentTruth.REVERSED)
        self.assertEqual(IgBotNotification.objects.filter(event_type="payment_reversed_review").count(), 1)

    def test_losing_invoice_receipt_cannot_replace_winner_payment_provenance(self):
        from management.services.ig_checkout_generation import apply_generation_provider_status
        proposal = self.proposal()
        first = self.invoice(proposal, "settlement-loser")
        apply_generation_provider_status(first.pk, "failure", payload=self.payload(first, status="failure"), source="provider_pull")
        proposal.refresh_from_db()
        second = self.invoice(proposal, "settlement-winner-new")
        winner, _created = _apply_payment_attempt_status(second, "success", payload=self.payload(second, seconds=5), source="provider_pull")
        head = IgPaymentProjection.objects.get(deal=proposal.deal).last_event_id
        order, created = _apply_payment_attempt_status(first, "success", payload=self.payload(first, seconds=10), source="provider_pull")
        self.assertIsNone(order)
        self.assertFalse(created)
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        self.assertEqual(projection.last_event_id, head)
        self.assertEqual(projection.last_event.invoice_id, second.monobank_invoice_id)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(winner.pk, Order.objects.get().pk)

    def test_pending_callback_does_not_reopen_converted_attempt(self):
        proposal, attempt, order = self.paid()
        _apply_payment_attempt_status(attempt, "processing", payload={}, source="provider_pull")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.CONVERTED)

    def test_repair_rereads_newer_already_repaired_head_before_mirror(self):
        from management.services import bot_payments, ig_checkout_payment
        proposal, attempt, order = self.paid("settlement-repair-current-head")
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        original_lock = ig_checkout_payment._lock_attempt_proposal_graph
        advanced = False

        def newer_completed_before_lock(attempt_id):
            nonlocal advanced
            if not advanced:
                advanced = True
                with self.captureOnCommitCallbacks(execute=True):
                    project_attempt_settlement(attempt.pk, status="reversed",
                        payload=self.payload(attempt, status="reversed", net=0, seconds=10), source="provider_pull")
                current = IgPaymentProjection.objects.get(pk=projection.pk)
                self.assertFalse(current.needs_reconciliation)
                self.assertEqual(IgDeal.objects.get(pk=proposal.deal_id).payment_truth, IgDeal.PaymentTruth.REVERSED)
            return original_lock(attempt_id)

        with patch.object(ig_checkout_payment, "_lock_attempt_proposal_graph", side_effect=newer_completed_before_lock):
            bot_payments.reconcile_payment_projection(projection.pk)
        projection.refresh_from_db()
        self.assertFalse(projection.needs_reconciliation)
        self.assertEqual(IgDeal.objects.get(pk=proposal.deal_id).payment_truth, IgDeal.PaymentTruth.REVERSED)

    def test_contradictory_receipt_aliases_and_non_ascii_money_abstain(self):
        proposal, attempt, order = self.paid("settlement-alias-proof")
        head = IgPaymentProjection.objects.get(deal=proposal.deal).last_event_id
        cases = [{"invoice_id": "foreign"}, {"merchantPaymInfo": {"reference": "foreign"}},
            {"currencyCode": 840}, {"statusCode": "reversed"},
            {"amount": "²"}, {"finalAmount": "٧٠٠٠٠"}, {"finalAmount": "７００００"},
            {"finalAmount": "070000"}, {"finalAmount": "+70000"}, {"finalAmount": "70000.0"}]
        for fields in cases:
            with self.subTest(fields=fields):
                result = project_attempt_settlement(attempt.pk, status="success",
                    payload=self.payload(attempt, net=70000, seconds=10, **fields), source="provider_pull")
                self.assertFalse(result["applied"])
                self.assertIn(result["reason"], {"settlement_identity_currency_mismatch", "settlement_amount_missing_or_malformed"})
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        self.assertEqual((projection.last_event_id, projection.net_paid_amount), (head, Decimal("900.00")))

    def test_webhook_rejects_non_ascii_minor_and_conflicting_alias_without_500(self):
        proposal, attempt, order = self.paid("settlement-webhook-finite-proof")
        head = IgPaymentProjection.objects.get(deal=proposal.deal).last_event_id
        for fields in ({"finalAmount": "²"}, {"currencyCode": 840}, {"statusCode": "reversed"}):
            with self.subTest(fields=fields):
                self.webhook(attempt, self.payload(attempt, net=70000, seconds=10, **fields))
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        self.assertEqual((projection.last_event_id, projection.truth), (head, IgDeal.PaymentTruth.CONFIRMED))
        self.assertEqual(IgPaymentEvent.objects.count(), 1)
        self.assertTrue(IgBotNotification.objects.filter(event_type="hosted_settlement_review").exists())

    def test_initial_receipts_use_same_alias_and_ascii_guards(self):
        from management.services.ig_checkout_generation import _exact_provider_paid_amount
        from storefront.views.monobank import _resolve_attempt_invoice_status
        proposal = self.proposal()
        attempt = self.invoice(proposal, "settlement-initial-alias-proof")
        for fields in [{"currencyCode": 840}, {"invoice_id": "foreign"},
                {"merchantPaymInfo": {"reference": "foreign"}}, {"statusCode": "reversed"}, {"finalAmount": "²"}]:
            with self.subTest(fields=fields):
                payload = self.payload(attempt, **fields)
                with patch("storefront.views.monobank._monobank_api_request", return_value=payload):
                    status, pulled = _resolve_attempt_invoice_status(attempt, attempt.monobank_invoice_id)
                self.assertEqual(status, "processing")
                self.assertTrue(pulled.get("_twc_reconciliation_reason"))
                self.assertEqual({key: value for key, value in pulled.items()
                    if key != "_twc_reconciliation_reason"}, payload)
                self.assertEqual(_apply_payment_attempt_status(attempt, status, payload=pulled, source="provider_pull"), (None, False))
        self.assertEqual(_exact_provider_paid_amount(attempt, {"amount": "²"}), (False, "amount_malformed"))
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(IgPaymentEvent.objects.count(), 0)

    def test_two_orders_same_client_keep_exact_invoice_settlement_scope(self):
        first_proposal, first_attempt, first_order = self.paid("settlement-order-one")
        new_deal = IgDeal.objects.create(client=self.client_row, amount=Decimal("900.00"), currency="UAH")
        second_proposal = self.proposal(deal=new_deal)
        second_attempt = self.invoice(second_proposal, "settlement-order-two")
        second_order, _created = _apply_payment_attempt_status(second_attempt, "success",
            payload=self.payload(second_attempt, seconds=5), source="provider_pull")
        second_head = IgPaymentProjection.objects.get(deal=new_deal).last_event_id
        self.webhook(first_attempt, self.payload(first_attempt, status="reversed", net=0, seconds=10))
        self.assertEqual(IgPaymentProjection.objects.get(deal=first_proposal.deal).truth, IgDeal.PaymentTruth.REVERSED)
        second_projection = IgPaymentProjection.objects.get(deal=new_deal)
        self.assertEqual((second_projection.truth, second_projection.last_event_id), (IgDeal.PaymentTruth.CONFIRMED, second_head))
        second_order.refresh_from_db()
        self.assertEqual(second_order.payment_status, "paid")
        self.assertEqual(Order.objects.count(), 2)


@override_settings(IG_ASSISTED_CHECKOUT_V2="off", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0)
class LegacyHostedSettlementTests(HostedSettlementFixture, TestCase):
    def test_legacy_hosted_reversal_flows_through_webhook_after_conversion(self):
        proposal, attempt, order = self.paid("legacy-hosted-settlement")
        self.webhook(attempt, self.payload(attempt, status="reversed", net=0, seconds=10))
        self.assertEqual(IgPaymentProjection.objects.get(deal=proposal.deal).truth, IgDeal.PaymentTruth.REVERSED)
        self.assertEqual(Order.objects.count(), 1)

    def test_direct_old_bind_and_positive_projection_cannot_erase_refund(self):
        proposal, attempt, order = self.paid("legacy-monotonic-settlement")
        project_attempt_settlement(attempt.pk, status="success", payload=self.payload(attempt, net=70000, seconds=10), source="provider_pull")
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        head = projection.last_event_id
        bind_verified_payment(attempt.pk, order)
        project_verified_payment_without_order(attempt=attempt, deal=proposal.deal, proposal=proposal,
            verified_at=self.clock + timedelta(seconds=30))
        projection.refresh_from_db()
        self.assertEqual((projection.last_event_id, projection.net_paid_amount), (head, Decimal("700.00")))


@skipUnless(connection.vendor == "mysql", "Requires root-owned disposable InnoDB")
@override_settings(IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativeHostedSettlementTests(HostedSettlementFixture, TransactionTestCase):
    def workers(self, callbacks):
        import threading
        from queue import Queue
        from django.db import close_old_connections, connections
        output = Queue()

        def worker(callback):
            close_old_connections()
            try:
                output.put((True, callback()))
            except Exception as exc:
                output.put((False, exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(callback,), daemon=True) for callback in callbacks]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive(), "bounded settlement race did not complete")
        values = [output.get_nowait() for thread in threads]
        for success, value in values:
            self.assertTrue(success, repr(value))
        return [value for success, value in values]

    def test_webhook_and_reconcile_same_refund_claim_one_receipt(self):
        import threading
        proposal, attempt, order = self.paid("native-settlement-race")
        barrier = threading.Barrier(2)
        payload = self.payload(attempt, status="reversed", net=0, seconds=10)

        def webhook():
            barrier.wait(5)
            return self.webhook(attempt, payload)

        def reconcile():
            barrier.wait(5)
            return project_attempt_settlement(attempt.pk, status="reversed", payload=payload, source="ig_reconcile")

        self.workers([webhook, reconcile])
        self.assertEqual(IgPaymentEvent.objects.filter(provider_status="reversed", invoice_id=attempt.monobank_invoice_id).count(), 1)
        self.assertEqual(IgPaymentProjection.objects.get(deal=proposal.deal).truth, IgDeal.PaymentTruth.REVERSED)
        self.assertEqual(Order.objects.count(), 1)

    def test_repair_waits_on_hosted_graph_before_projection_no_joined_lock_cycle(self):
        import threading
        from django.db import connections
        from management.services.bot_payments import reconcile_payment_projection
        from management.services.ig_checkout_payment import _lock_attempt_proposal_graph
        proposal, attempt, order = self.paid("native-settlement-repair-prefix")
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        held, repair_arrived = threading.Event(), threading.Event()
        observed = []

        def settlement():
            with transaction.atomic():
                _lock_attempt_proposal_graph(attempt.pk)
                held.set()
                self.assertTrue(repair_arrived.wait(5))
                return project_attempt_settlement(attempt.pk, status="reversed",
                    payload=self.payload(attempt, status="reversed", net=0, seconds=10), source="provider_pull")

        def repair():
            self.assertTrue(held.wait(5))

            def observe(execute, sql, params, many, context):
                if "FOR UPDATE" in sql.upper():
                    observed.append(sql)
                    repair_arrived.set()
                return execute(sql, params, many, context)

            with connections["default"].execute_wrapper(observe):
                return reconcile_payment_projection(projection.pk)

        self.workers([settlement, repair])
        self.assertTrue(observed)
        self.assertIn(IgDeal._meta.db_table, observed[0])
        self.assertNotIn(" JOIN ", observed[0].upper())
        self.assertEqual(IgPaymentProjection.objects.get(pk=projection.pk).truth, IgDeal.PaymentTruth.REVERSED)

    def test_newer_completed_repair_precedes_old_worker_mirror(self):
        import threading
        from management.services import bot_payments, ig_checkout_payment
        proposal, attempt, order = self.paid("native-settlement-mirror-current")
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        old_ready, newer_done = threading.Event(), threading.Event()
        old_ident, seen = [], []
        original_lock = ig_checkout_payment._lock_attempt_proposal_graph
        original_sync = bot_payments._sync_legacy_payment_mirror

        def lock(attempt_id):
            if old_ident and threading.get_ident() == old_ident[0]:
                old_ready.set()
                self.assertTrue(newer_done.wait(5))
            return original_lock(attempt_id)

        def mirror(current):
            if old_ident and threading.get_ident() == old_ident[0]:
                self.assertTrue(newer_done.is_set())
                seen.append(current.truth)
            return original_sync(current)

        def old_repair():
            old_ident.append(threading.get_ident())
            return bot_payments.reconcile_payment_projection(projection.pk)

        def newer_settlement():
            self.assertTrue(old_ready.wait(5))
            result = project_attempt_settlement(attempt.pk, status="reversed",
                payload=self.payload(attempt, status="reversed", net=0, seconds=10), source="provider_pull")
            current = IgPaymentProjection.objects.get(pk=projection.pk)
            self.assertFalse(current.needs_reconciliation)
            self.assertEqual(IgDeal.objects.get(pk=proposal.deal_id).payment_truth, IgDeal.PaymentTruth.REVERSED)
            newer_done.set()
            return result

        with patch.object(ig_checkout_payment, "_lock_attempt_proposal_graph", side_effect=lock), patch.object(
                bot_payments, "_sync_legacy_payment_mirror", side_effect=mirror):
            self.workers([old_repair, newer_settlement])
        self.assertEqual(seen, [IgDeal.PaymentTruth.REVERSED])
        self.assertEqual(IgDeal.objects.get(pk=proposal.deal_id).payment_truth, IgDeal.PaymentTruth.REVERSED)
        self.assertFalse(IgPaymentProjection.objects.get(pk=projection.pk).needs_reconciliation)

    def test_mirror_holds_current_receipt_lock_until_acknowledgement(self):
        import threading
        from django.db import connections
        from management.services import bot_payments
        proposal, attempt, order = self.paid("native-settlement-mirror-lock")
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        mirror_ready, newer_arrived, newer_done = threading.Event(), threading.Event(), threading.Event()
        old_ident = []
        original = bot_payments._sync_legacy_payment_mirror

        def mirror(current):
            if old_ident and threading.get_ident() == old_ident[0]:
                self.assertTrue(connections["default"].in_atomic_block, "hosted mirror escaped financial transaction")
                mirror_ready.set()
                self.assertTrue(newer_arrived.wait(5))
                self.assertFalse(newer_done.wait(.2), "newer repair escaped the current receipt lock")
            return original(current)

        def old_repair():
            old_ident.append(threading.get_ident())
            return bot_payments.reconcile_payment_projection(projection.pk)

        def newer_settlement():
            self.assertTrue(mirror_ready.wait(5))
            def observe(execute, sql, params, many, context):
                if "FOR UPDATE" in sql.upper():
                    newer_arrived.set()
                return execute(sql, params, many, context)
            with connections["default"].execute_wrapper(observe):
                result = project_attempt_settlement(attempt.pk, status="reversed",
                    payload=self.payload(attempt, status="reversed", net=0, seconds=10), source="provider_pull")
            newer_done.set()
            return result

        with patch.object(bot_payments, "_sync_legacy_payment_mirror", side_effect=mirror):
            self.workers([old_repair, newer_settlement])
        self.assertTrue(newer_done.is_set())
        self.assertEqual(IgDeal.objects.get(pk=proposal.deal_id).payment_truth, IgDeal.PaymentTruth.REVERSED)
        self.assertFalse(IgPaymentProjection.objects.get(pk=projection.pk).needs_reconciliation)

    def _initial_conversion_race(self, *, status, net):
        import threading
        from django.db import connections
        from management.services.ig_checkout_generation import _lock_generation_graph
        from storefront.views.monobank import _resolve_attempt_invoice_status
        proposal = self.proposal()
        attempt = self.invoice(proposal, "native-settlement-conversion-" + status)
        held, waiting = threading.Event(), threading.Event()
        payload = self.payload(attempt, status=status, net=net, seconds=10)

        def initial_conversion():
            with transaction.atomic():
                _lock_generation_graph(attempt.pk)
                order, created = _apply_payment_attempt_status(attempt, "success", payload=self.payload(attempt), source="provider_pull")
                self.assertTrue(created)
                held.set()
                self.assertTrue(waiting.wait(5))
                return order.pk

        def newer_observation():
            self.assertTrue(held.wait(5))
            stale = PaymentAttempt.objects.get(pk=attempt.pk)
            self.assertIsNone(stale.order_id)
            # A created invoice has no verified payment yet: the nullable
            # amount stays NULL until materialize_payment_attempt commits.
            self.assertIsNone(stale.paid_amount)
            with patch("storefront.views.monobank._monobank_api_request", return_value=payload):
                pulled_status, pulled = _resolve_attempt_invoice_status(stale, stale.monobank_invoice_id)
            self.assertEqual(pulled_status, "processing" if status == "success" else status)
            self.assertEqual({key: value for key, value in pulled.items()
                if key != "_twc_reconciliation_reason"}, payload)
            def observe(execute, sql, params, many, context):
                if "FOR UPDATE" in sql.upper():
                    waiting.set()
                return execute(sql, params, many, context)
            with connections["default"].execute_wrapper(observe):
                return _apply_payment_attempt_status(stale, pulled_status, payload=pulled, source="provider_pull")

        self.workers([initial_conversion, newer_observation])
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        self.assertEqual(projection.net_paid_amount, Decimal(net) / 100)
        self.assertEqual(projection.truth, IgDeal.PaymentTruth.REVERSED if status == "reversed" else IgDeal.PaymentTruth.PARTIALLY_REFUNDED)
        self.assertFalse(projection.needs_reconciliation)
        self.assertEqual(IgPaymentEvent.objects.filter(invoice_id=attempt.monobank_invoice_id).count(), 2)
        self.assertEqual(Order.objects.count(), 1)

    def test_initial_conversion_commits_before_waiting_partial_refund_dispatch(self):
        self._initial_conversion_race(status="success", net=65000)

    def test_initial_conversion_commits_before_waiting_reversal_dispatch(self):
        self._initial_conversion_race(status="reversed", net=0)
