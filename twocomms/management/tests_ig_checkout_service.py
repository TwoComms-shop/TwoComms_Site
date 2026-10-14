from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from product_catalog.models import ProductInventoryPolicy
from management.models import (
    IgCheckoutInventoryReservation,
    IgCheckoutProposal,
    IgClient,
    IgDeal,
    IgDealItem,
    IgFollowUpTask,
    InstagramBotMessage,
)
from orders.models import Order, PaymentAttempt
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product, ProductFitOption, ProductStatus
from management.services.ig_checkout import CheckoutConfigurationError


class InstagramCheckoutConfigurationTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Футболки", slug="ig-checkout-shirts")
        self.shirt = Product.objects.create(
            title="Футболка Київ",
            slug="ig-checkout-shirt",
            category=self.category,
            price=950,
            status=ProductStatus.PUBLISHED,
        )
        self.classic = ProductFitOption.objects.create(
            product=self.shirt,
            code="classic",
            label="Класичний",
            is_active=True,
        )
        self.oversize = ProductFitOption.objects.create(
            product=self.shirt,
            code="oversize",
            label="Оверсайз",
            is_active=True,
        )
        blue = Color.objects.create(name="Синій", primary_hex="#2255AA")
        black = Color.objects.create(name="Чорний", primary_hex="#111111")
        self.blue = ProductColorVariant.objects.create(
            product=self.shirt,
            color=blue,
            stock=5,
            sku="TEE-BLUE",
        )
        self.black = ProductColorVariant.objects.create(
            product=self.shirt,
            color=black,
            stock=5,
            sku="TEE-BLACK",
        )
        ProductInventoryPolicy.objects.create(
            product=self.shirt,
            source=ProductInventoryPolicy.Source.CATALOG_VARIANT,
        )
        self.client = IgClient.get_or_create_for_sender("ig-checkout-service")

    def _valid_item(self, **overrides):
        item = {
            "product_id": self.shirt.pk,
            "color_variant_id": self.blue.pk,
            "qty": 1,
            "size": "M",
            "fit_option_code": "classic",
        }
        item.update(overrides)
        return item

    def test_tshirt_requires_fit_size_and_color(self):
        from management.services.ig_checkout import (
            CheckoutConfigurationError,
            validate_checkout_items,
        )

        with self.assertRaises(CheckoutConfigurationError) as ctx:
            validate_checkout_items(
                client=self.client,
                item_specs=[{"product_id": self.shirt.pk, "qty": 1}],
                evidence={},
            )
        self.assertEqual(ctx.exception.missing_fields, {"size", "fit", "color"})

    def test_generic_option_axis_is_required_before_pricing(self):
        from product_catalog.models import GarmentFlow, GarmentFlowCategory
        from management.services.ig_checkout import (
            CheckoutConfigurationError,
            validate_checkout_items,
        )

        flow = GarmentFlow.objects.create(
            code="ig-checkout-material-axis",
            name="Material axis",
            axes=[{
                "code": "material",
                "label": "Матеріал",
                "options": [
                    {"code": "cotton", "label": "Бавовна", "default": True},
                    {"code": "thermo", "label": "Термохром"},
                ],
            }],
        )
        GarmentFlowCategory.objects.create(flow=flow, category=self.category)

        with self.assertRaises(CheckoutConfigurationError) as ctx:
            validate_checkout_items(
                client=self.client,
                item_specs=[self._valid_item()],
                evidence={},
            )

        self.assertEqual(ctx.exception.code, "missing_configuration")
        self.assertIn("option:material", ctx.exception.missing_fields)

    def test_selected_generic_option_reaches_authoritative_unit_price(self):
        from product_catalog.models import GarmentFlow, GarmentFlowCategory, ProductOptionProfile
        from management.services.ig_checkout import validate_checkout_items

        flow = GarmentFlow.objects.create(
            code="ig-checkout-material-price",
            name="Material price axis",
            axes=[{
                "code": "material",
                "label": "Матеріал",
                "options": [
                    {"code": "cotton", "label": "Бавовна", "default": True},
                    {"code": "thermo", "label": "Термохром"},
                ],
            }],
        )
        GarmentFlowCategory.objects.create(flow=flow, category=self.category)
        ProductOptionProfile.objects.create(
            product=self.shirt,
            option_key="material=thermo",
            option_values={"material": "thermo"},
            price_delta=360,
        )

        quote = validate_checkout_items(
            client=self.client,
            item_specs=[self._valid_item(option_values={"material": "thermo"})],
            evidence={},
        )

        self.assertEqual(quote.items[0].option_values["material"], "thermo")
        self.assertEqual(quote.items[0].option_labels["material"], "Термохром")
        self.assertEqual(quote.items[0].catalog_unit_price, Decimal("1310.00"))

    @patch("product_catalog.services.product_option_context", side_effect=RuntimeError("catalog unavailable"))
    def test_option_context_failure_never_prices_base_configuration(self, _context):
        from management.services.ig_checkout import (
            CheckoutConfigurationError,
            validate_checkout_items,
        )

        with self.assertRaises(CheckoutConfigurationError) as ctx:
            validate_checkout_items(
                client=self.client,
                item_specs=[self._valid_item()],
                evidence={},
            )

        self.assertEqual(ctx.exception.code, "configuration_unavailable")

    def test_generic_option_surcharge_applies_without_a_color_variant(self):
        from product_catalog.models import GarmentFlow, GarmentFlowCategory, ProductOptionProfile
        from management.services.ig_checkout import validate_checkout_items

        plain = Product.objects.create(
            title="Базова річ без кольорових variants",
            slug="ig-checkout-no-color-variant",
            category=self.category,
            price=1090,
            status=ProductStatus.PUBLISHED,
        )
        flow = GarmentFlow.objects.create(
            code="ig-checkout-no-variant-material",
            name="No variant material",
            axes=[{
                "code": "material",
                "label": "Матеріал",
                "options": [
                    {"code": "cotton", "label": "Бавовна", "default": True},
                    {"code": "thermo", "label": "Термохром"},
                ],
            }],
        )
        GarmentFlowCategory.objects.create(flow=flow, category=self.category)
        ProductOptionProfile.objects.create(
            product=plain,
            option_key="material=thermo",
            option_values={"material": "thermo"},
            price_delta=360,
        )

        quote = validate_checkout_items(
            client=self.client,
            item_specs=[{
                "product_id": plain.pk,
                "qty": 1,
                "size": "M",
                "option_values": {"material": "thermo"},
            }],
            evidence={},
        )

        self.assertIsNone(quote.items[0].color_variant)
        self.assertEqual(quote.items[0].catalog_unit_price, Decimal("1450.00"))

    def test_unknown_generic_option_cannot_fall_back_to_base_price_without_variant(self):
        from product_catalog.models import GarmentFlow, GarmentFlowCategory
        from management.services.ig_checkout import CheckoutConfigurationError, validate_checkout_items

        plain = Product.objects.create(
            title="Річ без variant з контрольованими опціями",
            slug="ig-checkout-unknown-option",
            category=self.category,
            price=1090,
            status=ProductStatus.PUBLISHED,
        )
        flow = GarmentFlow.objects.create(
            code="ig-checkout-unknown-option-flow",
            name="Unknown option flow",
            axes=[{
                "code": "material",
                "label": "Матеріал",
                "options": [{"code": "cotton", "label": "Бавовна", "default": True}],
            }],
        )
        GarmentFlowCategory.objects.create(flow=flow, category=self.category)

        with self.assertRaises(CheckoutConfigurationError) as ctx:
            validate_checkout_items(
                client=self.client,
                item_specs=[{
                    "product_id": plain.pk,
                    "qty": 1,
                    "size": "M",
                    "option_values": {"material": "thermo"},
                }],
                evidence={},
            )

        self.assertEqual(ctx.exception.code, "invalid_options")

    def test_single_sellable_color_variant_is_selected_without_prompt(self):
        """Один продаваний варіант обирається без питання клієнту.

        Критерій продаваності — правила вітрини (`variant_allows_purchase`), а не
        числовий `stock`. На проді `stock > 0` лише в 1 варіанта з 81, тоді як
        сайт продає всі опубліковані товари: речі відшиваються під замовлення.
        Раніше цей тест вимикав варіант через `stock = 0`, тобто закріплював
        трактування нуля як «немає» — саме воно давало клієнту «Выбранный
        вариант сейчас недоступен» на кожен товар.
        """
        from product_catalog.models import VariantSizeRule
        from management.services.ig_checkout import validate_checkout_items

        VariantSizeRule.objects.create(
            variant=self.black,
            fit_code="classic",
            size="M",
            is_enabled=False,
        )
        quote = validate_checkout_items(
            client=self.client,
            item_specs=[{
                "product_id": self.shirt.pk,
                "qty": 1,
                "size": "M",
                "fit_option_code": "classic",
            }],
            evidence={},
        )

        self.assertEqual(quote.items[0].color_variant.pk, self.blue.pk)

    def test_single_price_adjusted_variant_uses_authoritative_variant_price(self):
        """The automatically selected PDP variant keeps its exact catalog price."""
        from management.services.ig_checkout import validate_checkout_items

        self.black.delete()
        self.blue.price_override = 1450
        self.blue.save(update_fields=["price_override"])

        quote = validate_checkout_items(
            client=self.client,
            item_specs=[{
                "product_id": self.shirt.pk,
                "qty": 1,
                "size": "M",
                "fit_option_code": "classic",
            }],
            evidence={},
        )

        self.assertEqual(quote.items[0].color_variant, self.blue)
        self.assertEqual(quote.items[0].catalog_unit_price, Decimal("1450.00"))

    def test_zero_stock_variant_stays_sellable_like_on_the_website(self):
        """Нульовий `stock` не робить варіант недоступним.

        Це паритет із вітриною: ні кошик storefront, ні `variant_allows_purchase`
        це поле не читають, а каталог бота прямо пише «під замовлення». Єдиним
        місцем, де нуль означав заборону, був IG-чекаут.
        """
        from management.services.ig_checkout import validate_checkout_items

        ProductColorVariant.objects.filter(product=self.shirt).update(stock=0)
        quote = validate_checkout_items(
            client=self.client,
            item_specs=[self._valid_item()],
            evidence={},
        )

        self.assertEqual(quote.items[0].color_variant.pk, self.blue.pk)

    def test_no_sellable_color_variant_fails_closed_without_prompting(self):
        """Якщо жоден варіант не продається за правилами — відмова, не вгадування."""
        from product_catalog.models import VariantSizeRule
        from management.services.ig_checkout import (
            CheckoutConfigurationError,
            validate_checkout_items,
        )

        for variant in ProductColorVariant.objects.filter(product=self.shirt):
            VariantSizeRule.objects.create(
                variant=variant,
                fit_code="classic",
                size="M",
                is_enabled=False,
            )
        with self.assertRaises(CheckoutConfigurationError) as ctx:
            validate_checkout_items(
                client=self.client,
                item_specs=[{
                    "product_id": self.shirt.pk,
                    "qty": 1,
                    "size": "M",
                    "fit_option_code": "classic",
                }],
                evidence={},
            )

        self.assertEqual(ctx.exception.code, "unavailable_selection")

    def test_fit_specific_grid_accepts_size_outside_generic_grid(self):
        from management.services.ig_checkout import validate_checkout_items

        with patch(
            "storefront.services.size_guides.resolve_product_sizes",
            return_value=["S", "M", "L"],
        ), patch(
            "product_catalog.size_grid_services.resolve_option_size_grid",
            return_value=object(),
        ), patch(
            "product_catalog.size_grid_services.resolve_effective_sizes",
            return_value=[{"size": "XS", "is_enabled": True}],
        ), patch(
            "product_catalog.services.variant_allows_purchase",
            return_value=True,
        ):
            quote = validate_checkout_items(
                client=self.client,
                item_specs=[self._valid_item(
                    size="XS",
                    fit_option_code="oversize",
                )],
                evidence={},
            )

        self.assertEqual(quote.items[0].size, "XS")

    def test_wrong_variant_inactive_fit_and_invalid_size_fail_closed(self):
        from management.services.ig_checkout import (
            CheckoutConfigurationError,
            validate_checkout_items,
        )

        other = Product.objects.create(
            title="Інша футболка",
            slug="ig-checkout-other-shirt",
            category=self.category,
            price=790,
            status=ProductStatus.PUBLISHED,
        )
        other_color = Color.objects.create(name="Білий", primary_hex="#FFFFFF")
        wrong_variant = ProductColorVariant.objects.create(
            product=other,
            color=other_color,
            stock=2,
        )
        self.oversize.is_active = False
        self.oversize.save(update_fields=["is_active"])

        cases = (
            (self._valid_item(color_variant_id=wrong_variant.pk), "invalid_color"),
            (self._valid_item(fit_option_code="oversize"), "invalid_fit"),
            (self._valid_item(size="NOT-A-SIZE"), "invalid_size"),
        )
        for item, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(CheckoutConfigurationError) as ctx:
                    validate_checkout_items(
                        client=self.client,
                        item_specs=[item],
                        evidence={},
                    )
                self.assertEqual(ctx.exception.code, code)

    def test_unpublished_and_insufficient_stock_fail_closed(self):
        from management.services.ig_checkout import (
            CheckoutConfigurationError,
            validate_checkout_items,
        )

        self.shirt.status = ProductStatus.DRAFT
        self.shirt.save(update_fields=["status"])
        with self.assertRaises(CheckoutConfigurationError) as unpublished:
            validate_checkout_items(
                client=self.client,
                item_specs=[self._valid_item()],
                evidence={},
            )
        self.assertEqual(unpublished.exception.code, "unpublished_product")

        self.shirt.status = ProductStatus.PUBLISHED
        self.shirt.save(update_fields=["status"])
        with self.assertRaises(CheckoutConfigurationError) as stock:
            validate_checkout_items(
                client=self.client,
                item_specs=[self._valid_item(qty=6)],
                evidence={},
            )
        self.assertEqual(stock.exception.code, "insufficient_stock")

    def test_multi_item_quote_preserves_fit_size_color_and_exact_total(self):
        from management.services.ig_checkout import create_or_update_proposal

        proposal = create_or_update_proposal(
            client=self.client,
            pay_type="online_full",
            item_specs=[
                self._valid_item(color_variant_id=self.blue.pk, size="M", fit_option_code="classic"),
                self._valid_item(color_variant_id=self.black.pk, size="L", fit_option_code="oversize"),
            ],
        )

        self.assertEqual(proposal.items.count(), 2)
        self.assertEqual(proposal.catalog_total, Decimal("1900.00"))
        self.assertEqual(proposal.quoted_total, Decimal("1900.00"))
        self.assertEqual(
            list(
                proposal.items.order_by("position").values_list(
                    "color_label", "fit_code", "size", "quantity"
                )
            ),
            [
                ("Синій", "classic", "M", 1),
                ("Чорний", "oversize", "L", 1),
            ],
        )

    def test_inventory_reservation_aggregates_same_variant_across_sizes(self):
        from management.services.ig_checkout import CheckoutConfigurationError, create_or_update_proposal
        from management.services.ig_inventory import reserve_proposal_inventory

        with self.assertRaisesMessage(CheckoutConfigurationError, "insufficient_stock"):
            create_or_update_proposal(
                client=self.client,
                pay_type="online_full",
                item_specs=[
                    self._valid_item(qty=3, size="M", fit_option_code="classic"),
                    self._valid_item(qty=3, size="L", fit_option_code="classic"),
                ],
            )

    def test_consumed_reservation_decrements_variant_stock_once(self):
        from management.services.ig_checkout import create_or_update_proposal
        from management.services.ig_inventory import (
            consume_proposal_inventory,
            reserve_proposal_inventory,
        )

        proposal = create_or_update_proposal(
            client=self.client,
            pay_type="online_full",
            item_specs=[self._valid_item(qty=2)],
        )
        reserve_proposal_inventory(proposal)

        self.assertEqual(consume_proposal_inventory(proposal), 1)
        self.assertEqual(consume_proposal_inventory(proposal), 0)

        self.blue.refresh_from_db()
        self.assertEqual(self.blue.stock, 3)

    def test_stale_terminal_attempt_cannot_release_converted_order_inventory(self):
        from management.services.ig_checkout import create_or_update_proposal
        from management.services.ig_inventory import (
            release_attempt_inventory,
            reserve_proposal_inventory,
        )

        proposal = create_or_update_proposal(
            client=self.client,
            pay_type="online_full",
            item_specs=[self._valid_item()],
        )
        reserve_proposal_inventory(proposal)
        attempt = PaymentAttempt.objects.create(
            fingerprint="stale-terminal-inventory-release",
            full_name="Instagram Buyer",
            phone="+380501112233",
            city="Київ",
            np_office="Відділення 1",
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.FAILED,
            cart_snapshot={"checkout_surface": "instagram_proposal", "cart": []},
            gross_amount=Decimal("950.00"),
            payable_amount=Decimal("950.00"),
            payment_amount=Decimal("950.00"),
        )
        proposal.payment_attempt = attempt
        proposal.status = IgCheckoutProposal.Status.DETAILS_LOCKED
        proposal.details_locked_at = timezone.now()
        proposal.save(update_fields=[
            "payment_attempt",
            "status",
            "details_locked_at",
            "updated_at",
        ])
        stale_attempt = PaymentAttempt.objects.get(pk=attempt.pk)
        order = Order.objects.create(
            full_name=attempt.full_name,
            phone=attempt.phone,
            city=attempt.city,
            np_office=attempt.np_office,
            pay_type=attempt.pay_type,
            payment_status="paid",
            total_sum=attempt.gross_amount,
        )
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            status=PaymentAttempt.Status.CONVERTED,
            order=order,
        )

        self.assertEqual(release_attempt_inventory(stale_attempt), 0)
        reservation = IgCheckoutInventoryReservation.objects.get(proposal=proposal)
        self.assertEqual(reservation.state, IgCheckoutInventoryReservation.State.ACTIVE)

    def test_negotiated_total_is_order_discount_not_invented_line_price(self):
        from management.services.ig_checkout import create_or_update_proposal

        offer = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role="manager",
            text="За дві футболки разом 1700 грн",
        )
        accepted = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role="user",
            text="Так, оформлюйте",
        )
        proposal = create_or_update_proposal(
            client=self.client,
            pay_type="online_full",
            item_specs=[
                self._valid_item(color_variant_id=self.blue.pk, size="M"),
                self._valid_item(color_variant_id=self.black.pk, size="L", fit_option_code="oversize"),
            ],
            negotiated_total=Decimal("1700.00"),
            evidence={"message_ids": [offer.pk, accepted.pk]},
        )

        self.assertEqual(proposal.catalog_total, Decimal("1900.00"))
        self.assertEqual(proposal.negotiated_discount, Decimal("200.00"))
        self.assertEqual(proposal.quoted_total, Decimal("1700.00"))
        self.assertEqual(
            sum(item.quoted_line_total for item in proposal.items.all()),
            Decimal("1900.00"),
        )
        proposal.deal.refresh_from_db()
        self.assertEqual(proposal.deal.amount, Decimal("1700.00"))

    def test_identical_replay_reuses_proposal_and_revision(self):
        from management.services.ig_checkout import create_or_update_proposal

        kwargs = {
            "client": self.client,
            "pay_type": "online_full",
            "item_specs": [self._valid_item()],
        }
        first = create_or_update_proposal(**kwargs)
        second = create_or_update_proposal(**kwargs)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.revision, 1)
        self.assertEqual(second.revisions.count(), 1)
        self.assertEqual(IgDeal.objects.filter(client=self.client).count(), 1)

    def test_proposal_creation_registers_one_revision_expiry_event(self):
        from management.services.ig_checkout import create_or_update_proposal

        kwargs = {
            "client": self.client,
            "pay_type": "online_full",
            "item_specs": [self._valid_item()],
        }
        first = create_or_update_proposal(**kwargs)
        second = create_or_update_proposal(**kwargs)

        event_key = f"proposal_expired:{first.deal_id}:{first.pk}:{first.revision}"
        events = IgFollowUpTask.objects.filter(event_key=event_key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(event.trigger, IgFollowUpTask.Trigger.EVENT)
        self.assertEqual(event.event_payload["event"], "proposal_expired")
        self.assertEqual(event.event_payload["proposal_id"], str(first.pk))
        self.assertEqual(event.event_payload["revision"], first.revision)
        self.assertEqual(event.event_occurred_at, first.expires_at)

    def test_identical_replay_after_expiry_issues_fresh_proposal(self):
        from management.services.ig_checkout import create_or_update_proposal

        kwargs = {
            "client": self.client,
            "pay_type": "online_full",
            "item_specs": [self._valid_item()],
        }
        first = create_or_update_proposal(**kwargs)
        first.expires_at = timezone.now() - timedelta(seconds=1)
        first.save(update_fields=["expires_at", "updated_at"])

        second = create_or_update_proposal(**kwargs)

        first.refresh_from_db()
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.status, IgCheckoutProposal.Status.EXPIRED)
        self.assertEqual(second.revision, 1)
        self.assertEqual(second.deal.active_checkout_proposal_id, second.pk)

    def test_allow_promo_policy_is_part_of_proposal_digest(self):
        from management.services.ig_checkout import create_or_update_proposal

        base = {
            "client": self.client,
            "pay_type": "online_full",
            "item_specs": [self._valid_item()],
        }
        first = create_or_update_proposal(**base, allow_promo=False)
        second = create_or_update_proposal(**base, allow_promo=True)

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.revision, 2)
        self.assertNotEqual(second.items_digest, first.items_digest)

    def test_ai_model_message_cannot_authorize_negotiated_price(self):
        from management.services.ig_checkout import (
            CheckoutConfigurationError,
            create_or_update_proposal,
        )

        offer = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role="model",
            text="За футболку 800 грн",
        )
        accepted = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role="user",
            text="Так, оформлюйте",
        )
        with self.assertRaises(CheckoutConfigurationError) as ctx:
            create_or_update_proposal(
                client=self.client,
                pay_type="online_full",
                item_specs=[self._valid_item()],
                negotiated_total=Decimal("800.00"),
                evidence={"message_ids": [offer.pk, accepted.pk]},
            )
        self.assertEqual(ctx.exception.code, "invalid_price_evidence")

    def test_revision_updates_proposal_deal_items_and_episode_atomically(self):
        from management.services.ig_checkout import create_or_update_proposal

        first = create_or_update_proposal(
            client=self.client,
            pay_type="online_full",
            item_specs=[self._valid_item()],
        )
        second = create_or_update_proposal(
            client=self.client,
            pay_type="online_full",
            item_specs=[self._valid_item(color_variant_id=self.black.pk, size="L")],
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.revision, 2)
        self.assertEqual(second.revisions.count(), 2)
        self.assertEqual(second.items.get().color_variant_id, self.black.pk)
        self.assertEqual(second.deal.items.get().color_variant_id, self.black.pk)
        second.commercial_episode.refresh_from_db()
        self.assertEqual(
            second.commercial_episode.product_snapshot[0]["color_variant_id"],
            self.black.pk,
        )
        self.assertEqual(
            second.commercial_episode.price_snapshot["negotiated_total"],
            "950.00",
        )

    def test_revision_failure_rolls_back_deal_and_proposal_together(self):
        from management.models import IgCheckoutRevision
        from management.services.ig_checkout import create_or_update_proposal

        proposal = create_or_update_proposal(
            client=self.client,
            pay_type="online_full",
            item_specs=[self._valid_item()],
        )
        with patch.object(
            IgCheckoutRevision.objects,
            "create",
            side_effect=RuntimeError("revision write failed"),
        ):
            with self.assertRaisesMessage(RuntimeError, "revision write failed"):
                create_or_update_proposal(
                    client=self.client,
                    pay_type="online_full",
                    item_specs=[self._valid_item(color_variant_id=self.black.pk, size="L")],
                )

        proposal.refresh_from_db()
        self.assertEqual(proposal.revision, 1)
        self.assertEqual(proposal.items.get().color_variant_id, self.blue.pk)
        self.assertEqual(IgDealItem.objects.get(deal=proposal.deal).color_variant_id, self.blue.pk)

    def test_invoice_created_proposal_cannot_be_mutated(self):
        from management.services.ig_checkout import (
            CheckoutConfigurationError,
            create_or_update_proposal,
        )

        proposal = create_or_update_proposal(
            client=self.client,
            pay_type="online_full",
            item_specs=[self._valid_item()],
        )
        proposal.status = IgCheckoutProposal.Status.INVOICE_CREATED
        proposal.save(update_fields=["status", "updated_at"])

        with self.assertRaises(CheckoutConfigurationError) as ctx:
            create_or_update_proposal(
                client=self.client,
                pay_type="online_full",
                item_specs=[self._valid_item(color_variant_id=self.black.pk)],
            )
        self.assertEqual(ctx.exception.code, "proposal_locked")


