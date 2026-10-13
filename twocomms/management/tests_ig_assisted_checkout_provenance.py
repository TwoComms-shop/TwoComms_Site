"""Native quote capability and strict mixed v2 transport regressions."""
from copy import deepcopy
from decimal import Decimal
import uuid

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management.services import ig_assisted_commerce as approvals
from management.services import ig_revision_cart_binding as binding
from management.services import ig_checkout_cart_provenance as lineage
from management.services.ig_checkout import (
    CheckoutConfigurationError, _build_assisted_quote, _deal_item_snapshot, build_assisted_quote,
)
from management.tests_ig_assisted_commerce import AssistedCommerceFixture


def _custom_row():
    return {'product_id': None, 'color_variant_id': None, 'is_custom': True,
        'title': 'Фіолетове худі', 'color_name_custom': 'Фіолетовий', 'size': 'L',
        'fit_option_code': 'oversize', 'fit_option_label': 'Оверсайз',
        'option_values': {}, 'option_labels': {}, 'qty': 2, 'unit_price': '970.00',
        'line_total': '1940.00', 'currency': 'UAH', 'photo_asset_id': None, 'photo_digest': '',
        'assisted_line': {'line_id': 'custom-1', 'recipient_id': 'friend-1', 'original_index': 1,
            'decision_id': '983cd3a4-fb11-461f-9e23-6ba3d5a7dbea',
            'decision_digest': 'd' * 64, 'line_digest': 'e' * 64}}


def _structural_binding(items):
    """Structural fixture only: no manager/current/purchase authority is granted."""
    scope = {'client_id': 7, 'episode_id': None, 'order_id': None,
        'reset_id': None, 'reset_floor': 1, 'source_namespace': 'instagram_login:fixture'}
    lines = [{'line_id': 'catalog-1', 'recipient_id': 'self', 'index': 0,
        'source_selection': {'schema': 'source-selection.v1', 'scope': scope, 'fields': {}}},
        {'line_id': 'custom-1', 'recipient_id': 'friend-1', 'index': 1,
        'source_selection': {'schema': 'source-selection.v1', 'scope': scope, 'fields': {}}}]
    for line in lines:
        line['source_selection']['scope'] = {**scope, 'session_id': 11, 'generation': 2,
            'revision': 4, 'line_id': line['line_id'], 'recipient_id': line['recipient_id']}
    capture = {'schema': 'source-selections.v1', 'status': 'captured', 'scope': scope,
        'session_id': 11, 'generation': 2, 'selection_revision': 4,
        'active_line_id': 'catalog-1', 'active_index': 0, 'lines': lines,
        'fence': {'owner_digest': 'a' * 64}}
    capture['capture_digest'] = binding._digest(capture)
    evidence = {'schema': 'ig-assisted-quote.v1',
        'request_id': '74a2a07e-b099-4e3b-982c-fb3e21a85c6e',
        'decision_id': '983cd3a4-fb11-461f-9e23-6ba3d5a7dbea',
        'intention_id': 'ab65e7a4-ab3c-40f0-9002-4e46a454c151', 'client_id': 7,
        'request_version': 1, 'decision_version': 2, 'request_digest': 'c' * 64,
        'decision_digest': 'd' * 64, 'full_capture_digest': capture['capture_digest'],
        'snapshot_digest': 'f' * 64, 'expires_at': '2026-10-15T12:00:00+00:00',
        'currency': 'UAH', 'line_digests': ['a' * 64, 'e' * 64]}
    result = {'schema': binding.ASSISTED_SCHEMA, 'scope': scope, 'bound_episode_id': None,
        'original_full_capture': deepcopy(capture), 'current_capture': deepcopy(capture),
        'assisted_commerce': evidence, 'source_capture_digest': capture['capture_digest'],
        'checkout_item_limit': 12, 'lines': deepcopy(lines)}
    result['source_binding_digest'] = binding._digest(result)
    result['quote_line_map'] = []
    for index, (line, item) in enumerate(zip(lines, items, strict=True)):
        configuration = binding.union_configuration(item, schema=binding.ASSISTED_SCHEMA)
        result['quote_line_map'].append({'line_id': line['line_id'], 'recipient_id': line['recipient_id'],
            'quote_position': index, 'configuration': configuration,
            'configuration_digest': binding._digest(configuration), 'quantity_source_confirmed': True})
    result['quote_binding_digest'] = binding._digest({'source_binding_digest': result['source_binding_digest'],
        'quote_line_map': result['quote_line_map']})
    return binding.rebind_assisted_cart_episode(result, 13)


