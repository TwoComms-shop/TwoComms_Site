"""Payment evidence remains distinct from commercial and shipment authority."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from management.services.ig_manual_order_reconciliation import reconcile_manager_paid_order
from management.services.ig_order_links import _commercial_fingerprint, order_fulfillment_payment_verified
from management.services.ig_order_fulfillment import _event_specs, _message
from orders.services.order_builder import _commercial_item_fingerprint


class ReceiptOrderBoundaryTests(SimpleTestCase):
    def _custom_item(self, color):
        return SimpleNamespace(
            product_id=None, color_variant_id=None, title='TWOCOMMS 1654',
            size='L', color_name_custom=color, is_custom=True,
            fit_option_code='oversize', option_values={'print': '1654'},
            qty=1, unit_price=Decimal('850'), line_total=Decimal('850'),
        )

    def test_off_catalog_color_is_part_of_both_commercial_identities(self):
        white, black = self._custom_item('Білий'), self._custom_item('Чорний')
        self.assertNotEqual(_commercial_item_fingerprint(white), _commercial_item_fingerprint(black))
        self.assertNotEqual(_commercial_fingerprint(white), _commercial_fingerprint(black))

    def test_unattributed_manual_paid_status_is_not_fulfillment_authority(self):
        order = SimpleNamespace(source='manual', payment_status='paid', payment_payload={})
        self.assertFalse(order_fulfillment_payment_verified(order))
        order.payment_payload = {'manual_payment_action': {
            'actor_id': 10, 'source': 'management_user', 'payment_status': 'paid',
        }}
        self.assertTrue(order_fulfillment_payment_verified(order))

    def test_ttn_creation_does_not_claim_carrier_received_parcel(self):
        order = SimpleNamespace(order_number='TWC-RECEIPT', pk=1)
        for locale, forbidden in [('en', 'on its way'), ('ru', 'уже в пути'), ('uk', 'вже в дорозі')]:
            message = _message('ttn_assigned', locale, order, '20400000000000')
            self.assertIn('20400000000000', message)
            self.assertNotIn(forbidden, message)

    def test_source_qualified_unpaid_manager_order_materializes_payment_ack(self):
        order = SimpleNamespace(
            pk=1, order_number='TWC-RECEIPT', tracking_number='',
            payment_status='unpaid', status='new', total_sum=Decimal('850'), discount_amount=0,
        )
        assignment = SimpleNamespace(pk=2, version=1, order=order, client=SimpleNamespace(language='uk'))
        with patch('management.services.ig_order_fulfillment.order_fulfillment_payment_verified', return_value=True), patch(
            'management.services.ig_order_fulfillment.nova_poshta_order_fulfillment_confirmed', return_value=False,
        ):
            specs = list(_event_specs(assignment, now=None))
        self.assertEqual([spec['kind'] for spec in specs], ['payment_confirmed'])
        self.assertEqual(specs[0]['payload']['payment_status'], 'unpaid')


from django.contrib.auth import get_user_model
from django.test import TestCase


class ApprovedReceiptAutoOrderTests(TestCase):
    def setUp(self):
        from management.models import IgClient, InstagramBotSettings
        from management.services.instagram_bot import ingress_provider_namespace
        self.settings, _ = InstagramBotSettings.objects.update_or_create(
            pk=1, defaults={'ig_user_id': 'receipt-auto-fixture', 'page_id': 'receipt-auto-fixture'},
        )
        self.namespace = ingress_provider_namespace(self.settings)
        self.manager = get_user_model().objects.create_user('receipt-auto-manager', is_staff=True)
        self.customer = IgClient.get_or_create_for_sender('receipt-auto-customer')

    def _review(self, *, incomplete=None, canonical=False, earlier_examples=False, photo_role='manager', private_photo=None):
        from copy import deepcopy
        from django.utils import timezone
        from management.models import InstagramBotMessage
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.services.ig_conversation_agreement import persist_conversation_agreement

        now = timezone.now()
        self.reference_examples = []
        if earlier_examples:
            for index in range(2):
                self.reference_examples.append(InstagramBotMessage.objects.create(
                    client=self.customer, sender_id=self.customer.igsid,
                    provider_namespace=self.namespace, role='user', text='', status='done',
                    source='webhook', provider_created_at=now,
                    provider_message_id=f'earlier-example-{index}', mid=f'earlier-example-{index}',
                    attachment_media=[{'type': 'image', 'role': 'product', 'url': f'https://fixture.invalid/example-{index}.jpg'}],
                ))
        texts = [
            ('user', 'Хочу одну футболку розмір L' if canonical else 'Хочу одну футболку'),
            ('manager', 'L білу оверсайз TWOCOMMS 1654'),
            (photo_role, ''),
            ('user', 'Так'),
            ('manager', '850 грн + 120 доставка = 970 грн'),
            ('user', 'ПІБ: Іван Іванов\nТелефон: 0931112233\nМісто: Київ\nВідділення: Відділення №4'),
        ]
        self.source_rows = []
        for index, (role, text) in enumerate(texts):
            media = [{'type': 'image', 'role': 'product', 'url': 'https://fixture.invalid/reference.jpg'}] if index == 2 else []
            lifecycle = {}
            if index == 2 and private_photo:
                from datetime import timedelta
                deadline = now + timedelta(hours=1)
                media = [{'type': 'image', 'role': 'product', 'mime': 'image/jpeg',
                    'source_part_id': 'mp1_' + 'a' * 32, 'content_hash': 'a' * 64,
                    'status': 'owned', 'capture_state': 'owned', 'private_storage': True,
                    'storage_name': 'fixture-only/accepted-reference.jpg'}]
                if private_photo != 'unknown':
                    media[0]['delete_after'] = deadline.isoformat()
                lifecycle = {'private_media_state': 'active',
                    'private_media_delete_after': deadline if private_photo != 'unknown' else None}
                if private_photo == 'expired_sibling':
                    media.append({**media[0], 'source_part_id': 'mp1_' + 'b' * 32,
                        'content_hash': 'b' * 64, 'storage_name': 'fixture-only/older-reference.jpg',
                        'delete_after': (now - timedelta(seconds=1)).isoformat()})
                if private_photo == 'deleting':
                    lifecycle['private_media_state'] = 'delete_pending'
            self.source_rows.append(InstagramBotMessage.objects.create(
                client=self.customer, sender_id=self.customer.igsid,
                provider_namespace=self.namespace,
                role=role, text=text, status='done',
                source='echo' if role == 'manager' else 'webhook',
                send_state='sent' if role in {'manager', 'model'} else '',
                provider_message_id=f'receipt-auto-{index}', mid=f'receipt-auto-{index}',
                provider_created_at=now,
                attachment_media=media, **lifecycle,
            ))
            if canonical and index == 0:
                from management.services.ig_commerce_state import apply_turn
                from management.services.ig_commerce_turns import parse_turn
                self.selection_decision = apply_turn(
                    self.customer, self.source_rows[0], parse_turn(text), reply_payload={},
                )
                self.customer.refresh_from_db()
        result = persist_conversation_agreement(self.customer, [*self.reference_examples, *self.source_rows], watermark=self.source_rows[-1].pk)
        self.assertTrue(result['persisted'], result)
        agreement = result['agreement']
        draft = {key: deepcopy(agreement.get(key)) for key in (
            'quoted_total', 'merchandise_total', 'delivery_amount', 'payable_total',
            'amount_source_message_id', 'items',
        )}
        draft['agreement'] = deepcopy(agreement)
        draft['delivery'] = {key: agreement['shipping'].get(key, '') for key in ('full_name', 'phone', 'city', 'office')}
        if incomplete:
            draft['items'][0][incomplete] = None
        return IgPaymentConfirmationReview.objects.create(
            client=self.customer, dedupe_key='approved-receipt-auto',
            evidence={'order_draft': draft}, watermark_message_id=self.source_rows[-1].pk,
        )

    def _approve(self, review):
        from management.services.ig_payment_review import record_review_decision
        return record_review_decision(
            review, actor=self.manager, decision='manager_verified',
            verification_scope='full_payment', confirmed_amount='970.00',
        )

    def test_complete_accepted_custom_order_is_created_by_approval_once(self):
        from orders.models import Order
        from orders.nova_poshta_documents import build_order_payment_snapshot
        from orders.services.ig_review_order_builder import create_order_from_payment_review
        from management.models import IgClient
        review = self._review()
        with patch('management.services.instagram_bot.send_text') as send:
            self._approve(review)
            review.refresh_from_db()
            self.assertIsNotNone(review.order_id)
            result = create_order_from_payment_review(review, actor=self.manager)
        send.assert_not_called()
        self.assertEqual(result['status'], 'already_created')
        self.assertEqual(Order.objects.count(), 1)
        order = review.order
        item = order.items.get()
        self.assertIsNone(item.product_id)
        self.assertTrue(item.is_custom)
        self.assertEqual(item.title, 'TWOCOMMS 1654')
        self.assertEqual(item.size, 'L')
        self.assertEqual(item.fit_option_code, 'oversize')
        self.assertEqual(item.color_name_custom, 'Білий')
        self.assertEqual(item.option_values['garment_type'], 'tshirt')
        self.assertEqual(item.option_values['_reference_message_ids'], sorted([self.source_rows[1].pk, self.source_rows[2].pk, self.source_rows[3].pk]))
        self.assertEqual(item.option_values['accepted_reference_message_ids'], [self.source_rows[2].pk])
        self.assertEqual(item.option_values['primary_reference_message_id'], self.source_rows[2].pk)
        self.assertEqual(item.unit_price, Decimal('850.00'))
        self.assertEqual(order.total_sum, Decimal('850.00'))
        self.assertEqual(order.payment_status, 'unpaid')
        self.assertEqual(order.payment_provider, '')
        self.assertFalse(order.payment_payload['provider_payment_confirmed'])
        snapshot = build_order_payment_snapshot(order)
        self.assertEqual(snapshot['payable_total'], '970.00')
        self.assertEqual(snapshot['declared_cost'], '850.00')
        self.assertEqual(snapshot['delivery_payer_type'], 'Sender')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.stage, IgClient.Stage.ORDER_CREATED)

    def test_accepted_seller_photo_remains_primary_after_examples_and_receipt(self):
        from django.utils import timezone
        from management.models import InstagramBotMessage
        from orders.models import Order
        review = self._review(earlier_examples=True)
        receipt = InstagramBotMessage.objects.create(
            client=self.customer, sender_id=self.customer.igsid,
            provider_namespace=self.namespace, role='user', text='Оплатив, ось квитанція',
            status='done', source='webhook', provider_created_at=timezone.now(),
            provider_message_id='late-receipt', mid='late-receipt',
            attachment_media=[{'type': 'image', 'role': 'receipt', 'payment_evidence': True,
                'url': 'https://fixture.invalid/receipt.jpg'}],
        )
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['status'], 'created', approved.order_creation_result)
        options = Order.objects.get().items.get().option_values
        self.assertEqual(options['accepted_reference_message_ids'], [self.source_rows[2].pk])
        self.assertEqual(options['primary_reference_message_id'], self.source_rows[2].pk)
        self.assertTrue(all(row.pk in options['_reference_message_ids'] for row in self.reference_examples))
        self.assertNotIn(receipt.pk, options['_reference_message_ids'])

    def test_live_owned_accepted_photo_allows_order_without_reading_private_bytes(self):
        from orders.models import Order
        review = self._review(private_photo='live')
        with patch('management.services.ig_private_media.private_media_storage') as storage:
            approved = self._approve(review)
        storage.assert_not_called()
        self.assertEqual(approved.order_creation_result['status'], 'created', approved.order_creation_result)
        self.assertEqual(Order.objects.get().items.get().option_values['primary_reference_message_id'], self.source_rows[2].pk)

    def test_expired_private_sibling_prevents_accepted_photo_order(self):
        from orders.models import Order
        review = self._review(private_photo='expired_sibling')
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['missing_fields'], ['accepted_reference_unavailable'])
        self.assertFalse(Order.objects.exists())

    def test_unknown_private_retention_prevents_accepted_photo_order(self):
        from orders.models import Order
        review = self._review(private_photo='unknown')
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['missing_fields'], ['accepted_reference_unavailable'])
        self.assertFalse(Order.objects.exists())

    def test_pending_deletion_prevents_accepted_photo_order(self):
        from orders.models import Order
        review = self._review(private_photo='deleting')
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['missing_fields'], ['accepted_reference_unavailable'])
        self.assertFalse(Order.objects.exists())

    def test_native_sent_model_photo_becomes_accepted_primary(self):
        from orders.models import Order
        review = self._review(photo_role='model')
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['status'], 'created', approved.order_creation_result)
        options = Order.objects.get().items.get().option_values
        self.assertEqual(options['accepted_reference_message_ids'], [self.source_rows[2].pk])
        self.assertEqual(options['primary_reference_message_id'], self.source_rows[2].pk)

    def test_model_photo_without_sent_receipt_cannot_be_accepted_primary(self):
        from orders.models import Order
        review = self._review(photo_role='model')
        photo = self.source_rows[2]
        photo.send_state = 'pending'
        photo.save(update_fields=['send_state'])
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['status'], 'needs_manual_completion')
        self.assertIn('conversation_agreement_source_changed', approved.order_creation_result['missing_fields'])
        self.assertFalse(Order.objects.exists())

    def test_changed_accepted_photo_requires_manual_completion(self):
        from orders.models import Order
        review = self._review(earlier_examples=True)
        photo = self.source_rows[2]
        photo.attachment_media = [{'type': 'image', 'role': 'product', 'url': 'https://fixture.invalid/replacement.jpg'}]
        photo.save(update_fields=['attachment_media'])
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['status'], 'needs_manual_completion')
        self.assertIn('conversation_agreement_source_changed', approved.order_creation_result['missing_fields'])
        self.assertFalse(Order.objects.exists())

    def test_unbound_approved_photo_cannot_be_injected_into_draft(self):
        from copy import deepcopy
        from orders.models import Order
        review = self._review(earlier_examples=True)
        evidence = deepcopy(review.evidence)
        evidence['order_draft']['items'][0]['accepted_reference_message_ids'] = [self.reference_examples[0].pk]
        review.evidence = evidence
        review.save(update_fields=['evidence', 'updated_at'])
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['status'], 'needs_manual_completion')
        self.assertIn('conversation_agreement_draft_changed', approved.order_creation_result['missing_fields'])
        self.assertFalse(Order.objects.exists())

    def test_receipt_without_manager_decision_never_creates_order(self):
        from orders.models import Order
        from orders.services.ig_review_order_builder import create_order_from_payment_review
        result = create_order_from_payment_review(self._review(), actor=self.manager)
        self.assertEqual(result['status'], 'needs_manual_completion')
        self.assertIn('manager_payment_decision', result['missing_fields'])
        self.assertFalse(Order.objects.exists())

    def test_approved_receipt_missing_type_requires_manual_completion(self):
        from orders.models import Order
        from orders.services.ig_review_order_builder import create_order_from_payment_review
        review = self._review(incomplete='garment_type')
        approved = self._approve(review)
        result = approved.order_creation_result
        self.assertEqual(result['status'], 'needs_manual_completion')
        self.assertIn('conversation_agreement_draft_changed', result['missing_fields'])
        self.assertFalse(Order.objects.exists())

    def test_approved_receipt_missing_quantity_never_uses_default_one(self):
        from orders.models import Order
        from orders.services.ig_review_order_builder import create_order_from_payment_review
        review = self._review(incomplete='qty')
        approved = self._approve(review)
        result = approved.order_creation_result
        self.assertEqual(result['status'], 'needs_manual_completion')
        self.assertIn('conversation_agreement_draft_changed', result['missing_fields'])
        self.assertFalse(Order.objects.exists())

    def test_partial_manual_order_repair_never_invents_full_payment(self):
        order = SimpleNamespace(payment_status='prepaid', source='manual', sale_source='Instagram')
        with self.assertRaisesMessage(ValueError, 'для передоплати потрібні точна сума'):
            reconcile_manager_paid_order(
                client=self.customer, order=order, actor=self.manager, evidence_message_id=3285,
            )

    def test_changed_seller_source_cannot_authorize_automatic_order(self):
        from management.models import InstagramBotMessage
        from orders.models import Order
        from orders.services.ig_review_order_builder import create_order_from_payment_review
        review = self._review()
        InstagramBotMessage.objects.filter(pk=self.source_rows[1].pk).update(text='M чорну оверсайз TWOCOMMS 1654')
        approved = self._approve(review)
        result = approved.order_creation_result
        self.assertEqual(result['status'], 'needs_manual_completion')
        self.assertIn('conversation_agreement_source_changed', result['missing_fields'])
        self.assertFalse(Order.objects.exists())

    def test_changed_active_account_namespace_blocks_old_source_order(self):
        from orders.models import Order
        from orders.services.ig_review_order_builder import create_order_from_payment_review
        review = self._review()
        self.settings.ig_user_id = self.settings.page_id = 'different-receipt-account'
        self.settings.save(update_fields=['ig_user_id', 'page_id'])
        approved = self._approve(review)
        result = approved.order_creation_result
        self.assertEqual(result['status'], 'needs_manual_completion')
        self.assertIn('conversation_agreement_current_namespace_changed', result['missing_fields'])
        self.assertFalse(Order.objects.exists())

    def test_newer_unobserved_accepted_offer_invalidates_older_review_order(self):
        from django.utils import timezone
        from management.models import InstagramBotMessage
        from orders.models import Order
        from orders.services.ig_review_order_builder import create_order_from_payment_review
        review = self._review()
        for index, (role, text) in enumerate([('manager', 'M чорну оверсайз TWOCOMMS 1654'), ('user', 'Так')]):
            InstagramBotMessage.objects.create(
                client=self.customer, sender_id=self.customer.igsid, provider_namespace=self.namespace,
                role=role, text=text, source='echo' if role == 'manager' else 'webhook', status='done',
                send_state='sent' if role == 'manager' else '', provider_message_id=f'newer-auto-{index}',
                provider_created_at=timezone.now(),
            )
        approved = self._approve(review)
        result = approved.order_creation_result
        self.assertEqual(result['status'], 'needs_manual_completion')
        self.assertFalse(Order.objects.exists())

    def test_completion_preview_performs_no_persistent_writes(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from orders.services.ig_review_order_builder import payment_review_order_completion_requirements
        review = self._review()
        with CaptureQueriesContext(connection) as captured:
            result = payment_review_order_completion_requirements(review)
        self.assertEqual(result['status'], 'needs_manual_completion')
        self.assertTrue(all(query['sql'].lstrip().upper().startswith('SELECT') for query in captured))

    def _correct_canonical_size(self, *, operation='set', value='M'):
        import uuid
        from django.contrib.auth.models import Permission
        from management.services.ig_admin_state_capture import current_admin_state
        from management.services.ig_selection_corrections import CAPABILITIES, save_size_correction

        self.manager.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='management',
            codename__in=[permission.split('.')[1] for permission in CAPABILITIES],
        ))
        captured = current_admin_state(self.customer.pk)
        candidate = captured.state.as_dict()['boundary']['size_correction_context']
        self.assertEqual(captured.status, 'captured', captured.reason)
        self.assertTrue(candidate['available'], candidate)
        result = save_size_correction(
            self.customer.pk, actor=self.manager, operation_id=uuid.uuid4(),
            expected_selection_revision=candidate['context']['selection_revision'],
            expected_context_digest=candidate['context_digest'],
            operation=operation, value=value,
        )
        self.assertEqual(result.status, 'applied')
        return result

    def test_audited_size_change_prevents_old_accepted_size_auto_order(self):
        from orders.models import Order
        review = self._review(canonical=True)
        self._correct_canonical_size(value='M')
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['status'], 'needs_manual_completion')
        self.assertEqual(approved.order_creation_result['missing_fields'], ['configuration_correction'])
        self.assertFalse(Order.objects.exists())
        self.source_rows[0].refresh_from_db()
        self.assertEqual(self.source_rows[0].text, 'Хочу одну футболку розмір L')

    def test_audited_size_clear_prevents_old_accepted_size_auto_order(self):
        from orders.models import Order
        review = self._review(canonical=True)
        self._correct_canonical_size(operation='clear', value=None)
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['status'], 'needs_manual_completion')
        self.assertEqual(approved.order_creation_result['missing_fields'], ['configuration_correction'])
        self.assertFalse(Order.objects.exists())

    def test_valid_audited_size_matching_accepted_offer_allows_auto_order(self):
        from orders.models import Order
        review = self._review(canonical=True)
        self._correct_canonical_size(value='M')
        self._correct_canonical_size(value='L')
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['status'], 'created', approved.order_creation_result)
        self.assertEqual(Order.objects.get().items.get().size, 'L')

    def test_invalid_audited_size_proof_cannot_revive_old_agreement(self):
        from copy import deepcopy
        from management.services.ig_commerce_state import persist_size_correction_transition
        from orders.models import Order
        review = self._review(canonical=True)
        def append_invalid_proof(session, source, *, operation_id, receipt):
            receipt = deepcopy(receipt)
            receipt['context_digest'] = '0' * 64
            return persist_size_correction_transition(
                session, source, operation_id=operation_id, receipt=receipt,
            )
        # Inject invalid provenance at the append boundary; immutable history
        # remains immutable once persisted.
        with patch('management.services.ig_commerce_state.persist_size_correction_transition', side_effect=append_invalid_proof):
            self._correct_canonical_size(value='M')
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['missing_fields'], ['configuration_correction'])
        self.assertFalse(Order.objects.exists())

    def test_audited_size_cannot_be_applied_to_ambiguous_multiple_order_lines(self):
        from copy import deepcopy
        from orders.models import Order
        review = self._review(canonical=True)
        self._correct_canonical_size(value='M')
        evidence = deepcopy(review.evidence)
        evidence['order_draft']['items'].append(deepcopy(evidence['order_draft']['items'][0]))
        review.evidence = evidence
        review.save(update_fields=['evidence', 'updated_at'])
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['missing_fields'], ['configuration_correction'])
        self.assertFalse(Order.objects.exists())

    def test_retained_accepted_order_survives_more_than_160_later_noise_rows(self):
        from django.utils import timezone
        from management.models import InstagramBotMessage
        from management.services.ig_conversation_agreement import persist_conversation_agreement
        from orders.models import Order
        review = self._review()
        for batch in range(9):
            rows = [InstagramBotMessage.objects.create(
                client=self.customer, sender_id=self.customer.igsid, provider_namespace=self.namespace,
                role='user', source='webhook', status='done', text='Вітаю.',
                provider_created_at=timezone.now(), mid=f'retained-noise-{batch}-{index}',
            ) for index in range(20)]
            result = persist_conversation_agreement(self.customer, rows, watermark=rows[-1].pk)
            self.assertTrue(result['persisted'], result)
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['status'], 'created', approved.order_creation_result)
        item = Order.objects.get().items.get()
        self.assertEqual(item.title, 'TWOCOMMS 1654')
        self.assertEqual(item.size, 'L')
        self.assertEqual(item.unit_price, Decimal('850.00'))

    def test_unobserved_source_overflow_requires_manual_completion(self):
        from django.utils import timezone
        from management.models import InstagramBotMessage
        from orders.models import Order
        review = self._review()
        for index in range(161):
            InstagramBotMessage.objects.create(
                client=self.customer, sender_id=self.customer.igsid, provider_namespace=self.namespace,
                role='user', source='webhook', status='done', text='Вітаю.',
                provider_created_at=timezone.now(), mid=f'unobserved-overflow-{index}',
            )
        approved = self._approve(review)
        self.assertEqual(approved.order_creation_result['status'], 'needs_manual_completion')
        self.assertIn('conversation_agreement_current_sources_overflow', approved.order_creation_result['missing_fields'])
        self.assertFalse(Order.objects.exists())
