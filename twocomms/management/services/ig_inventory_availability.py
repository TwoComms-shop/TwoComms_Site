"""Exact retail inventory availability, independently of manager fulfilment facts.

This is a read-only observation of configured stock after protected commitments.
It grants only positive availability for its exact current source cart. It is
neither a warehouse recount nor reservation, purchase, payment or timing authority.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import hmac
import json
import re

from django.conf import settings
from django.db import DatabaseError, connection
from django.utils import timezone

SCHEMA = "ig-retail-inventory-availability.v1"
SLOT = "inventory.availability"
AUTHORITY = "inventory_available"
MAX_LINES = 16
MAX_QUERIES = 96


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _sign(value):
    return hmac.new(str(settings.SECRET_KEY).encode(), (SCHEMA + ":" + _json(value)).encode(), hashlib.sha256).hexdigest()


def verified_inventory_capture(value):
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("authority") != AUTHORITY:
        return False
    payload = {key: item for key, item in value.items() if key not in {"digest", "signature"}}
    try:
        return (hmac.compare_digest(str(value.get("digest", "")), _digest(payload))
            and hmac.compare_digest(str(value.get("signature", "")), _sign(payload)))
    except (TypeError, ValueError, OverflowError):
        return False


def inventory_capture_fingerprint(value):
    return _digest({key: value.get(key) for key in ("schema", "authority", "status", "client_id", "scope",
        "source_cart_capture_digest", "source_binding", "inventory_digest", "lines", "owned_checkout")})


def owned_inventory_reference(revision, capture):
    return {"revision_id": revision.pk, "generation_proposal_digest": revision.generation_proposal_digest,
        "parent_capture_digest": capture["owned_checkout"]["parent_capture"]["digest"],
        "proposal_id": capture["owned_checkout"]["proposal_id"],
        "inventory_fingerprint": inventory_capture_fingerprint(capture)}


def resolve_inventory_reference(client_id, reference):
    """Reproduce an exact frozen post-checkout witness from native artifacts."""
    from management.models import IgCustomerTurnRevision
    from management.services.ig_revision_authority import CLAIM_INVENTORY_AVAILABILITY
    keys = {"revision_id", "generation_proposal_digest", "parent_capture_digest", "proposal_id", "inventory_fingerprint"}
    if not isinstance(reference, dict) or set(reference) != keys:
        return None
    revision = IgCustomerTurnRevision.objects.filter(pk=reference["revision_id"], client_id=client_id).first()
    if (not revision or not revision.generation_proposal_digest
        or revision.generation_proposal_digest != reference["generation_proposal_digest"]
        or _digest(revision.generation_proposal) != revision.generation_proposal_digest):
        return None
    rows = [row for row in (revision.generation_proposal.get("authority") or {}).get("fact_bindings") or []
        if row.get("claim") == CLAIM_INVENTORY_AVAILABILITY]
    if len(rows) != 1:
        return None
    parent = (rows[0].get("selector") or {}).get("inventory_availability_capture")
    if not verified_inventory_capture(parent) or parent.get("digest") != reference["parent_capture_digest"]:
        return None
    capture = capture_owned_checkout_inventory(client_id, parent, proposal_id=reference["proposal_id"])
    if not verified_inventory_capture(capture) or inventory_capture_fingerprint(capture) != reference["inventory_fingerprint"]:
        return None
    return capture


class InventoryCaptureError(ValueError):
    pass


def inventory_source_for_variant(variant):
    """Request-local policy lookup; warehouse stock supersedes ledger size caps."""
    if not hasattr(variant, "_ig_inventory_source"):
        from product_catalog.models import ProductInventoryPolicy
        variant._ig_inventory_policy = ProductInventoryPolicy.objects.filter(product_id=variant.product_id).first()
        variant._ig_inventory_source = variant._ig_inventory_policy.source if variant._ig_inventory_policy else "catalog_variant"
    return variant._ig_inventory_source


def _targets(cart, *, prepared_variants=None):
    """Resolve source-owned colour wishes; never borrow an old selected SKU."""
    from productcolors.models import ProductColorVariant
    from management.services.ig_response_cart_plan import source_line_preferences
    from management.services.ig_restock_subscriptions import _quantity_basis
    from management.services.ig_restock_availability import _target, ObservationError
    from management.services.ig_source_cart_catalog import _color_matches
    preferences = []
    for row in cart["lines"]:
        preference = source_line_preferences(cart, row)
        preferences.append(_quantity_basis(preference, row) if preference else {})
    def exact(values):
        return (type(values.get("product_id")) is int and isinstance(values.get("size"), str) and values["size"]
            and isinstance(values.get("fit_option_code"), str) and values["fit_option_code"]
            and type(values.get("quantity")) is int and 1 <= values["quantity"] <= 99)
    products = {row.get("values", {}).get("product_id") for row in preferences if exact(row.get("values") or {})}
    products = {value for value in products if type(value) is int and value > 0}
    variants = ([variant for variant in prepared_variants if variant.product_id in products] if prepared_variants is not None
        else list(ProductColorVariant.objects.filter(product_id__in=products).select_related("color").order_by("pk")[:129]))
    if len(variants) > 128:
        raise InventoryCaptureError("inventory_variant_bound")
    entries = []
    for row, preference in zip(cart["lines"], preferences):
        values = preference.get("values") or {}
        entry = {"line_id": row["line_id"], "recipient_id": row.get("recipient_id", "self"),
            "target": {}, "status": "selection_incomplete", "reason": "source_interest_incomplete"}
        matches = [variant for variant in variants if exact(values) and variant.product_id == values.get("product_id")
            and (not values.get("color") or _color_matches(values["color"], variant))]
        if len(matches) == 1:
            fit = values.get("fit_option_code")
            options = dict(values.get("option_values") or {})
            if fit:
                options.setdefault("fit", fit)
            try:
                entry["target"] = _target({"product_id": values.get("product_id"), "color_variant_id": matches[0].pk,
                    "size": values.get("size"), "fit_option_code": fit, "option_values": options,
                    "quantity": values.get("quantity")})
                entry["status"] = "check_required"
            except ObservationError as exc:
                entry["reason"] = str(exc)
        elif exact(values):
            entry["status"] = "check_required"
            entry["reason"] = "source_color_ambiguous"
        entries.append(entry)
    return entries


def _pre_owned_pool(pool):
    value = deepcopy(pool)
    ids = set(value.get("owned_hold_ids") or [])
    value["reservations"] = [row for row in value.get("reservations") or [] if row["id"] not in ids]
    value["protected_before_owned"] = value.get("protected", 0)
    value["owned_hold_ids"] = []
    return value


def _read_inventory(entries, configurations, now, *, prepared=None, owned_hold_ids=()):
    """Use the same allocation identities and protection rules as stock sync."""
    from management.services import ig_fulfillment_stock_sync as stock
    owned_hold_ids = set(owned_hold_ids)
    pools, result, allocations, warehouse_mappings = {}, [], {}, {}
    def allocate(target):
        exact = _json({key: value for key, value in target.items() if key != "quantity"})
        if exact in allocations:
            return deepcopy(allocations[exact])
        mapping_key = (target["product_id"], target["color_variant_id"], target["fit_option_code"], _json(target["option_values"]))
        prior = warehouse_mappings.get(mapping_key)
        if prior is None:
            inputs = None
            variant = (prepared[1].get(target["color_variant_id"]) if prepared else None)
            if variant is not None:
                inputs = {"variant": variant, "rules": list(variant.product_catalog_size_rules.all())}
                if hasattr(variant, "_ig_inventory_policy"):
                    inputs["policy"] = variant._ig_inventory_policy
            value = stock._allocation(target, lock=False, _prepared=inputs)
        else:
            # Same exact catalogue/mapping, another size: retain the shared
            # native allocation identity and read only its own rule/stock row.
            from product_catalog.models import VariantSizeRule
            from warehouse.models import StockItem
            rules = list(VariantSizeRule.objects.filter(variant_id=target["color_variant_id"],
                size__iexact=target["size"], fit_code__in=["", target["fit_option_code"]]).order_by("pk")[:3])
            if len(rules) > 2:
                raise stock.StockSyncError("inventory_mapping_ambiguous")
            rule = next((row for row in rules if row.fit_code == target["fit_option_code"]), None)
            rule = rule or next((row for row in rules if not row.fit_code), None)
            if rule and not rule.is_enabled:
                raise stock.StockSyncError("disabled_size_requires_editor")
            matches = list(StockItem.objects.filter(subcategory_id=prior["mapping"]["storage_subcategory_id"],
                color_id=prior["variant"]["color_id"], size__iexact=target["size"]).order_by("pk")[:2])
            if len(matches) != 1:
                raise stock.StockSyncError("inventory_stock_identity_unknown")
            item = matches[0]
            value = deepcopy(prior)
            value.update(pool_id=f"warehouse:{item.pk}", identity=item.pk, total=item.quantity,
                rules=stock._primitive([{"id": row.pk, "fit_code": row.fit_code, "size": row.size,
                    "is_enabled": row.is_enabled, "stock": row.stock, "updated_at": row.updated_at} for row in rules]),
                stock=stock._primitive({"id": item.pk, "subcategory_id": item.subcategory_id, "color_id": item.color_id,
                    "size": item.size, "quantity": item.quantity, "updated_at": item.updated_at}))
        if value["source"] == "warehouse":
            warehouse_mappings[mapping_key] = value
        allocations[exact] = value
        return deepcopy(value)
    for entry, configuration in zip(entries, configurations):
        row = deepcopy(entry)
        row.update(configuration_digest=configuration.get("configuration_digest", ""),
            labels=deepcopy(configuration.get("labels") or {}))
        if not row["target"] or configuration.get("status") != "valid":
            if row["target"]:
                row["reason"] = configuration.get("reason") or "configuration_unavailable"
            result.append(row)
            continue
        try:
            allocation = allocate(row["target"])
        except stock.StockSyncError as exc:
            row["reason"] = exc.reason
            result.append(row)
            continue
        pool_id = allocation["pool_id"]
        row.update(pool_id=pool_id, allocation=allocation)
        pool = pools.setdefault(pool_id, {"pool_id": pool_id, "source": allocation["source"],
            "identity": allocation["identity"], "before_total": allocation["total"],
            "required": 0, "size_counts": {}, "line_ids": []})
        if pool["before_total"] != allocation["total"]:
            raise InventoryCaptureError("inventory_snapshot_changed")
        pool["required"] += row["target"]["quantity"]
        pool["line_ids"].append(row["line_id"])
        if allocation["source"] == "catalog_variant":
            key = str(allocation["size_rule_id"])
            size = pool["size_counts"].setdefault(key, {"before_total": allocation["size_total"], "required": 0})
            if size["before_total"] != allocation["size_total"]:
                raise InventoryCaptureError("inventory_snapshot_changed")
            size["required"] += row["target"]["quantity"]
        result.append(row)
    for pool in pools.values():
        try:
            pool["protected"], pool["reservations"] = stock._reservations(pool["source"], pool["identity"], now, lock=False)
            pool["protected_before_owned"] = pool["protected"]
            owned = [row for row in pool["reservations"] if row["id"] in owned_hold_ids]
            reused = sum(row["quantity"] - (row.get("fulfilled_quantity", 0) if pool["source"] == "warehouse" else 0) for row in owned)
            pool["protected"] -= reused
            if pool["protected"] < 0:
                raise InventoryCaptureError("inventory_owned_hold_changed")
            pool["owned_hold_ids"] = sorted(row["id"] for row in owned)
            if pool["source"] == "catalog_variant":
                protection, scope = stock._size_protection(pool, owned_hold_ids, now, lock=False)
                pool["protected_size_scope"] = scope
                for key, size in pool["size_counts"].items():
                    size["protected"] = protection.get(key, 0)
            pool["free_quantity"] = max(0, pool["before_total"] - pool["protected"])
            pool["status"] = "available" if pool["free_quantity"] >= pool["required"] else "check_required"
            pool["reason"] = "inventory_available" if pool["status"] == "available" else "insufficient_free_stock"
        except stock.StockSyncError as exc:
            pool.update(status="check_required", reason=exc.reason)
    for row in result:
        if not row.get("pool_id"):
            continue
        pool = pools[row["pool_id"]]
        row.update(status=pool["status"], reason=pool["reason"], free_quantity=pool.get("free_quantity", 0))
        if pool["source"] == "catalog_variant" and row["status"] == "available":
            size = pool["size_counts"][str(row["allocation"]["size_rule_id"])]
            count = size["before_total"]
            if type(count) is not int or count < 0:
                row.update(status="check_required", reason="exact_size_stock_untracked")
            elif count - size.get("protected", 0) < size["required"]:
                row.update(status="check_required", reason="insufficient_size_stock")
            else:
                row["free_quantity"] = min(row["free_quantity"], count - size.get("protected", 0))
            # A consumption after the editor's exact-size count makes that
            # count stale. Reuse the captured reservation/rule scope, with no
            # invented size allocation from a reservation's primary item.
            if row["status"] == "available":
                from django.utils.dateparse import parse_datetime
                rule_id = row["allocation"]["size_rule_id"]
                rule = next(item for item in pool["protected_size_scope"]["rules"] if item["id"] == rule_id)
                stamp = parse_datetime(rule["updated_at"])
                for reservation in pool["reservations"]:
                    if reservation["state"] not in {"fulfilled", "consumed"}:
                        continue
                    times = [parse_datetime(reservation[key]) for key in ("fulfilled_at", "consumed_at") if reservation.get(key)]
                    if not times or any(value > stamp for value in times):
                        row.update(status="check_required", reason="stale_catalog_size_count")
                        break
        row["inventory_digest"] = _digest({"allocation": row["allocation"], "pool": pool})
        if owned_hold_ids:
            row["pre_owned_inventory_digest"] = _digest({"allocation": row["allocation"], "pool": _pre_owned_pool(pool)})
        row.pop("allocation", None)
    return result, sorted(pools.values(), key=lambda row: row["pool_id"])


def capture_inventory_availability(client_id, *, current_source_cart=None, now=None, max_read_queries=None, _catalog_readback=None, _owned_hold_ids=()):
    """Read exact current cart and two agreeing inventory/configuration fences.

    A supplied cart must come from the caller's native source capture; that
    caller retains its existing before/after source and permission fences.
    Standalone callers receive a fresh native cart and recheck it afterwards.
    """
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_restock_availability import _read_configurations
    now = now or timezone.now()
    if not isinstance(now, datetime) or timezone.is_naive(now):
        raise ValueError("An aware observation time is required")
    cart = current_source_cart
    payload = {"schema": SCHEMA, "authority": AUTHORITY, "status": "unknown", "reason": "inventory_capture_unavailable",
        "client_id": client_id, "captured_at": now.isoformat(), "lines": [], "scope": {},
        "source_cart_capture_digest": "", "inventory_digest": "", "source_binding": {}}
    reads = 0
    limit = min(MAX_QUERIES, max(0, max_read_queries)) if type(max_read_queries) is int else MAX_QUERIES
    def readonly(execute, sql, params, many, context):
        nonlocal reads
        if not sql.lstrip().upper().startswith("SELECT") or re.search(r"FOR\s+UPDATE|LOCK\s+IN\s+SHARE", sql, re.I):
            raise InventoryCaptureError("inventory_read_not_readonly")
        if reads >= limit:
            raise InventoryCaptureError("inventory_query_bound")
        reads += 1
        return execute(sql, params, many, context)
    try:
        if limit == 0:
            raise InventoryCaptureError("inventory_query_bound")
        with connection.execute_wrapper(readonly):
            if cart is None:
                if _catalog_readback is None:
                    _catalog_readback = {}
                cart = capture_current_selection_lines(client_id, now=now, _catalog_readback=_catalog_readback)
            if (not isinstance(cart, dict) or cart.get("status") != "captured" or not cart.get("coverage_complete")
                or cart.get("scope", {}).get("client_id") != client_id or not cart.get("capture_digest")
                or not 0 < len(cart.get("lines") or []) <= MAX_LINES):
                raise InventoryCaptureError("inventory_source_cart_unavailable")
            payload.update(scope=deepcopy(cart["scope"]), source_cart_capture_digest=cart["capture_digest"],
                source_binding={"session_id": cart.get("session_id"), "generation": cart.get("generation"),
                    "selection_revision": cart.get("selection_revision"), "fence": deepcopy(cart.get("fence") or {})})
            prepared = None
            if _catalog_readback is not None:
                from management.services.ig_source_cart_catalog import _prepared_catalog_rows, SourceCartCatalogReadError
                product_ids = {row.get("fields", {}).get("product_id", {}).get("value") for row in cart["lines"]}
                product_ids = {identity for identity in product_ids if type(identity) is int and identity > 0}
                try:
                    products, variants = _prepared_catalog_rows(cart, _catalog_readback, product_ids)
                except SourceCartCatalogReadError as exc:
                    raise InventoryCaptureError("inventory_catalog_readback_invalid") from exc
                prepared = ({product.pk: product for product in products}, {variant.pk: variant for variant in variants})
            entries = _targets(cart, prepared_variants=list(prepared[1].values()) if prepared else None)
            targets = [row["target"] or None for row in entries]
            configurations = _read_configurations(targets, prepared=prepared)
            configurations = [row or {} for row in configurations]
            first, pools = _read_inventory(entries, configurations, now, prepared=prepared, owned_hold_ids=_owned_hold_ids)
            second, next_pools = _read_inventory(entries, configurations, now, owned_hold_ids=_owned_hold_ids)
            final_configurations = [row or {} for row in _read_configurations(targets)]
            if first != second or pools != next_pools or configurations != final_configurations:
                raise InventoryCaptureError("inventory_snapshot_changed")
            if current_source_cart is None:
                from management.models import IgClient, InstagramBotMessage
                from management.services.ig_admin_state_capture import _owner_fence, _namespace, _source_watermark
                from management.services.ig_commerce_projection import _source_fence_row
                fence = cart.get("fence") or {}
                owner = _owner_fence(client_id)
                sources = list(InstagramBotMessage.objects.filter(pk__in=fence.get("source_ids") or []).select_related("client"))
                source_digest = _digest({row.pk: _source_fence_row(row) for row in sources})
                namespace = _namespace()
                if (not owner or _digest(owner) != fence.get("owner_digest") or source_digest != fence.get("source_digest")
                    or namespace != fence.get("namespace")
                    or _source_watermark(cart["scope"], namespace, owner["owner"]["igsid"], now, customer_only=True) != cart.get("source_watermark")):
                    raise InventoryCaptureError("inventory_source_cart_changed")
            payload.update(status="captured", reason="", lines=first, inventory_digest=_digest(pools))
            if _owned_hold_ids:
                payload["pre_owned_inventory_digest"] = _digest([_pre_owned_pool(pool) for pool in pools])
    except InventoryCaptureError as exc:
        payload.update(status="unknown", reason=str(exc), lines=[])
    except (DatabaseError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        payload.update(status="unknown", reason="inventory_capture_unavailable", lines=[])
    payload["query_count"] = reads
    payload["digest"], payload["signature"] = _digest(payload), _sign(payload)
    return payload


def inventory_prompt_value(value):
    """Expose positive exact facts and scoped checks; keep internal fences private."""
    if not verified_inventory_capture(value):
        return {"status": "unknown", "lines": []}
    return {"status": value["status"], "authority": AUTHORITY,
        "guidance": "Positive exact requested quantities may continue normally. Check-required means ask the manager, never that goods are absent. No timing, payment or reservation authority.",
        "lines": [{key: deepcopy(row[key]) for key in ("line_id", "recipient_id", "target", "labels", "status", "reason") if key in row}
            for row in value["lines"]]}


def capture_inventory_context(client, *, boundary, captured_at, current_source_cart=None, max_read_queries=None, _catalog_readback=None):
    from management.services.ig_client_state_card import CAPTURE_SCOPE_KEYS, SCOPE_KEYS
    purchase = ("selection_session_id", "selection_generation", "selection_revision", "source_cart_capture_digest")
    value = capture_inventory_availability(getattr(client, "pk", client), current_source_cart=current_source_cart,
        now=captured_at, max_read_queries=max_read_queries, _catalog_readback=_catalog_readback)
    provisional = value["status"] == "captured" and all(row.get("status") == "selection_incomplete" for row in value["lines"])
    valid = (value["status"] == "captured" and not provisional
        and value["source_cart_capture_digest"] == boundary.get("source_cart_capture_digest"))
    refs = {}
    for row in (current_source_cart or {}).get("lines") or []:
        for field in row.get("fields", {}).values():
            source = field.get("source") or {}
            identity = source.get("source_message_id")
            if type(identity) is int and identity > 0:
                refs[identity] = {"kind": "message", "id": identity, "event_at": source.get("observed_at"),
                    "digest": source.get("source_digest") or ""}
    if not refs and valid:
        refs[value["client_id"]] = {"kind": "source_cart_capture", "id": value["client_id"],
            "event_at": captured_at.isoformat(), "digest": value["source_cart_capture_digest"]}
    return {"value": value if valid else None, "status": "confirmed" if valid else "unknown",
        "authority": AUTHORITY if valid else "none", "source_refs": list(refs.values()),
        "scope": {key: boundary.get(key) for key in SCOPE_KEYS},
        "capture_scope": {key: boundary.get(key) for key in (*CAPTURE_SCOPE_KEYS, *purchase)},
        "observed_at": captured_at.isoformat(), "validity": "valid" if valid else "unknown",
        "mandatory": valid and any(row.get("status") == "available" for row in value["lines"]),
        "omission_reason": "" if valid else "inventory_selection_incomplete" if provisional else value.get("reason") or "inventory_scope_changed"}


def captured_inventory_projection(captured_client_state):
    payload = captured_client_state.as_dict() if hasattr(captured_client_state, "as_dict") else captured_client_state
    if not isinstance(payload, dict) or payload.get("status") != "captured":
        return None
    slot = (payload.get("slots") or {}).get(SLOT) or {}
    if (slot.get("status") != "confirmed" or slot.get("authority") != AUTHORITY
        or slot.get("validity") != "valid" or slot.get("omission_reason")):
        return None
    value = slot.get("value")
    return value if verified_inventory_capture(value) else None


def validate_inventory_capture(client_id, capture, *, now=None, current_source_cart=None):
    """Final send hook: compare source/configuration/pools, never refresh old facts."""
    if not verified_inventory_capture(capture) or capture.get("client_id") != client_id:
        return "inventory_availability_unverified"
    ownership = capture.get("owned_checkout")
    if ownership:
        current = capture_owned_checkout_inventory(client_id, ownership.get("parent_capture"),
            proposal_id=ownership.get("proposal_id"), now=now)
    else:
        current = capture_inventory_availability(client_id, current_source_cart=current_source_cart, now=now)
    if current.get("status") != "captured":
        return current.get("reason") or "inventory_availability_unavailable"
    keys = ("source_cart_capture_digest", "source_binding", "inventory_digest", "lines", "owned_checkout")
    if any(current.get(key) != capture.get(key) for key in keys):
        return "inventory_availability_changed"
    return ""


def _owned_checkout_witness(client_id, parent, proposal_id, cart, now):
    """Exact native quote-to-item-to-pool proof; never all holds of a client."""
    from management.models import IgCheckoutProposal, IgCheckoutInventoryReservation as Reservation
    from management.services.ig_checkout_cart_provenance import capture_proposal_provenance, CartProvenanceError
    from management.services.ig_revision_cart_binding import same_checkout_source_capture
    proposal = IgCheckoutProposal.objects.select_related("client", "deal", "commercial_episode").filter(
        pk=proposal_id, client_id=client_id).first()
    if (not proposal or not proposal.deal or not proposal.commercial_episode
        or proposal.client.current_commercial_episode_id != proposal.commercial_episode_id
        or proposal.commercial_episode.deal_id != proposal.deal_id
        or proposal.deal.active_checkout_proposal_id != proposal.pk
        or proposal.commercial_episode.intended_order_id or proposal.deal.order_id
        or proposal.status not in {"ready", "viewed"} or proposal.expires_at <= now):
        raise InventoryCaptureError("inventory_owned_checkout_changed")
    revision = proposal.revisions.filter(revision=proposal.revision).first()
    snapshot = revision.snapshot if revision is not None else {}
    original = snapshot.get("source_cart_capture") or {}
    if (original.get("capture_digest") != parent.get("source_cart_capture_digest")
        or not same_checkout_source_capture(original, cart)):
        raise InventoryCaptureError("inventory_owned_checkout_source_changed")
    items = list(proposal.items.order_by("position", "id"))
    raw = [{"proposal_item_id": item.pk, "position": item.position, "product_id": item.product_id,
        "qty": item.quantity, "size": item.size or "", "fit_option_code": item.fit_code or "",
        "color_variant_id": item.color_variant_id, "option_values": item.option_values or {},
        "unit_price": str(item.quoted_unit_price), "line_total": str(item.quoted_line_total)} for item in items]
    owner = {"proposal_id": str(proposal.public_id), "proposal_pk": proposal.pk,
        "revision_id": revision.pk if revision else None, "revision": proposal.revision,
        "client_id": client_id, "deal_id": proposal.deal_id, "episode_id": proposal.commercial_episode_id}
    try:
        provenance = capture_proposal_provenance(revision_snapshot=snapshot, owner=owner, proposal_items=raw)
    except CartProvenanceError as exc:
        raise InventoryCaptureError("inventory_owned_checkout_quote_changed") from exc
    if provenance.get("status") != "bound":
        raise InventoryCaptureError("inventory_owned_checkout_quote_unbound")
    rows = provenance["binding"]["quote_line_map"]
    by_line = {row["line_id"]: row for row in parent["lines"]}
    if len(rows) != len(by_line) or len(items) != len(rows):
        raise InventoryCaptureError("inventory_owned_checkout_line_scope_changed")
    pools = {}
    for mapping, item in zip(rows, items):
        line = by_line.get(mapping["line_id"])
        target = {"product_id": item.product_id, "color_variant_id": item.color_variant_id, "size": item.size,
            "fit_option_code": item.fit_code, "option_values": item.option_values or {}, "quantity": item.quantity}
        if not line or line.get("status") != "available" or line.get("target") != target or not line.get("pool_id"):
            raise InventoryCaptureError("inventory_owned_checkout_line_scope_changed")
        pool = pools.setdefault(line["pool_id"], {"quantity": 0, "item_ids": []})
        pool["quantity"] += item.quantity
        pool["item_ids"].append(item.pk)
    holds = list(Reservation.objects.filter(proposal=proposal, state=Reservation.State.ACTIVE,
        expires_at__gt=now).order_by("pk").values("id", "proposal_id", "invoice_generation_id", "allocation_source",
            "stock_item_id", "color_variant_id", "quantity", "fulfilled_quantity", "line_ids", "expires_at",
            "allocation_key", "reservation_fingerprint", "updated_at"))
    if len(holds) != len(pools):
        raise InventoryCaptureError("inventory_owned_hold_scope_changed")
    seen = set()
    for hold in holds:
        pool_id = (f"warehouse:{hold['stock_item_id']}" if hold["allocation_source"] == "warehouse"
            else f"catalog_variant:{hold['color_variant_id']}" if hold["allocation_source"] in {"catalog_variant", ""} else "")
        expected = pools.get(pool_id)
        if (not expected or pool_id in seen or hold["invoice_generation_id"] is not None
            or hold["fulfilled_quantity"] != 0 or hold["quantity"] != expected["quantity"]
            or sorted(hold["line_ids"] or []) != sorted(expected["item_ids"])):
            raise InventoryCaptureError("inventory_owned_hold_scope_changed")
        seen.add(pool_id)
    return {"proposal_id": proposal.pk, "revision_id": revision.pk, "revision": proposal.revision,
        "provenance_digest": _digest(provenance), "holds": json.loads(_json(holds))}


def capture_owned_checkout_inventory(client_id, parent, *, proposal_id, now=None):
    """Rebind only an exact backend-created checkout's own finite live holds."""
    from management.services.ig_commerce_projection import capture_current_selection_lines
    now = now or timezone.now()
    if (not verified_inventory_capture(parent) or parent.get("client_id") != client_id
        or parent.get("owned_checkout") or not parent.get("lines")
        or any(row.get("status") != "available" for row in parent["lines"])):
        return {"status": "unknown", "reason": "inventory_owned_parent_unverified"}
    try:
        readback = {}
        cart = capture_current_selection_lines(client_id, now=now, _catalog_readback=readback)
        before = _owned_checkout_witness(client_id, parent, proposal_id, cart, now)
        captured = capture_inventory_availability(client_id, current_source_cart=cart, now=now,
            _owned_hold_ids=[row["id"] for row in before["holds"]], _catalog_readback=readback)
        after = _owned_checkout_witness(client_id, parent, proposal_id, cart, now)
        current_cart = capture_current_selection_lines(client_id, now=now)
        if before != after or current_cart.get("capture_digest") != cart.get("capture_digest"):
            raise InventoryCaptureError("inventory_owned_checkout_changed")
        if captured.get("status") != "captured":
            return captured
        if any(row.get("status") != "available" for row in captured["lines"]):
            raise InventoryCaptureError("inventory_owned_stock_changed")
        parent_lines = {row["line_id"]: row for row in parent["lines"]}
        if (captured.get("pre_owned_inventory_digest") != parent.get("inventory_digest")
            or set(parent_lines) != {row["line_id"] for row in captured["lines"]}
            or any(row.get("pre_owned_inventory_digest") != parent_lines[row["line_id"]].get("inventory_digest")
                or any(row.get(key) != parent_lines[row["line_id"]].get(key)
                    for key in ("target", "configuration_digest", "labels", "pool_id")) for row in captured["lines"])):
            raise InventoryCaptureError("inventory_owned_parent_changed")
        payload = {key: value for key, value in captured.items() if key not in {"digest", "signature"}}
        payload["owned_checkout"] = {**before, "parent_capture": deepcopy(parent)}
        payload["digest"], payload["signature"] = _digest(payload), _sign(payload)
        return payload
    except InventoryCaptureError as exc:
        return {"status": "unknown", "reason": str(exc)}
    except (DatabaseError, TypeError, ValueError, KeyError, AttributeError):
        return {"status": "unknown", "reason": "inventory_owned_checkout_unavailable"}


