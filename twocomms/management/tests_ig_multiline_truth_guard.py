"""Pure actual guard and factory-boundary regressions; no DB/provider setup.

The two small factory/helper functions are compiled directly from their owned
source AST so importing Instagram's daemon does not start a Django app runtime.
The parser, guard, response plan, price extractor and truth validators are real.
"""
import ast
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from management import tests_ig_response_cart_plan as plan_fixtures
from management.services.gemini_routing import TaskClass
from management.services.ig_reply_truth import ReplyTruthContext, ReplyTruthResult, validate_reply_truth
from management.services.ig_response_guard import ProviderResponseGuard


def _owned_functions():
    path = Path(__file__).parent / "services" / "instagram_bot.py"
    tree = ast.parse(path.read_text())
    helper_names = {"_extract_authoritative_price_claim", "_provider_reply_truth_context"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in helper_names
        or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
            and target.id in {"_PRICE_CLAIM_RE", "_PRICE_RANGE_RE"} for target in node.targets)]
    environment = {"Decimal": Decimal, "InvalidOperation": InvalidOperation, "re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), environment)
    return tree, environment


def _actual_factory(boundary, *, context):
    tree, environment = _owned_functions()
    generate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "gemini_generate")
    nodes = [node for node in generate.body if isinstance(node, ast.FunctionDef) and node.name == "reply_truth_context"
        or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "response_guard" for target in node.targets)]
    environment.update(generation_boundary=boundary, client=SimpleNamespace(pk=1), images=[],
        routing_decision=SimpleNamespace(task_class=TaskClass.ORDINARY_LIVE), TaskClass=TaskClass,
        prize_programme=None, hashlib=hashlib, ProviderResponseGuard=ProviderResponseGuard)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<actual-gemini-guard-factory>", "exec"), environment)
    return environment["response_guard"], environment


