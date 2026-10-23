"""Bounded current-catalog authority for an already owned source-cart capture.

The isolated native-color branch reads authenticated source receipts to retain
an exact chosen variant when color names coincide. It performs no checkout
effect or provider call. Effect owners retain source/permission/catalog fences.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import re

from management.services.ig_reply_truth import ReplyTruthContext

MAX_LINES = 16
MAX_PRODUCTS = 8
MAX_VARIANTS = 32
MAX_READS = 128


@dataclass(frozen=True)
class SourceCartCatalogResult:
    _payload_json: str

    def as_dict(self):
        return json.loads(self._payload_json)

    @property
    def complete(self):
        return self.as_dict()["complete"]

    @property
    def gaps(self):
        return tuple(self.as_dict()["gaps"])

    @property
    def readiness_by_line(self):
        return self.as_dict()["readiness_by_line"]

    @property
    def item_specs_by_line(self):
        return self.as_dict()["item_specs_by_line"]

    @property
    def contexts_by_line(self):
        return {line_id: ReplyTruthContext(authorized_prices=tuple(Decimal(value) for value in row["prices"]),
            authorized_price_ranges=tuple((Decimal(low), Decimal(high)) for low, high in row["price_ranges"]),
            allowed_sizes=tuple(row["sizes"]), allowed_fits=tuple(row["fits"]), allowed_colors=tuple(row["colors"]))
            for line_id, row in self.as_dict()["authority_by_line"].items()}


def _result(*, capture=None, readiness=None, authorities=None, specs=None, gaps=(), reads=0):
    payload = {"schema": "source-cart-catalog.v1", "complete": not gaps,
        "source_capture_digest": (capture or {}).get("capture_digest"),
        "scope": (capture or {}).get("scope") or {}, "readiness_by_line": readiness or {},
        "authority_by_line": authorities or {}, "item_specs_by_line": specs or {},
        "gaps": list(gaps), "select_count": reads, "query_limit": MAX_READS}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    payload["catalog_digest"] = hashlib.sha256(canonical.encode()).hexdigest()
    return SourceCartCatalogResult(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))


class SourceCartCatalogReadError(RuntimeError):
    pass


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _color_matches(wish, variant):
    """Current catalogue name or a known colour alias; never provider IDs."""
    from management.services.ig_response_plan import CHOICE_ALIASES
    label = str(variant.color.name or "").strip().casefold()
    requested = str(wish or "").strip().casefold()
    if not requested:
        return False
    if requested == label:
        return True
    aliases = next((values for key, values in CHOICE_ALIASES.items()
        if key not in {"hoodie", "tshirt", "classic", "oversize"} and requested in values), ())
    return any(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", label, re.I) for alias in aliases)


def _size_blocked(variant, size, fit, *, include_ledger=True):
    """Mirror general/specific rule precedence using the prepared relation."""
    if variant is None or not size:
        return False
    rows = sorted((row for row in variant.product_catalog_size_rules.all()
        if str(row.size).casefold() == str(size).casefold() and row.fit_code in {"", fit}), key=lambda row: row.pk)
    general = next((row for row in reversed(rows) if not row.fit_code), None)
    specific = next((row for row in reversed(rows) if row.fit_code == fit), None) if fit else None
    rule = specific or general
    return bool(rule and (not rule.is_enabled or (include_ledger and rule.stock is not None and rule.stock <= 0)))


def _config_matches(row, *, fit, options, size, variant, include_ledger=True):
    if fit and row.get("fit_code") != fit:
        return False
    if any(row.get("option_values", {}).get(key) != value for key, value in options.items()):
        return False
    if size and row.get("has_compatible_size_contract") and size not in row.get("compatible_sizes", ()):
        return False
    return not _size_blocked(variant, size, fit, include_ledger=include_ledger)


def resolve_source_cart_catalog(capture, *, _catalog_readback=None):
    """Read one batched graph, fail closed on any budget or observation failure."""
    from django.db import connection, DatabaseError
    if (not isinstance(capture, dict) or capture.get("schema") != "source-selections.v1"
        or capture.get("status") != "captured" or not capture.get("coverage_complete")):
        return _result(gaps=({"reason": "source_cart_catalog_capture_unavailable"},))
    rows = capture.get("lines")
    if (not isinstance(rows, list) or not rows or len(rows) > MAX_LINES
        or any(not isinstance(row, dict) or not isinstance(row.get("line_id"), str) or not 0 < len(row["line_id"]) <= 128 for row in rows)
        or len({row["line_id"] for row in rows}) != len(rows)):
        return _result(capture=capture, gaps=({"reason": "source_cart_catalog_line_bound"},))
    reads, rejection = 0, ""
    def bounded(execute, sql, params, many, context):
        nonlocal reads, rejection
        if not sql.lstrip().upper().startswith("SELECT"):
            rejection = "source_cart_catalog_not_read_only"
        elif reads >= MAX_READS:
            rejection = "source_cart_catalog_query_bound"
        if rejection:
            raise SourceCartCatalogReadError(rejection)
        reads += 1
        return execute(sql, params, many, context)
    try:
        with connection.execute_wrapper(bounded):
            result = _resolve(capture, **({"_catalog_readback": _catalog_readback} if _catalog_readback is not None else {}))
        if rejection:
            raise SourceCartCatalogReadError(rejection)
        data = result.as_dict()
        return _result(capture=capture, readiness=data["readiness_by_line"], authorities=data["authority_by_line"],
            specs=data["item_specs_by_line"], gaps=data["gaps"], reads=reads)
    except SourceCartCatalogReadError as exc:
        reason = str(exc)
    except (DatabaseError, AttributeError, KeyError, TypeError, ValueError, ArithmeticError):
        reason = "source_cart_catalog_unavailable"
    return _result(capture=capture, gaps=({"reason": rejection or reason},), reads=reads)


def _prepared_catalog_rows(capture, readback, product_ids):
    """Accept only the private prepared graph sealed by this source capture."""
    from storefront.models import Product, ProductStatus
    from productcolors.models import ProductColorVariant
    from management.services.ig_catalog_pricing import PRODUCT_PRICING_LOOKUPS, VARIANT_PRICING_LOOKUPS
    if (not isinstance(readback, dict) or readback.get("prepared") is not True
            or readback.get("source_capture_digest") != capture.get("capture_digest")
            or readback.get("scope") != capture.get("scope")
            or any(readback.get(key) != capture.get(key) for key in ("session_id", "generation", "selection_revision"))):
        raise SourceCartCatalogReadError("source_cart_catalog_readback_scope_changed")
    products, variants = readback.get("products"), readback.get("variants")
    if (not isinstance(products, (tuple, list)) or len(products) > MAX_LINES
            or not isinstance(variants, (tuple, list)) or len(variants) > MAX_LINES * MAX_VARIANTS
            or any(not isinstance(row, Product) or not _positive(row.pk) or row.status != ProductStatus.PUBLISHED for row in products)
            or len({row.pk for row in products}) != len(products)):
        raise SourceCartCatalogReadError("source_cart_catalog_readback_product_bound")
    by_id = {row.pk: row for row in products}
    if not product_ids.issubset(by_id):
        raise SourceCartCatalogReadError("source_cart_catalog_readback_product_mismatch")
    if (any(not isinstance(row, ProductColorVariant) or not _positive(row.pk)
            or row.product_id not in by_id or row._state.fields_cache.get("product") is not by_id[row.product_id]
            or "color" not in row._state.fields_cache for row in variants)
            or len({row.pk for row in variants}) != len(variants)
            or any(not {"category", "catalog", "size_grid"}.issubset(row._state.fields_cache) for row in products)):
        raise SourceCartCatalogReadError("source_cart_catalog_readback_variant_mismatch")
    for rows, lookups in ((products, PRODUCT_PRICING_LOOKUPS), (variants, VARIANT_PRICING_LOOKUPS)):
        roots = {name.split("__", 1)[0] for name in lookups}
        if any(not roots.issubset(set(row._state.fields_cache) | set(getattr(row, "_prefetched_objects_cache", {}))) for row in rows):
            raise SourceCartCatalogReadError("source_cart_catalog_readback_unprepared")
    selected = [row for row in products if row.pk in product_ids]
    selected_variants = [row for row in variants if row.product_id in product_ids]
    if len(selected_variants) > MAX_VARIANTS:
        raise SourceCartCatalogReadError("source_cart_catalog_variant_bound")
    return selected, selected_variants


def _resolve(capture, *, _catalog_readback=None):
    from storefront.models import Product, ProductStatus
    from productcolors.models import ProductColorVariant
    from product_catalog.services import product_option_context
    from management.services.ig_catalog_pricing import prepare_pricing_context, resolve_product_pricing
    from management.services.ig_catalog_compatibility import resolve_configuration_size_contract
    from management.services.ig_checkout_readiness import selection_readiness
    from management.services.ig_response_cart_plan import source_line_preferences
    preferences = {row["line_id"]: source_line_preferences(capture, row) for row in capture["lines"]}
    if any(not row for row in preferences.values()):
        return _result(capture=capture, gaps=({"reason": "source_cart_catalog_source_unknown"},))
    product_ids = {row.get("values", {}).get("product_id") for row in preferences.values()}
    product_ids.discard(None)
    if len(product_ids) > MAX_PRODUCTS or any(not _positive(value) for value in product_ids):
        return _result(capture=capture, gaps=({"reason": "source_cart_catalog_product_bound"},))
    if _catalog_readback is not None:
        products, variants = _prepared_catalog_rows(capture, _catalog_readback, product_ids)
    else:
        products = list(Product.objects.filter(pk__in=product_ids, status=ProductStatus.PUBLISHED)
            .select_related("category", "catalog", "size_grid").order_by("pk")[:MAX_PRODUCTS + 1])
        variants = list(ProductColorVariant.objects.filter(product_id__in=[row.pk for row in products])
            .select_related("color").order_by("product_id", "order", "pk")[:MAX_VARIANTS + 1])
    # Reject a foreign/stale prepared graph before observing any native choice.
    # A valid graph still needs fresh customer color-source ownership checks.
    from management.services.ig_catalog_choice_actions import admitted_native_color_variants
    native_variants = admitted_native_color_variants(capture, capture["lines"])
    if len(variants) > MAX_VARIANTS:
        return _result(capture=capture, gaps=({"reason": "source_cart_catalog_variant_bound"},))
    if _catalog_readback is None:
        prepare_pricing_context(products, variants)
    products_by_id = {row.pk: row for row in products}
    variants_by_id = {row.pk: row for row in variants}
    variant_groups = {product.pk: [row for row in variants if row.product_id == product.pk] for product in products}
    # The prepared resolver enumerates backend-enabled option/variant prices once.
    pricing = {product.pk: resolve_product_pricing(product, variants=variant_groups[product.pk], context_prepared=True) for product in products}
    readiness, authorities, specs, gaps = {}, {}, {}, []
    for row in capture["lines"]:
        line_id = row["line_id"]
        values = preferences[line_id]["values"]
        product_id = values.get("product_id")
        product = products_by_id.get(product_id)
        authority = {"prices": [], "price_ranges": [], "sizes": [], "fits": [], "colors": []}
        authorities[line_id] = authority
        if product is None:
            readiness[line_id] = {"has_product": False, "product": {"id": product_id}, "missing": ["product"], "applicability_known": False}
            gaps.append({"line_id": line_id, "reason": "source_cart_catalog_product_unknown"})
            continue
        product_variants = variant_groups[product_id]
        wish = values.get("color")
        native = native_variants.get(line_id)
        if native is not None:
            matches = [variant for variant in product_variants if native.get("status") == "verified"
                and variant.pk == native.get("variant_id") and _color_matches(wish, variant)]
        else:
            matches = [variant for variant in product_variants if _color_matches(wish, variant)] if wish else product_variants
        selected_variant = matches[0] if len(matches) == 1 else None
        color_gap = bool((wish and len(matches) != 1) or (not wish and len(product_variants) > 1))
        fit = str(values.get("fit_option_code") or "").strip().lower()
        size = str(values.get("size") or "").strip().upper()
        options = {"fit": fit} if fit else {}
        axes = product_option_context(product, variant=selected_variant, option_values=options).get("axes") or []
        # Sole/fixed backend options are configuration defaults, never source wishes.
        for axis in axes:
            code = str(axis.get("code") or "")
            if code == "fit":
                continue
            enabled = [choice for choice in axis.get("choices") or [] if choice.get("is_enabled")]
            fixed = axis.get("fixed_choice")
            if fixed or len(enabled) == 1:
                options[code] = str((fixed or enabled[0])["code"])
        selected_configs = [config for config in pricing[product_id]["configurations"]
            if (selected_variant is None or config.get("variant_id") == selected_variant.pk)
            and _config_matches(config, fit=fit, options=options, size=size, variant=variants_by_id.get(config.get("variant_id")))]
        allowed_variants = {config["variant_id"] for config in selected_configs if config.get("variant_id")}
        from product_catalog.content_resolution import build_combination_key
        sizes, has_contract = resolve_configuration_size_contract(product, selected_variant,
            {"fit_code": fit, "option_key": build_combination_key(options)})
        available = [value for value in sizes if not _size_blocked(selected_variant, value, fit)]
        disabled = sorted({str(rule.size).upper() for rule in selected_variant.product_catalog_size_rules.all()
            if rule.fit_code in {"", fit} and _size_blocked(selected_variant, str(rule.size).upper(), fit)}) if selected_variant else []
        quantity = values.get("quantity")
        if quantity is None:
            default = (row.get("defaults") or {}).get("quantity") or {}
            quantity = 1 if default == {"value": 1, "authority": "existing_cart_default", "source_confirmed": False} else None
        selection = {"fit_option_code": fit, "option_values": options,
            **({"color_variant_id": selected_variant.pk} if selected_variant else {})}
        graph = {"product_id": product_id, "product": product,
            "fit_rows": [item for item in product.fit_options.all() if item.is_active],
            "variants": product_variants, "allowed_variant_ids": allowed_variants,
            "grid": {"available": available, "disabled": disabled, "resolved": has_contract and selected_variant is not None}}
        state = selection_readiness(product_id=product_id, selection=selection, size=size, fit=fit,
            quantity=quantity or 1, color=str(wish or ""), strict=True, _catalog_inputs=graph)
        if color_gap:
            state["applicability_known"] = False
            state["can_issue_link"] = False
            if "color" not in state["missing"]:
                state["missing"].append("color")
            state["color"]["resolution"] = "ambiguous" if len(matches) > 1 else "unavailable" if wish else "unselected"
        if not _positive(quantity):
            state["can_issue_link"] = False
            state["missing"].append("quantity")
        readiness[line_id] = state
        exact = bool(state.get("applicability_known") and not state.get("missing") and selected_variant
            and selected_variant.pk in allowed_variants and selected_configs)
        if not exact:
            # A source-owned exact configuration can have a known unit price
            # while its physical availability still needs manager review.
            # Keep every readiness/spec/checkout gate above unchanged. Only
            # this read-only price projection ignores accounting stock; an
            # explicitly disabled size or option remains excluded.
            if selected_variant and not color_gap and fit and size:
                priced = [config for config in pricing[product_id]["configurations"]
                    if config.get("variant_id") == selected_variant.pk
                    and config.get("has_compatible_size_contract")
                    and _config_matches(config, fit=fit, options=options, size=size,
                        variant=selected_variant, include_ledger=False)]
                prices = sorted({config["price"] for config in priced})
                if len(priced) == 1 and len(prices) == 1:
                    authority["prices"] = [str(prices[0])]
            gaps.append({"line_id": line_id, "reason": "source_cart_catalog_configuration_unready"})
            continue
        # Resolve again against the exact selectors, reusing the prepared graph.
        exact_pricing = resolve_product_pricing(product, variants=[selected_variant],
            selected_variant_id=selected_variant.pk, option_values=state["options"]["selected"], context_prepared=True)
        current = [config for config in exact_pricing["configurations"] if _config_matches(config,
            fit=fit, options=state["options"]["selected"], size=size, variant=selected_variant)]
        prices = sorted({config["price"] for config in current})
        if len(prices) != 1:
            gaps.append({"line_id": line_id, "reason": "source_cart_catalog_price_unverified"})
            continue
        authority.update(prices=[str(prices[0])], sizes=available,
            fits=[state["fit"]["selected"]] if state["fit"]["selected"] else [],
            colors=[selected_variant.color.name])
        state["product"].update(price=str(prices[0]), price_exact=True)
        specs[line_id] = {"line_id": line_id, "recipient_id": row.get("recipient_id", "self"),
            "product_id": product_id, "size": size, "fit_option_code": fit, "qty": quantity,
            "color_variant_id": selected_variant.pk, "option_values": state["options"]["selected"],
            "unit_price": str(prices[0])}
    return _result(capture=capture, readiness=readiness, authorities=authorities, specs=specs, gaps=gaps)
