"""Accounting is observational; physical supply remains scoped manager authority."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import uuid
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, SimpleTestCase, override_settings
from management.models import IgBotNotification, AdminAuditLog
from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest as Request, IgFulfillmentReviewDecision as Decision
from management.services import ig_fulfillment_reviews as reviews
from management.services.ig_fulfillment_delivery import render_manager_update
from management.tests_ig_restock_subscriptions import _RestockFixture


class StockPromptBoundaryTests(SimpleTestCase):
    def test_checkout_projection_retains_choices_price_without_internal_inventory(self):
        from management.services.ig_client_state_card import checkout_prompt_value
        for stock in (0, 100):
            with self.subTest(stock=stock):
                value = {"product": {"id": 7, "price": "900.00", "price_exact": True},
                    "color": {"selected": "black", "options": [{"name": "Black", "stock": stock, "variant_id": 3}]},
                    "size": {"selected": "M", "available": ["L"] if stock == 0 else ["M", "L"],
                        "disabled": ["M"] if stock == 0 else [], "requested_unavailable": "M" if stock == 0 else ""}}
                before = deepcopy(value)
                rendered = checkout_prompt_value(value)
                self.assertEqual(value, before)
                self.assertEqual(rendered["product"], before["product"])
                self.assertEqual(rendered["color"]["selected"], "black")
                self.assertEqual(rendered["color"]["options"], [{"name": "Black", "variant_id": 3}])
                self.assertEqual(rendered["size"], {"selected": "M",
                    "checkout_eligible_values": before["size"]["available"],
                    "checkout_disabled_values": before["size"]["disabled"],
                    "checkout_ineligible_request": before["size"]["requested_unavailable"]})

    def test_ledger_input_is_private_and_exact_manager_decision_is_unchanged(self):
        from management.services.ig_fulfillment_context import fulfillment_prompt_value
        for stock in (0, 100):
            with self.subTest(stock=stock):
                observation = {"status": "available" if stock else "unavailable",
                    "reason": "physical_stock_available" if stock else "insufficient_free_stock",
                    "physical_quantity": stock, "free_quantity": stock, "size_cap": stock}
                lines = [{"line_id": "line:1:0", "preferences": {"size": "M", "color": "мокко"},
                    "observation": observation}]
                decision = {"id": 9, "lines": [{"line_id": "line:1:0", "availability": "available",
                    "checked_quantity": 1, "supply_basis": "accessible_unrecorded"}]}
                value = {"status": "reviewed", "current_request": {"lines": lines},
                    "lines": deepcopy(lines), "decision": decision}
                before = deepcopy(value)
                rendered = fulfillment_prompt_value(value)
                self.assertEqual(value, before)
                self.assertEqual(rendered["decision"], decision)
                line = rendered["current_request"]["lines"][0]
                self.assertEqual(line["preferences"], {"size": "M", "color": "мокко"})
                self.assertNotIn("observation", line)
                self.assertNotIn("accounting_observation", line)

    def test_unreviewed_prompt_has_no_manager_confirmation_or_internal_inventory(self):
        import json
        from management.services.ig_client_state_card import CapturedClientState, _slot, render_client_state_prompt
        for stock in (0, 100):
            with self.subTest(stock=stock):
                value = {"status": "pending", "decision": None,
                    "current_request": {"lines": [{"line_id": "selected",
                        "target": {"product_id": 7, "size": "M", "quantity": 1},
                        "preferences": {"values": {"size": "M", "color": "мокко"}},
                        "observation": {"status": "available" if stock else "unavailable",
                            "reason": "physical_stock_available" if stock else "insufficient_free_stock",
                            "physical_quantity": stock, "free_quantity": stock, "size_cap": stock}}],
                        "customer_requirements": [{"source_id": 3, "raw_quote": "Чи є M?"}]}}
                raw = {"status": "captured", "digest": "a" * 64, "slots": {
                    "fulfillment.review": _slot(value, status="confirmed", authority="verified_manager_review",
                        mandatory=True, validity="valid")}}
                state = CapturedClientState(json.dumps(raw, ensure_ascii=False).encode())
                before, digest = state.as_dict(), state.digest
                rendered = render_client_state_prompt(state, budget=2400)
                self.assertFalse(rendered.oversized)
                row = next(json.loads(line) for line in rendered.text.splitlines() if line.startswith('{'))
                self.assertEqual((row["status"], row["authority"]), ("pending", "pending_manager_review"))
                self.assertIsNone(row["value"]["decision"])
                self.assertNotIn("confirmation_semantics", row)
                line = row["value"]["current_request"]["lines"][0]
                self.assertEqual(line["target"], value["current_request"]["lines"][0]["target"])
                self.assertEqual(line["preferences"]["values"], {"size": "M", "color": "мокко"})
                self.assertNotIn("observation", line)
                self.assertNotIn("accounting_observation", line)
                self.assertNotIn("physical_quantity", json.dumps(row))
                self.assertEqual(state.as_dict(), before)
                self.assertEqual(state.digest, digest)
                self.assertEqual(rendered.capture_digest, digest)

    def test_unavailable_or_stale_review_slots_never_render_manager_confirmation(self):
        import json
        from management.services.ig_client_state_card import CapturedClientState, _slot, render_client_state_prompt
        for status, reason in (("unknown", "fulfillment_review_absent"),
                ("stale", "fulfillment_purchase_changed"), ("stale", "fulfillment_review_expired"),
                ("unknown", "fulfillment_scope_unavailable")):
            with self.subTest(reason=reason):
                raw = {"status": "captured", "digest": "a" * 64, "slots": {"fulfillment.review":
                    _slot({"status": "reviewed", "decision": {"lines": [{"availability": "available", "checked_quantity": 1}]}},
                        status=status, reason=reason, authority="verified_manager_review", validity="unknown")}}
                state = CapturedClientState(json.dumps(raw).encode())
                before = state.as_dict()
                rendered = render_client_state_prompt(state, budget=2400)
                self.assertNotIn("fulfillment.review", rendered.included)
                self.assertNotIn('"authority":"verified_manager_review"', rendered.text)
                self.assertEqual(state.as_dict(), before)

    def test_verified_review_keeps_checked_quantity_and_timing_authority(self):
        import json
        from management.services.ig_client_state_card import CapturedClientState, _slot, render_client_state_prompt
        for availability in ("available", "wait"):
            with self.subTest(availability=availability):
                line = {"line_id": "selected", "availability": availability, "checked_quantity": 1,
                    "preparation": {"min_days": 1, "max_days": 3, "unit": "calendar"}}
                if availability == "wait":
                    line["base_arrival"] = {"min_days": 7, "max_days": 7, "unit": "calendar",
                        "anchor_at": "2026-10-23T10:00:00+03:00"}
                value = {"status": "reviewed", "current_request": {"lines": []},
                    "decision": {"id": 9, "lines": [line]}}
                raw = {"status": "captured", "digest": "a" * 64, "slots": {"fulfillment.review":
                    _slot(value, status="confirmed", authority="verified_manager_review", mandatory=True, validity="valid")}}
                state = CapturedClientState(json.dumps(raw).encode())
                before = state.as_dict()
                rendered = render_client_state_prompt(state, budget=2400)
                row = next(json.loads(text) for text in rendered.text.splitlines() if text.startswith('{'))
                self.assertEqual((row["status"], row["authority"]), ("confirmed", "verified_manager_review"))
                self.assertEqual(row["value"]["decision"], value["decision"])
                self.assertEqual(state.as_dict(), before)

    def test_catalogue_price_and_configuration_keep_zero_and_positive_accounting_local(self):
        from decimal import Decimal
        from management.services.ig_catalog_pricing import format_variant_pricing
        for stock in (0, 100):
            with self.subTest(stock=stock):
                rows = [{"variant_id": 3, "color": "Black", "price": Decimal("900.00"),
                    "price_text": "900", "price_reason": "", "fit_code": "classic",
                    "compatible_sizes": ("M", "L"), "stock": stock}]
                before = deepcopy(rows)
                text = format_variant_pricing(rows)
                self.assertEqual(rows, before)
                for known in ("Black", "variant_id=3", "900 грн", "classic=M/L", f"обліковий stock={stock}"):
                    self.assertIn(known, text)
                self.assertNotIn("на складі", text)
                customer_text = format_variant_pricing(rows, include_inventory=False)
                for known in ("Black", "variant_id=3", "900 грн", "classic=M/L"):
                    self.assertIn(known, customer_text)
                self.assertNotIn("stock", customer_text)
                self.assertEqual(rows, before)

    def test_shared_catalogue_encoding_keeps_accounting_counts_local_and_lossless(self):
        from decimal import Decimal
        from management.services.ig_catalog_pricing import format_variant_pricing
        from management.services.ig_catalog_prompt_compaction import compact_catalog_text, expand_compact_catalog_text
        lines = ["Каталог TwoComms (усі актуальні товари; скорочений формат):"]
        for stock in range(40):
            variant_id = stock + 1
            row = {"variant_id": variant_id, "color": "Black", "price": Decimal("900.00"),
                "price_text": "900", "price_reason": "", "fit_code": "classic",
                "compatible_sizes": ("M", "L"), "stock": stock}
            lines.append(f"• id={variant_id} | Fictional {variant_id} — 900 грн; кольори: " + format_variant_pricing([row]))
        original = "\n".join(lines)
        encoded = compact_catalog_text(original)
        self.assertLess(len(encoded), len(original))
        self.assertEqual(expand_compact_catalog_text(encoded), original)
        for stock in range(40):
            self.assertIn(f"variant_id={stock + 1}, @C", encoded)
            self.assertIn(f", обліковий stock={stock})", encoded)


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    IG_TURN_CONTEXT_MODE="unified", IG_CLIENT_STATE_PROMPT_TOKENS=2400,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeStockPromptInputTests(_RestockFixture, TestCase):
    def setUp(self):
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests
        original = SourceCartCatalogTests.setUp
        def catalog_before_sources(case):
            original(case)
            case.category.name, case.category.slug = "Худі", "hoodies"
            case.category.save(update_fields=["name", "slug"])
            case.products[0].price = case.variants[0].price_override = 900
            case.products[0].save(update_fields=["price"])
            case.variants[0].save(update_fields=["price_override"])
        with patch.object(SourceCartCatalogTests, "setUp", catalog_before_sources):
            super().setUp()
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        self.canonical("Хочу худі Restock Aurora, кількість 1, розмір M, колір black, крій classic")

    def capture_model_input(self, question, stock):
        import json
        from management.services import instagram_bot as bot
        from management.services.call_ai_analysis import CallAIAnalysisError
        from management.services.ig_revision_live import RevisionGenerationBoundary
        from management.services.ig_revision_media import collect_revision_media
        from management.services.ig_turn_integration import prepare_revision_turn_context
        from management.services.ig_product_inquiries import inquiry_prompt
        self.variant.stock = self.rule.stock = stock
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        source = self.source(question)
        revision, token = self.revision(source)
        reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        collection = collect_revision_media(revision.pk, token)
        boundary = RevisionGenerationBoundary(revision, token, self.settings, self.binding, has_images=False)
        prepared = prepare_revision_turn_context(revision, generation_boundary=boundary,
            collection=collection, settings_row=self.settings, publication=self.binding, now=self.now)
        before = prepared.state.as_dict()
        captured = []
        def transport(payload, **kwargs):
            captured.append(deepcopy(payload))
            raise CallAIAnalysisError("offline prompt capture")
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=transport) as model, \
                patch.object(bot, "_provider_http", side_effect=AssertionError("prompt capture cannot deliver")) as http:
            bot.gemini_generate(self.settings, list(prepared.history), client=self.client_row,
                routing_decision=prepared.context.decision, generation_boundary=boundary,
                turn_note=prepared.context.turn_note + "\n" + inquiry_prompt(boundary.product_inquiries),
                memory_note=prepared.context.memory_note,
                context_note=prepared.context.context_note, captured_client_state=prepared.state,
                captured_dynamic_notes=prepared.dynamic_notes, captured_turn_text=prepared.current_text,
                captured_policy_tags=prepared.policy_inputs.tags,
                captured_knowledge_language=prepared.policy_inputs.knowledge_language,
                captured_knowledge_selectors=prepared.policy_inputs.knowledge_selectors,
                request_context_metadata=prepared.request_metadata, turn_media_binding=collection.binding,
                turn_media_context=[], deadline_at=revision.overall_deadline - timedelta(seconds=5))
        self.assertEqual(model.call_count, 1)
        http.assert_not_called()
        self.assertEqual(prepared.state.as_dict(), before)
        payload = captured[0]
        system = payload["system_instruction"]["parts"][0]["text"]
        self.assertIn(question, json.dumps(payload, ensure_ascii=False))
        product_line = next(line for line in system.splitlines() if line.startswith(f"• id={self.product.pk} |"))
        for known in ("Restock Aurora", "900 грн", "Black", "classic=M/L"):
            self.assertIn(known, product_line)
        self.assertNotRegex(product_line, r"\bstock\s*=|\bcheckout_status\s*=|на складі")
        rows = [json.loads(line) for line in system.splitlines() if line.startswith('{')]
        cart = next(row["value"] for row in rows if row.get("slot") == "current.source_cart")
        columns, selected = cart["field_columns"], cart["lines"][0]["values"]
        for key, value in (("product_id", self.product.pk), ("size", "M"), ("color", "black"), ("fit_option_code", "classic")):
            self.assertEqual(selected[columns.index(key)], value)
        for row in rows:
            if row.get("slot") == "fulfillment.review":
                self.assertEqual((row["status"], row["authority"]), ("pending", "pending_manager_review"))
                for inventory in ("accounting_observation", "physical_stock_available", "physical_quantity", "free_quantity", "size_cap"):
                    self.assertNotIn(inventory, json.dumps(row))
            if row.get("has_product"):
                self.assertEqual(row["size"]["selected"], "M" if stock else "")
                for color in row.get("color", {}).get("options", []):
                    self.assertNotIn("stock", color)
                    self.assertNotIn("accounting_stock", color)
        return system, before

    def test_zero_ledger_model_input_keeps_m_without_accounting_claims(self):
        system, canonical = self.capture_model_input("Чи є це худі в розмірі M у наявності?", 0)
        self.assertIn("observation", str(canonical["slots"]["fulfillment.review"]["value"]))

    def test_positive_ledger_known_price_model_input_has_no_physical_confirmation(self):
        _, canonical = self.capture_model_input("Чи є це худі в розмірі M у наявності та яка його ціна?", 100)
        self.assertEqual(canonical["slots"]["fulfillment.review"]["value"]["status"], "pending")

    def test_unlisted_color_model_input_keeps_exact_wish_and_configured_choices(self):
        system, _ = self.capture_model_input("Хочу це худі кольору мокко.", 100)
        self.assertIn("мокко", system)
        self.assertIn("requested_color_unverified", system)


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeStockAuthorityTests(_RestockFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(username="stock-authority-manager", password="test")

    def case(self, text="Ця позиція є в наявності?", *, source=None, at=None):
        source = source or self.source(text, at=at)
        revision, _ = self.revision(source)
        with patch("django.utils.timezone.now", return_value=at or source.provider_created_at), patch("management.services.instagram_bot._provider_http") as http:
            result = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=at or source.provider_created_at)
        http.assert_not_called()
        self.assertTrue(result["ready"], result)
        return Request.objects.get(pk=result["request_id"]), revision, result

    def decide(self, request, *, at=None, **changes):
        rows = [{"line_id": line["line_id"], "availability": "available", "checked_quantity": 1,
            "supply_basis": "accessible_unrecorded", **changes} for line in request.envelope["lines"]]
        with patch("django.utils.timezone.now", return_value=at or self.now):
            result = reviews.apply_fulfillment_decision(request.pk, actor=self.manager,
                expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(), decisions_by_line=rows, now=at or self.now)
        self.assertTrue(result["ready"], result)
        return Decision.objects.get(pk=result["decision_id"])

    def test_positive_count_requires_exact_manager_check_with_source_excerpt(self):
        self.rule.stock = self.variant.stock = 8
        self.rule.save(update_fields=["stock"])
        self.variant.save(update_fields=["stock"])
        request, _, _ = self.case()
        projection = reviews.fulfillment_review_projection(self.client_row.pk, now=self.now)
        self.assertEqual(request.envelope["lines"][0]["observation"]["status"], "available")
        self.assertEqual(projection["status"], "pending")
        self.assertTrue(projection["can_decide"])
        self.assertEqual(projection["decision_mode"], "initial")
        self.assertFalse(projection["decision_recorded"])
        self.assertIsNone(projection["decision"])
        self.assertEqual(projection["source_excerpts"][0]["text"], request.source.text)
        self.assertEqual(projection["source_excerpts"][0]["message_id"], request.source_id)
        self.assertFalse(Decision.objects.exists())

    def test_exact_selection_requires_check_even_with_positive_count(self):
        self.rule.stock = self.variant.stock = 8
        self.rule.save(update_fields=["stock"])
        self.variant.save(update_fields=["stock"])
        request, _, _ = self.case(source=self.choice)
        self.assertEqual(request.source_id, self.choice.pk)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 1)

    def test_same_question_and_raw_count_edit_reuse_one_case(self):
        original, revision, _ = self.case(source=self.choice)
        replay = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertEqual(replay["request_id"], str(original.pk))
        self.rule.stock = self.variant.stock = 12
        self.rule.save(update_fields=["stock"])
        self.variant.save(update_fields=["stock"])
        for offset in (1, 2):
            question, _, result = self.case(at=self.now + timedelta(seconds=offset))
            self.assertEqual(question.pk, original.pk)
            self.assertFalse(result["created"])
        unrelated = self.source("Яка ціна?", at=self.now + timedelta(seconds=3))
        revision, _ = self.revision(unrelated)
        independent = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=unrelated.provider_created_at)
        self.assertFalse(independent["pure"])
        self.assertEqual(independent["request_id"], str(original.pk))
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 1)
        self.assertEqual(Decision.objects.count(), 0)

    def test_fresh_notification_optin_keeps_exact_source_binding_and_replay(self):
        from management.services import ig_restock_subscriptions as subscriptions
        first, _, _ = self.case()
        result, source, revision, _ = self.request("Повідомте, коли ця позиція буде в наявності")
        self.assertIsNotNone(result.invitation_id, result.reason)
        current = Request.objects.order_by("-created_at", "-source_id").first()
        self.assertNotEqual(current.pk, first.pk)
        self.assertEqual(current.source_id, source.pk)
        self.assertEqual(current.revision_id, revision.pk)
        again = subscriptions.prepare_request(revision, source, settings_obj=self.settings, now=source.provider_created_at)
        self.assertEqual(again.invitation_id, result.invitation_id)
        self.assertEqual(Request.objects.count(), 2)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 2)

    def test_unknown_colour_question_creates_no_wrong_selected_black_stock_case(self):
        from management.services.ig_product_inquiries import product_inquiries_for_revision
        source = self.source("Чи є худі кольору мокко в наявності?")
        revision, _ = self.revision(source)
        result = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "desired_configuration_requires_review")
        self.assertFalse(Request.objects.exists())
        inquiries = product_inquiries_for_revision(revision)
        self.assertTrue(any(row["axis"] == "color" and row["requested_value"] == "мокко"
            for row in inquiries["inquiries"]))

    def test_extra_colour_question_keeps_black_proof_but_explicit_change_supersedes_it(self):
        request, _, _ = self.case()
        self.decide(request)
        question = self.source("Чи буває колір мокко?", at=self.now+timedelta(seconds=1))
        revision, _ = self.revision(question)
        result = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=question.provider_created_at)
        self.assertFalse(result["created"])
        self.assertEqual(Request.objects.count(), 1)
        still_black = reviews.fulfillment_review_projection(self.client_row.pk, now=question.provider_created_at)
        self.assertEqual(still_black["status"], "reviewed", still_black)
        self.assertIsNotNone(still_black["decision"])
        change = self.source("Хочу колір мокко замість чорного", at=self.now+timedelta(seconds=2))
        self.revision(change)
        superseded = reviews.fulfillment_review_projection(self.client_row.pk, now=change.provider_created_at)
        self.assertEqual(superseded["status"], "stale", superseded)
        self.assertFalse(superseded["can_decide"])
        self.assertEqual(superseded["reason"], "requirements_superseded")

    def test_quantity_change_creates_new_scope_preserving_decision(self):
        first, _, _ = self.case()
        decision = self.decide(first)
        source = self.canonical("количество 2", at=self.now+timedelta(seconds=1))
        second, _, _ = self.case(source=source)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(second.envelope["lines"][0]["target"]["quantity"], 2)
        old = reviews.fulfillment_review_projection(self.client_row.pk, now=source.provider_created_at, request_id=first.pk)
        self.assertFalse(old["can_decide"])
        self.assertTrue(old["decision_recorded"])
        self.assertEqual(Decision.objects.get(pk=decision.pk).envelope["lines"][0]["checked_quantity"], 1)

    def test_new_stock_inquiry_after_expiry_creates_one_fresh_scoped_request(self):
        first, _, _ = self.case()
        self.decide(first)
        later = self.now + timedelta(hours=25)
        newer, revision, result = self.case(at=later)
        self.assertTrue(result["created"])
        self.assertNotEqual(newer.pk, first.pk)
        self.assertTrue(reviews.fulfillment_review_projection(self.client_row.pk, now=later)["can_decide"])
        replay = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=later+timedelta(seconds=1))
        self.assertEqual(replay["request_id"], str(newer.pk))
        self.assertFalse(replay["created"])
        self.assertEqual(Request.objects.count(), 2)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 2)

    def test_expired_stored_decision_is_pending_but_not_decidable(self):
        request, _, _ = self.case()
        self.decide(request)
        projection = reviews.fulfillment_review_projection(self.client_row.pk, now=self.now+timedelta(hours=25))
        self.assertEqual(projection["status"], "pending")
        self.assertFalse(projection["can_decide"])
        self.assertTrue(projection["decision_recorded"])
        self.assertEqual(projection["decision_mode"], "none")
        self.assertEqual(projection["decision_blocked_reason"], "manager_review_expired")

    def test_future_stored_decision_never_looks_like_initial_form(self):
        request, _, _ = self.case()
        self.decide(request, at=self.now+timedelta(minutes=1))
        projection = reviews.fulfillment_review_projection(self.client_row.pk, now=self.now)
        self.assertFalse(projection["can_decide"])
        self.assertTrue(projection["decision_recorded"])
        self.assertEqual(projection["decision_mode"], "none")

    def test_unrecorded_supply_confirms_physical_count_without_ledger_write(self):
        request, _, _ = self.case()
        decision = self.decide(request)
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.stock, 0)
        self.assertEqual(decision.envelope["lines"][0]["supply_basis"], "accessible_unrecorded")
        self.assertTrue(decision.envelope["lines"][0]["accounting_recount_required"])
        projection = reviews.fulfillment_review_projection(self.client_row.pk, now=self.now)
        self.assertFalse(projection["can_decide"])
        self.assertEqual(projection["decision_blocked_reason"], "decision_already_recorded")
        self.assertEqual(AdminAuditLog.objects.filter(action="ig_fulfillment_decision").count(), 1)

    def test_base_arrival_is_distinct_and_wait_proposed(self):
        request, _, _ = self.case()
        decision = self.decide(request, availability="wait", checked_quantity=None, supply_basis="incoming_confirmed",
            base_arrival={"min_days": 7, "max_days": 7, "unit": "calendar"})
        projection = reviews.fulfillment_review_projection(self.client_row.pk, now=self.now)
        row = projection["decision"]["lines"][0]
        self.assertEqual(row["base_arrival"], {"min_days": 7, "max_days": 7, "unit": "calendar", "anchor_at": self.now.isoformat()})
        self.assertIsNone(row["dispatch"])
        self.assertIsNone(row["preparation"])
        self.assertEqual(projection["customer_waiting"]["state"], "proposed")
        self.assertFalse(projection["customer_waiting"]["accepted"])
        self.assertTrue(decision.envelope["waiting_acceptance_required"])
        text = render_manager_update(projection, copy_version=2)
        self.assertIn("прибуття основи до нас 7", text)
        self.assertIn("Чи згодні ви зачекати", text)
        self.assertNotIn("відправка 7", text)


class BaseArrivalSchemaTests(SimpleTestCase):
    def test_requires_incoming_wait_exact_schema_and_manager_clock(self):
        from django.utils import timezone
        now = timezone.now()
        timing = {"min_days": 7, "max_days": 7, "unit": "calendar"}
        for availability, basis in (("available", "incoming_confirmed"), ("wait", "warehouse_checked"), ("unavailable", "incoming_confirmed")):
            with self.subTest(availability=availability), self.assertRaises(ValueError):
                reviews._base_arrival(timing, availability=availability, basis=basis, now=now)
        self.assertEqual(reviews._base_arrival(timing, availability="wait", basis="incoming_confirmed", now=now), {**timing, "anchor_at": now.isoformat()})
        for invalid in ({**timing, "anchor": "after_payment_confirmation"}, {**timing, "min_days": True}, {**timing, "max_days": 6}, {**timing, "unit": "hours"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                reviews._base_arrival(invalid, availability="wait", basis="incoming_confirmed", now=now)

    def test_all_list_uses_same_strict_schema(self):
        from django.utils import timezone
        from management.services import ig_restock_waiting_list_review as waiting
        now = timezone.now()
        target = {"line_id": "line:1", "item_kind": "catalog_apparel", "quantity": 1}
        request = SimpleNamespace(envelope={"lines": [{"line_id": "line:1", "target": target}]})
        row = {"line_id": "line:1", "availability": "wait", "supply_basis": "incoming_confirmed", "base_arrival": {"min_days": 7, "max_days": 7, "unit": "calendar"}}
        with patch.object(waiting, "_request_baseline", return_value={"line:1": "c"*64}), patch.object(waiting.waiting, "observe_item", return_value={"status": "unavailable", "configuration_digest": "c"*64}):
            clean, _ = waiting.normalize_decisions([row], request, now)
            self.assertEqual(clean[0]["base_arrival"]["min_days"], 7)
            with self.assertRaises(ValueError):
                waiting.normalize_decisions([{**row, "availability": "available", "checked_quantity": 1, "supply_basis": "warehouse_checked"}], request, now)


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeWaitingAcceptanceTests(_RestockFixture, TransactionTestCase):
    case = NativeStockAuthorityTests.case
    decide = NativeStockAuthorityTests.decide

    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(username="wait-manager", password="test")

    def proposal(self, *, send=True):
        import json
        from management.services.ig_fulfillment_delivery import process_due_fulfillment_updates
        request, _, _ = self.case()
        decision = self.decide(request, availability="wait", checked_quantity=None,
            supply_basis="incoming_confirmed", base_arrival={"min_days": 7, "max_days": 7, "unit": "calendar"})
        if send:
            at = self.now+timedelta(seconds=1)
            with patch("django.utils.timezone.now", return_value=at), patch(
                    "management.services.instagram_bot.get_page_token", return_value="synthetic-test-token"), patch(
                    "management.services.instagram_bot._provider_http", return_value=(200, json.dumps({"message_id": "native-wait-proposal"}))) as http:
                self.assertEqual(process_due_fulfillment_updates(self.settings, now=at), 1)
                self.assertEqual(process_due_fulfillment_updates(self.settings, now=at), 0)
            http.assert_called_once()
            from management.models import IgFollowUpTask
            task = IgFollowUpTask.objects.get(reason=reviews.UPDATE_REASON)
            self.assertEqual(task.status, "sent")
            self.assertEqual(task.provider_message_id, "native-wait-proposal")
        return request, decision

    def reply(self, text, *, second=2, target="native-wait-proposal"):
        source = self.source(text, at=self.now+timedelta(seconds=second))
        source.reply_to_provider_message_id = target
        source.save(update_fields=["reply_to_provider_message_id"])
        self.revision(source)
        return source

    def state(self, second=10):
        result = reviews.fulfillment_review_projection(self.client_row.pk, now=self.now+timedelta(seconds=second))
        self.assertEqual(result["status"], "reviewed", result)
        return result["customer_waiting"]

    @override_settings(ROOT_URLCONF="management.urls")
    def test_existing_manager_api_accepts_separate_base_arrival_and_dispatch_fields(self):
        import json
        from django.urls import reverse
        request, _, _ = self.case()
        self.client.force_login(self.manager)
        url = reverse("management_bot_fulfillment_review_api", args=[self.client_row.pk])
        before = self.client.get(url)
        self.assertTrue(before.json()["review"]["can_decide"])
        response = self.client.post(url, json.dumps({"request_id": str(request.pk),
            "expected_request_digest": request.envelope_hmac, "operation_id": str(uuid.uuid4()),
            "decisions_by_line": [{"line_id": request.envelope["lines"][0]["line_id"],
                "availability": "wait", "supply_basis": "incoming_confirmed",
                "base_arrival": {"min_days": 7, "max_days": 7, "unit": "calendar"},
                "dispatch": {"min_days": 10, "max_days": 12, "unit": "calendar"}}]}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        readback = self.client.get(url).json()["review"]
        self.assertEqual(readback["decision"]["lines"][0]["base_arrival"]["min_days"], 7)
        self.assertEqual(readback["decision"]["lines"][0]["dispatch"]["min_days"], 10)
        self.assertFalse(readback["can_decide"])
        self.assertFalse(readback["customer_waiting"]["accepted"])

    def test_pending_stock_case_does_not_block_independent_known_price_sent_once(self):
        from management.services.ig_revision_outbox import plan_revision_effects
        from management.services.ig_revision_delivery import drain_group, ProviderPartResult
        from management.models import IgRevisionDeliveryEffect
        self.rule.stock = self.variant.stock = 8
        self.rule.save(update_fields=["stock"])
        self.variant.save(update_fields=["stock"])
        original, _, _ = self.case(source=self.choice)
        source = self.source("What is the price?", at=self.now+timedelta(seconds=2))
        revision, token = self.revision(source)
        result = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=source.provider_created_at)
        self.assertEqual(result["request_id"], str(original.pk))
        self.assertFalse(result["pure"])
        text = f"The price is {self.variant.price_override} UAH."
        planned = plan_revision_effects(revision.pk, token, source_message_id=source.pk,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding, authority_context_digest="e"*64,
            effects=[{"group": "substantive_text", "kind": "text", "payload": {
                "recipient": {"id": self.client_row.igsid}, "message": {"text": text}}}])
        self.assertTrue(planned.created, planned.reasons)
        physical = []
        def transport(payload):
            physical.append(payload)
            return ProviderPartResult("instagram_login:owner-restock", 200, "native-independent-price")
        sent = drain_group(revision.pk, token, "substantive_text", transport)
        self.assertEqual(sent.state, "sent")
        drain_group(revision.pk, token, "substantive_text", transport)
        self.assertEqual(len(physical), 1)
        self.assertEqual(IgRevisionDeliveryEffect.objects.filter(state="sent").count(), 1)
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 1)

    def test_explicit_reply_to_native_sent_proposal_accepts_without_new_stock_or_order(self):
        self.proposal()
        source = self.reply("I will wait.")
        result = self.state()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["source_refs"][0]["id"], source.pk)
        self.assertEqual(Decision.objects.count(), 1)
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.stock, 0)

    def test_pre_sent_reply_and_wrong_receipt_never_accept_waiting(self):
        self.proposal(send=False)
        self.reply("I will wait.")
        self.assertFalse(self.state()["accepted"])

    def test_notification_optin_does_not_mean_waiting_agreement(self):
        self.proposal()
        self.reply("Повідомте, коли буде в наявності")
        self.assertFalse(self.state()["accepted"])

    def test_later_duration_or_subset_counteroffer_cannot_resurrect_old_acceptance(self):
        for index, text in enumerate(("I will wait for 3 days.", "I will wait only for the first item.")):
            with self.subTest(text=text):
                # Exercise each sequence with one native source-owned request;
                # later counteroffer owns the current waiting state.
                if not Request.objects.exists():
                    self.proposal()
                self.reply("I will wait.", second=2+index*2)
                self.assertTrue(self.state()["accepted"])
                self.reply(text, second=3+index*2)
                self.assertFalse(self.state()["accepted"])

    def test_delayed_old_affirmation_cannot_override_newer_provider_refusal(self):
        self.proposal()
        refusal = self.reply("I will not wait.", second=4)
        delayed = self.reply("I will wait.", second=2)
        self.assertGreater(delayed.pk, refusal.pk)
        self.assertFalse(self.state()["accepted"])

    def test_plain_refusal_to_exact_proposal_revokes_prior_wait_acceptance(self):
        self.proposal()
        for index, text in enumerate(("No.", "Ні.", "Нет.")):
            self.reply("I will wait.", second=2+index*2)
            self.assertTrue(self.state()["accepted"])
            self.reply(text, second=3+index*2)
            self.assertFalse(self.state()["accepted"])

    def test_unrelated_plain_no_does_not_revoke_waiting(self):
        self.proposal()
        self.reply("I will wait.", second=2)
        self.reply("No.", second=4, target="other-question")
        self.assertTrue(self.state()["accepted"])

    def test_wrong_native_reply_target_does_not_accept(self):
        self.proposal()
        self.reply("I will wait.", target="another-proposal")
        self.assertFalse(self.state()["accepted"])


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False,
    ROOT_URLCONF='management.urls')
class NativeManagerReviewProjectionTests(TransactionTestCase):
    from management.tests_ig_stock_request_scope_20261023 import NativeUnlistedProductRequestTests as Fixtures
    setUp = Fixtures.setUp
    _message = Fixtures._message
    _prepare = Fixtures._prepare
    _execute = Fixtures._execute
    _replace_bundle = Fixtures._replace_bundle
    _generate = Fixtures._generate
    _fresh = Fixtures._fresh
    _answer = Fixtures._answer
    _select_product = Fixtures._select_product
    color_case = Fixtures.color_case

    def pending(self):
        from management.models import IgFollowUpTask
        self.color_case('Хочу худі кольору мокко.', 'мокко')
        self.customer.refresh_from_db()
        return IgFollowUpTask.objects.get(reason='revision_case:product_configuration_review')

    def projection(self, tasks):
        from management.services.ig_manager_review_projection import manager_review_projections
        return manager_review_projections(self.customer, tasks)

    def test_actual_skipped_pending_case_exposes_read_only_owned_source_navigation(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        task = self.pending()
        with CaptureQueriesContext(connection) as queries, patch('management.services.instagram_bot._provider_http') as provider:
            result = self.projection([task])
        self.assertIn(task.pk, result)
        hint = result[task.pk]
        self.assertEqual((hint['state'], hint['actionable'], hint['type']), ('pending', True, 'product_configuration_review'))
        self.assertEqual(hint['source_message_ids'], [self.source.pk])
        self.assertEqual(hint['client_id'], self.customer.pk)
        self.assertIn('мокко', hint['question'])
        self.assertEqual(task.status, 'skipped')
        self.assertTrue(all(row['sql'].lstrip().upper().startswith('SELECT') for row in queries), queries.captured_queries)
        provider.assert_not_called()

    def test_processed_and_mutated_source_or_permission_cannot_be_elevated(self):
        task = self.pending()
        self.assertIn(task.pk, self.projection([task]))
        for field, value in (('manager_approval_status', 'approved'), ('status', 'completed'), ('status', 'cancelled')):
            original = getattr(task, field)
            setattr(task, field, value)
            self.assertEqual(self.projection([task]), {})
            setattr(task, field, original)
        text = self.source.text
        self.source.text = 'A different source'
        self.source.save(update_fields=['text'])
        self.assertEqual(self.projection([task]), {})
        self.source.text = text
        self.source.save(update_fields=['text'])
        self.customer.reply_permission_epoch += 1
        self.customer.save(update_fields=['reply_permission_epoch'])
        self.assertEqual(self.projection([task]), {})

    def test_reset_namespace_and_forged_reason_only_rows_fail_closed(self):
        from management.models import IgFunnelResetAudit
        task = self.pending()
        self.assertIn(task.pk, self.projection([task]))
        original_namespace = self.source.provider_namespace
        self.source.provider_namespace = 'instagram_login:foreign-owner'
        self.source.save(update_fields=['provider_namespace'])
        self.assertEqual(self.projection([task]), {})
        self.source.provider_namespace = original_namespace
        self.source.save(update_fields=['provider_namespace'])
        IgFunnelResetAudit.objects.create(client=self.customer,
            reset_after_message_id=self.source.pk, reason='native projection reset')
        self.assertEqual(self.projection([task]), {})

    def test_malformed_native_receipts_fail_closed(self):
        task = self.pending()
        # Explicitly corrupt only this disposable historical test row. Both
        # model.save and queryset.update correctly fence immutable receipts.
        import json
        from django.db import connection
        table = connection.ops.quote_name(self.revision._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f'UPDATE {table} SET action_receipts = %s WHERE id = %s',
                [json.dumps(['unowned']), self.revision.pk])
        self.assertEqual(self.projection([task]), {})

    def test_changed_configuration_and_reset_hide_prior_case(self):
        from management.models import IgFunnelResetAudit
        task = self.pending()
        self.assertIn(task.pk, self.projection([task]))
        self._select_product(2)
        self.customer.refresh_from_db()
        self.assertEqual(self.projection([task]), {})

    def test_batched_sources_do_not_add_queries_per_displayed_candidate(self):
        from management.models import IgFollowUpTask
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        task = self.pending()
        with CaptureQueriesContext(connection) as first:
            self.assertIn(task.pk, self.projection([task]))
        tasks = [task]
        for index in range(7):
            tasks.append(IgFollowUpTask.objects.create(client=self.customer, kind='manager_task',
                status='skipped', reason=task.reason, manager_approval_status='pending',
                due_at=task.due_at, event_key=f'forged-candidate-{index}', manager_context=deepcopy(task.manager_context)))
        with CaptureQueriesContext(connection) as many:
            result = self.projection(tasks)
        self.assertEqual(set(result), {task.pk})
        self.assertLessEqual(len(many), len(first))

    def test_episode_watermark_is_not_mistaken_for_reset_floor(self):
        from django.urls import reverse
        from management.models import IgCommercialEpisode
        episode = IgCommercialEpisode.objects.create(client=self.customer, sequence=1,
            state='active', open_slot=1, opened_watermark_message_id=self.source.pk+1)
        self.customer.current_commercial_episode = episode
        self.customer.save(update_fields=['current_commercial_episode'])
        task = self.pending()
        self.assertIn(task.pk, self.projection([task]))
        manager = get_user_model().objects.create_superuser(username='episode-projection-reader', password='test')
        self.client.force_login(manager)
        response = self.client.get(reverse('management_bot_client_detail_api', args=[self.customer.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        row = next(row for row in response.json()['followups'] if row['id'] == task.pk)
        self.assertTrue(row['manager_review']['actionable'])

    def test_staff_detail_adds_hint_without_creating_or_resolving_cases(self):
        from django.urls import reverse
        from management.models import IgFollowUpTask
        task = self.pending()
        manager = get_user_model().objects.create_superuser(username='projection-reader', password='test')
        self.client.force_login(manager)
        before = IgFollowUpTask.objects.count()
        response = self.client.get(reverse('management_bot_client_detail_api', args=[self.customer.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        row = next(row for row in response.json()['followups'] if row['id'] == task.pk)
        self.assertTrue(row['manager_review']['actionable'])
        self.assertEqual(row['manager_review']['task_id'], task.pk)
        self.assertEqual(IgFollowUpTask.objects.count(), before)


class ColorChoiceFamilyCoverageTests(SimpleTestCase):
    def test_response_aliases_cover_source_canonical_color_vocabulary(self):
        from management.services.ig_commerce_turns import _COLOR_WORDS
        from management.services.ig_response_plan import CHOICE_ALIASES
        self.assertLessEqual(set(_COLOR_WORDS.values()), set(CHOICE_ALIASES))

    def test_missing_families_acknowledge_finite_english_ukrainian_russian_wishes(self):
        from management.services.ig_response_plan import _acknowledges_choice
        families = {'olive': ('olive', 'оливкового', 'оливкової'),
            'purple': ('violet', 'фиолетового', 'фіолетового'), 'beige': ('beige', 'бежевого', 'бежевої'),
            'burgundy': ('burgundy', 'бордо', 'бордового', 'бордовой', 'бордової', 'бордовій'), 'orange': ('orange', 'оранжевого', 'помаранчевого'),
            'red': ('red', 'красного', 'червоного'), 'yellow': ('yellow', 'жёлтого', 'жовтого'),
            'brown': ('brown', 'коричневого', 'коричневої')}
        for color, words in families.items():
            for word in words:
                with self.subTest(color=color, word=word):
                    self.assertTrue(_acknowledges_choice('color', color, f'Your preference is {word}.', {}))

    def test_negation_quotes_other_color_and_unbounded_suffix_are_not_acknowledgements(self):
        from management.services.ig_response_plan import _acknowledges_choice
        for text in ('Ваше пожелание — не фиолетового цвета.', 'Your preference is not purple.',
                     'Менеджер сообщил: «Ваше пожелание — фиолетового цвета».',
                     'Ваше пожелание — красного цвета.', 'Your preference is purpleish.',
                     'Ваше пожелание — фиолетовогошума.'):
            with self.subTest(text=text):
                self.assertFalse(_acknowledges_choice('color', 'purple', text, {}))

    def test_burgundy_word_boundaries_negation_and_quotes_retain_debt(self):
        from management.services.ig_response_plan import _acknowledges_choice
        for text in ('Ваше побажання — не бордової барви.',
                     'Менеджер повідомив: «Ваше побажання — бордової барви».',
                     'Ваше побажання — бордовоїшуми.', 'Your preference is burgundyish.'):
            with self.subTest(text=text):
                self.assertFalse(_acknowledges_choice('color', 'burgundy', text, {}))

    def test_new_color_acknowledgement_aliases_do_not_authorize_physical_stock(self):
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
        from management.services.ig_response_plan import CHOICE_ALIASES
        context = ReplyTruthContext(source_chosen_colors=CHOICE_ALIASES['purple'])
        for text in ('Фіолетове худі є в наявності.', 'Фиолетовое худи есть в наличии.',
                     'The purple hoodie is in stock.'):
            with self.subTest(text=text):
                self.assertFalse(validate_reply_truth(text, context=context).valid)

    def test_other_product_line_cannot_discharge_current_purple_wish(self):
        import json
        from management.services.ig_response_plan import ResponsePlan
        from management.services.ig_response_cart_plan import ResponseLinePlan
        rows = []
        for index, (title, garment) in enumerate((('Aurora hoodie', 'hoodie'), ('Other Tee', 'tshirt')), 1):
            rows.append(ResponseLinePlan(json.dumps({'line_id': f'line:{index}', 'index': index-1,
                'recipient_id':'self', 'choices':{'product_id':index, 'color':'purple', 'garment_type':garment},
                'evidence':{}, 'scope':{}, 'configuration':{'product_id':index, 'product_title':title},
                'next_selector':'', 'authority':{}, 'obligations':[]})))
        obligation = {'id':'7:line:1:color', 'kind':'color', 'line_id':'line:1',
            'source_message_id':7, 'value':'purple'}
        plan = ResponsePlan({}, {}, {}, {}, '', {}, (obligation,), line_plans=tuple(rows))
        coverage = plan.coverage(SimpleNamespace(reply_text='Your preference for Other Tee is purple.', control={}))
        self.assertEqual(coverage['remaining'], [obligation['id']])


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_TURN_CONTEXT_MODE='unified', IG_CLIENT_STATE_PROMPT_TOKENS=2400,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeColorChoiceCoverageTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True
    from management.tests_ig_standard_funnel_qa import NativeStockContinuationTests as _Fixture
    def setUp(self):
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests
        original = SourceCartCatalogTests.setUp
        def catalog_before_sources(case):
            original(case)
            case.category.name, case.category.slug = 'Худі', 'hoodies'
            case.category.save(update_fields=['name', 'slug'])
        with patch.object(SourceCartCatalogTests, 'setUp', catalog_before_sources):
            super().setUp()
        self.settings.ai_enabled = True
        self.settings.save(update_fields=['ai_enabled'])
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count = 0
        self.actual_hash_override = 'observed'
        self.provider_failure = False

    _generate = _Fixture._generate
    _execute = _Fixture._execute

    def answer(self, language, source_text, reply, *, expected_color="purple"):
        from management.models import IgFollowUpTask
        self.customer.language = language
        self.customer.save(update_fields=['language'])
        source = self.source(source_text)
        self.revision, self.token = _RestockFixture.revision(self, source)
        self.parsed = {'reply_text':reply, 'controls':[{'kind':'manager','value':True}]}
        result, generated, http = self._execute()
        self.revision.refresh_from_db()
        self.assertEqual(result.state, 'completed', (result.reasons, self.revision.action_receipts))
        self.assertEqual((generated.call_count, http.call_count), (1, 1))
        self.assertEqual(self.revision.delivery_effects.filter(state='sent').count(), 1)
        self.assertFalse(self.revision.action_receipts['response_coverage']['remaining'])
        task = IgFollowUpTask.objects.get(client=self.customer)
        self.assertEqual(task.reason, 'revision_case:product_configuration_review')
        inquiry, = task.manager_context['product_inquiries']['inquiries']
        self.assertEqual((inquiry['source_message_id'], inquiry['requested_value']), (source.pk, expected_color))
        self.assertEqual(IgBotNotification.objects.filter(client=self.customer).count(), 1)
        replay, generated, http = self._execute()
        self.assertEqual(replay.state, 'completed', replay.reasons)
        generated.assert_not_called()
        http.assert_not_called()

    def test_russian_natural_purple_wish_has_one_sent_and_no_debt(self):
        self.answer('ru', 'Хочу это худи фиолетового цвета.',
            'Ваше пожелание — худи фиолетового цвета. Уточню у менеджера возможность худи фиолетового цвета.')

    def test_ukrainian_natural_purple_wish_has_one_sent_and_no_debt(self):
        self.answer('uk', 'Хочу це худі фіолетового кольору.',
            'Ваше побажання — худі фіолетового кольору. Уточню у менеджера можливість такого варіанту.')

    def test_english_natural_purple_wish_has_one_sent_and_no_debt(self):
        self.answer('en', 'I want this hoodie in purple.',
            'Your preference is a purple hoodie. I will check the possibility with our manager.')


    def test_russian_burgundy_genitive_color_has_one_sent_and_no_debt(self):
        self.answer('ru', 'Хочу это худи бордового цвета.',
            'Ваше пожелание — худи бордового цвета. Уточню у менеджера возможность такого варианта.',
            expected_color='burgundy')

    def test_russian_burgundy_inflected_palette_has_one_sent_and_no_debt(self):
        self.answer('ru', 'Хочу это худи в бордовой расцветке.',
            'Ваше пожелание — худи в бордовой расцветке. Уточню у менеджера возможность такого варианта.',
            expected_color='burgundy')

    def test_ukrainian_burgundy_inflected_color_has_one_sent_and_no_debt(self):
        self.answer('uk', 'Хочу це худі бордової барви.',
            'Ваше побажання — худі бордової барви. Уточню у менеджера можливість такого варіанту.',
            expected_color='burgundy')


    def test_ukrainian_burgundy_locative_palette_has_one_sent_and_no_debt(self):
        self.answer('uk', 'Хочу це худі у бордовій гамі.',
            'Ваше побажання — худі у бордовій гамі. Уточню у менеджера можливість такого варіанту.',
            expected_color='burgundy')
