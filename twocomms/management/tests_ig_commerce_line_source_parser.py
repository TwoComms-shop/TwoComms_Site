"""Actual source parsing and canonical cart reductions, without provider I/O."""
from datetime import timedelta
from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.models import IgCommerceTurnDecision
from management.services.ig_commerce_turns import parse_turn, understand_turn
from management.services.ig_revision_commerce import reduce_inbound_commerce_source
from management.tests_ig_commerce_state import CommerceStateFixture


class CommerceLineSourceParserTests(SimpleTestCase):
    def operation(self, text):
        request = parse_turn(text)
        self.assertFalse(request.pending_line_clarification)
        self.assertEqual(len(request.line_operations), 1)
        self.assertEqual(dict(request.field_updates), {})
        self.assertFalse(request.reset_requested)
        return request.line_operations[0]

    def test_add_hoodie_is_not_replacement_or_physical_order_authority(self):
        request = parse_turn("добавьте ещё худи")
        operation = self.operation("добавьте ещё худи")
        self.assertEqual(operation.operation, "add")
        self.assertEqual(operation.garment_type, "hoodie")
        self.assertEqual(operation.target_garment_type, "")
        self.assertFalse(request.new_order_requested)

    def test_negated_old_color_correction_binds_positive_tshirt_clause(self):
        operation = self.operation("не чёрную, а розовую футболку")
        self.assertEqual(operation.operation, "update")
        self.assertEqual(operation.target_garment_type, "tshirt")
        self.assertEqual(dict(operation.field_updates), {"color": "pink"})

    def test_replace_garment_uses_old_selector_and_new_configuration(self):
        for text in ("вместо футболки худи", "замените футболку на худи", "не футболку, а худи"):
            with self.subTest(text=text):
                operation = self.operation(text)
                self.assertEqual(operation.operation, "replace")
                self.assertEqual(operation.target_garment_type, "tshirt")
                self.assertEqual(operation.garment_type, "hoodie")
        operation = self.operation("replace black t-shirt with pink hoodie size XL")
        self.assertEqual(dict(operation.field_updates), {"color": "pink", "size": "XL"})

    def test_another_same_is_source_copy_intent_not_explicit_new_order(self):
        request = parse_turn("ещё одну такую же")
        operation = self.operation("ещё одну такую же")
        self.assertEqual(operation.operation, "add")
        self.assertTrue(operation.copy_previous)
        self.assertFalse(request.new_order_requested)
        self.assertTrue(parse_turn("Хочу новый заказ: добавьте худи").new_order_requested)

    def test_gift_recipient_is_per_operation_and_unknown_gift_target_abstains(self):
        operation = self.operation("добавьте худи для друга")
        self.assertEqual(operation.recipient_id, "friend")
        request = parse_turn("другой подарок")
        self.assertEqual(request.pending_line_clarification, "ambiguous_line_target")
        self.assertFalse(request.reset_requested)

    def test_remove_ordinal_and_quantity_are_distinct_from_size(self):
        operation = self.operation("уберите вторую футболку")
        self.assertEqual(operation.operation, "remove")
        self.assertEqual(operation.target_line_index, 1)
        self.assertEqual(dict(operation.field_updates), {})
        operation = self.operation("добавьте 2 футболки размер XL")
        self.assertIsNone(operation.target_line_index)
        self.assertEqual(dict(operation.field_updates), {"quantity": 2, "size": "XL"})
        operation = self.operation("измените количество худи на 2")
        self.assertEqual(dict(operation.field_updates), {"quantity": 2})
        operation = self.operation("change quantity of line 2 to 3")
        self.assertEqual(operation.target_line_index, 1)
        self.assertEqual(dict(operation.field_updates), {"quantity": 3})

    def test_explicit_line_id_select_and_zero_quantity_remove(self):
        operation = self.operation("select line:25:2")
        self.assertEqual(operation.operation, "select")
        self.assertEqual(operation.target_line_id, "line:25:2")
        operation = self.operation("количество футболки 0")
        self.assertEqual(operation.operation, "remove")
        self.assertEqual(operation.target_garment_type, "tshirt")

    def test_zero_in_add_or_replacement_cannot_authorize_implicit_removal(self):
        for text in ("add 0 t-shirts", "add Extra Sketch 0 футболку", "replace tshirt with 0 hoodies"):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual(request.pending_line_clarification, "ambiguous_line_quantity")
                self.assertEqual(request.line_operations, ())
                self.assertFalse(request.reset_requested)
        self.assertEqual(self.operation("количество футболки 0").operation, "remove")
        self.assertEqual(self.operation("уберите футболку").operation, "remove")

    def test_quantity_cap_and_positive_quantity_stay_explicit_source_requirements(self):
        operation = self.operation("добавьте 2 футболки")
        self.assertEqual(dict(operation.field_updates), {"quantity": 2})
        request = parse_turn("добавьте 51 футболку")
        self.assertEqual(request.pending_line_clarification, "ambiguous_line_quantity")
        self.assertEqual(request.line_operations, ())

    def test_ambiguous_quantity_or_combined_unmapped_items_abstains(self):
        for text, reason in (("добавьте худи и футболку", "ambiguous_line_target"),
            ("количество худи 2 или 3 штуки", "ambiguous_line_quantity"),
            ("уберите первую и вторую футболку", "ambiguous_line_target")):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual(request.pending_line_clarification, reason)
                self.assertEqual(request.line_operations, ())
                self.assertEqual(dict(request.field_updates), {})

    def test_quote_history_ad_caption_and_negative_commands_have_no_line_authority(self):
        for text in ("«хочу другую футболку»", "В описании написано добавьте худи",
            "Caption: replace black t-shirt with pink hoodie", "раньше хотел ещё одну худи",
            "не хочу другую футболку", "I don't want another one", "I don’t want another one",
            "I don't need another one", "I haven't requested another one", "Not a new order",
            "don't replace the hoodie with pink t-shirt size XL", "не измените количество худи на 2",
            "не добавьте розовую футболку", "Не хочу новый заказ", "add hoodie?"):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual(request.line_operations, ())
                self.assertFalse(request.reset_requested)
                self.assertFalse(request.new_purchase_requested)
                self.assertFalse(request.new_order_requested)
                self.assertEqual(dict(request.field_updates), {})

    def test_historical_color_does_not_hide_current_positive_correction(self):
        request = parse_turn("Раньше хотел чёрную футболку, теперь розовую")
        self.assertEqual(dict(request.field_updates), {"color": "pink"})
        self.assertNotIn("color", parse_turn("Раньше хотел чёрную футболку").field_updates)

    def test_color_roots_are_words_not_prose_substrings(self):
        for text in ("магазин сертификат", "синтетика", "сердце", "blackberry"):
            with self.subTest(text=text):
                self.assertNotIn("color", parse_turn(text).field_updates)
        self.assertEqual(dict(parse_turn("чёрную футболку").field_updates), {"color": "black"})

    def test_model_hints_cannot_fill_missing_mapping_or_override_typed_correction(self):
        for text in ("другой подарок", "не чёрную, а розовую футболку"):
            with self.subTest(text=text):
                self.assertEqual(understand_turn(text, model_payload={"color": "black", "size": "M"}), parse_turn(text))

    def test_quoted_url_has_no_catalog_identity_or_line_operation(self):
        request = parse_turn('В рекламе написано «add https://twocomms.shop/product/black-shirt/»')
        self.assertIsNone(request.exact_product_id)
        self.assertEqual(request.line_operations, ())


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class CommerceParsedSourceReductionTests(CommerceStateFixture, TestCase):
    def source(self, text, *, at=None):
        self._counter = getattr(self, "_counter", 0) + 1
        source = self.message(f"source-{self._counter}", at=at)
        source.text = text
        source.source = "webhook"
        source.provider_namespace = "commerce-source-test"
        source.save(update_fields=["text", "source", "provider_namespace"])
        return source

    def reduce(self, text, *, at=None):
        source = self.source(text, at=at)
        with transaction.atomic():
            result = reduce_inbound_commerce_source(self.client, source,
                expected_provider_namespace=source.provider_namespace)
        self.assertTrue(result.ready, result.reason)
        decision = IgCommerceTurnDecision.objects.get(source_message=source)
        self.client.refresh_from_db()
        return decision

    def initial_tshirt(self):
        return self.reduce(f"https://twocomms.shop/product/{self.classic.slug}/ чёрную футболку размер L")

    def test_actual_ingress_add_preserves_original_line_and_episode(self):
        first = self.initial_tshirt()
        before = dict(first.session.lines[0])
        episode_id = self.client.current_commercial_episode_id
        added = self.reduce("добавьте ещё худи для друга")
        first.session.refresh_from_db()
        self.assertEqual(self.client.current_commercial_episode_id, episode_id)
        self.assertEqual(first.session.lines[0], before)
        self.assertEqual(len(first.session.lines), 2)
        self.assertEqual(first.session.lines[1]["garment_type"], "hoodie")
        self.assertEqual(first.session.lines[1]["recipient_id"], "friend")
        self.assertNotIn("product_id", first.session.lines[1])
        self.assertNotEqual(first.session.lines[1]["line_id"], before["line_id"])
        self.assertEqual(added.session_id, first.session_id)

    def test_actual_ingress_correction_targets_tshirt_after_other_active_line(self):
        first = self.initial_tshirt()
        self.reduce("добавьте ещё худи")
        corrected = self.reduce("не чёрную, а розовую футболку")
        corrected.session.refresh_from_db()
        lines = corrected.session.lines
        self.assertEqual(lines[0]["line_id"], first.session.lines[0]["line_id"])
        self.assertEqual(lines[0]["product_id"], self.classic.pk)
        self.assertEqual(lines[0]["color"], "pink")
        self.assertNotIn("color", lines[1])

    def test_actual_ingress_ambiguous_two_tshirts_changes_neither(self):
        self.initial_tshirt()
        self.reduce("добавьте ещё одну футболку размер M")
        previous = self.client.commerce_selection_sessions.get(open_slot=1).snapshot()
        rejected = self.reduce("не чёрную, а розовую футболку")
        rejected.session.refresh_from_db()
        self.assertEqual(rejected.session.lines, previous["lines"])
        self.assertIn("ambiguous_line_target", rejected.result_payload.get("reasons", []) + rejected.transition.reasons)

    def test_actual_ingress_late_replacement_does_not_touch_newer_lines(self):
        self.initial_tshirt()
        current_at = timezone.now()
        latest = self.reduce("добавьте ещё худи", at=current_at)
        previous = latest.session.snapshot()
        late = self.reduce("вместо футболки худи", at=current_at - timedelta(minutes=1))
        latest.session.refresh_from_db()
        self.assertTrue(late.is_stale)
        self.assertFalse(late.accepted)
        self.assertEqual(latest.session.lines, previous["lines"])
        self.assertEqual(latest.session.revision, previous["revision"])

    def test_actual_ingress_unmapped_two_links_preserves_current_cart(self):
        first = self.initial_tshirt()
        previous = first.session.snapshot()
        ambiguous = self.reduce(f"добавьте худи и футболку https://twocomms.shop/product/{self.classic.slug}/ https://twocomms.shop/product/{self.reality.slug}/")
        first.session.refresh_from_db()
        self.assertEqual(first.session.lines, previous["lines"])
        self.assertFalse(ambiguous.accepted)