class MultilineTruthGuardTests(unittest.TestCase):
    def plan(self):
        return plan_fixtures.ResponseCartPlanTests().plan(contexts={
            "first": ReplyTruthContext(authorized_prices=(Decimal("790"),)),
            "second": ReplyTruthContext(authorized_prices=(Decimal("1290"),)),
        })

    def validator(self, plan):
        def validate(response, context):
            reason = plan.validate_multiline_claims(response, context)
            return ReplyTruthResult(not bool(reason), (reason,) if reason else ())
        return validate

    def test_actual_guard_accepts_two_correct_scoped_prices_through_callback(self):
        callback = Mock(side_effect=self.validator(self.plan()))
        guard = ProviderResponseGuard(context_factory=lambda *_: ReplyTruthContext(), truth_validator=callback)
        parsed = {"reply_text": "Classic коштує 790 грн, Storm коштує 1290 грн.", "controls": []}
        self.assertTrue(guard.validate(parsed).valid)
        self.assertIs(guard.source, parsed)
        self.assertEqual(guard.response.reply_text, parsed["reply_text"])
        self.assertEqual(callback.call_count, 1)
        self.assertIsInstance(callback.call_args.args[1], ReplyTruthContext)

    def test_callback_rejects_swapped_prices_even_when_flat_context_contains_both(self):
        guard = ProviderResponseGuard(context_factory=lambda *_: ReplyTruthContext(
            authorized_prices=(Decimal("790"), Decimal("1290"))), truth_validator=self.validator(self.plan()))
        result = guard.validate({"reply_text": "Classic коштує 1290 грн, Storm коштує 790 грн.", "controls": []})
        self.assertFalse(result.valid)
        self.assertEqual(result.reason_codes, ("response_plan_line_claim_unverified",))
        self.assertIsNone(guard.response)
        self.assertIsNone(guard.source)

    def test_callback_keeps_business_status_and_url_truth_for_each_clause(self):
        guard = ProviderResponseGuard(context_factory=lambda *_: ReplyTruthContext(), truth_validator=self.validator(self.plan()))
        for text, reason in (("Оплата підтверджена.", "unverified_payment"),
                ("Classic коштує 790 грн. https://foreign.example/checkout", "unauthorized_url")):
            with self.subTest(text=text):
                result = guard.validate({"reply_text": text, "controls": []})
                self.assertFalse(result.valid)
                # Root callback returns a finite scoped failure; the original
                # protected positive assertion still must not be accepted.
                self.assertEqual(result.reason_codes, ("response_plan_line_claim_unverified",))

    def test_callback_is_never_invoked_before_schema_controls_or_actual_media_pass(self):
        callback = Mock(return_value=ReplyTruthResult(True))
        context_factory = Mock(return_value=ReplyTruthContext())
        cases = (
            ({"reply_text": "Дякую.", "controls": [{"manager": True}]}, {}, {}, "invalid_response_schema"),
            ({"reply_text": "Домовились.", "controls": [{"kind": "price", "value": "777"}]}, {}, {}, "unverified_price"),
            ({"reply_text": "Бачу зображення.", "controls": []}, {"image_mimes": ("image/png",)}, {}, "unknown_inline_coverage"),
            ({"reply_text": "Бачу зображення.", "controls": []}, {"image_mimes": ("image/png",), "expected_content_hashes": ("a" * 64,)},
                {"_request_inline_count": 1, "_request_inline_content_hashes": ["b" * 64]}, "actual_media_binding_mismatch"),
            ({"reply_text": "Дякую.", "controls": []}, {"require_intelligence": True}, {}, "missing_turn_intelligence"),
        )
        for parsed, kwargs, usage, reason in cases:
            with self.subTest(reason=reason):
                result = ProviderResponseGuard(context_factory=context_factory, truth_validator=callback, **kwargs).validate(parsed, usage=usage)
                self.assertFalse(result.valid)
                self.assertEqual(result.reason_codes[0], reason)
        callback.assert_not_called()
        context_factory.assert_not_called()

    def test_callback_observes_normalized_receipt_response(self):
        callback = Mock(return_value=ReplyTruthResult(True))
        normalized = "Квитанцію отримано, оплата очікує перевірки."
        guard = ProviderResponseGuard(context_factory=lambda *_: ReplyTruthContext(), truth_validator=callback,
            response_normalizer=lambda response: replace(response, reply_text=normalized))
        self.assertTrue(guard.validate({"reply_text": "Квитанцію отримано.", "controls": []}).valid)
        self.assertEqual(callback.call_args.args[0].reply_text, normalized)
        self.assertEqual(guard.response.reply_text, normalized)

    def test_malformed_or_raising_callback_has_no_cached_winner(self):
        for callback in (lambda *_: True, lambda *_: None, lambda *_: ReplyTruthResult("true"),
                Mock(side_effect=RuntimeError("untrusted private details"))):
            with self.subTest(callback=callback):
                guard = ProviderResponseGuard(context_factory=lambda *_: ReplyTruthContext(), truth_validator=callback)
                result = guard.validate({"reply_text": "Дякую.", "controls": []})
                self.assertEqual(result.reason_codes, ("authority_unavailable",))
                self.assertIsNone(guard.response)

    def test_default_validator_behavior_and_clear_previous_winner_are_preserved(self):
        guard = ProviderResponseGuard(context_factory=lambda *_: ReplyTruthContext(authorized_prices=(Decimal("790"),)))
        self.assertTrue(guard.validate({"reply_text": "Ціна 790 грн.", "controls": []}).valid)
        result = guard.validate({"reply_text": "Ціна 1290 грн.", "controls": []})
        self.assertEqual(result.reason_codes, ("unverified_price",))
        self.assertIsNone(guard.response)

    def test_actual_gemini_factory_bypasses_single_price_extractor_only_for_captured_line_plan(self):
        plan = self.plan()
        callback = Mock(side_effect=self.validator(plan))
        boundary = SimpleNamespace(response_plan=plan, validate_response_truth=callback, truth_context=lambda context: context)
        business = ReplyTruthContext(authorized_prices=(Decimal("790"), Decimal("1290")))
        guard, environment = _actual_factory(boundary, context=business)
        environment["_provider_reply_truth_context"] = Mock(side_effect=AssertionError("single-line inference forbidden"))
        with patch("management.services.ig_reply_authority.build_reply_truth_context", return_value=business) as builder:
            result = guard.validate({"reply_text": "Classic коштує 790 грн, Storm коштує 1290 грн.", "controls": []})
        self.assertTrue(result.valid)
        self.assertEqual(callback.call_count, 1)
        builder.assert_called_once_with(environment["client"], control={})
        environment["_provider_reply_truth_context"].assert_not_called()

    def test_multiline_factory_missing_callback_cannot_fall_back_to_union_authority(self):
        boundary = SimpleNamespace(response_plan=self.plan())
        guard, _ = _actual_factory(boundary, context=ReplyTruthContext())
        result = guard.validate({"reply_text": "Classic коштує 1290 грн.", "controls": []})
        self.assertFalse(result.valid)
        self.assertEqual(result.reason_codes, ("authority_unavailable",))

    def test_single_line_factory_keeps_original_context_path(self):
        guard, environment = _actual_factory(SimpleNamespace(response_plan=SimpleNamespace(line_plans=())), context=ReplyTruthContext())
        original = Mock(return_value=ReplyTruthContext(authorized_prices=(Decimal("790"),)))
        environment["_provider_reply_truth_context"] = original
        self.assertTrue(guard.validate({"reply_text": "Ціна 790 грн.", "controls": []}).valid)
        original.assert_called_once_with(environment["client"], {}, "Ціна 790 грн.")

    def test_actual_rejected_exact_quote_cannot_be_rescued_by_sibling_proposal_amount_or_range(self):
        _, environment = _owned_functions()
        environment["_validated_price_quote"] = Mock(return_value=None)
        control = {"product": 55, "size": "M"}
        original = dict(control)
        proposal_context = ReplyTruthContext(authorized_prices=(Decimal("1290"),),
            authorized_price_ranges=((Decimal("790"), Decimal("1290")),), order_created=True)
        with patch("management.services.ig_reply_authority.build_reply_truth_context", return_value=proposal_context):
            context = environment["_provider_reply_truth_context"](SimpleNamespace(pk=1), control, "Ціна 1290 грн.")
        self.assertEqual(control, original)
        self.assertTrue(context.order_created)
        self.assertEqual(context.authorized_prices, ())
        self.assertEqual(context.authorized_price_ranges, ())
        self.assertEqual(validate_reply_truth("Ціна 1290 грн.", context=context).reasons, ("unverified_price",))
        inferred = environment["_validated_price_quote"].call_args.args[1]
        self.assertEqual(inferred["price_quoted"], "1290")
        self.assertTrue(inferred["_price_claim_invalid"])

    def test_valid_legacy_exact_quote_retains_authority_and_does_not_mutate_controls(self):
        _, environment = _owned_functions()
        environment["_validated_price_quote"] = Mock(return_value={"amount": Decimal("790")})
        control = {"product": 55, "size": "M"}
        original = dict(control)
        valid = ReplyTruthContext(authorized_prices=(Decimal("790"),))
        with patch("management.services.ig_reply_authority.build_reply_truth_context", return_value=valid):
            context = environment["_provider_reply_truth_context"](SimpleNamespace(pk=1), control, "Ціна 790 грн.")
        self.assertEqual(control, original)
        self.assertIs(context, valid)
        self.assertTrue(validate_reply_truth("Ціна 790 грн.", context=context).valid)
