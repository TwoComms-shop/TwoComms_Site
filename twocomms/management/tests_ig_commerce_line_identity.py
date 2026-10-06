"""Exact per-operation original-source identities, without generated authority."""
from dataclasses import replace
from types import SimpleNamespace
import os
from unittest.mock import patch
from django.db import connection
from django.test.utils import CaptureQueriesContext

from django.test import SimpleTestCase, TestCase, override_settings

from management import tests_ig_commerce_line_source_parser as fixtures
from management.services.ig_commerce_source_identity import resolve_line_operation_identities, resolve_source_product_request
from management.services.ig_commerce_turns import parse_turn, line_operation_source_clauses
from management.services.ig_commerce_types import CatalogGraph, CatalogProduct


class CommerceLineIdentityPureTests(SimpleTestCase):
    def setUp(self):
        self.classic = CatalogProduct(1, "classic", "Classic", 1, "shirts", "Shirts", garment_type="tshirt")
        self.storm = CatalogProduct(2, "storm", "Storm", 2, "hoodies", "Hoodies", garment_type="hoodie")
        self.black = CatalogProduct(3, "black-heart", "Black Heart", 1, "shirts", "Shirts", garment_type="tshirt")
        self.graph = CatalogGraph((self.classic, self.storm, self.black), "a" * 64, "{}")

    def resolve(self, text, *, graph=None, request=None):
        return resolve_line_operation_identities(text, request or parse_turn(text), catalog_graph=graph or self.graph)

    def test_distinct_named_lines_resolve_their_own_ids_and_configuration(self):
        result = self.resolve("add black Classic size L; add pink Storm size XL")
        self.assertFalse(result.pending_line_clarification)
        self.assertEqual([operation.exact_product_id for operation in result.line_operations], [1, 2])
        self.assertEqual([dict(operation.field_updates) for operation in result.line_operations],
            [{"color": "black", "size": "L"}, {"color": "pink", "size": "XL"}])
        self.assertEqual([operation.garment_type for operation in result.line_operations], ["tshirt", "hoodie"])

    def test_titles_classic_and_black_are_identity_not_selected_fit_or_color(self):
        result = self.resolve("add Classic; add Black Heart")
        self.assertEqual([operation.exact_product_id for operation in result.line_operations], [1, 3])
        self.assertEqual([dict(operation.field_updates) for operation in result.line_operations], [{}, {}])

    def test_explicit_fit_marker_is_requirement_not_product_name(self):
        result = self.resolve("add fit Classic")
        self.assertIsNone(result.line_operations[0].exact_product_id)
        self.assertEqual(dict(result.line_operations[0].field_updates), {"fit": "classic"})
        result = self.resolve("add Classic fit oversize size M")
        self.assertEqual(result.line_operations[0].exact_product_id, 1)
        self.assertEqual(dict(result.line_operations[0].field_updates), {"fit": "oversize", "size": "M"})

    def test_old_and_new_named_replacement_ids_have_different_roles(self):
        result = self.resolve("replace Classic with Storm")
        self.assertFalse(result.pending_line_clarification)
        operation = result.line_operations[0]
        self.assertEqual((operation.target_product_id, operation.exact_product_id), (1, 2))
        self.assertEqual(operation.garment_type, "hoodie")
        self.assertEqual(dict(operation.field_updates), {})

    def test_reviewed_title_third_is_not_an_ordinal_target_or_add_selector(self):
        third = replace(self.storm, product_id=4, title="Third", slug="third")
        graph = CatalogGraph((*self.graph.products, third), "d" * 64, "{}")
        result = self.resolve("replace Classic with Third", graph=graph)
        self.assertEqual((result.line_operations[0].target_product_id, result.line_operations[0].exact_product_id), (1, 4))
        self.assertIsNone(result.line_operations[0].target_line_index)
        result = self.resolve("remove Third", graph=graph)
        self.assertEqual(result.line_operations[0].target_product_id, 4)
        self.assertIsNone(result.line_operations[0].target_line_index)
        result = self.resolve("add Third", graph=graph)
        self.assertEqual(result.line_operations[0].exact_product_id, 4)
        self.assertIsNone(result.line_operations[0].target_line_index)
        result = self.resolve("remove third line", graph=graph)
        self.assertEqual(result.line_operations[0].target_line_index, 2)
        self.assertIsNone(result.line_operations[0].target_product_id)
        result = self.resolve("remove third hoodie", graph=graph)
        self.assertEqual(result.pending_line_clarification, "ambiguous_line_target")

    def test_old_target_name_can_not_become_new_selected_product(self):
        result = self.resolve("remove Classic")
        operation = result.line_operations[0]
        self.assertEqual(operation.target_product_id, 1)
        self.assertIsNone(operation.exact_product_id)
        self.assertEqual(dict(operation.field_updates), {})
        result = self.resolve("change size of Classic to XL")
        operation = result.line_operations[0]
        self.assertEqual(operation.target_product_id, 1)
        self.assertIsNone(operation.exact_product_id)
        self.assertEqual(dict(operation.field_updates), {"size": "XL"})

    def test_unknown_old_target_or_new_replacement_never_changes_identity(self):
        for text in ("replace Unknown with Storm", "replace Classic with Unknown"):
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertTrue(result.pending_line_clarification)
                self.assertEqual(result.line_operations, ())

    def test_alias_collisions_and_contradictory_selection_do_not_pick_first(self):
        for text in ("add Classic or Storm", "add Classic, not Classic"):
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result.pending_line_clarification, "ambiguous_line_operation")
                self.assertEqual(result.line_operations, ())
        duplicate = replace(self.storm, aliases={"en": ("Classic",)})
        result = self.resolve("add Classic", graph=CatalogGraph((self.classic, duplicate), "b" * 64, "{}"))
        self.assertEqual(result.line_operations, ())

    def test_negated_old_name_does_not_win_over_positive_new_clause(self):
        result = self.resolve("add not Classic, but Storm")
        self.assertEqual(result.line_operations[0].exact_product_id, 2)
        self.assertEqual(dict(result.line_operations[0].field_updates), {})

    def test_question_quote_history_and_caption_have_no_name_authority(self):
        for text in ("add Classic?", "«add Classic»", "Earlier I wanted to add Classic",
            "Caption: add Classic", "I don't want to add Classic"):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text).line_operations, ())
        result = self.resolve("Earlier I wanted Classic, now add Storm")
        self.assertEqual(result.line_operations[0].exact_product_id, 2)

    def test_supplied_model_or_caller_fields_do_not_override_source_reparse(self):
        text = "add Classic"
        parsed = parse_turn(text)
        forged = replace(parsed, line_operations=(replace(parsed.line_operations[0], exact_product_id=2,
            field_updates={"color": "pink", "size": "M"}),))
        result = self.resolve(text, request=forged)
        self.assertEqual(result, self.resolve(text))

    def test_empty_graph_keeps_garment_only_operation_without_presentation_guess(self):
        empty = CatalogGraph((), "c" * 64, "{}")
        text = "добавьте ещё худи"
        self.assertEqual(self.resolve(text, graph=empty), parse_turn(text))

    def test_reviewed_numeric_versions_do_not_become_quantity_or_remove(self):
        for number in (0, 2, 51):
            with self.subTest(number=number):
                product = replace(self.classic, title=f"Extra Sketch {number}", aliases={})
                graph = CatalogGraph((product,), "d" * 64, "{}")
                result = self.resolve(f"add Extra Sketch {number} футболку", graph=graph)
                self.assertFalse(result.pending_line_clarification)
                self.assertEqual(len(result.line_operations), 1)
                operation = result.line_operations[0]
                self.assertEqual((operation.operation, operation.exact_product_id), ("add", product.product_id))
                self.assertNotIn("quantity", operation.field_updates)

    def test_real_quantity_outside_reviewed_numeric_title_is_preserved(self):
        product = replace(self.classic, title="Extra Sketch 0", aliases={})
        graph = CatalogGraph((product,), "d" * 64, "{}")
        for text in ("add 2 футболки Extra Sketch 0", "add Extra Sketch 0 quantity 2"):
            with self.subTest(text=text):
                result = self.resolve(text, graph=graph)
                self.assertFalse(result.pending_line_clarification)
                self.assertEqual(result.line_operations[0].exact_product_id, product.product_id)
                self.assertEqual(dict(result.line_operations[0].field_updates), {"quantity": 2})
        result = self.resolve("add 51 футболку Extra Sketch 0", graph=graph)
        self.assertEqual(result.pending_line_clarification, "ambiguous_line_quantity")
        self.assertEqual(result.line_operations, ())

    def test_units_and_quantity_words_inside_reviewed_titles_are_not_requirements(self):
        for title in ("Edition 2 Pieces", "Quantity 2", "Sketch 0 футболку"):
            with self.subTest(title=title):
                product = replace(self.classic, title=title, aliases={})
                result = self.resolve("add " + title, graph=CatalogGraph((product,), "d" * 64, "{}"))
                self.assertFalse(result.pending_line_clarification)
                self.assertEqual(result.line_operations[0].operation, "add")
                self.assertNotIn("quantity", result.line_operations[0].field_updates)

    def test_unknown_or_ambiguous_numeric_name_cannot_supply_remove_authority(self):
        text = "add Extra Sketch 0 футболку"
        empty = CatalogGraph((), "d" * 64, "{}")
        result = self.resolve(text, graph=empty)
        self.assertEqual(result.line_operations, ())
        self.assertEqual(result.pending_line_clarification, "ambiguous_line_quantity")
        product = replace(self.classic, title="Extra Sketch 0", aliases={})
        duplicate = replace(self.storm, title="Extra Sketch 0", aliases={})
        result = self.resolve(text, graph=CatalogGraph((product, duplicate), "d" * 64, "{}"))
        self.assertEqual(result.line_operations, ())
        self.assertEqual(result.pending_line_clarification, "ambiguous_line_operation")

    def test_prepared_graph_resolves_real_urls_without_orm_or_unknown_slug_fallback(self):
        from management.services.ig_product_references import resolve_product_reference
        with patch('management.services.ig_product_references.Product.objects') as orm:
            orm.filter.side_effect = AssertionError('prepared graph URL path must not query ORM')
            exact = resolve_product_reference('https://twocomms.shop/product/classic/', catalog_graph=self.graph)
            self.assertTrue(exact.is_exact)
            self.assertEqual(exact.product_id, self.classic.product_id)
            for value in ('https://twocomms.shop/product/missing/',
                'http://twocomms.shop/product/classic/', 'https://twocomms.shop.evil/product/classic/',
                'https://user@twocomms.shop/product/classic/', 'https://twocomms.shop:444/product/classic/',
                'https://twocomms.shop/product/classic/invalid-option/'):
                with self.subTest(value=value):
                    self.assertFalse(resolve_product_reference(value, catalog_graph=self.graph).is_exact)
            orm.filter.assert_not_called()

    def test_original_operation_clause_order_and_replacement_boundary_are_preserved(self):
        self.assertEqual(line_operation_source_clauses("add Classic; replace Classic with Storm"),
            (("", "add Classic"), (" replace Classic ", " Storm")))

    def test_owned_source_entry_point_rejects_foreign_client_without_catalog_io(self):
        client = SimpleNamespace(pk=1, igsid="client-1", privacy_erasure_started_at=None)
        source = SimpleNamespace(pk=9, client_id=2, sender_id="client-1", role="user", source="webhook",
            status="done", provider_namespace="instagram_login:owner", text="add Classic")
        result = resolve_source_product_request(client, source, parse_turn(source.text), catalog_graph=self.graph)
        self.assertEqual(result.pending_line_clarification, "line_source_unverified")
        self.assertEqual(result.line_operations, ())


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class CommerceNamedSourceReductionTests(fixtures.CommerceStateFixture, TestCase):
    source = fixtures.CommerceParsedSourceReductionTests.source
    reduce = fixtures.CommerceParsedSourceReductionTests.reduce

    def test_named_additions_capture_separate_source_product_ids(self):
        first = self.reduce("add Classic size L; add Reality Bends size M")
        first.session.refresh_from_db()
        self.assertEqual([line["product_id"] for line in first.session.lines], [self.classic.pk, self.reality.pk])
        self.assertEqual([line["size"] for line in first.session.lines], ["L", "M"])
        self.assertTrue(all("fit_option_code" not in line for line in first.session.lines))

    def test_named_replacement_uses_exact_old_sku_even_when_other_line_is_active(self):
        initial = self.reduce("add Classic size L; add Reality Bends size M")
        old_ids = [line["line_id"] for line in initial.session.lines]
        replacement = self.reduce("replace Classic with Third")
        replacement.session.refresh_from_db()
        self.assertEqual([line["product_id"] for line in replacement.session.lines], [self.third.pk, self.reality.pk])
        self.assertEqual([line["line_id"] for line in replacement.session.lines], old_ids)
        self.assertNotIn("size", replacement.session.lines[0])
        self.assertEqual(replacement.session.lines[1]["size"], "M")

    def test_named_remove_does_not_remove_a_different_current_sku(self):
        initial = self.reduce("add Reality Bends size M")
        previous = initial.session.snapshot()
        rejected = self.reduce("remove Classic")
        rejected.session.refresh_from_db()
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.session.lines, previous["lines"])

    def test_actual_old_and_new_product_urls_are_target_and_replacement(self):
        old = f"https://twocomms.shop/product/{self.classic.slug}/"
        new = f"https://twocomms.shop/product/{self.reality.slug}/"
        initial = self.reduce(f"{old} black size L")
        previous_line_id = initial.session.lines[0]["line_id"]
        replaced = self.reduce(f"replace {old} with {new}")
        replaced.session.refresh_from_db()
        self.assertTrue(replaced.accepted)
        self.assertEqual(replaced.session.lines[0]["product_id"], self.reality.pk)
        self.assertEqual(replaced.session.lines[0]["line_id"], previous_line_id)
        self.assertNotIn("size", replaced.session.lines[0])
        self.assertNotIn("color", replaced.session.lines[0])

    def test_exact_link_and_different_named_product_do_not_pin_a_first_match(self):
        initial = self.reduce("add Third size M")
        previous = initial.session.snapshot()
        link = f"https://twocomms.shop/product/{self.reality.slug}/"
        rejected = self.reduce(f"add Classic {link}")
        rejected.session.refresh_from_db()
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.session.lines, previous["lines"])

    def test_actual_zero_title_add_never_deletes_unrelated_single_position(self):
        product = self.product("Extra Sketch 0", "numeric-zero")
        initial = self.reduce("add Classic size L")
        previous = dict(initial.session.lines[0])
        added = self.reduce("add Extra Sketch 0 футболку")
        added.session.refresh_from_db()
        self.assertTrue(added.accepted)
        self.assertEqual(added.session.lines[0], previous)
        self.assertEqual(len(added.session.lines), 2)
        self.assertEqual(added.session.lines[1]["product_id"], product.pk)
        self.assertEqual(added.session.lines[1]["quantity"], 1)

    def test_actual_numeric_versions_and_real_external_quantity_remain_distinct(self):
        second = self.product("Extra Sketch 2", "numeric-two")
        large = self.product("Extra Sketch 51", "numeric-large")
        added = self.reduce("add Extra Sketch 2 футболку; add Extra Sketch 51 футболку")
        self.assertEqual([line["product_id"] for line in added.session.lines], [second.pk, large.pk])
        self.assertEqual([line["quantity"] for line in added.session.lines], [1, 1])
        multiple = self.reduce("add 2 футболки Extra Sketch 2")
        self.assertEqual(multiple.session.lines[-1]["quantity"], 2)

    def test_actual_unpublished_numeric_title_is_finite_and_preserves_single_position(self):
        from storefront.models import ProductStatus
        draft = self.product("Extra Sketch 0", "numeric-draft")
        draft.status = ProductStatus.DRAFT
        draft.save(update_fields=["status"])
        initial = self.reduce("add Classic size L")
        previous = initial.session.snapshot()
        rejected = self.reduce("add Extra Sketch 0 футболку")
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.session.lines, previous["lines"])

    def test_actual_two_url_source_capture_has_bounded_selects_and_fresh_source_fence(self):
        from management.models import InstagramBotMessage, InstagramBotSettings
        from management.services.instagram_bot import ingress_provider_namespace
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_turn_capture import validate_current_source_cart_sources
        from management.services.ig_turn_intelligence import TurnContextError
        with patch.dict(os.environ, {'IG_PROVIDER_TRANSPORT': 'instagram_login'}):
            settings = InstagramBotSettings.objects.create(pk=1, ig_user_id='real-url-capture-owner')
            namespace = ingress_provider_namespace(settings)
            original_source = self.source
            def scoped_source(*args, **kwargs):
                row = original_source(*args, **kwargs)
                row.provider_namespace = namespace
                row.save(update_fields=['provider_namespace'])
                return row
            with patch.object(self, 'source', side_effect=scoped_source):
                first = self.reduce(f'add https://twocomms.shop/product/{self.classic.slug}/ size L')
                second = self.reduce(f'add https://twocomms.shop/product/{self.reality.slug}/ size M')
            with CaptureQueriesContext(connection) as reads:
                captured = capture_current_selection_lines(self.client.pk)
            self.assertEqual(captured['status'], 'captured', captured.get('reason'))
            self.assertTrue(captured['coverage_complete'])
            self.assertEqual([line['fields']['product_id']['value'] for line in captured['lines']],
                [self.classic.pk, self.reality.pk])
            self.assertLessEqual(len(reads), 64)
            self.assertTrue(all(row['sql'].lstrip().upper().startswith('SELECT') for row in reads))
            self.assertTrue(validate_current_source_cart_sources(captured))
            InstagramBotMessage.objects.filter(pk=first.source_message_id).update(text='changed original URL source')
            with self.assertRaises(TurnContextError) as rejected:
                validate_current_source_cart_sources(captured)
            self.assertEqual(rejected.exception.reason, 'source_cart_sources_changed')
