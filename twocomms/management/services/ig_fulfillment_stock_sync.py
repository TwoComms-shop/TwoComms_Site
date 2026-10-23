"""Explicit audited absolute recount, followed by ordinary checkout allocation.

A manager dialogue decision never changes stock. This second action confirms
whole physical pool counts, compares a sealed preview and keeps reservations
and checkout creation in the same database transaction. No provider I/O.
"""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hmac
import uuid

from django.core.exceptions import ValidationError
from django.db import OperationalError, IntegrityError, connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from management.services import ig_fulfillment_reviews as reviews

MAX_LINES = 20
MAX_RESERVATIONS = 128
MAX_COUNT = 1000000
DEBT = "stock_record_sync_or_manual_order"


class StockSyncError(ValueError):
    def __init__(self, reason):
        self.reason = str(reason)
        super().__init__(self.reason)


def _model():
    from management.ig_fulfillment_stock_sync_models import IgFulfillmentStockSync
    return IgFulfillmentStockSync


def _primitive(value):
    if isinstance(value, dict):
        return {key: _primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_primitive(item) for item in value]
    if isinstance(value, (datetime, uuid.UUID, Decimal)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    return value


def _request(client_id, request_id=None):
    rows = reviews.Request.objects.filter(client_id=client_id).select_related("client", "revision", "task")
    if request_id is not None:
        try:
            return rows.filter(pk=request_id).first()
        except (ValidationError, ValueError, TypeError):
            return None
    return rows.filter(envelope__scope="cart").order_by("-created_at", "pk").first()


def _inventory_read(queryset, lock=False, *, skip_locked=False):
    if not lock:
        return queryset
    if skip_locked and connection.features.has_select_for_update_skip_locked:
        return queryset.select_for_update(skip_locked=True)
    return queryset.select_for_update(nowait=connection.features.has_select_for_update_nowait)


def _reservations(source, identity, now, *, lock=False):
    from management.models import IgCheckoutInventoryReservation as Reservation
    from django.db.models import BooleanField, Case, Value, When
    from management.services.ig_inventory import protected_inventory_reservation_q
    filters = {"allocation_source": "warehouse", "stock_item_id": identity} if source == "warehouse" else {
        "allocation_source__in": ["catalog_variant", ""], "color_variant_id": identity}
    rows = list(_inventory_read(Reservation.objects.filter(**filters).annotate(
        _payment_protected=Case(When(protected_inventory_reservation_q(now), then=Value(True)),
            default=Value(False), output_field=BooleanField())), lock).order_by("pk").values()[:MAX_RESERVATIONS + 1])
    if len(rows) > MAX_RESERVATIONS:
        raise StockSyncError("reservation_capture_bound")
    protected = 0
    for row in rows:
        payment_protected = row.pop("_payment_protected")
        ordinary_protected = (row["state"] == Reservation.State.PAID_COMMITTED
            or row["state"] == Reservation.State.ACTIVE and row["expires_at"] > now)
        # Preserve ordinary captured rows/digests. Only the newly protected
        # uncertain obligation needs an additional fact for size allocation.
        if payment_protected and not ordinary_protected:
            row["_uncertain_payment_protected"] = True
        if payment_protected:
            protected += row["quantity"] - (row.get("fulfilled_quantity", 0) if source == "warehouse" else 0)
    return protected, _primitive(rows)


def _allocation(target, *, lock=False, _prepared=None):
    from product_catalog.models import ProductInventoryPolicy, VariantSizeRule, VariantBlankLink
    from productcolors.models import ProductColorVariant
    from warehouse.models import StockItem
    if _prepared is not None and lock:
        raise StockSyncError("inventory_preload_not_readonly")
    prepared = _prepared if isinstance(_prepared, dict) else {}
    policy = (prepared["policy"] if "policy" in prepared else
        _inventory_read(ProductInventoryPolicy.objects.filter(product_id=target["product_id"]), lock).first())
    variant = (prepared["variant"] if "variant" in prepared else
        _inventory_read(ProductColorVariant.objects.filter(pk=target["color_variant_id"], product_id=target["product_id"]), lock).first())
    if ((policy is not None and (not isinstance(policy, ProductInventoryPolicy) or policy.product_id != target["product_id"]))
        or (variant is not None and (not isinstance(variant, ProductColorVariant)
            or variant.pk != target["color_variant_id"] or variant.product_id != target["product_id"]))):
        raise StockSyncError("inventory_preload_scope_changed")
    if not policy or not variant:
        raise StockSyncError("inventory_mapping_unknown")
    if "rules" in prepared:
        if (not isinstance(prepared["rules"], (tuple, list)) or len(prepared["rules"]) > MAX_RESERVATIONS
            or any(not isinstance(rule, VariantSizeRule) or rule.variant_id != variant.pk for rule in prepared["rules"])):
            raise StockSyncError("inventory_preload_scope_changed")
        rules = sorted([rule for rule in prepared["rules"] if rule.size.casefold() == target["size"].casefold()
            and rule.fit_code in {"", target["fit_option_code"]}], key=lambda row: row.pk)
    else:
        rules = list(_inventory_read(VariantSizeRule.objects.filter(variant=variant, size__iexact=target["size"],
            fit_code__in=["", target["fit_option_code"]]), lock).order_by("pk")[:3])
    if len(rules) > 2:
        raise StockSyncError("inventory_mapping_ambiguous")
    specific = next((rule for rule in rules if rule.fit_code == target["fit_option_code"]), None)
    rule = specific or next((rule for rule in rules if not rule.fit_code), None)
    if rule and not rule.is_enabled:
        raise StockSyncError("disabled_size_requires_editor")
    base = {"product_id": target["product_id"], "variant_id": variant.pk,
        "policy": _primitive({"id": policy.pk, "source": policy.source, "updated_at": policy.updated_at}),
        "variant": {"id": variant.pk, "product_id": variant.product_id, "color_id": variant.color_id, "stock": variant.stock},
        "rules": _primitive([{"id": row.pk, "fit_code": row.fit_code, "size": row.size,
            "is_enabled": row.is_enabled, "stock": row.stock, "updated_at": row.updated_at} for row in rules])}
    if policy.source == "catalog_variant":
        if rule is None:
            raise StockSyncError("exact_size_count_requires_editor")
        if rule and rule.stock is not None and (type(rule.stock) is not int or rule.stock < 0):
            raise StockSyncError("exact_size_count_invalid")
        return {**base, "source": "catalog_variant", "pool_id": f"catalog_variant:{variant.pk}",
            "identity": variant.pk, "total": variant.stock, "size_rule_id": rule.pk if rule else None,
            "size_total": rule.stock if rule else None}
    if policy.source != "warehouse" or set(target["option_values"]) != {"fit"}:
        raise StockSyncError("inventory_mapping_unsupported")
    links = list(_inventory_read(VariantBlankLink.objects.filter(variant=variant,
        option_key=f"fit={target['fit_option_code']}"), lock).order_by("pk")[:2])
    if len(links) != 1:
        raise StockSyncError("inventory_mapping_ambiguous")
    stocks = list(_inventory_read(StockItem.objects.filter(subcategory_id=links[0].storage_subcategory_id,
        color_id=variant.color_id, size__iexact=target["size"]), lock).order_by("pk")[:2])
    if len(stocks) != 1:
        raise StockSyncError("inventory_stock_identity_unknown")
    stock = stocks[0]
    return {**base, "source": "warehouse", "pool_id": f"warehouse:{stock.pk}", "identity": stock.pk,
        "total": stock.quantity, "mapping": _primitive({"id": links[0].pk, "option_key": links[0].option_key,
            "storage_subcategory_id": links[0].storage_subcategory_id, "updated_at": links[0].updated_at}),
        "stock": _primitive({"id": stock.pk, "subcategory_id": stock.subcategory_id, "color_id": stock.color_id,
            "size": stock.size, "quantity": stock.quantity, "updated_at": stock.updated_at})}


def _size_protection(pool, reusable_ids, now, *, lock=False):
    """Resolve every grouped commitment's exact items, never its primary item."""
    from management.models import IgCheckoutProposalItem, IgCheckoutInventoryReservation as Reservation
    from product_catalog.models import VariantSizeRule
    protected = [row for row in pool["reservations"] if row["id"] not in reusable_ids and
        (row.get("_uncertain_payment_protected") is True or row["state"] == Reservation.State.PAID_COMMITTED or row["state"] == Reservation.State.ACTIVE
            and parse_datetime(row["expires_at"]) > now)]
    ids = set()
    for row in protected:
        if (not isinstance(row["line_ids"], list) or not row["line_ids"]
            or any(type(value) is not int for value in row["line_ids"])
            or len(set(row["line_ids"])) != len(row["line_ids"])):
            raise StockSyncError("protected_size_scope_unverified")
        ids.update(row["line_ids"])
    if len(ids) > MAX_RESERVATIONS:
        raise StockSyncError("protected_item_capture_bound")
    items = list(_inventory_read(IgCheckoutProposalItem.objects.filter(pk__in=ids), lock, skip_locked=True).order_by("pk").values(
        "id", "proposal_id", "product_id", "color_variant_id", "quantity", "size", "fit_code")[:MAX_RESERVATIONS + 1])
    by_id = {row["id"]: row for row in items}
    if set(by_id) != ids:
        raise StockSyncError("protected_size_scope_unverified")
    rules = list(_inventory_read(VariantSizeRule.objects.filter(variant_id=pool["identity"]), lock).order_by("pk").values(
        "id", "size", "fit_code", "is_enabled", "stock", "updated_at")[:MAX_RESERVATIONS + 1])
    if len(rules) > MAX_RESERVATIONS:
        raise StockSyncError("size_rule_capture_bound")
    totals = {}
    for row in protected:
        scoped = [by_id[value] for value in row["line_ids"]]
        if (sum(item["quantity"] for item in scoped) != row["quantity"]
            or any(item["proposal_id"] != row["proposal_id"] or item["color_variant_id"] != pool["identity"] for item in scoped)):
            raise StockSyncError("protected_size_scope_unverified")
        for item in scoped:
            matches = [rule for rule in rules if rule["size"].casefold() == item["size"].casefold()
                and rule["fit_code"] in {"", item["fit_code"]}]
            specific = [rule for rule in matches if rule["fit_code"] == item["fit_code"]]
            general = [rule for rule in matches if not rule["fit_code"]]
            if len(specific) > 1 or len(general) > 1:
                raise StockSyncError("protected_size_scope_unverified")
            effective = specific[0] if specific else general[0] if general else None
            if effective:
                key = str(effective["id"])
                totals[key] = totals.get(key, 0) + item["quantity"]
    return totals, _primitive({"items": items, "rules": rules})


def _capture_inventory_pools(lines, now, *, reusable_ids=(), lock=False):
    """Shared exact inventory facts; callers independently prove every line.

    No review/decision/source authority is constructed here. Whole-pool paid
    and live holds and exact size coverage use the existing protected capture.
    """
    reusable_ids = set(reusable_ids)
    pools, captures = {}, []
    for line in lines:
        target = line.get("target") or {}
        if type(target.get("quantity")) is not int or not 1 <= target["quantity"] <= 99:
            raise StockSyncError("target_incomplete")
        allocation = _allocation(target, lock=lock)
        captures.append({"line_id": line["line_id"], "target": target, "configuration_digest": line["configuration_digest"],
            "allocation": allocation})
        pool = pools.get(allocation["pool_id"])
        if pool is None:
            protected, reservation_rows = _reservations(allocation["source"], allocation["identity"], now, lock=lock)
            reused = sum(row["quantity"] for row in reservation_rows if row["id"] in reusable_ids)
            pool = {"pool_id": allocation["pool_id"], "source": allocation["source"], "identity": allocation["identity"],
                "before_total": allocation["total"], "protected_total": protected, "protected": protected - reused,
                "reused_quantity": reused, "reused_reservation_ids": sorted(row["id"] for row in reservation_rows if row["id"] in reusable_ids),
                "required": 0, "line_ids": [],
                "labels": [], "size_counts": {}, "reservations": reservation_rows}
            if allocation["source"] == "catalog_variant":
                size_protected, scope = _size_protection(pool, reusable_ids, now, lock=lock)
                pool["size_protected"] = size_protected
                pool["protected_size_scope"] = scope
            pools[allocation["pool_id"]] = pool
        pool["required"] += target["quantity"]
        pool["line_ids"].append(line["line_id"])
        pool["labels"].append(str(line.get("label") or "")[:160])
        if allocation["source"] == "catalog_variant":
            key = str(allocation["size_rule_id"])
            size_pool = pool["size_counts"].setdefault(key, {"rule_id": allocation["size_rule_id"],
                "size": target["size"], "fit": target["fit_option_code"], "before_total": allocation["size_total"],
                "protected": pool["size_protected"].get(key, 0), "required": 0, "line_ids": []})
            size_pool["required"] += target["quantity"]
            size_pool["line_ids"].append(line["line_id"])
    for pool in pools.values():
        pool["minimum_total"] = pool["protected"] + pool["required"]
        pool["total_free"] = max(0, pool["before_total"] - pool["protected"])
        for size_pool in pool["size_counts"].values():
            size_pool["minimum_total"] = size_pool["protected"] + size_pool["required"]
    return captures, sorted(pools.values(), key=lambda row: (row["source"] == "warehouse", row["identity"]))


def _capture(request, now, *, lock=False):
    from django.contrib.auth import get_user_model
    from management.bot_access import has_all_bot_capabilities, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
    from management.services.ig_commerce_projection import capture_current_selection_lines
    if not reviews._verified(request, "request") or request.envelope.get("scope") != "cart":
        raise StockSyncError("request_unverified")
    settings_obj = reviews._settings()
    cart = capture_current_selection_lines(request.client_id, now=now)
    reason = reviews._current_reason(request, settings_obj, now=now, current_source_cart=cart)
    if reason:
        raise StockSyncError(reason)
    decision = reviews.Decision.objects.filter(request=request).select_related("actor").order_by("-version").first()
    if not decision or not reviews._verified(decision, "decision") or not decision.actor.is_active:
        raise StockSyncError("decision_unverified")
    # Historical signatures preserve who decided; a new inventory effect
    # additionally needs that principal's current capabilities. Reload to
    # discard Django permission caches and honour the reviewer dominant deny.
    decision_actor = get_user_model().objects.filter(pk=decision.actor_id).first()
    if not decision_actor or not has_all_bot_capabilities(decision_actor,
            OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
        raise StockSyncError("decision_actor_forbidden")
    reason = reviews._decision_reason(request, decision, now=now, current_source_cart=cart)
    if reason:
        raise StockSyncError(reason)
    if _model().objects.filter(decision=decision).exists():
        raise StockSyncError("already_synced")
    lines, answers = request.envelope.get("lines"), decision.envelope.get("lines")
    if (not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES or not isinstance(answers, list)
        or len(lines) != len(answers)):
        raise StockSyncError("line_coverage_invalid")
    by_id = {row["line_id"]: row for row in answers}
    if len(by_id) != len(lines) or {row["line_id"] for row in lines} != set(by_id):
        raise StockSyncError("line_coverage_invalid")
    if any(answer.get("availability") != "available" or answer.get("supply_basis") not in
            {"accessible_unrecorded", "warehouse_checked"} for answer in answers):
        raise StockSyncError("all_lines_require_available")
    from management.services.ig_fulfillment_checkout import owned_stock_sync_reservation_ids
    reusable_ids = set(owned_stock_sync_reservation_ids(request, decision, cart, now))
    captures, pool_rows = _capture_inventory_pools([{**line, "configuration_digest": by_id[line["line_id"]]["configuration_digest"]}
        for line in lines], now, reusable_ids=reusable_ids, lock=lock)
    pools = {pool["pool_id"]: pool for pool in pool_rows}
    # A checked physical AVAILABLE decision can disagree with the actual
    # accounting pool too. Only the explicit absolute recount below grants
    # capacity; the decision's checked quantity is never an inventory write.
    accounting_shortfall = any(pool["before_total"] < pool["minimum_total"]
        or any(type(size_pool["before_total"]) is int
            and size_pool["before_total"] < size_pool["minimum_total"]
            for size_pool in pool["size_counts"].values()) for pool in pools.values())
    if not any(answer.get("supply_basis") == "accessible_unrecorded" for answer in answers) and not accounting_shortfall:
        raise StockSyncError("accessible_stock_reconciliation_not_required")
    payload = _primitive({"request_id": str(request.pk), "request_digest": request.envelope_hmac,
        "decision_id": str(decision.pk), "decision_digest": decision.envelope_hmac,
        "settings_id": settings_obj.pk, "settings_epoch": settings_obj.reply_permission_epoch,
        "client_epoch": request.client.reply_permission_epoch, "cart": cart, "allocations": captures,
        "pools": sorted(pools.values(), key=lambda row: (row["source"] == "warehouse", row["identity"]))})
    return decision, payload


def projection(client_id, request_id=None, now=None):
    """Read-only current CAS preview; counts are totals, never an increment."""
    now = now or timezone.now()
    request = _request(client_id, request_id)
    if not request:
        return {"eligible": False, "reason": "request_missing", "pools": []}
    receipt = _model().objects.filter(client_id=client_id, decision__request=request).select_related("decision").first()
    if receipt:
        if (not reviews._verified(receipt, "stock_sync") or not reviews._verified(receipt.decision, "decision")
            or receipt.envelope.get("decision_digest") != receipt.decision.envelope_hmac
            or receipt.envelope.get("request_digest") != request.envelope_hmac):
            return {"eligible": False, "reason": "sync_receipt_unverified", "pools": []}
        return {"eligible": False, "reason": "already_synced", "request_id": str(request.pk), "pools": [],
            "sync_id": str(receipt.pk), "decision_id": str(receipt.decision_id),
            "decision_digest": receipt.decision.envelope_hmac,
            "checkout": _checkout_readback(receipt, now), "proof_digest": receipt.envelope_hmac}
    try:
        decision, payload = _capture(request, now)
        second_decision, second = _capture(_request(client_id, request.pk), now)
        if payload != second or decision.pk != second_decision.pk:
            raise StockSyncError("preview_read_conflict")
    except StockSyncError as exc:
        return {"eligible": False, "reason": exc.reason, "request_id": str(request.pk), "pools": []}
    return {"eligible": True, "reason": "", "request_id": str(request.pk), "decision_id": str(decision.pk),
        "decision_digest": decision.envelope_hmac, "preview_digest": reviews._digest(payload),
        "pools": deepcopy(payload["pools"])}


def _counts(raw, pools):
    if not isinstance(raw, dict) or set(raw) != {pool["pool_id"] for pool in pools}:
        raise StockSyncError("count_pool_coverage_invalid")
    result = {}
    for pool in pools:
        row = raw[pool["pool_id"]]
        keys = {"total", "size_counts"} if pool["source"] == "catalog_variant" else {"total"}
        if not isinstance(row, dict) or set(row) != keys or type(row.get("total")) is not int or not 0 <= row["total"] <= MAX_COUNT:
            raise StockSyncError("physical_total_invalid")
        if row["total"] < pool["minimum_total"]:
            raise StockSyncError("physical_total_below_protected_and_requested")
        if pool["source"] == "catalog_variant":
            counts = row["size_counts"]
            if not isinstance(counts, dict) or set(counts) != set(pool["size_counts"]):
                raise StockSyncError("size_count_coverage_invalid")
            for key, value in counts.items():
                if type(value) is not int or not 0 <= value <= row["total"]:
                    raise StockSyncError("physical_size_total_invalid")
                if value < pool["size_counts"][key]["minimum_total"]:
                    raise StockSyncError("physical_size_total_below_protected_and_requested")
        result[pool["pool_id"]] = deepcopy(row)
    return result


def _lock_inventory(payload):
    from storefront.models import Product
    from productcolors.models import ProductColorVariant
    from product_catalog.models import ProductInventoryPolicy, VariantSizeRule, VariantBlankLink
    from warehouse.models import StockItem
    captures = payload["allocations"]
    product_ids = sorted({row["target"]["product_id"] for row in captures})
    variant_ids = sorted({row["target"]["color_variant_id"] for row in captures})
    list(Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk"))
    list(ProductColorVariant.objects.select_for_update().filter(pk__in=variant_ids).order_by("pk"))
    list(ProductInventoryPolicy.objects.select_for_update().filter(product_id__in=product_ids).order_by("product_id"))
    for model in (VariantSizeRule, VariantBlankLink):
        rows = list(model.objects.select_for_update().filter(variant_id__in=variant_ids)
            .order_by("variant_id", "pk")[:MAX_RESERVATIONS + 1])
        if len(rows) > MAX_RESERVATIONS:
            raise StockSyncError("inventory_lock_capture_bound")
    ids = sorted({pool["identity"] for pool in payload["pools"] if pool["source"] == "warehouse"})
    list(StockItem.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    from management.models import IgCheckoutInventoryReservation
    reservation_ids = sorted({row["id"] for pool in payload["pools"] for row in pool["reservations"]})
    list(_inventory_read(IgCheckoutInventoryReservation.objects.filter(pk__in=reservation_ids), True).order_by("pk"))


def _lock_commercial_graph(request, captured):
    """Preserve existing payment lock prefix before acquiring any stock row."""
    from management.models import IgCommercialEpisode, IgDeal, IgCheckoutProposal, IgCheckoutInvoiceGeneration
    from orders.models import PaymentAttempt
    episode_id = (captured["cart"].get("scope") or {}).get("episode_id")
    if episode_id != request.client.current_commercial_episode_id:
        raise StockSyncError("commercial_episode_changed")
    episode = IgCommercialEpisode.objects.filter(pk=episode_id, client_id=request.client_id,
        state=IgCommercialEpisode.State.ACTIVE, open_slot=1).first()
    if not episode:
        raise StockSyncError("commercial_episode_changed")
    if episode.deal_id is None:
        return
    deal = IgDeal.objects.select_for_update().filter(pk=episode.deal_id, client_id=request.client_id).first()
    if not deal:
        raise StockSyncError("commercial_deal_changed")
    if not deal.active_checkout_proposal_id:
        return
    proposal = IgCheckoutProposal.objects.select_for_update().filter(pk=deal.active_checkout_proposal_id,
        deal=deal, client_id=request.client_id, commercial_episode_id=episode.pk).first()
    if not proposal:
        raise StockSyncError("commercial_proposal_changed")
    generations = list(IgCheckoutInvoiceGeneration.objects.select_for_update().filter(proposal=proposal)
        .order_by("generation", "pk")[:MAX_RESERVATIONS + 1])
    if len(generations) > MAX_RESERVATIONS:
        raise StockSyncError("commercial_generation_capture_bound")
    ids = {row.payment_attempt_id for row in generations if row.payment_attempt_id}
    if proposal.payment_attempt_id:
        ids.add(proposal.payment_attempt_id)
    list(PaymentAttempt.objects.select_for_update().filter(pk__in=sorted(ids)).order_by("pk"))


def _write_counts(pools, counts, actor, operation_id, now):
    from productcolors.models import ProductColorVariant
    from product_catalog.models import VariantSizeRule
    from warehouse.models import StockItem, MovementReason
    from warehouse.services.inventory import set_stock_quantity
    movements = []
    for pool in pools:
        values = counts[pool["pool_id"]]
        if pool["source"] == "warehouse":
            movement = set_stock_quantity(stock_item=StockItem.objects.get(pk=pool["identity"]),
                new_quantity=values["total"], user=actor, reason=MovementReason.RECOUNT,
                comment=f"Manager fulfillment absolute stock sync {operation_id}")
            if movement:
                movements.append(movement.pk)
        else:
            ProductColorVariant.objects.filter(pk=pool["identity"]).update(stock=values["total"])
            for rule_id, total in values["size_counts"].items():
                VariantSizeRule.objects.filter(pk=int(rule_id)).update(stock=total, updated_at=now)
    return movements


def _checkout_readback(receipt, now):
    from management.models import IgCheckoutProposal, IgCheckoutInventoryReservation, IgCommercialEpisode, IgDeal
    frozen = receipt.envelope.get("checkout") or {}
    identity = frozen.get("proposal_id")
    if identity is None:
        return {"status": "stock_ready", "proposal_id": None, "url": "", "reservation_ids": [],
            "manager_workspace_url": f"/bot/?section=clients&client_id={receipt.client_id}"}
    proposal = IgCheckoutProposal.objects.filter(pk=identity, client_id=receipt.client_id).first()
    if not proposal or str(frozen.get("proposal_public_id") or frozen.get("public_id") or "") != str(proposal.public_id):
        return {"status": "checkout_binding_changed", "proposal_id": identity, "url": "", "reservation_ids": []}
    ids = frozen.get("reservation_ids") or []
    reservations = list(IgCheckoutInventoryReservation.objects.filter(proposal=proposal, pk__in=ids)
        .order_by("pk").values("id", "quantity", "state", "allocation_source", "line_ids")[:MAX_RESERVATIONS + 1])
    if len(reservations) > MAX_RESERVATIONS or {row["id"] for row in reservations} != set(ids):
        return {"status": "checkout_binding_changed", "proposal_id": identity, "url": "", "reservation_ids": []}
    current_episode = proposal.commercial_episode_id
    current = IgCommercialEpisode.objects.filter(pk=current_episode, client_id=receipt.client_id,
        deal_id=proposal.deal_id, state="active", open_slot=1).exists()
    from management.models import IgClient
    current = current and IgClient.objects.filter(pk=receipt.client_id, current_commercial_episode_id=current_episode,
        privacy_erasure_started_at__isnull=True).exists()
    current = current and IgDeal.objects.filter(pk=proposal.deal_id, client_id=receipt.client_id,
        active_checkout_proposal_id=proposal.pk).exists()
    revision = (frozen.get("proof") or {}).get("proposal_revision", frozen.get("proposal_revision"))
    if revision is not None and revision != proposal.revision:
        current = False
    if current:
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_revision_cart_binding import same_checkout_source_capture
        stored_revision = proposal.revisions.filter(revision=proposal.revision).first()
        stored_cart = (stored_revision.snapshot or {}).get("source_cart_capture") if stored_revision else None
        current = same_checkout_source_capture(stored_cart,
            capture_current_selection_lines(receipt.client_id, now=now))
    return {"status": proposal.status, "proposal_id": proposal.pk, "proposal_public_id": str(proposal.public_id),
        "proposal_revision": proposal.revision, "url": "",
        "manager_workspace_url": f"/bot/?section=clients&client_id={receipt.client_id}",
        "reservation_ids": [row["id"] for row in reservations], "reservations": reservations,
        "source_binding_current": current}


def _result(receipt, *, created, now):
    return {"ready": True, "created": created, "reason": "", "sync_id": str(receipt.pk),
        "decision_id": str(receipt.decision_id), "proof_digest": receipt.envelope_hmac,
        **_checkout_readback(receipt, now)}


def _apply_stock_sync(request_id, *, actor, expected_decision_digest, expected_preview_digest,
        operation_id, counts_by_pool, now=None):
    """Reconcile once; checkout failure rolls back counts, allocation and audit."""
    from management.bot_access import has_all_bot_capabilities, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
    from django.contrib.auth import get_user_model
    from management.models import IgClient, AdminAuditLog
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    actor = get_user_model().objects.filter(pk=getattr(actor, "pk", None)).first()
    if not actor or not actor.is_active or not has_all_bot_capabilities(actor, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
        return {"ready": False, "reason": "actor_forbidden"}
    try:
        operation_id, request_id = uuid.UUID(str(operation_id)), uuid.UUID(str(request_id))
    except (ValueError, TypeError, AttributeError):
        return {"ready": False, "reason": "identity_invalid"}
    request = reviews.Request.objects.filter(pk=request_id).first()
    if not request:
        return {"ready": False, "reason": "request_missing"}
    now = now or timezone.now()
    with commercial_episode_client_lock(request.client_id), transaction.atomic():
        IgClient.objects.select_for_update().get(pk=request.client_id)
        request = reviews.Request.objects.select_for_update().select_related("client", "revision", "task").get(pk=request.pk)
        prior = _model().objects.select_for_update().filter(operation_id=operation_id).first()
        if prior:
            if (not reviews._verified(prior, "stock_sync") or prior.client_id != request.client_id or prior.actor_id != actor.pk
                or prior.envelope.get("request_id") != str(request.pk)
                or prior.envelope.get("decision_digest") != expected_decision_digest
                or prior.envelope.get("preview_digest") != expected_preview_digest
                or prior.envelope.get("counts") != counts_by_pool):
                return {"ready": False, "reason": "operation_conflict"}
            return _result(prior, created=False, now=now)
        try:
            decision, baseline = _capture(request, now)
            decision = reviews.Decision.objects.select_for_update().get(pk=decision.pk)
            if not hmac.compare_digest(str(expected_decision_digest), decision.envelope_hmac):
                raise StockSyncError("decision_digest_changed")
            _lock_commercial_graph(request, baseline)
            _lock_inventory(baseline)
            decision, captured = _capture(_request(request.client_id, request.pk), now, lock=True)
            if not hmac.compare_digest(str(expected_preview_digest), reviews._digest(captured)) or baseline != captured:
                raise StockSyncError("preview_changed")
            counts = _counts(counts_by_pool, captured["pools"])
            movement_ids = _write_counts(captured["pools"], counts, actor, operation_id, now)
            from management.services.ig_fulfillment_checkout import prepare_fulfillment_checkout
            checkout = prepare_fulfillment_checkout(request, decision, actor=actor, now=now)
            stock_only = isinstance(checkout, dict) and checkout.get("status") == "stock_ready" and checkout.get("proposal_id") is None
            if (not isinstance(checkout, dict) or not checkout.get("ready")
                or not (checkout.get("proposal_id") or stock_only)):
                raise StockSyncError((checkout or {}).get("reason", "checkout_allocation_failed") if isinstance(checkout, dict) else "checkout_allocation_failed")
            receipt = reviews._seal(_model()(decision=decision, client_id=request.client_id, actor=actor, operation_id=operation_id),
                "stock_sync", {"request_id": str(request.pk), "request_digest": request.envelope_hmac,
                    "decision_digest": decision.envelope_hmac, "preview_digest": expected_preview_digest,
                    "baseline": captured, "counts": counts, "movement_ids": movement_ids,
                    "checkout": _primitive({key: value for key, value in checkout.items() if key != "url"}),
                    "synced_at": now.isoformat()})
            task = request.task
            context = deepcopy(task.manager_context or {})
            context["required_decisions"] = [value for value in context.get("required_decisions", []) if value != DEBT]
            context["business_debt_open"] = bool(context["required_decisions"])
            context["stock_sync_id"] = str(receipt.pk)
            context["stock_sync_digest"] = receipt.envelope_hmac
            task.manager_context = context
            task.save(update_fields=["manager_context", "updated_at"])
            AdminAuditLog.objects.create(actor=actor, action="ig_fulfillment_stock_sync", entity_type="IgFulfillmentStockSync",
                entity_id=str(receipt.pk), before={"preview_digest": expected_preview_digest, "pools": captured["pools"]},
                after={"counts": counts, "proposal_id": checkout["proposal_id"], "receipt_digest": receipt.envelope_hmac},
                reason="Explicit absolute physical stock reconciliation and ordinary checkout reservation")
            return _result(receipt, created=True, now=now)
        except StockSyncError:
            # Catching inside the transaction must never commit partial counts.
            transaction.set_rollback(True)
            raise


def apply_stock_sync(request_id, *, actor, expected_decision_digest, expected_preview_digest,
        operation_id, counts_by_pool, now=None):
    try:
        return _apply_stock_sync(request_id, actor=actor, expected_decision_digest=expected_decision_digest,
            expected_preview_digest=expected_preview_digest, operation_id=operation_id,
            counts_by_pool=counts_by_pool, now=now)
    except OperationalError as exc:
        if exc.args and exc.args[0] in {1205, 1213}:
            return {"ready": False, "reason": "inventory_conflict"}
        raise
    except IntegrityError:
        return {"ready": False, "reason": "operation_conflict"}
    except StockSyncError as exc:
        return {"ready": False, "reason": exc.reason}
