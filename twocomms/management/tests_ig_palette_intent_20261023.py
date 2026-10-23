"""Palette consultation names its subject without choosing a cart garment."""
import base64
import json
from copy import deepcopy
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management import tests_ig_revision_live as live
from management import tests_ig_live_conversation_contract_20261022 as contract
from management.models import IgFollowUpTask
from management.services.ig_commerce_turns import parse_turn, understand_turn


class PaletteSourceIntentTests(SimpleTestCase):
    def test_numeric_spaced_size_alias_is_one_size_without_losing_source_controls(self):
        for text, expected in (("Беру 2 XL", "XXL"), ("Беру 2\tXL", "XXL"),
                               ("Беру 3 XL", "XXXL"), ("Беру 2 ХL", "XXL"),
                               ("Беру XL", "XL"), ("Беру 2XL", "XXL")):
            with self.subTest(text=text):
                self.assertEqual(parse_turn(text).field_updates.get("size"), expected)
        for text in ('«2 XL»', "Є худі 2 XL?", "Не хочу 2 XL", "I don't want 2 XL",
                     "Беру A2XL", "Беру 2XLcode", "Беру 2 XL або M"):
            with self.subTest(text=text):
                self.assertNotIn("size", parse_turn(text).field_updates)

    def test_catalog_subject_filters_mixed_candidates_instead_of_only_ranking_them(self):
        from management import tests_ig_catalog_cards as card_fixtures
        from management.services.ig_catalog_candidates import rank_candidates
        from management.services.ig_commerce_types import CatalogGraph, CatalogProduct

        graph = CatalogGraph(products=tuple(CatalogProduct(product_id=pk, slug=f"model-{pk}",
            title=f"Model {pk}", category_id=1, category_slug=kind, category_label=kind,
            garment_type=kind, catalog_priority=priority, pricing=card_fixtures._pricing())
            for pk, kind, priority in ((1, "tshirt", 999), (2, "hoodie", 0), (3, "hoodie", 0))),
            digest="mixed-category-test", canonical_json="{}")
        for text in ("Покажіть худі", "Show me hoodies", "Які худі є?"):
            with self.subTest(text=text):
                result = rank_candidates(graph, parse_turn(text))
                self.assertEqual(set(result.ordered_product_ids), {2, 3})
                self.assertTrue(all(("garment_type", "hoodie") in row.constraints for row in result.candidates))

    def test_empty_catalog_subject_preserves_historical_request_canonical_payload(self):
        from management.services.ig_commerce_state import _jsonable, _request_payload
        from management.services.ig_commerce_types import CommerceTurnRequest

        request = parse_turn("Хочу худі")
        previous = _jsonable(request)
        previous.pop("catalog_garment_type")
        self.assertEqual(_request_payload(request), previous)
        self.assertEqual(_request_payload(CommerceTurnRequest(**previous)), previous)
        self.assertEqual(_request_payload(parse_turn("Show me hoodies"))["catalog_garment_type"], "hoodie")
        with self.assertRaisesRegex(ValueError, "catalog_garment_type_invalid"):
            CommerceTurnRequest(catalog_garment_type="invented-category")

    def test_ordinary_palette_imperatives_name_a_subject_without_selecting_it(self):
        for text in ("Покажіть кольори худі", "Покажіть палітру кольорів худі",
                     "Покажите цвета худи", "Покажите палитру цветов худи",
                     "Show hoodie colors", "Show me the colors of this hoodie",
                     "Show me the colours of this hoodie"):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual(request.garment_type, "")
                self.assertEqual(request.field_updates, {})
                self.assertEqual(request.line_operations, ())
                self.assertFalse(request.checkout_requested)
                self.assertEqual(request.catalog_garment_type, "hoodie")

    def test_catalog_show_and_category_questions_keep_retrieval_subject_without_cart_choice(self):
        for text in ("Покажіть худі", "Покажите худи", "Show me hoodies", "Які худі є?"):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual(request.garment_type, "")
                self.assertEqual(request.catalog_garment_type, "hoodie")
                self.assertFalse(request.purchase_requested)
                proposed = understand_turn(text, model_payload={"garment_type": "hoodie"})
                self.assertNotIn("garment_type", proposed.field_updates)
                self.assertEqual(proposed.catalog_garment_type, "hoodie")

    def test_actual_choices_and_cart_commands_remain_affirmed(self):
        for text in ("Хочу худі", "Хочу худи", "I want a hoodie",
                     "Хочу худі, покажіть кольори", "Покажіть кольори, хочу худі",
                     "I want a hoodie, show me the colors", "Покажіть кольори, а я беру худі"):
            with self.subTest(text=text):
                self.assertEqual(parse_turn(text).garment_type, "hoodie")
        for text in ("Додайте худі до замовлення", "Добавьте худи в заказ", "Add a hoodie to my order"):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertTrue(request.line_operations)
                self.assertEqual(request.line_operations[0].operation, "add")
                self.assertEqual(request.line_operations[0].garment_type, "hoodie")

    def test_negated_quoted_and_historical_palette_text_does_not_select_a_garment(self):
        for text in ('«Покажіть кольори худі»', "Раніше просив показати кольори худі.",
                     "Не хочу худі", "Не хочу худи", "I don't want a hoodie",
                     "Не показуйте худі", "Don't show hoodies"):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual(request.garment_type, "")
                self.assertEqual(request.catalog_garment_type, "")


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativePaletteIntentTests(TransactionTestCase):
    reset_sequences = True
    setUp = live.RevisionLiveTests.setUp
    _message = live.RevisionLiveTests._message
    _prepare = live.RevisionLiveTests._prepare
    _execute = live.RevisionLiveTests._execute
    _replace_bundle = live.RevisionLiveTests._replace_bundle
    _generate = contract.NativeLiveConversationContractTests._generate
    _fresh = contract.NativeLiveConversationContractTests._fresh
    _answer = contract.NativeLiveConversationContractTests._answer
    _select_product = contract.NativePendingProductInformationBoundaryTests._select_product

    def _category_products(self):
        from storefront.models import Category, Product
        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=")
        products = []
        for index, kind in enumerate(("tshirts", "hoodie", "hoodie", "hoodie")):
            category, _ = Category.objects.get_or_create(slug=kind, defaults={"name": kind})
            product = Product(title=f"Catalog model {index}", slug=f"catalog-model-{index}",
                category=category, price=900, status="published")
            product.main_image.save(f"catalog-model-{index}.png", ContentFile(image), save=False)
            product.save()
            products.append(product)
        return products

    def test_initial_category_request_sends_native_cards_only_from_its_hard_subject(self):
        from management.models import IgCommerceSelectionSession
        from management.services.ig_catalog_candidates import rank_candidates
        from management.services.ig_catalog_graph import build_catalog_graph
        from management.services.ig_revision_outbox import _canonical

        with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            products = self._category_products()
            for text, reply in (("Покажіть худі", "Ось кілька моделей худі."),
                                ("Покажите худи", "Вот несколько моделей худи."),
                                ("Show me hoodies", "Here are several hoodie models.")):
                with self.subTest(text=text):
                    ranked = rank_candidates(build_catalog_graph(), parse_turn(text))
                    self.assertEqual(set(ranked.ordered_product_ids), {row.pk for row in products[1:]})
                    self._fresh(text)
                    self.parsed = {"reply_text": reply,
                        "controls": [{"kind": "show_products", "value": list(ranked.ordered_product_ids)}]}
                    result, model, http = self._execute([(200, json.dumps({"message_id": f"catalog-{self.revision.pk}-{index}"})) for index in range(8)])
                    self.revision.refresh_from_db()
                    self.assertEqual(result.state, "completed", (result.reasons, self.revision.action_receipts))
                    model.assert_called_once()
                    sent = list(self.revision.delivery_effects.filter(state="sent"))
                    native = [row for row in sent if (row.payload.get("message", {}).get("attachment") or {}).get("type") == "template"]
                    self.assertEqual(len(native), 1)
                    binding = next(row for row in native[0].fact_bindings if row["claim"] == "catalog_asset_references")
                    self.assertLessEqual(len(_canonical(binding)), 4096)
                    reference = binding["selector"]["catalog_choice_reference"]
                    self.assertEqual(reference["scope"]["revision_snapshot_digest"], self.revision.snapshot_digest)
                    self.assertEqual(reference["scope"]["source_message_ids"], [self.source.pk])
                    self.assertTrue(all("category_reference_digest" in row for row in reference["cards"]))
                    elements = native[0].payload["message"]["attachment"]["payload"]["elements"]
                    self.assertEqual({row["title"] for row in elements}, {row.title for row in products[1:]})
                    self.assertEqual(http.call_count, len(sent))
                    session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
                    self.assertEqual(session.query_constraints.get("garment_type"), "hoodie")
                    self.assertFalse(any(row.get("garment_type") or row.get("product_id") for row in session.lines))
                    self.assertFalse(IgFollowUpTask.objects.filter(client=self.customer).exists())
                    self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])

    def test_compact_category_binding_rejects_category_reference_source_and_body_tampering_before_send(self):
        from django.db.models import QuerySet
        from management.services import ig_revision_live as service
        from storefront.models import Category

        with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            products = self._category_products()
            drain = service._drain_effects
            for changed_field in ("category", "reference", "source", "body"):
                with self.subTest(changed_field=changed_field):
                    self._fresh("Show me hoodies")
                    self.parsed = {"reply_text": "Here are several hoodie models.", "controls": [
                        {"kind": "show_products", "value": [row.pk for row in products[1:]]}]}
                    mutations = []
                    def changed(*args, **kwargs):
                        native = self.revision.delivery_effects.get(group="template")
                        mutations.append(native.pk)
                        if changed_field == "category":
                            Category.objects.filter(pk=products[1].category_id).update(name="Changed after planning")
                        elif changed_field == "reference":
                            for effect in self.revision.delivery_effects.all():
                                bindings = deepcopy(effect.fact_bindings)
                                for binding in bindings:
                                    if binding["claim"] == "catalog_asset_references":
                                        binding["selector"]["catalog_choice_reference"]["cards"][0]["category_reference_digest"] = "0" * 64
                                # Simulate persisted corruption below the ORM's
                                # immutable-plan guard; the send guard must
                                # independently reject it as well.
                                QuerySet.update(type(effect).objects.filter(pk=effect.pk), fact_bindings=bindings)
                        elif changed_field == "source":
                            type(self.source).objects.filter(pk=self.source.pk).update(text="Show me T-shirts instead")
                        else:
                            payload = deepcopy(native.payload)
                            payload["message"]["attachment"]["payload"]["elements"][0]["title"] = "Substituted body"
                            QuerySet.update(type(native).objects.filter(pk=native.pk), payload=payload)
                        return drain(*args, **kwargs)
                    try:
                        with patch.object(service, "_drain_effects", side_effect=changed):
                            result, _model, http = self._execute([(200, json.dumps({"message_id": f"prefix-{index}"})) for index in range(8)])
                        self.assertTrue(mutations, result.reasons)
                        self.assertNotEqual(result.state, "completed", result.reasons)
                        self.assertFalse(self.revision.delivery_effects.filter(group="template", state="sent").exists())
                        if changed_field != "body":
                            http.assert_not_called()
                        self.assertFalse(any((call.kwargs.get("data") or {}).get("message", {}).get("attachment")
                            for call in http.call_args_list))
                    finally:
                        Category.objects.filter(pk=products[1].category_id).update(name="hoodie")

    def test_initial_catalog_show_retains_query_subject_without_a_false_selected_garment(self):
        from management.models import IgCommerceSelectionSession
        for text, reply in (("Покажіть худі", "Можна переглянути моделі в каталозі."),
                            ("Show me hoodies", "You can browse the models in our catalog."),
                            ("Які худі є?", "Можна переглянути моделі в каталозі.")):
            with self.subTest(text=text):
                self._fresh(text)
                self._answer(reply)
                session = IgCommerceSelectionSession.objects.filter(client=self.customer, open_slot=1).get()
                self.assertEqual(session.query_constraints.get("garment_type"), "hoodie")
                self.assertFalse(any(row.get("garment_type") for row in session.snapshot().get("lines", [])))
                self.assertFalse(IgFollowUpTask.objects.filter(client=self.customer).exists())

    def test_palette_imperatives_deliver_once_without_false_choice_or_manager_debt(self):
        from management.models import IgCommerceSelectionSession
        product = self._select_product(1)
        for text, reply in (
            ("Покажіть кольори худі", "Палітру кольорів можна переглянути на сторінці моделі."),
            ("Покажите цвета худи", "Палитру можно посмотреть на странице модели."),
            ("Show me the colors of this hoodie", "You can view the palette on the product page."),
        ):
            with self.subTest(text=text):
                self._fresh(text)
                self._answer(reply)
                self.revision.refresh_from_db()
                coverage = self.revision.action_receipts.get("response_coverage") or {}
                self.assertEqual(coverage.get("remaining"), [])
                self.assertFalse(any(str(value).endswith(":garment_type") for value in coverage.get("covered", [])))
                effect = self.revision.delivery_effects.get()
                self.assertEqual(effect.state, "sent")
                self.assertEqual(effect.source_message_id, self.source.pk)
                session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
                self.assertEqual(session.snapshot()["lines"][session.snapshot()["active_index"]]["product_id"], product.pk)
                self.assertFalse(IgFollowUpTask.objects.filter(client=self.customer).exists())
