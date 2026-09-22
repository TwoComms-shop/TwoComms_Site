"""Current client-level order assignments, separate from the viewed purchase."""
from copy import deepcopy
from types import SimpleNamespace

from django.db import DatabaseError
from django.db.models import Q

from management.models import IgOrderAssignment, IgOrderAssignmentEvent
from orders.fulfillment_truth import nova_poshta_delivery_confirmed_at


ORDER_LIMIT = 10
AUDIT_LIMIT = 20
_PRODUCER = "client_order_assignments"
_ORDER_LABELS = {
    "new": "В обробці", "prep": "Готується до відправлення", "ship": "Відправлено",
    "done": "Завершено у системі", "cancelled": "Скасовано",
}
_PAYMENT_LABELS = {
    "unpaid": "Не оплачено", "checking": "На перевірці", "prepaid": "Внесено передплату",
    "partial": "Внесено передплату", "paid": "Оплачено повністю",
}
_SOURCE_LABELS = {
    "provider_auto": "Автоматично за оплатою", "checkout_auto": "Автоматично через Direct checkout",
    "manager_payment_review": "Менеджер після перевірки оплати", "manager_manual": "Менеджер вручну",
    "manager_created": "Створено менеджером", "legacy_attribution": "Імпортовано з атрибуції",
}
_ORDER_FIELDS = (
    "id", "order_number", "source", "status", "payment_status", "updated", "tracking_number",
    "tracking_status_code", "tracking_terminal_at", "tracking_provider_event_at",
)


def _positive(value):
    return type(value) is int and value > 0


def _iso(value):
    return value.isoformat() if value is not None else None


def _read(client_id, bound_order_id, coverage):
    queryset = IgOrderAssignment.objects.filter(client_id=client_id, unassigned_at__isnull=True)
    if bound_order_id is not None:
        queryset = queryset.exclude(order_id=bound_order_id)
    rows = list(queryset.order_by("-assigned_at", "-id").values(
        "id", "client_id", "order_id", "source", "version", "assigned_by_id", "assigned_at", "updated_at",
        *(f"order__{field}" for field in _ORDER_FIELDS),
    )[:ORDER_LIMIT + 1])
    coverage["truncated"] = len(rows) > ORDER_LIMIT
    rows = rows[:ORDER_LIMIT]
    if not rows:
        return [], {}
    exact_versions = Q()
    for row in rows:
        exact_versions |= Q(assignment_id=row["id"], assignment_version=row["version"], order_id=row["order_id"])
    events = list(IgOrderAssignmentEvent.objects.filter(
        exact_versions, to_client_id=client_id,
    ).order_by("-id").values(
        "id", "assignment_id", "order_id", "kind", "to_client_id", "assignment_version",
        "assignment_source", "actor_source", "actor_id", "snapshot", "created_at",
    )[:AUDIT_LIMIT + 1])
    coverage["audit_truncated"] = len(events) > AUDIT_LIMIT
    grouped = {}
    for event in events[:AUDIT_LIMIT]:
        grouped.setdefault(event["assignment_id"], []).append(event)
    return rows, grouped


def _audit(row, events, client_id):
    # A single exact event proves this current assignment version. Ambiguous or
    # legacy audit data does not remove the current operational assignment.
    if len(events) != 1:
        return None
    event = events[0]
    snapshot = event["snapshot"]
    actor_source = event["actor_source"]
    if (
        event["assignment_id"] != row["id"] or event["order_id"] != row["order_id"]
        or event["to_client_id"] != client_id or event["assignment_version"] != row["version"]
        or event["assignment_source"] != row["source"] or row["source"] not in _SOURCE_LABELS
        or actor_source not in IgOrderAssignmentEvent.ActorSource.values
        or event["kind"] != ("auto_confirmed" if actor_source == "automation" else "linked")
        or (actor_source == "management_user" and (
            not _positive(event["actor_id"]) or event["actor_id"] != row["assigned_by_id"]
        ))
        or not isinstance(snapshot, dict)
        or type(snapshot.get("assignment_version")) is not int
        or snapshot["assignment_version"] != row["version"]
        or snapshot.get("source") != row["source"]
    ):
        return None
    return event


