"""Structured, fit-specific size-grid resolution for Fable 5."""

from __future__ import annotations

import html
import re
from copy import deepcopy
from typing import Any, Iterable

from django.core.exceptions import ValidationError
from django.utils.html import strip_tags

from .content_resolution import build_combination_key, normalize_option_values
from .models import (
    ProductOptionSizeGrid,
    ProductSizeRule,
    VariantSizeRule,
)


CELL_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")
SIZE_ALIASES = {
    "2XL": "XXL",
    "XXL": "XXL",
    "X2L": "XXL",
}
TEXT_LIST_FIELDS = ("notes", "fit_notes")


def _plain_text(value: Any) -> str:
    return html.unescape(strip_tags(str(value or ""))).strip()


def _cell_key(value: Any) -> str:
    key = _plain_text(value).lower().replace(" ", "_")
    if not CELL_KEY_RE.fullmatch(key):
        raise ValidationError({"columns": f"Некоректний код колонки: {key or 'порожній'}"})
    return key


def normalize_size_value(value: Any) -> str:
    normalized = _plain_text(value).upper().replace(" ", "")
    return SIZE_ALIASES.get(normalized, normalized)


def normalize_option_key(value: str | dict | None) -> str:
    if isinstance(value, dict):
        return build_combination_key(value)
    raw = str(value or "").strip()
    if not raw:
        raise ValidationError({"option_key": "Оберіть посадку або комбінацію опцій."})
    pairs = {}
    try:
        for part in raw.split(";"):
            key, option_value = part.split("=", 1)
            pairs[key] = option_value
        return build_combination_key(pairs)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"option_key": "Некоректний ключ опції."}) from exc


def normalize_size_grid_payload(payload: dict | None) -> dict:
    """Validate and convert editor input to the public ``guide_data`` format."""

    if not isinstance(payload, dict):
        raise ValidationError("Розмірна сітка має бути об'єктом.")

    raw_columns = payload.get("columns")
    raw_rows = payload.get("rows")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ValidationError({"columns": "Додайте хоча б колонку розміру."})
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValidationError({"rows": "Додайте хоча б один розмір."})

    columns = []
    column_keys = set()
    for raw_column in raw_columns:
        if not isinstance(raw_column, dict):
            raise ValidationError({"columns": "Кожна колонка має бути об'єктом."})
        key = _cell_key(raw_column.get("key"))
        if key in column_keys:
            raise ValidationError({"columns": f"Колонка {key} дублюється."})
        column_keys.add(key)
        columns.append(
            {
                "key": key,
                "label": _plain_text(raw_column.get("label")) or key,
            }
        )
    if "size" not in column_keys:
        raise ValidationError({"columns": "Сітка повинна містити колонку size."})

    rows = []
    seen_sizes = set()
    ordered_column_keys = [column["key"] for column in columns]
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            raise ValidationError({"rows": "Кожен рядок має бути об'єктом."})
        raw_size = _plain_text(raw_row.get("size"))
        size = normalize_size_value(raw_size)
        if not size:
            raise ValidationError({"rows": "Кожен рядок повинен мати розмір."})
        if size in seen_sizes:
            raise ValidationError({"rows": f"Розмір {size} дублюється."})
        seen_sizes.add(size)

        row = {
            "size": size,
            "display_size": _plain_text(raw_row.get("display_size")) or raw_size or size,
        }
        for key in ordered_column_keys:
            if key != "size":
                row[key] = _plain_text(raw_row.get(key))
        for raw_key, raw_value in raw_row.items():
            if raw_key in {"size", "display_size"} or raw_key in column_keys:
                continue
            extra_key = _cell_key(raw_key)
            if extra_key not in row:
                row[extra_key] = _plain_text(raw_value)
        rows.append(row)

    normalized = {
        "profile_key": _plain_text(payload.get("profile_key")),
        "title": _plain_text(payload.get("title")),
        "eyebrow": _plain_text(payload.get("eyebrow")),
        "intro": _plain_text(payload.get("intro")),
        "columns": columns,
        "rows": rows,
        "legend": [],
        "notes": [],
        "fit_notes": [],
    }
    raw_legend = payload.get("legend") or []
    if not isinstance(raw_legend, list):
        raise ValidationError({"legend": "Легенда має бути списком."})
    for item in raw_legend:
        if isinstance(item, dict):
            normalized["legend"].append(
                {
                    "label": _plain_text(item.get("label")),
                    "description": _plain_text(item.get("description")),
                }
            )
        else:
            normalized["legend"].append(
                {"label": "", "description": _plain_text(item)}
            )
    for field in TEXT_LIST_FIELDS:
        raw_items = payload.get(field) or []
        if not isinstance(raw_items, list):
            raise ValidationError({field: "Значення має бути списком."})
        normalized[field] = [text for item in raw_items if (text := _plain_text(item))]
    return normalized


