"""A split-shipping choice is native consent to one exact basket and cost frame."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone as datetime_timezone
import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from management.ig_fulfillment_review_models import (
    IgFulfillmentReviewRequest as Request, IgFulfillmentReviewDecision as Decision,
)
from management.ig_split_shipping_models import (
    IgSplitShippingOffer as Offer, IgSplitShippingReceipt as Receipt,
    IgSplitShippingChoice as Choice,
)
from management.models import (
    IgClient, IgCustomerTurn, IgCustomerTurnRevision, IgTurnMessage, IgFunnelResetAudit, IgFollowUpTask,
    IgCheckoutInventoryReservation, InstagramBotMessage,
)
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_split_shipping as shipping
from management.services import ig_split_delivery as delivery
from management.tests_ig_restock_subscriptions import _RestockFixture
from orders.models import Order, PaymentAttempt
from product_catalog.models import ProductInventoryPolicy, VariantSizeRule


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True)
class NativeSplitShippingTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(
            username="split-offer-manager", email="split@example.test", password="test")
        self.canonical("количество 2")
        second, variant = self.products[1], self.variants[1]
        variant.stock = 0
        variant.save(update_fields=["stock"])
        ProductInventoryPolicy.objects.create(product=second, source="catalog_variant")
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="L",
            stock=0, is_enabled=True)
        self.canonical("добавьте Storm, количество 2, размер L, цвет pink, крій classic")
        self.canonical("Хочу купити")

    def manager_offer(self, *, locale="uk", payer="customer", amount="70.00", at=None):
        now = at or self.now
        text = {"uk": "Це подарунок. Встигнете відправити до 20.10.2026?",
            "ru": "Это подарок. Успеете отправить до 20.10.2026?",
            "en": "This is a gift. Can you dispatch by 20.10.2026?"}[locale]
        source = self.source(text, at=now)
        revision, _token = self.revision(source)
        with patch("django.utils.timezone.now", return_value=now):
            result = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=now)
            self.assertTrue(result["ready"], result)
            request = Request.objects.get(pk=result["request_id"])
            lines = request.envelope["lines"]
            self.assertEqual(len(lines), 2)
            self.assertEqual([line["target"]["quantity"] for line in lines], [2, 2])
            split = {"groups": [[line["line_id"]] for line in lines],
                "extra_delivery_amount": amount, "payer": payer, "feasible": True}
            decision = reviews.apply_fulfillment_decision(request.pk, actor=self.manager,
                expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(),
                decisions_by_line={"lines": [{"line_id": line["line_id"], "availability": "available",
                    "checked_quantity": line["target"]["quantity"], "supply_basis": "accessible_unrecorded"}
                    for line in lines], "split_offer": split}, now=now)
        self.assertTrue(decision["ready"], decision)
        return request, Decision.objects.get(pk=decision["decision_id"])

    def no_commerce_effects(self):
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.stock, 0)

    @staticmethod
    def real_clock():
        return datetime.now(datetime_timezone.utc)

    def test_real_advancing_clock_default_issuance_commits_and_reads_current_offer(self):
        # Mature the original signed request/decision before the live operation.
        # Its inserts are deliberately separate from the unpatched offer clock.
        request, decision = self.manager_offer(at=self.real_clock() - timedelta(seconds=1))
        with patch("django.utils.timezone.now", side_effect=self.real_clock), \
                patch("management.services.instagram_bot._provider_http") as provider:
            offer = shipping.issue_offer(self.client_row.pk, self.manager,
                decision.envelope_hmac, uuid.uuid4(), self.options(request))
            self.assertGreater(offer.created_at, parse_datetime(offer.envelope["issued_at"]))
            self.assertEqual(shipping.current_offer_reason(offer, self.settings), "")
            projected = shipping.shipping_projection(self.client_row, self.settings)
        provider.assert_not_called()
        self.assertEqual(projected["status"], "pending")
        self.assertTrue(projected["current"])
        self.assertEqual(projected["offer"]["id"], str(offer.pk))
        self.assertEqual(Offer.objects.count(), 1)
        self.assertFalse(Receipt.objects.exists())
        self.no_commerce_effects()

    @override_settings(ROOT_URLCONF="management.urls")
    def test_real_advancing_clock_authenticated_api_issues_first_offer(self):
        request, decision = self.manager_offer(at=self.real_clock() - timedelta(seconds=1))
        client = Client(HTTP_HOST="management.twocomms.shop")
        client.force_login(self.manager)
        payload = {"action": "issue_offer", "expected_decision_digest": decision.envelope_hmac,
            "operation_id": str(uuid.uuid4()), "options": self.options(request)}
        with patch("django.utils.timezone.now", side_effect=self.real_clock), \
                patch("management.services.instagram_bot._provider_http") as provider:
            response = client.post(reverse("management_bot_split_shipping_api", args=[self.client_row.pk]),
                json.dumps(payload), content_type="application/json")
        provider.assert_not_called()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["success"], response.json())
        offer = Offer.objects.get(pk=response.json()["offer_id"])
        self.assertGreater(offer.created_at, parse_datetime(offer.envelope["issued_at"]))
        self.assertEqual(response.json()["shipping"]["status"], "pending")
        self.no_commerce_effects()

    def test_explicit_asof_clock_keeps_future_insert_and_existing_future_offer_fenced(self):
        request, decision = self.manager_offer(at=self.real_clock() - timedelta(seconds=1))
        asof = self.real_clock()
        with patch("django.utils.timezone.now", side_effect=self.real_clock), \
                self.assertRaisesMessage(shipping.SplitShippingError, "offer_expired"):
            shipping.issue_offer(self.client_row.pk, self.manager, decision.envelope_hmac,
                uuid.uuid4(), self.options(request), now=asof)
        self.assertFalse(Offer.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.filter(reason=shipping.ORIGIN).exists())
        with patch("django.utils.timezone.now", side_effect=self.real_clock):
            offer = shipping.issue_offer(self.client_row.pk, self.manager, decision.envelope_hmac,
                uuid.uuid4(), self.options(request))
        # Do not mask an already persisted future row by advancing an explicit
        # historical read to its created_at or by adding timestamp tolerance.
        self.assertEqual(shipping.current_offer_reason(offer, self.settings,
            now=parse_datetime(offer.envelope["issued_at"])), "offer_expired")
        self.no_commerce_effects()

    def options(self, request, *, payer="customer", cap="70.00"):
        lines = [row["line_id"] for row in request.envelope["lines"]]
        cap = "0.00" if payer == "merchant" else cap
        return {"together": {"parcels": [{"key": "p1", "line_ids": lines,
            "payer": payer, "customer_delivery_cap": cap, "currency": "UAH"}]},
            "separate": {"parcels": [{"key": "p1", "line_ids": [lines[0]],
                "payer": payer, "customer_delivery_cap": cap, "currency": "UAH"},
                {"key": "p2", "line_ids": [lines[1]], "payer": payer,
                    "customer_delivery_cap": cap, "currency": "UAH"}]}}

    def offer(self, *, locale="uk", payer="customer", cap="70.00", at=None):
        now = at or self.now
        request, decision = self.manager_offer(locale=locale, payer=payer, at=now)
        with patch("django.utils.timezone.now", return_value=now):
            offered = shipping.issue_offer(self.client_row.pk, actor=self.manager,
                expected_decision_digest=decision.envelope_hmac, operation_id=uuid.uuid4(),
                options=self.options(request, payer=payer, cap=cap), now=now)
        self.assertTrue(shipping.verified_offer(offered))
        return offered

    def complete_input(self):
        InstagramBotMessage.objects.filter(client=self.client_row, role="user").update(status="done")
        IgCustomerTurnRevision.objects.filter(client=self.client_row).update(
            state="processed", active_slot=None, claim_token="", lease_until=None)

    def send(self, offer, *, outcome="sent", at=None):
        from management.services import instagram_bot as bot
        self.complete_input()
        now = at or self.now + timedelta(seconds=1)
        physical = []
        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            if outcome == "unknown":
                raise TimeoutError("native card request reached provider; outcome unknown")
            return 200, json.dumps({"message_id": "native-split-offer:" + offer.pk.hex} if outcome == "sent" else {})
        with patch("django.utils.timezone.now", return_value=now), \
                patch.object(bot, "get_page_token", return_value="synthetic-split-token"), \
                patch.object(bot, "_provider_http", side_effect=provider):
            count = delivery.process_due_split_offers(self.settings, now=now, limit=20)
        offer.task.refresh_from_db()
        return count, physical

    def answer(self, offer, *, option="separate", payload=None, text=None, at=None, owner=None, attachments=None):
        body, buttons = shipping.render_offer(offer)
        encoded = shipping.encode_choice(offer, option)
        button = next(row for row in buttons if row.payload == encoded)
        now = at or self.now + timedelta(seconds=2)
        source = self.source(button.title if text is None else text,
            payload=encoded if payload is None else payload, at=now, client=owner)
        source.reply_to_provider_message_id = "native-split-offer:" + offer.pk.hex
        fields = ["reply_to_provider_message_id"]
        if attachments is not None:
            source.attachments = attachments
            fields.append("attachments")
        source.save(update_fields=fields)
        revision, _token = self.revision(source, now=now)
        result = shipping.apply_choice(source, revision, self.settings, now=now)
        return result, source, revision

    def sent_offer(self, **options):
        offer = self.offer(**options)
        count, physical = self.send(offer)
        self.assertEqual((count, len(physical)), (1, 1))
        receipt = Receipt.objects.get(offer=offer)
        self.assertEqual(receipt.native_mid, "native-split-offer:" + offer.pk.hex)
        text, buttons = shipping.render_offer(offer)
        from management.services.instagram_bot import _provider_quick_replies_payload
        self.assertEqual(physical[0]["message"], {"text": text,
            "quick_replies": _provider_quick_replies_payload(buttons)})
        return offer, receipt

    def test_issuance_creates_only_one_signed_offer_without_receipt_or_commerce(self):
        offer = self.offer()
        self.assertEqual(Offer.objects.count(), 1)
        self.assertFalse(Receipt.objects.exists())
        self.assertFalse(Choice.objects.exists())
        self.assertEqual(offer.task.reason, "split_shipping_offer")
        self.assertEqual(offer.task.trigger, "event")
        self.no_commerce_effects()

    def assert_localized_cost_card(self, locale):
        offer, _receipt = self.sent_offer(locale=locale)
        text, buttons = shipping.render_offer(offer)
        self.assertLessEqual(len(text.encode("utf-8")), 950)
        self.assertEqual(len(buttons), 2)
        self.assertTrue(all(len(button.title) <= 20 and button.payload.startswith("SS1:") for button in buttons))
        self.assertIn(self.product.title, text)
        self.assertIn(self.products[1].title, text)
        self.assertIn({"uk": "разом:", "ru": "вместе:", "en": "together:"}[locale], text.lower())
        self.assertIn({"uk": "окремо:", "ru": "отдельно:", "en": "separate:"}[locale], text.lower())
        # Customer liability is separate from the goods payment and bounded
        # per parcel; together costs one cap, separate costs two caps.
        self.assertEqual(text.count("70.00 UAH"), 4)
        self.assertEqual(text.count("140.00 UAH"), 1)
        self.assertIn({"uk": "оплата перевізнику окремо: до", "ru": "оплата перевозчику отдельно: до",
            "en": "paid separately to the carrier: up to"}[locale], text.lower())
        self.assertIn({"uk": "разом для вас: до", "ru": "итого для вас: до", "en": "your total: up to"}[locale], text.lower())
        self.assertNotIn("TwoComms", text)
        self.no_commerce_effects()

    def test_uk_native_card_discloses_two_options_exact_items_customer_cost_and_payer(self):
        self.assert_localized_cost_card("uk")

    def test_ru_native_card_discloses_two_options_exact_items_customer_cost_and_payer(self):
        self.assert_localized_cost_card("ru")

    def test_en_native_card_discloses_two_options_exact_items_customer_cost_and_payer(self):
        self.assert_localized_cost_card("en")

    def test_merchant_payer_discloses_zero_customer_cost_without_order_or_stock_effect(self):
        offer, _receipt = self.sent_offer(payer="merchant")
        text, _buttons = shipping.render_offer(offer)
        self.assertEqual(text.count("0.00 UAH"), 5)
        self.assertIn("доставку оплачує TwoComms; для вас 0.00 UAH", text)
        self.assertNotIn("оплата перевізнику окремо", text)
        result, _source, _revision = self.answer(offer)
        self.assertTrue(result.ready, result)
        self.assertTrue(shipping.verified_choice(Choice.objects.get(pk=result.choice_id)))
        self.no_commerce_effects()

    def test_actual_native_separate_choice_binds_actual_receipt_and_all_whole_line_quantities(self):
        offer, receipt = self.sent_offer()
        result, source, _revision = self.answer(offer)
        self.assertTrue(result.handled, result)
        self.assertTrue(result.ready, result)
        self.assertEqual(str(result.offer_id), str(offer.pk))
        choice = Choice.objects.get(pk=result.choice_id)
        self.assertTrue(shipping.verified_choice(choice))
        self.assertEqual(choice.offer_id, offer.pk)
        self.assertEqual(choice.receipt_id, receipt.pk)
        self.assertEqual(choice.source_id, source.pk)
        self.assertEqual(choice.option, "separate")
        self.assertEqual(choice.envelope["option"], "separate")
        parcels = offer.envelope["options"][choice.option]["parcels"]
        expected_lines = [line["line_id"] for line in offer.decision.request.envelope["lines"]]
        self.assertEqual([parcel["line_ids"] for parcel in parcels], [[line] for line in expected_lines])
        self.assertEqual([line["target"]["quantity"] for line in offer.decision.request.envelope["lines"]], [2, 2])
        self.assertEqual([parcel["customer_delivery_cap"] for parcel in parcels], ["70.00", "70.00"])
        self.assertEqual(shipping.shipping_projection(self.client_row, self.settings,
            now=self.now + timedelta(seconds=2))["status"], "chosen")
        self.assertEqual(Choice.objects.count(), 1)
        self.no_commerce_effects()

    def test_actual_native_together_choice_does_not_create_available_subset_or_commerce(self):
        offer, receipt = self.sent_offer()
        result, _source, _revision = self.answer(offer, option="together")
        self.assertTrue(result.ready, result)
        choice = Choice.objects.get(pk=result.choice_id)
        self.assertEqual(choice.offer_id, offer.pk)
        self.assertEqual(choice.receipt_id, receipt.pk)
        self.assertTrue(shipping.verified_choice(choice))
        self.assertEqual(choice.option, "together")
        self.no_commerce_effects()

    def test_duplicate_same_native_source_reuses_choice_once_without_customer_or_commerce_effects(self):
        offer, _receipt = self.sent_offer()
        first, source, revision = self.answer(offer)
        self.assertTrue(first.ready, first)
        with patch("management.services.instagram_bot._provider_http") as provider:
            replay = shipping.apply_choice(source, revision, self.settings, now=source.provider_created_at)
        provider.assert_not_called()
        self.assertEqual(str(replay.choice_id), str(first.choice_id))
        self.assertEqual(Choice.objects.count(), 1)
        self.no_commerce_effects()

    def test_bare_yes_and_copied_payload_text_have_no_native_choice_authority(self):
        offer, _receipt = self.sent_offer()
        for text in ("Так", shipping.encode_choice(offer, "separate")):
            with self.subTest(text=text):
                result, _source, _revision = self.answer(offer, payload="", text=text)
                self.assertFalse(result.ready, result)
                self.assertFalse(Choice.objects.exists())

    def test_foreign_customer_cannot_accept_another_customers_actual_offer(self):
        offer, _receipt = self.sent_offer()
        foreign = IgClient.objects.create(igsid="foreign-split-customer", reply_permission_epoch=3,
            last_user_message_at=self.now)
        result, _source, _revision = self.answer(offer, owner=foreign)
        self.assertFalse(result.ready, result)
        self.assertFalse(Choice.objects.exists())

    def test_signed_payload_with_wrong_button_body_never_grants_choice(self):
        offer, _receipt = self.sent_offer()
        result, _source, _revision = self.answer(offer, text="Так, але інші умови")
        self.assertFalse(result.ready, result)
        self.assertFalse(Choice.objects.exists())

    def test_native_choice_mixed_with_another_customer_requirement_has_no_choice_authority(self):
        from management.services.ig_turn_revisions import (
            create_collecting_revision, claim_revision_preparation, seal_revision, claim_sealed_revision,
        )
        offer, _receipt = self.sent_offer()
        _text, buttons = shipping.render_offer(offer)
        payload = shipping.encode_choice(offer, "separate")
        button = next(row for row in buttons if row.payload == payload)
        now = self.now + timedelta(seconds=2)
        selected = self.source(button.title, payload=payload, at=now)
        extra = self.source("Але суму доставки ще не погоджую", at=now)
        turn = IgCustomerTurn.objects.create(client=self.client_row, primary_source_message=selected,
            window_started_at=now, window_deadline=now)
        for index, source in enumerate((selected, extra), 1):
            IgTurnMessage.objects.create(turn=turn, message=source, ordinal=index, role="user")
        made = create_collecting_revision(turn, [selected, extra], now=now, bypass_quiet=True)
        prepared = claim_revision_preparation(made.revision.pk, now=now)
        sealed = seal_revision(made.revision.pk, prepared.token, now=now)
        claimed = claim_sealed_revision(sealed.revision.pk, now=now)
        self.assertIsNotNone(claimed.revision, claimed.reason)
        result = shipping.apply_choice(selected, claimed.revision, self.settings, now=now)
        self.assertFalse(result.ready, result)
        self.assertFalse(Choice.objects.exists())
        self.no_commerce_effects()

    def test_tampered_native_signature_cannot_select_any_option(self):
        offer, _receipt = self.sent_offer()
        payload = shipping.encode_choice(offer, "separate")
        forged = payload[:-1] + ("0" if payload[-1] != "0" else "1")
        result, _source, _revision = self.answer(offer, payload=forged)
        self.assertFalse(result.ready, result)
        self.assertFalse(Choice.objects.exists())

    def test_signed_native_payload_with_attachment_never_grants_choice(self):
        offer, _receipt = self.sent_offer()
        result, _source, _revision = self.answer(offer,
            attachments=[{"type": "image", "payload": {"url": "https://example.test/receipt.jpg"}}])
        self.assertFalse(result.ready, result)
        self.assertFalse(Choice.objects.exists())

    def test_unknown_actual_native_card_send_cannot_grant_or_blindly_replay(self):
        offer = self.offer()
        count, physical = self.send(offer, outcome="unknown")
        self.assertEqual((count, len(physical)), (0, 1))
        self.assertFalse(Receipt.objects.exists())
        result, _source, _revision = self.answer(offer)
        self.assertFalse(result.ready, result)
        self.assertFalse(Choice.objects.exists())
        self.assertEqual(self.send(offer, at=self.now + timedelta(seconds=3)), (0, []))
        self.no_commerce_effects()

    def test_http_success_without_native_card_mid_cannot_grant_or_replay(self):
        offer = self.offer()
        count, physical = self.send(offer, outcome="missing_mid")
        self.assertEqual((count, len(physical)), (0, 1))
        self.assertFalse(Receipt.objects.exists())
        result, _source, _revision = self.answer(offer)
        self.assertFalse(result.ready, result)
        self.assertFalse(Choice.objects.exists())
        self.assertEqual(self.send(offer, at=self.now + timedelta(seconds=3)), (0, []))

    def test_unsent_task_cannot_mint_receipt_from_an_arbitrary_native_mid(self):
        offer = self.offer()
        receipt = shipping.record_delivery(offer.task, "unseen-native-card", now=self.now)
        self.assertIsNone(receipt)
        self.assertFalse(Receipt.objects.exists())
        result, _source, _revision = self.answer(offer)
        self.assertFalse(result.ready, result)
        self.assertFalse(Choice.objects.exists())

    def test_actual_native_receipt_cannot_authorize_changed_or_partial_card_body(self):
        offer, receipt = self.sent_offer()
        # Keep the actual immutable native receipt; corrupt only its mutable
        # current task projection to a partial body and require exact binding.
        IgFollowUpTask.objects.filter(pk=offer.task_id).update(message_text=offer.task.message_text[:30])
        result, _source, _revision = self.answer(offer)
        self.assertFalse(result.ready, result)
        self.assertFalse(Choice.objects.exists())
        self.assertEqual(Receipt.objects.get(pk=receipt.pk).native_mid, receipt.native_mid)

    def test_full_partition_rejects_omitted_duplicate_foreign_lines_and_partial_quantities(self):
        request, decision = self.manager_offer()
        options = self.options(request)
        invalid = []
        omitted = deepcopy(options)
        omitted["together"]["parcels"][0]["line_ids"].pop()
        invalid.append(omitted)
        duplicate = deepcopy(options)
        duplicate["separate"]["parcels"][1]["line_ids"] = duplicate["separate"]["parcels"][0]["line_ids"][:]
        invalid.append(duplicate)
        foreign = deepcopy(options)
        foreign["separate"]["parcels"][1]["line_ids"] = ["line-from-another-basket"]
        invalid.append(foreign)
        partial = deepcopy(options)
        partial["separate"]["parcels"][0]["quantity"] = 1
        invalid.append(partial)
        for frame in invalid:
            with self.subTest(frame=frame), self.assertRaises(shipping.SplitShippingError):
                shipping.issue_offer(self.client_row.pk, actor=self.manager,
                    expected_decision_digest=decision.envelope_hmac, operation_id=uuid.uuid4(),
                    options=frame, now=self.now)
        self.assertFalse(Offer.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.filter(reason="split_shipping_offer").exists())
        self.no_commerce_effects()

    def test_same_issue_operation_reuses_one_offer_and_conflicting_cost_frame_is_rejected(self):
        request, decision = self.manager_offer()
        operation = uuid.uuid4()
        args = {"actor": self.manager, "expected_decision_digest": decision.envelope_hmac,
            "operation_id": operation, "options": self.options(request), "now": self.now}
        first = shipping.issue_offer(self.client_row.pk, **args)
        second = shipping.issue_offer(self.client_row.pk, **args)
        self.assertEqual(first.pk, second.pk)
        with self.assertRaises(shipping.SplitShippingError):
            shipping.issue_offer(self.client_row.pk, **{**args, "options": self.options(request, cap="80.00")})
        self.assertEqual(Offer.objects.count(), 1)
        self.assertEqual(IgFollowUpTask.objects.filter(reason="split_shipping_offer").count(), 1)
        self.no_commerce_effects()

    def assert_choice_invalidated(self, mutate):
        offer, _receipt = self.sent_offer()
        mutate(offer)
        with patch("management.services.instagram_bot._provider_http") as provider:
            result, _source, _revision = self.answer(offer, at=self.now + timedelta(seconds=4))
        provider.assert_not_called()
        self.assertFalse(result.ready, result)
        self.assertFalse(Choice.objects.exists())
        self.no_commerce_effects()

    def test_quantity_change_invalidates_exact_original_offer(self):
        self.assert_choice_invalidated(lambda _offer: self.canonical("количество 3", at=self.now + timedelta(seconds=3)))

    def test_color_correction_invalidates_exact_original_offer(self):
        self.assert_choice_invalidated(lambda _offer: self.canonical("цвет black", at=self.now + timedelta(seconds=3)))

    def test_new_deadline_invalidates_original_manager_and_cost_frame(self):
        self.assert_choice_invalidated(lambda _offer: self.revision(
            self.source("Потрібна відправка до 14.10.2026", at=self.now + timedelta(seconds=3))))

    def test_privacy_erasure_invalidates_offer_before_actual_native_choice(self):
        self.assert_choice_invalidated(lambda _offer: IgClient.objects.filter(pk=self.client_row.pk).update(
            privacy_erasure_started_at=self.now))

    def test_settings_permission_epoch_change_invalidates_offer_before_choice(self):
        self.assert_choice_invalidated(lambda _offer: type(self.settings).objects.filter(pk=self.settings.pk).update(
            reply_permission_epoch=self.settings.reply_permission_epoch + 1))

    def test_actual_configuration_change_invalidates_offer_before_choice(self):
        self.assert_choice_invalidated(lambda _offer: self.product.fit_options.filter(code="classic").update(
            label="Changed physical configuration label"))

    def test_reset_invalidates_original_offer_before_choice(self):
        self.assert_choice_invalidated(lambda offer: IgFunnelResetAudit.objects.create(
            client=self.client_row, reset_after_message_id=offer.decision.request.source_id, reason="split test reset"))

    def test_expired_offer_cannot_be_accepted_through_historical_native_receipt(self):
        offer, _receipt = self.sent_offer()
        result, _source, _revision = self.answer(offer, at=offer.expires_at + timedelta(microseconds=1))
        self.assertFalse(result.ready, result)
        self.assertFalse(Choice.objects.exists())
        self.no_commerce_effects()

    def test_new_cost_offer_supersedes_old_native_button_and_needs_its_own_receipt(self):
        first, _receipt = self.sent_offer()
        request = first.decision.request
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=2)):
            second = shipping.issue_offer(self.client_row.pk, actor=self.manager,
                expected_decision_digest=first.decision.envelope_hmac, operation_id=uuid.uuid4(),
                options=self.options(request, cap="80.00"), now=self.now + timedelta(seconds=2))
        self.assertNotEqual(second.pk, first.pk)
        self.assertFalse(Receipt.objects.filter(offer=second).exists())
        result, _source, _revision = self.answer(first, at=self.now + timedelta(seconds=3))
        self.assertFalse(result.ready, result)
        self.assertFalse(Choice.objects.exists())
        count, physical = self.send(second, at=self.now + timedelta(seconds=4))
        self.assertEqual((count, len(physical)), (1, 1))
        result, _source, _revision = self.answer(second, at=self.now + timedelta(seconds=5))
        self.assertTrue(result.ready, result)
        self.assertEqual(Choice.objects.count(), 1)
        self.no_commerce_effects()
