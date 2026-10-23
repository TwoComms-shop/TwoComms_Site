"""Current own assent and answered questions precede actual proposal creation."""
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.db import transaction

from management import tests_ig_revision_checkout as checkout_fixture
from management.models import IgCheckoutProposal, IgCheckoutAccessToken
from management.services.ig_revision_checkout import authorize_revision_checkout
from management.services.ig_commerce_projection import capture_current_selection_lines


class PurchaseSourceSemanticsTests(SimpleTestCase):
    def test_synthetic_or_unsaved_client_cannot_supply_purchase_or_carrier_authority(self):
        from types import SimpleNamespace
        from management.models import IgClient
        from management.services.instagram_bot import _current_purchase_source_texts
        from management.services.ig_revision_checkout import requested_nonstandard_carrier
        sources = [{"message_id": 1, "role": "user", "text": "Беру футболку. Відправте Укрпоштою."}]
        for client in (None, SimpleNamespace(intent=""),
                SimpleNamespace(pk=1, current_commercial_episode_id=1, igsid="synthetic"),
                IgClient(igsid="unsaved"), IgClient(pk=1, igsid="unsaved", current_commercial_episode_id=1)):
            with self.subTest(client_type=type(client).__name__):
                self.assertEqual(_current_purchase_source_texts(client, sealed_sources=sources), ())
                self.assertEqual(requested_nonstandard_carrier(client, sources), {})

    def test_own_commitment_and_later_refusal_are_distinct_from_consultation(self):
        from management.services.instagram_bot import _source_purchase_assent
        for text in ("Беру обидві футболки.", "Так, оформлюй обидві", "Хочу замовити футболку для себе.", "Оформіть замовлення.", "Як я можу оплатити?",
                "Оформлюй. Якщо прати при 30, принт не зіпсується?"):
            self.assertEqual(_source_purchase_assent(text), 1, text)
        for text in ("Поки не готова замовляти. Підкажіть ціну на парні футболки. Чи є знижка?",
                "Передумала. Поки не готова.", "Не хочу зараз.", "Поки не хочу оплачувати.",
                "Не хочу замовляти", "Not ready to buy", "Беру якщо буде 10% знижки", "Беру, якщо буде 10% знижки",
                "Оформлюй, якщо буде 10% знижки.", "Оформіть замовлення, якщо буде 10% знижки.",
                "Оформлюй. Але тільки якщо буде 10% знижки.", "How can I pay if there is a discount?"):
            self.assertEqual(_source_purchase_assent(text), -1, text)
        for text in ("Хочу синю футболку", "Який розмір краще?", "Підтверджую розмір M", "Сестра сказала хочу замовити футболку", "Раніше казала «беру»", "Він написав: «беру футболку»"):
            self.assertEqual(_source_purchase_assent(text), 0, text)


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.shop")
class PreofferCheckoutTests(TransactionTestCase):
    reset_sequences = True
    source_text = "Розмір M."
    setUp = checkout_fixture.RevisionCheckoutTests.setUp
    _admit_choice = checkout_fixture.RevisionCheckoutTests._admit_choice
    _renew_purchase_revision = checkout_fixture.RevisionCheckoutTests._renew_purchase_revision
    _prepare = checkout_fixture.RevisionCheckoutTests._prepare

    def _source(self, mid):
        self.source_number = getattr(self, "source_number", 0) + 1
        return checkout_fixture.RevisionCheckoutTests._source(self, f"{mid}-{self.source_number}")

    def current(self, text):
        self.source_text = text
        self._renew_purchase_revision()

    def authority(self):
        self.client_row.refresh_from_db()
        return authorize_revision_checkout(self.client_row, {"paylink": "full"}, self.source.text,
            sealed_sources=self.revision.bundle_snapshot["sources"],
            source_cart_capture=capture_current_selection_lines(self.client_row.pk))

    def no_offer(self, reason):
        result = self._prepare()
        self.assertFalse(result.planned, result)
        self.assertIn(reason, result.reasons)
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutAccessToken.objects.exists())

    def install_reply(self, reply):
        self.candidate_reply = reply
        self._install_generation()

    def admit_catalog_price_contract(self):
        from storefront.models import Catalog, SizeGrid, ProductFitOption
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        catalog = Catalog.objects.create(name="Preoffer", slug="preoffer-price")
        grid = SizeGrid.objects.create(catalog=catalog, name="Preoffer", guide_data={
            "columns": [{"key": "size", "label": "Size"}], "rows": [{"size": "M", "display_size": "M"}]})
        self.product.catalog, self.product.size_grid = catalog, grid
        self.product.save(update_fields=["catalog", "size_grid"])
        ProductFitOption.objects.create(product=self.product, code="classic", label="Classic", is_active=True, is_default=True)
        source = self._source("preoffer-fit")
        source.text = "Для себе крій classic"
        source.save(update_fields=["text"])
        with transaction.atomic():
            admitted = reduce_inbound_commerce_source(self.client_row, source,
                expected_provider_namespace=source.provider_namespace)
        self.assertTrue(admitted.ready, admitted.reason)
        self.controls[0] = {"kind": "item", "value": f"{self.product.pk}|2|M|classic|{self.variant.pk}"}

    def _install_generation(self):
        from django.utils import timezone
        from management.services.ig_revision_outbox import _digest
        from management.services.ig_revision_actions import _authority_projection
        from management.services.ig_revision_authority import CLAIM_CATALOG_CONFIGURATION, build_revision_authority_bindings
        from management.services.ig_revision_checkout import checkout_authority_control
        from management.services.ig_response_control import ResponseControl, ValidatedResponse
        response = ValidatedResponse(reply_text=getattr(self, "candidate_reply", "Перевірте деталі:"),
            controls=tuple(ResponseControl(**row) for row in self.controls))
        capture = capture_current_selection_lines(self.client_row.pk)
        control, reasons = checkout_authority_control(self.client_row, response.control,
            source_cart_capture=capture, source_cart_revision_id=self.revision.pk)
        self.assertFalse(reasons)
        self.authority = build_revision_authority_bindings(self.client_row, claims=(CLAIM_CATALOG_CONFIGURATION,),
            control=control, server_authorized_actions=("checkout_proposal_create",))
        self.assertTrue(self.authority.ready, self.authority.reasons)
        proposal = {"schema_version": 1, "sources": [{"message_id": self.source.pk}],
            "generation": {"request_id": "checkout-test-request", "actual_model": "gemini-3.7-flash"},
            "policy_manifest": {"instruction_publication": {"id": self.publication.pk,
                "version": self.publication.version, "hash": self.publication.snapshot_hash}},
            "authority": _authority_projection(self.authority), "source_cart_capture": capture,
            "response": {"reply_text": response.reply_text, "controls": self.controls}}
        self.revision.generation_proposal = proposal
        self.revision.generation_proposal_digest = _digest(proposal)
        self.revision.generation_proposed_at = timezone.now()
        self.revision.save(update_fields=["generation_proposal", "generation_proposal_digest", "generation_proposed_at", "updated_at"])

    def test_ready_crm_stage_without_own_purchase_assent_cannot_issue_offer(self):
        self.assertIn("checkout_purchase_authority_missing", self.authority()[1])
        self.no_offer("checkout_purchase_authority_missing")

    def test_ready_cart_explicit_not_ready_and_pair_question_create_nothing(self):
        self.current("Поки не готова замовляти. Підкажіть ціну на парні футболки. Чи є знижка?")
        self.no_offer("checkout_purchase_authority_missing")

    def test_later_refusal_overrules_an_earlier_genuine_purchase_wish(self):
        self.current("Хочу замовити футболку.")
        self.current("Поки не готова замовляти. Чи є знижка?")
        self.no_offer("checkout_purchase_authority_missing")

    def test_standalone_latest_refusals_override_the_real_earlier_wish(self):
        from management.services.instagram_bot import payment_link_allowed
        for text in ("Передумала. Поки не готова.", "Не хочу зараз.", "Поки не хочу оплачувати."):
            self.current("Хочу замовити футболку.")
            self.current(text)
            self.assertFalse(payment_link_allowed(self.client_row, {"paylink": "full", "product": self.product.pk}, text,
                sealed_sources=self.revision.bundle_snapshot["sources"], source_cart_capture=capture_current_selection_lines(self.client_row.pk)), text)
        self.no_offer("checkout_purchase_authority_missing")

    def test_exact_current_confirmation_works_without_any_prior_purchase_wish(self):
        self.current("Хочу обрати футболки для себе та хлопця.")
        self.current("Так, оформлюй обидві")
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)

    def test_conditional_discount_assent_does_not_accept_current_full_price(self):
        self.current("Хочу замовити футболку.")
        self.current("Беру якщо буде 10% знижки")
        self.no_offer("checkout_purchase_authority_missing")

    def test_conditional_new_imperatives_cannot_issue_current_full_price_offer(self):
        for text in ("Оформлюй, якщо буде 10% знижки.", "Оформіть замовлення, якщо буде 10% знижки.",
                "Оформлюй. Але тільки якщо буде 10% знижки."):
            self.current("Хочу замовити футболку.")
            self.current(text)
            self.assertIn("checkout_purchase_authority_missing", self.authority()[1], text)
        self.no_offer("checkout_purchase_authority_missing")

    def test_earlier_genuine_wish_survives_a_later_size_answer(self):
        self.current("Хочу замовити футболку.")
        self.current("Розмір M.")
        self.assertFalse(self.authority()[1])
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)

    def test_ready_assent_with_unanswered_discount_cannot_create_proposal(self):
        self.current("Беру футболку. Чи є знижка на парний образ?")
        self.no_offer("response_plan_checkout_questions_unanswered")

    def test_honest_unconfirmed_pair_entitlement_answer_and_offer_share_one_reply(self):
        self.current("Беру футболку. Чи є знижка на парний образ?")
        reply = "Парну знижку не підтверджено. Пропозиція за поточними цінами каталогу."
        self.install_reply(reply)
        result = self._prepare(reply_text=reply)
        self.assertTrue(result.planned, result.reasons)
        self.assertEqual(IgCheckoutProposal.objects.count(), 1)

    def test_pending_personal_discount_decision_cannot_be_covered_by_a_deferment(self):
        self.current("Беру футболку. Чи є знижка на парний образ?")
        reply = "Парну знижку не підтверджено, уточню персональну знижку."
        self.install_reply(reply)
        result = self._prepare(reply_text=reply)
        self.assertFalse(result.planned)
        self.assertIn("response_plan_checkout_questions_unanswered", result.reasons)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_known_catalog_price_answer_and_offer_share_one_reply(self):
        self.admit_catalog_price_contract()
        self.current("Беру футболку. Яка ціна?")
        reply = f"Ціна {self.product.title} — 900 грн. Перевірте деталі пропозиції."
        self.install_reply(reply)
        result = self._prepare(reply_text=reply)
        self.assertTrue(result.planned, result.reasons)

    def test_wrong_price_cannot_cover_the_single_current_line_question(self):
        self.admit_catalog_price_contract()
        self.current("Беру футболку. Яка ціна?")
        reply = f"Ціна {self.product.title} — 901 грн."
        self.install_reply(reply)
        result = self._prepare(reply_text=reply)
        self.assertFalse(result.planned)
        self.assertIn("response_plan_checkout_questions_unanswered", result.reasons)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_global_price_question_stays_ambiguous_with_multiple_lines(self):
        self.admit_catalog_price_contract()
        from storefront.models import Product
        from productcolors.models import ProductColorVariant
        second = Product.objects.create(title="Second", slug="preoffer-second", category=self.product.category,
            price=600, status="published")
        variant = ProductColorVariant.objects.create(product=second, color=self.variant.color,
            price_override=600, stock=20, is_default=True)
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        from management.models import IgCommerceTurnDecision
        second_source = self._source("preoffer-second-current")
        second_source.text = f"добавьте {second.title}, количество 1, размер M, цвет чёрный"
        second_source.save(update_fields=["text"])
        with transaction.atomic():
            admitted = reduce_inbound_commerce_source(self.client_row, second_source,
                expected_provider_namespace=second_source.provider_namespace)
        self.assertTrue(admitted.ready, admitted.reason)
        self.assertTrue(IgCommerceTurnDecision.objects.get(source_message=second_source).accepted)
        self.current("Беру обидві. Яка ціна?")
        self.controls.insert(1, {"kind": "item", "value": f"{second.pk}|1|M||{variant.pk}"})
        reply = f"Ціна {self.product.title} — 900 грн."
        self.install_reply(reply)
        result = self._prepare(reply_text=reply)
        self.assertFalse(result.planned)
        self.assertIn("response_plan_checkout_questions_unanswered", result.reasons)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_two_complete_items_create_one_native_offer(self):
        self.source_text = "Беру. Оформіть замовлення."
        checkout_fixture.RevisionCheckoutTests.test_two_items_current_prices_and_second_item_drift(self)

    def test_explicit_ukrposhta_request_preserves_cart_and_creates_no_np_offer(self):
        from management.models import IgCommerceSelectionSession
        before = IgCommerceSelectionSession.objects.get(client=self.client_row, open_slot=1).lines
        self.current("Беру футболку. Відправте Укрпоштою.")
        self.no_offer("response_plan_checkout_delivery_requires_manager")
        self.assertEqual(IgCommerceSelectionSession.objects.get(client=self.client_row, open_slot=1).lines, before)

    def test_negated_ukrposhta_with_affirmed_np_does_not_hold_checkout(self):
        self.current("Беру футболку. Не Укрпошта, а Нова пошта.")
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)

    def test_alternative_carriers_require_clarification_before_np_offer(self):
        self.current("Беру футболку. Доставка Укрпошта або Нова пошта.")
        self.no_offer("response_plan_checkout_delivery_requires_manager")

    def test_later_own_np_choice_resumes_the_collected_cart(self):
        self.current("Беру футболку. Відправте Укрпоштою.")
        self.assertIn("response_plan_checkout_delivery_requires_manager", self.authority()[1])
        self.current("Тепер відправте Новою поштою.")
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)

    def test_pure_carrier_withdrawal_clears_the_previous_carrier_hold(self):
        from management.services.ig_revision_checkout import requested_nonstandard_carrier
        self.current("Беру футболку. Відправте Укрпоштою.")
        self.assertTrue(requested_nonstandard_carrier(self.client_row, self.revision.bundle_snapshot["sources"]))
        self.current("Не Укрпошта.")
        self.assertFalse(requested_nonstandard_carrier(self.client_row, self.revision.bundle_snapshot["sources"]))
        self.current("Не хочу через Укрпошту.")
        self.assertFalse(requested_nonstandard_carrier(self.client_row, self.revision.bundle_snapshot["sources"]))

    def test_ambiguous_size_correction_does_not_use_old_ready_configuration(self):
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        self.current("Замість L хочу інший розмір.")
        with transaction.atomic():
            reduce_inbound_commerce_source(self.client_row, self.source,
                expected_provider_namespace=self.source.provider_namespace)
        self.assertIn("checkout_cart_clarification_required", self.authority()[1])
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_complete_unconditional_purchase_creates_one_offer(self):
        self.current("Беру. Оформіть замовлення.")
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)
        self.assertEqual(IgCheckoutProposal.objects.count(), 1)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)

    def test_legacy_assistant_checkout_prose_never_substitutes_for_the_customer(self):
        from unittest.mock import patch
        from management.services import instagram_bot as bot
        control = {"paylink": "full", "product": self.product.pk, "size": "M", "qty": 2}
        with (patch.object(bot, "payment_link_allowed", wraps=bot.payment_link_allowed) as gate,
              patch.object(bot, "notify_manager"),
              patch("management.services.bot_orders.create_deal_and_link", return_value={"error": "test_stop_before_invoice"}) as create):
            bot.finalize_paylink("Оформлюємо замовлення, ось посилання на оплату.", control,
                self.client_row, self.client_row.igsid, trigger_text="Який крій футболки?")
            gate.assert_called_once_with(self.client_row, control, "Який крій футболки?")
            create.assert_not_called()
        self.current("Беру. Оформіть замовлення.")
        self.assertTrue(bot.payment_link_allowed(self.client_row, control, self.source.text))


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class CarrierManagerNativeJourneyTests(TransactionTestCase):
    from management import tests_ig_revision_live as live
    from management import tests_ig_media_live_integration as media
    setUp = live.RevisionLiveTests.setUp
    _message = live.RevisionLiveTests._message
    _prepare = live.RevisionLiveTests._prepare
    _generate = live.RevisionLiveTests._generate
    _replace_bundle = live.RevisionLiveTests._replace_bundle
    _execute = live.RevisionLiveTests._execute
    select_tee = media.CanonicalMediaIntegrationTests.select_published_tee_before_media

    def _assert_carrier_case_before_sent(self, reply, controls):
        from unittest.mock import patch
        from management.models import IgFollowUpTask, IgBotNotification, IgCommerceSelectionSession
        from management.services.ig_revision_live import execute_claimed_revision
        self.select_tee()
        before = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1).lines
        self._replace_bundle(["Відправте Укрпоштою, будь ласка."])
        self.parsed["reply_text"] = reply
        self.parsed["controls"] = controls
        with (patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate),
              patch("management.services.instagram_bot.get_page_token", return_value=""),
              patch("management.services.instagram_bot._deliver_manager_notification") as external):
            first = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(first.reasons, ("provider_not_configured",))
        external.assert_not_called()
        task = IgFollowUpTask.objects.get(reason="revision_case:manager_handoff")
        self.assertEqual(task.manager_context["delivery_request"]["carrier"], "ukr")
        self.assertIn("Укрпоштою", task.manager_context["delivery_request"]["source_quote"])
        self.assertEqual(task.manager_context["delivery_cart"][0]["line_id"], before[0]["line_id"])
        self.assertEqual(IgBotNotification.objects.count(), 1)
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertTrue(self.revision.delivery_effects.filter(state="sent").exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutAccessToken.objects.exists())
        self.assertEqual(IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1).lines, before)

    def test_ukrposhta_handoff_is_recorded_before_native_customer_sent(self):
        self._assert_carrier_case_before_sent("Уточню з менеджером відправку Укрпоштою; ваші обрані речі збережено.",
            [{"kind": "manager", "value": True}])

    def test_plain_ack_cannot_omit_the_source_owned_delivery_case(self):
        self._assert_carrier_case_before_sent("Добре, дякую!", [])
