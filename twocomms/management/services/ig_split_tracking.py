"""Carrier evidence for a signed, fully allocated split order.

Tracking observes existing dispatches. It cannot create shipments, allocate
stock, settle payment, or turn one delivered parcel into a delivered order.
"""
from datetime import datetime, timedelta, timezone as datetime_timezone
import logging
import re
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)
SUCCESS_CODES = frozenset({9, 10, 11})
MAX_PARCELS = 20
MAX_ORDERS = 200
CHECK_INTERVAL = timedelta(minutes=5)
KYIV = ZoneInfo("Europe/Kyiv")


def _models():
    from management.models import IgSplitOrderPlan, IgSplitParcelDispatch, IgSplitParcelTracking
    return IgSplitOrderPlan, IgSplitParcelDispatch, IgSplitParcelTracking


def _aware(value):
    if not isinstance(value, datetime):
        return None
    return (value.replace(tzinfo=KYIV) if timezone.is_naive(value) else value).astimezone(datetime_timezone.utc)


def _provider_time(info):
    raw = info.get("DateLastMovementStatus") or info.get("DateLastMovement")
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 80 or not re.search(r"\d{1,2}:\d{2}", raw):
        return None
    try:
        parsed = parse_datetime(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                parsed = datetime.strptime(raw.strip(), fmt)
                break
            except ValueError:
                continue
    if parsed is not None and timezone.is_naive(parsed):
        local = parsed.replace(tzinfo=KYIV)
        # Native local timestamps do not identify a DST fold or nonexistent time.
        if (local.utcoffset() != parsed.replace(tzinfo=KYIV, fold=1).utcoffset()
                or local.astimezone(datetime_timezone.utc).astimezone(KYIV).replace(tzinfo=None) != parsed):
            return None
    return _aware(parsed)


def _code(value):
    if isinstance(value, bool):
        return None
    raw = str(value or "").strip()
    return int(raw) if raw.isdecimal() and len(raw) <= 3 else None


def _number(value):
    return "".join(char for char in str(value or "").strip() if char.isalnum()).casefold()


def has_split_plan(order):
    """Presence is authoritative even when its signature or coverage is invalid."""
    if (not getattr(order, "pk", None) or not getattr(order, "_meta", None)
            or getattr(getattr(order, "_state", None), "adding", False)):
        return False
    Plan, _, _ = _models()
    return Plan.objects.filter(order_id=order.pk).exists()


def _verified(order, *, now):
    if str(getattr(order, "status", "")) in {"cancelled", "refunded"}:
        return None
    try:
        from management.services.ig_split_execution import verified_tracking_plan
        result = verified_tracking_plan(order, now=now)
    except Exception:
        logger.exception("Split tracking proof unavailable for order %s", order.pk)
        return None
    if not isinstance(result, dict) or result.get("ready") is not True:
        return None
    parcels, dispatches = result.get("parcels"), result.get("dispatches")
    if not isinstance(parcels, list) or not 1 <= len(parcels) <= MAX_PARCELS:
        return None
    if not isinstance(dispatches, list) or len(dispatches) > MAX_PARCELS:
        return None
    parcel_ids = {row.pk for row in parcels}
    if len(parcel_ids) != len(parcels) or any(row.parcel_id not in parcel_ids for row in dispatches):
        return None
    if len({row.parcel_id for row in dispatches}) != len(dispatches):
        return None
    if (not result.get("proof") or getattr(result.get("plan"), "order_id", None) != order.pk
            or getattr(result.get("order"), "pk", None) != order.pk):
        return None
    return result


def _completion_time(verified, rows, *, now):
    """The verifier owns exact original-item coverage; every parcel still counts."""
    created_at = _aware(getattr(verified.get("order"), "created", None))
    if created_at is None:
        return None
    required = {row.pk for row in verified["parcels"]}
    dispatched = {row.parcel_id for row in verified["dispatches"]}
    if dispatched != required or len(rows) != len(required):
        return None
    by_parcel = {row.parcel_id: row for row in rows}
    if set(by_parcel) != required:
        return None
    terminal_times = []
    for row in rows:
        event, terminal, checked = map(_aware, (row.provider_event_at, row.terminal_at, row.checked_at))
        if (_code(row.code) not in SUCCESS_CODES or event is None or terminal is None or checked is None
                or event < created_at or event > checked or checked > now or terminal != event):
            return None
        terminal_times.append(terminal)
    return max(terminal_times)


def split_fulfilled_at(order):
    now = _aware(timezone.now())
    verified = _verified(order, now=now)
    if verified is None:
        return None
    _, _, Tracking = _models()
    rows = list(Tracking.objects.filter(parcel_id__in=[row.pk for row in verified["parcels"]])[:MAX_PARCELS + 1])
    return _completion_time(verified, rows, now=now)


def split_delivery_confirmed(order):
    return split_fulfilled_at(order) is not None


def pending_split_orders(*, onlyorder=None, now=None, include_deferred=False):
    """Bounded polling work includes orders with no legacy scalar TTN."""
    from django.db.models import Q
    from orders.models import Order
    Plan, Dispatch, _ = _models()
    now = _aware(now or timezone.now())
    # A manually marked done order still needs actual evidence for every parcel.
    dispatches = Dispatch.objects.exclude(parcel__plan__order__status="cancelled").filter(
        ~Q(parcel__plan__order__status="done") | Q(parcel__tracking__terminal_at__isnull=True)
        | ~Q(parcel__tracking__code__in=("9", "10", "11")))
    if not include_deferred:
        dispatches = dispatches.filter(Q(parcel__tracking__checked_at__isnull=True)
            | Q(parcel__tracking__checked_at__lte=now - CHECK_INTERVAL))
    orders = Order.objects.filter(pk__in=Plan.objects.filter(
        pk__in=dispatches.values("parcel__plan_id")).values("order_id"))
    if onlyorder is not None:
        orders = orders.filter(pk=getattr(onlyorder, "pk", onlyorder))
    return orders.order_by("pk")[:MAX_ORDERS]


def _dispatch_token(dispatch):
    return (dispatch.pk, dispatch.parcel_id, dispatch.tracking_number, dispatch.envelope_hmac)


def _tracking_token(row):
    return (row.code, row.status, row.provider_event_at, row.terminal_at, row.checked_at) if row else None


def _apply_result(order_id, expected, info, *, now):
    """Revalidate signed authority and the exact pre-HTTP row under local locks."""
    from orders.models import Order
    Plan, Dispatch, Tracking = _models()
    event = _provider_time(info)
    code = _code(info.get("StatusCode"))
    actual_number = info.get("Number") or info.get("DocumentNumber")
    if (event is None or event > now or code is None or not isinstance(expected, dict)
            or _number(actual_number) != _number(expected["dispatch"][2])):
        return False, False
    with transaction.atomic():
        from management.models import IgClient
        IgClient.objects.select_for_update().get(pk=expected["client_id"])
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.status == "cancelled":
            return False, False
        created_at = _aware(order.created)
        if code in SUCCESS_CODES and (created_at is None or event < created_at):
            return False, False
        plan = Plan.objects.select_for_update().filter(order_id=order.pk).first()
        if plan is None:
            return False, False
        from management.models import IgSplitParcel
        list(IgSplitParcel.objects.select_for_update().filter(plan_id=plan.pk).order_by("pk")[:MAX_PARCELS + 1])
        list(Dispatch.objects.select_for_update().filter(parcel__plan_id=plan.pk).order_by("parcel_id")[:MAX_PARCELS + 1])
        verified = _verified(order, now=now)
        if (verified is None or verified["proof"] != expected["proof"]
                or verified["plan"].client_id != expected["client_id"]):
            return False, False
        dispatch = next((row for row in verified["dispatches"] if row.parcel_id == expected["parcel_id"]), None)
        if dispatch is None or _dispatch_token(dispatch) != expected["dispatch"]:
            return False, False
        rows = list(Tracking.objects.select_for_update().filter(
            parcel_id__in=[row.pk for row in verified["parcels"]]).order_by("parcel_id")[:MAX_PARCELS + 1])
        current = next((row for row in rows if row.parcel_id == dispatch.parcel_id), None)
        if _tracking_token(current) != expected["tracking"]:
            return False, False
        old_event = _aware(current.provider_event_at) if current else None
        if old_event is not None and (event < old_event or (event == old_event and str(code) != current.code)):
            return False, False
        status = " · ".join(str(info.get(key) or "").strip() for key in ("Status", "StatusDescription") if info.get(key))[:500]
        if current is None:
            current = Tracking(parcel_id=dispatch.parcel_id)
            rows.append(current)
        changed = current.code != str(code) or current.status != status or old_event != event
        current.code, current.status = str(code), status
        current.provider_event_at, current.checked_at = event, now
        current.terminal_at = event if code in SUCCESS_CODES else None
        current.save()
        fulfilled_at = _completion_time(verified, rows, now=now)
        completed = bool(fulfilled_at and (order.status != "done"
            or order.tracking_terminal_at != fulfilled_at or order.tracking_status_code != 9))
        if completed:
            order.status = "done"
            order.tracking_status_code = 9
            order.tracking_terminal_at = fulfilled_at
            order.tracking_provider_event_at = fulfilled_at
            order.tracking_checked_at = now
            order.tracking_next_check_at = None
            order.save(update_fields=("status", "tracking_status_code", "tracking_terminal_at",
                "tracking_provider_event_at", "tracking_checked_at", "tracking_next_check_at"))
        return changed, completed


def track_split_orders(service, onlyorder=None, now=None):
    """Poll real existing child TTNs in native batches, without commerce writes."""
    from orders.nova_poshta_service import NovaPoshtaTransientAPIError, NovaPoshtaFatalAPIError
    now = _aware(now or timezone.now())
    result = {key: 0 for key in ("total_orders", "processed", "updated", "errors", "provider_errors",
        "transient_provider_errors", "fatal_provider_errors", "row_errors", "application_errors")}
    _, _, Tracking = _models()
    work = []
    for order in pending_split_orders(onlyorder=onlyorder, now=now):
        verified = _verified(order, now=now)
        if verified is None:
            continue
        rows = {row.parcel_id: row for row in Tracking.objects.filter(
            parcel_id__in=[row.pk for row in verified["parcels"]])[:MAX_PARCELS + 1]}
        count = 0
        for dispatch in verified["dispatches"]:
            tracking = rows.get(dispatch.parcel_id)
            if tracking and _aware(tracking.checked_at) and _aware(tracking.checked_at) > now - CHECK_INTERVAL:
                continue
            expected = {"parcel_id": dispatch.parcel_id, "dispatch": _dispatch_token(dispatch),
                "tracking": _tracking_token(tracking), "proof": verified["proof"],
                "client_id": verified["plan"].client_id}
            work.append((order.pk, dispatch.tracking_number, order.phone or "", expected))
            count += 1
        result["total_orders"] += bool(count)
    for start in range(0, len(work), 100):
        batch = work[start:start + 100]
        try:
            received = service.get_tracking_info_batch([{"DocumentNumber": row[1], "Phone": row[2]} for row in batch])
        except Exception as exc:
            category = "transient_provider_errors" if isinstance(exc, NovaPoshtaTransientAPIError) else (
                "fatal_provider_errors" if isinstance(exc, NovaPoshtaFatalAPIError) else "application_errors")
            result[category] += len(batch)
            result["errors"] += len(batch)
            result["processed"] += len(batch)
            logger.warning("Split parcel tracking batch failed: %s", type(exc).__name__)
            continue
        for order_id, number, _, expected in batch:
            result["processed"] += 1
            info = received.get(service._tracking_key(number))
            if not info or service._tracking_key(info.get("Number") or info.get("DocumentNumber")) != service._tracking_key(number):
                result["row_errors"] += 1
                result["errors"] += 1
                continue
            try:
                changed, completed = _apply_result(order_id, expected, info, now=now)
                result["updated"] += bool(changed)
                if completed:
                    from orders.models import Order
                    delivered = Order.objects.get(pk=order_id)
                    service._record_purchase_action(delivered)
                    service._dispatch_ig_delivery_lifecycle(delivered)
            except Exception:
                logger.exception("Split parcel tracking application failed for order %s", order_id)
                result["application_errors"] += 1
                result["errors"] += 1
    result["provider_errors"] = result["transient_provider_errors"]
    return result
