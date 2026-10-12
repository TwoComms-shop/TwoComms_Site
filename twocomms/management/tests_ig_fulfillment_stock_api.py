"""Staff API and physical evidence guards for actual finite-stock preparation."""
import json
import uuid

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import Client, TransactionTestCase, override_settings

from management.ig_fulfillment_stock_sync_models import IgFulfillmentStockSync as Sync
from management.models import IgClient
from management import tests_ig_fulfillment_integration as integration_helpers
from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    ROOT_URLCONF="management.urls", SITE_BASE_URL="https://twocomms.test",
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class FulfillmentStockApiTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True
    endpoint = integration_helpers.FulfillmentNativeIntegrationTests.endpoint
    case = integration_helpers.FulfillmentNativeIntegrationTests.case
    decision_payload = integration_helpers.FulfillmentNativeIntegrationTests.decision_payload
    decide = integration_helpers.FulfillmentNativeIntegrationTests.decide

    def setUp(self):
        super().setUp()
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        self.manager = get_user_model().objects.create_superuser("stock-api-manager", password="test")
        self.client = Client(HTTP_HOST="management.twocomms.shop")
        self.client.force_login(self.manager)

    def prepared_action(self):
        request, _, _ = self.case()
        self.decide(request)
        body = self.client.get(self.endpoint(), {"request_id": str(request.pk)}).json()
        self.assertTrue(body["success"], body)
        preview = body["review"]["stock_sync"]
        self.assertTrue(preview["eligible"], preview)
        counts = {}
        for pool in preview["pools"]:
            value = {"total": pool["minimum_total"]}
            if pool["source"] == "catalog_variant":
                value["size_counts"] = {key: row["minimum_total"] for key, row in pool["size_counts"].items()}
            counts[pool["pool_id"]] = value
        return request, {"action": "stock_sync", "request_id": str(request.pk),
            "expected_decision_digest": preview["decision_digest"],
            "expected_preview_digest": preview["preview_digest"],
            "operation_id": str(uuid.uuid4()), "counts_by_pool": counts}

    def post(self, body, client=None):
        return (client or self.client).post(self.endpoint(), json.dumps(body), content_type="application/json")

    def test_real_authenticated_sync_reload_and_idempotent_retry(self):
        request, body = self.prepared_action()
        first = self.post(body)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertTrue(first.json()["success"])
        receipt = Sync.objects.get(decision__request=request)
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (1, 1))
        second = self.post(body)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(Sync.objects.count(), 1)
        self.assertEqual(Sync.objects.get().pk, receipt.pk)
        reload = self.client.get(self.endpoint(), {"request_id": str(request.pk)}).json()["review"]["stock_sync"]
        self.assertEqual(reload["reason"], "already_synced", reload)
        self.assertFalse(reload["eligible"])

    def test_stale_preview_foreign_request_and_non_operator_do_not_mutate_stock(self):
        _, body = self.prepared_action()
        stale = dict(body, expected_preview_digest="0" * 64)
        self.assertEqual(self.post(stale).status_code, 409)
        other = IgClient.objects.create(igsid="foreign-stock-owner")
        response = self.client.post(self.endpoint(other), json.dumps(body), content_type="application/json")
        self.assertEqual(response.status_code, 404)
        viewer = get_user_model().objects.create_user("stock-api-no-caps", password="test")
        client = Client(HTTP_HOST="management.twocomms.shop")
        client.force_login(viewer)
        self.assertEqual(self.post(body, client).status_code, 403)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 0)
        self.assertEqual(Sync.objects.count(), 0)

    def test_completed_receipt_keeps_history_but_detects_changed_source_cart(self):
        self.canonical("Хочу купити")
        request, body = self.prepared_action()
        response = self.post(body)
        self.assertEqual(response.status_code, 200, response.content)
        current = self.client.get(self.endpoint(), {"request_id": str(request.pk)}).json()["review"]["stock_sync"]
        self.assertTrue(current["checkout"]["source_binding_current"], current)
        self.canonical("змініть кількість на 2")
        changed = self.client.get(self.endpoint(), {"request_id": str(request.pk)}).json()["review"]["stock_sync"]
        self.assertEqual(changed["reason"], "already_synced", changed)
        self.assertFalse(changed["checkout"]["source_binding_current"], changed)
        self.assertEqual(changed["decision_digest"], body["expected_decision_digest"])
        self.assertEqual(Sync.objects.count(), 1)

    def test_csrf_duplicate_fields_and_privacy_fence_fail_before_mutation(self):
        _, body = self.prepared_action()
        csrf = Client(enforce_csrf_checks=True, HTTP_HOST="management.twocomms.shop")
        csrf.force_login(self.manager)
        self.assertEqual(self.post(body, csrf).status_code, 403)
        malformed = json.dumps(body)[:-1] + ',"action":"stock_sync"}'
        self.assertEqual(self.client.post(self.endpoint(), malformed, content_type="application/json").status_code, 400)
        IgClient.objects.filter(pk=self.client_row.pk).update(privacy_erasure_started_at=self.now)
        self.assertEqual(self.post(body).status_code, 410)
        self.assertEqual(Sync.objects.count(), 0)

    def test_native_receipt_rewrite_and_delete_are_denied_and_privacy_purge_preserves_inventory(self):
        _, body = self.prepared_action()
        response = self.post(body)
        self.assertEqual(response.status_code, 200, response.content)
        row = Sync.objects.get()
        pk = row._meta.pk.get_db_prep_value(row.pk, connection)
        for statement in (
            "UPDATE management_igfulfillmentstocksync SET envelope_hmac=envelope_hmac WHERE id=%s",
            "DELETE FROM management_igfulfillmentstocksync WHERE id=%s",
        ):
            with self.subTest(statement=statement), self.assertRaises(DatabaseError):
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(statement, [pk])
        columns = [field.column for field in row._meta.concrete_fields]
        names = ",".join(connection.ops.quote_name(name) for name in columns)
        values = ",".join("UPPER(envelope_hmac)" if name == "envelope_hmac"
            else connection.ops.quote_name(name) for name in columns)
        with self.assertRaisesRegex(DatabaseError, "Fulfilment stock immutable/owner/privacy guard"):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f"INSERT INTO management_igfulfillmentstocksync ({names}) "
                    f"SELECT {values} FROM management_igfulfillmentstocksync WHERE id=%s", [pk])
        from management.services.ig_fulfillment_privacy import purge_fulfillment_data
        IgClient.objects.filter(pk=self.client_row.pk).update(privacy_erasure_started_at=self.now)
        with transaction.atomic():
            purge_fulfillment_data([self.client_row.pk])
        self.assertEqual(Sync.objects.count(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 1)
