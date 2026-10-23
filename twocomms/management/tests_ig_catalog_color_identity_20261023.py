"""Known catalogue color remains identity when accounting stock is zero."""
from copy import deepcopy

from django.test import TestCase, override_settings

from product_catalog.models import VariantSizeRule
from management import tests_ig_source_cart_catalog as fixture
from management.services.ig_source_cart_catalog import resolve_source_cart_catalog
from management.services.ig_product_inquiries import capture_product_inquiries


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class CatalogColorIdentityTests(TestCase):
    setUp = fixture.SourceCartCatalogTests.setUp
    capture = fixture.SourceCartCatalogTests.capture

    def zero(self):
        VariantSizeRule.objects.create(variant=self.variants[0], fit_code="classic", size="M", is_enabled=True, stock=0)
        cart = self.capture()
        cart["lines"] = cart["lines"][:1]
        cart["active_line_id"] = cart["lines"][0]["line_id"]
        catalog = resolve_source_cart_catalog(cart)
        return cart, catalog

    def inquiry(self, cart, readiness, text="Is black M available?"):
        return capture_product_inquiries([{"message_id": 9, "role": "user", "text": text,
            "source_digest": "c"*64}], cart_capture=cart, readiness_by_line=readiness)

    def first_line(self, cart):
        # Keep the native catalogue's first product as the question's subject.
        cart["active_line_id"] = cart["lines"][0]["line_id"]
        return cart

    def test_zero_ledger_preserves_identity_without_orderability_or_stock_grants(self):
        cart, catalog = self.zero()
        state = catalog.readiness_by_line["line-0"]
        identity = state["color"]["catalog_color_identity"]
        self.assertEqual((identity["product_id"], identity["variant_id"], identity["resolved_choice"]),
            (self.products[0].pk, self.variants[0].pk, "black"))
        self.assertEqual(state["color"]["options"], [])
        self.assertIsNone(state["color"]["selected_variant_id"])
        self.assertFalse(state["applicability_known"])
        self.assertNotIn("line-0", catalog.item_specs_by_line)
        self.assertEqual(catalog.contexts_by_line["line-0"].allowed_colors, ())
        result = self.inquiry(self.first_line(cart), catalog.readiness_by_line)
        self.assertEqual(result["status"], "none", result)

    def test_unknown_blue_keeps_real_color_review(self):
        cart, catalog = self.zero()
        result = self.inquiry(self.first_line(cart), catalog.readiness_by_line, "Is blue M available?")
        self.assertEqual([(row["axis"], row["requested_value"]) for row in result["inquiries"]], [("color", "blue")])

    def test_sole_native_catalogue_variant_is_identity_without_inventing_customer_choice(self):
        VariantSizeRule.objects.create(variant=self.variants[0], fit_code="classic", size="M", is_enabled=True, stock=0)
        cart = self.capture()
        cart["lines"] = cart["lines"][:1]
        cart["lines"][0]["fields"].pop("color")
        self.first_line(cart)
        catalog = resolve_source_cart_catalog(cart)
        self.assertEqual(self.inquiry(cart, catalog.readiness_by_line)["status"], "none")
        self.assertNotIn("color", cart["lines"][0]["fields"])
        self.assertNotIn("line-0", catalog.item_specs_by_line)

    def test_unsigned_or_changed_product_variant_and_name_cannot_supply_identity(self):
        cart, catalog = self.zero()
        for field, value in (("signature", ""), ("product_id", self.products[1].pk),
                             ("variant_id", self.variants[1].pk), ("catalog_name", "Blue"), ("resolved_choice", "blue")):
            readiness = catalog.readiness_by_line
            readiness["line-0"]["color"]["catalog_color_identity"][field] = value
            with self.subTest(field=field):
                result = self.inquiry(self.first_line(deepcopy(cart)), readiness)
                self.assertEqual(result["reason"], "product_configuration_review", result)

    def test_replayed_identity_requires_exact_product_source_and_configuration(self):
        cart, catalog = self.zero()
        for field, value in (("product_id", self.products[1].pk), ("size", "L"), ("quantity", 2)):
            changed = self.first_line(deepcopy(cart))
            changed["lines"][0]["fields"][field]["value"] = value
            with self.subTest(field=field):
                self.assertEqual(self.inquiry(changed, catalog.readiness_by_line)["reason"], "product_configuration_review")
        changed = self.first_line(deepcopy(cart))
        changed["lines"][0]["fields"]["color"]["source"]["source_digest"] = "d"*64
        self.assertEqual(self.inquiry(changed, catalog.readiness_by_line)["reason"], "product_configuration_review")
        readiness = catalog.readiness_by_line
        readiness["line-0"]["product"]["id"] = self.products[1].pk
        self.assertEqual(self.inquiry(self.first_line(cart), readiness)["reason"], "product_configuration_review")

    def test_source_wish_alone_cannot_replace_a_catalogue_witness(self):
        cart, catalog = self.zero()
        readiness = catalog.readiness_by_line
        color = readiness["line-0"]["color"]
        color.pop("catalog_color_identity")
        color.update(selected="black", resolved_choice="black", verified_resolution=True)
        self.assertEqual(self.inquiry(self.first_line(cart), readiness)["reason"], "product_configuration_review")

    def test_known_black_does_not_suppress_independent_hardware_question(self):
        cart, catalog = self.zero()
        result = self.inquiry(self.first_line(cart), catalog.readiness_by_line,
            "Is black M available, and does it have metal eyelets?")
        self.assertEqual(result["reason"], "product_information_review", result)
        self.assertTrue(result["inquiries"])
        self.assertEqual({row["axis"] for row in result["inquiries"]}, {"information"})

    def test_known_black_does_not_suppress_independent_custom_size(self):
        cart, catalog = self.zero()
        result = self.inquiry(self.first_line(cart), catalog.readiness_by_line, "Is black XXS available?")
        self.assertEqual(result["reason"], "product_configuration_review", result)
        self.assertEqual([(row["axis"], row["requested_value"]) for row in result["inquiries"]], [("size", "XXS")])

    def test_per_line_plan_attaches_only_its_exact_witness_and_never_prompts_it(self):
        from management.services.ig_response_plan import build_response_plan
        from management.services.ig_reply_truth import ReplyTruthContext
        cart = self.capture()
        catalog = resolve_source_cart_catalog(cart)
        def plan(readiness):
            return build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(),
                sources=[{"message_id": 8, "role": "user", "text": "Classic black M; Storm pink L"}],
                captured_cart=cart, readiness_by_line=readiness, contexts_by_line=catalog.contexts_by_line)
        native = plan(catalog.readiness_by_line)
        self.assertFalse(native.plan_gap, native.plan_gap)
        self.assertEqual([row.as_dict()["configuration"]["catalog_color_identity"]["product_id"]
            for row in native.line_plans], [product.pk for product in self.products])
        self.assertNotIn("catalog_color_identity", str(native.prompt_projection()))
        self.assertNotIn("signature", str(native.prompt_projection()))
        for mutation in ("foreign_line", "tamper"):
            readiness = catalog.readiness_by_line
            if mutation == "foreign_line":
                readiness["line-0"]["color"]["catalog_color_identity"] = readiness["line-1"]["color"]["catalog_color_identity"]
            else:
                readiness["line-0"]["color"]["catalog_color_identity"]["resolved_choice"] = "blue"
            with self.subTest(mutation=mutation):
                denied = plan(readiness)
                self.assertNotIn("catalog_color_identity", denied.line_plans[0].as_dict()["configuration"])
                self.assertIn("catalog_color_identity", denied.line_plans[1].as_dict()["configuration"])
