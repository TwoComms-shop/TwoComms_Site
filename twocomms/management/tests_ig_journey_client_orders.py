"""Client assignments remain separate from the selected purchase history."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.db import DatabaseError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.ig_bot_models import _ig_order_assignment_mutation_scope
from management.models import IgClient, IgOrderAssignment, IgOrderAssignmentEvent
from management.services.ig_journey_client_orders import append_client_order_context
from orders.models import Order


class JourneyClientOrdersTests(TestCase):
    def setUp(self):
        self.buyer = IgClient.get_or_create_for_sender("journey-order-owner")
        self.other = IgClient.get_or_create_for_sender("journey-order-other")
        self.graph = {"nodes": [{"id": "guide:selection", "state": "partial", "current": True}],
                      "edges": [], "overview_node_ids": ["guide:selection"],
                      "history": {"events": [], "total": 0}, "coverage": {"semantic_transitions": "missing_source"}}

    def assignment(self, *, client=None, source="web", audit=True, **order_values):
        number = Order.objects.count() + 1
        values = dict(order_number=f"JOURNEY-{number}", full_name="PRIVATE NAME", phone="PRIVATE PHONE",
                      city="PRIVATE CITY", np_office="PRIVATE ADDRESS", total_sum="790.00",
                      status="ship", payment_status="paid", source=source,
                      tracking_number=f"TTN-{number}", tracking_status_code=1)
        values.update(order_values)
        order = Order.objects.create(**values)
        with _ig_order_assignment_mutation_scope():
            assignment = IgOrderAssignment.objects.create(
                order=order, client=client or self.buyer, source="manager_manual", assigned_by_id=98765,
            )
        if audit:
            self.audit(assignment)
        return assignment

    def audit(self, assignment, **overrides):
        values = dict(assignment=assignment, order_id=assignment.order_id, to_client_id=assignment.client_id,
                      kind="linked", actor_source="management_user", actor_id=98765,
                      assignment_source=assignment.source, assignment_version=assignment.version,
                      snapshot={"assignment_version": assignment.version, "source": assignment.source,
                                "reason": "PRIVATE REASON"}, reason="PRIVATE REASON")
        values.update(overrides)
        return IgOrderAssignmentEvent.objects.create(**values)

    def project(self, graph=None, **kwargs):
        return append_client_order_context(graph or self.graph, client_id=self.buyer.pk, is_history=kwargs.pop("is_history", False), **kwargs)

    def context_nodes(self, graph):
        return [node for node in graph["nodes"] if node.get("scope") == "client"]

    def test_current_assignment_adds_context_without_changing_focus_episode_or_history(self):
        assignment = self.assignment()
        before = deepcopy(self.graph)
        with CaptureQueriesContext(connection) as queries:
            result = self.project()
        self.assertEqual(len(queries), 2)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") and "COUNT(" not in row["sql"].upper() for row in queries))
        self.assertEqual(before, self.graph)
        self.assertEqual(result["nodes"][0], before["nodes"][0])
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["history"], before["history"])
        node = self.context_nodes(result)[0]
        self.assertEqual(node["id"], f"client-order:{assignment.order_id}")
        self.assertEqual(node["label"], "Замовлення з сайту")
        self.assertIsNone(node["episode_id"])
        self.assertFalse(node["current"])
        self.assertNotIn("recorded_visits", node)
        self.assertEqual(result["coverage"]["client_orders"]["audit_verified"], 1)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(self.project(result), result)
        from management.services.ig_journey_snapshot import build_journey_snapshot
        snapshot = build_journey_snapshot(self.buyer)
        self.assertIsNone(snapshot["viewed_episode_id"])
        self.assertIn(node["id"], {n["id"] for n in snapshot["graph"]["nodes"]})
        self.assertEqual(snapshot["graph"]["coverage"]["client_orders"]["returned"], 1)

    def test_history_has_no_current_assignments_and_bound_order_is_not_duplicated(self):
        assignment = self.assignment()
        live = self.project()
        with self.assertNumQueries(0):
            historical = self.project(live, is_history=True)
        self.assertEqual(self.context_nodes(historical), [])
        self.assertEqual(historical["coverage"]["client_orders"]["status"], "historical_view")
        self.assertEqual(self.context_nodes(self.project(bound_order_id=assignment.order_id)), [])

    def test_other_clients_unlinked_and_reassigned_orders_do_not_leak(self):
        self.assignment(client=self.other)
        unlinked = self.assignment()
        reassigned = self.assignment()
        live = self.project()
        with _ig_order_assignment_mutation_scope():
            IgOrderAssignment.objects.filter(pk=unlinked.pk).update(client=None, unassigned_at=timezone.now(), version=2)
            IgOrderAssignment.objects.filter(pk=reassigned.pk).update(client=self.other, version=2)
        self.assertEqual(self.context_nodes(self.project(live)), [])
        with self.assertNumQueries(0):
            invalid = append_client_order_context(self.graph, client_id=True, is_history=False)
        self.assertEqual(invalid["coverage"]["client_orders"]["status"], "invalid_scope")

    def test_missing_stale_or_mismatched_audit_never_becomes_a_visit(self):
        missing = self.assignment(audit=False, source="manual")
        stale = self.assignment(audit=False, source="website")
        self.audit(stale, assignment_version=99)
        mismatch = self.assignment(audit=False)
        self.audit(mismatch, actor_id=99999)
        result = self.project()
        self.assertEqual(result["coverage"]["client_orders"]["returned"], 3)
        self.assertEqual(result["coverage"]["client_orders"]["audit_verified"], 0)
        self.assertEqual(result["coverage"]["client_orders"]["audit_missing"], 3)
        by_id = {node["id"]: node for node in self.context_nodes(result)}
        for assignment in (missing, stale):
            self.assertEqual(by_id[f"client-order:{assignment.order_id}"]["label"], "Пов’язане замовлення")
        self.assertTrue(all(not any(ref["kind"] == "order_assignment_event" for ref in node["evidence_refs"])
                            and "recorded_visits" not in node for node in by_id.values()))
        with patch("management.services.ig_journey_client_orders._read", side_effect=DatabaseError("optional table")):
            unavailable = self.project()
        self.assertEqual(unavailable["coverage"]["client_orders"]["status"], "unavailable")
        self.assertEqual(self.context_nodes(unavailable), [])

    def test_bounded_assignments_and_audit_queries(self):
        for _ in range(12):
            self.assignment()
        with CaptureQueriesContext(connection) as queries:
            result = self.project()
        self.assertEqual(len(queries), 2)
        self.assertEqual(len(self.context_nodes(result)), 10)
        self.assertTrue(result["coverage"]["client_orders"]["truncated"])
        self.assertEqual(result["coverage"]["client_orders"]["audit_verified"], 10)

    def test_tracking_string_and_order_done_do_not_confirm_receipt(self):
        shipped = self.assignment()
        unverified = self.assignment(status="done", tracking_status_code=1)
        delivered = self.assignment(status="done", tracking_status_code=9, tracking_terminal_at=timezone.now())
        nodes = {node["id"]: node for node in self.context_nodes(self.project())}
        for assignment in (shipped, unverified):
            node = nodes[f"client-order:{assignment.order_id}"]
            self.assertEqual(node["state"], "partial")
            tracking = next(fact for fact in node["facts"] if fact["id"].endswith(":tracking"))
            self.assertEqual(tracking["state"], "partial")
            self.assertNotIn("Підтверджено перевізником", [fact["value"] for fact in node["facts"]])
        delivered_node = nodes[f"client-order:{delivered.order_id}"]
        self.assertEqual(delivered_node["state"], "complete")
        self.assertIn("Підтверджено перевізником", [fact["value"] for fact in delivered_node["facts"]])
        self.assertFalse(delivered_node["current"])
