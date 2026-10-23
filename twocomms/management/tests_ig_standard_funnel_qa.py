"""Ordinary stock/price continuations through sealed native replies, fake HTTP only."""
import json
from dataclasses import replace
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from management import tests_ig_revision_live as live
from management.tests_ig_restock_subscriptions import _RestockFixture
from management.models import IgFollowUpTask, IgBotNotification
from management.services.ig_response_control import parse_structured_response


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_TURN_CONTEXT_MODE='unified',
    IG_CLIENT_STATE_PROMPT_TOKENS=20000, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeStockContinuationTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True
    _execute = live.RevisionLiveTests._execute

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
            grid = case.products[0].size_grid
            grid.guide_data['rows'].append({'size': 'XXL', 'display_size': 'XXL'})
            grid.save(update_fields=['guide_data'])
            from product_catalog.models import VariantSizeRule
            VariantSizeRule.objects.create(variant=case.variants[0], fit_code='classic', size='XXL', stock=100, is_enabled=True)
        with patch.object(SourceCartCatalogTests, 'setUp', catalog_before_sources):
            super().setUp()
        self.settings.ai_enabled = True
        self.settings.save(update_fields=['ai_enabled'])
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count = 0
        self.actual_hash_override = 'observed'
        self.provider_failure = False

    def _generate(self, payload, **kwargs):
        # Retain the real plan created inside this execution; no fixture DTO
        # manufactures stock, catalogue, source-choice or payment authority.
        from management.services.ig_revision_live import RevisionGenerationBoundary
        return live.RevisionLiveTests._generate(self, payload, **kwargs)

    def answer(self, question, text, *, stock=0, controls=None):
        self.variant.stock = self.rule.stock = stock
        self.variant.save(update_fields=['stock'])
        self.rule.save(update_fields=['stock'])
        source = self.source(question)
        self.revision, self.token = _RestockFixture.revision(self, source)
        self.parsed = {'reply_text': text, 'controls': [{'kind': 'manager', 'value': True}] if controls is None else controls}
        with patch('management.services.ig_presence.start_presence', return_value=None):
            result, model, http = self._execute()
        self.revision.refresh_from_db()
        return result, model, http

    def assert_complete(self, result, model, http, *, case=True):
        coverage = self.revision.action_receipts.get('response_coverage', {})
        self.assertEqual(result.state, 'completed', (result.reasons, coverage))
        self.assertEqual((model.call_count, http.call_count), (1, 1))
        self.assertFalse(coverage.get('remaining'), coverage)
        self.assertEqual(self.revision.delivery_effects.filter(state='sent').count(), 1)
        if case:
            self.assertEqual(IgFollowUpTask.objects.filter(client=self.customer).count(), 1)
            self.assertEqual(IgFollowUpTask.objects.get(client=self.customer).reason, 'fulfillment_review')
            self.assertEqual(IgBotNotification.objects.filter(client=self.customer, event_type='fulfillment_review').count(), 1)
        replay, generated, transport = self._execute()
        self.assertEqual(replay.state, 'completed', replay.reasons)
        generated.assert_not_called()
        transport.assert_not_called()

    def test_zero_ledger_stock_question_and_source_owned_english_check_complete(self):
        self.assert_complete(*self.answer('Is this hoodie in size M in stock?',
            'I will check the availability of this hoodie in size M with our manager.', stock=0))

    def test_positive_ledger_stock_question_continues_without_manager(self):
        self.assert_complete(*self.answer('Is this hoodie in size M in stock?',
            'Restock Aurora, black, size M is in stock.', stock=100, controls=[]), case=False)

    def test_known_price_is_answered_under_same_pending_stock_review(self):
        self.assert_complete(*self.answer('Чи є це худі в розмірі M у наявності та яка його ціна?',
            'Ціна худі Restock Aurora — 900 грн. Уточню у менеджера наявність у розмірі M.'))

    def test_short_price_followup_has_one_current_sku_owner(self):
        result, model, http = self.answer('А ціна?', 'Ціна Restock Aurora — 900 грн.')
        self.assert_complete(result, model, http, case=False)

    def test_stock_q1_then_bare_price_q2_sends_each_once_and_keeps_one_review(self):
        self.assert_complete(*self.answer('Is this hoodie in size M in stock?',
            'I will check availability of this hoodie in size M with our manager.'))
        self.assert_complete(*self.answer('What is the price?', 'The price is 900 UAH.', controls=[]))
        from management.models import IgRevisionDeliveryEffect
        self.assertEqual(IgRevisionDeliveryEffect.objects.filter(revision__client=self.customer, state='sent').count(), 2)

    def test_current_price_answers_can_be_concise_values(self):
        for text in ('It is 900 UAH.', '900 грн.'):
            with self.subTest(text=text):
                self.assert_complete(*self.answer('What is the price?', text, controls=[]), case=False)

    def test_wrong_named_price_does_not_borrow_current_price(self):
        self.assert_complete(*self.answer('Is this hoodie in size M in stock?',
            'I will check availability of this hoodie in size M with our manager.'))
        result, _, http = self.answer('What is the price?', 'The price of Other Print is 900 UAH.', controls=[])
        self.assertEqual(result.state, 'blocked')
        self.assertEqual(http.call_count, 0)
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_cyrillic_selected_size_stock_question(self):
        self.assert_complete(*self.answer('А М є?', 'Уточню у менеджера наявність у розмірі M.'))

    def test_spaced_double_xl_stock_question_uses_only_chosen_size(self):
        self.canonical('Розмір XXL')
        self.assert_complete(*self.answer('Є худі 2 XL?', 'Уточню у менеджера наявність худі Restock Aurora у розмірі XXL.'))
        from management.services.ig_response_cart_plan import stock_question
        for text in ('А М є?', 'Є худі 2 XL?', 'Є худі два XL?', 'Є худі двііксель?', 'Є худі ххл?'):
            chosen = 'M' if text == 'А М є?' else 'XXL'
            self.assertTrue(stock_question(text, size=chosen))
            self.assertFalse(stock_question(text, size='L'))

    def test_short_ukrainian_stock_check(self):
        self.assert_complete(*self.answer('А M є?', 'Уточню у менеджера наявність у розмірі M.'))

    def test_black_hoodie_size_stock_check(self):
        self.assert_complete(*self.answer('Є чорне худі M?', 'Уточню у менеджера наявність худі Restock Aurora у розмірі M.'))

    def test_black_size_stock_check(self):
        self.assert_complete(*self.answer('Чорне M є?', 'Уточню у менеджера наявність у розмірі M.'))

    def test_native_capture_unknown_blue_keeps_its_configuration_review(self):
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_product_inquiries import product_inquiries_for_revision
        source = self.source('Синє M є?')
        revision, _ = _RestockFixture.revision(self, source)
        inquiry = product_inquiries_for_revision(revision,
            response_plan=capture_response_plan(self.customer, revision=revision))
        self.assertEqual(inquiry['reason'], 'product_configuration_review', inquiry)
        self.assertTrue(any(row['axis'] == 'color' and row['requested_value'] == 'blue'
            for row in inquiry['inquiries']), inquiry)

    def test_native_capture_known_black_preserves_independent_hardware_review(self):
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_product_inquiries import product_inquiries_for_revision
        source = self.source('Чорне M є? Які люверси на цьому худі?')
        revision, _ = _RestockFixture.revision(self, source)
        inquiry = product_inquiries_for_revision(revision,
            response_plan=capture_response_plan(self.customer, revision=revision))
        self.assertEqual(inquiry['reason'], 'product_information_review', inquiry)
        self.assertEqual({row['axis'] for row in inquiry['inquiries']}, {'information'})

    def test_native_nonactive_zero_black_and_active_unknown_blue_remain_separate(self):
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_product_inquiries import product_inquiries_for_revision
        from management.services.ig_turn_revisions import (create_collecting_revision,
            claim_revision_preparation, seal_revision, claim_sealed_revision)
        from management.models import IgCustomerTurn, IgTurnMessage
        blue = self.canonical('добавьте Storm, количество 1, размер L, цвет blue, крій classic')
        turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=blue,
            window_started_at=self.now, window_deadline=self.now)
        for ordinal, source in enumerate((self.choice, blue), 1):
            IgTurnMessage.objects.create(turn=turn, message=source, ordinal=ordinal, role='user')
        created = create_collecting_revision(turn, [self.choice, blue], now=self.now, bypass_quiet=True)
        self.assertIsNotNone(created.revision, created.reason)
        prepared = claim_revision_preparation(created.revision.pk, now=self.now)
        sealed = seal_revision(created.revision.pk, prepared.token, now=self.now)
        self.assertTrue(sealed.sealed, sealed.reason)
        claimed = claim_sealed_revision(sealed.revision.pk, now=self.now)
        self.assertIsNotNone(claimed.revision, claimed.reason)
        plan = capture_response_plan(self.customer, revision=claimed.revision)
        self.assertFalse(plan.plan_gap, plan.plan_gap)
        rows = plan.source_cart_capture['lines']
        self.assertEqual(len(rows), 2, rows)
        black_line = next(row for row in rows if row['fields']['product_id']['value'] == self.product.pk)
        blue_line = next(row for row in rows if row['fields']['product_id']['value'] == self.products[1].pk)
        self.assertEqual(plan.source_cart_capture['active_line_id'], blue_line['line_id'])
        self.assertEqual(black_line['fields']['color']['value'], 'black')
        self.assertEqual(blue_line['fields']['color']['value'], 'blue')
        black_plan = next(row.as_dict() for row in plan.line_plans if row.line_id == black_line['line_id'])
        self.assertEqual(black_plan['configuration']['catalog_color_identity']['variant_id'], self.variant.pk)
        self.assertNotIn('catalog_color_identity', json.dumps(plan.prompt_projection()))
        self.assertNotIn('signature', json.dumps(plan.prompt_projection()))
        inquiry = product_inquiries_for_revision(claimed.revision, response_plan=plan)
        colors = [(row['line_id'], row['requested_value']) for row in inquiry['inquiries'] if row['axis'] == 'color']
        self.assertEqual(colors, [(blue_line['line_id'], 'blue')], inquiry)
        self.assertEqual(inquiry['reason'], 'product_configuration_review', inquiry)
        from management.services.ig_response_cart_plan import ResponseLinePlan
        tampered = []
        for line in plan.line_plans:
            data = line.as_dict()
            if line.line_id == black_line['line_id']:
                data['configuration']['catalog_color_identity']['variant_id'] = self.variants[1].pk
            tampered.append(ResponseLinePlan(json.dumps(data)))
        replay = product_inquiries_for_revision(claimed.revision,
            response_plan=replace(plan, line_plans=tuple(tampered)))
        self.assertEqual({(row['line_id'], row['requested_value']) for row in replay['inquiries']
            if row['axis'] == 'color'}, {(black_line['line_id'], 'black'), (blue_line['line_id'], 'blue')}, replay)

    def test_english_have_size_stock_check(self):
        self.assert_complete(*self.answer('Do you have M?', 'I will check availability of Restock Aurora in size M with our manager.'))

    def test_english_size_available_stock_check(self):
        self.assert_complete(*self.answer('Is M available?', 'I will check availability of Restock Aurora in size M with our manager.'))

    def test_pending_stock_coverage_requires_own_exact_check_and_native_case(self):
        self.assert_complete(*self.answer('Is this hoodie in size M in stock?',
            'I will check availability of Restock Aurora in size M with our manager.'))
        from management.services.ig_response_plan import capture_response_plan, _pending_stock_answer_covered
        plan = capture_response_plan(self.customer, revision=self.revision)
        source_id = self.revision.bundle_snapshot['sources'][0]['message_id']
        self.assertTrue(plan.pending_stock_questions)
        self.assertTrue(_pending_stock_answer_covered(plan,
            'I will check availability of the "Restock Aurora" hoodie in size M with our manager.', source_id))
        for text in ('I will check with our manager.', 'I will not check availability with our manager.',
                'The manager said: I will check availability of this hoodie in size M with our manager.',
                '“Please confirm. I will check availability of this hoodie in size M with our manager.”',
                'I will check availability of the "Other Print" hoodie in size M with our manager.',
                'I will check availability of Other Print in size M with our manager.',
                'I will check availability of this T-shirt in size M with our manager.',
                'I will check availability of this hoodie in size L with our manager.',
                'This hoodie in size M is in stock.'):
            with self.subTest(text=text):
                self.assertFalse(_pending_stock_answer_covered(plan, text, source_id))
        self.assertFalse(_pending_stock_answer_covered(replace(plan, pending_stock_questions={}),
            'I will check availability with our manager.', source_id))
        self.assertFalse(_pending_stock_answer_covered(plan,
            'I will check availability with our manager.', source_id + 100))

    def test_price_anaphor_abstains_for_named_wrong_type_unknown_and_multiple_targets(self):
        self.assert_complete(*self.answer('Чи є це худі в розмірі M у наявності та яка його ціна?',
            'Ціна Restock Aurora — 900 грн. Уточню у менеджера наявність у розмірі M.'))
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_response_cart_plan import anaphoric_price_line_id, ResponseLinePlan
        plan = capture_response_plan(self.customer, revision=self.revision)
        active = plan.source_cart_capture['active_line_id']
        readiness = {active: plan.readiness_snapshot}
        def owner(text, lines=None):
            return anaphoric_price_line_id(text, plan.line_plans if lines is None else lines,
                active_line_id=active, readiness_by_line=readiness)
        self.assertEqual(owner('А ціна?'), active)
        self.assertEqual(owner('What is its price?'), active)
        for text in ('Яка ціна цього футболки?', 'What is the price of this T-shirt?',
                'What is the price of the Other Print hoodie?', 'What is the price of this мокко hoodie?',
                'What is the price of this hoodie in size L?', 'Can I collect it in person?',
                '“What is its price?”', 'The customer asked: What is its price?'):
            with self.subTest(text=text):
                self.assertFalse(owner(text))
        other = plan.line_plans[0].as_dict()
        other['line_id'] = 'another-line'
        self.assertFalse(owner('What is its price?', (*plan.line_plans, ResponseLinePlan(json.dumps(other)))))

    def test_mixed_unrelated_question_is_not_discharged_by_pending_stock_check(self):
        from management.services.ig_response_plan import capture_response_plan
        result, _, _ = self.answer('Is this hoodie in size M in stock? Can I collect it in person?',
            'I will check availability of Restock Aurora in size M with our manager.')
        plan = capture_response_plan(self.customer, revision=self.revision)
        self.assertFalse(plan.pending_stock_questions)
        self.assertNotEqual(result.state, 'completed')
        self.assertTrue(self.revision.action_receipts.get('response_coverage', {}).get('remaining')
            or result.state == 'blocked')


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativeColorSupersessionFallbackTests(TransactionTestCase):
    reset_sequences = True
    from management import tests_ig_stock_request_scope_20261023 as color_fixtures
    setUp = color_fixtures.NativeUnlistedProductRequestTests.setUp
    _message = color_fixtures.NativeUnlistedProductRequestTests._message
    _prepare = color_fixtures.NativeUnlistedProductRequestTests._prepare
    _execute = color_fixtures.NativeUnlistedProductRequestTests._execute
    _replace_bundle = color_fixtures.NativeUnlistedProductRequestTests._replace_bundle
    _generate = color_fixtures.NativeUnlistedProductRequestTests._generate
    _fresh = color_fixtures.NativeUnlistedProductRequestTests._fresh
    _select_product = color_fixtures.NativeUnlistedProductRequestTests._select_product

    def test_actual_server_inquiry_reply_acknowledges_replacement_color(self):
        self._select_product(1)
        self._fresh('Хочу худі кольору мокко')
        self._replace_bundle(['Хочу худі кольору мокко', 'Хочу худі кольору гірчичний'])
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_product_inquiries import product_inquiries_for_revision, inquiry_reply
        plan = capture_response_plan(self.customer, revision=self.revision)
        captured = product_inquiries_for_revision(self.revision, response_plan=plan)
        self.assertEqual(len(captured['superseded_color_wishes']), 1)
        self.parsed = {'reply_text': inquiry_reply(captured), 'controls': [{'kind': 'manager', 'value': True}]}
        result, model, http = self._execute()
        self.revision.refresh_from_db()
        coverage = self.revision.action_receipts.get('response_coverage', {})
        self.assertEqual(result.state, 'completed', (result.reasons, coverage))
        self.assertFalse(coverage.get('remaining'), coverage)
        self.assertEqual((model.call_count, http.call_count), (1, 1))
        self.assertEqual(self.revision.delivery_effects.filter(state='sent').count(), 1)
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.customer).count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(client=self.customer).count(), 1)
        from management.services.ig_response_plan import _superseded_color_wish_covered
        from types import SimpleNamespace
        old = captured['superseded_color_wishes'][0]['previous_source_message_id']
        pending = SimpleNamespace(control={'manager': True},
            reply_text='Уточню у менеджера можливість виконати це худі в кольорі «гірчичний».')
        self.assertTrue(_superseded_color_wish_covered(plan, pending, old))
        for reply in ('Уточню у менеджера можливість кольору мокко.',
                'Уточню у менеджера можливість гірчичний для Other Print.',
                '«Уточню у менеджера можливість кольору гірчичний.»',
                'Не уточню у менеджера можливість кольору гірчичний.'):
            pending.reply_text = reply
            self.assertFalse(_superseded_color_wish_covered(plan, pending, old))
