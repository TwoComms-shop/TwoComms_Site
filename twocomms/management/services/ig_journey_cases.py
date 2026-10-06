"""Bounded projections of existing review/service records, never business effects.

Case state is current record truth. It does not reconstruct a customer's path,
grant a prize, settle a refund, or prove that a parcel was sent.
"""
from collections import Counter
from copy import deepcopy

from django.db import DatabaseError

from management.models import (
    IgClient, IgFollowUpTask, IgFunnelResetAudit,
    IgOrderAssignment, IgPostSaleCase, InstagramBotMessage,
)
from management.services.ig_prize_cases import CASE_SCHEMA_VERSION, CASE_REASON_PREFIX

CASE_LIMIT = 20
SOURCE_LIMIT = 32
PRODUCER = "persisted_case_records"
PRIZE_FIELDS = ("id", "manager_context", "event_payload", "manager_approval_status", "status")
SERVICE_FIELDS = ("id", "order_id", "commercial_episode_id", "source_message_id", "case_type", "status",
                  "commercial_episode__client_id", "commercial_episode__intended_order_id")
SOURCE_FIELDS = ("id", "role", "sender_id", "source", "media_capture_eligible", "private_media_state",
                 "created_at", "provider_created_at", "attachment_media")


def _id(value):
    return value if type(value) is int and value > 0 else None


def _boundary(client_id):
    client = IgClient.objects.filter(pk=client_id).values("igsid", "privacy_erasure_started_at").first()
    reset = IgFunnelResetAudit.objects.filter(client_id=client_id).order_by("-pk").values(
        "id", "reset_after_message_id", "created_at").first()
    return client, reset


def _source_ok(source, client, reset):
    if not source or source["role"] != "user" or source["sender_id"] != client["igsid"]:
        return False
    if source["private_media_state"] not in {"", "active"}:
        return False
    if reset:
        event_at = source["provider_created_at"] or source["created_at"]
        if source["id"] <= reset["reset_after_message_id"] or not event_at or event_at <= reset["created_at"]:
            return False
    return True


def _prize_sources(row, sources, client, reset):
    document, payload = row["manager_context"], row["event_payload"]
    if not isinstance(document, dict) or document.get("schema_version") != CASE_SCHEMA_VERSION:
        return [], "case_schema_unknown"
    if (not isinstance(payload, dict) or payload.get("schema_version") != CASE_SCHEMA_VERSION
            or payload.get("case_kind") != "prize_review"
            or not document.get("programme_id") or not document.get("programme_version")
            or payload.get("programme_id") != document["programme_id"]
            or payload.get("programme_version") != document["programme_version"]):
        return [], "case_identity_invalid"
    evidence = document.get("evidence")
    if not isinstance(evidence, list) or not evidence or len(evidence) > SOURCE_LIMIT:
        return [], "case_evidence_missing"
    refs = []
    for item in evidence:
        if not isinstance(item, dict):
            return [], "case_evidence_invalid"
        source = sources.get(_id(item.get("source_message_id")))
        if not _source_ok(source, client, reset):
            return [], "source_unavailable_or_reset"
        if source["source"] != "webhook" or not source["media_capture_eligible"]:
            return [], "prize_source_unverified"
        media = source["attachment_media"] if isinstance(source["attachment_media"], list) else []
        parts = [part for part in media if isinstance(part, dict)
                 and part.get("source_part_id") == item.get("source_part_id")
                 and part.get("content_hash") == item.get("content_hash")]
        if len(parts) != 1 or not item.get("content_hash") or not item.get("source_part_id"):
            return [], "prize_media_changed"
        part = parts[0]
        if (part.get("status") != "owned" or part.get("private_storage") is not True
                or not part.get("storage_name") or not str(part.get("mime", "")).startswith("image/")):
            return [], "prize_media_unavailable"
        inspection = part.get("inspection") if isinstance(part.get("inspection"), dict) else {}
        if (inspection.get("state") != "inspected" or not item.get("request_id") or not item.get("provider_model")
                or any(inspection.get(key) != item.get(key)
                       for key in ("source_part_id", "content_hash", "request_id", "provider_model"))
                or item.get("revision_id") is not None and inspection.get("revision_id") != item["revision_id"]):
            return [], "prize_inspection_unproven"
        if (item.get("type_code") != "certificate"
                or item.get("programme_id") != document["programme_id"]
                or item.get("programme_version") != document["programme_version"]):
            return [], "prize_evidence_identity_invalid"
        ref = {"kind": "message", "id": source["id"]}
        if ref not in refs:
            refs.append(ref)
    if _id(payload.get("initial_source_message_id")) not in [ref["id"] for ref in refs]:
        return [], "initial_source_unproven"
    return refs, ""


