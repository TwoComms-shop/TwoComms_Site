from copy import deepcopy

from django.utils.translation import gettext_lazy as _

from storefront.models import CatalogOption


DEFAULT_SIZE_SETS = {
    "default": ["S", "M", "L", "XL", "XXL"],
    "hoodie": ["XS", "S", "M", "L", "XL", "XXL"],
    "basic_tshirt": ["S", "M", "L", "XL", "XXL"],
}

SOURCE_LABELS = {
    "product_override": _("Індивідуальна сітка товару"),
    "catalog_default": _("Сітка каталогу"),
    "preset_detected": _("Підказка по категорії"),
    "fallback": _("Загальна підказка"),
}

SIZE_GUIDE_PRESETS = {
    "hoodie": {
        "aliases": [
            "hoodie",
            "hoodies",
            "худі",
            "худи",
            "fleece",
            "флис",
        ],
        "title": _("Худі"),
        "eyebrow": "Hoodie fit guide",
        "intro": _("Заміри зняті з виробу. Порівнюйте їх зі своїм худі, яке вже добре сидить по корпусу."),
        "columns": [
            {"key": "size", "label": _("Розмір")},
            {"key": "length", "label": _("Довжина")},
            {"key": "width", "label": _("Ширина")},
        ],
        "rows": [
            {"size": "XS", "display_size": "XS", "length": "54", "width": "52"},
            {"size": "S", "display_size": "S", "length": "58", "width": "56"},
            {"size": "M", "display_size": "M", "length": "67", "width": "60"},
            {"size": "L", "display_size": "L", "length": "70", "width": "64"},
            {"size": "XL", "display_size": "XL", "length": "73", "width": "68"},
            {"size": "XXL", "display_size": "XXL", "length": "78", "width": "72"},
        ],
        "legend": [
            {"label": "Length", "description": _("довжина по центру спини без урахування резинки")},
            {"label": "Width", "description": _("ширина виробу по корпусу")},
        ],
        "notes": [
            _("Можлива похибка вимірювання: ±2 см."),
            _("Якщо хочете більш вільну посадку, закладайте запас по ширині корпусу."),
        ],
        "fit_notes": [
            _("Порівняйте заміри зі своїм худі, яке вже подобається по посадці."),
            _("Для шару під низ або relaxed fit краще орієнтуватися насамперед на ширину."),
        ],
    },
    "basic_tshirt": {
        "aliases": [
            "t-shirt",
            "tshirt",
            "t shirts",
            "t-shirts",
            "tee",
            "tees",
            "футболка",
            "футболки",
            "базова футболка",
            "basic tee",
        ],
        "title": _("Футболка базова"),
        "eyebrow": "Basic tee fit guide",
        "intro": _("Для футболки дивіться не тільки на довжину, а й на ширину корпусу та плечі. Це дає найбільш точне співпадіння по посадці."),
        "columns": [
            {"key": "size", "label": _("Розмір")},
            {"key": "length", "label": _("Довжина (A)")},
            {"key": "width", "label": _("Ширина (B)")},
            {"key": "shoulders", "label": _("Плечі (C)")},
            {"key": "sleeve", "label": _("Рукав (D)")},
        ],
        "rows": [
            {"size": "S", "display_size": "S", "length": "68,5", "width": "49", "shoulders": "42", "sleeve": "23,5"},
            {"size": "M", "display_size": "M", "length": "70", "width": "51", "shoulders": "44", "sleeve": "24"},
            {"size": "L", "display_size": "L", "length": "72", "width": "52,5", "shoulders": "46", "sleeve": "25"},
            {"size": "XL", "display_size": "XL", "length": "74", "width": "54", "shoulders": "47,5", "sleeve": "25,5"},
            {"size": "XXL", "display_size": "2XL", "length": "76", "width": "58,5", "shoulders": "52", "sleeve": "26,5"},
        ],
        "legend": [
            {"label": "A", "description": _("довжина виробу")},
            {"label": "B", "description": _("ширина по грудях")},
            {"label": "C", "description": _("плечі")},
            {"label": "D", "description": _("довжина рукава")},
        ],
        "notes": [
            _("Заміри зняті з виробу, а не з параметрів тіла."),
            _("Порівнюйте таблицю зі своєю футболкою схожого крою."),
        ],
        "fit_notes": [
            _("Для стандартної посадки орієнтуйтесь на ширину й плечі."),
            _("Для relaxed fit обирайте розмір з додатковим запасом по ширині корпусу."),
        ],
    },
}


