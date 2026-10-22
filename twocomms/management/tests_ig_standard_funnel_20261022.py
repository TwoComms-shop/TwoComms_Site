"""Golden ordinary retail conversations through native replies and fake HTTP.

Won photo identity uses the existing native image fixture. These tests check
continued selection, scope, coverage and delivery; they do not evaluate actual
model relevance or turn customer-reported payment into bank-confirmed money.
"""
import json

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management import tests_ig_product_focus as focus_fixtures
from management import tests_ig_revision_live as live_fixtures
from management.models import IgFollowUpTask
from management.services.ig_commerce_projection import capture_current_selection_lines


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class StandardPhotoRetailFunnelTests(TransactionTestCase):
    reset_sequences = True
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _generate = live_fixtures.RevisionLiveTests._generate
    _execute = live_fixtures.RevisionLiveTests._execute
    _replace_bundle = live_fixtures.RevisionLiveTests._replace_bundle
    configure_fit_catalog = focus_fixtures.ProductFocusTests.configure_fit_catalog

    def setUp(self):
        focus_fixtures.ProductFocusTests.setUp(self)
        self.configure_fit_catalog()
        grid = self.product.size_grid
        grid.guide_data['rows'].append({'size': 'XXL'})
        grid.save(update_fields=['guide_data'])

    def _fresh(self, *texts):
        focus_fixtures.ProductFocusTests.followup(self, texts[0])
        self.turn = self.revision.turn
        self.source = self.revision.sources.get().message
        if len(texts) > 1:
            self._replace_bundle(list(texts))
        self.actual_inline_count = 0

    def _reply(self, text, controls=()):
        self.parsed = {'reply_text': text, 'controls': list(controls)}
        result, generated, http = self._execute([(200, json.dumps({'message_id': f'golden-{self.revision.pk}'}))])
        self.revision.refresh_from_db()
        self.assertEqual(result.state, 'completed', (result.reasons, self.revision.action_receipts.get('response_coverage')))
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.delivery_effects.get().state, 'sent')
        self.assertEqual(generated.call_count, 1)
        self.assertFalse(self.revision.action_receipts['response_coverage']['remaining'])
        return result

    def _select_then_size(self, *, fit, fit_text, size_texts, fit_reply, size_reply):
        self._fresh(fit_text)
        self._reply(fit_reply, [{'kind': 'product', 'value': str(self.product.pk)}, {'kind': 'fit', 'value': fit}])
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_product_id, self.product.pk)
        self.assertFalse(self.customer.current_size)
        self._fresh(*size_texts)
        choice_source = self.source.pk
        self._reply(size_reply, [{'kind': 'size', 'value': 'XXL'}])
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.current_product_id, self.customer.current_size), (self.product.pk, 'XXL'))
        capture = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(capture['status'], 'captured')
        self.assertEqual(len(capture['lines']), 1)
        fields = capture['lines'][0]['fields']
        self.assertEqual(fields['product_id']['value'], self.product.pk)
        self.assertEqual(fields['fit_option_code']['value'], fit)
        self.assertEqual(fields['size']['value'], 'XXL')
        self.assertEqual(fields['size']['source']['source_message_id'], choice_source)
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_photo_regular_then_russian_xxl_burst_keeps_original_print(self):
        self._select_then_size(fit='classic', fit_text='Хочу классический крой',
            size_texts=('Размер два XL', 'Спасибо'),
            fit_reply='Для футболки «Правил немає» вы выбрали классический крой. Какой размер вам нужен?',
            size_reply='Для футболки «Правил немає» вы выбрали размер XXL и классический крой.')

    def test_photo_oversize_then_ukrainian_spelled_xxl_keeps_original_print(self):
        self._select_then_size(fit='oversize', fit_text='Обираю оверсайз',
            size_texts=('Розмір двііксель',),
            fit_reply='Ви обрали посадку оверсайз для футболки «Правил немає». Який розмір вам потрібен?',
            size_reply='Для футболки «Правил немає» ви обрали розмір XXL і посадку оверсайз.')

    def test_photo_regular_then_english_xxl_delivery_and_reported_payment(self):
        self._select_then_size(fit='classic', fit_text='I prefer regular fit.', size_texts=('Size 2XL',),
            fit_reply='You selected regular fit. Which size do you need?',
            size_reply='You selected size XXL and regular fit.')
        self._fresh('What is the price? When will you ship?')
        self._reply('The price is 900 UAH. Standard preparation for dispatch usually takes 1–3 days after payment.')
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.current_product_id, self.customer.current_size), (self.product.pk, 'XXL'))
        self._fresh('I paid. Is my payment confirmed?')
        self._reply('You reported a payment. Payment verification is pending; it is not yet confirmed.')
        self.customer.refresh_from_db()
        from management.services.bot_payment_truth import client_has_confirmed_purchase
        self.assertFalse(client_has_confirmed_purchase(self.customer))
        self.assertEqual((self.customer.current_product_id, self.customer.current_size), (self.product.pk, 'XXL'))

    def test_truthful_english_fit_for_product_ack_is_not_a_configuration_mismatch(self):
        self._fresh('I prefer regular fit.')
        self._reply('You selected regular fit for the «Правил немає» T-shirt. Which size do you need?',
            [{'kind': 'product', 'value': str(self.product.pk)}, {'kind': 'fit', 'value': 'classic'}])

    def test_truthful_english_regular_fit_ack_covers_current_choice(self):
        self._fresh('I prefer regular fit.')
        self._reply('You selected regular fit. Which size do you need?',
            [{'kind': 'product', 'value': str(self.product.pk)}, {'kind': 'fit', 'value': 'classic'}])


