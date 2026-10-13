"""Explicit absolute inventory recount for a verified assisted catalog subset.

The complete intention remains sealed and unconsumed. This action repairs
accounting records only; ordinary quote/reservation/purchase/payment producers
remain responsible for checkout. Checked approval quantities are never counts.
"""
from copy import deepcopy
import hmac

from django.db import IntegrityError, OperationalError, transaction

from management.services import ig_assisted_commerce as commerce
from management.services import ig_fulfillment_stock_sync as inventory


def _model():
    from management.ig_assisted_commerce_models import IgAssistedCommerceStockSync
    return IgAssistedCommerceStockSync


def _rows(client_id, request_id=None, decision_id=None, *, lock=False):
    Request, Decision, _Execution, _Image = commerce._models()
    requests = Request.objects.filter(client_id=client_id)
    if lock:
        requests = requests.select_for_update()
    request = (requests.filter(pk=commerce._uuid(request_id)).first() if request_id is not None
        else requests.order_by("-created_at", "-version", "pk").first())
    if not request:
        raise inventory.StockSyncError("request_missing")
    decisions = Decision.objects.filter(request=request, client_id=client_id)
    if lock:
        decisions = decisions.select_for_update()
    decision = (decisions.filter(pk=commerce._uuid(decision_id)).first() if decision_id is not None
        else decisions.order_by("-version").first())
    if not decision:
        raise inventory.StockSyncError("approved_decision_missing")
    return request, decision


def _capture(request, decision, *, actor, now, lock=False):
    cap = commerce.load_verified_assisted_decision(request.pk, decision.pk, actor=actor,
        expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac, now=now)
    if cap.proof_scope != "unconsumed" or not cap.lines or len(cap.ready_lines) != len(cap.lines):
        raise inventory.StockSyncError("full_ready_required")
    if _model().objects.filter(decision=decision).exists():
        raise inventory.StockSyncError("already_synced")
    catalog = [line for line in cap.lines if line.item_kind == "catalog_apparel"]
    if not catalog:
        raise inventory.StockSyncError("catalog_lines_missing")
    entries = []
    observations = {line["line_id"]: line for line in cap.source_proof["lines"]}
    for line in catalog:
        if line.supply_basis not in {"accessible_unrecorded", "warehouse_checked"}:
            raise inventory.StockSyncError("catalog_physical_basis_unverified")
        spec = observations[line.line_id].get("catalog_item_spec") or {}
        target = {"product_id": line.product_id, "color_variant_id": line.color_variant_id,
            "size": line.size, "fit_option_code": line.fit_code, "quantity": line.quantity,
            "option_values": line.as_dict()["option_values"]}
        if (not spec or any(target[key] != spec.get(key)
                for key in ("product_id", "color_variant_id", "size", "fit_option_code", "option_values"))):
            raise inventory.StockSyncError("catalog_configuration_not_source_owned")
        entries.append({"line_id": line.line_id, "target": target,
            "label": " · ".join(value for value in (line.title, line.color, line.size, line.fit_code) if value)[:160],
            "configuration_digest": line.line_digest})
    allocations, pools = inventory._capture_inventory_pools(entries, now, lock=lock)
    shortfall = any(pool["before_total"] < pool["minimum_total"]
        or any(type(size["before_total"]) is int and size["before_total"] < size["minimum_total"]
            for size in pool["size_counts"].values()) for pool in pools)
    if not shortfall and not any(line.supply_basis == "accessible_unrecorded" for line in catalog):
        raise inventory.StockSyncError("stock_reconciliation_not_required")
    return cap, inventory._primitive({"request_id": str(request.pk), "request_digest": request.envelope_hmac,
        "decision_id": str(decision.pk), "decision_digest": decision.envelope_hmac,
        "full_capture_digest": request.full_capture_digest, "decision_snapshot_digest": decision.snapshot_digest,
        "settings_id": cap.source_proof["settings_id"],
        "settings_epoch": cap.source_proof["settings_permission_epoch"],
        "client_epoch": request.client_permission_epoch, "cart": cap.current_capture,
        "allocations": allocations, "pools": pools, "requires_sync": shortfall,
        "catalog_metadata": _catalog_metadata(cap)})


