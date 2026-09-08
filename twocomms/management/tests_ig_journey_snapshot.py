"""Truth, isolation and query budgets of the migration-0201 journey adapter."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from management.models import (
    IgClient, IgCommercialEpisode, IgCommercialEpisodeEvent, IgConversationRouteDecision,
    IgFunnelResetAudit, IgFunnelStepEvent,
    IgObjection, IgObjectionAttempt, IgPaymentConfirmationReview, InstagramBotMessage,
)
from management.services.ig_journey_snapshot import build_journey_snapshot, InvalidJourneyEpisode
from orders.models import Order


class JourneySnapshotTests(TestCase):
    def setUp(self):
        self.buyer = IgClient.get_or_create_for_sender("journey-buyer")
        self.other = IgClient.get_or_create_for_sender("journey-other")

    def episode(self, sequence=1, *, client=None, current=True, **kwargs):
        client = client or self.buyer
        return IgCommercialEpisode.objects.create(
            client=client, sequence=sequence, open_slot=1 if current else None,
            materialization_key=f"journey:{client.pk}:{sequence}", **kwargs,
        )

    def step(self, episode, event_type, *, key=None, **kwargs):
        return IgFunnelStepEvent.objects.create(
            episode=episode, event_type=event_type,
            event_key=key or f"journey:{episode.pk}:{event_type}",
            occurred_at=kwargs.pop("occurred_at", timezone.now()), **kwargs,
        )

    def route_decision(self, *, sequence, message, active_intents, transitions,
                       focus_key="", previous=None, reset_floor=None):
        reset_floor = reset_floor or 1
        return IgConversationRouteDecision.objects.create(
            client=self.buyer, revision_id=sequence, decision_key=f"journey-route:{self.buyer.pk}:{reset_floor}:{sequence}",
            sequence=sequence, reset_floor=reset_floor, watermark_message_id=message.pk,
            input_digest=(f"{sequence:064x}"[-64:]), interpretation_digest=(f"{sequence + 1000:064x}"[-64:]),
            decision_digest=(f"{sequence + 2000:064x}"[-64:]), source_binding={}, interpretation={},
            active_intents=active_intents, focus_key=focus_key, transitions=transitions,
            previous=previous, occurred_at=timezone.now(),
        )

    @staticmethod
    def nodes(snapshot):
        return {node["id"]: node for node in snapshot["nodes"]}

    def test_no_episode_is_read_only_and_uses_only_actual_inbound(self):
        self.buyer.stage = "paid"
        self.buyer.save(update_fields=["stage"])
        empty = build_journey_snapshot(self.buyer)
        self.assertIsNone(empty["focus"]["node_id"])
        message = InstagramBotMessage.objects.create(
            client=self.buyer, sender_id="journey-buyer", role="user", text="PRIVATE CUSTOMER TEXT",
        )
        with CaptureQueriesContext(connection) as queries:
            snapshot = build_journey_snapshot(self.buyer)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        # Two bounded reads also check current privacy and the latest optional trace.
        self.assertLessEqual(len(queries), 10)
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.buyer).exists())
        self.assertIsNone(snapshot["viewed_episode_id"])
        self.assertEqual(snapshot["focus"]["node_id"], "inquiry")
        self.assertEqual(self.nodes(snapshot)["inquiry"]["facts"][0]["evidence_refs"], [{"kind": "message", "id": message.pk}])
        self.assertEqual(snapshot["graph"]["overview_node_ids"], ["guide:inquiry"])
        self.assertEqual(snapshot["graph"]["nodes"][0]["id"], "guide:inquiry")
        self.assertNotIn("PRIVATE CUSTOMER TEXT", json.dumps(snapshot))
        self.assertTrue(all(node["state"] == "open" for node in snapshot["nodes"][1:]))

    def test_owned_episode_lookup_rejects_missing_cross_client_and_invalid_ids(self):
        alien = self.episode(client=self.other)
        for value in (alien.pk, 999999, "not-an-id", "1.5", True, 0, -1):
            with self.subTest(value=value), self.assertRaises(InvalidJourneyEpisode):
                build_journey_snapshot(self.buyer, view_episode_id=value)

    def test_history_isolated_from_current_choice_payment_and_stage(self):
        old = self.episode(current=False, product_snapshot=[{"title": "Перша річ", "size": "S"}])
        current = self.episode(2, product_snapshot=[{"title": "Друга річ", "size": "XL"}], payment_snapshot={
            "provider_source": "monobank_projection", "projection_id": 1,
            "provider_truth": "confirmed", "provider_confirmed_amount": "950.00",
        })
        self.step(current, "payment_confirmed", actor="provider", evidence={"provider_event_id": 1, "projection_id": 1})
        before = build_journey_snapshot(self.buyer, view_episode_id=old.pk)
        self.buyer.stage = "paid"
        self.buyer.current_size = "XXL"
        self.buyer.save(update_fields=["stage", "current_size"])
        after = build_journey_snapshot(self.buyer, view_episode_id=old.pk)
        self.assertEqual(before["revision"], after["revision"])
        self.assertTrue(after["is_history"])
        self.assertEqual(after["current_episode_id"], current.pk)
        self.assertIn("Перша річ", json.dumps(after, ensure_ascii=False))
        self.assertNotIn("Друга річ", json.dumps(after, ensure_ascii=False))
        self.assertEqual(self.nodes(after)["payment"]["facts"], [])
        self.assertIsNone(after["focus"]["node_id"])

    def test_late_paid_milestone_does_not_complete_prior_nodes_or_derive_edges(self):
        episode = self.episode(stage_snapshot={"stage": "paid"})
        self.step(episode, "payment_confirmed", actor="provider", evidence={"provider_event_id": 9, "projection_id": 8})
        snapshot = build_journey_snapshot(self.buyer)
        self.assertEqual(snapshot["focus"], {"node_id": "payment", "display_only": True})
        self.assertTrue(all(node["state"] == "open" for node in snapshot["nodes"]))
        self.assertEqual(snapshot["history"]["events"][0]["provenance"], "provider_event")
        self.assertEqual(snapshot["history"]["edges"], [])
        self.assertEqual(snapshot["history"]["visits"][0]["node_id"], "payment")
        self.assertEqual(snapshot["graph"]["history"]["events"][0]["node_id"], "guide:payment")
        payment = next(node for node in snapshot["graph"]["nodes"] if node["id"] == "guide:payment")
        self.assertEqual(payment["recorded_visits"]["count"], 1)
        self.assertEqual(payment["state"], "open")
        self.assertEqual(snapshot["graph"]["coverage"]["semantic_transitions"], "missing_source")

    def test_recorded_visits_count_only_owned_typed_events_without_mirrored_history(self):
        episode = self.episode()
        first = self.step(episode, "paylink_issued", is_backfilled=True)
        second = self.step(episode, "paylink_viewed")
        IgCommercialEpisodeEvent.objects.create(episode=episode, event_type="stage_transition")
        self.step(self.episode(client=self.other), "paylink_issued")
        snapshot = build_journey_snapshot(self.buyer)
        offer = next(node for node in snapshot["graph"]["nodes"] if node["id"] == "guide:offer")
        visits = offer["recorded_visits"]
        self.assertEqual(visits["count"], 2)
        self.assertTrue(visits["has_backfilled"])
        self.assertFalse(visits["history_truncated"])
        self.assertEqual({ref["id"] for ref in visits["evidence_refs"]}, {first.pk, second.pk})
        self.assertEqual(offer["label"], "Посилання на оплату")
        self.assertEqual(snapshot["graph"]["edges"], [])

    def test_price_quote_is_history_and_manager_is_not_provider_truth(self):
        episode = self.episode(payment_snapshot={
            "manager_truth": "manager_verified", "manager_decision_id": 7,
            "manager_confirmed_amount": "950.00", "deal_payment_truth": "confirmed",
        })
        self.step(episode, "price_quoted", evidence={"amount": "950.00"})
        self.step(episode, "payment_confirmed", actor="manager", evidence={"message_id": 1})
        snapshot = build_journey_snapshot(self.buyer)
        nodes = self.nodes(snapshot)
        self.assertEqual(nodes["terms"]["facts"], [])
        self.assertEqual(nodes["terms"]["state"], "open")
        self.assertEqual(nodes["payment"]["state"], "partial")
        self.assertNotIn("provider_truth", [fact["id"].split(":")[-1] for fact in nodes["payment"]["facts"]])
        self.assertEqual(snapshot["history"]["events"][-1]["provenance"], "recorded_observation")

    def test_bounded_history_selector_stable_revision_and_deduplication(self):
        old = self.episode(current=False)
        for sequence in range(2, 25):
            self.episode(sequence, current=sequence == 24)
        now = timezone.now()
        IgFunnelStepEvent.objects.bulk_create([
            IgFunnelStepEvent(episode=old, event_type="price_quoted", event_key=f"bounded:{index}",
                              occurred_at=now + timedelta(seconds=index), evidence={"raw": "SECRET"})
            for index in range(70)
        ])
        IgCommercialEpisodeEvent.objects.create(episode=old, dedupe_key="mirror", event_type="payment_updated")
        with CaptureQueriesContext(connection) as queries:
            first = build_journey_snapshot(self.buyer, view_episode_id=old.pk)
        second = build_journey_snapshot(SimpleNamespace(pk=self.buyer.pk), view_episode_id=old.pk)
        # Trace reads add privacy, exact episode ownership, and one latest-row lookup.
        self.assertLessEqual(len(queries), 15)
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(first["episodes"]["total"], 24)
        self.assertEqual(len(first["episodes"]["items"]), 20)
        self.assertTrue(first["episodes"]["has_more"])
        self.assertEqual(first["viewed_episode_id"], old.pk)
        self.assertEqual(first["history"]["total"], 71)
        self.assertEqual(len(first["history"]["events"]), 50)
        self.assertTrue(first["history"]["has_more"])
        self.assertEqual(len({row["id"] for row in first["history"]["visits"]}), len(first["history"]["visits"]))
        self.assertNotIn("SECRET", json.dumps(first))
        existing = IgFunnelStepEvent.objects.get(event_key="bounded:69")
        _, created = IgFunnelStepEvent.objects.get_or_create(event_key=existing.event_key, defaults={"episode": old})
        self.assertFalse(created)
        self.assertEqual(first["revision"], build_journey_snapshot(self.buyer, view_episode_id=old.pk)["revision"])

    def test_exact_old_order_latest_truth_and_review_ownership(self):
        old = self.episode(current=False)
        order = Order.objects.create(full_name="Тест", phone="380500000000", city="Київ", np_office="1", total_sum="900.00",
                                     status="done", payment_status="paid", tracking_number="123", tracking_status_code=9,
                                     tracking_terminal_at=timezone.now(), shipment_status="Вручено одержувачу",
                                     shipment_status_updated=timezone.now())
        review = IgPaymentConfirmationReview.objects.create(client=self.other, dedupe_key="wrong-owner")
        old.intended_order = order
        old.primary_payment_review = review
        old.save(update_fields=["intended_order", "primary_payment_review"])
        self.episode(2)
        snapshot = build_journey_snapshot(self.buyer, view_episode_id=old.pk)
        self.assertEqual(self.nodes(snapshot)["fulfillment"]["state"], "complete")
        self.assertIn("owned_payment_review", self.nodes(snapshot)["payment"]["missing_sources"])
        payment = self.nodes(snapshot)["payment"]
        self.assertEqual(payment["state"], "partial")
        self.assertEqual([fact["id"] for fact in payment["facts"]], ["payment:order_payment"])
        self.assertEqual(payment["facts"][0]["source"], "intended_order.current")
        self.assertTrue(payment["facts"][0]["captured_at"])
        graph_fulfillment = next(n for n in snapshot["graph"]["nodes"] if n["id"] == "guide:fulfillment")
        facts = {fact["id"]: fact for fact in graph_fulfillment["facts"]}
        self.assertEqual(facts["fulfillment:tracking_number"]["value"], "123")
        self.assertEqual(facts["fulfillment:carrier_status"]["value"], "Вручено одержувачу")
        self.assertTrue(facts["fulfillment:carrier_status"]["captured_at"])
        self.assertNotIn("waiting", payment)  # Foreign review cannot create an obligation.

    def test_owned_pending_review_exposes_waiting_without_invented_deadline(self):
        episode = self.episode()
        review = IgPaymentConfirmationReview.objects.create(client=self.buyer, dedupe_key="owned-wait")
        episode.primary_payment_review = review
        episode.save(update_fields=["primary_payment_review"])
        snapshot = build_journey_snapshot(self.buyer)
        node = next(n for n in snapshot["graph"]["nodes"] if n["id"] == "guide:payment")
        self.assertEqual(node["waiting"]["kind"], "manager_review")
        self.assertEqual(node["waiting"]["evidence_refs"], [{"kind": "payment_review", "id": review.pk}])
        self.assertFalse(node.get("timers"))

    def test_empty_cancelled_archive_is_not_a_future_purchase_option(self):
        current = self.episode(3)
        empty = self.episode(4, current=False, state="cancelled")
        attempted = self.episode(2, current=False, state="cancelled")
        self.step(attempted, "paylink_issued")
        snapshot = build_journey_snapshot(self.buyer)
        self.assertEqual({row["id"] for row in snapshot["episodes"]["items"]}, {current.pk, attempted.pk})
        self.assertEqual(snapshot["episodes"]["hidden_empty_archives"], 1)
        self.assertTrue(IgCommercialEpisode.objects.filter(pk=empty.pk).exists())
        history = build_journey_snapshot(self.buyer, view_episode_id=empty.pk)
        self.assertEqual(history["viewed_episode_id"], empty.pk)

    def test_raw_event_payload_and_unknown_actor_are_not_exposed(self):
        episode = self.episode(product_snapshot=[{"title": "https://example.test/private?token=SECRET", "size": "M"}])
        self.step(episode, "paylink_issued", actor="SECRET", evidence={
            "url": "https://example.test/pay?token=SECRET", "invoice_id": "SECRET",
            "raw": {"text": "SECRET"}, "proposal_id": 12,
        })
        snapshot = build_journey_snapshot(self.buyer)
        self.assertNotIn("SECRET", json.dumps(snapshot))
        event = snapshot["history"]["events"][0]
        self.assertEqual(event["actor"], "unknown")
        self.assertEqual(event["values"], {"proposal_id": 12})

    def test_fact_success_tone_requires_positive_outcome_not_merely_known_status(self):
        episode = self.episode(fulfillment_snapshot={"order_status": "shipped"})
        expectations = {
            "confirmed": ("complete", "success"),
            "pending": ("open", "neutral"), "unknown": ("open", "neutral"),
            "unverified": ("open", "neutral"), "failed": ("invalidated", "warning"),
            "cancelled": ("invalidated", "warning"), "reversed": ("invalidated", "warning"),
            "refunded": ("partial", "neutral"), "partially_refunded": ("partial", "warning"),
        }
        for truth, expected in expectations.items():
            with self.subTest(truth=truth):
                episode.payment_snapshot = {
                    "provider_source": "monobank_projection", "projection_id": 1,
                    "provider_truth": truth, "provider_confirmed_amount": "0.00",
                    "manager_truth": "manager_rejected", "manager_decision_id": 2,
                }
                episode.save(update_fields=["payment_snapshot"])
                nodes = self.nodes(build_journey_snapshot(self.buyer))
                facts = {fact["id"]: fact for fact in nodes["payment"]["facts"]}
                provider = facts["payment:provider_truth"]
                self.assertEqual((provider["state"], provider["tone"]), expected)
                self.assertEqual(facts["payment:provider_confirmed_amount"]["tone"], "neutral")
                self.assertEqual(facts["payment:manager_decision"]["state"], "invalidated")
                self.assertEqual(facts["payment:manager_decision"]["tone"], "warning")
                self.assertEqual(nodes["fulfillment"]["facts"][0]["value"], "Відправлено")
                self.assertEqual(nodes["fulfillment"]["facts"][0]["tone"], "neutral")
        order = Order.objects.create(full_name="Тест", phone="380500000000", city="Київ", np_office="1",
                                     total_sum="900.00", status="done", payment_status="checking")
        episode.intended_order = order
        episode.save(update_fields=["intended_order"])
        nodes = self.nodes(build_journey_snapshot(self.buyer))
        order_fact = next(fact for fact in nodes["fulfillment"]["facts"] if fact["id"] == "fulfillment:order")
        self.assertEqual((order_fact["state"], order_fact["tone"]), ("partial", "neutral"))
        payment_fact = next(fact for fact in nodes["payment"]["facts"] if fact["id"] == "payment:order_payment")
        self.assertEqual(payment_fact["tone"], "neutral")

    def test_graph_uses_allowlisted_lifecycle_edges_and_episode_bound_objections(self):
        episode = self.episode()
        lifecycle = IgCommercialEpisodeEvent.objects.create(
            episode=episode, dedupe_key="graph-order", event_type="order_bound",
            from_state="active", to_state="order_created", source="order_resolution", evidence={"order_id": 9},
        )
        IgCommercialEpisodeEvent.objects.create(
            episode=episode, dedupe_key="graph-stage", event_type="stage_transition",
            from_state="paid", to_state="qualifying", evidence={"message_id": 7},
        )
        objection = IgObjection.objects.create(
            client=self.buyer, episode=episode, objection_type=IgObjection.Type.PRICE,
            dedupe_key="graph-objection", repeat_count=3, attempts_count=1,
        )
        attempt = IgObjectionAttempt.objects.create(
            objection=objection, method="answer", verified=False,
            result=IgObjectionAttempt.Result.PENDING,
        )
        with CaptureQueriesContext(connection) as queries:
            snapshot = build_journey_snapshot(self.buyer)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        # Privacy, episode ownership and latest trace are fixed-cost optional reads.
        self.assertLessEqual(len(queries), 18)
        graph = snapshot["graph"]
        self.assertEqual((graph["schema_version"], graph["version"]), (1, 1))
        self.assertEqual(len(graph["edges"]), 1)
        lifecycle_edge = next(edge for edge in graph["edges"] if edge["id"] == f"lifecycle:episode_event:{lifecycle.pk}")
        self.assertEqual(lifecycle_edge["relation"], "episode_lifecycle")
        self.assertEqual(lifecycle_edge["event_ids"], [f"episode_event:{lifecycle.pk}"])
        self.assertNotIn("qualifying", json.dumps(graph))
        objection_node = next(node for node in graph["nodes"] if node["id"] == f"objection:{objection.pk}")
        self.assertEqual([(fact["label"], fact["value"]) for fact in objection_node["facts"][:2]],
                         [("Повторних згадок", 3), ("Спроб відповіді", 1)])
        detail = objection_node["facts"][2]
        self.assertEqual(detail["id"], f"objection_attempt:{attempt.pk}")
        self.assertEqual(detail["state"], "partial")
        self.assertFalse(any(node["id"].startswith("objection_attempt:") for node in graph["nodes"]))

    def test_graph_bounds_attempt_details_and_reports_hidden_unresolved_cases(self):
        episode = self.episode()
        cases = [IgObjection.objects.create(
            client=self.buyer, episode=episode, objection_type=IgObjection.Type.PRICE,
            dedupe_key=f"graph-open-{index}", attempts_count=101 if index == 10 else 0,
        ) for index in range(11)]
        IgObjectionAttempt.objects.bulk_create([
            IgObjectionAttempt(objection=cases[10], method="answer", verified=False,
                               result=IgObjectionAttempt.Result.ACCEPTED)
            for _ in range(101)
        ])
        graph = build_journey_snapshot(self.buyer)["graph"]
        coverage = graph["coverage"]
        self.assertEqual(coverage["objections"], {
            "total": 11, "returned": 10, "limit": 20,
            "unresolved_total": 11, "unresolved_returned": 10, "has_more": True,
        })
        self.assertEqual(coverage["attempt_details"]["returned"], 100)
        self.assertTrue(coverage["attempt_details"]["has_more"])
        self.assertEqual(len(graph["overview_node_ids"]), 10)
        self.assertTrue(all(node_id.startswith("objection:") for node_id in graph["overview_node_ids"]))
        first = next(node for node in graph["nodes"] if node["id"] == f"objection:{cases[10].pk}")
        self.assertEqual(len(first["facts"]), 102)
        self.assertTrue(all(fact.get("state") != "complete" for fact in first["facts"][2:]))

    def test_analysis_is_annotation_not_activated_route(self):
        from management.models import IgConversationAnalysisSnapshot
        foreign_message = InstagramBotMessage.objects.create(
            client=self.other, sender_id="journey-other", role="user", text="OTHER CLIENT",
        )
        analysis = IgConversationAnalysisSnapshot.objects.create(
            client=self.buyer, dedupe_key="graph-analysis", score_band="exploring",
            interaction_type=IgConversationAnalysisSnapshot.InteractionType.COLLABORATION,
            last_analyzed_message=foreign_message,
        )
        graph = build_journey_snapshot(self.buyer)["graph"]
        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["edges"], [])
        self.assertEqual(graph["interpretations"][0]["status"], "interpretation_not_route_authority")
        self.assertNotIn("semantic_key", graph["interpretations"][0])
        self.assertEqual(graph["interpretations"][0]["evidence_refs"], [{"kind": "analysis_snapshot", "id": analysis.pk}])

    def test_accepted_conversation_journal_is_a_current_overlay_with_only_observed_focus_edge(self):
        episode = self.episode(stage_snapshot={"stage": "paid"})
        first_message = InstagramBotMessage.objects.create(
            client=self.buyer, sender_id="journey-buyer", role="user", text="Робота",
        )
        second_message = InstagramBotMessage.objects.create(
            client=self.buyer, sender_id="journey-buyer", role="user", text="Співпраця",
        )
        first = self.route_decision(
            sequence=1, message=first_message,
            active_intents=[
                {"key": "employment:none", "kind": "employment", "subtype": "none"},
                {"key": "collaboration:designer", "kind": "collaboration", "subtype": "designer"},
            ], focus_key="collaboration:designer", transitions=[
                {"operation": "open", "key": "employment:none", "reason_code": "customer_intent",
                 "evidence_message_ids": [first_message.pk]},
                {"operation": "open", "key": "collaboration:designer", "reason_code": "customer_intent",
                 "evidence_message_ids": [second_message.pk]},
                {"operation": "focus", "from_key": "", "key": "collaboration:designer",
                 "reason_code": "customer_intent"},
            ],
        )
        self.route_decision(
            sequence=2, message=second_message, previous=first,
            active_intents=[
                {"key": "employment:none", "kind": "employment", "subtype": "none"},
                {"key": "collaboration:designer", "kind": "collaboration", "subtype": "designer"},
            ], focus_key="employment:none", transitions=[
                {"operation": "focus", "from_key": "collaboration:designer", "key": "employment:none",
                 "reason_code": "customer_intent"},
            ],
        )
        snapshot = build_journey_snapshot(self.buyer)
        route = snapshot["conversation_route"]
        self.assertEqual(route["status"], "accepted_journal")
        self.assertEqual({item["key"] for item in route["active_intents"]},
                         {"employment:none", "collaboration:designer"})
        self.assertEqual(route["focus_key"], "employment:none")
        self.assertEqual({node["id"] for node in snapshot["graph"]["nodes"] if node["id"].startswith("conversation_intent:")},
                         {"conversation_intent:employment:none", "conversation_intent:collaboration:designer"})
        edges = [edge for edge in snapshot["graph"]["edges"] if edge["relation"] == "conversation_focus"]
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0]["from_node_id"], edges[0]["to_node_id"]),
                         ("conversation_intent:collaboration:designer", "conversation_intent:employment:none"))
        self.assertNotIn("episode", json.dumps(route))
        self.assertEqual({ref["kind"] for transition in route["history"]["transitions"]
                          for ref in transition["evidence_refs"]}, {"message"})
        self.assertEqual(snapshot["current_episode_id"], episode.pk)

    def test_route_history_keeps_explicit_withdrawal_and_omits_current_overlay_from_old_purchase(self):
        old = self.episode(current=False)
        current = self.episode(2)
        message = InstagramBotMessage.objects.create(
            client=self.buyer, sender_id="journey-buyer", role="user", text="Зміна теми",
        )
        self.route_decision(
            sequence=1, message=message,
            active_intents=[{"key": "employment:none", "kind": "employment", "subtype": "none"},
                            {"key": "collaboration:designer", "kind": "collaboration", "subtype": "designer"}],
            transitions=[],
        )
        self.route_decision(
            sequence=2, message=message,
            active_intents=[{"key": "collaboration:designer", "kind": "collaboration", "subtype": "designer"}],
            transitions=[
                {"operation": "withdraw", "key": "employment:none", "reason_code": "customer_intent",
                 "evidence_message_ids": [message.pk]},
                {"operation": "correct", "key": "collaboration:designer", "reason_code": "customer_correction",
                 "evidence_message_ids": [message.pk]},
            ],
        )
        current_snapshot = build_journey_snapshot(self.buyer)
        transitions = current_snapshot["conversation_route"]["history"]["transitions"]
        self.assertEqual([item["operation"] for item in transitions], ["withdraw", "correct"])
        employment = next(node for node in current_snapshot["graph"]["nodes"]
                          if node["id"] == "conversation_intent:employment:none")
        self.assertEqual(employment["state"], "invalidated")
        history = build_journey_snapshot(self.buyer, view_episode_id=old.pk)
        self.assertTrue(history["is_history"])
        self.assertEqual(history["viewed_episode_id"], old.pk)
        self.assertEqual(history["current_episode_id"], current.pk)
        self.assertNotIn("conversation_route", history)
        self.assertFalse(any(node["id"].startswith("conversation_intent:") for node in history["graph"]["nodes"]))

    def test_route_adapter_uses_current_explicit_reset_scope_and_owned_user_refs_read_only(self):
        before_reset = InstagramBotMessage.objects.create(
            client=self.buyer, sender_id="journey-buyer", role="user", text="Старий маршрут",
        )
        self.route_decision(
            sequence=1, message=before_reset,
            active_intents=[{"key": "employment:none", "kind": "employment", "subtype": "none"}],
            transitions=[{"operation": "open", "key": "employment:none", "reason_code": "customer_intent",
                          "evidence_message_ids": [before_reset.pk]}],
        )
        IgFunnelResetAudit.objects.create(client=self.buyer, reset_after_message_id=before_reset.pk, reason="test")
        foreign = InstagramBotMessage.objects.create(client=self.other, sender_id="journey-other", role="user", text="Чужий")
        manager = InstagramBotMessage.objects.create(client=self.buyer, sender_id="journey-buyer", role="manager", text="Менеджер")
        current_message = InstagramBotMessage.objects.create(client=self.buyer, sender_id="journey-buyer", role="user", text="Новий маршрут")
        floor = before_reset.pk + 1
        self.route_decision(
            sequence=1, reset_floor=floor, message=current_message,
            active_intents=[{"key": "support:none", "kind": "support", "subtype": "none"}],
            transitions=[{"operation": "open", "key": "support:none", "reason_code": "customer_intent",
                          "evidence_message_ids": [current_message.pk, foreign.pk, manager.pk]}],
        )
        with CaptureQueriesContext(connection) as queries:
            snapshot = build_journey_snapshot(self.buyer)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        self.assertEqual(snapshot["conversation_route"]["reset_floor"], floor)
        refs = snapshot["conversation_route"]["history"]["transitions"][0]["evidence_refs"]
        self.assertEqual(refs, [{"kind": "message", "id": current_message.pk}])
        self.assertNotIn(before_reset.pk, [ref["id"] for ref in refs])


@override_settings(ROOT_URLCONF="twocomms.urls_management", ALLOWED_HOSTS=["testserver"])
class JourneySnapshotEndpointTests(TestCase):
    """Exercise the decorated endpoint with real capability-bearing principals."""

    def setUp(self):
        self.viewer = get_user_model().objects.create_user(username="journey-api-viewer")
        permission = Permission.objects.get(
            content_type__app_label="management", codename="view_ig_conversation_pii",
        )
        self.viewer.user_permissions.add(permission)
        self.buyer = IgClient.objects.create(
            igsid="journey-api-buyer", display_name="PRIVATE BUYER", phone="PRIVATE PHONE",
        )
        self.other = IgClient.objects.create(igsid="journey-api-other")
        self.episode = IgCommercialEpisode.objects.create(
            client=self.buyer, sequence=1, materialization_key="journey-api-owned",
        )
        self.foreign = IgCommercialEpisode.objects.create(
            client=self.other, sequence=1, materialization_key="journey-api-foreign",
        )
        InstagramBotMessage.objects.create(
            client=self.buyer, sender_id=self.buyer.igsid, role="user", text="PRIVATE TRANSCRIPT",
        )
        self.url = reverse("management_bot_client_detail_api", args=[self.buyer.pk])

    def request(self, user, params=None):
        from management.bot_views import bot_client_detail_api

        request = RequestFactory().get(self.url, params or {"journey_only": "1"})
        request.user = user
        return bot_client_detail_api(request, self.buyer.pk)

    def test_journey_only_returns_snapshot_without_entering_full_detail(self):
        with patch("management.bot_views._message_media_rows", side_effect=AssertionError("Full message detail must not run")):
            response = self.request(self.viewer, {"journey_only": "1", "view_episode_id": str(self.episode.pk)})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(set(data), {"success", "journey"})
        self.assertIs(data["success"], True)
        self.assertEqual(data["journey"]["client_id"], self.buyer.pk)
        self.assertEqual(data["journey"]["viewed_episode_id"], self.episode.pk)
        self.assertEqual(data["journey"]["schema_version"], 1)
        for private in ("PRIVATE BUYER", "PRIVATE PHONE", "PRIVATE TRANSCRIPT"):
            self.assertNotIn(private, response.content.decode())

    def test_foreign_missing_and_invalid_episode_have_the_same_safe_404(self):
        payloads = []
        for episode_id in (str(self.foreign.pk), str(self.foreign.pk + 999999), "not-an-id"):
            with self.subTest(episode_id=episode_id):
                response = self.request(self.viewer, {"journey_only": "1", "view_episode_id": episode_id})
                self.assertEqual(response.status_code, 404)
                data = json.loads(response.content)
                self.assertEqual(set(data), {"success", "error"})
                self.assertIs(data["success"], False)
                self.assertNotIn("PRIVATE", response.content.decode())
                payloads.append(data)
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[1], payloads[2])

    def test_real_authentication_and_capability_guards_prevent_journey_pii(self):
        from management.bot_access import META_REVIEWER_GROUP_NAME

        staff = get_user_model().objects.create_user(username="journey-api-staff", is_staff=True)
        operator = get_user_model().objects.create_user(username="journey-api-operator")
        operator.user_permissions.add(Permission.objects.get(
            content_type__app_label="management", codename="operate_ig_bot",
        ))
        reviewer = get_user_model().objects.create_superuser(username="journey-api-reviewer")
        reviewer.groups.add(Group.objects.create(name=META_REVIEWER_GROUP_NAME))
        with patch("management.services.ig_journey_snapshot.build_journey_snapshot") as producer:
            for user, expected_status in ((AnonymousUser(), 302), (staff, 403), (operator, 403), (reviewer, 403)):
                with self.subTest(user=str(user)):
                    response = self.request(user)
                    self.assertEqual(response.status_code, expected_status)
                    self.assertNotIn("PRIVATE", response.content.decode())
                    self.assertNotIn('"journey"', response.content.decode())
                    if expected_status == 403:
                        self.assertEqual(set(json.loads(response.content)), {"success", "error"})
                    else:
                        self.assertIn(reverse("management_login"), response["Location"])
            producer.assert_not_called()
