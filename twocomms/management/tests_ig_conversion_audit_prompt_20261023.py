"""Native prompt/quote regressions; actual model compliance needs separate probing."""
from django.test import SimpleTestCase, override_settings

from management.services.ig_conversion_audit import (
    CONVERSION_AUDIT_PROMPT_FRAGMENT, normalize_conversion_audit,
)


class ConversionAuditPromptAndOwnClauseTests(SimpleTestCase):
    def setUp(self):
        self.text = ('Менеджер каже: «Не купую, бо ціна зависока». Це цитата, не моя відмова. '
                     'Не на подарунок, беру для себе. Я лише питаю про тканину.')
        self.sources = {107: {'message_id': 107, 'role': 'user', 'text': self.text,
            'evidence_eligible': True, 'analysis_scope': 'current_episode'}}
        self.scope = {'schema_version': 1, 'client_id': 91, 'episode_id': 8,
            'namespace': 'synthetic-native', 'reset_floor': 100, 'watermark_message_id': 107,
            'client_permission_epoch': 3, 'digest': 'a' * 64,
            'coverage': 'reply_not_yet_observable', 'diagnostic_refs': [], 'diagnostics': []}

    def audit(self):
        return {'schema_version': 1,
            'motivation': [{'code': 'self', 'basis': 'customer_stated', 'confidence': 'high',
                'evidence': [{'message_id': 107, 'quote': 'Не на подарунок, беру для себе.'}]}],
            'readiness_evidence': [{'message_id': 107, 'quote': 'Я лише питаю про тканину.'}],
            'nonconversion_reasons': [{'code': 'price', 'basis': 'reported_by_customer',
                'confidence': 'high', 'evidence': [{'message_id': 107, 'quote': 'Не купую, бо ціна зависока'}],
                'diagnostic_refs': [], 'counterevidence': []}]}

    def test_own_sentences_retain_exact_punctuation_without_admitting_quoted_manager_refusal(self):
        result = normalize_conversion_audit(self.audit(), self.sources, self.scope)
        self.assertEqual(result['motivation'][0]['basis'], 'customer_stated')
        self.assertEqual(result['motivation'][0]['evidence'][0]['quote'], 'Не на подарунок, беру для себе.')
        self.assertEqual(result['readiness_evidence'], [{'message_id': 107, 'quote': 'Я лише питаю про тканину.'}])
        self.assertEqual(result['nonconversion_reasons'], [])
        self.assertEqual(normalize_conversion_audit(result, self.sources, self.scope), result)

    def test_literal_manager_report_remains_excluded_when_other_own_sentences_are_valid(self):
        self.sources[107]['text'] = ('Менеджер написав: Не купую, бо ціна зависока. '
            'Це його слова. Я лише питаю про тканину.')
        value = self.audit()
        value['motivation'] = []
        value['nonconversion_reasons'][0]['evidence'][0]['quote'] += '.'
        result = normalize_conversion_audit(value, self.sources, self.scope)
        self.assertEqual(result['nonconversion_reasons'], [])
        self.assertEqual(result['readiness_evidence'], value['readiness_evidence'])

    @override_settings(IG_ANALYSIS_V2_MODE='off')
    def test_actual_same_call_prompt_payload_and_normalizer_share_updated_contract(self):
        from management.services import bot_conversation_analysis as analysis
        system = analysis._analysis_system_prompt()
        self.assertEqual(system.count(CONVERSION_AUDIT_PROMPT_FRAGMENT), 1)
        self.assertIn('Do not default motivation', system)
        self.assertIn('CAUSE OF\nREFUSAL', system)
        self.assertIn('Do not skip this comparison', system)
        payload = analysis._analysis_prompt_payload(watermark=107,
            transcript=list(self.sources.values()), truth_state={'verified_payment': False},
            memory={'text': '', 'reason': 'none'}, memory_metadata={'selected': False},
            source_boundary={'namespace': 'synthetic-native', 'event_at': '2026-10-23T12:00:00+03:00', 'message_id': 107},
            response_review_context=self.scope)
        self.assertEqual(payload['response_review_context'], self.scope)
        self.assertEqual(payload['conversation'], list(self.sources.values()))
        parsed = {'interaction_type': 'information_only', 'score_band': 'exploring',
            'purchase_probability': 0.2, 'confidence': 0.7, 'evidence': [],
            'uncertainties': [], 'conversion_audit': self.audit()}
        result = analysis._normalize(parsed, self.sources, verified_payment=False,
            response_review_context=self.scope)
        self.assertEqual(result['conversion_audit']['motivation'][0]['basis'], 'customer_stated')
        self.assertEqual(result['conversion_audit']['nonconversion_reasons'], [])
