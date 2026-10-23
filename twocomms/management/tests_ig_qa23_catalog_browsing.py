"""Relevant merchandise, source-bound themed navigation and receipt compatibility."""
from copy import deepcopy
from dataclasses import replace
import base64
import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from django.test import TestCase, SimpleTestCase, TransactionTestCase, override_settings
from django.core.files.base import ContentFile

from management.services import ig_catalog_browsing as browse
from management.services import ig_catalog_choice_presentations as choices
from management.services.ig_revision_outbox import _canonical, _digest, _safe_bindings
from management import tests_ig_catalog_choice_presentations as fixtures
from product_catalog.models import MerchCollection, ProductMerchCollection
from storefront.models import Category, Product
from management import tests_ig_revision_live as live
from management import tests_ig_live_conversation_contract_20261022 as contract


@override_settings(SITE_BASE_URL="https://twocomms.test")
class CatalogBrowseCaptureTests(TestCase):
    def setUp(self):
        self.tee = Category.objects.create(name="Футболки", slug="tshirts")
        self.hoodie = Category.objects.create(name="Худі", slug="hoodie")
        self.kharkiv = MerchCollection.objects.create(slug="kharkiv", kind="city", name_uk="Харків", name_ru="Харьков", name_en="Kharkiv")
        self.street = MerchCollection.objects.create(slug="streetwear", kind="theme", name_uk="Streetwear")
        self.military = MerchCollection.objects.create(slug="military", kind="theme", name_uk="Військові")
        self.brigades = MerchCollection.objects.create(slug="brigades", kind="theme", name_uk="Бригади")
        self.b225 = MerchCollection.objects.create(slug="225", kind="brigade", parent=self.brigades, name_uk="225 бригада")
        MerchCollection.objects.create(slug="127", kind="brigade", parent=self.brigades, name_uk="127 бригада")
        self.a = self.product("Aurora", "tee", self.tee, self.kharkiv, self.street)
        self.b = self.product("Aurora", "hoodie", self.hoodie, self.kharkiv)
        self.c = self.product("Military", "military", self.hoodie, self.military)
        self.d = self.product("225 Design", "225", self.tee, self.b225)
        self.customer = SimpleNamespace(pk=7, language="uk")

    def product(self, title, slug, category, *collections):
        product = Product.objects.create(title=title, slug=slug, category=category, price=900, status="published")
        for collection in collections:
            ProductMerchCollection.objects.create(product=product, collection=collection)
        return product

    def revision(self, *texts):
        bundle = {"sources": [{"message_id": index + 10, "source_namespace": "instagram_login:owner", "role": "user",
            "text": text, "source_digest": _digest({"text": text})} for index, text in enumerate(texts)]}
        return SimpleNamespace(pk=777, client_id=7, client=self.customer, bundle_snapshot=bundle, snapshot_digest=_digest(bundle))

    def capture(self, *texts):
        return browse.capture_catalog_browse(self.revision(*texts))

    def response(self, ids, text="Ось кілька моделей."):
        return SimpleNamespace(control={"show_products": ",".join(str(pk) for pk in ids)}, reply_text=text)

    def test_membership_not_exact_tag_set_and_garment_is_hard(self):
        all_types = self.capture("Покажіть Харків")
        self.assertEqual(set(all_types["eligible_product_ids"]), {self.a.pk, self.b.pk})
        capture = self.capture("Покажіть худі з Харковом")
        self.assertEqual(capture["eligible_product_ids"], [self.b.pk])
        self.assertEqual(browse.validate_catalog_browse_response(capture, self.response([self.a.pk])), "catalog_browse_product_mismatch")
        self.assertEqual(browse.validate_catalog_browse_response(capture, self.response([self.b.pk])), "")
        self.assertFalse(capture["selection_authority"])
        self.assertFalse(capture["stock_authority"])
        self.assertFalse(capture["consent_authority"])

    def test_attribute_existence_questions_do_not_create_catalogue_requests(self):
        for text in ("Підкажіть, які є розміри?", "Які є кольори?", "Які є ціни?",
                     "Які є способи оплати?", "Які є варіанти доставки?",
                     "Які є способи оплати для товарів?", "Какие есть цвета футболок?",
                     "Какие есть размеры?", "Какие есть цвета?", "Какие есть цены?",
                     "Какие есть способы оплаты?", "Какие есть варианты доставки?",
                     "What sizes are available?", "What colors do you have?", "What are the prices?",
                     "What payment methods are available?", "What delivery options are there?"):
            with self.subTest(text=text), self.assertNumQueries(0):
                self.assertEqual(self.capture(text), {})

    def test_merchandise_existence_questions_and_explicit_unknown_browse_stay_owned(self):
        for text, expected in (("Які є худі?", {self.b.pk, self.c.pk}),
                               ("Какие есть футболки?", {self.a.pk, self.d.pk}),
                               ("What hoodies are available?", {self.b.pk, self.c.pk})):
            with self.subTest(text=text):
                self.assertEqual(set(self.capture(text)["eligible_product_ids"]), expected)
        for text in ("Які є моделі?", "Какие есть модели?", "What models do you have?",
                     "Які є категорії?", "Какие есть товары?", "What products do you have?",
                     "Які є дизайни Одеса?", "Покажіть Одесу", "Show Odessa"):
            with self.subTest(text=text):
                capture = self.capture(text)
                self.assertEqual(capture["requests"][0]["status"], "unresolved")
                self.assertEqual(capture["eligible_product_ids"], [])
        self.assertEqual(set(self.capture("Каталог")["eligible_product_ids"]), {self.a.pk, self.b.pk, self.c.pk, self.d.pk})

    def test_exclusion_and_or_are_honored_without_broad_themed_link(self):
        ProductMerchCollection.objects.create(product=self.b, collection=self.military)
        excluded = self.capture("Покажіть Харків, без військових принтів")
        self.assertEqual(excluded["eligible_product_ids"], [self.a.pk])
        self.assertEqual(excluded["navigation"], {})
        union = self.capture("Покажіть Харків або military")
        self.assertEqual(set(union["eligible_product_ids"]), {self.a.pk, self.b.pk, self.c.pk})
        self.assertEqual(union["navigation"], {})
        intersection = self.capture("Покажіть Харків і streetwear")
        self.assertEqual(intersection["eligible_product_ids"], [self.a.pk])

    def test_mixed_types_only_when_scope_allows_and_deduplicates(self):
        mixed = self.capture("Покажіть худі і футболки з Харковом")
        self.assertEqual(set(mixed["eligible_product_ids"]), {self.a.pk, self.b.pk})
        only_hoodie = self.capture("Покажіть худі, не футболки")
        self.assertNotIn(self.a.pk, only_hoodie["eligible_product_ids"])
        self.assertNotIn(self.d.pk, only_hoodie["eligible_product_ids"])
        self.assertEqual(len(mixed["eligible_product_ids"]), len(set(mixed["eligible_product_ids"])))

    def test_unpunctuated_contrast_keeps_the_positive_garment_and_theme(self):
        sleeve = Category.objects.create(name="Лонгсліви", slug="long-sleeve")
        other_type = self.product("Sleeve", "contrast-sleeve", sleeve, self.kharkiv)
        for text in ("Покажіть не футболки а худі", "Покажіть не футболки, а худі",
                     "Show not tshirts but hoodies"):
            with self.subTest(text=text):
                capture = self.capture(text)
                request = capture["requests"][0]
                self.assertEqual(request["garments"], ["hoodie"])
                self.assertEqual(request["excluded_garments"], ["tshirt"])
                self.assertEqual(set(capture["eligible_product_ids"]), {self.b.pk, self.c.pk})
                self.assertEqual(browse.validate_catalog_browse_response(capture, self.response([other_type.pk])), "catalog_browse_product_mismatch")
        for text in ("Покажіть не military а Харків", "Покажіть худі не military а Харків",
                     "Покажіть худі не military, а Харків"):
            with self.subTest(text=text):
                capture = self.capture(text)
                request = capture["requests"][0]
                self.assertEqual(request["include"], [self.kharkiv.pk])
                self.assertEqual(request["exclude"], [self.military.pk])
                self.assertIn(self.b.pk, capture["eligible_product_ids"])
                self.assertNotIn(self.c.pk, capture["eligible_product_ids"])

    def test_real_exclusion_lists_keep_scope_across_conjunctions_and_commas(self):
        safe = self.product("Safe", "list-safe", self.hoodie, self.street)
        for text in ("Покажіть худі без military і Харків", "Покажіть худі без military, Харків",
                     "Show hoodies without military and Kharkiv", "Show hoodies without military, Kharkiv"):
            with self.subTest(text=text):
                capture = self.capture(text)
                request = capture["requests"][0]
                self.assertEqual(request["include"], [])
                self.assertEqual(set(request["exclude"]), {self.military.pk, self.kharkiv.pk})
                self.assertEqual(capture["eligible_product_ids"], [safe.pk])
                self.assertEqual(capture["navigation"], {})

    def test_not_only_broadens_without_excluding_the_mentioned_theme(self):
        for text in ("Покажіть не тільки військові", "Покажіть не лише military", "Show not only military"):
            with self.subTest(text=text):
                capture = self.capture(text)
                self.assertEqual(set(capture["eligible_product_ids"]), {self.a.pk, self.b.pk, self.c.pk, self.d.pk})
                self.assertEqual(capture["requests"][0]["exclude"], [])
                self.assertEqual(capture["navigation"], {})
        hoodies = self.capture("Покажіть не тільки військові худі")
        self.assertEqual(set(hoodies["eligible_product_ids"]), {self.b.pk, self.c.pk})
        paired = self.capture("Покажіть не тільки military а й Харків")
        self.assertEqual(set(paired["eligible_product_ids"]), {self.a.pk, self.b.pk, self.c.pk})
        self.assertEqual(paired["requests"][0]["mode"], "any")

    def test_225_requires_actual_published_membership_127_has_no_invented_sku(self):
        capture = self.capture("Покажіть 225 бригаду")
        self.assertEqual(capture["eligible_product_ids"], [self.d.pk])
        self.assertEqual(capture["navigation"][str(self.d.pk)]["kind"], "collection")
        self.assertEqual(self.capture("Покажіть 127 бригаду")["eligible_product_ids"], [])
        self.d.status = "draft"
        self.d.save(update_fields=["status"])
        self.assertEqual(self.capture("Покажіть 225 бригаду")["eligible_product_ids"], [])

    def test_numeric_taxonomy_keeps_constraint_and_ambiguous_count_needs_clarification(self):
        ProductMerchCollection.objects.create(product=self.b, collection=self.b225)
        self.assertEqual(self.capture("Покажіть худі 225")["eligible_product_ids"], [self.b.pk])
        self.assertEqual(self.capture("Покажіть худі225")["eligible_product_ids"], [self.b.pk])
        for text in ("Покажіть 225 худі", "Покажіть225худі", "Покажіть худі 999"):
            capture = self.capture(text)
            self.assertEqual(capture["eligible_product_ids"], [])
            self.assertEqual(capture["requests"][0]["status"], "unresolved")
        self.assertEqual(set(self.capture("Покажіть 3 худі")["eligible_product_ids"]), {self.b.pk, self.c.pk})

    def test_exact_design_retention_unknown_design_and_forged_source(self):
        capture = self.capture("Покажіть тільки фото дизайну Aurora на худі")
        self.assertEqual(capture["eligible_product_ids"], [self.b.pk])
        self.assertEqual(capture["navigation"], {})
        unknown = self.capture("Покажіть тільки фото дизайну Невідомий на худі")
        self.assertEqual(unknown["eligible_product_ids"], [])
        self.assertEqual(unknown["requests"][0]["status"], "unresolved")
        revision = self.revision("Покажіть худі")
        revision.bundle_snapshot["sources"][0]["text"] = "Покажіть футболки"
        self.assertEqual(browse.capture_catalog_browse(revision)["status"], "unavailable")
        self.assertEqual(self.capture("Яка різниця між посадками?"), {})
        self.assertEqual(self.capture("L. Покажіть фото?"), {})
        unknown_theme = self.capture("Покажіть невідому категорію")
        self.assertEqual(unknown_theme["requests"][0]["status"], "unresolved")
        self.assertEqual(unknown_theme["eligible_product_ids"], [])

    def test_unknown_named_predicate_does_not_disappear_behind_a_known_garment(self):
        for text in ("Покажіть худі з принтом Одеса", "Покажіть худі з Одесою",
                     "Покажіть худі тема Одеса", "Покажіть худі з принтами Одеса",
                     "Покажіть не тільки військові худі з принтом Одеса",
                     "Show hoodies with Odessa prints"):
            with self.subTest(text=text):
                capture = self.capture(text)
                self.assertEqual(capture["requests"][0]["status"], "unresolved")
                self.assertEqual(capture["eligible_product_ids"], [])
                self.assertEqual(capture["navigation"], {})
                self.assertEqual(browse.validate_catalog_browse_response(capture, self.response([self.b.pk])), "catalog_browse_product_mismatch")
                clarifier = SimpleNamespace(control={}, reply_text="Яку тему або дизайн ви маєте на увазі?")
                self.assertTrue(browse.browse_clarification_covered(capture, clarifier))
        for text in ("Покажіть худі", "Покажіть фото худі", "Покажіть худі з принтами", "Show prints on hoodies"):
            self.assertEqual(set(self.capture(text)["eligible_product_ids"]), {self.b.pk, self.c.pk}, text)

    def test_same_title_body_category_is_not_authorized_by_correct_control(self):
        capture = self.capture("Покажіть худі Aurora")
        self.assertEqual(browse.validate_catalog_browse_response(capture, self.response([self.b.pk], "Ось футболка Aurora.")), "catalog_browse_body_mismatch")
        self.assertTrue(browse.browse_presentation_covered(capture, self.response([self.b.pk]), 10))
        self.assertFalse(browse.browse_presentation_covered(capture, self.response([self.a.pk]), 10))
        self.assertFalse(browse.browse_presentation_covered(capture, self.response([self.b.pk]), 99))

    def test_actual_preparer_rejects_wrong_published_id_even_with_valid_catalog_price(self):
        revision = self.revision("Покажіть худі з Харковом")
        capture = browse.capture_catalog_browse(revision)
        plan = SimpleNamespace(configuration={"catalog_browse": capture})
        response = self.response([self.a.pk], "Ось модель за 900 грн.")
        fixture = fixtures.CatalogChoicePresentationTests()
        with patch.object(choices, "_presentation_locale", return_value="uk"), \
            patch.object(choices, "_scope", return_value=fixture.scope()), \
            patch.object(choices, "_configured_card") as configured:
            result = choices.prepare_catalog_choice_presentations(revision, response, object(), response_plan=plan)
        self.assertEqual(result.reasons, ("catalog_browse_product_mismatch",))
        self.assertEqual(result.effects, ())
        configured.assert_not_called()

    def test_actual_preparer_passes_same_capture_and_only_verified_theme_to_v4(self):
        revision = self.revision("Покажіть худі з Харковом")
        capture = browse.capture_catalog_browse(revision)
        plan = SimpleNamespace(configuration={"catalog_browse": capture})
        fixture = fixtures.CatalogChoicePresentationTests()
        card = fixture.cards(1)[0]
        card.update(product_id=self.b.pk, title=self.b.title)
        with patch.object(choices, "_presentation_locale", return_value="uk"), \
            patch.object(choices, "_scope", return_value=fixture.scope()), \
            patch.object(choices, "_configured_card", return_value=card), \
            patch.object(browse, "current_browse_capture", return_value=capture) as current:
            result = choices.prepare_catalog_choice_presentations(revision, self.response([self.b.pk]), object(), response_plan=plan)
        self.assertTrue(result.ready, result.reasons)
        current.assert_called_once_with(browse.browse_reference(capture), self.customer, revision.pk)
        self.assertEqual(result.manifest["schema"], choices.SCHEMA_THEMED)
        self.assertEqual(result.manifest["scope"]["catalog_browse"], browse.browse_reference(capture))
        self.assertEqual(result.manifest["cards"][0]["theme_navigation"], capture["navigation"][str(self.b.pk)])

    def test_compact_catalog_keeps_identical_title_garment_and_actual_membership(self):
        from management.services.bot_catalog import _build
        text = _build(compact=True)
        tee = next(row for row in text.splitlines() if row.startswith(f"• id={self.a.pk} |"))
        hoodie = next(row for row in text.splitlines() if row.startswith(f"• id={self.b.pk} |"))
        self.assertIn("категорія: tshirts", tee)
        self.assertIn("колекції: kharkiv,streetwear", tee)
        self.assertIn("категорія: hoodie", hoodie)
        self.assertIn("колекції: kharkiv", hoodie)

    def test_each_source_keeps_its_subject_and_small_projection_has_count(self):
        capture = self.capture("Покажіть худі з Харковом", "Покажіть футболки streetwear")
        response = self.response([self.b.pk])
        self.assertTrue(browse.browse_presentation_covered(capture, response, 10))
        self.assertFalse(browse.browse_presentation_covered(capture, response, 11))
        self.assertFalse(browse.browse_presentation_covered(capture, response))
        projection = browse.browse_prompt_guidance(self.capture("Покажіть весь каталог"))
        self.assertEqual(projection["eligible_count"], 4)
        self.assertEqual(len(projection["shortlist"]), 3)
        self.assertNotIn("eligible_product_ids", str(projection))
        with patch.object(browse, "MAX_PRODUCTS", 1):
            capped = self.capture("Покажіть весь каталог")
        self.assertEqual(capped["status"], "unavailable")
        self.assertEqual(capped["reason"], "catalog_browse_catalog_bound")

    def test_supported_navigation_roundtrips_actual_public_filter_predicate(self):
        from storefront.services.catalog_facets import normalize_catalog_facet_state, filter_products_by_facets, invalidate_collection_facet_contract
        invalidate_collection_facet_contract()
        capture = self.capture("Покажіть худі з Харковом")
        nav = capture["navigation"][str(self.b.pk)]
        parsed = urlsplit(nav["url"])
        self.assertEqual(parsed.path, "/catalog/hoodie/")
        state = normalize_catalog_facet_state(parse_qs(parsed.query))
        actual = filter_products_by_facets(Product.objects.filter(status="published", category=self.hoodie), state)
        self.assertEqual(set(actual.values_list("pk", flat=True)), set(capture["eligible_product_ids"]))
        for altered in ({**nav, "url": nav["url"] + "&exclude=military"}, {**nav, "category_slug": ""}, {**nav, "kind": "unknown"}):
            with self.assertRaises(ValueError):
                browse.navigation_url(altered)

    def test_fresh_reference_rechecks_assignment_category_and_active_taxonomy(self):
        revision = self.revision("Покажіть худі з Харковом")
        capture = browse.capture_catalog_browse(revision)
        reference = browse.browse_reference(capture)
        with patch("management.models.IgCustomerTurnRevision.objects.select_related") as queried:
            queried.return_value.filter.return_value.first.return_value = revision
            self.assertEqual(browse.current_browse_capture(reference, self.customer, 777), capture)
            ProductMerchCollection.objects.filter(product=self.b, collection=self.kharkiv).delete()
            with self.assertRaisesRegex(ValueError, "context_changed"):
                browse.current_browse_capture(reference, self.customer, 777)
        for field, value in (("indexable", True), ("is_active", False)):
            revision = self.revision("Покажіть Харків")
            before = browse.capture_catalog_browse(revision)
            setattr(self.kharkiv, field, value)
            self.kharkiv.save(update_fields=[field])
            self.assertNotEqual(before["capture_digest"], browse.capture_catalog_browse(revision)["capture_digest"])