def _default_cta():
    return {
        "title": _("Потрібна допомога з розміром?"),
        "text": _("Перейдіть у повний size guide або напишіть нам перед оплатою, якщо вагаєтесь між двома розмірами."),
        "label": _("Розмірна сітка"),
        "url_name": "size_guide",
    }


def _normalize_size_value(value):
    normalized = str(value or "").strip().upper()
    aliases = {
        "2XL": "XXL",
        "XXL": "XXL",
        "X2L": "XXL",
    }
    return aliases.get(normalized, normalized)


def _serialize_image_payload(size_grid):
    if not size_grid or not getattr(size_grid, "image", None):
        return {"image_url": "", "image_alt": "", "image_width": 0, "image_height": 0}
    try:
        image_url = size_grid.image.url
    except Exception:
        image_url = ""
    image_alt = f"{size_grid.name} — таблиця розмірів" if image_url else ""
    # Phase 21 — surface intrinsic dimensions so the PDP template can
    # set <img width/height> and prevent layout shift (CLS) when the
    # size-guide image first paints.
    image_width = 0
    image_height = 0
    if image_url:
        try:
            image_width = int(getattr(size_grid.image, "width", 0) or 0)
            image_height = int(getattr(size_grid.image, "height", 0) or 0)
        except Exception:
            image_width = 0
            image_height = 0
    return {
        "image_url": image_url,
        "image_alt": image_alt,
        "image_width": image_width,
        "image_height": image_height,
    }


def detect_size_profile(product=None, raw=""):
    raw_candidate = str(raw or "").strip().lower()
    if raw_candidate:
        for profile_key, preset in SIZE_GUIDE_PRESETS.items():
            if any(alias in raw_candidate for alias in preset.get("aliases", [])):
                return profile_key

    candidates = []

    if product is not None:
        candidates.extend(
            [
                getattr(product, "title", ""),
                getattr(getattr(product, "category", None), "name", ""),
                getattr(getattr(product, "category", None), "slug", ""),
                getattr(getattr(product, "catalog", None), "name", ""),
                getattr(getattr(product, "catalog", None), "slug", ""),
                getattr(getattr(product, "size_grid", None), "name", ""),
                getattr(getattr(product, "size_grid", None), "description", ""),
            ]
        )

    normalized = " ".join(part.lower() for part in candidates if isinstance(part, str) and part.strip())
    for profile_key, preset in SIZE_GUIDE_PRESETS.items():
        if any(alias in normalized for alias in preset.get("aliases", [])):
            return profile_key
    return ""


def get_default_sizes_for_profile(profile_key):
    return list(DEFAULT_SIZE_SETS.get(profile_key or "", DEFAULT_SIZE_SETS["default"]))


def _resolve_catalog_size_option(product):
    if product is None or not getattr(product, "catalog_id", None):
        return None

    catalog = getattr(product, "catalog", None)
    if catalog is None:
        return None

    if hasattr(catalog, "_prefetched_objects_cache") and "options" in catalog._prefetched_objects_cache:
        options = list(catalog._prefetched_objects_cache["options"])
        size_options = [
            option
            for option in options
            if option.option_type == CatalogOption.OptionType.SIZE
        ]
        size_options.sort(key=lambda option: (option.order, option.id))
        if size_options:
            return size_options[0]

    return (
        catalog.options.filter(option_type=CatalogOption.OptionType.SIZE)
        .order_by("order", "id")
        .first()
    )