def _node(row, event):
    order = SimpleNamespace(**{field: row[f"order__{field}"] for field in _ORDER_FIELDS})
    delivery_at = nova_poshta_delivery_confirmed_at(order)
    assignment_ref = {"kind": "order_assignment", "id": row["id"]}
    order_ref = {"kind": "order", "id": order.id}
    refs = [assignment_ref, order_ref]
    facts = []

    def fact(key, label, value, *, evidence=None, state="partial", captured_at=None, source="order.current", format=None):
        facts.append({
            "id": f"client-order:{order.id}:{key}", "label": label, "value": value,
            "state": state, "tone": "neutral", "source": source,
            "captured_at": _iso(captured_at or order.updated), "evidence_refs": evidence or [order_ref],
            **({"format": format} if format else {}),
        })

    fact("number", "Номер замовлення", order.order_number)
    fact("assignment", "Пов’язано з клієнтом", _SOURCE_LABELS.get(row["source"], "Джерело не визначено"),
         evidence=[assignment_ref], captured_at=row["updated_at"], source="order_assignment.current")
    fact("status", "Стан замовлення", _ORDER_LABELS.get(order.status, "Невідомо"))
    fact("payment", "Оплата цього замовлення", _PAYMENT_LABELS.get(order.payment_status, "Невідомо"),
         state="complete" if order.payment_status == "paid" else "partial")
    if order.tracking_number:
        fact("tracking", "ТТН указана", order.tracking_number)
    if delivery_at:
        fact("delivery", "Отримання", "Підтверджено перевізником", state="complete", captured_at=delivery_at)
    elif order.status == "done":
        fact("delivery", "Отримання", "Підтвердження перевізника відсутнє")
    if event:
        event_ref = {"kind": "order_assignment_event", "id": event["id"]}
        refs.append(event_ref)
        fact("linked", "Прив’язку зафіксовано", _iso(event["created_at"]), evidence=[assignment_ref, event_ref],
             captured_at=event["created_at"], source="order_assignment_event", format="datetime")
    return {
        "id": f"client-order:{order.id}", "semantic_key": "client_order_context", "producer": _PRODUCER,
        "scope": "client", "client_id": row["client_id"], "episode_id": None,
        "label": "Замовлення з сайту" if order.source == "web" else "Пов’язане замовлення",
        "summary": "Замовлення клієнта · зв’язок із цією покупкою не встановлено",
        "short_label": ("Із сайту" if order.source == "web" else "Замовлення") + " · " + (
            "отримано" if delivery_at else _ORDER_LABELS.get(order.status, "стан невідомий").lower()),
        "state": "complete" if delivery_at else "invalidated" if order.status == "cancelled" else "partial",
        "current": False, "facts": facts, "evidence_refs": refs, "layout": {"rank": 7, "lane": 2},
    }


def _context_path(row, event, parent, inbound_id):
    """Current order states and ownership, not reconstructed episode transitions."""
    order_id = row["order_id"]
    binding = {"client_id": row["client_id"], "order_id": order_id,
               "assignment_id": row["id"], "assignment_version": row["version"],
               "audit_event_id": event["id"] if event else None, "episode_binding": "absent"}
    parent["contextual_binding"] = binding
    order_ref = {"kind": "order", "id": order_id}
    delivered = any(fact["id"].endswith(":delivery") and fact["state"] == "complete"
                    for fact in parent["facts"])
    shipped = row["order__status"] == "ship" or delivered
    cancelled = row["order__status"] == "cancelled"
    status_facts = [fact for fact in parent["facts"] if fact["id"].endswith((":status", ":tracking"))]
    delivery_facts = [fact for fact in parent["facts"] if fact["id"].endswith((":tracking", ":delivery"))]

    def node(suffix, key, label, state, summary, facts, refs):
        return {"id": f"client-order:{order_id}:{suffix}", "semantic_key": key,
                "producer": _PRODUCER, "scope": "client", "client_id": row["client_id"],
                "episode_id": None, "contextual_binding": binding, "current": False,
                "label": label, "short_label": label, "state": state, "summary": summary,
                "facts": deepcopy(facts), "evidence_refs": refs,
                "presentation_kind": "client_context"}

    shipping = node("shipping", "client_order_shipping", "Відправлено" if shipped else "Відправлення",
                    "complete" if shipped else "invalidated" if cancelled else "open",
                    "Стан пов’язаного замовлення; час переходу не відновлюється з переписки.",
                    status_facts, [order_ref] if shipped else [])
    delivery = node("delivery", "client_order_delivery", "Отримано" if delivered else "Отримання",
                    "complete" if delivered else "open",
                    "Отримання підтверджене перевізником." if delivered else "Підтвердження отримання від перевізника відсутнє.",
                    delivery_facts, [order_ref] if delivered else [])
    if shipped and not delivered and not cancelled:
        delivery["waiting"] = {"kind": "indefinite", "label": "Очікуємо підтвердження отримання від перевізника; строк невідомий.",
                               "evidence_refs": [order_ref]}
    contact = node("contact", "client_order_contact", "Дозвіл на контакт", "open",
                   "Підтвердженого дозволу для автоматичного повідомлення після покупки немає. Доставка від цього не залежить.",
                   [], [])
    contact.update({"implementation_status": "planned", "marketing_permission": "not_confirmed",
                    "implementation_note": "Планується окреме підтвердження каналу та мети повідомлення. Без нього автоматичне повідомлення не надсилається."})

    def edge(source, target, relation, authority, label, refs):
        return {"id": f"client-order-edge:{order_id}:{source}:{target}", "producer": _PRODUCER,
                "from_node_id": source, "to_node_id": target, "relation": relation,
                "scope": "client", "episode_id": None, "authority": authority,
                "contextual_binding": binding, "tone": "neutral", "condition_label": label,
                "evidence_refs": refs, "summary": "Контекст пов’язаного замовлення; не перехід вибраної покупки."}

    edges = []
    if inbound_id:
        edges.append(edge(inbound_id, parent["id"], "client_scope_assignment", "owned_order_assignment",
                          "Прив’язано вручну" if row["source"] == "manager_manual" else "Пов’язано з клієнтом",
                          parent["evidence_refs"]))
    for source, target, label in ((parent, shipping, "Відправлення замовлення"),
                                  (shipping, delivery, "Отримання замовлення"),
                                  (parent, contact, "Окремий дозвіл на повідомлення")):
        edges.append(edge(source["id"], target["id"], "client_order_lifecycle", "order_state_context", label, []))
    return [shipping, delivery, contact], edges


