"""Lossless prompt presentation for ordinary grouped recipient conversations."""
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as revision_fixture
from management import tests_ig_source_cart_catalog as catalog_fixture
from management.models import IgCustomerTurn, IgTurnMessage, IgCheckoutProposal, IgCheckoutInventoryReservation
from management.services import ig_revision_live as live
from management.services.ig_client_state_card import render_client_state_prompt
from management.services.ig_fit_consultation import fit_consultation_projection, fit_consultation_guidance
from management.services.ig_product_focus import product_focus_guidance
from management.services.ig_prompt_projections import deduplicate_captured_consultation_note, deduplicate_source_cart_note
from management.services.ig_turn_revisions import create_collecting_revision


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ConsultationProjectionTests(SimpleTestCase):
    def note(self, fit):
        plan = {"obligation_groups": [{"line_id": "boyfriend", "source_message_id": 7,
            "obligation_kinds": ["info:question", "unresolved_line_operation"]}],
            "fit_consultation": fit_consultation_projection(fit)}
        turn = "[SERVER RESPONSE PLAN]\n" + _json(plan) + "\n" + fit_consultation_guidance(fit)
        dynamic = {"fit_consultation": "[CAPTURED FIT CONSULTATION]\n" + _json(fit) + "\n" + fit_consultation_guidance(fit)}
        return plan, turn, dynamic

    def test_reference_preserves_all_obligations_and_exact_captured_fact(self):
        fit = {"source_message_ids": [7], "supported_fits": ["classic"], "unknown": None,
            "focus": {"product_id": 3, "title": "Original design", "source_message_ids": [7], "proof_digest": "a" * 64}}
        plan, turn, notes = self.note(fit)
        before = deepcopy(notes)
        result = deduplicate_captured_consultation_note(turn, dynamic_notes=notes)
        projected = json.loads(result.splitlines()[1])
        self.assertEqual(projected["obligation_groups"], plan["obligation_groups"])
        self.assertEqual(projected["fact_references"], {"fit_consultation": "facts:fit_consultation"})
        self.assertNotIn("fit_consultation", projected)
        self.assertIn(_json(fit), notes["fit_consultation"])
        self.assertIn(fit_consultation_guidance(fit), notes["fit_consultation"])
        self.assertEqual(notes, before)
        self.assertLess(len(result), len(turn))

    def test_absent_changed_source_or_changed_rules_keep_full_original(self):
        fit = {"source_message_ids": [7], "supported_fits": ["classic"]}
        _, turn, notes = self.note(fit)
        self.assertEqual(deduplicate_captured_consultation_note(turn, dynamic_notes={}), turn)
        changed = {**fit, "source_message_ids": [8]}
        self.assertEqual(deduplicate_captured_consultation_note(turn, dynamic_notes=self.note(changed)[2]), turn)
        notes["fit_consultation"] += " Changed scope."
        self.assertEqual(deduplicate_captured_consultation_note(turn, dynamic_notes=notes), turn)

    def test_numeric_boolean_difference_is_not_identical_evidence(self):
        _, turn, _ = self.note({"scope_flag": 1})
        notes = self.note({"scope_flag": True})[2]
        self.assertEqual(deduplicate_captured_consultation_note(turn, dynamic_notes=notes), turn)


