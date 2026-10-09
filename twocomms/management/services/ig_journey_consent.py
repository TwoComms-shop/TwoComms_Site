"""Read-only opt-in presentation. A manual bot opt-in is not a marketing grant."""
from copy import deepcopy
from uuid import UUID


MAX_ORDER_PROJECTIONS = 11  # Ten client-order contexts plus the viewed purchase.


def _positive(value):
    return type(value) is int and value > 0


def _refs(values):
    if not isinstance(values, (list, tuple)):
        return []
    result, seen = [], set()
    for ref in values[:8]:
        if not isinstance(ref, dict) or not isinstance(ref.get("kind"), str) or ref.get("kind") not in {"message", "source_message", "consent_invitation"}:
            continue
        identity = ref.get("id")
        if ref["kind"] == "consent_invitation":
            try:
                if not isinstance(identity, str) or str(UUID(identity)) != identity:
                    continue
            except (ValueError, TypeError, AttributeError):
                continue
        elif not _positive(identity):
            continue
        key = (ref["kind"], identity)
        if key not in seen:
            result.append({"kind": key[0], "id": key[1]})
            seen.add(key)
    return result


def _business_progress(projection):
    if (not isinstance(projection, dict) or projection.get("schema") != "journey-consent.v2"
            or projection.get("channel") != "instagram" or projection.get("purpose") != "post_purchase_marketing"):
        return None
    result = {"schema": "journey-consent.v2", "channel": "instagram", "purpose": "post_purchase_marketing",
              "native_receipts_available": False, "native_grant": "unverified",
              "provider_basis": "standard_window" if projection.get("provider_basis") == "standard_window" else "unverified"}
    invitation = projection.get("invitation") or {}
    invitation = invitation if isinstance(invitation, dict) else {}
    refs = _refs(invitation.get("evidence_refs"))
    status = invitation.get("status") if isinstance(invitation.get("status"), str) and invitation.get("status") in {"pending", "processing", "sent", "unknown", "failed"} else "unavailable"
    receipt = invitation.get("provider_message_id")
    if status == "sent" and (not refs or not isinstance(receipt, str) or not receipt.strip()):
        status = "unknown"
    result["invitation"] = {"status": status, "evidence_refs": refs}
    response = projection.get("response") or {}
    response = response if isinstance(response, dict) else {}
    refs = _refs(response.get("evidence_refs"))
    source_id = response.get("source_message_id")
    status = response.get("status") if isinstance(response.get("status"), str) and response.get("status") in {"accepted", "declined", "revoked"} else "unknown"
    if result["invitation"]["status"] != "sent" or not refs or not _positive(source_id) or not any(ref["kind"] in {"message", "source_message"} and ref["id"] == source_id for ref in refs):
        status = "unknown"
    result["response"] = {"status": status, "evidence_refs": refs if status != "unknown" else []}
    permission = projection.get("permission") or {}
    permission = permission if isinstance(permission, dict) else {}
    refs = _refs(permission.get("evidence_refs"))
    status = permission.get("status") if isinstance(permission.get("status"), str) and permission.get("status") in {"business_accepted", "declined", "revoked", "expired", "unconfirmed", "blocked"} else "unconfirmed"
    if status == "business_accepted" and (result["response"]["status"] != "accepted" or not refs):
        status = "unconfirmed"
    if status in {"declined", "revoked", "expired"} and not refs:
        status = "unconfirmed"
    result["permission"] = {"status": status, "evidence_refs": refs}
    if status == "business_accepted":
        result["note"] = "Згоду клієнта на новинки й пропозиції зафіксовано. Маркетингові повідомлення — після отримання замовлення."
    elif status == "declined":
        result["note"] = "Клієнт відмовився від цього запрошення. Сервісні звернення та доставка від цього не залежать."
    elif status in {"revoked", "expired"}:
        result["note"] = "Це запрошення або його згода більше не чинні. Нові повідомлення за ним не дозволено."
    else:
        result["note"] = "Запрошення та відповідь клієнта перевіряються за окремими джерелами."
    return result


