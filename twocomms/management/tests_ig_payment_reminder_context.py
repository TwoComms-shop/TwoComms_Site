"""Pure reminder capture parity/privacy; SimpleTestCase prohibits ORM reads."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.db import connection
from django.test.utils import CaptureQueriesContext

from management.services.ig_client_state_card import assemble_client_state, client_state_admin_payload, render_client_state_prompt
from management.services.ig_admin_state_capture import _current_readiness
from management.services.ig_payment_reminder_context import (
    SLOT, VERSION, capture_payment_reminder_context, payment_reminder_manifest, payment_reminder_slot,
)
from management.services.ig_request_manifest import capture_request_context, sanitize_request_context
from management.services.gemini_accounting_contract import RequestPolicyManifestError


class PaymentReminderContextTests(SimpleTestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
        self.boundary = {"client_id": 7, "episode_id": 13, "order_id": 21,
            "line_id": "line-a", "recipient_id": "self", "reset_floor": 10,
            "source_namespace": "instagram_login:reminder", "reset_id": None, "erasure_epoch": "",
            "selection_session_id": 8, "selection_generation": 1, "selection_revision": 0,
            "source_cart_capture_digest": "a" * 64,
            "source_watermark": {"message_id": 100, "event_at": self.at.isoformat()}}
        self.projection = {"version": 1, "status": "pending", "reason": "",
            "captured_at": self.at.isoformat(), "client_id": 7, "due_at": "2026-10-12T12:00:00+00:00",
            "source_ids": {"request": 20, "card": 21, "answer": 22, "send": None},
            "source_times": {"request": "2026-10-12T08:00:00+00:00", "card": "2026-10-12T08:01:00+00:00",
                "answer": "2026-10-12T08:02:00+00:00", "send": None},
            "source_scope": deepcopy(self.boundary)}

    def slot(self, projection=None, boundary=None):
        return payment_reminder_slot(projection if projection is not None else self.projection,
            boundary=boundary or self.boundary, captured_at=self.at)

    def state(self, slot):
        return assemble_client_state(boundary=self.boundary, components={"slots": {SLOT: slot}}, captured_at=self.at)

    def test_business_acceptance_has_exact_kyiv_time_and_no_native_grant(self):
        slot = self.slot()
        self.assertEqual(slot["value"]["state"], "accepted")
        self.assertEqual(slot["value"]["scheduled_for_label"], "12.10.2026 о 15:00 (Київ)")
        self.assertEqual(slot["authority"], "conversation_agreement")
        state = self.state(slot)
        self.assertEqual(state.as_dict()["slots"][SLOT]["status"], "confirmed")
        native = state.as_dict()["slots"]["consent.payment_reminder"]
        self.assertEqual((native["status"], native["authority"], native["value"]), ("unknown", "none", None))
        self.assertEqual(slot["value"]["native_future_permission"], "unverified")

    def test_one_read_frozen_admin_prompt_manifest_never_read_current_again(self):
        with patch("management.services.ig_payment_reminders.payment_reminder_projection", return_value=self.projection) as read:
            slot = capture_payment_reminder_context(7, boundary=self.boundary, captured_at=self.at)
            read.assert_called_once_with(7, now=self.at, current_source_cart=None)
            state = self.state(slot)
            before = client_state_admin_payload(state)
            manifest = payment_reminder_manifest(before["slots"][SLOT])
            self.projection["status"] = "sent"
            self.projection["source_scope"]["selection_revision"] = 9
            slot["value"]["state"] = "sent"
            prompt = render_client_state_prompt(state, budget=4000)
            self.assertIn(SLOT, prompt.included)
            self.assertIn('"state":"accepted"', prompt.text)
            self.assertEqual(before, client_state_admin_payload(state))
            self.assertEqual(manifest, payment_reminder_manifest(state.as_dict()["slots"][SLOT]))
            self.assertEqual(read.call_count, 1)

    def test_invitation_is_not_acceptance_or_delivery(self):
        for status in ("awaiting_delivery", "awaiting_confirmation"):
            raw = deepcopy(self.projection)
            raw["status"] = status
            raw["source_ids"]["answer"] = None
            raw["source_times"]["answer"] = None
            slot = self.slot(raw)
            self.assertEqual(slot["status"], "confirmed")
            self.assertFalse(slot["value"]["business_agreement"])
            self.assertNotEqual(slot["value"]["state"], "sent")

    def test_revoked_expired_and_ambiguous_are_distinct_from_sent(self):
        for status, expected in (("cancelled", "revoked"), ("expired", "expired"), ("ambiguous", "delivery_unknown")):
            raw = deepcopy(self.projection)
            raw["status"] = status
            slot = self.slot(raw)
            self.assertEqual(slot["value"]["state"], expected)
            self.assertNotEqual(slot["value"]["state"], "sent")

    def test_enclosing_cart_is_forwarded_once_and_defensively_copied(self):
        cart = {"schema": "source-selections.v1", "lines": [{"line_id": "a"}]}
        def reader(client, *, now, current_source_cart):
            current_source_cart["lines"].clear()
            return self.projection
        with patch("management.services.ig_payment_reminders.payment_reminder_projection", side_effect=reader) as read:
            slot = capture_payment_reminder_context(7, boundary=self.boundary, captured_at=self.at, current_source_cart=cart)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(cart["lines"], [{"line_id": "a"}])
        self.assertEqual(slot["value"]["state"], "accepted")

    def test_sent_requires_actual_send_source_and_captured_time(self):
        raw = deepcopy(self.projection)
        raw["status"] = "sent"
        self.assertEqual(self.slot(raw)["status"], "unknown")
        raw["source_ids"]["send"] = 23
        raw["source_times"]["send"] = "2026-10-12T08:03:00+00:00"
        self.assertEqual(self.slot(raw)["value"]["state"], "sent")
        raw["source_times"]["send"] = "2026-10-12T09:01:00+00:00"
        self.assertEqual(self.slot(raw)["omission_reason"], "payment_reminder_source_after_capture")

    def test_every_current_purchase_binding_and_source_boundary_is_checked(self):
        for key, value in (("episode_id", 99), ("reset_floor", 11), ("source_namespace", "foreign"),
                ("selection_session_id", 99), ("selection_generation", 2), ("selection_revision", 1),
                ("source_cart_capture_digest", "b" * 64)):
            with self.subTest(key=key):
                raw = deepcopy(self.projection)
                raw["source_scope"][key] = value
                self.assertEqual(self.slot(raw)["omission_reason"], "payment_reminder_scope_changed")
        raw = deepcopy(self.projection)
        raw["source_ids"]["answer"] = 101
        self.assertEqual(self.slot(raw)["omission_reason"], "payment_reminder_source_after_capture")
        raw["source_ids"]["answer"] = 9
        self.assertEqual(self.slot(raw)["omission_reason"], "payment_reminder_source_before_reset")
        boundary = {**self.boundary, "historical": True}
        self.assertEqual(self.slot(boundary=boundary)["status"], "unknown")

    def test_assembler_cannot_relabel_a_stale_purchase_slot(self):
        slot = self.slot()
        slot["capture_scope"]["source_cart_capture_digest"] = "b" * 64
        protected = self.state(slot).as_dict()["slots"][SLOT]
        self.assertEqual((protected["status"], protected["value"], protected["authority"]), ("unknown", None, "none"))
        self.assertNotIn(SLOT, render_client_state_prompt(self.state(slot), budget=4000).included)

    def test_private_or_arbitrary_projection_text_never_reaches_consumers(self):
        raw = deepcopy(self.projection)
        secret = "PRIVATE_PR1_HMAC_TOKEN_URL"
        raw.update(provider_mid=secret, button_payload=secret, policy=secret, due_at_kyiv=secret, reason=secret)
        slot = self.slot(raw)
        state = self.state(slot)
        self.assertNotIn(secret, json.dumps(client_state_admin_payload(state)))
        self.assertNotIn(secret, render_client_state_prompt(state, budget=4000).text)
        self.assertNotIn(secret, json.dumps(payment_reminder_manifest(slot)))

    def test_absence_invalid_or_reader_failure_is_unknown(self):
        for raw in ({"version": 1, "status": "none"}, {"version": 1, "status": "invalid"}, {}):
            slot = self.slot(raw)
            self.assertEqual((slot["status"], slot["value"]), ("unknown", None))
            self.assertEqual(payment_reminder_manifest(slot)["status"], "unknown")
        with patch("management.services.ig_payment_reminders.payment_reminder_projection", side_effect=RuntimeError("PRIVATE")):
            slot = capture_payment_reminder_context(7, boundary=self.boundary, captured_at=self.at)
        self.assertEqual(slot["omission_reason"], "payment_reminder_capture_unavailable")
        self.assertNotIn("PRIVATE", json.dumps(slot))

    def test_closed_manifest_rejects_unknown_keys_status_reason_and_private_values(self):
        snapshot = payment_reminder_manifest(self.slot())
        context = capture_request_context(payload={"contents": []}, metadata={"payment_reminder_snapshot": snapshot,
            "view_versions": {"payment_reminder_context": VERSION}})
        self.assertEqual(sanitize_request_context(context)["payment_reminder_snapshot"], snapshot)
        for changes in ({"provider_url": "PRIVATE"}, {"status": "anything"}, {"reason": "PRIVATE"},
                {"scope_digest": "PRIVATE"}, {"source_message_ids": [True]}, {"scheduled_for_epoch": 2**63},
                {"source_watermark_id": 1}, {"source_watermark_epoch": snapshot["as_of_epoch"] + 1}):
            with self.subTest(changes=changes), self.assertRaises(RequestPolicyManifestError):
                capture_request_context(payload={}, metadata={"payment_reminder_snapshot": {**snapshot, **changes}})


class AdminCapturedReminderReadinessTests(SimpleTestCase):
    def setUp(self):
        self.cart = {"schema": "source-selections.v1", "status": "captured", "capture_digest": "a" * 64,
            "coverage_complete": True, "active_line_id": "gift", "lines": [{"line_id": "self"}, {"line_id": "gift"}]}
        self.active = {"applicability_known": True, "can_issue_link": False, "missing": ["size"],
            "product": {"id": 12}, "size": {"required": True}}
        self.data = {"source_capture_digest": "a" * 64, "readiness_by_line": {
            "self": {"can_issue_link": True, "product": {"id": 11}}, "gift": self.active}}

    def catalog(self, data):
        return SimpleNamespace(as_dict=lambda: data)

    def test_active_readiness_uses_one_batched_all_position_read_without_lazy_fallback(self):
        with patch("management.services.ig_source_cart_catalog.resolve_source_cart_catalog", return_value=self.catalog(self.data)) as read, \
                patch("management.services.ig_checkout_readiness.selection_readiness") as lazy:
            result = _current_readiness(self.cart, {"product_id": 11})
        read.assert_called_once_with(self.cart)
        lazy.assert_not_called()
        self.assertEqual(result, self.active)
        result["product"]["id"] = 99
        self.assertEqual(self.active["product"]["id"], 12)

    def test_catalog_failure_scope_mismatch_or_missing_active_never_borrows_or_recaptures(self):
        for data in ({"source_capture_digest": "b" * 64, "readiness_by_line": self.data["readiness_by_line"]},
                {"source_capture_digest": "a" * 64, "readiness_by_line": {"self": {"can_issue_link": True}}}, {}):
            with self.subTest(data=data), \
                    patch("management.services.ig_source_cart_catalog.resolve_source_cart_catalog", return_value=self.catalog(data)) as read, \
                    patch("management.services.ig_checkout_readiness.selection_readiness") as lazy:
                result = _current_readiness(self.cart, {})
                self.assertFalse(result["can_issue_link"])
                self.assertFalse(result["applicability_known"])
                self.assertEqual(read.call_count, 1)
                lazy.assert_not_called()

    def test_private_readback_is_passed_to_the_same_active_catalog_read(self):
        readback = {"source_capture_digest": "a" * 64, "prepared": True}
        with patch("management.services.ig_source_cart_catalog.resolve_source_cart_catalog", return_value=self.catalog(self.data)) as read:
            self.assertEqual(_current_readiness(self.cart, {}, _catalog_readback=readback), self.active)
        read.assert_called_once_with(self.cart, _catalog_readback=readback)


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class PreparedAdminReminderCatalogTests(TestCase):
    """Native oracle for transient reuse, without importing inherited tests."""
    def setUp(self):
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests
        SourceCartCatalogTests.setUp(self)

    def capture(self):
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests
        return SourceCartCatalogTests.capture(self)

    def prepared(self, cart):
        from management.services.ig_catalog_graph import build_catalog_graph
        readback = {}
        build_catalog_graph(product_ids=[row.pk for row in self.products], _catalog_readback=readback)
        readback.update(source_capture_digest=cart["capture_digest"], scope=deepcopy(cart["scope"]),
            **{key: cart[key] for key in ("session_id", "generation", "selection_revision")})
        return readback

    def test_reused_graph_matches_fresh_all_position_catalog_with_zero_additional_selects(self):
        from management.services.ig_source_cart_catalog import resolve_source_cart_catalog
        cart = self.capture()
        readback = self.prepared(cart)
        fresh = resolve_source_cart_catalog(cart).as_dict()
        with CaptureQueriesContext(connection) as queries:
            reused = resolve_source_cart_catalog(cart, _catalog_readback=readback).as_dict()
        self.assertEqual(len(queries), 0)
        for key in ("complete", "gaps", "readiness_by_line", "authority_by_line", "item_specs_by_line"):
            self.assertEqual(reused[key], fresh[key], key)
        self.assertEqual(reused["select_count"], 0)
        json.dumps(reused)  # Private ORM models never enter the returned DTO.

    def test_changed_owner_head_digest_or_product_set_rejects_without_requery(self):
        from management.services.ig_source_cart_catalog import resolve_source_cart_catalog
        cart = self.capture()
        readback = self.prepared(cart)
        for key, value in (("scope", {**cart["scope"], "client_id": 99}), ("session_id", 99),
                ("generation", 99), ("selection_revision", 99), ("source_capture_digest", "b" * 64),
                ("products", ()), ("prepared", False)):
            altered = {**readback, key: value}
            with self.subTest(key=key), CaptureQueriesContext(connection) as queries:
                result = resolve_source_cart_catalog(cart, _catalog_readback=altered)
            self.assertFalse(result.complete)
            self.assertFalse(result.readiness_by_line)
            self.assertEqual(len(queries), 0)