def resolve_option_size_grid(product, option_key: str | dict):
    key = normalize_option_key(option_key)
    assignment = (
        ProductOptionSizeGrid.objects
        .filter(product=product, option_key=key, size_grid__is_active=True)
        .select_related("size_grid")
        .first()
    )
    if assignment is None:
        return None
    profile = getattr(assignment.size_grid, "fable5_profile", None)
    if profile is not None and not profile.is_active:
        return None
    return assignment.size_grid


def _fit_code(option_key: str) -> str:
    values = dict(part.split("=", 1) for part in option_key.split(";") if "=" in part)
    return values.get("fit", "")


def _rules_by_size(rules: Iterable[Any]) -> dict[str, Any]:
    result = {}
    for rule in rules:
        size = normalize_size_value(rule.size)
        if size:
            result[size] = rule
    return result


def resolve_effective_sizes(product, option_key, variant=None) -> list[dict]:
    """Return ordered, enabled rows after product and color/fit overrides."""

    key = normalize_option_key(option_key)
    size_grid = resolve_option_size_grid(product, key)
    if size_grid is None:
        return []
    try:
        guide = normalize_size_grid_payload(deepcopy(size_grid.guide_data or {}))
    except ValidationError:
        return []

    product_rules = _rules_by_size(
        ProductSizeRule.objects.filter(product=product, option_key=key).order_by("id")
    )
    variant_rules = {}
    if variant is not None:
        fit_code = _fit_code(key)
        general = VariantSizeRule.objects.filter(variant=variant, fit_code="").order_by("id")
        specific = VariantSizeRule.objects.filter(
            variant=variant,
            fit_code=fit_code,
        ).order_by("id")
        variant_rules.update(_rules_by_size(general))
        variant_rules.update(_rules_by_size(specific))

    resolved = []
    for source_row in guide["rows"]:
        row = deepcopy(source_row)
        rule = product_rules.get(row["size"])
        enabled = rule.is_enabled if rule is not None else True
        color_rule = variant_rules.get(row["size"])
        if color_rule is not None:
            enabled = color_rule.is_enabled and (
                color_rule.stock is None or color_rule.stock > 0
            )
        if enabled:
            row["is_enabled"] = True
            resolved.append(row)
    return resolved


def _fit_label(product, fit_code: str, lang: str) -> str:
    option = next(
        (item for item in product.fit_options.all() if item.code == fit_code),
        None,
    )
    if option is None:
        return fit_code.replace("-", " ").title()
    localized = getattr(option, f"label_{lang}", "")
    return _plain_text(localized) or option.label


def build_size_grid_comparison(product, variants=None, lang: str = "uk") -> list[dict]:
    """Build all assigned fit grids so the PDP can compare them at once."""

    language = lang if lang in {"uk", "ru", "en"} else "uk"
    variants = list(variants or [])
    fit_order = {
        item.code: (item.order, item.id)
        for item in product.fit_options.all()
    }
    assignments = list(
        ProductOptionSizeGrid.objects
        .filter(product=product)
        .select_related("size_grid")
        .order_by("id")
    )
    assignments.sort(
        key=lambda item: (*fit_order.get(_fit_code(item.option_key), (10_000, item.id)), item.id)
    )

    comparison = []
    for assignment in assignments:
        size_grid = resolve_option_size_grid(product, assignment.option_key)
        if size_grid is None:
            continue
        try:
            guide = normalize_size_grid_payload(deepcopy(size_grid.guide_data or {}))
        except ValidationError:
            continue
        fit_code = _fit_code(assignment.option_key)
        base_sizes = resolve_effective_sizes(product, assignment.option_key)
        comparison.append(
            {
                "option_key": assignment.option_key,
                "option_values": normalize_option_values(
                    dict(part.split("=", 1) for part in assignment.option_key.split(";"))
                ),
                "fit_code": fit_code,
                "label": _fit_label(product, fit_code, language),
                "grid_id": size_grid.id,
                "grid_name": size_grid.name,
                "guide": guide,
                "sizes": base_sizes,
                "variants": [
                    {
                        "variant_id": variant.id,
                        "color_id": variant.color_id,
                        "sizes": resolve_effective_sizes(
                            product,
                            assignment.option_key,
                            variant=variant,
                        ),
                    }
                    for variant in variants
                ],
            }
        )
    return comparison


__all__ = [
    "build_size_grid_comparison",
    "normalize_option_key",
    "normalize_size_grid_payload",
    "normalize_size_value",
    "resolve_effective_sizes",
    "resolve_option_size_grid",
]
