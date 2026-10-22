"""Native manager-case continuity with model candidates and fake HTTP only."""
import json

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management.models import IgCustomerTurn, IgCustomerTurnRevision, IgFollowUpTask, IgRevisionDeliveryEffect, IgTurnMessage
from management.services.ig_turn_revisions import create_collecting_revision


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativeManagerCaseContinuityTests(TransactionTestCase):
    reset_sequences = True
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare
    _generate = fixtures.RevisionLiveTests._generate
    _execute = fixtures.RevisionLiveTests._execute
    _replace_bundle = fixtures.RevisionLiveTests._replace_bundle

    def _fresh(self, text):
        self.source = self._message(text, f'manager-continuity-{self.revision.pk}')
        self.turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=1, role='user')
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()

    def _send(self, text, *, manager=True):
        if not hasattr(self, '_native_tokens'):
            self._native_tokens = {}
        self._native_tokens[self.revision.pk] = self.token
        self.parsed = {'reply_text': text, 'controls': [{'kind': 'manager', 'value': True}] if manager else []}
        result, model, http = self._execute([(200, json.dumps({'message_id': f'manager-reply-{self.revision.pk}'}))])
        self.revision.refresh_from_db()
        effects = list(self.revision.delivery_effects.values('state', 'provider_message_id'))
        tasks = [{'id': task.pk, 'reason': task.reason, 'status': task.status,
            'anchor': task.manager_context.get('latest_revision_id'),
            'cooperation_kind': task.manager_context.get('cooperation_kind'),
            'source_ids': [row['message_id'] for row in task.manager_context.get('sources', ())]}
            for task in IgFollowUpTask.objects.filter(client=self.customer)]
        print('MANAGER_CONTINUITY', json.dumps({'test': self._testMethodName, 'revision_id': self.revision.pk,
            'state': result.state, 'reasons': list(result.reasons), 'generation_calls': model.call_count,
            'http_calls': http.call_count, 'effects': effects, 'revision_state': self.revision.state,
            'tasks': tasks},
            ensure_ascii=False, default=str))
        return result, model, http

    def _assert_sent(self, observation):
        result, model, http = observation
        self.assertEqual(result.state, 'completed', result.reasons)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.state, 'processed')
        self.assertEqual(self.revision.delivery_effects.filter(state='sent').count(), 1)
        self.assertFalse(self.revision.delivery_effects.exclude(state='sent').exists())
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, 'done')

    def _start(self, source, reply):
        self.customer.language = 'en'
        self.customer.save(update_fields=['language'])
        self._replace_bundle([source])
        self._assert_sent(self._send(reply))
        return IgFollowUpTask.objects.get(kind='manager_task')

    def _known_q3(self):
        self._fresh('Can you reply in English?')
        return self._send('Yes, I can reply in English.', manager=False)

    def _finish_and_replay(self):
        with self.subTest('independent known question still sends'):
            self._assert_sent(self._known_q3())
        with self.subTest('three replies delivered with no stuck claim'):
            self.assertFalse(IgCustomerTurnRevision.objects.filter(client=self.customer, state='claimed').exists())
            self.assertEqual(IgRevisionDeliveryEffect.objects.filter(revision__client=self.customer, state='sent').count(), 3)
        for revision in IgCustomerTurnRevision.objects.filter(pk__in=self._native_tokens, state='processed'):
            with self.subTest('completed reply is not generated or sent again', revision=revision.pk):
                self.revision, self.token = revision, self._native_tokens[revision.pk]
                result, model, http = self._execute()
                self.assertEqual(result.state, 'completed', result.reasons)
                model.assert_not_called()
                http.assert_not_called()
                self.assertEqual(revision.delivery_effects.filter(state='sent').count(), 1)

    def test_business_q2_sends_while_same_business_q1_remains_pending(self):
        task = self._start(
            'I plan to open an online store to resell your clothes. What are your wholesale partnership terms?',
            'I will forward your questions about wholesale partnership terms and reselling our clothes online to our team for review.')
        first_source, first_revision = self.source.pk, self.revision.pk
        self._fresh('For my wholesale store partnership, may I use your product photos in my store?')
        second_source = self.source.pk
        second = self._send('I will forward your question about using our product photos in your store to our team for review.')
        with self.subTest('second native reply'):
            self._assert_sent(second)
        task.refresh_from_db()
        with self.subTest('original business question remains represented'):
            self.assertNotIn(task.status, ('completed', 'cancelled'))
            self.assertEqual(task.manager_context['cooperation_kind'], 'business')
            self.assertIn(first_source, [row['message_id'] for row in task.manager_context['sources']])
            if IgFollowUpTask.objects.filter(kind='manager_task').count() == 1:
                self.assertEqual(task.manager_context['latest_revision_id'], first_revision)
                self.assertIn(second_source, [row['message_id'] for row in task.manager_context['sources']])
        self._finish_and_replay()

    def test_creator_then_business_does_not_overwrite_the_creator_brief(self):
        task = self._start(
            'I am a photographer and can create photos and videos for your brand. I would like a collaboration.',
            'Thank you for your photography and video collaboration offer. I will forward your proposal to our team for review.')
        original_context = dict(task.manager_context)
        self._fresh('I plan to open an online store to resell your clothes. What are your wholesale partnership terms?')
        second = self._send('I will forward your questions about wholesale partnership terms and reselling our clothes online to our team for review.')
        with self.subTest('second native reply'):
            self._assert_sent(second)
        task.refresh_from_db()
        with self.subTest('distinct cooperation subtype preserves original brief'):
            self.assertEqual(task.manager_context, original_context)
            self.assertEqual(IgFollowUpTask.objects.filter(kind='manager_task', reason='revision_case:collaboration_review').count(), 2)
        self._finish_and_replay()

    def test_generic_manager_q2_sends_while_q1_remains_pending(self):
        task = self._start('Please connect me to a manager about account access.',
            'I will connect you with our manager about account access.')
        first_source, first_revision = self.source.pk, self.revision.pk
        self._fresh('Please connect me to a manager about an invoice copy.')
        second = self._send('I will connect you with our manager about an invoice copy.')
        with self.subTest('second native reply'):
            self._assert_sent(second)
        task.refresh_from_db()
        with self.subTest('original manager question remains represented'):
            self.assertNotIn(task.status, ('completed', 'cancelled'))
            self.assertIn(first_source, [row['message_id'] for row in task.manager_context['sources']])
            if IgFollowUpTask.objects.filter(kind='manager_task').count() == 1:
                self.assertEqual(task.manager_context['latest_revision_id'], first_revision)
        self._finish_and_replay()
