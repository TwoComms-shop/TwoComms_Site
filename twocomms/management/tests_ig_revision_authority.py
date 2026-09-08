from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.test import TestCase, override_settings
from django.utils import timezone

from management.models import (
    IgCheckoutProposal,
    IgClient,
    IgCommercialEpisode,
    IgDeal,
    InstagramBotSettings,
)
from management.services.ig_revision_authority import (
    CLAIM_CANONICAL_URLS,
    CLAIM_CATALOG_CONFIGURATION,
    CLAIM_CURRENT_OFFER,
    CLAIM_ORDER,
    CLAIM_PAYMENT,
    CLAIM_PUBLIC_POLICY_INPUTS,
    CLAIM_SHIPMENT,
    build_revision_authority_bindings,
    check_fact_bindings,
    check_offer_bindings,
)
from management.services.ig_revision_outbox import _safe_bindings
from orders.models import Order


@override_settings(SITE_BASE_URL="https://twocomms.test")
class RevisionAuthorityBindingTests(TestCase):
    def _product(self, suffix="one", *, price=900, status="published"):
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Catalog, Category, Product, SizeGrid

        category = Category.objects.create(
            name=f"Revision authority {suffix}", slug=f"revision-authority-{suffix}"
        )
        catalog = Catalog.objects.create(
            name=f"Revision catalog {suffix}", slug=f"revision-catalog-{suffix}"
        )
        size_grid = SizeGrid.objects.create(
            catalog=catalog,
            name=f"Revision sizes {suffix}",
            guide_data={
                "columns": [{"key": "size", "label": "Size"}],
                "rows": [{"size": "M"}, {"size": "XL"}],
            },
        )
        product = Product.objects.create(
            title=f"Bound product {suffix}",
            slug=f"bound-product-{suffix}",
            category=category,
            catalog=catalog,
            size_grid=size_grid,
            price=price,
            status=status,
        )
        color = Color.objects.create(
            name=f"Bound color {suffix}", primary_hex="#111111"
        )
        variant = ProductColorVariant.objects.create(
            product=product,
            color=color,
            price_override=price,
            is_default=True,
        )
        return product, variant

    def _client_with_configuration(self, suffix="one"):
        product, variant = self._product(suffix)
        client = IgClient.objects.create(
            igsid=f"revision-authority-{suffix}",
            current_product=product,
            sales_context={
                "assisted_checkout_selection": {
                    "product_id": product.pk,
                    "color_variant_id": variant.pk,
                }
            },
        )
        return client, product, variant

    def _order(self, suffix="one", **overrides):
        values = {
            "order_number": f"REV-{suffix}",
            "full_name": "Fixture Customer",
            "phone": "+380000000000",
            "city": "Kyiv",
            "np_office": "1",
            "total_sum": Decimal("900.00"),
            "status": "prep",
        }
        values.update(overrides)
        return Order.objects.create(**values)

    def _episode(self, client, suffix="one", *, order=None):
        deal = IgDeal.objects.create(
            client=client,
            amount=Decimal("900.00"),
            requested_payment_amount=Decimal("900.00"),
            order=order,
        )
        episode = IgCommercialEpisode.objects.create(
            client=client,
            deal=deal,
            intended_order=order,
            sequence=1,
            open_slot=1,
            materialization_key=f"revision-authority:{suffix}:1",
        )
        client.current_commercial_episode = episode
        client.save(update_fields=["current_commercial_episode", "updated_at"])
        return deal, episode

    def _proposal(self, client, deal, episode, product, variant):
        proposal = IgCheckoutProposal.objects.create(
            client=client,
            deal=deal,
            commercial_episode=episode,
            catalog_total=Decimal("900.00"),
            quoted_total=Decimal("900.00"),
            requested_payment_amount=Decimal("900.00"),
            items_digest="a" * 64,
            expires_at=timezone.now() + timedelta(minutes=25),
        )
        proposal.items.create(
            product=product,
            color_variant=variant,
            product_title="Fixture product",
            size="XL",
            fit_code="classic",
            quantity=1,
            catalog_unit_price=Decimal("900.00"),
            catalog_line_total=Decimal("900.00"),
            quoted_unit_price=Decimal("900.00"),
            quoted_line_total=Decimal("900.00"),
            price_source="catalog",
        )
        deal.active_checkout_proposal = proposal
        deal.save(update_fields=["active_checkout_proposal", "updated_at"])
        return proposal

    def _revision(self, client):
        return SimpleNamespace(client_id=client.pk)

    def test_catalog_bindings_fit_outbox_and_contain_only_ids_and_digests(self):
        client, product, variant = self._client_with_configuration("safe")
        result = build_revision_authority_bindings(
            client,
            claims=(CLAIM_CATALOG_CONFIGURATION, CLAIM_CANONICAL_URLS),
            control={
                "product": product.pk,
                "variant": variant.pk,
                "catalog_link": True,
                "show_products": str(product.pk),
                "price": "1",
                "price_quoted": "1",
                "size": "xl",
                "qty": "2",
            },
            server_authorized_actions=("client_configuration_update",),
        )

        self.assertTrue(result.ready, result.reasons)
        self.assertEqual(result.offer_bindings, ())
        self.assertEqual(result.allowed_actions, ("client_configuration_update",))
        self.assertEqual(_safe_bindings(result.fact_bindings), list(result.fact_bindings))
        encoded = str(result.fact_bindings)
        self.assertNotIn("https://", encoded)
        self.assertNotIn("Fixture Customer", encoded)
        self.assertNotIn("price_quoted", encoded)
        configuration = next(
            item
            for item in result.fact_bindings
            if item["claim"] == CLAIM_CATALOG_CONFIGURATION
        )
        self.assertEqual(configuration["selector"]["size"], "XL")
        self.assertEqual(configuration["selector"]["qty"], 2)
        self.assertTrue(
            check_fact_bindings(
                result.fact_bindings,
                revision=self._revision(client),
                client=client,
            )
        )

    def test_catalog_price_and_publication_changes_make_binding_stale(self):
        client, product, variant = self._client_with_configuration("catalog-stale")
        result = build_revision_authority_bindings(
            client,
            claims=(CLAIM_CATALOG_CONFIGURATION, CLAIM_CANONICAL_URLS),
            control={
                "product": product.pk,
                "variant": variant.pk,
                "catalog_link": True,
                "show_products": str(product.pk),
            },
        )
        self.assertTrue(result.ready, result.reasons)

        variant.price_override = 1100
        variant.save(update_fields=["price_override"])
        self.assertFalse(
            check_fact_bindings(
                result.fact_bindings,
                revision=self._revision(client),
                client=client,
            )
        )

        fresh = build_revision_authority_bindings(
            client,
            claims=(CLAIM_CATALOG_CONFIGURATION, CLAIM_CANONICAL_URLS),
            control={
                "product": product.pk,
                "variant": variant.pk,
                "catalog_link": True,
                "show_products": str(product.pk),
            },
        )
        self.assertTrue(fresh.ready, fresh.reasons)
        product.status = "draft"
        product.save(update_fields=["status"])
        self.assertFalse(
            check_fact_bindings(
                fresh.fact_bindings,
                revision=self._revision(client),
                client=client,
            )
        )

    def test_persisted_selection_drift_requires_post_action_rebuild(self):
        client, product, variant = self._client_with_configuration("selection-stale")
        result = build_revision_authority_bindings(
            client,
            claims=(CLAIM_CATALOG_CONFIGURATION,),
            control={
                "product": product.pk,
                "variant": variant.pk,
                "size": "XL",
                "qty": 2,
            },
        )
        self.assertTrue(result.ready, result.reasons)

        client.current_size = "XL"
        client.current_qty = 2
        client.save(update_fields=["current_size", "current_qty", "updated_at"])
        self.assertFalse(
            check_fact_bindings(
                result.fact_bindings,
                revision=self._revision(client),
                client=client,
            )
        )
        rebuilt = build_revision_authority_bindings(
            client,
            claims=(CLAIM_CATALOG_CONFIGURATION,),
            control={
                "product": product.pk,
                "variant": variant.pk,
                "size": "XL",
                "qty": 2,
            },
        )
        self.assertTrue(rebuilt.ready, rebuilt.reasons)
        self.assertNotEqual(result.authority_digest, rebuilt.authority_digest)

    def test_public_policy_binding_anchors_locale_core_knowledge_and_head(self):
        from management.tests_ig_policy_helpers import (
            ensure_test_instruction_publication,
        )

        ensure_test_instruction_publication()
        settings_obj = InstagramBotSettings.load()
        client = IgClient.objects.create(
            igsid="revision-authority-policy", language="de"
        )
        result = build_revision_authority_bindings(
            client,
            claims=(CLAIM_PUBLIC_POLICY_INPUTS,),
            settings_obj=settings_obj,
            server_authorized_actions=("manager_escalation_intent",),
        )
        self.assertTrue(result.ready, result.reasons)
        binding = result.fact_bindings[0]
        self.assertEqual(binding["selector"], {"language": "uk"})
        self.assertRegex(binding["subjects"]["core_prompt_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(binding["subjects"]["knowledge_hash"], r"^[0-9a-f]{64}$")
        self.assertTrue(binding["subjects"]["knowledge_version"])
        self.assertEqual(
            binding["subjects"]["publication_hash"],
            settings_obj.active_instruction_publication.snapshot_hash,
        )
        self.assertEqual(
            result.allowed_actions, ("manager_escalation_intent",)
        )
        self.assertTrue(
            check_fact_bindings(
                result.fact_bindings,
                revision=self._revision(client),
                client=client,
                settings_obj=settings_obj,
            )
        )

        client.language = "ru"
        client.save(update_fields=["language", "updated_at"])
        self.assertFalse(
            check_fact_bindings(
                result.fact_bindings,
                revision=self._revision(client),
                client=client,
                settings_obj=settings_obj,
            )
        )
        result = build_revision_authority_bindings(
            client,
            claims=(CLAIM_PUBLIC_POLICY_INPUTS,),
            settings_obj=settings_obj,
        )
        self.assertTrue(result.ready, result.reasons)

        settings_obj.system_prompt += "\nChanged core."
        settings_obj.save(update_fields=["system_prompt", "updated_at"])
        self.assertFalse(
            check_fact_bindings(
                result.fact_bindings,
                revision=self._revision(client),
                client=client,
                settings_obj=settings_obj,
            )
        )

    def test_url_claim_requires_explicit_catalog_link(self):
        client, product, _variant = self._client_with_configuration("url-gate")
        denied = build_revision_authority_bindings(
            client,
            claims=(CLAIM_CANONICAL_URLS,),
            control={"show_products": str(product.pk)},
        )
        self.assertFalse(denied.ready)
        self.assertEqual(denied.reasons, ("canonical_url_not_requested",))

    def test_model_price_aliases_never_change_catalog_authority(self):
        client, product, variant = self._client_with_configuration("model-price")
        controls = {
            "product": product.pk,
            "variant": variant.pk,
            "price": "1",
            "price_quoted": "2",
        }
        with_model_amounts = build_revision_authority_bindings(
            client, claims=(CLAIM_CATALOG_CONFIGURATION,), control=controls
        )
        without_model_amounts = build_revision_authority_bindings(
            client,
            claims=(CLAIM_CATALOG_CONFIGURATION,),
            control={"product": product.pk, "variant": variant.pk},
        )
        self.assertTrue(with_model_amounts.ready, with_model_amounts.reasons)
        self.assertEqual(
            with_model_amounts.fact_bindings,
            without_model_amounts.fact_bindings,
        )

    def test_configuration_subjects_are_complete_beyond_eight_variants(self):
        from productcolors.models import Color, ProductColorVariant

        client, product, first_variant = self._client_with_configuration(
            "variant-subjects"
        )
        client.sales_context = {}
        client.save(update_fields=["sales_context", "updated_at"])
        expected_ids = {first_variant.pk}
        for index in range(2, 10):
            color = Color.objects.create(
                name=f"Bound variant color {index}",
                primary_hex=f"#{index:06d}",
            )
            expected_ids.add(
                ProductColorVariant.objects.create(
                    product=product,
                    color=color,
                    price_override=900,
                ).pk
            )

        result = build_revision_authority_bindings(
            client,
            claims=(CLAIM_CATALOG_CONFIGURATION,),
            control={"product": product.pk},
        )
        self.assertTrue(result.ready, result.reasons)
        subjects = result.fact_bindings[0]["subjects"]
        self.assertEqual(subjects["variant_count"], 9)
        self.assertEqual(set(subjects["variant_ids"]), expected_ids)

    def test_current_offer_revision_and_client_ownership_are_rechecked(self):
        client, product, variant = self._client_with_configuration("offer")
        deal, episode = self._episode(client, "offer")
        proposal = self._proposal(client, deal, episode, product, variant)
        result = build_revision_authority_bindings(
            client, claims=(CLAIM_CURRENT_OFFER,)
        )
        self.assertTrue(result.ready, result.reasons)
        self.assertTrue(
            check_offer_bindings(
                result.offer_bindings,
                revision=self._revision(client),
                client=client,
            )
        )

        other = IgClient.objects.create(igsid="revision-authority-other")
        self.assertFalse(
            check_offer_bindings(
                result.offer_bindings,
                revision=self._revision(client),
                client=other,
            )
        )

        proposal.revision += 1
        proposal.quoted_total = Decimal("950.00")
        proposal.save(update_fields=["revision", "quoted_total", "updated_at"])
        self.assertFalse(
            check_offer_bindings(
                result.offer_bindings,
                revision=self._revision(client),
                client=client,
            )
        )

    def test_payment_order_and_shipment_changes_make_fact_bindings_stale(self):
        client, _product, _variant = self._client_with_configuration("facts")
        order = self._order("facts")
        deal, _episode = self._episode(client, "facts", order=order)
        result = build_revision_authority_bindings(
            client,
            claims=(CLAIM_PAYMENT, CLAIM_ORDER, CLAIM_SHIPMENT),
        )
        self.assertTrue(result.ready, result.reasons)
        self.assertTrue(
            check_fact_bindings(
                result.fact_bindings,
                revision=self._revision(client),
                client=client,
            )
        )

        deal.status = IgDeal.Status.PAID
        deal.payment_status = "paid"
        deal.paid_at = timezone.now()
        deal.save(
            update_fields=["status", "payment_status", "paid_at", "updated_at"]
        )
        self.assertFalse(
            check_fact_bindings(
                result.fact_bindings,
                revision=self._revision(client),
                client=client,
            )
        )

        result = build_revision_authority_bindings(
            client,
            claims=(CLAIM_PAYMENT, CLAIM_ORDER, CLAIM_SHIPMENT),
        )
        self.assertTrue(result.ready, result.reasons)

        order.status = "ship"
        order.tracking_number = "TRACK-NEW"
        order.save(update_fields=["status", "tracking_number"])
        self.assertFalse(
            check_fact_bindings(
                result.fact_bindings,
                revision=self._revision(client),
                client=client,
            )
        )

    def test_closed_episode_and_more_than_eight_products_fail_closed(self):
        client, _product, _variant = self._client_with_configuration("bounded")
        _deal, episode = self._episode(client, "bounded")
        facts = build_revision_authority_bindings(client, claims=(CLAIM_PAYMENT,))
        self.assertTrue(facts.ready, facts.reasons)
        episode.open_slot = None
        episode.save(update_fields=["open_slot", "updated_at"])
        self.assertFalse(
            check_fact_bindings(
                facts.fact_bindings,
                revision=self._revision(client),
                client=client,
            )
        )

        excessive = build_revision_authority_bindings(
            client,
            claims=(CLAIM_CANONICAL_URLS,),
            control={
                "catalog_link": True,
                "show_products": ",".join(str(value) for value in range(1, 10)),
            },
        )
        self.assertFalse(excessive.ready)
        self.assertEqual(excessive.reasons, ("catalog_selector_invalid",))
