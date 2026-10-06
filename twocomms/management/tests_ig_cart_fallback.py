"""Actual pure cart fallback over sealed sources; no Django/provider/DB setup."""
from copy import deepcopy
from dataclasses import replace
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from management import tests_ig_response_cart_plan as plan_fixtures
from management.services.ig_response_guard import build_source_preference_fallback
from management.services.ig_turn_intelligence import capture_digest
from management.services.ig_response_cart_plan import ResponseLinePlan
from management.services.ig_reply_truth import ReplyTruthContext
import json


class CartSourceFallbackTests(unittest.TestCase):
    def fixture(self, *, missing=None, language='uk'):
        helper = plan_fixtures.ResponseCartPlanTests()
        cart, client, revision = helper.sealed_capture()
        client.language = language
        text = 'Ви обрали Classic худі M чорний і Storm футболку L рожеву'
        revision.bundle_snapshot['sources'][0]['text'] = text
        digest = hashlib.sha256(text.encode()).hexdigest()
        envelope_digest = capture_digest(revision.bundle_snapshot['sources'][0])
        revision.bundle_snapshot['sources'][0]['source_digest'] = envelope_digest
        for row in cart['lines']:
            for fact in row['fields'].values():
                fact['source'].update(source_digest=digest, decision_id=30)
        revision.snapshot_digest = capture_digest(revision.bundle_snapshot)
        revision.action_receipts = {'commerce_reduction': {'snapshot_digest': revision.snapshot_digest,
            'decisions': [{'source_message_id': 8, 'source_digest': envelope_digest, 'decision_id': 30,
                'transition_id': 12, 'session_id': 10, 'accepted': True, 'is_stale': False}]}}
        self.rehash(cart)
        plan = helper.plan(cart, sources=revision.bundle_snapshot['sources'], missing=missing or {'first': ['fit']})
        return cart, client, revision, plan

    def rehash(self, cart):
        cart['capture_digest'] = capture_digest({key: value for key, value in cart.items() if key != 'capture_digest'})

    def build(self, client, revision, plan):
        with patch('management.services.ig_response_plan.capture_response_plan', side_effect=AssertionError('must not recapture')):
            return build_source_preference_fallback(client, revision=revision, response_plan=plan)

    def test_all_current_sizes_colors_titles_have_exact_position_and_one_scoped_question(self):
        _cart, client, revision, plan = self.fixture()
        response, proof = self.build(client, revision, plan)
        self.assertIsNotNone(response, proof)
        self.assertIn('Позиція 1 для себе · Classic: ви обрали розмір M.', response.reply_text)
        self.assertIn('Позиція 2 для себе · Storm: ви обрали розмір L.', response.reply_text)
        self.assertIn('Позиція 1 для себе · Classic: ви обрали колір чорний.', response.reply_text)
        self.assertIn('Позиція 2 для себе · Storm: ви обрали колір рожевий.', response.reply_text)
        self.assertEqual(response.reply_text.count('?'), 1)
        self.assertIn('Позиція 1 для себе · Classic: Яку посадку', response.reply_text)
        self.assertEqual(response.control, {})
        self.assertEqual(plan.validate_multiline_claims(response, ReplyTruthContext()), '')
        self.assertIn('8:first:size', proof['coverage']['covered'])
        self.assertIn('8:second:size', proof['coverage']['covered'])
        self.assertEqual(proof['current_source_preference']['decision_id'], 30)
        self.assertEqual(proof['source_cart_capture']['capture_digest'], plan.source_cart_capture['capture_digest'])
        proof['source_cart_capture']['lines'][0]['fields']['size']['value'] = 'XL'
        self.assertEqual(plan.source_cart_capture['lines'][0]['fields']['size']['value'], 'M')

    def test_latest_pink_replaces_old_black_without_history_becoming_current_ack(self):
        cart, client, revision, _plan = self.fixture()
        row = cart['lines'][0]
        row['fields']['color']['value'] = 'pink'; row['source_selection']['values']['color'] = 'pink'
        row['history'] = [{'field': 'color', 'value': 'black', 'source_message_id': 7, 'source_digest': 'b'*64}]
        cart['fence']['source_ids'] = [7, 8]
        self.rehash(cart)
        plan = plan_fixtures.ResponseCartPlanTests().plan(cart, sources=revision.bundle_snapshot['sources'], missing={'first': ['fit']})
        response, proof = self.build(client, revision, plan)
        self.assertIsNotNone(response, proof)
        self.assertNotIn('чорний', response.reply_text)
        self.assertIn('Позиція 1 для себе · Classic: ви обрали колір рожевий.', response.reply_text)
        self.assertIn('8:first:color', proof['coverage']['covered'])

    def test_duplicate_sku_distinguishes_recipients_and_never_borrows_sibling_size(self):
        cart, client, revision, _plan = self.fixture()
        for index, row in enumerate(cart['lines']):
            row['recipient_id'] = ('alice', 'bob')[index]
            row['source_selection']['scope']['recipient_id'] = row['recipient_id']
            row['fields']['product_id']['value'] = 55
            row['source_selection']['values']['product_id'] = 55
        self.rehash(cart)
        plan = plan_fixtures.ResponseCartPlanTests().plan(cart, sources=revision.bundle_snapshot['sources'], missing={'second': ['fit']})
        response, proof = self.build(client, revision, plan)
        self.assertIsNotNone(response, proof)
        self.assertIn('Позиція 1 для alice · Classic: ви обрали розмір M.', response.reply_text)
        self.assertIn('Позиція 2 для bob · Classic: ви обрали розмір L.', response.reply_text)
        self.assertIn('Позиція 2 для bob · Classic: Яку посадку', response.reply_text)
        self.assertIn('8:first:size', proof['coverage']['covered']); self.assertIn('8:second:size', proof['coverage']['covered'])

    def test_foreign_owner_expired_episode_and_after_seal_artifact_fail_without_recapture(self):
        for mutation, reason in ((lambda cart, client, revision: setattr(client, 'pk', 99), 'response_plan_provided_cart_revision_changed'),
            (lambda cart, client, revision: setattr(client, 'current_commercial_episode_id', 99), 'source_cart_scope_changed'),
            (lambda cart, client, revision: cart['source_watermark'].update(event_at='2026-10-07T10:00:00+00:00'), 'source_cart_after_seal')):
            cart, client, revision, plan = self.fixture()
            mutation(cart, client, revision); self.rehash(cart)
            plan = replace(plan, source_cart_capture=cart)
            response, proof = self.build(client, revision, plan)
            self.assertIsNone(response); self.assertEqual(proof['reason'], reason)

    def test_exact_current_accepted_reduction_required_no_result_payload_authority(self):
        for change in ('rejected', 'stale', 'foreign_digest', 'body_digest', 'missing_envelope', 'foreign_decision', 'missing_receipt'):
            cart, client, revision, plan = self.fixture()
            row = revision.action_receipts['commerce_reduction']['decisions'][0]
            if change == 'rejected': row['accepted'] = False
            elif change == 'stale': row['is_stale'] = True
            elif change == 'foreign_digest': row['source_digest'] = 'f'*64
            elif change == 'body_digest': row['source_digest'] = hashlib.sha256(revision.bundle_snapshot['sources'][0]['text'].encode()).hexdigest()
            elif change == 'missing_envelope':
                del revision.bundle_snapshot['sources'][0]['source_digest']
                revision.snapshot_digest = capture_digest(revision.bundle_snapshot)
                revision.action_receipts['commerce_reduction']['snapshot_digest'] = revision.snapshot_digest
            elif change == 'foreign_decision': row['decision_id'] = 999
            else: revision.action_receipts = {}
            revision.result_payload = {'source_facts': cart['lines'][0]['evidence']}
            response, proof = self.build(client, revision, plan)
            self.assertIsNone(response)
            self.assertIn(proof['reason'], {'fallback_no_current_source_reduction', 'fallback_source_reduction_unavailable'})

    def test_mutated_current_source_and_line_plan_proof_cannot_supply_fallback_authority(self):
        cart, client, revision, plan = self.fixture()
        revision.bundle_snapshot['sources'][0]['text'] = 'Інший вибір'
        revision.snapshot_digest = capture_digest(revision.bundle_snapshot)
        revision.action_receipts['commerce_reduction']['snapshot_digest'] = revision.snapshot_digest
        response, proof = self.build(client, revision, plan)
        self.assertIsNone(response); self.assertEqual(proof['reason'], 'fallback_current_source_changed')
        cart, client, revision, plan = self.fixture()
        row = plan.line_plans[0].as_dict(); row['evidence']['size']['source_digest'] = 'f'*64
        plan = replace(plan, line_plans=(ResponseLinePlan(json.dumps(row)), plan.line_plans[1]))
        response, proof = self.build(client, revision, plan)
        self.assertIsNone(response); self.assertEqual(proof['reason'], 'fallback_line_source_unavailable')

    def test_complete_checkout_request_requires_action_and_purchase_debt_is_not_fake_complete(self):
        cart, client, revision, plan = self.fixture()
        plan = plan_fixtures.ResponseCartPlanTests().plan(cart, sources=revision.bundle_snapshot['sources'])
        response, proof = self.build(client, revision, plan)
        self.assertIsNone(response); self.assertEqual(proof['reason'], 'fallback_complete_selection_requires_action')
        row = cart['lines'][0]
        row['fields']['purchase_requested'] = {'value': True, 'status': 'confirmed', 'authority': 'customer_source',
            'source': deepcopy(row['fields']['size']['source'])}
        row['source_selection']['fields'] = row['fields']; row['source_selection']['values']['purchase_requested'] = True
        row['source_selection']['evidence']['purchase_requested'] = row['fields']['purchase_requested']['source']
        self.rehash(cart)
        plan = plan_fixtures.ResponseCartPlanTests().plan(cart, sources=revision.bundle_snapshot['sources'], missing={'first': ['fit']})
        response, proof = self.build(client, revision, plan)
        self.assertIsNotNone(response, proof)
        self.assertIn('8:first:purchase_requested', proof['coverage']['remaining'])
        self.assertNotEqual(proof['coverage']['disposition'], 'complete')
        for token in ('наявності', 'оплачено', 'оформлено', 'відправимо', 'грн'):
            self.assertNotIn(token, response.reply_text)

    def test_unresolved_remove_and_ambiguous_line_are_finite_recovery(self):
        cart, client, revision, plan = self.fixture()
        plan = replace(plan, obligations=plan.obligations+({'id': '8:unresolved_line_operation', 'source_message_id': 8,
            'kind': 'unresolved_line_operation'},))
        response, proof = self.build(client, revision, plan)
        self.assertIsNone(response); self.assertEqual(proof['reason'], 'fallback_line_operation_requires_recovery')
        cart, client, revision, _plan = self.fixture()
        cart['lines'][1]['fields']['size']['status'] = 'ambiguous'; self.rehash(cart)
        plan = plan_fixtures.ResponseCartPlanTests().plan(cart, sources=revision.bundle_snapshot['sources'], missing={'first': ['fit']})
        response, proof = self.build(client, revision, plan)
        self.assertIsNone(response); self.assertEqual(proof['reason'], 'fallback_line_source_unavailable')

    def test_sixteen_lines_byte_bound_refuses_whole_reply_never_silently_truncates(self):
        cart, client, revision, _plan = self.fixture()
        first = cart['lines'][0]
        cart['lines'] = []
        for index in range(16):
            row = deepcopy(first); row.update(line_id=f'row-{index}', index=index)
            row['source_selection']['scope'].update(line_id=row['line_id'], active_index=index)
            cart['lines'].append(row)
        cart.update(active_index=0, active_line_id='row-0'); self.rehash(cart)
        plan = plan_fixtures.ResponseCartPlanTests().plan(cart, sources=revision.bundle_snapshot['sources'], missing={'row-0': ['fit']})
        response, proof = self.build(client, revision, plan)
        self.assertIsNone(response); self.assertEqual(proof['reason'], 'fallback_cart_reply_bound')

    def test_languages_use_the_same_line_scope_and_only_one_requested_selector(self):
        for language, ordinal, question in (('en', 'Item 1 for yourself', 'Which fit'), ('ru', 'Позиция 1 для себя', 'Какую посадку')):
            cart, client, revision, plan = self.fixture(language=language)
            response, proof = self.build(client, revision, plan)
            self.assertIsNotNone(response, proof)
            self.assertIn(ordinal, response.reply_text); self.assertIn(question, response.reply_text)
            self.assertEqual(response.reply_text.count('?'), 1)


    def test_audited_size_is_neutral_and_original_customer_size_is_not_rewritten(self):
        from management.services import ig_selection_corrections as corrections
        cart, client, revision, _plan = self.fixture()
        original_text = revision.bundle_snapshot['sources'][0]['text']
        row = cart['lines'][0]; proof = deepcopy(row['fields']['size']['source'])
        scope = {**cart['scope'], 'line_id': row['line_id'], 'recipient_id': row['recipient_id']}
        context = {'schema': 'size-correction-context.v1', 'field': 'size', 'scope': scope,
            'source_namespace': 'test:ig', 'reset_id': None, 'session_id': 10, 'generation': 2,
            'selection_revision': 2, 'value': 'M', 'source': deepcopy(proof)}
        operation = uuid.uuid4(); digest = corrections._digest(context)
        receipt = {'schema': corrections.SCHEMA, 'field': 'size', 'context': context, 'context_digest': digest,
            'actor_id': 42, 'capabilities': list(corrections.CAPABILITIES), 'operation_id': str(operation),
            'operation': 'set', 'before': 'M', 'after': 'L', 'reason_code': 'requirement_verified',
            'supersedes_transition_id': 12, 'recorded_at': '2026-10-06T10:00:00+00:00',
            'input_digest': corrections._digest(corrections._input(client_id=4, actor_id=42, operation_id=operation,
                expected_selection_revision=2, expected_context_digest=digest, operation='set', size='L', reason_code='requirement_verified'))}
        proof.update(authority='audited_correction', transition_id=31, decision_id=None,
            correction={'transition_id': 31, 'receipt': receipt})
        row['fields']['size'].update(value='L', authority='audited_correction', source=proof)
        row['source_selection']['values']['size'] = 'L'; row['source_selection']['evidence']['size'] = proof
        row['evidence']['size'] = proof
        self.rehash(cart)
        plan = plan_fixtures.ResponseCartPlanTests().plan(cart, sources=revision.bundle_snapshot['sources'], missing={'first': ['fit']})
        response, provenance = self.build(client, revision, plan)
        self.assertIsNotNone(response, provenance)
        self.assertIn('Позиція 1 для себе · Classic: уточнений розмір — L.', response.reply_text)
        self.assertNotIn('Позиція 1 для себе · Classic: ви обрали розмір L.', response.reply_text)
        self.assertIn('8:first:size', provenance['coverage']['covered'])
        self.assertEqual(revision.bundle_snapshot['sources'][0]['text'], original_text)
        self.assertEqual(receipt['before'], 'M')

    def test_unproven_readiness_title_does_not_become_source_selection_identity(self):
        cart, client, revision, _plan = self.fixture()
        for row in cart['lines']:
            del row['fields']['product_id']; del row['source_selection']['values']['product_id']
            del row['source_selection']['evidence']['product_id']
        self.rehash(cart)
        plan = plan_fixtures.ResponseCartPlanTests().plan(cart, sources=revision.bundle_snapshot['sources'], missing={'first': ['fit']})
        response, proof = self.build(client, revision, plan)
        self.assertIsNotNone(response, proof)
        self.assertNotIn('Classic', response.reply_text); self.assertNotIn('Storm', response.reply_text)
        self.assertIn('Позиція 1 для себе: ви обрали розмір M.', response.reply_text)

    def test_malformed_supplied_plan_is_finite_without_legacy_orm_import_or_provider(self):
        _cart, client, revision, plan = self.fixture()
        for candidate in ({'line_plans': 'forged'}, replace(plan, line_plans=(SimpleNamespace(as_dict=lambda: {'line_id': 'first'}),))):
            response, proof = self.build(client, revision, candidate)
            self.assertIsNone(response); self.assertEqual(proof['reason'], 'fallback_captured_cart_unavailable')