@override_settings(SITE_BASE_URL="https://twocomms.test", SECRET_KEY="browse-native-test")
class ThemedNativeBuilderTests(SimpleTestCase):
    def prepare(self):
        fixture = fixtures.CatalogChoicePresentationTests()
        scope, cards = fixture.scope(), fixture.cards()
        scope["catalog_browse"] = {"digest": "d" * 64, "locale": "uk"}
        from management.services.ig_catalog_category_discovery import category_url
        for card in cards:
            card["category"] = {"product_id": card["product_id"], "category_id": 9, "slug": "hoodie",
                "name": "Худі", "locale": "uk", "url": category_url("hoodie")}
            nav = {"kind": "theme", "category_slug": "hoodie", "collection_slug": "kharkiv", "locale": "uk", "label": "Харків", "url": ""}
            nav["url"] = browse.navigation_url(nav)
            card["theme_navigation"] = nav
        return fixture, choices.build_catalog_choice_effects(12, scope, cards, _schema=choices.SCHEMA_THEMED)

    def test_three_ctas_keep_signed_choose_and_no_chart_or_fourth_button(self):
        fixture, prepared = self.prepare()
        elements = prepared.effects[0]["payload"]["message"]["attachment"]["payload"]["elements"]
        for card in elements:
            self.assertEqual([b["type"] for b in card["buttons"]], ["postback", "web_url", "web_url"])
            self.assertEqual([b["title"] for b in card["buttons"]], ["Обрати", "Харків", "Всі худі"])
        self.assertEqual([a["kind"] for a in prepared.manifest["actions"]], ["product"] * 3)
        for effect in prepared.effects:
            self.assertTrue(choices.validate_catalog_choice_metadata(effect["projection_metadata"], payload=effect["payload"], group=effect["group"], kind=effect["kind"]))
        reference = choices.catalog_choice_reference(prepared.manifest)
        bindings = [{"selector": {"catalog_choice_reference": reference}}]
        self.assertLessEqual(len(_canonical(bindings)), 4096)
        self.assertEqual(_safe_bindings(bindings), bindings)
        from management.services.ig_revision_authority import _base_binding
        wrapped = _base_binding("catalog_asset_references", SimpleNamespace(pk=7), None,
            {"catalog_choice_reference": reference}, reference, product_ids=[101, 102, 103],
            part_count=1, asset_reference_digest="a" * 64)
        self.assertEqual(_safe_bindings([wrapped]), [wrapped])
        self.assertNotIn("https://", str(reference))
        source = fixture.source(elements[0]["buttons"][0]["payload"])
        delivered = fixture.deliver(source, fixture.receipts(prepared), fresh=False)
        self.assertTrue(delivered["ready"], delivered)
        self.assertEqual(delivered["action"]["product_id"], 101)

    def test_themed_capability_is_required_and_changing_url_breaks_seal(self):
        _fixture, prepared = self.prepare()
        effect = deepcopy(prepared.effects[0])
        effect["payload"]["message"]["attachment"]["payload"]["elements"][0]["buttons"][1]["url"] += "&fake=1"
        self.assertFalse(choices.validate_catalog_choice_metadata(effect["projection_metadata"], payload=effect["payload"], group=effect["group"], kind=effect["kind"]))
        scope = deepcopy(prepared.manifest["scope"])
        scope.pop("catalog_browse")
        with self.assertRaisesRegex(ValueError, "scope_missing"):
            choices.build_catalog_choice_effects(12, scope, prepared.manifest["cards"], _schema=choices.SCHEMA_THEMED)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False,
    SITE_BASE_URL="https://twocomms.test")
