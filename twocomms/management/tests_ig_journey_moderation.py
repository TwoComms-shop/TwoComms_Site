from copy import deepcopy
from django.test import TestCase
from management.models import IgClient, IgFunnelResetAudit, InstagramBotMessage
from management.services.ig_journey_moderation import append_moderation


class JourneyModerationTests(TestCase):
    def setUp(self):
        self.customer = IgClient.get_or_create_for_sender('journey-moderation')
        self.graph = {'nodes': [{'id': 'entry', 'semantic_key': 'inbound'}], 'edges': []}

    def project(self, **kwargs):
        return append_moderation(self.graph, client_id=self.customer.pk, is_history=kwargs.get('history', False))

    def mark(self, **values):
        IgClient.objects.filter(pk=self.customer.pk).update(**values)

    def test_normal_pause_is_not_called_spam_and_history_never_uses_current_flags(self):
        self.mark(bot_paused=True, paused_reason='manager')
        self.assertEqual(self.project(), self.graph)
        self.mark(spam_strikes=3, paused_reason='spam')
        with self.assertNumQueries(0):
            self.assertEqual(self.project(history=True), self.graph)

    def test_strike_does_not_imply_warning_or_pause(self):
        self.mark(spam_strikes=1)
        before = deepcopy(self.graph)
        graph = self.project(); p = graph['nodes'][-1]['moderation_progress']
        self.assertEqual(p['warning']['status'], 'unknown')
        self.assertEqual(p['processing']['status'], 'active')
        self.assertEqual(graph['edges'][-1]['relation'], 'moderation_context')
        self.assertEqual(self.graph, before)
        self.assertEqual(append_moderation(graph, client_id=self.customer.pk, is_history=False), graph)

    def test_warning_needs_delivered_outgoing_source_after_reset(self):
        self.mark(spam_strikes=3, bot_paused=True, paused_reason='spam', stage='spam')
        warning = InstagramBotMessage.objects.create(client=self.customer, sender_id='journey-moderation', role='model', text='Попереджаємо: за повторний спам заблокуємо діалог.', provider_message_id='sent-1', status='done')
        p = self.project()['nodes'][-1]['moderation_progress']
        self.assertEqual(p['warning']['status'], 'sent')
        self.assertEqual(p['processing']['status'], 'stopped')
        self.assertEqual(p['warning']['evidence_refs'][0]['id'], warning.pk)
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=warning.pk)
        self.assertEqual(self.project()['nodes'][-1]['moderation_progress']['warning']['status'], 'unknown')

    def test_unsent_warning_and_user_quote_do_not_complete_warning(self):
        self.mark(stage='spam')
        for role, provider in [('user', 'inbound'), ('model', '')]:
            InstagramBotMessage.objects.create(client=self.customer, sender_id='journey-moderation', role=role, text='Попередження за спам', provider_message_id=provider)
        self.assertEqual(self.project()['nodes'][-1]['moderation_progress']['warning']['status'], 'unknown')
