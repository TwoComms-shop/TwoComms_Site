"""Authoritative catalog pricing for Instagram prompt readers.

Orders already use ``product_catalog.services.effective_cart_unit_price``. This module
exposes the same variant/option truth to the model before it quotes a price.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from itertools import product as option_product

from django.db.models import prefetch_related_objects

from .ig_catalog_compatibility import resolve_configuration_size_contract


logger = logging.getLogger(__name__)
MAX_OPTION_COMBINATIONS = 128

PRODUCT_PRICING_LOOKUPS = (
    "catalog__options__values",
    "catalog__size_grids__product_catalog_profile",
    "category__product_catalog_flows",
    "fit_options",
    "product_catalog_fit_notes",
    "product_catalog_option_profiles__i18n",
    "product_catalog_axis_presentations",
    "product_catalog_size_grid_assignments__size_grid__product_catalog_profile",
    "product_catalog_size_rules",
    "size_grid__product_catalog_profile",
)
VARIANT_PRICING_LOOKUPS = (
    "color__product_catalog_profile",
    "product_catalog_details__i18n",
    "product_catalog_fit_rules",
    "product_catalog_size_rules",
    "product_catalog_faqs",
    "product_catalog_combinations__i18n",
    "product_catalog_size_grid_assignments__size_grid__product_catalog_profile",
)


def _money(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def money_text(value) -> str:
    amount = _money(value)
    if amount == amount.to_integral():
        return str(int(amount))
    return format(amount, "f")


def _variant_rows(product, variants=None):
    if variants is not None:
        return list(variants)
    prefetched = getattr(product, "_prefetched_objects_cache", {}).get("color_variants")
    if prefetched is not None:
        return list(prefetched)
    return list(
        product.color_variants.select_related("color").order_by("order", "id")
    )


def prepare_pricing_context(products, variants) -> None:
    """Batch-load the graph used by ProductCatalog pricing and content resolution."""

    product_rows = list(products)
    variant_rows = list(variants)
    product_by_id = {row.pk: row for row in product_rows}
    for variant in variant_rows:
        product = product_by_id.get(variant.product_id)
        if product is not None:
            variant.product = product
    if product_rows:
        prefetch_related_objects(product_rows, *PRODUCT_PRICING_LOOKUPS)
    if variant_rows:
        prefetch_related_objects(variant_rows, *VARIANT_PRICING_LOOKUPS)


def _variant_configurations(product, variant) -> list[dict]:
    from product_catalog.content_resolution import build_combination_key
    from product_catalog.services import (
        product_option_context,
        variant_public_context,
    )

    option_context = product_option_context(product, variant=variant)
    axes = option_context.get("axes") or []
    enabled_groups = [
        [choice for choice in (axis.get("choices") or []) if choice.get("is_enabled")]
        for axis in axes
    ]
    combinations = 1
    for group in enabled_groups:
        combinations *= len(group)
    if enabled_groups and (not all(enabled_groups) or combinations > MAX_OPTION_COMBINATIONS):
        logger.warning(
            "IG catalog option matrix unavailable for product=%s variant=%s combinations=%s",
            getattr(product, "pk", None),
            getattr(variant, "pk", None),
            combinations,
        )
        return []

    choices_iter = option_product(*enabled_groups) if enabled_groups else [()]
    rows = []
    combination_rows = {
        row.combination_key: row
        for row in variant.product_catalog_combinations.all()
    }
    for choices in choices_iter:
        values = {
            axis["code"]: choice["code"]
            for axis, choice in zip(axes, choices)
        }
        combination = combination_rows.get(build_combination_key(values))
        if combination is not None and not combination.is_active:
            continue
        resolved = variant_public_context(variant, option_values=values)
        price = _money(resolved.get("final_price"))
        if price <= 0:
            continue
        labels = {
            axis["code"]: str(choice.get("label") or choice.get("code") or "")
            for axis, choice in zip(axes, choices)
        }
        row = {
            "variant_id": variant.pk,
            "color_id": getattr(variant, "color_id", None),
            "color_slug": str(getattr(variant, "slug", "") or ""),
            "color": str(getattr(getattr(variant, "color", None), "name", "") or ""),
            "option_values": values,
            "option_labels": labels,
            "option_key": build_combination_key(values),
            "fit_code": str(values.get("fit") or ""),
            "price": price,
            "price_text": money_text(price),
            "price_reason": str(resolved.get("price_delta_reason") or "").strip(),
            "is_thermo": bool(resolved.get("is_thermo")),
            "stock": int(getattr(variant, "stock", 0) or 0),
        }
        compatible_sizes, has_size_contract = resolve_configuration_size_contract(
            product,
            variant,
            row,
        )
        row["compatible_sizes"] = compatible_sizes
        row["has_compatible_size_contract"] = has_size_contract
        rows.append(row)
    return rows


def _product_configurations_without_variants(product) -> list[dict]:
    """Build option pricing rows for products that have no colour variants."""
    from itertools import product as option_product

    from product_catalog.content_resolution import build_combination_key
    from product_catalog.services import effective_cart_unit_price, product_option_context

    option_context = product_option_context(product, variant=None)
    axes = option_context.get("axes") or []
    enabled_groups = [
        [choice for choice in (axis.get("choices") or []) if choice.get("is_enabled")]
        for axis in axes
    ]
    combinations = 1
    for group in enabled_groups:
        combinations *= len(group)
    if enabled_groups and (not all(enabled_groups) or combinations > MAX_OPTION_COMBINATIONS):
        logger.warning(
            "IG catalog option matrix unavailable for product=%s combinations=%s",
            getattr(product, "pk", None), combinations,
        )
        return []

    choices_iter = option_product(*enabled_groups) if enabled_groups else [()]
    rows = []
    for choices in choices_iter:
        values = {
            axis["code"]: choice["code"]
            for axis, choice in zip(axes, choices)
        }
        price = _money(effective_cart_unit_price(product, None, option_values=values))
        if price <= 0:
            continue
        rows.append({
            "variant_id": None,
            "color_id": None,
            "color_slug": "",
            "color": "",
            "option_values": values,
            "option_labels": {
                axis["code"]: str(choice.get("label") or choice.get("code") or "")
                for axis, choice in zip(axes, choices)
            },
            "option_key": build_combination_key(values),
            "fit_code": str(values.get("fit") or ""),
            "price": price,
            "price_text": money_text(price),
            "price_reason": "",
            "is_thermo": False,
        })
    return rows


def resolve_product_pricing(
    product,
    *,
    variants=None,
    selected_variant_id=None,
    option_values=None,
    context_prepared=False,
) -> dict:
    """Return exact/ranged prices for currently sellable product configurations."""

    variant_rows = _variant_rows(product, variants=variants)
    if not context_prepared:
        prepare_pricing_context([product], variant_rows)
    configurations = []
    for variant in variant_rows:
        if selected_variant_id and int(variant.pk) != int(selected_variant_id):
            continue
        configurations.extend(_variant_configurations(product, variant))

    selected_options = {
        str(key).strip().lower(): str(value).strip().lower()
        for key, value in (option_values or {}).items()
        if str(key).strip() and str(value).strip()
    }
    if selected_options:
        configurations = [
            row for row in configurations
            if all(row["option_values"].get(key) == value for key, value in selected_options.items())
        ]

    if not configurations and not variant_rows:
        configurations = _product_configurations_without_variants(product)
        if selected_options:
            configurations = [
                row for row in configurations
                if all(
                    row["option_values"].get(key) == value
                    for key, value in selected_options.items()
                )
            ]

    prices = sorted({row["price"] for row in configurations})
    minimum = prices[0] if prices else None
    maximum = prices[-1] if prices else None
    exact = bool(prices) and len(prices) == 1
    display = ""
    if exact:
        display = money_text(minimum)
    elif minimum is not None and maximum is not None:
        display = f"{money_text(minimum)}-{money_text(maximum)}"
    return {
        "configurations": configurations,
        "prices": prices,
        "minimum": minimum,
        "maximum": maximum,
        "exact": exact,
        "display": display,
    }


def format_variant_pricing(rows: list[dict], *, include_inventory: bool = True) -> str:
    """Compact variant matrix for the system prompt."""

    grouped: dict[int, list[dict]] = {}
    for row in rows:
        if row.get("variant_id"):
            grouped.setdefault(int(row["variant_id"]), []).append(row)
    rendered = []
    for variant_id, options in grouped.items():
        color = options[0].get("color") or "колір"
        distinct_prices = {row["price"] for row in options}
        reasons = list(dict.fromkeys(
            row["price_reason"] for row in options if row.get("price_reason")
        ))
        if len(distinct_prices) == 1:
            detail = f"ціна {options[0]['price_text']} грн"
            fits = list(dict.fromkeys(row.get("fit_code") for row in options if row.get("fit_code")))
            sized_fits = list(dict.fromkeys(
                f"{row['fit_code']}={'/'.join(row['compatible_sizes'])}"
                for row in options
                if row.get("fit_code") and row.get("compatible_sizes")
            ))
            if sized_fits:
                detail += f", фасони/розміри: {'; '.join(sized_fits)}"
            elif fits:
                detail += f", фасони: {'/'.join(fits)}"
        else:
            parts = []
            for row in options:
                option_label = ",".join(
                    f"{key}={value}" for key, value in row["option_values"].items()
                ) or "базова"
                parts.append(f"{option_label}={row['price_text']} грн")
            detail = "ціни: " + ", ".join(dict.fromkeys(parts))
            sized_fits = list(dict.fromkeys(
                f"{row['fit_code']}={'/'.join(row['compatible_sizes'])}"
                for row in options
                if row.get("fit_code") and row.get("compatible_sizes")
            ))
            if sized_fits:
                detail += "; фасони/розміри: " + "; ".join(sized_fits)
        if reasons:
            detail += ", причина: " + "; ".join(reasons)
        if include_inventory:
            stock = int(options[0].get("stock") or 0)
            detail += f", обліковий stock={stock}"
        rendered.append(f"{color} (variant_id={variant_id}, {detail})")
    return ", ".join(rendered)
