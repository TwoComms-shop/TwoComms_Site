"""Native sealed-turn regression for the 48k zero-dispatch retail incident."""
from datetime import timedelta
import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from management.services.ig_catalog_prompt_compaction import compact_catalog_text, expand_compact_catalog_text, fit_compact_catalog
from management.services.ig_prompt_projections import deduplicate_source_cart_note, source_catalog_priorities
from management.services.ig_revision_conversation_context import conversation_timing_guidance as native_timing_guidance
from management import tests_ig_turn_context_integration as fixtures


INITIAL = "Привіт! Хочу замовити футболку для себе та хлопця"


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_TURN_CONTEXT_MODE="unified", IG_CLIENT_STATE_PROMPT_TOKENS=2400,
    IG_BOT_POLICY_BUDGET_CHARS=48000)
class LesiaNativePromptBudgetTests(TestCase):
    setUp = fixtures.TurnContextConsumerIntegrationTests.setUp
    publish = fixtures.TurnContextConsumerIntegrationTests.publish
    fixture = fixtures.TurnContextConsumerIntegrationTests.fixture
    transport = fixtures.TurnContextConsumerIntegrationTests.transport

    def catalog(self):
        from storefront.models import Catalog, Category, Product, ProductFitOption, ProductStatus, SizeGrid
        from product_catalog.models import ProductOptionSizeGrid
        from productcolors.models import Color, ProductColorVariant
        black = Color.objects.create(name="Чорний", primary_hex="#000000")
        white = Color.objects.create(name="Білий", primary_hex="#FFFFFF")
        for garment, price in (("tshirts", 1100), ("hoodies", 1800)):
            category = Category.objects.create(name=garment, slug=garment)
            catalog = Catalog.objects.create(name=garment, slug=garment)
            grid = SizeGrid.objects.create(catalog=catalog, name=garment,
                guide_data={"columns": [{"key": "size", "label": "Розмір"}],
                    "rows": [{"size": size, "display_size": size} for size in ("XS", "S", "M", "L", "XL", "XXL")]},
                is_active=True)
            for number in range(60):
                product = Product.objects.create(title=f"Бойова квіточка {number}",
                    slug=f"{garment}-{number}", category=category, catalog=catalog,
                    price=price, status=ProductStatus.PUBLISHED)
                ProductFitOption.objects.create(product=product, code="oversize", label="Оверсайз",
                    is_active=True, is_default=True)
                ProductOptionSizeGrid.objects.create(product=product, option_key="fit=oversize", size_grid=grid)
                for color in (black, white):
                    ProductColorVariant.objects.create(product=product, color=color,
                        metadata={"bot_vision": {"summary": f"Квітка {number}, напис «З Україною в серці»"}})
        cache.clear()

    def packet(self, *, long_history=False, source_text=INITIAL):
        from management.services.ig_revision_live import _generate_proposal
        from management.services.ig_core_policy import CANONICAL_IG_CORE_POLICY
        self.assertEqual(self.settings_row.system_prompt, CANONICAL_IG_CORE_POLICY)
        self.catalog()
        if long_history:
            from management.models import IgClient, InstagramBotMessage
            create_customer = IgClient.objects.create
            def with_history(**kwargs):
                customer = create_customer(**kwargs)
                InstagramBotMessage.objects.bulk_create([InstagramBotMessage(client=customer,
                    sender_id=customer.igsid, provider_namespace="instagram_login:owner-1",
                    role="user", source="webhook", status="done", mid=f"bounded-history-{number}",
                    text=f"Історичне повідомлення {number}. " + "Уточнюю каталог та умови. " * 25,
                    provider_created_at=self.now - timedelta(seconds=80-number)) for number in range(60)])
                return customer
            with patch.object(IgClient.objects, "create", side_effect=with_history):
                fixture = self.fixture(source_text)
        else:
            fixture = self.fixture(source_text)
        before = fixture.prepared.state.as_dict()
        self.requests = []
        from management.services.ig_policy_compiler import compile_policy
        mandatory_sizes, compiled_sizes = [], []
        def diagnosed(**kwargs):
            modules = [row for key in ('immutable_authority', 'published_core', 'verified_dynamic_facts')
                for row in kwargs.get(key, [])]
            mandatory_sizes.append(len("\n\n".join(row.body for row in modules)))
            result = compile_policy(**kwargs)
            compiled_sizes.append(len(result.text))
            return result
        with patch("management.services.ig_revision_conversation_context.conversation_timing_guidance", side_effect=native_timing_guidance), patch("management.services.ig_policy_compiler.compile_policy", side_effect=diagnosed), patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self.transport) as provider:
            result = _generate_proposal(fixture.revision, fixture.token, self.settings_row,
                self.publication_binding, fixture.collection)
        self.assertEqual(provider.call_count, 1, ("Complete native packet must pass policy admission before transport", result[2]))
        packet, manifest, _ = self.requests[0]
        instruction = packet["system_instruction"]["parts"][0]["text"]
        self.assertLessEqual(max(compiled_sizes), 48000)
        self.assertLessEqual(max(mandatory_sizes), 42000,
            f"Reserve at least 6k before optional knowledge; actual mandatory sizes {mandatory_sizes}")
        self.assertIn(source_text, str(packet))
        self.assertIn("current.source_cart", instruction)
        self.assertEqual(fixture.prepared.state.as_dict(), before)
        self.assertEqual(manifest["request_context"]["source_message_ids"], [fixture.source.pk])
        self.assertIn("core:published_prompt", manifest["mandatory_ids"])
        self.assertIn("context:turn", manifest["mandatory_ids"])
        cart_block = next(json.loads(row) for row in instruction.splitlines()
            if row.startswith('{') and json.loads(row).get("slot") == "current.source_cart")
        self.assertEqual({row["recipient_id"] for row in cart_block["value"]["lines"]}, {"self", "boyfriend"})
        self.assertIn("не означає відсутність товару чи залишку", instruction)
        return packet, manifest

    def test_native_initial_two_recipients_with_120_catalog_products_reaches_packet_admission(self):
        self.packet()

    def test_long_bounded_history_does_not_reinflate_native_mandatory_packet(self):
        packet, manifest = self.packet(long_history=True)
        self.assertGreater(len(manifest["request_context"]["history_message_ids"]), 1)
        self.assertIn("Історичне повідомлення", str(packet["contents"]))

    def test_native_packet_preserves_current_questions_negation_and_language_preference(self):
        self.packet(source_text=INITIAL
            + ". Для мене M, не білий; хлопцю L, чорний. Українською, будь ласка. Яка ціна і як оплатити?")

    def test_deduplication_retains_complete_native_cart_and_other_current_obligations(self):
        from management.services.ig_client_state_card import render_client_state_prompt
        text = INITIAL + ". Для мене M, не білий; хлопцю L, чорний. Українською, будь ласка. Яка ціна і як оплатити?"
        fixture = self.fixture(text)
        state = fixture.prepared.state
        rendered = render_client_state_prompt(state, budget=2400,
            excluded_slots=("communication.preferences", "context.useful_memory"))
        original = fixture.prepared.context.turn_note
        before = state.as_dict()
        projected = deduplicate_source_cart_note(original, state=state, state_text=rendered.text)
        self.assertLess(len(projected), len(original))
        self.assertIn("[SERVER RESPONSE PLAN]", projected)
        self.assertIn("[ALL CURRENT CUSTOMER POSITIONS", projected)
        self.assertIn("current.source_cart above", projected)
        self.assertEqual(state.as_dict(), before)
        self.assertEqual(deduplicate_source_cart_note(original, state=state, state_text=""), original)
        self.assertEqual(deduplicate_source_cart_note(original, state=None, state_text=rendered.text), original)
        self.assertEqual(deduplicate_source_cart_note(original.replace('"recipient_id":"self"', '"recipient_id":"other"'),
            state=state, state_text=rendered.text), original.replace('"recipient_id":"self"', '"recipient_id":"other"'))

    def test_catalog_priorities_keep_current_and_source_bound_browse_candidates(self):
        priorities = source_catalog_priorities({"lines": [{"fields": {
            "product_id": {"value": 7}, "garment_type": {"value": "tshirt"}}}]},
            {"catalog_browse": {"requests": [{"garments": ["hoodie"]}],
                "shortlist": [{"product_id": 44}, {"product_id": 2}, {"product_id": 88}]}})
        self.assertEqual(priorities["required_product_ids"], (2, 7, 44, 88))
        self.assertEqual(priorities["garment_types"], ("hoodie", "tshirt"))

    def test_source_catalog_prioritizes_protected_ids_before_garments_and_warns_about_slice(self):
        from management.services.bot_catalog import SOURCE_PROMPT_MAX_CHARS, get_catalog_context
        from storefront.models import Product
        self.catalog()
        protected = list(Product.objects.filter(category__slug="hoodies").order_by("id").values_list("id", flat=True)[:3])
        for limit in (SOURCE_PROMPT_MAX_CHARS, 12000):
            with self.subTest(limit=limit):
                scoped = get_catalog_context(compact=True, required_product_ids=protected,
                    garment_types=("tshirt",), prompt_limit=limit)
                expanded = expand_compact_catalog_text(scoped)
                rows = [row for row in expanded.splitlines() if row.startswith("• id=")]
                for product_id, row in zip(reversed(protected), rows, strict=False):
                    self.assertTrue(row.startswith(f"• id={product_id} |"))
                self.assertGreater(len(rows), len(protected), "Keep additional relevant choices after protected rows")
                self.assertIn("неповний перелік", expanded)
                self.assertIn("не означає відсутність товару чи залишку", expanded)
                self.assertIn("Правило ціни:", expanded)
                self.assertLessEqual(len(scoped), limit)
        with self.assertRaisesRegex(ValueError, "coverage_and_rules"):
            get_catalog_context(compact=True, required_product_ids=protected, prompt_limit=100)

    def test_garment_and_collection_interning_retains_every_id_relation_exactly(self):
        source = "Каталог TwoComms (усі актуальні товари; скорочений формат):\n" + "\n".join(
            f"• id={number} | Квітка — 1100 грн; кольори: Чорний (variant_id={number}, ціна 1100 грн); "
            f"категорія: {'tshirts' if number % 2 else 'hoodies'}; колекції: ukrainian-flowers; принт: малюнок {number}"
            for number in range(1, 121))
        compact = compact_catalog_text(source)
        self.assertEqual(expand_compact_catalog_text(compact), source)
        self.assertLess(len(compact), len(source) - 5000)

    def test_budget_never_drops_protected_product_rows(self):
        lines = ["Каталог TwoComms (усі актуальні товари; скорочений формат):", *(
            f"• id={number} | Обрана позиція {number} — {1000 + number} грн; " + "унікальний опис " * 8
            for number in range(10))]
        fitted, dropped = fit_compact_catalog(lines, limit=1100)
        self.assertGreater(dropped, 0)
        self.assertLessEqual(len(fitted), 1100)
        with self.assertRaisesRegex(ValueError, "coverage_and_rules"):
            fit_compact_catalog(lines, limit=1100, minimum_products=10)
