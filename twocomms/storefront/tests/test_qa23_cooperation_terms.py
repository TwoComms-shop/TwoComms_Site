"""Public partner offers stay localized, source-consistent and manager-agreed."""
import json
import re
from html import unescape
from html.parser import HTMLParser
from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import translation


def normalized(value):
    return " ".join(unescape(value).split())


class PartnerPage(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.faqs, self.schemas, self.anchors, self.visible = [], [], [], []
        self.faq = self.field = self.anchor = self.script = None
        self.ignored = None
        self.blocks, self.block = {}, None
        self.div_depth = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.div_depth += 1
            key = attrs.get("data-partner-terms")
            if "data-partner-test-notice" in attrs:
                key = "test-notice"
            if key:
                self.block = (key, self.div_depth)
                self.blocks[key] = []
        if tag in {"script", "style"}:
            self.ignored = tag
            if tag == "script" and attrs.get("type") == "application/ld+json":
                self.script = []
        if tag == "details" and "faq-item" in attrs.get("class", ""):
            self.faq = {"question": [], "answer": []}
        if self.faq is not None and tag in {"summary", "p"}:
            self.field = "question" if tag == "summary" else "answer"
        if tag == "a":
            self.anchor = {**attrs, "text": []}

    def handle_data(self, text):
        if self.ignored:
            if self.script is not None:
                self.script.append(text)
            return
        self.visible.append(text)
        if self.faq is not None and self.field:
            self.faq[self.field].append(text)
        if self.anchor is not None:
            self.anchor["text"].append(text)
        if self.block:
            self.blocks[self.block[0]].append(text)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.schemas.append(json.loads("".join(self.script)))
            self.script = None
        if tag == self.ignored:
            self.ignored = None
        if self.faq is not None and tag in {"summary", "p"}:
            self.field = None
        if tag == "details" and self.faq is not None:
            self.faqs.append(tuple(normalized("".join(self.faq[key])) for key in ("question", "answer")))
            self.faq = None
        if tag == "a" and self.anchor is not None:
            self.anchor["text"] = normalized("".join(self.anchor["text"]))
            self.anchors.append(self.anchor)
            self.anchor = None
        if tag == "div":
            if self.block and self.block[1] == self.div_depth:
                self.block = None
            self.div_depth -= 1


class PublicPartnerTermsTests(TestCase):
    markers = {
        "uk": ("повної ціни на сайті", "на місяць", "кратно 8", "не активуються автоматично"),
        "ru": ("полной цены на сайте", "в месяц", "кратно 8", "не активируются автоматически"),
        "en": ("full website price", "per month", "multiples of 8", "not activated automatically"),
    }

    def page(self, name, language):
        with translation.override(language):
            response = self.client.get(reverse(name), secure=True, HTTP_HOST="twocomms.shop")
        self.assertEqual(response.status_code, 200)
        return PartnerPage(response.content.decode())

    def test_rendered_faq_and_machine_offer_agree_in_every_supported_locale(self):
        for language, markers in self.markers.items():
            for route in ("cooperation", "wholesale_page"):
                with self.subTest(language=language, route=route):
                    page = self.page(route, language)
                    schema = next(value for value in page.schemas if value.get("@type") == "FAQPage")
                    machine_faqs = [(normalized(item["name"]), normalized(item["acceptedAnswer"]["text"]))
                                    for item in schema["mainEntity"]]
                    self.assertGreater(len(page.faqs), 0)
                    self.assertEqual(page.faqs, machine_faqs)
                    text = normalized(" ".join(page.visible))
                    machine_text = " ".join(answer for _, answer in machine_faqs)
                    for marker in markers:
                        self.assertIn(marker, text)
                        self.assertIn(marker, machine_text)
                    self.assertIn("10–15%", machine_text)
                    self.assertIn("IBAN", machine_text)
                    self.assertIn("200", machine_text)
                    self.assertNotRegex(text, r"5\s*[-–]\s*20\s*%|15\s*%.*25\s*%.*35\s*%")
                    self.assertNotRegex(machine_text, r"8-29|30-99|100\+")

    def test_main_contact_is_telegram_and_test_account_is_secondary(self):
        for language in self.markers:
            for route, primary_class in (("cooperation", "partnership-btn"), ("wholesale_page", "hero-btn-primary")):
                with self.subTest(language=language, route=route):
                    page = self.page(route, language)
                    primary = next(a for a in page.anchors if primary_class in a.get("class", "").split())
                    self.assertEqual(primary["href"], "https://t.me/twocomms")
                    cabinet = next(a for a in page.anchors if a.get("href") == "/dropshipper/")
                    self.assertIn("TEST", cabinet["text"])
                    self.assertNotIn(primary_class, cabinet.get("class", "").split())

    def test_wholesale_offer_has_no_automatic_tariff_and_keeps_existing_request_routes(self):
        for language in self.markers:
            with self.subTest(language=language):
                page = self.page("wholesale_page", language)
                wholesale = normalized(" ".join(page.blocks["wholesale"]))
                dropship = normalized(" ".join(page.blocks["dropship"]))
                self.assertIn("10–15%", dropship)
                self.assertNotRegex(wholesale, r"\d+\s*%")
                self.assertRegex(wholesale, r"кратн[а-яіїє]* 8" if language != "en" else r"multiples of 8")
                with translation.override(language):
                    expected = {reverse("wholesale_order_form"), reverse("wholesale_prices_xlsx")}
                self.assertTrue(expected.issubset({a.get("href") for a in page.anchors}))

    def test_existing_tariff_context_cannot_become_an_automatic_public_offer(self):
        request = RequestFactory().get("/wholesale/", secure=True, HTTP_HOST="twocomms.shop")
        request.user = AnonymousUser()
        legacy = {"drop": "LEGACY_DROP_PRICE", "wholesale": ["LEGACY_TIER_PRICE"] * 5}
        product = SimpleNamespace(title="Existing product", main_image=None)
        with translation.override("uk"):
            html = render_to_string("pages/wholesale.html", {
                "tshirt_prices": legacy, "hoodie_prices": legacy,
                "tshirt_products": [product], "hoodie_products": [product],
            }, request=request)
        page = PartnerPage(html)
        text = normalized(" ".join(page.visible))
        self.assertIn("Existing product", text)
        self.assertNotIn("LEGACY_DROP_PRICE", text)
        self.assertNotIn("LEGACY_TIER_PRICE", text)

    def test_existing_account_explains_test_status_without_removing_order_entry(self):
        for language in self.markers:
            with self.subTest(language=language):
                page = self.page("orders:dropshipper_dashboard", language)
                banner = normalized(" ".join(page.blocks["test-notice"]))
                self.assertIn("TEST", banner)
                self.assertIn("Telegram", banner)
                self.assertTrue(any(a.get("href") == "https://t.me/twocomms" for a in page.anchors))
                # Existing public account navigation remains present.
                with translation.override(language):
                    products = reverse("orders:dropshipper_products")
                self.assertTrue(any(a.get("href") == products for a in page.anchors))

    def test_global_faq_distinguishes_dropship_discount_from_wholesale_in_visible_and_machine_copy(self):
        for language, markers in self.markers.items():
            with self.subTest(language=language):
                page = self.page("faq", language)
                with translation.override(language):
                    question = translation.gettext("Які умови опту і дропшипінгу?")
                visible_answer = dict(page.faqs)[question]
                schema = next(value for value in page.schemas if value.get("@type") == "FAQPage")
                machine_answer = next(item["acceptedAnswer"]["text"] for item in schema["mainEntity"]
                                      if item["name"] == question)
                self.assertEqual(visible_answer, normalized(machine_answer))
                self.assertIn("10–15%", visible_answer)
                self.assertIn(markers[0], visible_answer)
                self.assertIn(markers[2], visible_answer)
                self.assertIn("https://t.me/twocomms", visible_answer)
                self.assertNotRegex(visible_answer, r"10\s*[-–]\s*20\s*%")
                wholesale_marker = {"uk": "Оптові закупівлі", "ru": "Оптовые закупки", "en": "Wholesale orders"}[language]
                dropship, wholesale = visible_answer.split(wholesale_marker, 1)
                self.assertIn("10–15%", dropship)
                self.assertIn({"uk": "не активується автоматично", "ru": "не активируется автоматически", "en": "not activated automatically"}[language], dropship)
                self.assertNotRegex(wholesale, r"\d+\s*%")

    def test_canonical_wholesale_companion_does_not_publish_a_percentage(self):
        # This companion is not the observed /wholesale/ consumer. Keep its
        # shared canonical answer aligned without claiming it is live output.
        from storefront.support_content import WHOLESALE_FAQ_ITEMS

        for language in self.markers:
            with self.subTest(language=language), translation.override(language):
                question = translation.gettext("Які знижки діють на опт?")
                answer = str(next(row["answer"] for row in WHOLESALE_FAQ_ITEMS if str(row["question"]) == question))
                self.assertNotRegex(answer, r"\d+\s*%")
                self.assertRegex(answer, r"менеджер|manager")