def _catalog_option_values(option):
    if option is None:
        return []
    if hasattr(option, "_prefetched_objects_cache") and "values" in option._prefetched_objects_cache:
        values = list(option._prefetched_objects_cache["values"])
        values.sort(key=lambda item: (item.order, item.id))
        return values
    return list(option.values.order_by("order", "id"))


def _ordered_size_values_from_catalog(product):
    option = _resolve_catalog_size_option(product)
    resolved = []
    for item in _catalog_option_values(option):
        normalized = _normalize_size_value(item.value)
        if normalized and normalized not in resolved:
            resolved.append(normalized)
    return resolved


def _catalog_display_labels(product):
    option = _resolve_catalog_size_option(product)
    labels = {}
    for item in _catalog_option_values(option):
        normalized = _normalize_size_value(item.value)
        if normalized and normalized not in labels:
            labels[normalized] = (item.display_name or item.value or normalized).strip()
    return labels


def _resolve_size_grid_source(product):
    if product is None:
        return None, "fallback"

    if getattr(product, "size_grid_id", None) and getattr(product, "size_grid", None) is not None:
        return product.size_grid, "product_override"

    catalog = getattr(product, "catalog", None)
    if not getattr(product, "catalog_id", None) or catalog is None:
        return None, "fallback"

    if hasattr(catalog, "_prefetched_objects_cache") and "size_grids" in catalog._prefetched_objects_cache:
        grids = [grid for grid in catalog._prefetched_objects_cache["size_grids"] if grid.is_active]
        grids.sort(key=lambda grid: (grid.order, grid.name))
        return (grids[0], "catalog_default") if grids else (None, "fallback")

    size_grid = catalog.size_grids.filter(is_active=True).order_by("order", "name").first()
    return (size_grid, "catalog_default") if size_grid else (None, "fallback")


def _normalize_columns(columns):
    normalized = []
    for index, column in enumerate(columns or []):
        if isinstance(column, dict):
            key = column.get("key") or f"col_{index}"
            label = column.get("label") or key
        else:
            key = f"col_{index}"
            label = str(column)
        normalized.append({"key": key, "label": label})
    return normalized


def _normalize_rows(rows, columns):
    normalized_rows = []
    column_keys = [column["key"] for column in columns]
    for raw_row in rows or []:
        row = dict(raw_row)
        row["size"] = _normalize_size_value(row.get("size"))
        row["display_size"] = (row.get("display_size") or row.get("size") or "").strip()
        for key in column_keys:
            if key not in row:
                row[key] = ""
        normalized_rows.append(row)
    return normalized_rows


def _guide_display_labels(guide):
    labels = {}
    for row in guide.get("rows", []):
        size = row.get("size")
        if size:
            labels[size] = row.get("display_size") or size
    return labels


def _size_values_from_guide(guide):
    values = []
    for row in guide.get("rows", []):
        size = row.get("size")
        if size and size not in values:
            values.append(size)
    return values


def _build_structured_guide(profile_key, source, size_grid=None, guide_data=None):
    payload = deepcopy(SIZE_GUIDE_PRESETS.get(profile_key, {}))
    if guide_data:
        payload.update({key: value for key, value in guide_data.items() if value not in (None, "", [], {})})

    columns = _normalize_columns(payload.get("columns"))
    rows = _normalize_rows(payload.get("rows"), columns)

    notes = list(payload.get("notes", []))
    description = (getattr(size_grid, "description", "") or "").strip()
    if description and description not in notes and description != payload.get("intro", "").strip():
        notes.insert(0, description)

    image_payload = _serialize_image_payload(size_grid)
    sizes = _size_values_from_guide({"rows": rows}) or get_default_sizes_for_profile(profile_key)

    guide = {
        "profile_key": profile_key,
        "guide_key": profile_key,
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "eyebrow": payload.get("eyebrow") or SOURCE_LABELS[source],
        "title": payload.get("title") or _("Розмірна сітка"),
        "intro": payload.get("intro", ""),
        "columns": columns,
        "rows": rows,
        "legend": list(payload.get("legend", [])),
        "notes": notes,
        "fit_notes": list(payload.get("fit_notes", [])),
        "cta": deepcopy(payload.get("cta") or _default_cta()),
        "size_grid": size_grid,
        "sizes": sizes,
        **image_payload,
    }
    return guide


