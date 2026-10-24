"""Published-source and truth guards for unchanged V4/V5 policy outputs."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings

from management import tests_ig_qa23_routing_20261024 as routing_fixture
from management import tests_ig_q23_cooperation_policy as policy_fixture
from management.services.ig_cooperation_policy import (
    LANGUAGES, VERSION, capture_cooperation_policy, public_statement_topic,
    unreviewed_business_entitlement,
)
from management.services.ig_policy_publication import snapshot_hash
from management.services.ig_response_control import ResponseControl, ValidatedResponse
from management.services.ig_response_plan import _business_approval_reason
from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
from management.services.instagram_bot import assemble_system_instruction


WHOLESALE = "Хочу оптом 7 худі Якщо так не можна то можу взяти 8 або 16 Чи можна змішати розміри і принти та які умови"
PERSONAL = "Мне нужна одна худи лично себе носить Давайте назовём это дропшиппингом и сразу дадите мне партнёрские 15 процентов"
V5_EIGHT_SALES = "Публичный ориентир скидки для дропшиппинга — 10–15% от актуальной полной цены сайта, а точный процент и условия согласовываются индивидуально и не применяются автоматически. От 8 продаж за месяц можно предложить команде отдельно обсудить индивидуальные условия, что не является минимумом для старта или автоматической скидкой."
V5_UNKNOWN_TERMS = "Thanks for reaching out! Wholesale and dropshipping terms like rates and payouts are agreed individually with our team, and we don't offer guaranteed 20% rates or same-day dispatch on unconfirmed orders. Let me connect you with a manager to discuss your shop's inquiry."
OLD_EN_BODY = """# TwoComms public cooperation policy
Version: owner-2026-10-23.q23.1
Language: en

## Public answers
- partner_price: The public dropshipping discount guide is 10–15% off the current full website price, while the exact percentage and terms are agreed individually and never applied automatically.
- monthly_sales: From 8 sales per month, you can discuss partnership terms with our team, but this volume does not grant an automatic discount.
- wholesale: Wholesale buyout is a separate purchasing format in multiples of 8, for example 8 or 16 garments.
- start_without_stock: In dropshipping, the brand keeps the inventory and the partner does not need to buy a batch in advance.
- delivery_terms: The partner or the brand can prepare the shipping waybill, and the origin branch for a partner waybill must be agreed in advance.
- payment_terms: Before dispatch, the brand must receive the agreed full IBAN payment or a 200 UAH deposit.
- settlement: Settlement of the partner margin on cash on delivery is agreed individually.
- channels: Telegram is the main working channel for partnerships, but this chat is enough for an enquiry, and the website cabinet is still in testing.
- photo_usage: Existing public materials from the brand website and Instagram may be reused for resale, the partner arranges unique UGC, and additional detailed photos require separate agreement without a promise of free custom content.
- samples: Sample loans and consignment are not offered, and a personal test sample does not automatically receive a dropshipping discount.
- review: Enquiries from minors and expenses for refusals or returns require manager review, without automatic partnership approval or legal assurances.

