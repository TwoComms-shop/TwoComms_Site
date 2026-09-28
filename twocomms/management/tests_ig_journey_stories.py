from copy import deepcopy
from django.test import TestCase
from django.utils import timezone
from management.models import IgClient, InstagramBotMessage, IgFunnelResetAudit
from management.services.ig_journey_stories import append_stories


class JourneyStoryTests(TestCase):
    def setUp(self):
        self.customer = IgClient.get_or_create_for_sender('story-journey')
        self.graph = {'nodes': [{'id': 'entry', 'semantic_key': 'inbound'}], 'edges': []}

    def message(self, **kwargs):
        part = {'media_type': 'story_mention', 'source_part_id': 'part-a', 'content_hash': 'a'*64,
                'status': 'owned', 'provider_native_mention': True, 'target_username': 'twocomms'}
        part.update(kwargs.pop('part', {}))
        return InstagramBotMessage.objects.create(client=self.customer, sender_id='story-journey', role='user',
            provider_namespace='account:one', attachment_media=[part], **kwargs)

    def project(self, history=False):
        return append_stories(self.graph, client_id=self.customer.pk, is_history=history)

    def items(self):
        return self.project()['nodes'][-1]['story_interactions']['items']

    def test_separate_cycles_no_purchase_required_and_neighbour_reply_is_not_proof(self):
        self.message(mid='one', text='Купувала в Telegram')
        self.message(mid='two', part={'provider_native_mention': False, 'media_type': 'share'})
        InstagramBotMessage.objects.create(client=self.customer, sender_id='story-journey', role='model', text='Дякуємо!', status='done', provider_message_id='unrelated')
        before = deepcopy(self.graph)
        graph = self.project(); items = graph['nodes'][-1]['story_interactions']['items']
        self.assertEqual(len(items), 2)
        self.assertEqual([i['kind'] for i in items], ['share', 'mention'])
        self.assertTrue(all(not i['replies'] for i in items))
        self.assertEqual(self.graph, before)
        self.assertEqual(graph['edges'][-1]['relation'], 'story_context')
        self.assertEqual(append_stories(graph, client_id=self.customer.pk, is_history=False), graph)

    def test_owned_is_not_inspected_and_stale_hash_cannot_prove_analysis(self):
        self.message(part={'inspection': {'state': 'inspected', 'source_part_id': 'part-a', 'content_hash': 'b'*64, 'outcome': 'understood', 'type_code': 'selfie'}})
        self.assertTrue(self.items()[0]['media_available'])
        self.assertFalse(self.items()[0]['inspected'])
        self.assertEqual(self.items()[0]['theme'], '')

    def test_bound_inspection_and_exact_sent_reply_are_visible(self):
        self.message(mid='source', part={'inspection': {'state': 'inspected', 'source_part_id': 'part-a', 'content_hash': 'a'*64, 'outcome': 'understood', 'type_code': 'selfie'}})
        for namespace, status, receipt in [('account:other', 'done', 'wrong'), ('account:one', 'pending', 'unsent'), ('account:one', 'done', 'sent')]:
            InstagramBotMessage.objects.create(client=self.customer, sender_id='story-journey', role='model', text='Гарні фото!', reply_to_provider_message_id='source', provider_namespace=namespace, status=status, provider_message_id=receipt)
        item = self.items()[0]
        self.assertTrue(item['inspected'])
        self.assertEqual(item['theme'], 'Фото людей')
        self.assertEqual(len(item['replies']), 1)

    def test_reply_to_our_story_is_not_customer_mention(self):
        self.message(part={'media_type': 'story', 'interaction_kind': 'story_reply', 'provider_native_mention': False, 'status': 'metadata_only'})
        item = self.items()[0]
        self.assertEqual(item['kind'], 'reply')
        self.assertFalse(item['native_mention'])
        self.assertFalse(item['media_available'])

    def test_history_reset_erasure_and_deleted_media_are_fenced(self):
        row = self.message()
        with self.assertNumQueries(0):
            self.assertEqual(self.project(history=True), self.graph)
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=row.pk)
        self.assertEqual(self.project(), self.graph)
        self.message(private_media_state='deleted')
        self.assertEqual(self.project(), self.graph)
        self.message()
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=timezone.now())
        self.assertEqual(self.project(), self.graph)

    def test_bounded_repeat_history(self):
        for i in range(26):
            self.message(mid='repeat-'+str(i))
        data = self.project()['nodes'][-1]['story_interactions']
        self.assertEqual(data['count'], 24)
        self.assertTrue(data['truncated'])
