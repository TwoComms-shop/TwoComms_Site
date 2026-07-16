import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from fable5.models import (
    ProductOptionSizeGrid,
    ProductSizeRule,
    SizeGridProfile,
    VariantSizeRule,
)
from productcolors.models import Color, ProductColorVariant
from storefront.models import Catalog, Category, Product, ProductFitOption, SizeGrid


class RestockSubscriptionEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.product = Product.objects.create(
            title="Restock T-shirt",
            slug="restock-shirt",
            category=Category.objects.create(name="Restock", slug="restock"),
            price=900,
            status="published",
        )
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=Color.objects.create(name="Black", primary_hex="#111111"),
            slug="black",
            is_default=True,
        )
        VariantSizeRule.objects.create(
            variant=self.variant,
            size="M",
            is_enabled=False,
        )
        self.url = reverse("restock_subscribe")

    def payload(self, **overrides):
        data = {
            "product_id": self.product.id,
            "color_variant_id": self.variant.id,
            "size": "M",
            "option_values": {},
            "option_labels": {},
            "channel": "email",
            "name": "Buyer",
            "contact": "Buyer@Example.com",
            "website": "",
        }
        data.update(overrides)
        return data

    @patch("storefront.services.restock.notify_restock_admin", return_value=True)
    def test_email_subscription_is_idempotent(self, notify_mock):
        from storefront.models import RestockSubscription

        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(
                self.url,
                json.dumps(self.payload()),
                content_type="application/json",
            )
        second = self.client.post(
            self.url,
            json.dumps(self.payload(contact="buyer@example.com")),
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(RestockSubscription.objects.count(), 1)
        subscription = RestockSubscription.objects.get()
        self.assertEqual(subscription.normalized_contact, "buyer@example.com")
        self.assertEqual(subscription.status, RestockSubscription.Status.ACTIVE)
        notify_mock.assert_called_once_with(subscription)

    def test_invalid_email_is_rejected_without_creating_a_lead(self):
        from storefront.models import RestockSubscription

        response = self.client.post(
            self.url,
            json.dumps(self.payload(contact="not-an-email")),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(RestockSubscription.objects.count(), 0)

    def test_available_size_is_rejected(self):
        from storefront.models import RestockSubscription

        response = self.client.post(
            self.url,
            json.dumps(self.payload(size="L")),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(RestockSubscription.objects.count(), 0)

    @patch("storefront.services.restock.notify_restock_admin", return_value=True)
    def test_grid_only_unavailable_fit_size_can_create_subscription(self, _notify):
        VariantSizeRule.objects.filter(variant=self.variant, size="M").delete()
        catalog = Catalog.objects.create(name="Restock grids", slug="restock-grids")
        self.product.catalog = catalog
        self.product.save(update_fields=["catalog"])
        ProductFitOption.objects.create(
            product=self.product,
            code="classic",
            label="Classic",
            is_active=True,
            is_default=True,
        )
        grid = SizeGrid.objects.create(
            catalog=catalog,
            name="Classic restock grid",
            guide_data={
                "columns": [{"key": "size", "label": "Size"}],
                "rows": [{"size": "M"}, {"size": "L"}],
            },
            is_active=True,
        )
        SizeGridProfile.objects.create(size_grid=grid, option_key="fit=classic")
        ProductOptionSizeGrid.objects.create(
            product=self.product,
            option_key="fit=classic",
            size_grid=grid,
        )
        ProductSizeRule.objects.create(
            product=self.product,
            option_key="fit=classic",
            size="M",
            is_enabled=False,
        )

        response = self.client.post(
            self.url,
            json.dumps(self.payload(option_values={"fit": "classic"})),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["ok"], True)

    @patch("storefront.services.restock.notify_restock_admin", return_value=True)
    def test_telegram_subscription_starts_as_draft(self, notify_mock):
        from storefront.models import RestockSubscription

        response = self.client.post(
            self.url,
            json.dumps(self.payload(channel="telegram", contact="")),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        subscription = RestockSubscription.objects.get()
        self.assertEqual(subscription.status, RestockSubscription.Status.DRAFT)
        self.assertEqual(response.json()["subscription_id"], subscription.id)
        notify_mock.assert_not_called()

    def test_honeypot_returns_success_without_persisting(self):
        from storefront.models import RestockSubscription

        response = self.client.post(
            self.url,
            json.dumps(self.payload(website="https://spam.example")),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(RestockSubscription.objects.count(), 0)
