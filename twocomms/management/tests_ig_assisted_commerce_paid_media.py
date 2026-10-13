"""Canonical paid photo retention and account authorization at the barrier."""
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from management.services import ig_assisted_commerce_media as media
from management.services.ig_commercial_episodes import commercial_episode_client_lock
from management.services.ig_private_media import private_media_storage, purge_due_assisted_images
from management.tests_ig_assisted_checkout_payment import AssistedCheckoutPaymentFixture
from orders.models import Order


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class PaidAssistedMediaBoundaryTests(AssistedCheckoutPaymentFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        # Canonicalize only the fixture's own temp root; production rejects
        # symlink components, including the macOS /tmp alias.
        private = override_settings(IG_PRIVATE_MEDIA_ROOT=str(Path(settings.IG_PRIVATE_MEDIA_ROOT).resolve()))
        private.enable()
        self.addCleanup(private.disable)

    def account_order(self):
        owner = get_user_model().objects.create_user("assisted-photo-account-owner", password="test")
        self.client.force_login(owner)
        result = self._paid_custom_order(photo=True)
        self.assertEqual(result["order"].user_id, owner.pk)
        return owner, result

    def test_actual_paid_photo_survives_unconverted_expiry_and_authenticated_order_route(self):
        owner, result = self.account_order()
        image, order = result["image"], result["order"]
        item, = result["items"]
        with patch("django.utils.timezone.now", return_value=image.delete_after + timedelta(seconds=1)), \
                patch("management.services.instagram_bot._provider_http") as provider:
            self.assertTrue(media.has_business_retention(image))
            purge_due_assisted_images(limit=1)
            image.refresh_from_db()
            self.assertEqual(image.state, "active")
            served = media.serve_order_asset(order, item, account_user=owner)
            with private_media_storage().open(image.storage_key, "rb") as handle:
                self.assertEqual(served.content, handle.read())
            response = self.client.get(reverse("account_assisted_order_item_image",
                kwargs={"order_id": order.pk, "item_id": item.pk}))
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.content, served.content)
            self.assertIn("no-store", response["Cache-Control"])
            provider.assert_not_called()

    def test_account_owner_change_at_client_barrier_blocks_the_prechecked_owner(self):
        owner, result = self.account_order()
        other = get_user_model().objects.create_user("assisted-photo-new-owner", password="test")
        order = result["order"]
        item, = result["items"]
        entered = []

        @contextmanager
        def owner_changed_at_barrier(client_id):
            with commercial_episode_client_lock(client_id):
                entered.append(client_id)
                Order.objects.filter(pk=order.pk).update(user=other)
                yield

        with patch("management.services.ig_commercial_episodes.commercial_episode_client_lock",
                side_effect=owner_changed_at_barrier), self.assertRaises(Http404):
            media.serve_order_asset(order, item, account_user=owner)
        self.assertEqual(entered, [result["execution"].client_id])
        order.refresh_from_db()
        self.assertEqual(order.user_id, other.pk)

    def test_account_deactivation_at_client_barrier_blocks_the_cached_active_user(self):
        owner, result = self.account_order()
        order, item = result["order"], result["items"][0]
        entered = []

        @contextmanager
        def deactivated_at_barrier(client_id):
            with commercial_episode_client_lock(client_id):
                entered.append(client_id)
                get_user_model().objects.filter(pk=owner.pk).update(is_active=False)
                yield

        with patch("management.services.ig_commercial_episodes.commercial_episode_client_lock",
                side_effect=deactivated_at_barrier), self.assertRaises(Http404):
            media.serve_order_asset(order, item, account_user=owner)
        self.assertTrue(owner.is_active, "caller still holds the old pre-barrier object")
        self.assertEqual(entered, [result["execution"].client_id])

    def test_hosted_photo_requires_actual_token_grant_and_privacy_fence(self):
        result = self._custom_proposal(photo=True)
        proposal = result["proposal"]
        item = proposal.items.get()
        path = reverse("ig_checkout_item_image", kwargs={"proposal_id": proposal.public_id, "item_id": item.pk})
        self.assertEqual(self.client.get(path).status_code, 404)
        self._open(proposal)
        self.assertEqual(self.client.get(path).status_code, 200)
        from management.models import IgCheckoutAccessToken
        IgCheckoutAccessToken.objects.filter(proposal=proposal).update(revoked_at=self.now)
        self.assertEqual(self.client.get(path).status_code, 404)
        self.client_row.privacy_erasure_started_at = self.now
        self.client_row.save(update_fields=["privacy_erasure_started_at"])
        with self.assertRaises(media.commerce.AssistedDecisionError):
            media.serve_operator_asset(self.client_row.pk, result["image"].pk, actor=self.manager)
