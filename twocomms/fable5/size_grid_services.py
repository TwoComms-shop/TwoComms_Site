"""Structured, fit-specific size-grid resolution for Fable 5."""

from __future__ import annotations

import html
import re
from copy import deepcopy
from typing import Any, Iterable

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.templatetags.static import static
from django.utils.html import strip_tags

from .content_resolution import build_combination_key, normalize_option_values
from .models import (
    ProductOptionSizeGrid,
    ProductSizeRule,
    SizeGridProfile,
    VariantOptionSizeGrid,
    VariantSizeRule,
)
from storefront.models import SizeGrid


CELL_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")
SIZE_ALIASES = {
    "2XL": "XXL",
    "XXL": "XXL",
    "X2L": "XXL",
    "3XL": "XXXL",
    "XXXL": "XXXL",
    "X3L": "XXXL",
}
TEXT_LIST_FIELDS = ("notes", "fit_notes")
DEFAULT_OVERSIZE_OPTION_KEY = "fit=oversize"
DEFAULT_OVERSIZE_STATIC_PATH = "img/size-guides/oversize-tshirt.webp"
DEFAULT_OVERSIZE_AVIF_PATH = "img/size-guides/oversize-tshirt.avif"
DEFAULT_CLASSIC_STATIC_PATH = "img/size-guides/classic-tshirt.webp"
LEGACY_SELLABLE_SIZES = ("XS", "S", "M", "L", "XL", "XXL")

GUIDE_LOCALIZATION = {
    "ru": {
        "classic_title": "Таблица размеров классической футболки",
        "oversize_title": "Таблица размеров оверсайз-футболки",
        "classic_intro": "Фактические замеры классической футболки в разложенном виде. Допустимая погрешность ±1–2 см.",
        "oversize_intro": "Фактические замеры оверсайз-футболки в разложенном виде. Допустимая погрешность ±1–2 см.",
        "columns": {
            "size": "Размер",
            "chest": "Обхват груди",
            "garment_length": "Длина изделия",
            "shoulder_length": "Длина плеча",
            "sleeve_length": "Длина рукава",
            "shoulder_width": "Ширина плеч",
            "width": "Ширина",
        },
    },
    "en": {
        "classic_title": "Classic T-shirt size chart",
        "oversize_title": "Oversize T-shirt size chart",
        "classic_intro": "Actual measurements of the classic T-shirt laid flat. Allow for a ±1–2 cm measuring tolerance.",
        "oversize_intro": "Actual measurements of the oversize T-shirt laid flat. Allow for a ±1–2 cm measuring tolerance.",
        "columns": {
            "size": "Size",
            "chest": "Chest circumference",
            "garment_length": "Garment length",
            "shoulder_length": "Shoulder length",
            "sleeve_length": "Sleeve length",
            "shoulder_width": "Shoulder width",
            "width": "Width",
        },
    },
}


def _plain_text(value: Any) -> str:
    return html.unescape(strip_tags(str(value or ""))).strip()


def _static_asset_url(path: str) -> str:
    """Resolve a manifest URL lazily without breaking startup before collectstatic."""

    try:
        return static(path)
    except (ValueError, OSError):
        return f"{settings.STATIC_URL.rstrip('/')}/{path}"


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


