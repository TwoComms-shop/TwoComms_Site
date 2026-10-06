import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import translation

from storefront.custom_print_notifications import _build_message
from storefront.models import CustomPrintLead


@override_settings(COMPRESS_ENABLED=False, COMPRESS_OFFLINE=False)
class CustomPrintGiftFlowTests(TestCase):
    def setUp(self):
        translation.activate('uk')
        self.addCleanup(translation.deactivate)

    def snapshot(self, enabled=True):
        return {
            'mode': 'personal', 'order_purpose': 'gift',
            'product': {'type': 'hoodie', 'fit': 'regular', 'fabric': 'standard', 'color': 'black'},
            'print': {'zones': ['front'], 'zone_options': {'front': {'size_preset': 'A4'}}},
            'artwork': {'service_kind': 'design'},
            'order': {'quantity': 1, 'size_mode': 'single', 'sizes_note': 'M',
                      'gift': {'enabled': enabled, 'text': 'Зі святом!'}},
            'contact': {'name': 'Gift QA', 'channel': 'phone', 'value': '+380671234567'},
            'notes': {'brief': 'Лаконічна ілюстрація для подарунка'},
            'ui': {'current_step': 'contact'},
        }

    def test_page_exposes_ordered_choices_and_cached_animation_assets(self):
        response = self.client.get(reverse('custom_print'), secure=True, HTTP_HOST='twocomms.shop')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertLess(html.index('cp-purpose-card--personal'), html.index('cp-purpose-card--gift'))
        self.assertLess(html.index('cp-purpose-card--gift'), html.index('cp-purpose-card--brand'))
        self.assertIn('custom-print-purpose.js?v=20261007-custom-creation-v3', html)
        self.assertIn('custom-print-purpose.css?v=20261007-custom-creation-v3', html)
        self.assertNotIn('data-gift-reveal', html)
        self.assertIn('cp-gift-box-heart', html)
        mode = html.split('id="cp-step-mode"', 1)[1].split('</section>', 1)[0]
        self.assertNotIn('cp-purpose-pack', mode)
        self.assertNotIn('cp-purpose-caption', mode)
        self.assertNotIn('+100', mode)
        self.assertIn('Бригади, підрозділи', html)

    @patch('storefront.views.static_pages.notify_new_custom_print_lead')
    def test_submitted_gift_reaches_manager_with_packaging_enabled(self, notify):
        snapshot = self.snapshot()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('custom_print_lead'), {
                'service_kind': 'design', 'product_type': 'hoodie', 'placements': ['front'],
                'quantity': 1, 'size_mode': 'single', 'sizes_note': 'M', 'client_kind': 'personal',
                'name': 'Gift QA', 'contact_channel': 'phone', 'contact_value': '+380671234567',
                'brief': 'Лаконічна ілюстрація для подарунка',
                'config_draft_json': json.dumps(snapshot),
            }, secure=True, HTTP_HOST='twocomms.shop', HTTP_X_REQUESTED_WITH='fetch')
        self.assertEqual(response.status_code, 200, response.content[:1200])
        lead = CustomPrintLead.objects.get()
        self.assertEqual(lead.client_kind, 'personal')
        self.assertEqual(lead.config_draft_json['order_purpose'], 'gift')
        self.assertTrue(lead.config_draft_json['order']['gift'])
        notify.assert_called_once_with(lead)
        message = _build_message(lead)
        self.assertIn('На подарунок', message)
        self.assertIn('<b>Подарункова упаковка:</b> так', message)
        self.assertIn('Зі святом!', message)

    @patch('storefront.views.static_pages.notify_custom_print_safe_exit')
    def test_manager_handoff_keeps_gift_intent_with_packaging_disabled(self, notify):
        response = self.client.post(reverse('custom_print_safe_exit'),
            data=json.dumps(self.snapshot(enabled=False)), content_type='application/json',
            secure=True, HTTP_HOST='twocomms.shop')
        self.assertEqual(response.status_code, 200, response.content[:1200])
        lead = CustomPrintLead.objects.get()
        self.assertEqual(lead.config_draft_json['order_purpose'], 'gift')
        self.assertFalse(lead.config_draft_json['order']['gift'])
        notify.assert_called_once_with(lead=lead, snapshot=lead.config_draft_json)
        message = _build_message(lead)
        self.assertIn('На подарунок', message)
        self.assertIn('<b>Подарункова упаковка:</b> ні', message)

    def test_gift_cart_session_is_serializable_and_preserves_purpose(self):
        from storefront.views.static_pages import _build_custom_cart_session_item

        snapshot = self.snapshot()
        snapshot['print']['add_ons'] = ['lacing']
        lead = CustomPrintLead.objects.create(
            service_kind='design', product_type='hoodie', client_kind='personal',
            name='Gift QA', contact_channel='phone', contact_value='+380671234567',
            config_draft_json=snapshot, quantity=1,
        )
        item = _build_custom_cart_session_item(lead)
        restored = json.loads(json.dumps(item))
        self.assertEqual(restored['order_purpose'], 'gift')
        self.assertTrue(restored['gift_enabled'])
        self.assertEqual(restored['gift_text'], 'Зі святом!')
        self.assertEqual(restored['zone_labels'], ['Спереду'])
