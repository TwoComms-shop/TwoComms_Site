"""Source-bound operator size corrections in the existing selection history."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from management.bot_access import (OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION,
    has_all_bot_capabilities)

SCHEMA = "manager-correction.v1"
ACTION = "manager_size_correction"
CAPABILITIES = (OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)
REASONS = frozenset({"source_interpretation_corrected", "requirement_verified", "clear_unverified_requirement"})
MAX_RECEIPT_BYTES = 8192
MAX_ID = 2**63-1
SIZE_CODES = frozenset({"XS", "S", "M", "L", "XL", "XXL", "XXXL", "XXXXL", "5XL", "6XL", "7XL", "8XL", "ONE SIZE"})


class SizeCorrectionRejected(ValueError):
    def __init__(self, code, *, status=409, retryable=False):
        super().__init__(code)
        self.code, self.status, self.retryable = code, status, retryable


@dataclass(frozen=True)
class SizeCorrectionResult:
    status: str
    operation_id: str
    transition_id: int | None
    selection_revision: int

    def as_dict(self):
        return {"status": self.status, "operation_id": self.operation_id,
            "transition_id": self.transition_id, "selection_revision": self.selection_revision,
            "field": "size", "refresh_current_state": True}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _id(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_ID


def normalize_size(operation, value):
    if operation == "clear" and value is None:
        return None
    if operation != "set" or not isinstance(value, str):
        raise SizeCorrectionRejected("correction_size_invalid", status=400)
    size = " ".join(value.strip().upper().split())
    # Bounded conventional requirements; catalog applicability stays separate.
    if size not in SIZE_CODES and not re.fullmatch(r"(?:[2-5][0-9]|1[0-7][0-9])", size):
        raise SizeCorrectionRejected("correction_size_invalid", status=400)
    return size


def build_size_correction_context(*, scope, selection, line, permission_epoch, reset_id, namespace, watermark, source_time_origin):
    """Stable CAS data from one already validated capture; no mutable read."""
    source = (selection.get("evidence") or {}).get("size") if isinstance(selection, dict) else None
    if not isinstance(source, dict) or not _id(source.get("source_message_id")):
        return {"available": False, "reason": "size_source_unavailable"}
    proof = {key: source.get(key) for key in ("source_message_id", "source_digest", "observed_at",
        "decision_id", "transition_id", "authority")}
    context = {"schema": "size-correction-context.v1", "field": "size", "scope": deepcopy(scope),
        "session_id": selection.get("session_id"), "generation": selection.get("generation"),
        "selection_revision": selection.get("revision"), "active_index": selection.get("active_index"),
        "line_digest": _digest(line), "value": line.get("size") or None, "source": proof,
        "permission_epoch": permission_epoch, "reset_id": reset_id, "source_namespace": namespace,
        "source_watermark": deepcopy(watermark), "source_time_origin": source_time_origin}
    return {"available": True, "context": context, "context_digest": _digest(context)}


def _input(*, client_id, actor_id, operation_id, expected_selection_revision, expected_context_digest,
        operation, size, reason_code):
    return {"schema": SCHEMA, "client_id": client_id, "actor_id": actor_id, "operation_id": str(operation_id),
        "expected_selection_revision": expected_selection_revision, "expected_context_digest": expected_context_digest,
        "field": "size", "operation": operation, "value": size, "reason_code": reason_code}


def validated_correction_receipt(transition, *, scope, namespace):
    """Verify immutable receipt/snapshots against the original owned source.

    No permissions are inferred from message prose or a current manager note.
    Historical capability proof is the server-recorded audited event.
    """
    from management.services.ig_commerce_state import size_correction_snapshot
    if transition.action != ACTION or not transition.correction_operation_id:
        return None
    receipt = (transition.effects or {}).get("manager_correction")
    try:
        if not isinstance(receipt, dict) or len(json.dumps(receipt).encode()) > MAX_RECEIPT_BYTES:
            return None
        context, source = receipt["context"], transition.source_message
        source_proof = context["source"]
        context_scope = context["scope"]
        if (receipt.get("schema") != SCHEMA or receipt.get("field") != "size"
            or not _id(receipt.get("actor_id")) or receipt.get("capabilities") != list(CAPABILITIES)
            or receipt.get("operation_id") != str(transition.correction_operation_id)
            or receipt.get("reason_code") not in REASONS or receipt.get("context_digest") != _digest(context)
            or context.get("schema") != "size-correction-context.v1" or context.get("field") != "size"
            or context.get("session_id") != transition.session_id
            or context.get("selection_revision") != transition.from_revision
            or transition.to_revision != transition.from_revision+1
            or context.get("generation") != transition.previous_snapshot.get("generation")
            or context_scope.get("client_id") != scope["client_id"]
            or any(context_scope.get(key) != scope.get(key) for key in ("episode_id", "line_id", "recipient_id", "reset_floor"))
            or (context_scope.get("order_id") is not None and context_scope["order_id"] != scope.get("order_id"))
            or context.get("reset_id") != scope.get("reset_id")
            or context.get("source_namespace") != namespace or not namespace
            or source.client_id != scope["client_id"] or source.role != "user" or source.source != "webhook"
            or source.status == "failed" or source.provider_namespace != namespace or source.pk < scope["reset_floor"]
            or source_proof.get("source_message_id") != source.pk
            or source_proof.get("source_digest") != hashlib.sha256(source.text.encode()).hexdigest()
            or context.get("source_time_origin") != ("provider_event" if source.provider_created_at else "local_observation")
            or source_proof.get("observed_at") != (source.provider_created_at or source.created_at).isoformat()):
            return None
        value = normalize_size(receipt["operation"], receipt["after"])
        if value != receipt["after"]:
            return None
        before = transition.previous_snapshot
        index = before["active_index"]
        line = before["lines"][index]
        if (context.get("active_index") != index or context.get("line_digest") != _digest(line)
            or str(line.get("line_id") or "") != scope["line_id"]
            or str(line.get("recipient_id") or "self") != scope["recipient_id"]
            or receipt.get("before") != (line.get("size") or None) or context.get("value") != receipt["before"]
            or receipt.get("supersedes_transition_id") != source_proof.get("transition_id")
            or size_correction_snapshot(before, operation=receipt["operation"], size=value) != transition.next_snapshot):
            return None
        expected = _input(client_id=scope["client_id"], actor_id=receipt["actor_id"],
            operation_id=transition.correction_operation_id, expected_selection_revision=transition.from_revision,
            expected_context_digest=receipt["context_digest"], operation=receipt["operation"], size=value,
            reason_code=receipt["reason_code"])
        recorded = datetime.fromisoformat(receipt["recorded_at"])
        if recorded.tzinfo is None or receipt.get("input_digest") != _digest(expected):
            return None
        return deepcopy(receipt)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return None


def validated_correction_capture(source, *, field, value, scope, boundary):
    """Pure structural proof for the same already validated canonical capture."""
    try:
        proof = source["correction"]
        receipt = proof["receipt"]
        context = receipt["context"]
        original = context["source"]
        if (field != "size" or source.get("authority") != "audited_correction"
            or not _id(proof["transition_id"]) or proof["transition_id"] != source.get("transition_id")
            or receipt["schema"] != SCHEMA or receipt["field"] != "size"
            or receipt["capabilities"] != list(CAPABILITIES) or not _id(receipt["actor_id"])
            or receipt["reason_code"] not in REASONS or receipt["context_digest"] != _digest(context)
            or context["schema"] != "size-correction-context.v1" or context["field"] != "size"
            or any(context["scope"].get(key) != scope.get(key) for key in ("client_id", "episode_id", "line_id", "recipient_id", "reset_floor"))
            or (context["scope"].get("order_id") is not None and context["scope"]["order_id"] != scope.get("order_id"))
            or context["source_namespace"] != boundary.get("source_namespace")
            or context.get("reset_id") != boundary.get("reset_id")
            or original["source_message_id"] != source["source_message_id"]
            or original["source_digest"] != source["source_digest"] or original["observed_at"] != source["observed_at"]
            or receipt["supersedes_transition_id"] != original.get("transition_id")
            or context["value"] != receipt["before"] or normalize_size(receipt["operation"], receipt["after"]) != value
            or receipt["input_digest"] != _digest(_input(client_id=scope["client_id"], actor_id=receipt["actor_id"],
                operation_id=uuid.UUID(receipt["operation_id"]), expected_selection_revision=context["selection_revision"],
                expected_context_digest=receipt["context_digest"], operation=receipt["operation"], size=value, reason_code=receipt["reason_code"]))):
            return None
        if datetime.fromisoformat(receipt["recorded_at"]).tzinfo is None:
            return None
        return receipt
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _actor(actor):
    actor_id = getattr(actor, "pk", None)
    if not _id(actor_id):
        raise SizeCorrectionRejected("correction_permission_denied", status=403)
    try:
        fresh = get_user_model().objects.filter(pk=actor_id).first()
        permitted = fresh is not None and has_all_bot_capabilities(fresh, *CAPABILITIES)
    except DatabaseError:
        raise SizeCorrectionRejected("correction_permission_unavailable", status=503, retryable=True) from None
    if not permitted:
        raise SizeCorrectionRejected("correction_permission_denied", status=403)
    return fresh


def _replay_checked(existing, *, request_digest, client_id, actor_id):
    receipt = (existing.effects or {}).get("manager_correction") or {}
    if (existing.session.client_id != client_id or receipt.get("actor_id") != actor_id
        or receipt.get("input_digest") != request_digest or existing.action != ACTION):
        raise SizeCorrectionRejected("correction_operation_conflict")
    from management.models import IgClient
    owner = IgClient.objects.filter(pk=client_id).values("privacy_erasure_started_at", "igsid").first()
    if not owner or owner["privacy_erasure_started_at"]:
        raise SizeCorrectionRejected("client_unavailable", status=410)
    context = receipt.get("context") or {}
    scope = {**(context.get("scope") or {}), "reset_id": context.get("reset_id")}
    if (existing.source_message.sender_id != owner["igsid"] or validated_correction_receipt(
            existing, scope=scope, namespace=context.get("source_namespace")) is None):
        raise SizeCorrectionRejected("correction_receipt_invalid")
    return SizeCorrectionResult("replayed", str(existing.correction_operation_id), existing.pk, existing.to_revision)


def _replay(existing, **kwargs):
    try:
        return _replay_checked(existing, **kwargs)
    except DatabaseError:
        raise SizeCorrectionRejected("correction_write_unavailable", status=503, retryable=True) from None
    except (ObjectDoesNotExist, TypeError, AttributeError):
        raise SizeCorrectionRejected("correction_receipt_invalid") from None


def save_size_correction(client_id, *, actor, operation_id, expected_selection_revision,
        expected_context_digest, operation, value=None, reason_code="source_interpretation_corrected", now=None):
    """Serialize with physical sends, then canonical client/source/session locks."""
    from management.models import IgClient, IgCommerceSelectionSession, IgCommerceSelectionTransition, InstagramBotMessage
    from management.services.ig_admin_state_capture import current_admin_state
    from management.services.ig_commerce_state import persist_size_correction_transition
    from management.services.ig_permission_transitions import WEB_LOCK_TIMEOUT_SECONDS
    from management.services.ig_reply_boundary import pause_reply_boundary, ReplyBoundaryTimeout
    if not _id(client_id) or not _id(expected_selection_revision):
        raise SizeCorrectionRejected("correction_identity_invalid", status=400)
    if not isinstance(expected_context_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_context_digest):
        raise SizeCorrectionRejected("correction_context_invalid", status=400)
    if not isinstance(reason_code, str) or reason_code not in REASONS:
        raise SizeCorrectionRejected("correction_reason_invalid", status=400)
    size = normalize_size(operation, value)
    try:
        op = uuid.UUID(str(operation_id))
    except (TypeError, ValueError, AttributeError):
        raise SizeCorrectionRejected("correction_operation_invalid", status=400) from None
    fresh_actor = _actor(actor)
    request_digest = _digest(_input(client_id=client_id, actor_id=fresh_actor.pk, operation_id=op,
        expected_selection_revision=expected_selection_revision, expected_context_digest=expected_context_digest,
        operation=operation, size=size, reason_code=reason_code))
    try:
        existing = IgCommerceSelectionTransition.objects.filter(correction_operation_id=op).select_related("session").first()
    except DatabaseError:
        raise SizeCorrectionRejected("correction_write_unavailable", status=503, retryable=True) from None
    if existing:
        return _replay(existing, request_digest=request_digest, client_id=client_id, actor_id=fresh_actor.pk)
    try:
        with pause_reply_boundary(timeout_seconds=WEB_LOCK_TIMEOUT_SECONDS):
            with transaction.atomic():
                client = IgClient.objects.select_for_update().filter(pk=client_id).first()
                if client is None:
                    raise SizeCorrectionRejected("client_missing", status=404)
                if client.privacy_erasure_started_at:
                    raise SizeCorrectionRejected("client_unavailable", status=410)
                fresh_actor = _actor(actor)
                existing = IgCommerceSelectionTransition.objects.filter(correction_operation_id=op).select_related("session").first()
                if existing:
                    return _replay(existing, request_digest=request_digest, client_id=client_id, actor_id=fresh_actor.pk)
                clock = now or timezone.now()
                captured = current_admin_state(client_id, expected_selection_revision=expected_selection_revision, now=clock)
                card = captured.state.as_dict()
                candidate = (card.get("boundary") or {}).get("size_correction_context") or {}
                if captured.status != "captured":
                    raise SizeCorrectionRejected("correction_state_unavailable", retryable=captured.reason == "state_read_unavailable")
                if not candidate.get("available"):
                    raise SizeCorrectionRejected("size_source_unavailable")
                if candidate.get("context_digest") != expected_context_digest:
                    raise SizeCorrectionRejected("correction_context_conflict")
                context = candidate["context"]
                source = InstagramBotMessage.objects.select_for_update().filter(pk=context["source"]["source_message_id"], client=client).first()
                session = IgCommerceSelectionSession.objects.select_for_update().filter(pk=context["session_id"],
                    client=client, open_slot=1, state="open", commercial_episode_id=client.current_commercial_episode_id).first()
                if source is None or session is None or session.revision != expected_selection_revision:
                    raise SizeCorrectionRejected("correction_selection_conflict")
                # Recheck after source/session lock waits, using the same clock
                # semantics as the displayed current observation vector.
                captured = current_admin_state(client_id, expected_selection_revision=expected_selection_revision, now=now or timezone.now())
                current = captured.state.as_dict().get("boundary", {}).get("size_correction_context") or {}
                if captured.status != "captured" or current.get("context_digest") != expected_context_digest:
                    raise SizeCorrectionRejected("correction_context_conflict")
                if (source.sender_id != client.igsid or source.role != "user" or source.source != "webhook"
                    or source.status == "failed" or source.provider_namespace != context["source_namespace"]
                    or hashlib.sha256(source.text.encode()).hexdigest() != context["source"]["source_digest"]):
                    raise SizeCorrectionRejected("correction_source_conflict")
                if context["value"] == size:
                    return SizeCorrectionResult("noop", str(op), None, session.revision)
                fresh_actor = _actor(actor)
                receipt = {"schema": SCHEMA, "field": "size", "actor_id": fresh_actor.pk,
                    "capabilities": list(CAPABILITIES), "operation_id": str(op), "reason_code": reason_code,
                    "context": deepcopy(context), "context_digest": expected_context_digest, "input_digest": request_digest,
                    "operation": operation, "before": context["value"], "after": size,
                    "supersedes_transition_id": context["source"]["transition_id"], "recorded_at": (now or timezone.now()).isoformat()}
                if len(json.dumps(receipt).encode()) > MAX_RECEIPT_BYTES:
                    raise SizeCorrectionRejected("correction_receipt_budget_exceeded")
                transition = persist_size_correction_transition(session, source, operation_id=op, receipt=receipt)
                return SizeCorrectionResult("applied", str(op), transition.pk, transition.to_revision)
    except ReplyBoundaryTimeout:
        raise SizeCorrectionRejected("correction_boundary_busy", retryable=True) from None
    except IntegrityError:
        existing = IgCommerceSelectionTransition.objects.filter(correction_operation_id=op).select_related("session").first()
        if existing:
            return _replay(existing, request_digest=request_digest, client_id=client_id, actor_id=fresh_actor.pk)
        raise SizeCorrectionRejected("correction_write_unavailable", status=503, retryable=True) from None
    except DatabaseError:
        raise SizeCorrectionRejected("correction_write_unavailable", status=503, retryable=True) from None
