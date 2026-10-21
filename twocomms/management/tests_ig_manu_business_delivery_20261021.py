"""Business questions through native ingress worker, Gemini guard and outbox."""
import json
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management.models import IgCustomerTurn, IgFollowUpTask, IgTurnMessage, InstagramBotMessage
from management.services.ig_turn_revisions import create_collecting_revision
from management.tests_ig_business_cooperation import SHOP_REQUEST


BUSINESS_ACK = (
    "Дякуємо за звернення щодо запуску магазину! Передам адміністрації ваші питання: "
    "партнерська ціна; розміри й кольори чорних oversize-футболок; старт без закупівлі партії; "
    "продаж через Instagram і умови доставки; використання фото товарів TwoComms. "
    "Ці партнерські умови потребують окремого підтвердження команди."
)


@override_settings(IG_REVISION_EXECUTION_ENABLED=True,
    IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00",
    IG_TURN_CONTEXT_MODE="unified", GOOGLE_INDEXING_ENABLED=False)
class NativeBusinessDeliveryTests(TransactionTestCase):
    reset_sequences = True
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    def _generate(self, payload, **kwargs):
        if getattr(self, "expected_rejection", ""):
            from management.services.call_ai_analysis import CallAIAnalysisError
            decision = kwargs["result_validator"](self.parsed,
                usage={"_request_inline_count": 0, "_request_inline_content_hashes": []})
            self.assertFalse(decision.valid)
            self.assertIn(self.expected_rejection, decision.reason_codes)
            self.observed_rejection = decision.reason_codes
            raise CallAIAnalysisError("expected trade approval rejection")
        return fixtures.RevisionLiveTests._generate(self, payload, **kwargs)

    def _collect(self, *texts):
        self.sources = [InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", status="pending", mid=f"business-{self.revision.pk}-{index}",
            provider_namespace="instagram_login:owner-1", provider_created_at=timezone.now(), text=text)
            for index, text in enumerate(texts)]
        self.source = self.sources[-1]
        self.turn = IgCustomerTurn.objects.create(client=self.customer,
            primary_source_message=self.sources[0], window_started_at=timezone.now(), window_deadline=timezone.now())
        for index, source in enumerate(self.sources, 1):
            IgTurnMessage.objects.create(turn=self.turn, message=source, ordinal=index, role="user")
        self.revision = create_collecting_revision(self.turn, self.sources, bypass_quiet=True).revision

    def _worker(self, text, *, manager=True):
        from management.services.instagram_bot import process_pending
        self.worker_logs = []
        def fail_worker_error(level, event, detail, **kwargs):
            self.worker_logs.append((level, event, detail))
            if level == "error" and event == "revision_execution":
                import traceback
                self.fail(traceback.format_exc())
        self.parsed = {"reply_text": text, "controls": [{"kind": "manager", "value": True}] if manager else []}
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate) as model, \
                patch("management.services.instagram_bot.get_page_token", return_value="memory-token"), \
                patch("management.services.instagram_bot._provider_http", return_value=(200, json.dumps({"message_id": f"business-sent-{self.revision.pk}"}))) as send, \
                patch("management.services.instagram_bot.log", side_effect=fail_worker_error), \
                patch("management.services.instagram_bot._register_outgoing_message"):
            handled = process_pending(self.settings, max_items=1)
        self.revision.refresh_from_db()
        for source in self.sources:
            source.refresh_from_db()
        return handled, model, send

    def _known_personal_product(self):
        from storefront.models import Category, Product
        from productcolors.models import Color, ProductColorVariant
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import parse_turn
        from management.services.ig_revision_commerce import resolve_source_product_request
        category = Category.objects.create(name="Personal purchase", slug="personal-purchase")
        product = Product.objects.create(title="Personal Hoodie", slug="personal-hoodie",
            category=category, price=900, status="published")
        color = Color.objects.create(name="Business test black", primary_hex="#111111")
        ProductColorVariant.objects.create(product=product, color=color, price_override=900, stock=3, is_default=True)
        prior = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", status="pending", mid="personal-product-before-business",
            provider_namespace="instagram_login:owner-1", provider_created_at=timezone.now(), text="Personal Hoodie")
        selected = resolve_source_product_request(self.customer, prior, parse_turn(prior.text))
        self.assertEqual(selected.exact_product_id, product.pk)
        apply_turn(self.customer, prior, selected, reply_payload={})
        return product

    def test_five_business_questions_are_handed_off_and_sent_without_false_execution_debt(self):
        self._collect(SHOP_REQUEST)
        handled, model, send = self._worker(BUSINESS_ACK)
        self.assertEqual(send.call_count, 1, (handled, self.revision.state, self.revision.recovery_code))
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        case = IgFollowUpTask.objects.get(reason="revision_case:collaboration_review")
        self.assertEqual(case.manager_context["cooperation_kind"], "business")
        self.assertEqual(set(case.manager_context["business_cooperation"]["requested_topics"]), {
            "partner_price", "catalog_options", "start_without_stock", "delivery_terms", "resale_channel", "photo_usage"})
        coverage = self.revision.action_receipts.get("response_coverage", {})
        self.assertEqual(coverage.get("remaining"), [], (self.revision.recovery_code, coverage))
        self.assertEqual(self.revision.state, "processed")
        self.assertEqual(self.source.status, "done")
        self.assertFalse(IgFollowUpTask.objects.filter(reason="revision_case:execution_debt").exists())

    def test_independent_retail_price_is_not_covered_by_business_handoff(self):
        self._known_personal_product()
        self._collect(SHOP_REQUEST, "Для себе також хочу худі. Яка ціна?")
        _, _, send = self._worker(BUSINESS_ACK)
        self.assertEqual(send.call_count, 1, self.revision.recovery_code)
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        coverage = self.revision.action_receipts.get("response_coverage", {})
        self.assertIn(f"{self.source.pk}:info:price", coverage.get("remaining", []))

    def test_collective_business_brief_forwarding_completes_only_acknowledgement(self):
        self._collect(SHOP_REQUEST)
        _, _, send = self._worker("Ваші запитання щодо співпраці передала на розгляд.")
        self.assertEqual(send.call_count, 1, self.revision.recovery_code)
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])
        self.assertEqual(self.revision.state, "processed")
        case = IgFollowUpTask.objects.get(reason="revision_case:collaboration_review")
        self.assertEqual(case.manager_approval_status, "pending")
        self.assertNotIn(case.status, ("completed", "cancelled"))

    def test_incomplete_topic_acknowledgment_retains_missing_business_question(self):
        self._collect(SHOP_REQUEST)
        _, _, send = self._worker(BUSINESS_ACK.replace("; використання фото товарів TwoComms", ""))
        self.assertEqual(send.call_count, 1, self.revision.recovery_code)
        coverage = self.revision.action_receipts["response_coverage"]
        self.assertEqual(coverage["remaining"], [f"{self.source.pk}:business:photo_usage"])

    def test_generic_manager_flag_does_not_cover_business_questions(self):
        self._collect(SHOP_REQUEST)
        _, _, send = self._worker("Дякуємо! Передам адміністрації на розгляд.")
        self.assertEqual(send.call_count, 1, self.revision.recovery_code)
        coverage = self.revision.action_receipts["response_coverage"]
        self.assertEqual(len(coverage["remaining"]), 6)
        self.assertEqual(coverage["covered"], [])

    def test_same_source_personal_price_survives_collective_business_handoff(self):
        self._known_personal_product()
        self._collect(SHOP_REQUEST + " Для себе також хочу худі. Яка ціна?")
        _, _, send = self._worker("Ваші запитання щодо співпраці передала на розгляд.")
        self.assertEqual(send.call_count, 1, self.revision.recovery_code)
        coverage = self.revision.action_receipts["response_coverage"]
        self.assertIn(f"{self.source.pk}:info:price", coverage["remaining"])
        self.assertFalse(any(":business:" in identity for identity in coverage["remaining"]))

    def test_personal_first_mixed_source_keeps_both_question_owners(self):
        self._known_personal_product()
        self._collect("Яка ціна худі? Для себе хочу худі. А ще запускаю Instagram-магазин. "
            "Яка партнерська ціна та чи можна використовувати ваші фото?")
        _, _, send = self._worker("Ваші запитання щодо співпраці передала на розгляд.")
        self.assertEqual(send.call_count, 1, self.revision.recovery_code)
        coverage = self.revision.action_receipts["response_coverage"]
        self.assertIn(f"{self.source.pk}:info:price", coverage["remaining"])
        self.assertIn(f"{self.source.pk}:business:partner_price", coverage["covered"])
        self.assertIn(f"{self.source.pk}:business:photo_usage", coverage["covered"])

    def test_known_personal_price_confirmation_does_not_approve_trade_terms(self):
        self._known_personal_product()
        self._collect(SHOP_REQUEST, "Для себе хочу Personal Hoodie. Яка ціна?")
        _, _, send = self._worker("Ціна Personal Hoodie для себе підтверджена: 900 грн. "
            "Ваші запитання щодо співпраці передала на розгляд.")
        self.assertEqual(send.call_count, 1, (self.revision.recovery_code, self.worker_logs))
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        coverage = self.revision.action_receipts["response_coverage"]
        self.assertNotIn(f"{self.source.pk}:info:price", coverage["remaining"])
        self.assertFalse(any(":business:" in identity for identity in coverage["remaining"]))

    def test_native_unsupported_photo_rights_approval_is_rejected_before_send(self):
        self._collect(SHOP_REQUEST)
        self.expected_rejection = "response_plan_business_terms_unconfirmed"
        _, _, send = self._worker("Команда підтвердила: можете використовувати фото товарів TwoComms.")
        self.assertIn(self.expected_rejection, self.observed_rejection)
        send.assert_not_called()

    def test_review_clause_cannot_hide_later_photo_rights_grant(self):
        self._collect(SHOP_REQUEST)
        self.expected_rejection = "response_plan_business_terms_unconfirmed"
        _, _, send = self._worker("Я уточню в команди, чи дозволено фото; ви можете використовувати фото товарів уже зараз.")
        self.assertIn(self.expected_rejection, self.observed_rejection)
        send.assert_not_called()

    def test_collective_handoff_cannot_hide_english_photo_rights_approval(self):
        self._collect("I want to open an Instagram store. Can I use your product photos? Please reply in English.")
        self.expected_rejection = "response_plan_business_terms_unconfirmed"
        _, _, send = self._worker("Your questions about business cooperation are with the team for review. "
            "The team confirmed you may use our product photos.")
        self.assertTrue(hasattr(self, "observed_rejection"), self.worker_logs)
        self.assertIn(self.expected_rejection, self.observed_rejection)
        send.assert_not_called()

    def test_coordinated_permission_grant_is_not_scoped_by_earlier_whether(self):
        self._collect("I want to open an Instagram store. Can I use your product photos? Please reply in English.")
        self.expected_rejection = "response_plan_business_terms_unconfirmed"
        _, _, send = self._worker("I will check whether you may use our photos, and you may use our photos already.")
        self.assertIn(self.expected_rejection, self.observed_rejection)
        send.assert_not_called()

    def test_native_english_pending_confirmation_is_safe_but_does_not_grant_photo_rights(self):
        self._collect("I want to open an Instagram store. Can I use your product photos? Please reply in English.")
        _, _, send = self._worker("I can reply in English. Your questions about business cooperation have been passed to the team for review.")
        self.assertEqual(send.call_count, 1, self.worker_logs)
        self.assertEqual(self.revision.state, "processed")
        self._collect("Any news?")
        _, _, send = self._worker("There is no confirmed update on your business questions yet.", manager=False)
        self.assertEqual(send.call_count, 1, self.worker_logs)
        self.assertEqual(self.revision.state, "processed")
        self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])
        self._collect("Any news?")
        self.expected_rejection = "response_plan_business_terms_unconfirmed"
        _, _, send = self._worker("There is no confirmed update yet. You may use our product photos.", manager=False)
        self.assertIn(self.expected_rejection, self.observed_rejection)
        send.assert_not_called()

    def test_native_business_status_then_known_personal_price_without_duplicate_handoff(self):
        from management.models import IgBotNotification
        from management.services.ig_pending_question_context import capture_pending_question_context
        self._known_personal_product()
        self._collect(SHOP_REQUEST)
        handled, model, send = self._worker("Ваші запитання щодо співпраці передала на розгляд.")
        self.assertEqual(send.call_count, 1, (handled, model.call_count, self.revision.state, self.revision.recovery_code, self.worker_logs))
        self.assertEqual(self.revision.state, "processed")
        case = IgFollowUpTask.objects.get(reason="revision_case:collaboration_review")
        counts = (IgFollowUpTask.objects.count(), IgBotNotification.objects.count())
        self._collect("Any news?")
        _, model, send = self._worker("Ще не маю підтвердженої відповіді щодо ваших питань співпраці.", manager=False)
        self.assertEqual((model.call_count, send.call_count), (1, 1), (self.revision.recovery_code, self.worker_logs))
        context = capture_pending_question_context(self.revision)
        self.assertEqual(context.get("status_source_ids"), [self.source.pk], context)
        self.assertEqual(context["cases"][0]["task_id"], case.pk)
        self.assertEqual(self.revision.state, "processed")
        self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])
        self.assertEqual((IgFollowUpTask.objects.count(), IgBotNotification.objects.count()), counts)
        self._collect("Чи вже є відповідь керівництва щодо партнерської ціни?")
        _, _, send = self._worker("Ще не маю підтвердженої відповіді керівництва щодо партнерської ціни.", manager=False)
        self.assertEqual(send.call_count, 1, (self.revision.recovery_code, self.worker_logs))
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        self.assertEqual(self.revision.state, "processed")
        self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])
        self.assertEqual((IgFollowUpTask.objects.count(), IgBotNotification.objects.count()), counts)
        self._collect("Яка ціна Personal Hoodie?")
        _, _, send = self._worker("Personal Hoodie коштує 900 грн.", manager=False)
        self.assertEqual(send.call_count, 1, self.revision.recovery_code)
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        self.assertEqual(self.revision.state, "processed")
        self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])
        self.assertEqual((IgFollowUpTask.objects.count(), IgBotNotification.objects.count()), counts)
        case.refresh_from_db()
        self.assertEqual(case.manager_approval_status, "pending")

    def test_partner_status_cannot_cover_same_source_independent_retail_price(self):
        from management.models import IgBotNotification
        product = self._known_personal_product()
        self._collect(SHOP_REQUEST)
        _, _, send = self._worker("Ваші запитання щодо співпраці передала на розгляд.")
        self.assertEqual(send.call_count, 1, self.worker_logs)
        self.assertEqual(self.revision.state, "processed")
        case_count = IgFollowUpTask.objects.filter(reason="revision_case:collaboration_review").count()
        case = IgFollowUpTask.objects.get(reason="revision_case:collaboration_review")
        notification_key = f"ig-revision-case:{case.pk}"
        notification_count = IgBotNotification.objects.filter(dedupe_key=notification_key).count()
        self._collect("Чи вже є відповідь керівництва щодо партнерської ціни? "
            f"Яка ціна {product.title}?")
        _, _, send = self._worker("Ще не маю підтвердженої відповіді керівництва щодо партнерської ціни.", manager=False)
        self.assertEqual(send.call_count, 1, self.worker_logs)
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        coverage = self.revision.action_receipts["response_coverage"]
        self.assertIn(f"{self.source.pk}:info:manager_status", coverage["covered"])
        self.assertTrue(any(identity.startswith(f"{self.source.pk}:") and identity.endswith(":info:price")
            for identity in coverage["remaining"]), coverage)
        self.assertEqual(IgFollowUpTask.objects.filter(reason="revision_case:collaboration_review").count(), case_count)
        self.assertEqual(IgBotNotification.objects.filter(dedupe_key=notification_key).count(), notification_count)
