from django.template import Context, Template
from django.test import SimpleTestCase
from django.utils.safestring import mark_safe

from storefront.services.product_rich_text import render_product_rich_text


class ProductRichTextTests(SimpleTestCase):
    def test_plain_text_preserves_newlines_and_escapes_characters(self):
        self.assertEqual(render_product_rich_text('Рядок & текст\r\n5 < 10\n"Цитата"'),
                         'Рядок &amp; текст<br>5 &lt; 10<br>&quot;Цитата&quot;')
        self.assertEqual(render_product_rich_text(None), '')

    def test_saved_editor_html_preserves_paragraphs_emphasis_and_lists(self):
        html = '<p><strong>Харків Вокзальна</strong> — історія міста.</p><ul><li>Бавовна</li><li><em>Класична посадка</em></li></ul>'
        self.assertEqual(render_product_rich_text(html), html)

    def test_legacy_escaped_html_is_rendered_with_bounded_decoding(self):
        for html in ('&lt;p&gt;&lt;strong&gt;Текст&lt;/strong&gt;&lt;/p&gt;',
                     '&amp;lt;p&amp;gt;&amp;lt;strong&amp;gt;Текст&amp;lt;/strong&amp;gt;&amp;lt;/p&amp;gt;'):
            with self.subTest(html=html):
                self.assertEqual(render_product_rich_text(html), '<p><strong>Текст</strong></p>')

    def test_entities_inside_existing_html_remain_literal_text(self):
        self.assertEqual(render_product_rich_text('<p>Приклад: &lt;script&gt; та &amp;</p>'),
                         '<p>Приклад: &lt;script&gt; та &amp;</p>')

    def test_script_style_and_embedded_payloads_are_removed(self):
        html = '<p>До</p><script>alert(1)</script><style>body{display:none}</style><iframe src="evil">Hidden</iframe><svg><script>alert(2)</script></svg><p>Після</p>'
        self.assertEqual(render_product_rich_text(html), '<p>До</p><p>Після</p>')
        self.assertEqual(render_product_rich_text('&lt;script&gt;alert(1)&lt;/script&gt;&lt;p&gt;Опис&lt;/p&gt;'), '<p>Опис</p>')

    def test_void_embed_does_not_remove_following_product_copy(self):
        self.assertEqual(render_product_rich_text('<embed src="evil"><p>Опис</p>'), '<p>Опис</p>')

    def test_unclosed_script_is_removed_without_discarding_preceding_copy(self):
        self.assertEqual(render_product_rich_text('<p>Опис</p><script>alert(1)'), '<p>Опис</p>')

    def test_event_style_class_target_and_media_attributes_are_not_allowed(self):
        html = '<p class="evil" style="position:fixed" onclick="alert(1)">Опис<img src=x onerror=alert(2)></p><a href="https://example.com" target="_blank" rel="opener" title="Далі">Посилання</a>'
        self.assertEqual(render_product_rich_text(html), '<p>Опис</p><a href="https://example.com" title="Далі">Посилання</a>')

    def test_unsafe_href_protocols_and_obfuscated_javascript_are_removed(self):
        for href in ('javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'java&#x09;script:alert(1)',
                     '&#106;avascript:alert(1)', 'data:text/html,evil', 'vbscript:evil', 'file:///etc/passwd'):
            with self.subTest(href=href):
                self.assertEqual(render_product_rich_text(f'<a href="{href}">Текст</a>'), '<a>Текст</a>')

    def test_allowed_https_relative_mail_and_phone_links_are_preserved(self):
        for href in ('https://example.com/path', '/catalog/', 'mailto:hello@example.com', 'tel:+380501234567'):
            with self.subTest(href=href):
                self.assertEqual(render_product_rich_text(f'<a href="{href}">Текст</a>'), f'<a href="{href}">Текст</a>')

    def test_safe_string_input_does_not_bypass_sanitization(self):
        self.assertEqual(render_product_rich_text(mark_safe('<p onmouseover="evil()">Опис</p>')), '<p>Опис</p>')

    def test_malformed_html_is_repaired_and_never_emits_active_markup(self):
        result = render_product_rich_text('<p><b>Опис</p><img src=x onerror=evil()><!-- comment -->')
        self.assertNotIn('<img', result)
        self.assertNotIn('onerror', result)
        self.assertNotIn('<!--', result)
        self.assertIn('Опис', result)

    def test_template_filter_renders_both_ordinary_and_html_copy(self):
        template = Template('{% load product_rich_text %}{{ copy|product_rich_text }}')
        self.assertEqual(template.render(Context({'copy': '<p><strong>Опис</strong></p>'})), '<p><strong>Опис</strong></p>')
        self.assertEqual(template.render(Context({'copy': 'Один\nДва'})), 'Один<br>Два')
