"""Existing case records project without creating history or business effects."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import (
    IgClient, IgCommercialEpisode, IgCommercialEpisodeEvent, IgFollowUpTask,
    IgFunnelResetAudit, IgOrderAssignment, IgPostSaleCase, InstagramBotMessage,
)
from management.services.ig_journey_cases import append_case_context, _boundary, CASE_LIMIT
from management.services.ig_prize_cases import CASE_SCHEMA_VERSION
from management.services.ig_journey_snapshot import build_journey_snapshot
from management.services.ig_order_assignments import link_order_to_client
from orders.models import Order


class JourneyCaseRecordTests(TestCase):
    def setUp(self):
        self.buyer = IgClient.objects.create(igsid="journey-case-buyer")
        self.other = IgClient.objects.create(igsid="journey-case-other")

    def source(self, *, buyer=None, event_at=None):
        buyer = buyer or self.buyer
        return InstagramBotMessage.objects.create(client=buyer, sender_id=buyer.igsid,
            role="user", source="webhook", provider_created_at=event_at or timezone.now(),
            private_media_state="active", media_capture_eligible=True,
            attachment_media=[{"source_part_id": "part", "content_hash": "a" * 64,
                "status": "owned", "private_storage": True, "storage_name": "private/test.jpg",
                "mime": "image/jpeg", "inspection": {"state": "inspected", "source_part_id": "part",
                    "content_hash": "a" * 64, "request_id": "case-request", "provider_model": "case-model"}}])

    def prize(self, source=None, **changes):
        source = source or self.source()
        programme = {"programme_id": "test-prize", "programme_version": "v1"}
        context = {"schema_version": CASE_SCHEMA_VERSION, **programme,
            "evidence": [{"source_message_id": source.pk, "source_part_id": "part",
                "content_hash": "a" * 64, "type_code": "certificate", "request_id": "case-request",
                "provider_model": "case-model", **programme}],
            "authority": {"entitlement": "unconfirmed"}}
        values = dict(client=self.buyer, kind="manager_task", reason="prize_review:test-prize",
            due_at=timezone.now(), status="skipped", manager_context=context,
            manager_approval_status="pending", event_payload={"schema_version": CASE_SCHEMA_VERSION,
                "case_kind": "prize_review", "initial_source_message_id": source.pk, **programme})
        values.update(changes)
        return IgFollowUpTask.objects.create(**values)

    def service(self, source=None, **changes):
        values = dict(client=self.buyer, source_message=source or self.source(), case_type="exchange")
        values.update(changes)
        return IgPostSaleCase.objects.create(**values)

    def graph(self):
        return {"nodes": [], "edges": [], "coverage": {}}

    def test_two_prize_cases_keep_identity_and_task_approval_never_grants_entitlement(self):
        first = self.prize(manager_approval_status="approved", status="completed")
        second = self.prize(manager_approval_status="rejected")
        graph = self.graph()
        with CaptureQueriesContext(connection) as queries:
            result = append_case_context(graph, client_id=self.buyer.pk)
        self.assertEqual(graph, self.graph())
        self.assertEqual({n["id"] for n in result["nodes"]}, {f"prize-case:{first.pk}", f"prize-case:{second.pk}"})
        self.assertTrue(all(n["case_record"]["entitlement"] == "unconfirmed" for n in result["nodes"]))
        self.assertTrue(all(n["semantic_key"] == "prize_candidate" and n["state"] == "partial" for n in result["nodes"]))
        self.assertEqual(result["edges"], [])
        self.assertLessEqual(len(queries), 9)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries))

    def test_empty_read_cost_is_two_selects(self):
        with CaptureQueriesContext(connection) as queries:
            result = append_case_context(self.graph(), client_id=self.buyer.pk)
        self.assertEqual(len(queries), 2)
        self.assertEqual(result["coverage"]["case_records"]["status"], "missing_source")

    def test_foreign_or_changed_prize_source_has_named_unknown_coverage(self):
        self.prize(self.source(buyer=self.other))
        source = self.source()
        self.prize(source)
        source.attachment_media = []
        source.save(update_fields=["attachment_media"])
        result = append_case_context(self.graph(), client_id=self.buyer.pk)
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["coverage"]["case_records"]["rejected"], 2)
        self.assertIn("prize_media_changed", result["coverage"]["case_records"]["reasons"])

    def test_new_database_id_of_old_provider_event_cannot_cross_reset(self):
        reset = IgFunnelResetAudit.objects.create(client=self.buyer, reset_after_message_id=0, reason="test")
        old = self.source(event_at=reset.created_at - timedelta(days=1))
        self.prize(old)
        result = append_case_context(self.graph(), client_id=self.buyer.pk)
        self.assertEqual(result["nodes"], [])
        self.assertIn("source_unavailable_or_reset", result["coverage"]["case_records"]["reasons"])

    def test_unproven_inspection_cannot_make_a_validated_prize_case(self):
        source = self.source()
        source.attachment_media[0]["inspection"]["request_id"] = "different-request"
        source.save(update_fields=["attachment_media"])
        self.prize(source)
        result = append_case_context(self.graph(), client_id=self.buyer.pk)
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["coverage"]["case_records"]["reasons"], {"prize_inspection_unproven": 1})

    def test_erasure_or_source_change_during_read_abstains(self):
        self.prize()
        original = _boundary(self.buyer.pk)
        changed = (dict(original[0], privacy_erasure_started_at=timezone.now()), original[1])
        with patch("management.services.ig_journey_cases._boundary", side_effect=[original, changed]):
            result = append_case_context(self.graph(), client_id=self.buyer.pk)
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["coverage"]["case_records"]["status"], "unknown")

    def test_service_without_order_stays_source_bound_without_money_or_shipping_truth(self):
        service = self.service(status="completed")
        result = append_case_context(self.graph(), client_id=self.buyer.pk)
        node = result["nodes"][0]
        self.assertEqual(node["id"], f"service-case:{service.pk}")
        self.assertEqual(node["case_record"]["financial_outcome"], "unknown")
        self.assertIsNone(node["contextual_binding"]["order_id"])
        self.assertNotIn("fulfillment_progress", node)
        self.assertEqual(result["edges"], [])

    def test_service_two_orders_never_attach_to_neighbour_order(self):
        orders = [Order.objects.create(order_number=f"CASE-{n}", full_name="Test", phone="0", total_sum="0") for n in range(2)]
        for order in orders:
            link_order_to_client(order, client=self.buyer, source="manager_manual")
            self.service(order=order)
        graph = self.graph()
        graph["nodes"] = [{"id": f"order:{order.pk}", "semantic_key": "client_order_context",
                            "evidence_refs": [{"kind": "order", "id": order.pk}]} for order in orders]
        result = append_case_context(graph, client_id=self.buyer.pk)
        by_id = {n["id"]: n for n in result["nodes"]}
        self.assertEqual(len(result["edges"]), 2)
        for edge in result["edges"]:
            self.assertEqual(edge["relation"], "case_record_context")
            self.assertEqual(edge["from_node_id"], "order:" + str(by_id[edge["to_node_id"]]["contextual_binding"]["order_id"]))

    def test_foreign_episode_or_unverified_order_is_not_projected(self):
        order = Order.objects.create(order_number="UNBOUND-CASE", full_name="Test", phone="0", total_sum="0")
        self.service(order=order)
        episode = IgCommercialEpisode.objects.create(client=self.other, sequence=1, materialization_key="foreign-case")
        self.service(commercial_episode=episode)
        self.assertEqual(append_case_context(self.graph(), client_id=self.buyer.pk)["nodes"], [])

    def test_history_is_explicit_unknown_and_never_reads_or_creates_records(self):
        self.prize()
        with CaptureQueriesContext(connection) as queries:
            result = append_case_context(self.graph(), client_id=self.buyer.pk, is_history=True)
        self.assertEqual(len(queries), 0)
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["coverage"]["case_records"]["reasons"], {"historical_projection_unverified": 1})

    def test_case_scan_is_bounded_with_visible_truncation(self):
        source = self.source()
        for _ in range(CASE_LIMIT + 1):
            self.prize(source)
        result = append_case_context(self.graph(), client_id=self.buyer.pk)
        self.assertEqual(len(result["nodes"]), CASE_LIMIT)
        self.assertTrue(result["coverage"]["case_records"]["truncated"])

    def test_partial_transcript_not_hidden_by_real_business_edge(self):
        episode = IgCommercialEpisode.objects.create(client=self.buyer, sequence=1,
            open_slot=1, materialization_key="partial-trace-business")
        source = self.source()
        IgCommercialEpisodeEvent.objects.create(episode=episode, dedupe_key="case-business-edge",
            event_type="stage_transition", from_state="qualifying", to_state="checkout", stage="checkout",
            source="repeat_intent", evidence={"message_ids": [source.pk]})

        def partial(graph, **kwargs):
            graph = deepcopy(graph)
            graph["coverage"]["transcript_reconstruction"] = "partial"
            graph["transcript_reconstruction"] = {"freshness": "current",
                "coverage": {"reasons": {"quote_mismatch": 1}, "accepted": 1, "omitted": 1}}
            return graph

        with patch("management.services.ig_journey_trace_projection.append_journey_trace", side_effect=partial):
            graph = build_journey_snapshot(self.buyer)["graph"]
        self.assertEqual(graph["coverage"]["semantic_path"]["state"], "partial")
        self.assertEqual(graph["coverage"]["semantic_path"]["reason"], "trace_partial")
        self.assertEqual(graph["coverage"]["business_transitions"]["edge_count"], 1)
        self.assertEqual(graph["coverage"]["transcript_sources"]["reasons"], {"quote_mismatch": 1})