def _catalog_metadata(cap):
    """Optimistic fresh legacy descriptors; Product may be MyISAM.

    FOR UPDATE cannot promise protection or rollback of Product metadata.
    This writer never edits it. Stock/size/movement/hold/receipt writes use
    transactional tables, and both this receipt and invoice gates re-read the
    real published identity, configuration and ordinary current price.
    """
    from management.services.ig_source_cart_catalog import resolve_source_cart_catalog
    cart = cap.current_capture
    current = commerce._catalog_configuration_observations(cart, resolve_source_cart_catalog(cart).as_dict())
    result = {}
    for line in cap.lines:
        if line.item_kind != "catalog_apparel":
            continue
        spec = current.get(line.line_id)
        if (not spec or spec.get("product_id") != line.product_id
            or spec.get("color_variant_id") != line.color_variant_id or spec.get("size") != line.size
            or spec.get("fit_option_code") != line.fit_code or spec.get("option_values") != line.as_dict()["option_values"]
            or spec.get("title") != line.title or spec.get("color") != line.color
            or commerce._price(str(spec.get("unit_price")), required=True) != line.unit_price):
            raise inventory.StockSyncError("catalog_metadata_changed")
        result[line.line_id] = {key: deepcopy(spec[key]) for key in
            ("product_id", "color_variant_id", "title", "color", "size", "fit_option_code", "option_values", "unit_price")}
    return result


def _receipt_valid(receipt):
    request = receipt.decision.request
    body = receipt.envelope.get("snapshot") or {}
    return (commerce.verify_assisted_evidence(receipt, "stock_sync")
        and commerce.verify_assisted_evidence(receipt.decision, "decision")
        and commerce.verify_assisted_evidence(request, "request")
        and receipt.client_id == receipt.decision.client_id == request.client_id
        and receipt.decision.status == "approved"
        and body.get("request_id") == str(request.pk) and body.get("request_digest") == request.envelope_hmac
        and body.get("decision_digest") == receipt.decision.envelope_hmac)


def _result(receipt, *, created):
    body = receipt.envelope["snapshot"]
    return {"ready": True, "created": created, "reason": "", "status": "stock_ready",
        "sync_id": str(receipt.pk), "operation_id": str(receipt.operation_id),
        "request_id": body["request_id"], "request_digest": body["request_digest"],
        "decision_id": str(receipt.decision_id), "decision_digest": body["decision_digest"],
        "preview_digest": body["preview_digest"], "proof_digest": receipt.envelope_hmac}


def projection(client_id, *, actor, request_id=None, decision_id=None, now=None):
    """Read-only double-captured preview of absolute whole-pool totals."""
    base = {"eligible": False, "reason": "", "requires_sync": False, "pools": []}
    try:
        actor, now = commerce._actor(getattr(actor, "pk", None)), commerce._now(now)
        request, decision = _rows(client_id, request_id, decision_id)
        base.update(request_id=str(request.pk), request_digest=request.envelope_hmac,
            decision_id=str(decision.pk), decision_digest=decision.envelope_hmac)
        receipt = _model().objects.filter(decision=decision, client_id=client_id).select_related("decision__request").first()
        if receipt:
            if not _receipt_valid(receipt):
                raise inventory.StockSyncError("sync_receipt_unverified")
            return {**base, **_result(receipt, created=False), "eligible": False, "reason": "already_synced"}
        _cap, first = _capture(request, decision, actor=actor, now=now)
        second_request, second_decision = _rows(client_id, request.pk, decision.pk)
        _cap, second = _capture(second_request, second_decision, actor=actor, now=now)
        if first != second:
            raise inventory.StockSyncError("preview_read_conflict")
        return {**base, "eligible": True, "reason": "", "requires_sync": first["requires_sync"],
            "preview_digest": commerce._digest(first), "pools": deepcopy(first["pools"])}
    except (inventory.StockSyncError, commerce.AssistedDecisionError) as exc:
        return {**base, "reason": exc.reason}