class InstagramCheckoutLinkBoundaryTests(TestCase):
    def setUp(self):
        from storefront.models import Category, ProductStatus

        category = Category.objects.create(name="IG offer", slug="ig-offer-link")
        self.product = Product.objects.create(
            title="Худі для offer",
            slug="ig-offer-hoodie",
            category=category,
            price=Decimal("950.00"),
            status=ProductStatus.PUBLISHED,
        )
        ProductInventoryPolicy.objects.create(
            product=self.product,
            source=ProductInventoryPolicy.Source.UNTRACKED,
        )
        self.client = IgClient.get_or_create_for_sender("ig-offer-link")

    @patch("storefront.views.monobank._monobank_api_request")
    def test_bot_deal_path_returns_first_party_offer_without_monobank_call(self, provider):
        from management.services import bot_orders

        result = bot_orders.create_deal_and_link(
            self.client,
            pay_type="full",
            product_id=self.product.pk,
            size="M",
        )

        self.assertTrue(result["ok"], result)
        self.assertIn("/offer/a/", result["invoice_url"])
        self.assertEqual(result["invoice_url"], result["proposal_url"])
        provider.assert_not_called()
        deal = IgDeal.objects.get(client=self.client)
        self.assertEqual(deal.invoice_id, "")
        self.assertEqual(deal.invoice_url, "")
        self.assertIsNotNone(deal.active_checkout_proposal_id)
        proposal = deal.active_checkout_proposal
        self.assertEqual(proposal.status, IgCheckoutProposal.Status.READY)
        self.assertEqual(proposal.items.count(), 1)
        self.client.refresh_from_db()
        self.assertEqual(self.client.stage, IgClient.Stage.CHECKOUT)

    @patch("orders.telegram_notifications.TelegramNotifier.send_payment_attempt_notification")
    @patch("management.services.instagram_bot.notify_manager")
    @patch(
        "storefront.views.monobank._monobank_api_request",
        return_value={
            "invoiceId": "PRIVATE_PROVIDER_INVOICE_MARKER",
            "pageUrl": "https://pay.monobank.test/private-provider-marker",
        },
    )
    def test_invoice_creation_uses_minimum_necessary_ig_operator_alert(
        self, provider, notify_manager, legacy_payment_alert
    ):
        from types import SimpleNamespace

        from django.contrib.auth.models import AnonymousUser
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from management.services import bot_orders
        from management.services.ig_checkout_payment import create_or_reuse_invoice

        result = bot_orders.create_deal_and_link(
            self.client,
            pay_type="full",
            product_id=self.product.pk,
            size="M",
        )
        self.assertTrue(result["ok"], result)
        proposal = IgCheckoutProposal.objects.get(client=self.client)
        request = RequestFactory().post("/offer/payment/", secure=True)
        SessionMiddleware(lambda req: None).process_request(request)
        request.user = AnonymousUser()
        delivery = SimpleNamespace(
            city="PRIVATE_CITY_MARKER",
            np_office="PRIVATE_OFFICE_MARKER",
            settlement_ref="settlement-ref",
            city_ref="city-ref",
            warehouse_ref="warehouse-ref",
            warehouse_kind="branch",
        )
        payload = {
            "full_name": "Private Customer Marker",
            "phone": "+380501234567",
            "email": "private-marker@example.test",
        }
        shown_revision = proposal.revisions.get(revision=proposal.revision)
        payload.update(expected_revision=shown_revision.revision, expected_digest=shown_revision.digest)
        with (
            patch(
                "management.services.ig_checkout_payment.resolve_delivery_selection",
                return_value=delivery,
            ),
            patch(
                "management.services.ig_checkout_payment._send_add_payment_info_if_missing",
                return_value=True,
            ),
        ):
            attempt, invoice_url, reused = create_or_reuse_invoice(
                proposal,
                request=request,
                payload=payload,
            )

        self.assertFalse(reused)
        self.assertEqual(invoice_url, "https://pay.monobank.test/private-provider-marker")
        provider.assert_called_once()
        legacy_payment_alert.assert_not_called()
        notify_manager.assert_called_once()
        self.assertFalse(notify_manager.call_args.kwargs["deliver_immediately"])
        alert = notify_manager.call_args.args[0]
        for private_marker in (
            payload["full_name"],
            payload["phone"],
            payload["email"],
            delivery.city,
            delivery.np_office,
            self.product.title,
            "PRIVATE_PROVIDER_INVOICE_MARKER",
            invoice_url,
        ):
            self.assertNotIn(private_marker, alert)
        for local_id in (self.client.pk, proposal.deal_id, proposal.pk, attempt.pk):
            self.assertIn(str(local_id), alert)
        self.assertIn("950.00", alert)

    @patch("management.services.instagram_bot.notify_manager", return_value=False)
    @patch(
        "storefront.views.monobank._monobank_api_request",
        return_value={
            "invoiceId": "ALERT_OUTBOX_FAILURE_INVOICE",
            "pageUrl": "https://pay.monobank.test/alert-outbox-failure",
        },
    )
    def test_invoice_creation_keeps_provider_invoice_when_operator_alert_outbox_fails(
        self, provider, notify_manager
    ):
        from types import SimpleNamespace

        from django.contrib.auth.models import AnonymousUser
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from management.services import bot_orders
        from management.services.ig_checkout_payment import create_or_reuse_invoice
        from orders.models import PaymentSideEffectJob

        result = bot_orders.create_deal_and_link(
            self.client,
            pay_type="full",
            product_id=self.product.pk,
            size="M",
        )
        self.assertTrue(result["ok"], result)
        proposal = IgCheckoutProposal.objects.get(client=self.client)
        request = RequestFactory().post("/offer/payment/", secure=True)
        SessionMiddleware(lambda req: None).process_request(request)
        request.user = AnonymousUser()
        delivery = SimpleNamespace(
            city="Alert failure city",
            np_office="Alert failure office",
            settlement_ref="settlement-ref",
            city_ref="city-ref",
            warehouse_ref="warehouse-ref",
            warehouse_kind="branch",
        )
        payload = {
            "full_name": "Alert failure customer",
            "phone": "+380501234567",
            "email": "alert-failure@example.test",
        }
        shown_revision = proposal.revisions.get(revision=proposal.revision)
        payload.update(expected_revision=shown_revision.revision, expected_digest=shown_revision.digest)
        with patch(
            "management.services.ig_checkout_payment.resolve_delivery_selection",
            return_value=delivery,
        ):
            attempt, invoice_url, reused = create_or_reuse_invoice(
                proposal,
                request=request,
                payload=payload,
            )

        self.assertFalse(reused)
        self.assertEqual(invoice_url, "https://pay.monobank.test/alert-outbox-failure")
        attempt.refresh_from_db()
        self.assertEqual(attempt.monobank_invoice_id, "ALERT_OUTBOX_FAILURE_INVOICE")
        self.assertEqual(attempt.invoice_url, invoice_url)
        self.assertTrue(attempt.side_effect_jobs.filter(
            kind=PaymentSideEffectJob.Kind.ATTEMPT_ADD_PAYMENT_INFO,
        ).exists())

        repeat_attempt, repeat_url, repeated = create_or_reuse_invoice(
            proposal,
            request=request,
            payload=payload,
        )

        self.assertTrue(repeated)
        self.assertEqual(repeat_attempt.pk, attempt.pk)
        self.assertEqual(repeat_url, invoice_url)
        provider.assert_called_once()
        self.assertEqual(notify_manager.call_count, 2)

    @patch("storefront.views.monobank._monobank_api_request")
    def test_bot_deal_path_blocks_missing_generic_option(self, provider):
        from product_catalog.models import GarmentFlow, GarmentFlowCategory
        from management.services import bot_orders

        flow = GarmentFlow.objects.create(
            code="ig-offer-required-material",
            name="Required material",
            axes=[{
                "code": "material",
                "label": "Матеріал",
                "options": [
                    {"code": "cotton", "label": "Бавовна", "default": True},
                    {"code": "thermo", "label": "Термохром"},
                ],
            }],
        )
        GarmentFlowCategory.objects.create(flow=flow, category=self.product.category)

        result = bot_orders.create_deal_and_link(
            self.client,
            pay_type="full",
            product_id=self.product.pk,
            size="M",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_configuration")
        self.assertEqual(result["missing_fields"], ["option:material"])
        provider.assert_not_called()

    @patch("storefront.views.monobank._monobank_api_request")
    def test_bot_deal_path_preserves_generic_option_and_price(self, provider):
        from product_catalog.models import GarmentFlow, GarmentFlowCategory, ProductOptionProfile
        from management.services import bot_orders

        flow = GarmentFlow.objects.create(
            code="ig-offer-priced-material",
            name="Priced material",
            axes=[{
                "code": "material",
                "label": "Матеріал",
                "options": [
                    {"code": "cotton", "label": "Бавовна", "default": True},
                    {"code": "thermo", "label": "Термохром"},
                ],
            }],
        )
        GarmentFlowCategory.objects.create(flow=flow, category=self.product.category)
        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="material=thermo",
            option_values={"material": "thermo"},
            price_delta=360,
        )
        self.client.sales_context = {
            "assisted_checkout_selection": {
                "product_id": self.product.pk,
                "option_values": {"material": "thermo"},
            }
        }
        self.client.save(update_fields=["sales_context", "updated_at"])

        result = bot_orders.create_deal_and_link(
            self.client,
            pay_type="full",
            product_id=self.product.pk,
            size="M",
        )

        self.assertTrue(result["ok"], result)
        proposal = IgCheckoutProposal.objects.get(client=self.client)
        item = proposal.items.get()
        self.assertEqual(item.option_values, {"material": "thermo"})
        self.assertEqual(item.catalog_unit_price, Decimal("1310.00"))
        provider.assert_not_called()

    @patch("storefront.views.monobank._monobank_api_request")
    def test_bot_deal_path_prices_a_single_paid_option_instead_of_dropping_the_delta(self, provider):
        from product_catalog.models import GarmentFlow, GarmentFlowCategory, ProductOptionProfile
        from management.services import bot_orders

        flow = GarmentFlow.objects.create(
            code="ig-offer-single-paid-material",
            name="Single paid material",
            axes=[{
                "code": "material",
                "label": "Матеріал",
                "options": [{"code": "thermo", "label": "Термохром"}],
            }],
        )
        GarmentFlowCategory.objects.create(flow=flow, category=self.product.category)
        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="material=thermo",
            option_values={"material": "thermo"},
            price_delta=360,
        )

        result = bot_orders.create_deal_and_link(
            self.client,
            pay_type="full",
            product_id=self.product.pk,
            size="M",
        )

        self.assertTrue(result["ok"], result)
        item = IgCheckoutProposal.objects.get(client=self.client).items.get()
        self.assertEqual(item.option_values, {"material": "thermo"})
        self.assertEqual(item.catalog_unit_price, Decimal("1310.00"))
        provider.assert_not_called()
