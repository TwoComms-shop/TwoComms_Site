"""An explicit unanswered feasibility question survives ordinary purchase assent."""
from datetime import timedelta
from unittest.mock import patch
from management.models import IgRevisionDeliveryEffect

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management.models import IgCheckoutProposal, IgCheckoutAccessToken, IgBotNotification
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_revision_checkout as checkout
from management.services import ig_revision_live as live
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_fulfillment_question_debt import (
    explicit_feasibility_question, capture_fulfillment_question_debt, _own_release,
)
from management.tests_ig_restock_subscriptions import _RestockFixture
from management import tests_ig_revision_live as live_fixture


class FeasibilityQuestionSemanticsTests(SimpleTestCase):
    def test_explicit_question_is_distinct_from_soft_preference_or_reported_words(self):
        from management.services.ig_fulfillment_requirements import extract_requirements
        from datetime import datetime, timezone
        now = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
        for text in ("Встигнете відправити до 20.10.2026?", "Успеете отправить до 20.10.2026?", "Can you ship by 2026-10-20?",
                "Встигнете відправити до 20.10.2026", "Успеете отправить до 20.10.2026",
                'Встигнете відправити худі “Restock Aurora” до 20.10.2026?'):
            self.assertTrue(explicit_feasibility_question(extract_requirements(text, provider_timestamp=now, now=now)), text)
        for text in ("Хочу отримати до 20.10.2026.", 'Він написав «Встигнете відправити до 20.10.2026?»', "Раніше питав, чи встигнете відправити до 20.10.2026.", '“Встигнете відправити до 20.10.2026?”'):
            self.assertFalse(explicit_feasibility_question(extract_requirements(text, provider_timestamp=now, now=now)), text)

    def test_own_release_requires_actual_withdrawal_or_acceptance_of_uncertainty(self):
        for text in ("Дата відправки неважлива, оформлюйте.", "Погоджуюсь оформити без гарантії дати.", "I accept uncertain shipping timing."):
            self.assertTrue(_own_release(text), text)
        for text in ("Беру.", "Дякую.", "Дата важлива.", "Не погоджуюсь оформити без гарантії дати.", 'Він сказав: «Дата неважлива».', "Чи можна оформити без гарантії дати?",
                "Погоджуюсь на ціну. Дата відправки не підтверджена.", "I accept the price. The shipping date is uncertain."):
            self.assertFalse(_own_release(text), text)
        self.assertFalse(_own_release("Не зовсім згоден оформлювати без гарантії дати."))


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_TURN_CONTEXT_MODE="unified",
    IG_CLIENT_STATE_PROMPT_TOKENS=20000, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeFeasibilityQuestionTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True
    _generate = live_fixture.RevisionLiveTests._generate

    def setUp(self):
        super().setUp()
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count = 0
        self.actual_hash_override = "observed"
        self.provider_failure = False

    def prepare_question(self, text="Встигнете відправити до 20.10.2026?"):
        source = self.source(text)
        revision, _ = _RestockFixture.revision(self, source)
        prepared = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(prepared["ready"], prepared)
        self.request = reviews.Request.objects.get(pk=prepared["request_id"])
        self.assertTrue(reviews._verified(self.request, "request"))
        return self.request

    def stock_and_buy(self, text="Беру. Оформіть замовлення.", *, stock=5, expected_reason="inventory_available"):
        self.variant.stock = self.rule.stock = stock
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock", "updated_at"])
        inquiry = self.source("Ця позиція є в наявності?")
        revision, _ = _RestockFixture.revision(self, inquiry)
        result = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertEqual(result["reason"], expected_reason, result)
        self.buyer = self.source(text)
        self.revision, self.token = _RestockFixture.revision(self, self.buyer)

    def bundle_question(self, *texts):
        from management.models import IgCustomerTurn, IgTurnMessage
        from management.services.ig_turn_revisions import (
            create_collecting_revision, claim_revision_preparation, seal_revision, claim_sealed_revision,
        )
        rows = [self.source(text) for text in texts]
        turn = IgCustomerTurn.objects.create(client=self.client_row, primary_source_message=rows[0],
            window_started_at=self.now, window_deadline=self.now)
        for ordinal, row in enumerate(rows, 1):
            IgTurnMessage.objects.create(turn=turn, message=row, ordinal=ordinal, role="user")
        collected = create_collecting_revision(turn, rows, now=self.now, bypass_quiet=True)
        prepared = claim_revision_preparation(collected.revision.pk, now=self.now)
        sealed = seal_revision(collected.revision.pk, prepared.token, now=self.now)
        claimed = claim_sealed_revision(sealed.revision.pk, now=self.now)
        self.revision, self.token, self.buyer = claimed.revision, claimed.token, rows[-1]
        result = reviews.prepare_fulfillment_review(self.revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(result["ready"], result)
        self.request = reviews.Request.objects.get(pk=result["request_id"])
        self.assertEqual(self.request.source_id, rows[-1].pk)
        self.assertEqual(self.request.envelope["customer_requirements"][0]["source_id"], rows[0].pk)
        self.variant.stock = self.rule.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock", "updated_at"])
        return rows

    def execute(self, reply, *, paylink=False):
        self.parsed = {"reply_text": reply, "controls": [{"kind": "paylink", "value": "full"}] if paylink else []}
        validations = []
        original = live.RevisionGenerationBoundary.validate
        def validate(boundary, response, **kwargs):
            result = original(boundary, response, **kwargs)
            validations.append((result.valid, tuple(boundary.last_reasons)))
            return result
        with (patch.object(live.RevisionGenerationBoundary, "validate", validate),
            patch.object(checkout, "create_or_update_proposal", wraps=checkout.create_or_update_proposal) as factory,
            patch.object(checkout, "prepare_revision_checkout", wraps=checkout.prepare_revision_checkout) as prepare,
            patch("management.services.ig_presence.start_presence", return_value=None)):
            result, generated, http = live_fixture.RevisionLiveTests._execute(self)
        return result, validations, factory.call_count, prepare.call_count, http.call_count

    def test_original_full_native_offer_is_rejected_before_factory_and_token(self):
        self.prepare_question(getattr(self, "question_text", "Встигнете відправити до 20.10.2026?"))
        self.stock_and_buy()
        result, validations, factories, prepares, sends = self.execute(
            "Restock Aurora чорний розмір M є в наявності. Щодо бажаної дати відправки менеджер ще уточнює можливість. Перевірте деталі оформлення:", paylink=True)
        self.assertNotEqual(result.state, "completed")
        self.assertIn((False, ("checkout_fulfillment_question_unanswered",)), validations)
        self.assertEqual((factories, prepares, sends), (0, 0, 0))
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (0, 0))

    def test_direct_legacy_invoice_gate_cannot_issue_link_for_unanswered_date(self):
        from management.services import instagram_bot as bot
        self.prepare_question()
        self.stock_and_buy()
        with (patch.object(bot, "notify_manager"),
            patch("management.services.bot_orders.create_deal_and_link") as factory):
            bot.finalize_paylink("Оформлюємо замовлення, ось посилання на оплату.",
                {"paylink": "full", "product": self.product.pk, "size": "M", "qty": 1},
                self.client_row, self.client_row.igsid, trigger_text=self.buyer.text)
        factory.assert_not_called()
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (0, 0))

    def test_real_staff_recount_private_proposal_survives_but_customer_offer_stays_blocked(self):
        import uuid
        from django.contrib.auth import get_user_model
        from django.db import transaction
        from management.services.ig_fulfillment_checkout import prepare_fulfillment_checkout
        self.canonical("Хочу купити")
        request = self.prepare_question()
        manager = get_user_model().objects.create_superuser(username="private-date-stock-manager", password="test")
        decision = reviews.apply_fulfillment_decision(request.pk, actor=manager,
            expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(), now=self.now,
            decisions_by_line=[{"line_id": line["line_id"], "availability": "available", "checked_quantity": 1,
                "supply_basis": "accessible_unrecorded"} for line in request.envelope["lines"]])
        self.assertTrue(decision["ready"], decision)
        with transaction.atomic():
            type(self.variant).objects.filter(pk=self.variant.pk).update(stock=1)
            type(self.rule).objects.filter(pk=self.rule.pk).update(stock=1, updated_at=self.now)
            prepared = prepare_fulfillment_checkout(request, reviews.Decision.objects.get(pk=decision["decision_id"]), actor=manager, now=self.now)
        self.assertEqual(prepared["status"], "proposal_ready", prepared)
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (1, 0))
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.stock, 1)
        from management.services.ig_revision_authority import build_revision_authority_bindings, CLAIM_CATALOG_CONFIGURATION
        from management.services.ig_revision_checkout import checkout_authority_control
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        normalized, reasons = checkout_authority_control(self.client_row, {}, source_cart_capture=cart)
        self.assertFalse(reasons, reasons)
        for control in (normalized, {"items": normalized["items"]}):
            binding = build_revision_authority_bindings(self.client_row, claims=(CLAIM_CATALOG_CONFIGURATION,),
                control=control, server_authorized_actions=("checkout_proposal_create",))
            self.assertFalse(binding.ready)
            self.assertIn("checkout_fulfillment_question_unanswered", binding.reasons)
        self.stock_and_buy("Беру. Оформіть замовлення.", stock=1,
            expected_reason="inventory_available")
        result, validations, factories, prepares, sends = self.execute("Перевірте деталі оформлення:", paylink=True)
        self.assertNotEqual(result.state, "completed", (result.reasons, validations, factories, prepares, sends,
            IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()))
        self.assertIn((False, ("checkout_fulfillment_question_unanswered",)), validations)
        self.assertEqual((factories, prepares, sends), (0, 0, 0))
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (1, 0))
        _RestockFixture.revision(self, self.source("Дата відправки неважлива."))
        self.buyer = self.source("Беру. Оформіть замовлення.")
        self.revision, self.token = _RestockFixture.revision(self, self.buyer)
        result, _, factories, prepares, sends = self.execute("Перевірте деталі оформлення:", paylink=True)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, sends), (1, 1, 1))
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (1, 1))
        from management.models import IgCheckoutInventoryReservation
        holds = IgCheckoutInventoryReservation.objects.filter(state="active")
        self.assertEqual(holds.count(), 1)
        self.assertEqual(holds.get().quantity, 1)
        self.assertEqual(reviews.Request.objects.count(), 1, list(reviews.Request.objects.values(
            "source_id", "task_id", "envelope")))

    def test_unpunctuated_ukrainian_question_blocks_actual_native_offer(self):
        self.question_text = "Встигнете відправити до 20.10.2026"
        self.test_original_full_native_offer_is_rejected_before_factory_and_token()

    def test_unpunctuated_russian_question_blocks_actual_native_offer(self):
        self.question_text = "Успеете отправить до 20.10.2026"
        self.test_original_full_native_offer_is_rejected_before_factory_and_token()

    def test_own_question_with_quoted_product_title_blocks_actual_native_offer(self):
        self.question_text = 'Встигнете відправити худі “Restock Aurora” до 20.10.2026?'
        self.test_original_full_native_offer_is_rejected_before_factory_and_token()

    def test_two_source_question_then_plain_assent_blocks_actual_native_offer(self):
        rows = self.bundle_question("Встигнете відправити до 20.10.2026?", "Беру. Оформіть замовлення.")
        self.assertEqual(self.debt()["source_message_ids"], [rows[0].pk])
        result, validations, factories, prepares, sends = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertNotEqual(result.state, "completed")
        self.assertIn((False, ("checkout_fulfillment_question_unanswered",)), validations)
        self.assertEqual((factories, prepares, sends, IgCheckoutAccessToken.objects.count()), (0, 0, 0, 0))

    def test_same_bundle_own_withdrawal_after_question_allows_actual_offer(self):
        self.bundle_question("Встигнете відправити до 20.10.2026?", "Дата відправки неважлива.", "Беру. Оформіть замовлення.")
        self.assertEqual(self.debt(), {})
        result, _, factories, prepares, _ = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, IgCheckoutAccessToken.objects.count()), (1, 1, 1))

    def test_same_bundle_own_uncertainty_acceptance_after_question_allows_actual_offer(self):
        self.bundle_question("Встигнете відправити до 20.10.2026?", "Погоджуюсь оформити без гарантії дати.", "Беру. Оформіть замовлення.")
        self.assertEqual(self.debt(), {})
        result, _, factories, prepares, _ = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, IgCheckoutAccessToken.objects.count()), (1, 1, 1))

    def test_same_bundle_renewed_question_after_withdrawal_blocks_actual_offer(self):
        rows = self.bundle_question("Встигнете відправити до 20.10.2026?", "Дата відправки неважлива.",
            "Встигнете відправити до 21.10.2026?", "Беру. Оформіть замовлення.")
        self.assertIn(rows[2].pk, self.debt()["source_message_ids"])
        result, validations, factories, prepares, sends = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertNotEqual(result.state, "completed")
        self.assertIn((False, ("checkout_fulfillment_question_unanswered",)), validations)
        self.assertEqual((factories, prepares, sends, IgCheckoutAccessToken.objects.count()), (0, 0, 0, 0))

    @override_settings(IG_CLIENT_STATE_PROMPT_TOKENS=2400)
    def test_helpful_pending_date_clarification_sends_and_keeps_existing_case(self):
        request = self.prepare_question()
        self.stock_and_buy()
        result, validations, factories, prepares, sends = self.execute(
            "Restock Aurora чорний розмір M є в наявності. Можливість відправки до бажаної дати менеджер ще уточнює. Чи критична ця дата для вас?")
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, sends), (0, 0, 1))
        self.assertTrue(validations[-1][0], validations)
        self.assertEqual(reviews.Request.objects.count(), 1)
        self.assertEqual(reviews.Request.objects.get().pk, request.pk)
        self.assertEqual(IgBotNotification.objects.filter(dedupe_key=f"ig-fulfillment-review:{request.pk}").count(), 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts["response_coverage"]["disposition"], "complete")
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (0, 0))

    def test_no_date_control_creates_actual_single_proposal_and_token(self):
        self.stock_and_buy()
        result, _, factories, prepares, sends = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, sends), (1, 1, 1))
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (1, 1))

    def test_own_explicit_withdrawal_allows_actual_offer(self):
        self.prepare_question()
        _RestockFixture.revision(self, self.source("Дата відправки неважлива."))
        self.stock_and_buy()
        result, _, factories, prepares, _ = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, IgCheckoutAccessToken.objects.count()), (1, 1, 1))

    def test_own_explicit_acceptance_of_uncertainty_allows_actual_offer(self):
        self.prepare_question()
        _RestockFixture.revision(self, self.source("Погоджуюсь оформити без гарантії дати."))
        self.stock_and_buy()
        result, _, factories, prepares, _ = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, IgCheckoutAccessToken.objects.count()), (1, 1, 1))

    def test_price_acceptance_and_separate_uncertain_date_do_not_allow_actual_offer(self):
        self.prepare_question()
        _RestockFixture.revision(self, self.source("Погоджуюсь на ціну. Дата відправки не підтверджена."))
        self.stock_and_buy()
        result, validations, factories, prepares, sends = self.execute(
            "Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertNotEqual(result.state, "completed")
        self.assertIn((False, ("checkout_fulfillment_question_unanswered",)), validations)
        self.assertEqual((factories, prepares, sends, IgCheckoutAccessToken.objects.count()), (0, 0, 0, 0))

    def test_negated_agreement_with_modifier_does_not_allow_actual_offer(self):
        self.prepare_question()
        _RestockFixture.revision(self, self.source("Не зовсім згоден оформлювати без гарантії дати."))
        self.stock_and_buy()
        result, validations, factories, prepares, sends = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertNotEqual(result.state, "completed")
        self.assertIn((False, ("checkout_fulfillment_question_unanswered",)), validations)
        self.assertEqual((factories, prepares, sends, IgCheckoutAccessToken.objects.count()), (0, 0, 0, 0))

    def _assert_shipping_cost_question_survives(self, text):
        from management.services.ig_response_plan import capture_response_plan
        rows = self.bundle_question(text,
            "Дата відправки неважлива.", "Беру. Оформіть замовлення.")
        plan = capture_response_plan(self.client_row, revision=self.revision, publication=self.binding)
        self.assertTrue(any(row["source_message_id"] == rows[0].pk and row["kind"].startswith("info:") for row in plan.obligations))
        result, _, factories, prepares, sends = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertNotEqual(result.state, "completed")
        self.assertEqual((factories, prepares, sends, IgCheckoutAccessToken.objects.count()), (0, 0, 0, 0))

    def test_same_source_shipping_cost_question_survives_own_date_withdrawal(self):
        self._assert_shipping_cost_question_survives("Встигнете відправити до 20.10.2026? Яка вартість доставки?")

    def test_same_source_unpunctuated_shipping_cost_survives_own_date_withdrawal(self):
        self._assert_shipping_cost_question_survives("Встигнете відправити до 20.10.2026? Яка вартість доставки")

    def test_joined_shipping_cost_survives_own_date_withdrawal(self):
        self._assert_shipping_cost_question_survives("Встигнете відправити до 20.10.2026 яка вартість доставки")

    def test_mixed_release_source_keeps_its_independent_price_question_and_no_offer(self):
        from management.services.ig_response_plan import capture_response_plan
        rows = self.bundle_question("Встигнете відправити до 20.10.2026?",
            "Дата відправки неважлива. Яка ціна?", "Беру. Оформіть замовлення.")
        plan = capture_response_plan(self.client_row, revision=self.revision, publication=self.binding)
        self.assertTrue(any(row["source_message_id"] == rows[1].pk and row["kind"] == "info:price" for row in plan.obligations))
        result, _, factories, prepares, sends = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertNotEqual(result.state, "completed")
        self.assertEqual((factories, prepares, sends, IgCheckoutAccessToken.objects.count()), (0, 0, 0, 0))

    def test_fully_quoted_question_mints_no_review_or_debt_and_ordinary_offer_works(self):
        source = self.source('“Встигнете відправити до 20.10.2026?”')
        revision, _ = _RestockFixture.revision(self, source)
        result = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertFalse(result["ready"], result)
        self.assertEqual(reviews.Request.objects.count(), 0)
        self.assertEqual(self.debt(), {})
        self.stock_and_buy()
        result, _, factories, prepares, _ = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, IgCheckoutAccessToken.objects.count()), (1, 1, 1))

    def debt(self):
        self.client_row.refresh_from_db()
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        return capture_fulfillment_question_debt(self.client_row, cart, now=self.now)

    def test_thanks_and_quantity_change_do_not_answer_same_garment_question(self):
        self.prepare_question()
        source = self.source("Дякую. А яка ціна?")
        _RestockFixture.revision(self, source)
        self.assertEqual(self.debt()["source_message_ids"], [self.request.source_id])
        self.canonical("Змініть кількість на 2.")
        self.assertEqual(capture_current_selection_lines(self.client_row.pk, now=self.now)["lines"][0]["fields"]["quantity"]["value"], 2)
        self.assertEqual(self.debt()["source_message_ids"], [self.request.source_id])

    def test_different_variant_does_not_borrow_old_question(self):
        self.prepare_question()
        self.canonical("Змініть розмір на L.")
        self.assertEqual(self.debt(), {})

    def test_different_product_does_not_borrow_old_question(self):
        self.prepare_question()
        self.canonical(f"Замініть Restock Aurora на {self.products[1].title}, розмір M, classic, pink, кількість 1.")
        self.assertEqual(capture_current_selection_lines(self.client_row.pk, now=self.now)["lines"][0]["fields"]["product_id"]["value"], self.products[1].pk)
        self.assertEqual(self.debt(), {})

    def test_soft_wanted_by_preference_has_no_question_hold(self):
        self.prepare_question("Хочу отримати до 20.10.2026.")
        self.assertEqual(self.debt(), {})

    def native_dispatch_answer(self, *, retry=False, max_days=2):
        import uuid
        from django.contrib.auth import get_user_model
        from management.models import InstagramBotMessage, IgFollowUpTask
        from management.services import ig_fulfillment_delivery as delivery
        from management.services.instagram_bot import ProviderDeliveryReceipt
        manager = get_user_model().objects.create_superuser(username="date-answer-manager", password="test")
        result = reviews.apply_fulfillment_decision(self.request.pk, actor=manager,
            expected_request_digest=self.request.envelope_hmac, operation_id=uuid.uuid4(), now=self.now,
            decisions_by_line=[{"line_id": line["line_id"], "availability": "available", "checked_quantity": 1,
                "supply_basis": "accessible_unrecorded", "dispatch": {"min_days": 1, "max_days": max_days, "unit": "calendar"}}
                for line in self.request.envelope["lines"]])
        self.assertTrue(result["ready"], result)
        self.assertTrue(self.debt(), "A decision that was not delivered does not answer the customer's question")
        InstagramBotMessage.objects.filter(client=self.client_row, role="user").update(status="done")
        def sender(settings, recipient, text, **kwargs):
            with kwargs["provider_request_boundary_factory"](planned_chunk_count=1, delivered_chunk_count=0) as allowed:
                self.assertTrue(allowed, allowed.reason)
                self.assertTrue(kwargs["provider_message_callback"]("native-date-answer-mid"))
            return ProviderDeliveryReceipt(True, "", provider_message_id="native-date-answer-mid",
                provider_message_ids=("native-date-answer-mid",), planned_chunk_count=1, delivered_chunk_count=1, request_text=text)
        if retry:
            from datetime import timedelta
            def not_dispatched(settings, recipient, text, **kwargs):
                with kwargs["provider_request_boundary_factory"](planned_chunk_count=1, delivered_chunk_count=0) as allowed:
                    self.assertTrue(allowed, allowed.reason)
                return ProviderDeliveryReceipt(False, "cancelled", planned_chunk_count=1, delivered_chunk_count=0, request_text=text)
            with patch("management.services.instagram_bot.send_text", side_effect=not_dispatched):
                self.assertEqual(delivery.process_due_fulfillment_updates(self.settings, now=self.now), 0)
            pending = IgFollowUpTask.objects.get(reason=reviews.UPDATE_REASON)
            self.assertEqual((pending.status, pending.attempt_count, pending.skip_reason), ("pending", 0, "known_not_dispatched"))
            self.now += timedelta(minutes=16)
        with patch("django.utils.timezone.now", return_value=self.now), patch("management.services.instagram_bot.send_text", side_effect=sender) as transport:
            sent = delivery.process_due_fulfillment_updates(self.settings, now=self.now)
        task = IgFollowUpTask.objects.get(reason=reviews.UPDATE_REASON)
        self.assertEqual(sent, 1, (task.status, task.skip_reason))
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(task.status, "sent")
        self.assertEqual(task.attempt_count, 1)
        self.assertIn("відправка", task.message_text)

    def test_real_typed_delivered_dispatch_answer_allows_actual_offer(self):
        self.prepare_question()
        self.native_dispatch_answer()
        self.assertEqual(self.debt(), {})
        self.stock_and_buy()
        result, _, factories, prepares, _ = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, IgCheckoutAccessToken.objects.count()), (1, 1, 1))

    def test_real_known_not_dispatched_retry_has_native_sent_count_one_and_closes_question(self):
        self.prepare_question()
        self.native_dispatch_answer(retry=True)
        self.assertEqual(self.debt(), {})

    def test_delivered_day_window_does_not_answer_same_day_ten_am_question(self):
        self.prepare_question("Встигнете відправити завтра до 10:00?")
        self.native_dispatch_answer(max_days=1)
        self.assertTrue(self.debt())
        self.stock_and_buy()
        result, validations, factories, prepares, sends = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertNotEqual(result.state, "completed")
        self.assertIn((False, ("checkout_fulfillment_question_unanswered",)), validations)
        self.assertEqual((factories, prepares, sends, IgCheckoutAccessToken.objects.count()), (0, 0, 0, 0))

    def test_own_uncertainty_acceptance_resolves_clock_question_after_day_window_answer(self):
        self.prepare_question("Встигнете відправити завтра до 10:00?")
        self.native_dispatch_answer(max_days=1)
        _RestockFixture.revision(self, self.source("Погоджуюсь оформити без гарантії дати."))
        self.stock_and_buy()
        result, _, factories, prepares, _ = self.execute("Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, IgCheckoutAccessToken.objects.count()), (1, 1, 1))

    def test_delivered_dispatch_answer_does_not_answer_customer_arrival_question(self):
        self.prepare_question("Встигнете доставити до 20.10.2026?")
        self.native_dispatch_answer()
        self.assertTrue(self.debt())

    def test_quantity_change_cannot_borrow_delivered_old_dispatch_answer(self):
        self.prepare_question()
        self.native_dispatch_answer()
        self.assertEqual(self.debt(), {})
        self.canonical("Змініть кількість на 2.")
        self.assertTrue(self.debt())

    def test_prompt_fact_requires_real_publication_and_generic_ack_keeps_purchase_debt(self):
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_response_control import ValidatedResponse
        self.prepare_question()
        self.stock_and_buy()
        plan = capture_response_plan(self.client_row, revision=self.revision, publication=self.binding)
        fact = plan.prompt_projection()["fulfillment_question_debt"]
        self.assertEqual(fact["source_message_ids"], [self.request.source_id])
        self.assertEqual(fact["checkout"], "blocked")
        self.assertNotIn("raw_quote", str(fact))
        self.assertNotIn("request_digest", str(fact))
        self.assertNotIn(self.request.envelope_hmac, plan.prompt_guidance())
        with patch("management.services.ig_fulfillment_question_debt.capture_fulfillment_question_debt", wraps=capture_fulfillment_question_debt) as reader:
            self.assertNotIn("fulfillment_question_debt", capture_response_plan(self.client_row, revision=self.revision).prompt_projection())
        reader.assert_not_called()
        self.assertTrue(plan.coverage(ValidatedResponse("Добре, дякую!"))["remaining"])

    def test_actual_admin_capture_retains_96_budget_and_ordinary_debt_lookup_is_one_read(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from management.services.ig_admin_state_capture import current_admin_state, MAX_READ_QUERIES
        from management.services.ig_client_state_card import render_client_state_prompt
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(capture_fulfillment_question_debt(self.client_row, cart, now=self.now), {})
        self.assertEqual(len(queries), 1)
        self.prepare_question()
        capture = current_admin_state(self.client_row.pk, now=self.now)
        self.assertEqual(MAX_READ_QUERIES, 96)
        self.assertEqual(capture.status, "captured", capture.reason)
        self.assertLessEqual(capture.read_queries, 96)
        rendered = render_client_state_prompt(capture.state, budget=2400)
        self.assertLessEqual(rendered.estimated_tokens, 2400)
        self.assertIn("2026-10-20", rendered.text)

    def deliver_question(self, body='Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи ця дата для вас критична?'):
        self.prepare_question()
        self.stock_and_buy()
        result, validations, factories, prepares, sends = self.execute(body)
        self.assertEqual(result.state, "completed", (result.reasons, validations))
        self.assertEqual((factories, prepares, sends), (0, 0, 1))
        self.revision.refresh_from_db()
        effects = list(IgRevisionDeliveryEffect.objects.filter(revision=self.revision))
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0].state, "sent")
        self.assertTrue(effects[0].provider_message_id)
        self.assertTrue(self.revision.generation_proposal.get("source_cart_capture"))
        self.prompt_effect = effects[0]
        return effects[0]

    def answer(self, text, *, reply_to=""):
        self.now += timedelta(seconds=2)
        clock = patch("django.utils.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        source = self.source(text, at=self.now)
        if reply_to:
            source.reply_to_provider_message_id = reply_to
            source.save(update_fields=["reply_to_provider_message_id"])
        self.revision, self.token = _RestockFixture.revision(self, source)
        return source

    def test_criticality_short_answer_clears(self):
        self.deliver_question()
        self.answer("Не критична")
        self.assertEqual(self.debt(), {})
        from management.models import IgCheckoutProposal
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def test_uncertainty_one_question_short_answer_clears(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи погоджуєтесь оформлювати без гарантії цієї дати?')
        self.answer("Так, підходить")
        self.assertEqual(self.debt(), {})
        from management.models import IgCheckoutProposal
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def full_native_purchase(self):
        from management.models import IgCheckoutProposal, IgCheckoutAccessToken
        source = self.source("Беру. Оформіть замовлення.")
        self.buyer = source
        self.revision, self.token = _RestockFixture.revision(self, source)
        result, validations, factories, prepares, sends = self.execute(
            "Restock Aurora чорний розмір M є в наявності. Перевірте деталі оформлення:", paylink=True)
        self.assertEqual(result.state, "completed", (result.reasons, validations))
        self.assertEqual((factories, prepares, sends), (1, 1, 1))
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (1, 1))
        effect = IgRevisionDeliveryEffect.objects.get(revision=self.revision)
        self.assertEqual(effect.state, "sent")
        self.assertTrue(effect.provider_message_id)

    def test_criticality_full_native_checkout_after_new_explicit_purchase(self):
        self.deliver_question()
        self.answer("Не критична")
        self.assertEqual(self.debt(), {})
        self.full_native_purchase()

    def test_uncertainty_full_native_checkout_after_new_explicit_purchase(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи погоджуєтесь оформлювати без гарантії цієї дати?')
        self.answer("Так, підходить")
        self.assertEqual(self.debt(), {})
        self.full_native_purchase()

    def assert_not_released(self):
        from management.services.ig_fulfillment_question_debt import _contextual_question_release as contextual_question_release
        from management.services.ig_commerce_projection import capture_current_selection_lines
        self.client_row.refresh_from_db()
        self.settings.refresh_from_db()
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.request.refresh_from_db()
        self.assertEqual(contextual_question_release(self.request, cart, self.settings,
            after_source_id=self.request.source_id, now=self.now), (0, False))

    def test_no_delivered_question_cannot_release(self):
        self.prepare_question()
        self.stock_and_buy()
        self.answer("Так, підходить")
        self.assertTrue(self.debt())

    def test_criticality_question_does_not_treat_positive_phrase_as_withdrawal(self):
        self.deliver_question()
        self.answer("Так, підходить")
        self.assertTrue(self.debt())

    def test_bare_yes_cannot_release(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи погоджуєтесь оформлювати без гарантії цієї дати?')
        self.answer("Так")
        self.assertTrue(self.debt())

    def test_disjunctive_one_question_cannot_release(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи ця дата для вас критична?'[:-1] + " чи погоджуєтесь оформити без гарантії дати?")
        self.answer("Так, підходить")
        self.assertTrue(self.debt())

    def test_two_question_prompt_cannot_release(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи ця дата для вас критична?' + " Чи погоджуєтесь оформити без гарантії дати?")
        self.answer("Не критична")
        self.assertTrue(self.debt())

    def test_one_question_mixing_price_and_uncertainty_cannot_release(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи погоджуєтесь оформлювати без гарантії цієї дати?'.replace("оформлювати", "на ціну та оформлення"))
        self.answer("Так, підходить")
        self.assertTrue(self.debt())

    def test_wrong_reply_reference_cannot_release(self):
        self.deliver_question()
        self.answer("Не критична", reply_to="other-real-conversation-mid")
        self.assertTrue(self.debt())

    def test_wrong_case_cannot_release(self):
        self.deliver_question()
        self.now += timedelta(seconds=1)
        self.prepare_question("Встигнете відправити до 21.10.2026?")
        self.answer("Не критична")
        self.assertTrue(self.debt())

    def test_multiple_prior_cases_cannot_borrow_generic_question(self):
        self.prepare_question("Встигнете відправити до 21.10.2026?")
        self.deliver_question()
        self.answer("Не критична")
        self.assertTrue(self.debt())

    def test_mixed_short_answer_with_new_price_question_is_not_consumed(self):
        self.deliver_question()
        self.answer("Не критична. А яка ціна?")
        self.assertTrue(self.debt())

    def test_changed_variant_cannot_release_original_case(self):
        self.deliver_question()
        self.canonical("Змініть розмір на L.")
        self.answer("Не критична")
        self.assert_not_released()

    def test_changed_epoch_cannot_release(self):
        self.deliver_question()
        self.settings.reply_permission_epoch += 1
        self.settings.save(update_fields=["reply_permission_epoch"])
        self.answer("Не критична")
        self.assert_not_released()

    def test_changed_client_epoch_cannot_release(self):
        self.deliver_question()
        self.client_row.reply_permission_epoch += 1
        self.client_row.save(update_fields=["reply_permission_epoch"])
        self.answer("Не критична")
        self.assert_not_released()

    def test_real_reset_cannot_release_old_case(self):
        from django.contrib.auth import get_user_model
        from management.services.ig_funnel_reset import reset_funnel
        self.deliver_question()
        actor = get_user_model().objects.create_superuser(username="context-reset-owner", password="test")
        result = reset_funnel(client_id=self.client_row.pk, actor=actor, mode="fresh_conversation")
        self.assertTrue(result["ok"], result)
        self.client_row.refresh_from_db()
        self.answer("Не критична")
        self.assert_not_released()

    def test_intervening_customer_question_cannot_release(self):
        self.deliver_question()
        _RestockFixture.revision(self, self.source("А яка ціна?"))
        self.answer("Не критична")
        self.assertTrue(self.debt())

    def test_intervening_actual_delivered_other_question_cannot_release(self):
        self.deliver_question()
        source = self.source("Привіт")
        self.revision, self.token = _RestockFixture.revision(self, source)
        result, _, _, _, sends = self.execute("Вітаю! Чи зручно вам писати тут?")
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(sends, 1)
        self.answer("Не критична")
        self.assertTrue(self.debt())

    def test_actual_unknown_transport_receipt_cannot_release(self):
        from management import tests_ig_revision_live as fixtures
        self.prepare_question()
        self.stock_and_buy()
        execute = fixtures.RevisionLiveTests._execute
        with patch.object(fixtures.RevisionLiveTests, "_execute", lambda instance: execute(instance, send_results=[TimeoutError()])):
            result, _, _, _, sends = self.execute('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи ця дата для вас критична?')
        self.assertNotEqual(result.state, "completed")
        self.assertEqual(sends, 1)
        self.assertFalse(IgRevisionDeliveryEffect.objects.filter(revision=self.revision, state="sent").exists())
        self.answer("Не критична")
        self.assertTrue(self.debt())

    def test_negated_uncertainty_proposition_cannot_release(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи погоджуєтесь оформлювати без гарантії цієї дати?'.replace("Чи погоджуєтесь оформлювати без гарантії цієї дати?",
            "Чи погоджуєтесь, що без гарантії цієї дати оформлювати не можна?"))
        self.answer("Так, підходить")
        self.assertTrue(self.debt())

    def test_conjunction_compound_question_cannot_release(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи погоджуєтесь оформлювати без гарантії цієї дати?'.replace("цієї дати?", "цієї дати та підтверджуєте замовлення?"))
        self.answer("Так, підходить")
        self.assertTrue(self.debt())

    def test_wrong_iso_date_cannot_release(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи ця дата для вас критична?'.replace("20.10.2026", "2026-10-25"))
        self.answer("Не критична")
        self.assertTrue(self.debt())

    def test_refund_date_cannot_release_dispatch_case(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи ця дата для вас критична?'.replace("відправки", "повернення"))
        self.answer("Не критична")
        self.assertTrue(self.debt())

    def test_arrival_question_cannot_release_dispatch_case(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи ця дата для вас критична?'.replace("відправки", "отримання"))
        self.answer("Не критична")
        self.assertTrue(self.debt())

    def test_explicit_other_beneficiary_cannot_release(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи ця дата для вас критична?'.replace("дата відправки", "дата відправки для хлопця"))
        self.answer("Не критична")
        self.assertTrue(self.debt())

    def test_inline_numeric_date_preserves_whole_question(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи ця дата для вас критична?'.replace("Чи ця дата для вас критична?", "Чи дата 20.10.2026 для вас критична?"))
        self.answer("Не критична")
        self.assertEqual(self.debt(), {})
        self.full_native_purchase()

    def test_inline_numeric_date_cannot_replace_preceding_refund_topic(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи ця дата для вас критична?'.replace("відправки", "повернення").replace(
            "Чи ця дата для вас критична?", "Чи дата 20.10.2026 для вас критична?"))
        self.answer("Не критична")
        self.assertTrue(self.debt())

    def test_inline_numeric_date_cannot_replace_preceding_arrival_topic(self):
        self.deliver_question('Restock Aurora чорний розмір M є в наявності. Бажана дата відправки 20.10.2026 поки не підтверджена. Чи ця дата для вас критична?'.replace("відправки", "отримання").replace(
            "Чи ця дата для вас критична?", "Чи дата 20.10.2026 для вас критична?"))
        self.answer("Не критична")
        self.assertTrue(self.debt())

    def test_sixteen_ordinary_rows_do_not_resurrect_proven_release(self):
        self.deliver_question()
        self.answer("Не критична")
        self.assertEqual(self.debt(), {})
        for _ in range(16):
            self.answer("Дякую")
        self.assertEqual(self.debt(), {})

    def test_real_manager_photo_echo_prevents_borrowing_old_question(self):
        from management.services.ig_revision_echo_integration import observe_and_project_echo
        from management.models import InstagramBotMessage
        self.deliver_question()
        self.now += timedelta(seconds=1)
        with patch("management.services.instagram_bot._provider_http") as network:
            self.assertTrue(observe_and_project_echo(self.settings,
                namespace=self.prompt_effect.provider_namespace, recipient=self.client_row.igsid,
                mid="context-manager-photo", received_at=self.now,
                attachments=[{"url": "https://example.test/manager-photo.jpg", "type": "image"}]))
        network.assert_not_called()
        manager = InstagramBotMessage.objects.get(mid="context-manager-photo")
        self.assertEqual(manager.role, "manager")
        self.assertTrue(manager.attachment_media)
        self.client_row.refresh_from_db()
        self.answer("Не критична")
        self.assert_not_released()
