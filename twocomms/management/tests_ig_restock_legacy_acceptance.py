"""Legacy RS1 agreements use the actual revision reducer and native outbox."""
from contextlib import ExitStack
from datetime import timedelta
from copy import deepcopy
from types import SimpleNamespace
import json
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management.models import IgFollowUpTask, IgRevisionDeliveryEffect
from management.services import ig_restock_subscriptions as restock
from management.services import ig_restock_waiting_list as waiting
from management.tests_ig_restock_subscriptions import _RestockFixture
from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
from productcolors.models import ProductColorVariant


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, SITE_BASE_URL="https://twocomms.test")
class LegacyRestockAcceptanceTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True

    def native_agreement(self, locale, *, all_items=False, old_invitation=False):
        from management.services import instagram_bot as bot
        from management.services.ig_revision_live import execute_claimed_revision
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        if all_items:
            self.variants[1].stock = 0
            self.variants[1].save(update_fields=["stock"])
            ProductInventoryPolicy.objects.create(product=self.products[1], source="catalog_variant")
            VariantSizeRule.objects.create(variant=self.variants[1], fit_code="classic", size="L", stock=0, is_enabled=True)
            self.canonical("добавьте Storm, количество 2, размер L, цвет pink, крій classic")
        inventory_before = (
            list(ProductColorVariant.objects.order_by("pk").values_list("pk", "stock")),
            list(VariantSizeRule.objects.order_by("pk").values_list("pk", "stock")),
        )
        texts = {
            "uk": "Повідомте, коли всі позиції будуть в наявності" if all_items else "Повідомте, коли ця позиція буде в наявності",
            "ru": "Сообщите, когда все позиции появятся в наличии" if all_items else "Сообщите, когда эта позиция появится в наличии",
            "en": "Notify me when all items are back in stock" if all_items else "Notify me when this item is back in stock",
        }
        source = self.source(texts[locale])
        revision, token = self.revision(source)
        physical = []

        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            return 200, json.dumps({"message_id": f"native-legacy-{len(physical)}"})

        with patch.object(bot, "get_page_token", return_value="synthetic-legacy-token"), \
                patch.object(bot, "_provider_http", side_effect=provider), \
                patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("RS1 must bypass Gemini")) as gemini:
            with ExitStack() as historical:
                if old_invitation:
                    original_seal, original_fresh_copy = restock._seal, restock._fresh_copy

                    def old_seal(row, kind, **proof):
                        if kind == "invitation":
                            proof.pop("ack_copy_version", None)
                        return original_seal(row, kind, **proof)

                    def old_copy(locale, kind, label, *, all_items=False):
                        if kind == "invite":
                            return (waiting.copy_for(locale, kind, label) if all_items
                                else restock._copy(locale, kind, label))
                        return original_fresh_copy(locale, kind, label, all_items=all_items)

                    historical.enter_context(patch.object(restock, "_seal", side_effect=old_seal))
                    historical.enter_context(patch.object(restock, "_fresh_copy", side_effect=old_copy))
                first = execute_claimed_revision(revision.pk, token, self.settings)
            self.assertEqual(first.state, "completed", first.reasons)
            inv = restock.Invitation.objects.get(request_source=source)
            self.assertNotIn("producer", inv.envelope)
            self.assertEqual(inv.locale, locale)
            self.assertEqual(waiting.is_list(inv.target), all_items)
            frozen_envelope, frozen_hmac = deepcopy(inv.envelope), inv.envelope_hmac
            if old_invitation:
                self.assertNotIn("ack_copy_version", inv.envelope)
                self.assertEqual(physical[0]["message"]["text"], restock._copy(inv.locale, "invite", inv.envelope["label"]))
            if all_items:
                self.assertEqual(len(inv.target["items"]), 2)
                self.assertEqual(sorted(row["quantity"] for row in inv.target["items"]), [1, 2])
            receipt = restock.Receipt.objects.get(invitation=inv)
            self.assertIsNotNone(receipt.effect_id)
            self.assertIsNone(receipt.followup_task_id)
            self.assertTrue(restock._valid_receipt(inv, receipt))
            self.assertEqual(receipt.provider_message_id, "native-legacy-1")
            self.assertEqual(physical[0]["message"]["text"], inv.body)
            self.assertEqual(physical[0]["message"]["quick_replies"], [restock._native_button(inv)])
            self.assertFalse(restock.Subscription.objects.exists())
            answer = self.source(inv.button_title, payload=inv.accept_payload, at=self.now + timedelta(seconds=1))
            answer.reply_to_provider_message_id = receipt.provider_message_id
            answer.save(update_fields=["reply_to_provider_message_id"])
            answer_revision, answer_token = self.revision(answer)
            with patch("django.utils.timezone.now", return_value=answer.provider_created_at):
                second = execute_claimed_revision(answer_revision.pk, answer_token, self.settings)
            self.assertEqual(second.state, "completed", second.reasons)
            subscription = restock.Subscription.objects.get(accepted_answer__source=answer)
            self.assertEqual(subscription.state, "active")
            self.assertEqual(subscription.accepted_answer.envelope["ack_copy_version"], restock.ACK_COPY_VERSION)
            self.assertEqual(restock.Subscription.objects.count(), 1)
            self.assertEqual(len(physical), 2)
            ack = IgRevisionDeliveryEffect.objects.get(revision=answer_revision, source_message=answer)
            self.assertEqual(ack.provider_message_id, "native-legacy-2")
            self.assertEqual(ack.payload, physical[1])
            self.assertEqual(physical[1]["message"]["text"], restock._copy_for(inv, "accepted", accepted=subscription.accepted_answer))
            if old_invitation:
                self.assertNotEqual(physical[1]["message"]["text"], restock._copy(inv.locale, "accepted", inv.envelope["label"]))
            self.assertLessEqual(len(physical[1]["message"]["text"].encode()), 1000)
            self.assertEqual(physical[1]["message"]["quick_replies"], [restock._native_button(inv, "revoke")])
            self.assertTrue(restock._ack_card(inv, subscription.accepted_answer, reply_to=ack.provider_message_id))
            cancel = self.source("Cancel", payload=inv.revoke_payload, at=self.now + timedelta(seconds=2))
            cancel.reply_to_provider_message_id = ack.provider_message_id
            cancel.save(update_fields=["reply_to_provider_message_id"])
            cancel_revision, cancel_token = self.revision(cancel)
            with patch("django.utils.timezone.now", return_value=cancel.provider_created_at):
                third = execute_claimed_revision(cancel_revision.pk, cancel_token, self.settings)
            self.assertEqual(third.state, "completed", third.reasons)
            subscription.refresh_from_db()
            self.assertEqual(subscription.state, "revoked")
            self.assertEqual(len(physical), 3)
            inv.refresh_from_db()
            self.assertEqual(inv.envelope, frozen_envelope)
            self.assertEqual(inv.envelope_hmac, frozen_hmac)
            self.assertTrue(restock._verified(inv, "invitation"))
            self.assertFalse(IgFollowUpTask.objects.filter(reason=restock.ORIGIN).exists())
            self.assertEqual((
                list(ProductColorVariant.objects.order_by("pk").values_list("pk", "stock")),
                list(VariantSizeRule.objects.order_by("pk").values_list("pk", "stock")),
            ), inventory_before)
            self.variant.refresh_from_db()
            self.rule.refresh_from_db()
            self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
            if all_items:
                self.variants[1].refresh_from_db()
                self.assertEqual(self.variants[1].stock, 0)
            gemini.assert_not_called()

    def test_old_sent_single_invitation_accepts_fresh_ack_and_native_cancel(self):
        self.native_agreement("uk", old_invitation=True)

    def test_exact_catalog_uk_actual_initial_delivery_acceptance_and_cancel(self):
        self.native_agreement("uk")

    def test_exact_catalog_ru_actual_initial_delivery_acceptance_and_cancel(self):
        self.native_agreement("ru")

    def test_exact_catalog_en_actual_initial_delivery_acceptance_and_cancel(self):
        self.native_agreement("en")

    def test_all_catalog_uk_actual_initial_delivery_acceptance_and_cancel(self):
        self.native_agreement("uk", all_items=True)

    def test_all_catalog_ru_actual_initial_delivery_acceptance_and_cancel(self):
        self.native_agreement("ru", all_items=True)

    def test_all_catalog_en_actual_initial_delivery_acceptance_and_cancel(self):
        self.native_agreement("en", all_items=True)


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, SITE_BASE_URL="https://twocomms.test")
class HistoricalLegacyAcknowledgementTests(_RestockFixture, TransactionTestCase):
    """Freeze pre-version signed rows; historical receipts retain exact bodies."""
    reset_sequences = True

    def historical_acceptance(self, *, ack):
        original_seal = restock._seal

        def historical_seal(row, kind, **proof):
            proof.pop("ack_copy_version", None)
            return original_seal(row, kind, **proof)

        def historical_copy(inv, kind, **kwargs):
            return (waiting.copy_for(inv.locale, kind, inv.envelope["label"])
                if waiting.is_list(inv.target) else restock._copy(inv.locale, kind, inv.envelope["label"]))

        with patch.object(restock, "_seal", side_effect=historical_seal), \
                patch.object(restock, "_copy_for", side_effect=historical_copy):
            inv, subscription = self.accepted(ack=ack)
        self.assertNotIn("ack_copy_version", inv.envelope)
        self.assertNotIn("ack_copy_version", subscription.accepted_answer.envelope)
        return inv, subscription

    def test_already_delivered_unversioned_ack_remains_exact_and_can_revoke(self):
        inv, subscription = self.historical_acceptance(ack=True)
        accepted = subscription.accepted_answer
        original_envelope = json.dumps(accepted.envelope, sort_keys=True)
        original_hmac = accepted.envelope_hmac
        effect = IgRevisionDeliveryEffect.objects.get(source_message_id=accepted.source_id)
        self.assertEqual(effect.payload["message"]["text"], restock._copy(inv.locale, "accepted", inv.envelope["label"]))
        self.assertTrue(restock._ack_card(inv, accepted, reply_to=effect.provider_message_id))
        revoked, _source, _revision, _token = self.reply(inv, action="revoke", reply_to=effect.provider_message_id,
            at=self.now + timedelta(seconds=2))
        self.assertEqual(revoked.reason, "revoke")
        subscription.refresh_from_db()
        self.assertEqual(subscription.state, "revoked")
        accepted.refresh_from_db()
        self.assertEqual(json.dumps(accepted.envelope, sort_keys=True), original_envelope)
        self.assertEqual(accepted.envelope_hmac, original_hmac)
        self.assertTrue(restock._verified(accepted, "answer"))

    def test_old_accepted_answer_without_sent_ack_is_not_a_delivered_ack(self):
        inv, subscription = self.historical_acceptance(ack=False)
        self.assertEqual(subscription.state, "active")
        self.assertIsNone(restock._ack_card(inv, subscription.accepted_answer))
        self.assertFalse(IgRevisionDeliveryEffect.objects.filter(source_message_id=subscription.accepted_answer.source_id).exists())
        response, _source, _revision, _token = self.reply(inv, action="revoke", reply_to="invented-old-ack",
            at=self.now + timedelta(seconds=2))
        self.assertEqual(response.reason, "answer_not_current")
        subscription.refresh_from_db()
        self.assertEqual(subscription.state, "active")

    def test_same_revision_replay_recovers_old_unsent_ack_without_resealing_answer(self):
        from management.models import IgCustomerTurnRevision
        from management.services import instagram_bot as bot
        from management.services.ig_revision_live import execute_claimed_revision
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        inv, subscription = self.historical_acceptance(ack=False)
        accepted = subscription.accepted_answer
        original_envelope = json.dumps(accepted.envelope, sort_keys=True)
        original_hmac = accepted.envelope_hmac
        revision = IgCustomerTurnRevision.objects.get(pk=accepted.envelope["source"]["revision_id"])
        physical = []

        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            return 200, json.dumps({"message_id": f"native-old-unsent-recovery-{len(physical)}"})

        with patch.object(bot, "get_page_token", return_value="synthetic-legacy-recovery-token"), \
                patch.object(bot, "_provider_http", side_effect=provider), \
                patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("RS1 recovery must bypass Gemini")) as gemini:
            with patch("django.utils.timezone.now", return_value=accepted.source.provider_created_at):
                recovered = execute_claimed_revision(revision.pk, revision.claim_token, self.settings)
            self.assertEqual(recovered.state, "completed", recovered.reasons)
            self.assertEqual(len(physical), 1)
            ack = restock._ack_card(inv, accepted, reply_to="native-old-unsent-recovery-1")
            self.assertTrue(ack)
            self.assertNotEqual(physical[0]["message"]["text"], restock._copy(inv.locale, "accepted", inv.envelope["label"]))
            self.assertEqual(restock.Subscription.objects.count(), 1)
            accepted.refresh_from_db()
            self.assertEqual(json.dumps(accepted.envelope, sort_keys=True), original_envelope)
            self.assertEqual(accepted.envelope_hmac, original_hmac)
            cancel = self.source("Cancel", payload=inv.revoke_payload, at=self.now + timedelta(seconds=2))
            cancel.reply_to_provider_message_id = "native-old-unsent-recovery-1"
            cancel.save(update_fields=["reply_to_provider_message_id"])
            cancel_revision, cancel_token = self.revision(cancel)
            with patch("django.utils.timezone.now", return_value=cancel.provider_created_at):
                revoked = execute_claimed_revision(cancel_revision.pk, cancel_token, self.settings)
            self.assertEqual(revoked.state, "completed", revoked.reasons)
            subscription.refresh_from_db()
            self.assertEqual(subscription.state, "revoked")
            self.assertEqual(len(physical), 2)
            gemini.assert_not_called()

    def test_fresh_single_card_retains_complete_long_title(self):
        self.product.title = "Aurora " + "я" * 90
        self.product.save(update_fields=["title"])
        from management.models import IgClient
        # Catalog renaming invalidates the earlier customer's historical alias.
        # Start this customer's genuine source history after catalog preparation.
        self.client_row = IgClient.objects.create(igsid="restock-long-title-customer", language="uk",
            reply_permission_epoch=3, last_user_message_at=self.now)
        self.canonical(f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic")
        result, _source, _revision, _token = self.request()
        self.assertTrue(result.invitation_id, result.reason)
        self.assertIn(self.product.title, result.reply_text)
        inv = restock.Invitation.objects.get(pk=result.invitation_id)
        self.assertIn(self.product.title, restock._copy_for(inv, "accepted"))
        self.assertLessEqual(len(result.reply_text.encode()), 1000)
        self.assertLessEqual(len(restock._copy_for(inv, "accepted").encode()), 1000)


class LegacyChoiceCopyTests(SimpleTestCase):
    def test_punctuation_keeps_each_configuration_clause_customer_attributed(self):
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
        from management.services.ig_response_cart_plan import scoped_claim_clauses
        target = {"schema": waiting.SCHEMA, "completion": "all", "items": [{
            "title": "Aurora, limited; edition", "color": "black", "size": "M", "fit": "classic",
            "options": {"fit": "classic", "print": "front, back; sleeves 40,5 cm"}, "quantity": 2,
            "recipient_id": "self"}]}
        original = deepcopy(target)
        context = ReplyTruthContext(source_chosen_sizes=("M",), source_chosen_fits=("classic",),
            source_chosen_colors=("black",))
        for locale in ("uk", "ru", "en"):
            inv = SimpleNamespace(locale=locale, target=target, envelope={})
            for kind in ("invite", "accepted"):
                with self.subTest(locale=locale, kind=kind):
                    body = restock._fresh_copy_for(inv, kind)
                    for clause in scoped_claim_clauses(body):
                        truth = validate_reply_truth(clause, context=context)
                        self.assertTrue(truth.valid, (locale, clause, truth.reasons))
                    for fragment in ("Aurora,", "limited;", "edition", "front,", "back;", "sleeves", "40,5 cm", "×2"):
                        self.assertIn(fragment, body)
                    self.assertEqual(body.count("classic"), 1)
                    self.assertFalse(validate_reply_truth(body + "\nThese items are available.", context=context).valid)
        self.assertEqual(target, original)

    def test_unknown_signed_copy_version_is_not_rendered_as_a_known_ack(self):
        inv = SimpleNamespace(locale="en", target={}, envelope={})
        for marker in (None, "unknown-future-version"):
            with self.subTest(marker=marker):
                accepted = SimpleNamespace(envelope={"ack_copy_version": marker})
                with self.assertRaisesMessage(ValueError, "restock_ack_copy_version_unknown"):
                    restock._copy_for(inv, "accepted", accepted=accepted)