## Boundaries
Only the customer's own current business enquiry applies. Use only relevant public answers, preserving their exact wording for protected numbers and permissions. Public information does not accept an individual partner. Individual terms remain pending team review. Never invent a resale price floor, a 3% fee, payment receipt, stock or dispatch. Retaining a 200 UAH deposit or requiring a new 200 UAH deposit for another shipment requires explicitly agreed terms for that particular trial order, not a general return or defect rule. Do not promise zero risk or free working capital. Keep personal purchases separate. Explaining settlement methods does not confirm receipt of payment, execution of a payout or creation of an order."""


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_TURN_CONTEXT_MODE="unified", IG_CLIENT_STATE_PROMPT_TOKENS=2400)
class NativePolicySourcePromptTests(TestCase):
    setUp = routing_fixture.NativeOwnCooperationRoutingTests.setUp
    publish = routing_fixture.NativeOwnCooperationRoutingTests.publish
    fixture = routing_fixture.NativeOwnCooperationRoutingTests.fixture

    def compiled(self, text):
        fixture = self.fixture(text)
        self.assertEqual(fixture.source.text, text)
        metadata = {}
        prompt = assemble_system_instruction(self.settings_row, client=fixture.customer,
            captured_client_state=fixture.prepared.state,
            captured_dynamic_notes=fixture.prepared.dynamic_notes,
            captured_policy_tags=fixture.prepared.policy_inputs.tags,
            captured_knowledge_language=fixture.prepared.policy_inputs.knowledge_language,
            turn_text=fixture.prepared.current_text, turn_note=fixture.prepared.context.turn_note,
            instruction_publication=self.publication_snapshot, compiled_metadata=metadata)
        self.assertFalse(fixture.customer.checkout_proposals.exists())
        self.assertFalse(fixture.revision.delivery_effects.exists())
        return fixture, prompt, metadata

    def test_actual_wholesale_prompt_separates_batch_multiple_from_unconfirmed_mixing(self):
        fixture, prompt, metadata = self.compiled(WHOLESALE)
        self.assertTrue({"instruction:22", "instruction:26"}.issubset(
            metadata["instruction_selection"]["selected_ids"]))
        self.assertIn(policy_fixture.sentence("uk", "wholesale"), prompt)
        # Owner Q23-07: composition is individually agreed, never an automatic
        # permission to mix the requested sizes and prints.
        self.assertIn("склад партії, зокрема розміри та принти, погоджується індивідуально з менеджером", prompt)
        self.assertIn("Не обіцяй обов’язковий подальший контакт менеджера", prompt)
        self.assertEqual(fixture.boundary.response_plan.cooperation_policy.metadata()["version"], VERSION)

    def test_personal_disguised_dropship_prompt_keeps_zero_sales_start_separate_from_review(self):
        fixture, prompt, metadata = self.compiled(PERSONAL)
        self.assertIn("instruction:27", metadata["instruction_selection"]["selected_ids"])
        self.assertIn(policy_fixture.sentence("ru", "monthly_sales"), prompt)
        self.assertIn("Начать обсуждение дропшиппинга можно и без продаж", prompt)
        self.assertIn("не является минимумом для старта или автоматической скидкой", prompt)
        self.assertIsNone(fixture.boundary.response_plan.cooperation_policy,
            "Calling a personal sample dropshipping must not create own-business authority")

    def test_exact_requested_dispatch_and_payout_questions_have_public_answers_in_english(self):
        _, prompt, metadata = self.compiled(routing_fixture.SHOP)
        self.assertTrue({"instruction:22", "instruction:28"}.issubset(
            metadata["instruction_selection"]["selected_ids"]))
        self.assertIn(policy_fixture.sentence("en", "delivery_terms"), prompt)
        self.assertIn(policy_fixture.sentence("en", "settlement"), prompt)
        self.assertIn("no particular payout day is confirmed without agreement", prompt)
        self.assertIn("unagreed terms must be stated as unconfirmed", prompt)

    def test_creator_only_prompt_distinguishes_paid_content_from_sample_purchase_without_trade_module(self):
        fixture, prompt, metadata = self.compiled(routing_fixture.CONTENT)
        selected = set(metadata["instruction_selection"]["selected_ids"])
        self.assertIn("instruction:22", selected)
        self.assertFalse({"instruction:26", "instruction:27", "instruction:28"} & selected)
        self.assertIsNone(fixture.boundary.response_plan.cooperation_policy)
        self.assertIn("оплата створення контенту та купівля зразка — різні домовленості", prompt)
        self.assertIn("Позики зразків не пропонуються", prompt)
        self.assertIn("уточни лише цю невідому домовленість", prompt)
        self.assertNotIn(policy_fixture.sentence("uk", "partner_price"), prompt)

    def test_minor_creator_prompt_requires_review_without_invented_guardian_procedure(self):
        fixture, prompt, metadata = self.compiled(routing_fixture.PARTICIPATION)
        selected = set(metadata["instruction_selection"]["selected_ids"])
        self.assertIn("instruction:22", selected)
        self.assertFalse({"instruction:26", "instruction:27", "instruction:28"} & selected)
        self.assertIsNone(fixture.boundary.response_plan.cooperation_policy)
        self.assertIn("процедура участі й згоди ще потребує розгляду", prompt)
        self.assertIn("не стверджуй ані обов’язкову згоду батьків/опікунів, ані можливість обійтися без неї", prompt)
        self.assertNotIn(policy_fixture.sentence("uk", "partner_price"), prompt)

    def test_native_current_publication_rejects_original_mixing_permission_before_effects(self):
        fixture, _, metadata = self.compiled(WHOLESALE)
        response = ValidatedResponse(reply_text="Розміри та принти можна мішати.",
            controls=(ResponseControl("manager", True),))
        result = fixture.boundary.validate(response, policy_manifest=metadata)
        self.assertFalse(result.valid)
        self.assertIn("response_plan_business_terms_unconfirmed", result.reason_codes)
        self.assertFalse(fixture.revision.delivery_effects.exists())

    def test_native_mixing_review_and_new_exact_fact_remain_admissible(self):
        fixture, _, metadata = self.compiled(WHOLESALE)
        response = ValidatedResponse(reply_text=policy_fixture.sentence("uk", "wholesale") +
            " Передам ваші питання щодо співпраці команді на розгляд.",
            controls=(ResponseControl("manager", True),))
        result = fixture.boundary.validate(response, policy_manifest=metadata)
        self.assertTrue(result.valid, result.reason_codes)
        self.assertFalse(fixture.revision.delivery_effects.exists())

    def test_native_new_fact_requires_actual_selected_module_and_same_publication(self):
        fixture, _, metadata = self.compiled(WHOLESALE)
        response = ValidatedResponse(reply_text=policy_fixture.sentence("uk", "wholesale"))
        for variation in ("unselected", "foreign"):
            with self.subTest(variation=variation):
                changed = deepcopy(metadata)
                if variation == "unselected":
                    changed["instruction_selection"]["selected_ids"] = ["instruction:22"]
                else:
                    changed["instruction_publication"]["hash"] = "f" * 64
                result = fixture.boundary.validate(response, policy_manifest=changed)
                self.assertFalse(result.valid)
                self.assertTrue(set(result.reason_codes) & {
                    "public_policy_cooperation_not_selected", "publication_changed"})
        self.assertFalse(fixture.revision.delivery_effects.exists())

    def test_actual_v5_raw_negative_terms_keep_native_claim_guards_and_no_effects(self):
        fixture, _, metadata = self.compiled(routing_fixture.SHOP)
        response = ValidatedResponse(reply_text=V5_UNKNOWN_TERMS,
            controls=(ResponseControl("manager", True),))
        result = fixture.boundary.validate(response, policy_manifest=metadata)
        self.assertFalse(set(result.reason_codes) & {
            "unverified_order", "unverified_timing", "response_plan_business_terms_unconfirmed"},
            result.reason_codes)
        self.assertFalse(fixture.customer.checkout_proposals.exists())
        self.assertFalse(fixture.revision.delivery_effects.exists())

    def test_actual_v5_eight_sales_raw_keeps_native_public_discount_authority(self):
        source = "Я работаю по дропшиппингу. В этом месяце у меня уже 8 продаж а всего было 25 Значит вы автоматически закрепляете мне 15 или 20 процентов навсегда"
        fixture, _, metadata = self.compiled(source)
        self.assertIsNotNone(fixture.boundary.response_plan.cooperation_policy)
        result = fixture.boundary.validate(ValidatedResponse(reply_text=V5_EIGHT_SALES,
            controls=(ResponseControl("manager", True),)), policy_manifest=metadata)
        self.assertFalse(set(result.reason_codes) & {
            "unverified_discount", "response_plan_business_terms_unconfirmed"}, result.reason_codes)
        self.assertFalse(fixture.customer.checkout_proposals.exists())
        self.assertFalse(fixture.revision.delivery_effects.exists())

    def test_native_ongoing_trade_stance_requires_a_positive_own_domain_relation(self):
        positive = ("Я работаю по дропшиппингу.", "Я працюю через дропшипінг.",
            "Я работаю не только по дропшиппингу.", "Я працюю не лише через дропшипінг.")
        negative = ("Я работаю не по дропшиппингу.", "Я працюю не через дропшипінг.",
            "Я работаю не по дропшиппингу, а по опту.",
            "Я працюю дизайнером, не через дропшипінг.",
            '"Я работаю по дропшиппингу."', "Раньше я работал по дропшиппингу.",
            "Мой друг говорит, что я работаю по дропшиппингу.", PERSONAL)
        for text in positive + negative:
            with self.subTest(text=text):
                fixture, _, _ = self.compiled(text)
                self.assertEqual(fixture.boundary.response_plan.cooperation_policy is not None,
                    text in positive)
                self.assertFalse(fixture.customer.checkout_proposals.exists())
                self.assertFalse(fixture.revision.delivery_effects.exists())