class NativeCatalogBrowseTests(TransactionTestCase):
    """Real generation validator, preparation, outbox, guards and local receipts."""
    reset_sequences = True
    _message = live.RevisionLiveTests._message
    _prepare = live.RevisionLiveTests._prepare
    _execute = live.RevisionLiveTests._execute
    _generate = contract.NativeLiveConversationContractTests._generate
    _fresh = contract.NativeLiveConversationContractTests._fresh

    def setUp(self):
        live.RevisionLiveTests.setUp(self)
        directory = TemporaryDirectory(prefix="qa23-browse-native-")
        self.addCleanup(directory.cleanup)
        media = override_settings(MEDIA_ROOT=directory.name, MEDIA_URL="/media/")
        media.enable()
        self.addCleanup(media.disable)
        self.tee = Category.objects.create(name="Футболки", slug="tshirts")
        self.hoodie = Category.objects.create(name="Худі", slug="hoodie")
        self.kharkiv = MerchCollection.objects.create(slug="kharkiv", kind="city", name_uk="Харків")
        self.military = MerchCollection.objects.create(slug="military", kind="theme", name_uk="Військові")
        self.street = MerchCollection.objects.create(slug="streetwear", kind="theme", name_uk="Streetwear")
        self.shirt = self.product("Aurora", "native-tee", self.tee, self.kharkiv)
        self.good = self.product("Aurora", "native-hoodie", self.hoodie, self.kharkiv, self.street)
        self.foreign = self.product("Other", "native-other", self.hoodie, self.military)

    def product(self, title, slug, category, *collections):
        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=")
        product = Product(title=title, slug=slug, category=category, price=900, status="published")
        product.main_image.save(slug + ".png", ContentFile(image), save=False)
        product.save()
        for collection in collections:
            ProductMerchCollection.objects.create(product=product, collection=collection)
        return product

    def answer_cards(self, ids, reply="Ось кілька моделей худі."):
        self.parsed = {"reply_text": reply, "controls": [{"kind": "show_products", "value": list(ids)}]}
        return self._execute([(200, json.dumps({"message_id": f"browse-{self.revision.pk}-{index}"})) for index in range(8)])

    def assert_no_delivery(self, result, http):
        self.assertNotEqual(result.state, "completed", result.reasons)
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.filter(state="sent").exists())

    def test_native_wrong_garment_and_wrong_theme_rejected_without_price_claim(self):
        for wrong in (self.shirt, self.foreign):
            with self.subTest(product=wrong.pk):
                self._fresh("Покажіть худі з Харковом")
                self._expected_rejection = "catalog_browse_product_mismatch"
                result, model, http = self.answer_cards([wrong.pk])
                self.assert_no_delivery(result, http)
                self.assertIn(self._expected_rejection, self._observed_rejection)

    def test_native_correct_theme_receipt_binds_three_ctas_and_signed_choice(self):
        from management.services.ig_response_plan import capture_response_plan
        from management.models import IgCommerceSelectionSession
        peers = [self.product(f"City {index}", f"native-city-{index}", self.hoodie, self.kharkiv) for index in range(2)]
        self._fresh("Покажіть худі з Харковом")
        plan = capture_response_plan(self.customer, revision=self.revision, publication=self.publication)
        nav = plan.configuration["catalog_browse"]["navigation"][str(self.good.pk)]
        result, model, http = self.answer_cards([self.good.pk, *[row.pk for row in peers]], "Ось моделі худі. " + nav["url"])
        self.assertEqual(result.state, "completed", result.reasons)
        self.revision.refresh_from_db()
        native = self.revision.delivery_effects.get(group="template", state="sent")
        buttons = native.payload["message"]["attachment"]["payload"]["elements"][0]["buttons"]
        self.assertEqual([row["title"] for row in buttons], ["Обрати", "Харків", "Всі худі"])
        self.assertEqual(buttons[1]["url"], nav["url"])
        self.assertEqual(native.projection_metadata["catalog_choice"]["schema"], choices.SCHEMA_THEMED)
        source = self._message("Обрати", "browse-choice")
        source.quick_reply_payload = buttons[0]["payload"]
        from django.utils import timezone
        source.provider_created_at = timezone.now()
        source.save(update_fields=["quick_reply_payload", "provider_created_at"])
        self.customer.refresh_from_db()
        delivered = choices.delivered_catalog_choice(self.customer, source)
        self.assertTrue(delivered["ready"], delivered)
        self.assertEqual(delivered["action"]["product_id"], self.good.pk)
        self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])
        self.assertIn(f"{self.source.pk}:info:presentation", self.revision.action_receipts["response_coverage"]["covered"])
        session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        self.assertFalse(any(row.get("product_id") for row in session.lines))
        self.assertEqual(http.call_count, self.revision.delivery_effects.filter(state="sent").count())

    def test_native_negative_exclusion_cards_correct_without_broadened_theme_link(self):
        ProductMerchCollection.objects.create(product=self.good, collection=self.military)
        safe = self.product("Safe", "native-safe", self.hoodie, self.kharkiv)
        self._fresh("Покажіть худі з Харковом, без військових принтів")
        result, _model, _http = self.answer_cards([safe.pk])
        self.assertEqual(result.state, "completed", result.reasons)
        native = self.revision.delivery_effects.get(group="template", state="sent")
        manifest = native.projection_metadata["catalog_choice"]
        self.assertEqual([row["product_id"] for row in manifest["cards"]], [safe.pk])
        self.assertNotIn("theme_navigation", manifest["cards"][0])
        self.assertEqual(manifest["schema"], choices.SCHEMA)

    def test_native_unpunctuated_contrast_delivers_the_positive_theme_and_garment(self):
        self._fresh("Покажіть не футболки а худі не military а Харків")
        result, _model, _http = self.answer_cards([self.good.pk])
        self.assertEqual(result.state, "completed", result.reasons)
        native = self.revision.delivery_effects.get(group="template", state="sent")
        self.assertEqual([row["product_id"] for row in native.projection_metadata["catalog_choice"]["cards"]], [self.good.pk])

    def test_native_unknown_print_on_known_garment_rejects_unrelated_cards(self):
        self._fresh("Покажіть худі з принтом Одеса")
        self._expected_rejection = "catalog_browse_product_mismatch"
        result, _model, http = self.answer_cards([self.good.pk])
        self.assert_no_delivery(result, http)
        self.assertIn(self._expected_rejection, self._observed_rejection)

    def test_native_unknown_print_on_known_garment_accepts_one_subject_clarifier(self):
        self._fresh("Покажіть худі з принтом Одеса")
        self.parsed = {"reply_text": "Яку тему або дизайн ви маєте на увазі?", "controls": []}
        result, _model, http = self._execute([(200, json.dumps({"message_id": "unknown-print-hoodie-clarifier"}))])
        self.assertEqual(result.state, "completed", result.reasons)
        self.revision.refresh_from_db()
        self.assertIn(f"{self.source.pk}:info:presentation", self.revision.action_receipts["response_coverage"]["covered"])
        self.assertFalse(self.revision.delivery_effects.filter(group="template").exists())
        http.assert_called_once()

    def test_native_assignment_change_after_model_validation_blocks_all_delivery(self):
        self._fresh("Покажіть худі з Харковом")
        self.after_validate = lambda: ProductMerchCollection.objects.filter(product=self.good, collection=self.kharkiv).delete()
        result, _model, http = self.answer_cards([self.good.pk])
        self.assert_no_delivery(result, http)
        self.assertIn("catalog_browse_context_changed", result.reasons)

    def test_native_assignment_change_after_outbox_plan_blocks_before_sent(self):
        from management.services import ig_revision_live as service
        self._fresh("Покажіть худі з Харковом")
        drain = service._drain_effects
        def changed(*args, **kwargs):
            self.assertTrue(self.revision.delivery_effects.filter(group="template").exists())
            ProductMerchCollection.objects.filter(product=self.good, collection=self.kharkiv).delete()
            return drain(*args, **kwargs)
        with patch.object(service, "_drain_effects", side_effect=changed):
            result, _model, http = self.answer_cards([self.good.pk])
        self.assert_no_delivery(result, http)
        self.assertTrue(self.revision.delivery_effects.exists())

    def test_native_url_without_cards_has_no_catalog_capability(self):
        from management.services.ig_response_plan import capture_response_plan
        self._fresh("Покажіть худі з Харковом")
        capture = capture_response_plan(self.customer, revision=self.revision).configuration["catalog_browse"]
        self.parsed = {"reply_text": "Ось каталог. " + capture["navigation"][str(self.good.pk)]["url"], "controls": []}
        self._expected_rejection = "unauthorized_url"
        result, _model, http = self._execute()
        self.assert_no_delivery(result, http)

    def test_native_ordinary_cards_keep_existing_three_buttons(self):
        self._fresh("Покажіть худі")
        result, _model, _http = self.answer_cards([self.good.pk, self.foreign.pk])
        self.assertEqual(result.state, "completed", result.reasons)
        native = self.revision.delivery_effects.get(group="template", state="sent")
        for row in native.payload["message"]["attachment"]["payload"]["elements"]:
            self.assertEqual([b["title"] for b in row["buttons"]], ["Обрати", "Таблиця розмірів", "Всі худі"])

    def test_native_unknown_named_theme_accepts_one_necessary_clarifier(self):
        self._fresh("Покажіть Одесу")
        self.parsed = {"reply_text": "Яку тему або категорію ви маєте на увазі?", "controls": []}
        result, _model, http = self._execute([(200, json.dumps({"message_id": "unknown-theme-clarifier"}))])
        self.assertEqual(result.state, "completed", result.reasons)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])
        self.assertIn(f"{self.source.pk}:info:presentation", self.revision.action_receipts["response_coverage"]["covered"])
        self.assertEqual(self.revision.delivery_effects.filter(state="sent").count(), 1)
        self.assertFalse(self.revision.delivery_effects.filter(group="template").exists())
        http.assert_called_once()

    def test_attribute_question_has_no_new_presentation_debt(self):
        from management.services.ig_response_plan import capture_response_plan
        for text in ("Підкажіть, які є розміри?", "Какие есть способы оплаты?", "What delivery options are there?"):
            with self.subTest(text=text):
                self._fresh(text)
                plan = capture_response_plan(self.customer, revision=self.revision, publication=self.publication)
                self.assertNotIn("catalog_browse", plan.configuration)
                self.assertFalse(any(row["kind"] == "info:presentation" for row in plan.obligations))

    def test_native_unknown_theme_cannot_guess_published_products(self):
        self._fresh("Покажіть Одесу")
        self._expected_rejection = "catalog_browse_product_mismatch"
        result, _model, http = self.answer_cards([self.good.pk], "Ось моделі з Одесою.")
        self.assert_no_delivery(result, http)

    def test_native_unknown_theme_cannot_invent_category_without_cards(self):
        self._fresh("Покажіть Одесу")
        self.parsed = {"reply_text": "Це категорія Харків.", "controls": []}
        self._expected_rejection = "catalog_browse_unresolved_claim"
        result, _model, http = self._execute()
        self.assert_no_delivery(result, http)

    def test_native_incomplete_capture_accepts_one_clarifier_without_catalog_guess(self):
        with patch.object(browse, "MAX_PRODUCTS", 1):
            self._fresh("Покажіть щось")
            self.parsed = {"reply_text": "Яка тема або категорія вас цікавить?", "controls": []}
            result, _model, http = self._execute([(200, json.dumps({"message_id": "incomplete-browse-clarifier"}))])
        self.assertEqual(result.state, "completed", result.reasons)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])
        self.assertIn(f"{self.source.pk}:info:presentation", self.revision.action_receipts["response_coverage"]["covered"])
        self.assertFalse(self.revision.delivery_effects.filter(group="template").exists())
        http.assert_called_once()

    def test_native_numeric_collection_after_garment_uses_real_membership(self):
        parent = MerchCollection.objects.create(slug="brigades", kind="theme", name_uk="Бригади")
        numeric = MerchCollection.objects.create(slug="225", kind="brigade", name_uk="225 бригада", parent=parent)
        ProductMerchCollection.objects.create(product=self.good, collection=numeric)
        self._fresh("Покажіть худі 225")
        result, _model, _http = self.answer_cards([self.good.pk])
        self.assertEqual(result.state, "completed", result.reasons)
        manifest = self.revision.delivery_effects.get(group="template", state="sent").projection_metadata["catalog_choice"]
        self.assertEqual([row["product_id"] for row in manifest["cards"]], [self.good.pk])
        self.assertEqual(manifest["cards"][0]["theme_navigation"]["collection_slug"], "225")

    def test_native_number_before_garment_asks_design_or_quantity_without_broadening(self):
        MerchCollection.objects.create(slug="225", kind="brigade", name_uk="225 бригада")
        self._fresh("Покажіть225худі")
        self.parsed = {"reply_text": "Який саме дизайн ви маєте на увазі — 225 чи кількість худі?", "controls": []}
        result, _model, http = self._execute([(200, json.dumps({"message_id": "numeric-clarifier"}))])
        self.assertEqual(result.state, "completed", result.reasons)
        self.revision.refresh_from_db()
        self.assertIn(f"{self.source.pk}:info:presentation", self.revision.action_receipts["response_coverage"]["covered"])
        self.assertFalse(self.revision.delivery_effects.filter(group="template").exists())
        http.assert_called_once()

    def test_native_known_empty_collection_is_catalog_absence_not_stock(self):
        MerchCollection.objects.create(slug="127", kind="brigade", name_uk="127 бригада")
        self._fresh("Покажіть 127 бригаду")
        self.parsed = {"reply_text": "Яку іншу тему або категорію показати?", "controls": []}
        result, _model, http = self._execute([(200, json.dumps({"message_id": "empty-published-clarifier"}))])
        self.assertEqual(result.state, "completed", result.reasons)
        self.revision.refresh_from_db()
        self.assertIn(f"{self.source.pk}:info:presentation", self.revision.action_receipts["response_coverage"]["covered"])
        self.assertFalse(self.revision.delivery_effects.filter(group="template").exists())
        http.assert_called_once()

    def test_both_projection_branches_and_line_early_return_keep_hard_browse_guard(self):
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_response_control import parse_structured_response
        self._fresh("Покажіть худі з Харковом")
        plan = capture_response_plan(self.customer, revision=self.revision)
        for candidate in (plan, replace(plan, line_plans=())):
            projection = candidate.prompt_projection()
            self.assertNotIn("catalog_browse", projection.get("configuration", {}))
            self.assertEqual(projection["catalog_browse"]["eligible_count"], 1)
            self.assertLessEqual(len(projection["catalog_browse"]["shortlist"]), 3)
            self.assertNotIn("eligible_product_ids", str(projection["catalog_browse"]))
            response = parse_structured_response({"reply_text": "Ось модель.",
                "controls": [{"kind": "show_products", "value": [self.shirt.pk]}]})
            self.assertEqual(candidate.validate(response), "catalog_browse_product_mismatch")


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False,
    SITE_BASE_URL="https://twocomms.test")
