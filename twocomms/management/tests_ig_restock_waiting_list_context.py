"""Pure captured agreement tests; native source proofs remain with producer."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.db import connection
from django.test.utils import CaptureQueriesContext

from management.services.ig_client_state_card import assemble_client_state, render_client_state_prompt
from management.services.ig_restock_waiting_list_context import (
    SUMMARY, DETAILS, MAX_CONTEXT_READS, capture_waiting_list_context, waiting_list_slots)
from management.tests_ig_restock_waiting_list import RestockWaitingListFixture


class WaitingListContextTests(SimpleTestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 15, 10, tzinfo=timezone.utc)
        self.boundary = {"client_id": 7, "episode_id": 99, "order_id": None, "line_id": "new-cart",
            "recipient_id": "self", "reset_floor": 10, "source_namespace": "instagram_login:owner",
            "reset_id": 3, "erasure_epoch": "", "source_watermark": {"message_id": 100, "event_at": self.now.isoformat()}}

    def projection(self):
        return {"version": 2, "client_id": 7, "captured_at": self.now.isoformat(), "coverage_complete": True,
            "native_future_capability": "unverified", "reason": "", "lists": [{"id": "old-list", "version": 2,
            "completion": "all", "locale": "uk", "items": [{"line_id": "old-line", "recipient_id": "gift",
                "title": "Худі Aurora", "color": "фіолетове", "size": "L", "fit": "oversize", "options": {},
                "quantity": 2, "item_kind": "custom_apparel"}],
            "agreement": {"state": "accepted", "accepted_at": (self.now-timedelta(days=1)).isoformat(),
                "expires_at": (self.now+timedelta(days=89)).isoformat(), "invitation_receipt_confirmed": True},
            "manager_review": {"state": "unverified", "all_ready": False, "ready": False, "checked_at": None, "lines": []},
            "notification": {"state": "pending", "reason": "waiting_window", "sent_at": None, "native_receipt_confirmed": False},
            "source_refs": [{"kind": "message", "id": 20, "event_at": (self.now-timedelta(days=2)).isoformat()},
                {"kind": "message", "id": 30, "event_at": (self.now-timedelta(days=1)).isoformat()}]}]}

    def slots(self, projection=None, boundary=None):
        return waiting_list_slots(projection or self.projection(), boundary=boundary or self.boundary, captured_at=self.now)

    def test_exact_accepted_historical_list_is_detached_from_new_cart_without_fact_grants(self):
        dto = self.projection()
        slots = self.slots(dto)
        state = assemble_client_state(boundary=self.boundary, components={"slots": slots}, captured_at=self.now)
        captured = state.as_dict()["slots"]
        self.assertEqual(captured[SUMMARY]["value"]["lists"][0]["state"], "accepted")
        self.assertEqual(captured[DETAILS]["value"]["lists"][0]["items"][0]["line_id"], "old-line")
        self.assertEqual(captured[DETAILS]["scope"]["episode_id"], 99)
        self.assertEqual(captured[SUMMARY]["authority"], "conversation_agreement")
        self.assertEqual(captured[SUMMARY]["value"]["lists"][0]["all_ready"], "unverified")
        self.assertEqual(captured[SUMMARY]["value"]["lists"][0]["channel"], "not_evaluated")
        dto["lists"][0]["items"][0]["quantity"] = 50
        self.assertEqual(captured[DETAILS]["value"]["lists"][0]["items"][0]["quantity"], 2)
        prompt = render_client_state_prompt(state, budget=6000)
        self.assertIn("Худі Aurora", prompt.text)
        self.assertIn("do not prove current cart", prompt.text)
        self.assertNotIn("RS1:", prompt.text)

    def test_wrong_owner_future_acceptance_or_source_outside_watermark_never_appears_accepted(self):
        for mutate in (
            lambda dto: dto.update(client_id=8),
            lambda dto: dto["lists"][0]["agreement"].update(accepted_at=(self.now+timedelta(seconds=1)).isoformat()),
            lambda dto: dto["lists"][0]["source_refs"][1].update(id=101),
            lambda dto: dto["lists"][0]["source_refs"][1].update(id=9),
            lambda dto: dto["lists"][0].update(source_refs=dto["lists"][0]["source_refs"][:1]),
            lambda dto: dto["lists"][0]["agreement"].update(invitation_receipt_confirmed=False),
        ):
            dto = self.projection()
            mutate(dto)
            with self.subTest(dto=dto):
                result = self.slots(dto)
                self.assertEqual(result[SUMMARY]["value"]["state"], "not_captured")
                self.assertNotIn(DETAILS, result)

    def test_raw_derived_slot_or_changed_capture_binding_does_not_smuggle_agreement(self):
        for alter in (lambda slot: slot["capture_scope"].update(source_namespace="foreign"),
            lambda slot: slot.update(authority="purpose_grant"), lambda slot: slot.update(authority="derived")):
            slots = self.slots()
            alter(slots[SUMMARY])
            state = assemble_client_state(boundary=self.boundary, components={"slots": slots}, captured_at=self.now)
            self.assertIsNone(state.as_dict()["slots"][SUMMARY]["value"])

    def test_unknown_receipt_and_future_manager_or_send_never_become_sent(self):
        for mutate in (
            lambda row: row["notification"].update(state="sent", sent_at=self.now.isoformat(), native_receipt_confirmed=False),
            lambda row: row["notification"].update(state="sent", sent_at=(self.now+timedelta(seconds=1)).isoformat(), native_receipt_confirmed=True),
            lambda row: row["manager_review"].update(checked_at=(self.now+timedelta(seconds=1)).isoformat()),
        ):
            dto = self.projection()
            mutate(dto["lists"][0])
            self.assertEqual(self.slots(dto)[SUMMARY]["value"]["state"], "not_captured")

    def test_complete_list_is_optional_and_omission_retains_compact_mandatory_agreement(self):
        dto = self.projection()
        item = dto["lists"][0]["items"][0]
        dto["lists"][0]["items"] = [{**deepcopy(item), "line_id": f"line-{index}",
            "title": "Худі з довгою назвою " * 10, "quantity": 1} for index in range(16)]
        state = assemble_client_state(boundary=self.boundary, components={"slots": self.slots(dto)}, captured_at=self.now)
        full = render_client_state_prompt(state, budget=2400)
        self.assertFalse(full.oversized)
        self.assertIn(SUMMARY, full.included)
        self.assertNotIn(DETAILS, full.included)
        self.assertIn((DETAILS, "budget_exceeded"), full.omitted)
        self.assertIn('"state":"accepted"', full.text)

    def test_partial_coverage_never_claims_absence_of_other_agreements(self):
        dto = self.projection()
        dto.update(lists=[], coverage_complete=False, reason="waiting_list_read_budget_exceeded")
        slot = self.slots(dto)[SUMMARY]
        self.assertFalse(slot["value"]["coverage_complete"])
        self.assertEqual(slot["value"]["details"], "unavailable")
        self.assertTrue(slot["mandatory"])

    @patch("management.services.ig_restock_waiting_list.waiting_list_projection")
    def test_capture_calls_one_bounded_nonphysical_projection_and_zero_reads_without_budget(self, reader):
        reader.return_value = self.projection()
        capture_waiting_list_context(7, boundary=self.boundary, captured_at=self.now, max_read_queries=7)
        reader.assert_called_once_with(7, now=self.now, limit=1, include_manager_review=False, max_read_queries=7)
        reader.reset_mock()
        result = capture_waiting_list_context(7, boundary=self.boundary, captured_at=self.now, max_read_queries=0)
        reader.assert_not_called()
        self.assertEqual(result[SUMMARY]["value"]["state"], "not_captured")


@override_settings(IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeWaitingListCaptureTests(RestockWaitingListFixture, TransactionTestCase):
    def test_actual_native_acceptance_reaches_shared_state_and_prompt_with_no_provider(self):
        case = self.accepted_list()
        source = case["subscription"].accepted_answer.source
        now = self.now + timedelta(seconds=3)
        boundary = {"client_id": self.client_row.pk, "episode_id": self.client_row.current_commercial_episode_id,
            "order_id": None, "line_id": "new-cart", "recipient_id": "self", "reset_floor": 1,
            "source_namespace": source.provider_namespace, "reset_id": None, "erasure_epoch": "",
            "source_watermark": {"message_id": source.pk, "event_at": now.isoformat()}}
        with CaptureQueriesContext(connection) as reads, patch("management.services.instagram_bot._provider_http") as provider:
            slots = capture_waiting_list_context(self.client_row.pk, boundary=boundary, captured_at=now)
        provider.assert_not_called()
        self.assertTrue(slots[SUMMARY]["value"].get("lists"),
            {"slots": slots, "read_queries": len(reads), "sql": [row["sql"] for row in reads]})
        self.assertEqual(slots[SUMMARY]["value"]["lists"][0]["state"], "accepted", slots)
        state = assemble_client_state(boundary=boundary, components={"slots": slots}, captured_at=now)
        prompt = render_client_state_prompt(state, budget=6000)
        self.assertIn("accepted", prompt.text)
        self.assertIn(case["invitation"].target["items"][0]["title"], prompt.text)
        from management.services.ig_admin_state_capture import current_admin_state
        with CaptureQueriesContext(connection) as admin_reads, patch("management.services.instagram_bot._provider_http") as provider:
            admin = current_admin_state(self.client_row.pk, now=now)
        provider.assert_not_called()
        self.assertEqual(admin.status, "captured", {"admin": admin.as_dict(),
            "sql": [row["sql"] for row in admin_reads]})
        self.assertLessEqual(admin.read_queries, 96)
        summary = admin.state.as_dict()["slots"][SUMMARY]
        self.assertTrue(summary["value"].get("lists"), {"admin": admin.as_dict(),
            "sql": [row["sql"] for row in admin_reads]})
        self.assertEqual(summary["value"]["lists"][0]["state"], "accepted", admin.as_dict())
        actual_prompt = render_client_state_prompt(admin.state, budget=2400)
        self.assertFalse(actual_prompt.oversized, actual_prompt.as_dict())
        self.assertIn(SUMMARY, actual_prompt.included)
        self.assertIn('"state":"accepted"', actual_prompt.text)
        if DETAILS not in actual_prompt.included:
            self.assertIn((DETAILS, "budget_exceeded"), actual_prompt.omitted)
