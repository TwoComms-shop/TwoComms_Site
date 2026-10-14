"""Actual paid partial scopes remain readable within the protected UI budget."""
from copy import deepcopy
import json
import re
import uuid
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from management.ig_assisted_commerce_models import IgAssistedCommerceExecution
from management.models import IgCheckoutProposal, IgFollowUpTask, InstagramBotMessage
from management.services import ig_commerce_projection, ig_assisted_scopes
from management.services.ig_commerce_scope_read_model import capture_commerce_scope
from management.tests_ig_assisted_partial_sources import PartialCommerceFixture
from orders.models import Order, PaymentAttempt


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativeAssistedPartialHistoryTests(PartialCommerceFixture, TransactionTestCase):
    reset_sequences = True

    def paid_chain(self, line_count):
        first = self.selected_case(line_count=line_count)
        first["order"] = self.pay_prepared(self.prepare_selection(first["selection"]))
        second = self.paid_remainder_case(first["request"], ready_only_first=line_count == 3)
        return [first, second, *([self.paid_remainder_case(second["request"])] if line_count == 3 else [])]

    def graph_counts(self):
        return (Order.objects.count(), PaymentAttempt.objects.count(), IgCheckoutProposal.objects.count(),
            IgAssistedCommerceExecution.objects.count(), IgFollowUpTask.objects.count(), InstagramBotMessage.objects.count())

    def assert_bounded_native_history(self, chain):
        root = chain[0]["request"]
        expected_history = ig_assisted_scopes.original_intention_source_history(chain[-1]["request"], now=self.now)
        self.assertTrue(expected_history["ready"], expected_history)
        before = self.graph_counts()
        for row in chain:
            row["order"].refresh_from_db()
        immutable = [(row["order"].pk, deepcopy(row["order"].payment_payload),
            list(row["order"].items.values_list("pk", "qty", "unit_price"))) for row in chain]
        with CaptureQueriesContext(connection) as queries, \
                patch("management.services.instagram_bot._provider_http") as meta, \
                patch("storefront.views.monobank._monobank_api_request") as bank, \
                patch("management.services.ig_commerce_projection.capture_current_selection_lines",
                    wraps=ig_commerce_projection.capture_current_selection_lines) as captures:
            response = self.client.get(reverse("management_bot_client_commerce_scope_api", args=[self.client_row.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertTrue(payload["success"], payload)
        self.assertEqual(payload["status"], "captured")
        self.assertLessEqual(payload["diagnostics"]["read_queries"], 96, payload["diagnostics"])
        self.assertEqual(payload["diagnostics"]["max_read_queries"], 96)
        self.assertEqual(captures.call_count, 2)
        meta.assert_not_called()
        bank.assert_not_called()
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")) for row in queries))
        orders = payload["history"]["orders"]
        self.assertEqual({row["id"] for row in orders}, {row["order"].pk for row in chain})
        self.assertEqual(len(orders), len(chain))
        self.assertTrue(all(row["payment"]["status"] == "confirmed" for row in orders))
        self.assertTrue(all(row["revenue"]["status"] == "confirmed" for row in orders))
        self.assertEqual(sum(item["quantity"] for row in orders for item in row["items"]),
            sum(row["quantity"] for row in expected_history["original_lines"]))
        current = payload["current"]
        self.assertEqual(current["status"], "captured", current)
        self.assertTrue(current["lines"])
        self.assertFalse(current["editable"])
        self.assertIsNone(current["editable_scope"])
        originals = {row["line_id"]: row for row in expected_history["original_lines"]}
        for line in current["lines"]:
            self.assertFalse(line["editable"])
            for key in ("size", "color", "quantity"):
                field = line["fields"][key]
                self.assertEqual(field["status"], "confirmed", (key, field))
                self.assertEqual(field["authority"], "assisted_continuation")
                self.assertEqual(field["origin"]["root_request_id"], str(root.pk))
                original = originals[field["origin"]["line_id"]]
                self.assertEqual(field["origin"]["recipient_id"], original["recipient_id"])
                self.assertEqual(field["value"], original["quantity"] if key == "quantity" else original["source_choices"][key])
                self.assertEqual(field["adopted_by"]["source"]["message_id"], chain[-1]["resume_source"].pk)
                self.assertEqual(field["adopted_by"]["receipt"]["task_id"], chain[-1]["resume_task"].pk)
                self.assertTrue(field["adopted_at"])
                self.assertEqual(field["availability"], "unknown")
        serialized = json.dumps(payload)
        for private_key in ('"phone"', '"full_name"', '"np_office"', '"address"'):
            self.assertNotIn(private_key, serialized)
        self.assertEqual(before, self.graph_counts())
        root.refresh_from_db()
        self.assertEqual(root.envelope_hmac, expected_history["root_request_digest"])
        for order_id, payment_payload, items in immutable:
            order = Order.objects.get(pk=order_id)
            self.assertEqual(order.payment_payload, payment_payload)
            self.assertEqual(list(order.items.values_list("pk", "qty", "unit_price")), items)

    def test_two_actual_paid_partial_orders_have_bounded_authenticated_commerce_scope_history(self):
        self.assert_bounded_native_history(self.paid_chain(2))

    def test_three_actual_paid_partial_orders_have_bounded_authenticated_commerce_scope_history(self):
        from management.models import IgTurnRevisionSource
        from management.ig_assisted_commerce_models import IgAssistedCommerceRequest

        chain = self.paid_chain(3)
        self.assert_bounded_native_history(chain)
        with CaptureQueriesContext(connection) as queries:
            history = ig_assisted_scopes.original_intention_source_history(chain[-1]["request"], now=self.now)
        self.assertTrue(history["ready"], history)
        self.assertEqual(len(history["request_history"]), 3)
        table = re.escape(IgTurnRevisionSource._meta.db_table)
        memberships = [row for row in queries if re.search(r"\bFROM\s+[`\"]?" + table + r"[`\"]?\b", row["sql"], re.I)]
        self.assertEqual(len(memberships), 1)
        request_table = re.escape(IgAssistedCommerceRequest._meta.db_table)
        requests = [row for row in queries if re.search(r"\bFROM\s+[`\"]?" + request_table + r"[`\"]?\b", row["sql"], re.I)]
        self.assertEqual(len(requests), 1)

    def test_changed_original_source_is_reloaded_in_the_next_history_capture(self):
        chain = self.paid_chain(2)
        before = ig_assisted_scopes.original_intention_source_history(chain[-1]["request"], now=self.now)
        self.assertTrue(before["ready"], before)
        source_id = chain[0]["request"].envelope["snapshot"]["sources"][0]["message_id"]
        source = InstagramBotMessage.objects.get(pk=source_id)
        source.text = "Змінені початкові побажання клієнта."
        source.save(update_fields=["text"])
        after = ig_assisted_scopes.original_intention_source_history(chain[-1]["request"], now=self.now)
        self.assertFalse(after["ready"])
        self.assertEqual(after["reason"], "original_source_changed")
        with patch("management.services.instagram_bot._provider_http") as provider:
            scope = capture_commerce_scope(self.client_row.pk, now=self.now)
        provider.assert_not_called()
        self.assert_no_native_choice_authority(scope)

    def test_changed_actual_resume_receipt_is_reloaded_in_the_next_current_scope_capture(self):
        chain = self.paid_chain(2)
        initial = capture_commerce_scope(self.client_row.pk, now=self.now)
        self.assertEqual(initial["status"], "captured", initial)
        task = chain[-1]["resume_task"]
        task.refresh_from_db()
        context = deepcopy(task.manager_context)
        context["assisted_partial_delivery"]["manifest"]["native_mid"] = "changed-receipt"
        task.manager_context = context
        task.save(update_fields=["manager_context", "updated_at"])
        with patch("management.services.instagram_bot._provider_http") as provider:
            changed = capture_commerce_scope(self.client_row.pk, now=self.now)
        provider.assert_not_called()
        self.assert_no_native_choice_authority(changed)

    def test_thirty_three_actual_superseded_roots_keep_the_chain_only_history_bound(self):
        from management.services import ig_assisted_commerce as commerce

        first = self.partial_case()
        head = first["request"]
        revision = first["original_revision"]
        original_capture = deepcopy(head.envelope["snapshot"]["full_capture"])
        with patch("management.services.instagram_bot._provider_http") as provider:
            for expected_version in range(1, ig_assisted_scopes.MAX_HISTORY + 1):
                head, created = commerce.create_assisted_request(revision.pk, settings_id=self.settings.pk,
                    operation_id=uuid.uuid4(), expected_revision_digest=revision.snapshot_digest,
                    expected_full_capture_digest=original_capture["capture_digest"],
                    intention_id=head.intention_id, expected_request_version=head.version,
                    expected_request_digest=head.envelope_hmac, now=self.now)
                self.assertTrue(created)
                self.assertEqual(head.version, expected_version + 1)
                if head.version in (ig_assisted_scopes.MAX_HISTORY, ig_assisted_scopes.MAX_HISTORY + 1):
                    history = ig_assisted_scopes.original_intention_source_history(head, now=self.now)
                    self.assertTrue(history["ready"], history)
                    self.assertEqual(history["root_request_id"], str(head.pk))
                    self.assertEqual(history["root_request_digest"], head.envelope_hmac)
                    self.assertEqual(len(history["request_history"]), 1)
                    self.assertEqual(history["origin_full_capture"], original_capture)
        provider.assert_not_called()
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(IgAssistedCommerceExecution.objects.exists())

    def assert_no_native_choice_authority(self, scope):
        current = scope.get("current") or {}
        self.assertFalse(current.get("editable"))
        for line in current.get("lines", ()):
            for field in line["fields"].values():
                self.assertNotEqual(field.get("status"), "confirmed")
