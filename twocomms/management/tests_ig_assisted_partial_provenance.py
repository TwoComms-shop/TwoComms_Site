"""Strict v3 transport tests. Structural DTOs grant no native commerce authority."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.services import ig_revision_cart_binding as binding
from management.services import ig_checkout_cart_provenance as provenance
from management.tests_ig_assisted_partial_sources import PartialCommerceFixture


class SelectedProvenanceStructureTests(SimpleTestCase):
    def _rehash(self, value):
        value['source_binding_digest'] = binding._digest({key: item for key, item in value.items()
            if key not in {'source_binding_digest', 'quote_line_map', 'quote_binding_digest'}})
        value['quote_binding_digest'] = binding._digest({'source_binding_digest': value['source_binding_digest'],
            'quote_line_map': value['quote_line_map']})
        return value

    def _binding(self):
        # Parser-only fixture, deliberately never passed to an effect owner.
        root_id, request_id, decision_id, intention = [f'00000000-0000-4000-8000-{index:012d}' for index in range(1, 5)]
        execution_id = '00000000-0000-4000-8000-000000000005'
        scope = {'client_id': 1, 'episode_id': 11, 'order_id': None, 'reset_id': None,
            'reset_floor': 1, 'source_namespace': 'fixture:source'}

        def capture(ids, episode):
            current_scope = {**scope, 'episode_id': episode}
            result = {'schema': binding.SOURCE_SCHEMA, 'status': 'captured', 'scope': current_scope,
                'session_id': episode, 'generation': 2, 'selection_revision': 3, 'coverage_complete': True,
                'fence': {'owner_digest': 'a' * 64, 'source_ids': [7]}, 'lines': []}
            for index, line_id in enumerate(ids):
                result['lines'].append({'line_id': line_id, 'recipient_id': 'self', 'index': index,
                    'fields': {'quantity': {'status': 'confirmed', 'value': 1}}, 'defaults': {},
                    'source_selection': {'schema': 'source-selection.v1', 'scope': {**current_scope,
                        'session_id': episode, 'generation': 2, 'revision': 3, 'line_id': line_id, 'recipient_id': 'self'}}})
            result['capture_digest'] = binding._digest(result)
            return result

        origin = capture(['root-a', 'root-b'], 10)
        current = capture(['adopted-b'], 11)
        quantities = {'root-a': 1, 'root-b': 2}
        coverage = {'schema': 'assisted-intention-coverage.v1', 'client_id': 1, 'intention_id': intention,
            'root_request_id': root_id, 'root_request_digest': 'b' * 64, 'original_quantities': quantities,
            'selected_line_ids': [], 'deferred_line_ids': ['root-b'], 'settled_line_ids': ['root-a'],
            'inflight_line_ids': [], 'cancelled_line_ids': [],
            'orders': [{'execution_id': execution_id, 'episode_id': 10, 'order_id': 100, 'line_ids': ['root-a'], 'quantities': {'root-a': 1}}],
            'executions': [{'execution_id': execution_id, 'execution_digest': 'c' * 64, 'request_id': root_id,
                'decision_id': decision_id, 'episode_id': 10, 'scope': 'partial', 'line_ids': ['root-a'], 'quantities': {'root-a': 1}, 'state': 'settled'}]}
        coverage['ledger_digest'] = binding._digest(coverage)
        coverage['selected_line_ids'] = ['root-b']
        coverage['deferred_line_ids'] = []
        coverage['coverage_digest'] = binding._digest(coverage)
        mapping = {'line_id': 'root-b', 'recipient_id': 'self', 'original_index': 1,
            'current_line_id': 'adopted-b', 'current_index': 0, 'quote_position': 0}
        choice, purchase = {'source_id': 7, 'receipt_digest': 'd' * 64}, {'message_id': 7, 'source_digest': 'e' * 64}
        evidence = {'schema': 'ig-assisted-selection.v1', 'client_id': 1, 'intention_id': intention,
            'root_request_id': root_id, 'root_request_digest': 'b' * 64, 'origin_full_capture_digest': origin['capture_digest'],
            'request_id': request_id, 'decision_id': decision_id, 'request_digest': 'f' * 64, 'decision_digest': '1' * 64,
            'full_capture_digest': current['capture_digest'], 'decision_snapshot_digest': '2' * 64,
            'scope': 'partial', 'selected_line_ids': ['root-b'], 'deferred_line_ids': [],
            'settled_line_ids': ['root-a'], 'cancelled_line_ids': [], 'quoted_total': '3000.00', 'custom_quantity': 2,
            'currency': 'UAH', 'terms_digest': '3' * 64,
            'shipping_terms': {'now': {'payer': 'merchant', 'customer_maximum': '0.00', 'currency': 'UAH'},
                'later': {'payer': 'merchant', 'customer_maximum': '0.00', 'currency': 'UAH'},
                'together': {'payer': 'merchant', 'customer_maximum': '0.00', 'currency': 'UAH'}},
            'delivery_scope': 'later', 'coverage_digest': coverage['coverage_digest'], 'ledger_coverage_digest': coverage['ledger_digest'],
            'choice_source_id': 7, 'purchase_source_id': 7, 'choice_digest': binding._digest(choice),
            'purchase_digest': binding._digest(purchase), 'line_map': [mapping], 'expires_at': '2026-10-15T09:00:00+00:00'}
        evidence['scope_digest'] = binding._digest({'scope': 'partial', 'selected_line_ids': ['root-b'], 'deferred_line_ids': []})
        raw = {'product_id': None, 'color_variant_id': None, 'is_custom': True, 'title': 'Approved purple hoodie',
            'color_name_custom': 'Purple', 'size': 'L', 'fit_option_code': '', 'fit_option_label': '',
            'option_values': {}, 'option_labels': {}, 'qty': 2, 'currency': 'UAH', 'photo_asset_id': None, 'photo_digest': '',
            'assisted_line': {'line_id': 'root-b', 'recipient_id': 'self', 'original_index': 1,
                'decision_id': decision_id, 'decision_digest': '1' * 64, 'line_digest': '4' * 64}}
        config = binding.union_configuration(raw, schema=binding.SELECTED_SCHEMA)
        value = {'schema': binding.SELECTED_SCHEMA, 'scope': scope, 'bound_episode_id': 11, 'origin_full_capture': origin,
            'request_full_capture': current, 'current_capture': current, 'assisted_commerce': evidence, 'coverage': coverage,
            'choice_proof': choice, 'purchase_proof': purchase, 'line_map': [mapping], 'source_capture_digest': current['capture_digest'],
            'checkout_item_limit': binding.CATALOG_CHECKOUT_ITEM_LIMIT,
            'lines': [{**{key: mapping[key] for key in mapping if key != 'quote_position'}, 'index': 0,
                'source_selection': current['lines'][0]['source_selection']}],
            'quote_line_map': [{**mapping, 'configuration': config, 'configuration_digest': binding._digest(config),
                'quantity_source_confirmed': True}]}
        return self._rehash(value), {**raw, 'proposal_item_id': 50, 'position': 0, 'unit_price': '1500.00', 'line_total': '3000.00'}

    def test_dense_selected_index_preserves_original_ordinal_and_approved_counteroffer_quantity(self):
        value, item = self._binding()
        checked = binding.validate_selected_cart_binding(value, episode_id=11)
        self.assertTrue(checked.ok, checked.as_dict())
        owner = {'proposal_id': '00000000-0000-4000-8000-000000000008', 'proposal_pk': 8, 'revision_id': 9,
            'revision': 1, 'client_id': 1, 'deal_id': 10, 'episode_id': 11}
        revision = {'source_cart_binding': value, 'assisted_commerce': value['assisted_commerce'], 'items': [item]}
        captured = provenance.capture_proposal_provenance(revision_snapshot=revision, owner=owner, proposal_items=[item])
        expanded = provenance.bind_expanded_cart(captured, cart_items=[item])
        snapshot = {'proposal_id': owner['proposal_id'], 'checkout_surface': 'instagram_proposal',
            'source_cart_provenance': expanded, 'assisted_commerce': value['assisted_commerce'], 'cart': [item]}
        self.assertEqual(provenance.validate_attempt_provenance(snapshot), expanded)
        bound = provenance.bind_order_items(expanded, cart_items=[item], order_id=20,
            order_items=[{**item, 'id': 21, 'order_id': 20}])
        mapped, = bound['item_map']
        self.assertEqual((mapped['line_id'], mapped['original_index'], mapped['current_line_id'], mapped['current_index'],
            mapped['quote_position'], mapped['order_item_id']), ('root-b', 1, 'adopted-b', 0, 0, 21))

    def test_rehashed_missing_or_overlapping_original_line_cannot_hide_deferred_intention(self):
        original, _item = self._binding()
        for bucket, replacement in (('settled_line_ids', []), ('deferred_line_ids', ['root-b'])):
            with self.subTest(bucket=bucket):
                changed = deepcopy(original)
                changed['coverage'][bucket] = replacement
                changed['assisted_commerce'][bucket] = replacement
                changed['coverage']['coverage_digest'] = binding._digest({key: value for key, value in changed['coverage'].items() if key != 'coverage_digest'})
                changed['assisted_commerce']['coverage_digest'] = changed['coverage']['coverage_digest']
                self.assertFalse(binding.validate_selected_cart_binding(self._rehash(changed)).ok)

    def test_rehashed_wrong_recipient_current_mapping_owner_and_choice_fail_closed(self):
        original, _item = self._binding()
        for change in ('recipient', 'index', 'owner', 'choice'):
            with self.subTest(change=change):
                value = deepcopy(original)
                if change == 'recipient':
                    value['line_map'][0]['recipient_id'] = 'friend'
                    value['assisted_commerce']['line_map'] = deepcopy(value['line_map'])
                elif change == 'index':
                    value['line_map'][0]['original_index'] = 0
                    value['assisted_commerce']['line_map'] = deepcopy(value['line_map'])
                elif change == 'owner':
                    value['bound_episode_id'] = 12
                else:
                    value['choice_proof']['source_id'] = 99
                self.assertFalse(binding.validate_selected_cart_binding(self._rehash(value), episode_id=11).ok)

    def test_selected_binding_cannot_downgrade_to_full_v2_or_legacy_unknown(self):
        value, _item = self._binding()
        downgraded = deepcopy(value)
        downgraded['schema'] = binding.ASSISTED_SCHEMA
        self.assertFalse(binding.validate_assisted_cart_binding(self._rehash(downgraded)).ok)
        with self.assertRaises(provenance.CartProvenanceError):
            provenance.validate_attempt_provenance({'assisted_commerce': value['assisted_commerce'], 'cart': []})
        with self.assertRaises(ValueError):
            binding.build_selected_cart_binding(verified_selection=value, quoted_items=[])

    def test_selected_frozen_money_cannot_replace_the_approved_scope_total(self):
        value, item = self._binding()
        owner = {'proposal_id': '00000000-0000-4000-8000-000000000008', 'proposal_pk': 8, 'revision_id': 9,
            'revision': 1, 'client_id': 1, 'deal_id': 10, 'episode_id': 11}
        altered = {**item, 'unit_price': '1000.00', 'line_total': '2000.00'}
        with self.assertRaisesMessage(provenance.CartProvenanceError, 'selected_cart_approved_totals_changed'):
            provenance.capture_proposal_provenance(owner=owner, proposal_items=[altered],
                revision_snapshot={'source_cart_binding': value, 'assisted_commerce': value['assisted_commerce'], 'items': [altered]})


@override_settings(ROOT_URLCONF='twocomms.urls_management', SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2='off', IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0)
class NativeSelectedProvenanceTests(PartialCommerceFixture, TransactionTestCase):
    def test_read_observer_consumes_same_native_selection_without_repeating_authority_graph(self):
        from management.services.ig_checkout import build_assisted_quote, observe_assisted_quote, CheckoutConfigurationError
        produced = self.selected_case()
        selection = produced['selection']
        fresh = build_assisted_quote(client=self.client_row, verified_selection=selection)
        with patch('management.services.ig_assisted_scopes.revalidate_verified_assisted_selection',
                side_effect=AssertionError('readonly observation must reuse enclosing native capture')):
            with CaptureQueriesContext(connection) as queries:
                observed = observe_assisted_quote(client=self.client_row, verified_selection=selection)
        self.assertEqual(len(queries), 0)
        self.assertEqual((observed.digest, observed.quoted_total, observed.assisted_commerce),
            (fresh.digest, fresh.quoted_total, fresh.assisted_commerce))
        self.assertEqual(observed.items[0].product_title, 'Фіолетове худі')
        self.assertEqual(observed.quoted_total, Decimal('1590.00'))
        with self.assertRaises(CheckoutConfigurationError):
            observe_assisted_quote(client=self.client_row, verified_selection={'ready': True, 'selection': selection})

    def test_actual_delivered_choice_quotes_and_pays_only_selected_rows_with_full_origin_retained(self):
        from management.models import IgCheckoutInvoiceGeneration, IgCheckoutInventoryReservation
        from management.services.ig_checkout import build_assisted_quote
        produced = self.selected_case()
        selection = produced['selection']
        quote = build_assisted_quote(client=self.client_row, verified_selection=selection)
        self.assertEqual((len(quote.items), quote.quoted_total), (1, Decimal('1590.00')))
        self.assertEqual(quote.assisted_commerce, binding.assisted_quote_evidence(selection))
        prepared = self.prepare_selection(selection)
        proposal = prepared['proposal']
        revision = proposal.revisions.get(revision=proposal.revision)
        frozen = revision.snapshot['source_cart_binding']
        self.assertEqual(frozen['schema'], binding.SELECTED_SCHEMA)
        self.assertEqual((len(frozen['origin_full_capture']['lines']), len(frozen['lines']), len(revision.snapshot['items'])), (2, 1, 1))
        self.assertEqual(frozen['coverage']['selected_line_ids'], selection.selected_line_ids)
        self.assertEqual(frozen['coverage']['deferred_line_ids'], selection.deferred_line_ids)
        self.assertEqual(frozen['line_map'], selection.line_map)
        self.assertTrue(binding.validate_selected_cart_binding(frozen, episode_id=proposal.commercial_episode_id).ok)
        order = self.pay_prepared(prepared)
        item = order.items.get()
        self.assertEqual((item.title, item.qty, item.unit_price, item.line_total),
            ('Фіолетове худі', 1, Decimal('1590.00'), Decimal('1590.00')))
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=proposal)
        original = provenance.validate_attempt_provenance(generation.payment_attempt.cart_snapshot)
        self.assertEqual(original['schema'], provenance.SELECTED_SCHEMA)
        self.assertEqual(len(original['item_map']), 1)
        paid = order.payment_payload['source_cart_provenance']
        mapped, = paid['item_map']
        self.assertEqual((mapped['line_id'], mapped['original_index'], mapped['current_line_id'],
            mapped['current_index'], mapped['quote_position'], mapped['order_item_id']),
            (selection.line_map[0]['line_id'], 0, selection.line_map[0]['current_line_id'], 0, 0, item.pk))
        self.assertEqual(paid['binding'], frozen)
        self.assertEqual((generation.payment_attempt.payable_amount, order.total_sum), (Decimal('1590.00'), Decimal('1590.00')))
        self.assertFalse(IgCheckoutInventoryReservation.objects.filter(proposal=proposal).exists())
        self.assertFalse(order.items.filter(title='Оливкове худі').exists())
