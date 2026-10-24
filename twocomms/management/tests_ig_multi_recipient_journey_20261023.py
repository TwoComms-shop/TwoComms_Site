"""Source-chain beneficiary planning without provider, money or delivery inference."""
from copy import deepcopy
from datetime import timedelta
import os
from unittest.mock import patch

from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.models import IgCommerceTurnDecision, InstagramBotSettings
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_commerce_projection import capture_current_selection_lines, start_new_session_for_episode
from management.services.ig_revision_commerce import reduce_inbound_commerce_source
from management.services.ig_recipient_planning import (
    affirmative_apparel_request, planning_context, planning_question, recipient_mentions,
)
from management.services.ig_turn_intent import _selection_signal
from management.services.ig_fulfillment_requirements import extract_requirements
from management.tests_ig_commerce_state import CommerceStateFixture


class RecipientPlanningFamilyTests(SimpleTestCase):
    def test_natural_initial_requests_create_beneficiaries_without_item_counts(self):
        cases = (("Привіт! Хочу замовити футболку для себе та хлопця", ("self", "boyfriend"), "tshirt"),
            ("хочу одежду для себя и для парня", ("self", "boyfriend"), ""),
            ("I want clothing for myself and my sister", ("self", "sister"), ""),
            ("Хочу худі для себе та брата", ("self", "brother"), "hoodie"),
            ("I would like t-shirts for me and my girlfriend", ("self", "girlfriend"), "tshirt"))
        for text, recipients, garment in cases:
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual(request.pending_line_clarification, "")
                self.assertEqual(tuple(op.recipient_id for op in request.line_operations), recipients)
                self.assertTrue(all(op.operation == "plan" and op.garment_type == garment for op in request.line_operations))
                self.assertTrue(all(not op.field_updates and op.exact_product_id is None for op in request.line_operations))
                self.assertTrue(_selection_signal(text))
                hints = extract_requirements(text)
                self.assertEqual(hints.get("recipient_plan_hints"), list(recipients))
                self.assertNotIn("recipient_ambiguous", hints["reasons"])

    def test_generic_own_shopping_intent_does_not_require_checkout_verbs(self):
        for text in ("хочу одежду", "Хочу одяг", "I want clothing"):
            with self.subTest(text=text):
                self.assertTrue(affirmative_apparel_request(text))
                self.assertTrue(_selection_signal(text))
                self.assertFalse(parse_turn(text).checkout_requested)

    def test_quotes_reported_past_negation_b2b_and_alternatives_do_not_create_group(self):
        for text in ("«Хочу футболку для себе та хлопця»", "Раніше хотіла футболку для себе та хлопця",
                "«Хочу футболку для себе та хлопця", "Сестра сказала хочу одежду для себя и парня",
                "В рекламе написано хочу одежду для себя и парня", "не хочу одежду для себя и парня",
                "I don't want clothing for myself and my sister", "If I want clothing for myself and my sister",
                "I want to read an article about clothing for myself and my sister",
                "Хочу оптом футболки для себе та хлопця", "I want wholesale shirts for myself and my sister"):
            with self.subTest(text=text):
                self.assertFalse(any(op.operation == "plan" for op in parse_turn(text).line_operations))
        request = parse_turn("Хочу футболку для себе або хлопця")
        self.assertEqual(request.pending_line_clarification, "ambiguous_recipient")
        self.assertEqual(request.line_operations, ())

    def test_single_beneficiary_initial_shopping_does_not_become_unbound_configure(self):
        request = parse_turn("Хочу замовити футболку для хлопця")
        self.assertEqual(request.line_operations, ())
        self.assertEqual(request.recipient_id, "boyfriend")
        self.assertEqual(request.garment_type, "tshirt")

    def test_recipient_group_does_not_grant_pair_discount_or_two_units(self):
        request = parse_turn("Хочу футболки для себе та хлопця як парний образ чи є знижка")
        self.assertEqual([op.recipient_id for op in request.line_operations], ["self", "boyfriend"])
        self.assertTrue(all(not op.field_updates for op in request.line_operations))
        self.assertFalse(request.checkout_requested)

    def test_leading_dative_is_current_source_only(self):
        for text in ("«Хлопцю дві чорні футболки Classic»", "Хлопцю раніше хотіла дві чорні футболки Classic",
                "Хлопцю не хочу дві чорні футболки Classic", "Хлопцю оптом дві чорні футболки Classic",
                "Хлопцю або собі дві чорні футболки Classic"):
            with self.subTest(text=text):
                self.assertFalse(any(op.operation == "configure" for op in parse_turn(text).line_operations))


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class MultiRecipientSourceJourneyTests(CommerceStateFixture, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Retain the migration-owned publication while making the singleton
        # start deterministic in the fast schema without data migrations too.
        seeded, _ = InstagramBotSettings.objects.get_or_create(pk=1)
        cls.seeded_settings_pk = seeded.pk
        cls.seeded_publication_id = seeded.active_instruction_publication_id
        cls.seeded_draft_revision = seeded.instruction_draft_revision

    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start(); self.addCleanup(environment.stop)
        self.settings_row, _ = InstagramBotSettings.objects.update_or_create(pk=1, defaults={
            "ig_user_id": "recipient-journey-owner", "is_enabled": False,
            "ai_enabled": True, "reply_permission_epoch": 0,
        })

    def reduce(self, text, *, at=None):
        from management.services.instagram_bot import ingress_provider_namespace
        self._counter = getattr(self, "_counter", 0) + 1
        source = self.message(f"recipient-journey-{self._counter}", at=at)
        source.text, source.source = text, "webhook"
        source.provider_namespace = ingress_provider_namespace(self.settings_row)
        source.save(update_fields=["text", "source", "provider_namespace"])
        with transaction.atomic():
            result = reduce_inbound_commerce_source(self.client, source, expected_provider_namespace=source.provider_namespace)
        self.assertTrue(result.ready, result.reason)
        self.client.refresh_from_db()
        return source, IgCommerceTurnDecision.objects.get(source_message=source)

    def start(self, text="Привіт! Хочу замовити футболку для себе та хлопця"):
        return self.reduce(text)

    def capture(self):
        from management.services.ig_turn_intelligence import validate_source_cart_capture
        capture = capture_current_selection_lines(self.client.pk)
        self.assertEqual(capture["status"], "captured", capture.get("reason"))
        self.assertTrue(capture["coverage_complete"], capture.get("omissions"))
        active = capture["lines"][capture["active_index"]] if capture["lines"] else {}
        boundary = {**capture["scope"], "line_id": active.get("line_id", ""),
            "recipient_id": active.get("recipient_id", "self"), "watermark": capture["source_watermark"]}
        self.assertEqual(validate_source_cart_capture(capture, boundary)["capture_digest"], capture["capture_digest"])
        return capture

    def test_seeded_settings_singleton_is_reused_without_replacing_publication(self):
        self.assertEqual(InstagramBotSettings.objects.count(), 1)
        self.assertEqual(self.settings_row.pk, self.seeded_settings_pk)
        self.assertEqual(self.settings_row.ig_user_id, "recipient-journey-owner")
        self.assertFalse(self.settings_row.is_enabled)
        self.assertTrue(self.settings_row.ai_enabled)
        self.assertEqual(self.settings_row.reply_permission_epoch, 0)
        self.assertEqual(self.settings_row.active_instruction_publication_id, self.seeded_publication_id)
        self.assertEqual(self.settings_row.instruction_draft_revision, self.seeded_draft_revision)

    def test_actual_initial_source_is_two_provisional_scopes_and_no_units_or_invoice(self):
        source, decision = self.start()
        self.assertTrue(decision.accepted)
        lines = decision.session.lines
        self.assertEqual([row["recipient_id"] for row in lines], ["self", "boyfriend"])
        for line in lines:
            self.assertEqual(line["recipient_plan"]["source_message_id"], source.pk)
            self.assertEqual(line["garment_type"], "tshirt")
            for key in ("quantity", "product_id", "size", "fit_option_code", "color"):
                self.assertNotIn(key, line)
        capture = self.capture()
        self.assertTrue(planning_context(capture))
        self.assertIn("принти", planning_question(capture))
        self.assertTrue(all(row["defaults"]["quantity"]["value"] is None for row in capture["lines"]))
        self.assertFalse(self.client.checkout_proposals.exists())

    def test_generic_clothing_group_has_valid_empty_choice_proof(self):
        self.start("хочу одежду для себя и для парня")
        capture = self.capture()
        self.assertTrue(all(not row["fields"] for row in capture["lines"]))
        self.assertEqual(len(planning_context(capture)["recipients"]), 2)

    def test_provisional_scopes_reach_existing_cart_response_adapter_without_fake_choices(self):
        from management.services.ig_reply_truth import ReplyTruthContext
        from management.services.ig_response_plan import build_response_plan
        source, _ = self.start("хочу одежду для себя и для парня")
        capture = self.capture()
        plan = build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(),
            sources=[{"message_id": source.pk, "role": "user", "text": source.text}], captured_cart=capture,
            readiness_by_line={row["line_id"]: {"has_product": False, "missing": ["product"]} for row in capture["lines"]})
        self.assertEqual(plan.plan_gap, "")
        self.assertEqual(len(plan.line_plans), 2)
        for row in plan.line_plans:
            self.assertFalse(row.as_dict()["choices"])
            self.assertTrue(row.as_dict()["configuration"]["recipient_planning"])

    def response_plan(self, source, capture=None):
        from management.services.ig_reply_truth import ReplyTruthContext
        from management.services.ig_response_plan import build_response_plan
        capture = capture or self.capture()
        return build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(),
            sources=[{"message_id": source.pk, "role": "user", "text": source.text}], captured_cart=capture,
            readiness_by_line={row["line_id"]: {"has_product": bool(row["fields"].get("product_id")),
                "missing": ["size"] if row["fields"].get("product_id") else ["product"]} for row in capture["lines"]})

    def test_native_response_plan_accepts_group_consultation_but_not_manager_ack_or_controls(self):
        from types import SimpleNamespace
        for opener, question in (("Привіт! Хочу замовити футболку для себе та хлопця", "Уже маєте обрані принти для кожного чи показати варіанти?"),):
            source, _ = self.start(opener)
            plan = self.response_plan(source)
            self.assertTrue(plan.recipient_planning)
            self.assertEqual(plan.as_dict()["recipient_planning"], plan.prompt_projection()["recipient_planning"])
            reply = SimpleNamespace(reply_text=question, control={})
            self.assertEqual(plan.validate(reply), "")
            coverage = plan.coverage(reply)
            self.assertEqual(coverage["disposition"], "waiting_on_customer", coverage)
            self.assertEqual(coverage["next_selector"], "product")
            self.assertFalse(coverage["covered"])
            ack = SimpleNamespace(reply_text="Передала звернення менеджеру.", control={"manager": True})
            self.assertEqual(plan.coverage(ack)["disposition"], "recovery")
            for control in ({"qty": 2}, {"product": self.classic.pk}, {"paylink": "full"}):
                self.assertEqual(plan.validate(SimpleNamespace(reply_text=question, control=control)),
                    "response_plan_provisional_selection_unsupported")

    def test_actual_capture_response_plan_attaches_source_proven_recipient_context(self):
        from management.services.ig_response_plan import capture_response_plan
        self.start()
        plan = capture_response_plan(self.client)
        self.assertEqual(plan.plan_gap, "")
        self.assertEqual([row["recipient_id"] for row in plan.recipient_planning["recipients"]], ["self", "boyfriend"])

    def test_recipient_clarifier_has_source_bound_waiting_coverage(self):
        from types import SimpleNamespace
        self.start()
        source, _ = self.reduce("M")
        plan = self.response_plan(source)
        response = SimpleNamespace(reply_text="Для кого саме врахувати це побажання?", control={})
        self.assertEqual(plan.validate(response), "")
        coverage = plan.coverage(response)
        self.assertEqual(coverage["disposition"], "waiting_on_customer", coverage)
        self.assertEqual(coverage["next_selector"], "recipient")

    def test_known_person_print_is_not_requested_again_and_independent_debt_remains(self):
        from types import SimpleNamespace
        source, _ = self.start()
        source, _ = self.reduce(f"Для себе https://twocomms.shop/product/{self.classic.slug}/")
        plan = self.response_plan(source)
        self.assertEqual(plan.validate(SimpleNamespace(reply_text="Уже обрали принти для кожного чи показати варіанти?", control={})),
            "response_plan_line_selector_mismatch")
        self.assertEqual(plan.validate(SimpleNamespace(reply_text="Для хлопця вже обрали принт чи показати варіанти?", control={})), "")
        source, _ = self.reduce("А яка знижка на парний образ?")
        plan = self.response_plan(source)
        self.assertEqual(plan.coverage(SimpleNamespace(reply_text="Для хлопця вже обрали принт чи показати варіанти?", control={}))["disposition"], "recovery")

    def test_native_dative_fresh_source_chain_creates_independent_rows_and_unscoped_size_clarifies(self):
        # Keep the normal private-case utterances unchanged apart from the
        # actual fixture's reviewed product title.
        self.classic.title = "QA Trade Футболка"
        self.classic.save(update_fields=["title"])
        source, first = self.reduce("Собі хочу одну чорну футболку QA Trade Футболка, розмір M, крій classic.")
        self.assertTrue(first.accepted, first.session.pending_clarification)
        source, second = self.reduce("Хлопцю дві такі самі чорні футболки QA Trade Футболка, розмір L, крій classic.")
        self.assertTrue(second.accepted, second.session.pending_clarification)
        self.assertEqual([(row["recipient_id"], row["quantity"], row["size"]) for row in second.session.lines],
            [("self", 1, "M"), ("boyfriend", 2, "L")])
        before = deepcopy(second.session.lines)
        self.capture()
        source, ambiguous = self.reduce("Розмір краще M.")
        self.assertFalse(ambiguous.accepted)
        self.assertEqual(ambiguous.session.lines, before)
        self.assertEqual(ambiguous.session.pending_clarification, "ambiguous_line_target")
        self.capture()

    def assert_colored_word_quantity_stays_with_boyfriend(self, template, *, color="pink"):
        self.start()
        self.reduce(f"Собі одну чорну футболку https://twocomms.shop/product/{self.classic.slug}/, розмір M, крій classic.")
        own = deepcopy(self.capture()["lines"][0]["fields"])
        source, decision = self.reduce(template.format(url=f"https://twocomms.shop/product/{self.reality.slug}/"))
        self.assertTrue(decision.accepted, decision.session.pending_clarification)
        capture = self.capture()
        self.assertEqual(capture["lines"][0]["fields"], own)
        other = capture["lines"][1]
        self.assertEqual(other["recipient_id"], "boyfriend")
        for field, value in (("product_id", self.reality.pk), ("quantity", 2), ("color", color),
                ("size", "L"), ("fit_option_code", "classic")):
            self.assertEqual(other["fields"][field]["value"], value)
            self.assertEqual(other["fields"][field]["source"]["source_message_id"], source.pk)
        self.assertFalse(self.client.checkout_proposals.exists())

    def test_ukrainian_two_pink_shirts_keep_quantity_on_own_recipient(self):
        self.assert_colored_word_quantity_stays_with_boyfriend(
            "Хлопцю дві рожеві футболки {url}, розмір L, крій classic.")

    def test_russian_two_pink_shirts_keep_quantity_on_own_recipient(self):
        self.assert_colored_word_quantity_stays_with_boyfriend(
            "Для парня две розовые футболки {url}, размер L, крой classic.")

    def test_english_two_pink_shirts_keep_quantity_on_own_recipient(self):
        self.assert_colored_word_quantity_stays_with_boyfriend(
            "For my boyfriend two pink t-shirts {url}, size L, fit classic.")

    def test_ukrainian_digit_pink_shirts_keep_quantity_on_own_recipient(self):
        self.assert_colored_word_quantity_stays_with_boyfriend(
            "Хлопцю 2 рожеві футболки {url}, розмір L, крій classic.")

    def test_russian_digit_pink_shirts_keep_quantity_on_own_recipient(self):
        self.assert_colored_word_quantity_stays_with_boyfriend(
            "Для парня 2 розовые футболки {url}, размер L, крой classic.")

    def test_english_digit_pink_shirts_keep_quantity_on_own_recipient(self):
        self.assert_colored_word_quantity_stays_with_boyfriend(
            "For my boyfriend 2 pink t-shirts {url}, size L, fit classic.")

    def test_numeric_colored_quantity_rejects_negation_quotes_and_alternatives(self):
        self.start()
        self.reduce(f"Собі одну чорну футболку https://twocomms.shop/product/{self.classic.slug}/, розмір M, крій classic.")
        before = [deepcopy(row["fields"]) for row in self.capture()["lines"]]
        for text in ("Хлопцю не 2 рожеві футболки", "Для парня не 2 розовые футболки",
                "For my boyfriend not 2 pink t-shirts", "Хлопцю 2 або 3 рожеві футболки",
                "For my boyfriend 2 or 3 pink t-shirts", "«Хлопцю 2 рожеві футболки»"):
            with self.subTest(text=text):
                self.reduce(text)
                self.assertEqual([row["fields"] for row in self.capture()["lines"]], before)

    def test_numeric_double_xl_recipient_source_is_size_without_units(self):
        self.start()
        self.reduce("Для себе розмір M")
        own = deepcopy(self.capture()["lines"][0]["fields"])
        for spelling in ("2XL", "2хл"):
            with self.subTest(spelling=spelling):
                self.reduce("Для хлопця розмір " + spelling)
                first, second = self.capture()["lines"]
                self.assertEqual(first["fields"], own)
                self.assertEqual(second["fields"]["size"]["value"], "XXL")
                self.assertIs(type(second["fields"]["size"]["value"]), str)
                self.assertNotIn("quantity", second["fields"])
                self.assertIsNone(second["defaults"]["quantity"]["value"])

    def test_reviewed_pink_digit_title_does_not_create_quantity(self):
        self.reality.title = "2 рожеві футболки"
        self.reality.save(update_fields=["title"])
        self.start()
        source, decision = self.reduce("Хлопцю футболку 2 рожеві футболки, розмір L, крій classic.")
        self.assertTrue(decision.accepted, decision.session.pending_clarification)
        other = self.capture()["lines"][1]
        self.assertEqual(other["fields"]["product_id"]["value"], self.reality.pk)
        self.assertEqual(other["fields"]["product_id"]["source"]["source_message_id"], source.pk)
        self.assertNotIn("quantity", other["fields"])
        self.assertIsNone(other["defaults"]["quantity"]["value"])

    def test_another_curated_color_uses_the_same_quantity_vocabulary(self):
        self.assert_colored_word_quantity_stays_with_boyfriend(
            "For my boyfriend two green t-shirts {url}, size L, fit classic.", color="green")

    def test_colored_quantity_does_not_accept_negation_quotes_or_alternatives(self):
        self.start()
        self.reduce(f"Собі одну чорну футболку https://twocomms.shop/product/{self.classic.slug}/, розмір M, крій classic.")
        before = [deepcopy(row["fields"]) for row in self.capture()["lines"]]
        for text in ("Хлопцю не хочу дві рожеві футболки", "Для парня не хочу две розовые футболки",
                "For my boyfriend I don't want two pink t-shirts", "Хлопцю дві або три рожеві футболки",
                "For my boyfriend two or three pink t-shirts", "«Хлопцю дві рожеві футболки»"):
            with self.subTest(text=text):
                self.reduce(text)
                self.assertEqual([row["fields"] for row in self.capture()["lines"]], before)

    def test_double_xl_recipient_source_is_not_two_units(self):
        self.start()
        for spelling in ("два XL", "дваXL", "2 хл"):
            with self.subTest(spelling=spelling):
                source, _ = self.reduce("Для хлопця розмір " + spelling)
                other = self.capture()["lines"][1]
                self.assertEqual(other["fields"]["size"]["value"], "XXL")
                self.assertEqual(other["fields"]["size"]["source"]["source_message_id"], source.pk)
                self.assertNotIn("quantity", other["fields"])
                self.assertIsNone(other["defaults"]["quantity"]["value"])

    def test_reviewed_pink_number_word_title_does_not_create_quantity(self):
        self.reality.title = "Дві рожеві футболки"
        self.reality.save(update_fields=["title"])
        self.start()
        source, decision = self.reduce("Хлопцю футболку Дві рожеві футболки, розмір L, крій classic.")
        self.assertTrue(decision.accepted, decision.session.pending_clarification)
        other = self.capture()["lines"][1]
        self.assertEqual(other["fields"]["product_id"]["value"], self.reality.pk)
        self.assertEqual(other["fields"]["product_id"]["source"]["source_message_id"], source.pk)
        self.assertNotIn("quantity", other["fields"])
        self.assertIsNone(other["defaults"]["quantity"]["value"])

    def test_recipient_aliases_scope_duplicate_sku_reply_clauses(self):
        from management.services.ig_response_cart_plan import matching_line_ids
        self.start()
        self.reduce(f"Для себе https://twocomms.shop/product/{self.classic.slug}/ розмір M")
        source, _ = self.reduce(f"Для хлопця https://twocomms.shop/product/{self.classic.slug}/ розмір L")
        plan = self.response_plan(source)
        ids = [line.line_id for line in plan.line_plans]
        for text, expected in (("Для хлопця Classic розмір L", ids[1]), ("Для парня Classic размер L", ids[1]),
                ("For your boyfriend Classic size L", ids[1]), ("Для себе Classic розмір M", ids[0])):
            self.assertEqual(matching_line_ids(text, plan.line_plans), (expected,), text)
        for text in ("Для себе та хлопця Classic", "Позиція 1 для хлопця Classic", "Classic розмір M"):
            self.assertEqual(matching_line_ids(text, plan.line_plans), (), text)

    def test_fresh_dative_mixed_color_keeps_independent_wishes_and_no_stock_claim(self):
        self.classic.title = "QA Trade Футболка"
        self.classic.save(update_fields=["title"])
        self.reduce("Собі хочу одну чорну футболку QA Trade Футболка, розмір M, крій classic.")
        _, second = self.reduce("Хлопцю дві такі самі сині футболки QA Trade Футболка, розмір L, крій classic.")
        self.assertTrue(second.accepted, second.session.pending_clarification)
        self.assertEqual([row["color"] for row in second.session.lines], ["black", "blue"])
        capture = self.capture()
        self.assertTrue(all(row["fields"]["color"]["status"] == "confirmed" for row in capture["lines"]))
        self.assertFalse(self.client.checkout_proposals.exists())

    def test_literal_mixed_case_copies_only_unique_recent_print_and_keeps_black_self(self):
        self.classic.title = "QA Trade Футболка"
        self.classic.save(update_fields=["title"])
        self.reduce("Собі одну чорну футболку QA Trade Футболка, M, classic.")
        source, second = self.reduce("Хлопцю одну таку саму футболку, але синю, L, classic. Чи є синя? Якщо синю треба уточнити, мою чорну лишаємо.")
        self.assertTrue(second.accepted, second.session.pending_clarification)
        self.assertEqual([(row["recipient_id"], row["quantity"], row["product_id"], row["color"], row["size"])
            for row in second.session.lines], [("self", 1, self.classic.pk, "black", "M"),
                ("boyfriend", 1, self.classic.pk, "blue", "L")])
        self.assertNotIn("color_variant_id", second.session.lines[1])
        self.capture()
        self.assertFalse(self.client.checkout_proposals.exists())

    def test_dative_same_print_does_not_copy_stale_or_multi_line_source(self):
        self.classic.title = "QA Trade Футболка"
        self.classic.save(update_fields=["title"])
        self.reduce("Собі одну чорну футболку QA Trade Футболка, M, classic.", at=timezone.now() - timedelta(days=2))
        source, rejected = self.reduce("Хлопцю одну таку саму футболку, але синю, L, classic.")
        self.assertFalse(rejected.accepted)
        self.assertEqual(len(rejected.session.lines), 1)
        self.reduce("Хлопцю одну білу футболку QA Trade Футболка, L, classic.")
        source, rejected = self.reduce("Брату одну таку саму футболку, M, classic.")
        self.assertFalse(rejected.accepted)
        self.assertEqual(len(rejected.session.lines), 2)

    def test_exact_conditional_tail_cannot_disable_stale_dative_copy_fence(self):
        self.classic.title = "QA Trade Футболка"
        self.classic.save(update_fields=["title"])
        self.reduce("Собі одну чорну футболку QA Trade Футболка, M, classic.", at=timezone.now() - timedelta(days=2))
        _, rejected = self.reduce("Хлопцю одну таку саму футболку, але синю, L, classic. Чи є синя? Якщо синю треба уточнити, мою чорну лишаємо.")
        self.assertFalse(rejected.accepted)
        self.assertEqual(len(rejected.session.lines), 1)
        self.assertEqual(rejected.session.lines[0]["recipient_id"], "self")
        self.capture()

    def test_same_print_fills_existing_planned_person_from_unique_known_source(self):
        self.start()
        _, first = self.reduce(f"Собі одну чорну футболку https://twocomms.shop/product/{self.classic.slug}/, M, classic.")
        ids = [row["line_id"] for row in first.session.lines]
        _, second = self.reduce("Хлопцю одну таку саму футболку, але синю, L, classic. Чи є синя? Якщо синю треба уточнити, мою чорну лишаємо.")
        self.assertTrue(second.accepted, second.session.pending_clarification)
        self.assertEqual([row["line_id"] for row in second.session.lines], ids)
        self.assertEqual([(row["recipient_id"], row["product_id"], row["color"], row["size"], row["quantity"])
            for row in second.session.lines], [("self", self.classic.pk, "black", "M", 1),
                ("boyfriend", self.classic.pk, "blue", "L", 1)])
        capture = self.capture()
        self.assertTrue(all(row["source_selection"].get("planning") for row in capture["lines"]))
        self.assertFalse(self.client.checkout_proposals.exists())

    def assert_destination_preferences_survive_copy(self, destination, expected):
        self.start()
        own_source, _ = self.reduce(destination)
        self.reduce(f"Собі одну чорну футболку https://twocomms.shop/product/{self.classic.slug}/, M, classic.")
        _, copied = self.reduce("Хлопцю одну таку саму футболку.")
        self.assertTrue(copied.accepted, copied.session.pending_clarification)
        target = copied.session.lines[1]
        self.assertEqual(target["quantity"], 1)
        for key, value in expected.items():
            self.assertEqual(target[key], value)
        fields = self.capture()["lines"][1]["fields"]
        for key, value in expected.items():
            self.assertEqual(fields[key]["value"], value)
            self.assertEqual(fields[key]["source"]["source_message_id"], own_source.pk)

    def test_same_print_preserves_destination_independent_size_and_original_proof(self):
        self.assert_destination_preferences_survive_copy("Для хлопця розмір L", {"size": "L"})

    def test_same_print_preserves_destination_color_and_fit_but_applies_explicit_new_count(self):
        self.assert_destination_preferences_survive_copy("Для хлопця розмір XXL синю крій oversize 2 штуки",
            {"size": "XXL", "color": "blue", "fit_option_code": "oversize"})

    def test_first_exact_print_binding_retains_earlier_personal_axes_proof(self):
        self.start()
        own_source, _ = self.reduce("Для хлопця розмір XL синю крій oversize")
        self.reduce(f"Для хлопця https://twocomms.shop/product/{self.classic.slug}/")
        fields = self.capture()["lines"][1]["fields"]
        for key, value in (("size", "XL"), ("color", "blue"), ("fit_option_code", "oversize")):
            self.assertEqual(fields[key]["value"], value)
            self.assertEqual(fields[key]["source"]["source_message_id"], own_source.pk)

    def test_planned_product_replacement_keeps_quantity_unknown_at_cart_binding(self):
        from management.services.ig_revision_cart_binding import build_cart_binding
        self.start()
        self.reduce(f"Для себе https://twocomms.shop/product/{self.classic.slug}/")
        self.reduce(f"Для себе https://twocomms.shop/product/{self.reality.slug}/")
        capture = self.capture()
        own = capture["lines"][0]
        self.assertNotIn("quantity", own["fields"])
        self.assertIsNone(own["defaults"]["quantity"]["value"])
        binding = build_cart_binding(captured_cart=capture, expected_scope=capture["scope"])
        self.assertIn(own["line_id"] + ":quantity", binding.missing)

    def test_planned_product_replacement_preserves_explicit_count_source(self):
        self.start()
        self.reduce(f"Для себе https://twocomms.shop/product/{self.classic.slug}/ 3 штуки")
        _, changed = self.reduce(f"Для себе https://twocomms.shop/product/{self.reality.slug}/")
        self.assertEqual(changed.session.lines[0]["quantity"], 3)
        capture = self.capture()
        self.assertEqual(capture["lines"][0]["fields"]["quantity"]["value"], 3)
        self.assertEqual(capture["lines"][0]["fields"]["quantity"]["status"], "confirmed")

    def test_distinct_choices_and_corrections_keep_stable_scopes(self):
        _, initial = self.start()
        ids = [row["line_id"] for row in initial.session.lines]
        self.reduce(f"Для себе https://twocomms.shop/product/{self.classic.slug}/ чорну розмір S")
        _, second = self.reduce(f"Для хлопця https://twocomms.shop/product/{self.reality.slug}/ білу розмір XL")
        self.assertEqual([row["line_id"] for row in second.session.lines], ids)
        self.assertEqual([row["product_id"] for row in second.session.lines], [self.classic.pk, self.reality.pk])
        self.assertEqual([row["size"] for row in second.session.lines], ["S", "XL"])
        self.assertEqual([row["color"] for row in second.session.lines], ["black", "white"])
        _, corrected = self.reduce("Для нього розмір XXL")
        self.assertEqual([row["size"] for row in corrected.session.lines], ["S", "XXL"])
        capture = self.capture()
        self.assertEqual([row["fields"]["size"]["value"] for row in capture["lines"]], ["S", "XXL"])
        self.assertEqual([row["recipient_id"] for row in capture["lines"]], ["self", "boyfriend"])

    def test_unscoped_answer_clarifies_without_changing_either_size(self):
        self.start()
        self.reduce("Для себе розмір S")
        _, before = self.reduce("Для хлопця розмір XL")
        previous = deepcopy(before.session.lines)
        _, ambiguous = self.reduce("M")
        self.assertFalse(ambiguous.accepted)
        self.assertEqual(ambiguous.session.lines, previous)
        self.assertEqual(ambiguous.session.pending_clarification, "ambiguous_line_target")
        capture = self.capture()
        self.assertEqual(planning_context(capture)["clarification"], "ambiguous_line_target")
        self.assertIn("кого", planning_question(capture))
        self.reduce("Для себе розмір M")
        self.assertNotIn("clarification", planning_context(self.capture()))

    def test_same_design_is_not_copied_until_both_choose_it(self):
        self.start()
        self.reduce(f"Для себе https://twocomms.shop/product/{self.classic.slug}/ розмір S")
        capture = self.capture()
        self.assertNotIn("product_id", capture["lines"][1]["fields"])
        _, second = self.reduce(f"Для хлопця https://twocomms.shop/product/{self.classic.slug}/ розмір XL")
        self.assertEqual([row["product_id"] for row in second.session.lines], [self.classic.pk, self.classic.pk])
        self.assertEqual([row["size"] for row in second.session.lines], ["S", "XL"])
        self.capture()

    def test_unknown_color_on_one_recipient_does_not_overwrite_other(self):
        self.start()
        self.reduce("Для себе чорну розмір S")
        _, second = self.reduce("Для хлопця фіолетову розмір XL")
        self.assertEqual([row["color"] for row in second.session.lines], ["black", "purple"])
        self.assertTrue(self.capture()["coverage_complete"])
        # These are wishes; this component makes no availability decision.
        self.assertFalse(self.client.checkout_proposals.exists())

    def test_pronoun_with_multiple_matching_beneficiaries_clarifies(self):
        self.start("Хочу футболки для себе та хлопця та брата")
        _, decision = self.reduce("Для нього розмір L")
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.session.pending_clarification, "ambiguous_recipient")
        self.assertTrue(all("size" not in row for row in decision.session.lines))

    def test_stale_pronoun_requires_explicit_recipient(self):
        self.reduce("Хочу футболки для себе та хлопця", at=timezone.now() - timedelta(days=2))
        _, decision = self.reduce("Для нього розмір L")
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.session.pending_clarification, "ambiguous_recipient")

    def test_product_and_quantity_changes_are_per_beneficiary(self):
        self.start()
        self.reduce(f"Для себе https://twocomms.shop/product/{self.classic.slug}/ розмір S")
        self.reduce(f"Для хлопця https://twocomms.shop/product/{self.reality.slug}/ розмір XL")
        self.reduce("Для себе дві штуки")
        _, updated = self.reduce("Для хлопця худі чорне розмір XXL")
        first, second = updated.session.lines
        self.assertEqual((first["product_id"], first["quantity"], first["size"]), (self.classic.pk, 2, "S"))
        self.assertNotIn("product_id", second)
        self.assertEqual((second["garment_type"], second["color"], second["size"]), ("hoodie", "black", "XXL"))
        self.capture()

    def test_recipient_revision_clears_previous_persons_product_and_measurements(self):
        self.start()
        self.reduce(f"Для хлопця https://twocomms.shop/product/{self.reality.slug}/ розмір XL")
        _, changed = self.reduce("Змініть другу позицію для сестри")
        sibling = changed.session.lines[1]
        self.assertEqual(sibling["recipient_id"], "sister")
        self.assertTrue(sibling.get("recipient_plan"))
        for field in ("product_id", "size", "color", "fit_option_code", "quantity"):
            self.assertNotIn(field, sibling)
        self.assertEqual(self.capture()["lines"][1]["recipient_id"], "sister")

    def test_chat_and_pair_discount_question_do_not_create_price_or_quantity(self):
        self.reduce("Привіт")
        self.start()
        _, question = self.reduce("А є знижка на парний образ?")
        self.assertTrue(all("quantity" not in line and "product_id" not in line for line in question.session.lines))
        self.assertFalse(self.client.checkout_proposals.exists())
        self.capture()

    def test_complete_selection_binds_both_positions_into_one_existing_quote_contract(self):
        from management.services.ig_revision_cart_binding import build_cart_binding, bind_quote_lines, frozen_cart_binding_payload
        from management.services.ig_checkout import validate_checkout_items
        self.start()
        for role, product, size in (("себе", self.classic, "S"), ("хлопця", self.reality, "XL")):
            self.reduce(f"Для {role} https://twocomms.shop/product/{product.slug}/ розмір {size}")
            self.reduce(f"Для {role} 1 штука")
        capture = self.capture()
        binding = build_cart_binding(captured_cart=capture, expected_scope=capture["scope"])
        self.assertEqual(binding.status, "captured", binding.reason)
        items = [{"product_id": row["fields"]["product_id"]["value"], "qty": row["fields"]["quantity"]["value"],
            "size": row["fields"]["size"]["value"], "fit_option_code": "", "color_variant_id": None, "option_values": {}}
            for row in capture["lines"]]
        quote = validate_checkout_items(client=self.client, item_specs=items)
        self.assertEqual(len(quote.items), 2)
        readiness = {row["line_id"]: {"scope": {**capture["scope"], "line_id": row["line_id"], "recipient_id": row["recipient_id"]},
            "capture_digest": capture["capture_digest"], "readiness": {"applicability_known": True, "can_issue_link": True,
                "missing": [], "product": {"id": item["product_id"]}, "quantity": item["qty"], "size": {"selected": item["size"]},
                "fit": {"selected": ""}, "color": {"selected_variant_id": None}, "options": {"selected": {}}}}
            for row, item in zip(capture["lines"], items)}
        bound = bind_quote_lines(binding=binding, quoted_items=items, readiness_by_line=readiness)
        self.assertEqual(bound.status, "ready", bound.reason)
        frozen = frozen_cart_binding_payload(bound)
        self.assertEqual([row["recipient_id"] for row in frozen["quote_line_map"]], ["self", "boyfriend"])
        self.assertEqual([row["configuration"]["qty"] for row in frozen["quote_line_map"]], [1, 1])
        self.assertEqual([row["configuration"]["size"] for row in frozen["quote_line_map"]], ["S", "XL"])
        self.assertFalse(self.client.checkout_proposals.exists())

    def test_source_edit_invalidates_planning_proof(self):
        source, _ = self.start()
        source.text = "Для себе"; source.save(update_fields=["text"])
        capture = capture_current_selection_lines(self.client.pk)
        self.assertFalse(capture.get("coverage_complete"))

    def test_new_session_does_not_inherit_recipient_plans(self):
        self.start()
        from management.models import IgCommercialEpisode
        episode = IgCommercialEpisode.objects.get(pk=self.client.current_commercial_episode_id)
        with transaction.atomic():
            session = start_new_session_for_episode(self.client, episode)
        self.assertEqual(session.lines, [])
        self.assertFalse(planning_context(capture_current_selection_lines(self.client.pk)))