class NativeBrowseFitCompatibilityTests(TransactionTestCase):
    reset_sequences = True
    from management import tests_ig_fit_illustration as fit_fixture
    setUp = fit_fixture.FitIllustrationTests.setUp
    _message = fit_fixture.FitIllustrationTests._message
    _prepare = fit_fixture.FitIllustrationTests._prepare
    _generate = fit_fixture.FitIllustrationTests._generate
    followup = fit_fixture.FitIllustrationTests.followup
    _execute = live.RevisionLiveTests._execute

    def test_native_explicit_new_theme_browse_can_leave_retained_print_without_selecting(self):
        from pathlib import Path
        from management.services.ig_response_plan import capture_response_plan
        theme = MerchCollection.objects.create(slug="kharkiv", kind="city", name_uk="Харків")
        other = Product.objects.create(title="Інший Харків", slug="browse-retained-other",
            category=self.product.category, status="published", price=500, main_image="products/retained-other.jpg")
        target = Path(other.main_image.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"local public image fixture")
        ProductMerchCollection.objects.create(product=other, collection=theme)
        self.followup("Покажіть футболки з Харковом")
        plan = capture_response_plan(self.customer, revision=self.revision, publication=self.publication)
        self.assertEqual(plan.product_focus["product_id"], self.product.pk)
        self.assertEqual(plan.configuration["catalog_browse"]["eligible_product_ids"], [other.pk])
        self.parsed = {"reply_text": "Ось кілька моделей футболок.",
            "controls": [{"kind": "show_products", "value": [other.pk]}]}
        self.actual_inline_count = 0
        result, _model, _http = self._execute([(200, json.dumps({"message_id": f"new-theme-{index}"})) for index in range(8)])
        self.assertEqual(result.state, "completed", result.reasons)
        manifest = self.revision.delivery_effects.get(group="template", state="sent").projection_metadata["catalog_choice"]
        self.assertEqual(manifest["schema"], choices.SCHEMA_THEMED)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)

    def test_native_foreign_print_remains_labeled_fit_illustration(self):
        from pathlib import Path
        from productcolors.models import ProductColorImage, ProductColorVariant
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_response_control import parse_structured_response
        other = Product.objects.create(title="Футболка Інший принт", slug="browse-fit-other",
            category=self.product.category, status="published", price=500)
        variant = ProductColorVariant.objects.create(product=other, color=self.variant.color, stock=0)
        image = ProductColorImage.objects.create(variant=variant, image="product_colors/browse-example.jpg",
            alt_text="Класична футболка з іншим принтом")
        Path(image.image.path).write_bytes(b"local fit example fixture")
        self.followup("Покажіть приклади classic та oversize крою")
        plan = capture_response_plan(self.customer, revision=self.revision, publication=self.publication)
        self.assertNotIn("catalog_browse", plan.configuration)
        reply = "Це інший принт, лише як приклад classic крою; ваш принт залишається тим самим. Оверсайз вільніший."
        self.parsed = {"reply_text": reply, "controls": [{"kind": "show_products", "value": [other.pk]}]}
        self.assertEqual(plan.validate(parse_structured_response(self.parsed)), "")
        self.actual_inline_count = 0
        result, _model, _http = self._execute([(200, json.dumps({"message_id": f"fit-browse-{index}"})) for index in range(8)])
        self.assertEqual(result.state, "completed", result.reasons)
        native = self.revision.delivery_effects.get(group="template", state="sent")
        manifest = native.projection_metadata["fit_illustration"]
        self.assertTrue(any(row["product_id"] == other.pk and row["role"] == "example" for row in manifest["items"]))
        self.assertNotIn("catalog_choice", native.projection_metadata)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)