def _node(identifier, semantic_key, label, record_ref, sources, *, state="partial", summary="", binding=None, status=""):
    return {"id": identifier, "semantic_key": semantic_key, "structural_key": semantic_key,
            "label": label, "short_label": label, "state": state, "current": False,
            "presentation_kind": "client_context", "producer": PRODUCER, "scope": "client",
            "episode_id": None, "contextual_binding": binding or {},
            "summary": summary, "evidence_refs": [record_ref, *sources],
            "facts": [{"id": identifier + ":state", "label": "Стан збереженого випадку",
                       "value": status, "source": "case_record.current", "state": state,
                       "evidence_refs": [record_ref]}]}


def append_case_context(graph, *, client_id, episode_id=None, is_history=False):
    """Read existing cases with exact source/scope fences; GET performs only SELECTs."""
    result = deepcopy(graph)
    coverage = {"status": "missing_source", "producer": PRODUCER, "accepted": 0,
                "rejected": 0, "truncated": False, "reasons": {}}
    result.setdefault("coverage", {})["case_records"] = coverage
    if is_history:
        coverage.update(status="unknown", reasons={"historical_projection_unverified": 1})
        return result
    try:
        prizes = list(IgFollowUpTask.objects.filter(client_id=client_id, kind="manager_task",
            reason__startswith=CASE_REASON_PREFIX).order_by("-id").values(*PRIZE_FIELDS)[:CASE_LIMIT + 1])
        services = list(IgPostSaleCase.objects.filter(client_id=client_id).order_by("-id").values(
            *SERVICE_FIELDS)[:CASE_LIMIT + 1])
        coverage["truncated"] = len(prizes) > CASE_LIMIT or len(services) > CASE_LIMIT
        prizes, services = prizes[:CASE_LIMIT], services[:CASE_LIMIT]
        if not prizes and not services:
            return result
        boundary = _boundary(client_id)
        client, reset = boundary
        if not client or client["privacy_erasure_started_at"] is not None:
            coverage.update(status="unknown", reasons={"client_unavailable": 1})
            return result
        ids = {_id(row["source_message_id"]) for row in services}
        for row in prizes:
            document = row["manager_context"]
            if isinstance(document, dict):
                evidence = document.get("evidence")
                for item in (evidence if isinstance(evidence, list) else [])[:SOURCE_LIMIT]:
                    if isinstance(item, dict):
                        ids.add(_id(item.get("source_message_id")))
        ids.discard(None)
        source_rows = list(InstagramBotMessage.objects.filter(client_id=client_id, pk__in=ids).values(*SOURCE_FIELDS))
        sources = {row["id"]: row for row in source_rows}
        order_ids = {row["order_id"] for row in services if row["order_id"]}
        assignments = list(IgOrderAssignment.objects.filter(client_id=client_id,
            order_id__in=order_ids, unassigned_at__isnull=True).values("id", "order_id", "version")) if order_ids else []
        owned_orders = {row["order_id"] for row in assignments}
        additions, reasons = [], Counter()
        for row in prizes:
            refs, reason = _prize_sources(row, sources, client, reset)
            if reason:
                reasons[reason] += 1
                continue
            node = _node(f"prize-case:{row['id']}", "prize_candidate", "Призовий випадок",
                         {"kind": "prize_case", "id": row["id"]}, refs,
                         binding={"client_id": client_id, "case_id": row["id"]},
                         status="Потребує перевірки права та умов",
                         summary="Збережений кандидат призу. Рішення задачі менеджера не підтверджує право на приз, оплату або нагороду.")
            node["case_record"] = {"kind": "prize_review", "case_id": row["id"],
                "review_state": row["manager_approval_status"], "task_state": row["status"],
                "entitlement": "unconfirmed", "authority": "validated_prize_case"}
            review_labels = {"pending": "Очікує рішення щодо задачі", "approved": "Задачу підтверджено",
                             "rejected": "Задачу відхилено", "not_required": "Рішення задачі не записано"}
            node["facts"].append({"id": node["id"] + ":task-review", "label": "Рішення задачі менеджера",
                "value": review_labels.get(row["manager_approval_status"], "Невідомо") + "; право на приз не підтверджене",
                "source": "case_record.current", "state": "partial",
                "evidence_refs": [{"kind": "prize_case", "id": row["id"]}]})
            additions.append(node)
        labels = dict(IgPostSaleCase.Status.choices)
        for row in services:
            if not _source_ok(sources.get(row["source_message_id"]), client, reset):
                reasons["source_unavailable_or_reset"] += 1
                continue
            if row["commercial_episode_id"] and row["commercial_episode__client_id"] != client_id:
                reasons["episode_owner_mismatch"] += 1
                continue
            episode_order = row["commercial_episode__intended_order_id"]
            if row["order_id"] and (episode_order and episode_order != row["order_id"]
                    or row["order_id"] not in owned_orders and episode_order != row["order_id"]):
                reasons["order_binding_unverified"] += 1
                continue
            state = "complete" if row["status"] in {"completed", "rejected", "cancelled"} else "partial"
            node = _node(f"service-case:{row['id']}", "post_sale_case",
                         "Обмін" if row["case_type"] == "exchange" else "Повернення",
                         {"kind": "post_sale_case", "id": row["id"]},
                         [{"kind": "message", "id": row["source_message_id"]}], state=state,
                         status=str(labels.get(row["status"], "Стан невідомий")),
                         binding={"client_id": client_id, "case_id": row["id"], "order_id": row["order_id"],
                                  "episode_id": row["commercial_episode_id"]},
                         summary="Стан сервісного випадку; не підтвердження повернення коштів, відправлення чи отримання. Сервіс не потребує маркетингової згоди.")
            node["case_record"] = {"kind": "post_sale", "case_id": row["id"], "status": row["status"],
                                   "authority": "service_case_record", "financial_outcome": "unknown"}
            additions.append(node)
        # A reset/erasure or source removal during reading cannot revive old evidence.
        reread = list(InstagramBotMessage.objects.filter(client_id=client_id, pk__in=ids).values(*SOURCE_FIELDS))
        prize_heads = list(IgFollowUpTask.objects.filter(client_id=client_id, kind="manager_task",
            reason__startswith=CASE_REASON_PREFIX, pk__in=[row["id"] for row in prizes]).values(*PRIZE_FIELDS)) if prizes else []
        service_heads = list(IgPostSaleCase.objects.filter(client_id=client_id,
            pk__in=[row["id"] for row in services]).values(*SERVICE_FIELDS)) if services else []
        assignment_heads = list(IgOrderAssignment.objects.filter(client_id=client_id,
            order_id__in=order_ids, unassigned_at__isnull=True).values("id", "order_id", "version")) if order_ids else []
        indexed = lambda rows: {row["id"]: row for row in rows}
        if (_boundary(client_id) != boundary or indexed(reread) != sources
                or indexed(prize_heads) != indexed(prizes) or indexed(service_heads) != indexed(services)
                or indexed(assignment_heads) != indexed(assignments)):
            coverage.update(status="unknown", reasons={"source_boundary_changed": 1})
            return result
        result["nodes"].extend(additions)
        coverage.update(status="partial" if reasons or coverage["truncated"] else "available",
                        accepted=len(additions), rejected=sum(reasons.values()), reasons=dict(reasons))
        for node in additions:
            order_id = node["contextual_binding"].get("order_id")
            parents = [n for n in graph["nodes"] if n.get("semantic_key") in {"client_order_context", "fulfillment"}
                       and any(ref.get("kind") == "order" and ref.get("id") == order_id
                               for ref in n.get("evidence_refs", []))] if order_id else []
            if len(parents) == 1:
                result["edges"].append({"id": node["id"] + ":order-context", "from_node_id": parents[0]["id"],
                    "to_node_id": node["id"], "relation": "case_record_context", "evidence_refs": node["evidence_refs"],
                    "condition_label": "Сервісний випадок цього замовлення; не перехід у переписці"})
        return result
    except DatabaseError:
        coverage.update(status="unknown", reasons={"projection_unavailable": 1})
        return result
