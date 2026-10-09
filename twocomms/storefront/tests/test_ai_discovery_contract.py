"""Discovery contracts exercised without database access or provider calls."""

import json
from pathlib import Path
import re
from unittest.mock import MagicMock, patch

from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils.translation import get_language, override

from storefront.services.fact_registry import BRAND_IDENTITY_SOURCE, PUBLIC_FACTS
from storefront.views import static_pages


class AIDiscoveryContractTests(SimpleTestCase):
    def _orientation_response(self, view, language):
        with override(language), patch.multiple(
            static_pages,
            Category=MagicMock(),
            BlogPost=MagicMock(),
            public_products_queryset=MagicMock(),
        ):
            response = view(RequestFactory().get("/llms.txt"))
            self.assertEqual(get_language(), language)
            return response

    def test_discovery_files_keep_primary_urls_and_verified_identity_in_every_language(self):
        for language in ("uk", "ru", "en"):
            for view in (static_pages.llms_txt, static_pages.llms_full_txt):
                with self.subTest(language=language, view=view.__name__):
                    response = self._orientation_response(view, language)
                    body = response.content.decode()
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
                    self.assertEqual(response["Cache-Control"], "public, max-age=3600")
                    self.assertIn("https://twocomms.shop/pro-brand/", body)
                    self.assertIn("https://twocomms.shop/custom-print/", body)
                    self.assertNotIn("https://twocomms.shop/en/", body)
                    self.assertNotIn("https://twocomms.shop/ru/", body)
                    self.assertIn(PUBLIC_FACTS["founder_name"].value, body)
                    self.assertIn(PUBLIC_FACTS["origin_city"].value, body)
                    self.assertIn(BRAND_IDENTITY_SOURCE, body)

    @override_settings(FREE_SHIPPING_THRESHOLD="2750")
    def test_compact_context_links_full_and_qualifies_review_and_shipping_facts(self):
        body = self._orientation_response(static_pages.llms_txt, "en").content.decode()
        self.assertIn("https://twocomms.shop/llms-full.txt", body)
        self.assertIn("orders of 2750 UAH and above", body)
        self.assertIn("without a verified purchase", body)
        self.assertIn("comments are not counted as ratings", body)
        self.assertNotIn("star ratings come from real verified customers", body)

    def test_claude_discovery_and_user_agents_can_fetch_public_brand_and_service(self):
        body = static_pages.robots_txt(RequestFactory().get("/robots.txt")).content.decode()
        for agent in ("Claude-SearchBot", "Claude-User", "ClaudeBot"):
            group = body.split(f"User-agent: {agent}\n", 1)[1].split("\n\n", 1)[0]
            self.assertIn("Allow: /\n", group)
            self.assertIn("Disallow: /admin/", group)
            self.assertNotIn("Disallow: /pro-brand/", group)
            self.assertNotIn("Disallow: /custom-print/", group)

    def test_custom_print_service_references_global_provider_and_current_language_url(self):
        template_path = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme/templates/pages/custom_print.html"
        )
        source = template_path.read_text()
        service_script = re.search(
            r'<script type="application/ld\+json">(.*?)</script>', source, re.S
        ).group(1)
        template = Template("{% load i18n %}" + service_script)
        for language, prefix in (("uk", ""), ("ru", "/ru"), ("en", "/en")):
            with self.subTest(language=language), override(language):
                schema = json.loads(template.render(Context({"site_base_url": "https://twocomms.shop"})))
                service_url = f"https://twocomms.shop{prefix}/custom-print/"
                self.assertEqual(schema["url"], service_url)
                self.assertEqual(schema["@id"], service_url + "#service")
                self.assertEqual(schema["provider"]["@id"], "https://twocomms.shop/#organization")
                self.assertEqual(schema["areaServed"]["identifier"], "UA")

    def test_identity_registry_does_not_invent_foundation_or_postal_data(self):
        for key in ("founder_name", "origin_city", "founder_story_url"):
            fact = PUBLIC_FACTS[key]
            self.assertEqual(fact.owner, "public_brand_identity")
            self.assertIn(BRAND_IDENTITY_SOURCE, fact.source)
            self.assertTrue(fact.effective_date)
        self.assertNotIn("founding_date", PUBLIC_FACTS)
        self.assertNotIn("postal_address", PUBLIC_FACTS)

    def test_custom_print_has_one_shared_heading_outside_the_mobile_wrapper(self):
        template_path = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme/templates/pages/custom_print.html"
        )
        source = template_path.read_text()
        # Render both hero headers, including the real mobile partial, without
        # executing the commerce shell or querying the product database.
        hero_source = source.split("{% block content %}", 1)[1].split(
            '<div class="cp-hero-theatre', 1
        )[0]
        html = Template("{% load static i18n %}" + hero_source).render(Context({}))
        self.assertEqual(len(re.findall(r"<h1\b", html)), 1)
        self.assertIn('<h1 class="visually-hidden">', html)
        self.assertLess(html.index("<h1"), html.index('<header class="cp-mobile-hero"'))
        self.assertIn('<h2 class="cp-mobile-hero__title">', html)
        self.assertIn('<h2 class="cp-hero-title ', html)
