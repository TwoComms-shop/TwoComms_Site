"""Bounded, read-only purchase map over producers available at migration 0201.

Milestones describe observations, not current checkout readiness. In particular,
neither a client stage nor a later payment completes earlier guide nodes. This
adapter intentionally does not invoke the (not yet live) node projector or any
episode materializer. Its returned JSON contains no raw evidence or action URLs.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from management.models import IgCommercialEpisode, IgCommercialEpisodeEvent, IgFunnelStepEvent


EPISODE_LIMIT = 20
EVENT_LIMIT = 50
LINE_LIMIT = 10
GUIDE = (
    ("inquiry", "Звернення"), ("selection", "Підбір/Бриф"),
    ("terms", "Умови"), ("offer", "Пропозиція/Макет"),
    ("payment", "Розрахунок"), ("fulfillment", "Виконання"),
)
STEP_NODES = {
    "conversation_started": "inquiry", "bot_replied_first": "inquiry",
    "product_pinned": "selection", "variant_selected": "selection",
    "price_quoted": "terms", "discount_offered": "terms",
    "paylink_issued": "offer", "paylink_viewed": "offer",
    "payment_confirmed": "payment", "order_created": "fulfillment",
    "ttn_created": "fulfillment", "delivered": "fulfillment",
}
STEP_LABELS = dict(IgFunnelStepEvent.Type.choices)
EPISODE_LABELS = {
    "episode_opened": "Цикл відкрито", "deal_bound": "Угоду пов’язано",
    "payment_review_bound": "Перевірку оплати пов’язано",
    "payment_updated": "Дані оплати оновлено", "order_bound": "Замовлення пов’язано",
    "fulfillment_updated": "Дані виконання оновлено",
    "stage_changed": "Фокус діалогу змінено", "episode_closed": "Цикл закрито",
}
EPISODE_FIELDS = (
    "id", "client_id", "sequence", "open_slot", "state", "repeat_kind",
    "opened_at", "closed_at", "updated_at", "intended_order_id",
    "primary_payment_review_id", "stage_snapshot", "product_snapshot",
    "price_snapshot", "payment_snapshot", "fulfillment_snapshot",
)
SAFE_ACTORS = frozenset({
    "customer", "bot", "manager", "provider", "historical_backfill",
    "order_truth", "order_resolution", "linked_existing", "auto_created",
    "payment_review", "provider_projection", "manager_decision",
    "payment_reconciliation", "commercial_flow", "conversation_analysis",
})
EVIDENCE_IDS = (
    "message_id", "product_id", "color_variant_id", "order_id", "review_id",
    "decision_id", "projection_id", "provider_event_id", "proposal_id",
)


class InvalidJourneyEpisode(ValueError):
    """Requested episode is invalid, missing, or not owned by this client."""


def _iso(value):
    return value.isoformat() if value is not None else ""


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _positive_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}", value) and int(value) > 0:
        return int(value)
    return None


def _money(value):
    try:
        number = Decimal(str(value))
        if number.is_finite() and Decimal("0") <= number <= Decimal("1000000000"):
            return f"{number:.2f}"
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


def _short(value, length=100):
    # Known product labels only; never event source, raw text, token or URL fields.
    if not isinstance(value, str) or "http" in value.casefold() or "://" in value:
        return ""
    return " ".join(value.split())[:length]


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return ""
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _amount_label(value, snapshot):
    currency = snapshot.get("currency")
    return f"{value} {currency}" if currency in {"UAH", "USD", "EUR"} else f"{value} · валюта не вказана"


def _ref(kind, identifier):
    return {"kind": kind, "id": identifier}


def _fact(node, key, label, value, *, source, ref=None, state="partial", tone="neutral", captured_at=""):
    fact = {
        "id": f"{node['id']}:{key}", "label": label, "value": value,
        "state": state, "tone": tone, "classification": "context", "source": source, "captured_at": captured_at,
        "evidence_refs": [ref] if ref else [],
    }
    node["facts"].append(fact)
    if ref and ref not in node["evidence_refs"]:
        node["evidence_refs"].append(ref)
    if node["state"] == "open":
        node["state"] = "partial"
    node["coverage"] = "partial"
    return fact


def _selector(episode):
    return {
        "id": episode["id"], "sequence": episode["sequence"],
        "label": f"Покупка {episode['sequence']}",
        "state": episode["state"], "repeat_kind": episode["repeat_kind"],
        "current": episode["open_slot"] == 1,
        "opened_at": _iso(episode["opened_at"]), "closed_at": _iso(episode["closed_at"]),
    }


def _event(row, kind):
    evidence = _mapping(row["evidence"])
    event_type = row["event_type"]
    known = event_type in (STEP_LABELS if kind == "funnel_event" else EPISODE_LABELS)
    actor = row.get("actor", row.get("source", ""))
    verified_payment = bool(
        kind == "funnel_event" and event_type == "payment_confirmed"
        and actor == "provider" and _positive_id(evidence.get("provider_event_id"))
        and _positive_id(evidence.get("projection_id"))
    )
    return {
        "id": f"{kind}:{row['id']}", "type": event_type if known else "other",
        "label": str((STEP_LABELS if kind == "funnel_event" else EPISODE_LABELS).get(
            event_type, "Подія циклу")),
        "node_id": STEP_NODES.get(event_type) if kind == "funnel_event" else None,
        "occurred_at": _iso(row.get("occurred_at", row["created_at"])),
        "recorded_at": _iso(row["created_at"]),
        "actor": actor if actor in SAFE_ACTORS else "unknown",
        "provenance": "provider_event" if verified_payment else "recorded_observation",
        "is_backfilled": bool(row.get("is_backfilled", False)),
        "evidence_refs": [_ref(kind, row["id"])],
        "values": {
            key: identifier for key in EVIDENCE_IDS
            if (identifier := _positive_id(evidence.get(key))) is not None
        },
    }


def _history(episode_id):
    steps = IgFunnelStepEvent.objects.filter(episode_id=episode_id)
    commercial = IgCommercialEpisodeEvent.objects.filter(episode_id=episode_id)
    total = steps.count() + commercial.count()
    rows = [
        _event(row, "funnel_event") for row in steps.order_by("-occurred_at", "-id").values(
            "id", "event_type", "actor", "occurred_at", "created_at", "evidence", "is_backfilled",
        )[:EVENT_LIMIT]
    ]
    rows.extend(_event(row, "episode_event") for row in commercial.order_by(
        "-created_at", "-id",
    ).values("id", "event_type", "source", "created_at", "evidence")[:EVENT_LIMIT])
    rows.sort(key=lambda row: (row["occurred_at"], row["id"].split(":")[0], int(row["id"].split(":")[1])), reverse=True)
    rows = list(reversed(rows[:EVENT_LIMIT]))
    # Commercial events often mirror funnel milestones. Only the typed funnel
    # producer supplies visits; neither stream supplies semantic transition edges.
    visits = [{
        "id": f"visit:{row['id']}", "node_id": row["node_id"],
        "event_id": row["id"], "occurred_at": row["occurred_at"],
        "kind": "observed_milestone",
    } for row in rows if row["node_id"]]
    return {
        "events": rows, "total": total, "has_more": total > len(rows),
        "coverage": "partial", "visits": visits, "edges": [],
        "edge_coverage": "missing_source",
    }


def _snapshots(episode, nodes):
    ref = _ref("episode", episode["id"])
    # Episode.updated_at may reflect an unrelated review or fulfillment update;
    # it is not the time these individual historical facts were captured.
    captured = ""
    products = episode["product_snapshot"]
    if isinstance(products, list):
        for index, item in enumerate(products[:LINE_LIMIT]):
            item = _mapping(item)
            safe = {key: value for key in ("product_id", "color_variant_id", "qty")
                    if (value := _positive_id(item.get(key))) is not None}
            safe.update({key: value for key in ("title", "size", "fit_option_label")
                         if (value := _short(item.get(key)))})
            if safe:
                _fact(nodes["selection"], f"line:{index}", "Вибрана позиція · знімок", safe,
                      source="episode.product_snapshot", ref=ref, captured_at=captured)
        if products:
            nodes["selection"]["summary"] = "Є знімок вибору; актуальність варіантів не перевірено"
            nodes["selection"]["lines"] = {"total": len(products), "has_more": len(products) > LINE_LIMIT}
    price = _mapping(episode["price_snapshot"])
    amount = _money(price.get("negotiated_total"))
    if amount is not None and Decimal(amount) > 0:
        _fact(nodes["terms"], "quoted_total", "Узгоджена сума · знімок", _amount_label(amount, price),
              source="episode.price_snapshot", ref=ref, captured_at=captured)
        nodes["terms"]["summary"] = "Збережено суму; актуальність ціни та умов не перевірено"
    payment = _mapping(episode["payment_snapshot"])
    if payment.get("episode_id") is not None and _positive_id(payment["episode_id"]) != episode["id"]:
        payment = {}
        nodes["payment"]["missing_sources"].append("owned_payment_snapshot")
    stamp = _timestamp(payment.get("captured_at"))
    provider = (payment.get("provider_source") == "monobank_projection"
                and _positive_id(payment.get("projection_id")))
    truth = payment.get("provider_truth")
    if provider and truth in {"confirmed", "partially_refunded", "refunded", "reversed", "failed", "cancelled", "pending", "unknown", "unverified"}:
        labels = {"confirmed": "Підтверджено провайдером", "partially_refunded": "Частково повернено",
                  "refunded": "Повернено", "reversed": "Скасовано провайдером", "failed": "Неуспішно",
                  "cancelled": "Скасовано", "pending": "Очікується", "unknown": "Стан невідомий", "unverified": "Не підтверджено"}
        state, tone = {
            "confirmed": ("complete", "success"),
            "partially_refunded": ("partial", "warning"),
            "refunded": ("partial", "neutral"),
            "reversed": ("invalidated", "warning"),
            "failed": ("invalidated", "warning"),
            "cancelled": ("invalidated", "warning"),
        }.get(truth, ("open", "neutral"))
        if payment.get("needs_reconciliation") is True:
            state, tone = "partial", "warning"
        _fact(nodes["payment"], "provider_truth", "Провайдер · знімок", labels[truth],
              source="episode.payment_snapshot", ref=ref, captured_at=stamp, state=state, tone=tone)
        nodes["payment"]["summary"] = labels[truth] + " · знімок оплати"
        for key, label in (("provider_confirmed_amount", "Підтверджена провайдером сума"),
                           ("remaining_amount", "Залишок · знімок")):
            if (value := _money(payment.get(key))) is not None:
                positive_confirmation = key == "provider_confirmed_amount" and Decimal(value) > 0 and tone == "success"
                _fact(nodes["payment"], key, label, _amount_label(value, payment), source="episode.payment_snapshot", ref=ref,
                      captured_at=stamp, state="complete" if positive_confirmation else "partial",
                      tone="success" if positive_confirmation else "neutral")
        # A payment milestone alone may be a deposit, later refunded, or stale.
        # Full readiness remains unavailable until a live settlement adapter exists.
    manager = payment.get("manager_truth")
    if manager in {"manager_verified", "manager_rejected"} and _positive_id(payment.get("manager_decision_id", payment.get("decision_id"))):
        _fact(nodes["payment"], "manager_decision", "Рішення менеджера · знімок",
              "Підтверджено менеджером" if manager == "manager_verified" else "Відхилено менеджером",
              source="episode.payment_snapshot", ref=ref, captured_at=stamp,
              state="complete" if manager == "manager_verified" else "invalidated",
              tone="success" if manager == "manager_verified" else "warning")
        if not provider:
            nodes["payment"]["summary"] = "Є рішення менеджера; підтвердження провайдера відсутнє"
    if payment.get("needs_reconciliation") is True:
        _fact(nodes["payment"], "reconciliation", "Звірка оплати", "Потрібна звірка",
              source="episode.payment_snapshot", ref=ref, captured_at=stamp, tone="warning")
        nodes["payment"]["summary"] = "Потрібна звірка оплати"
    fulfillment = _mapping(episode["fulfillment_snapshot"])
    from orders.models import Order
    order_status = fulfillment.get("order_status")
    order_status = {"processing": "new", "shipped": "ship"}.get(order_status, order_status) if isinstance(order_status, str) else None
    if order_status in dict(Order.STATUS_CHOICES):
        _fact(nodes["fulfillment"], "order_snapshot", "Стан замовлення · знімок",
              str(dict(Order.STATUS_CHOICES)[order_status]), source="episode.fulfillment_snapshot", ref=ref, captured_at=captured,
              state="invalidated" if order_status == "cancelled" else "partial",
              tone="warning" if order_status == "cancelled" else "neutral")


def _bound_records(episode, nodes):
    """Latest state of the selected purchase, including when viewing its history."""
    from management.models import IgPaymentConfirmationReview
    from orders.models import Order
    from orders.fulfillment_truth import nova_poshta_delivery_confirmed_at

    if episode["primary_payment_review_id"]:
        review = IgPaymentConfirmationReview.objects.filter(
            pk=episode["primary_payment_review_id"], client_id=episode["client_id"],
        ).values("id", "status", "updated_at").first()
        if review:
            labels = dict(IgPaymentConfirmationReview.Status.choices)
            state, tone = {
                "confirmed": ("complete", "success"), "cancelled": ("invalidated", "warning"),
                "superseded": ("superseded", "neutral"),
            }.get(review["status"], ("open", "neutral"))
            _fact(nodes["payment"], "review", "Перевірка менеджером", str(labels.get(review["status"], "Невідомо")),
                  source="payment_review.current", ref=_ref("payment_review", review["id"]), captured_at=_iso(review["updated_at"]),
                  state=state, tone=tone)
            if review["status"] == "pending":
                nodes["payment"]["summary"] = "Очікує перевірки менеджером"
                return_focus = "payment"
            else:
                return_focus = None
        else:
            nodes["payment"]["missing_sources"].append("owned_payment_review")
            return_focus = None
    else:
        return_focus = None
    if episode["intended_order_id"]:
        order = Order.objects.filter(pk=episode["intended_order_id"]).only(
            "id", "status", "payment_status", "updated", "tracking_number",
            "tracking_status_code", "tracking_terminal_at", "tracking_provider_event_at",
        ).first()
        if order:
            ref = _ref("order", order.pk)
            delivered = bool(nova_poshta_delivery_confirmed_at(order))
            _fact(nodes["fulfillment"], "order", "Замовлення", str(dict(Order.STATUS_CHOICES).get(order.status, "Невідомо")),
                  source="intended_order.current", ref=ref, captured_at=_iso(order.updated),
                  state="complete" if delivered else "invalidated" if order.status == "cancelled" else "partial",
                  tone="success" if delivered else "warning" if order.status == "cancelled" else "neutral")
            nodes["fulfillment"]["summary"] = str(dict(Order.STATUS_CHOICES).get(order.status, "Невідомо"))
            _fact(nodes["payment"], "order_payment", "Статус у замовленні", str(dict(Order.PAYMENT_STATUS_CHOICES).get(order.payment_status, "Невідомо")),
                  source="intended_order.current", ref=ref, captured_at=_iso(order.updated),
                  state="complete" if order.payment_status == "paid" else "partial",
                  tone="success" if order.payment_status == "paid" else "neutral")
            if delivered:
                nodes["fulfillment"]["state"] = "complete"
                nodes["fulfillment"]["summary"] = "Отримання підтверджено перевізником"
        else:
            nodes["fulfillment"]["missing_sources"].append("bound_order")
    return return_focus


def build_journey_snapshot(client, *, view_episode_id=None):
    """Return JSON-safe v1; ownership is checked before any selected data read."""
    client_id = _positive_id(getattr(client, "pk", None))
    if client_id is None:
        raise ValueError("A persisted client is required")
    queryset = IgCommercialEpisode.objects.filter(client_id=client_id)
    total = queryset.count()
    recent = list(queryset.order_by("-sequence", "-id").values(*EPISODE_FIELDS)[:EPISODE_LIMIT])
    current = next((row for row in recent if row["open_slot"] == 1), None)
    if current is None:
        current = queryset.filter(open_slot=1).values(*EPISODE_FIELDS).first()
    if view_episode_id is not None:
        requested = _positive_id(view_episode_id)
        if requested is None:
            raise InvalidJourneyEpisode("Недоступний цикл покупки")
        episode = next((row for row in recent if row["id"] == requested), None)
        if episode is None:
            episode = queryset.filter(pk=requested).values(*EPISODE_FIELDS).first()
        if episode is None:
            raise InvalidJourneyEpisode("Недоступний цикл покупки")
    else:
        episode = current
    nodes = {key: {
        "id": key, "label": label, "state": "open", "current": False,
        "coverage": "unknown", "missing_sources": ["live_node_projection"],
        "summary": "Даних для цього етапу поки немає", "facts": [], "evidence_refs": [],
    } for key, label in GUIDE}
    history = {"events": [], "total": 0, "has_more": False, "coverage": "unknown", "visits": [], "edges": [], "edge_coverage": "missing_source"}
    covered = ["commercial_episodes"]
    focus = None
    if episode:
        _snapshots(episode, nodes)
        history = _history(episode["id"])
        covered.extend(["episode_snapshots", "funnel_step_events", "commercial_episode_events"])
        for event in history["events"]:
            if event["node_id"]:
                node = nodes[event["node_id"]]
                if not node["facts"]:
                    node["summary"] = "Є події; поточні умови не перевірено"
                node["coverage"] = "partial"
                # Observed visits remain separate from the node's current state.
                node["evidence_refs"].extend(event["evidence_refs"])
        if history["visits"]:
            focus = history["visits"][-1]["node_id"]
        review_focus = _bound_records(episode, nodes)
        focus = review_focus or focus
        for node in nodes.values():
            covered.extend(fact["source"] for fact in node["facts"] if fact["source"].endswith(".current"))
    else:
        from management.models import InstagramBotMessage
        first = InstagramBotMessage.objects.filter(client_id=client_id, role="user").order_by("id").values("id", "created_at").first()
        if first:
            _fact(nodes["inquiry"], "dialogue", "Діалог", "Є вхідне повідомлення",
                  source="client_message", ref=_ref("message", first["id"]), captured_at=_iso(first["created_at"]))
            nodes["inquiry"]["summary"] = "Діалог розпочато; цикл покупки ще не створено"
            focus = "inquiry"
            covered.append("client_message")
    if focus:
        nodes[focus]["current"] = True
    result = {
        "schema_version": 1, "client_id": client_id,
        "current_episode_id": current["id"] if current else None,
        "viewed_episode_id": episode["id"] if episode else None,
        "is_history": bool(episode and (not current or episode["id"] != current["id"])),
        "episodes": {"items": [_selector(row) for row in recent], "total": total, "has_more": total > len(recent)},
        "viewed_episode": _selector(episode) if episode else None,
        "route_kind": "unknown", "focus": {"node_id": focus, "display_only": True},
        "nodes": list(nodes.values()), "history": history,
        "covered_sources": sorted(set(covered)),
        "deferred_domains": ["live_node_projection", "semantic_transitions", "route_classification", "media_understanding", "checkout_readiness", "offer_validity", "settlement_projection", "attention", "next_action"],
        "capabilities": {"attention": False, "actions": False, "full_projection": False},
    }
    result["revision"] = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    return result
