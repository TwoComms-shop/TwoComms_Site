"""Explicit assisted catalog recount through the real authenticated API."""
from copy import deepcopy
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings

from management.models import IgCheckoutProposal
from management.ig_assisted_commerce_models import IgAssistedCommerceExecution as Execution, IgAssistedCommerceDecision as Decision
from management.tests_ig_assisted_commerce_api import _AssistedApiFixture


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class AssistedStockApiTests(_AssistedApiFixture, TestCase):
    def approved_catalog(self):
        request, _original, context = self.produced_request()
        draft = self.post({"action": "draft", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
            **context["cas"], "items": self.draft_items(request)})
        self.assertEqual(draft.status_code, 200, draft.content)
        approved = self.post({"action": "approve", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
            **draft.json()["commerce"]["cas"]})
        self.assertEqual(approved.status_code, 200, approved.content)
        return request, Decision.objects.get(pk=approved.json()["decision_id"]), approved.json()["commerce"]

    def recount_payload(self, request, decision, context, *, count=8):
        preview = context["stock_sync"]
        self.assertTrue(preview["eligible"], preview)
        self.assertTrue(preview["pools"])
        counts = {}
        for pool in preview["pools"]:
            counts[pool["pool_id"]] = {"total": count}
            if pool["source"] == "catalog_variant":
                counts[pool["pool_id"]]["size_counts"] = {key: count for key in pool["size_counts"]}
        return {"action": "stock_sync", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
            "decision_id": str(decision.pk), "expected_request_digest": request.envelope_hmac,
            "expected_decision_digest": decision.envelope_hmac, "expected_preview_digest": preview["preview_digest"],
            "counts_by_pool": counts}

    def test_actual_explicit_recount_preserves_approval_and_same_operation_does_not_apply_twice(self):
        with patch("management.services.instagram_bot._provider_http") as provider:
            request, decision, context = self.approved_catalog()
            self.variant.refresh_from_db();self.rule.refresh_from_db()
            self.assertEqual((self.variant.stock, self.rule.stock), (0, 0), "approval did not manufacture stock")
            original = (request.envelope_hmac, deepcopy(request.envelope), decision.envelope_hmac, deepcopy(decision.envelope))
            payload = self.recount_payload(request, decision, context)
            response = self.post(payload)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["stock_sync"]["status"], "stock_ready")
            self.variant.refresh_from_db();self.rule.refresh_from_db()
            self.assertEqual((self.variant.stock, self.rule.stock), (8, 8))
            request.refresh_from_db();decision.refresh_from_db()
            self.assertEqual((request.envelope_hmac, request.envelope, decision.envelope_hmac, decision.envelope), original)
            self.assertEqual(response.json()["commerce"]["status"], "current")
            replay = self.post(payload)
            self.assertEqual(replay.status_code, 200, replay.content)
            self.assertFalse(replay.json()["stock_sync"]["created"])
            self.assertEqual(replay.json()["stock_sync"]["sync_id"], response.json()["stock_sync"]["sync_id"])
            self.variant.refresh_from_db();self.rule.refresh_from_db()
            self.assertEqual((self.variant.stock, self.rule.stock), (8, 8))
            self.assertFalse(IgCheckoutProposal.objects.exists())
            self.assertFalse(Execution.objects.exists())
            provider.assert_not_called()

    def test_changed_operation_counts_stale_preview_and_untyped_pool_identifiers_fail_without_stock_writes(self):
        request, decision, context = self.approved_catalog()
        payload = self.recount_payload(request, decision, context)
        bad = deepcopy(payload);bad["expected_preview_digest"] = "0" * 64
        self.assertEqual(self.post(bad).status_code, 409)
        bad = deepcopy(payload);bad["counts_by_pool"] = {"warehouse:999999": {"total": 8}}
        self.assertEqual(self.post(bad).status_code, 409)
        self.variant.refresh_from_db();self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        self.assertEqual(self.post(payload).status_code, 200)
        conflict = deepcopy(payload)
        for count in conflict["counts_by_pool"].values():
            count["total"] = 9
            if "size_counts" in count:
                count["size_counts"] = {key: 9 for key in count["size_counts"]}
        result = self.post(conflict)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["stock_sync"]["reason"], "operation_conflict")
        self.variant.refresh_from_db();self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (8, 8))

    def test_counts_are_required_typed_absolute_values_and_privacy_fence_remains_dominant(self):
        request, decision, context = self.approved_catalog()
        original = self.recount_payload(request, decision, context)
        missing = deepcopy(original);missing.pop("counts_by_pool")
        self.assertEqual(self.post(missing).status_code, 400)
        for invalid in (True, -1, 0, "8"):
            payload = deepcopy(original)
            payload["operation_id"] = str(uuid.uuid4())
            next(iter(payload["counts_by_pool"].values()))["total"] = invalid
            self.assertEqual(self.post(payload).status_code, 409)
        self.variant.refresh_from_db();self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        self.client_row.privacy_erasure_started_at = self.now
        self.client_row.save(update_fields=["privacy_erasure_started_at"])
        self.assertEqual(self.post(original).status_code, 410)

    def test_mixed_full_request_recounts_only_catalog_pools_without_dropping_custom_line(self):
        added = self.canonical("добавьте Storm, количество 1, размер L, цвет purple, крій classic")
        self.revision_row, _claim = self.revision(added)
        request, _original, context = self.produced_request()
        items = deepcopy(context["editable_items"])
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["item_kind"], "catalog_apparel")
        self.assertEqual(items[1]["item_kind"], "custom_apparel")
        for index, item in enumerate(items):
            item.update(availability="ready", checked_quantity=1,
                supply_basis="warehouse_checked" if index == 0 else "accessible_unrecorded")
            if index:
                item.update(title="Худі Storm · фіолетове", color="purple", size="L", unit_price="1500.00")
            item["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed",
                "answer": "Менеджер перевірив точні параметри цієї позиції."}
                for question in request.envelope["snapshot"]["questions"] if question["line_id"] == item["line_id"]]
        draft = self.post({"action": "draft", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
            **context["cas"], "items": items})
        self.assertEqual(draft.status_code, 200, draft.content)
        approved = self.post({"action": "approve", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
            **draft.json()["commerce"]["cas"]})
        self.assertEqual(approved.status_code, 200, approved.content)
        decision = Decision.objects.get(pk=approved.json()["decision_id"])
        before = (deepcopy(request.envelope), deepcopy(decision.envelope))
        commerce = approved.json()["commerce"]
        pool_lines = {line for pool in commerce["stock_sync"]["pools"] for line in pool["line_ids"]}
        self.assertEqual(pool_lines, {items[0]["line_id"]})
        payload = self.recount_payload(request, decision, commerce)
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        request.refresh_from_db();decision.refresh_from_db()
        self.assertEqual((request.envelope, decision.envelope), before)
        after = response.json()["commerce"]
        self.assertEqual([item["line_id"] for item in after["approved_head"]["items"]], [item["line_id"] for item in items])
        self.assertEqual(after["approved_head"]["items"][1]["item_kind"], "custom_apparel")
        self.assertFalse(Execution.objects.exists())