class GatewayReached(BaseException):
    pass


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_TURN_CONTEXT_MODE="unified",
    IG_CLIENT_STATE_PROMPT_TOKENS=2400, IG_BOT_POLICY_BUDGET_CHARS=48000,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeGroupedPromptTests(TransactionTestCase):
    reset_sequences = True
    _message = revision_fixture.RevisionLiveTests._message
    _prepare = revision_fixture.RevisionLiveTests._prepare

    def setUp(self):
        revision_fixture.RevisionLiveTests.setUp(self)
        from management.services.ig_core_policy import CANONICAL_IG_CORE_POLICY
        self.settings.system_prompt = CANONICAL_IG_CORE_POLICY
        self.settings.save(update_fields=["system_prompt"])
        catalog_fixture.SourceCartCatalogTests.setUp(self)
        self.category.name, self.category.slug = "Футболки", "tshirts"
        self.category.save()
        self.products[0].title = "QA Trade Футболка"
        self.products[0].save(update_fields=["title"])
        self.revision.delete()
        self.turn.delete()
        self.source.delete()
        self.compaction_metrics = {}

    def grouped_revision(self, texts):
        rows = [self._message(text, f"grouped-budget-{index}") for index, text in enumerate(texts)]
        for index, row in enumerate(rows):
            row.provider_created_at = timezone.now() - timedelta(seconds=len(rows) - index)
            row.save(update_fields=["provider_created_at"])
        self.source = rows[0]
        self.turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        for index, row in enumerate(rows):
            IgTurnMessage.objects.create(turn=self.turn, message=row, ordinal=index + 1, role="user")
        self.revision = create_collecting_revision(self.turn, rows, bypass_quiet=True).revision
        self._prepare()
        return rows

    def reach_native_gateway(self):
        from management.services import ig_turn_integration as integration, ig_prompt_projections as projections
        from management.services import ig_policy_compiler as compiler
        original_prepare = integration.prepare_revision_turn_context
        original_dedup = projections.deduplicate_captured_consultation_note
        original_compile = compiler.compile_policy
        captured = {}
        def prepare(*args, **kwargs):
            result = original_prepare(*args, **kwargs)
            captured["prepared"] = result
            return result
        def dedup(note, **kwargs):
            result = original_dedup(note, **kwargs)
            captured.update(original_turn=note, compact_turn=result)
            return result
        def compile_policy(**kwargs):
            result = original_compile(**kwargs)
            modules = [*kwargs["immutable_authority"], *kwargs["published_core"], *kwargs["verified_dynamic_facts"]]
            captured["mandatory_total"] = len("\n\n".join(row.body for row in modules))
            captured["compiled_turn"] = next(row.body for row in modules if row.id == "context:turn")
            return result
        def gateway(payload, **kwargs):
            captured["payload"] = deepcopy(payload)
            captured["manifest"] = deepcopy(kwargs["request_policy_manifest"])
            raise GatewayReached()
        with (patch.object(integration, "prepare_revision_turn_context", side_effect=prepare),
            patch.object(projections, "deduplicate_captured_consultation_note", side_effect=dedup),
            patch.object(compiler, "compile_policy", side_effect=compile_policy),
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=gateway) as provider,
            patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("No customer transport")) as transport):
            with self.assertRaises(GatewayReached):
                live.execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(provider.call_count, 1)
        transport.assert_not_called()
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutInventoryReservation.objects.count(),
            self.revision.delivery_effects.count()), (0, 0, 0))
        self.assertEqual(captured["manifest"]["budget_chars"], 48000)
        self.assertLessEqual(captured["mandatory_total"], 48000)
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts["commerce_reduction"]
        self.assertEqual(sorted(row["source_message_id"] for row in receipt["decisions"]),
            sorted(row.message_id for row in self.revision.sources.all()))
        state = captured["prepared"].state
        before = state.as_dict()
        render = render_client_state_prompt(state, budget=2400,
            excluded_slots=("communication.preferences", "context.useful_memory"))
        self.assertFalse(render.oversized, render.estimated_tokens)
        self.assertLessEqual(render.estimated_tokens, 2400)
        self.assertEqual(state.as_dict(), before)
        captured["render"] = render
        self.compaction_metrics.update(state_tokens=render.estimated_tokens, state_utf8=len(render.text.encode()),
            mandatory_chars=captured["mandatory_total"], turn_before_chars=len(captured["original_turn"]),
            turn_after_chars=len(captured["compact_turn"]))
        return captured

    def test_grouped_distinct_pair_and_ambiguous_size_fits_without_losing_review(self):
        self.grouped_revision([
            "Собі хочу одну чорну футболку QA Trade Футболка, розмір M, крій classic.",
            "Хлопцю дві такі самі чорні футболки QA Trade Футболка, розмір L, крій classic.",
            "Розмір краще M.",
        ])
        captured = self.reach_native_gateway()
        state = captured["prepared"].state
        with patch("management.services.ig_fulfillment_context._compact_review_tables", side_effect=lambda value: value):
            original = render_client_state_prompt(state, budget=2400,
                excluded_slots=("communication.preferences", "context.useful_memory"))
        self.assertTrue(original.oversized)
        self.assertGreater(original.estimated_tokens, 2400)
        self.compaction_metrics["original_state_tokens"] = original.estimated_tokens
        block = next(json.loads(line) for line in captured["render"].text.splitlines()
            if line.startswith("{") and json.loads(line).get("slot") == "fulfillment.review")
        self.assertEqual((block["status"], block["authority"]), ("pending", "pending_manager_review"))
        request = state.as_dict()["slots"]["fulfillment.review"]["value"]["current_request"]
        compact = deepcopy(block["value"]["current_request"])
        table = compact["customer_requirements"]
        requirements = [{**table["shared"], **row} for row in table["rows"]]
        self.assertEqual(_json(requirements), _json(request["customer_requirements"]))
        for actual, original in zip(compact["lines"], request["lines"], strict=True):
            self.assertEqual((actual["line_id"], actual["recipient_id"], actual["target"]),
                (original["line_id"], original["recipient_id"], original["target"]))
            preferences = actual["preferences"]
            evidence = {field: group["proof"] for group in preferences["evidence_groups"] for field in group["fields"]}
            for key, value in (("values", preferences["values"]), ("evidence", evidence), ("cleared", preferences["cleared"])):
                self.assertEqual(_json(value), _json(original["preferences"][key]))
        self.assertIn("unresolved_line_operation", captured["payload"]["system_instruction"]["parts"][0]["text"])

    def test_mixed_known_line_unknown_color_fits_without_inventing_availability(self):
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        self.variants[0].stock = 10
        self.variants[0].save(update_fields=["stock"])
        ProductInventoryPolicy.objects.create(product=self.products[0], source="catalog_variant")
        for size in ("M", "L"):
            VariantSizeRule.objects.create(variant=self.variants[0], fit_code="classic", size=size, stock=5, is_enabled=True)
        self.grouped_revision([
            "Собі одну чорну футболку QA Trade Футболка, M, classic.",
            "Хлопцю одну таку саму футболку, але синю, L, classic. Чи є синя? Якщо синю треба уточнити, мою чорну лишаємо.",
        ])
        captured = self.reach_native_gateway()
        prepared = captured["prepared"]
        original_turn = deduplicate_source_cart_note(captured["original_turn"], state=prepared.state,
            state_text=captured["render"].text)
        old_total = captured["mandatory_total"] - len(captured["compiled_turn"]) + len(original_turn)
        self.assertGreater(old_total, 48000)
        self.compaction_metrics["original_mandatory_chars"] = old_total
        cart = prepared.state.as_dict()["source_cart"]
        self.assertEqual([(row["recipient_id"], row["fields"]["color"]["value"]) for row in cart["lines"]],
            [("self", "black"), ("boyfriend", "blue")])
        system = captured["payload"]["system_instruction"]["parts"][0]["text"]
        self.assertIn('"fact_references"', system)
        self.assertIn('"check_required"', system)
        self.assertIn('"available"', system)
        self.assertIn('"info:question"', system)
