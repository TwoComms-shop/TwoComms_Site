"""Current source positions stay separate from actual, owned order history."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path

from management import tests_ig_selection_corrections as fixtures
from management import tests_ig_commerce_line_operations as line_fixtures
from management.bot_access import META_REVIEWER_GROUP_NAME
from management.bot_commerce_scope_views import bot_client_commerce_scope_api
from management.models import IgClient, IgOrderAttribution
from management.services import ig_commerce_scope_read_model as reader

urlpatterns = [path("bot/api/clients/<int:client_id>/commerce-scope/", bot_client_commerce_scope_api)]


class CommerceScopeIdentityTests(SimpleTestCase):
    def test_invalid_identifiers_fail_before_orm(self):
        for client_id, selected in ((True, None), (0, None), (2**63, None), (1, True), (1, 2**63), (1, -1)):
            with self.subTest(client_id=client_id, selected=selected):
                result = reader.capture_commerce_scope(client_id, selected_order_id=selected)
                self.assertEqual(result["reason"], "commerce_scope_identity_invalid")
                self.assertEqual(result["diagnostics"]["read_queries"], 0)


@override_settings(GOOGLE_INDEXING_ENABLED=False, ROOT_URLCONF=__name__, SECURE_SSL_REDIRECT=False)
class CommerceScopeReadTests(TestCase):
    setUp = fixtures.SizeCorrectionTests.setUp
    message = fixtures.SizeCorrectionTests.message

    def order(self, *, client=None, number="", status="done", payment="paid"):
        from orders.models import Order, OrderItem
        order = Order.objects.create(full_name="Do not expose this name", phone="+380501110000", city="Private city",
            np_office="Private office", order_number=number, status=status, payment_status=payment, total_sum=Decimal("790"))
        OrderItem.objects.create(order=order, title="Реальна попередня футболка", size="S", qty=1,
            unit_price=Decimal("790"), line_total=Decimal("790"), is_custom=True, color_name_custom="Чорний")
        IgOrderAttribution.objects.create(order=order, client=client or self.row, creation_mode="manager_review", payment_source="unknown")
        return order

    def test_partial_source_size_has_no_sku_no_order_and_no_fabricated_purchase(self):
        result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual(result["status"], "captured", result)
        self.assertEqual(result["current"]["status"], "captured", result)
        line = result["current"]["lines"][0]
        self.assertEqual(line["fields"]["size"]["value"], "M")
        self.assertEqual(line["fields"]["size"]["status"], "confirmed")
        self.assertEqual(line["fields"]["product_id"]["status"], "unknown")
        self.assertIsNone(result["current"]["scope"]["order_id"])
        self.assertEqual(result["history"]["orders"], [])
        self.assertEqual(result["history"]["completed_order_count"], 0)
        self.assertIsNone(result["history"]["ltv"]["value"])

    def test_previous_real_order_items_and_recorded_paid_do_not_become_current_payment(self):
        order = self.order()
        result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual(result["status"], "captured", result)
        self.assertIsNone(result["current"]["scope"]["order_id"])
        self.assertNotIn("payment", result["current"])
        old = result["history"]["orders"][0]
        self.assertEqual((old["id"], old["kind"]), (order.pk, "physical_order"))
        self.assertEqual(old["items"][0]["title"], "Реальна попередня футболка")
        self.assertEqual(old["items"][0]["size"], "S")
        self.assertEqual(old["payment"]["recorded_status"], "paid")
        self.assertNotIn("full_name", old); self.assertNotIn("phone", old)
        self.assertEqual(result["history"]["completed_order_count"], 1)
        selected = reader.capture_commerce_scope(self.row.pk, selected_order_id=order.pk, now=self.now)
        self.assertEqual(selected["selection"], {"mode": "order", "order_id": order.pk})
        self.assertEqual(selected["current"]["scope"], result["current"]["scope"])
        self.assertEqual([line["fields"] for line in selected["current"]["lines"]], [line["fields"] for line in result["current"]["lines"]])
        self.assertFalse(selected["current"]["editable"])
        self.assertIsNone(selected["current"]["editable_scope"])
        self.assertFalse(any(line["editable"] for line in selected["current"]["lines"]))

    def test_explicit_unlink_keeps_history_evidence_separate_without_reviving_owned_order(self):
        from management.services.ig_order_assignments import link_order_to_client, unlink_order_from_client
        order = self.order()
        assignment = link_order_to_client(order, client=self.row, actor=self.actor)
        unlink_order_from_client(order, client=self.row, actor=self.actor, expected_version=assignment.version,
            reason_code="incorrect_binding_corrected", reason="Verified incorrect client binding")
        result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual(result["status"], "captured", result)
        self.assertEqual(result["history"]["orders"], [])
        self.assertIsNone(result["history"]["completed_order_count"])
        self.assertEqual(result["history"]["coverage"], "uncertain")
        unresolved = result["history"]["unresolved"][0]
        self.assertEqual(unresolved["order_id"], order.pk)
        self.assertFalse(unresolved["editable"])
        self.assertNotIn("items", unresolved)

    def test_order_pointer_of_another_client_cannot_select_foreign_history(self):
        other = IgClient.objects.create(igsid="scope-foreign-client")
        order = self.order(client=other)
        result = reader.capture_commerce_scope(self.row.pk, selected_order_id=order.pk, now=self.now)
        self.assertEqual((result["status"], result["reason"]), ("conflict", "selected_order_not_captured"))
        self.assertEqual(result["history"], {})

    def test_exact_current_order_owner_is_independent_of_twenty_order_history_window(self):
        pinned = self.order(number="SCOPE-OLD-PINNED")
        for index in range(21): self.order(number=f"SCOPE-NEW-{index + 1}")
        history = reader._history(self.row.pk)
        self.assertNotIn(pinned.pk, [order["id"] for order in history["orders"]])
        binding = reader._order_binding(self.row.pk, pinned.pk)
        self.assertEqual(binding["status"], "confirmed")
        self.assertEqual(binding["order_id"], pinned.pk)
        self.assertEqual(reader._order_binding(self.row.pk, pinned.pk + 100000)["status"], "unknown")

    def test_owned_current_episode_without_source_session_keeps_real_history_and_no_editable_legacy_size(self):
        from management.models import IgCommercialEpisode
        other = IgClient.objects.create(igsid="scope-no-source-session", current_size="L")
        order = self.order(client=other, number="SCOPE-NO-SELECTION")
        episode = IgCommercialEpisode.objects.create(client=other, sequence=1, materialization_key="scope-no-source-episode",
            intended_order=order)
        IgClient.objects.filter(pk=other.pk).update(current_commercial_episode=episode)
        result = reader.capture_commerce_scope(other.pk, now=self.now)
        self.assertEqual(result["status"], "captured", result)
        self.assertEqual(result["current"]["status"], "unavailable")
        self.assertEqual(result["current"]["scope"]["episode_id"], episode.pk)
        self.assertEqual(result["current"]["scope"]["order_id"], order.pk)
        self.assertEqual(result["current"]["lines"], [])
        self.assertFalse(result["current"]["editable"])
        self.assertEqual(result["history"]["orders"][0]["id"], order.pk)

    def test_more_than_twenty_real_orders_has_bounded_coverage_and_no_global_ordinal_or_total(self):
        for index in range(21): self.order(number=f"SCOPE-{index + 1}")
        result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual(result["status"], "captured", result)
        self.assertEqual(len(result["history"]["orders"]), 20)
        self.assertEqual(result["history"]["coverage"], "bounded")
        self.assertIsNone(result["history"]["completed_order_count"])
        self.assertIsNone(result["history"]["ltv"]["value"])
        self.assertFalse(any("purchase_number" in order for order in result["history"]["orders"]))

    def test_erasure_and_missing_owner_stop_before_selection_or_order_reads(self):
        IgClient.objects.filter(pk=self.row.pk).update(privacy_erasure_started_at=self.now)
        with patch("management.services.ig_commerce_projection.capture_current_selection_lines") as source, patch.object(reader, "_history") as history:
            result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        source.assert_not_called(); history.assert_not_called()
        self.assertEqual(result["reason"], "client_erasing")
        self.assertEqual(result["history"], {})

    def test_final_source_fence_change_discards_both_current_and_history(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        calls = 0
        def changed(*args, **kwargs):
            nonlocal calls
            actual = capture_current_selection_lines(*args, **kwargs)
            calls += 1
            if calls == 2:
                actual = deepcopy(actual); actual["capture_digest"] = "different-final-source"
            return actual
        with patch("management.services.ig_commerce_projection.capture_current_selection_lines", side_effect=changed):
            result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual((result["status"], result["reason"]), ("conflict", "commerce_scope_changed"))
        self.assertEqual(result["current"], {}); self.assertEqual(result["history"], {})

    def test_read_budget_and_rejected_writer_do_not_poison_caller_transaction(self):
        with patch.object(reader, "MAX_READ_QUERIES", 1):
            result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual(result["reason"], "commerce_scope_read_budget_exceeded")
        def writer(_client_id):
            IgClient.objects.filter(pk=self.row.pk).update(current_size="L")
            return {}
        with patch.object(reader, "_history", side_effect=writer):
            result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual(result["reason"], "commerce_scope_side_effect_rejected")
        self.row.refresh_from_db(); self.assertEqual(self.row.current_size, "")

    def test_protected_direct_and_fetch_get_are_select_only_and_do_zero_work(self):
        self.client.force_login(self.actor)
        for headers in ({}, {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}):
            with self.subTest(headers=headers), patch("management.services.ig_commerce_projection.bootstrap_session_from_legacy") as bootstrap, patch(
                    "management.services.call_ai_analysis.gemini_generate_text") as provider, patch(
                    "management.services.ig_memory_producer.enqueue_memory_source") as memory, CaptureQueriesContext(connection) as queries:
                response = self.client.get(f"/bot/api/clients/{self.row.pk}/commerce-scope/", **headers)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()["success"], response.content)
            self.assertIn("no-store", response.headers["Cache-Control"])
            bootstrap.assert_not_called(); provider.assert_not_called(); memory.assert_not_called()
            self.assertFalse(any(query["sql"].lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"} for query in queries))
            self.assertFalse(any("FOR UPDATE" in query["sql"].upper() for query in queries))

    def test_reviewer_dominates_permissions_and_invalid_duplicate_parameters_are_finite(self):
        self.client.force_login(self.actor)
        endpoint = f"/bot/api/clients/{self.row.pk}/commerce-scope/"
        for suffix in ("?order_id=1&order_id=2", "?order_id=9223372036854775808", "?revision_id=1"):
            self.assertEqual(self.client.get(endpoint + suffix).status_code, 400)
        self.actor.groups.add(Group.objects.get_or_create(name=META_REVIEWER_GROUP_NAME)[0])
        with patch("management.bot_commerce_scope_views.capture_commerce_scope") as capture:
            response = self.client.get(endpoint)
        self.assertEqual(response.status_code, 403)
        capture.assert_not_called()


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class CommerceScopeLineScenesTests(TestCase):
    setUp = line_fixtures.CommerceLineOperationsTests.setUp
    source = line_fixtures.CommerceLineOperationsTests.source
    reduce = line_fixtures.CommerceLineOperationsTests.reduce
    initial = line_fixtures.CommerceLineOperationsTests.initial
    attach_order = line_fixtures.CommerceLineOperationsTests.attach_order

    def test_three_real_customer_positions_keep_each_scope_and_only_active_size_editable(self):
        shirt, _ = self.initial()
        hoodie, _ = self.reduce('добавьте худи размер M')
        gift, _ = self.reduce('добавьте футболку размер XL для друга')
        result = reader.capture_commerce_scope(self.customer.pk, now=self.now)
        self.assertEqual(result['status'], 'captured', result)
        lines = result['current']['lines']
        self.assertEqual([line['title'] for line in lines], ['Футболка', 'Худі', 'Футболка'])
        self.assertEqual([line['recipient_id'] for line in lines], ['self', 'self', 'friend'])
        self.assertEqual([line['fields']['size']['value'] for line in lines], ['L', 'M', 'XL'])
        self.assertEqual([line['fields']['size']['source_refs'][0]['id'] for line in lines], [shirt.pk, hoodie.pk, gift.pk])
        self.assertEqual([line['editable'] for line in lines], [False, False, True])
        scope = result['current']['editable_scope']
        self.assertEqual((scope['line_id'], scope['recipient_id']), (lines[2]['line_id'], 'friend'))
        self.assertEqual(result['history']['orders'], [])
        self.assertEqual(result['history']['completed_order_count'], 0)

    def test_pink_current_has_distinct_source_from_superseded_black_and_history_is_bounded(self):
        black, _ = self.initial()
        self.reduce('добавьте худи размер M')
        pink, _ = self.reduce('не чёрную, а розовую футболку')
        result = reader.capture_commerce_scope(self.customer.pk, now=self.now)
        self.assertEqual(result['status'], 'captured', result)
        line = result['current']['lines'][0]
        self.assertEqual(line['fields']['color']['value'], 'pink')
        self.assertEqual(line['fields']['color']['source_refs'][0]['id'], pink.pk)
        history = [row for row in line['history']['items'] if row['field'] == 'color']
        self.assertEqual([row['value'] for row in history], ['pink', 'black'])
        self.assertEqual(history[0]['previous'], 'black')
        self.assertEqual(history[0]['previous_source']['source_refs'][0]['id'], black.pk)
        self.assertTrue(all(row['read_only'] for row in history))
        self.assertFalse(line['history']['coverage']['complete'])
        self.assertEqual(line['history']['coverage']['event_limit'], 8)
        self.assertNotIn('copied_from', line['fields']['color'])

    def test_uniquely_proven_copy_keeps_original_size_source_new_pink_source_and_old_paid_separate(self):
        from orders.models import OrderItem
        original, _ = self.initial()
        pink, _ = self.reduce('не чёрную, а розовую футболку')
        old_episode, old_order = self.attach_order(paid=True)
        OrderItem.objects.create(order=old_order, title='Перша реальна футболка', size='L', qty=1,
            unit_price=Decimal('790'), line_total=Decimal('790'), is_custom=True, color_name_custom='Рожевий')
        repeat, _ = self.reduce('ещё одну такую же', expected_revision=2)
        result = reader.capture_commerce_scope(self.customer.pk, now=self.now)
        self.assertEqual(result['status'], 'captured', result)
        self.assertNotEqual(result['current']['scope']['episode_id'], old_episode.pk)
        self.assertIsNone(result['current']['scope']['order_id'])
        line = result['current']['lines'][0]
        self.assertEqual(line['fields']['size']['copied_from']['proof']['source_message_id'], original.pk)
        self.assertEqual(line['fields']['color']['copied_from']['proof']['source_message_id'], pink.pk)
        self.assertEqual(line['fields']['size']['source_refs'][0]['id'], repeat.pk)
        self.assertEqual(line['fields']['quantity']['status'], 'unknown')
        self.assertFalse(line['defaults']['quantity']['source_confirmed'])
        self.assertEqual(result['history']['orders'][0]['id'], old_order.pk)
        self.assertEqual(result['history']['orders'][0]['payment']['recorded_status'], 'paid')
        self.assertNotIn('payment', result['current'])
        self.assertIsNone(result['history']['ltv']['value'])

    def test_replacement_does_not_revive_previous_size_or_enable_editor(self):
        self.initial()
        self.reduce('вместо футболки худи')
        result = reader.capture_commerce_scope(self.customer.pk, now=self.now)
        self.assertEqual(result['status'], 'captured', result)
        line = result['current']['lines'][0]
        self.assertEqual(line['title'], 'Худі')
        self.assertEqual(line['fields']['size']['status'], 'unknown')
        self.assertEqual(line['fields']['color']['status'], 'unknown')
        self.assertFalse(line['editable'])
        self.assertFalse(result['current']['editable'])
        self.assertIsNone(result['current']['editable_scope'])


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class CommerceScopeRevenueTests(TestCase):
    setUp = fixtures.SizeCorrectionTests.setUp
    message = fixtures.SizeCorrectionTests.message
    order = CommerceScopeReadTests.order

    def canonical_order(self, *, gross='790.00', refunded='0.00', status='done'):
        """Use the real signed payment reducer; no provider transport is called."""
        from datetime import timedelta
        from management.models import IgDeal, IgCommercialEpisode, IgPaymentProjection
        from management.services.bot_payments import apply_payment_status
        self.revenue_index = getattr(self, 'revenue_index', 0) + 1
        order = self.order(status=status, number=f'REVENUE-{self.revenue_index}')
        deal = IgDeal.objects.create(client=self.row, order=order, amount=Decimal(gross),
            requested_payment_amount=Decimal(gross), invoice_id=f'revenue-invoice-{order.pk}', currency='UAH')
        episode = IgCommercialEpisode.objects.create(client=self.row, sequence=100+order.pk, open_slot=None,
            materialization_key=f'revenue-order:{order.pk}', state='fulfilled', intended_order=order, deal=deal)
        payload = {'invoiceId': deal.invoice_id, 'status': 'success', 'amount': int(Decimal(gross)*100),
            'finalAmount': int(Decimal(gross)*100), 'modifiedDate': self.now.isoformat()}
        apply_payment_status(deal, 'success', payload, source='provider_pull')
        if Decimal(refunded) > 0:
            payload.update(status='reversed' if refunded == gross else 'success',
                finalAmount=int((Decimal(gross)-Decimal(refunded))*100),
                modifiedDate=(self.now+timedelta(seconds=1)).isoformat())
            apply_payment_status(deal, payload['status'], payload, source='provider_pull')
        return order, deal, episode, IgPaymentProjection.objects.get(deal=deal)

    def test_two_actual_orders_net_partial_and_full_refunds_do_not_change_new_draft(self):
        before = reader.capture_commerce_scope(self.row.pk, now=self.now)
        first, _, _, _ = self.canonical_order(gross='900.00', refunded='250.00')
        second, _, _, _ = self.canonical_order(gross='790.00', refunded='790.00')
        with patch('management.services.call_ai_analysis.gemini_generate_text') as provider, patch(
                'management.services.ig_memory_producer.enqueue_memory_source') as memory, CaptureQueriesContext(connection) as queries:
            result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual(result['status'], 'captured', result)
        self.assertEqual(result['current'], before['current'])
        self.assertNotIn('payment', result['current'])
        self.assertEqual(result['history']['ltv']['status'], 'confirmed')
        self.assertEqual((result['history']['ltv']['value'], result['history']['ltv']['gross'], result['history']['ltv']['refunded']),
            ('650.00', '1690.00', '1040.00'))
        self.assertEqual(set(result['history']['ltv']['order_ids']), {first.pk, second.pk})
        self.assertEqual(result['history']['ltv']['currency_code'], 980)
        self.assertEqual(result['history']['ltv']['label'], 'Підтверджена виручка')
        self.assertEqual(result['history']['ltv']['refund_coverage'], 'confirmed_projection_as_of_capture')
        self.assertEqual(result['history']['completed_order_count'], 2)
        self.assertFalse(any(query['sql'].lstrip().split(None, 1)[0].upper() in {'INSERT', 'UPDATE', 'DELETE'} for query in queries))
        provider.assert_not_called(); memory.assert_not_called()

    def test_exact_fully_refunded_receipt_is_known_zero_empty_or_recorded_paid_is_unknown(self):
        self.assertEqual(reader.capture_commerce_scope(self.row.pk, now=self.now)['history']['ltv']['reason'], 'payment_history_empty')
        self.canonical_order(gross='790.00', refunded='790.00')
        result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual((result['history']['ltv']['status'], result['history']['ltv']['value']), ('confirmed', '0.00'))
        self.order(number='RECORD-ONLY')
        result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual(result['history']['ltv']['status'], 'unknown')
        self.assertIsNone(result['history']['ltv']['value'])

    def test_actual_payment_history_survives_chat_reset_without_reviving_erased_source_choice(self):
        from management.models import IgFunnelResetAudit
        order, _, _, _ = self.canonical_order()
        IgFunnelResetAudit.objects.create(client=self.row, reset_after_message_id=self.source.pk,
            reason='Reset current conversation inference', actor=self.actor)
        result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual(result['status'], 'captured', result)
        self.assertEqual(result['history']['ltv']['value'], '790.00')
        self.assertEqual(result['history']['ltv']['order_ids'], [order.pk])
        self.assertFalse(result['current']['editable'])
        self.assertFalse(any(line['fields']['size']['status'] == 'confirmed' for line in result['current']['lines']))
        IgClient.objects.filter(pk=self.row.pk).update(privacy_erasure_started_at=self.now)
        self.assertEqual(reader.capture_commerce_scope(self.row.pk, now=self.now)['history'], {})

    def test_corrected_owner_cannot_reactivate_original_signed_payment_as_lifetime_revenue(self):
        from management.services.ig_order_assignments import link_order_to_client, unlink_order_from_client
        order, _, _, _ = self.canonical_order()
        assignment = link_order_to_client(order, client=self.row, actor=self.actor)
        unlink_order_from_client(order, client=self.row, actor=self.actor, expected_version=assignment.version,
            reason_code='incorrect_binding_corrected', reason='Wrong binding reviewed')
        result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual(result['history']['coverage'], 'uncertain')
        self.assertIsNone(result['history']['ltv']['value'])
        self.assertEqual(result['history']['unresolved'][0]['order_id'], order.pk)
        self.assertNotIn('items', result['history']['unresolved'][0])

    def test_dirty_projection_currency_conflict_and_receipt_corruption_stay_unknown(self):
        from management.models import IgDeal, IgPaymentProjection
        order, deal, episode, projection = self.canonical_order()
        IgPaymentProjection.objects.filter(pk=projection.pk).update(needs_reconciliation=True)
        self.assertEqual(reader.capture_commerce_scope(self.row.pk, now=self.now)['history']['ltv']['reason'], 'payment_refund_coverage_unknown')
        IgPaymentProjection.objects.filter(pk=projection.pk).update(needs_reconciliation=False)
        IgDeal.objects.filter(pk=deal.pk).update(currency='USD')
        self.assertEqual(reader.capture_commerce_scope(self.row.pk, now=self.now)['history']['ltv']['reason'], 'payment_currency_mismatch')
        deal.currency = 'UAH'; projection.refresh_from_db()
        missing = reader._net_revenue(order, episode, order.instagram_attribution, self.row.pk, [deal], {}, [], ownership_conflict=False)
        self.assertEqual(missing['reason'], 'payment_projection_missing')
        copied = deepcopy(projection); copied.last_event.evidence['signature'] = 'corrupted'
        result = reader._net_revenue(order, episode, order.instagram_attribution, self.row.pk, [deal],
            {deal.pk: copied}, [], ownership_conflict=False)
        self.assertEqual(result['reason'], 'payment_receipt_integrity_unknown')
        self.assertIsNone(result['value'])

    def test_twenty_actual_order_payment_proofs_are_batched_inside_unchanged_read_budget(self):
        for _ in range(20): self.canonical_order()
        result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual(result['status'], 'captured', result)
        self.assertEqual(result['history']['ltv']['value'], '15800.00')
        self.assertEqual(len(result['history']['ltv']['proofs']), 20)
        self.assertLessEqual(result['diagnostics']['read_queries'], 96)
        self.assertEqual(result['diagnostics']['max_read_queries'], 96)
        self.order(number='OVERFLOW')
        result = reader.capture_commerce_scope(self.row.pk, now=self.now)
        self.assertEqual(result['history']['coverage'], 'bounded')
        self.assertIsNone(result['history']['ltv']['value'])
        self.assertEqual(result['history']['ltv']['reason'], 'order_history_incomplete')


from management.tests_ig_hosted_settlement import HostedSettlementFixture


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_ASSISTED_CHECKOUT_V2='enforced', IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class CommerceScopeHostedRevenueTests(HostedSettlementFixture, TestCase):
    def test_actual_hosted_settlement_refund_after_fulfillment_is_net_revenue_not_order_total(self):
        from management.services.bot_payments import reconcile_payment_projection
        from management.models import IgPaymentProjection
        from management.services.ig_checkout_payment import project_attempt_settlement
        from orders.models import Order
        proposal, attempt, order = self.paid('revenue-hosted')
        Order.objects.filter(pk=order.pk).update(status='done')
        outcome = project_attempt_settlement(attempt.pk, status='success', source='provider_pull',
            payload=self.payload(attempt, net=65000, seconds=10))
        self.assertTrue(outcome['applied'], outcome)
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        self.assertTrue(reconcile_payment_projection(projection.pk))
        result = reader.capture_commerce_scope(self.client_row.pk)
        self.assertEqual(result['status'], 'captured', result)
        self.assertEqual(result['history']['ltv']['value'], '650.00')
        self.assertEqual(result['history']['ltv']['refunded'], '250.00')
        # Service reconciliation labels remain separate from exact received net.
        self.assertEqual(result['history']['orders'][0]['payment']['status'], 'reconciliation')
        self.assertEqual(result['history']['ltv']['proofs'][0]['event_id'], outcome['event_id'])
        outcome = project_attempt_settlement(attempt.pk, status='reversed', source='provider_pull',
            payload=self.payload(attempt, status='reversed', net=0, seconds=20))
        self.assertTrue(outcome['applied'], outcome)
        reconcile_payment_projection(projection.pk)
        result = reader.capture_commerce_scope(self.client_row.pk)
        self.assertEqual((result['history']['ltv']['status'], result['history']['ltv']['value']), ('confirmed', '0.00'))

    def test_same_provider_version_ambiguous_receipt_with_old_good_projection_is_not_known_revenue(self):
        from management.services.ig_checkout_payment import project_attempt_settlement
        proposal, attempt, _order = self.paid('revenue-ambiguous')
        outcome = project_attempt_settlement(attempt.pk, status='success', source='provider_pull',
            payload=self.payload(attempt, net=65000))
        self.assertEqual(outcome['reason'], 'settlement_provider_version_conflict')
        result = reader.capture_commerce_scope(self.client_row.pk)
        self.assertEqual(result['status'], 'captured', result)
        self.assertEqual(result['history']['ltv']['reason'], 'payment_refund_coverage_unknown')
        self.assertIsNone(result['history']['ltv']['value'])