class RegularFitAcknowledgementScopeTests(SimpleTestCase):
    def test_regular_alias_does_not_cover_negated_quoted_or_different_fit(self):
        from management.services.ig_reply_truth import ReplyTruthContext
        from management.services.ig_response_control import parse_structured_response
        from management.services.ig_response_plan import build_response_plan
        plan = build_response_plan(preferences={'values': {'fit_option_code': 'classic'},
            'evidence': {'fit_option_code': {'source_message_id': 8}}},
            readiness={'missing': ['product']}, context=ReplyTruthContext(),
            sources=[{'message_id': 8, 'role': 'user', 'text': 'I prefer regular fit.'}])
        for text in ('You did not select regular fit.', 'You selected no regular fit.',
                'The customer wrote “You selected regular fit.”', 'You selected oversize fit.'):
            with self.subTest(text=text):
                self.assertIn('8:fit_option_code', plan.coverage(
                    parse_structured_response({'reply_text': text, 'controls': []}))['remaining'])

    def test_regular_alias_is_limited_to_exact_named_cart_line(self):
        from management.tests_ig_response_cart_plan import ResponseCartPlanTests
        fixture = ResponseCartPlanTests()
        capture = fixture.capture(fields=[
            {'product_id': 55, 'fit_option_code': 'classic'},
            {'product_id': 56, 'fit_option_code': 'classic'}])
        plan = fixture.plan(capture)
        self.assertEqual(capture['active_line_id'], 'second')
        coverage = plan.coverage(fixture.response('You selected regular fit for Classic.'))
        self.assertIn('8:first:fit_option_code', coverage['covered'])
        self.assertIn('8:second:fit_option_code', coverage['remaining'])
        generic = plan.coverage(fixture.response('You selected regular fit.'))
        self.assertIn('8:first:fit_option_code', generic['remaining'])
        self.assertIn('8:second:fit_option_code', generic['remaining'])


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class StandardGreetingTests(TransactionTestCase):
    reset_sequences = True
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _generate = live_fixtures.RevisionLiveTests._generate
    _execute = live_fixtures.RevisionLiveTests._execute
    _replace_bundle = live_fixtures.RevisionLiveTests._replace_bundle
    setUp = live_fixtures.RevisionLiveTests.setUp

    def _greeting(self, source, reply):
        self._replace_bundle([source])
        self.parsed = {'reply_text': reply, 'controls': []}
        result, generated, http = self._execute()
        self.assertEqual(result.state, 'completed', result.reasons)
        self.assertEqual(generated.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.delivery_effects.get().state, 'sent')
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_ukrainian_greeting_has_ordinary_delivered_reply(self):
        self._greeting('Привіт!', 'Вітаю! Чим можу допомогти?')

    def test_russian_greeting_has_ordinary_delivered_reply(self):
        self._greeting('Здравствуйте!', 'Здравствуйте! Чем могу помочь?')

    def test_english_greeting_has_ordinary_delivered_reply(self):
        self._greeting('Hello!', 'Hello! How can I help?')
