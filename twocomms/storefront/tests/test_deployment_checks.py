from django.conf import settings
from django.core.checks import run_checks
from django.test import SimpleTestCase, TestCase
from django.urls import resolve, reverse

from storefront.models import Category, Product
from storefront.viewsets import ProductViewSet
from twocomms.cache_headers import add_cache_headers


class DeploymentChecksTests(SimpleTestCase):
    def test_openapi_schema_generation_has_no_spectacular_warnings(self):
        issues = run_checks(include_deployment_checks=True)
        spectacular_issues = [
            issue for issue in issues if issue.id in {"drf_spectacular.W001", "drf_spectacular.W002"}
        ]

        self.assertEqual(spectacular_issues, [])

    def test_responses_cannot_be_embedded_in_frames(self):
        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")
        self.assertIn("frame-ancestors 'none'", settings.CONTENT_SECURITY_POLICY)
        headers = add_cache_headers({}, "/assets/app.js", "/assets/app.js")
        self.assertEqual(headers["X-Frame-Options"], "DENY")


class ProductApiRoutingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name="API shirts", slug="api-shirts")
        cls.product = Product.objects.create(
            title="API shirt",
            slug="api-shirt",
            category=cls.category,
            price=500,
            status="published",
        )
        cls.related_product = Product.objects.create(
            title="Related API shirt",
            slug="related-api-shirt",
            category=cls.category,
            price=600,
            status="published",
        )
        cls.draft_product = Product.objects.create(
            title="Draft API shirt",
            slug="draft-api-shirt",
            category=cls.category,
            price=700,
            status="draft",
        )
        cls.inactive_category = Category.objects.create(
            name="Inactive API category",
            slug="inactive-api-category",
            is_active=False,
        )
        Product.objects.create(
            title="Published product in inactive category",
            slug="published-product-in-inactive-category",
            category=cls.inactive_category,
            price=800,
            status="published",
        )

    def test_product_actions_are_registered_on_product_viewset(self):
        urls = [
            reverse("api-product-by-category", kwargs={"category_slug": self.category.slug}),
            reverse("api-product-related", kwargs={"slug": self.product.slug}),
            reverse("api-product-availability", kwargs={"slug": self.product.slug}),
            reverse("api-product-suggestions"),
        ]

        for url in urls:
            with self.subTest(url=url):
                self.assertIs(resolve(url).func.cls, ProductViewSet)

    def test_product_actions_return_expected_payloads(self):
        by_category = self.client.get(
            reverse("api-product-by-category", kwargs={"category_slug": self.category.slug})
        )
        self.assertEqual(by_category.status_code, 200)
        self.assertEqual(by_category.json()["count"], 2)

        related = self.client.get(
            reverse("api-product-related", kwargs={"slug": self.product.slug})
        )
        self.assertEqual(related.status_code, 200)
        self.assertEqual(related.json()["products"][0]["slug"], self.related_product.slug)

        availability = self.client.get(
            reverse("api-product-availability", kwargs={"slug": self.product.slug})
        )
        self.assertEqual(availability.status_code, 200)
        self.assertTrue(availability.json()["available"])

        suggestions = self.client.get(reverse("api-product-suggestions"), {"q": "API"})
        self.assertEqual(suggestions.status_code, 200)
        self.assertEqual(suggestions.json()["count"], 1)

    def test_public_product_api_never_exposes_drafts(self):
        product_list = self.client.get(reverse("api-product-list"))
        self.assertEqual(product_list.status_code, 200)
        self.assertEqual(product_list.json()["count"], 2)

        draft_detail = self.client.get(
            reverse("api-product-detail", kwargs={"slug": self.draft_product.slug})
        )
        self.assertEqual(draft_detail.status_code, 404)

        search = self.client.get(reverse("api-product-search"), {"q": "Draft"})
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()["count"], 0)

        by_category = self.client.get(
            reverse("api-product-by-category", kwargs={"category_slug": self.category.slug})
        )
        self.assertEqual(by_category.status_code, 200)
        self.assertEqual(by_category.json()["count"], 2)

        related = self.client.get(
            reverse("api-product-related", kwargs={"slug": self.product.slug})
        )
        self.assertNotIn(
            self.draft_product.slug,
            {item["slug"] for item in related.json()["products"]},
        )

        suggestions = self.client.get(reverse("api-product-suggestions"), {"q": "Draft"})
        self.assertEqual(suggestions.status_code, 200)
        self.assertEqual(suggestions.json()["count"], 0)

    def test_public_category_api_excludes_inactive_and_draft_counts(self):
        category_list = self.client.get(reverse("api-category-list"))
        self.assertEqual(category_list.status_code, 200)
        self.assertEqual(category_list.json()["count"], 1)
        self.assertEqual(category_list.json()["results"][0]["products_count"], 2)

        inactive_detail = self.client.get(
            reverse("api-category-detail", kwargs={"slug": self.inactive_category.slug})
        )
        self.assertEqual(inactive_detail.status_code, 404)

        inactive_products = self.client.get(
            reverse(
                "api-product-by-category",
                kwargs={"category_slug": self.inactive_category.slug},
            )
        )
        self.assertEqual(inactive_products.status_code, 404)

    def test_suggestions_limit_is_bounded_and_tolerates_invalid_input(self):
        suggestions_url = reverse("api-product-suggestions")

        for invalid_limit in ("invalid", "-1", "0"):
            with self.subTest(limit=invalid_limit):
                response = self.client.get(
                    suggestions_url,
                    {"q": "API", "limit": invalid_limit},
                )
                self.assertEqual(response.status_code, 200)
                self.assertLessEqual(response.json()["count"], 10)
