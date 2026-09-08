import hashlib
import json
from management.models import BotPolicyPublication, IgClient, IgCustomerTurn, IgTurnMessage, InstagramBotMessage, InstagramBotSettings
from management.services.ig_revision_authority import check_fact_bindings
from management.services.ig_revision_outbox import PublicationBinding, claim_next_effect, mark_provider_started
from management.services.ig_turn_revisions import claim_revision_preparation, claim_sealed_revision, create_collecting_revision, seal_revision
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from management.models import IgCheckoutAccessToken, IgCheckoutProposal, IgCheckoutRevision, IgCustomerTurnRevision
from management.services.ig_revision_actions import _authority_projection
from management.services.ig_revision_authority import CLAIM_CATALOG_CONFIGURATION, build_revision_authority_bindings, check_offer_bindings
from management.services.ig_revision_checkout import checkout_authority_control, prepare_revision_checkout
from management.services.ig_revision_outbox import EffectPlanResult, _digest


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.shop")
class RevisionCheckoutTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product

        snapshot = {"schema_version": 1, "instructions": []}
        snapshot_hash = hashlib.sha256(json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        self.publication = BotPolicyPublication.objects.create(
            version=1,
            kind=BotPolicyPublication.Kind.PUBLISH,
            schema_version=1,
            snapshot=snapshot,
            snapshot_hash=snapshot_hash,
            compiler_version="instruction-set-v1",
            instruction_count=0,
        )
        self.settings = InstagramBotSettings.objects.create(
            pk=1,
            is_enabled=True,
            reply_permission_epoch=4,
            active_instruction_publication=self.publication,
        )
        category = Category.objects.create(
            name="Revision action", slug="revision-action"
        )
        self.product = Product.objects.create(
            title="Revision action product",
            slug="revision-action-product",
            category=category,
            price=900,
            status="published",
        )
        color = Color.objects.create(name="Revision black", primary_hex="#111111")
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=color,
            price_override=900,
            is_default=True,
        )
        self.client_row = IgClient.objects.create(
            igsid="revision-action-client",
            reply_permission_epoch=3,
        )
        self.source = self._source("revision-action-source")
        self.turn = IgCustomerTurn.objects.create(
            client=self.client_row,
            primary_source_message=self.source,
            window_started_at=timezone.now(),
            window_deadline=timezone.now(),
        )
        IgTurnMessage.objects.create(
            turn=self.turn,
            message=self.source,
            ordinal=1,
            role=self.source.role,
        )
        revision = create_collecting_revision(
            self.turn,
            [self.source],
            now=timezone.now(),
            bypass_quiet=True,
        ).revision
        prep = claim_revision_preparation(revision.pk)
        sealed = seal_revision(revision.pk, prep.token).revision
        claimed = claim_sealed_revision(sealed.pk)
        self.revision = claimed.revision
        self.revision_token = claimed.token
        self.publication_binding = PublicationBinding(
            self.publication.pk,
            self.publication.version,
            self.publication.snapshot_hash,
        )
        self.client_row.intent = "checkout"
        self.client_row.stage = "checkout"
        self.client_row.save(update_fields=["intent", "stage"])
        self.controls = [{"kind": "item", "value": f"{self.product.pk}|2|M||{self.variant.pk}"}, {"kind": "paylink", "value": "full"}]

    def _source(self, mid):
        return InstagramBotMessage.objects.create(
            client=self.client_row,
            sender_id=self.client_row.igsid,
            provider_namespace="instagram_login:owner-1",
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            text=getattr(self, "source_text", "обираю цей варіант"),
            quick_reply_payload=getattr(self, "source_quick_reply", ""),
            mid=mid,
            status=InstagramBotMessage.Status.PENDING,
        )

    def _install_generation(self):
        from management.services.ig_response_control import ResponseControl, ValidatedResponse
        response = ValidatedResponse(reply_text="Перевірте деталі:", controls=tuple(ResponseControl(**row) for row in self.controls))
        control, reasons = checkout_authority_control(self.client_row, response.control)
        self.assertFalse(reasons)
        self.authority = build_revision_authority_bindings(self.client_row, claims=(CLAIM_CATALOG_CONFIGURATION,), control=control, server_authorized_actions=("checkout_proposal_create",))
        self.assertTrue(self.authority.ready, self.authority.reasons)
        proposal = {"schema_version": 1,
                    "sources": [{"message_id": self.source.pk}],
                    "generation": {"request_id": "checkout-test-request", "actual_model": "gemini-3.7-flash"}}
        proposal["policy_manifest"] = {"instruction_publication": {
            "id": self.publication.pk, "version": self.publication.version,
            "hash": self.publication.snapshot_hash,
        }}
        proposal["authority"] = _authority_projection(self.authority)
        proposal["response"] = {"reply_text": response.reply_text, "controls": self.controls}
        self.revision.generation_proposal = proposal
        self.revision.generation_proposal_digest = _digest(proposal)
        self.revision.generation_proposed_at = timezone.now()
        self.revision.save(update_fields=["generation_proposal", "generation_proposal_digest", "generation_proposed_at", "updated_at"])

    def _prepare(self, **overrides):
        if not self.revision.generation_proposal_digest:
            self._install_generation()
        values = dict(source_message_id=self.source.pk, settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch, publication=self.publication_binding, generation_proposal_digest=self.revision.generation_proposal_digest, authority=self.authority, reply_text="Перевірте деталі:")
        values.update(overrides)
        return prepare_revision_checkout(self.revision.pk, self.revision_token, **values)

    def test_standard_current_catalog_and_exact_crash_replay(self):
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)
        proposal = IgCheckoutProposal.objects.get(pk=result.proposal_id)
        self.assertEqual(proposal.quoted_total, Decimal("1800.00"))
        self.assertTrue(proposal.allow_promo)
        revision_count = IgCheckoutRevision.objects.count()
        again = self._prepare()
        self.assertTrue(again.planned, again.reasons)
        self.assertFalse(again.created)
        self.assertEqual([row.pk for row in result.effects], [row.pk for row in again.effects])
        self.assertEqual([row.payload for row in result.effects], [row.payload for row in again.effects])
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)
        self.assertEqual(IgCheckoutRevision.objects.count(), revision_count)
        original = self.revision.generation_proposal_digest
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal_digest, original)

    def test_two_items_current_prices_and_second_item_drift(self):
        from storefront.models import Product
        second = Product.objects.create(title="Second", slug="checkout-second", category=self.product.category, price=600, status="published")
        self.controls.insert(1, {"kind": "item", "value": f"{second.pk}|1|M|"})
        self._install_generation()
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)
        proposal = IgCheckoutProposal.objects.get(pk=result.proposal_id)
        self.assertEqual(proposal.items.count(), 2)
        self.assertEqual(proposal.quoted_total, Decimal("2400.00"))
        second.price = 700
        second.save(update_fields=["price"])
        self.assertFalse(self._prepare().planned)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)

    def test_model_price_never_enters_cart_authority(self):
        control = {"items": [self.controls[0]["value"]], "paylink": "full", "price": "0.01", "price_quoted": "0.01"}
        normalized, reasons = checkout_authority_control(self.client_row, control)
        self.assertFalse(reasons)
        result = build_revision_authority_bindings(self.client_row, claims=(CLAIM_CATALOG_CONFIGURATION,), control=normalized)
        self.assertTrue(result.ready)
        self.assertNotIn("price", str(result.fact_bindings[0]["selector"]))
        normalized["items"][0]["price"] = "0.01"
        self.assertFalse(build_revision_authority_bindings(self.client_row, claims=(CLAIM_CATALOG_CONFIGURATION,), control=normalized).ready)

    def test_failed_plan_rolls_back_offer_token_and_episode(self):
        with patch("management.services.ig_revision_checkout.plan_revision_effects", return_value=EffectPlanResult(reasons=("forced_cas_failure",))):
            result = self._prepare()
        self.assertFalse(result.planned)
        self.assertIn("forced_cas_failure", str(result.reasons))
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 0)
        self.assertEqual(IgCheckoutRevision.objects.count(), 0)
        self.assertEqual(self.revision.delivery_effects.count(), 0)

    def test_permission_or_catalog_drift_has_no_offer_token(self):
        self.client_row.bot_paused = True
        self.client_row.save(update_fields=["bot_paused"])
        self.assertFalse(self._prepare().planned)
        self.client_row.bot_paused = False
        self.client_row.save(update_fields=["bot_paused"])
        self.variant.price_override = 901
        self.variant.save(update_fields=["price_override"])
        self.assertFalse(self._prepare().planned)
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 0)

    def test_token_expiry_or_revocation_blocks_send_authority_and_remint(self):
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)
        claim = claim_next_effect(self.revision.pk, self.revision_token, "substantive_text")
        self.assertIsNotNone(claim.effect)
        token = IgCheckoutAccessToken.objects.get()
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        self.assertFalse(check_offer_bindings(result.effects[0].offer_bindings, revision=self.revision, client=self.client_row))
        self.assertFalse(self._prepare().planned)
        blocked = mark_provider_started(claim.effect.pk, claim.token, self.revision_token, fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
        self.assertNotEqual(blocked.effect.state, "provider_started")
        self.assertIn("offer_binding_unavailable", blocked.reason)
        token.revoked_at = None
        token.expires_at = timezone.now() - timedelta(seconds=1)
        token.save(update_fields=["revoked_at", "expires_at"])
        self.assertFalse(check_offer_bindings(result.effects[0].offer_bindings, revision=self.revision, client=self.client_row))
        self.assertFalse(self._prepare().planned)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)

    def test_model_purchase_claim_alone_does_not_authorize(self):
        self.client_row.intent = ""
        self.client_row.stage = "new"
        self.client_row.save(update_fields=["intent", "stage"])
        self.assertFalse(self._prepare().planned)
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def test_arbitrary_text_or_prior_text_plan_is_rejected(self):
        self.assertFalse(self._prepare(reply_text="Оплачено!").planned)
        self.assertFalse(self._prepare(noncheckout_effects=({"group": "substantive_text", "kind": "text"},)).planned)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 0)

    def test_failed_final_truth_rolls_back_created_token_and_offer(self):
        self._install_generation()
        with patch("management.services.ig_revision_checkout.prepare_text_effects") as prepare:
            from management.services.ig_reply_truth import ReplyTruthResult
            with patch("management.services.ig_reply_truth.validate_reply_truth", return_value=ReplyTruthResult(False, ("unauthorized_url",))):
                result = self._prepare()
        self.assertFalse(result.planned)
        self.assertIn("unauthorized_url", str(result.reasons))
        prepare.assert_not_called()
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 0)
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def test_catalog_media_and_checkout_commit_as_one_plan(self):
        from management.services.ig_catalog_media import _image_url
        self.product.main_image = "products/revision_checkout_catalog.png"
        self.product.save(update_fields=["main_image"])
        prior = ({"group": "catalog_media", "kind": "image", "payload": {
            "recipient": {"id": self.client_row.igsid},
            "message": {"attachment": {"type": "image", "payload": {"url": _image_url(self.product.main_image), "is_reusable": True}}},
        }, "projection_metadata": {"product_id": self.product.pk, "title": self.product.title, "part_index": 0}},)
        result = self._prepare(noncheckout_effects=prior)
        self.assertTrue(result.planned, result.reasons)
        self.assertEqual([row.group for row in result.effects], ["catalog_media", "substantive_text"])
        self.assertEqual(len({row.plan_digest for row in result.effects}), 1)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)
        self.assertFalse(self._prepare().planned)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)

    def test_prepay_without_direct_policy_source_is_denied(self):
        self.controls[-1] = {"kind": "paylink", "value": "prepay"}
        result = self._prepare()
        self.assertFalse(result.planned)
        self.assertIn("checkout_prepay_not_authorized", result.reasons)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 0)
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def test_viewed_offer_retains_authority_but_paid_offer_does_not(self):
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)
        proposal = IgCheckoutProposal.objects.get(pk=result.proposal_id)
        proposal.status = proposal.Status.VIEWED
        proposal.viewed_at = timezone.now()
        proposal.save(update_fields=["status", "viewed_at", "updated_at"])
        self.assertTrue(check_offer_bindings(result.effects[0].offer_bindings, revision=self.revision, client=self.client_row))
        self.assertTrue(self._prepare().planned)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)
        from management.services.ig_revision_authority import _proposal_projection
        proposal.status = proposal.Status.PAID
        self.assertEqual(_proposal_projection(proposal)["status"], "paid")
        proposal.status = proposal.Status.CANCELLED
        proposal.save(update_fields=["status", "updated_at"])
        self.assertFalse(check_offer_bindings(result.effects[0].offer_bindings, revision=self.revision, client=self.client_row))


