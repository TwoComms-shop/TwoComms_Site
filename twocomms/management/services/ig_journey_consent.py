"""Read-only opt-in presentation. A manual bot opt-in is not a marketing grant."""
from copy import deepcopy


def consent_progress(*, received=False, delivery_refs=(), opted_out_at=None, opt_out_message_id=None):
    blocked_refs = [{"kind": "message", "id": opt_out_message_id}] if opted_out_at and opt_out_message_id else []
    return {
        "schema": "journey-consent.v1", "channel": "instagram",
        "purpose": "post_purchase_marketing", "native_receipts_available": False,
        "delivery": {"status": "received" if received and delivery_refs else "waiting",
                     "evidence_refs": list(delivery_refs) if received else []},
        "invitation": {"status": "unavailable", "evidence_refs": []},
        "response": {"status": "unknown", "evidence_refs": []},
        "permission": {"status": "blocked" if opted_out_at else "unconfirmed", "evidence_refs": blocked_refs},
        "note": ("Клієнт заборонив повідомлення. Це загальна заборона, не відповідь на конкретне marketing opt-in."
                 if opted_out_at else "Нативні події запрошення та відповіді ще не підключені. Отримання замовлення не надає дозволу на маркетинг."),
    }


def append_consent_context(graph, *, client, is_history):
    result = deepcopy(graph)
    # A current permission must not be projected onto an old purchase.
    if is_history:
        return result
    options = {"opted_out_at": getattr(client, "opted_out_at", None),
               "opt_out_message_id": getattr(client, "opt_out_message_id", None)}
    result["marketing_consent"] = consent_progress(**options)
    orders = {n["id"]: n for n in result["nodes"] if n.get("semantic_key") == "client_order_context"}
    for node in result["nodes"]:
        if node.get("semantic_key") != "client_order_contact":
            continue
        order_id = node.get("contextual_binding", {}).get("order_id")
        parent = orders.get(f"client-order:{order_id}", {})
        progress = parent.get("fulfillment_progress", {})
        node["consent_progress"] = consent_progress(
            received=progress.get("step") == 4 and not progress.get("cancelled"),
            delivery_refs=progress.get("evidence_refs", []), **options)
        node.update(label="Маркетинг opt-in", short_label="Маркетинг opt-in",
                    summary=node["consent_progress"]["note"])
    return result
