from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from management.ig_bot_models import IgDeal
from management.models import IgClient
from management.services.bot_payment_truth import active_client_order_payment_context
from management.services.ig_commercial_episodes import ensure_episode_for_deal
from management.services.ig_order_assignments import (
    link_order_to_client,
    unlink_order_from_client,
)
from orders.models import Order


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False)
class ClientOrderPaymentContextTests(TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user(
            "client-order-payment-manager",
            password="x",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_login(self.manager)
        self.ig_client = IgClient.get_or_create_for_sender("client-order-payment")
        current_deal = IgDeal.objects.create(
            client=self.ig_client,
            status=IgDeal.Status.DRAFT,
        )
        self.episode = ensure_episode_for_deal(current_deal)
        self.ig_client.stage = IgClient.Stage.PAID
        self.ig_client.save(update_fields=["stage", "updated_at"])
        self.ig_client.refresh_from_db()

    def _order(self, key, *, payment_status="paid", source="web"):
        return Order.objects.create(
            order_number=f"TWC-CONTEXT-{key}",
            full_name="Client order context",
            phone="380501234567",
            city="Kyiv",
            np_office="1",
            total_sum=Decimal("950.00"),
            payment_status=payment_status,
            source=source,
            status="ship",
        )

    def _list_row(self, client_id=None):
        client_id = client_id or self.ig_client.pk
        response = self.client.get(
            reverse("management_bot_clients_api") + f"?client_id={client_id}"
        )
        self.assertEqual(response.status_code, 200)
        return next(row for row in response.json()["clients"] if row["id"] == client_id)

    def test_unbound_current_episode_exposes_only_client_order_context(self):
        order = self._order("ACTIVE")
        link_order_to_client(order, client=self.ig_client, actor=self.manager)

        expected = {
            "order_id": order.pk,
            "payment_status": "paid",
            "source": "web",
            "scope": "client",
        }
        list_row = self._list_row()
        detail = self.client.get(
            reverse("management_bot_client_detail_api", args=[self.ig_client.pk])
        )

        self.assertEqual(detail.status_code, 200)
        for card in (list_row, detail.json()["client"]):
            self.assertEqual(card["linked_order_payment_context"], expected)
            self.assertFalse(card["commercially_confirmed"])
            self.assertEqual(card["commercial_visual_state"], "")
        self.assertEqual(list_row["stage_label"], "Потребує звірки оплати")
        paid_ids = {
            row["id"]
            for row in self.client.get(
                reverse("management_bot_clients_api") + "?view=paid"
            ).json()["clients"]
        }
        self.assertNotIn(self.ig_client.pk, paid_ids)
        self.episode.refresh_from_db()
        self.assertIsNone(self.episode.intended_order_id)

    def test_foreign_unassigned_and_unpaid_orders_are_excluded(self):
        foreign = IgClient.get_or_create_for_sender("client-order-payment-foreign")
        foreign_order = self._order("FOREIGN")
        link_order_to_client(foreign_order, client=foreign, actor=self.manager)
        self.assertIsNone(active_client_order_payment_context(self.ig_client))

        unpaid_order = self._order("UNPAID", payment_status="unpaid")
        link_order_to_client(unpaid_order, client=self.ig_client, actor=self.manager)
        self.assertIsNone(active_client_order_payment_context(self.ig_client))

        paid_order = self._order("UNASSIGNED")
        assignment = link_order_to_client(
            paid_order, client=self.ig_client, actor=self.manager
        )
        unlink_order_from_client(
            paid_order,
            client=self.ig_client,
            actor=self.manager,
            expected_version=assignment.version,
            reason_code="test_unlink",
            reason="Context must follow active ownership.",
        )
        self.assertIsNone(active_client_order_payment_context(self.ig_client))

    def test_prepayment_is_contextual_and_the_direct_read_is_one_query(self):
        order = self._order("PREPAID", payment_status="prepaid", source="website")
        link_order_to_client(order, client=self.ig_client, actor=self.manager)

        with CaptureQueriesContext(connection) as queries:
            context = active_client_order_payment_context(self.ig_client)

        self.assertEqual(len(queries), 1)
        self.assertEqual(
            context,
            {
                "order_id": order.pk,
                "payment_status": "prepaid",
                "source": "website",
                "scope": "client",
            },
        )

    def test_list_uses_correlated_context_without_per_card_queries(self):
        for index in range(24):
            client = IgClient.get_or_create_for_sender(f"client-order-context-{index}")
            link_order_to_client(
                self._order(f"LIST-{index}"), client=client, actor=self.manager
            )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("management_bot_clients_api"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["clients"]), 20)
        # The response has a fixed set of workspace queries; a context lookup
        # for every displayed card would exceed this bound.
        self.assertLessEqual(len(queries), 35)
