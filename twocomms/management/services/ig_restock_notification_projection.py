"""Historical ALL-list delivery evidence, without current fulfilment authority.

Only already-loaded rows are admitted. This reader cannot fetch a relation,
observe inventory, validate a current manager action, or authorize a send.
"""
from datetime import datetime

from django.utils import timezone

from management.services.ig_delivery_receipts import normalize_provider_message_id


def _loaded(row):
    """Copy concrete scalar columns without invoking deferred descriptors."""
    if row is None:
        return None
    values = vars(row)
    columns = tuple(field.attname for field in row._meta.concrete_fields)
    if any(name not in values for name in columns):
        return None
    return {name: values[name] for name in columns}


def same_loaded_evidence(left, right):
    """Exact scalar parity before reusing this read's complete native proof."""
    if left is None or right is None or type(left) is not type(right):
        return False
    first, second = _loaded(left), _loaded(right)
    return first is not None and second is not None and first == second


def _cached(row, name, value):
    cache = row._state.fields_cache
    return name in cache and cache[name] is value


def notification_projection(*, invitation, accepted, subscription, event, task, history,
                            source_reason, accepted_valid, receipt_valid, now):
    """Return the common dedicated/shared DTO from a loaded native checkpoint.

The caller supplies its already-validated historical source and answer verdicts.
SENT is independent of today's stock, manager freshness, window and consent
expiry/revocation. UNKNOWN is never upgraded from a convenient-looking MID.
"""
    raw = vars(subscription) if subscription is not None else {}
    result = {"state": "unknown" if raw.get("state") in {"sent", "unknown"}
              else "pending" if raw.get("state") == "active" else "not_sent",
              "reason": raw.get("last_reason", "") if subscription is not None else source_reason,
              "sent_at": None, "native_receipt_confirmed": False}
    if source_reason or not accepted_valid or not receipt_valid or raw.get("state") != "sent":
        return result
    try:
        inv, answer, sub, observed, sent = map(_loaded,
            (invitation, accepted, subscription, event, task))
        if any(row is None for row in (inv, answer, sub, observed, sent)):
            return result
        if (not _cached(subscription, "availability_event", event)
            or not _cached(event, "task", task) or not _cached(task, "sent_message", history)):
            return result
        from management.services import ig_restock_subscriptions as restock
        if (not restock._verified(invitation, "invitation")
            or not restock._verified(accepted, "answer") or not restock._verified(event, "event")):
            return result
        client_id, target = inv["client_id"], inv["target"]
        if (answer["kind"] != "accept" or answer["invitation_id"] != inv["id"]
            or sub["accepted_answer_id"] != answer["id"] or sub["client_id"] != client_id
            or sub["expires_at"] != inv["expires_at"] or restock._digest(target) != inv["target_digest"]
            or observed["subscription_id"] != sub["id"] or observed["task_id"] != sent["id"]):
            return result
        proof = observed["envelope"]
        availability, manager = proof.get("availability", {}), proof.get("manager_decision", {})
        event_key = f"{restock.ORIGIN}:{sub['id']}"
        if (proof.get("target_digest") != inv["target_digest"]
            or proof.get("answer_hmac") != answer["envelope_hmac"]
            or availability.get("target") != target
            or availability.get("digest") != observed["availability_digest"]
            or manager.get("version") != 2 or manager.get("completion") != "all"
            or manager.get("subscription_id") != str(sub["id"])
            or manager.get("target_digest") != inv["target_digest"]
            or sent["client_id"] != client_id or sent["event_key"] != event_key
            or observed["event_key"] != event_key or sent["reason"] != restock.ORIGIN
            or sent["kind"] != "qualification" or sent["trigger"] != "event"
            or sent["policy_version"] != "restock-agreement.v1"
            or sent["event_payload"] != {"origin": restock.ORIGIN, "purpose": restock.PURPOSE,
                "subscription_id": str(sub["id"]), "event_id": str(observed["id"])}):
            return result
        from management.services.ig_restock_waiting_list import copy_for, label
        times = (answer["answered_at"], observed["observed_at"], sent["sent_at"], now)
        if (any(not isinstance(value, datetime) or not timezone.is_aware(value) for value in times)
            or not times[0] <= times[1] <= times[2] <= times[3]
            or sent["status"] != "sent" or not normalize_provider_message_id(sent["provider_message_id"])
            or sent["claim_token"] or sent["claim_until"] or sent["attempt_count"] != 1
            or sent["message_text"] != copy_for(inv["locale"], "notification", label(target))):
            return result
        # The native checkpoint precedes the optional CRM projection. A missing
        # projection cannot erase delivery; a present foreign history cannot
        # lend its MID, recipient or namespace to this subscription.
        if sent["sent_message_id"] is not None:
            message = _loaded(history)
            if (message is None or message["id"] != sent["sent_message_id"]
                or message["client_id"] != client_id or message["sender_id"] != inv["envelope"]["recipient"]
                or message["role"] != "model" or message["source"] != "followup" or message["status"] != "done"
                or message["text"] != sent["message_text"]
                or message["provider_message_id"] != sent["provider_message_id"]
                or message["provider_namespace"] != inv["envelope"]["source"]["namespace"]):
                return result
        elif history is not None:
            return result
        return {**result, "state": "sent", "sent_at": sent["sent_at"].isoformat(),
                "native_receipt_confirmed": True}
    except (ValueError, KeyError, TypeError, AttributeError):
        return result