@override_settings(
    GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.shop",
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100,
)
class RevisionCheckoutV2PrepayTests(TransactionTestCase):
    reset_sequences = True
    source_text = "Чи можна 200 грн передоплати, а решту післяплатою?"
    setUp = RevisionCheckoutTests.setUp
    _source = RevisionCheckoutTests._source
    _install_generation = RevisionCheckoutTests._install_generation
    _prepare = RevisionCheckoutTests._prepare

    def test_full_online_proposal_preserves_existing_browser_200_option(self):
        from management.services.ig_checkout_policy import payment_choice_for_post

        self.controls.append({"kind": "payment", "value": "0.01"})
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)
        proposal = IgCheckoutProposal.objects.get(pk=result.proposal_id)
        self.assertTrue(proposal.assisted_checkout_v2)
        self.assertEqual(proposal.pay_type, "online_full")
        self.assertEqual(proposal.requested_payment_amount, proposal.quoted_total)
        self.assertEqual(proposal.payment_policy, "full_or_200_cod")
        self.assertEqual(payment_choice_for_post(proposal, "prepay_200_cod"), "prepay_200_cod")
        self.assertEqual(payment_choice_for_post(proposal, None), "online_full")
        self.assertTrue(proposal.allow_promo)

    def test_typed_prepay_current_source_and_replay_keep_one_token(self):
        from management.services.ig_checkout_policy import payment_choice_for_post

        self.controls[-1] = {"kind": "paylink", "value": "prepay"}
        self.controls.append({"kind": "payment", "value": "200.00"})
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)
        proposal = IgCheckoutProposal.objects.get(pk=result.proposal_id)
        self.assertEqual(proposal.payment_policy_evidence_message_id, self.source.pk)
        self.assertEqual(proposal.payment_policy_evidence_kind, "direct_question")
        self.assertEqual(proposal.pay_type, "online_full")
        self.assertEqual(payment_choice_for_post(proposal, "prepay_200_cod"), "prepay_200_cod")
        again = self._prepare()
        self.assertTrue(again.planned, again.reasons)
        self.assertEqual([row.pk for row in result.effects], [row.pk for row in again.effects])
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)
        self.assertTrue(proposal.allow_promo)

    def test_payment_200_control_uses_existing_policy_not_model_amount_authority(self):
        self.controls[-1] = {"kind": "payment", "value": "200"}
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)
        proposal = IgCheckoutProposal.objects.get(pk=result.proposal_id)
        self.assertEqual(proposal.requested_payment_amount, Decimal("1800.00"))
        self.assertEqual(proposal.payment_policy, "full_or_200_cod")

    def test_source_drift_after_seal_denies_before_token(self):
        self.controls[-1] = {"kind": "paylink", "value": "prepay"}
        self.source.text = "Можно 200 грн предоплаты, остальное наложкой?"
        self.source.save(update_fields=["text"])
        result = self._prepare()
        self.assertFalse(result.planned)
        self.assertIn("checkout_payment_source_changed", result.reasons)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 0)
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def test_latest_unrelated_customer_message_cannot_reuse_old_policy_source(self):
        self.controls[-1] = {"kind": "paylink", "value": "prepay"}
        InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid, role="user", text="Добре")
        result = self._prepare()
        self.assertFalse(result.planned)
        self.assertIn("checkout_prepay_not_authorized", result.reasons)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 0)

    def test_legacy_checkout_is_not_forced_into_v2_for_prepay(self):
        from management.services.ig_checkout import create_or_update_proposal

        with override_settings(IG_ASSISTED_CHECKOUT_V2="off"):
            legacy = create_or_update_proposal(
                client=self.client_row, pay_type="online_full",
                item_specs=[{"product_id": self.product.pk, "color_variant_id": self.variant.pk, "qty": 2, "size": "M"}],
                evidence={"message_ids": [self.source.pk]}, allow_promo=True,
            )
        self.controls[-1] = {"kind": "paylink", "value": "prepay"}
        result = self._prepare()
        self.assertFalse(result.planned)
        self.assertIn("checkout_prepay_policy_unavailable", result.reasons)
        legacy.refresh_from_db()
        self.assertFalse(legacy.assisted_checkout_v2)
        self.assertEqual(legacy.payment_policy, "legacy")
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 0)

    def test_arbitrary_payment_or_custom_prepay_never_gets_authority(self):
        from management.services.ig_revision_checkout import authorize_revision_checkout

        control = {"items": [self.controls[0]["value"]], "paylink": "prepay", "payment": "300"}
        _normalized, reasons = authorize_revision_checkout(self.client_row, control, self.source.text, sealed_sources=self.revision.bundle_snapshot["sources"])
        self.assertIn("checkout_payment_amount_unsupported", reasons)
        self.client_row.intent = "custom_print"
        control["payment"] = "200"
        _normalized, reasons = authorize_revision_checkout(self.client_row, control, self.source.text, sealed_sources=self.revision.bundle_snapshot["sources"])
        self.assertIn("custom_confirmation_and_price_authority_missing", reasons)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 0)


