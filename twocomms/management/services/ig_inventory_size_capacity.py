"""Exact catalogue size caps under the ordinary allocation's variant lock.

Shared variant stock is checked separately. Protected grouped reservations are
resolved through every actual proposal item; the primary item cannot stand in
for a basket. Current-read row locks are required on MariaDB repeatable-read.
"""
from itertools import islice

from django.db import OperationalError, connection
from django.db.models import Q
from django.utils import timezone

MAX_RULES = 128
MAX_RESERVATIONS = 128
MAX_ITEMS = 512


class SizeCapacityError(ValueError):
    def __init__(self, reason):
        self.reason = str(reason)
        super().__init__(self.reason)


def _value(item, name, default=None):
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _position(item, variant_id):
    from product_catalog.size_grid_services import normalize_size_value
    quantity = _value(item, "quantity", _value(item, "qty"))
    identity = _value(item, "color_variant_id")
    size = _value(item, "size")
    fit = _value(item, "fit_code", _value(item, "fit_option_code", ""))
    if (identity != variant_id or type(quantity) is not int or quantity <= 0
        or not isinstance(size, str) or not size.strip() or not isinstance(fit, str)):
        raise SizeCapacityError("catalog_size_item_scope_unverified")
    return normalize_size_value(size), fit.strip().lower(), quantity


def _rules(variant_id):
    from product_catalog.models import VariantSizeRule
    rows = list(VariantSizeRule.objects.select_for_update().filter(variant_id=variant_id)
        .order_by("pk")[:MAX_RULES + 1])
    if len(rows) > MAX_RULES:
        raise SizeCapacityError("catalog_size_rule_bound")
    from product_catalog.size_grid_services import normalize_size_value
    index = {}
    for row in rows:
        key = (normalize_size_value(row.size), str(row.fit_code or "").strip().lower())
        if key in index:
            raise SizeCapacityError("catalog_size_rule_ambiguous")
        if row.stock is not None and (type(row.stock) is not int or row.stock < 0):
            raise SizeCapacityError("catalog_size_stock_invalid")
        index[key] = row
    return index


def _effective(index, size, fit):
    return index.get((size, fit)) or index.get((size, ""))


def _protected_rows(variant_id, now):
    from management.models import IgCheckoutInventoryReservation as Reservation
    from management.services.ig_inventory import protected_inventory_reservation_q
    query = Reservation.objects.select_for_update(nowait=True) if connection.vendor == "mysql" else Reservation.objects.select_for_update()
    try:
        rows = list(query.filter(allocation_source__in=["catalog_variant", ""], color_variant_id=variant_id).filter(
                protected_inventory_reservation_q(now))
            .order_by("pk")[:MAX_RESERVATIONS + 1])
    except OperationalError as exc:
        if exc.args and exc.args[0] in {1205, 3572}:
            raise SizeCapacityError("catalog_size_reservation_busy") from exc
        raise
    if len(rows) > MAX_RESERVATIONS:
        raise SizeCapacityError("catalog_size_reservation_bound")
    return rows


def _protected_items(rows, variant_id):
    from management.models import IgCheckoutProposalItem
    ids, scoped = set(), {}
    for row in rows:
        line_ids = row.line_ids
        if line_ids == []:
            # Legacy single-line evidence is valid only when its actual primary
            # item owns exactly the whole reserved quantity, checked below.
            line_ids = [row.item_id] if row.item_id else []
        if (not isinstance(line_ids, list) or not line_ids or any(type(value) is not int for value in line_ids)
            or len(set(line_ids)) != len(line_ids)):
            raise SizeCapacityError("catalog_size_reservation_scope_unverified")
        scoped[row.pk] = line_ids
        ids.update(line_ids)
    if len(ids) > MAX_ITEMS:
        raise SizeCapacityError("catalog_size_item_bound")
    # An editor may hold Item before waiting for this Variant. Never wait for
    # that foreign item while owning Variant: a current SKIP LOCKED read gives
    # a typed temporary denial instead of a lock cycle or stale snapshot.
    query = IgCheckoutProposalItem.objects.select_for_update(skip_locked=True) if connection.vendor == "mysql" else IgCheckoutProposalItem.objects.select_for_update()
    items = list(query.filter(pk__in=ids)
        .order_by("pk")[:MAX_ITEMS + 1])
    by_id = {row.pk: row for row in items}
    if set(by_id) != ids:
        raise SizeCapacityError("catalog_size_item_busy")
    result = {}
    for row in rows:
        owned = [by_id[value] for value in scoped[row.pk]]
        if (any(item.proposal_id != row.proposal_id or item.color_variant_id != variant_id
                or item.product_id != row.product_id for item in owned)
            or sum(item.quantity for item in owned) != row.quantity
            or (row.line_ids == [] and (len(owned) != 1 or owned[0].pk != row.item_id))):
            raise SizeCapacityError("catalog_size_reservation_scope_unverified")
        result[row.pk] = owned
    return result


