"""A waiting-list agreement remains private and cannot be mutated by its reader."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse

from management.bot_restock_waiting_list_views import bot_restock_waiting_list_api


@override_settings(ROOT_URLCONF="management.urls")
class WaitingListApiAccessTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.url = reverse("management_bot_restock_waiting_list_api", args=[7])

    def request(self, method="get", authenticated=True):
        request = getattr(self.factory, method)(self.url)
        request.user = SimpleNamespace(is_authenticated=authenticated)
        return request

    def test_anonymous_cannot_read_client_agreement(self):
        response = bot_restock_waiting_list_api(self.request(authenticated=False), 7)
        self.assertEqual(response.status_code, 302)
        self.assertIn("next=", response.url)

    def test_reader_does_not_accept_mutations(self):
        response = bot_restock_waiting_list_api(self.request("post"), 7)
        self.assertEqual(response.status_code, 405)

    @patch("management.bot_restock_waiting_list_views.has_all_bot_capabilities", return_value=False)
    @patch("management.models.IgClient.objects.filter")
    @patch("management.services.ig_restock_waiting_list.waiting_list_projection")
    def test_permission_denial_happens_before_owner_or_sources_read(self, projection, owners, permission):
        response = bot_restock_waiting_list_api(self.request(), 7)
        self.assertEqual(response.status_code, 403)
        owners.assert_not_called()
        projection.assert_not_called()

    @patch("management.bot_restock_waiting_list_views.has_all_bot_capabilities", return_value=True)
    @patch("management.models.IgClient.objects.filter")
    @patch("management.services.ig_restock_waiting_list.waiting_list_projection")
    def test_missing_or_erasing_client_has_no_agreement_leak(self, projection, owners, permission):
        for owner, status in ((None, 404), (SimpleNamespace(privacy_erasure_started_at=True), 410)):
            with self.subTest(status=status):
                owners.return_value.only.return_value.first.return_value = owner
                response = bot_restock_waiting_list_api(self.request(), 7)
                self.assertEqual(response.status_code, status)
        projection.assert_not_called()

    @patch("management.bot_restock_waiting_list_views.has_all_bot_capabilities", return_value=True)
    @patch("management.models.IgClient.objects.filter")
    @patch("management.services.ig_restock_waiting_list.waiting_list_projection")
    def test_reader_returns_only_its_owner_projection_without_caching(self, projection, owners, permission):
        owners.return_value.only.return_value.first.return_value = SimpleNamespace(privacy_erasure_started_at=None)
        expected = {"version": 2, "lists": [], "coverage_complete": True}
        projection.return_value = expected
        response = bot_restock_waiting_list_api(self.request(), 7)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"success": True, "waiting_list": expected})
        projection.assert_called_once_with(7)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("private", response.headers["Cache-Control"])
