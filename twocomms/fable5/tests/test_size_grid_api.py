import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from storefront.models import Catalog, Category, Product, SizeGrid

from fable5.models import ProductOptionSizeGrid, SizeGridProfile


class SizeGridApiTests(TestCase):
    def setUp(self):
        self.catalog = Catalog.objects.create(name="API T-shirts", slug="api-tshirts")
        self.other_catalog = Catalog.objects.create(name="API Hoodies", slug="api-hoodies")
        self.category = Category.objects.create(name="API category", slug="api-category")
        self.staff = get_user_model().objects.create_user(
            username="grid-staff",
            password="test-password",
            is_staff=True,
        )
        self.user = get_user_model().objects.create_user(
            username="grid-user",
            password="test-password",
        )
        self.client.force_login(self.staff)

    @staticmethod
    def _payload(**overrides):
        payload = {
            "catalog_id": None,
            "name": "Класична футболка",
            "description": "Заміри виробу",
            "order": 10,
            "profile": {"garment_code": "tshirt", "option_key": "fit=classic"},
            "guide_data": {
                "title": "Класика",
                "columns": [
                    {"key": "size", "label": "Розмір"},
                    {"key": "width", "label": "Ширина"},
                ],
                "rows": [{"size": "S", "width": "49"}, {"size": "2XL", "width": "58"}],
            },
        }
        payload.update(overrides)
        return payload

    def _post_json(self, url_name, payload):
        return self.client.post(
            reverse(url_name),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_anonymous_and_non_staff_are_rejected_with_json(self):
        self.client.logout()
        anonymous = self.client.get(reverse("fable5_api_size_grids"))
        self.client.force_login(self.user)
        non_staff = self.client.get(reverse("fable5_api_size_grids"))

        self.assertEqual(anonymous.status_code, 403)
        self.assertEqual(non_staff.status_code, 403)
        self.assertEqual(anonymous.json()["ok"], False)

    def test_method_restrictions_and_csrf_protection(self):
        wrong_method = self.client.get(reverse("fable5_api_size_grid_save"))
        self.assertEqual(wrong_method.status_code, 405)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        missing_token = csrf_client.post(
            reverse("fable5_api_size_grid_save"),
            data=json.dumps(self._payload(catalog_id=self.catalog.id)),
            content_type="application/json",
        )
        self.assertEqual(missing_token.status_code, 403)

    def test_save_normalizes_grid_and_profile_in_one_envelope(self):
        response = self._post_json(
            "fable5_api_size_grid_save",
            self._payload(catalog_id=self.catalog.id),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"ok", "grid"})
        self.assertTrue(body["ok"])
        grid = SizeGrid.objects.get(pk=body["grid"]["id"])
        self.assertEqual(grid.guide_data["rows"][-1]["size"], "XXL")
        self.assertEqual(grid.fable5_profile.option_key, "fit=classic")

    def test_save_rejects_catalog_change_for_existing_grid(self):
        created = self._post_json(
            "fable5_api_size_grid_save",
            self._payload(catalog_id=self.catalog.id),
        ).json()["grid"]

        response = self._post_json(
            "fable5_api_size_grid_save",
            self._payload(id=created["id"], catalog_id=self.other_catalog.id),
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    def test_duplicate_uses_stable_copy_name(self):
        original = self._post_json(
            "fable5_api_size_grid_save",
            self._payload(catalog_id=self.catalog.id),
        ).json()["grid"]

        first = self._post_json("fable5_api_size_grid_duplicate", {"id": original["id"]})
        second = self._post_json("fable5_api_size_grid_duplicate", {"id": original["id"]})

        self.assertEqual(first.json()["grid"]["name"], "Класична футболка — копія")
        self.assertEqual(second.json()["grid"]["name"], "Класична футболка — копія 2")

    def test_archive_is_soft_and_refuses_grid_used_by_product(self):
        grid = SizeGrid.objects.create(
            catalog=self.catalog,
            name="Assigned grid",
            guide_data={
                "columns": [{"key": "size", "label": "Розмір"}],
                "rows": [{"size": "S"}],
            },
        )
        SizeGridProfile.objects.create(size_grid=grid, option_key="fit=classic")
        product = Product.objects.create(
            title="Assigned product",
            slug="assigned-grid-product",
            category=self.category,
            catalog=self.catalog,
            price=1000,
        )
        ProductOptionSizeGrid.objects.create(
            product=product,
            option_key="fit=classic",
            size_grid=grid,
        )

        blocked = self._post_json("fable5_api_size_grid_archive", {"id": grid.id})
        self.assertEqual(blocked.status_code, 409)
        grid.refresh_from_db()
        self.assertTrue(grid.is_active)

        ProductOptionSizeGrid.objects.all().delete()
        archived = self._post_json("fable5_api_size_grid_archive", {"id": grid.id})
        self.assertEqual(archived.status_code, 200)
        grid.refresh_from_db()
        self.assertFalse(grid.is_active)
        self.assertTrue(SizeGrid.objects.filter(pk=grid.id).exists())

    def test_preview_and_list_have_stable_serialized_payloads(self):
        saved = self._post_json(
            "fable5_api_size_grid_save",
            self._payload(catalog_id=self.catalog.id),
        ).json()["grid"]

        preview = self.client.get(
            reverse("fable5_api_size_grid_preview"),
            {"id": saved["id"]},
        )
        listing = self.client.get(
            reverse("fable5_api_size_grids"),
            {"catalog_id": self.catalog.id},
        )

        self.assertEqual(set(preview.json()), {"ok", "preview"})
        self.assertEqual(preview.json()["preview"]["rows"][1]["display_size"], "2XL")
        self.assertEqual(set(listing.json()), {"ok", "grids"})
        self.assertEqual(listing.json()["grids"][0]["profile"]["option_key"], "fit=classic")

    def test_list_query_count_does_not_grow_per_grid(self):
        for index in range(5):
            grid = SizeGrid.objects.create(
                catalog=self.catalog,
                name=f"Grid {index}",
                guide_data={
                    "columns": [{"key": "size", "label": "Розмір"}],
                    "rows": [{"size": "S"}],
                },
            )
            SizeGridProfile.objects.create(size_grid=grid, option_key=f"fit=fit-{index}")

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("fable5_api_size_grids"))

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 4)