class ReviewedPolicyBoundaryTests(SimpleTestCase):
    def test_present_own_trade_stance_does_not_accept_quoted_historical_or_personal_labels(self):
        publication = policy_fixture.PublicCooperationPolicyTests().publication()
        for text in ("Я работаю по дропшиппингу.", "Ми працюємо по дропшипінгу.",
                "Я работаю не только по дропшиппингу.", "Я працюю не лише через дропшипінг."):
            with self.subTest(text=text):
                self.assertIsNotNone(capture_cooperation_policy(publication, [policy_fixture.source(text)]))
        for text in ('"Я работаю по дропшиппингу."', "Раньше я работал по дропшиппингу.",
                "Мой друг говорит, что я работаю по дропшиппингу.",
                "Я работаю не по дропшиппингу.", "Я працюю не через дропшипінг.",
                "Я работаю не по дропшиппингу, а по опту.",
                "Я працюю дизайнером, не через дропшипінг.", PERSONAL):
            with self.subTest(text=text):
                self.assertIsNone(capture_cooperation_policy(publication, [policy_fixture.source(text)]))

    def test_v5_russian_public_range_keeps_anti_automatic_qualifier_across_captured_locales(self):
        publication = policy_fixture.PublicCooperationPolicyTests().publication()
        for language in LANGUAGES:
            cap = capture_cooperation_policy(publication,
                [policy_fixture.source(policy_fixture.SOURCES[language])])
            with self.subTest(language=language):
                result = validate_reply_truth(V5_EIGHT_SALES,
                    context=ReplyTruthContext(cooperation_policy=cap))
                self.assertTrue(result.valid, result.reasons)
                self.assertEqual(public_statement_topic(V5_EIGHT_SALES.split(".")[0], cap), "partner_price")
        self.assertFalse(validate_reply_truth(V5_EIGHT_SALES, context=ReplyTruthContext()).valid)

    def test_public_range_qualifier_cannot_authorize_other_numbers_basis_or_embedded_claims(self):
        cap = capture_cooperation_policy(policy_fixture.PublicCooperationPolicyTests().publication(),
            [policy_fixture.source(policy_fixture.SOURCES["uk"])])
        base = V5_EIGHT_SALES.split(".")[0]
        for text in (base.replace("10–15%", "10–20%"), base.replace("полной цены", "цены"),
                base + " и ваша скидка гарантирована", base + " и худи стоит 850 UAH",
                base + " и отправим завтра", base + " и товар есть в наличии"):
            with self.subTest(text=text):
                self.assertFalse(public_statement_topic(text, cap))
                result = validate_reply_truth(text, context=ReplyTruthContext(cooperation_policy=cap))
                business = _business_approval_reason(SimpleNamespace(reply_text=text),
                    {"case_kind": "collaboration_review"}, cap)
                self.assertTrue(not result.valid or business)

    def test_v5_denied_offer_is_not_a_created_order_timing_promise_or_individual_approval(self):
        result = validate_reply_truth(V5_UNKNOWN_TERMS, context=ReplyTruthContext())
        self.assertTrue(result.valid, result.reasons)
        self.assertFalse(unreviewed_business_entitlement(V5_UNKNOWN_TERMS))
        self.assertEqual(_business_approval_reason(SimpleNamespace(reply_text=V5_UNKNOWN_TERMS),
            {"case_kind": "collaboration_review"}), "")
        for text in ("Your order is unconfirmed.", "We do not offer same-day dispatch.",
                "We cannot promise tomorrow dispatch for unpaid orders.",
                "We don't offer guaranteed 20% rates and same-day dispatch."):
            with self.subTest(text=text):
                self.assertTrue(validate_reply_truth(text, context=ReplyTruthContext()).valid)
                self.assertFalse(unreviewed_business_entitlement(text))
        # The new timing/entitlement scope grants no discount authority.
        self.assertIn("unverified_discount", validate_reply_truth(
            "We don't offer guaranteed 20% discounts and same-day dispatch.",
            context=ReplyTruthContext()).reasons)

    def test_denied_offer_does_not_hide_positive_claims_in_the_same_or_later_clause(self):
        cases = (
            ("Your order is confirmed.", "unverified_order"),
            ("We don't offer only 20% rates and we dispatch tomorrow.", "unverified_timing"),
            ("We don't offer guaranteed 20% rates, but same-day dispatch is guaranteed.", "unverified_timing"),
            ("We don't offer same-day dispatch and we deliver tomorrow.", "unverified_timing"),
            ("We dispatch tomorrow and don't offer same-day dispatch.", "unverified_timing"),
            ("It is not true that we don't offer same-day dispatch.", "unverified_timing"),
            ("We reject the claim that we do not offer tomorrow dispatch.", "unverified_timing"),
            ("We don't offer same-day dispatch, and your hoodie costs 850 UAH.", "unverified_price"),
            ("We don't offer same-day dispatch, and your discount is 20%.", "unverified_discount"),
            ("We don't offer same-day dispatch, and the hoodie is available.", "unverified_availability"),
            ("We don't offer same-day dispatch, and the hoodie is 100% cotton.", "unverified_product_material"),
        )
        for text, reason in cases:
            with self.subTest(text=text):
                self.assertIn(reason, validate_reply_truth(text, context=ReplyTruthContext()).reasons)
        for text in ("We don't offer guaranteed 20% rates, but your partnership is approved.",
                "We don't offer guaranteed 20% rates and your discount is guaranteed.",
                "Your wholesale terms are agreed individually with our team.",
                "Wholesale terms are agreed individually with our team and your discount is approved."):
            with self.subTest(text=text):
                self.assertEqual(_business_approval_reason(SimpleNamespace(reply_text=text),
                    {"case_kind": "collaboration_review"}), "response_plan_business_terms_unconfirmed")

    def test_all_localized_new_facts_keep_existing_exact_statement_truth_contract(self):
        publication = policy_fixture.PublicCooperationPolicyTests().publication()
        for language in LANGUAGES:
            cap = capture_cooperation_policy(publication,
                [policy_fixture.source(policy_fixture.SOURCES[language])])
            self.assertEqual(cap.metadata()["version"], "owner-2026-10-24.q23.2")
            for topic in ("monthly_sales", "wholesale", "delivery_terms", "settlement"):
                text = policy_fixture.sentence(language, topic)
                with self.subTest(language=language, topic=topic):
                    self.assertEqual(public_statement_topic(text, cap), topic)
                    result = validate_reply_truth(text, context=ReplyTruthContext(cooperation_policy=cap))
                    self.assertTrue(result.valid, result.reasons)
                    self.assertEqual(_business_approval_reason(SimpleNamespace(reply_text=text),
                        {"case_kind": "collaboration_review"}, cap), "")

    def test_old_q23_1_published_source_does_not_gain_q23_2_facts_from_new_code(self):
        publication = policy_fixture.PublicCooperationPolicyTests().publication()
        historical = deepcopy(publication.snapshot)
        historical["instructions"] = [next(row for row in historical["instructions"] if row["locale"] == "en")]
        row = historical["instructions"][0]
        # Exact q23.1 reviewed English body from publication 8, with its own
        # coherent historical source/body digests rather than current facts.
        row["body"] = OLD_EN_BODY
        row["reviewed_source"]["source_hash"] = sha256((OLD_EN_BODY + "\n").encode()).hexdigest()
        row["reviewed_source"]["body_hash"] = sha256(OLD_EN_BODY.encode()).hexdigest()
        row["reviewed_source"]["section_hash"] = row["reviewed_source"]["body_hash"]
        self.assertEqual(row["reviewed_source"]["body_hash"],
            "549cda98d47534e0300c943a037a815705d47f4a95b12727b4463b1865ff47f4")
        old = replace(publication, snapshot=historical, snapshot_hash=snapshot_hash(historical))
        self.assertIsNone(capture_cooperation_policy(old,
            [policy_fixture.source(policy_fixture.SOURCES["en"])]))
        self.assertEqual(_business_approval_reason(
            SimpleNamespace(reply_text=policy_fixture.sentence("en", "monthly_sales")),
            {"case_kind": "collaboration_review"}), "response_plan_business_terms_unconfirmed")

    def test_mixing_guard_rejects_assertions_but_preserves_questions_denials_and_review(self):
        positive = ("Розміри та принти можна мішати.", "Размеры и принты можно смешивать.",
            "You may mix sizes and prints.", "Sizes and prints can be mixed.")
        neutral = ("Чи можна змішати розміри та принти?", "Можно смешивать размеры и принты?",
            "Can sizes and prints be mixed?", "Розміри та принти не можна змішувати.",
            "Размеры и принты нельзя смешивать.", "You may not mix sizes and prints.",
            "Передам менеджеру питання, чи можна змішати розміри та принти.",
            "The team will check whether sizes and prints can be mixed.",
            "Можливість змішати розміри та принти потребує погодження менеджера.")
        for text in positive:
            with self.subTest(text=text):
                self.assertTrue(unreviewed_business_entitlement(text))
        for text in neutral:
            with self.subTest(text=text):
                self.assertFalse(unreviewed_business_entitlement(text))