def check_catalog_size_capacity(variant_id, items, now=None, *, exclude_proposal_id=None,
        exclude_generation_id=None, transfer_reservation_ids=()):
    """Return {numeric_rule_id: requested_quantity}, or raise a typed denial.

    Caller already owns the actual variant row lock and surrounding transaction.
    Exclusions are limited to its exact proposal/generation; transferred initial
    holds must additionally prove identical full item ownership and quantities.
    NULL/no-rule legacy caps remain untracked and never create a numeric plan.
    """
    if not connection.in_atomic_block:
        raise SizeCapacityError("catalog_size_transaction_required")
    if type(variant_id) is not int or variant_id <= 0:
        raise SizeCapacityError("catalog_size_variant_invalid")
    requested = list(islice(iter(items), MAX_ITEMS + 1))
    if not requested or len(requested) > MAX_ITEMS:
        raise SizeCapacityError("catalog_size_item_bound")
    positions = [_position(item, variant_id) for item in requested]
    from productcolors.models import ProductColorVariant
    variant = ProductColorVariant.objects.select_for_update().filter(pk=variant_id).first()
    if not variant or any(_value(item, "product_id") != variant.product_id for item in requested):
        raise SizeCapacityError("catalog_size_item_scope_unverified")
    owners = {_value(item, "proposal_id") for item in requested}
    if exclude_proposal_id is not None and owners != {exclude_proposal_id}:
        raise SizeCapacityError("catalog_size_exclusion_unverified")
    if exclude_generation_id is not None:
        from management.models import IgCheckoutInvoiceGeneration
        generation_proposal = IgCheckoutInvoiceGeneration.objects.filter(pk=exclude_generation_id)
        if len(owners) != 1 or None in owners or not generation_proposal.filter(proposal_id=next(iter(owners))).exists():
            raise SizeCapacityError("catalog_size_exclusion_unverified")
    index = _rules(variant_id)
    plan = {}
    for size, fit, quantity in positions:
        rule = _effective(index, size, fit)
        if rule and not rule.is_enabled:
            raise SizeCapacityError("catalog_size_disabled")
        if rule and rule.stock is not None:
            plan[rule.pk] = plan.get(rule.pk, 0) + quantity
    if not plan:
        return {}
    now = now or timezone.now()
    if timezone.is_naive(now):
        raise SizeCapacityError("catalog_size_clock_invalid")
    transfers = tuple(transfer_reservation_ids)
    if (len(transfers) > MAX_RESERVATIONS or len(set(transfers)) != len(transfers)
        or any(type(value) is not int or value <= 0 for value in transfers)):
        raise SizeCapacityError("catalog_size_transfer_unverified")
    rows = _protected_rows(variant_id, now)
    owned = _protected_items(rows, variant_id)
    protected = {}
    requested_ids = {_value(item, "pk", _value(item, "id")) for item in requested}
    by_id = {row.pk: row for row in rows}
    for identity in transfers:
        row = by_id.get(identity)
        if (not row or row.invoice_generation_id is not None or row.state != "active"
            or not row.line_ids or set(row.line_ids) != requested_ids
            or owners != {row.proposal_id}
            or row.quantity != sum(quantity for _size, _fit, quantity in positions)
            or (exclude_generation_id is None and row.proposal_id != exclude_proposal_id)):
            raise SizeCapacityError("catalog_size_transfer_unverified")
    for row in rows:
        if row.pk in transfers:
            continue
        if exclude_generation_id is not None and row.invoice_generation_id == exclude_generation_id:
            continue
        if exclude_generation_id is None and exclude_proposal_id is not None and row.proposal_id == exclude_proposal_id:
            continue
        for item in owned[row.pk]:
            size, fit, quantity = _position(item, variant_id)
            rule = _effective(index, size, fit)
            if rule and rule.pk in plan:
                protected[rule.pk] = protected.get(rule.pk, 0) + quantity
    by_rule = {row.pk: row for row in index.values()}
    for identity, quantity in plan.items():
        if protected.get(identity, 0) + quantity > by_rule[identity].stock:
            raise SizeCapacityError("insufficient_catalog_size_stock")
    return plan