@override_settings(
    GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.shop",
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100,
)
class RevisionCheckoutV2QuickReplyTests(TransactionTestCase):
    reset_sequences = True
    source_text = "Передоплата"
    source_quick_reply = "twc:v1:checkout_payment:prepay_200_cod"
    setUp = RevisionCheckoutTests.setUp
    _source = RevisionCheckoutTests._source
    _install_generation = RevisionCheckoutTests._install_generation
    _prepare = RevisionCheckoutTests._prepare

    def test_exact_sealed_quick_reply_unlocks_existing_200_policy(self):
        self.controls[-1] = {"kind": "paylink", "value": "prepay"}
        result = self._prepare()
        self.assertTrue(result.planned, result.reasons)
        proposal = IgCheckoutProposal.objects.get(pk=result.proposal_id)
        self.assertEqual(proposal.payment_policy, "full_or_200_cod")
        self.assertEqual(proposal.payment_policy_evidence_kind, "quick_reply")
        self.assertEqual(proposal.payment_policy_evidence_message_id, self.source.pk)
        proposal.payment_policy_evidence_kind = "direct_question"
        proposal.save(update_fields=["payment_policy_evidence_kind", "updated_at"])
        self.assertFalse(check_offer_bindings(
            result.effects[0].offer_bindings,
            revision=self.revision, client=self.client_row,
        ))
        self.assertFalse(self._prepare().planned)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)