def _build_visual_fallback(size_grid, source):
    image_payload = _serialize_image_payload(size_grid)
    description = (getattr(size_grid, "description", "") or "").strip()
    return {
        "profile_key": "",
        "guide_key": "",
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "eyebrow": SOURCE_LABELS[source],
        "title": getattr(size_grid, "name", _("Розмірна сітка")),
        "intro": description or _("Для цього товару доступна лише візуальна схема. Якщо сумніваєтесь, напишіть нам перед оплатою."),
        "columns": [],
        "rows": [],
        "legend": [],
        "notes": [_("Перейдіть у повний size guide або зверніться в підтримку, якщо потрібна підказка по посадці.")],
        "fit_notes": [],
        "cta": _default_cta(),
        "size_grid": size_grid,
        "sizes": DEFAULT_SIZE_SETS["default"],
        **image_payload,
    }


def _build_global_fallback(product=None):
    profile_key = detect_size_profile(product)
    sizes = get_default_sizes_for_profile(profile_key)
    return {
        "profile_key": profile_key,
        "guide_key": profile_key,
        "source": "fallback",
        "source_label": SOURCE_LABELS["fallback"],
        "eyebrow": "Fit guide",
        "title": _("Підбір розміру"),
        "intro": _("Якщо для товару ще немає окремої сітки, звіряйтеся зі своєю річчю схожого типу або напишіть нам перед оплатою."),
        "columns": [],
        "rows": [],
        "legend": [],
        "notes": [
            _("Порівнюйте ширину, довжину та посадку зі своєю річчю."),
            _("Для нестандартного фіту краще уточнити розмір у підтримки."),
        ],
        "fit_notes": [],
        "cta": _default_cta(),
        "size_grid": None,
        "sizes": sizes,
        "image_url": "",
        "image_alt": "",
    }


def resolve_product_size_guide(product):
    size_grid, source = _resolve_size_grid_source(product)
    guide_data = deepcopy(getattr(size_grid, "guide_data", {}) or {})

    raw_profile = " ".join(
        filter(
            None,
            [
                guide_data.get("profile_key", ""),
                getattr(size_grid, "name", "") if size_grid else "",
                getattr(size_grid, "description", "") if size_grid else "",
            ],
        )
    )
    profile_key = detect_size_profile(product, raw=raw_profile)

    if guide_data and (guide_data.get("columns") or guide_data.get("rows")):
        return _build_structured_guide(profile_key or detect_size_profile(product), source, size_grid=size_grid, guide_data=guide_data)

    if size_grid and profile_key:
        return _build_structured_guide(profile_key, source, size_grid=size_grid)

    if size_grid:
        return _build_visual_fallback(size_grid, source)

    return _build_global_fallback(product)


def resolve_product_sizes(product=None):
    catalog_sizes = _ordered_size_values_from_catalog(product)
    if catalog_sizes:
        return catalog_sizes

    guide = resolve_product_size_guide(product) if product is not None else None
    if guide:
        guide_sizes = _size_values_from_guide(guide)
        if guide_sizes:
            return guide_sizes
        if guide.get("sizes"):
            return list(guide["sizes"])

    profile_key = detect_size_profile(product) if product is not None else ""
    return get_default_sizes_for_profile(profile_key)


def get_size_display_labels(product=None, guide=None):
    labels = {size: size for size in resolve_product_sizes(product)}
    labels.update(_catalog_display_labels(product))
    if guide:
        labels.update(_guide_display_labels(guide))
    return labels


def normalize_requested_size(product, requested_size):
    sizes = resolve_product_sizes(product)
    normalized = _normalize_size_value(requested_size)
    if normalized in sizes:
        return normalized
    return sizes[0] if sizes else ""


