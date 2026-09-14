"""Event-time Instagram funnel facts and cohort analytics."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import connection, transaction
from django.db.models import Count, F, OuterRef, Q, Subquery
from django.utils import timezone

from management.services.bot_followups import KYIV_TZ, QUIET_END, QUIET_START
from management.services.ig_commercial_episodes import (
    commercial_episode_client_lock,
    ensure_open_episode_for_locked_client,
)
from orders.fulfillment_truth import nova_poshta_delivery_confirmed_at


FUNNEL_STEPS = (
    ("conversation_started", "Діалог розпочато"),
    ("bot_replied_first", "Бот відповів"),
    ("product_pinned", "Товар визначено"),
    ("price_quoted", "Ціну названо"),
    ("paylink_issued", "Посилання видано"),
    ("paylink_viewed", "Посилання відкрито"),
    ("payment_confirmed", "Оплату підтверджено"),
    ("order_created", "Замовлення створено"),
    ("ttn_created", "ТТН створено"),
    ("delivered", "Замовлення отримано"),
)

SILENCE_THRESHOLDS = {
    "new": Decimal("24"),
    "qualifying": Decimal("24"),
    "product_matched": Decimal("24"),
    "checkout": Decimal("12"),
    "payment_pending": Decimal("6"),
    "lead_manager": Decimal("4"),
}

DROP_OFF_STAGE_TO_STEP = {
    "new": "conversation_started",
    "qualifying": "bot_replied_first",
    "product_matched": "price_quoted",
    "checkout": "paylink_issued",
    "payment_pending": "paylink_issued",
    "paid": "payment_confirmed",
    "order_created": "order_created",
    "done": "delivered",
    "spam": "conversation_started",
}


def delivery_funnel_event_key(episode, order) -> str:
    """Return a replay-safe key for one carrier-confirmed delivery revision."""
    from management.models import IgFunnelStepEvent

    delivered_at = nova_poshta_delivery_confirmed_at(order)
    if delivered_at is None:
        raise ValueError("delivery_funnel_event_key requires carrier-confirmed delivery")
    legacy_key = f"ig-delivered:{order.pk}"
    legacy_event = IgFunnelStepEvent.objects.filter(
        episode=episode,
        event_key=legacy_key,
    ).first()
    normalized_delivered_at = _aware(delivered_at).astimezone(UTC)
    if (
        legacy_event is not None
        and _aware(legacy_event.occurred_at).astimezone(UTC)
        == normalized_delivered_at
    ):
        return legacy_key

    delivery_revision = normalized_delivered_at.isoformat(
        timespec="microseconds"
    )
    material = "\x1f".join((str(order.pk), delivery_revision))
    revision = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"{legacy_key}:{revision}"


def _aware(value: datetime | None) -> datetime:
    value = value or timezone.now()
    if timezone.is_naive(value):
        return timezone.make_aware(value, KYIV_TZ)
    return value


def _event_defaults(*, episode, event_type, occurred_at, stage, actor, evidence, is_backfilled):
    return {
        "episode": episode,
        "event_type": str(event_type),
        "occurred_at": _aware(occurred_at),
        "stage": str(stage or "")[:32],
        "actor": str(actor or "")[:40],
        "evidence": evidence if isinstance(evidence, dict) else {},
        "is_backfilled": bool(is_backfilled),
    }


def _record_step_event_locked(
    episode,
    *,
    event_type: str,
    event_key: str,
    occurred_at: datetime | None = None,
    stage: str = "",
    actor: str = "",
    evidence: dict | None = None,
    is_backfilled: bool = False,
):
    from management.models import IgFunnelStepEvent

    key = str(event_key or "").strip()[:160]
    if not key:
        raise ValueError("event_key is required")
    event, _created = IgFunnelStepEvent.objects.get_or_create(
        event_key=key,
        defaults=_event_defaults(
            episode=episode,
            event_type=event_type,
            occurred_at=occurred_at,
            stage=stage,
            actor=actor,
            evidence=evidence,
            is_backfilled=is_backfilled,
        ),
    )
    if event.episode_id != episode.pk or event.event_type != str(event_type):
        raise ValueError("event_key already belongs to another funnel fact")
    return event


def _ensure_episode_locked(client):
    return ensure_open_episode_for_locked_client(
        client,
        materialization_prefix="ig-funnel",
    )


def ensure_episode_for_client(client):
    """Return one open episode, creating it under the MariaDB client lock."""
    from management.models import IgClient

    if not getattr(client, "pk", None):
        raise ValueError("A persisted client is required")
    with commercial_episode_client_lock(client.pk):
        with transaction.atomic():
            locked = IgClient.objects.select_for_update().get(pk=client.pk)
            return _ensure_episode_locked(locked)


def _ensure_conversation_started(
    episode,
    client,
    *,
    occurred_at,
    event_key="",
    evidence=None,
    is_backfilled=False,
):
    from management.models import IgFunnelStepEvent

    existing = IgFunnelStepEvent.objects.filter(
        episode=episode,
        event_type=IgFunnelStepEvent.Type.CONVERSATION_STARTED,
    ).first()
    if existing:
        return existing
    started_at = occurred_at or client.first_contact_at or episode.opened_at
    return _record_step_event_locked(
        episode,
        event_type="conversation_started",
        event_key=event_key or f"funnel:{episode.pk}:conversation_started",
        occurred_at=started_at,
        stage=client.stage,
        actor="customer",
        evidence={"client_id": client.pk, **(evidence or {})},
        is_backfilled=is_backfilled,
    )


def _record_drop_off(event, *, evidence):
    from management.models import IgFunnelDropOff

    payload = evidence if isinstance(evidence, dict) else {}
    kind = str(payload.get("kind") or IgFunnelDropOff.Kind.SILENCE)
    if kind not in set(IgFunnelDropOff.Kind.values):
        raise ValueError("Unknown drop-off kind")
    drop_off, _created = IgFunnelDropOff.objects.get_or_create(
        step_event=event,
        defaults={
            "episode": event.episode,
            "kind": kind,
            "reason_code": str(payload.get("reason_code") or "")[:80],
            "stage_at_drop": str(payload.get("stage_at_drop") or event.stage or "")[:32],
            "objection_at_drop": str(payload.get("objection_at_drop") or "")[:32],
            "silence_hours": Decimal(str(payload.get("silence_hours") or 0)),
            "followups_sent_before": max(0, int(payload.get("followups_sent_before") or 0)),
            "detected_by": str(payload.get("detected_by") or event.actor or "")[:40],
            "is_recoverable": bool(
                payload.get("is_recoverable", kind == IgFunnelDropOff.Kind.SILENCE)
            ),
            "occurred_at": event.occurred_at,
        },
    )
    if drop_off.episode_id != event.episode_id:
        raise ValueError("Drop-off event belongs to another episode")
    return drop_off


def _recover_open_drop_off(
    episode,
    *,
    trigger_event=None,
    occurred_at=None,
    stage="",
    actor="",
    evidence=None,
):
    from management.models import IgFunnelDropOff, IgFunnelStepEvent

    drop_off = (
        IgFunnelDropOff.objects.select_for_update()
        .filter(episode=episode, recovered_at__isnull=True, is_recoverable=True)
        .order_by("-occurred_at", "-id")
        .first()
    )
    if not drop_off:
        return None
    occurred_at = _aware(
        occurred_at
        if occurred_at is not None
        else trigger_event.occurred_at
    )
    trigger_evidence = dict(evidence or {})
    if trigger_event is not None:
        stage = trigger_event.stage
        actor = trigger_event.actor
        trigger_evidence["trigger_event_id"] = trigger_event.pk
    recovery = _record_step_event_locked(
        episode,
        event_type=IgFunnelStepEvent.Type.RECOVERED,
        event_key=f"funnel:drop-off:{drop_off.pk}:recovered",
        occurred_at=occurred_at,
        stage=stage,
        actor=actor,
        evidence={"drop_off_id": drop_off.pk, **trigger_evidence},
    )
    IgFunnelDropOff.objects.filter(pk=drop_off.pk, recovered_at__isnull=True).update(
        recovered_at=occurred_at,
        recovered_by_followup=actor == "bot_followup",
        recovery_event=recovery,
    )
    return recovery


def record_client_step_event(
    client,
    *,
    event_type: str,
    event_key: str,
    occurred_at: datetime | None = None,
    stage: str = "",
    actor: str = "",
    evidence: dict | None = None,
    is_backfilled: bool = False,
):
    """Atomically ensure an episode and append one idempotent funnel fact."""
    from management.models import IgClient, IgFunnelStepEvent

    if not getattr(client, "pk", None):
        raise ValueError("A persisted client is required")
    with commercial_episode_client_lock(client.pk):
        with transaction.atomic():
            locked = IgClient.objects.select_for_update().get(pk=client.pk)
            return record_client_step_event_in_transaction(
                locked,
                event_type=event_type,
                event_key=event_key,
                occurred_at=occurred_at,
                stage=stage,
                actor=actor,
                evidence=evidence,
                is_backfilled=is_backfilled,
            )


def record_client_step_event_in_transaction(
    locked_client,
    *,
    event_type: str,
    event_key: str,
    occurred_at: datetime | None = None,
    stage: str = "",
    actor: str = "",
    evidence: dict | None = None,
    is_backfilled: bool = False,
):
    """Append a fact while the caller owns the client row transaction."""
    from management.models import IgFunnelStepEvent

    if not connection.in_atomic_block:
        raise RuntimeError("record_client_step_event_in_transaction requires transaction.atomic")
    episode = _ensure_episode_locked(locked_client)
    started = _ensure_conversation_started(
        episode,
        locked_client,
        occurred_at=occurred_at,
        event_key=(
            event_key
            if str(event_type) == IgFunnelStepEvent.Type.CONVERSATION_STARTED
            else ""
        ),
        evidence=evidence,
        is_backfilled=is_backfilled,
    )
    if str(event_type) == IgFunnelStepEvent.Type.CONVERSATION_STARTED:
        _recover_open_drop_off(
            episode,
            occurred_at=occurred_at,
            stage=stage or locked_client.stage,
            actor=actor,
            evidence=evidence,
        )
        return started
    event = _record_step_event_locked(
        episode,
        event_type=event_type,
        event_key=event_key,
        occurred_at=occurred_at,
        stage=stage or locked_client.stage,
        actor=actor,
        evidence=evidence,
        is_backfilled=is_backfilled,
    )
    if str(event_type) == IgFunnelStepEvent.Type.DROP_OFF:
        _record_drop_off(event, evidence=evidence)
    elif str(event_type) != IgFunnelStepEvent.Type.RECOVERED:
        _recover_open_drop_off(episode, trigger_event=event)
    return event


def record_first_bot_reply_in_transaction(
    locked_client,
    *,
    occurred_at: datetime,
    reply_message_id: int,
    source_message_id: int | None = None,
):
    """Record the first provider-confirmed bot reply for the open episode."""
    from management.models import IgFunnelStepEvent

    if not connection.in_atomic_block:
        raise RuntimeError("record_first_bot_reply_in_transaction requires transaction.atomic")
    episode = _ensure_episode_locked(locked_client)
    _ensure_conversation_started(
        episode,
        locked_client,
        occurred_at=locked_client.first_contact_at or episode.opened_at,
    )
    existing = (
        IgFunnelStepEvent.objects.filter(
            episode=episode,
            event_type=IgFunnelStepEvent.Type.BOT_REPLIED_FIRST,
        )
        .order_by("occurred_at", "id")
        .first()
    )
    if existing:
        return existing
    event = _record_step_event_locked(
        episode,
        event_type=IgFunnelStepEvent.Type.BOT_REPLIED_FIRST,
        event_key=f"ig-bot-reply:{int(reply_message_id)}",
        occurred_at=occurred_at,
        stage=locked_client.stage,
        actor="bot",
        evidence={
            "reply_message_id": int(reply_message_id),
            "source_message_id": int(source_message_id) if source_message_id else None,
            "provider_confirmed": True,
        },
    )
    _recover_open_drop_off(episode, trigger_event=event)
    return event


def canonical_first_reply_events(events):
    """Filter superseded first-reply observations before dates or pagination.

    The earliest event-time fact wins globally within its episode, including
    an append-only correction received after a later reply was projected.
    """
    from management.models import IgFunnelStepEvent

    earliest = IgFunnelStepEvent.objects.filter(
        episode_id=OuterRef("episode_id"), event_type=IgFunnelStepEvent.Type.BOT_REPLIED_FIRST,
    ).order_by("occurred_at", "id").values("pk")[:1]
    return events.annotate(_first_reply_event_id=Subquery(earliest)).filter(
        ~Q(event_type=IgFunnelStepEvent.Type.BOT_REPLIED_FIRST) | Q(pk=F("_first_reply_event_id"))
    )


def record_receipt_first_reply_in_transaction(episode, *, revision_id, occurred_at, stage, evidence):
    """Append an original-episode first reply, without stage or recovery effects."""
    from management.models import IgFunnelStepEvent

    if not connection.in_atomic_block:
        raise RuntimeError("receipt_first_reply_requires_client_transaction")
    key = f"ig-revision-first-reply:{revision_id}"
    existing_key = IgFunnelStepEvent.objects.filter(event_key=key).first()
    if existing_key:
        expected = {key: value for key, value in existing_key.evidence.items() if key != "supersedes_event_id"}
        if (existing_key.episode_id != episode.pk or existing_key.event_type != IgFunnelStepEvent.Type.BOT_REPLIED_FIRST
            or existing_key.occurred_at != occurred_at or existing_key.stage != stage
            or existing_key.actor != "bot" or expected != evidence):
            raise ValueError("receipt_first_reply_event_binding_changed")
        return {"outcome": "projected", "event_id": existing_key.pk,
                "supersedes_event_id": existing_key.evidence.get("supersedes_event_id")}
    previous = IgFunnelStepEvent.objects.filter(
        episode=episode, event_type=IgFunnelStepEvent.Type.BOT_REPLIED_FIRST,
    ).order_by("occurred_at", "id").first()
    if previous and previous.occurred_at <= occurred_at:
        return {"outcome": "first_reply_already_recorded", "event_id": previous.pk}
    supersedes = previous.pk if previous else None
    event = _record_step_event_locked(
        episode, event_type=IgFunnelStepEvent.Type.BOT_REPLIED_FIRST, event_key=key,
        occurred_at=occurred_at, stage=stage, actor="bot",
        evidence={**evidence, "supersedes_event_id": supersedes},
    )
    return {"outcome": "projected", "event_id": event.pk, "supersedes_event_id": supersedes}


def record_episode_step_event_in_transaction(
    episode,
    *,
    event_type: str,
    event_key: str,
    occurred_at: datetime | None = None,
    stage: str = "",
    actor: str = "",
    evidence: dict | None = None,
    is_backfilled: bool = False,
):
    """Append an idempotent fact when the durable episode is already known."""
    from management.models import IgCommercialEpisode, IgFunnelStepEvent

    if not connection.in_atomic_block:
        raise RuntimeError("record_episode_step_event_in_transaction requires transaction.atomic")
    locked_episode = IgCommercialEpisode.objects.select_for_update().get(pk=episode.pk)
    event = _record_step_event_locked(
        locked_episode,
        event_type=event_type,
        event_key=event_key,
        occurred_at=occurred_at,
        stage=stage,
        actor=actor,
        evidence=evidence,
        is_backfilled=is_backfilled,
    )
    if str(event_type) == IgFunnelStepEvent.Type.DROP_OFF:
        _record_drop_off(event, evidence=evidence)
    elif str(event_type) != IgFunnelStepEvent.Type.RECOVERED:
        _recover_open_drop_off(locked_episode, trigger_event=event)
    return event


def record_drop_off_for_client_in_transaction(
    locked_client,
    *,
    kind: str,
    reason_code: str = "",
    occurred_at: datetime | None = None,
    stage: str = "",
    actor: str = "system",
    evidence: dict | None = None,
    is_recoverable: bool | None = None,
):
    """Persist one classified loss fact while the client row is locked.

    The key is derived from the episode and classification, so a repeated
    webhook/daemon pass cannot create a second drop-off for the same fact.
    """
    from management.models import IgFunnelDropOff

    kind = str(kind or "").strip()
    if kind not in set(IgFunnelDropOff.Kind.values):
        raise ValueError("Unknown drop-off kind")
    episode = _ensure_episode_locked(locked_client)
    reason = str(reason_code or kind).strip()[:80]
    stage_value = str(stage or locked_client.stage or "")[:32]
    payload = dict(evidence or {})
    payload.update({
        "kind": kind,
        "reason_code": reason,
        "stage_at_drop": stage_value,
        "detected_by": str(actor or "system")[:40],
        "is_recoverable": (
            kind == IgFunnelDropOff.Kind.SILENCE
            if is_recoverable is None
            else bool(is_recoverable)
        ),
    })
    return record_client_step_event_in_transaction(
        locked_client,
        event_type="drop_off",
        event_key=f"ig-drop-off:{episode.pk}:{kind}:{reason}",
        occurred_at=occurred_at,
        stage=stage_value,
        actor=actor,
        evidence=payload,
    )


def record_drop_off_for_client(
    client,
    *,
    kind: str,
    reason_code: str = "",
    occurred_at: datetime | None = None,
    stage: str = "",
    actor: str = "system",
    evidence: dict | None = None,
    is_recoverable: bool | None = None,
):
    """Lock a client and persist a classified loss fact atomically."""
    from management.models import IgClient

    with commercial_episode_client_lock(client.pk):
        with transaction.atomic():
            locked = IgClient.objects.select_for_update().get(pk=client.pk)
            return record_drop_off_for_client_in_transaction(
                locked,
                kind=kind,
                reason_code=reason_code,
                occurred_at=occurred_at,
                stage=stage,
                actor=actor,
                evidence=evidence,
                is_recoverable=is_recoverable,
            )


def record_verified_payment_in_transaction(
    deal,
    *,
    provider_event,
    projection,
    occurred_at: datetime | None = None,
):
    """Materialize a provider-confirmed payment fact for the deal episode."""
    from management.models import IgCommercialEpisode, IgFunnelStepEvent

    if not connection.in_atomic_block:
        raise RuntimeError("record_verified_payment_in_transaction requires transaction.atomic")
    episode = (
        IgCommercialEpisode.objects.select_for_update()
        .filter(deal_id=deal.pk)
        .order_by("-sequence", "-id")
        .first()
    )
    if episode is None:
        return None
    return record_episode_step_event_in_transaction(
        episode,
        event_type=IgFunnelStepEvent.Type.PAYMENT_CONFIRMED,
        event_key=f"ig-payment-confirmed:{provider_event.pk}",
        occurred_at=occurred_at,
        stage="payment_pending",
        actor="provider",
        evidence={
            "provider_event_id": provider_event.pk,
            "projection_id": projection.pk,
            "provider": provider_event.provider,
            "invoice_id": provider_event.invoice_id,
            "provider_status": provider_event.provider_status,
            "gross_amount": str(provider_event.gross_amount or "0.00"),
            "final_amount": str(provider_event.final_amount or "0.00"),
        },
    )


def working_hours_between(start: datetime, end: datetime) -> Decimal:
    """Count elapsed Kyiv initiation-window hours, excluding weekends."""
    start_local = _aware(start).astimezone(KYIV_TZ)
    end_local = _aware(end).astimezone(KYIV_TZ)
    if end_local <= start_local:
        return Decimal("0.00")
    total_seconds = 0.0
    day = start_local.date()
    while day <= end_local.date():
        if day.weekday() < 5:
            window_start = datetime.combine(day, QUIET_START, tzinfo=KYIV_TZ)
            window_end = datetime.combine(day, QUIET_END, tzinfo=KYIV_TZ)
            overlap_start = max(start_local, window_start)
            overlap_end = min(end_local, window_end)
            if overlap_end > overlap_start:
                total_seconds += (overlap_end - overlap_start).total_seconds()
        day += timedelta(days=1)
    return (Decimal(str(total_seconds)) / Decimal("3600")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def classify_drop_off(
    *,
    stage: str,
    last_inbound_at: datetime,
    now: datetime | None = None,
    delivery_status: str = "",
    explicit_refusal: bool = False,
    opted_out: bool = False,
    spam: bool = False,
    superseded: bool = False,
) -> dict:
    """Classify operational failures before customer behavior."""
    from management.models import IgFunnelDropOff

    now = _aware(now)
    hours = working_hours_between(last_inbound_at, now)
    threshold = SILENCE_THRESHOLDS.get(str(stage or ""), Decimal("24"))
    if opted_out:
        kind = IgFunnelDropOff.Kind.OPT_OUT
    elif spam:
        kind = IgFunnelDropOff.Kind.SPAM
    elif superseded:
        kind = IgFunnelDropOff.Kind.SUPERSEDED
    elif delivery_status:
        kind = IgFunnelDropOff.Kind.UNREACHABLE
    elif explicit_refusal:
        kind = IgFunnelDropOff.Kind.EXPLICIT_REFUSAL
    elif hours >= threshold:
        kind = IgFunnelDropOff.Kind.SILENCE
    else:
        kind = ""
    return {
        "kind": kind,
        "silence_hours": hours,
        "threshold_hours": threshold,
        "pending_silence": not kind,
        "is_recoverable": kind == IgFunnelDropOff.Kind.SILENCE,
    }


def scan_open_dropoffs(*, now: datetime | None = None, limit: int = 100, apply: bool = False) -> dict:
    """Find and optionally materialize durable operational drop-off facts.

    The default is a read-only report.  ``apply=True`` is deliberately
    idempotent and records only reconstructible classifications; customer
    silence is measured in Kyiv working hours.
    """
    from management.models import IgClient, IgFollowUpTask

    now = _aware(now)
    rows = []
    candidates = (
        IgClient.objects.filter(hidden_at__isnull=True)
        .exclude(stage=IgClient.Stage.DONE)
        .order_by("last_message_at", "id")[: max(1, int(limit or 100))]
    )
    for client in candidates:
        last_inbound = client.last_message_at or client.created_at or now
        classification = classify_drop_off(
            stage=client.stage,
            last_inbound_at=last_inbound,
            now=now,
            delivery_status=client.delivery_status,
            explicit_refusal=client.primary_objection in {
                IgClient.Objection.NO_BUY,
            },
            opted_out=bool(client.opted_out_at),
            spam=bool(client.is_blocked or client.stage == IgClient.Stage.SPAM or client.spam_strikes),
        )
        kind = classification["kind"]
        if not kind:
            continue
        followups = IgFollowUpTask.objects.filter(
            client_id=client.pk,
            status=IgFollowUpTask.Status.SENT,
        ).count()
        row = {
            "client_id": client.pk,
            "episode_id": client.current_commercial_episode_id,
            "kind": kind,
            "reason_code": client.delivery_status or kind,
            "stage": client.stage,
            "silence_hours": str(classification["silence_hours"]),
            "threshold_hours": str(classification["threshold_hours"]),
            "followups_sent_before": followups,
            "is_recoverable": classification["is_recoverable"],
        }
        if apply:
            with commercial_episode_client_lock(client.pk):
                with transaction.atomic():
                    locked = IgClient.objects.select_for_update().get(pk=client.pk)
                    event = record_drop_off_for_client_in_transaction(
                        locked,
                        kind=kind,
                        reason_code=row["reason_code"],
                        occurred_at=now,
                        stage=locked.stage,
                        actor="silence_scan",
                        evidence={
                            "silence_hours": row["silence_hours"],
                            "threshold_hours": row["threshold_hours"],
                            "followups_sent_before": followups,
                        },
                        is_recoverable=classification["is_recoverable"],
                    )
                    row["event_id"] = event.pk
        rows.append(row)
    return {"scanned": len(candidates), "matched": len(rows), "applied": bool(apply), "rows": rows}


def backfill_reconstructible_funnel_events(*, limit: int = 1000, apply: bool = False) -> dict:
    """Report/apply only facts with an authoritative persisted source.

    No text inference is performed.  Historical price quotes, objections,
    discounts and silence therefore remain explicitly unavailable.
    """
    from management.models import (
        IgCheckoutProposal,
        IgCommercialEpisode,
        IgDeal,
        IgFunnelStepEvent,
        IgPaymentProjection,
    )

    candidates = []
    for proposal in IgCheckoutProposal.objects.select_related("commercial_episode").order_by("pk")[:limit]:
        candidates.append((
            proposal.commercial_episode,
            IgFunnelStepEvent.Type.PAYLINK_ISSUED,
            f"ig-paylink-issued:{proposal.pk}",
            proposal.created_at,
            {"proposal_id": proposal.pk, "proposal_public_id": str(proposal.public_id), "backfill_source": "checkout_proposal"},
        ))
    for episode in IgCommercialEpisode.objects.select_related("intended_order").order_by("pk")[:limit]:
        order = episode.intended_order
        if order:
            candidates.append((episode, IgFunnelStepEvent.Type.ORDER_CREATED, f"ig-order-created:{order.pk}", order.created, {"order_id": order.pk, "backfill_source": "canonical_order"}))
            if order.tracking_number:
                candidates.append((episode, IgFunnelStepEvent.Type.TTN_CREATED, f"ig-ttn-created:{order.pk}:{order.tracking_number}", order.shipment_status_updated or order.updated, {"order_id": order.pk, "tracking_number": order.tracking_number, "backfill_source": "canonical_order"}))
            delivered_at = nova_poshta_delivery_confirmed_at(order)
            if delivered_at is not None:
                candidates.append(
                    (
                        episode,
                        IgFunnelStepEvent.Type.DELIVERED,
                        delivery_funnel_event_key(episode, order),
                        delivered_at,
                        {
                            "order_id": order.pk,
                            "tracking_number": order.tracking_number,
                            "tracking_status_code": order.tracking_status_code,
                            "tracking_terminal_at": order.tracking_terminal_at.isoformat(),
                            "backfill_source": "canonical_order",
                        },
                    )
                )
        deal = episode.deal
        projection = (
            IgPaymentProjection.objects.select_related("last_event")
            .filter(deal_id=deal.pk)
            .first()
            if deal
            else None
        )
        provider_event = projection.last_event if projection else None
        if deal and projection and provider_event and projection.truth in {IgDeal.PaymentTruth.CONFIRMED, IgDeal.PaymentTruth.PARTIALLY_REFUNDED}:
            candidates.append((episode, IgFunnelStepEvent.Type.PAYMENT_CONFIRMED, f"ig-payment-confirmed:{provider_event.pk}", projection.paid_at or provider_event.provider_modified_at or provider_event.received_at, {"provider_event_id": provider_event.pk, "projection_id": projection.pk, "backfill_source": "provider_projection"}))
    result = {"candidates": len(candidates), "created": 0, "applied": bool(apply), "unsupported": ["price_quoted", "objection_handled", "discount_offered", "drop_off"]}
    if not apply:
        return result
    for episode, event_type, event_key, occurred_at, evidence in candidates:
        with transaction.atomic():
            existed = IgFunnelStepEvent.objects.filter(event_key=event_key).exists()
            event = record_episode_step_event_in_transaction(
                episode,
                event_type=event_type,
                event_key=event_key,
                occurred_at=occurred_at,
                stage="",
                actor="historical_backfill",
                evidence=evidence,
                is_backfilled=True,
            )
            if not existed:
                result["created"] += 1
    return result


def build_funnel_analytics(since=None, until=None, *, client_ids=None) -> dict:
    """Build reconciled entry cohorts without consulting mutable client stages."""
    from management.models import (
        IgCommercialEpisode,
        IgCommercialEpisodeEvent,
        IgFollowUpTask,
        IgFunnelDropOff,
        IgFunnelStepEvent,
    )

    observation_cutoff = until or timezone.now()
    events = canonical_first_reply_events(IgFunnelStepEvent.objects.filter(
        episode__client__hidden_at__isnull=True
    )).filter(occurred_at__lt=observation_cutoff)
    drop_offs = IgFunnelDropOff.objects.filter(
        episode__client__hidden_at__isnull=True
    ).filter(occurred_at__lt=observation_cutoff)
    if client_ids is not None:
        events = events.filter(episode__client_id__in=client_ids)
        drop_offs = drop_offs.filter(episode__client_id__in=client_ids)
    fulfillment_events = IgCommercialEpisodeEvent.objects.filter(
        episode__client__hidden_at__isnull=True,
        event_type="fulfillment_updated",
        created_at__lt=observation_cutoff,
    )
    if client_ids is not None:
        fulfillment_events = fulfillment_events.filter(
            episode__client_id__in=client_ids
        )
    latest_fulfillment_state = {}
    for event in fulfillment_events.order_by("episode_id", "created_at", "id").values(
        "episode_id",
        "to_state",
    ):
        latest_fulfillment_state[event["episode_id"]] = event["to_state"]

    candidate_events = list(
        events.values(
            "id",
            "created_at",
            "episode_id",
            "event_type",
            "occurred_at",
            "actor",
            "evidence",
            "is_backfilled",
        )
    )
    delivery_episode_ids = {
        event["episode_id"]
        for event in candidate_events
        if event["event_type"] == IgFunnelStepEvent.Type.DELIVERED
    }
    current_delivery_at = {}
    if delivery_episode_ids:
        for episode in (
            IgCommercialEpisode.objects.filter(pk__in=delivery_episode_ids)
            .select_related("intended_order")
        ):
            order = episode.intended_order
            current_delivery_at[episode.pk] = (
                nova_poshta_delivery_confirmed_at(order) if order else None
            )
    latest_delivery_event_id = {}
    for event in candidate_events:
        if event["event_type"] != IgFunnelStepEvent.Type.DELIVERED:
            continue
        current = latest_delivery_event_id.get(event["episode_id"])
        candidate_order = (event["created_at"], event["id"])
        if current is None or candidate_order > current[0]:
            latest_delivery_event_id[event["episode_id"]] = (
                candidate_order,
                event["id"],
            )
    observed_events = []
    for event in candidate_events:
        if event["event_type"] != IgFunnelStepEvent.Type.DELIVERED:
            observed_events.append(event)
            continue
        authoritative_delivered_at = current_delivery_at.get(event["episode_id"])
        if (
            authoritative_delivered_at is None
            or event["occurred_at"] != authoritative_delivered_at
        ):
            continue
        latest_state = latest_fulfillment_state.get(event["episode_id"])
        if latest_state not in {None, IgCommercialEpisode.State.FULFILLED}:
            continue
        if latest_delivery_event_id[event["episode_id"]][1] == event["id"]:
            observed_events.append(event)
    raw_events = [
        event
        for event in observed_events
        if since is None or event["occurred_at"] >= since
    ]
    episode_sets = {}
    for event in raw_events:
        episode_sets.setdefault(event["event_type"], set()).add(
            event["episode_id"]
        )
    observed_drop_rows = list(drop_offs.values(
        "episode_id",
        "kind",
        "reason_code",
        "stage_at_drop",
        "is_recoverable",
        "occurred_at",
        "recovered_at",
    ))
    drop_rows = [
        drop
        for drop in observed_drop_rows
        if since is None or drop["occurred_at"] >= since
    ]
    observed_times = {}
    for event in observed_events:
        observed_times.setdefault(
            (event["episode_id"], event["event_type"]),
            [],
        ).append(event["occurred_at"])
    entered_times = {}
    for event in raw_events:
        key = (event["episode_id"], event["event_type"])
        entered_times[key] = min(
            entered_times.get(key, event["occurred_at"]),
            event["occurred_at"],
        )
    rows = []
    for index, (event_type, label) in enumerate(FUNNEL_STEPS):
        entered_ids = {
            episode_id
            for episode_id, candidate_type in entered_times
            if candidate_type == event_type
        }
        next_type = FUNNEL_STEPS[index + 1][0] if index + 1 < len(FUNNEL_STEPS) else None
        advanced_ids = set()
        if next_type:
            for episode_id in entered_ids:
                entered_at = entered_times[(episode_id, event_type)]
                if any(
                    occurred_at >= entered_at
                    for occurred_at in observed_times.get((episode_id, next_type), [])
                ):
                    advanced_ids.add(episode_id)
        drop_stages = [
            stage
            for stage, mapped_step in DROP_OFF_STAGE_TO_STEP.items()
            if mapped_step == event_type
        ]
        dropped_ids = set()
        for drop in observed_drop_rows:
            episode_id = drop["episode_id"]
            if episode_id not in entered_ids or episode_id in advanced_ids:
                continue
            entered_at = entered_times[(episode_id, event_type)]
            recovered_at = drop["recovered_at"]
            if (
                drop["stage_at_drop"] in drop_stages
                and drop["occurred_at"] >= entered_at
                and (recovered_at is None or recovered_at >= observation_cutoff)
            ):
                dropped_ids.add(episode_id)
        entered = len(entered_ids)
        advanced = len(advanced_ids)
        dropped = len(dropped_ids)
        in_progress = len(entered_ids - advanced_ids - dropped_ids)
        reconciled = entered == advanced + dropped + in_progress
        low_sample = entered < 20
        rows.append({
            "step": event_type,
            "label": label,
            "entered": entered,
            "advanced": advanced,
            "drop_off": dropped,
            "in_progress": in_progress,
            "right_censored_count": in_progress,
            "observation_cutoff": observation_cutoff.isoformat(),
            "cohort_basis": "entry_event_same_window",
            "time_field": "occurred_at",
            "reconciled": reconciled,
            "completeness": "complete" if reconciled else "integrity_error",
            "cr_percent": (
                None
                if low_sample or not reconciled
                else round(advanced * 100 / entered, 1)
            ),
            "low_sample": low_sample,
        })
    first_times = {}
    for row in raw_events:
        key = (row["episode_id"], row["event_type"])
        first_times[key] = min(first_times.get(key, row["occurred_at"]), row["occurred_at"])
    time_on_step = []
    for index, (event_type, label) in enumerate(FUNNEL_STEPS[:-1]):
        next_type = FUNNEL_STEPS[index + 1][0]
        durations = []
        entered_ids = {
            episode_id
            for episode_id, candidate_type in entered_times
            if candidate_type == event_type
        }
        right_censored_count = 0
        for episode_id in entered_ids:
            start = first_times.get((episode_id, event_type))
            next_times = observed_times.get((episode_id, next_type), [])
            end = min(
                (candidate for candidate in next_times if candidate >= start),
                default=None,
            ) if start else None
            if start and end and end >= start:
                durations.append((end - start).total_seconds() / 3600)
            else:
                right_censored_count += 1
        durations.sort()
        if durations:
            median = durations[len(durations) // 2] if len(durations) % 2 else (durations[len(durations) // 2 - 1] + durations[len(durations) // 2]) / 2
            p90 = durations[min(len(durations) - 1, max(0, int(len(durations) * 0.9) - 1))]
            time_on_step.append({
                "step": event_type,
                "label": label,
                "sample": len(durations),
                "median_hours": round(median, 2),
                "p90_hours": round(p90, 2),
                "right_censored_count": right_censored_count,
            })
        else:
            time_on_step.append({
                "step": event_type,
                "label": label,
                "sample": 0,
                "median_hours": None,
                "p90_hours": None,
                "right_censored_count": right_censored_count,
            })

    reason_counts = {}
    for drop in drop_rows:
        key = (drop["stage_at_drop"], drop["kind"], drop["reason_code"])
        row = reason_counts.setdefault(key, {
            "step": DROP_OFF_STAGE_TO_STEP.get(drop["stage_at_drop"], ""),
            "stage": drop["stage_at_drop"],
            "kind": drop["kind"],
            "reason_code": drop["reason_code"],
            "recoverable": 0,
            "unrecoverable": 0,
            "recovered": 0,
        })
        row["recoverable" if drop["is_recoverable"] else "unrecoverable"] += 1
        if drop["recovered_at"]:
            row["recovered"] += 1

    sent_tasks = IgFollowUpTask.objects.filter(
        status=IgFollowUpTask.Status.SENT,
        client__hidden_at__isnull=True,
    )
    if client_ids is not None:
        sent_tasks = sent_tasks.filter(client_id__in=client_ids)
    if since is not None:
        sent_tasks = sent_tasks.filter(sent_at__gte=since)
    if until is not None:
        sent_tasks = sent_tasks.filter(sent_at__lt=until)
    followup_effectiveness = []
    for row in sent_tasks.values("kind", "level").annotate(sent_count=Count("id")):
        followup_effectiveness.append({
            "kind": row["kind"],
            "level": row["level"],
            "sent": row["sent_count"],
            "recovered": 0,
            "bought": 0,
        })
    offer_episodes = episode_sets.get(
        IgFunnelStepEvent.Type.DISCOUNT_OFFERED,
        set(),
    )
    bought_episodes = (
        episode_sets.get(IgFunnelStepEvent.Type.PAYMENT_CONFIRMED, set())
        | episode_sets.get(IgFunnelStepEvent.Type.ORDER_CREATED, set())
    )
    discount_offers = sum(
        event["event_type"] == IgFunnelStepEvent.Type.DISCOUNT_OFFERED
        for event in raw_events
    )
    bought_after_offer = set()
    for episode_id in offer_episodes & bought_episodes:
        offer_at = min(
            entered_times[(episode_id, IgFunnelStepEvent.Type.DISCOUNT_OFFERED)]
            for candidate in (episode_id,)
            if (episode_id, IgFunnelStepEvent.Type.DISCOUNT_OFFERED) in entered_times
        )
        payment_times = observed_times.get(
            (episode_id, IgFunnelStepEvent.Type.PAYMENT_CONFIRMED),
            [],
        ) + observed_times.get((episode_id, IgFunnelStepEvent.Type.ORDER_CREATED), [])
        if any(payment_at >= offer_at for payment_at in payment_times):
            bought_after_offer.add(episode_id)
    discount_bought = len(bought_after_offer)
    still_open = offer_episodes - bought_after_offer
    manager_episodes = episode_sets.get(
        IgFunnelStepEvent.Type.MANAGER_ENGAGED,
        set(),
    )
    bot_episodes = episode_sets.get(
        IgFunnelStepEvent.Type.BOT_REPLIED_FIRST,
        set(),
    )
    return {
        "steps": rows,
        "backfilled": any(event["is_backfilled"] for event in raw_events),
        "event_types": len(IgFunnelStepEvent.Type.values),
        "drop_off_reasons": sorted(reason_counts.values(), key=lambda row: (row["stage"], row["kind"], row["reason_code"])),
        "followup_effectiveness": followup_effectiveness,
        "discounts": {
            "offered": discount_offers,
            "bought": discount_bought,
            "bought_after_offer": discount_bought,
            "still_open": len(still_open),
            "bought_without_known_offer": len(bought_episodes - offer_episodes),
            "without_discount_bought": max(0, len(bought_episodes - offer_episodes)),
            "observation_cutoff": observation_cutoff.isoformat(),
        },
        "manager_vs_bot": {
            "manager_engaged": len(manager_episodes),
            "bot_replied": len(bot_episodes),
            "bot_only": len(bot_episodes - manager_episodes),
            "shared": len(manager_episodes & bot_episodes),
            "manager_only": len(manager_episodes - bot_episodes),
            "manager_touched": len(manager_episodes & bot_episodes),
            "episodes_with_response_evidence": len(bot_episodes | manager_episodes),
        },
        "time_on_step": time_on_step,
    }
