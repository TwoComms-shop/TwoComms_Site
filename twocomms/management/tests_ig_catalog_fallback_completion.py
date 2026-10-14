"""Actual 4xx native template replacement completes only its exact whole plan."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management import tests_ig_catalog_native_delivery as native
from management.models import IgClient, IgCustomerTurnRevision, IgFollowUpTask, IgCustomerTurn
from management.services import ig_revision_execution as execution
from management.services.ig_revision_authority import check_fact_bindings
from management.services.ig_revision_delivery import drain_group
from management.services.ig_revision_transport import build_provider_part_callback


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class CatalogFallbackCompletionTests(TransactionTestCase):
    # Reuse only the actual catalogue/source/plan producers, not the eight
    # inherited test methods or a mocked completion/receipt business result.
    reset_sequences = True
    setUp = native.CatalogNativeDeliveryTests.setUp
    response = native.CatalogNativeDeliveryTests.response
    source_row = native.CatalogNativeDeliveryTests.source_row
    seal = native.CatalogNativeDeliveryTests.seal
    prepare = native.CatalogNativeDeliveryTests.prepare
    plan = native.CatalogNativeDeliveryTests.plan

    def rejected_template_with_delivered_replacement(self, *, missing_fallback_mid=False):
        prepared, response = self.prepare()
        self.plan(prepared, response)
        physical = []

        def http(_settings, _url, *, token, data):
            physical.append(json.loads(data))
            if len(physical) == 1:
                return 400, json.dumps({"error": {"message": "Invalid template parameter", "code": 100}})
            if len(physical) == 2 and missing_fallback_mid:
                return 200, "{}"
            return 200, json.dumps({"message_id": f"replacement-mid-{self.revision.pk}-{len(physical)}"})

        transport = build_provider_part_callback(self.settings, expected_namespace=native.NAMESPACE,
            expected_recipient=self.client_row.igsid, access_token="fixture-memory-token")
        with patch("management.services.instagram_bot._provider_http", side_effect=http) as provider, \
                patch("management.services.instagram_bot._register_outgoing_message"):
            outcomes = {group: drain_group(self.revision.pk, self.revision_token, group, transport,
                fact_checker=check_fact_bindings) for group in ("template", "template_fallback", "substantive_text")}
        self.assertEqual(outcomes["template"].state, "definite_failed")
        self.assertEqual(outcomes["template_fallback"].state, "unknown" if missing_fallback_mid else "sent")
        self.assertEqual(outcomes["substantive_text"].state, "sent")
        self.assertEqual(provider.call_count, len(physical))
        effects = list(self.revision.delivery_effects.order_by("order_index", "pk"))
        self.assertTrue(all(row.provider_message_id for row in effects if row.state == "sent"))
        return effects, len(physical)

    def test_actual_missing_mid_in_activated_replacement_keeps_source_and_turn_uncompleted(self):
        effects, _count = self.rejected_template_with_delivered_replacement(missing_fallback_mid=True)
        self.assertEqual(execution._completion_decision(effects), (False, True, "delivery_unknown"))
        self.assertFalse(execution._verified_replaced_failure_ids(effects))
        with patch("management.services.instagram_bot._provider_http") as provider:
            result = execution.finalize_sent_revision_effects(self.revision.pk, execution_token=self.revision_token)
        provider.assert_not_called()
        self.assertFalse(result.completed, result)
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        self.assertNotEqual(self.revision.state, "processed")
        self.assertNotEqual(self.source.status, "done")
        self.assertNotEqual(IgCustomerTurn.objects.get(pk=self.revision.turn_id).terminal_reason, "replied")

    def test_actual_replacement_completes_revision_source_and_turn_once_without_manual_debt(self):
        effects, count = self.rejected_template_with_delivered_replacement()
        self.assertEqual(execution._completion_decision(effects), (True, False, "delivered"))
        self.assertTrue(execution._whole_sent_proof(self.revision, effects)[0])
        with patch("management.services.instagram_bot._provider_http") as provider:
            result = execution.finalize_sent_revision_effects(self.revision.pk, execution_token=self.revision_token)
        provider.assert_not_called()
        self.assertTrue(result.completed, result)
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(self.revision.state, "processed")
        self.assertEqual((self.source.status, self.source.send_state), ("done", "sent"))
        self.assertEqual(IgCustomerTurn.objects.get(pk=self.revision.turn_id).terminal_reason, "replied")
        self.assertNotIn("response_debt", self.revision.action_receipts)
        self.assertFalse(IgFollowUpTask.objects.filter(client=self.client_row,
            event_key=f"ig-revision-debt:{self.revision.pk}").exists())
        self.assertEqual(sum(row.state == "sent" for row in effects), count - 1)
        with patch("management.services.instagram_bot._provider_http") as provider:
            replay = execution.finalize_sent_revision_effects(self.revision.pk)
        provider.assert_not_called()
        self.assertTrue(replay.completed, replay)

    def test_historical_failed_parent_manager_case_is_repaired_from_same_native_receipts(self):
        from management.services.ig_response_debt import park_manual_revision

        self.rejected_template_with_delivered_replacement()
        # Materialize the historical operator disposition through its real
        # producer; no effect receipt, state or authority field is forged.
        with transaction.atomic():
            IgClient.objects.select_for_update().get(pk=self.client_row.pk)
            revision = IgCustomerTurnRevision.objects.select_for_update().get(pk=self.revision.pk)
            task = park_manual_revision(revision, "partial_definite_failure")
        self.revision.refresh_from_db()
        self.assertIn(self.revision.pk, execution.finalization_due_ids())
        with patch("management.services.instagram_bot._provider_http") as provider:
            result = execution.finalize_sent_revision_effects(self.revision.pk)
        provider.assert_not_called()
        self.assertTrue(result.completed, result)
        task.refresh_from_db()
        self.assertEqual(task.status, task.Status.COMPLETED)
        self.source.refresh_from_db()
        self.assertEqual((self.source.status, self.source.send_state), ("done", "sent"))

    def test_unrelated_or_tampered_receipts_never_cover_failed_parent(self):
        original, _count = self.rejected_template_with_delivered_replacement()
        parent_index = next(index for index, row in enumerate(original) if row.group == "template")
        fallback_index = next(index for index, row in enumerate(original) if row.group == "template_fallback"
            and isinstance(row.payload.get("message", {}).get("text"), str))
        mutations = (
            (parent_index, "state", "unknown"),
            (parent_index, "state", "provider_started"),
            (parent_index, "provider_http_status", 503),
            (parent_index, "failure_code", "transport_preflight_check_failed"),
            (fallback_index, "activation_part_index", True),
            (fallback_index, "activation_group", "substantive_text"),
            (fallback_index, "activation_failure_code", "link_rejected"),
            (fallback_index, "provider_message_id", ""),
            (fallback_index, "state", "planned"),
            (fallback_index, "source_message_id", self.source.pk + 999),
            (fallback_index, "provider_namespace", "instagram_login:other-owner"),
            (fallback_index, "authority_context_digest", "0" * 64),
            (fallback_index, "projection_digest", "0" * 64),
        )
        for index, field, value in mutations:
            with self.subTest(field=field, value=value):
                effects = deepcopy(original)
                setattr(effects[index], field, value)
                self.assertFalse(execution._completion_decision(effects)[0])
                self.assertFalse(execution._verified_replaced_failure_ids(effects))
        effects = deepcopy(original)
        effects[fallback_index].payload["message"]["text"] += " changed"
        self.assertFalse(execution._completion_decision(effects)[0])
        # Removing the fallback leaves genuine unrelated substantive SENT;
        # it cannot stand in for the rejected product presentation.
        self.assertFalse(execution._completion_decision([row for row in original if row.group != "template_fallback"])[0])

    def test_monitor_filters_real_replacement_with_two_read_queries_and_retains_corrupt_proof(self):
        from django.db.models import Prefetch
        from management.models import IgRevisionDeliveryEffect
        from management.services import ig_technical_debt as monitor

        self.rejected_template_with_delivered_replacement()
        with CaptureQueriesContext(connection) as queries:
            case = monitor._query_case(execution.incomplete_revision_deliveries(),
                reason="canonical_delivery_incomplete", scope="turn_revision", now=timezone.now(),
                limit=10, time_field="created_at", verify_replacements=True)
        self.assertEqual((case["count"], case["sample_ids"], case["has_more"]), (0, [], False))
        self.assertLessEqual(len(queries), 2)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))
        with patch("management.services.instagram_bot._provider_http") as provider:
            finalized = execution.finalize_sent_revision_effects(self.revision.pk, execution_token=self.revision_token)
        provider.assert_not_called()
        self.assertTrue(finalized.completed, finalized)
        row = IgCustomerTurnRevision.objects.select_related("client").prefetch_related(Prefetch(
            "delivery_effects", to_attr="_delivery_proof_effects",
            queryset=IgRevisionDeliveryEffect.objects.order_by("order_index", "pk"))).get(pk=self.revision.pk)
        self.assertEqual(row.state, "processed")
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "done")
        damaged = deepcopy(row)
        fallback = next(part for part in damaged._delivery_proof_effects if part.group == "template_fallback")
        fallback.projection_digest = "0" * 64
        # Corrupt the detached read, not DB authority. Processed/DONE labels
        # still cannot suppress debt when the exact receipt proof is invalid.
        with CaptureQueriesContext(connection) as queries:
            retained = monitor.filter_verified_replacement_candidates([damaged])
        self.assertEqual([item.pk for item in retained], [self.revision.pk])
        self.assertEqual(len(queries), 0)

    @override_settings(IG_TECHNICAL_DEBT_DELIVERY_PROOF_SCAN_CAP=1)
    def test_monitor_capped_proof_scan_reports_unknown_coverage_without_false_debt_total(self):
        from management.services import ig_technical_debt as monitor

        self.rejected_template_with_delivered_replacement()
        self.client_row = IgClient.objects.create(igsid="second-replacement-monitor", reply_permission_epoch=3)
        self.seal(self.source_row(text="Покажіть футболки"))
        self.rejected_template_with_delivered_replacement()
        with CaptureQueriesContext(connection) as queries:
            case = monitor._query_case(execution.incomplete_revision_deliveries(),
                reason="canonical_delivery_incomplete", scope="turn_revision", now=timezone.now(),
                limit=10, time_field="created_at", verify_replacements=True)
        self.assertEqual((case["count"], case["sample_ids"]), (0, []))
        self.assertTrue(case["has_more"])
        self.assertTrue(case["count_is_lower_bound"])
        self.assertFalse(case["coverage_complete"])
        self.assertEqual(case["coverage_reason"], "delivery_candidate_scan_capped")
        self.assertLessEqual(len(queries), 2)
        snapshot = monitor.technical_debt_snapshot()
        self.assertFalse(snapshot["coverage_complete"])
        self.assertIn("delivery_candidate_scan_capped", snapshot["coverage_reasons"])

    def test_monitor_keeps_actual_unknown_fallback_in_unknown_inventory(self):
        from management.services import ig_technical_debt as monitor

        self.rejected_template_with_delivered_replacement(missing_fallback_mid=True)
        snapshot = monitor.technical_debt_snapshot()
        unknown = next(case for case in snapshot["cases"] if case["reason"] == "canonical_delivery_unknown")
        self.assertEqual(unknown["count"], 1)
