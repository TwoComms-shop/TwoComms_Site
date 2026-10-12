"""Fulfilment timing domains, bound review authority and safe negative prose."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from management.services.ig_reply_authority import (
    _fulfillment_timing_truth, apply_fulfillment_timing_context,
    build_reply_truth_context, fulfillment_availability_authorities, fulfillment_timing_authorities,
)
from management.services.ig_reply_truth import ReplyTruthContext, TimingAuthority, validate_reply_truth


def projection(*, status="reviewed", windows=((7, 10),), domain="preparation"):
    labels = ("Black hoodie L", "White shirt M")
    return {
        "version": 1, "status": status, "request_digest": "source-cart-digest",
        "current_request": {"scope": "cart", "lines": [
            {"line_id": str(index), "label": labels[index]}
            for index in range(len(windows))
        ]},
        "decision": {"request_digest": "source-cart-digest", "lines": [
            {"line_id": str(index), "availability": "wait", domain: {
                "min_days": low, "max_days": high, "unit": "calendar",
                "anchor_at": "2026-10-12T10:00:00+03:00",
            }} for index, (low, high) in enumerate(windows)
        ]},
    }


class FulfillmentTruthTests(SimpleTestCase):
    def check(self, text, context, *, valid):
        result = validate_reply_truth(text, context=context)
        self.assertEqual(result.valid, valid, (text, result.reasons))
        if not valid:
            self.assertIn("unverified_timing", result.reasons)

    def reviewed_context(self, **kwargs):
        status, facts = fulfillment_timing_authorities(projection(**kwargs))
        return ReplyTruthContext(timing_authorities=facts, fulfillment_review_status=status)

    def test_pending_exact_cart_suppresses_even_qualified_ordinary_dispatch(self):
        context = ReplyTruthContext(
            fulfillment_review_status="pending",
            explicitly_qualified_standard_dispatch_days=(1, 3),
            approved_timing_claims=("1–3 дні", "1–3 дня", "1–3 days"),
        )
        for text in (
            "Зазвичай підготовка до відправлення займає 1–3 дні після оплати.",
            "Обычно отправка занимает 1–3 дня после оплаты.",
            "Usually we dispatch within 1–3 days after payment.",
        ):
            with self.subTest(text=text):
                self.check(text, context, valid=False)

    def test_manager_preparation_does_not_authorize_dispatch_or_arrival(self):
        context = self.reviewed_context()
        for text in (
            "Підготовка займає 7–10 днів.",
            "Подготовка занимает 7–10 дней.",
            "Preparation takes 7–10 days.",
        ):
            with self.subTest(text=text):
                self.check(text, context, valid=True)
        for text in (
            "Відправимо за 7–10 днів.", "Отправим за 7–10 дней.",
            "We dispatch in 7–10 days.", "Отримаєте за 7–10 днів.",
            "Получите за 7–10 дней.", "Your order will arrive in 7–10 days.",
            "Зазвичай відправимо за 1–3 дні після оплати.",
        ):
            with self.subTest(text=text):
                self.check(text, context, valid=False)

    def test_explicit_preparation_start_cannot_shift_manager_anchor_day(self):
        context = self.reviewed_context()
        for text in (
            "Preparation takes 7–10 days starting 20 October.",
            "Preparation takes 7–10 days, starting October 20, 2026.",
            "Підготовка займає 7–10 днів починаючи з 20 жовтня.",
            "Підготовка займає 7–10 днів від 20.10.2026.",
            "Подготовка занимает 7–10 дней начиная с 20 октября.",
            "Подготовка занимает 7–10 дней с 2026-10-20.",
            "Preparation starts on 20 October.",
        ):
            with self.subTest(text=text):
                self.check(text, context, valid=False)

    def test_same_source_anchor_and_omitted_anchor_remain_supported(self):
        context = self.reviewed_context()
        for text in (
            "Preparation takes 7–10 days starting 12 October.",
            "Preparation takes 7–10 days, starting October 12, 2026.",
            "Підготовка займає 7–10 днів починаючи з 12 жовтня 2026.",
            "Підготовка займає 7–10 днів від 12.10.2026.",
            "Подготовка занимает 7–10 дней начиная с 12 октября.",
            "Подготовка занимает 7–10 дней с 2026-10-12.",
            "Preparation takes 7–10 days.",
        ):
            with self.subTest(text=text):
                self.check(text, context, valid=True)

    def test_incomplete_relative_invalid_or_wrong_year_start_does_not_guess(self):
        context = self.reviewed_context()
        for text in (
            "Preparation takes 7–10 days starting 12.",
            "Preparation takes 7–10 days starting tomorrow.",
            "Preparation takes 7–10 days starting 12 October 2027.",
            "Preparation takes 7–10 days, starting October 12, 2027.",
            "Підготовка займає 7–10 днів, починаючи з 12 жовтня, 2027.",
            "Подготовка занимает 7–10 дней с 12.10.27.",
            "Підготовка займає 7–10 днів від 31.02.2026.",
            "Preparation takes 7–10 days starting 12 Foober.",
        ):
            with self.subTest(text=text):
                self.check(text, context, valid=False)

    def test_start_uses_actual_kyiv_day_not_utc_day_or_current_clock(self):
        context = self.reviewed_context()
        facts = tuple(replace(fact, anchor_at="2026-10-11T22:00:00Z")
            for fact in context.timing_authorities)
        context = replace(context, timing_authorities=facts)
        self.check("Preparation takes 7–10 days starting 12 October.", context, valid=True)
        self.check("Preparation takes 7–10 days starting 11 October.", context, valid=False)

    def test_duration_only_authority_cannot_supply_an_explicit_start_date(self):
        fact = TimingAuthority(domain="preparation", min_days=7, max_days=10)
        context = ReplyTruthContext(timing_authorities=(fact,))
        self.check("Preparation takes 7–10 days.", context, valid=True)
        self.check("Preparation takes 7–10 days starting 12 October.", context, valid=False)

    def test_negated_historical_or_question_start_dates_are_not_new_claims(self):
        context = self.reviewed_context()
        for text in (
            "Does preparation take 7–10 days starting 20 October?",
            "Previously the manager said preparation takes 7–10 days starting 20 October.",
            "We cannot confirm preparation starts on 20 October.",
            "Раніше менеджер казав: підготовка 7–10 днів від 20 жовтня.",
        ):
            with self.subTest(text=text):
                self.check(text, context, valid=True)

    def test_start_anchor_is_scoped_per_named_line_in_mixed_cart(self):
        context = self.reviewed_context(windows=((7, 10), (1, 3)))
        facts = tuple(replace(fact, anchor_at="2026-10-20T10:00:00+03:00")
            if fact.line_id == "1" else fact for fact in context.timing_authorities)
        context = replace(context, timing_authorities=facts)
        self.check("Black hoodie L preparation takes 7–10 days starting 20 October.", context, valid=False)
        self.check("Black hoodie L preparation takes 7–10 days starting 12 October.", context, valid=True)
        self.check("White shirt M preparation takes 1–3 days starting 20 October.", context, valid=True)

    def test_timing_clause_cannot_hide_wrong_start_after_correct_duration(self):
        context = self.reviewed_context()
        self.check("Preparation takes 7–10 days, starting 20 October.", context, valid=False)
        self.check("Preparation takes 7–10 days, starting 12 October.", context, valid=True)

    def test_bare_feasibility_guarantees_need_explicit_domain_proof(self):
        context = self.reviewed_context()
        for text in ("Встигнемо!", "Успеем!", "We will make it!", "We'll make it!"):
            with self.subTest(text=text):
                self.check(text, context, valid=False)
        authority = TimingAuthority(domain="arrival_feasibility", claim="We will make it!")
        self.check("We will make it!", replace(context, timing_authorities=(authority,)), valid=True)
        self.check("We will make it by 15 October.", replace(context, timing_authorities=(authority,)), valid=False)

    def test_city_and_standard_policy_do_not_prove_same_day_arrival(self):
        context = ReplyTruthContext(explicitly_qualified_standard_dispatch_days=(1, 3))
        for text in (
            "Ви у Харкові, тому отримаєте сьогодні.",
            "Вы в Харькове, поэтому получите сегодня.",
            "You are in Kharkiv, so your order will arrive today.",
            "У Харкові можна забрати сьогодні.",
        ):
            with self.subTest(text=text):
                self.check(text, context, valid=False)

    def test_numeric_arrival_deadline_is_never_derived_from_preparation(self):
        for text in (
            "Отримаєте до 15 жовтня.", "Получите до 15.10.",
            "The order will arrive by 15 October.",
            "Всё будет к 15 октября.", "Все буде до 15 числа.",
            "It will be ready by 15 October.", "Все буде до 15.",
        ):
            with self.subTest(text=text):
                self.check(text, self.reviewed_context(), valid=False)

    def test_unqualified_arrival_guarantee_with_no_number_is_blocked(self):
        for text in (
            "Гарантуємо доставку вчасно.", "Гарантируем доставку вовремя.",
            "We guarantee delivery on time.",
        ):
            with self.subTest(text=text):
                self.check(text, ReplyTruthContext(), valid=False)

    def test_questions_negations_historical_attribution_and_review_are_safe(self):
        context = self.reviewed_context()
        for text in (
            "Чи встигнемо?", "Успеем?", "Will we make it?",
            "Не встигнемо.", "Не успеем.", "We cannot guarantee delivery by 15 October.",
            "Не можемо гарантувати, що встигнемо до 15 жовтня.",
            "Не можем гарантировать доставку за 7–10 дней.",
            "Раніше менеджер казав: підготовка 1–3 дні.",
            "Раньше менеджер говорил: доставка 7–10 дней.",
            "Previously the manager said delivery takes 2 days.",
            "Уточню, чи встигнемо.", "Уточню, успеем ли мы.",
            "I will check whether we will make it.",
        ):
            with self.subTest(text=text):
                self.check(text, context, valid=True)

    def test_negative_or_historical_first_clause_cannot_hide_new_promise(self):
        for text in (
            "Не гарантую, але встигнемо!",
            "Раньше менеджер говорил про подготовку, но получите до 15 октября.",
            "We cannot guarantee delivery; we will make it!",
            "Успеем, но ничего не гарантируем.",
        ):
            with self.subTest(text=text):
                self.check(text, self.reviewed_context(), valid=False)

    def test_carrier_variability_guidance_without_numeric_facts_is_safe(self):
        for text in (
            "Срок перевозки зависит от перевозчика и маршрута; дату прибытия нужно уточнить отдельно.",
            "Термін перевезення залежить від перевізника та маршруту; дату прибуття уточнимо окремо.",
            "Carrier transit varies by route; we need to check arrival separately.",
        ):
            with self.subTest(text=text):
                self.check(text, self.reviewed_context(), valid=True)

    def test_carrier_estimate_requires_own_authority_and_qualifier(self):
        context = ReplyTruthContext(timing_authorities=(TimingAuthority(
            domain="carrier_estimate", min_days=2, max_days=4,
        ),))
        self.check("Carrier delivery usually takes 2–4 days.", context, valid=True)
        self.check("Carrier delivery takes 2–4 days.", context, valid=False)
        self.check("Your order will arrive in 2–4 days.", context, valid=False)

    def test_ordinary_and_legacy_authority_remain_valid_outside_review(self):
        context = ReplyTruthContext(explicitly_qualified_standard_dispatch_days=(1, 3))
        self.check("Зазвичай підготовка до відправлення займає 1–3 дні після оплати.", context, valid=True)
        self.check("Usually we dispatch in 1–3 days after payment.", context, valid=True)
        legacy = ReplyTruthContext(approved_timing_claims=("2 дні", "завтра"))
        self.check("Доставка 2 дні. Відправимо завтра.", legacy, valid=True)
        self.check("Отримаєте завтра.", legacy, valid=False)
        self.check("Успеем!", legacy, valid=False)

    def test_business_days_are_not_calendar_days(self):
        data = projection()
        data["decision"]["lines"][0]["preparation"]["unit"] = "business"
        status, facts = fulfillment_timing_authorities(data)
        context = ReplyTruthContext(timing_authorities=facts, fulfillment_review_status=status)
        self.check("Подготовка занимает 7–10 рабочих дней.", context, valid=True)
        self.check("Preparation takes 7–10 business days.", context, valid=True)
        self.check("Підготовка займає 7–10 днів.", context, valid=False)

    def test_one_line_does_not_authorize_whole_cart(self):
        context = self.reviewed_context(windows=((7, 10), (1, 3)))
        self.check("Preparation takes 7–10 days.", context, valid=False)
        self.check("Black hoodie L preparation takes 7–10 days.", context, valid=True)
        self.check("White shirt M preparation takes 7–10 days.", context, valid=False)
        self.check("White shirt M preparation takes 1–3 days.", context, valid=True)
        self.check("Black hoodie L and White shirt M preparation takes 7–10 days.", context, valid=False)

    def test_uniform_cart_window_allows_generic_preparation(self):
        context = self.reviewed_context(windows=((7, 10), (7, 10)))
        self.check("Preparation takes 7–10 days.", context, valid=True)

    def test_dispatch_has_its_own_domain(self):
        context = self.reviewed_context(domain="dispatch", windows=((1, 3),))
        self.check("We dispatch in 1–3 days.", context, valid=True)
        self.check("Preparation takes 1–3 days.", context, valid=False)
        self.check("Your order will arrive in 1–3 days.", context, valid=False)

    def test_mixed_sentence_does_not_promote_preparation_to_arrival(self):
        self.check("Preparation takes 7–10 days, arrival takes 7–10 days.", self.reviewed_context(), valid=False)

    def test_stale_digest_missing_lines_and_unavailable_items_grant_no_timing(self):
        for change in ("stale", "digest", "missing_line", "unavailable", "bad_unit", "bad_number"):
            data = projection()
            if change == "stale":
                data["status"] = "stale"
            elif change == "digest":
                data["decision"]["request_digest"] = "obsolete-digest"
            elif change == "missing_line":
                data["decision"]["lines"] = []
            elif change == "unavailable":
                data["decision"]["lines"][0]["availability"] = "unavailable"
            elif change == "bad_unit":
                data["decision"]["lines"][0]["preparation"]["unit"] = "hours"
            else:
                data["decision"]["lines"][0]["preparation"]["min_days"] = True
            with self.subTest(change=change):
                status, facts = fulfillment_timing_authorities(data)
                self.assertIn(status, ("pending", "reviewed"))
                self.assertFalse(facts)

    def test_standing_restock_does_not_change_unrelated_purchase_timing(self):
        data = projection(status="pending")
        data["current_request"]["scope"] = "restock"
        self.assertEqual(fulfillment_timing_authorities(data), ("", ()))

    def test_only_frozen_confirmed_slot_can_grant_manager_timing(self):
        frame = {"status": "captured", "slots": {"fulfillment.review": {
            "status": "confirmed", "value": projection(),
        }}}
        status, facts = _fulfillment_timing_truth(frame)
        self.assertEqual(status, "reviewed")
        self.assertTrue(facts)
        for slot_status in ("unknown", "proposed", "stale"):
            data = deepcopy(frame)
            data["slots"]["fulfillment.review"]["status"] = slot_status
            self.assertEqual(_fulfillment_timing_truth(data), ("", ()))
        self.assertEqual(_fulfillment_timing_truth({"status": "unavailable", "fulfillment_review": projection()}), ("", ()))

    def test_authority_builder_uses_captured_review_without_new_projection_query(self):
        frame = {"status": "captured", "fulfillment_review": projection(status="pending")}
        with (
            patch("management.services.ig_reply_authority._current_episode", return_value=None),
            patch("management.services.ig_reply_authority._current_configuration_pricing", return_value=(None, False)),
            patch("management.services.ig_reply_authority._server_urls", return_value=((), (), ())),
            patch("management.services.ig_reply_authority._conversation_facts", return_value=((), ())),
        ):
            context = build_reply_truth_context(SimpleNamespace(pk=1), captured_client_state=frame)
        self.assertEqual(context.fulfillment_review_status, "pending")
        self.assertIsNone(context.explicitly_qualified_standard_dispatch_days)
        self.assertIn("source_bound_fulfillment_pending", context.evidence_codes)

    def test_frozen_overlay_replaces_ordinary_dispatch_without_mutating_context(self):
        context = ReplyTruthContext(explicitly_qualified_standard_dispatch_days=(1, 3))
        frame = {"status": "captured", "fulfillment_review": projection()}
        reviewed = apply_fulfillment_timing_context(context, frame)
        self.assertEqual(context.explicitly_qualified_standard_dispatch_days, (1, 3))
        self.assertIsNone(reviewed.explicitly_qualified_standard_dispatch_days)
        self.check("Preparation takes 7–10 days.", reviewed, valid=True)
        self.check("Usually we dispatch in 1–3 days after payment.", reviewed, valid=False)

    def test_current_scoped_unknown_review_holds_timing_without_granting_facts(self):
        context = ReplyTruthContext(explicitly_qualified_standard_dispatch_days=(1, 3))
        frame = {"status": "captured", "diagnostics": {"fulfillment_timing_hold": True},
            "slots": {"fulfillment.review": {"status": "unknown", "value": projection()}}}
        pending = apply_fulfillment_timing_context(context, frame)
        self.assertEqual(pending.fulfillment_review_status, "pending")
        self.assertFalse(pending.timing_authorities)
        self.check("Usually we dispatch in 1–3 days after payment.", pending, valid=False)
        self.check("Preparation takes 7–10 days.", pending, valid=False)

    def test_old_scope_unknown_review_without_hold_preserves_new_purchase_policy(self):
        context = ReplyTruthContext(explicitly_qualified_standard_dispatch_days=(1, 3))
        frame = {"status": "captured", "diagnostics": {}, "slots": {
            "fulfillment.review": {"status": "unknown", "value": projection()}}}
        unaffected = apply_fulfillment_timing_context(context, frame)
        self.assertIs(unaffected, context)
        self.check("Usually we dispatch in 1–3 days after payment.", unaffected, valid=True)


def availability_projection(*, second=False, second_status="wait", supply_basis="accessible_unrecorded", quantity=1, checked=1):
    data = projection(windows=((7, 10), (1, 3)) if second else ((7, 10),))
    for index, line in enumerate(data["current_request"]["lines"]):
        name = "Hoodie Alpha" if index == 0 else "Hoodie Beta"
        line.update(label=name + " · black · M · oversize · × " + str(quantity), target={
            "product_id": 101 + index, "color_variant_id": 201 + index, "size": "M",
            "fit_option_code": "oversize", "option_values": {"fit": "oversize"}, "quantity": quantity,
        }, observation={"status": "unavailable", "labels": {"product": name, "color": "black", "fit": "oversize"},
            "evidence": {"free_stock": 0}})
        data["decision"]["lines"][index].update(availability="available" if index == 0 else second_status,
            checked_quantity=checked, supply_basis=supply_basis, source_location="external supplier")
    return data


class FulfillmentAvailabilityTests(SimpleTestCase):
    def context(self, data=None, **kwargs):
        return apply_fulfillment_timing_context(ReplyTruthContext(**kwargs), {
            "status": "captured", "slots": {"fulfillment.review": {
                "status": "confirmed", "value": data if data is not None else availability_projection(),
            }},
        })

    def valid(self, text, context=None):
        result = validate_reply_truth(text, context=context or self.context())
        self.assertTrue(result.valid, (text, result.reasons))

    def rejected(self, text, context=None, reason="unverified_availability"):
        result = validate_reply_truth(text, context=context or self.context())
        self.assertFalse(result.valid, text)
        self.assertIn(reason, result.reasons, (text, result.reasons))

    def test_accessible_unrecorded_manager_count_overrides_db_zero_for_dialogue(self):
        self.valid("Hoodie Alpha black M is in stock.")

    def test_warehouse_checked_manager_count_also_grants_dialogue(self):
        self.valid("Hoodie Alpha black M is in stock.", self.context(availability_projection(supply_basis="warehouse_checked")))

    def test_uk_manager_confirmation_of_single_current_size(self):
        self.valid("Менеджер підтвердив: розмір M у наявності.")
        self.valid("Hoodie Alpha чорний M у наявності.")

    def test_ru_manager_confirmation_of_single_current_size(self):
        self.valid("Менеджер подтвердил: размер M в наличии.")
        self.valid("Hoodie Alpha черный M в наличии.")

    def test_en_manager_confirmation_of_single_current_size(self):
        self.valid("The manager confirmed size M is in stock.")

    def test_matching_color_and_fit_labels_are_not_added_to_global_catalog(self):
        context = self.context()
        self.valid("Size M, fit oversize, color black: in stock.", context)
        self.assertEqual(context.allowed_sizes, ())
        self.assertEqual(context.allowed_fits, ())
        self.assertEqual(context.allowed_colors, ())

    def test_wrong_size_is_not_authorized(self):
        self.rejected("Hoodie Alpha black size L is in stock.")

    def test_wrong_fit_is_not_authorized(self):
        self.rejected("Hoodie Alpha black M, fit regular, is in stock.")

    def test_wrong_color_is_not_authorized(self):
        self.rejected("Hoodie Alpha white M is in stock.")

    def test_single_line_does_not_authorize_another_product_with_same_size(self):
        self.rejected("Hoodie Beta black M is in stock.")

    def test_same_size_different_product_waits(self):
        context = self.context(availability_projection(second=True))
        self.valid("Hoodie Alpha black M is in stock.", context)
        self.rejected("Hoodie Beta black M is in stock.", context)

    def test_multiline_generic_size_cannot_promote_other_waiting_line(self):
        self.rejected("Size M is in stock.", self.context(availability_projection(second=True)))

    def test_whole_cart_assertion_is_rejected_when_one_line_waits(self):
        self.rejected("All selected items are in stock.", self.context(availability_projection(second=True)))

    def test_whole_cart_assertion_requires_every_exact_line_available(self):
        self.valid("All selected items are in stock.", self.context(availability_projection(second=True, second_status="available")))

    def test_combined_named_products_cannot_hide_waiting_line(self):
        self.rejected("Hoodie Alpha black M and Hoodie Beta black M are in stock.", self.context(availability_projection(second=True)))

    def test_manager_quantity_must_cover_exact_requested_count(self):
        self.rejected("Size M is in stock.", self.context(availability_projection(quantity=2, checked=1)))

    def test_boolean_quantity_is_not_manager_count_proof(self):
        self.rejected("Size M is in stock.", self.context(availability_projection(checked=True)))

    def test_incoming_confirmed_cannot_authorize_available(self):
        self.rejected("Size M is in stock.", self.context(availability_projection(supply_basis="incoming_confirmed")))

    def test_more_than_requested_quantity_is_not_authorized_by_line_fact(self):
        self.valid("Size M: 2 pieces in stock.", self.context(availability_projection(quantity=2, checked=3)))
        self.rejected("Size M: 3 pieces in stock.", self.context(availability_projection(quantity=2, checked=3)))

    def test_source_cache_stock_boolean_does_not_grant_availability(self):
        data = availability_projection()
        data["status"] = "pending"
        data["current_request"]["lines"][0]["observation"]["status"] = "available"
        data["current_request"]["lines"][0]["observation"]["stock_available"] = True
        self.rejected("Size M is in stock.", self.context(data))

    def test_ordinary_catalog_case_remains_valid(self):
        self.valid("Size M is in stock.", ReplyTruthContext(allowed_sizes=("M",)))

    def test_unknown_or_stale_manager_slot_grants_no_dialogue_fact(self):
        for status in ("unknown", "stale"):
            context = apply_fulfillment_timing_context(ReplyTruthContext(), {
                "status": "captured", "slots": {"fulfillment.review": {"status": status, "value": availability_projection()}}})
            self.rejected("Size M is in stock.", context)

    def test_available_dialogue_never_grants_price_order_payment_or_paylink(self):
        from management.services.ig_reply_truth import ProposedAction
        context = self.context()
        self.assertEqual(context.authorized_prices, ())
        self.assertEqual(context.authorized_actions, ())
        for text, reason in (("Price is 900 грн.", "unverified_price"),
                ("Order created.", "unverified_order"), ("Payment confirmed.", "unverified_payment")):
            self.rejected(text, context, reason)
        result = validate_reply_truth("Hoodie Alpha black M is in stock.", context=context,
            actions=(ProposedAction("paylink", "full"),))
        self.assertIn("unauthorized_action", result.reasons)

    def test_same_product_and_size_with_other_variant_is_not_global_stock(self):
        data = availability_projection(second=True)
        data["current_request"]["lines"][1]["target"]["product_id"] = 101
        data["current_request"]["lines"][1]["target"]["color_variant_id"] = 999
        data["current_request"]["lines"][1]["observation"]["labels"] = {"product": "Hoodie Alpha", "color": "white", "fit": "oversize"}
        data["current_request"]["lines"][1]["label"] = "Hoodie Alpha · white · M · oversize · × 1"
        self.valid("Hoodie Alpha black M is in stock.", self.context(data))
        self.rejected("Hoodie Alpha white M is in stock.", self.context(data))

    def test_same_configuration_different_options_requires_exact_line(self):
        data = availability_projection(second=True)
        second = data["current_request"]["lines"][1]
        second["target"].update(product_id=101, color_variant_id=201, option_values={"fit": "oversize", "print": "beta"})
        second["observation"]["labels"]["product"] = "Hoodie Alpha"
        second["label"] = "Hoodie Alpha · black · M · oversize · print beta · × 1"
        self.rejected("Hoodie Alpha black M is in stock.", self.context(data))
        self.valid("Line 0: size M in stock.", self.context(data))

    def test_target_and_decision_digest_changes_remove_manager_authority(self):
        data = availability_projection()
        data["decision"]["request_digest"] = "other-cart"
        self.assertFalse(fulfillment_availability_authorities(data))
        self.rejected("Size M is in stock.", self.context(data))

    def test_recruitment_availability_uses_recruitment_authority(self):
        context = ReplyTruthContext(recruitment_status="open")
        self.valid("Job vacancies are available.", context)
        self.rejected("Job vacancies are available.", ReplyTruthContext(), "unverified_recruitment")

    def test_recruitment_authority_does_not_hide_product_availability_in_same_sentence(self):
        context = ReplyTruthContext(recruitment_status="open")
        self.rejected("Job vacancies are available; size M is in stock.", context)

    def test_actual_restock_invite_copy_is_a_check_not_available_stock(self):
        from management.services.ig_restock_subscriptions import _copy
        for locale in ("uk", "ru", "en"):
            with self.subTest(locale=locale):
                self.valid(_copy(locale, "invite", "Hoodie Alpha · black · M · oversize · × 1"), ReplyTruthContext())

    def test_actual_restock_acceptance_and_review_copy_do_not_confirm_stock(self):
        from management.services.ig_restock_subscriptions import _copy
        for locale in ("uk", "ru", "en"):
            for kind in ("accepted", "review"):
                with self.subTest(locale=locale, kind=kind):
                    self.valid(_copy(locale, kind, "Hoodie Alpha · black · M · oversize · × 1"), ReplyTruthContext())

    def test_restock_invitation_does_not_hide_appended_available_assertion(self):
        from management.services.ig_restock_subscriptions import _copy
        for locale, claim in (("uk", "Розмір M у наявності."),
                ("ru", "Размер M в наличии."), ("en", "Size M is in stock.")):
            with self.subTest(locale=locale):
                self.rejected(_copy(locale, "invite", "Hoodie Alpha · black · M") + "\n" + claim,
                    ReplyTruthContext())

    def test_hypothetical_stock_clauses_do_not_hide_separate_assertions(self):
        context = ReplyTruthContext(allowed_sizes=("M",), fulfillment_review_status="pending")
        for hypothetical, assertion in (
            ("Якщо розмір M у наявності, повідомимо.", "Розмір M у наявності."),
            ("Если размер M в наличии, сообщим.", "Размер M в наличии."),
            ("If size M is in stock, we will notify you.", "Size M is in stock."),
        ):
            with self.subTest(hypothetical=hypothetical):
                self.valid(hypothetical, context)
                self.rejected(hypothetical + " " + assertion, context)

    def test_affirmative_checked_confirmation_still_requires_exact_manager_proof(self):
        for text in ("Менеджер перевірив розмір M і підтвердив наявність.",
                "Менеджер проверил размер M и подтвердил наличие.",
                "The manager checked and confirmed size M is in stock."):
            with self.subTest(text=text):
                self.rejected(text, ReplyTruthContext())
                self.valid(text, self.context())
