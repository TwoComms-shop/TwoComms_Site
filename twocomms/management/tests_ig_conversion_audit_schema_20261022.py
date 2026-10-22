"""Pure optional analysis schema: source attribution is not causal authority."""
from copy import deepcopy
import json

from django.test import SimpleTestCase

from management.services.ig_conversion_audit import (
    MAX_JSON_CHARS, normalize_conversion_audit,
)


class ConversionAuditSchemaTests(SimpleTestCase):
    def setUp(self):
        self.sources = {
            10: {'role': 'user', 'text': 'Не купую, бо ціна зависока. Це мав бути подарунок.'},
            11: {'role': 'manager', 'text': 'Не купую, бо ціна зависока.'},
            12: {'role': 'model', 'text': 'Не купую, бо ціна зависока.'},
            13: {'role': 'user', 'text': 'Дорого. Подумаю.'},
            14: {'role': 'user', 'text': 'Цена тут ни при чём, это не отказ.'},
            15: {'role': 'user', 'text': 'Дякую!'},
        }
        self.context = {'schema_version': 1, 'coverage': 'sent_receipts_observed',
            'client_id': 7, 'episode_id': 3, 'namespace': 'ig:synthetic', 'reset_floor': 10,
            'watermark_message_id': 15, 'client_permission_epoch': 2, 'digest': 'a' * 64,
            'diagnostic_refs': ['revision_effect:91'], 'diagnostics': [
                {'ref': 'revision_effect:91', 'effect_id': 91, 'kind': 'sent_reply', 'state': 'sent',
                 'provider_message_id': 'synthetic-provider-receipt', 'source_message_ids': [10],
                 'text': 'Which product?', 'response_seconds': 300.0, 'media_observed': False},
            ]}

    def value(self, code='price', mid=10, quote='Не купую, бо ціна зависока.', **overrides):
        row = {'code': code, 'basis': 'reported_by_customer', 'confidence': 'high',
               'evidence': [{'message_id': mid, 'quote': quote}],
               'diagnostic_refs': [], 'counterevidence': []}
        row.update(overrides)
        return {'schema_version': 1, 'motivation': [], 'readiness_evidence': [],
                'nonconversion_reasons': [row]}

    def normalize(self, value, **kwargs):
        return normalize_conversion_audit(value, self.sources,
            kwargs.get('context', self.context))

    def test_explicit_customer_causal_refusal_is_attributed_and_scope_is_code_owned(self):
        value = self.value()
        value['_source_scope'] = {'client_id': 999}
        result = self.normalize(value)
        self.assertEqual(result['nonconversion_reasons'][0]['basis'], 'reported_by_customer')
        self.assertEqual(result['_source_scope']['client_id'], 7)
        self.assertEqual(result['_source_scope']['source_namespace'], 'ig:synthetic')
        self.assertEqual(result['_source_scope']['response_review_digest'], 'a' * 64)
        self.assertNotIn('purchase_probability', result)

    def test_manager_model_tampered_and_historical_quotes_never_prove_customer_cause(self):
        for mid, quote in ((11, 'Не купую, бо ціна зависока.'),
                           (12, 'Не купую, бо ціна зависока.'),
                           (10, 'Не купую, бо доставка повільна.'),
                           (True, 'Не купую, бо ціна зависока.')):
            with self.subTest(mid=mid, quote=quote):
                self.assertEqual(self.normalize(self.value(mid=mid, quote=quote))['nonconversion_reasons'], [])
        for change in ({'evidence_eligible': False}, {'analysis_scope': 'historical_context'},
                       {'provider_namespace': 'foreign'}, {'client_id': 999}):
            with self.subTest(change=change):
                sources = deepcopy(self.sources)
                sources[10].update(change)
                result = normalize_conversion_audit(self.value(), sources, self.context)
                self.assertEqual(result['nonconversion_reasons'], [])

    def test_quoted_and_reported_third_party_refusals_are_not_own_customer_evidence(self):
        quote = 'Не купую, бо ціна зависока.'
        for text in ('Менеджер каже: «Не купую, бо ціна зависока.»',
                     '> Не купую, бо ціна зависока.',
                     'Менеджер написав: Не купую, бо ціна зависока.'):
            with self.subTest(text=text):
                self.sources[15] = {'role': 'user', 'text': text}
                self.assertEqual(self.normalize(self.value(mid=15, quote=quote))['nonconversion_reasons'], [])

    def test_price_objection_is_not_customer_reported_abandonment_and_silence_proves_nothing(self):
        result = self.normalize(self.value(mid=13, quote='Дорого. Подумаю.'))
        row = result['nonconversion_reasons'][0]
        self.assertEqual((row['basis'], row['confidence']), ('hypothesis', 'low'))
        result = self.normalize(self.value(mid=15, quote='Дякую!'))
        self.assertEqual(result['nonconversion_reasons'], [])
        result = self.normalize(self.value(evidence=[], diagnostic_refs=['invented:42']))
        self.assertEqual(result['nonconversion_reasons'], [])

    def test_negation_is_a_whole_word_not_a_suffix_of_customer_words(self):
        for text in ('Для мене дорого.', 'The piano price feels high.'):
            with self.subTest(text=text):
                self.sources[15] = {'role': 'user', 'text': text}
                row = self.normalize(self.value(mid=15, quote=text))['nonconversion_reasons'][0]
                self.assertEqual((row['code'], row['basis']), ('price', 'hypothesis'))
        for text in ('Це не дорого.', 'This is not expensive.', 'No price concern.'):
            with self.subTest(text=text):
                self.sources[15] = {'role': 'user', 'text': text}
                self.assertEqual(self.normalize(self.value(mid=15, quote=text))['nonconversion_reasons'], [])

    def test_causal_reports_require_same_quote_with_refusal_reason_and_causal_link(self):
        examples = (
            ('Не беру, бо доставка не встигає.', 'delivery_deadline'),
            ('Не буду покупать, потому что цена высокая.', 'price'),
            ('I will not buy because the size is wrong.', 'size_fit'),
        )
        for text, code in examples:
            with self.subTest(text=text):
                self.sources[15] = {'role': 'user', 'text': text}
                result = self.normalize(self.value(code=code, mid=15, quote=text))
                self.assertEqual(result['nonconversion_reasons'][0]['basis'], 'reported_by_customer')
        self.sources[15] = {'role': 'user', 'text': 'I will not buy because the weather changed. The price is fine.'}
        for quote in ('The price is fine.', self.sources[15]['text']):
            value = self.value(mid=15, quote=quote)
            self.assertEqual(self.normalize(value)['nonconversion_reasons'][0]['basis'], 'hypothesis')

    def test_causal_binding_drops_explicitly_exonerated_price_in_same_sentence(self):
        cases = (
            'Не купую через розмір, ціна тут ні до чого.',
            'Не буду покупать из-за размера, цена ни при чём.',
            'I will not buy because the size is wrong, the price is not a problem.',
        )
        for text in cases:
            self.sources[15] = {'role': 'user', 'text': text}
            with self.subTest(text=text):
                result = self.normalize(self.value(mid=15, quote=text))
                self.assertEqual(result['nonconversion_reasons'], [])
                size = self.normalize(self.value(code='size_fit', mid=15, quote=text))
                self.assertEqual(size['nonconversion_reasons'][0]['basis'], 'reported_by_customer')
        for text in ('Ціна зависока, тому не купую.', 'Цена высокая, поэтому не буду покупать.',
                     'The price is high, so I will not buy.'):
            self.sources[15] = {'role': 'user', 'text': text}
            with self.subTest(text=text):
                self.assertEqual(self.normalize(self.value(mid=15, quote=text))['nonconversion_reasons'][0]['basis'], 'reported_by_customer')

    def test_explicitly_negated_gift_motivation_is_dropped_in_all_three_languages(self):
        for text in ('Не на подарунок, беру для себе.', 'Не в подарок, беру для себя.',
                     'Not a gift, I am buying for myself.'):
            self.sources[15] = {'role': 'user', 'text': text}
            value = self.value()
            value['motivation'] = [{'code': 'gift', 'basis': 'customer_stated', 'confidence': 'high',
                'evidence': [{'message_id': 15, 'quote': text}]}]
            with self.subTest(text=text):
                self.assertEqual(self.normalize(value)['motivation'], [])

    def test_counterevidence_is_preserved_and_prevents_unqualified_reported_cause(self):
        value = self.value(counterevidence=[{'message_id': 14, 'quote': 'Цена тут ни при чём, это не отказ.'}])
        row = self.normalize(value)['nonconversion_reasons'][0]
        self.assertEqual(row['basis'], 'hypothesis')
        self.assertEqual(row['confidence'], 'low')
        self.assertEqual(row['counterevidence'][0]['message_id'], 14)

    def test_receipt_diagnostics_are_exact_allowlisted_observations_not_customer_causal_reports(self):
        value = self.value(code='response_delay', evidence=[], diagnostic_refs=['revision_effect:91', 'revision_effect:92'])
        row = self.normalize(value)['nonconversion_reasons'][0]
        self.assertEqual(row['diagnostic_refs'], ['revision_effect:91'])
        self.assertEqual((row['basis'], row['confidence']), ('hypothesis', 'low'))
        for change in ({'state': 'pending'}, {'effect_id': 92}, {'source_message_ids': [11]},
                       {'provider_message_id': ''}, {'response_seconds': None}):
            context = deepcopy(self.context)
            context['diagnostics'][0].update(change)
            with self.subTest(change=change):
                self.assertEqual(self.normalize(value, context=context)['nonconversion_reasons'], [])

    def test_media_only_receipt_permits_timing_but_not_content_inference(self):
        context = deepcopy(self.context)
        context['diagnostics'][0].update(text='', media_observed=True)
        delay = self.normalize(self.value(code='response_delay', evidence=[], diagnostic_refs=['revision_effect:91']), context=context)
        self.assertEqual(len(delay['nonconversion_reasons']), 1)
        product = self.normalize(self.value(code='product', evidence=[], diagnostic_refs=['revision_effect:91']), context=context)
        self.assertEqual(product['nonconversion_reasons'], [])

    def test_reset_watermark_and_context_invalidation_drop_stale_refs(self):
        for change in ({'reset_floor': 11}, {'watermark_message_id': 9}):
            context = {**self.context, **change}
            with self.subTest(change=change):
                result = self.normalize(self.value(), context=context)
                self.assertEqual(result['nonconversion_reasons'], [])
        value = self.value(code='response_delay', evidence=[], diagnostic_refs=['revision_effect:91'])
        for change in ({'coverage': 'reply_not_yet_observable'}, {'digest': 'tampered'}):
            with self.subTest(change=change):
                self.assertEqual(self.normalize(value, context={**self.context, **change})['nonconversion_reasons'], [])

    def test_motivation_is_bounded_not_a_personality_or_sensitive_profile(self):
        value = self.value()
        value['motivation'] = [
            {'code': 'gift', 'basis': 'customer_stated', 'confidence': 'high',
             'evidence': [{'message_id': 10, 'quote': 'Це мав бути подарунок.'}]},
            {'code': 'military', 'basis': 'hypothesis', 'confidence': 'high',
             'evidence': [{'message_id': 10, 'quote': 'Це мав бути подарунок.'}]},
        ]
        value['personality'] = 'rich extrovert'
        value['communication_preferences'] = {'emoji': 'allow'}
        result = self.normalize(value)
        self.assertEqual([row['code'] for row in result['motivation']], ['gift'])
        self.assertNotIn('personality', result)
        self.assertNotIn('communication_preferences', result)

    def test_normalized_payload_is_idempotent_bounded_and_deduplicated(self):
        value = self.value()
        value['nonconversion_reasons'] *= 50
        value['readiness_evidence'] = [{'message_id': 10, 'quote': 'Не купую, бо ціна зависока.'}] * 100
        value['motivation'] = [{'code': code, 'basis': 'hypothesis', 'confidence': 'medium',
            'evidence': value['readiness_evidence']} for code in ('self', 'gift', 'team', 'resale', 'custom')]
        result = self.normalize(value)
        self.assertEqual(result, self.normalize(result))
        self.assertLessEqual(len(result['motivation']), 3)
        self.assertEqual(len(result['nonconversion_reasons']), 1)
        self.assertEqual(len(result['readiness_evidence']), 1)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), MAX_JSON_CHARS)

    def test_malformed_optional_payload_never_raises_or_changes_baseline_input(self):
        for value in (None, [], 'injection', {'schema_version': True}, {'schema_version': 99}):
            with self.subTest(value=value):
                self.assertEqual(self.normalize(value), {})
        value = {'schema_version': 1, 'motivation': [{'code': []}],
                 'readiness_evidence': [{'message_id': [], 'quote': {}}],
                 'nonconversion_reasons': [{'code': {}}, {'code': 'price', 'basis': [],
                     'confidence': {}, 'evidence': [{'message_id': 13, 'quote': 'Дорого.'}]}]}
        before = deepcopy(value)
        result = self.normalize(value)
        self.assertEqual(value, before)
        self.assertEqual(result['nonconversion_reasons'][0]['confidence'], 'low')