def resolve_option_size_grid(product, option_key: str | dict, variant=None):
    key = normalize_option_key(option_key)
    if variant is not None and getattr(variant, "product_id", None) == product.id:
        variant_assignment = (
            VariantOptionSizeGrid.objects
            .filter(variant=variant, option_key=key, size_grid__is_active=True)
            .select_related("size_grid")
            .first()
        )
        if variant_assignment is not None:
            profile = getattr(variant_assignment.size_grid, "fable5_profile", None)
            if profile is None or profile.is_active:
                return variant_assignment.size_grid
    assignment = (
        ProductOptionSizeGrid.objects
        .filter(product=product, option_key=key, size_grid__is_active=True)
        .select_related("size_grid")
        .first()
    )
    if assignment is None:
        # A canonical oversize profile is a read-only default for new products.
        # Explicit product and variant assignments above always win. Classic
        # keeps the legacy catalog default when no Fable5 assignment exists.
        if key == "fit=classic":
            catalog_id = getattr(product, "catalog_id", None)
            grids = SizeGrid.objects.filter(is_active=True)
            if catalog_id:
                grids = grids.filter(catalog_id=catalog_id)
            return (
                grids
                .exclude(fable5_profile__option_key=DEFAULT_OVERSIZE_OPTION_KEY)
                .order_by("order", "name", "id")
                .first()
            )
        if key != DEFAULT_OVERSIZE_OPTION_KEY:
            return None
        catalog_id = getattr(product, "catalog_id", None)
        profiles = (
            SizeGridProfile.objects
            .filter(
                option_key=key,
                is_active=True,
                size_grid__is_active=True,
            )
            .select_related("size_grid")
            .order_by("size_grid__order", "size_grid_id")
        )
        if catalog_id:
            profiles = profiles.filter(size_grid__catalog_id=catalog_id)
        profile = (
            profiles.filter(
                Q(size_grid__catalog__name__icontains="футбол")
                | Q(size_grid__catalog__slug__icontains="shirt")
                | Q(size_grid__catalog__slug__icontains="tshirt")
            ).first()
            or profiles.first()
        )
        return profile.size_grid if profile is not None else None
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
    size_grid = resolve_option_size_grid(product, key, variant=variant)
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


def _guide_copy(product, fit_code: str, lang: str) -> dict[str, str]:
    title = _plain_text(getattr(product, "title", ""))
    label = _fit_label(product, fit_code, lang)
    if lang == "ru":
        return {
            "alt": f"Таблица размеров {label.lower()} футболки «{title}»",
            "caption": f"Размерная сетка {label.lower()} для {title}",
            "note": (
                "Снимайте мерки с разложенной футболки и сравнивайте их с таблицей. "
                "Классическая таблица охватывает S–3XL, оверсайз — XS–2XL."
            ),
        }
    if lang == "en":
        return {
            "alt": f"{label} T-shirt size chart for {title}",
            "caption": f"{label} size guide for {title}",
            "note": (
                "Measure a laid-flat T-shirt and compare it with the chart. "
                "The classic chart covers S–3XL; oversize covers XS–2XL."
            ),
        }
    return {
        "alt": f"Таблиця розмірів {label.lower()} футболки «{title}»",
        "caption": f"Розмірна сітка {label.lower()} для {title}",
        "note": (
            "Знімайте мірки з розкладеної футболки та порівнюйте їх із таблицею. "
            "Класична таблиця охоплює S–3XL, оверсайз — XS–2XL."
        ),
    }


def _decorate_guide(product, grid, guide: dict | None, fit_code: str, lang: str) -> dict:
    decorated = deepcopy(guide or {})
    localized = GUIDE_LOCALIZATION.get(lang)
    if localized:
        decorated["title"] = localized.get(f"{fit_code}_title", decorated.get("title", ""))
        decorated["intro"] = localized.get(f"{fit_code}_intro", decorated.get("intro", ""))
        decorated["columns"] = [
            {
                **column,
                "label": localized["columns"].get(column.get("key"), column.get("label", "")),
            }
            for column in decorated.get("columns", [])
        ]
    image_url = ""
    image_width = 0
    image_height = 0
    image = getattr(grid, "image", None) if grid is not None else None
    if image:
        try:
            image_url = image.url
            image_width = int(getattr(image, "width", 0) or 0)
            image_height = int(getattr(image, "height", 0) or 0)
        except (OSError, ValueError):
            image_url = ""
    from storefront.services.size_guides import detect_size_profile

    is_tshirt = detect_size_profile(product) == "basic_tshirt"
    used_static_oversize = not image_url and is_tshirt and fit_code == "oversize"
    used_static_classic = not image_url and is_tshirt and fit_code == "classic"
    if used_static_oversize:
        image_url = _static_asset_url(DEFAULT_OVERSIZE_STATIC_PATH)
        image_width = 2400
        image_height = 1800
    elif used_static_classic:
        image_url = _static_asset_url(DEFAULT_CLASSIC_STATIC_PATH)
        image_width = 993
        image_height = 292
    copy = _guide_copy(product, fit_code, lang)
    decorated.update(
        {
            "image_url": image_url,
            "image_width": image_width,
            "image_height": image_height,
            "image_avif_url": _static_asset_url(DEFAULT_OVERSIZE_AVIF_PATH) if used_static_oversize else "",
            "image_alt": copy["alt"],
            "image_caption": copy["caption"],
            "fit_explanation": copy["note"],
        }
    )
    return decorated


