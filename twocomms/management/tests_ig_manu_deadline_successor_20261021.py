"""Native successor reviews cannot erase an exact item's earlier hard cutoff."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
import uuid

from django.test import TestCase, override_settings

from management.services import ig_fulfillment_reviews as reviews
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_fulfillment_checkout import (
    deadline_later_acceptance, explicit_later_receipt_option, fulfillment_deadline_checkout_reason,
)
from management.services.ig_fulfillment_requirements import active_hard_requirements
from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeDeadlineSuccessorTests(_RestockFixture, TestCase):
    def setUp(self):
        super().setUp()
        transport = patch("management.services.instagram_bot._provider_http")
        self.transport = transport.start()
        self.addCleanup(transport.stop)
        self.addCleanup(self.transport.assert_not_called)

    def native_case(self):
        from management.tests_ig_manu_deadline_20261021 import NativeDepartureReviewTests
        return NativeDepartureReviewTests.native_case(self)

    def numeric_client(self):
        from management.models import IgClient
        self.client_row = IgClient.objects.create(igsid="17840000000363", language="uk",
            reply_permission_epoch=3, last_user_message_at=self.now)

    def poll_source(self, text, *, at, signed=True):
        from django.db import transaction
        from management.models import InstagramBotMessage
        from management.services.ig_ingress_source_proof import build_polled_ingress_proof, bind_polled_ingress_proof
        from management.services.instagram_bot import ingress_provider_namespace, _provider_account_id
        self.serial += 1
        mid = f"native-deadline-poll-{self.serial}"
        provider = {"id": mid, "from": {"id": self.client_row.igsid},
            "to": {"data": [{"id": _provider_account_id(self.settings)}]},
            "created_time": at.isoformat(), "message": text}
        proof = build_polled_ingress_proof(self.settings, provider,
            conversation_id="native-deadline-provider-conversation", now=at)
        self.assertTrue(proof)
        with transaction.atomic():
            source = InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
                source="poll", role="user", status="pending", text=text, mid=mid,
                provider_namespace=ingress_provider_namespace(self.settings), provider_created_at=at)
            if signed:
                bind_polled_ingress_proof(source, proof)
        return source

    def successor(self):
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        first, decision, manager, _ = self.native_case()
        original_request, original_decision = deepcopy(first.envelope), deepcopy(decision.envelope)
        ProductInventoryPolicy.objects.create(product=self.products[1], source="catalog_variant")
        self.variants[1].stock = 0
        self.variants[1].save(update_fields=["stock"])
        VariantSizeRule.objects.create(variant=self.variants[1], fit_code="classic", size="L", stock=0, is_enabled=True)
        self.canonical(f"добавьте {self.products[1].title}, количество 1, размер L, цвет pink, крій classic")
        question = self.source("Are these items available?")
        revision, _ = self.revision(question)
        prepared = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(prepared["ready"], prepared)
        second = reviews.Request.objects.get(pk=prepared["request_id"])
        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(len(second.envelope["lines"]), 2)
        self.assertFalse(active_hard_requirements(second.envelope["customer_requirements"]))
        result = reviews.apply_fulfillment_decision(second.pk, actor=manager,
            expected_request_digest=second.envelope_hmac, operation_id=uuid.uuid4(),
            decisions_by_line=[{"line_id": row["line_id"], "availability": "wait",
                "preparation": {"min_days": 1, "max_days": 3, "unit": "calendar",
                    "anchor": "after_payment_confirmation"}} for row in second.envelope["lines"]], now=self.now)
        self.assertTrue(result["ready"], result)
        first.refresh_from_db()
        decision.refresh_from_db()
        self.assertEqual(first.envelope, original_request)
        self.assertEqual(decision.envelope, original_decision)
        return first, decision, second

    def cart(self, *, now=None):
        return capture_current_selection_lines(self.client_row.pk, now=now or self.now)

    def gate(self, *, now=None):
        return fulfillment_deadline_checkout_reason(self.client_row, self.cart(now=now), now=now or self.now)

    def test_real_expanded_review_without_cutoff_retains_only_original_exact_target_hold(self):
        first, decision, second = self.successor()
        cart = self.cart()
        current = reviews.fulfillment_review_projection(self.client_row.pk, now=self.now, current_source_cart=cart)
        self.assertEqual((current["status"], current["current_request"]["id"]), ("reviewed", str(second.pk)))
        self.assertEqual(self.gate(), "checkout_hard_deadline_later_receipt_consent_required")
        # Removing the original exact garment leaves B's independent review
        # usable, without assigning A's departure cutoff to the added item.
        self.canonical("уберите позицию 1")
        remaining = self.cart()
        self.assertEqual(len(remaining["lines"]), 1)
        self.assertEqual(remaining["lines"][0]["fields"]["product_id"]["value"], self.products[1].pk)
        self.assertEqual(self.gate(), "")
        self.assertTrue(reviews._verified(first, "request"))
        self.assertTrue(reviews._verified(decision, "decision"))

    def test_expanded_cart_explicit_later_receipt_releases_exact_reviewed_target(self):
        first, decision, _ = self.successor()
        later = self.now + timedelta(minutes=1)
        source = self.source("Please send it later to my boyfriend in Kharkiv", at=later)
        self.revision(source, now=later)
        self.assertIsNone(deadline_later_acceptance(first, decision, self.cart(now=later), now=later,
            _allow_target_subset=True))
        self.assertEqual(self.gate(now=later), "checkout_hard_deadline_later_receipt_consent_required")
        later += timedelta(minutes=1)
        source = self.source(f"Please send {self.product.title} later to my boyfriend in Kharkiv", at=later)
        self.revision(source, now=later)
        proof = deadline_later_acceptance(first, decision, self.cart(now=later), now=later,
            _allow_target_subset=True)
        self.assertIsNotNone(proof)
        self.assertEqual(proof["decision_digest"], decision.envelope_hmac)
        self.assertEqual(self.gate(now=later), "")

    def test_native_uk_ru_positive_receiving_instruction_remains_available(self):
        first, decision, _, _ = self.native_case()
        for index, text in enumerate(("Я згодна отримати пізніше через хлопця в Харкові",
                "Я согласен получить позже через парня в Харькове"), 1):
            with self.subTest(text=text):
                at = self.now + timedelta(minutes=index)
                source = self.source(text, at=at)
                self.revision(source, now=at)
                self.assertEqual(explicit_later_receipt_option(text), "later_domestic_contact")
                self.assertIsNotNone(deadline_later_acceptance(first, decision, self.cart(now=at), now=at))
                self.assertEqual(self.gate(now=at), "")

    def test_native_uk_ru_en_refusal_reported_hypothetical_and_punctuation_never_release_cutoff(self):
        self.successor()
        texts = (
            "Я не хочу отримати пізніше через хлопця в Харкові",
            "Я не. Хочу отримати пізніше через хлопця в Харкові",
            "Я не хочу. Отримати пізніше через хлопця в Харкові",
            "Я не согласен получить позже через парня в Харькове",
            "Я несогласен получить позже через парня в Харькове",
            "Я отказываюсь получить позже через парня в Харькове",
            "I do not agree to receive it later via Nova Poshta",
            "I didn't agree to receive it later via Nova Poshta",
            "I don - t agree to receive it later via Nova Poshta",
            "I could receive it later via Nova Poshta",
            "I refuse to receive it later via Nova Poshta",
            "My boyfriend said send it later to my boyfriend in Kharkiv",
            "Він каже отримати пізніше через хлопця в Харкові",
            "Он написал получить позже через парня в Харькове",
            '"Please send it later to my boyfriend in Kharkiv"',
            '"Please send it later to my boyfriend in Kharkiv',
            "Якщо отримати пізніше через хлопця в Харкові",
            "Если получить позже через парня в Харькове",
            "Maybe send it later to my boyfriend in Kharkiv",
        )
        for index, text in enumerate(texts, 1):
            with self.subTest(text=text):
                at = self.now + timedelta(seconds=index)
                source = self.source(text, at=at)
                self.revision(source, now=at)
                self.assertIsNone(explicit_later_receipt_option(text))
                self.assertEqual(self.gate(now=at), "checkout_hard_deadline_later_receipt_consent_required")

    def test_sealed_deadline_withdrawal_releases_hold_but_unsealed_text_does_not(self):
        self.successor()
        at = self.now + timedelta(minutes=1)
        source = self.source("Cancel all deadlines", at=at)
        self.assertEqual(self.gate(now=at), "checkout_hard_deadline_later_receipt_consent_required")
        self.revision(source, now=at)
        self.assertEqual(self.gate(now=at), "")

    def test_current_catalogue_rename_cannot_retarget_a_sealed_review_withdrawal(self):
        from management.services.ig_fulfillment_checkout import _deadline_requirement_update_applies
        first, _, _ = self.successor()
        self.product.title = self.products[1].title
        self.product.save(update_fields=["title"])
        at = self.now + timedelta(minutes=1)
        source = self.source(f"Cancel the deadline for {self.products[1].title}", at=at)
        self.revision(source, now=at)
        cart = self.cart(now=at)
        self.assertFalse(_deadline_requirement_update_applies(first, source, cart, now=at))
        # Existing canonical-selection authority also refuses the now
        # ambiguous catalogue identity; no current chargeable cart is forged.
        self.assertFalse(cart.get("status") == "captured" and cart.get("coverage_complete"))

    def test_new_b_cutoff_and_b_withdrawal_do_not_release_original_a_deadline(self):
        self.successor()
        texts = (f"For {self.products[1].title}, I leave tomorrow at 11 AM",
            f"Cancel the deadline for {self.products[1].title}",
            f"Cancel all deadlines for {self.products[1].title}", "Cancel the deadline")
        for index, text in enumerate(texts, 1):
            with self.subTest(text=text):
                at = self.now + timedelta(seconds=index)
                source = self.source(text, at=at)
                self.revision(source, now=at)
                self.assertEqual(self.gate(now=at), "checkout_hard_deadline_later_receipt_consent_required")
        at = self.now + timedelta(minutes=1)
        source = self.source(f"Cancel the deadline for {self.product.title}", at=at)
        self.revision(source, now=at)
        self.assertEqual(self.gate(now=at), "")

    def test_settings_permission_reset_and_new_purchase_scope_do_not_reuse_old_hold(self):
        from management.models import IgFunnelResetAudit
        self.successor()
        cart = self.cart()
        for key, value in (("episode_id", cart["scope"]["episode_id"] + 1),
                ("source_namespace", "instagram_login:other-owner"), ("reset_floor", 99999)):
            changed = deepcopy(cart)
            changed["scope"][key] = value
            self.assertEqual(fulfillment_deadline_checkout_reason(self.client_row, changed, now=self.now), "")
        changed = deepcopy(cart)
        changed["generation"] += 1
        self.assertEqual(fulfillment_deadline_checkout_reason(self.client_row, changed, now=self.now), "")
        self.settings.reply_permission_epoch += 1
        self.settings.save(update_fields=["reply_permission_epoch"])
        self.assertEqual(self.gate(), "")
        self.settings.reply_permission_epoch -= 1
        self.settings.save(update_fields=["reply_permission_epoch"])
        self.assertEqual(self.gate(), "checkout_hard_deadline_later_receipt_consent_required")
        IgFunnelResetAudit.objects.create(client=self.client_row,
            reset_after_message_id=cart["source_watermark"]["message_id"], reason="new native customer scope")
        self.assertEqual(self.gate(), "")

    def test_changed_original_source_or_hmac_is_not_reused_as_a_live_hold(self):
        first, _, _ = self.successor()
        from django.db import connection
        import json
        original = deepcopy(first.envelope)
        first.envelope["customer_requirements"][0]["raw_quote"] = "changed signed evidence"
        # Deliberate disposable-fixture corruption bypasses the append-only
        # application API, proving the reader checks the persisted HMAC.
        table = connection.ops.quote_name(reviews.Request._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET envelope=%s WHERE id=%s", [json.dumps(first.envelope), first.pk.hex])
        self.assertEqual(self.gate(), "")
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET envelope=%s WHERE id=%s", [json.dumps(original), first.pk.hex])
        self.assertEqual(self.gate(), "checkout_hard_deadline_later_receipt_consent_required")
        from management.models import InstagramBotMessage
        old_source_id = original["sources"][0]["message_id"]
        InstagramBotMessage.objects.filter(pk=old_source_id).update(text="changed original source")
        self.assertEqual(self.gate(), "")

    def test_signed_current_poll_before_sku_keeps_original_deadline_after_native_selection(self):
        self.numeric_client()
        first = self.poll_source("I leave tomorrow at 10 AM", at=self.now)
        self.revision(first)
        self.canonical(f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic")
        question = self.source("Is this item available?")
        revision, _ = self.revision(question)
        prepared = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(prepared["ready"], prepared)
        request = reviews.Request.objects.get(pk=prepared["request_id"])
        hard = active_hard_requirements(request.envelope["customer_requirements"])
        self.assertEqual([hint["source_id"] for hint in hard], [first.pk])
        self.assertIn(first.pk, [proof["message_id"] for proof in request.envelope["sources"]])
        current = reviews.fulfillment_review_projection(self.client_row.pk,
            now=self.now, current_source_cart=self.cart())
        self.assertEqual(current["status"], "pending", current.get("reason"))
        self.assertEqual(self.gate(), "checkout_hard_deadline_review_required")

    def test_signed_current_poll_receiving_consent_and_unsigned_label_do_not_share_authority(self):
        self.numeric_client()
        self.canonical(f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic")
        request, decision, _, _ = self.native_case()
        at = self.now + timedelta(minutes=1)
        unsigned = self.poll_source("Please send it later to my boyfriend in Kharkiv", at=at, signed=False)
        # A bare poll label may exist as untrusted ingress data; sealing cannot
        # turn it into source authority, even if the body looks affirmative.
        from management.services.ig_purpose_sources import owned_source
        revision, _ = self.revision(unsigned, now=at)
        self.assertIsNone(owned_source(revision, unsigned, self.settings, require_head=False)[0])
        self.assertIsNone(deadline_later_acceptance(request, decision, self.cart(now=at), now=at))
        self.assertEqual(self.gate(now=at), "checkout_hard_deadline_later_receipt_consent_required")
        at += timedelta(minutes=1)
        signed = self.poll_source("Please send it later to my boyfriend in Kharkiv", at=at)
        self.revision(signed, now=at)
        proof = deadline_later_acceptance(request, decision, self.cart(now=at), now=at)
        self.assertIsNotNone(proof)
        self.assertEqual(proof["source"]["message_id"], signed.pk)
        self.assertEqual(self.gate(now=at), "")

    def test_unsigned_poll_before_sku_cannot_supply_a_deadline_or_an_inherited_source(self):
        self.numeric_client()
        unsigned = self.poll_source("I leave tomorrow at 10 AM", at=self.now, signed=False)
        self.revision(unsigned)
        self.canonical(f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic")
        question = self.source("Is this item available?")
        revision, _ = self.revision(question)
        prepared = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(prepared["ready"], prepared)
        request = reviews.Request.objects.get(pk=prepared["request_id"])
        self.assertFalse(active_hard_requirements(request.envelope["customer_requirements"]))
        self.assertNotIn(unsigned.pk, [proof["message_id"] for proof in request.envelope["sources"]])

    def test_unproved_poll_withdrawal_and_new_cutoff_cannot_erase_prior_signed_preselection_cutoff(self):
        self.numeric_client()
        first = self.poll_source("I leave tomorrow at 10 AM", at=self.now)
        self.revision(first)
        for index, body in enumerate(("Cancel all deadlines", "I leave tomorrow at 11 AM"), 1):
            unsigned = self.poll_source(body, at=self.now + timedelta(seconds=index), signed=False)
            self.revision(unsigned, now=unsigned.provider_created_at)
        at = self.now + timedelta(seconds=4)
        self.canonical(f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic", at=at)
        question = self.source("Is this item available?", at=at)
        revision, _ = self.revision(question)
        prepared = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=at)
        self.assertTrue(prepared["ready"], prepared)
        request = reviews.Request.objects.get(pk=prepared["request_id"])
        self.assertEqual([hint["source_id"] for hint in active_hard_requirements(
            request.envelope["customer_requirements"])], [first.pk])
        self.assertEqual(fulfillment_deadline_checkout_reason(self.client_row,
            self.cart(now=at), now=at),
            "checkout_hard_deadline_review_required")

    def select_after_history(self, *, at):
        self.canonical(f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic", at=at)
        question = self.source("Is this item available?", at=at)
        revision, _ = self.revision(question)
        prepared = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=at)
        self.assertTrue(prepared["ready"], prepared)
        return reviews.Request.objects.get(pk=prepared["request_id"]), revision, prepared

    def test_seventeen_ordinary_turns_cannot_displace_verified_preselection_cutoff(self):
        self.numeric_client()
        first = self.poll_source("I leave tomorrow at 10 AM", at=self.now)
        self.revision(first)
        for index in range(17):
            source = self.source("Thanks, I am still choosing my shirt", at=self.now + timedelta(seconds=index + 1))
            self.revision(source)
        at = self.now + timedelta(seconds=20)
        request, _, _ = self.select_after_history(at=at)
        self.assertEqual([hint["source_id"] for hint in active_hard_requirements(
            request.envelope["customer_requirements"])], [first.pk])
        self.assertNotIn("deadline_history", request.envelope)
        self.assertEqual(self.gate(now=at), "checkout_hard_deadline_review_required")

    def test_real_requirement_bound_is_signed_explained_and_reusable_until_owned_all_withdrawal(self):
        self.numeric_client()
        for index in range(17):
            source = self.poll_source("I leave tomorrow at 10 AM", at=self.now + timedelta(seconds=index))
            self.revision(source)
        at = self.now + timedelta(seconds=20)
        request, revision, prepared = self.select_after_history(at=at)
        raw = deepcopy(request.envelope)
        self.assertEqual(raw["deadline_history"]["reason"], "requirement_bound")
        self.assertTrue(raw["deadline_history"]["needs_customer_clarification"])
        self.assertTrue(prepared["ack_facts"]["deadline_clarification_required"])
        self.assertTrue(reviews._verified(request, "request"))
        self.assertEqual(self.gate(now=at), "checkout_hard_deadline_clarification_required")
        # The same ordinary review effect remains idempotent, with explicit
        # guidance in both the customer model context and manager task UI.
        repeated = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=at)
        self.assertEqual(repeated["request_id"], str(request.pk))
        self.assertFalse(repeated["created"])
        self.assertEqual(request.task.manager_context["deadline_history"], raw["deadline_history"])
        from management.services.ig_fulfillment_context import fulfillment_prompt_value
        projected = reviews.fulfillment_review_projection(self.client_row.pk, now=at, current_source_cart=self.cart(now=at))
        shown = fulfillment_prompt_value(projected)
        self.assertEqual(shown["current_request"]["deadline_history"], raw["deadline_history"])
        from management.services.ig_admin_state_capture import current_admin_state
        from management.services.ig_client_state_card import render_client_state_prompt
        captured = current_admin_state(self.client_row.pk, now=at)
        self.assertEqual(captured.status, "captured")
        prompt = render_client_state_prompt(captured.state, budget=2400,
            excluded_slots=("communication.preferences", "context.useful_memory"))
        self.assertFalse(prompt.oversized, prompt.estimated_tokens)
        self.assertIn("deadline_history", prompt.text)
        self.assertIn("ALL earlier deadlines", prompt.text)
        request.refresh_from_db()
        self.assertEqual(request.envelope, raw)
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        ProductInventoryPolicy.objects.create(product=self.products[1], source="catalog_variant")
        self.variants[1].stock = 0
        self.variants[1].save(update_fields=["stock"])
        VariantSizeRule.objects.create(variant=self.variants[1], fit_code="classic", size="L", stock=0, is_enabled=True)
        self.canonical(f"добавьте {self.products[1].title}, количество 1, размер L, цвет pink, крій classic", at=at)
        question = self.source("Are these items available?", at=at)
        successor_revision, _ = self.revision(question)
        successor = reviews.prepare_fulfillment_review(successor_revision, settings_obj=self.settings, now=at)
        self.assertTrue(successor["ready"], successor)
        self.assertNotEqual(successor["request_id"], str(request.pk))
        self.assertEqual(self.gate(now=at), "checkout_hard_deadline_clarification_required")
        # Seventeen newer ordinary sources also cannot turn unknown into
        # absent. The latest owned explicit ALL instruction can resolve it.
        for index in range(17):
            source = self.source("Thanks, still choosing", at=at + timedelta(seconds=index + 1))
            self.revision(source)
        later = at + timedelta(seconds=20)
        source = self.source("Cancel the deadline for another item", at=later)
        self.revision(source)
        self.assertEqual(self.gate(now=later), "checkout_hard_deadline_clarification_required")
        later += timedelta(seconds=1)
        source = self.source("Cancel all deadlines", at=later)
        self.assertEqual(self.gate(now=later), "checkout_hard_deadline_clarification_required")
        final_revision, _ = self.revision(source)
        self.assertEqual(self.gate(now=later), "")
        final = reviews.prepare_fulfillment_review(final_revision, settings_obj=self.settings, now=later)
        self.assertTrue(final["ready"], final)
        self.assertNotIn("deadline_history", reviews.Request.objects.get(pk=final["request_id"]).envelope)
        projected = reviews.fulfillment_review_projection(self.client_row.pk, now=later, current_source_cart=self.cart(now=later))
        self.assertEqual(projected["current_request"]["active_hard_requirement_digest"], "")
        self.assertEqual(self.gate(now=later), "")
        for index in range(17):
            source = self.source("Thanks, still choosing", at=later + timedelta(seconds=index + 1))
            self.revision(source)
        later += timedelta(seconds=20)
        question = self.source("Are these items available?", at=later)
        next_revision, _ = self.revision(question)
        next_result = reviews.prepare_fulfillment_review(next_revision, settings_obj=self.settings, now=later)
        self.assertTrue(next_result["ready"], next_result)
        self.assertNotIn("deadline_history", reviews.Request.objects.get(pk=next_result["request_id"]).envelope)
        self.assertEqual(self.gate(now=later), "")

    def test_candidate_bound_distinguishes_unknown_history_from_no_deadline(self):
        self.numeric_client()
        first = self.poll_source("I leave tomorrow at 10 AM", at=self.now)
        self.revision(first)
        for index in range(64):
            source = self.source("Which date is shown on your calendar?", at=self.now + timedelta(seconds=index + 1))
            self.revision(source)
        at = self.now + timedelta(seconds=70)
        request, _, _ = self.select_after_history(at=at)
        self.assertEqual(request.envelope["deadline_history"]["reason"], "candidate_bound")
        self.assertEqual(self.gate(now=at), "checkout_hard_deadline_clarification_required")

    def test_generic_size_change_cannot_withdraw_a_sealed_cutoff_or_old_changed_hint(self):
        first, _, _, _ = self.native_case()
        initial = deepcopy(first.envelope)
        at = self.now + timedelta(minutes=1)
        text = f"Тепер змініть розмір {self.product.title}"
        source = self.source(text, at=at)
        self.revision(source)
        from management.services.ig_fulfillment_requirements import extract_requirements
        fresh = extract_requirements(text, provider_timestamp=at, now=at)
        self.assertEqual(fresh["supersession_hint"], "none")
        old_hint = {**fresh, "supersession_hint": "changed"}
        old_copy = deepcopy(old_hint)
        hard = active_hard_requirements([*initial["customer_requirements"], old_hint])
        self.assertTrue(hard)
        self.assertEqual(old_hint, old_copy)
        self.assertEqual(active_hard_requirements([*initial["customer_requirements"],
            {"supersession_hint": "withdrawn"}]), hard)
        self.assertEqual(self.gate(now=at), "checkout_hard_deadline_later_receipt_consent_required")
        from management.services.ig_fulfillment_requirements import requirement_digest
        projected = reviews.fulfillment_review_projection(self.client_row.pk, now=at, current_source_cart=self.cart(now=at))
        self.assertEqual(projected["current_request"]["active_hard_requirement_digest"], requirement_digest(hard[-1]))
        self.assertNotIn("active_hard_requirement_digest", first.envelope)
        source = self.source("Cancel all deadlines", at=at + timedelta(seconds=1))
        self.revision(source)
        self.assertEqual(self.gate(now=source.provider_created_at), "")

    def bundle_revision(self, sources, *, now):
        from management.models import IgCustomerTurn, IgTurnMessage
        from management.services.ig_turn_revisions import (create_collecting_revision,
            claim_revision_preparation, seal_revision, claim_sealed_revision)
        turn = IgCustomerTurn.objects.create(client=self.client_row, primary_source_message=sources[0],
            window_started_at=sources[0].provider_created_at, window_deadline=now)
        for ordinal, source in enumerate(sources, 1):
            IgTurnMessage.objects.create(turn=turn, message=source, ordinal=ordinal, role="user")
        created = create_collecting_revision(turn, sources, now=now, bypass_quiet=True)
        self.assertIsNotNone(created.revision, created.reason)
        prepared = claim_revision_preparation(created.revision.pk, now=now)
        sealed = seal_revision(created.revision.pk, prepared.token, now=now)
        self.assertTrue(sealed.sealed, sealed.reason)
        claimed = claim_sealed_revision(sealed.revision.pk, now=now)
        self.assertIsNotNone(claimed.revision, claimed.reason)
        return claimed.revision

    def assert_native_burst(self, count, *, real_requirements=False):
        sources = [self.source("I leave tomorrow at 10 AM" if real_requirements or index == 0
            else "Thanks, I am still choosing", at=self.now + timedelta(seconds=index))
            for index in range(count)]
        at = sources[-1].provider_created_at
        revision = self.bundle_revision(sources, now=at)
        ledger, digest = deepcopy(revision.bundle_snapshot), revision.snapshot_digest
        result = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=at)
        self.assertTrue(result["ready"], result)
        request = reviews.Request.objects.get(pk=result["request_id"])
        self.assertLessEqual(len(request.envelope["sources"]), 16)
        self.assertEqual(request.envelope["source_bundle"]["source_count"], count)
        self.assertEqual(request.envelope["source_bundle"]["revision_digest"], digest)
        self.assertIn(sources[-1].pk, [row["message_id"] for row in request.envelope["sources"]])
        self.assertTrue(reviews._verified(request, "request"))
        revision.refresh_from_db()
        self.assertEqual(revision.bundle_snapshot, ledger)
        self.assertEqual(revision.snapshot_digest, digest)
        self.assertEqual(revision.source_count, count)
        self.assertEqual(revision.sources.count(), count)
        if real_requirements:
            self.assertEqual(request.envelope["deadline_history"]["reason"], "own_requirement_bound")
            self.assertEqual(self.gate(now=at), "checkout_hard_deadline_clarification_required")
        else:
            self.assertNotIn("deadline_history", request.envelope)
            self.assertEqual([hint["source_id"] for hint in active_hard_requirements(
                request.envelope["customer_requirements"])], [sources[0].pk])
            self.assertEqual(self.gate(now=at), "checkout_hard_deadline_review_required")

    def test_native_seventeen_source_burst_keeps_cutoff_and_complete_revision_ledger(self):
        self.assert_native_burst(17)

    def test_native_thirtytwo_source_burst_keeps_cutoff_and_complete_revision_ledger(self):
        self.assert_native_burst(32)

    def test_native_seventeen_real_own_requirements_create_signed_incomplete_hold(self):
        self.assert_native_burst(17, real_requirements=True)

    def test_incomplete_deadline_change_keeps_old_hold_and_real_replacement_departure_is_reviewed(self):
        first, _, _, _ = self.native_case()
        for index, text in enumerate(("Change the deadline", "Змініть дату", "Тепер доставка до 24.10.2026"), 1):
            at = self.now + timedelta(seconds=index)
            source = self.source(text, at=at)
            self.revision(source)
            self.assertEqual(self.gate(now=at), "checkout_hard_deadline_later_receipt_consent_required")
            from management.services.ig_fulfillment_requirements import extract_requirements
            updated = extract_requirements(text, provider_timestamp=at, now=at)
            self.assertTrue(active_hard_requirements([*first.envelope["customer_requirements"], updated]))
        at = self.now + timedelta(minutes=1)
        source = self.source("Departure changed: I leave tomorrow at 11 AM", at=at)
        revision, _ = self.revision(source)
        prepared = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=at)
        self.assertTrue(prepared["ready"], prepared)
        replacement = reviews.Request.objects.get(pk=prepared["request_id"])
        self.assertEqual([hint["source_id"] for hint in active_hard_requirements(
            replacement.envelope["customer_requirements"])], [source.pk])
        self.assertEqual(self.gate(now=at), "checkout_hard_deadline_review_required")

    def test_native_refusal_to_cancel_never_withdraws_deadline_in_new_review_or_old_hint(self):
        first, _, _, _ = self.native_case()
        texts = ("I do not cancel the deadline", "I don't want to cancel all deadlines",
            "Не отменяйте срок", "Не скасовуйте дедлайн", "Не. Скасовуйте дедлайн",
            "The deadline is not cancelled", "I refuse to cancel the deadline",
            "I haven't cancelled the deadline", "I cannot cancel the deadline", '"Cancel all deadlines',
            "Maybe cancel the deadline", "Можливо скасуйте дедлайн", "Возможно отмените срок",
            "My boyfriend asked me to cancel all deadlines", "Do I need to cancel the deadline?")
        from management.services.ig_fulfillment_requirements import extract_requirements
        for index, text in enumerate(texts, 1):
            with self.subTest(text=text):
                at = self.now + timedelta(seconds=index)
                source = self.source(text, at=at)
                self.revision(source)
                hint = extract_requirements(text, provider_timestamp=at, now=at)
                self.assertNotEqual(hint["supersession_hint"], "withdrawn")
                self.assertTrue(active_hard_requirements([*first.envelope["customer_requirements"],
                    {**hint, "supersession_hint": "withdrawn"}]))
                self.assertEqual(self.gate(now=at), "checkout_hard_deadline_later_receipt_consent_required")
