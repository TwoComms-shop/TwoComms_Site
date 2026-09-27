from copy import deepcopy
from types import SimpleNamespace
from django.test import SimpleTestCase
from management.services.ig_journey_consent import append_consent_context


class JourneyConsentTests(SimpleTestCase):
    def test_received_order_is_readiness_not_marketing_consent(self):
        graph = {"nodes": [
            {"id": "client-order:7", "semantic_key": "client_order_context",
             "fulfillment_progress": {"step": 4, "evidence_refs": [{"kind": "order", "id": 7}]}},
            {"id": "contact", "semantic_key": "client_order_contact",
             "contextual_binding": {"order_id": 7}}],
            "edges": []}
        original = deepcopy(graph)
        result = append_consent_context(graph, client=SimpleNamespace(opted_in_at="manual"), is_history=False)
        self.assertEqual(graph, original)
        progress = result["nodes"][1]["consent_progress"]
        self.assertEqual(progress["delivery"]["status"], "received")
        self.assertEqual(progress["permission"]["status"], "unconfirmed")
        self.assertEqual(progress["invitation"]["status"], "unavailable")
        self.assertEqual(progress["response"]["status"], "unknown")
        self.assertEqual(append_consent_context(result, client=SimpleNamespace(), is_history=False), result)
        self.assertEqual(append_consent_context(graph, client=SimpleNamespace(), is_history=True), graph)

    def test_global_opt_out_does_not_claim_declined_invitation(self):
        result = append_consent_context({"nodes": [], "edges": []},
            client=SimpleNamespace(opted_out_at="now", opt_out_message_id=17), is_history=False)
        progress = result["marketing_consent"]
        self.assertEqual(progress["permission"]["status"], "blocked")
        self.assertEqual(progress["permission"]["evidence_refs"], [{"kind": "message", "id": 17}])
        self.assertEqual(progress["response"]["status"], "unknown")
