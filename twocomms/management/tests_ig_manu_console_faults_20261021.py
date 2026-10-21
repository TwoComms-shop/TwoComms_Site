"""Finite local/proof faults survive the actual writer and bounded console read."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.models import InstagramBotLog
from management.services import instagram_bot
from management.services.ig_console_read_model import build_console_payload
from management.services.ig_revision_finalization_faults import FAULT_CODES
from management.services.ig_revision_live import _log_revision_blocked


FAULT_REASONS = (
    "required_context_budget_exceeded", "captured_state_exceeds_budget",
    "local_preprovider_fault", "finalization_proof_quarantined",
    *sorted(FAULT_CODES),
)


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeConsoleExecutionFaultTests(TestCase):
    def test_real_revision_producer_preserves_each_finite_reason_and_proven_scope(self):
        with patch.object(instagram_bot._INCIDENT_LOGGER, "log"), \
                patch("requests.sessions.Session.request", side_effect=AssertionError("provider forbidden")) as provider:
            for index, reason in enumerate(FAULT_REASONS):
                _log_revision_blocked(100 + index, 200 + index,
                    SimpleNamespace(state="blocked", reasons=(reason,)))
            with CaptureQueriesContext(connection) as queries:
                payload = build_console_payload(can_view=True, filters={"category": "errors"}, limit=120)
        provider.assert_not_called()
        self.assertEqual(len(payload["items"]), len(FAULT_REASONS))
        self.assertEqual({item["reason"] for item in payload["items"]}, set(FAULT_REASONS))
        for index, item in enumerate(payload["items"]):
            self.assertEqual(item["scope"], {"revision_id": 100 + index, "client_id": 200 + index})
            self.assertEqual(item["kind"], "reply_failed")
            self.assertTrue(item["structured"])
            self.assertTrue(item["actionable"])
            self.assertEqual(item["detail"], "")
        self.assertLessEqual(len(queries), 2)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))

    def test_safe_fault_reasons_are_actionable_even_without_warning_severity(self):
        for index, reason in enumerate(FAULT_REASONS):
            instagram_bot.log("info", "revision_execution", "unstructured text must not enter console",
                kind="decision_completed", scope={"revision_id": 100 + index}, reason=reason)
        payload = build_console_payload(can_view=True, filters={"category": "errors"}, limit=120)
        self.assertEqual({item["reason"] for item in payload["items"]}, set(FAULT_REASONS))
        self.assertTrue(all(item["actionable"] and item["level"] == "info" for item in payload["items"]))
        self.assertNotIn("unstructured text", json.dumps(payload))

    def test_arbitrary_raw_reason_is_filtered_at_producer_and_historical_reader(self):
        secret = "PRIVATE_SECRET_TOKEN_customer@example.test"
        with patch.object(instagram_bot._INCIDENT_LOGGER, "log"):
            _log_revision_blocked(101, 202, SimpleNamespace(state="blocked", reasons=(secret,)))
            instagram_bot.log("warning", "revision_execution", secret,
                kind="reply_failed", scope={"revision_id": 102, "client_id": 202}, reason=secret)
        row = InstagramBotLog.objects.order_by("pk").first()
        historical = json.loads(row.detail)
        historical["reason"] = secret
        InstagramBotLog.objects.create(level="warning", event="revision_execution", detail=json.dumps(historical))
        payload = build_console_payload(can_view=True, filters={"category": "all"}, limit=120)
        self.assertEqual([item["reason"] for item in payload["items"]],
            ["failed", "unknown", "legacy_unstructured"])
        self.assertEqual(payload["items"][0]["scope"], {"revision_id": 101, "client_id": 202})
        self.assertEqual(payload["items"][1]["scope"], {})
        self.assertEqual(payload["items"][2]["scope"], {})
        self.assertNotIn(secret, json.dumps(payload))
        self.assertNotIn(secret, " ".join(InstagramBotLog.objects.exclude(pk=InstagramBotLog.objects.latest("pk").pk)
            .values_list("detail", flat=True)))
