from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase
from management.models import IgClient, BotAdCampaign, IgFunnelResetAudit, InstagramBotMessage, IgTurnRevisionSource
from management.services.ig_ad_referral import AdReferralResolution
from management.services.ig_journey_ad_entry import append_ad_entry


class JourneyAdEntryTests(TestCase):
    def setUp(self):
        self.client = IgClient.get_or_create_for_sender('journey-ad')
        self.client.ad_id = 'ad-123'
        self.graph = {'nodes': [{'id': 'entry', 'semantic_key': 'inbound', 'facts': []}], 'edges': []}

    def project(self, **options):
        return append_ad_entry(self.graph, client=self.client, is_history=options.get('is_history', False))

    def test_unknown_and_duplicate_mapping_keep_entry_without_invented_product(self):
        before = deepcopy(self.graph)
        result = self.project()
        self.assertEqual(result['nodes'][0]['ad_entry']['resolution'], 'unavailable')
        self.assertIsNone(result['nodes'][0]['ad_entry']['product_id'])
        self.assertEqual(result['edges'][0]['relation'], 'advertising_attribution')
        self.assertEqual(result['nodes'][-1]['semantic_key'], 'advertising_entry')
        self.assertEqual(append_ad_entry(result, client=self.client, is_history=False), result)
        self.assertEqual(self.graph, before)
        from management.services.ig_journey_snapshot import build_journey_snapshot
        InstagramBotMessage.objects.create(client=self.client, sender_id='journey-ad', role='user', text='From ad', status='done')
        snapshot = build_journey_snapshot(self.client)
        self.assertTrue(any(n.get('ad_entry') for n in snapshot['graph']['nodes']))
        for theme in ['A', 'B']:
            BotAdCampaign.objects.create(ad_id='ad-123', theme=theme)
        self.assertEqual(self.project()['nodes'][0]['ad_entry']['resolution'], 'ambiguous')

    def test_product_requires_authoritative_resolver(self):
        campaign = SimpleNamespace(product_id=7, product=SimpleNamespace(title='Known product'))
        with patch('management.services.ig_journey_ad_entry.resolve_ad_referral', return_value=AdReferralResolution('resolved', campaign=campaign)):
            node = self.project()['nodes'][0]
        self.assertEqual(node['ad_entry']['product_id'], 7)
        self.assertEqual(node['facts'][-1]['evidence_refs'], [{'kind': 'product', 'id': 7}])
        self.assertEqual(node['semantic_key'], 'inbound')

    def test_history_erasure_and_absent_referral_do_not_query_or_project(self):
        with self.assertNumQueries(0):
            self.assertEqual(self.project(is_history=True), self.graph)
        self.client.ad_id = ''
        with self.assertNumQueries(0):
            self.assertEqual(self.project(), self.graph)
        self.client.ad_id = 'ad-123'
        self.client.privacy_erasure_started_at = 'now'
        with self.assertNumQueries(0):
            self.assertEqual(self.project(), self.graph)

    def test_reset_requires_new_referral_from_this_client(self):
        old = InstagramBotMessage.objects.create(client=self.client, sender_id='journey-ad', role='user', text='old', status='done')
        IgFunnelResetAudit.objects.create(client=self.client, reset_after_message_id=old.pk, reason='test')
        self.assertEqual(self.project(), self.graph)
        message = InstagramBotMessage.objects.create(client=self.client, sender_id='journey-ad', role='user', text='new', status='done')
        # Envelope FK intentionally has no database constraint; no pipeline is invoked.
        IgTurnRevisionSource.objects.create(revision_id=9001, message=message, ordinal=1, role='user', source_digest='test', referral={'source': 'ADS', 'ad_id': 'new-ad'})
        node = self.project()['nodes'][0]
        self.assertEqual(node['ad_entry']['scope'], 'message_after_reset')
        self.assertEqual(node['ad_entry']['evidence_refs'], [{'kind': 'message', 'id': message.pk}])
        message.status = 'failed'
        message.save(update_fields=['status'])
        self.assertEqual(self.project(), self.graph)

    def test_ref_only_requires_known_campaign_and_ad_context_can_prove_source(self):
        self.client.ad_id = ''
        self.client.ad_ref = 'opaque-ref'
        self.assertEqual(self.project(), self.graph)
        BotAdCampaign.objects.create(ref='opaque-ref', title='Launch', theme='hoodies')
        ad = self.project()['nodes'][-1]['ad_entry']
        self.assertEqual(ad['theme'], 'hoodies')
        self.assertIsNone(ad['product_id'])
        self.assertEqual(ad['intent_status'], 'not_inferred')
        self.client.ad_ref = ''
        self.client.referral_payload = {'ads_context_data': {'ad_title': 'Collection'}}
        ad = self.project()['nodes'][-1]['ad_entry']
        self.assertEqual(ad['title'], 'Collection')
        self.assertEqual(ad['resolution'], 'unavailable')

    def test_post_reset_ad_context_without_id_is_visible_but_not_a_product(self):
        old = InstagramBotMessage.objects.create(client=self.client, sender_id='journey-ad', role='user', text='old')
        IgFunnelResetAudit.objects.create(client=self.client, reset_after_message_id=old.pk)
        message = InstagramBotMessage.objects.create(client=self.client, sender_id='journey-ad', role='user', text='ціна?', status='done')
        IgTurnRevisionSource.objects.create(revision_id=9002, message=message, ordinal=1, role='user', source_digest='new', referral={'ads_context_data': {'ad_title': 'Нова колекція'}})
        ad = self.project()['nodes'][-1]['ad_entry']
        self.assertEqual(ad['scope'], 'message_after_reset')
        self.assertIsNone(ad['product_id'])
        self.assertEqual(ad['title'], 'Нова колекція')
