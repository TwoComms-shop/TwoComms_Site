"""Pure contract tests: no Django setup, database, provider, or raw-text output."""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from management.services.ig_journey_trace_contract import (
    SEMANTIC_NODE_KEYS, normalize_journey_trace, validate_normalized_journey_trace,
)


class JourneyTraceContractTests(unittest.TestCase):
    def setUp(self):
        self.by_id = {
            10: {"message_id": 10, "role": "user", "text": "Хочу\n  футболку M. Private phone 0123456789"},
            11: {"message_id": 11, "role": "manager", "text": "Очікуємо   наявність. Обговоримо варіанти."},
            12: {"message_id": 12, "role": "model", "text": "Оплату підтверджено, замовлення оформлено."},
        }

    def step(self, **changes):
        result = {
            "from_node": "inbound", "to_node": "catalog_discovery",
            "kind": "progress", "reason_code": "product_selected", "confidence": .9,
            "evidence": [{"message_id": 10, "quote": "Хочу футболку M."}],
        }
        result.update(changes)
        return result

    def normalize(self, steps, current="catalog_discovery", **kwargs):
        result = normalize_journey_trace(
            {"schema_version": 1, "steps": steps, "current_node": current},
            by_id=kwargs.get("by_id", self.by_id), watermark=kwargs.get("watermark", 12),
        )
        self.assertTrue(validate_normalized_journey_trace(result), result)
        return result

    def test_user_manager_and_model_attribution_without_authority_or_pii(self):
        result = self.normalize([
            self.step(from_node=""),
            self.step(
                from_node="catalog_discovery", to_node="stock_wait", kind="waiting",
                reason_code="awaiting_stock", evidence=[
                    {"message_id": 11, "quote": "Очікуємо наявність."},
                    {"message_id": 12, "quote": "Оплату підтверджено"},
                ],
            ),
        ], "stock_wait")
        self.assertEqual(result["status"], "interpretation_only")
        self.assertEqual(result["authority"], "none")
        self.assertEqual(result["provenance"], "transcript_reconstruction")
        self.assertEqual(result["current_node"], "stock_wait")
        evidence = result["steps"][1]["evidence"]
        self.assertEqual([row["role"] for row in evidence], ["manager", "model"])
        full_text = " ".join(self.by_id[11]["text"].split())
        self.assertEqual(evidence[0]["source_text_sha256"], hashlib.sha256(full_text.encode()).hexdigest())
        output = json.dumps(result, ensure_ascii=False)
        for private in ("0123456789", "Хочу", "Очікуємо", "Оплату", "quote", "text\""):
            self.assertNotIn(private, output)
        self.assertEqual(result["coverage"]["earlier_history"], "unknown")

    def test_model_only_step_and_its_current_focus_are_omitted(self):
        result = self.normalize([
            self.step(),
            self.step(from_node="catalog_discovery", to_node="settlement", reason_code="payment_discussed",
                      evidence=[{"message_id": 12, "quote": "Оплату підтверджено"}]),
        ], "settlement")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["current_node"], "")
        self.assertEqual(result["coverage"]["reasons"]["human_evidence_required"], 1)
        self.assertTrue(result["coverage"]["current_node_omitted"])

    def test_wrong_foreign_boolean_future_and_mismatched_source_ids(self):
        for identifier in (True, False, "10", 0, -1, 13, 999):
            with self.subTest(identifier=identifier):
                result = self.normalize([self.step(evidence=[{"message_id": identifier, "quote": "Хочу"}])])
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["steps"], [])
        missing = self.normalize([self.step(evidence=[{"message_id": 9, "quote": "Хочу"}])])
        self.assertIn("source_unavailable", missing["coverage"]["reasons"])
        for field, value in (("message_id", True), ("message_id", 11), ("role", "admin")):
            by_id = deepcopy(self.by_id)
            by_id[10][field] = value
            result = self.normalize([self.step()], by_id=by_id)
            self.assertEqual(result["steps"], [])

    def test_quote_must_match_same_normalized_source_and_step_is_not_salvaged(self):
        for quote in ("", " \n\t", "Очікуємо наявність.", "хочу", None):
            with self.subTest(quote=quote):
                result = self.normalize([self.step(evidence=[
                    {"message_id": 11, "quote": "Очікуємо наявність."},
                    {"message_id": 10, "quote": quote},
                ])])
                self.assertEqual(result["steps"], [])
                self.assertIn("quote_mismatch", result["coverage"]["reasons"])

    def test_bounds_and_explicit_partial_coverage(self):
        result = self.normalize([self.step(from_node="catalog_discovery") for _ in range(13)])
        self.assertEqual(len(result["steps"]), 12)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["omitted_steps"], 1)
        too_many_refs = self.normalize([self.step(evidence=[{"message_id": 10, "quote": "Хочу"}] * 9)])
        self.assertIn("evidence_bounds", too_many_refs["coverage"]["reasons"])
        by_id = {i: {"message_id": i, "role": "user", "text": "evidence"} for i in range(1, 73)}
        steps = [self.step(from_node="catalog_discovery", evidence=[
            {"message_id": i, "quote": "evidence"} for i in range(start, start + 8)
        ]) for start in range(1, 73, 8)]
        bounded = self.normalize(steps, by_id=by_id, watermark=72)
        self.assertEqual(bounded["coverage"]["cited_message_count"], 64)
        self.assertEqual(len(bounded["steps"]), 8)
        self.assertEqual(bounded["coverage"]["reasons"]["unique_message_limit"], 1)

    def test_confidence_is_finite_probability_and_low_confidence_is_not_promoted(self):
        for confidence in (float("nan"), float("inf"), -float("inf"), .69, -1, 1.1, True, "0.9", 10 ** 1000):
            with self.subTest(confidence=str(confidence)[:20]):
                result = self.normalize([self.step(confidence=confidence)])
                self.assertEqual(result["steps"], [])
                self.assertIn("confidence_invalid", result["coverage"]["reasons"])
        self.assertEqual(len(self.normalize([self.step(confidence=.7)])["steps"]), 1)

    def test_no_invented_adjacency_and_empty_origin_only_at_start(self):
        steps = [self.step(from_node=""), self.step(from_node="payment_help", to_node="awaiting_payment", kind="retry", reason_code="payment_problem")]
        result = self.normalize(steps, "awaiting_payment")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["disconnected_steps"], 1)
        self.assertEqual([(row["from_node"], row["to_node"]) for row in result["steps"]], [("", "catalog_discovery"), ("payment_help", "awaiting_payment")])
        invalid_start = self.normalize([self.step(confidence=.1), self.step(from_node="")])
        self.assertEqual(invalid_start["steps"], [])
        self.assertEqual(invalid_start["coverage"]["trace_start"], "unobserved")

    def test_unknown_registry_keys_commands_and_arbitrary_labels_never_escape(self):
        for changes in ({"to_node": "secret-paid-approved"}, {"kind": "send_message"}, {"reason_code": "Private phone 0123456789"}, {"label": "PRIVATE LABEL"}, {"authority": "payment_verified"}):
            result = self.normalize([self.step(**changes)])
            self.assertEqual(result["steps"], [])
            self.assertNotIn("0123456789", json.dumps(result))
            self.assertNotIn("PRIVATE LABEL", json.dumps(result))
        raw = {"schema_version": 1, "steps": [self.step()], "current_node": "catalog_discovery", "command": "send NOW"}
        result = normalize_journey_trace(raw, by_id=self.by_id, watermark=12)
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("send NOW", json.dumps(result))

    def test_missing_schema_and_trusted_index_boundaries(self):
        for raw in (None, {}, {"schema_version": 1, "steps": [], "current_node": ""}):
            self.assertEqual(normalize_journey_trace(raw, by_id=self.by_id, watermark=12)["status"], "missing")
        for watermark in (True, -1, "12"):
            self.assertEqual(self.normalize([self.step()], watermark=watermark)["status"], "rejected")
        self.assertEqual(self.normalize([self.step()], by_id={True: self.by_id[10]})["status"], "rejected")
        self.assertEqual(normalize_journey_trace({"schema_version": True, "steps": [], "current_node": ""}, by_id={}, watermark=0)["status"], "rejected")

    def test_constants_match_registry_without_importing_django(self):
        path = Path(__file__).parent / "services" / "ig_funnel_nodes.py"
        tree = ast.parse(path.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_semantic_definitions")
        keys = {
            node.args[0].value for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "route" and node.args and isinstance(node.args[0], ast.Constant)
        }
        self.assertEqual(len(keys), 44)
        self.assertEqual(SEMANTIC_NODE_KEYS, keys)

    def test_normalized_schema_rejects_extra_text_authority_and_invalid_coverage(self):
        valid = self.normalize([self.step()])
        changes = [
            ((), "label", "PRIVATE LABEL"), ((), "authority", "payment_verified"),
            ((), "provenance", "provider_receipt"), ((), "schema_version", True),
            (("steps", 0), "quote", "PRIVATE CUSTOMER TEXT"),
            (("coverage",), "omitted_steps", -1), (("coverage",), "input_steps", True),
            (("coverage",), "inspected_steps", 13), (("coverage",), "cited_message_count", 64),
            (("coverage",), "earlier_history", "complete"),
            (("coverage",), "reasons", {"arbitrary customer text": 1}),
            (("coverage",), "reasons", {"step_limit": -1}),
            (("coverage",), "reasons", {"confidence_invalid": 1}),
        ]
        for path, key, value in changes:
            malformed = deepcopy(valid)
            destination = malformed
            for part in path:
                destination = destination[part]
            destination[key] = value
            with self.subTest(path=path, key=key, value=value):
                self.assertFalse(validate_normalized_journey_trace(malformed))
        for invalid in (None, [], "PRIVATE TEXT", {}):
            self.assertFalse(validate_normalized_journey_trace(invalid))

    def test_normalized_current_focus_roles_hashes_and_confidence_are_checked(self):
        valid = self.normalize([self.step()])
        for key, value in (("message_id", True), ("message_id", 13), ("role", "model"), ("role", "admin"), ("source_text_sha256", "not-a-digest"), ("quote", "PRIVATE TEXT")):
            malformed = deepcopy(valid)
            malformed["steps"][0]["evidence"][0][key] = value
            with self.subTest(key=key, value=value):
                self.assertFalse(validate_normalized_journey_trace(malformed))
        for confidence in (float("nan"), float("inf"), True, .69, 10 ** 1000):
            malformed = deepcopy(valid)
            malformed["steps"][0]["confidence"] = confidence
            self.assertFalse(validate_normalized_journey_trace(malformed))
        malformed = deepcopy(valid)
        malformed["current_node"] = "settlement"
        self.assertFalse(validate_normalized_journey_trace(malformed))
        malformed = deepcopy(valid)
        malformed["coverage"]["current_node_omitted"] = True
        self.assertFalse(validate_normalized_journey_trace(malformed))

    def test_short_explanations_are_optional_bounded_and_render_safe(self):
        summary = "Розмір L відсутній; клієнт погодився чекати."
        result = self.normalize([{**self.step(), "summary": summary}])
        self.assertEqual(result["steps"][0]["summary"], summary)
        self.assertTrue(validate_normalized_journey_trace(result))
        for unsafe in ("x" * 141, "<img src=x>", "https://example.org", "+380 99 123 45 67", {"text": "x"}):
            with self.subTest(unsafe=unsafe):
                result = self.normalize([{**self.step(), "summary": unsafe}])
                self.assertEqual(result["steps"][0]["summary"], "")
                self.assertEqual(len(result["steps"]), 1)
                result["steps"][0]["summary"] = unsafe
                self.assertFalse(validate_normalized_journey_trace(result))

    def test_normalized_signature_consistency_and_external_source_check_boundary(self):
        valid = self.normalize([self.step(), self.step(from_node="catalog_discovery")])
        for key, value in (("role", "manager"), ("source_text_sha256", "a" * 64)):
            malformed = deepcopy(valid)
            malformed["steps"][1]["evidence"][0][key] = value
            self.assertFalse(validate_normalized_journey_trace(malformed))
        # A well-formed but forged hash needs the caller's owned-source check;
        # this pure shape validator must not claim to authenticate it.
        structurally_valid = self.normalize([self.step()])
        structurally_valid["steps"][0]["evidence"][0]["source_text_sha256"] = "a" * 64
        self.assertTrue(validate_normalized_journey_trace(structurally_valid))
        for raw in (None, {}, {"schema_version": True, "steps": [], "current_node": ""}):
            result = normalize_journey_trace(raw, by_id=self.by_id, watermark=12)
            self.assertTrue(validate_normalized_journey_trace(result))


if __name__ == "__main__":
    unittest.main()
