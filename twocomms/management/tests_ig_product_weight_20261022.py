"""Exact garment mass stays unknown; fabric GSM is independent authority."""
import json

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management.models import IgFollowUpTask
from management.services import ig_product_inquiries as inquiries
from management.services.approved_public_facts import approved_base_assertion, requested_product_topics
from management.services.ig_response_control import parse_structured_response
from management import tests_ig_live_conversation_contract_20261022 as contract
from management import tests_ig_revision_live as fixtures


class GarmentWeightSourceTests(SimpleTestCase):
    def capture(self, text):
        return inquiries.capture_product_inquiries(
            [{'message_id': 11, 'role': 'user', 'text': text, 'source_digest': 'a' * 64}],
            cart_capture={'active_line_id': 'hoodie', 'lines': [{'line_id': 'hoodie', 'product_id': 12}]},
            readiness_by_line={'hoodie': {'product': {'id': 12, 'category_slug': 'hoodies'}}})

    def test_ua_ru_en_weight_questions_resolve_exact_owner_policy(self):
        for text in ('Яка точна вага цього худі?', 'Скільки важить це худі?',
                     'Какой точный вес этого худи?', 'Сколько весит это худи?',
                     'What is the exact weight of this hoodie?', 'How much does this hoodie weigh?'):
            with self.subTest(text=text):
                captured = self.capture(text)
                self.assertEqual(captured['status'], 'none')
                row, = captured['resolved_information']
                self.assertEqual(row['requested_topic'], 'weight')
                self.assertEqual(row['question'], text)
                self.assertEqual(row['product_id'], 12)
                self.assertFalse(row['catalog_authority'])
                self.assertFalse(row['checkout_authority'])
                self.assertNotIn('gsm', row['assertion'])
                self.assertNotIn('grams', row['assertion']['text'])

    def test_gsm_is_a_separate_topic_and_never_mass_authority(self):
        for question in ('What is the weight and density of this hoodie?',
                         'Яка вага та щільність цього худі?', 'Какой вес и плотность этого худи?'):
            with self.subTest(question=question):
                self.assertEqual(set(requested_product_topics(question)), {'weight', 'density'})
                assertion = approved_base_assertion({'product': {'id': 12, 'category_slug': 'hoodies'}},
                    question=question, topic='weight', language='en')
                self.assertEqual(assertion['schema'], 'approved-weight-policy-assertion.v1')
                self.assertNotIn('gsm', assertion)
                captured = self.capture(question)
                self.assertEqual(captured['inquiries'], [])
                self.assertEqual({row['requested_topic'] for row in captured['resolved_information']}, {'density', 'weight'})

    def test_polite_comma_requests_keep_exact_source_and_sibling_specs(self):
        from management.services.ig_response_plan import _requested_topics
        for text, topic in (
            ('И уточните, пожалуйста, точный вес худи', 'weight'),
            ('І уточніть, будь ласка, точну вагу худі', 'weight'),
            ('Please check, the exact garment weight', 'weight'),
            ('Уточните, пожалуйста, состав худи', 'composition'),
            ('Уточніть, будь ласка, люверси худі', None),
        ):
            with self.subTest(text=text):
                captured = self.capture(text)
                row, = captured['inquiries'] + captured['resolved_information']
                self.assertEqual(row['question'], text)
                self.assertEqual(row.get('requested_topic'), topic)
                self.assertIn('info:' + ('material' if topic == 'composition' else topic or 'question'),
                              _requested_topics(text + '?'))
        text = 'По люверсам ещё жду ответ. И уточните, пожалуйста, точный вес худи. А сколько стоит выбранное худи?'
        row, = self.capture(text)['resolved_information']
        self.assertEqual(row['question'], 'И уточните, пожалуйста, точный вес худи')
        self.assertEqual(row['requested_topic'], 'weight')
        self.assertTrue({'info:weight', 'info:price'} <= set(_requested_topics(text)))
        self.assertNotIn('info:weight', _requested_topics('Уточните цену. Вага 80 кг, беру M'))

    def test_direct_weight_policy_covers_weight_and_not_density(self):
        captured = self.capture('What is the exact weight of this hoodie?')
        response = parse_structured_response({'reply_text': 'We do not weigh finished garments separately, so we do not have an exact garment weight.',
                                             'controls': [{'kind': 'manager', 'value': True}]})
        self.assertEqual(inquiries.validate_inquiry_response(captured, response), '')
        self.assertFalse(inquiries.inquiry_handoff_topic_covered('info:weight', response, captured, source_message_id=11))
        self.assertTrue(inquiries.approved_product_topic_covered('info:weight', response.reply_text, captured))
        self.assertFalse(inquiries.inquiry_handoff_topic_covered('info:density', response, captured, source_message_id=11))
        density = parse_structured_response({'reply_text': 'This base is approximately 320 gsm.',
                                            'controls': [{'kind': 'manager', 'value': True}]})
        self.assertFalse(inquiries.inquiry_handoff_topic_covered('info:weight', density, captured, source_message_id=11))

    def test_numeric_mass_cannot_be_invented_behind_a_check_commitment(self):
        captured = self.capture('What is the exact weight of this hoodie?')
        for text in ('The hoodie weighs 600 grams. I will check with our manager.',
                     'Вага худі 600 г. Уточню у менеджера.', 'Вес худи 600 г. Уточню у менеджера.',
                     'Уточню вес. Он весит 320 г.', 'I will check the weight. It is 320 g.',
                     'I will check the weight; it weighs 600 g.',
                     'I will check the weight, around 600 g.', 'I will check the weight, around 0.6 kg.'):
            with self.subTest(text=text):
                response = parse_structured_response({'reply_text': text, 'controls': [{'kind': 'manager', 'value': True}]})
                self.assertEqual(inquiries.validate_inquiry_response(captured, response), 'product_information_unverified')

    def test_guard_is_scoped_to_unknown_garment_mass_and_preserves_quotes_and_prices(self):
        captured = self.capture('What is the exact weight of this hoodie?')
        for text in ('I will check the exact weight. The price is 1490 UAH.',
                     'I will check the exact weight. You asked “Is it 600 g?”',
                     'I will check the exact weight. Does it weigh 600 g?'):
            with self.subTest(text=text):
                response = parse_structured_response({'reply_text': text, 'controls': [{'kind': 'manager', 'value': True}]})
                self.assertEqual(inquiries.validate_inquiry_response(captured, response), '')
        response = parse_structured_response({'reply_text': 'I will check the exact weight. Your weight is 80 kg.',
                                             'controls': [{'kind': 'manager', 'value': True}]})
        self.assertEqual(inquiries.validate_inquiry_response(captured, response), '')

    def test_history_quotes_rejection_and_body_weight_are_not_garment_mass_questions(self):
        for text in ('Раніше питав яка вага цього худі', 'Вчера спрашивал вес худи',
                     'Не цікавить вага худі', 'Вес худи не интересует',
                     'В описі написано «вага худі 600 г»', 'Моя вага 80 кг, який розмір худі?',
                     'Мой вес 80 кг, какой размер худи?', 'I weigh 80 kg, which hoodie size fits?'):
            with self.subTest(text=text):
                captured = self.capture(text)
                self.assertFalse(any(row.get('requested_topic') == 'weight' for row in captured['inquiries'] + captured['resolved_information']))

    def test_body_measurements_are_sizing_inputs_while_explicit_garment_weight_is_policy(self):
        from management.services.ig_response_plan import _requested_topics
        for text in ('Зріст 180, вага 80 кг, підкажіть розмір худі',
                     'Рост 180 вес 80 кг подскажите размер худи',
                     'У мене вага 80 кг, який розмір худі?', 'I am 180cm, 80kg, help with hoodie size'):
            with self.subTest(text=text):
                self.assertNotIn('info:weight', _requested_topics(text))
                self.assertNotIn('weight', requested_product_topics(text))
                self.assertFalse(any(row.get('requested_topic') == 'weight' for row in self.capture(text)['resolved_information']))
        for text in ('Точна вага худі 600 г?', 'Is the weight of this hoodie 600 grams?'):
            with self.subTest(text=text):
                self.assertIn('weight', requested_product_topics(text))

    def test_fabric_weight_is_density_and_unrelated_decline_does_not_erase_mass_question(self):
        from management.services.ig_response_plan import _requested_topics
        for text in ('What is the fabric weight of this hoodie?', 'Уточніть вагу тканини худі',
                     'Какой вес ткани худи?'):
            with self.subTest(text=text):
                self.assertEqual(requested_product_topics(text), ('density',))
                self.assertIn('info:density', _requested_topics(text))
                self.assertNotIn('info:weight', _requested_topics(text))
        for text in ('Не хочу regular, уточните вес худи.',
                     'Не интересна цена, а точный вес худи уточните.',
                     'Не цікавить ціна, а точну вагу худі уточніть.'):
            with self.subTest(text=text):
                self.assertIn('info:weight', _requested_topics(text))
                self.assertNotIn('info:price', _requested_topics(text))

    def test_owner_hoodie_density_has_no_blanket_numeric_authority(self):
        from management.knowledge.bases import BASES
        from management.services.ig_reply_truth import validate_approved_product_claims
        self.assertIsNone(BASES['current_hoodie']['gsm'])
        captured = self.capture('What is the density of this hoodie?')
        self.assertEqual(captured['status'], 'none')
        self.assertTrue(inquiries.approved_product_topic_covered('info:density', 'Density depends on the specific base.', captured))
        assertion, = [row['assertion'] for row in captured['resolved_information']]
        for number in (320, 350, 380):
            self.assertTrue(validate_approved_product_claims(f'This hoodie is {number} gsm.', (assertion,)))

    def test_generic_question_uses_each_resolved_policy_topic_without_dropping_debt(self):
        density = 'Density depends on the specific base.'
        weight = 'We do not weigh finished garments separately, so we do not have an exact garment weight.'
        for question, answer in (('What is the density of this hoodie?', density),
                                 ('What is the weight of this hoodie?', weight)):
            with self.subTest(question=question):
                captured = self.capture(question)
                self.assertTrue(inquiries.approved_product_topic_covered('info:question', answer, captured))
                self.assertFalse(inquiries.approved_product_topic_covered('info:question', 'I will check with our manager.', captured))
        captured = self.capture('What is the weight and density of this hoodie?')
        self.assertTrue(inquiries.approved_product_topic_covered('info:question', weight + ' ' + density, captured))
        self.assertFalse(inquiries.approved_product_topic_covered('info:question', density, captured))
        self.assertFalse(inquiries.approved_product_topic_covered('info:question', weight, captured))

    def test_policy_proof_is_recomputed_and_cannot_gain_numeric_authority(self):
        from management.services.approved_public_facts import approved_assertion_valid
        assertion = self.capture('What is the hoodie weight?')['resolved_information'][0]['assertion']
        self.assertTrue(approved_assertion_valid(assertion))
        for extra in ({'gsm': 320}, {'mass_g': 600}, {'text': 'This hoodie weighs 600 grams.'}):
            self.assertFalse(approved_assertion_valid({**assertion, **extra}))

    def test_unreviewed_base_retains_proven_sku_without_granting_composition(self):
        source = {'message_id': 11, 'role': 'user', 'text': 'What is the composition of this hoodie?', 'source_digest': 'a' * 64}
        for title, expected_product, expected_line in (('Contract hoodie 1', 12, 'selected'),
                                                       ('Contract T-shirt 1', None, '')):
            with self.subTest(title=title):
                captured = inquiries.capture_product_inquiries([source],
                    cart_capture={'active_line_id': 'selected', 'lines': [{'line_id': 'selected', 'product_id': 12}]},
                    readiness_by_line={'selected': {'product': {'id': 12, 'category_slug': 'unreviewed-base', 'title': title}}})
                self.assertEqual(captured['status'], 'manager_required')
                row, = captured['inquiries']
                self.assertEqual(row['product_id'], expected_product)
                self.assertEqual(row['line_id'], expected_line)
                self.assertFalse(row['catalog_authority'])
                self.assertEqual(captured['resolved_information'], [])
                self.assertEqual(captured['base_context_assertions'], [])

    def test_unreviewed_selected_sku_can_use_brand_weight_policy_without_base_facts(self):
        captured = inquiries.capture_product_inquiries(
            [{'message_id': 11, 'role': 'user', 'text': 'What is the weight of this hoodie?', 'source_digest': 'a' * 64}],
            cart_capture={'active_line_id': 'selected', 'lines': [{'line_id': 'selected', 'product_id': 12}]},
            readiness_by_line={'selected': {'product': {'id': 12, 'category_slug': 'unreviewed-base', 'title': 'Contract hoodie 1'}}})
        self.assertEqual(captured['status'], 'none')
        policy, = captured['resolved_information']
        self.assertEqual(policy['product_id'], 12)
        self.assertEqual(policy['assertion']['schema'], 'approved-weight-policy-assertion.v1')
        self.assertEqual(captured['base_context_assertions'], [])

    def test_weight_policy_is_not_material_authority_for_a_following_sentence(self):
        captured = inquiries.capture_product_inquiries([
            {'message_id': 11, 'role': 'user', 'text': 'What is the weight of this hoodie?', 'source_digest': 'a' * 64}])
        self.assertEqual(captured['status'], 'none')
        self.assertEqual(captured['base_context_assertions'], [])
        response = parse_structured_response({'reply_text': 'We do not weigh finished garments separately. It uses fleece.', 'controls': []})
        self.assertEqual(inquiries.validate_inquiry_response(captured, response), 'product_information_unverified')

    def test_spec_imperative_names_subject_without_selecting_preferences(self):
        from management.services.ig_commerce_turns import parse_turn
        for text in ('И уточните, пожалуйста, точный вес худи.',
                     'І уточніть, будь ласка, вагу худі розміру L.',
                     'Please check the weight of the black hoodie size L.',
                     'Уточніть склад футболки.'):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertFalse(request.garment_type)
                self.assertFalse(request.field_updates)
        for text in ('Беру худі L, уточніть його вагу.', 'Додай худі L, уточніть його вагу.',
                     'Уточніть вагу худі, беру L.', 'I choose hoodie size L, please check its weight.'):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual((request.line_operations[0].field_updates if request.line_operations else request.field_updates).get('size'), 'L')
        request = parse_turn('Беру худі L, уточніть вагу футболки.')
        self.assertEqual(request.garment_type, 'hoodie')


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativeGarmentWeightTests(TransactionTestCase):
    reset_sequences = True
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare
    _execute = fixtures.RevisionLiveTests._execute
    _replace_bundle = fixtures.RevisionLiveTests._replace_bundle
    _generate = contract.NativeLiveConversationContractTests._generate
    _fresh = contract.NativeLiveConversationContractTests._fresh
    _answer = contract.NativeLiveConversationContractTests._answer
    _select_product = contract.NativePendingProductInformationBoundaryTests._select_product

    def _select_hoodie(self):
        from storefront.models import Category
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        product = self._select_product(1)
        category, _ = Category.objects.get_or_create(slug='hoodies', defaults={'name': 'Hoodies'})
        product.category = category
        product.price = 1490
        product.save(update_fields=['category', 'price'])
        product.color_variants.update(price_override=1490)
        self.customer.refresh_from_db()
        return product

    def _sequence(self, locale):
        question_one, answer_one, question_two, answer_two, question_three, answer_three = {
            'en': ('What eyelets does this hoodie have?', 'I will check the eyelets with our manager.',
                   'What is the exact weight of this hoodie?', 'We do not weigh finished garments separately, so we do not have an exact garment weight.',
                   'What is the price of this hoodie?', 'This hoodie costs 1490 UAH.'),
            'ru': ('Какие люверсы на этом худи?', 'Я уточню люверсы у менеджера.',
                   'И уточните, пожалуйста, точный вес худи.', 'Готовые вещи отдельно не взвешиваем, поэтому точного значения веса нет.',
                   'Сколько стоит выбранное худи?', 'Выбранное худи стоит 1490 грн.'),
            'uk': ('Які люверси на цьому худі?', 'Я уточню люверси у менеджера.',
                   'І уточніть, будь ласка, точну вагу худі.', 'Готові речі окремо не зважуємо, тому точного значення ваги немає.',
                   'Скільки коштує вибране худі?', 'Вибране худі коштує 1490 грн.'),
        }[locale]
        product = self._select_hoodie()
        self.customer.language = locale
        self.customer.save(update_fields=['language'])
        self._fresh(question_one)
        first = self.source
        self._answer(answer_one, manager=True)
        task = IgFollowUpTask.objects.get(reason='revision_case:product_information_review')
        self._fresh(question_two)
        second = self.source
        self._answer(answer_two)
        task.refresh_from_db()
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual({row['message_id'] for row in task.manager_context['sources']}, {first.pk})
        text = json.dumps(task.manager_context, ensure_ascii=False)
        self.assertIn(question_one, text)
        self.assertNotIn(question_two.rstrip('.'), text)
        self.assertNotIn('"requested_topic": "weight"', text)
        policy = self.revision.action_receipts['response_coverage']['product_inquiries']['resolved_information']
        self.assertEqual([row['requested_topic'] for row in policy], ['weight'])
        self.assertNotIn('320', self.revision.delivery_effects.get().payload['message']['text'])
        self._fresh(question_three)
        self._answer(answer_three)
        task.refresh_from_db()
        self.assertNotIn(task.status, ('completed', 'cancelled'))
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual({row['message_id'] for row in task.manager_context['sources']}, {first.pk})
        self.assertEqual(self.customer.current_product_id, product.pk)

    def test_eyelets_case_stays_open_while_weight_policy_and_known_price_send_independently(self):
        self._sequence('en')

    def test_ru_polite_comma_weight_then_known_price(self):
        self._sequence('ru')

    def test_ua_polite_comma_weight_then_known_price(self):
        self._sequence('uk')

    def test_native_generic_followup_in_same_bundle_covers_known_density_policy(self):
        self._select_hoodie()
        self._fresh('What is the density of this hoodie?')
        self._replace_bundle(['What is the density of this hoodie?', 'What can you tell me?'])
        self._answer('Density depends on the specific base.')
        coverage = self.revision.action_receipts['response_coverage']
        self.assertTrue(any(value.endswith(':info:question') for value in coverage['covered']))
        self.assertTrue(any(value.endswith(':info:density') for value in coverage['covered']))
        self.assertEqual(coverage['remaining'], [])

    def test_native_fabric_gsm_cannot_answer_or_discharge_unknown_weight(self):
        self._select_hoodie()
        self._fresh('What is the exact weight of this hoodie?')
        self.parsed = {'reply_text': 'This base is approximately 320 gsm.',
                       'controls': [{'kind': 'manager', 'value': True}]}
        self._expected_rejection = 'unverified_product_material'
        result, _, http = self._execute()
        self.assertEqual(result.state, 'blocked', result.reasons)
        self.assertIn(self._expected_rejection, self._observed_rejection)
        http.assert_not_called()

    def test_native_check_commitment_cannot_launder_numeric_mass(self):
        self._select_hoodie()
        self._fresh('What is the exact weight of this hoodie?')
        self.parsed = {'reply_text': 'I will check the weight. It is 600 g.',
                       'controls': [{'kind': 'manager', 'value': True}]}
        self._expected_rejection = 'product_information_unverified'
        result, _, http = self._execute()
        self.assertEqual(result.state, 'blocked', result.reasons)
        self.assertIn(self._expected_rejection, self._observed_rejection)
        http.assert_not_called()

    def test_native_weight_policy_cannot_grant_fleece_to_an_unselected_garment(self):
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        self._fresh('What is the weight of this hoodie?')
        self.parsed = {'reply_text': 'We do not weigh finished garments separately. It uses fleece.', 'controls': []}
        self._expected_rejection = 'unverified_product_material'
        result, _, http = self._execute()
        self.assertEqual(result.state, 'blocked', result.reasons)
        self.assertIn(self._expected_rejection, self._observed_rejection)
        http.assert_not_called()

    def test_actual_ru_candidate_does_not_turn_gsm_into_whole_garment_weight(self):
        self._select_hoodie()
        self.customer.language = 'ru'
        self.customer.save(update_fields=['language'])
        self._fresh('И уточните, пожалуйста, точный вес худи.')
        self.parsed = {'reply_text': 'Точный вес худи составляет около 320 г/м² (трехнитка пенье флис).',
                       'controls': [{'kind': 'manager', 'value': True}]}
        self._expected_rejection = 'unverified_product_material'
        result, _, http = self._execute()
        self.assertEqual(result.state, 'blocked', result.reasons)
        self.assertIn(self._expected_rejection, self._observed_rejection)
        http.assert_not_called()

    def test_selected_shirt_is_preserved_when_customer_asks_hoodie_weight(self):
        from storefront.models import Category
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import parse_turn
        from management.services.ig_commerce_projection import capture_current_selection_lines
        product = self._select_product(1)
        category, _ = Category.objects.get_or_create(slug='tshirts', defaults={'name': 'T-shirts'})
        product.category = category
        product.save(update_fields=['category'])
        self.customer.refresh_from_db()
        before = capture_current_selection_lines(self.customer.pk)
        source = self._message('І уточніть, будь ласка, точну вагу худі.', 'different-garment-weight')
        request = parse_turn(source.text)
        self.assertFalse(request.garment_type)
        self.assertFalse(request.field_updates)
        apply_turn(self.customer, source, request, reply_payload={})
        self.customer.refresh_from_db()
        after = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(after['active_line_id'], before['active_line_id'])
        self.assertEqual([row['fields'] for row in after['lines']], [row['fields'] for row in before['lines']])
        self.assertEqual(self.customer.current_product_id, product.pk)
        captured = inquiries.capture_product_inquiries(
            [{'message_id': source.pk, 'role': 'user', 'text': source.text, 'source_digest': 'a' * 64}],
            cart_capture=after,
            readiness_by_line={after['active_line_id']: {'product': {'id': product.pk, 'category_slug': 'tshirts'}}})
        policy, = captured['resolved_information']
        self.assertIsNone(policy['product_id'])
        self.assertEqual(policy['line_id'], '')
