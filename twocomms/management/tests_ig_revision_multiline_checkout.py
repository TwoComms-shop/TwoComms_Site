"""All-line source proof, immutable quote artifacts and final effect admission."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management.models import (
    BotPolicyPublication, IgCheckoutAccessToken, IgCheckoutProposal, IgCheckoutRevision,
    IgClient, IgCommercialEpisode, IgCustomerTurn, IgDeal, IgFunnelResetAudit,
    IgTurnMessage, InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_checkout import CheckoutConfigurationError, create_or_update_proposal, validate_checkout_items
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_commerce_state import apply_turn
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_revision_actions import _authority_projection
from management.services.ig_revision_authority import (
    CLAIM_CATALOG_CONFIGURATION, build_revision_authority_bindings, check_fact_bindings,
)
from management.services.ig_revision_cart_binding import same_checkout_source_capture, stable_checkout_source_capture
from management.services.ig_revision_checkout import (
    authorize_revision_checkout, checkout_authority_control, checkout_owner_scope,
    prepare_revision_checkout, rebind_checkout_cart_owner,
)
from management.services.ig_revision_outbox import PublicationBinding, _digest
from management.services.ig_turn_revisions import (
    claim_revision_preparation, claim_sealed_revision, create_collecting_revision, seal_revision,
)
from management.services.instagram_bot import ingress_provider_namespace
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product, ProductFitOption


def signed_capture(payload):
    value = deepcopy(payload)
    value.pop("capture_digest", None)
    value["capture_digest"] = hashlib.sha256(json.dumps(value, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


class StableCheckoutCaptureTests(SimpleTestCase):
    def setUp(self):
        self.capture = signed_capture({"schema": "source-selections.v1", "status": "captured",
            "scope": {"episode_id": 8}, "lines": [{"line_id": "first", "recipient_id": "self"}],
            "fence": {"owner_digest": "a" * 64, "source_digest": "b" * 64,
                "snapshot_digest": "c" * 64}, "source_watermark": {"message_id": 15}})

    def test_only_owner_hash_and_derived_top_hash_are_excluded(self):
        current = deepcopy(self.capture)
        current["fence"]["owner_digest"] = "d" * 64
        current = signed_capture(current)
        self.assertTrue(same_checkout_source_capture(self.capture, current))
        detached = stable_checkout_source_capture(current)
        detached["lines"][0]["recipient_id"] = "someone_else"
        self.assertEqual(current["lines"][0]["recipient_id"], "self")

    def test_every_other_source_field_remains_strict(self):
        for path, value in (("source_digest", "e" * 64), ("snapshot_digest", "e" * 64)):
            current = deepcopy(self.capture)
            current["fence"][path] = value
            self.assertFalse(same_checkout_source_capture(self.capture, signed_capture(current)))
        for key, value in (("scope", {"episode_id": 9}), ("source_watermark", {"message_id": 16}),
                ("lines", [{"line_id": "first", "recipient_id": "friend"}])):
            current = {**self.capture, key: value}
            self.assertFalse(same_checkout_source_capture(self.capture, signed_capture(current)))

    def test_forged_stale_digest_and_missing_capture_abstain(self):
        current = deepcopy(self.capture)
        current["lines"][0]["recipient_id"] = "friend"
        self.assertIsNone(stable_checkout_source_capture(current))
        self.assertFalse(same_checkout_source_capture(None, self.capture))
        self.assertFalse(same_checkout_source_capture({}, {}))


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.shop",
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class RevisionMultilineCheckoutTests(TransactionTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        env.start()
        self.addCleanup(env.stop)
        no_http = patch("storefront.views.monobank._monobank_api_request",
            side_effect=AssertionError("source checkout must not call provider"))
        self.http = no_http.start()
        self.addCleanup(no_http.stop)
        snapshot = {"schema_version": 1, "instructions": []}
        self.publication = BotPolicyPublication.objects.create(version=1, kind="publish",
            schema_version=1, snapshot=snapshot, snapshot_hash=_digest(snapshot),
            compiler_version="instruction-set-v1", instruction_count=0)
        self.settings = InstagramBotSettings.objects.create(pk=1, is_enabled=True,
            ig_user_id="all-line-shop", reply_permission_epoch=4,
            active_instruction_publication=self.publication)
        self.namespace = ingress_provider_namespace(self.settings)
        self.customer = IgClient.objects.create(igsid="all-line-" + self._testMethodName[:45], reply_permission_epoch=3)
        self.products, self.variants = [], []
        category = Category.objects.create(name="Футболки", slug="all-line-shirts")
        color = Color.objects.create(name="Чорний", primary_hex="#111111")
        for index, title in enumerate(("Atlas Wave", "Ocean Frame")):
            product = Product.objects.create(title=title, slug="all-line-" + str(index),
                category=category, price=900 + 100 * index, status="published")
            ProductFitOption.objects.create(product=product, code="classic", label="Classic", is_active=True)
            self.variants.append(ProductColorVariant.objects.create(product=product, color=color,
                stock=20, is_default=True))
            self.products.append(product)
        self.ordinal = 0
        self.reduce("добавьте Atlas Wave футболку цвет чёрный крой classic размер L")
        self.reduce("добавьте Ocean Frame футболку для друга цвет чёрный крой classic размер M")
        self.source = self.reduce("Хочу купити все, дайте посилання на оплату")
        self.capture = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(self.capture["status"], "captured", self.capture)
        self.assertEqual(len(self.capture["lines"]), 2)

    def reduce(self, text):
        self.ordinal += 1
        source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", text=text, mid="all-line-source-" + str(self.ordinal),
            provider_namespace=self.namespace,
            provider_created_at=timezone.now() - timedelta(minutes=5) + timedelta(seconds=self.ordinal))
        apply_turn(self.customer, source, parse_turn(text), reply_payload={})
        self.customer.refresh_from_db()
        return source

    def control(self, capture=None, **provider):
        return checkout_authority_control(self.customer, {"paylink": "full", **provider},
            source_cart_capture=self.capture if capture is None else capture)

    def quote(self, control):
        return create_or_update_proposal(client=self.customer, pay_type="online_full", item_specs=control["items"],
            allow_promo=True, **{key: control[key] for key in (
                "source_cart_binding", "source_cart_capture", "checkout_owner_scope")})

    def authority(self, control):
        self.ensure_revision()
        control = deepcopy(control)
        control["source_cart_artifact"] = {"kind": "generation_capture", "revision_id": self.revision.pk}
        return build_revision_authority_bindings(self.customer, claims=(CLAIM_CATALOG_CONFIGURATION,),
            control=control, settings_obj=self.settings, server_authorized_actions=("checkout_proposal_create",))

    def facts_current(self, bindings):
        return check_fact_bindings(bindings, revision=SimpleNamespace(client_id=self.customer.pk),
            client=self.customer, settings_obj=self.settings)

    def ensure_revision(self):
        if hasattr(self, "revision"):
            return
        turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=turn, message=self.source, ordinal=1, role="user")
        revision = create_collecting_revision(turn, [self.source], bypass_quiet=True).revision
        preparation = claim_revision_preparation(revision.pk)
        sealed = seal_revision(revision.pk, preparation.token).revision
        claim = claim_sealed_revision(sealed.pk)
        self.revision, self.revision_token = claim.revision, claim.token

    def install_generation(self, *, include_capture=True):
        control, reasons = self.control()
        self.assertFalse(reasons, reasons)
        authority = self.authority(control)
        self.assertTrue(authority.ready, authority.reasons)
        revision = self.revision
        proposal = {"schema_version": 1, "sources": [{"message_id": self.source.pk}],
            "generation": {"request_id": "all-lines-generation", "actual_model": "gemini-3.7-flash"},
            "policy_manifest": {"instruction_publication": {"id": self.publication.pk,
                "version": self.publication.version, "hash": self.publication.snapshot_hash}},
            "authority": _authority_projection(authority),
            "response": {"reply_text": "Перевірте деталі:", "controls": [{"kind": "paylink", "value": "full"}]}}
        if include_capture:
            proposal["source_cart_capture"] = deepcopy(self.capture)
        revision.generation_proposal = proposal
        revision.generation_proposal_digest = _digest(proposal)
        revision.generation_proposed_at = timezone.now()
        revision.save(update_fields=["generation_proposal", "generation_proposal_digest", "generation_proposed_at", "updated_at"])
        self.bound_authority = authority

    def prepare(self):
        return prepare_revision_checkout(self.revision.pk, self.revision_token, source_message_id=self.source.pk,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash),
            generation_proposal_digest=self.revision.generation_proposal_digest, authority=self.bound_authority,
            reply_text="Перевірте деталі:", source_cart_capture=self.capture)

    def test_backend_uses_all_lines_and_ignores_provider_item_selectors(self):
        control, reasons = self.control(items=[{"product_id": 999999, "qty": 50}], product=999999, size="XS")
        self.assertFalse(reasons, reasons)
        self.assertEqual([item["product_id"] for item in control["items"]], [product.pk for product in self.products])
        self.assertEqual([item["size"] for item in control["items"]], ["L", "M"])
        maps = control["source_cart_binding"]["quote_line_map"]
        self.assertEqual([row["recipient_id"] for row in maps], ["self", "friend"])
        self.assertTrue(all(not row["quantity_source_confirmed"] for row in maps))
        authorized, denied = authorize_revision_checkout(self.customer, {"paylink": "full", "product": 999999},
            self.source.text, source_cart_capture=self.capture)
        self.assertFalse(denied, denied)
        self.assertEqual(authorized, control)
        self.http.assert_not_called()

    def test_unbound_multiline_cannot_fall_back_to_active_or_provider_items(self):
        for supplied in ({"paylink": "full"}, {"paylink": "full", "items": [f"{self.products[0].pk}|1|L|classic|{self.variants[0].pk}"]}):
            result, reasons = checkout_authority_control(self.customer, supplied)
            self.assertEqual((result, reasons), ({}, ("checkout_cart_capture_missing",)))
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def test_second_line_missing_fit_blocks_whole_cart_without_offer(self):
        self.reduce("уберите вторую футболку")
        self.reduce("добавьте Ocean Frame футболку для друга размер M")
        result, reasons = self.control(capture_current_selection_lines(self.customer.pk))
        self.assertEqual((result, reasons), ({}, ("checkout_cart_readiness_incomplete",)))
        self.assertEqual((IgDeal.objects.count(), IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (0, 0, 0))

    def test_original_capture_and_binding_are_immutable_quote_artifacts(self):
        control, reasons = self.control()
        self.assertFalse(reasons)
        proposal = self.quote(control)
        artifact = proposal.revisions.get(revision=proposal.revision).snapshot
        self.assertEqual(artifact["source_cart_capture"], self.capture)
        self.assertEqual(artifact["source_cart_binding"], control["source_cart_binding"])
        self.assertEqual(proposal.items.count(), 2)
        self.assertEqual(proposal.quoted_total, Decimal("1900.00"))
        control["source_cart_capture"]["lines"][0]["recipient_id"] = "tampered"
        self.assertEqual(proposal.revisions.get(revision=proposal.revision).snapshot, artifact)

    def test_fresh_same_quote_after_owned_materialization_replays_original_revision(self):
        control, reasons = self.control()
        self.assertFalse(reasons)
        first = self.quote(control)
        original = deepcopy(first.revisions.get(revision=first.revision).snapshot)
        self.customer.refresh_from_db()
        fresh_capture = capture_current_selection_lines(self.customer.pk)
        self.assertNotEqual(self.capture["capture_digest"], fresh_capture["capture_digest"])
        self.assertTrue(same_checkout_source_capture(self.capture, fresh_capture))
        retry_control, reasons = self.control(fresh_capture)
        self.assertFalse(reasons, reasons)
        retry = self.quote(retry_control)
        self.assertEqual((first.pk, retry.pk, retry.revision, retry.revisions.count()), (first.pk, first.pk, 1, 1))
        self.assertEqual(retry.revisions.get(revision=1).snapshot, original)

    def test_same_money_changed_head_makes_new_revision_not_early_replay(self):
        control, reasons = self.control()
        self.assertFalse(reasons)
        first = self.quote(control)
        old = deepcopy(first.revisions.get(revision=1).snapshot)
        self.reduce("Гаразд")
        fresh_capture = capture_current_selection_lines(self.customer.pk)
        current, reasons = self.control(fresh_capture)
        self.assertFalse(reasons, reasons)
        second = self.quote(current)
        self.assertEqual((first.pk, second.pk, first.items_digest, second.items_digest), (first.pk, first.pk, first.items_digest, first.items_digest))
        self.assertEqual((second.revision, second.revisions.count()), (2, 2))
        self.assertEqual(second.revisions.get(revision=1).snapshot, old)

    def test_unbound_retry_cannot_remove_existing_source_line_ownership(self):
        control, reasons = self.control()
        self.assertFalse(reasons)
        first = self.quote(control)
        original = deepcopy(first.revisions.get(revision=1).snapshot)
        with self.assertRaises(CheckoutConfigurationError) as denied:
            create_or_update_proposal(client=self.customer, pay_type="online_full",
                item_specs=control["items"], allow_promo=True)
        self.assertEqual(denied.exception.code, "checkout_cart_binding_missing")
        first.refresh_from_db()
        self.assertEqual((first.revision, first.revisions.count()), (1, 1))
        self.assertEqual(first.revisions.get(revision=1).snapshot, original)

    def test_nonactive_source_correction_invalidates_authority_and_quote(self):
        control, reasons = self.control()
        self.assertFalse(reasons)
        authority = self.authority(control)
        self.reduce("измените первую футболку на размер XL")
        self.assertFalse(self.facts_current(authority.fact_bindings))
        result, reasons = self.control()
        self.assertEqual((result, reasons), ({}, ("checkout_cart_source_changed",)))
        with self.assertRaises(CheckoutConfigurationError):
            self.quote(control)
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def test_second_product_price_drift_rejects_old_authority(self):
        control, reasons = self.control()
        self.assertFalse(reasons)
        authority = self.authority(control)
        self.products[1].price = Decimal("1200.00")
        self.products[1].save(update_fields=["price"])
        self.assertFalse(self.facts_current(authority.fact_bindings))
        current, reasons = self.control()
        self.assertFalse(reasons)
        proposal = self.quote(current)
        self.assertEqual(proposal.quoted_total, Decimal("2100.00"))

    def test_nonactive_color_replacement_rebinds_only_new_source_quote(self):
        pink = Color.objects.create(name="Рожевий", primary_hex="#dd6688")
        variant = ProductColorVariant.objects.create(product=self.products[0], color=pink, stock=20)
        control, reasons = self.control()
        self.assertFalse(reasons)
        first = self.quote(control)
        original = deepcopy(first.revisions.get(revision=1).snapshot)
        changed = self.reduce("не чёрную, а розовую первую футболку")
        rejected, reasons = self.control()
        self.assertEqual((rejected, reasons), ({}, ("checkout_cart_source_changed",)))
        capture = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(capture["lines"][0]["fields"]["color"]["source"]["source_message_id"], changed.pk)
        current, reasons = self.control(capture)
        self.assertFalse(reasons, reasons)
        self.assertEqual([item["color_variant_id"] for item in current["items"]], [variant.pk, self.variants[1].pk])
        second = self.quote(current)
        self.assertEqual((second.pk, second.revision, second.items.count()), (first.pk, 2, 2))
        self.assertEqual(second.revisions.get(revision=1).snapshot, original)

    def test_recipient_change_invalidates_prior_configuration_proof(self):
        control, reasons = self.control()
        self.assertFalse(reasons)
        self.reduce("измените первую футболку для друга")
        capture = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(capture["lines"][0]["recipient_id"], "friend")
        self.assertNotIn("product_id", capture["lines"][0]["fields"])
        result, reasons = self.control()
        self.assertEqual((result, reasons), ({}, ("checkout_cart_source_changed",)))
        with self.assertRaises(CheckoutConfigurationError):
            self.quote(control)
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def test_ambiguous_catalog_color_does_not_choose_first_matching_variant(self):
        dark = Color.objects.create(name="Вугільно-чорний", primary_hex="#222222")
        ProductColorVariant.objects.create(product=self.products[0], color=dark, stock=20)
        result, reasons = self.control()
        self.assertEqual((result, reasons), ({}, ("checkout_cart_color_unresolved",)))
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def test_foreign_owner_timestamp_is_not_an_owned_quote_rebind(self):
        control, reasons = self.control()
        self.assertFalse(reasons)
        episode = IgCommercialEpisode.objects.get(pk=self.customer.current_commercial_episode_id)
        episode.save(update_fields=["updated_at"])
        result, reasons = checkout_authority_control(self.customer, control)
        self.assertEqual((result, reasons), ({}, ("checkout_cart_owner_changed",)))
        self.assertIsNone(rebind_checkout_cart_owner(control, before=control["checkout_owner_scope"],
            after=checkout_owner_scope(self.customer), checkout=type("Offer", (), {
                "commercial_episode_id": episode.pk, "deal_id": None, "pk": None})()))

    def test_reset_erasure_namespace_and_source_text_fail_old_capture(self):
        for mutation in ("reset", "erasure", "namespace", "source"):
            with self.subTest(mutation=mutation):
                # Independent nested transactions roll fixtures back between fences.
                from django.db import transaction
                with transaction.atomic():
                    if mutation == "reset":
                        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=self.source.pk)
                    elif mutation == "erasure":
                        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=timezone.now())
                    elif mutation == "namespace":
                        InstagramBotSettings.objects.filter(pk=1).update(ig_user_id="different-shop")
                    else:
                        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="не хочу купувати")
                    result, reasons = self.control()
                    self.assertFalse(result)
                    self.assertTrue(reasons)
                    transaction.set_rollback(True)
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def test_complete_atomic_prepare_and_exact_effect_replay(self):
        self.install_generation()
        self.assertTrue(self.facts_current(self.bound_authority.fact_bindings))
        self.assertLessEqual(len(json.dumps(self.bound_authority.fact_bindings[0]).encode()), 4096)
        result = self.prepare()
        self.assertTrue(result.planned, result.reasons)
        proposal = IgCheckoutProposal.objects.get(pk=result.proposal_id)
        self.assertEqual(proposal.items.count(), 2)
        self.assertEqual(proposal.revisions.get(revision=1).snapshot["source_cart_capture"], self.capture)
        self.assertTrue(self.facts_current(result.effects[0].fact_bindings))
        replay = self.prepare()
        self.assertTrue(replay.planned, replay.reasons)
        self.assertFalse(replay.created)
        self.assertEqual([row.pk for row in replay.effects], [row.pk for row in result.effects])
        self.assertEqual((IgCheckoutRevision.objects.count(), IgCheckoutAccessToken.objects.count()), (1, 1))
        self.http.assert_not_called()

    def test_precommit_compact_reference_requires_exact_live_owned_revision(self):
        control, reasons = self.control()
        self.assertFalse(reasons)
        authority = self.authority(control)
        self.assertTrue(authority.ready, authority.reasons)
        self.assertTrue(self.facts_current(authority.fact_bindings))
        self.revision.lease_until = timezone.now() - timedelta(seconds=1)
        self.revision.save(update_fields=["lease_until", "updated_at"])
        self.assertFalse(self.facts_current(authority.fact_bindings))

    def test_committed_missing_generation_artifact_cannot_fall_back_to_current(self):
        # A historical missing key is present at its first immutable commit;
        # neither model nor queryset immutability is bypassed by this fixture.
        self.install_generation(include_capture=False)
        self.assertNotIn("source_cart_capture", self.revision.generation_proposal)
        self.assertEqual(_digest(self.revision.generation_proposal), self.revision.generation_proposal_digest)
        self.assertFalse(self.facts_current(self.bound_authority.fact_bindings))

    def test_postoffer_checker_uses_exact_revision_artifact_after_restart(self):
        self.install_generation()
        result = self.prepare()
        self.assertTrue(result.planned, result.reasons)
        facts = deepcopy(result.effects[0].fact_bindings)
        catalog = next(row for row in facts if row["claim"] == CLAIM_CATALOG_CONFIGURATION)
        self.assertEqual(catalog["selector"]["source_cart_reference"]["artifact"]["kind"], "checkout_revision")
        self.assertNotIn("source_cart_capture", catalog["selector"])
        self.assertNotIn("items", catalog["selector"])
        self.assertLessEqual(len(json.dumps(catalog).encode()), 4096)
        self.assertTrue(self.facts_current(facts))
        reference = catalog["selector"]["source_cart_reference"]
        reference["artifact"]["checkout_revision_id"] += 99999
        self.assertFalse(self.facts_current(facts))

    def test_eight_line_binding_budget_counts_all_source_without_raw_artifacts(self):
        original_episode = self.customer.current_commercial_episode_id
        original_lines = [line["line_id"] for line in self.capture["lines"]]
        extra_products, added_sources = [], []
        # A numeric title immediately before 'футболку' is an explicit quantity
        # in the real parser (0 removes); use unambiguous reviewed model names.
        names = ("Amber Reef", "Birch Ridge", "Copper Peak", "Dawn Field", "Ember Stone", "Frost Valley")
        for index, title in enumerate(names):
            product = Product.objects.create(title=title, slug=f"all-line-extra-{index}",
                category=self.products[0].category, price=700, status="published")
            ProductFitOption.objects.create(product=product, code="classic", label="Classic", is_active=True)
            ProductColorVariant.objects.create(product=product, color=self.variants[0].color, stock=20)
            text = f"добавьте {title} футболку цвет чёрный крой classic размер L"
            parsed = parse_turn(text)
            self.assertEqual([operation.operation for operation in parsed.line_operations], ["add"])
            self.assertNotIn("quantity", parsed.line_operations[0].field_updates)
            added_sources.append(self.reduce(text))
            extra_products.append(product)
            current = capture_current_selection_lines(self.customer.pk)
            self.assertEqual(len(current["lines"]), index + 3)
            self.assertEqual(self.customer.current_commercial_episode_id, original_episode)
        capture = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(len(capture["lines"]), 8)
        self.assertEqual([line["line_id"] for line in capture["lines"][:2]], original_lines)
        for line, source, product in zip(capture["lines"][2:], added_sources, extra_products):
            field = line["fields"]["product_id"]
            self.assertEqual((field["value"], field["source"]["source_message_id"]), (product.pk, source.pk))
        control, reasons = self.control(capture)
        self.assertFalse(reasons, reasons)
        self.assertEqual(len(control["items"]), 8)
        self.assertEqual([item["product_id"] for item in control["items"]],
            [product.pk for product in [*self.products, *extra_products]])
        authority = self.authority(control)
        self.assertTrue(authority.ready, authority.reasons)
        binding = authority.fact_bindings[0]
        self.assertEqual(binding["subjects"]["item_count"], 8)
        reference = binding["selector"]["source_cart_reference"]
        self.assertEqual(reference["semantic_digest"], _digest(stable_checkout_source_capture(capture)))
        self.assertEqual(reference["configuration_digest"], _digest(control["items"]))
        self.assertEqual(reference["quote_map_digest"], _digest(control["source_cart_binding"]["quote_line_map"]))
        self.assertLessEqual(len(json.dumps(binding).encode()), 4096)
        self.assertTrue(self.facts_current(authority.fact_bindings))

    def test_nonactive_drift_after_generation_prevents_every_effect(self):
        self.install_generation()
        self.reduce("измените первую футболку на размер XL")
        result = self.prepare()
        self.assertFalse(result.planned)
        self.assertEqual((IgDeal.objects.count(), IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (0, 0, 0))
        self.assertEqual(self.revision.delivery_effects.count(), 0)

    def test_effect_failure_rolls_back_offer_owner_and_token(self):
        self.install_generation()
        from management.services.ig_revision_outbox import EffectPlanResult
        episode = IgCommercialEpisode.objects.get(pk=self.customer.current_commercial_episode_id)
        before = checkout_owner_scope(self.customer)
        with patch("management.services.ig_revision_checkout.plan_revision_effects",
                return_value=EffectPlanResult(reasons=("forced_final_cas_failure",))):
            result = self.prepare()
        self.assertFalse(result.planned)
        self.assertEqual(checkout_owner_scope(self.customer), before)
        episode.refresh_from_db()
        self.assertIsNone(episode.deal_id)
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count(), self.revision.delivery_effects.count()), (0, 0, 0))

    def test_live_overflow_declines_every_line_without_truncation(self):
        for index in range(7):
            self.reduce("добавьте Atlas Wave футболку размер L крой classic")
        captured = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(len(captured["lines"]), 9)
        control, reasons = self.control(captured)
        self.assertEqual((control, reasons), ({}, ("cart_item_limit",)))
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def duplicate_cart(self, *, recipient="друга", first_quantity=1):
        self.reduce("уберите вторую футболку")
        if first_quantity != 1:
            self.reduce(f"измените количество первой футболки на {first_quantity}")
        self.reduce(f"добавьте Atlas Wave футболку для {recipient} количество 1 цвет чёрный крой classic размер L")
        self.source = self.reduce("Хочу купити обидві футболки. Дайте посилання на оплату.")
        self.capture = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(len(self.capture["lines"]), 2)
        return self.control()

    def test_same_sku_for_different_recipients_keeps_distinct_quote_positions(self):
        control, reasons = self.duplicate_cart()
        self.assertFalse(reasons, reasons)
        self.assertEqual([line["recipient_id"] for line in self.capture["lines"]], ["self", "friend"])
        maps = control["source_cart_binding"]["quote_line_map"]
        self.assertEqual([row["recipient_id"] for row in maps], ["self", "friend"])
        self.assertNotEqual(maps[0]["line_id"], maps[1]["line_id"])
        self.assertEqual(maps[0]["configuration"], maps[1]["configuration"])
        proposal = self.quote(control)
        items = list(proposal.items.order_by("position", "id"))
        self.assertEqual([item.position for item in items], [0, 1])
        self.assertNotEqual(items[0].pk, items[1].pk)
        self.assertEqual([item.quantity for item in items], [1, 1])
        self.assertEqual([item.quoted_unit_price for item in items], [Decimal("900.00")] * 2)
        self.assertEqual(proposal.quoted_total, Decimal("1800.00"))
        original = deepcopy(proposal.revisions.get(revision=1).snapshot)
        current, reasons = self.control(capture_current_selection_lines(self.customer.pk))
        self.assertFalse(reasons)
        replay = self.quote(current)
        self.assertEqual((replay.pk, replay.revision, replay.revisions.count()), (proposal.pk, 1, 1))
        self.assertEqual(replay.revisions.get(revision=1).snapshot, original)

    def test_same_recipient_duplicate_requires_quantity_clarification(self):
        control, reasons = self.duplicate_cart(recipient="себя")
        self.assertEqual((control, reasons), ({}, ("checkout:duplicate_recipient_items",)))
        self.assertEqual(IgCheckoutProposal.objects.count(), 0)

    def test_unbound_duplicate_and_boolean_optin_cannot_create_quote(self):
        control, reasons = self.duplicate_cart()
        self.assertFalse(reasons)
        for kwargs, code in (({}, "duplicate_items"), ({"source_cart_binding": True}, "source_cart_binding_invalid")):
            with self.subTest(kwargs=kwargs), self.assertRaises(CheckoutConfigurationError) as denied:
                validate_checkout_items(client=self.customer, item_specs=control["items"], **kwargs)
            self.assertEqual(denied.exception.code, code)
        provider_items = [f"{self.products[0].pk}|1|L|classic|{self.variants[0].pk}"] * 2
        _control, reasons = checkout_authority_control(self.customer, {"items": provider_items, "paylink": "full"})
        self.assertEqual(reasons, ("checkout_cart_capture_missing",))
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (0, 0))

    def test_duplicate_recipient_change_after_generation_admits_no_offer_or_effect(self):
        control, reasons = self.duplicate_cart()
        self.assertFalse(reasons)
        self.install_generation()
        self.reduce("измените вторую футболку для себя")
        result = self.prepare()
        self.assertFalse(result.planned)
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count(),
            self.revision.delivery_effects.count()), (0, 0, 0))

    def test_duplicate_sku_atomic_prepare_closes_both_positions_once(self):
        control, reasons = self.duplicate_cart()
        self.assertFalse(reasons)
        self.install_generation()
        result = self.prepare()
        self.assertTrue(result.planned, result.reasons)
        proposal = IgCheckoutProposal.objects.get(pk=result.proposal_id)
        self.assertEqual(proposal.items.count(), 2)
        self.assertTrue(self.facts_current(result.effects[0].fact_bindings))
        replay = self.prepare()
        self.assertTrue(replay.planned, replay.reasons)
        self.assertFalse(replay.created)
        self.assertEqual([row.pk for row in result.effects], [row.pk for row in replay.effects])
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)

    def test_duplicate_sku_aggregate_stock_shortfall_rolls_back_entire_quote(self):
        control, reasons = self.duplicate_cart()
        self.assertFalse(reasons)
        self.variants[0].stock = 1
        self.variants[0].save(update_fields=["stock"])
        with self.assertRaises(CheckoutConfigurationError) as denied:
            self.quote(control)
        self.assertEqual(denied.exception.code, "insufficient_stock")
        self.assertEqual((IgDeal.objects.count(), IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (0, 0, 0))

    def test_ambiguous_recipient_before_and_after_quote_cannot_price_partial_cart(self):
        from management.services.ig_checkout_payment import _revalidate_frozen_proposal
        control, reasons = self.duplicate_cart()
        self.assertFalse(reasons)
        for quoted in (False, True):
            from django.db import transaction
            with self.subTest(quoted=quoted), transaction.atomic():
                proposal = self.quote(control) if quoted else None
                self.reduce("добавьте Atlas Wave футболку для друга і для себе розмір L")
                capture = capture_current_selection_lines(self.customer.pk)
                current, reasons = self.control(capture)
                self.assertEqual((current, reasons), ({}, ("checkout_cart_clarification_required",)))
                stale, reasons = self.control()
                self.assertEqual((stale, reasons), ({}, ("checkout_cart_source_changed",)))
                if proposal is not None:
                    original = deepcopy(proposal.revisions.get(revision=1).snapshot)
                    _revalidate_frozen_proposal(proposal)
                    self.assertEqual(proposal.revisions.get(revision=1).snapshot, original)
                self.assertEqual(IgCheckoutProposal.objects.count(), int(quoted))
                self.assertEqual(IgCheckoutAccessToken.objects.count(), 0)
                transaction.set_rollback(True)
            self.customer.refresh_from_db()
        self.http.assert_not_called()

    def test_duplicate_sku_canonical_payment_order_and_refund_preserve_both_maps(self):
        from management.models import IgPaymentProjection
        from management.tests_ig_hosted_settlement import HostedSettlementFixture
        from orders.models import OrderItem
        from storefront.views.monobank import _apply_payment_attempt_status
        control, reasons = self.duplicate_cart()
        self.assertFalse(reasons)
        proposal = self.quote(control)
        original = deepcopy(proposal.revisions.get(revision=1).snapshot)
        clock = timezone.now()
        with patch("management.services.ig_lifecycle.dispatch_lifecycle_event", return_value="pending"):
            attempt = HostedSettlementFixture.invoice(self, proposal, "duplicate-sku-winner")
            payload = {"invoiceId": attempt.monobank_invoice_id, "reference": attempt.reference, "ccy": 980,
                "status": "success", "amount": int(attempt.payment_amount * 100),
                "finalAmount": int(attempt.payment_amount * 100), "modifiedDate": clock.isoformat()}
            order, created = _apply_payment_attempt_status(attempt, "success", payload=payload, source="provider_pull")
            self.assertTrue(created)
            items = list(OrderItem.objects.filter(order=order).order_by("pk"))
            self.assertEqual(len(items), 2)
            self.assertEqual([item.qty for item in items], [1, 1])
            self.assertEqual([item.product_id for item in items], [self.products[0].pk] * 2)
            self.assertEqual([item.color_variant_id for item in items], [self.variants[0].pk] * 2)
            self.assertEqual([item.unit_price for item in items], [Decimal("900.00")] * 2)
            provenance = deepcopy(order.payment_payload["source_cart_provenance"])
            self.assertEqual([row["order_item_id"] for row in provenance["item_map"]], [item.pk for item in items])
            self.assertEqual([row["recipient_id"] for row in provenance["item_map"]], ["self", "friend"])
            self.assertEqual(provenance["binding"], original["source_cart_binding"])
            self.assertEqual(order.full_name, "Іван Петренко")
            _apply_payment_attempt_status(attempt, "success", payload={**payload, "finalAmount": 90000,
                "modifiedDate": (clock + timedelta(seconds=10)).isoformat()}, source="provider_pull")
        order.refresh_from_db()
        projection = IgPaymentProjection.objects.get(deal_id=proposal.deal_id)
        self.assertEqual((projection.truth, projection.net_paid_amount), (IgDeal.PaymentTruth.PARTIALLY_REFUNDED, Decimal("900.00")))
        self.assertEqual(order.payment_payload["source_cart_provenance"], provenance)
        self.assertEqual(proposal.revisions.get(revision=1).snapshot, original)
        self.assertEqual(list(OrderItem.objects.filter(order=order).order_by("pk").values_list("pk", flat=True)), [item.pk for item in items])
        self.http.assert_not_called()

    def test_duplicate_sku_positions_preserve_independently_source_confirmed_quantities(self):
        control, reasons = self.duplicate_cart(first_quantity=2)
        self.assertFalse(reasons, reasons)
        self.assertEqual([item["qty"] for item in control["items"]], [2, 1])
        self.assertTrue(all(row["quantity_source_confirmed"] for row in control["source_cart_binding"]["quote_line_map"]))
        proposal = self.quote(control)
        self.assertEqual(list(proposal.items.order_by("position").values_list("quantity", flat=True)), [2, 1])
        self.assertEqual(proposal.quoted_total, Decimal("2700.00"))
