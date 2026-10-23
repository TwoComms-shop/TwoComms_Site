"""Ordinary multilingual source facts must survive canonical cart reduction."""
from types import SimpleNamespace

from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings

from management.models import IgCommerceTurnDecision
from management.services.ig_commerce_source_identity import resolve_line_operation_identities
from management.services.ig_commerce_state import _apply_line_operations
from management.services.ig_commerce_turns import parse_turn, understand_turn
from management.services.ig_commerce_types import CatalogGraph, CatalogProduct
from management.services.ig_revision_commerce import reduce_inbound_commerce_source
from management.tests_ig_commerce_state import CommerceStateFixture


class OrdinarySourceParsingTests(SimpleTestCase):
    graph = CatalogGraph(products=(), digest="qa23-empty", canonical_json="{}")

    def canonical(self, text, graph=None):
        return resolve_line_operation_identities(text, parse_turn(text), catalog_graph=graph or self.graph)

    def test_explicit_number_word_adds_reach_the_actual_reducer(self):
        for text, quantity in (("добавьте две футболки XL", 2), ("додайте дві футболки XL", 2),
                ("add two t-shirts XL", 2), ("добавьте три худи", 3),
                ("додайте чотири худі", 4), ("add five hoodies", 5), ("додайте п’ять худі", 5),
                ("add six pieces", 6), ("додайте сім штук", 7), ("добавьте восемь худи", 8),
                ("додайте дев’ять худі", 9), ("add ten t-shirts", 10)):
            with self.subTest(text=text):
                request = self.canonical(text)
                self.assertEqual(request.pending_line_clarification, "")
                after, facts, reason = _apply_line_operations({"lines": [], "active_index": 0}, request,
                    SimpleNamespace(pk=101, reply_to_provider_message_id=""))
                self.assertEqual(reason, "")
                self.assertEqual(after["lines"][0]["quantity"], quantity)
                self.assertEqual(facts[0]["values"]["quantity"], quantity)
                if text.endswith("XL"):
                    self.assertEqual(after["lines"][0]["size"], "XL")

    def test_reviewed_number_word_title_is_identity_not_quantity(self):
        product = CatalogProduct(product_id=11, title="Two", slug="two", category_id=1,
            category_slug="tshirts", category_label="T-shirts", garment_type="tshirt")
        request = self.canonical("add Two t-shirt", CatalogGraph(products=(product,), digest="two", canonical_json="{}"))
        self.assertEqual(request.line_operations[0].exact_product_id, 11)
        self.assertNotIn("quantity", request.line_operations[0].field_updates)

    def test_double_xl_aliases_keep_size_authority_without_quantity(self):
        for text in ("два XL", "дві іксель", "две иксэль", "дваXL", "2 XL"):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual(request.field_updates.get("size"), "XXL")
                self.assertNotIn("quantity", request.field_updates)
        request = self.canonical("добавьте две XL футболки")
        self.assertEqual(request.line_operations[0].field_updates.get("size"), "XXL")
        self.assertNotIn("quantity", request.line_operations[0].field_updates)

    def test_number_word_alternatives_and_negative_quantity_never_default_to_one(self):
        for text in ("add one or two t-shirts", "добавьте две или три футболки",
                "додайте дві або три футболки", "add not two t-shirts"):
            with self.subTest(text=text):
                request = self.canonical(text)
                self.assertEqual(request.pending_line_clarification, "ambiguous_line_quantity")
                self.assertEqual(request.line_operations, ())

    def test_history_quotes_questions_and_negated_adds_supply_no_new_quantity(self):
        for text in ("«добавьте две футболки»", "Раньше добавьте две футболки",
                "не добавьте две футболки", "don't add two t-shirts", "add two t-shirts?",
                "В описании написано добавьте две футболки"):
            with self.subTest(text=text):
                self.assertEqual(self.canonical(text).line_operations, ())

    def test_sibling_copy_clears_recipient_specific_size_and_fit(self):
        before = {"lines": [{"line_id": "line:1", "product_id": 11, "quantity": 1,
            "recipient_id": "self", "size": "M", "fit_option_code": "classic", "color": "black"}], "active_index": 0}
        copied = {"line_count": 1, "recipient_id": "self",
            "values": {"product_id": 11, "size": "M", "fit_option_code": "classic", "color": "black"},
            "evidence": {key: {"source_message_id": 1} for key in ("product_id", "size", "fit_option_code", "color")}}
        for text, recipient in (("ещё одну такую же для сестры", "sister"),
                ("ще одну таку саму для сестри", "sister"), ("another one for my sister", "sister"),
                ("ещё одну такую же в подарок сестре", "sister"),
                ("ще одну таку саму на подарунок сестрі", "sister"),
                ("ещё одну такую же в подарок для моей сестры", "sister"),
                ("ще одну таку саму на подарунок для моєї сестри", "sister"),
                ("another one for my brother", "brother"), ("ещё одну такую же для брата", "brother")):
            with self.subTest(text=text):
                after, facts, reason = _apply_line_operations(before, self.canonical(text),
                    SimpleNamespace(pk=102, reply_to_provider_message_id=""), copy_projection=copied)
                self.assertEqual(reason, "")
                self.assertEqual(after["lines"][0], before["lines"][0])
                line = after["lines"][1]
                self.assertEqual(line["recipient_id"], recipient)
                self.assertEqual(line["product_id"], 11)
                self.assertEqual(line["color"], "black")
                self.assertNotIn("size", line)
                self.assertNotIn("fit_option_code", line)
                self.assertNotIn("size", facts[0]["copy_evidence"])

    def test_uncertain_or_multiple_gift_recipients_cannot_copy_self_scope(self):
        for text in ("another one for my sister or brother", "ещё одну такую же для сестры и брата",
                "добавьте худи в подарок коллеге", "додайте худі на подарунок колезі",
                "ещё одну такую же для коллеги", "another one for my colleague",
                "ще одну таку саму для колеги"):
            with self.subTest(text=text):
                request = self.canonical(text)
                self.assertEqual(request.pending_line_clarification, "ambiguous_recipient")
                self.assertEqual(request.line_operations, ())

    def test_purpose_preposition_does_not_manufacture_unknown_beneficiary(self):
        for text in ("another one for summer", "ещё одну такую же для подарка"):
            with self.subTest(text=text):
                request = self.canonical(text)
                self.assertEqual(request.pending_line_clarification, "")
                self.assertEqual(request.line_operations[0].recipient_id, "")

    def test_quoted_or_historical_gift_recipient_has_no_current_scope(self):
        for text in ("«another one for my sister»", "Раньше хотел худи в подарок сестре",
                "не хочу ещё одну такую же для сестры"):
            with self.subTest(text=text):
                request = self.canonical(text)
                self.assertEqual(request.recipient_id, "")
                self.assertEqual(request.line_operations, ())

    def test_comma_free_colour_corrections_retain_positive_choice(self):
        for text in ("не черное а белое", "не чорне а біле", "not black but white",
                "не чорне але біле", "не черное, а белое"):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual(request.field_updates.get("color"), "white")
                self.assertEqual(request.preference_withdrawals.get("color"), "black")
                self.assertEqual(understand_turn(text, model_payload={"color": "black"}).field_updates.get("color"), "white")

    def test_colour_correction_with_garment_keeps_typed_line_target(self):
        for text in ("не черную а белую футболку", "не чорну а білу футболку",
                "not black but white t-shirt"):
            with self.subTest(text=text):
                request = self.canonical(text)
                self.assertEqual(request.field_updates, {})
                operation = request.line_operations[0]
                self.assertEqual(operation.operation, "update")
                self.assertEqual(operation.target_garment_type, "tshirt")
                self.assertEqual(operation.field_updates.get("color"), "white")

    def test_colour_correction_does_not_grant_authority_to_unaffirmed_white(self):
        for text in ("«не черное а белое»", "Раньше хотел не черное а белое",
                "не хочу не черное а белое", "don't change not black but white",
                "не черное а белое?", "не черное а белое или розовое", "not black but maybe white"):
            with self.subTest(text=text):
                request = self.canonical(text)
                self.assertNotEqual(request.field_updates.get("color"), "white")
                self.assertFalse(any(operation.field_updates.get("color") == "white" for operation in request.line_operations))

    def test_sealed_cart_fallback_localizes_both_sibling_scopes(self):
        import hashlib
        from management.tests_ig_cart_fallback import CartSourceFallbackTests, plan_fixtures
        from management.services.ig_turn_intelligence import capture_digest

        helper = CartSourceFallbackTests()
        for language, expected in (("uk", ("для сестри", "для брата")),
                ("ru", ("для сестры", "для брата")), ("en", ("for your sister", "for your brother"))):
            with self.subTest(language=language):
                cart, client, revision, _plan = helper.fixture(language=language)
                source = revision.bundle_snapshot["sources"][0]
                source["text"] += {"uk": ". Відповідайте українською.", "ru": ". Пожалуйста отвечайте на русском.",
                    "en": ". Please reply in English."}[language]
                body_digest = hashlib.sha256(source["text"].encode()).hexdigest()
                envelope = dict(source)
                envelope.pop("source_digest", None)
                source["source_digest"] = capture_digest(envelope)
                revision.snapshot_digest = capture_digest(revision.bundle_snapshot)
                receipt = revision.action_receipts["commerce_reduction"]
                receipt["snapshot_digest"] = revision.snapshot_digest
                receipt["decisions"][0]["source_digest"] = source["source_digest"]
                for row, recipient in zip(cart["lines"], ("sister", "brother")):
                    row["recipient_id"] = recipient
                    row["source_selection"]["scope"]["recipient_id"] = recipient
                    for fact in row["fields"].values():
                        fact["source"]["source_digest"] = body_digest
                helper.rehash(cart)
                plan = plan_fixtures.ResponseCartPlanTests().plan(cart,
                    sources=revision.bundle_snapshot["sources"], missing={"second": ["fit"]})
                response, proof = helper.build(client, revision, plan)
                self.assertIsNotNone(response, proof)
                for label in expected:
                    self.assertIn(label, response.reply_text)
                self.assertEqual(response.control, {})


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class OrdinaryInboundReductionTests(CommerceStateFixture, TestCase):
    def reduce(self, text):
        self._counter = getattr(self, "_counter", 0) + 1
        source = self.message(f"qa23-{self._counter}")
        source.text, source.source, source.provider_namespace = text, "webhook", "qa23-source"
        source.save(update_fields=["text", "source", "provider_namespace"])
        with transaction.atomic():
            result = reduce_inbound_commerce_source(self.client, source, expected_provider_namespace=source.provider_namespace)
        self.assertTrue(result.ready, result.reason)
        decision = IgCommerceTurnDecision.objects.get(source_message=source)
        self.client.refresh_from_db()
        return source, decision

    def test_ingress_number_word_add_preserves_quantity_source_fact(self):
        source, decision = self.reduce("додайте дві футболки розмір XL")
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.session.lines[0]["quantity"], 2)
        self.assertEqual(decision.session.lines[0]["size"], "XL")
        self.assertEqual(decision.transition.action, "line_operations_applied")
        self.assertEqual(decision.transition.source_message_id, source.pk)

    def test_ingress_sibling_copy_preserves_identity_without_self_measurements(self):
        self.reduce(f"https://twocomms.shop/product/{self.classic.slug}/ чорну футболку розмір M класичну")
        _, decision = self.reduce("ще одну таку саму на подарунок сестрі")
        self.assertTrue(decision.accepted)
        self.assertEqual(len(decision.session.lines), 2)
        first, sibling = decision.session.lines
        self.assertEqual(first["size"], "M")
        self.assertEqual(sibling["product_id"], self.classic.pk)
        self.assertEqual(sibling["recipient_id"], "sister")
        self.assertNotIn("size", sibling)
        self.assertNotIn("fit_option_code", sibling)

    def test_ingress_comma_free_colour_correction_is_source_bound(self):
        self.reduce(f"https://twocomms.shop/product/{self.classic.slug}/ чорну футболку розмір M")
        _, decision = self.reduce("не чорне а біле")
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.session.lines[0]["color"], "white")
        self.assertEqual(decision.session.lines[0]["product_id"], self.classic.pk)
        self.assertEqual(decision.session.lines[0]["size"], "M")
