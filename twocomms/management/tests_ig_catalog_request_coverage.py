"""Execute request coverage and legacy ownership branches without DB or I/O."""
import ast
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SERVICES = Path(__file__).parent / "services"


def module(name, **values):
    result = ModuleType(name)
    result.__dict__.update(values)
    return result


def plan_functions():
    tree = ast.parse((SERVICES / "ig_response_plan.py").read_text())
    names = {"_requested_topics", "_unquoted_text", "_topic_covered"}
    nodes = [row for row in tree.body if isinstance(row, ast.FunctionDef) and row.name in names]
    nodes += [row for row in tree.body if isinstance(row, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in {"_PAYMENT_WORDS", "_NEGATED_CHOICE_RE"} for target in row.targets)]
    namespace = {"re": re, "Decimal": Decimal, "InvalidOperation": InvalidOperation}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "ig_response_plan.py", "exec"), namespace)
    return namespace


def legacy_ownership_branch(namespace):
    tree = ast.parse((SERVICES / "instagram_bot.py").read_text())
    process = next(row for row in tree.body if isinstance(row, ast.FunctionDef)
        and row.name == "_process_one_inside_reply_boundary")
    branch = next(row for row in ast.walk(process) if isinstance(row, ast.If)
        and ast.unparse(row.test) == "commerce_decision is not None and commerce_decision.delivery_required")
    # Execute the actual production branch, preserving its returns. The sentinel
    # identifies continuation to the existing classifier/Gemini/media path.
    wrapper = ast.parse("def execute():\n    return ('continue_model_path', live_commerce_decision, live_commerce_fallback)\n").body[0]
    wrapper.body = [deepcopy(branch), *wrapper.body]
    executable = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    exec(compile(executable, "instagram_bot.py", "exec"), namespace)
    return namespace["execute"]


class CatalogRequestCoverageTests(unittest.TestCase):
    def setUp(self):
        self.plan_functions = plan_functions()
        self.truth = module("management.services.ig_reply_truth",
            _locally_negated=lambda clause, position: bool(re.search(r"\b(?:не|not)\b", clause[:position], re.I)),
            ReplyTruthContext=lambda **values: SimpleNamespace(**values),
            _claim_sentences=lambda text, _extra: [text], _MONEY_RE=re.compile(r"(\d+)"),
            _TIMING_RE=re.compile(r"never-match"), _RECRUITMENT_STATUS_RE=re.compile(r"never-match"),
            _RECRUITMENT_UNCERTAINTY_RE=re.compile(r"never-match"),
            _is_qualified_standard_dispatch_timing=Mock(return_value=False),
            validate_reply_truth=Mock(return_value=SimpleNamespace(valid=True)))
        self.topic_module = module("management.services.ig_response_plan", _requested_topics=self.plan_functions["_requested_topics"])

    def test_photo_and_link_source_keeps_photo_obligation_and_link_only_cannot_cover_it(self):
        text = "Покажи фото цього товару та скинь посилання на нього: https://twocomms.shop/product/model/"
        plan = SimpleNamespace(authority={"prices": [], "standard_dispatch_days": [],
            "timing_claims": [], "payment_confirmed": False}, configuration={"product_id": 101})
        response = SimpleNamespace(reply_text="Ось посилання: https://twocomms.shop/product/model/",
            control={"catalog_link": True})
        with patch.dict(sys.modules, {self.truth.__name__: self.truth}):
            self.assertIn("info:presentation", self.plan_functions["_requested_topics"](text))
            self.assertFalse(self.plan_functions["_topic_covered"]("info:presentation", response, plan))
            response.control["show_products"] = [101]
            self.assertTrue(self.plan_functions["_topic_covered"]("info:presentation", response, plan))

    def run_legacy_branch(self, text, *, ai_enabled=True, claimed=True, inbound_media=()):
        decision = SimpleNamespace(delivery_required=True, delivery_state="pending", reconciliation_result={},
            DeliveryState=SimpleNamespace(NOT_REQUIRED="not_required"))
        row = SimpleNamespace(text=text, client_id=7, sender_id="customer-7", pk=20,
            role="user", source="webhook")
        claim = Mock(return_value=decision if claimed else None)
        durable_send = Mock(return_value="durable_sent")
        namespace = {"s": SimpleNamespace(ai_enabled=ai_enabled), "row": row,
            "commerce_decision": decision, "commerce_request": SimpleNamespace(exact_product_id=101), "turn_media_parts": inbound_media,
            "LIVE_MEDIA_COMMERCE_OWNER": "live_media_turn_v1", "lease_token": "actual-lease", "permission": "actual-permission",
            "_claim_durable_commerce_for_live_media": claim, "_durable_commerce_text": Mock(return_value="stored fallback"),
            "_deliver_durable_commerce_reply": durable_send, "clear_typing_indicator": Mock(),
            "live_routing_decision": Mock(return_value="routing"), "live_commerce_decision": None}
        execute = legacy_ownership_branch(namespace)
        modules = {self.truth.__name__: self.truth, self.topic_module.__name__: self.topic_module,
            "management.services.gemini_routing": module("management.services.gemini_routing", persist_decision=Mock())}
        with patch.dict(sys.modules, modules):
            result = execute()
        if isinstance(result, tuple):
            result, namespace["live_commerce_decision"], namespace["live_commerce_fallback"] = result
        return result, claim, durable_send, row, namespace

    def test_actual_own_photo_link_request_transfers_once_and_continues_model_path(self):
        result, claim, durable_send, row, namespace = self.run_legacy_branch(
            "Покажи фото цього товару та скинь посилання на нього: https://twocomms.shop/product/model/")
        self.assertEqual(result, "continue_model_path")
        claim.assert_called_once()
        self.assertIs(claim.call_args.args[1], row)
        self.assertEqual(claim.call_args.kwargs, {"lease_token": "actual-lease", "permission": "actual-permission"})
        self.assertIs(namespace["live_commerce_decision"], claim.return_value)
        self.assertEqual(namespace["live_commerce_fallback"], "stored fallback")
        durable_send.assert_not_called()

    def test_quoted_photo_request_does_not_transfer_or_bypass_durable_reply(self):
        for text in ("Він написав «Покажи фото» https://twocomms.shop/product/model/",
            'He said "show photos" https://twocomms.shop/product/model/'):
            with self.subTest(text=text):
                result, claim, durable_send, *_ = self.run_legacy_branch(text)
                self.assertEqual(result, "durable_sent")
                claim.assert_not_called()
                durable_send.assert_called_once()

    def test_ai_disabled_plain_product_reference_keeps_durable_reply(self):
        result, claim, durable_send, *_ = self.run_legacy_branch("Покажи фото цього товару", ai_enabled=False)
        self.assertEqual(result, "durable_sent")
        claim.assert_not_called()
        durable_send.assert_called_once()

    def test_failed_existing_ownership_claim_cannot_start_second_reply_path(self):
        result, claim, durable_send, *_ = self.run_legacy_branch("Покажи фото цього товару", claimed=False)
        self.assertEqual(result, "durable_sent")
        claim.assert_called_once()
        durable_send.assert_called_once()

    def test_inbound_media_keeps_existing_guarded_ownership_transfer(self):
        result, claim, durable_send, *_ = self.run_legacy_branch("https://twocomms.shop/product/model/", inbound_media=({"mime": "image/jpeg"},))
        self.assertEqual(result, "continue_model_path")
        claim.assert_called_once()
        durable_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