def _apply(request_id, *, actor, expected_request_digest, expected_decision_digest,
        expected_preview_digest, operation_id, counts_by_pool, decision_id=None, now=None):
    from management.models import IgClient, AdminAuditLog
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    Request, _Decision, _Execution, _Image = commerce._models()
    actor, now = commerce._actor(getattr(actor, "pk", None)), commerce._now(now)
    request_id, operation_id = commerce._uuid(request_id), commerce._uuid(operation_id)
    if not all(commerce._hash(value) for value in
            (expected_request_digest, expected_decision_digest, expected_preview_digest)):
        raise inventory.StockSyncError("digest_invalid")
    identity = Request.objects.filter(pk=request_id).values("client_id").first()
    if not identity:
        raise inventory.StockSyncError("request_missing")
    with commercial_episode_client_lock(identity["client_id"]), transaction.atomic():
        client = IgClient.objects.select_for_update().get(pk=identity["client_id"])
        request, decision = _rows(client.pk, request_id, decision_id, lock=True)
        operation = {"request_id": str(request.pk), "request_digest": expected_request_digest,
            "decision_id": str(decision.pk), "decision_digest": expected_decision_digest,
            "preview_digest": expected_preview_digest, "actor_id": actor.pk, "counts": counts_by_pool}
        input_digest = commerce._digest(operation)
        prior = _model().objects.select_for_update().filter(operation_id=operation_id).select_related("decision__request").first()
        if prior:
            if (not _receipt_valid(prior) or prior.client_id != client.pk or prior.actor_id != actor.pk
                or prior.decision_id != decision.pk or prior.envelope.get("operation_input_digest") != input_digest):
                raise inventory.StockSyncError("operation_conflict")
            return _result(prior, created=False)
        if (not hmac.compare_digest(expected_request_digest, request.envelope_hmac)
            or not hmac.compare_digest(expected_decision_digest, decision.envelope_hmac)):
            raise inventory.StockSyncError("decision_digest_changed")
        _cap, baseline = _capture(request, decision, actor=actor, now=now)
        # Preserve the existing payment lock prefix before inventory locks.
        if baseline["cart"]["scope"].get("episode_id") is None:
            if client.current_commercial_episode_id is not None:
                raise inventory.StockSyncError("commercial_episode_changed")
        else:
            inventory._lock_commercial_graph(request, baseline)
        inventory._lock_inventory(baseline)
        _cap, captured = _capture(request, decision, actor=actor, now=now, lock=True)
        if baseline != captured or not hmac.compare_digest(expected_preview_digest, commerce._digest(captured)):
            raise inventory.StockSyncError("preview_changed")
        counts = inventory._counts(counts_by_pool, captured["pools"])
        movements = inventory._write_counts(captured["pools"], counts, actor, operation_id, now)
        # Inventory observations may change; all source/head/epoch/approval
        # authority remains mandatory even after the accounting write.
        fresh = commerce.load_verified_assisted_decision(request.pk, decision.pk, actor=actor,
            expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac, now=now)
        if _catalog_metadata(fresh) != captured["catalog_metadata"]:
            raise inventory.StockSyncError("catalog_metadata_changed")
        receipt = commerce._seal(_model()(decision=decision, client=client, actor=actor, operation_id=operation_id),
            "stock_sync", {"request_id": str(request.pk), "request_digest": request.envelope_hmac,
                "decision_digest": decision.envelope_hmac, "preview_digest": expected_preview_digest,
                "baseline": captured, "counts": counts, "movement_ids": movements,
                "synced_at": now.isoformat()}, operation_input_digest=input_digest)
        AdminAuditLog.objects.create(actor=actor, action="ig_assisted_stock_sync", entity_type="IgAssistedCommerceStockSync",
            entity_id=str(receipt.pk), before={"preview_digest": expected_preview_digest, "pools": captured["pools"]},
            after={"counts": counts, "receipt_digest": receipt.envelope_hmac},
            reason="Explicit absolute physical recount; ordinary checkout allocation remains separate")
        return _result(receipt, created=True)


def apply_stock_sync(request_id, *, actor, expected_request_digest, expected_decision_digest,
        expected_preview_digest, operation_id, counts_by_pool, decision_id=None, now=None):
    """Atomic one-shot recount; every failed fence rolls back stock and receipt."""
    try:
        return _apply(request_id, actor=actor, expected_request_digest=expected_request_digest,
            expected_decision_digest=expected_decision_digest, expected_preview_digest=expected_preview_digest,
            operation_id=operation_id, counts_by_pool=counts_by_pool, decision_id=decision_id, now=now)
    except (inventory.StockSyncError, commerce.AssistedDecisionError) as exc:
        return {"ready": False, "reason": exc.reason}
    except IntegrityError:
        return {"ready": False, "reason": "operation_conflict"}
    except OperationalError as exc:
        if exc.args and exc.args[0] in {1205, 1213}:
            return {"ready": False, "reason": "inventory_conflict"}
        raise
