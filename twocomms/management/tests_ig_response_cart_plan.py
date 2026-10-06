"""Pure source-cart response tests; no Django setup, database or provider I/O."""
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_response_cart_plan import source_line_preferences, matching_line_ids
from management.services.ig_response_plan import build_response_plan, capture_response_plan


class ResponseCartPlanTests(unittest.TestCase):
    def capture(self, *, duplicate=False, fields=None):
        capture = {"schema": "source-selections.v1", "status": "captured", "coverage_complete": True,
            "scope": {"client_id": 4, "episode_id": 7, "order_id": None, "reset_id": None,
                "reset_floor": 1, "source_namespace": "test:ig"}, "session_id": 10,
            "generation": 2, "selection_revision": 3, "active_line_id": "second",
            "capture_digest": "a" * 64, "fence": {"snapshot_digest": "b" * 64, "source_ids": [8]}, "lines": []}
        for index, line_id in enumerate(("first", "second")):
            recipient = ("alice", "bob")[index] if duplicate else "self"
            values = fields[index] if fields else {"product_id": 55 if duplicate else 55 + index,
                "size": ("M", "L")[index], "color": ("black", "pink")[index],
                "garment_type": "hoodie" if duplicate else ("hoodie", "tshirt")[index]}
            evidence = {key: {"source_message_id": 8, "source_digest": "c" * 64, "transition_id": 12,
                "operation_index": index, "authority": "customer_source"} for key in values}
            scope = {**capture["scope"], "session_id": 10, "generation": 2, "revision": 3,
                "line_id": line_id, "recipient_id": recipient}
            capture["lines"].append({"line_id": line_id, "recipient_id": recipient, "index": index,
                "evidence": evidence, "source_selection": {"scope": scope},
                "fields": {key: {"value": value, "status": "confirmed", "source": evidence[key]} for key, value in values.items()}})
        return capture

    def plan(self, capture=None, *, sources=None, missing=None, contexts=None, payment_observation=None):
        capture = capture or self.capture()
        readiness = {row["line_id"]: {"has_product": True, "product": {"id": row["fields"].get("product_id", {}).get("value"),
            "title": "Classic" if row["index"] == 0 or row["fields"].get("product_id", {}).get("value") == 55 else "Storm"},
            "missing": (missing or {}).get(row["line_id"], []), "applicability_known": False} for row in capture["lines"]}
        return build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(),
            sources=sources or [{"message_id": 8, "role": "user", "text": "Худі M і футболка L"}],
            captured_cart=capture, readiness_by_line=readiness, contexts_by_line=contexts, payment_observation=payment_observation)

    def response(self, text, control=None):
        return SimpleNamespace(reply_text=text, control=control or {})

    def test_same_source_two_lines_preserve_distinct_fields_and_ids(self):
        plan = self.plan()
        ids = [row["id"] for row in plan.obligations]
        self.assertIn("8:first:size", ids)
        self.assertIn("8:second:size", ids)
        self.assertEqual(len(ids), len(set(ids)))
        coverage = plan.coverage(self.response("Ви обрали для Storm розмір L."))
        self.assertIn("8:second:size", coverage["covered"])
        self.assertIn("8:first:size", coverage["remaining"])

    def test_product_identity_must_stay_in_its_clause(self):
        plan = self.plan()
        coverage = plan.coverage(self.response("Ви обрали для Storm розмір M. Ви обрали розмір L."))
        self.assertIn("8:first:size", coverage["remaining"])
        self.assertIn("8:second:size", coverage["remaining"])

    def test_explicit_comma_separated_lines_cover_their_own_choices(self):
        coverage = self.plan().coverage(self.response("Ви обрали для Classic розмір M, ви обрали для Storm розмір L."))
        self.assertIn("8:first:size", coverage["covered"])
        self.assertIn("8:second:size", coverage["covered"])

    def test_same_sku_requires_recipient_or_number(self):
        plan = self.plan(self.capture(duplicate=True))
        self.assertEqual(matching_line_ids("Ви обрали для Classic розмір M", plan.line_plans), ())
        self.assertEqual(matching_line_ids("Ви обрали для Classic alice розмір M", plan.line_plans), ("first",))
        self.assertEqual(matching_line_ids("Позиція 2: ви обрали розмір L", plan.line_plans), ("second",))
        coverage = plan.coverage(self.response("Ви обрали для Classic розмір M."))
        self.assertIn("8:first:size", coverage["remaining"])

    def test_conflicting_ordinal_and_title_is_ambiguous(self):
        plan = self.plan()
        self.assertEqual(matching_line_ids("Позиція 1 Storm коштує 790 грн", plan.line_plans), ())

    def test_conflicting_ordinal_and_recipient_is_ambiguous(self):
        plan = self.plan(self.capture(duplicate=True))
        self.assertEqual(matching_line_ids("Позиція 1 для bob розмір M", plan.line_plans), ())

    def test_independent_exact_price_question_binds_its_unique_line(self):
        plan = self.plan(sources=[{"message_id": 8, "role": "user", "text": "Худі M і футболка L"},
            {"message_id": 9, "role": "user", "text": "Скільки коштує Classic?"}],
            contexts={"first": ReplyTruthContext(authorized_prices=(Decimal("790"),))})
        coverage = plan.coverage(self.response("Classic коштує 790 грн."))
        self.assertIn("9:first:info:price", coverage["covered"])

    def receipt(self, source_id=9):
        return {"observation": {"state": "observed", "source_message_ids": [source_id], "receipts": [
            {"source_message_id": source_id, "role": "receipt", "state": "inspected", "source_part_id": "receipt-part",
                "content_hash": "a" * 64, "receipt_facts": {"payment_status": "completed"}}]},
            "source_refs": [{"message_id": source_id, "source_digest": "b" * 64}]}

    def test_multiline_receipt_response_keeps_payment_pending(self):
        plan = self.plan(sources=[{"message_id": 9, "role": "user", "text": "Ось квитанція"}],
            missing={"first": ["fit"]}, payment_observation=self.receipt())
        self.assertEqual(plan.payment_observation["state"], "observed")
        self.assertEqual(plan.next_selector_line, {})
        self.assertEqual(plan.next_selector, "")
        coverage = plan.coverage(self.response("Квитанцію отримано, оплата очікує перевірки."))
        self.assertEqual(coverage["disposition"], "complete")
        self.assertEqual(coverage["payment_verification"], "unresolved")
        self.assertEqual(plan.validate(self.response("Квитанцію передано менеджеру.")), "response_plan_payment_forwarding_unverified")

    def test_receipt_and_two_line_price_obligations_are_independent(self):
        plan = self.plan(sources=[{"message_id": 8, "role": "user", "text": "Худі M і футболка L"},
            {"message_id": 9, "role": "user", "text": "Квитанція. Скільки коштує Classic?"}],
            contexts={"first": ReplyTruthContext(authorized_prices=(Decimal("790"),))}, payment_observation=self.receipt())
        coverage = plan.coverage(self.response("Classic коштує 790 грн. Квитанцію отримано, оплата очікує перевірки."))
        self.assertIn("9:first:info:price", coverage["covered"])
        self.assertIn("9:payment:receipt", coverage["covered"])
        self.assertIn("8:second:size", coverage["remaining"])

    def test_obligation_overflow_is_named_without_truncation(self):
        capture = self.capture()
        template = capture["lines"][0]
        import copy
        fields = {"product_id": 55, "model_query": "Classic", "size": "M", "fit_option_code": "classic",
            "color": "black", "quantity": 2, "garment_type": "hoodie", "purchase_requested": True}
        capture["lines"] = []
        for index in range(16):
            row = copy.deepcopy(template)
            row["line_id"], row["index"] = f"row{index}", index
            row["source_selection"]["scope"]["line_id"] = row["line_id"]
            row["fields"] = {key: {"value": value, "status": "confirmed", "source": template["fields"]["size"]["source"]} for key, value in fields.items()}
            capture["lines"].append(row)
        plan = self.plan(capture)
        self.assertEqual(plan.plan_gap, "response_plan_obligation_bound")
        self.assertEqual(plan.coverage(self.response("Готово"))["disposition"], "recovery")

    def test_source_with_no_surviving_line_stays_finite_debt(self):
        plan = self.plan(sources=[{"message_id": 8, "role": "user", "text": "Худі M і футболка L"},
            {"message_id": 9, "role": "user", "text": "Приберіть стару футболку"}])
        self.assertIn("9:unresolved_line_operation", [row["id"] for row in plan.obligations])
        self.assertIn("9:unresolved_line_operation", plan.coverage(self.response("Ви обрали для Classic розмір M."))["remaining"])

    def test_color_replacement_only_current_color_is_owed(self):
        capture = self.capture()
        capture["lines"][0]["fields"]["color"]["value"] = "pink"
        capture["lines"][0]["history"] = [{"field": "color", "value": "black", "source_message_id": 7, "superseded_by": 12}]
        plan = self.plan(capture)
        color = next(row for row in plan.obligations if row["id"] == "8:first:color")
        self.assertEqual(color["value"], "pink")
        self.assertIn("8:first:color", plan.coverage(self.response("Ви обрали для Classic колір чорний."))["remaining"])
        self.assertIn("8:first:color", plan.coverage(self.response("Ви обрали для Classic колір рожевий."))["covered"])

    def test_line_scope_mismatch_is_finite_gap(self):
        capture = self.capture()
        capture["lines"][0]["source_selection"]["scope"]["episode_id"] = 9
        plan = self.plan(capture)
        self.assertEqual(plan.plan_gap, "response_plan_line_source_unavailable")
        self.assertEqual(plan.validate(self.response("Вітаємо")), plan.plan_gap)
        self.assertEqual(plan.coverage(self.response("Вітаємо"))["disposition"], "recovery")

    def test_newer_and_unfenced_field_cannot_enter_plan(self):
        capture = self.capture()
        capture["lines"][0]["fields"]["size"]["source"]["source_message_id"] = 9
        preferences = source_line_preferences(capture, capture["lines"][0], source_watermark=8)
        self.assertNotIn("size", preferences["values"])
        self.assertEqual(self.plan(capture).plan_gap, "response_plan_line_source_unavailable")

    def test_original_line_projection_supplies_actual_compatibility_scope(self):
        # Execute the production pure serializer's unchanged AST body without
        # importing projection's unrelated Django model adapter at module load.
        import ast
        from pathlib import Path
        path = Path(__file__).parent / "services" / "ig_commerce_projection.py"
        parsed = ast.parse(path.read_text())
        serializer = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == "captured_selection_from_preferences")
        namespace = {}
        exec(compile(ast.Module(body=[serializer], type_ignores=[]), str(path), "exec"), namespace)
        actual_serializer = namespace["captured_selection_from_preferences"]
        capture = self.capture()
        capture["active_index"] = 1
        for row in capture["lines"]:
            projection = source_line_preferences(capture, row)
            actual = actual_serializer(4, projection)
            self.assertEqual(actual["scope"], {"client_id": 4, "session_id": 10, "generation": 2,
                "revision": 3, "active_index": row["index"], "episode_id": 7,
                "line_id": row["line_id"], "recipient_id": "self", "reset_floor": 1})
            self.assertEqual(actual["fields"]["size"]["value"], row["fields"]["size"]["value"])
        active_row = capture["lines"][capture["active_index"]]
        self.assertEqual(source_line_preferences(capture, active_row)["active_index"], capture["active_index"])

    def test_incomplete_capture_never_returns_complete_coverage(self):
        capture = self.capture()
        capture["coverage_complete"] = False
        plan = self.plan(capture)
        self.assertEqual(plan.plan_gap, "response_plan_cart_capture_unavailable")
        self.assertEqual(plan.coverage(self.response("Ви обрали для Storm розмір L."))["disposition"], "recovery")

    def test_exact_price_is_valid_only_for_its_own_line(self):
        contexts = {"first": ReplyTruthContext(authorized_prices=(Decimal("790"),)),
            "second": ReplyTruthContext(authorized_prices=(Decimal("1290"),))}
        plan = self.plan(contexts=contexts)
        for separator in ("; ", ", "):
            self.assertEqual(plan.validate_multiline_claims(self.response("Classic коштує 790 грн" + separator + "Storm коштує 1290 грн."), ReplyTruthContext()), "")
            self.assertEqual(plan.validate_multiline_claims(self.response("Classic коштує 1290 грн" + separator + "Storm коштує 790 грн."), ReplyTruthContext()), "response_plan_line_claim_unverified")
        self.assertEqual(plan.validate_multiline_claims(self.response("Ціна 790 грн."), ReplyTruthContext()), "response_plan_line_claim_unverified")

    def test_decimal_comma_prices_remain_whole_with_separate_line_assertions(self):
        from management.services.ig_response_cart_plan import scoped_claim_clauses
        text = "Classic коштує 699,00 грн, Storm коштує 1299,50 грн."
        self.assertEqual(list(scoped_claim_clauses(text)), ["Classic коштує 699,00 грн", " Storm коштує 1299,50 грн"])
        plan = self.plan(sources=[{"message_id": 8, "role": "user", "text": "Худі M і футболка L"},
            {"message_id": 9, "role": "user", "text": "Скільки коштує Classic?"},
            {"message_id": 10, "role": "user", "text": "Скільки коштує Storm?"}],
            contexts={"first": ReplyTruthContext(authorized_prices=(Decimal("699.00"),)),
                "second": ReplyTruthContext(authorized_prices=(Decimal("1299.50"),))})
        self.assertEqual(plan.validate_multiline_claims(self.response(text), ReplyTruthContext()), "")
        coverage = plan.coverage(self.response(text))
        self.assertIn("9:first:info:price", coverage["covered"])
        self.assertIn("10:second:info:price", coverage["covered"])
        self.assertEqual(plan.validate_multiline_claims(self.response("Classic коштує 1299,50 грн, Storm коштує 699,00 грн."), ReplyTruthContext()), "response_plan_line_claim_unverified")

    def sealed_capture(self, *, single=False):
        from management.services.ig_turn_intelligence import capture_digest
        capture = self.capture()
        stamp = "2026-10-06T10:00:00+00:00"
        if single:
            capture["lines"] = capture["lines"][:1]
        capture.update(active_index=len(capture["lines"]) - 1, active_line_id=capture["lines"][-1]["line_id"],
            line_limit=16, transition_limit=64, query_limit=64, omissions=[],
            source_watermark={"message_id": 8, "event_at": stamp})
        capture["fence"].update(namespace="test:ig", source_watermark=capture["source_watermark"],
            owner_digest="a" * 64, source_digest="b" * 64)
        for row in capture["lines"]:
            row["history"] = []
            selection = row["source_selection"]
            selection.update(schema="source-selection.v1", fields=row["fields"],
                values={key: field["value"] for key, field in row["fields"].items()}, evidence=row["evidence"])
            selection["scope"]["active_index"] = row["index"]
            for key, field in row["fields"].items():
                field["authority"] = "customer_source"
                field["source"]["observed_at"] = stamp
                if single:
                    field["source"].pop("operation_index", None)
        capture["capture_digest"] = capture_digest({key: value for key, value in capture.items() if key != "capture_digest"})
        client = SimpleNamespace(pk=4, current_commercial_episode_id=7, privacy_erasure_started_at=None)
        bundle = {"sources": [{"message_id": 8, "role": "user", "text": "Худі M і футболка L",
            "source_namespace": "test:ig", "provider_created_at": stamp}]}
        revision = SimpleNamespace(client_id=4, bundle_snapshot=bundle, snapshot_digest=capture_digest(bundle), erasure_started_at_snapshot=None)
        return capture, client, revision

    def test_explicit_original_cart_single_and_multiline_never_recaptures(self):
        for single in (True, False):
            with self.subTest(single=single):
                capture, client, revision = self.sealed_capture(single=single)
                original_digest = capture["capture_digest"]
                def build(_client, **kwargs):
                    return self.plan(kwargs["cart_capture"], sources=kwargs["sources"])
                with patch("management.services.ig_response_plan._capture_cart_response_plan", side_effect=build) as cart_builder:
                    plan = capture_response_plan(client, revision=revision, source_cart_capture=capture)
                self.assertEqual(cart_builder.call_count, 1)
                handed = cart_builder.call_args.kwargs["cart_capture"]
                self.assertEqual(handed, capture)
                self.assertIsNot(handed, capture)
                self.assertEqual(plan.source_cart_capture["capture_digest"], original_digest)
                handed["lines"][0]["fields"]["size"]["value"] = "XL"
                self.assertEqual(capture["lines"][0]["fields"]["size"]["value"], "M")

    def test_explicit_cart_malformed_wrong_owner_or_after_seal_is_finite(self):
        from management.services.ig_turn_intelligence import capture_digest
        for fault in ("digest", "episode", "namespace", "after_seal", "empty"):
            with self.subTest(fault=fault):
                capture, client, revision = self.sealed_capture()
                if fault == "digest":
                    capture["lines"][0]["fields"]["size"]["value"] = "XL"
                elif fault == "episode":
                    client.current_commercial_episode_id = 9
                elif fault == "namespace":
                    revision.bundle_snapshot["sources"][0]["source_namespace"] = "other:ig"
                    revision.snapshot_digest = capture_digest(revision.bundle_snapshot)
                elif fault == "after_seal":
                    capture["fence"]["source_ids"].append(9)
                    capture["capture_digest"] = capture_digest({key: value for key, value in capture.items() if key != "capture_digest"})
                else:
                    capture = {}
                with patch("management.services.ig_response_plan._capture_cart_response_plan") as builder:
                    plan = capture_response_plan(client, revision=revision, source_cart_capture=capture)
                builder.assert_not_called()
                self.assertTrue(plan.plan_gap)
                self.assertEqual(plan.coverage(self.response("Вітаємо"))["disposition"], "recovery")

    def test_source_wish_does_not_grant_availability(self):
        plan = self.plan()
        self.assertEqual(plan.validate_multiline_claims(self.response("Ви обрали для Classic розмір M."), ReplyTruthContext()), "")
        self.assertEqual(plan.validate_multiline_claims(self.response("Classic розмір M є в наявності."), ReplyTruthContext()), "response_plan_line_claim_unverified")

    def test_ambiguous_price_question_does_not_use_any_line_price(self):
        plan = self.plan(sources=[{"message_id": 8, "role": "user", "text": "Худі M і футболка L. Скільки коштує?"}],
            contexts={"first": ReplyTruthContext(authorized_prices=(Decimal("790"),))})
        coverage = plan.coverage(self.response("Classic коштує 790 грн."))
        self.assertIn("8:info:price", coverage["remaining"])

    def test_question_selects_one_missing_line_and_preserves_other_size(self):
        plan = self.plan(missing={"first": ["fit", "size"], "second": ["color"]})
        self.assertEqual(plan.next_selector_line["line_id"], "first")
        self.assertEqual(plan.next_selector_line["field"], "fit")
        self.assertEqual(plan.validate(self.response("Яку посадку для Classic ви обираєте?")), "")
        for text in ("Який розмір для Storm ви обираєте?", "Яку посадку ви обираєте?", "Який колір для Storm ви обираєте?"):
            self.assertEqual(plan.validate(self.response(text)), "response_plan_line_selector_mismatch")

    def test_plan_json_is_detached_and_contains_no_full_cart_history(self):
        plan = self.plan()
        row = plan.line_plans[0].as_dict()
        row["choices"]["size"] = "XL"
        self.assertEqual(plan.line_plans[0].as_dict()["choices"]["size"], "M")
        self.assertNotIn("history", plan.as_dict()["line_plans"][0])

    def test_prompt_compacts_eight_lines_without_dropping_any_obligation(self):
        import copy
        import json
        capture = self.capture()
        template = capture["lines"][0]
        capture["lines"] = []
        for index in range(8):
            row = copy.deepcopy(template)
            row["line_id"], row["index"] = f"line:8:{index}", index
            row["source_selection"]["scope"]["line_id"] = row["line_id"]
            values = {"product_id": 55 + index, "model_query": "SOURCE_MODEL_VALUE", "size": "M",
                "fit_option_code": "classic", "color": "black", "quantity": 2,
                "garment_type": "hoodie", "purchase_requested": True}
            proof = copy.deepcopy(template["fields"]["size"]["source"])
            proof["private_full_proof"] = "FULL_ARTIFACT_ONLY" * 100
            row["fields"] = {key: {"value": value, "status": "confirmed", "source": proof} for key, value in values.items()}
            row["evidence"] = {key: proof for key in values}
            capture["lines"].append(row)
        plan = self.plan(capture)
        before, digest = plan.as_dict(), plan.digest
        projection = plan.prompt_projection()
        prompt = plan.prompt_guidance()
        self.assertEqual(len(plan.obligations), 64)
        actual = {(item[0], item[1], group["line_id"], group["source_message_id"])
            for group in projection["obligation_groups"] for item in
            ([[group["id_prefix"] + kind, kind] for kind in group["obligation_kinds"]]
                if "obligation_kinds" in group else group["obligations"])}
        expected = {(item["id"], item["kind"], item["line_id"], item["source_message_id"]) for item in plan.obligations}
        self.assertEqual(actual, expected)
        self.assertEqual(len(projection["lines"]), 8)
        self.assertLess(len(prompt), 6000)
        self.assertLess(len(prompt), len(json.dumps(before)))
        self.assertNotIn("FULL_ARTIFACT_ONLY", prompt)
        self.assertNotIn("SOURCE_MODEL_VALUE", prompt)
        self.assertEqual(plan.as_dict(), before)
        self.assertEqual(projection["plan_digest"], digest)
        self.assertEqual(plan.digest, digest)

    def test_compact_prompt_keeps_receipt_and_nonactive_audit_semantics(self):
        import json
        capture = self.capture()
        size = capture["lines"][0]["fields"]["size"]
        size["source"].update(authority="audited_correction", correction={"receipt": {
            "schema": "manager-correction.v1", "field": "size", "after": "M", "actor": "RAW_RECEIPT_ONLY"}})
        plan = self.plan(capture, sources=[{"message_id": 8, "role": "user", "text": "Худі M і футболка L"},
            {"message_id": 9, "role": "user", "text": "Квитанція"}], payment_observation=self.receipt())
        before, digest = plan.as_dict(), plan.digest
        projection = plan.prompt_projection()
        field = next(projection["choice_source_groups"][identity] for identity in projection["lines"][0]["choice_source_refs"]
            if "size" in projection["choice_source_groups"][identity]["fields"])
        self.assertEqual((field["authority"], field["source_message_id"]), ("audited_correction", 8))
        self.assertFalse(projection["payment_observation"]["payment_verified"])
        self.assertEqual(projection["payment_observation"]["verification"], "unresolved")
        prompt = plan.prompt_guidance()
        self.assertIn("audited manager correction", prompt)
        self.assertIn("reported evidence", prompt)
        self.assertIn("SENT notification proof", prompt)
        self.assertNotIn("RAW_RECEIPT_ONLY", prompt)
        self.assertNotIn("content_hash", prompt)
        self.assertEqual(plan.as_dict(), before)
        self.assertEqual(plan.digest, digest)
        json.dumps(projection)

    def test_legacy_prompt_keeps_wishes_without_full_evidence_receipt(self):
        plan = build_response_plan(preferences={"values": {"size": "L"}, "evidence": {"size": {
            "source_message_id": 8, "source_digest": "a" * 64, "transition_id": 12}}},
            readiness={"missing": ["product"]}, context=ReplyTruthContext())
        self.assertEqual(plan.prompt_projection()["choices"], {"size": "L"})
        self.assertEqual(plan.prompt_projection()["next_selector"], "product")
        self.assertNotIn("source_digest", plan.prompt_guidance())

    def test_audited_size_stays_corrected_requirement_for_its_line(self):
        capture = self.capture()
        field = capture["lines"][0]["fields"]["size"]
        field["source"].update(authority="audited_correction", correction={"receipt": {
            "schema": "manager-correction.v1", "field": "size", "after": "M"}})
        plan = self.plan(capture)
        self.assertEqual(plan.validate_multiline_claims(self.response("Ви обрали для Classic розмір M."), ReplyTruthContext()), "response_plan_audited_choice_misattributed")
        self.assertEqual(plan.validate_multiline_claims(self.response("Для Classic уточнений розмір M."), ReplyTruthContext()), "")
        self.assertIn("8:first:size", plan.coverage(self.response("Для Classic уточнений розмір M."))["covered"])


