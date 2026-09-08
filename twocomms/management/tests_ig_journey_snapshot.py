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
    IgClient, IgCommercialEpisode, IgCommercialEpisodeEvent, IgFunnelStepEvent,
    IgPaymentConfirmationReview, InstagramBotMessage,
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
        self.assertLessEqual(len(queries), 4)
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.buyer).exists())
        self.assertIsNone(snapshot["viewed_episode_id"])
        self.assertEqual(snapshot["focus"]["node_id"], "inquiry")
        self.assertEqual(self.nodes(snapshot)["inquiry"]["facts"][0]["evidence_refs"], [{"kind": "message", "id": message.pk}])
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
        self.assertLessEqual(len(queries), 8)
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
                                     tracking_terminal_at=timezone.now())
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
