"""Focused B02.8 contracts for the reviewed provider fact publication."""
from django.test import SimpleTestCase
from unittest.mock import patch

from management.services.approved_public_facts import (
    APPROVED_PUBLIC_FACTS_VERSION,
    SUPPORTED_PUBLIC_FACT_LANGUAGES,
    approved_public_fact_manifest,
    approved_public_fragment,
    approved_public_facts,
)
from management.services import bot_knowledge
from management.services.bot_knowledge import read_knowledge_manifest
from storefront.services.product_copy_v2 import CATEGORY_COMMON


class ApprovedPublicFactsTests(SimpleTestCase):
    def test_every_public_language_has_the_same_scoped_fact_keys(self):
        self.assertEqual(SUPPORTED_PUBLIC_FACT_LANGUAGES, ("uk", "ru", "en"))
        expected = {fact.key for fact in approved_public_facts("uk")}
        for language in SUPPORTED_PUBLIC_FACT_LANGUAGES:
            with self.subTest(language=language):
                manifest = approved_public_fact_manifest(language)
                self.assertEqual(manifest["version"], APPROVED_PUBLIC_FACTS_VERSION)
                self.assertEqual({fact.key for fact in manifest["facts"]}, expected)
                self.assertEqual(len(manifest["content_hash"]), 64)

    def test_provider_manifest_uses_approved_facts_not_legacy_markdown(self):
        manifest = read_knowledge_manifest()
        self.assertTrue(manifest.modules)
        self.assertTrue(all(module.id.startswith("approved_public:uk:") for module in manifest.modules))
        self.assertIn("190 г/м²", manifest.text)
        self.assertIn("1–3 днів", manifest.text)
        self.assertNotIn("важкого поранення", manifest.text)
        self.assertNotIn("Український ветеранський фонд", manifest.text)
        self.assertNotIn("knowledge:brand.md", manifest.text)

    def test_brand_meanings_and_shaka_are_public_without_founder_medical_detail(self):
        for language, required in (
            ("uk", ("важкий стан", "розділовий знак", "shaka")),
            ("ru", ("тяжёлое состояние", "запятая", "shaka")),
            ("en", ("serious condition", "comma", "shaka")),
        ):
            with self.subTest(language=language):
                brand = next(fact for fact in approved_public_facts(language) if fact.key == "brand")
                for value in required:
                    self.assertIn(value, brand.public_text)
                self.assertNotIn("поран", brand.public_text.casefold())

    def test_user_fragment_excludes_provider_directives_and_keeps_after_receipt_boundary(self):
        fragment = approved_public_fragment("uk", keys=("current_tshirt_bases", "service_boundary"))
        self.assertIn("190 г/м²", fragment)
        self.assertIn("після отримання", fragment)
        self.assertNotIn("Називай ці дані", fragment)
        self.assertNotIn("Не називай", fragment)

    def test_dispatch_range_comes_from_the_canonical_constant(self):
        with patch("management.services.approved_public_facts.ORDINARY_DISPATCH_WINDOW_DAYS", (2, 4)):
            for language in SUPPORTED_PUBLIC_FACT_LANGUAGES:
                with self.subTest(language=language):
                    dispatch = next(fact for fact in approved_public_facts(language) if fact.key == "dispatch")
                    self.assertIn("2–4", dispatch.public_text)
                    self.assertNotIn("1–3", dispatch.public_text)

    def test_knowledge_cache_key_contains_language_and_actual_fact_hash(self):
        with patch("management.services.approved_public_facts.ORDINARY_DISPATCH_WINDOW_DAYS", (2, 4)), patch.object(
            bot_knowledge.cache, "get", return_value=None
        ), patch.object(bot_knowledge.cache, "set") as cache_set:
            expected_hash = read_knowledge_manifest("en").content_hash
            text = bot_knowledge.get_brand_knowledge("en")

        self.assertIn("2–4", text)
        self.assertEqual(
            cache_set.call_args.args[0],
            f"{bot_knowledge.CACHE_KEY_PREFIX}:en:{expected_hash}",
        )

    def test_owner_current_bases_are_reviewed_without_unconfirmed_category_fallback(self):
        self.assertEqual(CATEGORY_COMMON["hoodie"]["para_material"], "")
        self.assertEqual(CATEGORY_COMMON["long-sleeve"]["para_material"], "")
        self.assertEqual(CATEGORY_COMMON["tshirts"]["para_material"], "")
        approved = "\n".join(f.body for f in approved_public_facts("uk"))
        self.assertIn("regular — близько 190 г/м²", approved)
        self.assertIn("oversize — близько 220 г/м²", approved)
        self.assertIn("близько 320 г/м²", approved)
        self.assertNotIn("210–220", approved)
        from management.services.approved_public_facts import approved_base_assertion
        self.assertIsNone(approved_base_assertion(
            {"product": {"id": 3, "kind": "longsleeves"}}, question="Який склад?"))
        self.assertIsNotNone(approved_base_assertion(
            {"product": {"id": 3, "kind": "hoodies"}}, question="Який склад?"))