def resolve_product_size_context(product, requested_size=None):
    fable_assignments = getattr(product, "fable5_size_grid_assignments", None)
    if fable_assignments is not None and fable_assignments.exists():
        # Import lazily: storefront remains usable when the optional Fable app
        # is not part of a deployment, and legacy products keep their exact
        # resolver path below.
        from fable5.size_grid_services import build_size_grid_comparison

        comparisons = build_size_grid_comparison(product)
        if comparisons:
            default_fit = (
                product.fit_options
                .filter(is_active=True, is_default=True)
                .order_by("order", "id")
                .first()
            )
            default_key = f"fit={default_fit.code}" if default_fit else ""
            selected_comparison = next(
                (item for item in comparisons if item["option_key"] == default_key),
                comparisons[0],
            )
            sizes = list(selected_comparison.get("available_sizes") or [])
            selected_size = _normalize_size_value(requested_size)
            if selected_size not in sizes:
                selected_size = sizes[0] if sizes else ""
            size_grid = selected_comparison["guide"]
            display_labels = {
                row["size"]: row.get("display_size") or row["size"]
                for row in selected_comparison["sizes"]
                if row["size"] in sizes
            }
            guide = _build_structured_guide(
                size_grid.get("profile_key") or detect_size_profile(product),
                "product_override",
                size_grid=selected_comparison["guide"].get("size_grid"),
                guide_data=size_grid,
            )
            return {
                "sizes": sizes,
                "selected_size": selected_size,
                "display_labels": display_labels,
                "guide": guide,
                "profile": {
                    "guide_key": guide.get("guide_key"),
                    "profile_key": guide.get("profile_key"),
                    "source": guide.get("source"),
                },
                "size_help_cta": deepcopy(guide.get("cta") or _default_cta()),
                "size_grid_comparison": comparisons,
                "fable5_size_grids": True,
            }

    guide = resolve_product_size_guide(product)
    sizes = _ordered_size_values_from_catalog(product) or _size_values_from_guide(guide) or list(guide.get("sizes", [])) or get_default_sizes_for_profile(guide.get("profile_key"))
    selected_size = normalize_requested_size(product, requested_size)

    if selected_size not in sizes and sizes:
        selected_size = sizes[0]

    return {
        "sizes": sizes,
        "selected_size": selected_size,
        "display_labels": get_size_display_labels(product, guide=guide),
        "guide": guide,
        "profile": {
            "guide_key": guide.get("guide_key"),
            "profile_key": guide.get("profile_key"),
            "source": guide.get("source"),
        },
        "size_help_cta": deepcopy(guide.get("cta") or _default_cta()),
    }


def build_public_size_guide_blocks(size_grids=None):
    size_grids = list(size_grids) if size_grids is not None else []
    matched = {}
    extras = []

    for grid in size_grids:
        profile_key = detect_size_profile(raw=" ".join(filter(None, [grid.name, grid.description, getattr(getattr(grid, "catalog", None), "name", ""), getattr(getattr(grid, "catalog", None), "slug", "")])))
        if profile_key and profile_key not in matched:
            matched[profile_key] = grid
            continue

        image_payload = _serialize_image_payload(grid)
        extras.append(
            {
                "title": grid.name,
                "intro": (grid.description or "").strip() or _("Візуальна схема для окремої категорії."),
                "image_url": image_payload["image_url"],
                "image_alt": image_payload["image_alt"],
                "catalog_name": getattr(getattr(grid, "catalog", None), "name", ""),
            }
        )

    blocks = []
    for profile_key in ("hoodie", "basic_tshirt"):
        if profile_key not in matched:
            continue
        block = _build_structured_guide(
            profile_key,
            "catalog_default",
            size_grid=matched[profile_key],
        )
        blocks.append(block)

    return {
        "blocks": blocks,
        "extra_visuals": [item for item in extras if item.get("image_url")],
    }
