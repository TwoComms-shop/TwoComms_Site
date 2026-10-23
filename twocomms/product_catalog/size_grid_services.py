"""Structured, fit-specific size-grid resolution for Product Catalog."""

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

from productcolors.color_i18n import translate_color_name

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
FIT_LABEL_FALLBACKS = {
    "classic": {"ru": "Классическая", "en": "Classic"},
    "oversize": {"ru": "Оверсайз", "en": "Oversize"},
}

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
            profile = getattr(variant_assignment.size_grid, "product_catalog_profile", None)
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
        # keeps the legacy catalog default when no ProductCatalog assignment exists.
        if key == "fit=classic":
            catalog_id = getattr(product, "catalog_id", None)
            grids = SizeGrid.objects.filter(is_active=True)
            if catalog_id:
                grids = grids.filter(catalog_id=catalog_id)
            return (
                grids
                .exclude(product_catalog_profile__option_key=DEFAULT_OVERSIZE_OPTION_KEY)
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
    profile = getattr(assignment.size_grid, "product_catalog_profile", None)
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


def resolve_effective_sizes(product, option_key, variant=None, *, ignore_variant_stock=False) -> list[dict]:
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
                ignore_variant_stock or color_rule.stock is None or color_rule.stock > 0
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
    standard_fallback = FIT_LABEL_FALLBACKS.get(fit_code, {}).get(lang, "")
    if standard_fallback:
        localized = getattr(option, f"label_{lang}", "") if option is not None else ""
        # ProductFitOption currently stores one editor label. Keep an
        # optional locale-field hook for future localized model contracts,
        # but never require a schema field that production does not have.
        if _plain_text(localized):
            return _plain_text(localized)
        return standard_fallback
    if option is None:
        return fit_code.replace("-", " ").title()
    localized = getattr(option, f"label_{lang}", "")
    return _plain_text(localized) or option.label


def _guide_copy(product, fit_code: str, lang: str) -> dict[str, str]:
    from storefront.services.size_guides import detect_size_profile

    title = _plain_text(getattr(product, "title", ""))
    label = _fit_label(product, fit_code, lang)
    profile = detect_size_profile(product)
    if profile != "basic_tshirt":
        if lang == "ru":
            garment = "худи" if profile == "hoodie" else "изделия"
            return {"alt": f"Таблица размеров {garment} «{title}»",
                "caption": f"Размерная сетка {garment} «{title}»",
                "note": "Сравнивайте подтверждённые замеры с удобной вещью, учитывая название замера и единицы. Если замеров нет, напишите рост и вес — поможем с выбором."}
        if lang == "en":
            garment = "hoodie" if profile == "hoodie" else "garment"
            return {"alt": f"{garment.title()} size chart for {title}",
                "caption": f"{garment.title()} size guide for {title}",
                "note": "Compare confirmed measurements with a comfortable garment, using the same measurement and units. If measurements are unavailable, share your height and weight for help choosing a size."}
        garment = "худі" if profile == "hoodie" else "виробу"
        return {"alt": f"Таблиця розмірів {garment} «{title}»",
            "caption": f"Розмірна сітка {garment} «{title}»",
            "note": "Порівнюйте підтверджені заміри зі зручною річчю, враховуючи назву заміру та одиниці. Якщо замірів немає, напишіть зріст і вагу — допоможемо з вибором."}
    if lang == "ru":
        garment = {
            "classic": "классической футболки",
            "oversize": "оверсайз-футболки",
        }.get(fit_code, f"футболки с посадкой «{label}»")
        return {
            "alt": f"Таблица размеров {garment} «{title}»",
            "caption": f"Размерная сетка {garment} «{title}»",
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
    from storefront.services.size_guides import detect_size_profile
    from storefront.services.size_guide_authority import is_unverified_legacy_hoodie_seed

    decorated = deepcopy(guide or {})
    profile = detect_size_profile(product)
    legacy_seed = is_unverified_legacy_hoodie_seed(getattr(grid, "guide_data", {}) or {})
    localized = GUIDE_LOCALIZATION.get(lang)
    if localized and profile == "basic_tshirt":
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
    is_tshirt = profile == "basic_tshirt"
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
    disclosure = {
        "uk": "Для цього худі ще немає підтверджених числових замірів. Напишіть свій зріст і вагу — допоможемо з вибором.",
        "ru": "Для этого худи пока нет подтверждённых числовых замеров. Напишите свой рост и вес — поможем с выбором.",
        "en": "Confirmed numerical measurements for this hoodie are not available yet. Share your height and weight for help choosing a size.",
    }
    if legacy_seed:
        decorated.update(rows=[], columns=[], legend=[], notes=[], fit_notes=[],
            measurement_authority="legacy_seed_unverified", intro=disclosure[lang])
        copy["note"] = disclosure[lang]
    decorated.update(
        {
            "garment_profile": profile,
            "comparison_intro": copy["note"],
            "measurement_label": ({"uk": "Підбір розміру", "ru": "Подбор размера", "en": "Size help"} if legacy_seed
                else {"uk": "Розмірна сітка", "ru": "Размерная сетка", "en": "Size guide"})[lang],
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
    def prefetched(obj, relation):
        cache = getattr(obj, "_prefetched_objects_cache", {})
        return list(cache[relation]) if relation in cache else None

    fit_options = list(product.fit_options.all())
    fit_notes = {note.fit_code: note for note in product.product_catalog_fit_notes.all()}
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
    assignments = prefetched(product, "product_catalog_size_grid_assignments")
    if assignments is None:
        assignments = list(
            ProductOptionSizeGrid.objects
            .filter(product=product)
            .select_related("size_grid", "size_grid__product_catalog_profile")
            .order_by("id")
        )
    else:
        assignments.sort(key=lambda item: item.id)
    assignment_by_key = {item.option_key: item for item in assignments}
    cached_variant_assignments = [
        item
        for variant in variants
        for item in (prefetched(variant, "product_catalog_size_grid_assignments") or [])
    ]
    has_all_variant_caches = all(
        prefetched(variant, "product_catalog_size_grid_assignments") is not None
        for variant in variants
    )
    variant_assignments = cached_variant_assignments if has_all_variant_caches else list(
        VariantOptionSizeGrid.objects
        .filter(variant__in=variants)
        .select_related("size_grid", "size_grid__product_catalog_profile")
        .order_by("id")
    ) if variants else []
    variant_assignments.sort(key=lambda item: item.id)
    variant_assignment_map = {
        (item.variant_id, item.option_key): item
        for item in variant_assignments
    }
    product_size_rules = prefetched(product, "product_catalog_size_rules")
    if product_size_rules is None:
        product_size_rules = ProductSizeRule.objects.filter(product=product).order_by("id")
    product_rule_map = {
        (rule.option_key, normalize_size_value(rule.size)): rule
        for rule in product_size_rules
    }
    cached_variant_rules = all(
        prefetched(variant, "product_catalog_size_rules") is not None
        for variant in variants
    )
    variant_size_rules = [
        rule
        for variant in variants
        for rule in (prefetched(variant, "product_catalog_size_rules") or [])
    ] if cached_variant_rules else VariantSizeRule.objects.filter(
        variant__in=variants
    ).order_by("id") if variants else []
    variant_rule_map = {
        (rule.variant_id, rule.fit_code, normalize_size_value(rule.size)): rule
        for rule in variant_size_rules
    } if variants else {}
    guide_cache = {}

    catalog_size_values = []
    catalog = getattr(product, "catalog", None)
    if catalog is not None:
        cached_options = prefetched(catalog, "options")
        size_options = [
            option for option in (cached_options or [])
            if option.option_type == "size"
        ] if cached_options is not None else (
            catalog.options
            .filter(option_type="size")
            # Size-grid resolution never reads the option surcharge.
            .only("id", "catalog_id", "option_type", "order")
            .order_by("order", "id")
        )
        if cached_options is not None:
            size_options.sort(key=lambda option: (option.order, option.id))
        size_option = size_options[0] if size_options else None
        if size_option is not None:
            cached_values = prefetched(size_option, "values")
            values = cached_values if cached_values is not None else size_option.values.order_by("order", "id")
            catalog_size_values = [
                normalize_size_value(value.value)
                for value in values
                if normalize_size_value(value.value)
            ]

    def sellable_sizes(rows, *, explicit_variant_grid=False):
        row_sizes = [normalize_size_value(row.get("size")) for row in rows if row.get("size")]
        if catalog_size_values:
            return [size for size in catalog_size_values if size in row_sizes]
        if explicit_variant_grid:
            return [
                str(row.get("display_size") or row.get("size") or "").strip().upper()
                for row in rows
                if row.get("size")
            ]
        return [size for size in LEGACY_SELLABLE_SIZES if size in row_sizes]

    def usable_grid(grid):
        if grid is None or not grid.is_active:
            return None
        try:
            profile = grid.product_catalog_profile
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
        from storefront.services.size_guide_authority import is_unverified_legacy_hoodie_seed

        guide = normalized_guide(grid)
        if guide is None:
            return []
        fit_code = _fit_code(option_key)
        legacy_seed = is_unverified_legacy_hoodie_seed(grid.guide_data or {})
        rows = []
        for source_row in guide["rows"]:
            # Keep these sizes sellable, while the historical seed cells must
            # not leak back into the public availability payload as measurements.
            row = ({key: source_row[key] for key in ("size", "display_size") if key in source_row}
                if legacy_seed else deepcopy(source_row))
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
            if option_key in assignment_by_key or any(
                item.option_key == option_key for item in variant_assignments
            ) or resolve_option_size_grid(product, option_key) is not None:
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
                "color_name": translate_color_name(
                    getattr(getattr(variant, "color", None), "name", ""),
                    language,
                ),
                "grid_id": variant_grid.id if variant_grid is not None else None,
                "guide": variant_guide,
                "sizes": effective_rows(
                    variant_grid,
                    option_key,
                    variant,
                ) if variant_grid is not None else [],
            })
            variant_payloads[-1]["available_sizes"] = sellable_sizes(
                variant_payloads[-1]["sizes"],
                explicit_variant_grid=(variant.id, option_key) in variant_assignment_map,
            )
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
