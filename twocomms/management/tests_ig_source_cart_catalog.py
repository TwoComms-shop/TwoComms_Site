"""Native catalogue integration tests; no provider or checkout effects."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from product_catalog.models import VariantSizeRule
from productcolors.models import Color, ProductColorVariant
from storefront.models import Catalog, Category, Product, ProductFitOption, SizeGrid
from management.services.ig_source_cart_catalog import resolve_source_cart_catalog
from management.services.ig_checkout_readiness import selection_readiness


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class SourceCartCatalogTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Source cart apparel", slug="source-cart-apparel")
        self.products, self.variants = [], []
        for index, (name, price, color_name) in enumerate((("Classic", 790, "Black"), ("Storm", 1290, "Pink"))):
            catalog = Catalog.objects.create(name=name, slug=f"source-cart-{index}")
            grid = SizeGrid.objects.create(catalog=catalog, name=name, guide_data={
                "columns": [{"key": "size", "label": "Size"}],
                "rows": [{"size": size, "display_size": size} for size in ("M", "L")],
            })
            product = Product.objects.create(title=name, slug=f"source-cart-product-{index}", category=self.category,
                price=price, status="published", catalog=catalog, size_grid=grid)
            ProductFitOption.objects.create(product=product, code="classic", label="Classic", is_active=True, is_default=True)
            color = Color.objects.create(name=color_name, primary_hex="#111111" if index == 0 else "#ffaaaa")
            variant = ProductColorVariant.objects.create(product=product, color=color, price_override=price, is_default=True)
            self.products.append(product)
            self.variants.append(variant)

    def capture(self):
        scope = {"client_id": 4, "episode_id": 7, "order_id": None, "reset_id": None,
            "reset_floor": 1, "source_namespace": "test:ig"}
        capture = {"schema": "source-selections.v1", "status": "captured", "coverage_complete": True,
            "scope": scope, "session_id": 10, "generation": 2, "selection_revision": 3,
            "capture_digest": "a" * 64, "fence": {"source_ids": [8]}, "lines": []}
        for index, (product, color, size) in enumerate(zip(self.products, ("black", "pink"), ("M", "L"))):
            line_id = f"line-{index}"
            values = {"product_id": product.pk, "color": color, "size": size, "fit_option_code": "classic", "quantity": index + 1}
            proof = {"source_message_id": 8, "source_digest": "b" * 64, "transition_id": 12, "operation_index": index}
            capture["lines"].append({"line_id": line_id, "recipient_id": "self", "index": index,
                "source_selection": {"scope": {**scope, "session_id": 10, "generation": 2, "revision": 3,
                    "line_id": line_id, "recipient_id": "self"}},
                "fields": {key: {"value": value, "status": "confirmed", "source": dict(proof)} for key, value in values.items()},
                "defaults": {"quantity": {"value": 1, "authority": "existing_cart_default", "source_confirmed": False}}})
        return capture

    def test_two_products_prices_and_quantities_stay_on_their_lines(self):
        capture = self.capture()
        with CaptureQueriesContext(connection) as queries:
            result = resolve_source_cart_catalog(capture)
        self.assertTrue(result.complete, result.gaps)
        contexts = result.contexts_by_line
        self.assertEqual(contexts["line-0"].authorized_prices, (Decimal("790.00"),))
        self.assertEqual(contexts["line-1"].authorized_prices, (Decimal("1290.00"),))
        self.assertEqual(result.item_specs_by_line["line-0"]["qty"], 1)
        self.assertEqual(result.item_specs_by_line["line-1"]["qty"], 2)
        self.assertEqual(result.item_specs_by_line["line-1"]["color_variant_id"], self.variants[1].pk)
        self.assertLessEqual(len(queries), 128)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries), [query["sql"] for query in queries])

    def test_wrong_requested_color_is_unknown_even_with_one_catalog_variant(self):
        capture = self.capture()
        capture["lines"][0]["fields"]["color"]["value"] = "pink"
        result = resolve_source_cart_catalog(capture)
        self.assertFalse(result.complete)
        self.assertEqual(result.contexts_by_line["line-0"].authorized_prices, ())
        self.assertNotIn("line-0", result.item_specs_by_line)
        self.assertFalse(result.readiness_by_line["line-0"]["applicability_known"])

    def test_ambiguous_catalog_color_does_not_choose_first_variant(self):
        same_alias = Color.objects.create(name="Pink Thermo", primary_hex="#ffaabb")
        ProductColorVariant.objects.create(product=self.products[1], color=same_alias, price_override=1390)
        result = resolve_source_cart_catalog(self.capture())
        self.assertEqual(result.contexts_by_line["line-1"].authorized_prices, ())
        self.assertNotIn("line-1", result.item_specs_by_line)

    def test_duplicate_sku_preserves_distinct_recipient_size_and_quantity(self):
        capture = self.capture()
        second = capture["lines"][1]
        second["fields"]["product_id"]["value"] = self.products[0].pk
        second["fields"]["color"]["value"] = "black"
        second["recipient_id"] = second["source_selection"]["scope"]["recipient_id"] = "friend"
        result = resolve_source_cart_catalog(capture)
        self.assertTrue(result.complete, result.gaps)
        first, second = result.item_specs_by_line["line-0"], result.item_specs_by_line["line-1"]
        self.assertEqual((first["recipient_id"], first["size"], first["qty"]), ("self", "M", 1))
        self.assertEqual((second["recipient_id"], second["size"], second["qty"]), ("friend", "L", 2))

    def test_size_rule_stock_zero_blocks_price_and_spec(self):
        VariantSizeRule.objects.create(variant=self.variants[0], fit_code="classic", size="M", is_enabled=True, stock=0)
        result = resolve_source_cart_catalog(self.capture())
        self.assertFalse(result.complete)
        self.assertEqual(result.contexts_by_line["line-0"].authorized_prices, ())
        self.assertNotIn("line-0", result.item_specs_by_line)

    def test_hard_query_budget_discards_all_partial_authority(self):
        from management.services import ig_source_cart_catalog as catalog_module
        capture = self.capture()
        executed = []
        original_resolve = catalog_module._resolve
        def observed_execute(execute, sql, params, many, context):
            executed.append(sql)
            return execute(sql, params, many, context)
        def observed_resolve(owned_capture):
            # Installed inside _resolve, AFTER the strict guard. Django's debug
            # log also records rejected statements/transaction cleanup; this
            # observer sees only SQL admitted to the underlying executor.
            with connection.execute_wrapper(observed_execute):
                return original_resolve(owned_capture)
        with patch("management.services.ig_source_cart_catalog.MAX_READS", 1), patch.object(catalog_module, "_resolve", side_effect=observed_resolve):
            result = resolve_source_cart_catalog(capture)
        self.assertFalse(result.complete)
        self.assertEqual(result.gaps[0]["reason"], "source_cart_catalog_query_bound")
        self.assertEqual(result.contexts_by_line, {})
        self.assertEqual(result.item_specs_by_line, {})
        self.assertEqual(result.as_dict()["select_count"], 1)
        self.assertEqual(len(executed), 1)
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql in executed), executed)

    def test_nine_distinct_products_fail_before_catalog_reads(self):
        capture = self.capture()
        capture["lines"] = []
        template = self.capture()["lines"][0]
        for index in range(9):
            row = deepcopy(template)
            row["line_id"] = row["source_selection"]["scope"]["line_id"] = f"new-{index}"
            row["index"] = index
            row["fields"]["product_id"]["value"] = 10000 + index
            capture["lines"].append(row)
        with self.assertNumQueries(0):
            result = resolve_source_cart_catalog(capture)
        self.assertEqual(result.gaps[0]["reason"], "source_cart_catalog_product_bound")

    def test_injected_graph_must_match_requested_product(self):
        with self.assertNumQueries(0), self.assertRaisesMessage(ValueError, "catalog_product_mismatch"):
            selection_readiness(product_id=self.products[0].pk, selection={}, size="M", strict=True,
                _catalog_inputs={"product_id": self.products[1].pk, "product": self.products[1]})

    def test_result_json_is_detached_and_contains_no_model_objects(self):
        result = resolve_source_cart_catalog(self.capture())
        self.assertTrue(result.complete, result.gaps)
        state = result.readiness_by_line
        state["line-0"]["product"]["price"] = "1"
        self.assertEqual(result.readiness_by_line["line-0"]["product"]["price"], "790.00")
        import json
        json.dumps(result.as_dict())

    def test_unsupported_raw_option_and_variant_hints_cannot_override_source_color(self):
        capture = self.capture()
        capture["lines"][0]["color_variant_id"] = self.variants[1].pk
        capture["lines"][0]["option_values"] = {"fit": "oversize", "price": "1"}
        result = resolve_source_cart_catalog(capture)
        self.assertTrue(result.complete, result.gaps)
        self.assertEqual(result.item_specs_by_line["line-0"]["color_variant_id"], self.variants[0].pk)
        self.assertEqual(result.contexts_by_line["line-0"].authorized_prices, (Decimal("790.00"),))
