"""Check customer copy and its structured projection without database access."""
import html
import json
import re
from pathlib import Path
from unittest.mock import patch

from django.http import HttpResponse
from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase
from django.utils.translation import gettext, override

from storefront.templatetags.seo_tags import faq_schema
from storefront.views import static_pages


class CustomPrintPublicCopyTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = Path(__file__).resolve().parents[2] / 'twocomms_django_theme/templates/pages/custom_print.html'
        source = path.read_text()
        cls.support = source.split('<div class="cp-support-stack">', 1)[1].split('{{ custom_print_config|json_script:', 1)[0]
        cls.strings = re.findall(r'{% trans (["\'])(.*?)\1 %}', cls.support)

    def _view_context(self):
        with patch.object(static_pages, 'render', return_value=HttpResponse()) as render:
            response = static_pages.custom_print(RequestFactory().get('/custom-print/'))
        self.assertEqual(response['Cache-Control'], 'no-store, no-cache, must-revalidate, max-age=0')
        return render.call_args.args[2]

    def test_estimate_distinguishes_garment_own_clothing_and_manager_approval(self):
        context = self._view_context()
        cfg = context['custom_print_config']
        self.assertGreater(cfg['products']['tshirt']['pricing']['base'], 350)
        self.assertGreater(cfg['products']['hoodie']['pricing']['base'], 500)
        self.assertTrue(cfg['products']['customer_garment']['pricing']['estimate_from_base'])
        with override('en'):
            rendered = html.unescape(Template('{% load i18n %}' + self.support).render(Context(context)))
        for phrase in ('preliminary', 'final price', 'your own garment', 'Transit time'):
            self.assertIn(phrase, rendered)
        for phrase in ('350', '500', 'same-day', '30+', '160–200', '320', '1200', 'photoshoot'):
            self.assertNotIn(phrase, rendered)

    def test_visible_faq_and_jsonld_publish_same_answers_in_each_language(self):
        context = self._view_context()
        for language in ('uk', 'ru', 'en'):
            with self.subTest(language=language), override(language):
                rendered = html.unescape(Template('{% load i18n %}' + self.support).render(Context(context)))
                schema = json.loads(faq_schema(context['faq_items'])['faq_schema'])
                self.assertEqual(len(schema['mainEntity']), len(context['faq_items']))
                for item in schema['mainEntity']:
                    self.assertIn(item['name'], rendered)
                    self.assertIn(item['acceptedAnswer']['text'], rendered)
                for phrase in ('350', '500', '30+', '160–200', '320', '1200'):
                    self.assertNotIn(phrase, json.dumps(schema, ensure_ascii=False))

    def test_support_faq_and_heading_have_explicit_russian_english_translations(self):
        messages = {text for quote, text in self.strings}
        for item in static_pages.CUSTOM_PRINT_FAQ_ITEMS:
            with override('uk'):
                messages.update((str(item['question']), str(item['answer'])))
        messages.add('Кастомний друк на футболках, худі та лонгслівах')
        for language in ('ru', 'en'):
            with self.subTest(language=language), override(language):
                for message in messages:
                    self.assertNotEqual(gettext(message), message, f'{language}: untranslated {message}')

    def test_use_cases_and_customer_actions_remain_available(self):
        context = self._view_context()
        with override('en'):
            rendered = Template('{% load i18n %}' + self.support).render(Context(context))
        for phrase in ('gift', 'teams', 'organisations', 'brigades', 'brands', 'dropshippers', 'veteran'):
            self.assertIn(phrase, rendered.lower())
        from django.urls import reverse
        with override('en'):
            for route in ('delivery', 'wholesale_page', 'contacts'):
                self.assertIn(reverse(route), rendered)
        self.assertTrue(context['custom_print_config']['add_to_cart_url'].endswith('/custom-print/add-to-cart/'))
        self.assertTrue(context['custom_print_config']['submit_url'].endswith('/custom-print/lead/'))
