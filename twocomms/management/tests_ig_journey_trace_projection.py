"""A cited discussion trail remains separate from authoritative route facts."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from unittest.mock import patch

from django.db import connection, DatabaseError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import IgClient, IgCommercialEpisode, IgFunnelResetAudit, IgJourneyTraceSnapshot, InstagramBotMessage
from management.services.ig_journey_snapshot import build_journey_snapshot
from management.services.ig_journey_trace_contract import normalize_journey_trace
from management.services.ig_journey_trace_projection import append_journey_trace
from management.services.ig_journey_trace_store import record_journey_trace


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


class JourneyTraceProjectionTests(TestCase):
    def setUp(self):
        self.client = IgClient.get_or_create_for_sender("trace-map-buyer")
        self.other = IgClient.get_or_create_for_sender("trace-map-other")
        self.message = InstagramBotMessage.objects.create(client=self.client, sender_id="trace-map-buyer", role="user", text="PRIVATE: хочу худі і зачекаю")
        self.manager = InstagramBotMessage.objects.create(client=self.client, sender_id="trace-map-buyer", role="manager", text="PRIVATE: чекаємо наявність")
        self.by_id = {m.pk: {"message_id": m.pk, "role": m.role, "text": m.text} for m in (self.message, self.manager)}
        self.graph = {"schema_version": 1, "nodes": [{"id": "guide:inquiry", "semantic_key": "inbound", "label": "Звернення", "state": "partial", "current": True, "facts": []}], "edges": [], "coverage": {"semantic_transitions": "missing_source"}}

    def step(self, origin, target, kind="progress", reason="entered", message=None):
        source = message or self.message
        return {"from_node": origin, "to_node": target, "kind": kind, "reason_code": reason, "confidence": .9,
                "evidence": [{"message_id": source.pk, "quote": source.text}]}

    def save(self, steps=None, current="stock_wait", episode_id=None):
        steps = steps or [self.step("inbound", "prize_candidate", reason="certificate_presented"), self.step("prize_candidate", "stock_wait", "waiting", "awaiting_stock", self.manager)]
        trace = normalize_journey_trace({"schema_version": 1, "steps": steps, "current_node": current}, by_id=self.by_id, watermark=self.manager.pk)
        return record_journey_trace(client_id=self.client.pk, episode_id=episode_id, watermark=self.manager.pk,
            normalized_trace=trace, by_id=self.by_id, prompt_version="journey-trace.v1", analysis_model="test", analyzed_at=timezone.now())

    def project(self, **kwargs):
        return append_journey_trace(self.graph, client_id=self.client.pk, **kwargs)

    def clone(self, row, **changes):
        values = {field.attname: getattr(row, field.attname) for field in row._meta.concrete_fields if field.name not in {"id", "created_at"}}
        values.update(changes)
        values["snapshot_key"] = digest({"client_id": values["client_id"], "episode_id": values["commercial_episode_id"], "watermark": values["watermark_message_id"], "source_digest": values["source_digest"], "prompt_version": values["prompt_version"], "schema_version": values["schema_version"]})
        return IgJourneyTraceSnapshot.objects.create(**values)

    def test_current_trail_reuses_nodes_and_has_no_authority_or_recorded_visits(self):
        self.save()
        with CaptureQueriesContext(connection) as queries:
            graph = self.project()
        self.assertEqual(len(queries), 5)
        self.assertEqual(len(graph["nodes"]), 3)
        self.assertEqual(len(graph["edges"]), 2)
        self.assertEqual(graph["coverage"]["semantic_transitions"], "missing_source")
        self.assertTrue(graph["nodes"][-1]["interpreted_focus"])
        for node in graph["nodes"]:
            self.assertNotIn("recorded_visits", node)
            self.assertNotEqual(node["state"], "complete")
            self.assertEqual(node["facts"], [])
            self.assertIsNone(node["transcript_interpretation"]["episode_id"])
        waiting = graph["nodes"][-1]
        self.assertEqual(waiting["implementation_status"], "planned")
        self.assertEqual(waiting["waiting"]["kind"], "indefinite")
        self.assertEqual(waiting["timers"], [])
        self.assertNotIn("PRIVATE", json.dumps(graph))
        self.assertEqual(graph["edges"][-1]["evidence_refs"][0]["role"], "manager")
        self.assertEqual(self.graph["nodes"][0].get("transcript_interpretation"), None)

    def test_freshness_for_every_role_and_foreign_new_message_is_irrelevant(self):
        self.save()
        InstagramBotMessage.objects.create(client=self.other, sender_id="trace-map-other", role="user", text="foreign")
        self.assertIn("transcript_reconstruction", self.project())
        for role in ("user", "manager", "model"):
            latest = InstagramBotMessage.objects.create(client=self.client, sender_id="trace-map-buyer", role=role, text="new")
            graph = self.project()
            self.assertEqual(graph["transcript_reconstruction"]["freshness"], "new_messages")
            self.assertEqual(len(graph["edges"]), 2)
            self.assertFalse(any(node.get("interpreted_focus") for node in graph["nodes"]))
            latest.delete()

    def test_cited_full_source_and_role_are_rechecked_without_old_row_fallback(self):
        row = self.save()
        for changes in ({"text": "edited"}, {"role": "model"}, {"client_id": self.other.pk}):
            InstagramBotMessage.objects.filter(pk=self.message.pk).update(**changes)
            self.assertEqual(self.project()["coverage"]["transcript_reconstruction"], "source_rejected")
            InstagramBotMessage.objects.filter(pk=self.message.pk).update(text=self.message.text, role=self.message.role, client_id=self.client.pk)
        self.clone(row, prompt_version="journey-trace.v2", trace_digest="0" * 64)
        self.assertEqual(self.project()["coverage"]["transcript_reconstruction"], "schema_rejected")

    def test_privacy_guard_reads_current_database_state(self):
        self.save()
        for field in ("hidden_at", "privacy_erasure_started_at"):
            IgClient.objects.filter(pk=self.client.pk).update(**{field: timezone.now()})
            self.assertEqual(self.project()["coverage"]["transcript_reconstruction"], "client_unavailable")
            IgClient.objects.filter(pk=self.client.pk).update(**{field: None})

    def test_reset_invalidates_both_cited_boundary_and_snapshot_age(self):
        row = self.save()
        reset = IgFunnelResetAudit.objects.create(client=self.client, reset_after_message_id=0, reason="new run")
        self.assertEqual(self.project()["coverage"]["transcript_reconstruction"], "reset_boundary")
        IgFunnelResetAudit.objects.filter(pk=reset.pk).update(created_at=row.analyzed_at - timedelta(seconds=1), reset_after_message_id=self.message.pk)
        self.assertEqual(self.project()["coverage"]["transcript_reconstruction"], "reset_boundary")

    def test_client_scope_never_inherits_into_history_and_episode_scope_is_exact(self):
        episode = IgCommercialEpisode.objects.create(client=self.client, sequence=1, materialization_key="trace-map-episode")
        other_episode = IgCommercialEpisode.objects.create(client=self.other, sequence=1, materialization_key="trace-map-foreign")
        self.save()
        self.assertNotIn("transcript_reconstruction", self.project(episode_id=episode.pk, is_history=True))
        self.assertEqual(self.project(episode_id=other_episode.pk)["coverage"]["transcript_reconstruction"], "scope_rejected")
        current = self.project(episode_id=episode.pk)
        self.assertEqual(current["transcript_reconstruction"]["scope"], "client")
        self.save(episode_id=episode.pk)
        self.assertEqual(self.project(episode_id=episode.pk, is_history=True)["transcript_reconstruction"]["scope"], "episode")

    def test_partial_gaps_and_return_retry_objection_are_not_invented_connections(self):
        steps = [self.step("inbound", "configured_line"), self.step("configured_line", "quoted_offer"),
            self.step("payment_help", "awaiting_payment", "retry", "payment_problem"),
            self.step("awaiting_payment", "quoted_offer", "return", "changed_request"),
            self.step("quoted_offer", "objection_case", "objection", "objection_raised"),
            self.step("quoted_offer", "objection_case", "objection", "objection_raised")]
        self.save(steps, current="objection_case")
        graph = self.project()
        self.assertGreater(graph["transcript_reconstruction"]["coverage"]["disconnected_steps"], 0)
        pairs = {(edge["from_node_id"], edge["to_node_id"]) for edge in graph["edges"]}
        self.assertNotIn(("trace:quoted_offer", "trace:payment_help"), pairs)
        self.assertEqual([edge["tone"] for edge in graph["edges"]][2:4], ["warning", "danger"])
        self.assertEqual(graph["edges"][-1]["repeated_count"], 2)

    def test_financial_discussion_and_consent_never_complete_or_override_typed_facts(self):
        self.save([self.step("quoted_offer", "settlement", reason="payment_discussed"), self.step("settlement", "channel_grant_checked", reason="consent_discussed")], current="settlement")
        interpreted = self.project()
        self.assertTrue(all(node["state"] != "complete" for node in interpreted["nodes"]))
        self.assertEqual(next(n for n in interpreted["nodes"] if n["semantic_key"] == "settlement")["label"], "Обговорення розрахунку")
        confirmed = {"id": "guide:payment", "semantic_key": "settlement", "label": "Оплата", "state": "complete", "current": True, "facts": [{"id": "verified", "value": "paid", "source": "intended_order.current"}], "recorded_visits": {"count": 1}}
        self.graph["nodes"].append(deepcopy(confirmed))
        graph = self.project()
        actual = next(node for node in graph["nodes"] if node["id"] == "guide:payment")
        self.assertEqual(actual["facts"], confirmed["facts"])
        self.assertTrue(actual["current"])
        self.assertEqual(actual["state"], "complete")
        self.assertNotIn("interpreted_focus", actual)
        self.assertEqual(len([n for n in graph["nodes"] if n["semantic_key"] == "settlement"]), 1)

    def test_accepted_conversation_focus_wins_and_snapshot_exposes_distinct_source(self):
        self.save()
        self.graph["nodes"].append({"id": "conversation_intent:employment:", "semantic_key": "conversation_intent", "label": "Робота", "current": True, "route_focus": True, "facts": []})
        self.assertFalse(any(node.get("interpreted_focus") for node in self.project()["nodes"]))
        snapshot = build_journey_snapshot(self.client)
        self.assertIn("source_verified_transcript_reconstruction", snapshot["covered_sources"])
        self.assertEqual(snapshot["graph"]["coverage"]["semantic_transitions"], "missing_source")
        self.assertEqual(snapshot["focus"]["node_id"], "inquiry")

    def test_old_milestone_does_not_override_fresh_discussion_focus(self):
        self.save()
        self.graph["nodes"].append({"id": "guide:selection", "semantic_key": "catalog_discovery", "state": "partial", "current": True,
            "recorded_visits": {"count": 2}, "facts": [{"id": "milestone:1", "value": "2020-01-01"}]})
        graph = self.project()
        self.assertEqual([node["id"] for node in graph["nodes"] if node.get("current")], ["trace:stock_wait"])
        self.assertEqual(next(n for n in graph["nodes"] if n["id"] == "guide:selection")["recorded_visits"], {"count": 2})

    def test_optional_table_unavailable_does_not_break_existing_graph(self):
        with patch("management.services.ig_journey_trace_projection._read", side_effect=DatabaseError("private SQL")):
            graph = self.project()
        self.assertEqual(graph["nodes"], self.graph["nodes"])
        self.assertEqual(graph["coverage"]["transcript_reconstruction"], "unavailable")
        self.assertNotIn("private", json.dumps(graph))

    def test_short_discussion_summaries_survive_without_merging_distinct_return_reasons(self):
        first = self.step("quoted_offer", "configured_line", "return", "changed_request")
        second = self.step("quoted_offer", "configured_line", "return", "changed_request")
        first["summary"] = "Клієнт уточнив розмір."
        second["summary"] = "Потрібен інший колір."
        self.save([first, second], current="configured_line")
        graph = self.project()
        self.assertEqual([edge["summary"] for edge in graph["edges"]], [first["summary"], second["summary"]])
        self.assertEqual(next(node for node in graph["nodes"] if node["semantic_key"] == "configured_line")["summary"], second["summary"])
        self.assertEqual(len({edge["id"] for edge in graph["edges"]}), 2)
        self.assertEqual([edge["last_step_index"] for edge in graph["edges"]], [0, 1])

    def test_last_step_order_tracks_returns_without_changing_stored_trace_or_facts(self):
        steps = [self.step("inbound", "configured_line"), self.step("configured_line", "quoted_offer"),
            self.step("quoted_offer", "configured_line", "return", "changed_request"),
            self.step("configured_line", "payment_help"), self.step("payment_help", "configured_line", "return", "changed_request"),
            self.step("configured_line", "objection_case"), self.step("objection_case", "configured_line", "return", "changed_request"),
            self.step("quoted_offer", "configured_line", "return", "changed_request")]
        saved = self.save(steps, current="configured_line")
        original = deepcopy(saved.trace)
        graph = self.project()
        last = next(edge for edge in graph["edges"] if edge["from_node_id"] == "trace:quoted_offer" and edge["to_node_id"] == "trace:configured_line")
        self.assertEqual((last["last_step_index"], last["repeated_count"]), (7, 2))
        focus = next(node for node in graph["nodes"] if node.get("interpreted_focus"))
        self.assertEqual(focus["transcript_interpretation"]["last_step_index"], 7)
        self.assertEqual(focus["facts"], [])
        saved.refresh_from_db()
        self.assertEqual(saved.trace, original)

    def test_stock_revisits_are_amber_without_reclassifying_original_steps(self):
        steps = [self.step("inbound", "availability_question"),
            self.step("availability_question", "stock_wait", "waiting", "awaiting_stock"),
            self.step("stock_wait", "availability_question", "progress", "awaiting_stock"),
            self.step("availability_question", "stock_wait", "waiting", "awaiting_stock"),
            self.step("stock_wait", "availability_question", "negative", "changed_request")]
        saved = self.save(steps, current="stock_wait")
        original = deepcopy(saved.trace)
        graph = self.project()
        # Identical waiting edges retain their aggregate; the later visit makes
        # that visual amber while the original payload still says "waiting".
        self.assertEqual([edge["tone"] for edge in graph["edges"]], ["recorded", "warning", "warning", "danger"])
        self.assertEqual([edge["interpretation_kind"] for edge in graph["edges"]], ["progress", "waiting", "progress", "negative"])
        self.assertEqual(graph["edges"][1]["repeated_count"], 2)
        self.assertEqual(graph["edges"][2]["reason_label"], "Очікування наявності")
        self.assertTrue(all(edge["authority"] == "none" for edge in graph["edges"]))
        saved.refresh_from_db()
        self.assertEqual(saved.trace, original)

    def test_objection_attention_is_amber_but_addressed_return_stays_blue(self):
        steps = [self.step("inbound", "fulfillment"),
            self.step("fulfillment", "objection_case", "objection", "objection_raised"),
            self.step("objection_case", "fulfillment", "progress", "objection_addressed"),
            self.step("fulfillment", "objection_case", "negative", "objection_raised")]
        saved = self.save(steps, current="fulfillment")
        original = deepcopy(saved.trace)
        graph = self.project()
        self.assertEqual([edge["tone"] for edge in graph["edges"]], ["recorded", "warning", "recorded", "danger"])
        self.assertTrue(all(node["state"] != "complete" for node in graph["nodes"]))
        saved.refresh_from_db()
        self.assertEqual(saved.trace, original)