def consent_progress(*, received=False, delivery_refs=(), opted_out_at=None, opt_out_message_id=None, business_projection=None):
    blocked_refs = [{"kind": "message", "id": opt_out_message_id}] if opted_out_at and opt_out_message_id else []
    result = {
        "schema": "journey-consent.v1", "channel": "instagram",
        "purpose": "post_purchase_marketing", "native_receipts_available": False,
        "delivery": {"status": "received" if received and delivery_refs else "waiting",
                     "evidence_refs": list(delivery_refs) if received else []},
        "invitation": {"status": "unavailable", "evidence_refs": []},
        "response": {"status": "unknown", "evidence_refs": []},
        "permission": {"status": "blocked" if opted_out_at else "unconfirmed", "evidence_refs": blocked_refs},
        "note": ("Клієнт заборонив повідомлення. Це загальна заборона, не відповідь на конкретне marketing opt-in."
                 if opted_out_at else "Запрошення — після оплати, маркетингові повідомлення — після отримання. Нативні події згоди ще не підключені; замовлення не надає дозволу на маркетинг."),
    }
    business = _business_progress(business_projection)
    if business is not None:
        business["delivery"] = result["delivery"]
        if opted_out_at:
            business["permission"] = result["permission"]
            business["note"] = result["note"]
        return business
    return result


def _order_id(node):
    binding = node.get("contextual_binding") or {}
    value = binding.get("order_id") if isinstance(binding, dict) else None
    if _positive(value):
        return value
    progress = node.get("fulfillment_progress") or {}
    refs = progress.get("evidence_refs") or []
    ids = {ref.get("id") for ref in refs if isinstance(ref, dict) and ref.get("kind") == "order" and _positive(ref.get("id"))}
    return next(iter(ids)) if len(ids) == 1 else None


def append_consent_context(graph, *, client, is_history):
    result = deepcopy(graph)
    # A current permission must not be projected onto an old purchase.
    if is_history:
        return result
    opted_out_at = getattr(client, "opted_out_at", None)
    opted_in_at = getattr(client, "opted_in_at", None)
    # Mirrors the global reply barrier; lifting a global ban still grants no
    # marketing permission. Only the separate evidenced answer can do that.
    if opted_out_at and opted_in_at and opted_in_at > opted_out_at:
        opted_out_at = None
    options = {"opted_out_at": opted_out_at,
               "opt_out_message_id": getattr(client, "opt_out_message_id", None)}
    result["marketing_consent"] = consent_progress(**options)
    order_ids = list(dict.fromkeys(order_id for node in result["nodes"] if (order_id := _order_id(node))))[:MAX_ORDER_PROJECTIONS]
    projections = {}
    if order_ids and _positive(getattr(client, "pk", None)) and getattr(client, "privacy_erasure_started_at", None) is None:
        from management.services.ig_marketing_consent import business_consent_projections

        projections = business_consent_projections(client, order_ids)
        projections = projections if isinstance(projections, dict) else {}
    result["marketing_consents_by_order"] = {str(order_id): consent_progress(
        business_projection=projections.get(order_id, projections.get(str(order_id))), **options) for order_id in order_ids}
    orders = {n["id"]: n for n in result["nodes"] if n.get("semantic_key") == "client_order_context"}
    for node in result["nodes"]:
        if node.get("semantic_key") != "client_order_contact":
            continue
        order_id = node.get("contextual_binding", {}).get("order_id")
        parent = orders.get(f"client-order:{order_id}", {})
        progress = parent.get("fulfillment_progress", {})
        node["consent_progress"] = consent_progress(
            received=progress.get("step") == 4 and not progress.get("cancelled"),
            delivery_refs=progress.get("evidence_refs", []),
            business_projection=projections.get(order_id, projections.get(str(order_id))), **options)
        node.update(label="Новинки й пропозиції", short_label="Новинки · згода",
                    summary=node["consent_progress"]["note"])
    return result
