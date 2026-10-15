"""Execute live integration boundaries without ORM, fixtures or provider I/O."""
import ast
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


LIVE = Path(__file__).parent / "services" / "ig_revision_live.py"


def function(name, **namespace):
    tree = ast.parse(LIVE.read_text())
    node = next(row for row in tree.body if isinstance(row, ast.FunctionDef) and row.name == name)
    values = {"re": re, **namespace}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(LIVE), "exec"), values)
    return values[name]


def module(name, **values):
    result = ModuleType(name)
    result.__dict__.update(values)
    return result


def preparation(*, handled=False, effects=(), reasons=(), copy_hint=""):
    return SimpleNamespace(handled=handled, effects=effects, reasons=reasons,
        ready=handled and bool(effects) and not reasons, copy_hint=copy_hint)


class CatalogLiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = SimpleNamespace(pk=7, igsid="customer-7", current_product_id=101,
            current_color="", current_size="")
        self.revision = SimpleNamespace(pk=50, client=self.client, generation_proposal={},
            bundle_snapshot={"sources": [{"message_id": 20, "source_namespace": "instagram_login:owner"}]})
        self.settings = SimpleNamespace(pk=4)
        self.source = SimpleNamespace(pk=20, text="Обрати", quick_reply_payload="CC1:12:0:signed")
        self.response = SimpleNamespace(valid=True, reply_text="Вартість доставки уточнить менеджер.", control={})

    def visible(self, *, choice=None, source_reasons=(), selector="", copy_reason="", plain_chart=False, **factories):
        self.bot = SimpleNamespace(_catalog_media_selection_for_control=Mock(return_value=None),
            _append_more_products_hint=Mock(side_effect=lambda text, *_args: text))
        self.media = Mock(return_value=SimpleNamespace(payloads=(), product_refs=()))
        self.factories = {name: Mock(return_value=preparation()) for name in (
            "prepare_catalog_choice_presentations", "prepare_catalog_configuration_choices",
            "prepare_catalog_chart_effect", "prepare_catalog_choice_page")}
        self.factories.update(factories)
        modules = {
            "management.services": module("management.services", instagram_bot=self.bot),
            "management.services.ig_catalog_media": module("management.services.ig_catalog_media", prepare_catalog_media=self.media),
            "management.services.ig_catalog_choice_presentations": module("management.services.ig_catalog_choice_presentations", **self.factories),
        }
        resolve = Mock(return_value=(choice, source_reasons))
        choose = Mock(return_value=selector)
        execute = function("_prepare_visible_content", _catalog_native_source_choice=resolve,
            _catalog_configuration_selector=choose, _catalog_unavailable_chart_reason=Mock(return_value=copy_reason),
            _catalog_plain_chart_requested=Mock(return_value=plain_chart),
            _catalog_prepared_reply=function("_catalog_prepared_reply"))
        with patch.dict(sys.modules, modules):
            result = execute(self.revision, self.response, self.settings)
        return result, resolve, choose

    def test_ordinary_products_use_native_before_legacy_and_keep_text(self):
        self.response.control = {"show_products": "101,102,103"}
        effects = ({"group": "template", "payload": {"message": "signed native"}},
            {"group": "template_fallback", "payload": {"message": "signed fallback"}})
        result, *_ = self.visible(prepare_catalog_choice_presentations=Mock(return_value=preparation(handled=True, effects=effects)))
        self.assertEqual(result, (effects, self.response.reply_text, ()))
        self.bot._catalog_media_selection_for_control.assert_not_called()
        self.media.assert_not_called()

    def test_native_preparation_error_does_not_fall_through_to_wrong_photos(self):
        result, *_ = self.visible(prepare_catalog_choice_presentations=Mock(return_value=preparation(
            handled=True, reasons=("catalog_choice_photo_changed",))))
        self.assertEqual(result[2], ("catalog_choice_photo_changed",))
        self.bot._catalog_media_selection_for_control.assert_not_called()

    def test_checkout_keeps_legacy_without_native_source_resolution(self):
        self.response.control = {"paylink": "full", "show_products": "101"}
        result, resolve, choose = self.visible()
        self.assertEqual(result, ((), self.response.reply_text, ()))
        resolve.assert_not_called()
        choose.assert_not_called()
        self.bot._catalog_media_selection_for_control.assert_called_once()
        for factory in self.factories.values():
            factory.assert_not_called()

    def test_delivered_chart_choice_passes_actual_result_and_preserves_mixed_question_reply(self):
        choice = {"ready": True, "action": {"kind": "chart", "product_id": 102}, "manifest_digest": "origin"}
        chart = Mock(return_value=preparation(handled=True, effects=({"payload": "real grid"},)))
        result, *_ = self.visible(choice=choice, prepare_catalog_chart_effect=chart)
        self.assertIs(chart.call_args.kwargs["choice"], choice)
        self.assertEqual(result[1], self.response.reply_text)
        self.factories["prepare_catalog_choice_presentations"].assert_not_called()
        self.bot._catalog_media_selection_for_control.assert_not_called()

    def test_page_uses_delivered_order_even_if_model_reranks(self):
        self.response.control = {"show_products": "999,998"}
        choice = {"ready": True, "action": {"kind": "page", "page": 1}, "manifest": {"ordered_product_ids": [101, 102]}}
        page = Mock(return_value=preparation(handled=True, effects=({"payload": "delivered order"},)))
        result, *_ = self.visible(choice=choice, prepare_catalog_choice_page=page)
        self.assertIs(page.call_args.kwargs["choice"], choice)
        self.assertEqual(result[0], ({"payload": "delivered order"},))
        self.factories["prepare_catalog_choice_presentations"].assert_not_called()

    def test_ambiguous_chart_preserves_disclosed_reply_without_product_photo(self):
        choice = {"ready": True, "action": {"kind": "chart"}}
        result, *_ = self.visible(choice=choice)
        self.assertEqual(result, ((), self.response.reply_text, ()))
        self.bot._catalog_media_selection_for_control.assert_not_called()

    def test_stale_chart_keeps_honest_mixed_question_reply_without_effects(self):
        result, *_ = self.visible(source_reasons=("catalog_choice_scope_changed",))
        self.assertEqual(result, ((), self.response.reply_text, ()))
        for factory in self.factories.values():
            factory.assert_not_called()

    def test_stale_chart_cannot_send_claim_of_attached_image(self):
        self.response.reply_text = "Таблиця розмірів нижче. Доставку оплачує отримувач."
        result, *_ = self.visible(source_reasons=("catalog_choice_scope_changed",),
            copy_reason="catalog_choice_chart_copy_without_image")
        self.assertEqual(result[0], ())
        self.assertEqual(result[2], ("catalog_choice_chart_copy_without_image",))

    def resolve(self, sources, delivery):
        execute = function("_catalog_native_source_choice", _revision_media_reply_sources=Mock(return_value=sources))
        with patch.dict(sys.modules, {"management.services.ig_catalog_choice_presentations":
            module("management.services.ig_catalog_choice_presentations", delivered_catalog_choice=delivery)}):
            return execute(self.revision)

    def test_chart_recovery_requires_exact_own_source_and_fresh_delivered_receipt(self):
        historical = {"ready": True, "action": {"kind": "chart"}}
        fresh = {**historical, "manifest_digest": "actual-delivered-manifest"}
        delivery = Mock(side_effect=[historical, fresh])
        result = self.resolve([self.source], delivery)
        self.assertIs(result[0], fresh)
        self.assertEqual([call.kwargs["fresh"] for call in delivery.call_args_list], [False, True])
        self.assertTrue(all(call.args[1] is self.source for call in delivery.call_args_list))

    def test_changed_source_or_model_payload_cannot_create_a_chart(self):
        delivery = Mock()
        result = self.resolve([], delivery)
        self.assertEqual(result, (None, ()))
        delivery.assert_not_called()
        self.source.quick_reply_payload = "[SHOW_PRODUCTS:101]"
        result = self.resolve([self.source], delivery)
        self.assertEqual(result, (None, ()))
        delivery.assert_not_called()

    def test_two_own_chart_actions_are_refused_instead_of_dropping_one(self):
        ready = {"ready": True, "action": {"kind": "chart"}}
        delivery = Mock(return_value=ready)
        result = self.resolve([self.source, SimpleNamespace(pk=21, quick_reply_payload="CC1:12:1:signed")], delivery)
        self.assertEqual(result, (None, ("catalog_choice_source_ambiguous",)))

    def selector(self, *, plan=None, sources=None, action=None, question=False):
        plan = plan or SimpleNamespace(plan_gap="", line_plans=(), source_cart_capture={},
            next_selector="size", configuration={"missing": ["size"]}, choices={"product_id": 101},
            evidence={"product_id": {"source_message_id": 20}})
        capture = Mock(return_value=plan)
        execute = function("_catalog_configuration_selector", _revision_media_reply_sources=Mock(
            return_value=[self.source] if sources is None else sources))
        modules = {"management.services.ig_response_plan": module("management.services.ig_response_plan",
            capture_response_plan=capture, asks_next_selector=Mock(return_value=question)),
            "management.services.ig_catalog_choice_actions": module("management.services.ig_catalog_choice_actions",
                native_choice_request=Mock(return_value=action))}
        with patch.dict(sys.modules, modules):
            return execute(self.revision, self.response), capture

    def test_actual_own_product_with_missing_required_size_gets_selector(self):
        result, _ = self.selector()
        self.assertEqual(result, "size")

    def test_known_size_and_ambiguous_multiline_never_get_selector(self):
        self.client.current_size = "L"
        self.assertEqual(self.selector()[0], "")
        self.client.current_size = ""
        plan = SimpleNamespace(plan_gap="", line_plans=({}, {}), source_cart_capture={})
        self.assertEqual(self.selector(plan=plan)[0], "")

    def test_no_current_source_or_invalid_response_cannot_get_selector(self):
        self.assertEqual(self.selector(sources=[])[0], "")
        self.response.valid = False
        result, capture = self.selector(question=True)
        self.assertEqual(result, "")
        capture.assert_not_called()

    def test_model_question_does_not_repeat_source_known_size(self):
        plan = SimpleNamespace(plan_gap="", line_plans=(), source_cart_capture={}, next_selector="size",
            configuration={"missing": ["size"]}, choices={"size": "L"}, evidence={})
        self.assertEqual(self.selector(plan=plan, question=True)[0], "")

    def test_resumed_chart_revalidates_original_source_and_origin(self):
        self.revision.delivery_effects = SimpleNamespace(all=lambda: [SimpleNamespace(projection_metadata={
            "catalog_chart": {"origin_manifest_digest": "old-origin", "scope": {}}})])
        execute = function("_catalog_source_effect_reason", _revision_media_reply_sources=Mock(return_value=[self.source]),
            _catalog_native_source_choice=Mock(return_value=({"manifest_digest": "changed-origin"}, ())))
        self.assertEqual(execute(self.revision), "catalog_choice_origin_changed")

    def test_resumed_native_effect_blocks_changed_sealed_source(self):
        self.revision.delivery_effects = SimpleNamespace(all=lambda: [SimpleNamespace(projection_metadata={
            "catalog_choice": {"scope": {}}})])
        execute = function("_catalog_source_effect_reason", _revision_media_reply_sources=Mock(return_value=[]))
        self.assertEqual(execute(self.revision), "catalog_choice_source_changed")

    def test_runtime_guidance_covers_payer_cap_stock_chart_and_remaining_questions(self):
        with patch.dict(sys.modules, {"management.services.ig_catalog_category_discovery":
            module("management.services.ig_catalog_category_discovery", catalog_discovery_guidance=lambda: "The shortlist is only a few models; full category is on the cards.")}):
            text = function("_catalog_customer_guidance")()
        for phrase in ("remaining request", "zero-stock", "recipient pays", "cap is not the actual",
            "explicit approved", "insurance", "guaranteed arrival", "manager confirmation", "image in this chat"):
            self.assertIn(phrase, text)
        self.assertIn("full category is on the cards", text)

    def test_ready_category_notice_preserves_generated_delivery_answer_and_link(self):
        self.response.reply_text = "Доставку оплачує отримувач. Посилання: https://twocomms.test/product/model/"
        hint = "Кілька моделей для початку. Повний вибір у категорії — кнопка «Всі футболки»."
        prepared = preparation(handled=True, effects=({"payload": "native cards"},), copy_hint=hint)
        result, *_ = self.visible(prepare_catalog_choice_presentations=Mock(return_value=prepared))
        self.assertEqual(result[1], self.response.reply_text + "\n\n" + hint)
        self.assertEqual(self.response.reply_text.count("Доставку"), 1)
        self.assertEqual(result[1].count("https://"), 1)

    def test_category_notice_requires_ready_effect_and_is_not_duplicated(self):
        execute = function("_catalog_prepared_reply")
        hint = "Explore the full category from the card."
        self.assertEqual(execute("Original answer", preparation(handled=True, reasons=("changed",), copy_hint=hint)), "Original answer")
        self.assertEqual(execute("Original answer\n" + hint, preparation(handled=True, effects=({},), copy_hint=hint)), "Original answer\n" + hint)

    def test_late_missing_chart_cannot_keep_positive_attachment_claim(self):
        execute = function("_catalog_unavailable_chart_reason")
        with patch.dict(sys.modules, {"management.services.ig_reply_truth": module("management.services.ig_reply_truth",
            _locally_negated=lambda clause, position: "не " in clause[:position].lower())}):
            self.assertEqual(execute("Таблиця розмірів нижче."), "catalog_choice_chart_copy_without_image")
            self.assertEqual(execute("Не надсилаю таблицю до підтвердження менеджера."), "")
            self.assertEqual(execute("Менеджер уточнить таблицю розмірів."), "")

    def test_unavailable_chart_is_disclosed_before_generation(self):
        choice = {"ready": True, "action": {"kind": "chart", "product_id": 101}}
        factory = Mock(return_value=preparation())
        execute = function("_catalog_source_guidance", _catalog_native_source_choice=Mock(return_value=(choice, ())),
            ValidatedResponse=lambda text: SimpleNamespace(reply_text=text, control={}),
            _catalog_plain_chart_requested=Mock(return_value=False))
        with patch.dict(sys.modules, {"management.services.ig_catalog_choice_presentations":
            module("management.services.ig_catalog_choice_presentations", prepare_catalog_chart_effect=factory)}):
            text = execute(self.revision, self.settings)
        self.assertIn("unavailable or ambiguous", text)
        self.assertIn("Do not say the chart is attached", text)
        self.assertIs(factory.call_args.kwargs["choice"], choice)

    def test_plain_own_chart_question_sends_actual_grid_without_fabricated_choice(self):
        factory = Mock(return_value=preparation(handled=True, effects=({"payload": "actual configured grid"},)))
        result, *_ = self.visible(plain_chart=True, prepare_catalog_chart_effect=factory)
        self.assertEqual(result[0], ({"payload": "actual configured grid"},))
        self.assertEqual(factory.call_args.kwargs, {})
        self.assertEqual(result[1], self.response.reply_text)
        self.bot._catalog_media_selection_for_control.assert_not_called()

    def test_plain_chart_source_parser_refuses_quotes_reports_foreign_model_and_multiline(self):
        self.source.quick_reply_payload = ""
        plan = SimpleNamespace(plan_gap="", line_plans=(), source_cart_capture={})
        capture = Mock(return_value=plan)
        execute = function("_catalog_plain_chart_requested", _revision_media_reply_sources=Mock(return_value=[self.source]))
        modules = {"management.services.ig_response_plan": module("management.services.ig_response_plan",
            capture_response_plan=capture, _unquoted_text=lambda value: re.sub(r'«[^»]*»|"[^"\n]*"', "", value)),
            "management.services.ig_reply_truth": module("management.services.ig_reply_truth",
                _locally_negated=lambda clause, position: "не " in clause[:position].lower())}
        with patch.dict(sys.modules, modules):
            self.source.text = "Надішліть таблицю розмірів. І як оплачується доставка?"
            self.assertTrue(execute(self.revision))
            for text in ('Він сказав «надішліть таблицю розмірів»', "Він просив надішліть таблицю розмірів",
                "Не надсилайте таблицю розмірів", "Покажіть таблицю для іншої моделі"):
                self.source.text = text
                self.assertFalse(execute(self.revision), text)
            self.source.text = "Надішліть таблицю розмірів"
            plan.line_plans = ({}, {})
            self.assertFalse(execute(self.revision))

    def test_generation_appends_runtime_guidance_to_active_publication_turn_note(self):
        tree = ast.parse(LIVE.read_text())
        generation = next(row for row in tree.body if isinstance(row, ast.FunctionDef) and row.name == "_generate_proposal")
        calls = [row for row in ast.walk(generation) if isinstance(row, ast.Call) and isinstance(row.func, ast.Name)]
        self.assertIn("_catalog_customer_guidance", [row.func.id for row in calls])
        self.assertIn("_catalog_source_guidance", [row.func.id for row in calls])


if __name__ == "__main__":
    unittest.main()