def build_size_grid_comparison(product, variants=None, lang: str = "uk") -> list[dict]:
    """Build all assigned fit grids so the PDP can compare them at once."""

    language = lang if lang in {"uk", "ru", "en"} else "uk"
    variants = list(variants or [])
    fit_options = list(product.fit_options.all())
    fit_notes = {note.fit_code: note for note in product.fable5_fit_notes.all()}
    active_fit_codes = {
        item.code
        for item in fit_options
        if item.is_active and (
            fit_notes.get(item.code) is None
            or fit_notes[item.code].is_enabled
        )
    }
    fit_order = {
        item.code: (item.order, item.id)
        for item in fit_options
    }
    assignments = list(
        ProductOptionSizeGrid.objects
        .filter(product=product)
        .select_related("size_grid", "size_grid__fable5_profile")
        .order_by("id")
    )
    assignment_by_key = {item.option_key: item for item in assignments}
    variant_assignments = list(
        VariantOptionSizeGrid.objects
        .filter(variant__in=variants)
        .select_related("size_grid", "size_grid__fable5_profile")
        .order_by("id")
    ) if variants else []
    variant_assignment_map = {
        (item.variant_id, item.option_key): item
        for item in variant_assignments
    }
    product_rule_map = {
        (rule.option_key, normalize_size_value(rule.size)): rule
        for rule in ProductSizeRule.objects.filter(product=product).order_by("id")
    }
    variant_rule_map = {
        (rule.variant_id, rule.fit_code, normalize_size_value(rule.size)): rule
        for rule in VariantSizeRule.objects.filter(variant__in=variants).order_by("id")
    } if variants else {}
    guide_cache = {}

    catalog_size_values = []
    catalog = getattr(product, "catalog", None)
    if catalog is not None:
        size_options = catalog.options.filter(option_type="size").order_by("order", "id")
        size_option = size_options.first()
        if size_option is not None:
            catalog_size_values = [
                normalize_size_value(value)
                for value in size_option.values.order_by("order", "id").values_list("value", flat=True)
                if normalize_size_value(value)
            ]

    def sellable_sizes(rows):
        row_sizes = [normalize_size_value(row.get("size")) for row in rows if row.get("size")]
        allowed_sizes = catalog_size_values or LEGACY_SELLABLE_SIZES
        return [size for size in allowed_sizes if size in row_sizes]

    def usable_grid(grid):
        if grid is None or not grid.is_active:
            return None
        try:
            profile = grid.fable5_profile
        except Exception:
            profile = None
        return grid if profile is None or profile.is_active else None

    def grid_for(option_key, variant=None):
        if variant is not None:
            override = variant_assignment_map.get((variant.id, option_key))
            override_grid = usable_grid(override.size_grid if override else None)
            if override_grid is not None:
                return override_grid
        shared = assignment_by_key.get(option_key)
        if shared is not None:
            return usable_grid(shared.size_grid)
        return usable_grid(resolve_option_size_grid(product, option_key))

    def normalized_guide(grid):
        if grid.id not in guide_cache:
            try:
                guide_cache[grid.id] = normalize_size_grid_payload(
                    deepcopy(grid.guide_data or {})
                )
            except ValidationError:
                guide_cache[grid.id] = None
        return guide_cache[grid.id]

    def effective_rows(grid, option_key, variant=None):
        guide = normalized_guide(grid)
        if guide is None:
            return []
        fit_code = _fit_code(option_key)
        rows = []
        for source_row in guide["rows"]:
            row = deepcopy(source_row)
            size = normalize_size_value(row.get("size"))
            product_rule = product_rule_map.get((option_key, size))
            enabled = product_rule.is_enabled if product_rule is not None else True
            if variant is not None:
                color_rule = (
                    variant_rule_map.get((variant.id, fit_code, size))
                    or variant_rule_map.get((variant.id, "", size))
                )
                if color_rule is not None:
                    enabled = color_rule.is_enabled and (
                        color_rule.stock is None or color_rule.stock > 0
                    )
            if enabled:
                row["is_enabled"] = True
                rows.append(row)
        return rows
    option_keys = set(assignment_by_key)
    option_keys.update(item.option_key for item in variant_assignments)
    for fit in fit_options:
        if fit.is_active:
            option_key = f"fit={fit.code}"
            if resolve_option_size_grid(product, option_key) is not None:
                option_keys.add(option_key)
    ordered_keys = sorted(
        option_keys,
        key=lambda key: (*fit_order.get(_fit_code(key), (10_000, 10_000)), key),
    )

    comparison = []
    for option_key in ordered_keys:
        fit_code = _fit_code(option_key)
        if fit_options and fit_code and fit_code not in active_fit_codes:
            continue
        assignment = assignment_by_key.get(option_key)
        size_grid = grid_for(option_key)
        if size_grid is None:
            for variant in variants:
                size_grid = grid_for(option_key, variant)
                if size_grid is not None:
                    break
        if size_grid is None:
            continue
        try:
            guide = _decorate_guide(
                product,
                size_grid,
                normalized_guide(size_grid),
                fit_code,
                language,
            )
        except Exception:
            guide = None
        if guide is None:
            continue
        base_sizes = effective_rows(size_grid, option_key)
        if not base_sizes and assignment is None:
            for variant in variants:
                variant_grid = grid_for(option_key, variant)
                base_sizes = effective_rows(
                    variant_grid,
                    option_key,
                    variant,
                ) if variant_grid is not None else []
                if base_sizes:
                    break
        variant_payloads = []
        for variant in variants:
            variant_grid = grid_for(option_key, variant)
            variant_guide = (
                _decorate_guide(
                    product,
                    variant_grid,
                    normalized_guide(variant_grid),
                    fit_code,
                    language,
                )
                if variant_grid is not None
                else guide
            )
            if variant_guide is None:
                variant_guide = guide
            variant_payloads.append({
                "variant_id": variant.id,
                "color_id": variant.color_id,
                "color_name": getattr(getattr(variant, "color", None), "name", ""),
                "grid_id": variant_grid.id if variant_grid is not None else None,
                "guide": variant_guide,
                "sizes": effective_rows(
                    variant_grid,
                    option_key,
                    variant,
                ) if variant_grid is not None else [],
            })
            variant_payloads[-1]["available_sizes"] = sellable_sizes(variant_payloads[-1]["sizes"])
        comparison.append(
            {
                "option_key": option_key,
                "option_values": normalize_option_values(
                    dict(part.split("=", 1) for part in option_key.split(";"))
                ),
                "fit_code": fit_code,
                "label": _fit_label(product, fit_code, language),
                "grid_id": size_grid.id,
                "grid_name": size_grid.name,
                "guide": guide,
                "sizes": base_sizes,
                "available_sizes": sellable_sizes(base_sizes),
                "variants": variant_payloads,
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