def revision_inventory_capture(revision, *, fact_bindings=None):
    """Read only the immutable signed inventory fact bound to this proposal."""
    from management.services.ig_revision_authority import CLAIM_INVENTORY_AVAILABILITY
    bindings = (fact_bindings if fact_bindings is not None else
        ((getattr(revision, "generation_proposal", None) or {}).get("authority") or {}).get("fact_bindings") or [])
    rows = [row for row in bindings if row.get("claim") == CLAIM_INVENTORY_AVAILABILITY]
    if len(rows) != 1:
        return None
    selector = rows[0].get("selector") or {}
    value = selector.get("inventory_availability_capture")
    if value is None and selector.get("inventory_availability_reference"):
        value = resolve_inventory_reference(revision.client_id, selector["inventory_availability_reference"])
    return value if verified_inventory_capture(value) and value.get("client_id") == revision.client_id else None


def validate_revision_inventory(revision, *, now=None, fact_bindings=None):
    from management.services.ig_revision_authority import CLAIM_INVENTORY_AVAILABILITY
    bindings = (fact_bindings if fact_bindings is not None else
        ((getattr(revision, "generation_proposal", None) or {}).get("authority") or {}).get("fact_bindings") or [])
    if not any(row.get("claim") == CLAIM_INVENTORY_AVAILABILITY for row in bindings):
        return ""
    capture = revision_inventory_capture(revision, fact_bindings=bindings)
    return validate_inventory_capture(revision.client_id, capture, now=now)
