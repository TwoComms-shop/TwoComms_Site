"""Source choice display is independent of catalogue/checkout authority."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import (
    IgClient, IgCommercialEpisode, IgCommerceSelectionSession,
    IgCommerceSelectionTransition, IgCommerceTurnDecision,
    IgFunnelResetAudit, InstagramBotMessage,
)
from management.services.ig_commerce_projection import captured_selection_for
from management.services.ig_journey_readiness import selection_requirements
from management.services.ig_journey_selection import selection_fields, source_selection_fields
from management.services.ig_journey_snapshot import build_journey_snapshot


class SelectionSourceProjectionTests(TestCase):
    def setUp(self):
        self.customer = IgClient.objects.create(igsid='source-choice-fixture', language='uk')
        self.episode = IgCommercialEpisode.objects.create(
            client=self.customer, sequence=1, materialization_key='source-choice-episode',
        )
        self.customer.current_commercial_episode = self.episode
        self.customer.save(update_fields=['current_commercial_episode'])
        self.session = IgCommerceSelectionSession.objects.create(
            client=self.customer, commercial_episode=self.episode, generation=1,
            lines=[{'line_id': 'source-line:0', 'quantity': 1}],
        )

    def record(self, text, *, line=None, constraints=None, request=None, action='selection_updated'):
        previous = deepcopy(self.session.snapshot())
        if line is not None:
            self.session.lines = [line]
        if constraints is not None:
            self.session.query_constraints = constraints
        self.session.revision += 1
        self.session.save()
        source = InstagramBotMessage.objects.create(
            client=self.customer, sender_id=self.customer.igsid, role='user', source='webhook',
            text=text, mid=f'source-choice-{self.session.revision}', provider_created_at=timezone.now(),
        )
        transition = IgCommerceSelectionTransition.objects.create(
            session=self.session, source_message=source, from_revision=self.session.revision - 1,
            to_revision=self.session.revision, previous_snapshot=previous,
            next_snapshot=self.session.snapshot(), action=action, source_order_key=str(source.pk),
        )
        decision = IgCommerceTurnDecision.objects.create(
            session=self.session, source_message=source, transition=transition,
            request_payload=request or {}, accepted=True,
        )
        return source, decision

    def size(self):
        return self.record('L', line={'line_id': 'source-line:0', 'size': 'L', 'quantity': 1},
                           request={'field_updates': {'size': 'L'}})

    def capture(self, **kwargs):
        return captured_selection_for(self.customer, **kwargs)

    def reduce_source(self, text, *, resolve_identity=False):
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import parse_turn
        from management.services.ig_commerce_source_identity import resolve_source_product_request
        source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role='user', source='webhook', text=text, mid=f'reduce-{InstagramBotMessage.objects.count()}',
            provider_created_at=timezone.now())
        request = parse_turn(text)
        if resolve_identity:
            request = resolve_source_product_request(self.customer, source, request)
        decision = apply_turn(self.customer, source, request, reply_payload={})
        return source, decision

    def test_partial_size_read_only_visible_without_fake_catalogue_denominator(self):
        source, _ = self.size()
        with CaptureQueriesContext(connection) as queries:
            captured = self.capture()
            with patch('management.services.ig_journey_readiness.selection_readiness') as catalog:
                result = selection_requirements(client_id=self.customer.pk,
                    episode_id=self.episode.pk, source_selection=captured)
        self.assertTrue(all(query['sql'].lstrip().upper().startswith('SELECT') for query in queries))
        catalog.assert_not_called()
        self.assertIsNone(result['requirements'])
        self.assertIsNone(result['selection_fields']['total'])
        self.assertEqual(result['reason'], 'source_choice_only')
        size = next(row for row in result['selection_fields']['items'] if row['key'] == 'size')
        self.assertEqual((size['value'], size['choice_status'], size['availability']), ('L', 'confirmed', 'unknown'))
        self.assertEqual(size['source']['source_message_id'], source.pk)

    def test_first_product_identity_preserves_original_choice_source(self):
        source, _ = self.size()
        self.record('Обираю цей товар', line={**self.session.lines[0], 'product_id': 731},
                    request={'exact_product_id': 731}, action='product_selected')
        captured = self.capture()
        self.assertEqual(captured['product_id'], 731)
        self.assertEqual(captured['values']['size'], 'L')
        self.assertEqual(captured['evidence']['size']['source_message_id'], source.pk)
        self.assertEqual(captured['fields']['size']['availability'], 'unknown')

    def test_product_switch_cannot_inherit_old_size_proof(self):
        self.size()
        self.record('Обираю цей товар', line={**self.session.lines[0], 'product_id': 731},
                    request={'exact_product_id': 731}, action='product_selected')
        self.record('Інший товар', line={**self.session.lines[0], 'product_id': 732},
                    request={'exact_product_id': 732}, action='product_selected')
        self.assertEqual(self.capture(), {})

    def test_reset_erasure_wrong_episode_foreign_line_and_revisionless_edit_abstain(self):
        source, _ = self.size()
        self.assertEqual(self.capture(episode_id=self.episode.pk + 1), {})
        self.assertEqual(self.capture(line_id='another-line'), {})
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=source.pk)
        self.assertEqual(self.capture(), {})
        IgFunnelResetAudit.objects.filter(client=self.customer).delete()
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=timezone.now())
        self.assertEqual(self.capture(), {})
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=None)
        IgCommerceSelectionSession.objects.filter(pk=self.session.pk).update(
            lines=[{**self.session.lines[0], 'size': 'XL'}],
        )
        self.assertEqual(self.capture(), {})

    def test_snapshot_stays_captured_while_current_correction_and_history_are_separate(self):
        self.size()
        old_capture = self.capture()
        self.record('XL', line={**self.session.lines[0], 'size': 'XL'},
                    request={'field_updates': {'size': 'XL'}})
        self.assertEqual(old_capture['values']['size'], 'L')
        self.assertEqual(self.capture()['values']['size'], 'XL')
        old_episode = IgCommercialEpisode.objects.create(client=self.customer, sequence=2,
            materialization_key='source-choice-history', open_slot=None)
        graph = build_journey_snapshot(self.customer, view_episode_id=old_episode.pk)['graph']
        self.assertFalse(any('source_selection' in node for node in graph['nodes']))

    @override_settings(ROOT_URLCONF='twocomms.urls_management')
    def test_current_journey_and_card_share_captured_partial_object(self):
        from management.bot_views import _client_card
        self.size()
        captured = self.capture()
        card = _client_card(self.customer, source_selection=captured)
        journey = build_journey_snapshot(self.customer, source_selection=captured)
        node = next(node for node in journey['graph']['nodes'] if node['id'] == 'guide:selection')
        self.assertEqual(card['current_size'], 'L')
        self.assertEqual(card['source_selection'], node['source_selection'])
        self.assertIsNone(node['selection_fields']['total'])

    def test_source_choice_never_completes_unavailable_size_gate(self):
        self.size()
        captured = self.capture()
        readiness = {'has_product': True, 'applicability_known': True,
                     'product': {'title': 'Test', 'kind': 'Футболка'},
                     'size': {'required': True, 'requested_unavailable': 'L'}}
        result = selection_fields(readiness, scope=captured['scope'],
            evidence_refs=[{'kind': 'message', 'id': 1}], source_selection=captured)
        size = next(row for row in result['items'] if row['key'] == 'size')
        self.assertEqual((size['value'], size['choice_status'], size['status'], size['availability']),
                         ('L', 'confirmed', 'invalidated', 'unavailable'))

    def test_sources_erased_or_changed_cannot_be_used_and_delivery_is_not_fact_admission(self):
        source, decision = self.size()
        for state in ('pending', 'not_required', 'sent', 'unknown'):
            IgCommerceTurnDecision.objects.filter(pk=decision.pk).update(delivery_state=state)
            self.assertEqual(self.capture()['values']['size'], 'L')
        InstagramBotMessage.objects.filter(pk=source.pk).update(text='Привіт')
        self.assertEqual(self.capture(), {})

    def test_no_session_does_not_bootstrap_and_no_episode_uses_only_unbound_session(self):
        IgCommerceSelectionSession.objects.filter(pk=self.session.pk).delete()
        self.assertEqual(self.capture(), {})
        self.assertFalse(IgCommerceSelectionSession.objects.filter(client=self.customer).exists())

    def test_purchase_source_and_size_survive_unrelated_noop_without_send_authority(self):
        source, _ = self.reduce_source('Хочу замовити розмір л')
        self.reduce_source('А чи шукаєте ви працівників?')
        captured = self.capture()
        self.assertEqual(captured['values']['size'], 'L')
        self.assertIs(captured['values']['purchase_requested'], True)
        self.assertEqual(captured['evidence']['purchase_requested']['source_message_id'], source.pk)
        self.assertEqual(captured['fields']['purchase_requested']['availability'], 'unknown')

    @override_settings(ROOT_URLCONF='twocomms.urls_management')
    def test_partial_card_displays_localized_source_choices_before_product_identity(self):
        from management.bot_views import _client_card
        source, _ = self.reduce_source('Хочу замовити футболку розмір л')
        captured = self.capture()
        self.assertEqual(captured['values']['garment_type'], 'tshirt')
        card = _client_card(self.customer, source_selection=captured)
        choices = {row['key']: row for row in card['source_selection_display']['items']}
        self.assertEqual(choices['kind']['value'], 'Футболка')
        self.assertEqual(choices['size']['value'], 'L')
        self.assertEqual(choices['purchase_requested']['value'], 'Так')
        self.assertEqual(choices['kind']['source']['source_message_id'], source.pk)
        self.assertIsNone(card['source_selection_display']['total'])
        self.assertEqual(choices['size']['availability'], 'unknown')
        self.assertFalse(card['current_product_title'])

    def test_named_ambiguity_remains_visible_with_original_size_and_intent_sources(self):
        from storefront.models import Category, Product
        category = Category.objects.create(name='Футболки', slug='projection-model-fixture')
        for index, title in enumerate(('Choice Alpha', 'Choice Beta')):
            Product.objects.create(title=title, slug=f'projection-choice-{index}', category=category,
                                   price=800, status='published')
        name_source, _ = self.reduce_source('Choice Alpha або Choice Beta', resolve_identity=True)
        size_source, _ = self.reduce_source('Хочу замовити розмір L')
        captured = self.capture()
        self.assertEqual(captured['values']['model_query'], 'Choice Alpha або Choice Beta')
        self.assertEqual(captured['fields']['model_query']['status'], 'ambiguous')
        self.assertEqual(captured['evidence']['model_query']['source_message_id'], name_source.pk)
        self.assertEqual(captured['evidence']['size']['source_message_id'], size_source.pk)
        self.assertIsNone(source_selection_fields(captured)['total'])

    def test_recipient_switch_and_size_withdrawal_cannot_restore_old_choice(self):
        self.reduce_source('Хочу заказать размер L')
        self.reduce_source('Для друга размер M')
        captured = self.capture()
        self.assertEqual(captured['scope']['recipient_id'], 'friend')
        self.assertEqual(captured['values']['size'], 'M')
        self.assertNotIn('purchase_requested', captured['values'])
        self.reduce_source('не M')
        session = IgCommerceSelectionSession.objects.get(pk=self.session.pk)
        session.lines[0]['size'] = 'M'
        session.save(update_fields=['lines'])
        self.assertNotIn('size', self.capture().get('values', {}))

    @override_settings(ROOT_URLCONF='twocomms.urls_management')
    def test_complete_selection_source_mutation_removes_current_badge_card_and_cart_choice(self):
        from management.bot_views import _client_card
        from management.services.ig_journey_cart import selection_cart
        from management.tests_ig_funnel_nodes import readiness_stub
        from storefront.models import Category, Product
        category = Category.objects.create(name='Футболки', slug='source-mutated-complete')
        product = Product.objects.create(title='Source Complete', slug='source-mutated-complete',
            category=category, price=800, status='published')
        self.session.lines = []
        self.session.save(update_fields=['lines'])
        source, _ = self.reduce_source('Хочу замовити Source Complete розмір L', resolve_identity=True)
        self.customer.refresh_from_db()
        state = readiness_stub(applicability_known=True,
            product={'id': product.pk, 'title': product.title, 'kind': 'Футболка'},
            size={'required': True, 'selected': 'L', 'available': ['L']})
        with patch('management.services.ig_journey_readiness.selection_readiness', return_value=state), \
             patch('management.services.ig_journey_cart._price', return_value='800.00'):
            before = selection_requirements(client_id=self.customer.pk, episode_id=self.episode.pk)
            self.assertEqual(before['requirements']['completed'], before['requirements']['total'])
            self.assertEqual(_client_card(self.customer)['current_size'], 'L')
            self.assertIsNotNone(selection_cart(client_id=self.customer.pk, episode_id=self.episode.pk))
            InstagramBotMessage.objects.filter(pk=source.pk).update(text='не L')
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.capture(), {})
                after = selection_requirements(client_id=self.customer.pk, episode_id=self.episode.pk)
                card = _client_card(self.customer)
                cart = selection_cart(client_id=self.customer.pk, episode_id=self.episode.pk)
            self.assertTrue(all(query['sql'].lstrip().upper().startswith('SELECT') for query in queries))
        self.assertIsNone(after['requirements'])
        self.assertEqual(after['reason'], 'no_owned_current_source')
        self.assertEqual(card['current_size'], '')
        self.assertEqual(card['current_product_title'], '')
        self.assertIsNone(card['current_product_id'])
        self.assertIsNone(card['current_qty'])
        self.assertIsNone(card['source_selection_display'])
        self.assertEqual(card['unverified_selection_snapshot']['size'], 'L')
        self.assertEqual(card['unverified_selection_snapshot']['status'], 'unverified')
        self.assertIsNone(cart)

    def test_source_text_mutation_while_catalogue_is_read_discards_complete_badge(self):
        from management.tests_ig_funnel_nodes import readiness_stub
        self.record('L', line={'line_id': 'source-line:0', 'product_id': 731, 'size': 'L'},
                    request={'field_updates': {'size': 'L'}})
        source = InstagramBotMessage.objects.order_by('-pk').first()
        def catalog(**kwargs):
            from management.services import ig_journey_readiness as reader
            with patch.object(reader.connection, 'execute_wrappers', []):
                InstagramBotMessage.objects.filter(pk=source.pk).update(text='не L')
            return readiness_stub(applicability_known=True)
        with patch('management.services.ig_journey_readiness.selection_readiness', side_effect=catalog):
            result = selection_requirements(client_id=self.customer.pk, episode_id=self.episode.pk)
        self.assertIsNone(result['requirements'])
        self.assertEqual(result['reason'], 'source_changed')


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class AuthorizedSelectionProjectionTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        from management.tests_ig_revision_actions import RevisionSelectionActionTests
        fixture_class = RevisionSelectionActionTests
        if self._testMethodName == 'test_post_action_source_rebind_uses_private_atomic_candidate_and_persists_one_receipt':
            from management.services.ig_revision_authority import (
                CLAIM_CATALOG_CONFIGURATION, CLAIM_SOURCE_PREFERENCES, build_revision_authority_bindings,
            )
            from management.services.ig_commerce_state import apply_turn
            from management.services.ig_commerce_turns import parse_turn

            class SourceFixture(RevisionSelectionActionTests):
                def _source(inner, mid):
                    source = super()._source(mid)
                    source.text = 'Хочу заказать размер L'
                    source.save(update_fields=['text'])
                    apply_turn(inner.client_row, source, parse_turn(source.text), reply_payload={})
                    inner.client_row.refresh_from_db()
                    return source

                def _authority(inner):
                    return build_revision_authority_bindings(inner.client_row,
                        claims=(CLAIM_CATALOG_CONFIGURATION, CLAIM_SOURCE_PREFERENCES),
                        control={'product': inner.product.pk, 'variant': inner.variant.pk, 'qty': 2},
                        server_authorized_actions=('client_configuration_update',))

            fixture_class = SourceFixture
        self.fixture = fixture_class(
            methodName='test_bound_product_and_variant_apply_atomically_and_require_rebuild')
        self.fixture.setUp()

    def test_first_action_read_model_uses_owned_cas_receipt_without_inventing_quantity_choice(self):
        result = self.fixture._apply()
        self.assertTrue(result.applied, result.reasons)
        captured = captured_selection_for(self.fixture.client_row)
        self.assertEqual(captured['values']['product_id'], self.fixture.product.pk)
        self.assertNotIn('quantity', captured['values'])
        self.assertEqual(captured['fields']['product_id']['authority'], 'validated_selection_action')
        self.assertEqual(captured['evidence']['product_id']['source_message_id'], self.fixture.source.pk)

    def test_missing_or_changed_action_receipt_cannot_authorize_current_selection(self):
        self.assertTrue(self.fixture._apply().applied)
        self.fixture.revision.refresh_from_db()
        receipt = deepcopy(self.fixture.revision.action_receipts)
        receipt['client_configuration_update']['selection_session']['source_message_id'] += 1
        # Corruption reproduction bypasses the model's append-only guard;
        # ordinary writers correctly reject this mutation.
        from management.models import IgCustomerTurnRevision
        with connection.cursor() as cursor:
            cursor.execute(f'UPDATE {IgCustomerTurnRevision._meta.db_table} SET action_receipts = %s WHERE id = %s',
                           [json.dumps(receipt), self.fixture.revision.pk])
        self.assertEqual(captured_selection_for(self.fixture.client_row), {})

    def test_post_action_source_rebind_uses_private_atomic_candidate_and_persists_one_receipt(self):
        fixture = self.fixture
        before = captured_selection_for(fixture.client_row)
        result = fixture._apply()
        self.assertTrue(result.applied, result.reasons)
        captured = captured_selection_for(fixture.client_row)
        self.assertEqual(captured['values']['size'], 'L')
        self.assertEqual(captured['evidence']['size']['transition_id'], before['evidence']['size']['transition_id'])
        self.assertEqual(captured['values']['product_id'], fixture.product.pk)
        fixture.revision.refresh_from_db()
        self.assertIn('client_configuration_update', fixture.revision.action_receipts)
        from management.services.ig_commerce_projection import _pending_action_projection
        self.assertIsNone(_pending_action_projection.get())
