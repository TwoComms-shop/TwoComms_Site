"""Own native AC1 choice -> live frozen authority -> selected quote -> SENT outbox."""
from copy import deepcopy
from decimal import Decimal
import json
import traceback
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from management.ig_assisted_commerce_models import IgAssistedCommerceExecution
from management.models import IgCheckoutAccessToken, IgCheckoutProposal, IgFollowUpTask
from management.services.ig_assisted_commerce_context import ACTION, CLAIM
from management.services.ig_assisted_scopes import selection_evidence
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_response_control import ResponseControl, ValidatedResponse
from management.services.ig_revision_authority import check_fact_bindings
from management.services.ig_revision_live import (
    RevisionGenerationBoundary, _revision_response_purpose_reason, execute_claimed_revision,
)
from management.tests_ig_assisted_partial_sources import PartialCommerceFixture
from management import tests_ig_revision_live as _live_fixture
from orders.models import Order, PaymentAttempt


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100,
    IG_REVISION_OPTIONAL_HOLDING_ENABLED=False)
class NativeAssistedPartialLiveTests(PartialCommerceFixture, TransactionTestCase):
    reset_sequences = True

    def _generate(self, payload, **kwargs):
        validator = kwargs["result_validator"]

        def diagnosed_validator(*args, **options):
            try:
                return validator(*args, **options)
            except Exception:
                self.validator_errors.append(traceback.format_exc())
                raise

        kwargs["result_validator"] = diagnosed_validator
        try:
            return _live_fixture.RevisionLiveTests._generate(self, payload, **kwargs)
        except Exception:
            if not self.validator_errors:
                self.validator_errors.append(traceback.format_exc())
            raise

    def setUp(self):
        super().setUp()
        presence = patch("management.services.ig_presence.start_presence", return_value=None)
        presence.start()
        self.addCleanup(presence.stop)
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count = 0
        self.actual_hash_override = "observed"
        self.provider_failure = False
        self.validator_errors = []

    def native_purchase(self):
        result = self.selected_case()
        self.source, self.revision, self.token = result["source"], result["revision"], result["token"]
        self.parsed = {"reply_text": "Фіолетове худі в наявності, розмір L, ціна 1590 грн. "
            "Оливкове худі залишається в очікуванні, ще не оплачено. Перевірте готову частину на сторінці оформлення.",
            "controls": [{"kind": "paylink", "value": "full"}]}
        return result

    def execute(self):
        # The existing live provider fixture retains the actual frozen route,
        # accounting winner and deterministic result validator. Only Gemini
        # transport and Instagram transport/token are replaced here.
        with override_settings(ROOT_URLCONF="twocomms.urls"), _live_fixture.native_provider_candidate_plan(), \
                patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate) as generate, \
                patch("management.services.instagram_bot.get_page_token", return_value="native-partial-live-token"), \
                patch("management.services.instagram_bot._provider_http", return_value=(200,
                    json.dumps({"message_id": "native-partial-live-sent"}))) as http:
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        return result, generate, http

    def test_claimed_native_partial_purchase_publishes_exact_selected_checkout_and_one_sent_reply(self):
        data = self.native_purchase()
        result, generate, http = self.execute()
        self.revision.refresh_from_db()
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_response_debt import response_coverage
        observed_plan = capture_response_plan(self.client_row, revision=self.revision,
            source_cart_capture=self.revision.generation_proposal.get("source_cart_capture") or None)
        observed_coverage = response_coverage(self.revision)
        diagnostics = {"reasons": result.reasons, "validator_errors": self.validator_errors,
            "coverage": {key: observed_coverage.get(key) for key in
                ("plan_digest", "covered", "remaining", "disposition", "next_selector", "gap")},
            "obligations": [{key: row.get(key) for key in ("id", "kind", "line_id", "source_message_id")}
                for row in observed_plan.obligations],
            "effects": [{"state": effect.state, "has_native_mid": bool(effect.provider_message_id),
                "claims": [fact.get("claim") for fact in effect.fact_bindings]}
                for effect in self.revision.delivery_effects.order_by("order_index")],
            "proposal_count": IgCheckoutProposal.objects.filter(client=self.client_row).count(),
            "execution_scopes": list(IgAssistedCommerceExecution.objects.filter(client=self.client_row)
                .values_list("scope", flat=True))}
        self.assertEqual(result.state, "completed", diagnostics)
        generate.assert_called_once()
        http.assert_called_once()
        prompt = json.dumps(self.generation_calls[0], ensure_ascii=False)
        self.assertIn("current-assisted-partial-dialogue.v1", prompt)
        self.assertIn("deferred_unpaid_lines", prompt)
        self.assertIn("unpaid_wishlist", prompt)
        self.assertIn("1590.00", prompt)
        self.revision.refresh_from_db()
        saved = self.revision.generation_proposal
        self.assertEqual(saved["assisted_offer_reference"]["selection"], selection_evidence(data["selection"]))
        self.assertIn(ACTION, saved["authority"]["allowed_actions"])
        proposal = IgCheckoutProposal.objects.get(client=self.client_row)
        self.assertTrue(proposal.assisted_checkout_v2)
        self.assertEqual(proposal.quoted_total, Decimal("1590.00"))
        self.assertEqual(list(proposal.items.values_list("product_title", "quantity", "quoted_unit_price")),
            [("Фіолетове худі", 1, Decimal("1590.00"))])
        self.assertEqual(IgCheckoutAccessToken.objects.filter(proposal=proposal).count(), 1)
        execution = IgAssistedCommerceExecution.objects.get(proposal=proposal)
        self.assertEqual(execution.scope, "partial")
        self.assertEqual(execution.envelope["snapshot"]["selection_evidence"], selection_evidence(data["selection"]))
        self.assertEqual(execution.envelope["snapshot"]["coverage"]["deferred_line_ids"], data["selection"].deferred_line_ids)
        effects = list(self.revision.delivery_effects.order_by("order_index"))
        self.assertTrue(effects)
        self.assertTrue(all(effect.state == "sent" and effect.provider_message_id for effect in effects))
        self.assertEqual(effects[0].provider_message_id, "native-partial-live-sent")
        self.assertTrue(check_fact_bindings(effects[0].fact_bindings, revision=self.revision,
            client=self.client_row, settings_obj=self.settings))
        assisted = [fact for fact in effects[0].fact_bindings if fact["claim"] == CLAIM]
        self.assertEqual(len(assisted), 1)
        self.assertEqual(assisted[0]["selector"]["assisted_reference"]["selection"], selection_evidence(data["selection"]))
        self.assertEqual(_revision_response_purpose_reason(self.revision), "")
        self.assertFalse(IgFollowUpTask.objects.filter(client=self.client_row,
            reason="revision_case:product_configuration_review").exists())
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        # A replay of a completed native revision cannot call either transport
        # or prepare another selected quote/access token.
        before = (IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count(),
            IgAssistedCommerceExecution.objects.count(), self.revision.delivery_effects.count())
        _replay, generated_again, sent_again = self.execute()
        generated_again.assert_not_called()
        sent_again.assert_not_called()
        self.assertEqual(before, (IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count(),
            IgAssistedCommerceExecution.objects.count(), self.revision.delivery_effects.count()))

    def test_actual_live_boundary_authorizes_selected_action_but_denies_wait_prices_and_full_cart_controls(self):
        self.native_purchase()
        with override_settings(ROOT_URLCONF="twocomms.urls"):
            boundary = RevisionGenerationBoundary(self.revision, self.token, self.settings, self.binding)
            self.assertIsNotNone(boundary.assisted_context, boundary.assisted_reason)
            self.assertIsNotNone(boundary.assisted_context.selection)
            good = ValidatedResponse(reply_text=self.parsed["reply_text"], controls=(ResponseControl("paylink", "full"),))
            truth = boundary.validate_response_truth(good, ReplyTruthContext())
            self.assertTrue(truth.valid, truth.reasons)
            authority = boundary.response_authority(good)
            self.assertTrue(authority.ready, authority.reasons)
            self.assertIn(ACTION, authority.allowed_actions)
            for text in ("Оливкове худі: ціна 1490 грн.", "Весь кошик: разом 1590 грн.",
                "Оливкове худі в наявності.", "Фіолетове худі гарантовано прибуде завтра."):
                with self.subTest(text=text):
                    self.assertFalse(boundary.validate_response_truth(ValidatedResponse(reply_text=text), ReplyTruthContext()).valid)
            override = ValidatedResponse(reply_text=good.reply_text,
                controls=(ResponseControl("paylink", "full"), ResponseControl("product", str(self.products[1].pk))))
            self.assertFalse(boundary.response_authority(override).ready)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_permission_change_after_real_model_validation_blocks_selected_effect_and_meta_publish(self):
        self.native_purchase()

        def revoke_permission():
            self.client_row.refresh_from_db()
            self.client_row.reply_permission_epoch += 1
            self.client_row.save(update_fields=["reply_permission_epoch", "updated_at"])

        self.after_validate = revoke_permission
        result, generate, http = self.execute()
        generate.assert_called_once()
        self.assertNotEqual(result.state, "completed")
        http.assert_not_called()
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutAccessToken.objects.exists())
        self.assertFalse(IgAssistedCommerceExecution.objects.exists())
        self.assertFalse(self.revision.delivery_effects.filter(state="sent").exists())
        self.assertFalse(Order.objects.exists())

    def test_actual_native_partial_repair_serializes_decimal_facts_without_changing_source_or_choice(self):
        from management.services.ig_reply_authority import build_reply_truth_context

        data = self.native_purchase()
        data["task"].refresh_from_db()
        before = (data["request"].envelope_hmac, data["decision"].envelope_hmac,
            deepcopy(data["task"].manager_context), self.revision.snapshot_digest,
            selection_evidence(data["selection"]))
        payload = {"contents": [{"role": "user", "parts": [{"text": "same sealed native purchase"}]}]}
        with override_settings(ROOT_URLCONF="twocomms.urls"), \
                patch("management.services.instagram_bot._provider_http") as provider:
            boundary = RevisionGenerationBoundary(self.revision, self.token, self.settings, self.binding)
            self.assertIsNotNone(boundary.assisted_context.selection)
            readiness = boundary.check()
            self.assertTrue(readiness.ready, readiness.reasons)
            invalid = ValidatedResponse(reply_text="Ціна 1590 грн.")
            truth = boundary.validate_response_truth(invalid, ReplyTruthContext())
            self.assertFalse(truth.valid)
            self.assertIn("assisted_reply_line_scope_unverified", truth.reasons)
            facts = build_reply_truth_context(self.client_row,
                captured_client_state=boundary.captured_client_state)
            self.assertTrue(facts.authorized_prices)
            self.assertTrue(all(isinstance(price, Decimal) for price in facts.authorized_prices))
            repaired = boundary.repair(payload, {"reply_text": invalid.reply_text}, truth.reasons,
                base_repair=lambda original, _parsed, _reasons: deepcopy(original))
            self.assertIsNotNone(repaired)
            text = repaired["contents"][-1]["parts"][0]["text"]
            serialized = text.split("\n", 2)[1]
            server_facts = json.loads(serialized, parse_constant=lambda value: self.fail(value))
            self.assertEqual(server_facts["authorized_prices"], [str(price) for price in facts.authorized_prices])
            self.assertTrue(boundary.repaired)
            self.assertEqual(payload["contents"], [{"role": "user", "parts": [{"text": "same sealed native purchase"}]}])
            provider.assert_not_called()
        for row in (data["request"], data["decision"], data["task"], self.revision):
            row.refresh_from_db()
        self.assertEqual(before, (data["request"].envelope_hmac, data["decision"].envelope_hmac,
            data["task"].manager_context, self.revision.snapshot_digest,
            selection_evidence(boundary.assisted_context.selection)))
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutAccessToken.objects.exists())
        self.assertFalse(IgAssistedCommerceExecution.objects.exists())
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())
