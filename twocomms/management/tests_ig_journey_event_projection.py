"""Offer history is source-bound, bounded and never payment authority."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.db import DatabaseError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from management.models import (
    IgCheckoutProposal, IgCheckoutRevision, IgClient, IgCommercialEpisode,
    IgCommercialEpisodeEvent, IgDeal, InstagramBotMessage,
)
from management.services.ig_journey_event_projection import append_offer_transitions


class OfferTransitionProjectionTests(TestCase):
    def setUp(self):
        self.buyer = IgClient.get_or_create_for_sender("offer-projection-buyer")
        self.other = IgClient.get_or_create_for_sender("offer-projection-other")
        self.episode = IgCommercialEpisode.objects.create(
            client=self.buyer, sequence=1, open_slot=1, materialization_key="offer-projection:1",
        )
        self.deal = IgDeal.objects.create(client=self.buyer, amount="790.00", requested_payment_amount="790.00")
        self.proposal = IgCheckoutProposal.objects.create(
            client=self.buyer, deal=self.deal, commercial_episode=self.episode,
            catalog_total="790.00", quoted_total="790.00", requested_payment_amount="790.00", items_digest="a" * 64,
        )
        self.message = InstagramBotMessage.objects.create(client=self.buyer, sender_id="offer-projection-buyer", role="user")
        self.graph = {
            "nodes": [{"id": f"episode:{self.episode.pk}", "state": "partial"},
                      {"id": "guide:terms", "semantic_key": "quoted_offer", "state": "open", "current": False,
                       "facts": [], "evidence_refs": []}],
            "edges": [], "overview_node_ids": ["guide:terms"],
            "coverage": {"semantic_transitions": "missing_source", "milestones": "bounded"},
        }

    def event(self, revision_number=1, *, message_ids=None, proposal=None, changes=None, revision_changes=None):
        message_ids = [self.message.pk] if message_ids is None else message_ids
        proposal = proposal or self.proposal
        values = dict(proposal=proposal, revision=revision_number, digest="a" * 64,
                      snapshot={"digest": "a" * 64, "private": "PRIVATE SNAPSHOT"}, source="bot_create",
                      evidence_message_ids=message_ids, source_watermark_message_id=max(message_ids, default=0))
        values.update(revision_changes or {})
        revision = IgCheckoutRevision.objects.create(**values)
        evidence = {"schema_version": 1, "from_node": "configured_line", "to_node": "quoted_offer",
                    "trigger": "validated_offer_created", "proposal_id": proposal.pk,
                    "checkout_revision_id": revision.pk, "revision": revision_number, "digest": "a" * 64,
                    "evidence_message_ids": message_ids, "authority": "validated_checkout_revision"}
        evidence.update(changes or {})
        return IgCommercialEpisodeEvent.objects.create(
            episode=self.episode, event_type="semantic_transition", source="checkout_revision",
            dedupe_key=f"journey:checkout-revision:{revision.pk}:validated-offer", evidence=evidence,
        )

    def project(self, graph=None):
        return append_offer_transitions(graph or self.graph, client_id=self.buyer.pk, episode_id=self.episode.pk)

    def test_historical_revision_remains_visible_after_current_proposal_changes(self):
        event = self.event()
        self.proposal.revision = 2
        self.proposal.items_digest = "b" * 64
        self.proposal.save(update_fields=["revision", "items_digest"])
        original = deepcopy(self.graph)
        result = self.project()
        self.assertEqual(original, self.graph)
        self.assertEqual(result["coverage"]["offer_transitions"]["verified"], 1)
        self.assertEqual(result["edges"][0]["event_ids"], [f"episode_event:{event.pk}"])
        self.assertEqual(result["edges"][0]["to_node_id"], "guide:terms")
        for node in result["nodes"][1:]:
            self.assertEqual(node["state"], "partial")
            self.assertFalse(node["current"])
            self.assertEqual(node["recorded_visits"]["count"], 1)
        rendered = json.dumps(result)
        self.assertNotIn("PRIVATE SNAPSHOT", rendered)
        self.assertNotIn("a" * 64, rendered)

    def test_real_snapshot_links_verified_edge_to_its_history(self):
        from management.services.ig_journey_snapshot import build_journey_snapshot
        event = self.event()
        snapshot = build_journey_snapshot(self.buyer)
        graph = snapshot["graph"]
        edge = next(item for item in graph["edges"] if item["relation"] == "semantic_transition")
        self.assertEqual(edge["event_ids"], [f"episode_event:{event.pk}"])
        self.assertTrue(set(edge["event_ids"]).issubset({item["id"] for item in graph["history"]["events"]}))
        self.assertEqual(graph["coverage"]["semantic_transitions"], "partial")
        self.assertTrue(edge["reason_label"])
        self.assertTrue(all(node["state"] != "complete" for node in graph["nodes"]))

    def test_foreign_episode_foreign_or_deleted_message_and_digest_tamper_are_rejected(self):
        other_episode = IgCommercialEpisode.objects.create(
            client=self.buyer, sequence=2, open_slot=None, materialization_key="offer-projection:2",
        )
        other_proposal = IgCheckoutProposal.objects.create(
            client=self.buyer, deal=self.deal, commercial_episode=other_episode,
            catalog_total="790.00", quoted_total="790.00", requested_payment_amount="790.00", items_digest="a" * 64,
        )
        self.event(proposal=other_proposal)
        foreign = InstagramBotMessage.objects.create(client=self.other, sender_id="offer-projection-other", role="user")
        self.event(2, message_ids=[foreign.pk])
        self.event(3, message_ids=[99999999])
        self.event(4, revision_changes={"snapshot": {"digest": "b" * 64}})
        self.event(5, changes={"digest": "b" * 64})
        self.event(6, revision_changes={"evidence_message_ids": [True]})
        result = self.project()
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["coverage"]["offer_transitions"]["rejected"], 6)

    def test_empty_malformed_and_optional_table_failure_do_not_invent_edges(self):
        self.assertEqual(self.project()["edges"], [])
        self.event(changes={"schema_version": True})
        self.event(2, changes={"payment_confirmed": True})
        self.assertEqual(self.project()["coverage"]["offer_transitions"]["rejected"], 2)
        with patch("management.services.ig_journey_event_projection._read", side_effect=DatabaseError("optional table")):
            result = self.project()
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["coverage"]["offer_transitions"]["status"], "unavailable")

    def test_query_bound_aggregate_and_idempotent_presentation(self):
        for number in range(1, 35):
            self.event(number)
        with CaptureQueriesContext(connection) as queries:
            result = self.project()
        self.assertLessEqual(len(queries), 3)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        self.assertTrue(all("COUNT(" not in row["sql"].upper() for row in queries))
        self.assertEqual(len(result["edges"]), 1)
        self.assertEqual(result["edges"][0]["repeated_count"], 32)
        self.assertTrue(result["coverage"]["offer_transitions"]["truncated"])
        self.assertEqual(self.project(result), result)

    def test_reference_overflow_and_graph_scope_are_fail_closed(self):
        messages = [InstagramBotMessage.objects.create(client=self.buyer, sender_id="offer-projection-buyer", role="user").pk
                    for _ in range(40)]
        for number in range(1, 9):
            self.event(number, message_ids=messages)
        result = self.project()
        self.assertEqual(result["coverage"]["offer_transitions"]["verified"], 5)
        self.assertTrue(result["coverage"]["offer_transitions"]["truncated"])
        wrong_graph = deepcopy(self.graph)
        wrong_graph["nodes"][0]["id"] = "episode:999999"
        with self.assertNumQueries(0):
            wrong = self.project(wrong_graph)
        self.assertEqual(wrong["edges"], [])
        self.assertEqual(wrong["coverage"]["offer_transitions"]["status"], "invalid_scope")

    def test_existing_proved_nodes_visits_and_other_edges_are_preserved(self):
        self.event()
        graph = deepcopy(self.graph)
        graph["nodes"][1].update(state="complete", current=True, recorded_visits={"count": 9})
        graph["edges"] = [{"id": "route:1", "relation": "conversation_focus"}]
        graph["coverage"]["semantic_transitions"] = "partial"
        result = self.project(graph)
        self.assertEqual(result["nodes"][1]["state"], "complete")
        self.assertTrue(result["nodes"][1]["current"])
        self.assertEqual(result["nodes"][1]["recorded_visits"], {"count": 9})
        self.assertEqual(result["edges"][0], graph["edges"][0])
        self.assertEqual(result["coverage"]["semantic_transitions"], "partial")
