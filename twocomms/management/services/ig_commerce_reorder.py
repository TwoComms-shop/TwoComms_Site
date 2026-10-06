"""Read an exact owned historical position; never infer a latest purchase.

Admission reprices the actual configuration. Replay proves the old immutable
binding/configuration and current customer intent; it grants no stock or price
authority. Neither path copies money, contacts, shipping or payment state.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json


class ReorderUnavailable(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _item_configuration(item):
    return {"product_id": item.product_id, "color_variant_id": item.color_variant_id,
        "size": item.size, "fit_option_code": item.fit_option_code,
        "option_values": item.option_values, "qty": item.qty, "is_custom": item.is_custom}


def _origin(client, request, *, exact=None):
    from management.services.ig_commercial_episodes import resolve_client_order, OrderResolutionError, _client_order_queryset
    from management.models import IgCheckoutRevision
    from orders.models import OrderItem
    from management.services.ig_checkout_cart_provenance import _binding, _owner, _configuration, CartProvenanceError
    try:
        if exact:
            order = _client_order_queryset(client).filter(pk=exact["order_id"]).first()
            if order is None:
                raise ReorderUnavailable("reorder_order_not_owned")
        else:
            order = resolve_client_order(client, request.historical_order_reference).order
    except OrderResolutionError as exc:
        raise ReorderUnavailable("reorder_order_ambiguous" if str(getattr(exc, "code", "")) == "ambiguous_order"
            else "reorder_order_unavailable") from exc
    from management.services.ig_commerce_scope_read_model import _order_binding
    if _order_binding(client.pk, order.pk).get("status") != "confirmed":
        raise ReorderUnavailable("reorder_order_not_owned")
    # A created invoice or unpaid order is not a completed prior purchase.
    if exact is None and order.payment_status != "paid":
        raise ReorderUnavailable("existing_order_amendment_or_new")
    items = list(OrderItem.objects.filter(order_id=order.pk).select_related("color_variant__color")
        .order_by("pk")[:17])
    if not items or len(items) > 16:
        raise ReorderUnavailable("reorder_item_ambiguous")
    payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    raw = payload.get("source_cart_provenance")
    binding = None
    item_map = []
    if raw is not None and (not isinstance(raw, dict) or raw.get("status") != "legacy_unknown"):
        try:
            owner = _owner(raw["owner"])
            binding = _binding(raw["binding"], owner)
            revision = IgCheckoutRevision.objects.select_related("proposal").filter(pk=owner["revision_id"]).first()
            if (raw.get("schema") != "ig-checkout-cart-provenance.v1" or raw.get("status") != "bound"
                or raw.get("order_id") != order.pk or owner["client_id"] != client.pk or revision is None
                or revision.proposal_id != owner["proposal_pk"] or revision.revision != owner["revision"]
                or str(revision.proposal.public_id) != owner["proposal_id"]
                or revision.proposal.client_id != client.pk or revision.proposal.deal_id != owner["deal_id"]
                or revision.proposal.commercial_episode_id != owner["episode_id"]
                or revision.snapshot.get("source_cart_binding") != binding):
                raise ReorderUnavailable("reorder_binding_invalid")
            item_map = raw["item_map"]
            if (not isinstance(item_map, list) or len(item_map) != len(items)
                or {row["order_item_id"] for row in item_map} != {item.pk for item in items}):
                raise ReorderUnavailable("reorder_binding_invalid")
            for item in items:
                rows = [row for row in item_map if row["order_item_id"] == item.pk]
                if len(rows) != 1 or rows[0]["configuration"] != _configuration(_item_configuration(item)):
                    raise ReorderUnavailable("reorder_binding_invalid")
        except (CartProvenanceError, KeyError, TypeError, AttributeError, ValueError) as exc:
            if isinstance(exc, ReorderUnavailable):
                raise
            raise ReorderUnavailable("reorder_binding_invalid") from exc
    elif raw is not None and raw != {"schema": "ig-checkout-cart-provenance.v1", "status": "legacy_unknown",
        "reason": "source_cart_never_captured"}:
        raise ReorderUnavailable("reorder_binding_invalid")
    if exact:
        matches = [item for item in items if item.pk == exact["order_item_id"]]
    else:
        matches = items
        if request.historical_item_index is not None:
            index = request.historical_item_index
            matches = [items[index]] if type(index) is int and 0 <= index < len(items) else []
        if request.historical_line_id or request.historical_recipient_id:
            if binding is None:
                raise ReorderUnavailable("reorder_recipient_unknown")
            ids = {row["order_item_id"] for row in item_map
                if (not request.historical_line_id or row["line_id"] == request.historical_line_id)
                and (not request.historical_recipient_id or row["recipient_id"] == request.historical_recipient_id)}
            matches = [item for item in matches if item.pk in ids]
    if len(matches) != 1:
        raise ReorderUnavailable("reorder_item_ambiguous")
    item = matches[0]
    config = _item_configuration(item)
    if (not item.product_id or item.is_custom or not isinstance(item.option_values, dict)
        or set(item.option_values) - {"fit"} or (item.option_values.get("fit") and item.option_values["fit"] != item.fit_option_code)):
        raise ReorderUnavailable("reorder_options_unsupported")
    row = next((row for row in item_map if row["order_item_id"] == item.pk), None)
    selection = next((line["source_selection"] for line in binding["lines"] if line["line_id"] == row["line_id"]), None) if binding else None
    proof = {"schema": "commerce-reorder-origin.v1", "kind": "bound_source" if binding else "owned_order_item",
        "client_id": client.pk, "order_id": order.pk, "order_item_id": item.pk,
        "configuration": config, "configuration_digest": _digest(config),
        "binding_digest": _digest(binding) if binding else "", "line_id": row["line_id"] if row else "",
        "recipient_id": row["recipient_id"] if row else "self"}
    if exact is not None and exact != proof:
        raise ReorderUnavailable("reorder_origin_changed")
    return item, selection, proof


def _source_projection(client, selection, capture):
    from management.services.ig_commerce_projection import _validated_copy_projection
    if not isinstance(selection, dict):
        raise ReorderUnavailable("reorder_copy_source_unverified")
    scope = {key: selection.get(key) for key in ("session_id", "generation", "revision", "episode_id", "line_id",
        "recipient_id", "reset_floor", "head_transition_id", "head_digest", "line_count")}
    result = _validated_copy_projection(client, scope, {**capture, "allow_order_line_copy": True})
    if not result or result.get("cleared"):
        raise ReorderUnavailable("reorder_copy_source_unverified")
    return result


def _projection(client, source, request, *, item, selection, proof, capture):
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_admin_state_capture import _namespace
    floor = capture["reset_floor"] if capture is not None else conversation_route_reset_floor(client.pk)
    namespace = capture["namespace"] if capture is not None else _namespace()
    current = capture["current"] if capture is not None else {"privacy_erasure_started_at": client.privacy_erasure_started_at,
        "igsid": client.igsid}
    if (current["privacy_erasure_started_at"] or source.client_id != client.pk or source.sender_id != current["igsid"]
        or source.role != "user" or source.source != "webhook" or source.status == "failed"
        or source.pk < floor or source.provider_namespace != namespace):
        raise ReorderUnavailable("reorder_source_unverified")
    capture_is_local = capture is None
    if capture_is_local:
        from management.models import IgCommerceSelectionTransition
        from management.services.ig_commerce_projection import _captured_product_ids
        rows = list(IgCommerceSelectionTransition.objects.filter(session__client_id=client.pk, source_message_id__gte=floor)
            .select_related("session", "source_message__commerce_turn_decision").order_by("-pk")[:64])
        ids = _captured_product_ids(rows)
        if ids is None:
            raise ReorderUnavailable("reorder_copy_source_unverified")
        capture = {"current": {"igsid": client.igsid, "privacy_erasure_started_at": None,
            "current_commercial_episode_id": client.current_commercial_episode_id}, "row_lookup": {row.pk: row for row in rows},
            "reset_floor": floor, "namespace": namespace, "catalog_product_ids": ids, "copy_depth": 0}
    if selection:
        result = _source_projection(client, selection, capture)
        values = result.get("values") or {}
        from management.services.ig_source_cart_catalog import _color_matches
        if (values.get("product_id") != item.product_id or values.get("size", "") != item.size
            or values.get("fit_option_code", "") != item.fit_option_code):
            raise ReorderUnavailable("reorder_binding_invalid")
        if item.color_variant_id and not _color_matches(values.get("color"), item.color_variant):
            raise ReorderUnavailable("reorder_binding_invalid")
    else:
        color = str(item.color_variant.color.name) if item.color_variant_id else ""
        values = {"product_id": item.product_id, **({"size": item.size} if item.size else {}),
            **({"fit_option_code": item.fit_option_code} if item.fit_option_code else {}), **({"color": color} if color else {})}
        result = {"values": values, "evidence": {key: {"authority": "validated_selection_action",
            "order_id": proof["order_id"], "order_item_id": item.pk, "configuration_digest": proof["configuration_digest"]}
            for key in values}, "recipient_id": "self"}
    result = deepcopy(result)
    result["line_count"] = 1  # exactly ONE chosen physical position, not the whole historical cart
    result["reorder_origin"] = proof
    if capture_is_local and selection:
        from management.models import InstagramBotMessage
        from management.services.ig_commerce_projection import _source_fence_row
        before = {row.source_message_id: _source_fence_row(row.source_message) for row in capture["row_lookup"].values()}
        after = {row.pk: _source_fence_row(row) for row in InstagramBotMessage.objects.filter(pk__in=before)}
        if before != after:
            raise ReorderUnavailable("reorder_copy_source_unverified")
    return result


def prepare_reorder_origin(client, source, request):
    """Caller owns client/source locks. All refusal precedes episode creation."""
    from django.db import connection
    reads = 0
    def bounded(execute, sql, params, many, context):
        nonlocal reads
        if not sql.lstrip().upper().startswith("SELECT") or reads >= 64:
            raise ReorderUnavailable("reorder_read_bound")
        reads += 1
        return execute(sql, params, many, context)
    with connection.execute_wrapper(bounded):
        return _prepare_reorder_origin(client, source, request)


def _prepare_reorder_origin(client, source, request):
    from management.services.ig_checkout import validate_checkout_items, CheckoutConfigurationError
    from management.services.ig_commerce_turns import parse_turn
    if not request.new_purchase_requested or not any(operation.copy_previous for operation in request.line_operations):
        raise ReorderUnavailable("reorder_source_unverified")
    original = parse_turn(source.text)
    if not any(operation.copy_previous for operation in original.line_operations):
        raise ReorderUnavailable("reorder_source_unverified")
    item, selection, proof = _origin(client, request)
    try:
        quote = validate_checkout_items(client=client, item_specs=[{**proof["configuration"], "qty": 1}],
            evidence={"message_ids": [source.pk]}, allow_promo=False)
        if len(quote.items) != 1 or quote.items[0].catalog_unit_price <= 0:
            raise ReorderUnavailable("reorder_catalog_unavailable")
        if set(quote.items[0].option_values) - {"fit"}:
            raise ReorderUnavailable("reorder_options_unsupported")
    except CheckoutConfigurationError as exc:
        raise ReorderUnavailable("reorder_catalog_unavailable") from exc
    # Detect mutable old item/order ownership changes during catalog reads.
    item, selection, proof = _origin(client, request, exact=proof)
    return _projection(client, source, request, item=item, selection=selection, proof=proof, capture=None)


def validated_reorder_projection(client, source, request, origin, capture):
    """Historical exact receipt validation; never refresh or create an origin."""
    try:
        if not isinstance(origin, dict) or origin.get("schema") != "commerce-reorder-origin.v1":
            return {}
        key = (source.pk, _digest(origin), capture.get("copy_depth", 0))
        cache = capture.setdefault("reorder_cache", {})
        if key in cache:
            return deepcopy(cache[key])
        item, selection, proof = _origin(client, request, exact=origin)
        result = _projection(client, source, request, item=item, selection=selection, proof=proof, capture=capture)
        cache[key] = result
        return deepcopy(result)
    except (ReorderUnavailable, KeyError, TypeError, ValueError, AttributeError):
        return {}
