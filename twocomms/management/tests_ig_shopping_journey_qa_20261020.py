"""Customer shopping journeys through real native receipts and source reduction.

External transport is replaced by the existing native-delivery fixture. Product
choice witnesses, cart transitions, configuration reads and projections are real.
"""
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management import tests_ig_catalog_native_delivery as native
from management.models import IgCommerceSelectionSession, IgCommerceTurnDecision
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_commerce_source_identity import resolve_source_product_request
from management.services.ig_commerce_state import apply_turn
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_response_plan import capture_response_plan
from management.services.ig_source_cart_catalog import resolve_source_cart_catalog


class ExplicitSizeCorrectionQA20261020Tests(SimpleTestCase):
    def test_alternatives_questions_quotes_and_reported_corrections_stay_unproven(self):
        for text in ("M or L", "Розмір M або L", "L/M", "L, ні, краще M?",
            '"Розмір L. Ні, краще M"', "L. Ні, краще M або S",
            "L. Ні, мабуть краще M", "L. Ні, краще M, але ще подумаю",
            "L. Нет, лучше M, но не уверен", "L. No, actually M, but not sure"):
            with self.subTest(text=text):
                self.assertNotIn("size", parse_turn(text).field_updates)
        for text in ("Розмір L. Ні, не хочу M", "Size L. They said no, actually M",
            "L. Не краще M?", "L. Ні, краще не M"):
            with self.subTest(text=text):
                self.assertNotEqual(parse_turn(text).field_updates.get("size"), "M")

    def test_affirmed_customer_self_correction_selects_only_the_final_size(self):
        for text in ("Розмір L. Ні, краще M", "Размер L. Нет, лучше M",
            "Size L. No, actually M", "L, ні, краще M", "L. Насправді розмір M",
            "L. Actually size M", "Розмір ель. Ні, краще ем", "Размер эль. Нет, лучше эм",
            "L. Ні, краще M. Ні, краще S"):
            with self.subTest(text=text):
                expected = "S" if text.endswith("краще S") else "M"
                self.assertEqual(parse_turn(text).field_updates.get("size"), expected)


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class ShoppingJourneyQA20261020Tests(TransactionTestCase):
    reset_sequences = True
    response = native.CatalogNativeDeliveryTests.response
    source_row = native.CatalogNativeDeliveryTests.source_row
    seal = native.CatalogNativeDeliveryTests.seal
    prepare = native.CatalogNativeDeliveryTests.prepare
    plan = native.CatalogNativeDeliveryTests.plan
    deliver = native.CatalogNativeDeliveryTests.deliver
    action_source = native.CatalogNativeDeliveryTests.action_source
    consume = native.CatalogNativeDeliveryTests.consume

    def setUp(self):
        from storefront.models import Category
        native.CatalogNativeDeliveryTests.setUp(self)
        self.category.name, self.category.slug = "Худі", "hoodie"
        self.category.save(update_fields=["name", "slug"])
        shirts = Category.objects.create(name="Футболки", slug="tshirts")
        for index, title in enumerate(("Худі «Шторм»", "Худі «Хвиля»", "Футболка «Світанок»")):
            self.products[index].title = title
            if index == 2:
                self.products[index].category = shirts
            self.products[index].save(update_fields=["title", "category"])

    def reduce_text(self, text):
        self.client_row.refresh_from_db()
        source = self.source_row(text=text)
        request = resolve_source_product_request(self.client_row, source, parse_turn(text))
        decision = apply_turn(self.client_row, source, request, reply_payload={})
        self.client_row.refresh_from_db()
        self.assertTrue(decision.accepted or decision.result_payload.get("reason") == "no_state_change",
            {"text": text, "result": decision.result_payload})
        return source, decision

    def shortlist(self):
        source = self.source_row(text="Які худі є? Покажіть кілька моделей")
        self.seal(source)
        prepared, response = self.prepare(self.products[:2])
        self.plan(prepared, response)
        sent, outcomes = self.deliver()
        self.assertEqual(outcomes["template"].state, "sent")
        self.assertEqual(len(sent[0]["message"]["attachment"]["payload"]["elements"]), 2)
        self.assertIsNone(self.client_row.current_product_id)
        return prepared

    def active_fields(self):
        cart = capture_current_selection_lines(self.client_row.pk)
        self.assertEqual(cart["status"], "captured", cart)
        self.assertTrue(cart["coverage_complete"], cart)
        line = next(row for row in cart["lines"] if row["line_id"] == cart["active_line_id"])
        return cart, line["fields"]

    def test_hoodie_shortlist_pick_fit_size_color_then_checkout_keeps_one_exact_item(self):
        prepared = self.shortlist()
        chosen = self.action_source(prepared, product_id=self.products[1].pk)
        self.consume(chosen)
        episode_id = self.client_row.current_commercial_episode_id
        self.reduce_text("Класичний крій")
        size_source, _ = self.reduce_text("Розмір L")
        color_source, _ = self.reduce_text("рожевий")
        purchase_source, purchase = self.reduce_text("Хочу замовити")
        cart, fields = self.active_fields()
        self.assertEqual(len(cart["lines"]), 1)
        self.assertEqual(fields["product_id"]["value"], self.products[1].pk)
        self.assertEqual(fields["product_id"]["source"]["source_message_id"], chosen.pk)
        self.assertEqual(fields["size"]["source"]["source_message_id"], size_source.pk)
        self.assertEqual(fields["color"]["source"]["source_message_id"], color_source.pk)
        self.assertTrue(purchase.request_payload["purchase_requested"])
        self.assertEqual(self.client_row.current_commercial_episode_id, episode_id)
        catalog = resolve_source_cart_catalog(cart)
        self.assertTrue(catalog.complete, catalog.gaps)
        spec = catalog.item_specs_by_line[cart["active_line_id"]]
        self.assertEqual((spec["product_id"], spec["color_variant_id"], spec["size"]),
            (self.products[1].pk, self.variants[1].pk, "L"))
        self.assertEqual(IgCommerceTurnDecision.objects.filter(source_message=purchase_source).count(), 1)

    def test_exact_tshirt_then_fit_and_audience_questions_do_not_choose_another_product(self):
        source, _ = self.reduce_text(f"Хочу https://twocomms.shop/product/{self.products[2].slug}/")
        episode_id = self.client_row.current_commercial_episode_id
        for question in ("А які є фасони?", "Це жіноча чи чоловіча?", "А які кольори?"):
            with self.subTest(question=question):
                self.reduce_text(question)
                cart, fields = self.active_fields()
                self.assertEqual(fields["product_id"]["value"], self.products[2].pk)
                self.assertEqual(fields["product_id"]["source"]["source_message_id"], source.pk)
                self.assertNotIn("fit_option_code", fields)
                self.assertEqual(self.client_row.current_commercial_episode_id, episode_id)
                self.assertEqual(len(cart["lines"]), 1)

    def test_ambiguous_this_one_after_two_cards_does_not_select_first_or_last_card(self):
        self.shortlist()
        self.reduce_text("Хочу цю, розмір L")
        _cart, fields = self.active_fields()
        self.assertNotIn("product_id", fields)
        self.assertIsNone(self.client_row.current_product_id)
        self.assertEqual(fields["size"]["value"], "L")
        session = IgCommerceSelectionSession.objects.get(client=self.client_row, open_slot=1)
        self.assertEqual(session.pending_clarification, "which_product")

    def test_fragmented_customer_corrections_replace_size_but_retain_exact_print(self):
        source, _ = self.reduce_text(f"Хочу https://twocomms.shop/product/{self.products[0].slug}/")
        self.reduce_text("Розмір L")
        correction, _ = self.reduce_text("Ні, краще M")
        self.reduce_text("чорний")
        cart, fields = self.active_fields()
        self.assertEqual(fields["product_id"]["value"], self.products[0].pk)
        self.assertEqual(fields["product_id"]["source"]["source_message_id"], source.pk)
        self.assertEqual(fields["size"]["value"], "M")
        self.assertEqual(fields["size"]["source"]["source_message_id"], correction.pk)
        self.assertEqual(len(cart["lines"]), 1)
        self.assertEqual(self.client_row.current_size, "M")

    def test_same_message_correction_uses_final_size_without_duplicating_line(self):
        self.reduce_text(f"Хочу https://twocomms.shop/product/{self.products[0].slug}/")
        correction, _ = self.reduce_text("Розмір L. Ні, краще M")
        cart, fields = self.active_fields()
        self.assertEqual(fields["size"]["value"], "M")
        self.assertEqual(fields["size"]["source"]["source_message_id"], correction.pk)
        self.assertEqual(len(cart["lines"]), 1)

    def test_zvychayno_is_consent_word_and_not_an_invented_regular_fit(self):
        self.reduce_text(f"Хочу https://twocomms.shop/product/{self.products[0].slug}/")
        source, _ = self.reduce_text("звичайно")
        self.seal(source)
        cart, fields = self.active_fields()
        self.assertNotIn("fit_option_code", fields)
        plan = capture_response_plan(self.client_row, revision=self.revision, source_cart_capture=cart)
        self.assertNotIn("fit_option_code", plan.choices)
        self.assertFalse(plan.configuration.get("focus_selection_candidate"))