class AssistedProvenanceTransportTests(SimpleTestCase):
    def setUp(self):
        self.owner = {'proposal_id': '8dafdb30-a6de-4a73-bc4e-72f5b3b72261', 'proposal_pk': 9,
            'revision_id': 10, 'revision': 1, 'client_id': 7, 'deal_id': 8, 'episode_id': 13}
        catalog = {'product_id': 37, 'color_variant_id': None, 'qty': 3, 'size': 'M',
            'fit_option_code': 'regular', 'option_values': {}, 'unit_price': '100.00', 'line_total': '260.00'}
        self.items = [{**catalog, 'proposal_item_id': 101, 'position': 0},
            {**_custom_row(), 'proposal_item_id': 102, 'position': 1}]
        self.binding = _structural_binding(self.items)
        self.revision = {'items': deepcopy(self.items), 'source_cart_binding': self.binding,
            'assisted_commerce': deepcopy(self.binding['assisted_commerce'])}
        self.cart = [{**self.items[0], 'qty': 1, 'unit_price': '100.00', 'line_total': '100.00'},
            {**self.items[0], 'qty': 2, 'unit_price': '80.00', 'line_total': '160.00'},
            deepcopy(self.items[1])]

    def capture(self):
        return lineage.capture_proposal_provenance(revision_snapshot=self.revision,
            owner=self.owner, proposal_items=self.items)

    def snapshot(self):
        return {'checkout_surface': 'instagram_proposal', 'proposal_id': self.owner['proposal_id'],
            'assisted_commerce': deepcopy(self.binding['assisted_commerce']), 'cart': deepcopy(self.cart),
            'source_cart_provenance': lineage.bind_expanded_cart(self.capture(), cart_items=self.cart)}

    def test_mixed_expansion_binds_actual_ids_without_losing_custom_recipient(self):
        snapshot = self.snapshot()
        checked = lineage.validate_attempt_provenance(snapshot)
        self.assertEqual(checked['schema'], lineage.ASSISTED_SCHEMA)
        actual = [{**row, 'id': 501 + index, 'order_id': 700} for index, row in enumerate(self.cart)]
        result = lineage.bind_order_items(checked, cart_items=self.cart, order_id=700, order_items=actual)
        self.assertEqual([row['order_item_id'] for row in result['item_map']], [501, 502, 503])
        self.assertEqual([row['recipient_id'] for row in result['item_map']], ['self', 'self', 'friend-1'])
        self.assertIsNone(result['binding']['scope']['episode_id'])
        self.assertEqual(result['binding']['bound_episode_id'], 13)

    def test_catalog_v1_never_accepts_nullable_or_custom_configuration(self):
        self.assertIsNone(binding.union_configuration(_custom_row()))
        self.assertIsNone(binding._configuration({'product_id': None, 'qty': 1}))
        row = _custom_row(); row['product_id'] = 37
        self.assertIsNone(binding.union_configuration(row, schema=binding.ASSISTED_SCHEMA))

    def test_custom_facts_approval_and_recipient_tamper_reject(self):
        for key, value in [('title', 'Інший товар'), ('color_name_custom', 'Чорний'),
            ('qty', 1), ('photo_digest', 'a' * 64), ('option_values', {'approved': 'yes'})]:
            with self.subTest(key=key):
                changed = deepcopy(self.cart); changed[-1][key] = value
                with self.assertRaises(lineage.CartProvenanceError):
                    lineage.bind_expanded_cart(self.capture(), cart_items=changed)
        changed = deepcopy(self.cart)
        changed[-1]['assisted_line']['recipient_id'] = 'self'
        with self.assertRaises(lineage.CartProvenanceError):
            lineage.bind_expanded_cart(self.capture(), cart_items=changed)

    def test_malformed_present_v2_never_becomes_legacy_unknown(self):
        for value in (None, {}, {'schema': binding.ASSISTED_SCHEMA},
                {**self.binding, 'quote_line_map': []}, {**self.binding, 'bound_episode_id': True}):
            with self.subTest(value=value), self.assertRaises(lineage.CartProvenanceError):
                lineage.capture_proposal_provenance(revision_snapshot={**self.revision, 'source_cart_binding': value},
                    owner=self.owner, proposal_items=self.items)
        with self.assertRaises(lineage.CartProvenanceError):
            lineage.validate_attempt_provenance({'assisted_commerce': {}})

    def test_attempt_schema_or_approval_downgrade_reject(self):
        for key in ('schema', 'approval', 'legacy'):
            snapshot = self.snapshot()
            if key == 'schema': snapshot['source_cart_provenance']['schema'] = lineage.SCHEMA
            elif key == 'approval': snapshot['assisted_commerce']['decision_digest'] = '0' * 64
            else: snapshot['source_cart_provenance'] = lineage.legacy_unknown()
            with self.subTest(key=key), self.assertRaises(lineage.CartProvenanceError):
                lineage.validate_attempt_provenance(snapshot)

    def test_foreign_episode_or_order_item_identity_reject(self):
        with self.assertRaises(lineage.CartProvenanceError):
            lineage.capture_proposal_provenance(revision_snapshot=self.revision,
                owner={**self.owner, 'episode_id': 14}, proposal_items=self.items)
        actual = [{**row, 'id': 501 + index, 'order_id': 700} for index, row in enumerate(self.cart)]
        actual[-1]['color_name_custom'] = 'Чорний'
        with self.assertRaises(lineage.CartProvenanceError):
            lineage.bind_order_items(lineage.validate_attempt_provenance(self.snapshot()),
                cart_items=self.cart, order_id=700, order_items=actual)

    def test_explicit_v2_parser_does_not_change_catalog_v1_parser(self):
        self.assertIsNone(binding._binding_payload(self.binding))
        self.assertTrue(binding.validate_assisted_cart_binding(self.binding, episode_id=13).ok)

    def test_existing_original_episode_cannot_rebind_to_a_foreign_episode(self):
        value = deepcopy(self.binding)
        for capture_key in ('original_full_capture', 'current_capture'):
            capture = value[capture_key]
            capture['scope']['episode_id'] = 13
            for line in capture['lines']:
                line['source_selection']['scope']['episode_id'] = 13
            capture.pop('capture_digest')
            capture['capture_digest'] = binding._digest(capture)
        value['scope']['episode_id'] = 13
        value['source_capture_digest'] = value['original_full_capture']['capture_digest']
        value['assisted_commerce']['full_capture_digest'] = value['source_capture_digest']
        for index, line in enumerate(value['lines']):
            line['source_selection'] = deepcopy(value['original_full_capture']['lines'][index]['source_selection'])
        value['source_binding_digest'] = binding._digest({key: val for key, val in value.items()
            if key not in {'source_binding_digest', 'quote_line_map', 'quote_binding_digest'}})
        value['quote_binding_digest'] = binding._digest({'source_binding_digest': value['source_binding_digest'],
            'quote_line_map': value['quote_line_map']})
        self.assertTrue(binding.validate_assisted_cart_binding(value, episode_id=13).ok)
        with self.assertRaisesRegex(ValueError, 'assisted_cart_owner_changed'):
            binding.rebind_assisted_cart_episode(value, 14)


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True)
class NativeAssistedQuoteTests(AssistedCommerceFixture, TransactionTestCase):
    def produced_request(self):
        request, _revision = self.request_case(custom=True)
        return request

    def produced_custom_cap(self, *, state='ready'):
        request = self.produced_request()
        source = request.envelope['snapshot']['lines'][0]
        row = {'line_id': source['line_id'], 'recipient_id': source['recipient_id'],
            'original_index': source['original_index'], 'item_kind': 'custom_apparel',
            'product_id': None, 'color_variant_id': None, 'title': 'Фіолетове худі',
            'color': 'Фіолетовий', 'size': 'L', 'fit_code': 'oversize', 'quantity': 1,
            'unit_price': '970.00', 'currency': 'UAH', 'availability': state,
            'checked_quantity': 1 if state == 'ready' else None,
            'supply_basis': 'accessible_unrecorded' if state == 'ready' else 'incoming_confirmed',
            'source_location': 'Менеджер перевірив товар', 'option_values': {},
            'preparation': None, 'dispatch': None, 'image_id': None, 'image_digest': '',
            'question_answers': [{'question_id': question['question_id'], 'state': 'confirmed',
                'answer': 'Параметр перевірено менеджером.'} for question in request.envelope['snapshot']['questions']
                if question['line_id'] == source['line_id']]}
        draft, _ = approvals.record_assisted_decision(request.pk, actor=self.manager,
            operation_id=uuid.uuid4(), status='draft', expected_request_version=request.version,
            expected_request_digest=request.envelope_hmac, expected_full_capture_digest=request.full_capture_digest,
            expected_decision_version=0, expected_draft_digest='', items=[row], now=self.now)
        decision, _ = approvals.record_assisted_decision(request.pk, actor=self.manager,
            operation_id=uuid.uuid4(), status='approved', expected_request_version=request.version,
            expected_request_digest=request.envelope_hmac, expected_full_capture_digest=request.full_capture_digest,
            expected_decision_version=draft.version, expected_draft_digest=draft.draft_digest, now=self.now)
        return approvals.load_verified_assisted_decision(request.pk, decision.pk, actor=self.manager,
            expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac, now=self.now)

    def produced_catalog_cap(self):
        self.variant.stock = self.rule.stock = 5
        self.variant.save(update_fields=['stock'])
        self.rule.save(update_fields=['stock'])
        request, _revision = self.request_case(custom=False)
        items = approvals._editable_items(request.envelope['snapshot'])
        for row in items:
            row.update(availability='ready', checked_quantity=1, supply_basis='warehouse_checked')
            row['question_answers'] = [{'question_id': question['question_id'],
                'state': 'confirmed', 'answer': 'Перевірено менеджером.'}
                for question in request.envelope['snapshot']['questions'] if question['line_id'] == row['line_id']]
        draft, _ = self.record(request, items=items)
        decision, _ = self.record(request, status='approved', latest=draft)
        return self.verified(request, decision)

    def test_native_approved_custom_quote_builds_without_fake_catalog_product(self):
        from storefront.models import Product
        from orders.models import Order, PaymentAttempt
        before = Product.objects.count()
        cap = self.produced_custom_cap()
        quote = build_assisted_quote(client=self.client_row, verified_decision=cap)
        self.assertEqual(quote.quoted_total, Decimal('970.00'))
        self.assertIsNone(quote.items[0].product)
        self.assertEqual(quote.items[0].color_label, 'Фіолетовий')
        source = binding.build_assisted_cart_binding(verified_decision=cap,
            quoted_items=[_deal_item_snapshot(item) for item in quote.items])
        episode_id = cap.full_capture['scope']['episode_id']
        self.assertIsNotNone(episode_id)
        rebound = binding.rebind_assisted_cart_episode(source, episode_id)
        self.assertTrue(binding.validate_assisted_cart_binding(rebound, episode_id=episode_id).ok)
        with self.assertRaisesRegex(ValueError, 'assisted_cart_owner_changed'):
            binding.rebind_assisted_cart_episode(source, episode_id + 1)
        self.assertEqual(Product.objects.count(), before)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(PaymentAttempt.objects.count(), 0)

    def test_wait_decision_never_forms_a_payable_quote(self):
        cap = self.produced_custom_cap(state='wait')
        with self.assertRaisesRegex(CheckoutConfigurationError, 'assisted_full_ready_required'):
            build_assisted_quote(client=self.client_row, verified_decision=cap)

    def test_raw_manager_or_option_json_cannot_issue_typed_quote(self):
        with self.assertRaises(ValueError):
            build_assisted_quote(client=self.client_row, verified_decision={
                'status': 'approved', 'unit_price': '970.00', 'option_values': {'approved': 'yes'}})

    def test_fresh_catalog_title_drift_cannot_rewrite_admitted_approved_quote(self):
        cap = self.produced_catalog_cap()
        self.product.title = 'Нова назва після підтвердження'
        self.product.save(update_fields=['title'])
        # Exercise readonly quote assembly after the genuine native approval
        # reader admitted the cap. Public effect owners retain their own gates.
        with self.assertRaisesRegex(CheckoutConfigurationError, 'assisted_catalog_facts_changed'):
            _build_assisted_quote(client=self.client_row, cap=cap)

    def test_fresh_catalog_color_drift_cannot_rewrite_admitted_approved_quote(self):
        cap = self.produced_catalog_cap()
        color = self.variant.color
        color.name = 'Midnight Black'
        color.save(update_fields=['name'])
        with self.assertRaisesRegex(CheckoutConfigurationError, 'assisted_catalog_facts_changed'):
            _build_assisted_quote(client=self.client_row, cap=cap)