def append_client_order_context(graph, *, client_id, is_history, bound_order_id=None):
    """Return a copy with current client context; never bind or complete an episode.

    The caller authorizes PII access and selects the purchase. These facts have
    no journey visits or focus: contextual edges explain client ownership and
    current order states, without claiming episode transition chronology.
    """
    result = deepcopy(graph)
    stale_ids = {node["id"] for node in result["nodes"] if node.get("producer") == _PRODUCER}
    result["nodes"] = [node for node in result["nodes"] if node["id"] not in stale_ids]
    result["edges"] = [edge for edge in result["edges"] if edge.get("producer") != _PRODUCER
                       and edge["from_node_id"] not in stale_ids and edge["to_node_id"] not in stale_ids]
    result["overview_node_ids"] = [value for value in result.get("overview_node_ids", []) if value not in stale_ids]
    coverage = {"status": "missing_source", "returned": 0, "limit": ORDER_LIMIT,
                "truncated": False, "audit_verified": 0, "audit_missing": 0,
                "audit_truncated": False, "event_coverage": "missing_source"}
    result.setdefault("coverage", {})["client_orders"] = coverage
    if not _positive(client_id) or type(is_history) is not bool or (bound_order_id is not None and not _positive(bound_order_id)):
        coverage["status"] = "invalid_scope"
        return result
    if is_history:
        coverage["status"] = "historical_view"
        return result
    inbound_ids = [node["id"] for node in result["nodes"] if node.get("semantic_key") == "inbound"]
    inbound_id = inbound_ids[0] if len(inbound_ids) == 1 else None
    coverage["context_anchor"] = "available" if inbound_id else "missing_or_ambiguous"
    try:
        rows, events = _read(client_id, bound_order_id, coverage)
    except DatabaseError:
        coverage["status"] = "unavailable"
        return result
    for row in rows:
        event = _audit(row, events.get(row["id"], []), client_id)
        if coverage["audit_truncated"]:
            # The bounded audit result cannot establish uniqueness if cut off.
            event = None
        coverage["audit_verified" if event else "audit_missing"] += 1
        node = _node(row, event)
        if any(existing["id"] == node["id"] for existing in result["nodes"]):
            continue
        result["nodes"].append(node)
        result["overview_node_ids"].append(node["id"])
        children, context_edges = _context_path(row, event, node, inbound_id)
        result["nodes"].extend(children)
        result["edges"].extend(context_edges)
        result["overview_node_ids"].extend(child["id"] for child in children)
        coverage["returned"] += 1
    if coverage["returned"]:
        coverage["status"] = "partial"
    if coverage["audit_verified"]:
        coverage["event_coverage"] = "partial"
    return result
