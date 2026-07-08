import ast
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import PromoCode, PromoCodeGroup, UserPromoCode


def _safe_eval_node(node, helpers):
    """
    W3-10 (NEW-505): безопасный интерпретатор условий опроса.

    Исполняет только whitelist AST-узлов: константы, and/or/not,
    сравнения (==, !=, <, <=, >, >=, in, not in) и вызовы helper-функций
    по имени. Атрибуты, индексация, comprehension'ы, f-строки и любые
    другие конструкции → ValueError (в вызывающем коде это warning+False).
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result = True
            for value in node.values:
                result = _safe_eval_node(value, helpers)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            for value in node.values:
                result = _safe_eval_node(value, helpers)
                if result:
                    return result
            return result
        raise ValueError("unsupported bool op")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _safe_eval_node(node.operand, helpers)
    if isinstance(node, ast.Compare):
        left = _safe_eval_node(node.left, helpers)
        for op, comparator in zip(node.ops, node.comparators):
            right = _safe_eval_node(comparator, helpers)
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.Lt):
                ok = left is not None and right is not None and left < right
            elif isinstance(op, ast.LtE):
                ok = left is not None and right is not None and left <= right
            elif isinstance(op, ast.Gt):
                ok = left is not None and right is not None and left > right
            elif isinstance(op, ast.GtE):
                ok = left is not None and right is not None and left >= right
            elif isinstance(op, ast.In):
                ok = right is not None and left in right
            elif isinstance(op, ast.NotIn):
                ok = right is None or left not in right
            else:
                raise ValueError("unsupported comparison op")
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in helpers:
            raise ValueError("unsupported function call")
        if node.keywords:
            raise ValueError("keyword arguments not allowed")
        args = [_safe_eval_node(arg, helpers) for arg in node.args]
        return helpers[node.func.id](*args)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_safe_eval_node(elt, helpers) for elt in node.elts]
    raise ValueError(f"unsupported expression node: {type(node).__name__}")

logger = logging.getLogger(__name__)


def _default_survey_path() -> Path:
    return Path(
        getattr(
            settings,
            "SURVEY_DEFINITION_PATH",
            settings.BASE_DIR / "surveys" / "twocomms_survey_v3_4_adaptive_research.json",
        )
    )


_SURVEY_CACHE: Dict[str, Any] = {
    "path": None,
    "mtime": None,
    "data": None,
}


def load_survey_definition(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load survey definition JSON with simple mtime cache."""
    target = Path(path or _default_survey_path())
    try:
        mtime = target.stat().st_mtime
    except FileNotFoundError:
        logger.error("Survey definition not found: %s", target)
        return {}

    if (
        _SURVEY_CACHE["data"]
        and _SURVEY_CACHE["mtime"] == mtime
        and _SURVEY_CACHE["path"] == target
    ):
        return _SURVEY_CACHE["data"]

    try:
        with target.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Failed to load survey definition %s: %s", target, exc)
        return {}

    _SURVEY_CACHE.update({"path": target, "mtime": mtime, "data": data})
    return data


def clear_survey_definition_cache() -> None:
    """Clear cached survey definition (useful in tests)."""
    _SURVEY_CACHE.update({"path": None, "mtime": None, "data": None})


def get_soft_hint(answered_count: int) -> str:
    """Return a soft progress hint without numeric progress."""
    if answered_count <= 3:
        return "Ще трохи — це швидко."
    if answered_count <= 7:
        return "Супер, рухаємось далі."
    if answered_count <= 11:
        return "Майже готово."
    return "Останні штрихи."


def create_survey_promocode(
    survey_key: str,
    reward_def: Dict[str, Any],
) -> PromoCode:
    """Create a one-use promo code for a completed survey."""
    reward_def = reward_def or {}
    amount = reward_def.get("amount_uah", 0)
    expires_in_days = int(reward_def.get("expires_in_days", 5))

    group_name = f"Survey: {survey_key}"
    promo_group, _ = PromoCodeGroup.objects.get_or_create(
        name=group_name,
        defaults={
            "description": f"Survey reward for {survey_key}",
            "one_per_account": True,
            "is_active": True,
        },
    )

    now = timezone.now()
    return PromoCode.objects.create(
        code=PromoCode.generate_code(),
        promo_type="voucher",
        discount_type="fixed",
        discount_value=amount,
        description=reward_def.get("title_uk") or f"Survey reward {survey_key}",
        group=promo_group,
        max_uses=1,
        one_time_per_user=True,
        valid_from=now,
        valid_until=now + timedelta(days=expires_in_days),
        is_active=True,
    )


