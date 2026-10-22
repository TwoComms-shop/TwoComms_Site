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

ACTUAL_REGULAR_XXL_REPLY = ('Got it! I\'ve noted your selection: the "Воля" T-shirt in a regular fit and size XXL. '
    'Let me know if you are ready to proceed or if you need help with anything else!')
ACTUAL_REGULAR_XXL_SOURCE = 'Please reply in English. Keep my selected Воля T-shirt in regular fit, size XXL. Just confirm this selection for me.'


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

    def _actual_selection_confirmation(self):
        self.product.title = 'Воля'
        self.product.save(update_fields=['title'])
        self._select_then_size(fit='classic', fit_text='I prefer regular fit.', size_texts=('Size 2XL',),
            fit_reply='You selected regular fit. Which size do you need?',
            size_reply='You selected size XXL and regular fit.')
        self._fresh(ACTUAL_REGULAR_XXL_SOURCE)

    def test_actual_english_fit_and_size_confirmation_is_sent_without_controls(self):
        self._actual_selection_confirmation()
        self._reply(ACTUAL_REGULAR_XXL_REPLY)
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.current_product_id, self.customer.current_size), (self.product.pk, 'XXL'))

    def test_actual_foreign_size_and_fit_are_rejected_before_send(self):
        self._actual_selection_confirmation()
        for index, reply in enumerate((ACTUAL_REGULAR_XXL_REPLY.replace('size XXL', 'size L'),
                ACTUAL_REGULAR_XXL_REPLY.replace('regular fit', 'oversize fit'))):
            with self.subTest(reply=reply):
                if index:
                    self._fresh(ACTUAL_REGULAR_XXL_SOURCE)
                self.parsed = {'reply_text': reply, 'controls': []}
                result, _, http = self._execute()
                self.assertEqual(result.state, 'blocked')
                self.assertEqual(http.call_count, 0)
                self.assertFalse(self.revision.delivery_effects.exists())

    def test_actual_wrong_title_or_reply_language_never_completes_confirmation(self):
        self._actual_selection_confirmation()
        for index, (reply, debt) in enumerate(((ACTUAL_REGULAR_XXL_REPLY.replace('Воля', 'Мрія'), 'product_id'),
                ('Я записала ваш выбор: футболка «Воля», классический крой и размер XXL. Если нужна помощь, напишите нам.', 'info:reply_language'))):
            with self.subTest(debt=debt):
                if index:
                    self._fresh(ACTUAL_REGULAR_XXL_SOURCE)
                self.parsed = {'reply_text': reply, 'controls': []}
                result, _, http = self._execute()
                self.assertNotEqual(result.state, 'completed')
                self.revision.refresh_from_db()
                if http.call_count:
                    self.assertIn(f'{self.source.pk}:{debt}',
                        self.revision.action_receipts['response_coverage']['remaining'])
                self.customer.refresh_from_db()
                self.assertEqual((self.customer.current_product_id, self.customer.current_size), (self.product.pk, 'XXL'))


class RegularFitAcknowledgementScopeTests(SimpleTestCase):
    def test_english_filler_or_manager_deferral_cannot_cover_a_language_only_concern(self):
        from management.services.ig_reply_truth import ReplyTruthContext
        from management.services.ig_response_control import parse_structured_response
        from management.services.ig_response_plan import build_response_plan
        for question in ('Do you speak English?', 'sorry, I dont speak the language'):
            plan = build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(),
                sources=[{'message_id': 8, 'role': 'user', 'text': question}])
            for reply in ('I will check with our manager.', 'Hello! How can I help?',
                    'Thank you for your message. We will review your request.'):
                with self.subTest(question=question, reply=reply):
                    self.assertIn('8:info:reply_language', plan.coverage(parse_structured_response(
                        {'reply_text': reply, 'controls': []}))['remaining'])
            self.assertEqual(plan.coverage(parse_structured_response(
                {'reply_text': 'Yes, I can reply in English.', 'controls': []}))['remaining'], [])

    def test_exact_quoted_selected_title_and_english_prose_cover_their_own_obligations(self):
        from management.services.ig_reply_truth import ReplyTruthContext
        from management.services.ig_response_control import parse_structured_response
        from management.services.ig_response_plan import build_response_plan
        values = {'product_id': 55, 'fit_option_code': 'classic', 'size': 'XXL', 'garment_type': 'tshirt'}
        plan = build_response_plan(preferences={'values': values,
            'evidence': {key: {'source_message_id': 8} for key in values}},
            readiness={'has_product': True, 'product': {'id': 55, 'title': 'Воля'}, 'missing': []},
            context=ReplyTruthContext(), sources=[{'message_id': 8, 'role': 'user',
                'text': 'Please reply in English. Keep my selected Воля T-shirt in regular fit, size XXL. Just confirm this selection for me.'}])
        def coverage(text):
            return plan.coverage(parse_structured_response({'reply_text': text, 'controls': []}))
        self.assertEqual(coverage(ACTUAL_REGULAR_XXL_REPLY)['remaining'], [])
        self.assertIn('8:info:reply_language', coverage('I will check with our manager.')['remaining'])
        for text in (ACTUAL_REGULAR_XXL_REPLY.replace('Воля', 'Мрія'),
                'The customer wrote “I noted your selection: the Воля T-shirt.”',
                'The customer wrote “Please confirm. I noted your selection: the "Воля" T-shirt.”',
                'The customer wrote: I noted your selection: the "Воля" T-shirt.',
                'The manager said: I noted your selection: the "Воля" T-shirt.',
                'The bot wrote: I noted your selection: the "Воля" T-shirt.',
                'The assistant said: I noted your selection: the "Воля" T-shirt.',
                'I did not note your selection of the "Воля" T-shirt.'):
            with self.subTest(text=text):
                self.assertIn('8:product_id', coverage(text)['remaining'])
        self.assertIn('8:info:reply_language', coverage(
            'Я записала ваш выбор: футболка «Воля», классический крой и размер XXL. Если нужна помощь, напишите нам.')['remaining'])

    def test_actual_fit_and_size_sentence_uses_fit_and_size_authority_independently(self):
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
        context = ReplyTruthContext(allowed_fits=('regular',), allowed_sizes=('XXL',),
            source_chosen_fits=('regular',), source_chosen_sizes=('XXL',))
        self.assertTrue(validate_reply_truth(ACTUAL_REGULAR_XXL_REPLY, context=context).valid)
        for reply in ('You selected regular fit and size XXL.',
                'You selected regular fit and the size is XXL.',
                'You selected regular fit in XXL.', 'You selected regular fit   in XXL.',
                'You selected regular fit in size XXL.',
                'You selected regular fit, size XXL.',
                'You selected regular fit for this T-shirt, size XXL.',
                'You selected regular fit with size XXL.',
                'You selected regular fit of this T-shirt, size XXL.',
                'You selected the regular fit version, size XXL.'):
            with self.subTest(reply=reply):
                self.assertTrue(validate_reply_truth(reply, context=context).valid)
        for reply in (ACTUAL_REGULAR_XXL_REPLY.replace('regular fit', 'oversize fit'),
                ACTUAL_REGULAR_XXL_REPLY.replace('size XXL', 'size L'),
                ACTUAL_REGULAR_XXL_REPLY.replace('regular fit', 'mystery fit'),
                'You selected fit: mystery and size XXL.', 'You selected regular fit: slim and size XXL.',
                'You selected regular fit in L.', 'You selected regular fit   in L.',
                'You selected oversize fit in XXL.'):
            with self.subTest(reply=reply):
                self.assertIn('configuration_mismatch', validate_reply_truth(reply, context=context).reasons)

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
