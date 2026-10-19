"""Restock consent comes from actual selected goods and native card replies."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone as datetime_timezone
import hashlib
import json
import os
import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Max
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.ig_restock_subscription_models import (
    IgRestockInvitation, IgRestockReceipt, IgRestockAnswer, IgRestockSubscription,
)
from management.models import (BotPolicyPublication, IgClient, IgCustomerTurn,
    IgCustomerTurnRevision, IgFollowUpTask, IgFunnelResetAudit, IgTurnMessage,
    InstagramBotMessage, InstagramBotSettings)
from management.services import ig_restock_subscriptions as restock
from management.services.ig_revision_outbox import (PublicationBinding, plan_revision_effects,
    claim_next_effect, mark_provider_started, finish_effect)
from management.services.ig_turn_revisions import (create_collecting_revision,
    claim_revision_preparation, seal_revision, claim_sealed_revision)
from product_catalog.models import ProductInventoryPolicy, VariantSizeRule


class _RestockFixture:
    def setUp(self):
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests
        SourceCartCatalogTests.setUp(self)
        cache.clear()
        reviews = override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True)
        reviews.enable()
        self.addCleanup(reviews.disable)
        self.now = datetime(2026, 10, 12, 9, 0, tzinfo=datetime_timezone.utc)
        clock = patch("django.utils.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        transport = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login",
            "IG_INSTAGRAM_BOT": "synthetic-restock-test-token"})
        transport.start()
        self.addCleanup(transport.stop)
        snapshot = {"schema_version": 1, "instructions": []}
        digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        version = int(BotPolicyPublication.objects.aggregate(value=Max("version"))["value"] or 0) + 1
        self.publication = BotPolicyPublication.objects.create(version=version, kind="publish", schema_version=1,
            snapshot=snapshot, snapshot_hash=digest, compiler_version="instruction-set-v1", instruction_count=0)
        self.settings, _ = InstagramBotSettings.objects.update_or_create(pk=1, defaults={
            "is_enabled": True, "ig_user_id": "owner-restock", "reply_permission_epoch": 4,
            "active_instruction_publication": self.publication,
        })
        self.binding = PublicationBinding(self.publication.pk, self.publication.version, digest)
        self.client_row = IgClient.objects.create(igsid="restock-source-customer", language="uk",
            reply_permission_epoch=3, last_user_message_at=self.now)
        self.product, self.variant = self.products[0], self.variants[0]
        self.product.title = "Restock Aurora"
        self.product.save(update_fields=["title"])
        self.variant.stock = 0
        self.variant.save(update_fields=["stock"])
        self.rule = VariantSizeRule.objects.create(variant=self.variant, fit_code="classic", size="M", stock=0, is_enabled=True)
        ProductInventoryPolicy.objects.create(product=self.product, source="catalog_variant")
        self.serial = 0
        self.choice = self.canonical(f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic")

    def source(self, text, *, payload="", at=None, namespace="instagram_login:owner-restock", client=None):
        self.serial += 1
        client = client or self.client_row
        return InstagramBotMessage.objects.create(client=client, sender_id=client.igsid, role="user",
            source="webhook", status="pending", text=text, quick_reply_payload=payload,
            provider_namespace=namespace, mid=f"restock-user-{self.serial}", provider_created_at=at or self.now)

    def canonical(self, text, *, at=None):
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        source = self.source(text, at=at)
        self.client_row.refresh_from_db()
        with transaction.atomic():
            admitted = reduce_inbound_commerce_source(self.client_row, source,
                expected_provider_namespace=source.provider_namespace)
        self.assertTrue(admitted.ready, admitted.reason)
        source.status = "done"
        source.save(update_fields=["status"])
        self.client_row.refresh_from_db()
        return source

    def revision(self, source, *, now=None):
        now = now or source.provider_created_at
        turn = IgCustomerTurn.objects.create(client=source.client, primary_source_message=source,
            window_started_at=now, window_deadline=now)
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        created = create_collecting_revision(turn, [source], now=now, bypass_quiet=True)
        self.assertIsNotNone(created.revision, created.reason)
        prepared = claim_revision_preparation(created.revision.pk, now=now)
        sealed = seal_revision(created.revision.pk, prepared.token, now=now)
        self.assertTrue(sealed.sealed, sealed.reason)
        claimed = claim_sealed_revision(sealed.revision.pk, now=now)
        self.assertIsNotNone(claimed.revision, claimed.reason)
        return claimed.revision, claimed.token

    @staticmethod
    def buttons(result):
        from management.services.instagram_bot import _provider_quick_replies_payload
        return _provider_quick_replies_payload(result.quick_replies)

    def request(self, text="Повідомте, коли ця позиція буде в наявності", *, source=None):
        source = source or self.source(text)
        revision, token = self.revision(source)
        result = restock.prepare_request(revision, source, settings_obj=self.settings, now=source.provider_created_at)
        return result, source, revision, token

    def sent_card(self, revision, token, source, result, *, mid="native-restock-card", outcome="response", wrong_body=False, now=None):
        now = now or source.provider_created_at
        payload = {"recipient": {"id": self.client_row.igsid}, "message": {
            "text": "changed exact body" if wrong_body else result.reply_text,
            "quick_replies": self.buttons(result)}}
        planned = plan_revision_effects(revision.pk, token, source_message_id=source.pk,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding, authority_context_digest="e" * 64,
            effects=[{"group": "substantive_text", "kind": "text", "payload": payload}])
        self.assertTrue(planned.created, planned.reasons)
        claim = claim_next_effect(revision.pk, token, "substantive_text", now=now)
        self.assertIsNotNone(claim.effect, claim.reason)
        started = mark_provider_started(claim.effect.pk, claim.token, token, now=now)
        self.assertEqual(started.reason, "provider_started")
        finished = finish_effect(claim.effect.pk, claim.token, provider_namespace="instagram_login:owner-restock",
            http_status=200 if outcome == "response" else None, provider_message_id=mid,
            transport_outcome=outcome, now=now)
        self.assertTrue(finished.changed, finished.reason)
        return claim.effect

    def invitation(self, **card_options):
        result, source, revision, token = self.request()
        self.assertTrue(result.invitation_id, result.reason)
        self.sent_card(revision, token, source, result, **card_options)
        restock.record_delivery(revision.pk, now=self.now)
        return IgRestockInvitation.objects.get(pk=result.invitation_id)

    def reply(self, invitation, *, action="accept", native=True, at=None, namespace="instagram_login:owner-restock", reply_to=""):
        at = at or self.now + timedelta(seconds=1)
        source = self.source(invitation.button_title if action == "accept" else "Скасувати",
            payload=(invitation.accept_payload if action == "accept" else invitation.revoke_payload) if native else "",
            at=at, namespace=namespace)
        if reply_to:
            source.reply_to_provider_message_id = reply_to
            source.save(update_fields=["reply_to_provider_message_id"])
        revision, token = self.revision(source, now=at)
        result = restock.handle_reply(source, revision=revision, settings_obj=self.settings, now=at)
        return result, source, revision, token

    def accepted(self, *, ack=False):
        inv = self.invitation()
        result, source, revision, token = self.reply(inv)
        self.assertEqual(result.reason, "accept", result)
        subscription = IgRestockSubscription.objects.get(accepted_answer__source=source)
        if ack:
            self.sent_card(revision, token, source, result, mid="native-restock-ack", now=source.provider_created_at)
        return inv, subscription


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, SITE_BASE_URL="https://twocomms.test")
class NativeRestockSubscriptionTests(_RestockFixture, TestCase):
    def test_exact_unavailable_selected_target_produces_one_positive_native_button(self):
        result, source, revision, _ = self.request(source=self.choice)
        self.assertTrue(result.invitation_id, result.reason)
        inv = IgRestockInvitation.objects.get(pk=result.invitation_id)
        self.assertEqual(len(result.quick_replies), 1)
        self.assertLessEqual(len(result.quick_replies[0].title), 20)
        self.assertLessEqual(len(result.reply_text.encode()), 1000)
        self.assertEqual(inv.target, {"product_id": self.product.pk, "color_variant_id": self.variant.pk,
            "size": "M", "fit_option_code": "classic", "option_values": {"fit": "classic"}, "quantity": 1})
        self.assertEqual(inv.request_source_id, source.pk)
        self.assertEqual(inv.revision_id, revision.pk)
        self.assertFalse(IgRestockSubscription.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.filter(reason=restock.ORIGIN).exists())

    def test_explicit_uk_ru_en_notify_requests_preserve_exact_target(self):
        for text, locale in (("Повідомте, коли ця позиція буде в наявності", "uk"),
                ("Сообщите когда эта модель появится в наличии", "ru"),
                ("Notify me when this item is back in stock", "en")):
            with self.subTest(locale=locale):
                result, _, _, _ = self.request(text)
                self.assertTrue(result.invitation_id, result.reason)
                inv = IgRestockInvitation.objects.get(pk=result.invitation_id)
                self.assertEqual(inv.locale, locale)
                self.assertEqual(len(result.quick_replies), 1)

    def test_shared_notify_spellings_create_actual_same_revision_target_manager_case(self):
        from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest
        from management.services.ig_fulfillment_reviews import _verified
        for text in ("Сповістіть мене коли ця позиція буде в наявності", "When this item is back in stock notify me"):
            with self.subTest(text=text):
                self.assertTrue(restock.is_restock_interest(text))
                result, source, revision, _ = self.request(text)
                self.assertTrue(result.invitation_id, result)
                inv = IgRestockInvitation.objects.get(pk=result.invitation_id)
                binding = inv.envelope["manager_review"]
                request = IgFulfillmentReviewRequest.objects.get(pk=binding["request_id"])
                self.assertTrue(_verified(request, "request"))
                self.assertEqual((request.source_id, request.revision_id, request.client_id), (source.pk, revision.pk, self.client_row.pk))
                self.assertEqual(binding["request_hmac"], request.envelope_hmac)
                self.assertEqual(request.envelope["lines"][0]["target"], inv.target)
                self.assertTrue(restock._invitation_manager_case(inv))

    def test_independently_disabled_manager_feature_cannot_offer_or_claim_manager_check(self):
        from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest
        with override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=False):
            result, source, _, _ = self.request()
        self.assertFalse(result.handled)
        self.assertEqual(result.reply_text, "")
        self.assertFalse(result.invitation_id)
        self.assertFalse(IgFulfillmentReviewRequest.objects.filter(source=source).exists())

    def test_fabricated_ready_boolean_without_actual_manager_request_cannot_offer(self):
        with patch("management.services.ig_fulfillment_reviews.prepare_fulfillment_review", return_value={
                "ready": True, "request_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "request_digest": "a" * 64}):
            result, _, _, _ = self.request()
        self.assertFalse(result.handled)
        self.assertFalse(result.invitation_id)
        self.assertEqual(result.quick_replies, ())

    def test_real_manager_case_for_prior_source_cannot_authorize_new_source_invitation(self):
        from management.services.ig_fulfillment_reviews import prepare_fulfillment_review
        revision, _ = self.revision(self.choice)
        prior = prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(prior.get("request_id"), prior)
        with patch("management.services.ig_fulfillment_reviews.prepare_fulfillment_review", return_value=prior):
            result, _, _, _ = self.request()
        self.assertFalse(result.handled)
        self.assertFalse(result.invitation_id)

    def test_physical_invitation_requires_actual_manager_case_hmac_after_preparation(self):
        from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest
        from management.services import ig_fulfillment_reviews as reviews
        result, source, revision, token = self.request()
        self.assertTrue(result.invitation_id, result.reason)
        inv = IgRestockInvitation.objects.get(pk=result.invitation_id)
        original = reviews._verified
        def corrupted(row, kind):
            return False if isinstance(row, IgFulfillmentReviewRequest) else original(row, kind)
        with patch.object(reviews, "_verified", side_effect=corrupted):
            with restock.provider_boundary_for_revision(revision.pk, now=self.now) as allowed:
                self.assertFalse(allowed)
                self.assertEqual(allowed.reason, "manager_review_binding_changed")
        self.assertFalse(IgRestockReceipt.objects.filter(invitation=inv).exists())

    def test_available_with_seven_to_ten_day_preparation_never_claims_immediate_readiness(self):
        from django.contrib.auth import get_user_model
        from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest, IgFulfillmentReviewDecision
        from management.services.ig_fulfillment_reviews import apply_fulfillment_decision
        result, _, _, _ = self.request()
        self.assertTrue(result.invitation_id, result.reason)
        inv = IgRestockInvitation.objects.get(pk=result.invitation_id)
        request = IgFulfillmentReviewRequest.objects.get(pk=inv.envelope["manager_review"]["request_id"])
        manager = get_user_model().objects.create_superuser(username="restock-preparation-manager", email="restock@example.test", password="test")
        decision = apply_fulfillment_decision(request.pk, actor=manager,
            expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(),
            decisions_by_line=[{"line_id": request.envelope["lines"][0]["line_id"],
                "availability": "available", "checked_quantity": 1, "supply_basis": "accessible_unrecorded",
                "preparation": {"min_days": 7, "max_days": 10, "unit": "calendar"}}], now=self.now)
        self.assertTrue(decision["ready"], decision)
        persisted = IgFulfillmentReviewDecision.objects.get(pk=decision["decision_id"])
        self.assertEqual(persisted.envelope["lines"][0]["preparation"]["max_days"], 10)
        for locale, availability in (("uk", "наявність"), ("ru", "наличие"), ("en", "available")):
            with self.subTest(locale=locale):
                text = restock._copy(locale, "notification", inv.envelope["label"])
                self.assertIn(availability, text)
                self.assertNotIn("готовність", text)
                self.assertNotIn("готовность", text)
                self.assertNotIn("ready", text)
                self.assertLessEqual(len(text.encode()), 1000)

    def test_unknown_null_inventory_requires_manager_review_not_false_subscription(self):
        self.rule.stock = None
        self.rule.save(update_fields=["stock", "updated_at"])
        result, _, _, _ = self.request()
        self.assertEqual(result.reason, "manager_review_required")
        self.assertFalse(result.handled)
        self.assertFalse(result.invitation_id)
        self.assertEqual(result.quick_replies, ())
        self.assertFalse(IgRestockInvitation.objects.exists())

    def test_actual_available_stock_does_not_offer_restock(self):
        self.variant.stock = self.rule.stock = 2
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock", "updated_at"])
        result, _, _, _ = self.request()
        self.assertFalse(result.handled)
        self.assertFalse(IgRestockInvitation.objects.exists())

    def default_quantity_choice(self):
        self.client_row = IgClient.objects.create(igsid="restock-default-quantity-customer", language="uk", reply_permission_epoch=3)
        self.choice = self.canonical(f"добавьте {self.product.title}, размер M, цвет black, крій classic")

    def test_canonical_cart_default_quantity_one_is_frozen_with_its_real_basis(self):
        self.default_quantity_choice()
        result, source, revision, token = self.request()
        self.assertTrue(result.invitation_id, result.reason)
        inv = IgRestockInvitation.objects.get(pk=result.invitation_id)
        self.assertEqual(inv.target["quantity"], 1)
        basis = inv.envelope["selection"]["evidence"]["quantity"]
        self.assertEqual((basis["value"], basis["authority"], basis["source_confirmed"]),
            (1, "existing_cart_default", False))
        self.sent_card(revision, token, source, result)
        self.assertTrue(restock.record_delivery(revision.pk, now=self.now))
        accepted, _source, _revision, _token = self.reply(inv)
        self.assertEqual(accepted.reason, "accept", accepted)
        self.assertTrue(IgRestockSubscription.objects.filter(accepted_answer__invitation=inv).exists())

    def test_absent_or_malformed_quantity_default_cannot_be_invented(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        self.default_quantity_choice()
        original = capture_current_selection_lines(self.client_row.pk, now=self.now)
        for defaults in ({}, {"quantity": {"value": 2, "authority": "existing_cart_default", "source_confirmed": False}},
                {"quantity": {"value": 1, "authority": "model_guess", "source_confirmed": False}}):
            with self.subTest(defaults=defaults):
                captured = deepcopy(original)
                captured["lines"][0]["defaults"] = defaults
                with patch("management.services.ig_commerce_projection.capture_current_selection_lines", return_value=captured):
                    result, _, _, _ = self.request()
                self.assertFalse(result.invitation_id)
                self.assertEqual(result.quick_replies, ())

    def test_free_yes_or_copied_payload_text_has_no_consent(self):
        inv = self.invitation()
        self.reply(inv, native=False)
        source = self.source(inv.accept_payload, at=self.now + timedelta(seconds=2))
        revision, _ = self.revision(source)
        result = restock.handle_reply(source, revision=revision, settings_obj=self.settings, now=source.provider_created_at)
        self.assertFalse(result.handled)
        self.assertFalse(IgRestockAnswer.objects.exists())
        self.assertFalse(IgRestockSubscription.objects.exists())

    def test_unknown_or_missing_mid_or_wrong_native_card_body_cannot_grant(self):
        for options in ({"outcome": "unknown", "mid": ""}, {"mid": ""}, {"wrong_body": True}):
            with self.subTest(options=options):
                inv = self.invitation(**options)
                self.reply(inv)
                self.assertFalse(IgRestockReceipt.objects.filter(invitation=inv).exists())
                self.assertFalse(IgRestockSubscription.objects.filter(accepted_answer__invitation=inv).exists())

    def test_signed_payload_tamper_or_foreign_namespace_rejected(self):
        inv = self.invitation()
        source = self.source(inv.button_title, payload=inv.accept_payload[:-1] + ("0" if inv.accept_payload[-1] != "0" else "1"),
            at=self.now + timedelta(seconds=1))
        revision, _ = self.revision(source)
        restock.handle_reply(source, revision=revision, settings_obj=self.settings, now=source.provider_created_at)
        self.reply(inv, namespace="instagram_login:foreign-account")
        self.assertFalse(IgRestockSubscription.objects.exists())

    def test_reset_before_answer_blocks(self):
        inv = self.invitation()
        IgFunnelResetAudit.objects.create(client=self.client_row, reset_after_message_id=inv.request_source_id,
            reason="restock test reset")
        self.reply(inv)
        self.assertFalse(IgRestockSubscription.objects.exists())

    def test_pause_optout_no_buy_or_erasure_before_answer_blocks(self):
        for field, value in (("bot_paused", True), ("opted_out_at", self.now),
                ("primary_objection", "no_buy"), ("privacy_erasure_started_at", self.now)):
            with self.subTest(field=field):
                inv = self.invitation()
                IgClient.objects.filter(pk=self.client_row.pk).update(**{field: value})
                self.reply(inv)
                self.assertFalse(IgRestockSubscription.objects.filter(accepted_answer__invitation=inv).exists())
                IgClient.objects.filter(pk=self.client_row.pk).update(**{field: False if field == "bot_paused" else "" if field == "primary_objection" else None})

    def test_expired_acceptance_has_no_ninety_day_future_native_grant(self):
        inv = self.invitation()
        self.reply(inv, at=inv.acceptance_deadline + timedelta(seconds=1))
        self.assertFalse(IgRestockSubscription.objects.exists())
        self.assertEqual(inv.expires_at, inv.created_at + timedelta(days=90))
        self.assertEqual(inv.envelope["native_future_capability"], "unverified")

    def test_real_native_accept_creates_subscription_and_no_task_until_manager_gate(self):
        inv, subscription = self.accepted()
        self.assertEqual(subscription.state, "active")
        self.assertEqual(subscription.expires_at, inv.expires_at)
        self.assertEqual(IgRestockAnswer.objects.filter(kind="accept").count(), 1)
        self.assertFalse(IgFollowUpTask.objects.filter(reason=restock.ORIGIN).exists())

    def test_duplicate_native_answer_creates_no_second_subscription_or_task(self):
        inv, subscription = self.accepted()
        accepted = subscription.accepted_answer
        revision = IgCustomerTurnRevision.objects.get(pk=accepted.envelope["source"]["revision_id"])
        result = restock.handle_reply(accepted.source, revision=revision, settings_obj=self.settings, now=accepted.answered_at)
        self.assertEqual(result.reason, "duplicate_answer")
        self.assertEqual(IgRestockSubscription.objects.count(), 1)
        self.assertEqual(IgRestockAnswer.objects.filter(kind="accept").count(), 1)
        self.assertFalse(IgFollowUpTask.objects.filter(reason=restock.ORIGIN).exists())

    def test_unknown_or_rewritten_runtime_state_cannot_add_a_second_grant(self):
        inv, subscription = self.accepted()
        IgRestockSubscription.objects.filter(pk=subscription.pk).update(state="unknown")
        self.reply(inv, at=self.now + timedelta(seconds=2))
        self.assertEqual(IgRestockSubscription.objects.count(), 1)
        self.assertEqual(IgRestockAnswer.objects.filter(kind="accept").count(), 1)

    def test_signature_key_rotation_accepts_only_verified_previous_key_fallback(self):
        from django.conf import settings
        inv = self.invitation()
        original = settings.SECRET_KEY
        with override_settings(SECRET_KEY="rotated-restock-signing-secret", SECRET_KEY_FALLBACKS=[original]):
            self.reply(inv)
        self.assertTrue(IgRestockSubscription.objects.filter(accepted_answer__invitation=inv).exists())

    def test_cancellation_requires_native_delivered_ack_and_only_narrows_one_interest(self):
        inv, subscription = self.accepted(ack=True)
        unrelated = IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now,
            kind="manager_task", reason="separate-customer-manager-request")
        self.reply(inv, action="revoke", reply_to="native-restock-ack", at=self.now + timedelta(seconds=3))
        subscription.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual(subscription.state, "revoked")
        self.assertEqual(unrelated.status, "pending")
        self.assertEqual(IgRestockAnswer.objects.filter(kind="revoke").count(), 1)

    def test_typed_cancel_or_unseen_ack_never_revokes(self):
        inv, subscription = self.accepted()
        self.reply(inv, action="revoke", native=False)
        self.reply(inv, action="revoke", at=self.now + timedelta(seconds=2), reply_to="unseen-native-card")
        subscription.refresh_from_db()
        self.assertEqual(subscription.state, "active")
        self.assertFalse(IgRestockAnswer.objects.filter(kind="revoke").exists())

    def test_same_line_amendment_before_confirmation_invalidates_original_card(self):
        inv = self.invitation()
        self.canonical("розмір L", at=self.now + timedelta(seconds=1))
        self.reply(inv, at=self.now + timedelta(seconds=2))
        self.assertFalse(IgRestockSubscription.objects.exists())

    def test_after_accept_different_purchase_cannot_retarget_standing_interest(self):
        inv, subscription = self.accepted()
        self.canonical("добавьте Storm, количество 2, размер L, цвет pink, крій classic",
            at=self.now + timedelta(seconds=2))
        projection = restock.restock_projection(self.client_row.pk, now=self.now + timedelta(seconds=3))
        item = projection["subscriptions"][0]
        self.assertEqual(item["state"], "active", item)
        self.assertEqual(item["target"], inv.target)
        self.assertEqual(item["target"]["product_id"], self.product.pk)

    def test_original_field_source_mutation_blocks_independent_subscription(self):
        _inv, subscription = self.accepted()
        self.choice.text = self.choice.text.replace("M", "L")
        self.choice.save(update_fields=["text"])
        projection = restock.restock_projection(self.client_row.pk, now=self.now + timedelta(seconds=2))
        self.assertNotEqual(projection["subscriptions"][0]["reason"], "")

    def test_quoted_negative_and_mixed_question_do_not_consume_original_question(self):
        for text in ("Не повідомляйте коли буде в наявності", "«Повідомте коли буде в наявності»",
                "Сообщите когда будет в наличии, и есть ли размер L?",
                "Notify me when back in stock, how much does it cost?"):
            with self.subTest(text=text):
                result, _, _, _ = self.request(text)
                self.assertFalse(result.handled, result)
                self.assertFalse(result.invitation_id)

    def test_real_legacy_attachment_and_native_media_never_become_pure_notify_request(self):
        source = self.source("Повідомте коли ця позиція буде в наявності")
        source.attachments = '["https://example.invalid/customer-photo.jpg"]'
        source.save(update_fields=["attachments"])
        result, _, _, _ = self.request(source=source)
        self.assertFalse(result.handled)
        self.assertFalse(result.invitation_id)

    def test_availability_question_is_not_a_customer_size_selection(self):
        source = self.canonical("Есть ли размер L?", at=self.now + timedelta(seconds=1))
        result, _, _, _ = self.request(source=source)
        self.assertFalse(result.handled, result)
        self.assertFalse(result.invitation_id)

    def test_projection_is_read_only_and_contains_no_payload_native_mid_or_secret(self):
        inv, _subscription = self.accepted()
        with CaptureQueriesContext(connection) as queries, patch("management.services.instagram_bot._provider_http") as provider:
            result = restock.restock_projection(self.client_row.pk, now=self.now + timedelta(seconds=2))
        provider.assert_not_called()
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") and "FOR UPDATE" not in row["sql"].upper() for row in queries))
        encoded = json.dumps(result)
        self.assertNotIn(inv.accept_payload, encoded)
        self.assertNotIn(inv.revoke_payload, encoded)
        self.assertNotIn("native-restock-card", encoded)

    def test_immutable_evidence_orm_rejects_rewrite(self):
        inv = self.invitation()
        with self.assertRaises(ValueError):
            IgRestockInvitation.objects.filter(pk=inv.pk).update(target={})
        inv.body = "rewrite"
        with self.assertRaises(ValueError):
            inv.save()


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, SITE_BASE_URL="https://twocomms.test")
class NativeRestockLiveExecutionTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True

    def test_live_revision_card_accept_and_cancel_use_native_http_without_gemini(self):
        from management.services.ig_revision_live import execute_claimed_revision
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        source = self.source("Повідомте, коли ця позиція буде в наявності")
        revision, token = self.revision(source)
        physical = []
        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            return 200, json.dumps({"message_id": f"native-live-restock-{len(physical)}"})
        with patch("management.services.instagram_bot.get_page_token", return_value="synthetic-test-token"), \
                patch("management.services.instagram_bot._provider_http", side_effect=provider), \
                patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("restock agreement must bypass Gemini")) as gemini:
            first = execute_claimed_revision(revision.pk, token, self.settings)
            self.assertEqual(first.state, "completed", first.reasons)
            inv = IgRestockInvitation.objects.get(request_source=source)
            receipt = IgRestockReceipt.objects.get(invitation=inv)
            self.assertEqual(receipt.provider_message_id, "native-live-restock-1")
            self.assertEqual(physical[0]["message"]["quick_replies"], [{"content_type": "text",
                "title": inv.button_title, "payload": inv.accept_payload}])
            answer = self.source(inv.button_title, payload=inv.accept_payload, at=self.now + timedelta(seconds=1))
            answer.reply_to_provider_message_id = receipt.provider_message_id
            answer.save(update_fields=["reply_to_provider_message_id"])
            answer_revision, answer_token = self.revision(answer)
            with patch("django.utils.timezone.now", return_value=answer.provider_created_at):
                second = execute_claimed_revision(answer_revision.pk, answer_token, self.settings)
            self.assertEqual(second.state, "completed", second.reasons)
            subscription = IgRestockSubscription.objects.get(accepted_answer__source=answer)
            self.assertEqual(len(physical[1]["message"]["quick_replies"]), 1)
            cancel = self.source("Скасувати", payload=physical[1]["message"]["quick_replies"][0]["payload"],
                at=self.now + timedelta(seconds=2))
            cancel.reply_to_provider_message_id = "native-live-restock-2"
            cancel.save(update_fields=["reply_to_provider_message_id"])
            cancel_revision, cancel_token = self.revision(cancel)
            with patch("django.utils.timezone.now", return_value=cancel.provider_created_at):
                third = execute_claimed_revision(cancel_revision.pk, cancel_token, self.settings)
            self.assertEqual(third.state, "completed", third.reasons)
            subscription.refresh_from_db()
            self.assertEqual(subscription.state, "revoked")
            self.assertEqual(len(physical), 3)
            self.assertFalse(IgFollowUpTask.objects.filter(reason=restock.ORIGIN).exists())
            gemini.assert_not_called()
