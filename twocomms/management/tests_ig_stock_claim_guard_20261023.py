"""Legacy prose cannot promote accounting stock to a customer availability fact."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from management.services import instagram_bot as bot


class PendingManagerStockGrammarTests(SimpleTestCase):
    positives = (
        'Що стосується наявності саме розміру M на складі, я зараз уточню у менеджера та одразу вам повідомлю!',
        'Щодо наявності худі M на складі, я зараз уточню у менеджера й одразу повідомлю вам.',
        'Наявність худі M перевірю з менеджером та дам вам знати.',
        'Что касается наличия M на складе, я сейчас уточню у менеджера и сразу вам сообщу.',
        'По наличию M уточню у менеджера.',
        'Regarding availability of this hoodie in size M, I will check with our manager and let you know.',
        'As for availability of M, I’ll check with our manager.',
        'I am checking availability of M with our manager.',
        'Щодо фізичної наявності худі M, я уточню з нашою командою та повідомлю вам.',
        'По физическому наличию худи M, я уточню с нашей командой и сразу вам сообщу.',
        'Regarding physical availability for M, I will check with our team and let you know.',
    )

    def test_topic_and_own_manager_check_share_one_nonassertive_predicate(self):
        from management.services.ig_reply_truth import _pending_manager_stock_check, _product_availability_claim
        for text in self.positives:
            with self.subTest(text=text):
                parsed = _pending_manager_stock_check(text)
                self.assertTrue(parsed)
                start, end = parsed['availability_span']
                self.assertTrue(text[start:end])
                self.assertFalse(_product_availability_claim(text))

    def test_internal_team_role_does_not_authorize_foreign_reported_or_firm_stock(self):
        from management.services.ig_reply_truth import _pending_manager_stock_check, validate_reply_truth, ReplyTruthContext
        positive = 'Regarding physical availability for M, I will check with our team and let you know.'
        for text in (positive.replace('our team', 'your team'), positive.replace('our team', 'their team'),
                positive.replace('I will', 'They will'), positive.replace('I will check', 'I checked'),
                positive.replace('I will check', 'I will not check'), 'If ' + positive,
                'Our team wrote: ' + positive, f'«{positive}»',
                positive.replace('let you know.', 'let you know that size M is in stock.'),
                positive.replace('physical availability', 'guaranteed physical availability'),
                positive.replace('physical availability', 'physical confirmed availability'),
                positive.replace('for M', 'for M in stock'), positive.rstrip('.') + ', size M is in stock.'):
            with self.subTest(text=text):
                self.assertFalse(_pending_manager_stock_check(text))
        for text in (positive + ' Size M is in stock.', positive + ' It arrives in 2 days.',
                     positive + ' The price is 1 UAH.'):
            with self.subTest(asserted=text):
                self.assertFalse(validate_reply_truth(text, context=ReplyTruthContext()).valid)

    def test_unknown_stock_predicates_are_never_product_labels_regardless_of_case(self):
        from management.services.ig_reply_truth import (_pending_manager_stock_check,
            validate_reply_truth, ReplyTruthContext)
        context = ReplyTruthContext(fulfillment_review_status='pending', source_chosen_sizes=('M',),
            source_chosen_colors=('black',), source_chosen_fits=('classic',))
        for predicate in ('стовідсоткова', 'СТОВІДСОТКОВА', 'Стовідсоткова',
                          'забезпечена', 'ЗАБЕЗПЕЧЕНА', 'Забезпечена'):
            for subject in ('Наявність худі M', 'Доступність худі розмір M', 'Наявність худі Restock Aurora M'):
                text = f'{subject} {predicate}, я уточню у менеджера.'
                with self.subTest(text=text):
                    self.assertFalse(_pending_manager_stock_check(text))
                    result = validate_reply_truth(text, context=context)
                    self.assertFalse(result.valid)
                    self.assertIn('unverified_availability', result.reasons)
        # A capitalized model-like span is insufficient proof of a label.
        self.assertFalse(_pending_manager_stock_check(
            'Щодо наявності худі Other Print у розмірі M, я уточню у менеджера.'))

    def test_fronted_topic_keeps_exact_source_choices_across_comma_in_all_languages(self):
        from management.services.ig_reply_truth import validate_reply_truth, ReplyTruthContext
        context = ReplyTruthContext(fulfillment_review_status='pending', allowed_sizes=('M', 'L'),
            allowed_colors=('black', 'white'), allowed_fits=('classic', 'oversize'),
            source_chosen_sizes=('M',), source_chosen_colors=('black',), source_chosen_fits=('classic',))
        pairs = (
            ('Щодо наявності size M на складі, я уточню у менеджера.', 'size M', 'size L'),
            ('Щодо наявності розміру M на складі, я уточню у менеджера.', 'розміру M', 'розміру L'),
            ('Щодо наявності худі M, я уточню у менеджера.', 'худі M', 'худі L'),
            ('Что касается наличия размера M, я уточню у менеджера.', 'размера M', 'размера L'),
            ('Regarding availability of this hoodie in size M, I will check with our manager.', 'size M', 'size L'),
            ('Щодо наявності color black, я уточню у менеджера.', 'color black', 'color white'),
            ('Щодо наявності худі у чорному кольорі, я уточню у менеджера.', 'чорному', 'білому'),
            ('По наличию худи в черном цвете, я уточню у менеджера.', 'черном', 'белом'),
            ('Regarding availability of black hoodie, I will check with our manager.', 'black', 'white'),
            ('Щодо наявності fit classic, я уточню у менеджера.', 'fit classic', 'fit oversize'),
        )
        for positive, selected, other in pairs:
            for comma in (True, False):
                source = positive if comma else positive.replace(',', '')
                with self.subTest(source=source):
                    self.assertTrue(validate_reply_truth(source, context=context).valid)
                    rejected = validate_reply_truth(source.replace(selected, other), context=context)
                    self.assertFalse(rejected.valid)
                    self.assertIn('configuration_mismatch', rejected.reasons)

    def test_explicit_nominal_stock_assurances_require_physical_authority(self):
        from management.services.ig_reply_truth import validate_reply_truth, ReplyTruthContext
        context = ReplyTruthContext(fulfillment_review_status='pending', source_chosen_sizes=('M',),
            source_chosen_colors=('black',), source_chosen_fits=('classic',))
        observed = (
            'Наявність худі Restock Aurora Стовідсоткова, я уточню у менеджера.',
            'Наличие худи M Полное, я уточню у менеджера.',
            'Availability of hoodie M Certain, I will check with our manager.',
            'Availability of hoodie M CERTAIN, I will check with our manager.',
        )
        for text in (*observed, 'Гарантована наявність худі Restock Aurora.',
                'Guaranteed availability of this hoodie.', 'Наличие худи полное.',
                'Наявність худі Restock Aurora 100%.'):
            with self.subTest(text=text):
                result = validate_reply_truth(text, context=context)
                self.assertFalse(result.valid)
                self.assertIn('unverified_availability', result.reasons)
        # Exact quoted customer evidence and conservative pending topics do
        # not become physical-stock assertions through this nominal grammar.
        quote = observed[0]
        self.assertTrue(validate_reply_truth(f'«{quote}»', context=ReplyTruthContext(
            quoted_data=(quote,), fulfillment_review_status='pending')).valid)
        for text in ('If availability of this hoodie is certain, I will check with our manager.',
                'I cannot confirm availability of this hoodie is certain.',
                'Availability of this hoodie is not certain.',
                'Availability of this hoodie is uncertain.',
                'If guaranteed availability of this hoodie, I will check with our manager.'):
            with self.subTest(nonassertion=text):
                self.assertTrue(validate_reply_truth(text, context=context).valid)
        pending_context = ReplyTruthContext(fulfillment_review_status='pending', allowed_sizes=('M',),
            allowed_colors=('black',), allowed_fits=('classic',), source_chosen_sizes=('M',),
            source_chosen_colors=('black',), source_chosen_fits=('classic',))
        for text in self.positives:
            with self.subTest(pending=text):
                self.assertTrue(validate_reply_truth(text, context=pending_context).valid)

    def test_quote_negation_history_conditional_and_different_check_object_are_not_own_checks(self):
        from management.services.ig_reply_truth import _pending_manager_stock_check
        for text in ('«Щодо наявності M, я уточню у менеджера»',
                'Щодо наявності M, я не уточню у менеджера.',
                'Менеджер написав: я уточню наявність M у менеджера.',
                'Щодо наявності M, я вчора уточнив у менеджера.',
                'Якщо я уточню наявність M у менеджера.',
                'If I check availability of M with our manager.',
                'Regarding availability of M, I will not check with our manager.',
                'Щодо наявності M, я уточню у менеджера час доставки.',
                'Regarding confirmed availability of M, I will check shipping time with the manager.',
                'Наявність M уточню.', 'Менеджер уточнить наявність M.'):
            with self.subTest(text=text):
                self.assertFalse(_pending_manager_stock_check(text))

    def test_a_pending_topic_never_hides_independent_stock_absence_wait_price_or_eta(self):
        from management.services.ig_reply_truth import (_pending_manager_stock_check,
            _product_availability_claim, validate_reply_truth, ReplyTruthContext)
        stock = ('Що стосується наявності M, я уточню у менеджера, але L є в наявності.',
            'Щодо наявності M, я уточню у менеджера, M уже є на складі.',
            'Regarding availability of M, I will check with our manager, and size M is in stock.',
            'По наличию M, я уточню у менеджера, но худи нет.',
            'По наличию M, я уточню у менеджера, но худи под заказ.',
            'Щодо наявності M, я уточню у менеджера та повідомлю, що M є в наявності.',
            'Наявність розміру M стовідсоткова, я уточню у менеджера.',
            'Доступність розміру M забезпечена, я уточню у менеджера.',
            'Підтверджена наявність M, я уточню у менеджера.')
        for text in stock:
            with self.subTest(text=text):
                self.assertFalse(_pending_manager_stock_check(text))
                self.assertTrue(_product_availability_claim(text))
                self.assertFalse(validate_reply_truth(text, context=ReplyTruthContext()).valid)
        self.assertFalse(_pending_manager_stock_check('Regarding availability of size M in stock, I will check with our manager.'))
        for text in ('Щодо наявності M, я уточню у менеджера. Ціна — 1 грн.',
                'Regarding availability of M, I will check with our manager. It will arrive in 2 days.'):
            with self.subTest(text=text):
                self.assertFalse(validate_reply_truth(text, context=ReplyTruthContext()).valid)


def reviewed_projection(*, availability="available", quantity=1, checked=1, status="reviewed"):
    return {
        "version": 1, "status": status, "request_digest": "verified-current-purchase",
        "current_request": {"scope": "cart", "lines": [{
            "line_id": "a", "label": "Black hoodie L",
            "target": {"product_id": 11, "color_variant_id": 12, "size": "L",
                "fit_option_code": "oversize", "option_values": {"fit": "oversize"}, "quantity": quantity},
            "observation": {"labels": {"product": "Hoodie", "color": "Black", "fit": "Oversize"}},
        }]},
        "decision": {"request_digest": "verified-current-purchase", "lines": [{
            "line_id": "a", "availability": availability, "checked_quantity": checked,
            "supply_basis": "warehouse_checked" if availability == "available" else "incoming_confirmed",
        }]},
    }


class LegacyStockClaimGuardTests(SimpleTestCase):
    def setUp(self):
        self.client_row = SimpleNamespace(pk=4, current_product_id=11, current_size="L", current_qty=1,
            sales_context={"availability": "available", "manager_approved": True})
        self.control = {"product_id": 11, "color_variant_id": 12, "size": "L", "fit": "oversize",
            "qty": 1, "show_products": "11"}

    def failures(self, text, projection=None, **kwargs):
        with patch("management.services.ig_fulfillment_reviews.fulfillment_review_projection",
                   return_value=projection or {"version": 1, "status": "none"}):
            return bot._authoritative_reply_claim_failures(self.client_row, text, self.control, **kwargs)

    def test_positive_ledger_and_catalog_offer_never_authorize_stock(self):
        with (patch("management.services.ig_availability.resolve_allocation") as allocation,
              patch("management.services.ig_offer_resolver.resolve_offer") as offer):
            for observed in ("allocatable", "in_stock", "made_to_order", "unavailable", "unknown"):
                with self.subTest(observed=observed):
                    allocation.return_value = SimpleNamespace(status=observed)
                    offer.return_value = SimpleNamespace(status=observed)
                    self.assertEqual(self.failures("Black hoodie L is in stock."), ("stock",))
            allocation.assert_not_called()
            offer.assert_not_called()

    def test_raw_sales_context_and_model_manager_flags_grant_nothing(self):
        self.control.update(manager_approved=True, stock=99, availability="available")
        self.assertEqual(self.failures("Футболка є."), ("stock",))
        self.assertFalse(bot._has_exact_stock_evidence(self.client_row, self.control))

    def test_current_exact_manager_fact_authorizes_only_matching_item(self):
        projection = reviewed_projection()
        self.assertEqual(self.failures("Black hoodie L is in stock.", projection), ())
        for text in ("White hoodie L is in stock.", "Black hoodie M is in stock.",
                     "Black hoodie L, classic fit, is in stock.", "2 Black hoodies L are in stock."):
            with self.subTest(text=text):
                self.assertEqual(self.failures(text, projection), ("stock",))

    def test_pending_stale_and_mismatched_decision_do_not_authorize(self):
        for status in ("none", "pending", "stale"):
            with self.subTest(status=status):
                self.assertEqual(self.failures("Black hoodie L is in stock.", reviewed_projection(status=status)), ("stock",))
        projection = reviewed_projection()
        projection["decision"]["request_digest"] = "different-purchase"
        self.assertEqual(self.failures("Black hoodie L is in stock.", projection), ("stock",))

    def test_quantity_bool_or_insufficient_checked_supply_is_not_positive_proof(self):
        for checked in (None, True, 0):
            with self.subTest(checked=checked):
                self.assertEqual(self.failures("Black hoodie L is in stock.", reviewed_projection(checked=checked)), ("stock",))
        projection = reviewed_projection(quantity=2, checked=1)
        self.assertEqual(self.failures("Black hoodie L is in stock.", projection), ("stock",))

    def test_catalog_presentation_and_photos_are_not_physical_stock(self):
        for text in ("Here is our hoodie catalog.", "Є фото худі oversize.",
                     "Є фото футболки.", "У каталозі є футболки.",
                     "Which hoodie size are you interested in?", "I will check availability with our manager."):
            with self.subTest(text=text):
                self.assertEqual(self.failures(text), ())

    def test_exact_customer_quotation_is_data_and_does_not_authorize_neighbor_claim(self):
        quote = "Black hoodie L is in stock"
        text = f'You wrote: "{quote}". I will check with our manager.'
        self.assertEqual(self.failures(text, quoted_data=(quote,)), ())
        self.assertEqual(self.failures(text + " Black hoodie L is in stock.", quoted_data=(quote,)), ("stock",))

    def test_rewrite_preserves_helpful_catalog_sentence_and_checks_manager(self):
        text = "Here are our hoodies in the catalog. Black hoodie L is in stock."
        rewritten = bot._reply_without_unproven_claims(text, ("stock",), locale="en")
        self.assertIn("Here are our hoodies in the catalog.", rewritten)
        self.assertNotIn("is in stock", rewritten)
        self.assertIn("check", rewritten)
        self.assertIn("manager", rewritten)

    def test_evidence_read_failure_is_not_stock_authority(self):
        with patch("management.services.ig_fulfillment_reviews.fulfillment_review_projection", side_effect=RuntimeError("unavailable")):
            self.assertFalse(bot._has_exact_stock_evidence(self.client_row, self.control, reply="Black hoodie L is in stock."))

    def test_ordinary_reply_avoids_review_read_and_price_facts_are_preserved(self):
        from decimal import Decimal
        from management.services.ig_reply_truth import ReplyTruthContext
        context = ReplyTruthContext(authorized_prices=(Decimal("950"),), payment_confirmed=True)
        with patch("management.services.ig_fulfillment_reviews.fulfillment_review_projection", return_value=reviewed_projection()) as read:
            self.assertIs(bot._stock_reply_truth_context(self.client_row, "Here is the catalog.", context), context)
            read.assert_not_called()
            checked = bot._stock_reply_truth_context(self.client_row, "Black hoodie L is in stock.", context)
        self.assertEqual(checked.authorized_prices, context.authorized_prices)
        self.assertTrue(checked.payment_confirmed)
        self.assertTrue(checked.availability_authorities)

    def test_every_stock_boundary_revalidates_and_drops_expired_review(self):
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
        text = "Black hoodie L is in stock."
        with patch("management.services.ig_fulfillment_reviews.fulfillment_review_projection",
                   side_effect=[reviewed_projection(), {"version": 1, "status": "stale"}]) as read:
            first = bot._stock_reply_truth_context(self.client_row, text, ReplyTruthContext())
            self.assertTrue(validate_reply_truth(text, context=first).valid)
            final = bot._stock_reply_truth_context(self.client_row, text, first)
            self.assertFalse(validate_reply_truth(text, context=final).valid)
            self.assertEqual(read.call_count, 2)


from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
                   GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeLegacyStockAuthorityTests(_RestockFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(
            username="legacy-stock-reviewer", email="legacy-stock@example.test", password="test")

    def review_case(self):
        from management.tests_ig_fulfillment_reviews import NativeFulfillmentReviewTests
        return NativeFulfillmentReviewTests.case(self)

    def decide(self, request, **changes):
        from management.tests_ig_fulfillment_reviews import NativeFulfillmentReviewTests
        return NativeFulfillmentReviewTests.decide(self, request, **changes)

    def test_real_signed_manager_accessible_supply_authorizes_zero_accounted_stock(self):
        request, _revision = self.review_case()
        self.assertTrue(self.decide(request)["ok"])
        sentence = request.envelope["lines"][0]["label"] + " is in stock."
        self.assertTrue(bot._has_exact_stock_evidence(self.client_row, {}, reply=sentence))
        self.assertEqual(self.variant.stock, 0)
        self.assertEqual(self.rule.stock, 0)

    def test_positive_accounting_without_a_manager_review_is_not_stock_proof(self):
        self.variant.stock = self.rule.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        self.assertEqual(bot._authoritative_reply_claim_failures(
            self.client_row, "This product is in stock.", {"product_id": self.product.pk}), ("stock",))

    def test_exact_signed_review_loses_authority_after_customer_changes_selection(self):
        request, _revision = self.review_case()
        self.assertTrue(self.decide(request)["ok"])
        sentence = request.envelope["lines"][0]["label"] + " is in stock."
        self.assertTrue(bot._has_exact_stock_evidence(self.client_row, {}, reply=sentence))
        self.canonical(f"добавьте {self.product.title}, количество 2, размер M, цвет black, крій classic")
        self.assertFalse(bot._has_exact_stock_evidence(self.client_row, {}, reply=sentence))


class StockPolarityAndBaseArrivalTests(SimpleTestCase):
    def context(self, *, availability="available", base_arrival=None):
        from management.services.ig_reply_authority import fulfillment_availability_authorities, fulfillment_timing_authorities
        from management.services.ig_reply_truth import ReplyTruthContext
        data = reviewed_projection(availability=availability)
        if base_arrival is not None:
            data["decision"]["lines"][0]["base_arrival"] = base_arrival
        status, timing = fulfillment_timing_authorities(data)
        return ReplyTruthContext(fulfillment_review_status=status, timing_authorities=timing,
            availability_authorities=fulfillment_availability_authorities(data))

    def check(self, text, context, expected):
        from management.services.ig_reply_truth import validate_reply_truth
        result = validate_reply_truth(text, context=context)
        self.assertEqual(result.valid, expected, (text, result.reasons))
        return result

    def test_raw_catalog_sizes_authorize_neither_presence_absence_nor_wait(self):
        from management.services.ig_reply_truth import ReplyTruthContext
        for text in ("Black hoodie L is in stock.", "Black hoodie L is out of stock.",
                     "It is out of stock.", "Немає в наявності.", "Нет в наличии.",
                     "Black hoodie L is not available.", "Black hoodie L немає в наявності.",
                     "Black hoodie L нет в наличии.", "Black hoodie L is on backorder.",
                     "Black hoodie L потрібно чекати на надходження."):
            with self.subTest(text=text):
                result = self.check(text, ReplyTruthContext(allowed_sizes=("L",)), False)
                self.assertIn("unverified_availability", result.reasons)

    def test_exact_manager_negative_fact_does_not_authorize_positive_or_wait(self):
        context = self.context(availability="unavailable")
        for text in ("Black hoodie L is out of stock.", "Black hoodie L is not available.",
                     "Black hoodie L немає в наявності.", "Black hoodie L нет в наличии."):
            with self.subTest(text=text):
                self.check(text, context, True)
        for text in ("Black hoodie L is in stock.", "White hoodie L is out of stock.",
                     "Black hoodie M is out of stock.", "Black hoodie L is on backorder."):
            with self.subTest(text=text):
                self.check(text, context, False)

    def test_waiting_is_a_distinct_exact_manager_decision(self):
        context = self.context(availability="wait")
        self.check("Black hoodie L is on backorder.", context, True)
        self.check("Black hoodie L потрібно чекати на надходження.", context, True)
        self.check("Black hoodie L is in stock.", context, False)
        self.check("Black hoodie L is out of stock.", context, False)
        self.check("White hoodie L is on backorder.", context, False)

    def test_legacy_guard_removes_unproved_negative_and_wait_clauses(self):
        client = SimpleNamespace(pk=4)
        for text in ("Black hoodie L is out of stock.", "Black hoodie L is on backorder."):
            with self.subTest(text=text), patch("management.services.ig_fulfillment_reviews.fulfillment_review_projection", return_value={"status": "none"}):
                self.assertEqual(bot._authoritative_reply_claim_failures(client, text, {}), ("stock",))
                rewritten = bot._reply_without_unproven_claims(text, ("stock",), locale="en")
                self.assertNotIn(text, rewritten)
                self.assertIn("manager", rewritten)
        with patch.object(bot, "_assisted_checkout_locale", return_value="en"):
            self.assertIn("manager", bot._response_validation_fallback(client, reasons=("unverified_availability",)))

    def test_source_quotation_grants_no_authority_to_an_independent_clause(self):
        from dataclasses import replace
        from management.services.ig_reply_truth import ReplyTruthContext
        quote = "Black hoodie L is out of stock"
        context = replace(ReplyTruthContext(), quoted_data=(quote,))
        self.check(f'You wrote "{quote}". I will check with our manager.', context, True)
        self.check(f'You wrote "{quote}". Black hoodie L is in stock.', context, False)
        self.check('You wrote hello; Black hoodie L is in stock.', context, False)

    def test_catalog_titles_photos_and_unknown_measurements_are_not_stock_absence(self):
        from management.services.ig_reply_truth import ReplyTruthContext
        for text in ('Футболка «Правил немає» — перегляньте цей принт.',
                     'Hoodie "No Rules" is in our catalog.',
                     'Є фото футболки.', 'У каталозі є футболка «Правил немає».',
                     'Немає фото regular.', 'Розмір M: немає фото regular.',
                     'Худі L: немає підтверджених замірів.',
                     'Худі L: немає підтвердженої ваги виробу.',
                     'You asked: "Is size M in stock?"'):
            with self.subTest(text=text):
                result = self.check(text, ReplyTruthContext(allowed_sizes=("M",), allowed_fits=("classic",)), True)
                self.assertNotIn("unverified_availability", result.reasons)
        for text in ('Розмір "M" недоступний.',
                     'Футболка «Правил немає» не в наявності.',
                     'Немає фото regular, але розмір M недоступний.'):
            with self.subTest(text=text):
                self.assertIn("unverified_availability", self.check(text, ReplyTruthContext(), False).reasons)

    def arrival(self):
        return {"min_days": 7, "max_days": 7, "unit": "calendar", "anchor_at": "2026-10-23T10:00:00+03:00"}

    def test_base_arrival_is_exact_wait_incoming_checked_clock_only(self):
        context = self.context(availability="wait", base_arrival=self.arrival())
        self.assertEqual([fact.domain for fact in context.timing_authorities], ["base_arrival"])
        for text in ("Black hoodie L: base arrival in 7 days.",
                     "Black hoodie L: прибуття основи за 7 днів.",
                     "Black hoodie L: основи прийдуть через 7 днів.",
                     "Black hoodie L: поступление основы через 7 дней."):
            with self.subTest(text=text):
                self.check(text, context, True)
        for text in ("Base arrival in 7 days.", "White hoodie L: base arrival in 7 days.",
                     "Black hoodie L: base arrival in 8 days.",
                     "Black hoodie L: base arrival in 7 days after payment.",
                     "Black hoodie L: base arrival next week.",
                     "Black hoodie L: base arrival in 7 days starting 24 October."):
            with self.subTest(text=text):
                self.check(text, context, False)

    def test_base_arrival_never_proves_ready_dispatch_preparation_or_customer_arrival(self):
        context = self.context(availability="wait", base_arrival=self.arrival())
        for text in ("Black hoodie L is in stock.", "Black hoodie L: dispatch in 7 days.",
                     "Black hoodie L: preparation takes 7 days.", "Black hoodie L will arrive in 7 days.",
                     "Black hoodie L: base arrival and customer delivery in 7 days.",
                     "Black hoodie L: base arrival in 7 days; we dispatch in 7 days."):
            with self.subTest(text=text):
                self.check(text, context, False)

    def test_no_legacy_duration_or_unanchored_fact_can_authorize_base_arrival(self):
        from management.services.ig_reply_truth import ReplyTruthContext, TimingAuthority
        text = "Black hoodie L: base arrival in 7 days."
        self.check(text, ReplyTruthContext(approved_timing_claims=("in 7 days",)), False)
        fact = TimingAuthority(domain="base_arrival", min_days=7, max_days=7, line_id="a", line_label="Black hoodie L")
        self.check(text, ReplyTruthContext(timing_authorities=(fact,)), False)
        for changes in ({"anchor_at": "2026-10-23T10:00:00"}, {"min_days": True},
                        {"anchor": "after_payment_confirmation"}, {"max_days": 91}):
            with self.subTest(changes=changes):
                context = self.context(availability="wait", base_arrival={**self.arrival(), **changes})
                self.check(text, context, False)


class LegacyStockReviewObligationTests(TestCase):
    def setUp(self):
        from management.tests_ig_live_reply_priority import StructuredWorkerAuthorityBoundaryTests
        StructuredWorkerAuthorityBoundaryTests.setUp(self)

    def client_row(self):
        from management.tests_ig_live_reply_priority import StructuredWorkerAuthorityBoundaryTests
        return StructuredWorkerAuthorityBoundaryTests._client(self, "stock-review-obligation")

    def run_reply(self, client, payload, *, suffix, text):
        from management.tests_ig_live_reply_priority import StructuredWorkerAuthorityBoundaryTests
        return StructuredWorkerAuthorityBoundaryTests._run(self, client, payload, suffix=suffix, text=text)

    def test_rejection_keeps_one_source_linked_manager_notification_and_q2_can_get_price(self):
        from management.models import IgBotNotification
        from storefront.models import Category, Product, ProductStatus
        client = self.client_row()
        product = Product.objects.create(title="Legacy review shirt", slug="legacy-review-shirt",
            category=Category.objects.create(name="Review shirts", slug="review-shirts"),
            price=900, status=ProductStatus.PUBLISHED)
        client.current_product = product
        client.save(update_fields=["current_product", "updated_at"])
        real_escalate, real_notify = bot._escalate_manager_for_row, bot.notify_manager
        def queued_escalation(row):
            def queue_only(text, **kwargs):
                return real_notify(text, **{**kwargs, "deliver_immediately": False})
            with patch.object(bot, "notify_manager", side_effect=queue_only):
                return real_escalate(row)
        with patch.object(bot, "_escalate_manager_for_row", side_effect=queued_escalation) as escalate:
            source, handled, delivered = self.run_reply(client,
                {"reply_text": "Цей товар є в наявності.", "controls": []},
                suffix="stock-review-required", text="Ця футболка є в наявності?")
            self.assertEqual(handled, 1)
            self.assertIn("менеджер", delivered)
            self.assertNotIn("уточню", delivered)
            escalate.assert_called_once()
        notification = IgBotNotification.objects.get(client=client, event_type="escalation")
        self.assertTrue(notification.payload["requires_human_review"])
        self.assertIn(f"Повідомлення ID: {source.pk}", notification.payload["text"])
        client.refresh_from_db()
        self.assertEqual(client.stage, client.Stage.LEAD_TO_MANAGER)
        self.assertFalse(client.bot_paused)
        self.assertFalse(client.manager_takeover)
        with patch.object(bot, "_escalate_manager_for_row") as second_escalation:
            _question, handled, delivered = self.run_reply(client,
                {"reply_text": "Ціна цієї футболки — 900 грн.", "controls": []},
                suffix="stock-review-price-q2", text="Яка ціна цієї футболки?")
        self.assertEqual(handled, 1)
        self.assertIn("900", delivered)
        second_escalation.assert_not_called()
        self.assertEqual(IgBotNotification.objects.filter(client=client, event_type="escalation").count(), 1)

    def test_final_boundary_rejection_also_preserves_server_review_obligation(self):
        client = self.client_row()
        with (patch.object(bot, "finalize_paylink", return_value="Цей товар є в наявності."),
              patch.object(bot, "_escalate_manager_for_row") as escalate):
            _source, handled, delivered = self.run_reply(client,
                {"reply_text": "Дякую за уточнення.", "controls": []},
                suffix="stock-review-final-boundary", text="Підкажіть щодо цієї футболки.")
        self.assertEqual(handled, 1)
        self.assertIn("менеджер", delivered)
        self.assertNotIn("є в наявності", delivered)
        escalate.assert_called_once()

    def test_customer_stock_quotation_does_not_mint_a_review_obligation(self):
        client = self.client_row()
        quote = "Товар є в наявності"
        reply = f"Ви написали: «{quote}». Дякую за уточнення."
        with patch.object(bot, "_escalate_manager_for_row") as escalate:
            _source, handled, delivered = self.run_reply(client,
                {"reply_text": reply, "controls": []}, suffix="stock-source-quotation", text=quote)
        self.assertEqual(handled, 1)
        self.assertEqual(delivered, reply)
        escalate.assert_not_called()


class SourceOwnedStockCheckWordingTests(SimpleTestCase):
    def check(self, text, *, size="M", color="black", fit="classic", valid=True):
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
        context = ReplyTruthContext(fulfillment_review_status="pending",
            source_chosen_sizes=(size,) if size else (),
            source_chosen_colors=(color,) if color else (),
            source_chosen_fits=(fit,) if fit else ())
        result = validate_reply_truth(text, context=context)
        self.assertEqual(result.valid, valid, (text, result.reasons))
        return result

    def test_check_may_repeat_only_captured_source_configuration(self):
        for text in (
            "I will check the availability of this hoodie in size M with our manager.",
            "I cannot confirm availability of this hoodie in size M yet.",
            "Не можу підтвердити наявність худі розмір M.",
            "Не могу подтвердить наличие худи размер M.",
            "Наявність худі розмір M перевірю з менеджером.",
            "Наличие худи размер M уточню у менеджера.",
            "I will check availability of size M color black fit classic with our manager.",
        ):
            with self.subTest(text=text):
                self.check(text)
        self.check("I will check availability of this hoodie in size L with our manager.", valid=False)
        self.check("I will check availability of color white with our manager.", valid=False)
        self.check("I will check availability of fit oversize with our manager.", valid=False)
        self.check("I will check availability of size M with our manager.", size="", valid=False)

    def test_check_never_grants_presence_absence_wait_or_unrelated_clause_values(self):
        for text in (
            "Size M is in stock.", "Size M is unavailable.", "Size M is on backorder.",
            "I will check availability of size M, but size L is in stock.",
            "I will check availability of size M and it is in stock.",
            "I will check availability of size M and we have size M in stock.",
            "I will check availability; color white is selected.",
            "The manager checked and confirmed size M is in stock.",
        ):
            with self.subTest(text=text):
                self.check(text, valid=False)

    def test_catalog_options_cannot_replace_the_source_value_inside_a_check(self):
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
        context = ReplyTruthContext(fulfillment_review_status="pending", source_chosen_sizes=("M",),
            source_chosen_colors=("black",), source_chosen_fits=("classic",),
            allowed_sizes=("L",), allowed_colors=("white",), allowed_fits=("oversize",))
        for text in ("I will check availability of size L with our manager.",
                     "I will check availability of color white with our manager.",
                     "I will check availability of fit oversize with our manager."):
            with self.subTest(text=text):
                self.assertIn("configuration_mismatch", validate_reply_truth(text, context=context).reasons)

    def test_bare_palette_description_is_not_a_concrete_color_value(self):
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
        for text in ("The color palette is shown on the product page.",
                     "The color palette is displayed below."):
            with self.subTest(text=text):
                self.assertTrue(validate_reply_truth(text, context=ReplyTruthContext()).valid)
        for text in ("Color: palette.", "Selected color palette is shown on the product page.",
                     "The color palette is shown; color white is selected."):
            with self.subTest(text=text):
                self.assertIn("configuration_mismatch", validate_reply_truth(text, context=ReplyTruthContext()).reasons)


from django.test import TransactionTestCase
from management import tests_ig_revision_live as native_live


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_TURN_CONTEXT_MODE="unified",
    IG_CLIENT_STATE_PROMPT_TOKENS=20000,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeSourceOwnedStockCheckTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True
    _generate = native_live.RevisionLiveTests._generate
    _execute = native_live.RevisionLiveTests._execute

    def setUp(self):
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests
        from storefront.models import Category
        original_catalog = SourceCartCatalogTests.setUp
        def hoodie_catalog(owner):
            original_catalog(owner)
            category, _ = Category.objects.get_or_create(slug="hoodies", defaults={"name": "Худі"})
            owner.products[0].category = category
            owner.products[0].save(update_fields=["category"])
        # Define the fictional catalog before the first signed source history;
        # changing applicability afterward would rightly invalidate that proof.
        with patch.object(SourceCartCatalogTests, "setUp", hoodie_catalog):
            super().setUp()
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        self.publish_reviewed_policy()
        self.canonical("Хочу худі Restock Aurora, кількість 1, розмір M, колір black, крій classic")
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count = 0
        self.actual_hash_override = "observed"
        self.provider_failure = False

    def publish_reviewed_policy(self):
        from management.knowledge.scenarios import SCENARIO_INSTRUCTIONS, reviewed_scenario_values
        from management.services.ig_policy_publication import draft_state, save_instruction_draft, publish_instruction_policy
        from management.services.ig_revision_outbox import PublicationBinding
        state = draft_state()
        for row in SCENARIO_INSTRUCTIONS:
            _, state = save_instruction_draft(expected_revision=state.revision,
                expected_snapshot_hash=state.snapshot_hash, values=reviewed_scenario_values(row["key"]),
                note="source-owned stock checking regression fixture")
        result = publish_instruction_policy(expected_draft_revision=state.revision,
            expected_draft_hash=state.snapshot_hash, expected_head_id=self.publication.pk,
            expected_head_hash=self.publication.snapshot_hash, note="source-owned stock checking regression fixture")
        self.publication = result.publication
        self.binding = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        self.settings.refresh_from_db()

    def execute_candidate(self, stock, text):
        self.variant.stock = self.rule.stock = stock
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        source = self.source("Is this hoodie in size M in stock?")
        revision, token = _RestockFixture.revision(self, source)
        self.revision, self.token = revision, token
        self.parsed = {"reply_text": text, "controls": [{"kind": "manager", "value": True}]}
        with patch("management.services.ig_presence.start_presence", return_value=None):
            result, model, http = self._execute()
        self.revision.refresh_from_db()
        return result, model, http

    def assert_delivered_check(self, stock, *, exact_title=False):
        from management.models import IgFollowUpTask, IgBotNotification
        subject = "Restock Aurora" if exact_title else "this hoodie"
        result, model, http = self.execute_candidate(stock,
            f"I will check the availability of {subject} in size M with our manager.")
        coverage = self.revision.action_receipts.get("response_coverage") or {}
        self.assertEqual(result.state, "completed", (result.reasons, coverage))
        self.assertEqual((model.call_count, http.call_count), (1, 1))
        self.assertEqual(self.revision.delivery_effects.filter(state="sent").count(), 1)
        self.assertFalse(coverage.get("remaining"), coverage)
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.customer, reason="fulfillment_review").count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(client=self.customer, event_type="fulfillment_review").count(), 1)
        self.client_row.refresh_from_db()
        self.assertFalse(self.client_row.bot_paused)
        self.assertFalse(self.client_row.manager_takeover)

    def test_positive_ledger_source_owned_check_delivers_once(self):
        self.assert_delivered_check(100)

    def test_zero_ledger_source_owned_check_delivers_once(self):
        self.assert_delivered_check(0)

    def test_named_positive_ledger_source_owned_check_delivers_once(self):
        self.assert_delivered_check(100, exact_title=True)

    def test_named_zero_ledger_source_owned_check_delivers_once(self):
        self.assert_delivered_check(0, exact_title=True)

    def test_wrong_source_size_stays_blocked(self):
        result, _model, http = self.execute_candidate(100,
            "I will check the availability of this hoodie in size L with our manager.")
        self.assertNotEqual(result.state, "completed")
        http.assert_not_called()

    def test_raw_positive_stock_fact_stays_blocked(self):
        result, _model, http = self.execute_candidate(100, "This hoodie in size M is in stock.")
        self.assertNotEqual(result.state, "completed")
        http.assert_not_called()

    def test_raw_zero_negative_stock_fact_stays_blocked(self):
        result, _model, http = self.execute_candidate(0, "This hoodie in size M is not available.")
        self.assertNotEqual(result.state, "completed")
        http.assert_not_called()
