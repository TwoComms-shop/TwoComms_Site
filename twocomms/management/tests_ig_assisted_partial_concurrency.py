"""Separate native MariaDB connections contend for one actual selected scope."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings

from management.ig_assisted_commerce_models import IgAssistedCommerceExecution
from management.models import IgCheckoutAccessToken, IgCheckoutProposal
from management.services.ig_assisted_checkout import prepare_assisted_checkout
from management.tests_ig_assisted_partial_sources import PartialCommerceFixture
from orders.models import Order, PaymentAttempt


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="off",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0, GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class NativePartialConcurrencyTests(PartialCommerceFixture, TransactionTestCase):
    @skipUnless(connection.vendor == "mysql", "Real row contention requires MariaDB")
    def test_two_connections_cannot_prepare_overlapping_selected_capacity(self):
        selected = self.selected_case()
        request, decision, selection = selected["request"], selected["decision"], selected["selection"]
        barrier = Barrier(2, timeout=20)
        actor_id, client_id, settings_id = self.manager.pk, self.client_row.pk, self.settings.pk
        revision_id = selected["revision"].pk

        def prepare(operation_id):
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=actor_id)
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT CONNECTION_ID()")
                    connection_id = cursor.fetchone()[0]
                barrier.wait()
                result = prepare_assisted_checkout(client_id, actor=actor,
                    request_id=request.pk, decision_id=decision.pk,
                    expected_request_digest=request.envelope_hmac,
                    expected_decision_digest=decision.envelope_hmac,
                    operation_id=operation_id, revision_id=revision_id, settings_id=settings_id,
                    verified_selection=selection)
                return connection_id, result
            finally:
                connections.close_all()

        # Both independent MariaDB sessions use the real cross-worker advisory
        # lock and canonical Client row lock; no authority or lock is mocked.
        # The actual hosted checkout route is on the public URL configuration.
        with override_settings(ROOT_URLCONF="twocomms.urls"), \
                patch("management.services.instagram_bot._provider_http") as provider:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(prepare, uuid.uuid4()) for _ in range(2)]
                outputs = [future.result(timeout=45) for future in futures]
        provider.assert_not_called()
        self.assertEqual(len({connection_id for connection_id, _ in outputs}), 2)
        results = [result for _, result in outputs]
        self.assertEqual(sum(result.get("ready") is True for result in results), 1, results)
        denied, = [result for result in results if result.get("ready") is not True]
        self.assertIsInstance(denied.get("reason"), str)
        self.assertTrue(denied["reason"])
        self.assertLessEqual(len(denied["reason"]), 128)
        self.assertEqual(IgAssistedCommerceExecution.objects.filter(intention_id=request.intention_id).count(), 1)
        self.assertEqual(IgCheckoutProposal.objects.filter(client_id=client_id).count(), 1)
        self.assertEqual(IgCheckoutAccessToken.objects.filter(proposal__client_id=client_id).count(), 1)
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())
        execution = IgAssistedCommerceExecution.objects.get(intention_id=request.intention_id)
        self.assertEqual(execution.envelope["snapshot"]["selected_line_ids"], list(selection.selected_line_ids))
        self.assertEqual(execution.envelope["snapshot"]["deferred_line_ids"], list(selection.deferred_line_ids))
