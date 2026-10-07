"""Real DB delivery boundaries with a fake Telegram transport; no provider traffic."""
import tempfile
from datetime import timedelta
from decimal import Decimal
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from orders.telegram_notifications import TelegramDeliveryReport, TelegramNotifier
from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
from storefront.custom_print_creation import prepare_creation, save_creation
from storefront.custom_print_notifications import (
    _build_creation_message, _claim_notification_slot, _collect_attachment_payloads,
    _deliver_notification, _telegram_message_parts,
)
from storefront.models import CustomPrintLeadAttachment
from storefront.tests.test_custom_print_creation import creation_payload


class StrictHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.text = [], []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, data):
        self.text.append(data)


def visible(part):
    parser = StrictHTML()
    parser.feed(part)
    parser.close()
    assert not parser.stack
    return ''.join(parser.text)


class CustomPrintDeliveryTests(TestCase):
    def setUp(self):
        self.leads, _ = save_creation(prepare_creation(creation_payload()))
        self.lead = self.leads[0]
        self.notifier = Mock()
        self.notifier.is_configured.return_value = True
        self.notifier.send_admin_message.return_value = TelegramDeliveryReport('sent', ({'message_id': 100},))
        self.notifier.send_admin_document.return_value = TelegramDeliveryReport('sent', ({'message_id': 101},))

    def deliver(self, *, scope='creation_lead', documents=()):
        return _deliver_notification(self.lead, self.notifier, 'Заявка', scope=scope, attachment_leads=documents)

    def latest(self):
        self.lead.refresh_from_db()
        return self.lead.telegram_delivery_json['latest']

    def test_claim_does_not_increment_counter_and_is_scoped(self):
        self.assertTrue(_claim_notification_slot(self.lead, scope='safe_exit'))
        self.assertFalse(_claim_notification_slot(self.lead, scope='safe_exit'))
        self.assertTrue(_claim_notification_slot(self.lead, scope='creation_cart'))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.notification_count, 0)
        self.assertIsNone(self.lead.last_notification_at)

    def test_confirmed_delivery_records_api_ids_and_all_parts(self):
        self.assertTrue(self.deliver())
        result = self.latest()
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(result['message_ids'], [100])
        self.assertEqual(result['summary_parts_sent'], 1)
        self.assertEqual(self.lead.notification_count, 1)
        self.assertFalse(self.deliver())
        self.assertEqual(self.notifier.send_admin_message.call_count, 1)

    def test_explicit_failure_can_retry_immediately_and_skips_files(self):
        CustomPrintLeadAttachment.objects.create(lead=self.lead, file='missing.pdf')
        self.notifier.send_admin_message.return_value = TelegramDeliveryReport('failed')
        self.assertFalse(self.deliver(documents=self.leads))
        self.assertEqual(self.latest()['status'], 'failed')
        self.assertEqual(self.latest()['documents_expected'], 1)
        self.assertEqual(self.lead.notification_count, 0)
        self.notifier.send_admin_document.assert_not_called()
        self.notifier.send_admin_message.return_value = TelegramDeliveryReport('sent')
        self.assertTrue(self.deliver())

    def test_missing_configuration_does_not_burn_retry_slot(self):
        self.notifier.is_configured.return_value = False
        self.assertFalse(self.deliver())
        self.assertEqual(self.latest()['error'], 'not_configured')
        self.notifier.is_configured.return_value = True
        self.assertTrue(self.deliver())

    def test_ambiguous_summary_does_not_automatically_resend(self):
        self.notifier.send_admin_message.return_value = TelegramDeliveryReport('ambiguous', ({'message_id': 1},))
        self.assertFalse(self.deliver())
        self.assertEqual(self.latest()['status'], 'ambiguous')
        self.assertFalse(self.deliver())
        self.assertEqual(self.notifier.send_admin_message.call_count, 1)
        self.assertFalse(self.notifier.send_admin_message.call_args.kwargs['retry_ambiguous'])

    def test_missing_document_prevents_complete_success(self):
        CustomPrintLeadAttachment.objects.create(lead=self.lead, file='custom_print/not-on-disk.pdf')
        self.assertFalse(self.deliver(documents=[self.lead]))
        result = self.latest()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual((result['documents_expected'], result['documents_sent'], result['documents_missing']), (1, 0, 1))
        self.assertEqual(self.lead.notification_count, 0)

    def test_partial_or_ambiguous_document_delivery_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            Path(directory, 'test.pdf').write_bytes(b'%PDF-test')
            CustomPrintLeadAttachment.objects.create(lead=self.lead, file='test.pdf', placement_zone='front')
            self.notifier.send_admin_document.return_value = TelegramDeliveryReport('ambiguous', ({'message_id': 2},))
            self.assertFalse(self.deliver(documents=[self.lead]))
            result = self.latest()
            self.assertEqual(result['status'], 'ambiguous')
            self.assertEqual((result['documents_expected'], result['documents_sent'], result['documents_failed']), (1, 0, 1))
            self.assertIn(2, result['message_ids'])
            self.assertEqual(result['file_warning_outcome'], 'sent')

    def test_full_box_location_and_card_text_survive_html_splitting(self):
        raw = creation_payload()
        raw['items'] = [raw['items'][0]]
        raw['items'][0]['snapshot']['order'].update(quantity=1, size_breakdown={'M': 1})
        raw['gift']['box'] = {'enabled': True, 'content_type': 'text', 'text': '<&🎁>' * 200}
        raw['gift']['certificate'] = {'enabled': True, 'message_mode': 'write', 'message': 'Привітання<&>' * 18}
        raw['gift']['wrapping'] = {'enabled': True, 'paper': 'red', 'style': 'hearts', 'preference': 'Не новорічне'}
        creation = prepare_creation(raw)
        snap = creation.items[0]['snapshot']
        snap['print']['zones'] = ['custom']
        snap['print']['zone_options'] = {'custom': {'location': 'На капюшоні', 'size_preset': 'A6'}}
        message = _build_creation_message([], creation=creation)
        parts = _telegram_message_parts(message, limit=350)
        joined = ''.join(visible(part) for part in parts)
        self.assertIn(creation.gift['box']['text'], joined)
        self.assertIn(creation.gift['certificate']['message'], joined)
        self.assertIn('На капюшоні', joined)
        for part in parts:
            self.assertLessEqual(len(visible(part).encode('utf-16-le')) // 2, 350)

    def test_ten_long_items_and_full_gift_notes_are_preserved(self):
        raw = creation_payload()
        raw['items'] = [{'id': f'item-{i}', 'snapshot': raw['items'][0]['snapshot']} for i in range(10)]
        raw['gift']['box'] = {'enabled': True, 'content_type': 'text', 'text': '&' * 1000}
        raw['gift']['certificate'] = {'enabled': True, 'message_mode': 'write', 'message': '🎁' * 240}
        raw['gift']['wrapping'] = {'enabled': True, 'preference': '&' * 240}
        creation = prepare_creation(raw)
        message = _build_creation_message([], creation=creation)
        chunks = _telegram_message_parts(message)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(visible(part) for part in chunks), visible(message))
        for part in chunks:
            self.assertLessEqual(len(visible(part).encode('utf-16-le')) // 2, 3800)

    def test_attachment_caption_keeps_original_name_and_reference_role(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            Path(directory, 'uuid.pdf').write_bytes(b'%PDF-test')
            self.lead.config_draft_json['artwork']['files'] = [{'file_index': 0, 'name': 'Мій референс<&>.pdf'}]
            CustomPrintLeadAttachment.objects.create(lead=self.lead, file='uuid.pdf', attachment_role='reference', sort_order=0)
            caption = _collect_attachment_payloads(self.lead)[0]['caption']
            self.assertIn(escape('Мій референс<&>.pdf'), caption)
            self.assertIn('Референс — зразок для дизайнера', caption)
            self.assertLessEqual(len(visible(caption).encode('utf-16-le')) // 2, 1024)

    def test_review_retries_failed_awaiting_group_and_then_reuses_confirmation(self):
        session = self.client.session
        session[SESSION_CUSTOM_CART_KEY] = {f'custom:{lead.pk}': {'lead_id': lead.pk} for lead in self.leads}
        session.save()
        self.notifier.send_admin_message.return_value = TelegramDeliveryReport('failed')
        with patch('storefront.custom_print_notifications._build_notifier', return_value=self.notifier):
            first = self.client.post(reverse('custom_print_submit_review')).json()
            self.assertEqual(first['notification_status'], 'pending')
            self.assertNotIn('надіслано', first['message'])
            self.notifier.send_admin_message.return_value = TelegramDeliveryReport('sent')
            second = self.client.post(reverse('custom_print_submit_review')).json()
            self.assertEqual(second['notification_status'], 'sent')
            calls = self.notifier.send_admin_message.call_count
            self.client.post(reverse('custom_print_submit_review'))
            self.assertEqual(self.notifier.send_admin_message.call_count, calls)

    def test_group_resend_command_keeps_all_siblings_and_common_extras(self):
        from django.core.management import call_command
        from io import StringIO
        with patch('storefront.custom_print_notifications.notify_custom_print_creation', return_value=True) as notify:
            call_command('resend_custom_print_notification', lead_id=self.leads[-1].pk, kind='new_lead', stdout=StringIO())
        self.assertEqual([lead.pk for lead in notify.call_args.args[0]], [lead.pk for lead in self.leads])

    def test_new_review_after_rejection_does_not_reuse_old_receipt(self):
        session = self.client.session
        session[SESSION_CUSTOM_CART_KEY] = {f'custom:{lead.pk}': {'lead_id': lead.pk} for lead in self.leads}
        session.save()
        with patch('storefront.custom_print_notifications._build_notifier', return_value=self.notifier):
            self.client.post(reverse('custom_print_submit_review'))
            count = self.notifier.send_admin_message.call_count
            self.lead.moderation_status = 'rejected'
            self.lead.save(update_fields=['moderation_status'])
            response = self.client.post(reverse('custom_print_submit_review')).json()
            self.assertEqual(response['notification_status'], 'sent')
            self.assertEqual(self.notifier.send_admin_message.call_count, count + 1)
            self.assertEqual(len(self.latest()['history']), 1)
            self.client.post(reverse('custom_print_submit_review'))
            self.assertEqual(self.notifier.send_admin_message.call_count, count + 1)

    def test_old_pending_attempt_is_uncertain_and_requires_operator_recovery(self):
        self.lead.telegram_delivery_json = {'scopes': {'creation_lead': {'status': 'pending', 'started_at': (timezone.now()-timedelta(minutes=5)).isoformat()}}}
        self.lead.save(update_fields=['telegram_delivery_json'])
        self.assertFalse(self.deliver())
        self.assertEqual(self.latest()['status'], 'ambiguous')
        self.notifier.send_admin_message.assert_not_called()

    def test_historical_huge_message_has_bounded_automatic_parts(self):
        self.assertTrue(_deliver_notification(self.lead, self.notifier, '<blockquote>'+'A'*200000+'</blockquote>', scope='creation_lead'))
        result = self.latest()
        self.assertEqual(self.notifier.send_admin_message.call_count, 20)
        self.assertTrue(result['summary_uses_panel_continuation'])
        self.assertIn('повний текст', self.notifier.send_admin_message.call_args.args[0])

    def test_oversized_new_brief_is_rejected_instead_of_flooding_telegram(self):
        from storefront.custom_print_creation import CreationValidationError
        raw = creation_payload()
        raw['items'][0]['snapshot']['notes']['brief'] = 'A'*2001
        with self.assertRaises(CreationValidationError) as caught:
            prepare_creation(raw)
        self.assertIn('brief', caught.exception.item_errors[raw['items'][0]['id']])

    def test_full_sleeve_text_survives_in_manager_message(self):
        snapshot = self.lead.config_draft_json
        text = 'Свій особливий текст <&> '*5
        snapshot['print']['zones'] = ['sleeve']
        snapshot['print']['zone_options'] = {'sleeve': {'left_enabled': True, 'left_mode': 'full_text', 'left_text': text, 'right_enabled': False}}
        self.assertIn(escape(text.strip()), _build_creation_message([self.lead]))

    def test_manager_approved_price_overrides_old_browser_snapshot_in_message(self):
        self.lead.approved_price = Decimal('9999')
        message = _build_creation_message(self.leads)
        self.assertIn('Погоджена сума позиції: 9999 грн', message)
        self.assertIn('Разом: 11499 грн', message)


class CustomPrintTransportReportsTests(TestCase):
    def test_partial_document_targets_return_ambiguous_report(self):
        notifier = TelegramNotifier(bot_token='test-token', admin_id='1,2', async_enabled=False)
        with patch.object(TelegramNotifier, '_post_send_document', side_effect=[('sent', {'ok': True, 'result': {'message_id': 7}}), ('failed', None)]):
            report = notifier.send_admin_document('test.pdf', 'Тест', return_report=True, retry_ambiguous=False)
        self.assertEqual(report.outcome, 'ambiguous')
        self.assertEqual(report.results, ({'message_id': 7},))

    def test_document_timeout_is_ambiguous_and_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'test.pdf')
            path.write_bytes(b'%PDF-test')
            notifier = TelegramNotifier(bot_token='test-token', admin_id='1', async_enabled=False)
            with patch('orders.telegram_notifications.requests.post', side_effect=requests.Timeout) as send:
                report = notifier.send_admin_document(str(path), 'Тест', return_report=True, retry_ambiguous=False)
            self.assertEqual(report.outcome, 'ambiguous')
            self.assertEqual(send.call_count, 1)

    def test_document_response_body_failure_is_also_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'test.pdf')
            path.write_bytes(b'%PDF-test')
            notifier = TelegramNotifier(bot_token='test-token', admin_id='1', async_enabled=False)
            with patch('orders.telegram_notifications.requests.post', side_effect=requests.exceptions.ChunkedEncodingError) as send:
                report = notifier.send_admin_document(str(path), 'Тест', return_report=True, retry_ambiguous=False)
            self.assertEqual(report.outcome, 'ambiguous')
            self.assertEqual(send.call_count, 1)
