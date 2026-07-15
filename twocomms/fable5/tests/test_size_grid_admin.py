from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from storefront.models import Catalog


class SizeGridAdminWorkspaceTests(TestCase):
    def setUp(self):
        self.catalog = Catalog.objects.create(name="Workspace tees", slug="workspace-tees")
        self.staff = get_user_model().objects.create_user(
            username="workspace-staff",
            password="test-password",
            is_staff=True,
        )
        self.user = get_user_model().objects.create_user(
            username="workspace-user",
            password="test-password",
        )
        self.url = f'{reverse("admin_panel")}?section=size_grids'

    def test_workspace_requires_staff(self):
        anonymous = self.client.get(self.url)
        self.client.force_login(self.user)
        non_staff = self.client.get(self.url)

        self.assertEqual(anonymous.status_code, 302)
        self.assertEqual(non_staff.status_code, 302)

    def test_staff_sees_size_grid_navigation_after_catalogs(self):
        self.client.force_login(self.staff)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn("Розмірні сітки", html)
        self.assertLess(html.index("Каталоги"), html.index("Розмірні сітки"))
        self.assertIn('href="?section=size_grids"', html)
        self.assertIn('class="admin-nav-item active"', html)

    def test_workspace_bootstrap_contains_catalog_and_all_api_urls(self):
        self.client.force_login(self.staff)

        response = self.client.get(self.url)

        self.assertContains(response, 'id="f5-size-grid-bootstrap"', html=False)
        self.assertContains(response, self.catalog.name)
        for url_name in (
            "fable5_api_size_grids",
            "fable5_api_size_grid_save",
            "fable5_api_size_grid_duplicate",
            "fable5_api_size_grid_archive",
            "fable5_api_size_grid_preview",
        ):
            self.assertContains(response, reverse(url_name))

    def test_workspace_has_accessible_editor_preview_and_cache_busted_assets(self):
        self.client.force_login(self.staff)

        response = self.client.get(self.url)

        self.assertContains(response, "Бібліотека сіток")
        self.assertContains(response, "Попередній перегляд")
        self.assertContains(response, 'aria-live="polite"', html=False)
        self.assertContains(response, 'data-action="move-row-up"', html=False)
        self.assertContains(response, 'data-action="move-row-down"', html=False)
        self.assertContains(response, "size-grids.css?v=20260715-1", html=False)
        self.assertContains(response, "size-grids.js?v=20260715-1", html=False)