class SurveyEngine:
    """Data-driven survey engine using JSON definition."""

    def __init__(self, definition: Dict[str, Any]) -> None:
        self.definition = definition or {}
        self.questions: List[Dict[str, Any]] = self.definition.get("questions", [])
        self.questions_by_id = {
            q.get("id"): q for q in self.questions if isinstance(q, dict) and q.get("id")
        }
        self.questions_by_section: Dict[str, List[Dict[str, Any]]] = {}
        for question in self.questions:
            section = question.get("section", "")
            self.questions_by_section.setdefault(section, []).append(question)
        for section, items in self.questions_by_section.items():
            items.sort(key=lambda item: item.get("order", 0))
        flow = self.definition.get("flow", {})
        self.flow_core = flow.get("core", []) if isinstance(flow, dict) else []
        self.flow_closing = flow.get("closing", []) if isinstance(flow, dict) else []
        if not self.flow_core and not self.flow_closing:
            self.flow_core, self.flow_closing = self._derive_flow_from_screens()
        self.caps = self.definition.get("caps", {})
        self.policy = self.definition.get("policy", {})
        end_conditions = self.definition.get("end_conditions", {})
        self.required_questions = set(end_conditions.get("required_questions", []))

    def _derive_flow_from_screens(self) -> Tuple[List[str], List[str]]:
        core_ids: List[str] = []
        closing_ids: List[str] = []
        screens = self.definition.get("screens", []) or []
        for screen in sorted(screens, key=lambda item: item.get("order", 0) if isinstance(item, dict) else 0):
            if not isinstance(screen, dict):
                continue
            question_ids = [qid for qid in screen.get("questions", []) if qid in self.questions_by_id]
            if screen.get("type") == "closing" or screen.get("id", "").startswith("s9"):
                closing_ids.extend(question_ids)
            else:
                core_ids.extend(question_ids)
        return self._unique_ids(core_ids), self._unique_ids(closing_ids)

    def _unique_ids(self, question_ids: List[str]) -> List[str]:
        seen = set()
        unique: List[str] = []
        for qid in question_ids:
            if qid in seen:
                continue
            seen.add(qid)
            unique.append(qid)
        return unique

    def get_question(self, question_id: str) -> Optional[Dict[str, Any]]:
        return self.questions_by_id.get(question_id)

    def serialize_question(
        self,
        question: Dict[str, Any],
        answer: Any = None,
        answers: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        answers = answers or {}
        question = question or {}
        prompt = question.get("prompt_uk") or question.get("prompt")
        payload = {
            "id": question.get("id"),
            "type": question.get("type"),
            "prompt": prompt,
            "help": question.get("help_uk"),
            "instruction": question.get("instruction_uk"),
            "ui_hint": question.get("ui_hint_uk"),
            "required": bool(question.get("required")),
            "options": [],
            "scale": question.get("scale"),
            "placeholder": question.get("placeholder_uk"),
            "max_select": question.get("max_select"),
            "max_chars": question.get("max_chars"),
            "answer": answer,
        }
        if question.get("type") == "price_ladder_by_product":
            payload.update(self._price_ladder_payload(question, answers))

        for key in ("concepts", "scale_options", "rows", "columns", "buckets", "channels"):
            if key in question:
                payload[key] = self._localize_items(question.get(key))

        options = payload.get("options") or self._options_for_question(question, answers)
        payload["options"] = []
        for opt in options:
            payload["options"].append(
                {
                    "value": opt.get("value"),
                    "label": opt.get("label_uk") or opt.get("label"),
                    "help": opt.get("help_uk"),
                }
            )
        return payload

    def evaluate_condition(self, expr: Any, context: Dict[str, Any]) -> bool:
        if expr in (None, {}, []):
            return True
        if isinstance(expr, list):
            return all(self.evaluate_condition(item, context) for item in expr)
        if isinstance(expr, str):
            return self._evaluate_string_condition(expr, context)
        if not isinstance(expr, dict):
            return False

        handled = False
        result = True

        if "any" in expr:
            handled = True
            result = result and any(self.evaluate_condition(item, context) for item in expr.get("any", []))
        if "all" in expr:
            handled = True
            result = result and all(self.evaluate_condition(item, context) for item in expr.get("all", []))
        if "not" in expr:
            handled = True
            result = result and not self.evaluate_condition(expr.get("not"), context)

        for flag in ("text_slots_available", "module_text_slots_available"):
            if flag in expr:
                handled = True
                result = result and bool(context.get(flag)) == bool(expr.get(flag))

        if "answer" in expr:
            handled = True
            left = self._answer_value(expr.get("answer"), context)
            if "row" in expr:
                if not isinstance(left, dict):
                    result = False
                else:
                    result = result and left.get(expr.get("row")) == expr.get("column")
            result = result and self._evaluate_direct_condition(left, expr)

        op = expr.get("op")
        var = expr.get("var")
        value = expr.get("value")
        if op or var:
            handled = True
            left = self._resolve_var(var, context)

            try:
                if op == "eq":
                    result = result and left == value
                elif op == "lte":
                    result = result and left is not None and left <= value
                elif op == "gte":
                    result = result and left is not None and left >= value
                elif op == "includes":
                    result = result and self._includes(left, value)
                elif op == "in":
                    result = result and value is not None and left in value
                elif op == "exists":
                    result = result and not self._is_empty(left)
                elif op:
                    logger.warning("Unknown operator in survey condition: %s", op)
                    result = False
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Condition eval failed for %s: %s", expr, exc)
                return False

        return result if handled else False

    def _evaluate_direct_condition(self, left: Any, expr: Dict[str, Any]) -> bool:
        try:
            if "eq" in expr and left != expr.get("eq"):
                return False
            if "neq" in expr and left == expr.get("neq"):
                return False
            if "not_eq" in expr and left == expr.get("not_eq"):
                return False
            if "in" in expr and left not in (expr.get("in") or []):
                return False
            if "not_in" in expr and left in (expr.get("not_in") or []):
                return False
            if "includes" in expr and not self._includes(left, expr.get("includes")):
                return False
            if "includes_any" in expr and not self._includes_any(left, expr.get("includes_any") or []):
                return False
            if "contains_any" in expr and not self._includes_any(left, expr.get("contains_any") or []):
                return False
            if "gte" in expr and (left is None or left < expr.get("gte")):
                return False
            if "lte" in expr and (left is None or left > expr.get("lte")):
                return False
            if "gt" in expr and (left is None or left <= expr.get("gt")):
                return False
            if "lt" in expr and (left is None or left >= expr.get("lt")):
                return False
            if "between" in expr:
                bounds = expr.get("between") or []
                if len(bounds) != 2 or left is None or left < bounds[0] or left > bounds[1]:
                    return False
            if "exists" in expr:
                exists = not self._is_empty(left)
                if bool(expr.get("exists")) != exists:
                    return False
        except TypeError:
            return False
        return True

    def _evaluate_string_condition(self, expr: str, context: Dict[str, Any]) -> bool:
        source = expr.strip()
        if not source:
            return True
        source = re.sub(r"\bAND\b", "and", source, flags=re.IGNORECASE)
        source = re.sub(r"\bOR\b", "or", source, flags=re.IGNORECASE)
        source = re.sub(
            r"answers\.([A-Za-z0-9_]+)\s+includes\s+'([^']*)'",
            r'_includes(_answer("\1"), "\2")',
            source,
        )
        source = re.sub(
            r"count\(answers\.([A-Za-z0-9_]+)\)",
            r'_count(_answer("\1"))',
            source,
        )
        source = re.sub(
            r"answers\.([A-Za-z0-9_]+)\[0\]",
            r'_first(_answer("\1"))',
            source,
        )
        source = re.sub(
            r"answers\.([A-Za-z0-9_]+)",
            r'_answer("\1")',
            source,
        )
        # W3-10 (NEW-505): eval() заменён на AST-walker с whitelist узлов.
        # `eval(..., {"__builtins__": {}})` обходится через атрибуты объектов
        # (клас��ический sandbox-escape). AST-подход исполняет ТОЛЬКО
        # разрешённые конструкции: литералы, and/or/not, сравнения и вызовы
        # четырёх helper-функций. Всё прочее (атрибуты, индексация, лямбды,
        # f-строки) — отказ с warning.
        helpers = {
            "_answer": lambda qid: self._answer_value(qid, context),
            "_count": lambda value: len(value) if isinstance(value, (list, tuple, set)) else (0 if self._is_empty(value) else 1),
            "_first": lambda value: value[0] if isinstance(value, (list, tuple)) and value else None,
            "_includes": self._includes,
        }
        try:
            tree = ast.parse(source, mode="eval")
            return bool(_safe_eval_node(tree.body, helpers))
        except Exception as exc:
            logger.warning("String condition eval failed for %s: %s", expr, exc)
            return False

    def _answer_value(self, question_id: Optional[str], context: Dict[str, Any]) -> Any:
        if not question_id:
            return None
        answers = context.get("answers") or {}
        return answers.get(question_id)

    def _includes(self, left: Any, value: Any) -> bool:
        if left is None:
            return False
        if isinstance(left, (list, tuple, set)):
            return value in left
        if isinstance(left, dict):
            return value in left.values() or value in left.keys()
        if isinstance(left, str):
            return str(value) in left
        return False

    def _includes_any(self, left: Any, values: List[Any]) -> bool:
        return any(self._includes(left, value) for value in values)

    def _resolve_var(self, var: Optional[str], context: Dict[str, Any]) -> Any:
        if not var:
            return None
        if var in context:
            return context.get(var)
        answers = context.get("answers") or {}
        if var in answers:
            return answers.get(var)
        parts = var.split('.')
        current = context.get(parts[0])
        for part in parts[1:]:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current

    def select_modules(self, answers: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> List[str]:
        modules = self.definition.get("modules", {})
        context = {"answers": answers or {}, "meta": meta or {}}
        engine_def = self.definition.get("engine") or {}
        if engine_def.get("module_selection_strategy"):
            scores = dict(engine_def.get("score_initialization") or {key: 0 for key in modules.keys()})
            for rule in engine_def.get("module_scoring_rules", []) or []:
                if not isinstance(rule, dict):
                    continue
                if self.evaluate_condition(rule.get("if"), context):
                    for module_key, amount in (rule.get("add") or {}).items():
                        scores[module_key] = scores.get(module_key, 0) + amount

            if answers.get("q010_entry_goal") == "partner" or answers.get("q020_purchase_stage") == "partner_discussion":
                scores["partner_lab"] = scores.get("partner_lab", 0) + 4
            if answers.get("q010_entry_goal") == "custom_team_unit":
                scores["partner_lab"] = scores.get("partner_lab", 0) + 2

            max_modules = int(self.caps.get("max_dynamic_modules_per_session", 2))
            order = {module_key: index for index, module_key in enumerate(scores.keys())}
            selected = [
                module_key
                for module_key, score in sorted(scores.items(), key=lambda item: (-item[1], order.get(item[0], 999)))
                if score > 0 and module_key in modules
            ]
            if not selected and "marketing_content_lab" in modules:
                selected = ["marketing_content_lab"]
            return selected[:max_modules]

        activated: List[str] = []
        for key, module in modules.items():
            activation = module.get("activation")
            if activation and self.evaluate_condition(activation, context):
                activated.append(key)

        module_policy = (
            self.policy
            .get("branching", {})
            .get("module_selection", {})
        )
        base_max = module_policy.get("base_max_modules", len(activated))
        low_score_threshold = module_policy.get("low_score_threshold")
        pain_threshold = module_policy.get("pain_threshold")
        max_if_many = module_policy.get("max_modules_if_many_pains", base_max)
        max_modules = base_max

        if low_score_threshold is not None and pain_threshold is not None:
            pain_count = 0
            for qid in self.flow_core:
                value = answers.get(qid)
                if isinstance(value, (int, float)) and value <= low_score_threshold:
                    pain_count += 1
            if pain_count >= pain_threshold:
                max_modules = max_if_many

        return activated[:max_modules]

    def get_next_question(
        self,
        answers: Dict[str, Any],
        history: List[str],
        meta: Optional[Dict[str, Any]] = None,
        module_order: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        answers = answers or {}
        history = history or []
        self._apply_auto_answers(answers, history)
        answered_ids = set(history)
        answered_ids.update(
            qid
            for qid, value in answers.items()
            if qid in self.questions_by_id and not self._is_empty(value)
        )
        total_answered = len(history)
        text_max = int(self.caps.get("max_text_questions_total", 999))
        total_max = int(self.caps.get("hard_max_questions", self.caps.get("total_max_questions", 999)))
        target_max = int(self.caps.get("target_questions_max", total_max))
        closing_reserve = min(2, len(self.flow_closing)) if self.flow_closing else 0
        module_max_map = self.caps.get("module_max_questions", {}) or {}
        max_followups = int(self.caps.get("max_followups_per_multi_choice", 0))

        text_count = sum(
            1
            for qid in history
            if self.questions_by_id.get(qid, {}).get("type") in ("text_short", "text_long")
        )
        context = {
            "answers": answers,
            "meta": meta or {},
            "text_slots_available": text_count < text_max,
            "module_text_slots_available": text_count < text_max,
            "answered_count": total_answered,
        }

        def can_ask(question: Dict[str, Any]) -> bool:
            qid = question.get("id")
            if not qid or qid in answered_ids:
                return False
            if question.get("skip_if") and self.evaluate_condition(question.get("skip_if"), context):
                return False
            dynamic_behavior = question.get("dynamic_behavior") or {}
            if dynamic_behavior.get("skip_if") and self.evaluate_condition(dynamic_behavior.get("skip_if"), context):
                return False
            if dynamic_behavior.get("show_if") and not self.evaluate_condition(dynamic_behavior.get("show_if"), context):
                return False
            if question.get("show_if") and not self.evaluate_condition(question.get("show_if"), context):
                return False
            is_required = qid in self.required_questions or question.get("required")
            qtype = question.get("type")
            section = question.get("section")
            if section not in ("core", "closing") and total_answered >= max(0, target_max - closing_reserve):
                return False
            if total_answered >= total_max and question.get("section") != "closing":
                return False
            if not is_required and total_answered >= total_max:
                return False
            if qtype in ("text_short", "text_long") and not is_required and text_count >= text_max:
                return False
            return True

        for qid in self.flow_core:
            question = self.questions_by_id.get(qid)
            if question and can_ask(question):
                return question

        followups = self._collect_followups(answers, history, context, max_followups)
        for qid in followups:
            question = self.questions_by_id.get(qid)
            if question and can_ask(question):
                return question

        module_order = module_order or self.select_modules(answers, meta)
        module_counts: Dict[str, int] = {}
        for qid in history:
            section = self.questions_by_id.get(qid, {}).get("section")
            if section:
                module_counts[section] = module_counts.get(section, 0) + 1

        for module_key in module_order:
            module_def = self.definition.get("modules", {}).get(module_key, {})
            module_limit = int(module_def.get("max_questions", 999))
            module_cap = module_max_map.get(module_key)
            if module_cap is not None:
                module_limit = min(module_limit, int(module_cap))
            if module_counts.get(module_key, 0) >= module_limit:
                continue

            for question in self.questions_by_section.get(module_key, []):
                if module_counts.get(module_key, 0) >= module_limit:
                    break
                if can_ask(question):
                    return question

        for qid in self.flow_closing:
            question = self.questions_by_id.get(qid)
            if question and can_ask(question):
                return question

        return None

    def _apply_auto_answers(self, answers: Dict[str, Any], history: List[str]) -> None:
        if "q035_primary_product_focus" in answers or "q035_primary_product_focus" in history:
            return
        question = self.questions_by_id.get("q035_primary_product_focus")
        if not question:
            return
        selected = answers.get("q030_product_interest")
        if not isinstance(selected, list) or len(selected) != 1:
            return
        entry_goal = answers.get("q010_entry_goal")
        if entry_goal in ("custom_personal", "custom_team_unit"):
            return
        dynamic_behavior = question.get("dynamic_behavior") or {}
        concrete = set(dynamic_behavior.get("concrete_product_values") or [])
        value = selected[0]
        if value in concrete:
            answers["q035_primary_product_focus"] = value
            answers[dynamic_behavior.get("store_auto_answer_flag") or "q035_auto_answered"] = True

    def _collect_followups(
        self,
        answers: Dict[str, Any],
        history: List[str],
        context: Dict[str, Any],
        max_followups: int,
    ) -> List[str]:
        followup_ids: List[str] = []
        for qid in history:
            question = self.questions_by_id.get(qid)
            if not question:
                continue
            items = question.get("followups") or []
            if not items:
                continue
            answer = answers.get(qid)
            for item in items:
                followup_id, condition = self._parse_followup(item, answer)
                if not followup_id:
                    continue
                if condition and not self.evaluate_condition(condition, context):
                    continue
                followup_ids.append(followup_id)
                if max_followups and len(followup_ids) >= max_followups:
                    return followup_ids
        return followup_ids

    def _parse_followup(
        self, item: Any, answer: Any
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        if isinstance(item, str):
            return item, None
        if not isinstance(item, dict):
            return None, None

        followup_id = item.get("question_id") or item.get("id") or item.get("followup_id")
        condition = item.get("show_if") or item.get("when") or item.get("condition")
        if "value" in item or "values" in item:
            expected = item.get("value") if "value" in item else item.get("values")
            if isinstance(answer, (list, tuple, set)):
                if isinstance(expected, (list, tuple, set)):
                    if not any(val in answer for val in expected):
                        return None, condition
                elif expected not in answer:
                    return None, condition
            elif answer != expected:
                return None, condition
        return followup_id, condition

    def validate_answer(self, question: Dict[str, Any], answer: Any) -> Tuple[bool, Any, str]:
        qtype = question.get("type")
        required = bool(question.get("required"))

        if self._is_empty(answer):
            if required:
                return False, None, "required"
            return True, None, "ok"

        if qtype in ("slider_1_10", "slider_0_10"):
            try:
                value = int(answer)
            except (TypeError, ValueError):
                return False, None, "invalid"
            min_value = 1 if qtype == "slider_1_10" else 0
            max_value = 10
            if value < min_value or value > max_value:
                return False, None, "range"
            return True, value, "ok"

        if qtype == "single_choice":
            options = {opt.get("value") for opt in question.get("options", [])}
            if answer not in options:
                return False, None, "invalid"
            return True, answer, "ok"

        if qtype in ("multi_choice", "multi_select"):
            if isinstance(answer, str):
                answers_list = [answer]
            else:
                answers_list = list(answer) if isinstance(answer, (list, tuple, set)) else []
            options = {opt.get("value") for opt in question.get("options", [])}
            answers_list = [item for item in answers_list if item in options]
            exclusive_values = set((question.get("selection_rules") or {}).get("exclusive_values") or [])
            selected_exclusive = [item for item in answers_list if item in exclusive_values]
            if selected_exclusive:
                answers_list = [selected_exclusive[0]]
            if required and not answers_list:
                return False, None, "required"
            max_select = question.get("max_select")
            if max_select and len(answers_list) > int(max_select):
                return False, None, "max_select"
            return True, answers_list, "ok"

        if qtype in ("text_short", "text_long"):
            text_value = str(answer).strip()
            if required and not text_value:
                return False, None, "required"
            limit = 280 if qtype == "text_short" else 1200
            if len(text_value) > limit:
                text_value = text_value[:limit]
            return True, text_value, "ok"

        if qtype == "maxdiff_best_worst":
            if not isinstance(answer, dict):
                return False, None, "invalid"
            options = {opt.get("value") for opt in question.get("options", [])}
            best = answer.get("best")
            worst = answer.get("worst")
            if best not in options or worst not in options:
                return False, None, "invalid"
            if best == worst:
                return False, None, "distinct_required"
            return True, {"best": best, "worst": worst}, "ok"

        if qtype in ("concept_reaction_cards", "tap_reaction_cards"):
            return self._validate_mapping_choice(
                answer,
                allowed_keys={item.get("value") for item in question.get("concepts", [])},
                allowed_values=set(question.get("scale_options") or []),
                required=required,
            )

        if qtype == "purchase_intent_matrix":
            return self._validate_mapping_choice(
                answer,
                allowed_keys={item.get("value") for item in question.get("rows", [])},
                allowed_values=set(question.get("columns") or []),
                required=required,
            )

        if qtype == "price_ladder_by_product":
            ranges = question.get("dynamic_matrix_ranges") or {}
            allowed = {
                option.get("value")
                for options in ranges.values()
                for option in options
                if isinstance(option, dict)
            }
            if answer not in allowed:
                return False, None, "invalid"
            return True, answer, "ok"

        if qtype == "budget_allocation_100":
            if not isinstance(answer, dict):
                return False, None, "invalid"
            allowed_keys = {item.get("value") for item in question.get("buckets", [])}
            cleaned: Dict[str, int] = {}
            for key, value in answer.items():
                if key not in allowed_keys:
                    continue
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    return False, None, "invalid"
                if number < 0 or number > 100:
                    return False, None, "range"
                if number:
                    cleaned[key] = number
            if required and not cleaned:
                return False, None, "required"
            if sum(cleaned.values()) != 100:
                return False, None, "total_must_be_100"
            return True, cleaned, "ok"

        if qtype == "contact_capture":
            if not isinstance(answer, dict):
                return False, None, "invalid"
            channel = str(answer.get("channel") or "").strip()
            value = str(answer.get("value") or "").strip()
            if channel not in set(question.get("channels") or []):
                return False, None, "invalid_channel"
            if not value:
                return False, None, "required"
            return True, {"channel": channel, "value": value[:160]}, "ok"

        if qtype == "info_card":
            return True, True if answer is True else None, "ok"

        return True, answer, "ok"

    def _validate_mapping_choice(
        self,
        answer: Any,
        *,
        allowed_keys: set,
        allowed_values: set,
        required: bool,
    ) -> Tuple[bool, Any, str]:
        if not isinstance(answer, dict):
            return False, None, "invalid"
        cleaned = {
            str(key): value
            for key, value in answer.items()
            if key in allowed_keys and value in allowed_values
        }
        if required and not cleaned:
            return False, None, "required"
        if not cleaned:
            return True, None, "ok"
        return True, cleaned, "ok"

    def format_answer(self, question: Dict[str, Any], answer: Any) -> str:
        if answer in (None, ""):
            return ""
        qtype = question.get("type")
        if qtype in ("single_choice",):
            return self._label_for_value(question, answer) or str(answer)
        if qtype in ("multi_choice", "multi_select"):
            if not isinstance(answer, (list, tuple, set)):
                return str(answer)
            labels = [self._label_for_value(question, item) or str(item) for item in answer]
            return ", ".join(labels)
        if qtype in ("slider_1_10", "slider_0_10"):
            return str(answer)
        if qtype == "maxdiff_best_worst" and isinstance(answer, dict):
            best = self._label_for_value(question, answer.get("best")) or answer.get("best", "")
            worst = self._label_for_value(question, answer.get("worst")) or answer.get("worst", "")
            return f"Найважливіше: {best}; найменш важливо: {worst}"
        if qtype in ("concept_reaction_cards", "tap_reaction_cards", "purchase_intent_matrix") and isinstance(answer, dict):
            return ", ".join(
                f"{self._label_for_value(question, key) or key}: {value}"
                for key, value in answer.items()
            )
        if qtype == "budget_allocation_100" and isinstance(answer, dict):
            return ", ".join(
                f"{self._label_for_value(question, key) or key}: {value}"
                for key, value in answer.items()
            )
        if qtype == "price_ladder_by_product":
            return self._label_for_value(question, answer) or str(answer)
        if qtype == "contact_capture" and isinstance(answer, dict):
            channel = answer.get("channel", "")
            value = answer.get("value", "")
            return f"{channel}: {value}".strip(": ")
        if isinstance(answer, dict):
            return ", ".join(f"{key}: {value}" for key, value in answer.items())
        return str(answer)

    def _label_for_value(self, question: Dict[str, Any], value: Any) -> Optional[str]:
        for key in ("options", "concepts", "rows", "buckets"):
            for option in question.get(key, []) or []:
                if option.get("value") == value:
                    return option.get("label_uk") or option.get("label")
        for options in (question.get("dynamic_matrix_ranges") or {}).values():
            for option in options or []:
                if option.get("value") == value:
                    return option.get("label_uk") or option.get("label")
        return None

    def _label_for_option(self, question: Dict[str, Any], value: Any) -> Optional[str]:
        return self._label_for_value(question, value)

    def _is_empty(self, value: Any) -> bool:
        return value in (None, "") or value == [] or value == {}

    def _localize_items(self, items: Any) -> Any:
        if not isinstance(items, list):
            return items
        localized = []
        for item in items:
            if isinstance(item, dict):
                localized.append(
                    {
                        **item,
                        "label": item.get("label_uk") or item.get("label") or item.get("value"),
                        "description": item.get("description_uk") or item.get("description"),
                    }
                )
            else:
                localized.append(item)
        return localized

    def _options_for_question(self, question: Dict[str, Any], answers: Dict[str, Any]) -> List[Dict[str, Any]]:
        options = question.get("options") or []
        if question.get("id") != "q035_primary_product_focus":
            return options

        selected = answers.get("q030_product_interest")
        if not isinstance(selected, list):
            return options

        dynamic_behavior = question.get("dynamic_behavior") or {}
        concrete_values = set(dynamic_behavior.get("concrete_product_values") or [])
        allowed = [value for value in selected if value in concrete_values and value != "not_sure"]
        context = {"answers": answers, "meta": {}}
        for item in dynamic_behavior.get("additional_options_if") or []:
            if self.evaluate_condition(item.get("if"), context):
                allowed.extend(item.get("include") or [])
        if "not_sure" in selected:
            allowed.extend(["not_sure", "custom_item"])
        if not allowed:
            allowed = ["tshirt", "hoodie", "custom_item", "not_sure"]

        allowed_set = set(allowed)
        filtered = [option for option in options if option.get("value") in allowed_set]
        return filtered or options

    def _price_ladder_payload(self, question: Dict[str, Any], answers: Dict[str, Any]) -> Dict[str, Any]:
        context_key, range_key = self._price_ladder_context(question, answers)
        ranges = question.get("dynamic_matrix_ranges") or {}
        range_options = ranges.get(range_key) or ranges.get("fallback") or []
        prompt = (
            (question.get("prompt_variants") or {}).get(context_key)
            or (question.get("prompt_variants") or {}).get("fallback")
            or question.get("prompt_uk")
        )
        return {
            "prompt": prompt,
            "price_context": context_key,
            "range_key": range_key,
            "ladder_points": (question.get("ladder_points_by_context") or {}).get(context_key, []),
            "options": range_options,
        }

    def _price_ladder_context(self, question: Dict[str, Any], answers: Dict[str, Any]) -> Tuple[str, str]:
        quantity = answers.get("q305_custom_quantity")
        entry_goal = answers.get("q010_entry_goal")
        stage = answers.get("q020_purchase_stage")
        if (
            (entry_goal == "custom_team_unit" or stage == "custom_request")
            and quantity in ("21_50", "50_plus")
        ):
            range_key = "team_unit_project_budget_fallback"
        elif entry_goal == "partner" or stage == "partner_discussion":
            range_key = "b2b_wholesale_fallback"
        elif entry_goal == "custom_team_unit" or stage == "custom_request":
            range_key = "team_unit_bulk_fallback"
        else:
            range_key = answers.get("q035_primary_product_focus") or "fallback"
            if range_key in (None, "", "not_sure"):
                range_key = "fallback"

        ranges = question.get("dynamic_matrix_ranges") or {}
        if range_key not in ranges:
            range_key = "fallback"
        context_key = (question.get("range_context_map") or {}).get(range_key, "retail_product")
        return context_key, range_key

    def validate_definition(self) -> List[str]:
        errors: List[str] = []
        for question in self.questions:
            if question.get("type") != "price_ladder_by_product":
                continue
            ranges = question.get("dynamic_matrix_ranges") or {}
            range_context_map = question.get("range_context_map") or {}
            ladder_points_by_context = question.get("ladder_points_by_context") or {}
            for item in question.get("routing_modifiers") or []:
                range_key = item.get("use_range_key")
                ladder_key = item.get("use_ladder_points")
                if range_key and range_key not in ranges:
                    errors.append(f"{question.get('id')}: missing range key {range_key}")
                if ladder_key and ladder_key not in ladder_points_by_context:
                    errors.append(f"{question.get('id')}: missing ladder context {ladder_key}")
            for range_key in ranges.keys():
                if range_key not in range_context_map:
                    errors.append(f"{question.get('id')}: missing context for range {range_key}")
            for range_key, context_key in range_context_map.items():
                if range_key in ranges and context_key not in ladder_points_by_context:
                    errors.append(f"{question.get('id')}: missing ladder points for context {context_key}")
        return errors


def award_survey_promocode(
    user,
    survey_key: str,
    reward_def: Dict[str, Any],
) -> Tuple[PromoCode, Optional[datetime]]:
    """Create or reuse a promo code for survey reward (idempotent)."""
    with transaction.atomic():
        existing = (
            UserPromoCode.objects.select_for_update()
            .select_related("promo_code")
            .filter(user=user, survey_key=survey_key)
            .first()
        )
        if existing and existing.promo_code:
            return existing.promo_code, existing.promo_code.valid_until

        promo_code = create_survey_promocode(survey_key, reward_def)

        try:
            UserPromoCode.objects.create(
                user=user,
                promo_code=promo_code,
                survey_key=survey_key,
                source="survey",
                expires_at=promo_code.valid_until,
            )
        except IntegrityError:
            existing = (
                UserPromoCode.objects.select_related("promo_code")
                .filter(user=user, survey_key=survey_key)
                .first()
            )
            if existing and existing.promo_code:
                promo_code.is_active = False
                promo_code.save(update_fields=["is_active"])
                return existing.promo_code, existing.promo_code.valid_until
            raise

    return promo_code, promo_code.valid_until


def award_anonymous_survey_promocode(
    survey_key: str,
    reward_def: Dict[str, Any],
) -> Tuple[PromoCode, Optional[datetime]]:
    """Create a one-use survey promo code for an anonymous browser session."""
    with transaction.atomic():
        promo_code = create_survey_promocode(survey_key, reward_def)
    return promo_code, promo_code.valid_until
