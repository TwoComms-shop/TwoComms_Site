"""Exact physical inventory observation; no subscription or send authority.

Catalogue configuration, physical stock and protected commitments are separate
facts. Two bounded reads must agree. No row lock, write, allocation or provider
operation is performed; MyISAM parents are not treated as transactional locks.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re

from django.db import DatabaseError, connection
from django.db.models import Q
from django.utils import timezone

TARGET_KEYS = frozenset({"product_id", "color_variant_id", "size", "fit_option_code", "option_values", "quantity"})
MAX_QUERIES = 64
MAX_RULES = 128
MAX_RESERVATIONS = 128
MAX_VARIANTS = 32
MAX_GRAPH_ROWS = 512
MAX_FENCE_BYTES = 256 * 1024
_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")
REASONS = frozenset({"target_invalid", "quantity_invalid", "target_options_invalid", "inventory_capture_bound",
    "configuration_capture_incomplete", "reservation_bound", "inventory_snapshot_changed", "product_unavailable",
    "variant_unavailable", "inventory_policy_unknown", "inventory_rule_bound", "fit_unavailable", "configuration_bound",
    "configuration_options_incomplete", "configuration_unavailable", "configuration_size_unknown", "configuration_size_unavailable",
    "exact_size_stock_untracked", "inventory_quantity_invalid", "actual_checkout_options_unsupported", "inventory_mapping_unknown",
    "inventory_stock_identity_unknown", "physical_stock_available", "insufficient_free_stock", "inventory_read_not_readonly",
    "inventory_query_bound", "inventory_observation_unavailable", "source_interest_incomplete", "inventory_variant_bound",
    "source_color_ambiguous", "stale_catalog_size_count", "configuration_readback_invalid"})


class ObservationError(ValueError):
    pass


@dataclass(frozen=True)
class RestockAvailability:
    _payload_json: str

    def as_dict(self):
        return json.loads(self._payload_json)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _digest(value):
    encoded = _json(value).encode()
    if len(encoded) > MAX_FENCE_BYTES:
        raise ObservationError("inventory_capture_bound")
    return hashlib.sha256(encoded).hexdigest()


def _result(*, status="unknown", reason, target=None, evidence=None, labels=None, now):
    payload = {"version": 1, "status": status, "reason": reason, "target": deepcopy(target or {}),
        "evidence": deepcopy(evidence or {}), "captured_at": now.isoformat(), "labels": deepcopy(labels or {})}
    payload["digest"] = _digest(payload)
    return RestockAvailability(_json(payload))


def _target(raw):
    if not isinstance(raw, dict) or set(raw) != TARGET_KEYS:
        raise ObservationError("target_invalid")
    for key in ("product_id", "color_variant_id"):
        if type(raw[key]) is not int or not 0 < raw[key] <= 2**63 - 1:
            raise ObservationError("target_invalid")
    if type(raw["quantity"]) is not int or not 1 <= raw["quantity"] <= 99:
        raise ObservationError("quantity_invalid")
    size, fit = raw["size"], raw["fit_option_code"]
    if (not isinstance(size, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 +./-]{0,11}", size)
            or not isinstance(fit, str) or not _TOKEN.fullmatch(fit)):
        raise ObservationError("target_invalid")
    options = raw["option_values"]
    if (not isinstance(options, dict) or not 1 <= len(options) <= 8
            or any(not isinstance(key, str) or not _TOKEN.fullmatch(key) or not isinstance(value, str)
                or not _TOKEN.fullmatch(value) for key, value in options.items())
            or options.get("fit") != fit):
        raise ObservationError("target_options_invalid")
    return {**deepcopy(raw), "size": size.upper(), "option_values": dict(sorted(options.items()))}


def _graph_fence(product, variant):
    """Hash only already loaded configuration graph; never lazy-read a field."""
    queue, seen, rows = [product, variant], set(), []
    while queue:
        model = queue.pop()
        key = (model._meta.db_table, model.pk)
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > MAX_GRAPH_ROWS:
            raise ObservationError("inventory_capture_bound")
        fields = {}
        for field in model._meta.concrete_fields:
            if field.attname not in model.__dict__:
                raise ObservationError("configuration_capture_incomplete")
            fields[field.attname] = model.__dict__[field.attname]
        rows.append({"table": key[0], "id": key[1], "fields": fields})
        queue.extend(value for value in model._state.fields_cache.values() if hasattr(value, "_meta"))
        for value in getattr(model, "_prefetched_objects_cache", {}).values():
            cached = value if isinstance(value, (list, tuple)) else getattr(value, "_result_cache", None)
            if cached is None:
                raise ObservationError("configuration_capture_incomplete")
            queue.extend(row for row in cached if hasattr(row, "_meta"))
    return _digest(sorted(rows, key=lambda row: (row["table"], str(row["id"]))))


def _protected(*, source, identity, now):
    from management.models import IgCheckoutInventoryReservation as Reservation
    if source == "warehouse":
        from warehouse.services.inventory import protected_stock_quantity
        protected = protected_stock_quantity(stock_item_id=identity, at=now)
        queryset = Reservation.objects.filter(allocation_source="warehouse", stock_item_id=identity)
    else:
        from management.services.ig_inventory import _catalog_variant_protected_quantity
        protected = _catalog_variant_protected_quantity(color_variant_id=identity, at=now)
        queryset = Reservation.objects.filter(allocation_source__in=["catalog_variant", ""], color_variant_id=identity)
    rows = list(queryset.filter(Q(state=Reservation.State.ACTIVE, expires_at__gt=now)
        | Q(state=Reservation.State.PAID_COMMITTED)).order_by("pk").values(
            "id", "quantity", "fulfilled_quantity", "state", "expires_at", "updated_at")[:MAX_RESERVATIONS + 1])
    if len(rows) > MAX_RESERVATIONS:
        raise ObservationError("reservation_bound")
    if sum(row["quantity"] - (row["fulfilled_quantity"] if source == "warehouse" else 0) for row in rows) != protected:
        raise ObservationError("inventory_snapshot_changed")
    return protected, rows


def _read(target, now):
    from storefront.models import Product, ProductStatus
    from productcolors.models import ProductColorVariant
    from product_catalog.models import ProductInventoryPolicy, VariantSizeRule, VariantBlankLink
    from product_catalog.content_resolution import build_combination_key
    from product_catalog.services import product_option_context, variant_allows_options, variant_allows_fit
    from management.services.ig_catalog_pricing import prepare_pricing_context
    from management.services.ig_catalog_compatibility import resolve_configuration_size_contract

    product = Product.objects.filter(pk=target["product_id"]).select_related("category", "catalog", "size_grid").first()
    if product is None or product.status != ProductStatus.PUBLISHED:
        raise ObservationError("product_unavailable")
    variant = ProductColorVariant.objects.filter(pk=target["color_variant_id"], product_id=product.pk).select_related("color").first()
    if variant is None:
        raise ObservationError("variant_unavailable")
    policy = ProductInventoryPolicy.objects.filter(product_id=product.pk).first()
    if policy is None or policy.source not in {"warehouse", "catalog_variant"}:
        raise ObservationError("inventory_policy_unknown")
    rules = list(VariantSizeRule.objects.filter(variant_id=variant.pk).order_by("fit_code", "size").values(
        "id", "fit_code", "size", "is_enabled", "stock", "updated_at")[:MAX_RULES + 1])
    if len(rules) > MAX_RULES:
        raise ObservationError("inventory_rule_bound")
    prepare_pricing_context([product], [variant])
    if len(list(variant.product_catalog_size_rules.all())) > MAX_RULES:
        raise ObservationError("inventory_rule_bound")
    fit = target["fit_option_code"]
    fits = [row for row in product.fit_options.all() if row.is_active and row.code == fit]
    if len(fits) != 1 or not variant_allows_fit(variant, fit):
        raise ObservationError("fit_unavailable")
    context = product_option_context(product, variant=variant, option_values=target["option_values"])
    axes = context.get("axes") or []
    if len(axes) > 8 or any(len(axis.get("choices") or []) > 32 for axis in axes):
        raise ObservationError("configuration_bound")
    if {axis["code"] for axis in axes} != set(target["option_values"]):
        raise ObservationError("configuration_options_incomplete")
    if not variant_allows_options(variant, target["option_values"]):
        raise ObservationError("configuration_unavailable")
    sizes, known = resolve_configuration_size_contract(product, variant,
        {"fit_code": fit, "option_key": build_combination_key(target["option_values"])})
    if not known:
        raise ObservationError("configuration_size_unknown")
    if target["size"] not in {str(value).upper() for value in sizes}:
        raise ObservationError("configuration_size_unavailable")
    evidence = {"policy_id": policy.pk, "mode": policy.source, "product_id": product.pk,
        "color_variant_id": variant.pk, "configuration_digest": _graph_fence(product, variant),
        "size_rule_ids": [row["id"] for row in rules], "stock_item_id": None, "blank_link_id": None,
        "size_cap": None}
    fence = {"policy": {"id": policy.pk, "source": policy.source, "updated_at": policy.updated_at},
        "configuration_digest": evidence["configuration_digest"], "rules": rules}
    if policy.source == "catalog_variant":
        matches = [row for row in rules if row["size"].upper() == target["size"] and row["fit_code"] in {"", fit}]
        specific = [row for row in matches if row["fit_code"] == fit]
        effective = specific if specific else [row for row in matches if not row["fit_code"]]
        if len(effective) != 1 or effective[0]["stock"] is None:
            raise ObservationError("exact_size_stock_untracked")
        rule = effective[0]
        if type(rule["stock"]) is not int or rule["stock"] < 0:
            raise ObservationError("inventory_quantity_invalid")
        if not rule["is_enabled"]:
            raise ObservationError("configuration_size_unavailable")
        evidence["size_cap"] = rule["stock"]
        from management.models import IgCheckoutInventoryReservation as Reservation
        terminal = list(Reservation.objects.filter(allocation_source__in=["catalog_variant", ""],
            color_variant_id=variant.pk, state__in=[Reservation.State.FULFILLED, Reservation.State.CONSUMED]).filter(
                Q(fulfilled_at__gt=rule["updated_at"]) | Q(consumed_at__gt=rule["updated_at"])
                | Q(fulfilled_at__isnull=True, consumed_at__isnull=True)).order_by("pk").values(
                    "id", "state", "quantity", "fulfilled_at", "consumed_at", "updated_at")[:MAX_RESERVATIONS + 1])
        if len(terminal) > MAX_RESERVATIONS:
            raise ObservationError("reservation_bound")
        if terminal:
            raise ObservationError("stale_catalog_size_count")
        evidence["size_count_observed_at"] = rule["updated_at"].isoformat()
        physical = int(variant.stock)
        protected, reservations = _protected(source="catalog_variant", identity=variant.pk, now=now)
        # Variant commitments may span sizes; subtract all of them from this
        # cap conservatively rather than guessing which units they protect.
        free = min(max(0, rule["stock"] - protected), max(0, physical - protected))
        # Existing editor revision format is preserved, including datetime str.
        revision_rows = [{key: row[key] for key in ("fit_code", "size", "is_enabled", "stock", "updated_at")} for row in rules]
        evidence["source_revision"] = "product_catalog:" + hashlib.sha256(json.dumps(
            revision_rows, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    else:
        if set(target["option_values"]) != {"fit"}:
            # Actual checkout currently reduces mappings to fit only. A richer
            # mapping cannot grant a physical fact the allocation owner drops.
            raise ObservationError("actual_checkout_options_unsupported")
        option_key = build_combination_key(target["option_values"])
        links = list(VariantBlankLink.objects.filter(variant_id=variant.pk, option_key=option_key).order_by("pk").values(
            "id", "option_key", "storage_subcategory_id", "updated_at")[:2])
        if len(links) != 1:
            raise ObservationError("inventory_mapping_unknown")
        from warehouse.models import StockItem
        items = list(StockItem.objects.filter(subcategory_id=links[0]["storage_subcategory_id"],
            color_id=variant.color_id, size__iexact=target["size"]).order_by("pk").values(
                "id", "subcategory_id", "color_id", "size", "quantity", "updated_at")[:2])
        if len(items) != 1:
            raise ObservationError("inventory_stock_identity_unknown")
        stock = items[0]
        evidence.update(stock_item_id=stock["id"], blank_link_id=links[0]["id"])
        physical = stock["quantity"]
        protected, reservations = _protected(source="warehouse", identity=stock["id"], now=now)
        free = max(0, physical - protected)
        fence.update(links=links, stock=stock)
    evidence.update(physical_quantity=physical, protected_quantity=protected, free_quantity=free,
        protected_reservation_ids=[row["id"] for row in reservations])
    fence.update(physical_quantity=physical, protected_quantity=protected, free_quantity=free, reservations=reservations)
    evidence["inventory_digest"] = _digest(fence)
    labels = {"product": str(product.title or "")[:160], "color": str(variant.color.name or "")[:80],
        "size": target["size"], "fit": fit, "quantity": target["quantity"], "option_key": build_combination_key(target["option_values"])}
    return {"status": "available" if free >= target["quantity"] else "unavailable",
        "reason": "physical_stock_available" if free >= target["quantity"] else "insufficient_free_stock",
        "evidence": evidence, "labels": labels}


def _bounded(operation, *, target, now):
    reads = 0
    def readonly(execute, sql, params, many, context):
        nonlocal reads
        if not sql.lstrip().upper().startswith("SELECT") or re.search(r"FOR\s+UPDATE|LOCK\s+IN\s+SHARE", sql, re.I):
            raise ObservationError("inventory_read_not_readonly")
        reads += 1
        if reads > MAX_QUERIES:
            raise ObservationError("inventory_query_bound")
        return execute(sql, params, many, context)
    try:
        with connection.execute_wrapper(readonly):
            return operation()
    except ObservationError as exc:
        return _result(reason=str(exc) if str(exc) in REASONS else "inventory_observation_unavailable", target=target, now=now)
    except (DatabaseError, TypeError, ValueError, KeyError, AttributeError, ArithmeticError):
        return _result(reason="inventory_observation_unavailable", target=target, now=now)


def observe_target(target: dict, *, now=None) -> RestockAvailability:
    """Two agreeing exact observations; no reservation, subscription or send."""
    now = now or timezone.now()
    if not isinstance(now, datetime) or timezone.is_naive(now):
        raise ValueError("An aware observation time is required")
    try:
        canonical = _target(target)
    except ObservationError as exc:
        return _result(reason=str(exc), now=now)
    def capture():
        before, after = _read(canonical, now), _read(canonical, now)
        if _digest(before) != _digest(after):
            raise ObservationError("inventory_snapshot_changed")
        return _result(target=canonical, now=now, **before)
    return _bounded(capture, target=canonical, now=now)


# Configuration is a separate contract from numeric availability. In
# particular these fields intentionally exclude stock, reserve/consumption
# evidence, inventory policy and stock-editor clocks.
_CONFIGURATION_FIELDS = {
    "product": ("id", "title", "status", "category_id", "catalog_id", "size_grid_id"),
    "productcolorvariant": ("id", "product_id", "color_id", "slug"),
    "color": ("id", "name", "primary_hex", "secondary_hex"),
    "colorprofile": ("id", "color_id", "is_thermo"),
    "category": ("id", "slug"), "catalog": ("id", "slug"),
    "catalogoption": ("id", "catalog_id", "option_type", "order"),
    "catalogoptionvalue": ("id", "option_id", "value", "order"),
    "sizegrid": ("id", "catalog_id", "guide_data", "is_active", "order"),
    "sizegridprofile": ("id", "size_grid_id", "option_key", "is_active"),
    "productfitoption": ("id", "product_id", "code", "label", "is_active", "is_default", "order"),
    "variantfitrule": ("id", "variant_id", "fit_code", "is_enabled"),
    "productfitnote": ("id", "product_id", "fit_code", "is_enabled"),
    "productoptionprofile": ("id", "product_id", "option_key", "option_values", "is_active"),
    "variantcombinationprofile": ("id", "variant_id", "combination_key", "option_values", "is_active"),
    "variantsizerule": ("id", "variant_id", "fit_code", "size"),
    "productsizerule": ("id", "product_id", "option_key", "size"),
    "productoptionsizegrid": ("id", "product_id", "option_key", "size_grid_id"),
    "variantoptionsizegrid": ("id", "variant_id", "option_key", "size_grid_id"),
    "garmentflow": ("id", "code", "axes", "is_active"),
    "garmentflowcategory": ("id", "category_id", "flow_id", "is_active", "order"),
    "variantdetails": ("id", "variant_id", "is_thermo"),
}


def _configuration_graph(product, variant):
    queue, seen, rows = [product, variant], set(), []
    while queue:
        model = queue.pop()
        key = (model._meta.db_table, model.pk)
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > MAX_GRAPH_ROWS:
            raise ObservationError("configuration_bound")
        names = _CONFIGURATION_FIELDS.get(model._meta.model_name, ())
        concrete = {field.attname for field in model._meta.concrete_fields}
        fields = {}
        for name in names:
            if name not in concrete:
                continue
            if name not in model.__dict__:
                raise ObservationError("configuration_capture_incomplete")
            fields[name] = model.__dict__[name]
        if fields:
            rows.append({"table": key[0], "id": key[1], "fields": fields})
        queue.extend(value for value in model._state.fields_cache.values() if hasattr(value, "_meta"))
        for value in getattr(model, "_prefetched_objects_cache", {}).values():
            cached = value if isinstance(value, (list, tuple)) else getattr(value, "_result_cache", None)
            if cached is None:
                raise ObservationError("configuration_capture_incomplete")
            queue.extend(row for row in cached if hasattr(row, "_meta"))
    return sorted(rows, key=lambda row: (row["table"], str(row["id"])))


def _read_configuration(target, *, prepared=None):
    from storefront.models import Product, ProductStatus
    from productcolors.models import ProductColorVariant
    from product_catalog.content_resolution import build_combination_key
    from product_catalog.services import product_option_context, variant_allows_options, variant_allows_fit
    from management.services.ig_catalog_pricing import prepare_pricing_context
    from management.services.ig_catalog_compatibility import resolve_configuration_size_contract

    product = (prepared[0].get(target["product_id"]) if prepared is not None else
        Product.objects.filter(pk=target["product_id"]).select_related("category", "catalog", "size_grid").first())
    if not product or product.status != ProductStatus.PUBLISHED:
        raise ObservationError("product_unavailable")
    variant = (prepared[1].get(target["color_variant_id"]) if prepared is not None else
        ProductColorVariant.objects.filter(pk=target["color_variant_id"], product_id=product.pk).select_related("color").first())
    if not variant or variant.product_id != product.pk or not variant.color_id:
        raise ObservationError("variant_unavailable")
    if prepared is None:
        prepare_pricing_context([product], [variant])
    fit = target["fit_option_code"]
    fits = [row for row in product.fit_options.all() if row.is_active and row.code == fit]
    if len(fits) != 1 or not variant_allows_fit(variant, fit):
        raise ObservationError("fit_unavailable")
    context = product_option_context(product, variant=variant, option_values=target["option_values"])
    axes = context.get("axes") or []
    if len(axes) > 8 or any(len(axis.get("choices") or []) > 32 for axis in axes):
        raise ObservationError("configuration_bound")
    if {axis["code"] for axis in axes} != set(target["option_values"]):
        raise ObservationError("configuration_options_incomplete")
    if not variant_allows_options(variant, target["option_values"]):
        raise ObservationError("configuration_unavailable")
    rules = list(product.product_catalog_size_rules.all()) + list(variant.product_catalog_size_rules.all())
    if len(rules) > MAX_RULES:
        raise ObservationError("inventory_rule_bound")
    graph = _configuration_graph(product, variant)
    # Disabled size rows restrict ordinary checkout availability. They do not
    # erase a size from its actual configured grid for a manager supply review.
    # Only local, freshly prefetched objects are changed, then restored; no save.
    flags = [row.is_enabled for row in rules]
    try:
        for row in rules:
            row.is_enabled = True
        sizes, known = resolve_configuration_size_contract(product, variant,
            {"fit_code": fit, "option_key": build_combination_key(target["option_values"])})
    finally:
        for row, enabled in zip(rules, flags):
            row.is_enabled = enabled
    if not known:
        raise ObservationError("configuration_size_unknown")
    if target["size"] not in {str(size).upper() for size in sizes}:
        raise ObservationError("configuration_size_unavailable")
    labels = {"product": str(product.title or "")[:160], "color": str(variant.color.name or "")[:80],
        "size": target["size"], "fit": fit, "quantity": target["quantity"], "option_key": build_combination_key(target["option_values"])}
    digest = _digest({"version": 1, "target": target, "graph": graph, "sizes": list(sizes),
        "axes": [{"code": axis["code"], "choices": [{"code": choice["code"], "is_enabled": choice["is_enabled"]}
            for choice in axis.get("choices", ())]} for axis in axes]})
    return {"version": 1, "status": "valid", "target": deepcopy(target), "configuration_digest": digest,
        "reason": "exact_configuration_valid", "labels": labels}


_INVALID_CONFIGURATION_REASONS = frozenset({"target_invalid", "quantity_invalid", "target_options_invalid",
    "product_unavailable", "variant_unavailable", "fit_unavailable", "configuration_options_incomplete",
    "configuration_unavailable", "configuration_size_unavailable"})
MAX_CONFIGURATION_TARGETS = 20


def _configuration_failure(target, reason, *, whole_capture=False):
    return {"version": 1, "status": "invalid" if reason in _INVALID_CONFIGURATION_REASONS and not whole_capture else "unknown",
        "target": deepcopy(target), "configuration_digest": "", "reason": reason, "labels": {}}


def _read_configurations(targets, *, prepared=None):
    """One bulk graph preload; a target cannot perform an independent read."""
    from storefront.models import Product
    from productcolors.models import ProductColorVariant
    from management.services.ig_catalog_pricing import prepare_pricing_context
    if prepared is None:
        products = list(Product.objects.filter(pk__in={target["product_id"] for target in targets if target})
            .select_related("category", "catalog", "size_grid").order_by("pk")[:MAX_CONFIGURATION_TARGETS])
        variants = list(ProductColorVariant.objects.filter(pk__in={target["color_variant_id"] for target in targets if target})
            .select_related("color").order_by("pk")[:MAX_CONFIGURATION_TARGETS])
        product_by_id = {product.pk: product for product in products}
        # A wrong-product variant is invalid for that target and must never lazily
        # fetch its real product through an ORM relation.
        variants = [variant for variant in variants if variant.product_id in product_by_id]
        prepare_pricing_context(products, variants)
        prepared = (product_by_id, {variant.pk: variant for variant in variants})
    results = []
    def no_target_reads(execute, sql, params, many, context):
        raise ObservationError("configuration_capture_incomplete")
    for target in targets:
        if target is None:
            results.append(None)
            continue
        try:
            with connection.execute_wrapper(no_target_reads):
                results.append(_read_configuration(target, prepared=prepared))
        except ObservationError as exc:
            reason = str(exc) if str(exc) in REASONS else "configuration_capture_incomplete"
            results.append(_configuration_failure(target, reason))
    return results


def observe_configurations(targets, *, now=None, _catalog_readback=None, source_cart=None) -> list[dict]:
    """Aligned exact configuration proofs from two bounded shared snapshots.

    At most twenty targets are accepted. Invalid individual input is isolated;
    any snapshot conflict or database failure withholds every batch proof.
    """
    now = now or timezone.now()
    if not isinstance(now, datetime) or timezone.is_naive(now):
        raise ValueError("An aware observation time is required")
    if not isinstance(targets, (list, tuple)) or len(targets) > MAX_CONFIGURATION_TARGETS:
        raise ValueError("At most twenty configuration targets are required")
    canonical, failures = [], {}
    for index, target in enumerate(targets):
        try:
            canonical.append(_target(target))
        except ObservationError as exc:
            canonical.append(None)
            failures[index] = _configuration_failure({}, str(exc))
    if not any(canonical):
        return [failures[index] for index in range(len(canonical))]
    reads = 0
    def readonly(execute, sql, params, many, context):
        nonlocal reads
        if not sql.lstrip().upper().startswith("SELECT") or re.search(r"FOR\s+UPDATE|LOCK\s+IN\s+SHARE", sql, re.I):
            raise ObservationError("inventory_read_not_readonly")
        reads += 1
        if reads > MAX_QUERIES:
            raise ObservationError("inventory_query_bound")
        return execute(sql, params, many, context)
    try:
        with connection.execute_wrapper(readonly):
            if _catalog_readback is not None:
                from management.services.ig_source_cart_catalog import _prepared_catalog_rows, SourceCartCatalogReadError
                def no_readback_reads(execute, sql, params, many, context):
                    raise ObservationError("configuration_readback_invalid")
                with connection.execute_wrapper(no_readback_reads):
                    if not isinstance(source_cart, dict) or source_cart.get("status") != "captured" or not source_cart.get("coverage_complete"):
                        raise ObservationError("configuration_readback_invalid")
                    try:
                        products, variants = _prepared_catalog_rows(source_cart, _catalog_readback,
                            {target["product_id"] for target in canonical if target})
                    except SourceCartCatalogReadError:
                        raise ObservationError("configuration_readback_invalid") from None
                    by_variant = {row.pk: row for row in variants}
                    if any(target and target["color_variant_id"] not in by_variant for target in canonical):
                        raise ObservationError("configuration_readback_invalid")
                    before = _read_configurations(canonical, prepared=({row.pk: row for row in products}, by_variant))
                    if any(row and row.get("status") == "unknown" for row in before):
                        raise ObservationError("configuration_readback_invalid")
                # A supplied but invalid private snapshot is never repaired by
                # reconstructing another owner or silently doing fresh reads.
                after = _read_configurations(canonical)
            else:
                before, after = _read_configurations(canonical), _read_configurations(canonical)
            if before != after:
                raise ObservationError("inventory_snapshot_changed")
            return [deepcopy(failures[index] if target is None else before[index]) for index, target in enumerate(canonical)]
    except ObservationError as exc:
        reason = str(exc) if str(exc) in REASONS else "configuration_capture_incomplete"
    except (DatabaseError, TypeError, ValueError, KeyError, AttributeError, ArithmeticError):
        reason = "configuration_capture_incomplete"
    return [deepcopy(failures[index]) if target is None else _configuration_failure(target, reason, whole_capture=True)
        for index, target in enumerate(canonical)]


def observe_configuration(target: dict, *, now=None) -> dict:
    """Single-target compatibility API using the same batch proof and digest."""
    return observe_configurations([target], now=now)[0]


def resolve_source_interest(preferences: dict, *, now=None) -> RestockAvailability:
    """Resolve only exact source values; source ownership remains with caller."""
    now = now or timezone.now()
    if not isinstance(now, datetime) or timezone.is_naive(now):
        raise ValueError("An aware observation time is required")
    values = preferences.get("values") if isinstance(preferences, dict) else None
    if not isinstance(values, dict) or type(values.get("product_id")) is not int:
        return _result(reason="source_interest_incomplete", now=now)
    try:
        fit = values.get("fit_option_code")
        options = values.get("option_values") or {}
        if not isinstance(options, dict) or not isinstance(fit, str):
            raise ObservationError("source_interest_incomplete")
        options = deepcopy(options)
        if "fit" not in options:
            options["fit"] = fit
        _target({"product_id": values["product_id"], "color_variant_id": 1, "size": values.get("size"),
            "fit_option_code": fit, "option_values": options, "quantity": values.get("quantity")})
    except ObservationError as exc:
        return _result(reason=str(exc), now=now)
    resolved_target = {}
    def resolve():
        from storefront.models import Product, ProductStatus
        from productcolors.models import ProductColorVariant
        from management.services.ig_source_cart_catalog import _color_matches
        product = Product.objects.filter(pk=values["product_id"], status=ProductStatus.PUBLISHED).first()
        if product is None:
            raise ObservationError("product_unavailable")
        variants = list(ProductColorVariant.objects.filter(product_id=product.pk).select_related("color").order_by("pk")[:MAX_VARIANTS + 1])
        if len(variants) > MAX_VARIANTS:
            raise ObservationError("inventory_variant_bound")
        wanted = values.get("color")
        if wanted is not None and (not isinstance(wanted, str) or not 0 < len(wanted) <= 80):
            raise ObservationError("source_interest_incomplete")
        identity = values.get("color_variant_id")
        if identity is not None:
            if type(identity) is not int:
                raise ObservationError("source_interest_incomplete")
            matches = [row for row in variants if row.pk == identity and (not wanted or _color_matches(wanted, row))]
        else:
            matches = [row for row in variants if _color_matches(wanted, row)] if wanted else variants
        if len(matches) != 1:
            raise ObservationError("source_color_ambiguous")
        target = _target({"product_id": product.pk, "color_variant_id": matches[0].pk,
            "size": values.get("size"), "fit_option_code": fit, "option_values": options, "quantity": values.get("quantity")})
        resolved_target.update(target)
        before, after = _read(target, now), _read(target, now)
        if _digest(before) != _digest(after):
            raise ObservationError("inventory_snapshot_changed")
        final_variants = list(ProductColorVariant.objects.filter(product_id=product.pk).select_related("color").order_by("pk")[:MAX_VARIANTS + 1])
        def identity_rows(rows):
            return [(row.pk, row.product_id, row.color_id, row.color.name) for row in rows]
        if identity_rows(variants) != identity_rows(final_variants):
            raise ObservationError("inventory_snapshot_changed")
        return _result(target=target, now=now, **before)
    return _bounded(resolve, target=resolved_target, now=now)