class CheckoutPurchaseCoverageTests(unittest.TestCase):
    def fixture(self):
        from copy import deepcopy
        from management.tests_ig_revision_cart_binding import RevisionCartBindingTests
        owner = RevisionCartBindingTests()
        owner.setUp()
        cart = owner.cart
        cart.update(coverage_complete=True, fence={"source_ids": [20, 21]})
        for row in cart["lines"]:
            fields = row["source_selection"]["fields"]
            fields["purchase_requested"] = {**deepcopy(fields["size"]), "value": True}
            row["fields"] = deepcopy(fields)
            row["evidence"] = {key: field["source"] for key, field in fields.items()}
        binding = owner.ready()
        self.assertEqual(binding.status, "ready", binding.reason)
        plan = build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(),
            sources=[{"message_id": 20, "role": "user", "text": "Оформіть першу позицію"},
                {"message_id": 21, "role": "user", "text": "Оформіть другу позицію"}],
            captured_cart=cart)
        self.assertFalse(plan.plan_gap, plan.plan_gap)
        return owner, plan, binding

    def purchases(self, plan, coverage, key):
        return {item["id"] for item in plan.obligations
            if item["kind"] == "purchase_requested" and item["id"] in coverage[key]}

    def test_exact_backend_quote_covers_both_purchases_without_covering_other_choices(self):
        owner, plan, binding = self.fixture()
        response = SimpleNamespace(reply_text="Перевірте деталі замовлення.", control={"paylink": "full"})
        covered = plan.coverage(response, checkout_cart_binding=binding)
        self.assertEqual(self.purchases(plan, covered, "covered"),
            {"20:line-0:purchase_requested", "21:line-1:purchase_requested"})
        self.assertIn("20:line-0:size", covered["remaining"])
        self.assertIn("21:line-1:size", covered["remaining"])
        self.assertEqual(plan.source_cart_capture, owner.cart)

    def test_model_controls_boolean_partial_and_local_reply_cannot_cover_purchases(self):
        owner, plan, binding = self.fixture()
        response = SimpleNamespace(reply_text="Перевірте деталі.",
            control={"paylink": "full", "source_cart_binding": binding.binding})
        for proof, local in ((None, False), (True, False), (owner.build(), False), (binding, True)):
            with self.subTest(proof=type(proof).__name__, local=local):
                result = plan.coverage(response, checkout_cart_binding=proof, local=local)
                self.assertFalse(self.purchases(plan, result, "covered"))
                self.assertEqual(len(self.purchases(plan, result, "remaining")), 2)

    def test_foreign_recipient_position_configuration_or_capture_fails_as_a_whole(self):
        from copy import deepcopy
        owner, plan, binding = self.fixture()
        response = SimpleNamespace(reply_text="Перевірте деталі.", control={"paylink": "full"})
        for fault in ("client", "recipient", "position", "size", "capture", "missing_line"):
            value = deepcopy(binding.binding)
            if fault == "client":
                value["scope"]["client_id"] += 1
            elif fault == "recipient":
                value["quote_line_map"][1]["recipient_id"] = "somebody-else"
            elif fault == "position":
                value["quote_line_map"][0]["quote_position"] = 1
            elif fault == "size":
                value["quote_line_map"][1]["configuration"]["size"] = "XL"
            elif fault == "capture":
                value["source_capture_digest"] = "f" * 64
            else:
                value["quote_line_map"].pop()
            with self.subTest(fault=fault):
                result = plan.coverage(response, checkout_cart_binding=value)
                self.assertFalse(self.purchases(plan, result, "covered"))


if __name__ == "__main__":
    unittest.main()
