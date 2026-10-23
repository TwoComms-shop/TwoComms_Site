"""Catalog selection is not physical stock evidence; retain selection safeguards."""
from copy import deepcopy
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings

from management.services import ig_catalog_cards as cards
from management.services.ig_checkout_readiness import (
    _disabled_sizes, checkout_readiness, readiness_prompt_note,
)


def readiness(sizes=(), *, disabled=(), requested=""):
    return {
        "has_product": True,
        "product": {"id": 7, "title": "Hoodie Urban", "published": True},
        "fit": {"required": True, "selected": "classic", "options": [{"code": "classic", "label": "Classic"}]},
        "size": {"required": True, "selected": "", "available": list(sizes),
                 "disabled": list(disabled), "requested_unavailable": requested},
        "missing": ["size"], "can_issue_link": False,
    }


def visible_copy(plan):
    payload = plan.payload
    if isinstance(payload, str):
        return payload
    return " ".join([
        getattr(payload, "text", ""), getattr(payload, "fallback_text", ""),
        *(" ".join([card.title, card.subtitle]) for card in getattr(payload, "cards", ())),
    ])


class StockCatalogCopyTests(SimpleTestCase):
    def assert_no_stock_claim(self, text):
        for claim in ("в наявності", "в наличии", "in stock", "out of stock",
                      "нічого немає", "ничего нет", "nothing matches"):
            self.assertNotIn(claim, text.casefold())

    def test_all_size_presentations_are_catalog_options_in_all_three_languages(self):
        cases = (("M",), ("S", "M", "L"), ("S", "M", "L", "XL"), tuple(f"SIZE{i}" for i in range(14)))
        for lang, word in (("uk", "каталозі"), ("ru", "каталог"), ("en", "catalogue")):
            for sizes in cases:
                with self.subTest(lang=lang, sizes=sizes):
                    state = readiness(sizes)
                    before = deepcopy(state)
                    plan = cards.plan_size_choice(state, lang=lang, generation=12, grid={"columns": ["Length", "Width"]})
                    text = visible_copy(plan)
                    self.assert_no_stock_claim(text)
                    self.assertIn(word, text.casefold())
                    self.assertNotIn("менедж", text.casefold())
                    self.assertNotIn("manager", text.casefold())
                    self.assertEqual(plan.available_sizes, sizes)
                    self.assertEqual(plan.generation, 12)
                    self.assertEqual(state, before)
                    for size in sizes:
                        self.assertIn(size, text)

    def test_disabled_sizes_never_reappear_as_buttons_after_copy_change(self):
        for lang in ("uk", "ru", "en"):
            plan = cards.plan_size_choice(readiness(("S", "M", "L"), disabled=("M",)), lang=lang, generation=3)
            self.assertEqual(plan.available_sizes, ("S", "L"))
            self.assertEqual(plan.disabled_sizes, ("M",))
            self.assertEqual([button.payload for button in plan.payload.cards[0].buttons],
                ["twc:1:size:3:set:S", "twc:1:size:3:set:L"])
            self.assert_no_stock_claim(visible_copy(plan))
            self.assertEqual(plan.payload.cards[0].title, "Hoodie Urban")

    def test_zero_sizes_explain_catalog_limitation_without_stock_or_sent_handoff_claim(self):
        for lang, manager in (("uk", "менеджера"), ("ru", "менеджера"), ("en", "manager")):
            for disabled in ((), ("S", "M")):
                plan = cards.plan_size_choice(readiness(disabled=disabled), lang=lang)
                self.assertEqual(plan.kind, cards.KIND_TEXT)
                self.assertEqual(plan.reason, "no_available_sizes")
                self.assertEqual(plan.disabled_sizes, disabled)
                self.assertIn(manager, plan.payload)
                self.assert_no_stock_claim(plan.payload)
                for promise in ("уточню", "i will", "повернуся", "вернусь"):
                    self.assertNotIn(promise, plan.payload.casefold())

    def test_measurement_fallback_names_catalog_sizes_with_or_without_fit(self):
        for lang in ("uk", "ru", "en"):
            for fit in ("", "Classic"):
                plan = cards.plan_size_grid_card(lang=lang, available_sizes=("S", "M"), fit_label=fit, grid={"columns": ["Length", "Width"]})
                self.assert_no_stock_claim(visible_copy(plan))
                self.assertNotIn("available sizes", visible_copy(plan).casefold())
                self.assertEqual(plan.available_sizes, ("S", "M"))

    def test_zero_search_results_describe_catalog_match_and_keep_source_filters(self):
        for lang, word in (("uk", "каталозі"), ("ru", "каталоге"), ("en", "catalogue")):
            for filters in ({}, {"color": "black", "size": "M"}):
                text = cards.zero_candidates_text(filters, lang)
                self.assertIn(word, text.casefold())
                self.assert_no_stock_claim(text)
                if filters:
                    self.assertIn("black", text)
                    self.assertIn("M", text)

    def test_prompt_requires_exact_manager_stock_check_without_rewriting_readiness(self):
        for lang in ("uk", "ru", "en"):
            state = readiness(("S", "M"), disabled=("L",), requested="L")
            before = deepcopy(state)
            note = readiness_prompt_note(SimpleNamespace(pk=7, language=lang), readiness=state)
            for expected in ("не підтверджують фізичну наявність чи відсутність",
                "обраного товару, кольору, крою, розміру та кількості",
                "не перенось підтвердження на іншу конфігурацію", "варіанти каталогу: S, M",
                "вимкнені для вибору розміри каталогу: L", "розмір L не можна обрати для автоматичного оформлення",
                "Не підмінюй запитаний розмір іншим"):
                self.assertIn(expected, note)
            self.assertNotIn("ти перевірила наявність", note)
            self.assertNotIn("немає в наявності", note)
            self.assertEqual(state, before)


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class StockCatalogSelectionInvariantTests(TestCase):
    def setUp(self):
        from management.models import IgClient
        from product_catalog.models import VariantSizeRule
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product, ProductFitOption, ProductStatus
        category = Category.objects.create(name="Stock copy", slug="stock-copy-20261023")
        product = Product.objects.create(title="Stock copy shirt", slug="stock-copy-shirt-20261023", category=category, price=880, status=ProductStatus.PUBLISHED)
        ProductFitOption.objects.create(product=product, code="classic", label="Classic", is_active=True)
        color = Color.objects.create(name="Stock copy black", primary_hex="#111111")
        self.variant = ProductColorVariant.objects.create(product=product, color=color, stock=0, slug="stock-copy-black")
        for size, enabled, stock in (("S", True, 3), ("M", True, 0), ("L", False, 999)):
            VariantSizeRule.objects.create(variant=self.variant, fit_code="classic", size=size, is_enabled=enabled, stock=stock)
        self.client_row = IgClient.get_or_create_for_sender("stock-copy-20261023")
        self.client_row.current_product = product
        self.client_row.current_size = "M"
        self.client_row.sales_context = {"assisted_checkout_selection": {"product_id": product.pk, "fit_option_code": "classic", "color_variant_id": self.variant.pk}}
        self.client_row.save(update_fields=["current_product", "current_size", "sales_context", "updated_at"])

    def test_zero_count_and_explicitly_disabled_sizes_stay_blocked_without_physical_stock_claims(self):
        from product_catalog.models import VariantSizeRule
        before = list(VariantSizeRule.objects.filter(variant=self.variant).values_list("size", "is_enabled", "stock"))
        self.assertEqual(_disabled_sizes(self.variant, "classic"), ["M", "L"])
        state = checkout_readiness(self.client_row)
        for size in ("M", "L"):
            self.assertIn(size, state["size"]["disabled"])
            self.assertNotIn(size, state["size"]["available"])
        self.assertFalse(state["can_issue_link"])
        note = readiness_prompt_note(self.client_row, readiness=state)
        self.assertIn("не підтверджують фізичну наявність чи відсутність", note)
        self.assertNotIn("ти перевірила наявність", note)
        self.assertEqual(before, list(VariantSizeRule.objects.filter(variant=self.variant).values_list("size", "is_enabled", "stock")))
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 0)
