"""Original failed-fit dialogue and selected-product resilience boundaries."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixtures
from management import tests_ig_revision_preference_fallback as fallback_fixtures
from management.models import (
    GeminiRequestAttempt, IgBotNotification, IgCheckoutProposal, IgCommercialEpisode, IgDeal, IgFollowUpTask,
    InstagramBotMessage,
)
from management.services.ig_response_guard import ProviderResponseGuard


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionReplyResilienceTests(TransactionTestCase):
    reset_sequences = True
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _execute = live_fixtures.RevisionLiveTests._execute
    _generate = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._generate
    _fit_fixture = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._fit_fixture
    _record = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._record

    def _selected_product(self, *, size=""):
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product

        category = Category.objects.create(name="Resilience fixture", slug="resilience-fixture")
        product = Product.objects.create(title="Selected fixture", slug="selected-fixture", category=category,
                                         price=900, status="published")
        color = Color.objects.create(name="Resilience black", primary_hex="#111111")
        variant = ProductColorVariant.objects.create(product=product, color=color, is_default=True, price_override=900)
        self.customer.current_product = product
        self.customer.current_color = "black"
        self.customer.current_size = size
        self.customer.sales_context = {"assisted_checkout_selection": {
            "product_id": product.pk, "color_variant_id": variant.pk, "fit_option_code": "classic",
        }}
        self.customer.save(update_fields=["current_product", "current_color", "current_size", "sales_context"])
        return product, variant

    def _checkout(self):
        deal = IgDeal.objects.create(client=self.customer)
        episode = IgCommercialEpisode.objects.create(client=self.customer, deal=deal, sequence=1,
                                                     open_slot=1, materialization_key="resilience:episode")
        self.customer.current_commercial_episode = episode
        self.customer.save(update_fields=["current_commercial_episode"])
        proposal = IgCheckoutProposal.objects.create(
            client=self.customer, deal=deal, commercial_episode=episode,
            catalog_total=Decimal("900"), quoted_total=Decimal("900"), requested_payment_amount=Decimal("900"),
            items_digest="a" * 64, expires_at=timezone.now() + timedelta(minutes=25),
        )
        deal.active_checkout_proposal = proposal
        deal.save(update_fields=["active_checkout_proposal"])
        return proposal

    def _assert_safe_delivered_fit(self, fallback, *, missing_model):
        self.assertTrue(fallback.ready, fallback.reason)
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.failed_graph.refresh_from_db()
        self.assertEqual(self.failed_graph.terminal_resolution, "failed")
        self.assertIsNone(self.failed_graph.winner_attempt_id)
        self.assertFalse(GeminiRequestAttempt.objects.filter(winner_claimed=True).exists())
        effect = self.revision.delivery_effects.get()
        text = effect.payload["message"]["text"]
        self.assertIn("оверсайз", text.casefold())
        self.assertNotRegex(text, r"\b(?:XL|XXL|190|100|900)\b|грн|UAH|https?://")
        self.assertEqual(effect.generation_request_id, "")
        self.assertEqual(fallback.receipt["authority"]["allowed_actions"], [])
        if missing_model:
            self.assertIn("принт", text)
        else:
            self.assertIn("размер", text)
            self.assertNotIn("принт", text)
            self.assertNotIn("модель", text)
        return effect

    def test_original_height_weight_black_tee_oversize_failure_gets_actual_reply(self):
        effect = self._assert_safe_delivered_fit(self._fit_fixture(), missing_model=True)
        text = effect.payload["message"]["text"]
        self.assertIn("ассортимента", text)
        self.assertIn("свой дизайн", text)
        self.assertNotIn("команде", text)
        self.assertFalse(IgFollowUpTask.objects.filter(reason="revision_case:execution_debt").exists())

    def test_known_product_missing_size_advances_without_repeating_model_or_fabricating_price(self):
        product, _ = self._selected_product()
        fallback = self._fit_fixture()
        effect = self._assert_safe_delivered_fit(fallback, missing_model=False)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_product_id, product.pk)
        self.assertEqual(self.customer.current_size, "")
        self.assertEqual(fallback.receipt["proof"]["template"], "preference_then_usual_size")
        self.assertEqual(fallback.receipt["proof"]["current_source_preference"]["source_message_id"], self.source.pk)
        self.assertEqual({binding["claim"] for binding in effect.fact_bindings}, {"public_policy_inputs"})

    def test_known_product_fallback_unknown_delivery_never_resends_or_mints_winner(self):
        self._selected_product()
        self.assertTrue(self._fit_fixture().ready)
        first, _, first_http = self._execute([TimeoutError()])
        self.assertEqual(first.state, "delivery_pending")
        self.assertEqual(first_http.call_count, 1)
        denied = self._record()
        self.assertEqual(denied.reason, "fallback_lineage_delivery_or_invalid")
        again, generate, http = self._execute()
        self.assertEqual(again.state, "delivery_pending")
        generate.assert_not_called()
        http.assert_not_called()
        self.assertFalse(GeminiRequestAttempt.objects.filter(winner_claimed=True).exists())

    def test_known_product_changed_source_cannot_send_stored_fit_acknowledgement(self):
        self._selected_product()
        self.assertTrue(self._fit_fixture().ready)
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="Не хочу оверсайз")
        result, generate, http = self._execute()
        self.assertEqual(result.state, "blocked", result.reasons)
        generate.assert_not_called()
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_known_product_negated_fit_is_not_turned_into_affirmative_acknowledgement(self):
        self._selected_product()
        fallback = self._fit_fixture(fit_text="Не хочу оверсайз")
        self.assertTrue(fallback.ready, fallback.reason)
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.delivery_effects.get().purpose, "normal_reply")
        self.assertIn("какую посадку", self.revision.delivery_effects.get().payload["message"]["text"].casefold())
        self.assertNotIn("оверсайз", self.revision.delivery_effects.get().payload["message"]["text"].casefold())
        self.assertEqual(fallback.receipt["proof"]["template"], "withdrawal_then_fit_preference")

    def test_existing_checkout_missing_size_can_clarify_without_rewriting_offer(self):
        self._selected_product()
        proposal = self._checkout()
        fallback = self._fit_fixture()
        self._assert_safe_delivered_fit(fallback, missing_model=False)
        proposal.refresh_from_db()
        self.assertEqual(proposal.quoted_total, Decimal("900"))
        self.assertEqual(proposal.status, "ready")
        self.assertEqual(IgCheckoutProposal.objects.count(), 1)

    def test_complete_checkout_requires_real_next_action_not_false_completion_ack(self):
        self._selected_product(size="M")
        proposal = self._checkout()
        fallback = self._fit_fixture()
        self.assertFalse(fallback.ready)
        self.assertEqual(fallback.reason, "fallback_complete_selection_requires_action")
        result, generate, http = self._execute()
        self.assertEqual(result.state, "delivery_pending", result.reasons)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "ready")
        self.assertEqual(IgCheckoutProposal.objects.count(), 1)
        self.assertEqual(self.revision.delivery_effects.get().purpose, "technical_holding")
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")

    def test_local_validation_failure_never_sends_rejected_text(self):
        self._selected_product()
        from management.services.ig_provider_dispatch_budget import ValidationDecision
        with patch.object(ProviderResponseGuard, "validate", return_value=ValidationDecision(False, ("unverified_price",))):
            fallback = self._fit_fixture()
        self.assertFalse(fallback.ready)
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_selector_repair_distinguishes_product_from_verified_configuration(self):
        payload = ProviderResponseGuard.repair({}, {}, ("catalog_selector_missing", "unverified_price"))
        guidance = payload["contents"][-1]["parts"][0]["text"]
        self.assertIn("selected product alone does not prove", guidance)
        self.assertIn("usual size if product is known", guidance)
        self.assertIn("Remove the unverified price", guidance)

    def _failed_nonfit_request(self, text):
        original = self._message
        def message(value, mid):
            return original(text if mid == "fit-order" else "", mid)
        with patch.object(self, "_message", side_effect=message):
            return self._fit_fixture(fit_text="")

    def test_holding_case_exists_before_http_and_remains_open_after_sent_and_restart(self):
        import json
        from management.services.ig_revision_execution import finalization_due_ids, finalize_sent_revision_effects

        self._selected_product(size="M")
        self.assertFalse(self._fit_fixture().ready)
        def send(*args, **kwargs):
            self.revision.refresh_from_db()
            receipt = self.revision.action_receipts["technical_holding"]
            self.assertTrue(IgFollowUpTask.objects.filter(pk=receipt["task_id"], status="skipped").exists())
            self.assertTrue(IgBotNotification.objects.filter(pk=receipt["notification_id"], status="pending").exists())
            return 200, json.dumps({"message_id": "held-once"})
        first, generate, http = self._execute(send)
        self.assertEqual(first.reasons, ("technical_holding_sent",))
        self.assertEqual(http.call_count, 1)
        generate.assert_not_called()
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts["technical_holding"]
        self.assertIn("technical_holding_delivery", self.revision.action_receipts)
        self.assertNotIn("normal_followups", self.revision.action_receipts)
        self.assertEqual(self.revision.recovery_state, "manual")
        self.assertEqual(self.revision.state, "claimed")
        self.assertNotIn(self.revision.pk, finalization_due_ids())
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.replies_count, 0)
        self.assertEqual(InstagramBotMessage.objects.filter(source="revision_holding").count(), 1)
        with patch("management.services.instagram_bot._provider_http") as no_send:
            again = finalize_sent_revision_effects(self.revision.pk)
        self.assertFalse(again.completed)
        no_send.assert_not_called()
        second, _, retry_http = self._execute()
        retry_http.assert_not_called()
        self.assertEqual(second.reasons, ("technical_holding_sent",))
        self.assertEqual(IgFollowUpTask.objects.get(pk=receipt["task_id"]).status, "skipped")
        self.assertEqual(IgBotNotification.objects.filter(pk=receipt["notification_id"]).count(), 1)

    def test_positive_price_request_without_fit_gets_honest_holding_not_silence(self):
        self.assertFalse(self._failed_nonfit_request("Сколько стоит эта футболка?").ready)
        result, generate, http = self._execute()
        self.assertEqual(result.reasons, ("technical_holding_sent",))
        self.assertEqual(http.call_count, 1)
        generate.assert_not_called()
        text = self.revision.delivery_effects.get().payload["message"]["text"]
        self.assertNotRegex(text, r"\d|грн|UAH|https?://")
        self.assertFalse(GeminiRequestAttempt.objects.filter(winner_claimed=True).exists())

    def test_unsolicited_url_does_not_create_technical_handoff(self):
        from management.services.ig_revision_holding import record_technical_holding

        self._failed_nonfit_request("https://radio.example/article")
        result = record_technical_holding(self.revision.pk, self.token, settings_id=self.settings.pk)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "holding_current_purpose_ineligible")
        self.assertFalse(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{self.revision.pk}").exists())

    def test_neutral_provider_holding_is_opt_in_and_delivers_ack_for_noncommercial_turn(self):
        from unittest.mock import patch
        from management.services.ig_revision_holding import record_technical_holding

        self._failed_nonfit_request("Добрий вечір")
        with (
            patch(
                "management.services.ig_revision_holding._positive_current_request",
                return_value=False,
            ),
            patch(
                "management.services.ig_revision_holding._neutral_current_request",
                return_value=True,
            ),
        ):
            holding = record_technical_holding(
                self.revision.pk, self.token, settings_id=self.settings.pk,
                allow_neutral=True,
            )
        self.assertTrue(holding.ready, holding.reason)
        self.assertEqual(holding.receipt["reply_mode"], "neutral_ack")
        result, generate, http = self._execute()
        self.assertEqual(result.reasons, ("technical_holding_sent",))
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1)
        text = self.revision.delivery_effects.get().payload["message"]["text"]
        self.assertIn("уточню", text.casefold())
        self.assertNotIn("техніч", text.casefold())

    def test_explicit_purchase_refusal_does_not_create_technical_handoff(self):
        from management.services.ig_revision_holding import record_technical_holding

        self._failed_nonfit_request("Не хочу ничего заказывать")
        result = record_technical_holding(self.revision.pk, self.token, settings_id=self.settings.pk)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "holding_current_purpose_ineligible")

    def test_review_after_plan_before_provider_start_suppresses_handoff_promise(self):
        from management.services.ig_revision_holding import record_technical_holding
        from management.services.ig_revision_outbox import PublicationBinding, plan_revision_effects
        from management.services.ig_revision_transport import prepare_text_effects
        from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings

        self._selected_product(size="M")
        self._fit_fixture()
        holding = record_technical_holding(self.revision.pk, self.token, settings_id=self.settings.pk)
        self.assertTrue(holding.ready, holding.reason)
        receipt = holding.receipt
        prepared = prepare_text_effects(self.customer.igsid, receipt["reply_text"], provider_namespace=self.source.provider_namespace)
        planned = plan_revision_effects(
            self.revision.pk, self.token, source_message_id=self.source.pk, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash),
            authority_context_digest=receipt["authority"]["authority_digest"], effects=prepared.effects,
            fact_bindings=receipt["authority"]["fact_bindings"], fact_checker=check_fact_bindings,
            offer_checker=check_offer_bindings, purpose="technical_holding",
        )
        self.assertTrue(planned.effects, planned.reasons)
        IgFollowUpTask.objects.filter(pk=receipt["task_id"]).update(status="cancelled")
        _, generate, http = self._execute()
        generate.assert_not_called()
        http.assert_not_called()
        self.assertEqual(self.revision.delivery_effects.get().state, "cancelled")

    def test_unknown_holding_does_not_resend_or_close_case(self):
        self._selected_product(size="M")
        self._fit_fixture()
        first, _, http = self._execute([TimeoutError()])
        self.assertEqual(first.state, "delivery_pending")
        self.assertEqual(http.call_count, 1)
        again, generate, retry_http = self._execute()
        generate.assert_not_called()
        retry_http.assert_not_called()
        self.assertEqual(again.state, "delivery_pending")
        self.revision.refresh_from_db()
        self.assertNotIn("technical_holding_delivery", self.revision.action_receipts)
        task = IgFollowUpTask.objects.get(pk=self.revision.action_receipts["technical_holding"]["task_id"])
        self.assertEqual(task.status, "skipped")

    def test_confirmed_holding_remains_eligible_for_explicit_audited_manual_resume(self):
        from django.contrib.auth import get_user_model
        from management.models import AdminAuditLog
        from management.services.ig_revision_manual_resume import create_manual_resume_successor

        self._selected_product(size="M")
        self._fit_fixture()
        result, _, _ = self._execute()
        self.assertEqual(result.state, "delivery_pending", result.reasons)
        self.customer.refresh_from_db()
        old_epoch = self.customer.reply_permission_epoch
        self.customer.reply_permission_epoch += 1
        self.customer.save(update_fields=["reply_permission_epoch"])
        actor = get_user_model().objects.create_superuser(username="holding-resume-reviewer", password="fixture")
        audit = AdminAuditLog.objects.create(
            actor=actor, actor_role="prompt_editor", action="ig_bot.manual_resume",
            entity_type="IgClient", entity_id=str(self.customer.pk),
            before={"permission_epoch": old_epoch, "bot_paused": True, "manager_takeover": False},
            after={"permission_epoch": old_epoch + 1, "bot_paused": False, "manager_takeover": False},
        )
        resumed = create_manual_resume_successor(self.customer, settings_obj=self.settings,
            audit_id=audit.pk, source_message_id=self.source.pk)
        self.assertTrue(resumed.created, resumed.reason)
        self.assertEqual(resumed.revision.parent_id, self.revision.pk)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")

    def test_review_after_provider_started_is_rechecked_before_physical_http(self):
        from management.services import ig_revision_outbox

        self._selected_product(size="M")
        self._fit_fixture()
        original = ig_revision_outbox.mark_provider_started

        def start_then_review(*args, **kwargs):
            result = original(*args, **kwargs)
            self.revision.refresh_from_db()
            receipt = self.revision.action_receipts["technical_holding"]
            IgFollowUpTask.objects.filter(pk=receipt["task_id"]).update(status="cancelled")
            return result

        with patch("management.services.ig_revision_delivery.mark_provider_started", side_effect=start_then_review):
            _, generate, http = self._execute()
        generate.assert_not_called()
        http.assert_not_called()
        self.assertNotEqual(self.revision.delivery_effects.get().state, "sent")
