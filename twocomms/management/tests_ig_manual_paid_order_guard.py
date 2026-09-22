from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
from orders.models import Order


@override_settings(
    ROOT_URLCONF="twocomms.urls_management",
    ALLOWED_HOSTS=["testserver", "management.twocomms.shop"],
)
class InstagramManualPaidOrderGuardTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(
            username="manual-paid-order-guard",
            password="test-password",
            is_staff=True,
            is_superuser=True,
        )
        self.client_row = IgClient.get_or_create_for_sender("manual-paid-order-guard-client")
        self.web = Client()
        self.web.force_login(self.actor)
        self.order = Order.objects.create(
            full_name="Instagram buyer",
            phone="380501112233",
            total_sum=Decimal("790.00"),
            payment_status="paid",
            source="manual",
            sale_source="Instagram",
        )

    def _post_link(self):
        return self.web.post(
            reverse("management_bot_client_order_link_api", args=[self.client_row.pk]),
            {"order_identifier": self.order.order_number},
            secure=True,
        )

    def test_paid_instagram_order_requires_payment_review(self):
        response = self._post_link()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "payment_review_required")

    def test_pending_review_cannot_be_bypassed_by_assignment(self):
        review = IgPaymentConfirmationReview.objects.create(
            client=self.client_row,
            dedupe_key="manual-paid-order-pending",
            evidence={"order_draft": {"quoted_total": "790.00"}},
        )

        response = self._post_link()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "payment_review_pending")
        self.assertEqual(response.json()["review_id"], review.pk)

    def test_confirmed_review_uses_canonical_attribution(self):
        from management.services.ig_payment_review import record_review_decision
        from management.ig_bot_models import IgOrderAttribution

        review = IgPaymentConfirmationReview.objects.create(
            client=self.client_row,
            dedupe_key="manual-paid-order-confirmed",
            evidence={"order_draft": {"quoted_total": "790.00"}},
        )
        record_review_decision(
            review,
            actor=self.actor,
            decision="manager_verified",
            verification_scope="full_payment",
            confirmed_amount="790.00",
        )

        response = self._post_link()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["attribution"])
        self.assertEqual(response.json()["payment_review_id"], review.pk)
        self.assertTrue(IgOrderAttribution.objects.filter(order=self.order).exists())
