"""Measure like-for-like full39 and partial40 reads before fixing a budget.

No numeric fresh-authority cap is invented here. The root runtime owner runs
these real producer cases on the same disposable native database and records
the phase counts. Provider/clock replacements do not replace authority reads.
"""
from collections import Counter
from datetime import timedelta
import hashlib
import json
import re
import uuid
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.ig_assisted_commerce_models import IgAssistedCommerceDecision as Decision
from management.models import IgCheckoutInventoryReservation, IgCheckoutProposal, IgFollowUpTask
from management.services import ig_assisted_commerce as commerce
from management.services import ig_assisted_scopes as scopes
from management.services.ig_assisted_commerce_context import capture_assisted_context
from management.services.ig_response_plan import capture_response_plan
from management.tests_ig_assisted_partial_sources import PartialCommerceFixture
from orders.models import Order, PaymentAttempt


def _selects(queries):
    return [row["sql"] for row in queries if row["sql"].lstrip().upper().startswith("SELECT ")]


def _read_summary(queries):
    statements = _selects(queries)
    tables = Counter()
    signatures = Counter()
    for sql in statements:
        tables.update(re.findall(r"\b(?:FROM|JOIN)\s+[`\"]?([a-zA-Z_][a-zA-Z0-9_]*)", sql, flags=re.I))
        # Emit counts/hashes only; source text, tokens and private values never
        # enter the benchmark output. Normalisation identifies duplicate reads.
        shape = re.sub(r"'(?:''|[^'])*'", "?", sql)
        shape = re.sub(r"\b\d+(?:\.\d+)?\b", "?", shape)
        signatures[hashlib.sha256(shape.encode()).hexdigest()[:12]] += 1
    return {"selects": len(statements), "tables": dict(tables.most_common(12)),
        "repeated_shapes": [{"shape": key, "count": count} for key, count in signatures.most_common(10) if count > 1],
        "locking_selects": sum("FOR UPDATE" in sql.upper() or "LOCK IN SHARE MODE" in sql.upper() for sql in statements),
        "session_lock_selects": sum("GET_LOCK(" in sql.upper() or "RELEASE_LOCK(" in sql.upper() for sql in statements)}


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativeAssistedPartialReadBudgetMeasurements(PartialCommerceFixture, TransactionTestCase):
    """Same actor/backend, two custom lines and native source/capture producers."""
    reset_sequences = True

    def _full39_case(self):
        self.canonical("цвет purple, размер L")
        source = self.canonical("добавьте Storm, количество 1, размер L, цвет olive, крій classic")
        self.revision_row, _claim = self.revision(source)
        request, _operation, context = self.produced_request()
        items = context["editable_items"]
        self.assertEqual(len(items), 2)
        for index, item in enumerate(items):
            self.assertEqual(item["item_kind"], "custom_apparel")
            item.update(title="Фіолетове худі" if index == 0 else "Оливкове худі",
                unit_price="1590.00" if index == 0 else "1490.00", availability="ready",
                checked_quantity=item["quantity"], supply_basis="accessible_unrecorded",
                source_location="Перевірено менеджером")
            item["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed",
                "answer": "Менеджер перевірив: " + str(question["requested_value"])}
                for question in request.envelope["snapshot"]["questions"] if question["line_id"] == item["line_id"]]
        draft = self.post({"action": "draft", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
            **context["cas"], "items": items})
        self.assertEqual(draft.status_code, 200, draft.content)
        approved = self.post({"action": "approve", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
            **draft.json()["commerce"]["cas"]})
        self.assertEqual(approved.status_code, 200, approved.content)
        decision = Decision.objects.get(pk=approved.json()["decision_id"])
        self.assertNotIn("scoped_quotes", decision.envelope["snapshot"])
        self.assertNotIn("ancestry", request.envelope["snapshot"])
        self.now += timedelta(seconds=1)
        buy = self.canonical("Хочу купити всі товари в кошику", at=self.now)
        revision, _token = self.revision(buy, now=self.now)
        return {"request": request, "decision": decision, "revision": revision, "source": buy}

    def _economic_counts(self):
        return (IgCheckoutProposal.objects.count(), IgCheckoutInventoryReservation.objects.count(),
            PaymentAttempt.objects.count(), Order.objects.count(), IgFollowUpTask.objects.count())

    def _measure(self, case, label):
        before = self._economic_counts()
        phases = {}
        with patch("management.services.instagram_bot._provider_http") as provider, \
                patch.object(commerce, "load_verified_assisted_decision", wraps=commerce.load_verified_assisted_decision) as parent_verify, \
                patch.object(commerce, "_current_request", wraps=commerce._current_request) as request_verify, \
                patch.object(commerce, "_capture", wraps=commerce._capture) as capture_verify, \
                patch.object(scopes, "_history", wraps=scopes._history) as ancestry_verify:
            counters = {"parent_verifications": parent_verify, "request_verifications": request_verify,
                "source_capture_verifications": capture_verify, "ancestry_materializations": ancestry_verify}
            counts = lambda: {key: call.call_count for key, call in counters.items()}
            previous = counts()
            def finish(phase):
                nonlocal previous
                current = counts()
                phases[phase] = {key: current[key] - previous[key] for key in current}
                previous = current
            with CaptureQueriesContext(connection) as plan_queries:
                plan = capture_response_plan(self.client_row, revision=case["revision"])
            finish("plan")
            with CaptureQueriesContext(connection) as context_queries:
                frame, reason = capture_assisted_context(self.client_row, case["revision"], plan, now=self.now)
            finish("current_context")
            self.assertIsNotNone(frame, reason)
            with CaptureQueriesContext(connection) as descriptor_queries:
                projection = frame.projection
                guidance = frame.prompt_guidance()
                reference = frame.reference
            finish("prompt_descriptor")
            with CaptureQueriesContext(connection) as approval_queries:
                cap = commerce.load_verified_assisted_decision(case["request"].pk, case["decision"].pk,
                    actor=self.manager, expected_request_digest=case["request"].envelope_hmac,
                    expected_decision_digest=case["decision"].envelope_hmac, now=self.now)
            finish("fresh_parent_approval")
        provider.assert_not_called()
        self.assertEqual(self._economic_counts(), before)
        measured = [*plan_queries, *context_queries, *descriptor_queries, *approval_queries]
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")) for row in measured))
        self.assertEqual(len(descriptor_queries), 0, "An already verified prompt descriptor must remain query-free")
        self.assertTrue(projection)
        self.assertTrue(guidance)
        self.assertEqual(reference["decision_id"], str(case["decision"].pk))
        self.assertEqual(cap.decision_id, case["decision"].pk)
        report = {"case": label, "backend": connection.vendor, "original_lines": 2,
            "plan": _read_summary(plan_queries), "current_context": _read_summary(context_queries),
            "prompt_descriptor": _read_summary(descriptor_queries), "fresh_parent_approval": _read_summary(approval_queries),
            "native_verifier_calls": phases,
            "combined_dialogue_selects": len(_selects([*plan_queries, *context_queries, *descriptor_queries]))}
        print("ASSISTED_NATIVE_READ_BASELINE " + json.dumps(report, sort_keys=True))
        return report

    def test_measure_released_full39_two_custom_api_approval_and_canonical_buy(self):
        report = self._measure(self._full39_case(), "full39-two-custom-api-canonical-buy")
        self.assertGreater(report["fresh_parent_approval"]["selects"], 0)

    def test_measure_partial40_two_custom_api_approval_and_native_delivered_choice(self):
        case = self.selected_case()
        self.assertEqual(len(case["request"].envelope["snapshot"]["lines"]), 2)
        report = self._measure(case, "partial40-two-custom-api-native-choice")
        self.assertGreater(report["fresh_parent_approval"]["selects"], 0)
        current = report["native_verifier_calls"]["current_context"]
        self.assertEqual(current["parent_verifications"], 1,
            "One scoped read DAG must authenticate its parent once, without recursive rereads")
        self.assertEqual(current["request_verifications"], 1)
        self.assertEqual(current["source_capture_verifications"], 1)
        self.assertLessEqual(current["ancestry_materializations"], 1)
        self.assertEqual(report["native_verifier_calls"]["fresh_parent_approval"]["parent_verifications"], 1,
            "The separate effect read must freshly authenticate again")
