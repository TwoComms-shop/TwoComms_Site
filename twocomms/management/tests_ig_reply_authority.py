from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from management.models import (
    IgCheckoutAccessToken,
    IgCheckoutProposal,
    IgClient,
    IgCommercialEpisode,
    IgDeal,
)
from management.services.ig_reply_authority import (
    EVIDENCE_CODES,
    READINESS_GAP_CODES,
    build_reply_truth_context,
)
from management.services.ig_reply_truth import validate_reply_truth
from orders.models import Order


@override_settings(SITE_BASE_URL="https://twocomms.test")
class ReplyAuthorityContextTests(TestCase):
    def test_source_proved_seller_components_are_attributed_not_catalogue_or_payment_authority(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from management.models import InstagramBotMessage, InstagramBotSettings
        from management.services.ig_conversation_agreement import persist_conversation_agreement
        from management.services.instagram_bot import ingress_provider_namespace
        with patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"}):
            settings_row, _ = InstagramBotSettings.objects.update_or_create(pk=1,
                defaults={"ig_user_id": "reply-agreement-owner", "page_id": "reply-agreement-owner"})
            namespace = ingress_provider_namespace(settings_row)
            client, _, _ = self._current_episode("authority-source-agreement")
            rows = [InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
                role=role, source="echo" if role == "manager" else "webhook", status="done",
                provider_namespace=namespace, text=text, mid=f"authority-agreement-{index}")
                for index, (role, text) in enumerate((
                    ("manager", "Футболка SamplePrint42 біла L oversize, 1 шт."), ("user", "Так"),
                    ("manager", "850 грн + 120 доставка = 970 грн")))]
            self.assertTrue(persist_conversation_agreement(client, rows, watermark=rows[-1].pk)["persisted"])
            client.refresh_from_db()
            with CaptureQueriesContext(connection) as queries:
                context = build_reply_truth_context(client)
            self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
            self.assertEqual([(kind, amount) for kind, amount, *_ in context.conversation_amounts],
                [("merchandise", "850.00"), ("delivery", "120.00"), ("payable", "970.00")])
            self.assertNotIn(Decimal("970"), context.authorized_prices)
            self.assertFalse(context.payment_confirmed)
            self.assertIn(("size", "L", rows[1].pk), context.agreement_choices)
            self.assertIn(("color", "біла", rows[1].pk), context.agreement_choices)
            self.assertTrue(validate_reply_truth("За домовленістю: біла футболка, розмір L, oversize.", context=context).valid)
            self.assertFalse(validate_reply_truth("За домовленістю: біла футболка L є в наявності.", context=context).valid)
            self.assertTrue(validate_reply_truth("За розрахунком менеджера: футболка 850 грн, доставка 120 грн, разом 970 грн.", context=context).valid)
            rows[-1].text = "Інша ціна"
            rows[-1].save(update_fields=["text"])
            self.assertEqual(build_reply_truth_context(client).conversation_amounts, ())
            self.assertEqual(build_reply_truth_context(client).agreement_choices, ())
            rows[-1].text = "850 грн + 120 доставка = 970 грн"
            rows[-1].save(update_fields=["text"])
            InstagramBotMessage.objects.create(client=client, sender_id=client.igsid, role="user",
                source="webhook", status="done", provider_namespace=namespace, text="Пропоную за 700 грн", mid="authority-counteroffer")
            self.assertEqual(build_reply_truth_context(client).conversation_amounts, ())

    def _catalog_product(self, *, suffix, status="published"):
        from storefront.models import Category, Product

        category = Category.objects.create(
            name=f"Catalog URL {suffix}",
            slug=f"catalog-url-{suffix}",
        )
        return Product.objects.create(
            title=f"Catalog URL product {suffix}",
            slug=f"catalog-url-product-{suffix}",
            category=category,
            price=Decimal("900.00"),
            status=status,
        )

    def _order(self, number, **overrides):
        values = {
            "order_number": number,
            "full_name": "Test Customer",
            "phone": "+380000000000",
            "city": "Харків",
            "np_office": "1",
            "total_sum": Decimal("900.00"),
        }
        values.update(overrides)
        return Order.objects.create(**values)

    def _current_episode(self, sender, *, deal=None, order=None):
        client = IgClient.objects.create(igsid=sender)
        deal = deal or IgDeal.objects.create(
            client=client,
            amount=Decimal("900.00"),
            requested_payment_amount=Decimal("900.00"),
        )
        episode = IgCommercialEpisode.objects.create(
            client=client,
            deal=deal,
            intended_order=order,
            sequence=1,
            open_slot=1,
            materialization_key=f"{sender}:episode:1",
        )
        client.current_commercial_episode = episode
        client.save(update_fields=["current_commercial_episode", "updated_at"])
        return client, deal, episode

    def _proposal(self, client, deal, episode, **overrides):
        values = {
            "client": client,
            "deal": deal,
            "commercial_episode": episode,
            "catalog_total": Decimal("900.00"),
            "quoted_total": Decimal("900.00"),
            "requested_payment_amount": Decimal("900.00"),
            "items_digest": "a" * 64,
            "expires_at": timezone.now() + timedelta(minutes=25),
        }
        values.update(overrides)
        proposal = IgCheckoutProposal.objects.create(**values)
        deal.active_checkout_proposal = proposal
        deal.save(update_fields=["active_checkout_proposal", "updated_at"])
        return proposal

    def test_visual_claim_does_not_confirm_payment_but_current_provider_truth_does(self):
        client, deal, _episode = self._current_episode("authority-payment")

        unconfirmed = build_reply_truth_context(
            client,
            control={"image_observation": "receipt", "payment": "900"},
        )
        self.assertFalse(unconfirmed.payment_confirmed)
        self.assertEqual(
            unconfirmed.explicitly_qualified_standard_dispatch_days,
            (1, 3),
        )
        self.assertIn(
            "unverified_payment",
            validate_reply_truth(
                "Оплату підтверджено.", context=unconfirmed
            ).reasons,
        )

        deal.status = IgDeal.Status.PAID
        deal.payment_status = "paid"
        deal.paid_at = timezone.now()
        deal.save(update_fields=["status", "payment_status", "paid_at", "updated_at"])
        confirmed = build_reply_truth_context(client)

        self.assertTrue(confirmed.payment_confirmed)
        self.assertIn("current_episode_provider_payment", confirmed.evidence_codes)

    def test_current_proposal_preserves_each_configuration_fact(self):
        client, deal, episode = self._current_episode("authority-configuration")
        proposal = self._proposal(client, deal, episode)
        proposal.items.create(
            product_title="Synthetic T-shirt", size="XL", fit_code="oversize",
            color_code="black", color_label="Чорний",
            catalog_unit_price=900, catalog_line_total=900,
            quoted_unit_price=900, quoted_line_total=900,
        )
        context = build_reply_truth_context(client)
        self.assertIn("XL", context.allowed_sizes)
        self.assertIn("oversize", context.allowed_fits)
        self.assertIn("black", context.allowed_colors)
        self.assertIn("Чорний", context.allowed_colors)

    def test_historical_paid_order_and_tracking_do_not_authorize_current_episode(self):
        client, current_deal, episode = self._current_episode("authority-orders")
        old_order = self._order(
            "AUTH-OLD",
            status="ship",
            payment_status="paid",
            tracking_number="OLDTRACK123",
        )
        IgDeal.objects.create(
            client=client,
            order=old_order,
            status=IgDeal.Status.ORDER_CREATED,
            payment_status="paid",
            paid_at=timezone.now(),
            amount=Decimal("700.00"),
        )

        before = build_reply_truth_context(client)
        self.assertFalse(before.payment_confirmed)
        self.assertFalse(before.order_created)
        self.assertNotIn("OLDTRACK123", before.known_tracking_refs)

        current_order = self._order(
            "AUTH-CURRENT",
            status="prep",
            tracking_number="CURRENT123",
        )
        episode.intended_order = current_order
        episode.save(update_fields=["intended_order", "updated_at"])
        current_deal.order = current_order
        current_deal.save(update_fields=["order", "updated_at"])
        preparing = build_reply_truth_context(client)

        self.assertTrue(preparing.order_created)
        self.assertEqual(preparing.shipment_state, "preparing")
        self.assertIn("current_order_preparing", preparing.evidence_codes)
        self.assertIn(
            "unverified_shipment",
            validate_reply_truth(
                "Замовлення готове до відправлення.", context=preparing
            ).reasons,
        )

        current_order.status = "ship"
        current_order.save(update_fields=["status"])
        current = build_reply_truth_context(client)

        self.assertTrue(current.order_created)
        self.assertEqual(current.shipment_state, "shipped")
        self.assertEqual(current.known_tracking_refs, ("CURRENT123",))

    def test_only_exact_variant_quote_is_authorized(self):
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product, ProductStatus

        category = Category.objects.create(name="Authority", slug="authority")
        product = Product.objects.create(
            title="Термохромна футболка",
            slug="authority-thermo",
            category=category,
            price=Decimal("1090.00"),
            status=ProductStatus.PUBLISHED,
        )
        color = Color.objects.create(name="Термохром", primary_hex="#222222")
        variant = ProductColorVariant.objects.create(
            product=product,
            color=color,
            price_override=Decimal("1590.00"),
            is_default=True,
        )
        client = IgClient.objects.create(igsid="authority-price-no-episode")
        client.current_product = product
        client.sales_context = {
            "assisted_checkout_selection": {
                "product_id": product.pk,
                "color_variant_id": variant.pk,
            }
        }
        client.save(update_fields=["current_product", "sales_context", "updated_at"])

        exact = build_reply_truth_context(
            client,
            control={
                "product": product.pk,
                "variant": variant.pk,
                "price": "700",
                "price_quoted": "1590",
            },
        )
        invented = build_reply_truth_context(
            client,
            control={
                "product": product.pk,
                "variant": variant.pk,
                "price_quoted": "1090",
            },
        )

        self.assertIn(Decimal("1590.00"), exact.authorized_prices)
        self.assertNotIn(Decimal("700.00"), exact.authorized_prices)
        self.assertIn("current_episode_unavailable", exact.readiness_gaps)
        self.assertIn("exact_catalog_quote", exact.evidence_codes)
        self.assertNotIn(Decimal("1090.00"), invented.authorized_prices)
        self.assertIn("catalog_quote_unverified", invented.readiness_gaps)

    def test_current_configuration_authorizes_exact_thermo_and_product_range(self):
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product, ProductStatus

        category = Category.objects.create(name="Current pricing", slug="current-pricing")
        product = Product.objects.create(
            title="Футболка з конфігураціями",
            slug="current-pricing-shirt",
            category=category,
            price=Decimal("1090.00"),
            status=ProductStatus.PUBLISHED,
        )
        base_color = Color.objects.create(name="Базовий", primary_hex="#FFFFFF")
        thermo_color = Color.objects.create(name="Термохром 2", primary_hex="#111111")
        ProductColorVariant.objects.create(
            product=product,
            color=base_color,
            price_override=Decimal("1090.00"),
            is_default=True,
        )
        thermo = ProductColorVariant.objects.create(
            product=product,
            color=thermo_color,
            price_override=Decimal("1590.00"),
        )
        client = IgClient.objects.create(
            igsid="authority-current-configuration",
            current_product=product,
        )

        ranged = build_reply_truth_context(client)
        self.assertIn(
            (Decimal("1090.00"), Decimal("1590.00")),
            ranged.authorized_price_ranges,
        )
        self.assertTrue(validate_reply_truth(
            "Ціна від 1090 до 1590 грн.", context=ranged
        ).valid)
        self.assertIn(
            "unverified_price",
            validate_reply_truth(
                "Ціна від 900 до 1000 грн.", context=ranged
            ).reasons,
        )

        client.sales_context = {
            "assisted_checkout_selection": {
                "product_id": product.pk,
                "color_variant_id": thermo.pk,
            }
        }
        client.save(update_fields=["sales_context", "updated_at"])
        exact = build_reply_truth_context(client)
        self.assertIn(Decimal("1590.00"), exact.authorized_prices)
        self.assertNotIn(Decimal("1090.00"), exact.authorized_prices)

    def test_unknown_explicit_variant_does_not_fall_back_to_base_price(self):
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product, ProductStatus

        category = Category.objects.create(name="Invalid variant", slug="invalid-variant")
        product = Product.objects.create(
            title="Футболка без fallback",
            slug="invalid-variant-shirt",
            category=category,
            price=Decimal("1090.00"),
            status=ProductStatus.PUBLISHED,
        )
        color = Color.objects.create(name="Єдиний", primary_hex="#333333")
        ProductColorVariant.objects.create(
            product=product,
            color=color,
            price_override=Decimal("1090.00"),
            is_default=True,
        )
        client = IgClient.objects.create(
            igsid="authority-unknown-variant",
            current_product=product,
        )

        context = build_reply_truth_context(
            client,
            control={"product": product.pk, "variant": 999999},
        )

        self.assertNotIn(Decimal("1090.00"), context.authorized_prices)
        self.assertFalse(context.authorized_price_ranges)
        self.assertIn("current_configuration_unverified", context.readiness_gaps)

        variantless = Product.objects.create(
            title="Товар без кольорових варіантів",
            slug="invalid-variantless-product",
            category=category,
            price=Decimal("800.00"),
            status=ProductStatus.PUBLISHED,
        )
        variantless_context = build_reply_truth_context(
            client,
            control={"product_id": variantless.pk, "variant": 999999},
        )
        self.assertNotIn(Decimal("800.00"), variantless_context.authorized_prices)
        self.assertIn(
            "current_configuration_unverified",
            variantless_context.readiness_gaps,
        )

    @patch("management.services.instagram_bot._validated_price_quote")
    def test_negotiated_price_alias_cannot_enter_catalog_authority(
        self, validate_price
    ):
        client = IgClient.objects.create(igsid="authority-negotiated-alias")
        validate_price.return_value = {
            "amount": "700.00",
            "product_id": 1,
            "price_source": "conversation_evidence",
        }

        context = build_reply_truth_context(
            client,
            control={
                "product": 1,
                "price": "700",
                "price_quoted": "700",
            },
        )

        self.assertNotIn(Decimal("700.00"), context.authorized_prices)
        self.assertIn("catalog_quote_unverified", context.readiness_gaps)
        sent_candidate = validate_price.call_args.args[1]
        self.assertNotIn("price", sent_candidate)
        self.assertEqual(sent_candidate["price_quoted"], "700")

    def test_checkout_url_must_have_owned_origin_and_current_proposal_token(self):
        client, deal, episode = self._current_episode("authority-url")
        proposal = self._proposal(client, deal, episode)
        raw_token, _token = IgCheckoutAccessToken.issue(proposal=proposal)
        current_url = f"https://twocomms.test/offer/a/{raw_token}/"

        other_client, other_deal, other_episode = self._current_episode(
            "authority-url-other"
        )
        other_proposal = self._proposal(other_client, other_deal, other_episode)
        other_raw, _other_token = IgCheckoutAccessToken.issue(proposal=other_proposal)
        other_url = f"https://twocomms.test/offer/a/{other_raw}/"
        foreign_url = "https://example.invalid/catalog/"

        context = build_reply_truth_context(
            client,
            server_urls=(current_url, other_url, foreign_url),
        )

        self.assertEqual(context.authorized_urls, (current_url,))
        self.assertIn("checkout_url_not_current", context.readiness_gaps)
        self.assertIn("server_url_not_owned", context.readiness_gaps)
        self.assertLessEqual(set(context.readiness_gaps), READINESS_GAP_CODES)
        self.assertLessEqual(set(context.evidence_codes), EVIDENCE_CODES)

    def test_catalog_link_authorizes_exact_published_product_url(self):
        product = self._catalog_product(suffix="published")
        client = IgClient.objects.create(igsid="authority-catalog-published")
        url = f"https://twocomms.test/product/{product.slug}/"

        context = build_reply_truth_context(
            client,
            control={"catalog_link": True, "show_products": str(product.pk)},
        )

        self.assertIn(url, context.authorized_urls)
        self.assertTrue(validate_reply_truth(
            f"Ось сторінка товару: {url}", context=context
        ).valid)

    def test_catalog_link_never_authorizes_foreign_or_arbitrary_model_url(self):
        product = self._catalog_product(suffix="foreign")
        client = IgClient.objects.create(igsid="authority-catalog-foreign")
        context = build_reply_truth_context(
            client,
            control={"catalog_link": True, "show_products": str(product.pk)},
        )

        for url in (
            "https://evil.example/product/fake/",
            "https://twocomms.test/product/not-the-db-slug/",
        ):
            with self.subTest(url=url):
                result = validate_reply_truth(f"Ось товар: {url}", context=context)
                self.assertIn("unauthorized_url", result.reasons)

    def test_catalog_link_does_not_authorize_unpublished_product(self):
        from storefront.models import ProductStatus

        product = self._catalog_product(
            suffix="unpublished",
            status=ProductStatus.DRAFT,
        )
        client = IgClient.objects.create(igsid="authority-catalog-unpublished")
        url = f"https://twocomms.test/product/{product.slug}/"

        context = build_reply_truth_context(
            client,
            control={"catalog_link": True, "show_products": str(product.pk)},
        )

        self.assertNotIn(url, context.authorized_urls)
        self.assertIn(
            "unauthorized_url",
            validate_reply_truth(f"Ось товар: {url}", context=context).reasons,
        )

    def test_product_control_without_catalog_link_does_not_authorize_url(self):
        product = self._catalog_product(suffix="missing-control")
        client = IgClient.objects.create(igsid="authority-catalog-no-link")
        url = f"https://twocomms.test/product/{product.slug}/"

        context = build_reply_truth_context(
            client,
            control={"show_products": str(product.pk)},
        )

        self.assertNotIn(url, context.authorized_urls)
        self.assertIn(
            "unauthorized_url",
            validate_reply_truth(f"Ось товар: {url}", context=context).reasons,
        )
