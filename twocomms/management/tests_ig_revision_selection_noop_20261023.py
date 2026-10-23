"""Redundant model selectors preserve original source choices; fake HTTP only."""
from copy import deepcopy
import hashlib
import json
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from management import tests_ig_revision_live as live
from management.tests_ig_restock_subscriptions import _RestockFixture
from management.models import (IgClient, IgCommerceSelectionSession, IgCommerceSelectionTransition,
    IgFollowUpTask, IgBotNotification, IgFunnelStepEvent, IgCheckoutProposal,
    IgCheckoutInventoryReservation, IgPaymentEvent, InstagramBotMessage)
from management.services import ig_revision_actions as actions


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_TURN_CONTEXT_MODE='unified',
    IG_CLIENT_STATE_PROMPT_TOKENS=20000, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeSelectionNoOpTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True
    _execute = live.RevisionLiveTests._execute

    def _generate(self, payload, **kwargs):
        validator = kwargs['result_validator']
        def observe(*args, **values):
            result = validator(*args, **values)
            self.validation_reasons = result.reason_codes
            return result
        kwargs['result_validator'] = observe
        return live.RevisionLiveTests._generate(self, payload, **kwargs)

    def setUp(self):
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests
        original = SourceCartCatalogTests.setUp
        def catalog_before_sources(case):
            original(case)
            case.category.name, case.category.slug = 'Худі', 'hoodies'
            case.category.save(update_fields=['name', 'slug'])
            case.products[0].price = case.variants[0].price_override = 900
            case.products[0].save(update_fields=['price'])
            case.variants[0].save(update_fields=['price_override'])
        with patch.object(SourceCartCatalogTests, 'setUp', catalog_before_sources):
            super().setUp()
        self.choice = self.canonical('Хочу худі Restock Aurora, кількість 1, розмір M, колір black, крій classic')
        self.settings.ai_enabled = True
        self.settings.save(update_fields=['ai_enabled'])
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count, self.actual_hash_override = 0, 'observed'
        self.provider_failure = False
        self.action_calls = []

    def candidate(self):
        # Unchanged parsed raw_zero_pending_m provider answer, not a repair.
        return {'reply_text': 'Зараз уточню у менеджера наявність худі Restock Aurora у чорному кольорі та розмірі M!',
            'controls': [{'kind': 'product', 'value': self.product.pk},
                {'kind': 'color_variant_id', 'value': self.variant.pk},
                {'kind': 'size', 'value': 'M'}, {'kind': 'manager', 'value': True}]}

    def domain_state(self):
        from orders.models import Order
        from management.services.ig_commerce_projection import capture_current_selection_lines
        return {'client': IgClient.objects.filter(pk=self.customer.pk).values('current_product_id',
                'current_size', 'current_color', 'current_qty', 'sales_context',
                'current_product_confidence', 'current_commercial_episode_id').get(),
            'session': IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1).snapshot(),
            'cart': capture_current_selection_lines(self.customer.pk),
            'transitions': list(IgCommerceSelectionTransition.objects.filter(session__client=self.customer).values()),
            'sources': list(InstagramBotMessage.objects.filter(client=self.customer).values()),
            'funnel': list(IgFunnelStepEvent.objects.filter(episode__client=self.customer).values()),
            'tasks': list(IgFollowUpTask.objects.filter(client=self.customer).values()),
            'notices': list(IgBotNotification.objects.filter(client=self.customer).values()),
            'stock': (type(self.variant).objects.get(pk=self.variant.pk).stock,
                      type(self.rule).objects.get(pk=self.rule.pk).stock),
            'purchase_counts': (IgCheckoutProposal.objects.count(), IgCheckoutInventoryReservation.objects.count(),
                                IgPaymentEvent.objects.count(), Order.objects.count())}

    def prepare(self, *, question='Чи є це худі в розмірі M у наявності?', parsed=None, capture_only=False):
        self.source_row = self.source(question)
        self.revision, self.token = _RestockFixture.revision(self, self.source_row)
        self.parsed = deepcopy(parsed or self.candidate())
        real_apply = actions.apply_revision_selection_actions
        def apply(*args, **kwargs):
            self.action_calls.append((args, kwargs))
            self.before_action = self.domain_state()
            if capture_only:
                return actions.RevisionSelectionActionResult(reasons=('fixture_capture_stop',))
            result = real_apply(*args, **kwargs)
            self.after_action = self.domain_state()
            return result
        with patch.object(actions, 'apply_revision_selection_actions', side_effect=apply), \
             patch('management.services.ig_presence.start_presence', return_value=None):
            result, model, http = self._execute()
        self.revision.refresh_from_db()
        return result, model, http

    def apply_captured(self):
        args, kwargs = self.action_calls[-1]
        return actions.apply_revision_selection_actions(*args, **kwargs)

    def assert_noop_receipt(self):
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts['client_configuration_update']
        self.assertEqual(receipt['outcome'], 'no_op')
        self.assertEqual(receipt['no_op']['schema'], 'unchanged-source-selection.v1')
        self.assertEqual(receipt['changed_fields'], [])
        self.assertEqual(receipt['selection_session']['before'], receipt['selection_session']['after'])
        self.assertEqual(receipt['before_authority'], receipt['after_authority'])
        return receipt

    def test_exact_raw_zero_answer_sends_once_without_rewriting_source_or_review(self):
        original = self.candidate()
        self.assertEqual((self.product.pk, self.variant.pk), (1, 1))
        self.assertEqual(hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest(),
            '4bccfbb7379efb540bec1c60032b7ec8b34686746521ff959b3fcda7d9d3cf1e')
        result, model, http = self.prepare(parsed=original)
        self.assertEqual(result.state, 'completed', (result.reasons, getattr(self, 'validation_reasons', ()),
            http.call_count, self.revision.action_receipts.get('response_coverage')))
        self.assertEqual((model.call_count, http.call_count), (1, 1))
        self.assertEqual(self.before_action, self.after_action)
        self.assertEqual(self.parsed, original)
        self.assert_noop_receipt()
        self.assertFalse(self.revision.action_receipts['response_coverage']['remaining'])
        self.assertEqual(self.revision.delivery_effects.filter(state='sent').count(), 1)
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.customer, reason='fulfillment_review').count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(client=self.customer, event_type='fulfillment_review').count(), 1)
        replay, generated, transport = self._execute()
        self.assertEqual(replay.state, 'completed', replay.reasons)
        generated.assert_not_called()
        transport.assert_not_called()

    def test_price_question_keeps_existing_review_and_exact_source_choices(self):
        # As in the existing known-price continuation fixture, a catalog ledger
        # is positive; physical availability still awaits the existing review.
        self.variant.stock = self.rule.stock = 100
        self.variant.save(update_fields=['stock'])
        self.rule.save(update_fields=['stock'])
        first, _, sent = self.prepare()
        self.assertEqual(first.state, 'completed', first.reasons)
        self.assertEqual(sent.call_count, 1)
        self.action_calls = []
        parsed = self.candidate()
        parsed['reply_text'] = 'Ціна Restock Aurora — 900 грн.'
        parsed['controls'] = parsed['controls'][:-1]
        result, model, http = self.prepare(question='А ціна?', parsed=parsed)
        self.assertEqual(result.state, 'completed', (result.reasons, getattr(self, 'validation_reasons', ())))
        self.assertEqual((model.call_count, http.call_count), (1, 1))
        self.assertEqual(self.before_action, self.after_action)
        self.assert_noop_receipt()
        self.assertFalse(self.revision.action_receipts['response_coverage']['remaining'])
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.customer, reason='fulfillment_review').count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(client=self.customer, event_type='fulfillment_review').count(), 1)

    def test_noop_receipt_replays_without_pin_selection_or_session_writes(self):
        result, _, http = self.prepare(capture_only=True)
        self.assertEqual(result.reasons, ('fixture_capture_stop',))
        self.assertEqual(http.call_count, 0)
        first = self.apply_captured()
        self.assertTrue(first.applied, first.reasons)
        before = self.domain_state()
        with patch.object(actions.bot_orders, 'pin_product', side_effect=AssertionError('no pin')), \
             patch.object(actions, 'persist_control_selection', side_effect=AssertionError('no selection')):
            replay = self.apply_captured()
        self.assertTrue(replay.already_applied, replay.reasons)
        self.assertEqual(self.domain_state(), before)
        self.assert_noop_receipt()

    def reject_stale_replay(self, mutate):
        self.prepare(capture_only=True)
        first = self.apply_captured()
        self.assertTrue(first.applied, first.reasons)
        mutate()
        before = self.domain_state()
        with patch.object(actions.bot_orders, 'pin_product', side_effect=AssertionError('no pin')), \
             patch.object(actions, 'persist_control_selection', side_effect=AssertionError('no selection')):
            rejected = self.apply_captured()
        self.assertFalse(rejected.applied, rejected)
        self.assertTrue(rejected.reasons)
        self.assertEqual(self.domain_state(), before)

    def test_replay_rejects_edited_current_source(self):
        self.reject_stale_replay(lambda: InstagramBotMessage.objects.filter(pk=self.source_row.pk).update(text='edited'))

    def test_replay_rejects_edited_original_choice_source(self):
        self.reject_stale_replay(lambda: InstagramBotMessage.objects.filter(pk=self.choice.pk).update(text='edited'))

    def test_replay_rejects_foreign_namespace(self):
        self.reject_stale_replay(lambda: InstagramBotMessage.objects.filter(pk=self.source_row.pk).update(provider_namespace='instagram_login:foreign'))

    def test_replay_rejects_client_permission_epoch(self):
        self.reject_stale_replay(lambda: IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=99))

    def test_replay_rejects_settings_permission_epoch(self):
        self.reject_stale_replay(lambda: type(self.settings).objects.filter(pk=self.settings.pk).update(reply_permission_epoch=99))

    def test_replay_rejects_reset_boundary(self):
        from management.models import IgFunnelResetAudit
        self.reject_stale_replay(lambda: IgFunnelResetAudit.objects.create(client=self.customer,
            reset_after_message_id=self.source_row.pk, reason='synthetic-reset'))

    def test_replay_rejects_catalog_drift(self):
        self.reject_stale_replay(lambda: type(self.variant).objects.filter(pk=self.variant.pk).update(price_override=901))

    def test_replay_rejects_selection_head_drift(self):
        def change():
            session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
            session.revision += 1
            session.save(update_fields=['revision', 'updated_at'])
        self.reject_stale_replay(change)

    def test_durable_noop_receipt_rejects_authority_tamper(self):
        self.prepare(capture_only=True)
        self.assertTrue(self.apply_captured().applied)
        self.revision.refresh_from_db()
        receipts = deepcopy(self.revision.action_receipts)
        receipts['client_configuration_update']['after_authority']['allowed_actions'].append('checkout_proposal_create')
        self.revision.action_receipts = receipts
        with self.assertRaisesMessage(ValueError, 'revision action receipt entries are immutable'):
            self.revision.save(update_fields=['action_receipts', 'updated_at'])
        replay = self.apply_captured()
        self.assertTrue(replay.already_applied, replay.reasons)
        self.assert_noop_receipt()

    def reject_foreign_selector(self, key, value):
        parsed = self.candidate()
        next(row for row in parsed['controls'] if row['kind'] == key)['value'] = value
        before = self.domain_state()
        result, _, http = self.prepare(parsed=parsed)
        self.assertNotEqual(result.state, 'completed')
        self.assertEqual(http.call_count, 0)
        self.assertNotIn('client_configuration_update', self.revision.action_receipts)
        after = self.domain_state()
        self.assertEqual(after['session']['lines'], before['session']['lines'])
        for field in ('stock', 'purchase_counts'):
            self.assertEqual(after[field], before[field])

    def test_wrong_product_control_does_not_become_noop(self):
        self.reject_foreign_selector('product', self.products[1].pk)

    def test_foreign_variant_control_does_not_become_noop(self):
        self.reject_foreign_selector('color_variant_id', self.variants[1].pk)

    def test_unowned_changed_size_rejects_before_any_selection_write(self):
        parsed = self.candidate()
        next(row for row in parsed['controls'] if row['kind'] == 'size')['value'] = 'L'
        result, _, http = self.prepare(parsed=parsed)
        self.assertNotEqual(result.state, 'completed')
        self.assertEqual(http.call_count, 0)
        self.assertEqual(result.reasons, ('source_selection_control_conflict:size',))
        self.assertEqual(self.before_action, self.after_action)
        self.assertNotIn('client_configuration_update', self.revision.action_receipts)

    def assert_source_conflict(self, parsed, field):
        result, _, http = self.prepare(parsed=parsed)
        self.assertEqual(result.reasons, ('source_selection_control_conflict:' + field,))
        self.assertEqual(http.call_count, 0)
        self.assertEqual(self.before_action, self.after_action)
        self.assertNotIn('client_configuration_update', self.revision.action_receipts)

    def test_same_product_catalog_color_cannot_replace_source_black(self):
        from productcolors.models import Color, ProductColorVariant
        from product_catalog.models import VariantSizeRule
        red = Color.objects.create(name='Red', primary_hex='#ff0000')
        variant = ProductColorVariant.objects.create(product=self.product, color=red, price_override=900, stock=0)
        VariantSizeRule.objects.create(variant=variant, fit_code='classic', size='M', stock=0, is_enabled=True)
        parsed = self.candidate()
        next(row for row in parsed['controls'] if row['kind'] == 'color_variant_id')['value'] = variant.pk
        self.assert_source_conflict(parsed, 'color')

    def test_catalog_compatible_quantity_cannot_replace_source_quantity(self):
        parsed = self.candidate()
        parsed['controls'].append({'kind': 'qty', 'value': 2})
        self.assert_source_conflict(parsed, 'qty')

    def test_catalog_compatible_fit_cannot_replace_source_fit(self):
        from storefront.models import ProductFitOption
        from product_catalog.models import VariantSizeRule
        ProductFitOption.objects.create(product=self.product, code='oversize', label='Oversize', is_active=True)
        VariantSizeRule.objects.create(variant=self.variant, fit_code='oversize', size='M', stock=0, is_enabled=True)
        parsed = self.candidate()
        parsed['controls'].append({'kind': 'fit', 'value': 'oversize'})
        self.assert_source_conflict(parsed, 'fit')

    def test_genuine_new_customer_size_l_is_preserved(self):
        parsed = self.candidate()
        parsed['reply_text'] = 'Ви обрали худі у розмірі L.'
        parsed['controls'] = [row for row in parsed['controls'] if row['kind'] != 'manager']
        next(row for row in parsed['controls'] if row['kind'] == 'size')['value'] = 'L'
        result, _, http = self.prepare(question='Хочу розмір L.', parsed=parsed)
        self.assertEqual(result.state, 'completed', (result.reasons, getattr(self, 'validation_reasons', ())))
        self.assertEqual(http.call_count, 1)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_size, 'L')
        self.assert_noop_receipt()
        self.assertEqual(self.before_action, self.after_action)

    def test_native_spaced_double_xl_control_matches_canonical_source(self):
        from product_catalog.models import VariantSizeRule
        grid = self.product.size_grid
        grid.guide_data['rows'].append({'size': 'XXL', 'display_size': 'XXL'})
        grid.save(update_fields=['guide_data'])
        VariantSizeRule.objects.create(variant=self.variant, fit_code='classic', size='XXL', stock=0, is_enabled=True)
        self.canonical('Хочу розмір XXL')
        parsed = self.candidate()
        parsed['reply_text'] = 'Зараз уточню у менеджера наявність худі Restock Aurora у чорному кольорі та розмірі XXL!'
        next(row for row in parsed['controls'] if row['kind'] == 'size')['value'] = '2 XL'
        result, _, http = self.prepare(question='Чи є це худі у розмірі XXL?', parsed=parsed)
        self.assertEqual(result.state, 'completed', (result.reasons, getattr(self, 'validation_reasons', ())))
        self.assertEqual(http.call_count, 1)
        self.assert_noop_receipt()
        self.assertEqual(self.before_action, self.after_action)

    def apply_fresh_product(self, current_product):
        from django.utils import timezone
        from management.services.ig_revision_authority import CLAIM_CATALOG_CONFIGURATION, build_revision_authority_bindings
        IgClient.objects.filter(pk=self.customer.pk).update(current_product_id=current_product)
        self.customer.refresh_from_db()
        self.source_row = self.source('Обираю це худі, M, чорний')
        self.revision, self.token = _RestockFixture.revision(self, self.source_row)
        authority = build_revision_authority_bindings(self.customer, claims=(CLAIM_CATALOG_CONFIGURATION,),
            control={'product': self.product.pk, 'variant': self.variant.pk, 'size': 'M', 'qty': 1},
            server_authorized_actions=('client_configuration_update',), settings_obj=self.settings)
        self.assertTrue(authority.ready, authority)
        from management.services.ig_commerce_projection import capture_current_selection_lines
        capture = capture_current_selection_lines(self.customer.pk)
        proposal = {'schema_version': 1, 'sources': [{'message_id': self.source_row.pk,
            'source_digest': self.revision.sources.get().source_digest, 'ordinal': 1}],
            'generation': {'request_id': 'fresh-selection-control', 'actual_model': 'fake',
                           'generated_at': timezone.now().isoformat()},
            'source_cart_capture': capture, 'authority': actions._authority_projection(authority)}
        self.revision.generation_proposal = proposal
        self.revision.generation_proposal_digest = actions._digest(proposal)
        self.revision.generation_proposed_at = timezone.now()
        self.revision.save(update_fields=['generation_proposal', 'generation_proposal_digest', 'generation_proposed_at', 'updated_at'])
        with patch.object(actions.bot_orders, 'pin_product', wraps=actions.bot_orders.pin_product) as pin:
            result = actions.apply_revision_selection_actions(self.revision.pk, self.token,
                source_message_id=self.source_row.pk, settings_id=self.settings.pk,
                settings_permission_epoch=self.settings.reply_permission_epoch, publication=self.binding,
                generation_proposal_digest=self.revision.generation_proposal_digest, authority=authority)
        self.assertTrue(result.applied, result.reasons)
        pin.assert_called_once()
        self.customer.refresh_from_db()
        self.revision.refresh_from_db()
        self.assertEqual(self.customer.current_product_id, self.product.pk)
        self.assertNotEqual(self.revision.action_receipts['client_configuration_update'].get('outcome'), 'no_op')
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutInventoryReservation.objects.count(), IgPaymentEvent.objects.count()), (0, 0, 0))

    def test_missing_durable_product_still_executes_real_fresh_pin(self):
        self.apply_fresh_product(None)

    def test_different_durable_product_still_executes_real_fresh_pin(self):
        self.apply_fresh_product(self.products[1].pk)
