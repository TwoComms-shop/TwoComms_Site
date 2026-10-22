"""Customer conversation seams through native revision validation and fake HTTP.

Run using test_settings_no_network_non_dtf (or a derivative), never production.
The model stub supplies candidates; these tests prove deterministic acceptance,
source capture and durable delivery, not actual Gemini response quality.
"""
from datetime import timedelta
import json
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management.models import IgCustomerTurn, IgFollowUpTask, IgTurnMessage
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_pending_question_context import capture_pending_question_context
from management.services.ig_reply_language import resolve_own_source_reply_language
from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
from management.services.ig_response_control import parse_structured_response
from management.services.ig_turn_revisions import create_collecting_revision


class LiveConversationSourceContractTests(SimpleTestCase):
    def test_spelled_and_mixed_script_xxl_are_one_affirmative_size(self):
        for text in ('2XL', 'два XL', 'дваXL', 'дві іксель', 'двііксель', '2ХL', '2XЛ', 'ХXL', 'XХL'):
            with self.subTest(text=text):
                self.assertEqual(parse_turn(text).field_updates.get('size', ''), 'XXL')

    def test_quoted_question_and_rejected_size_do_not_select_xxl(self):
        for text in ('«два XL»', 'Чи є два XL?', 'Не хочу два XL', '2ХL або M'):
            with self.subTest(text=text):
                self.assertEqual(parse_turn(text).field_updates.get('size', ''), '')

    def test_current_ru_ua_ru_switch_keeps_current_own_language_and_source(self):
        history = []
        for pk, text, language in ((1, 'Здравствуйте! Подскажите, какая цена на эту футболку?', 'ru'),
                (2, 'Дякую! Підкажіть, будь ласка, які кольори є?', 'uk'),
                (3, 'Спасибо! Подскажите, пожалуйста, какой материал у этой футболки?', 'ru')):
            current = {'message_id': pk, 'role': 'user', 'text': text, 'source_digest': str(pk) * 64,
                'provider_created_at': (timezone.now() + timedelta(seconds=pk)).isoformat()}
            decision = resolve_own_source_reply_language(sources=[current], history=history, profile_language='uk')
            self.assertEqual(decision.reply_language, language)
            self.assertEqual(decision.source_message_id, pk)
            history.append(current)

    def test_conditional_current_review_is_permitted_but_hiring_and_payment_are_not(self):
        allowed = ('Я автоматизований помічник TwoComms і допомагаю в Direct цілодобово. '
            'Дякуємо за вашу пропозицію допомоги! Передам її керівництву для поточного розгляду. '
            'Якщо після розгляду знадобляться уточнення, напишемо вам тут.')
        self.assertTrue(validate_reply_truth(allowed, context=ReplyTruthContext()).valid)
        for text, reason in (('Ми шукаємо менеджерів. Вас прийнято на роботу.', 'unverified_recruitment'),
                ('Оплату підтверджено.', 'unverified_payment')):
            self.assertIn(reason, validate_reply_truth(text, context=ReplyTruthContext()).reasons)

    def test_also_check_grammar_never_authorizes_fabric_assertions_or_a_negated_check(self):
        from management.services.ig_product_inquiries import capture_product_inquiries, validate_inquiry_response
        inquiry = capture_product_inquiries([{'message_id': 1, 'role': 'user',
            'text': 'What is the composition of this hoodie?', 'source_digest': 'a' * 64}])
        for text in ('I will not check the composition with our manager.',
                'I will also check the composition. It is 100% cotton.',
                'I will also check the composition; the hoodie is polyester.'):
            with self.subTest(text=text):
                response = parse_structured_response({'reply_text': text, 'controls': [{'kind': 'manager', 'value': True}]})
                self.assertEqual(validate_inquiry_response(inquiry, response), 'product_information_unverified')


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativeLiveConversationContractTests(TransactionTestCase):
    reset_sequences = True
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare
    _execute = fixtures.RevisionLiveTests._execute
    _replace_bundle = fixtures.RevisionLiveTests._replace_bundle

    def _generate(self, payload, **kwargs):
        expected = getattr(self, '_expected_rejection', '')
        if expected:
            from management.services.call_ai_analysis import CallAIAnalysisError
            decision = kwargs['result_validator'](self.parsed,
                usage={'_request_inline_count': 0, '_request_inline_content_hashes': []})
            self.assertFalse(decision.valid)
            self.assertIn(expected, decision.reason_codes)
            self._observed_rejection = decision.reason_codes
            raise CallAIAnalysisError('expected deterministic rejection')
        return fixtures.RevisionLiveTests._generate(self, payload, **kwargs)

    def _fresh(self, text):
        self.source = self._message(text, f'conversation-source-{self.revision.pk}')
        self.turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=1, role='user')
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()

    def _answer(self, text, *, manager=False):
        self.parsed = {'reply_text': text, 'controls': [{'kind': 'manager', 'value': True}] if manager else []}
        result, model, http = self._execute([(200, json.dumps({'message_id': f'conversation-reply-{self.revision.pk}'}))])
        self.revision.refresh_from_db()
        self.assertEqual(result.state, 'completed', (result.reasons, self.revision.action_receipts.get('response_coverage')))
        self.assertEqual(model.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.delivery_effects.get().state, 'sent')
        return result

    def test_distinct_unknown_q2_updates_same_pending_exact_manager_brief(self):
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        self._replace_bundle(['What material is this hoodie?'])
        first_source = self.source
        self._answer('I will check the material with our manager.', manager=True)
        first_task = IgFollowUpTask.objects.get(reason='revision_case:product_information_review')
        self._fresh('What is the density of this hoodie?')
        second_source = self.source
        self._answer('I will check the density with our manager.', manager=True)
        tasks = IgFollowUpTask.objects.filter(reason='revision_case:product_information_review').exclude(status__in=('completed', 'cancelled'))
        self.assertEqual(tasks.count(), 1, list(tasks.values('pk', 'manager_context')))
        first_task.refresh_from_db()
        ids = {row['message_id'] for row in first_task.manager_context['sources']}
        self.assertEqual(ids, {first_source.pk, second_source.pk})
        text = json.dumps(first_task.manager_context, ensure_ascii=False).casefold()
        self.assertIn('material', text)
        self.assertIn('density', text)
        self.assertIn('What material is this hoodie?', first_task.message_text)
        self.assertIn('What is the density of this hoodie?', first_task.message_text)
        self._fresh('Can you reply in English?')
        self._answer('Yes, I can reply in English.')
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        first_task.refresh_from_db()
        self.assertEqual({row['message_id'] for row in first_task.manager_context['sources']}, ids)
        self.assertNotIn(first_task.status, ('completed', 'cancelled'))

    def test_independent_known_q3_is_delivered_while_exact_unknown_stays_open(self):
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        self._replace_bundle(['What material is this hoodie?'])
        self._answer('I will check the material with our manager.', manager=True)
        task = IgFollowUpTask.objects.get(reason='revision_case:product_information_review')
        self._fresh('Can you reply in English?')
        self._answer('Yes, I can reply in English.')
        task.refresh_from_db()
        self.assertNotIn(task.status, ('completed', 'cancelled'))
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        context = capture_pending_question_context(self.revision)
        self.assertEqual(context['cases'][0]['task_id'], task.pk)

    def test_direct_offer_sends_truthful_current_review_without_contact_or_portfolio_pressure(self):
        offer = 'Можу відповідати на повідомлення клієнтів вашого бренду в Instagram Direct. Які умови роботи?'
        self._replace_bundle([offer])
        self._answer('Я автоматизований помічник TwoComms, допомагаю у Direct цілодобово. '
            'Дякуємо за пропозицію! Передам її керівництву для розгляду потреби та умов. '
            'Якщо після розгляду знадобляться уточнення, напишемо тут.', manager=True)
        task = IgFollowUpTask.objects.get(reason='revision_case:collaboration_review')
        self.assertEqual(task.manager_context['cooperation_kind'], 'recruitment_service')
        self.assertEqual(task.manager_context['recruitment_service']['hiring_status'], 'unknown')
        text = self.revision.delivery_effects.get().payload['message']['text']
        self.assertNotRegex(text.casefold(), r'портфол|телефон|резюме|ваканс')

    def test_fresh_material_question_does_not_send_default_wait_apology(self):
        self._replace_bundle(['Який матеріал цієї футболки?'])
        self._answer('Перепрошую за очікування. Уточню матеріал у менеджера.', manager=True)
        self.assertNotIn('Перепрошую за очікування', self.revision.delivery_effects.get().payload['message']['text'])

    def test_direct_offer_cannot_physically_send_invented_hiring_acceptance(self):
        self._replace_bundle(['Можу відповідати на повідомлення клієнтів вашого бренду в Instagram Direct.'])
        self.parsed = {'reply_text': 'Дякуємо за пропозицію допомоги з Direct! Вас прийнято на роботу.',
            'controls': [{'kind': 'manager', 'value': True}]}
        self._expected_rejection = 'unverified_recruitment'
        result, _, http = self._execute()
        self.assertEqual(result.state, 'blocked', result.reasons)
        self.assertIn('unverified_recruitment', self._observed_rejection)
        http.assert_not_called()

    def test_composition_question_accepts_exact_topic_ack_without_artificial_material_keyword(self):
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        self._replace_bundle(['What is the composition of this hoodie?'])
        self._answer('I will check the composition with our manager.', manager=True)

    def test_truthful_also_check_does_not_become_a_fabric_claim(self):
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        self._replace_bundle(['What is the composition of this hoodie?'])
        self._answer('I will also check the composition with our manager.', manager=True)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativePendingProductInformationBoundaryTests(TransactionTestCase):
    reset_sequences = True
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare
    _generate = fixtures.RevisionLiveTests._generate
    _execute = fixtures.RevisionLiveTests._execute
    _replace_bundle = fixtures.RevisionLiveTests._replace_bundle
    _fresh = NativeLiveConversationContractTests._fresh
    _answer = NativeLiveConversationContractTests._answer

    def _open(self):
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        self._replace_bundle(['What material is this hoodie?'])
        self._answer('I will check the material with our manager.', manager=True)
        return IgFollowUpTask.objects.get(reason='revision_case:product_information_review')

    def _second(self):
        self._fresh('What is the density of this hoodie?')
        self._answer('I will check the density with our manager.', manager=True)

    def test_merged_case_keeps_each_exact_receipt_and_replays_neither_send(self):
        from management.models import IgBotNotification
        from management.services.ig_revision_intents import product_review_receipt_reason
        task = self._open()
        first, first_token = self.revision, self.token
        original_notice = IgBotNotification.objects.get(dedupe_key=f'ig-revision-case:{task.pk}')
        original_notice.status = 'sent'
        original_notice.sent_at = timezone.now()
        original_notice.telegram_message_id = 'manager-read-initial'
        original_notice.save(update_fields=['status', 'sent_at', 'telegram_message_id'])
        original_payload = dict(original_notice.payload)
        self._second()
        second, second_token = self.revision, self.token
        task.refresh_from_db()
        self.assertEqual(len(task.manager_context['product_inquiry_scopes']), 2)
        self.assertEqual(task.manager_context['latest_product_inquiry_revision_id'], second.pk)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        original_notice.refresh_from_db()
        self.assertEqual(original_notice.payload, original_payload)
        newest = IgBotNotification.objects.get(dedupe_key=f'ig-revision-case:{task.pk}:{second.pk}')
        self.assertIn('material', newest.payload['text'])
        self.assertIn('density', newest.payload['text'])
        for revision, token in ((first, first_token), (second, second_token)):
            revision.refresh_from_db()
            self.assertEqual(product_review_receipt_reason(revision), '')
            self.revision, self.token = revision, token
            result, model, http = self._execute()
            self.assertEqual(result.state, 'completed', result.reasons)
            model.assert_not_called()
            http.assert_not_called()
        self.assertEqual(IgBotNotification.objects.count(), 2)

    def test_product_merge_notification_query_count_is_constant_for_two_and_three_prior_scopes(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from management.services.ig_revision_intents import _mergeable_native_manager_task
        self._open()
        self._second()
        observed = []
        def measured(revision, reason, tasks, **kwargs):
            prior_count = len(tasks[0].manager_context['product_inquiry_scopes'])
            with CaptureQueriesContext(connection) as queries:
                result = _mergeable_native_manager_task(revision, reason, tasks, **kwargs)
            self.assertIsNotNone(result)
            notices = [row for row in queries if 'management_igbotnotification' in row['sql'].casefold()]
            observed.append((prior_count, len(queries), len(notices)))
            return result
        with patch('management.services.ig_revision_intents._mergeable_native_manager_task', side_effect=measured):
            self._fresh('What is the composition of this hoodie?')
            self._answer('I will check the composition with our manager.', manager=True)
            self._fresh('What material is this hoodie?')
            self._answer('I will check the material with our manager.', manager=True)
        self.assertEqual([row[0] for row in observed], [2, 3])
        self.assertEqual([row[2] for row in observed], [1, 1])
        self.assertEqual(observed[0][1], observed[1][1], observed)
        print('PRODUCT_MERGE_QUERY_BUDGET', json.dumps(observed))
        task = IgFollowUpTask.objects.get()
        self.assertEqual(len(task.manager_context['product_inquiry_scopes']), 4)

    def test_missing_prior_notification_rejects_merge_without_fallback_notification_reads(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from management.models import IgBotNotification
        from management.services.ig_revision_intents import _mergeable_native_manager_task
        task = self._open()
        self._second()
        task.refresh_from_db()
        original = dict(task.manager_context)
        IgBotNotification.objects.filter(dedupe_key=f'ig-revision-case:{task.pk}').delete()
        observed = []
        def measured(*args, **kwargs):
            with CaptureQueriesContext(connection) as queries:
                result = _mergeable_native_manager_task(*args, **kwargs)
            self.assertIsNone(result)
            observed.append(sum('management_igbotnotification' in row['sql'].casefold() for row in queries))
            return result
        with patch('management.services.ig_revision_intents._mergeable_native_manager_task', side_effect=measured):
            self._fresh('What is the composition of this hoodie?')
            self._answer('I will check the composition with our manager.', manager=True)
        self.assertEqual(observed, [1])
        task.refresh_from_db()
        self.assertEqual(task.manager_context, original)
        self.assertEqual(IgFollowUpTask.objects.count(), 2)

    def test_later_status_prompt_contains_both_completed_questions_and_known_question_still_sends(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from management.services.ig_pending_question_context import pending_question_guidance
        task = self._open()
        first_source = self.source.pk
        self._second()
        second_source = self.source.pk
        self._fresh('What questions are pending?')
        with CaptureQueriesContext(connection) as queries:
            context = capture_pending_question_context(self.revision)
        case = context['cases'][0]
        self.assertEqual(case['task_id'], task.pk)
        self.assertEqual(set(case['source_message_ids']), {first_source, second_source})
        self.assertIn('What material is this hoodie?', case['question'])
        self.assertIn('What is the density of this hoodie?', case['question'])
        self.assertLessEqual(sum('management_igcustomerturnrevision' in row['sql'].casefold() for row in queries), 2)
        self.assertLessEqual(sum('management_igturnrevisionsource' in row['sql'].casefold() for row in queries), 3)
        self.assertIn('density', pending_question_guidance(context))
        self._answer('Your material and density questions are still pending with our manager; I have no confirmed update yet.')
        compiled = json.dumps(self.generation_calls[-1], ensure_ascii=False)
        self.assertIn('What material is this hoodie?', compiled)
        self.assertIn('What is the density of this hoodie?', compiled)
        self._fresh('Can you reply in English?')
        self._answer('Yes, I can reply in English.')
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        task.refresh_from_db()
        self.assertNotIn(task.status, ('completed', 'cancelled'))

    def test_later_status_abstains_when_an_appended_scope_source_changes(self):
        task = self._open()
        self._second()
        self.source.text = 'Changed historical density question'
        self.source.save(update_fields=['text'])
        self._fresh('What questions are pending?')
        self.assertEqual(capture_pending_question_context(self.revision), {})
        task.refresh_from_db()
        self.assertNotIn(task.status, ('completed', 'cancelled'))

    def test_pending_display_budget_counts_separators_without_partial_question_truncation(self):
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        prefix = 'What material is this hoodie '
        questions = [prefix + chr(97 + index) * (408 - len(prefix)) + '?' for index in range(5)]
        self.assertEqual(sum(map(len, questions)), 2045)
        self.assertGreater(len(' · '.join(questions)), 2048)
        self._replace_bundle(questions)
        self._answer('I will check the material with our manager.', manager=True)
        self._fresh('What questions are pending?')
        self.assertEqual(capture_pending_question_context(self.revision), {})

    def test_sent_manager_ack_keeps_q2_visible_when_an_independent_price_answer_is_still_owed(self):
        from management.services.ig_response_debt import response_obligation
        self._open()
        self._fresh('What is the density of this hoodie? What is the price?')
        self.parsed = {'reply_text': 'I will check the density with our manager.',
            'controls': [{'kind': 'manager', 'value': True}]}
        result, _, http = self._execute()
        self.assertEqual(result.state, 'delivery_pending', result.reasons)
        self.assertIn('semantic_reply_incomplete', result.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.delivery_effects.get().state, 'sent')
        second, source = self.revision, self.source
        self.assertTrue(response_obligation(second)['substantive_owed'])
        self._fresh('What questions are pending?')
        context = capture_pending_question_context(self.revision)
        self.assertIn('material', context['cases'][0]['question'])
        self.assertIn('density', context['cases'][0]['question'])
        self.assertIn(source.pk, context['cases'][0]['source_message_ids'])
        self._answer('Your material and density questions are still pending with our manager; I have no confirmed update yet.')
        second.refresh_from_db()
        source.refresh_from_db()
        self.assertTrue(response_obligation(second)['substantive_owed'])
        self.assertEqual(source.status, 'pending')

    def test_closed_case_is_not_reopened_or_merged(self):
        task = self._open()
        context = dict(task.manager_context)
        task.status = 'completed'
        task.save(update_fields=['status'])
        self._second()
        task.refresh_from_db()
        self.assertEqual(task.status, 'completed')
        self.assertEqual(task.manager_context, context)
        self.assertEqual(IgFollowUpTask.objects.count(), 2)

    def test_other_client_task_cannot_be_reused(self):
        from management.models import IgClient
        task = self._open()
        other = IgClient.objects.create(igsid='conversation-contract-other')
        task.client = other
        task.save(update_fields=['client'])
        context = dict(task.manager_context)
        self._second()
        task.refresh_from_db()
        self.assertEqual(task.client_id, other.pk)
        self.assertEqual(task.manager_context, context)
        self.assertEqual(IgFollowUpTask.objects.count(), 2)

    def test_permission_epoch_change_cannot_merge_prior_question(self):
        task = self._open()
        context = dict(task.manager_context)
        self.customer.reply_permission_epoch += 1
        self.customer.save(update_fields=['reply_permission_epoch'])
        self._second()
        task.refresh_from_db()
        self.assertEqual(task.manager_context, context)
        self.assertEqual(IgFollowUpTask.objects.count(), 2)

    def test_review_decision_cannot_be_reopened_by_appending_a_question(self):
        task = self._open()
        context = dict(task.manager_context)
        task.manager_approval_status = 'approved'
        task.save(update_fields=['manager_approval_status'])
        self._second()
        task.refresh_from_db()
        self.assertEqual(task.manager_approval_status, 'approved')
        self.assertEqual(task.manager_context, context)
        self.assertEqual(IgFollowUpTask.objects.count(), 2)

    def test_proven_other_episode_cannot_merge_prior_question(self):
        from management.models import IgCommercialEpisode, IgConversationRouteDecision
        def route(episode, sequence):
            IgConversationRouteDecision.objects.create(client=self.customer, revision=self.revision,
                decision_key=str(sequence) * 64, sequence=sequence, reset_floor=1,
                watermark_message_id=self.source.pk, input_digest=str(sequence) * 64,
                interpretation_digest=str(sequence) * 64, decision_digest=str(sequence) * 64,
                source_binding={'source_refs': {'commercial_episode_id': episode.pk}},
                interpretation={'intents': [{'kind': 'catalog', 'operation': 'open',
                    'evidence_message_ids': [self.source.pk]}]}, occurred_at=timezone.now())
        first = IgCommercialEpisode.objects.create(client=self.customer, sequence=1,
            materialization_key='conversation-episode-one')
        self.customer.current_commercial_episode = first
        self.customer.save(update_fields=['current_commercial_episode'])
        task = self._open()
        route(first, 1)
        context = dict(task.manager_context)
        first.open_slot = None
        first.state = 'cancelled'
        first.save(update_fields=['open_slot', 'state'])
        second = IgCommercialEpisode.objects.create(client=self.customer, sequence=2,
            materialization_key='conversation-episode-two')
        self.customer.current_commercial_episode = second
        self.customer.save(update_fields=['current_commercial_episode'])
        self._fresh('What is the density of this hoodie?')
        route(second, 2)
        self._answer('I will check the density with our manager.', manager=True)
        task.refresh_from_db()
        self.assertEqual(task.manager_context, context)
        self.assertEqual(IgFollowUpTask.objects.count(), 2)

    def test_changed_historical_source_cannot_be_merged(self):
        task = self._open()
        context = dict(task.manager_context)
        self.source.text = 'Changed customer source text'
        self.source.save(update_fields=['text'])
        self._second()
        task.refresh_from_db()
        self.assertEqual(task.manager_context, context)
        self.assertEqual(IgFollowUpTask.objects.count(), 2)

    def test_changed_reset_floor_cannot_merge_prior_question(self):
        task = self._open()
        context = dict(task.manager_context)
        floor = self.source.pk + 1
        with patch('management.services.ig_conversation_routes.conversation_route_reset_floor', return_value=floor):
            self._second()
        task.refresh_from_db()
        self.assertEqual(task.manager_context, context)
        self.assertEqual(IgFollowUpTask.objects.count(), 2)

    def test_malformed_prior_scope_is_not_merged_or_repaired_as_authority(self):
        task = self._open()
        task.manager_context['product_inquiry_scopes'][0]['snapshot_digest'] = 'f' * 64
        task.manager_context['product_inquiry_scopes'][0]['_legacy_native'] = True
        task.save(update_fields=['manager_context'])
        context = dict(task.manager_context)
        self._second()
        task.refresh_from_db()
        self.assertEqual(task.manager_context, context)
        self.assertEqual(IgFollowUpTask.objects.count(), 2)

    def _malformed_context_blocks_before_http(self, value):
        from management.services.ig_revision_intents import ensure_revision_manager_case, product_review_receipt_reason
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        self._replace_bundle(['What material is this hoodie?'])
        self.parsed = {'reply_text': 'I will check the material with our manager.',
            'controls': [{'kind': 'manager', 'value': True}]}
        corrupted = False
        def corrupt_after_record(*args, **kwargs):
            nonlocal corrupted
            result = ensure_revision_manager_case(*args, **kwargs)
            if not corrupted:
                self.assertTrue(result.ready, result.reason)
                IgFollowUpTask.objects.filter(pk=result.task_id).update(manager_context=value)
                corrupted = True
            return result
        with patch('management.services.ig_revision_intents.ensure_revision_manager_case', side_effect=corrupt_after_record):
            result, _, http = self._execute()
        # Corruption at the final transport preflight cancels the planned
        # delivery; earlier inspection can block it before planning instead.
        self.assertIn(result.state, ('blocked', 'cancelled'), result.reasons)
        self.assertTrue({'manager_receipt_invalid', 'product_inquiry_handoff_invalid', 'transport_preflight_check_failed'}.intersection(result.reasons))
        http.assert_not_called()
        self.revision.refresh_from_db()
        self.assertEqual(product_review_receipt_reason(self.revision), 'product_inquiry_handoff_invalid')

    def test_truthy_array_manager_context_blocks_natively_without_http(self):
        self._malformed_context_blocks_before_http(['corrupt'])

    def test_truthy_scalar_manager_context_blocks_natively_without_http(self):
        self._malformed_context_blocks_before_http('corrupt')

    def test_malformed_scope_disposition_blocks_natively_without_http(self):
        self._malformed_context_blocks_before_http({'product_inquiry_scopes': [{'product_inquiries': 'oops'}]})

    def test_malformed_writer_source_refs_fail_closed_without_http(self):
        self._malformed_context_blocks_before_http({'sources': [None]})

    def _select_product(self, number):
        from storefront.models import Category, Product
        from productcolors.models import Color, ProductColorVariant
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_revision_commerce import resolve_source_product_request
        category, _ = Category.objects.get_or_create(slug='contract-unreviewed-base', defaults={'name': 'Unreviewed base'})
        product = Product.objects.create(title=f'Contract hoodie {number}', slug=f'contract-hoodie-{number}',
            category=category, price=900, status='published')
        color = Color.objects.create(name=f'Contract black {number}', primary_hex='#111111')
        ProductColorVariant.objects.create(product=product, color=color, price_override=900, is_default=True)
        source = self._message(product.title, f'contract-product-{number}')
        request = resolve_source_product_request(self.customer, source, parse_turn(source.text))
        apply_turn(self.customer, source, request, reply_payload={})
        return product

    def test_proven_other_product_cannot_merge_even_with_same_garment(self):
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        first = self._select_product(1)
        self._fresh('What material is this hoodie?')
        self._answer('I will check the material with our manager.', manager=True)
        task = IgFollowUpTask.objects.get(reason='revision_case:product_information_review')
        self.assertEqual(task.manager_context['product_inquiries']['inquiries'][0]['product_id'], first.pk)
        context = dict(task.manager_context)
        second = self._select_product(2)
        self._second()
        task.refresh_from_db()
        self.assertEqual(task.manager_context, context)
        self.assertEqual(IgFollowUpTask.objects.count(), 2)
        fresh = IgFollowUpTask.objects.exclude(pk=task.pk).get()
        self.assertEqual(fresh.manager_context['product_inquiries']['inquiries'][0]['product_id'], second.pk)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativeGeneralManagerContextTests(TransactionTestCase):
    reset_sequences = True
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare
    _generate = fixtures.RevisionLiveTests._generate
    _execute = fixtures.RevisionLiveTests._execute
    _replace_bundle = fixtures.RevisionLiveTests._replace_bundle
    _fresh = NativeLiveConversationContractTests._fresh
    _answer = NativeLiveConversationContractTests._answer

    def _status_after_two_cases(self, first, second, reply):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        replies = reply if isinstance(reply, tuple) else (reply, reply)
        self._replace_bundle([first])
        self._answer(replies[0], manager=True)
        first_id = self.source.pk
        self._fresh(second)
        self._answer(replies[1], manager=True)
        second_id = self.source.pk
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self._fresh('What questions are pending?')
        with CaptureQueriesContext(connection) as reads:
            context = capture_pending_question_context(self.revision)
        self.assertLessEqual(len(reads), 24)
        self.assertEqual(set(context['cases'][0]['source_message_ids']), {first_id, second_id})
        self.assertIn(first, context['cases'][0]['question'])
        self.assertIn(second, context['cases'][0]['question'])
        self._answer('Both questions are still pending with our manager; I have no confirmed update yet.')
        payload = json.dumps(self.generation_calls[-1], ensure_ascii=False)
        self.assertIn(first, payload)
        self.assertIn(second, payload)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)

    def test_business_status_projects_both_native_question_scopes(self):
        self._status_after_two_cases(
            'I plan to open an online store to resell your clothes. What are your wholesale partnership terms?',
            'For my wholesale store partnership, may I use your product photos in my store?',
            ('I will forward your questions about wholesale partnership terms and reselling our clothes online to our team for review.',
             'I will forward your question about using our product photos in your store to our team for review.'))

    def test_generic_status_projects_both_native_question_scopes(self):
        self._status_after_two_cases('Please connect me to a manager about account access.',
            'Please connect me to a manager about an invoice copy.',
            'I will connect you with our manager about your question.')

    def test_mutable_legacy_marker_cannot_authorize_a_corrupted_generic_scope(self):
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        self._replace_bundle(['Please connect me to a manager about account access.'])
        self._answer('I will connect you with our manager about account access.', manager=True)
        task = IgFollowUpTask.objects.get()
        task.manager_context['manager_case_scopes'][0].update(snapshot_digest='f' * 64, _legacy_native=True)
        task.save(update_fields=['manager_context'])
        context = dict(task.manager_context)
        self._fresh('Please connect me to a manager about an invoice copy.')
        self._answer('I will connect you with our manager about an invoice copy.', manager=True)
        task.refresh_from_db()
        self.assertEqual(task.manager_context, context)
        self.assertEqual(IgFollowUpTask.objects.count(), 2)
