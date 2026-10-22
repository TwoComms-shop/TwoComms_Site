"""Source-bound spoken size aliases, without provider or production I/O."""

from django.test import SimpleTestCase

from management.services.ig_commerce_turns import parse_turn, understand_turn


class SpokenDoubleXLSourceTests(SimpleTestCase):
    def test_spoken_double_xl_variants_are_one_canonical_size(self):
        for spelling in (
            "два XL", "дві XL", "две XL", "дваXL", "двіXL", "двеXL",
            "два іксель", "дві іксель", "две иксэль", "дваіксель", "двііксель",
            "двеиксэль", "два ікс ель", "дві\tіксель", "ДВА ХЛ", "двіХL",
        ):
            with self.subTest(spelling=spelling):
                for text in (spelling, f"Хочу футболку розмір {spelling}"):
                    self.assertEqual(parse_turn(text).field_updates.get("size"), "XXL")
                    self.assertEqual(understand_turn(text, model_payload={"size": "XL"}).field_updates.get("size"), "XXL")

    def test_numeric_and_mixed_alphabet_sizes_keep_canonical_meaning(self):
        for spelling, expected in (
            ("2XL", "XXL"), ("2ХL", "XXL"), ("2XЛ", "XXL"),
            ("ХXL", "XXL"), ("XХL", "XXL"), ("3ХЛ", "XXXL"),
            ("XL", "XL"), ("іксель", "XL"), ("иксэль", "XL"),
        ):
            with self.subTest(spelling=spelling):
                self.assertEqual(parse_turn(spelling).field_updates.get("size"), expected)

    def test_unaffirmed_spoken_aliases_cannot_be_promoted_by_model(self):
        for text in (
            "два XL?", "Є двііксель", "який дваXL підійде", "Два XL чи XL",
            "двііксель/XL", "«два XL»", 'Він написав "двііксель".',
            "В описі два XL", "Раніше два XL", "два XL size guide",
            "Не хочу два XL", "не двііксель", "дваXL не хочу",
            "I don't want size дваXL", "I haven’t requested size двііксель",
        ):
            with self.subTest(text=text):
                self.assertNotIn("size", parse_turn(text).field_updates)
                self.assertNotIn("size", understand_turn(text, model_payload={"size": "XXL"}).field_updates)

    def test_whole_alias_negation_and_corrections_withdraw_canonical_size(self):
        for text in ("Не хочу два XL", "не двііксель", "I don't want size дваXL"):
            with self.subTest(text=text):
                self.assertEqual(parse_turn(text).preference_withdrawals.get("size"), "XXL")
        request = parse_turn("Не хочу два XL, а XL")
        self.assertEqual(request.field_updates.get("size"), "XL")
        self.assertEqual(request.preference_withdrawals.get("size"), "XXL")
        self.assertEqual(parse_turn("XL. Ні, краще двііксель").field_updates.get("size"), "XXL")
        self.assertNotIn("size", parse_turn("I haven’t requested size двііксель").preference_withdrawals)

    def test_size_alias_does_not_create_quantity_and_explicit_quantity_is_independent(self):
        for spelling in ("2XL", "2ХL", "два XL", "двііксель"):
            with self.subTest(spelling=spelling):
                request = parse_turn(f"Додай футболку {spelling}")
                operation, = request.line_operations
                self.assertEqual(operation.field_updates.get("size"), "XXL")
                self.assertNotIn("quantity", operation.field_updates)
                request = parse_turn(f"Додай 2 футболки {spelling}")
                operation, = request.line_operations
                self.assertEqual(operation.garment_type, "tshirt")
                self.assertEqual(operation.field_updates, {"size": "XXL", "quantity": 2})

    def test_spoken_alias_keeps_line_configuration_scoped(self):
        request = parse_turn("Додай футболку двііксель; додай худі XL")
        self.assertNotIn("size", request.field_updates)
        shirt, hoodie = request.line_operations
        self.assertEqual(shirt.field_updates.get("size"), "XXL")
        self.assertEqual(hoodie.field_updates.get("size"), "XL")
