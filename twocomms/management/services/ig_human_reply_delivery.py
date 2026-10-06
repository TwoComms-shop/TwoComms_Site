"""Human-owned delivery state and private-document stores; no transport I/O.

Consumers must commit record_human_part_started before the physical request,
then checkpoint every exact MID with settle_human_part. UNKNOWN never grants a
new request. Existing dispatcher/periodic/echo integration remains separate.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import re
import secrets
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone

from management.ig_human_reply_models import (
    HumanReplyPart, HumanReplyPrivateDocument, human_payload_digest,
)
from management.ig_bot_models import HumanReplyCommand
from management.models import AdminAuditLog, IgClient, InstagramBotMessage, InstagramBotSettings
from management.services.ig_delivery_plan import build_delivery_plan, DEFAULT_MAX_CHUNKS
from management.services.ig_delivery_receipts import normalize_provider_message_id
from management.services.ig_human_reply import MAX_HUMAN_REPLY_CHARS, HumanReplyRejected

PLAN_KEY = "human_delivery"
PLAN_VERSION = "human-delivery-plan.v1"
CONTEXT_VERSION = "human-context.v1"
# The existing shared effect claim interval; this is an I/O lease, not retention.
PART_LEASE_SECONDS = 60
_HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class HumanDeliveryResult:
    ready: bool = False
    reason: str = ""
    command: object = None
    parts: tuple = ()
    token: str = ""
    changed: bool = False

    @property
    def part(self):
        return self.parts[0] if self.parts else None


@dataclass(frozen=True)
class ReceiptClassification:
    state: str
    reason: str
    provider_message_id: str = ""


def classify_human_receipt(*, http_status=None, provider_message_id="", transport_outcome="response"):
    """Pure conservative classification; no ambiguous HTTP result is retried."""
    mid = normalize_provider_message_id(provider_message_id)
    if mid and any(ord(char) < 32 or ord(char) == 127 for char in mid):
        mid = ""
    if transport_outcome == "definite_rejection" and not mid:
        return ReceiptClassification(HumanReplyPart.State.DEFINITE_FAILED, "provider_rejected")
    if transport_outcome != "response":
        return ReceiptClassification(HumanReplyPart.State.UNKNOWN, "provider_transport_unknown")
    if http_status == 200 and mid:
        return ReceiptClassification(HumanReplyPart.State.SENT, "receipt_confirmed", mid)
    if http_status == 200:
        return ReceiptClassification(HumanReplyPart.State.UNKNOWN, "provider_message_id_missing")
    if isinstance(http_status, int) and not isinstance(http_status, bool) and 400 <= http_status < 500 and http_status not in {408, 425, 429}:
        return ReceiptClassification(HumanReplyPart.State.DEFINITE_FAILED, "provider_rejected")
    return ReceiptClassification(HumanReplyPart.State.UNKNOWN, "provider_result_unknown")


def _date(value):
    return value.isoformat() if value else ""


def _context_payload(source):
    return dict(schema_version=CONTEXT_VERSION, message_id=source.pk, client_id=source.client_id, sender_id=source.sender_id,
        provider_namespace=source.provider_namespace, role=source.role, source=source.source,
        mid=source.mid or "", text=source.text or "", provider_created_at=_date(source.provider_created_at),
        observed_created_at=_date(source.created_at), reply_to=source.reply_to_provider_message_id or "",
        quick_reply=source.quick_reply_payload or "", attachments=source.attachments or "",
        attachment_media=source.attachment_media or [])


def _context_binding(command):
    """Same source digest as private documents, plus the canonical reset floor."""
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    source = command.context_message
    if source is None:
        return None
    return dict(schema_version=CONTEXT_VERSION, message_id=source.pk,
        source_digest=human_payload_digest(_context_payload(source)),
        reset_floor=conversation_route_reset_floor(command.client_id))


def _current_context_reason(command):
    return "" if _plan(command).get("context_binding") == _context_binding(command) else "human_context_changed"


def _command_scope(command):
    return dict(command_id=command.pk, operation_id=str(command.operation_id), client_id=command.client_id,
        actor_id=command.actor_id, context_message_id=command.context_message_id,
        recipient_igsid=command.recipient_igsid, provider_namespace=command.provider_namespace,
        permission_epoch=command.permission_epoch, window_deadline=_date(command.window_deadline),
        context_revision=command.context_revision)


def _payload(namespace, recipient, text):
    result = {"recipient": {"id": recipient}, "message": {"text": text}}
    if namespace.startswith("legacy_page:"):
        result["messaging_type"] = "RESPONSE"
    return result


def _locked_scope(command_id):
    """Settings -> client -> command; never hold these locks across HTTP."""
    identity = HumanReplyCommand.objects.filter(pk=command_id).values("client_id").first()
    if identity is None:
        return None, None, None
    settings = InstagramBotSettings.objects.select_for_update().order_by("pk").first()
    client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
    command = HumanReplyCommand.objects.select_for_update().select_related("actor", "context_message").filter(
        pk=command_id, client_id=identity["client_id"]).first()
    return settings, client, command


def _send_reason(settings, client, command, now):
    if settings is None or client is None or command is None:
        return "human_owner_missing"
    from management.services.ig_human_reply import _command_boundary_reason
    from management.services.instagram_bot import ingress_provider_namespace
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    if command.context_message is None or command.context_message.status == "failed":
        return "context_changed"
    if not command.context_message_id or command.context_message_id < conversation_route_reset_floor(client.pk):
        return "context_before_reset"
    return _command_boundary_reason(command, client,
        namespace=ingress_provider_namespace(settings), now=now)


def _plan(command):
    value = (command.operation_context or {}).get(PLAN_KEY)
    return value if isinstance(value, dict) and value.get("version") == PLAN_VERSION else {}


def _part_rows(command):
    return list(HumanReplyPart.objects.select_for_update().filter(command_id=command.pk).order_by("ordinal")[:DEFAULT_MAX_CHUNKS + 1])


def _part_material(rows):
    return [dict(ordinal=row.ordinal, payload=row.payload, payload_digest=row.payload_digest) for row in rows]


def _valid_plan(command, rows, *, current_owner=True):
    plan = _plan(command)
    if (not plan or not rows or len(rows) > DEFAULT_MAX_CHUNKS or plan.get("part_count") != len(rows)
        or [row.ordinal for row in rows] != list(range(len(rows)))
        or plan.get("digest") != human_payload_digest({key: value for key, value in plan.items() if key not in {"digest", "claim"}})
        or plan.get("parts") != _part_material(rows)):
        return False
    scope = plan.get("scope") or {}
    binding = plan.get("context_binding")
    if (not isinstance(binding, dict) or binding.get("schema_version") != CONTEXT_VERSION
        or binding.get("message_id") != scope.get("context_message_id")
        or not isinstance(binding.get("source_digest"), str) or not _HASH.fullmatch(binding["source_digest"])
        or not isinstance(binding.get("reset_floor"), int) or isinstance(binding["reset_floor"], bool)
        or binding["reset_floor"] < 1 or binding["message_id"] < binding["reset_floor"]):
        return False
    if current_owner and scope != _command_scope(command):
        return False
    # Late settlement preserves original actor/context/epoch even after an
    # actor/context deletion or a later permission change; it grants no send.
    if (scope.get("command_id") != command.pk or scope.get("operation_id") != str(command.operation_id)
        or scope.get("client_id") != command.client_id or scope.get("recipient_igsid") != command.recipient_igsid
        or scope.get("provider_namespace") != command.provider_namespace):
        return False
    return all(row.plan_digest == plan["digest"] and row.part_count == len(rows)
        and row.command_id == command.pk and row.client_id == command.client_id
        and str(row.operation_id_snapshot) == scope.get("operation_id")
        and row.actor_id_snapshot == scope.get("actor_id")
        and row.context_message_id_snapshot == scope.get("context_message_id")
        and row.permission_epoch == scope.get("permission_epoch")
        and row.provider_namespace == scope.get("provider_namespace")
        and row.recipient_igsid == scope.get("recipient_igsid")
        and row.payload_digest == human_payload_digest(row.payload) for row in rows)


def _update_plan(command, plan):
    command.operation_context = {**(command.operation_context or {}), PLAN_KEY: plan}
    command.save(update_fields=["operation_context", "updated_at"])


def plan_human_command(command_id, *, now=None):
    """Persist every part before transport; refuse ambiguous legacy migration."""
    with transaction.atomic():
        settings, client, command = _locked_scope(command_id)
        if command is None:
            return HumanDeliveryResult(reason="human_command_missing")
        rows = _part_rows(command)
        if rows or _plan(command):
            reason = "human_plan_changed" if not _valid_plan(command, rows) else _current_context_reason(command)
            return HumanDeliveryResult(not reason, reason, command, tuple(rows))
        if (command.state != command.State.PENDING or command.provider_started_at
            or command.provider_message_ids or command.reply_message_id):
            return HumanDeliveryResult(reason="legacy_human_delivery_owned", command=command)
        checked_at = now or timezone.now()
        reason = _send_reason(settings, client, command, checked_at)
        if reason:
            return HumanDeliveryResult(reason=reason, command=command)
        delivery = build_delivery_plan(command.text)
        if not delivery.complete or not delivery.chunks or len(delivery.chunks) > DEFAULT_MAX_CHUNKS:
            return HumanDeliveryResult(reason="delivery_plan_incomplete", command=command)
        scope = _command_scope(command)
        if not scope["actor_id"] or not scope["context_message_id"]:
            return HumanDeliveryResult(reason="human_command_owner_unknown", command=command)
        material = [dict(ordinal=index, payload=_payload(command.provider_namespace, command.recipient_igsid, text))
            for index, text in enumerate(delivery.chunks)]
        for item in material:
            item["payload_digest"] = human_payload_digest(item["payload"])
        plan = dict(version=PLAN_VERSION, scope=scope, context_binding=_context_binding(command), text_digest=hashlib.sha256(command.text.encode()).hexdigest(),
                    part_count=len(material), parts=material)
        plan["digest"] = human_payload_digest(plan)
        for item in material:
            rows.append(HumanReplyPart.objects.create(command=command, client=client,
                operation_id_snapshot=command.operation_id, actor_id_snapshot=command.actor_id,
                context_message_id_snapshot=command.context_message_id, permission_epoch=command.permission_epoch,
                recipient_igsid=command.recipient_igsid, provider_namespace=command.provider_namespace,
                ordinal=item["ordinal"], part_count=len(material), plan_version=PLAN_VERSION,
                plan_digest=plan["digest"], payload=item["payload"], payload_digest=item["payload_digest"]))
        _update_plan(command, plan)
        return HumanDeliveryResult(True, command=command, parts=tuple(rows), changed=True)


def _aggregate(command, rows, now):
    ids = [row.provider_message_id for row in rows if row.state == row.State.SENT and row.provider_message_id]
    states = {row.state for row in rows}
    if states == {HumanReplyPart.State.SENT}:
        command.state, command.failure_code, command.terminal_at = command.State.SENT, "", now
    elif HumanReplyPart.State.UNKNOWN in states or (ids and states.intersection({HumanReplyPart.State.CANCELLED, HumanReplyPart.State.DEFINITE_FAILED})):
        command.state, command.failure_code, command.terminal_at = command.State.UNKNOWN, "human_delivery_partial_or_unknown", now
    elif states <= {HumanReplyPart.State.CANCELLED, HumanReplyPart.State.DEFINITE_FAILED}:
        command.state = command.State.DEFINITE_FAILED if HumanReplyPart.State.DEFINITE_FAILED in states else command.State.CANCELLED
        command.failure_code, command.terminal_at = "human_delivery_not_complete", now
    elif HumanReplyPart.State.PROVIDER_STARTED in states or ids:
        command.state = command.State.PROVIDER_STARTED
    else:
        command.state = command.State.CLAIMED if HumanReplyPart.State.CLAIMED in states else command.State.PENDING
    command.provider_message_ids = ids
    command.save(update_fields=["state", "failure_code", "terminal_at", "provider_message_ids", "updated_at"])


def _cancel_following(rows, failed, reason, now):
    for row in rows:
        if row.ordinal > failed.ordinal and row.state in {row.State.PLANNED, row.State.CLAIMED}:
            row.state, row.failure_code, row.terminal_at = row.State.CANCELLED, reason, now
            row.claim_token, row.lease_until = "", None
            row.save(update_fields=["state", "failure_code", "terminal_at", "claim_token", "lease_until", "updated_at"])


def claim_next_human_part(command_id, *, now=None, lease_seconds=PART_LEASE_SECONDS):
    with transaction.atomic():
        settings, client, command = _locked_scope(command_id)
        if command is None:
            return HumanDeliveryResult(reason="human_command_missing")
        rows = _part_rows(command)
        if not _valid_plan(command, rows) or _plan(command).get("text_digest") != hashlib.sha256(command.text.encode()).hexdigest():
            return HumanDeliveryResult(reason="human_plan_changed", command=command)
        checked_at = now or timezone.now()
        reason = _send_reason(settings, client, command, checked_at) or _current_context_reason(command)
        if reason:
            return HumanDeliveryResult(reason=reason, command=command)
        if command.state in {command.State.SENT, command.State.UNKNOWN, command.State.DEFINITE_FAILED, command.State.CANCELLED}:
            return HumanDeliveryResult(reason="human_command_terminal", command=command)
        if any(row.state in {row.State.PROVIDER_STARTED, row.State.UNKNOWN} for row in rows):
            return HumanDeliveryResult(reason="human_provider_result_unresolved", command=command)
        part = next((row for row in rows if row.state != row.State.SENT), None)
        if part is None or part.state not in {part.State.PLANNED, part.State.CLAIMED}:
            return HumanDeliveryResult(reason="human_part_not_claimable", command=command)
        if part.state == part.State.CLAIMED and part.lease_until and part.lease_until > checked_at:
            return HumanDeliveryResult(reason="human_part_already_claimed", command=command, parts=(part,))
        if part.provider_started_at is not None:
            return HumanDeliveryResult(reason="human_provider_result_unresolved", command=command, parts=(part,))
        try:
            seconds = int(lease_seconds)
        except (TypeError, ValueError, OverflowError):
            return HumanDeliveryResult(reason="human_lease_invalid", command=command)
        if seconds < 1 or seconds > PART_LEASE_SECONDS:
            return HumanDeliveryResult(reason="human_lease_invalid", command=command)
        token = secrets.token_hex(16)
        part.state, part.claim_token, part.claimed_at = part.State.CLAIMED, token, checked_at
        part.lease_until = min(checked_at + timedelta(seconds=seconds), command.window_deadline)
        part.save(update_fields=["state", "claim_token", "claimed_at", "lease_until", "updated_at"])
        plan = deepcopy(_plan(command))
        plan["claim"] = dict(part_id=part.pk, token=token, claimed_at=_date(checked_at), lease_until=_date(part.lease_until))
        _update_plan(command, plan)
        _aggregate(command, rows, checked_at)
        return HumanDeliveryResult(True, command=command, parts=(part,), token=token, changed=True)


def record_human_part_started(part_id, token, *, now=None):
    identity = HumanReplyPart.objects.filter(pk=part_id).values("command_id").first()
    if identity is None:
        return HumanDeliveryResult(reason="human_part_missing")
    with transaction.atomic():
        settings, client, command = _locked_scope(identity["command_id"])
        if command is None:
            return HumanDeliveryResult(reason="human_command_missing")
        rows = _part_rows(command)
        part = next((row for row in rows if row.pk == part_id), None)
        checked_at = now or timezone.now()
        if (part is None or not token or part.claim_token != token or part.state != part.State.CLAIMED
            or not part.lease_until or part.lease_until <= checked_at or part.provider_started_at is not None):
            return HumanDeliveryResult(reason="human_part_claim_lost", command=command)
        if not _valid_plan(command, rows) or _plan(command).get("text_digest") != hashlib.sha256(command.text.encode()).hexdigest():
            return HumanDeliveryResult(reason="human_plan_changed", command=command, parts=(part,))
        reason = ("human_command_terminal" if command.state not in {command.State.PENDING, command.State.CLAIMED, command.State.PROVIDER_STARTED}
                  else (_send_reason(settings, client, command, checked_at) or _current_context_reason(command)))
        if reason:
            part.state, part.failure_code, part.terminal_at = part.State.CANCELLED, reason, checked_at
            part.save(update_fields=["state", "failure_code", "terminal_at", "updated_at"])
            _cancel_following(rows, part, "human_boundary_changed", checked_at)
            _aggregate(command, rows, checked_at)
            return HumanDeliveryResult(reason=reason, command=command, parts=(part,), changed=True)
        if any(row.ordinal < part.ordinal and row.state != row.State.SENT for row in rows):
            return HumanDeliveryResult(reason="human_part_order_changed", command=command, parts=(part,))
        part.state, part.provider_started_at = part.State.PROVIDER_STARTED, checked_at
        part.save(update_fields=["state", "provider_started_at", "updated_at"])
        command.provider_started_at = command.provider_started_at or checked_at
        command.save(update_fields=["provider_started_at", "updated_at"])
        _aggregate(command, rows, checked_at)
        return HumanDeliveryResult(True, command=command, parts=(part,), token=token, changed=True)


def settle_human_part(part_id, token, *, provider_namespace, http_status=None,
                      provider_message_id="", transport_outcome="response", response_digest="", now=None):
    """Exact original receipt may finish an expired UNKNOWN; never re-sends."""
    with transaction.atomic():
        identity = HumanReplyPart.objects.filter(pk=part_id).values("command_id").first()
        if identity is None:
            return HumanDeliveryResult(reason="human_part_missing")
        command = HumanReplyCommand.objects.select_for_update().filter(pk=identity["command_id"]).first()
        if command is None:
            return HumanDeliveryResult(reason="human_command_missing")
        rows = _part_rows(command)
        part = next((row for row in rows if row.pk == part_id), None)
        if (part is None or not token or part.claim_token != token or not part.provider_started_at
            or part.state not in {part.State.PROVIDER_STARTED, part.State.UNKNOWN, part.State.SENT}):
            return HumanDeliveryResult(reason="human_part_receipt_not_owned", command=command)
        if not _valid_plan(command, rows, current_owner=False):
            return HumanDeliveryResult(reason="human_plan_changed", command=command, parts=(part,))
        if provider_namespace != part.provider_namespace:
            return HumanDeliveryResult(reason="human_receipt_namespace_changed", command=command, parts=(part,))
        outcome = classify_human_receipt(http_status=http_status, provider_message_id=provider_message_id,
                                         transport_outcome=transport_outcome)
        if part.state == part.State.SENT:
            same = outcome.state == part.State.SENT and part.provider_message_id == outcome.provider_message_id
            return HumanDeliveryResult(same, "" if same else "human_receipt_conflict", command, (part,))
        if part.state == part.State.UNKNOWN and outcome.state != part.State.SENT:
            return HumanDeliveryResult(reason="human_receipt_ambiguous_preserved", command=command, parts=(part,))
        if response_digest and not _HASH.fullmatch(response_digest):
            return HumanDeliveryResult(reason="human_response_digest_invalid", command=command, parts=(part,))
        checked_at = now or timezone.now()
        if outcome.provider_message_id and HumanReplyPart.objects.filter(provider_namespace=provider_namespace,
                provider_message_id=outcome.provider_message_id).exclude(pk=part.pk).exists():
            return HumanDeliveryResult(reason="human_receipt_identity_conflict", command=command, parts=(part,))
        part.state, part.failure_code, part.terminal_at = outcome.state, "" if outcome.state == part.State.SENT else outcome.reason, checked_at
        if outcome.provider_message_id:
            part.provider_message_id = outcome.provider_message_id
        part.response_digest, part.lease_until = response_digest, None
        try:
            with transaction.atomic():
                part.save(update_fields=["state", "failure_code", "terminal_at", "provider_message_id", "response_digest", "lease_until", "updated_at"])
        except IntegrityError:
            return HumanDeliveryResult(reason="human_receipt_identity_conflict", command=command)
        if part.state != part.State.SENT:
            _cancel_following(rows, part, "human_previous_part_not_confirmed", checked_at)
        _aggregate(command, rows, checked_at)
        return HumanDeliveryResult(part.state == part.State.SENT, outcome.reason, command, (part,), changed=True)


def human_reaper_candidates(*, now=None, limit=25):
    """Bounded IDs only; caller owns periodic scheduling and reconciliation UI."""
    bounded = max(0, min(int(limit), 100))
    return tuple(HumanReplyPart.objects.filter(state__in=(HumanReplyPart.State.CLAIMED, HumanReplyPart.State.PROVIDER_STARTED),
        lease_until__lte=now or timezone.now()).order_by("lease_until", "pk").values_list("pk", flat=True)[:bounded])


def reap_human_part(part_id, *, now=None):
    with transaction.atomic():
        identity = HumanReplyPart.objects.filter(pk=part_id).values("command_id").first()
        if identity is None:
            return HumanDeliveryResult(reason="human_part_missing")
        command = HumanReplyCommand.objects.select_for_update().filter(pk=identity["command_id"]).first()
        if command is None:
            return HumanDeliveryResult(reason="human_command_missing")
        rows = _part_rows(command)
        part = next((row for row in rows if row.pk == part_id), None)
        checked_at = now or timezone.now()
        if (part is None or part.state not in {part.State.CLAIMED, part.State.PROVIDER_STARTED}
            or not part.lease_until or part.lease_until > checked_at):
            return HumanDeliveryResult(reason="human_part_not_expired", command=command)
        if not _valid_plan(command, rows, current_owner=False):
            return HumanDeliveryResult(reason="human_plan_changed", command=command)
        if part.state == part.State.PROVIDER_STARTED:
            part.state, part.failure_code, part.terminal_at = part.State.UNKNOWN, "human_provider_lease_expired", checked_at
            # Preserve original token/start for late exact settlement.
            _cancel_following(rows, part, "human_previous_part_unknown", checked_at)
        else:
            if part.provider_started_at:
                return HumanDeliveryResult(reason="human_provider_result_unresolved", command=command)
            part.state, part.claim_token, part.failure_code = part.State.PLANNED, "", ""
        part.lease_until = None
        part.save(update_fields=["state", "claim_token", "failure_code", "terminal_at", "lease_until", "updated_at"])
        _aggregate(command, rows, checked_at)
        return HumanDeliveryResult(True, command=command, parts=(part,), changed=True)


def _private_actor(actor):
    from django.contrib.auth import get_user_model
    current = get_user_model().objects.filter(pk=getattr(actor, "pk", None)).first()
    from management.bot_access import has_all_bot_capabilities, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
    if not has_all_bot_capabilities(current, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
        raise HumanReplyRejected("actor_not_authorized")
    return current


def _private_identity(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise HumanReplyRejected("private_document_id_invalid") from None


def _private_cas(version, digest):
    if not isinstance(version, int) or isinstance(version, bool) or version < 1 or not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise HumanReplyRejected("private_document_stale")


def _private_text(text):
    if not isinstance(text, str) or not text.strip():
        raise HumanReplyRejected("empty_text")
    text = text.strip()
    if len(text) > MAX_HUMAN_REPLY_CHARS:
        raise HumanReplyRejected("text_too_long")
    return text


def _document_owner(client, context, namespace, kind, now):
    if client is None or client.hidden_at or client.privacy_erasure_started_at:
        raise HumanReplyRejected("client_unavailable")
    if context is None or context.client_id != client.pk or context.sender_id != client.igsid or context.role != "user" or context.status == "failed":
        raise HumanReplyRejected("context_changed")
    if not namespace or context.provider_namespace != namespace:
        raise HumanReplyRejected("context_namespace_changed")
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    floor = conversation_route_reset_floor(client.pk)
    if context.pk < floor:
        raise HumanReplyRejected("context_before_reset")
    if kind == HumanReplyPrivateDocument.Kind.REPLY_DRAFT:
        from management.services.ig_human_reply import _latest_inbound, _window_deadline
        latest = _latest_inbound(client)
        if latest is None or latest.pk != context.pk:
            raise HumanReplyRejected("newer_inbound")
        deadline = _window_deadline(context)
        if deadline is None or deadline <= now:
            raise HumanReplyRejected("reply_window_closed")
    return floor


def create_private_document(client_id, *, actor, kind, text, context_message_id, provider_namespace,
                            expected_permission_epoch=None, document_id=None, now=None):
    """Private save causes no takeover, transcript, command, or provider effect."""
    actor = _private_actor(actor)
    text = _private_text(text)
    if kind not in HumanReplyPrivateDocument.Kind.values:
        raise HumanReplyRejected("private_kind_invalid")
    try:
        identity = uuid.UUID(str(document_id)) if document_id else uuid.uuid4()
    except (TypeError, ValueError):
        raise HumanReplyRejected("private_document_id_invalid") from None
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=client_id).first()
        if client is None:
            raise HumanReplyRejected("client_unavailable")
        actor = _private_actor(actor)
        context = InstagramBotMessage.objects.filter(pk=context_message_id, client_id=client_id).first()
        floor = _document_owner(client, context, provider_namespace, kind, now or timezone.now())
        if expected_permission_epoch is not None and client.reply_permission_epoch != expected_permission_epoch:
            raise HumanReplyRejected("permission_epoch_changed")
        existing = HumanReplyPrivateDocument.objects.select_for_update().filter(document_id=identity).first()
        digest = human_payload_digest(_context_payload(context))
        if existing is not None:
            if (existing.client_id != client.pk or existing.actor_id != actor.pk or existing.kind != kind
                or existing.context_message_id != context.pk or existing.context_digest != digest or existing.text != text):
                raise HumanReplyRejected("private_document_conflict")
            return existing
        try:
            with transaction.atomic():
                return HumanReplyPrivateDocument.objects.create(document_id=identity, client=client, actor=actor,
                    kind=kind, recipient_igsid=client.igsid, provider_namespace=provider_namespace, context_message=context,
                    context_digest=digest, reset_floor=floor, source_permission_epoch=client.reply_permission_epoch,
                    text=text, text_hash=hashlib.sha256(text.encode()).hexdigest())
        except IntegrityError:
            raise HumanReplyRejected("private_document_conflict") from None


def update_private_document(document_id, *, actor, expected_version, expected_hash, text=None,
                            archive=False, now=None):
    actor = _private_actor(actor)
    document_id = _private_identity(document_id)
    _private_cas(expected_version, expected_hash)
    with transaction.atomic():
        identity = HumanReplyPrivateDocument.objects.filter(document_id=document_id).values("client_id").first()
        if identity is None:
            raise HumanReplyRejected("private_document_missing")
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        if client is None:
            raise HumanReplyRejected("client_unavailable")
        doc = HumanReplyPrivateDocument.objects.select_for_update().filter(document_id=document_id).first()
        if doc is None:
            raise HumanReplyRejected("private_document_missing")
        actor = _private_actor(actor)
        if doc.actor_id != actor.pk:
            raise HumanReplyRejected("private_document_owned_by_other_actor")
        if doc.version != expected_version or doc.text_hash != expected_hash:
            raise HumanReplyRejected("private_document_stale")
        if doc.state != doc.State.OPEN:
            raise HumanReplyRejected("private_document_closed")
        context = InstagramBotMessage.objects.filter(pk=doc.context_message_id, client_id=doc.client_id).first()
        floor = _document_owner(client, context, doc.provider_namespace, doc.kind, now or timezone.now())
        if floor != doc.reset_floor or human_payload_digest(_context_payload(context)) != doc.context_digest:
            raise HumanReplyRejected("private_context_changed")
        if doc.kind == doc.Kind.REPLY_DRAFT and client.reply_permission_epoch != doc.source_permission_epoch:
            raise HumanReplyRejected("permission_epoch_changed")
        if archive:
            doc.state = doc.State.ARCHIVED
        elif text is not None:
            doc.text = _private_text(text)
            doc.text_hash = hashlib.sha256(doc.text.encode()).hexdigest()
        else:
            raise HumanReplyRejected("private_update_empty")
        doc.version += 1
        doc.save(update_fields=["text", "text_hash", "state", "version", "updated_at"])
        return doc


def bind_private_draft_command(document_id, command_id, *, actor, expected_version, expected_hash, now=None):
    """Bind an ALREADY authorized command; this does not create/send one."""
    actor = _private_actor(actor)
    document_id = _private_identity(document_id)
    _private_cas(expected_version, expected_hash)
    with transaction.atomic():
        identity = HumanReplyPrivateDocument.objects.filter(document_id=document_id).values("client_id").first()
        if identity is None:
            raise HumanReplyRejected("private_document_missing")
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        if client is None:
            raise HumanReplyRejected("client_unavailable")
        command = HumanReplyCommand.objects.select_for_update().filter(pk=command_id, client_id=identity["client_id"]).first()
        doc = HumanReplyPrivateDocument.objects.select_for_update().filter(document_id=document_id).first()
        if doc is None:
            raise HumanReplyRejected("private_document_missing")
        actor = _private_actor(actor)
        if doc.actor_id != actor.pk:
            raise HumanReplyRejected("private_document_owned_by_other_actor")
        if doc.kind != doc.Kind.REPLY_DRAFT:
            raise HumanReplyRejected("private_note_not_sendable")
        if doc.state == doc.State.CONSUMED and doc.consumed_command_id == command_id:
            if doc.version != expected_version + 1 or doc.text_hash != expected_hash:
                raise HumanReplyRejected("private_document_stale")
            return doc
        if doc.state != doc.State.OPEN or doc.version != expected_version or doc.text_hash != expected_hash:
            raise HumanReplyRejected("private_document_stale")
        if (client is None or client.privacy_erasure_started_at or command is None or command.actor_id != actor.pk
            or command.context_message_id != doc.context_message_id or command.recipient_igsid != doc.recipient_igsid
            or command.provider_namespace != doc.provider_namespace or command.text != doc.text
            or command.state != command.State.PENDING or command.provider_started_at
            or command.draft_hash != doc.text_hash):
            raise HumanReplyRejected("private_command_binding_changed")
        context = InstagramBotMessage.objects.filter(pk=doc.context_message_id, client_id=doc.client_id).first()
        floor = _document_owner(client, context, doc.provider_namespace, doc.kind, now or timezone.now())
        if floor != doc.reset_floor or human_payload_digest(_context_payload(context)) != doc.context_digest:
            raise HumanReplyRejected("private_context_changed")
        # The existing takeover audit proves the original source epoch without
        # assuming every authorized human click increments it. An outer caller
        # transaction must keep draft CAS + command creation + binding atomic.
        audits = list(AdminAuditLog.objects.filter(action="ig_bot.human_reply_command_created",
            entity_type="HumanReplyCommand", entity_id=str(command.pk), actor_id=actor.pk).values("before", "after")[:2])
        if (len(audits) != 1 or doc.created_at > command.created_at
            or (audits[0]["before"] or {}).get("permission_epoch") != doc.source_permission_epoch
            or (audits[0]["after"] or {}).get("permission_epoch") != command.permission_epoch
            or command.permission_epoch != client.reply_permission_epoch):
            raise HumanReplyRejected("permission_epoch_changed")
        doc.state, doc.consumed_command, doc.version = doc.State.CONSUMED, command, doc.version + 1
        doc.save(update_fields=["state", "consumed_command", "version", "updated_at"])
        return doc


def check_human_part_boundary(part_id, token, *, require_started=False, now=None):
    """Fresh human permission/source proof; caller holds the physical send barrier."""
    identity = HumanReplyPart.objects.filter(pk=part_id).values("command_id").first()
    if identity is None:
        return HumanDeliveryResult(reason="human_part_missing")
    with transaction.atomic():
        settings, client, command = _locked_scope(identity["command_id"])
        if command is None:
            return HumanDeliveryResult(reason="human_command_missing")
        rows = _part_rows(command)
        part = next((row for row in rows if row.pk == part_id), None)
        checked_at = now or timezone.now()
        wanted = HumanReplyPart.State.PROVIDER_STARTED if require_started else HumanReplyPart.State.CLAIMED
        if (part is None or not token or part.claim_token != token or part.state != wanted
            or not part.lease_until or part.lease_until <= checked_at):
            return HumanDeliveryResult(reason="human_part_claim_lost", command=command)
        if command.state not in {command.State.PENDING, command.State.CLAIMED, command.State.PROVIDER_STARTED}:
            return HumanDeliveryResult(reason="human_command_terminal", command=command)
        if not _valid_plan(command, rows) or _plan(command).get("text_digest") != hashlib.sha256(command.text.encode()).hexdigest():
            return HumanDeliveryResult(reason="human_plan_changed", command=command)
        reason = _send_reason(settings, client, command, checked_at) or _current_context_reason(command)
        if reason:
            return HumanDeliveryResult(reason=reason, command=command, parts=(part,))
        if any(row.ordinal < part.ordinal and row.state != row.State.SENT for row in rows):
            return HumanDeliveryResult(reason="human_part_order_changed", command=command)
        return HumanDeliveryResult(True, command=command, parts=(part,), token=token)


def fail_human_part_before_request(part_id, token, *, reason, cancelled=False, now=None):
    """Definite local/preflight rejection, permitted only before start marker."""
    with transaction.atomic():
        identity = HumanReplyPart.objects.filter(pk=part_id).values("command_id").first()
        if identity is None:
            return HumanDeliveryResult(reason="human_part_missing")
        command = HumanReplyCommand.objects.select_for_update().filter(pk=identity["command_id"]).first()
        if command is None:
            return HumanDeliveryResult(reason="human_command_missing")
        rows = _part_rows(command)
        part = next((row for row in rows if row.pk == part_id), None)
        if (part is None or part.state != part.State.CLAIMED or part.claim_token != token or part.provider_started_at):
            return HumanDeliveryResult(reason="human_part_claim_lost", command=command)
        if not _valid_plan(command, rows, current_owner=False):
            return HumanDeliveryResult(reason="human_plan_changed", command=command)
        checked_at = now or timezone.now()
        part.state = part.State.CANCELLED if cancelled else part.State.DEFINITE_FAILED
        part.failure_code, part.terminal_at, part.lease_until = str(reason)[:64], checked_at, None
        part.save(update_fields=["state", "failure_code", "terminal_at", "lease_until", "updated_at"])
        _cancel_following(rows, part, "human_previous_part_not_confirmed", checked_at)
        _aggregate(command, rows, checked_at)
        return HumanDeliveryResult(True, command=command, parts=(part,), changed=True)


def stop_human_delivery_tail(command_id, *, reason, now=None):
    """Cancel only unstarted tail rows; keep every started/confirmed receipt."""
    with transaction.atomic():
        command = HumanReplyCommand.objects.select_for_update().filter(pk=command_id).first()
        if command is None:
            return HumanDeliveryResult(reason="human_command_missing")
        rows = _part_rows(command)
        if not _valid_plan(command, rows, current_owner=False):
            return HumanDeliveryResult(reason="human_plan_changed", command=command)
        checked_at, changed = now or timezone.now(), False
        for part in rows:
            if part.state in {part.State.PLANNED, part.State.CLAIMED} and part.provider_started_at is None:
                part.state, part.failure_code, part.terminal_at = part.State.CANCELLED, str(reason)[:64], checked_at
                part.claim_token, part.lease_until = "", None
                part.save(update_fields=["state", "failure_code", "terminal_at", "claim_token", "lease_until", "updated_at"])
                changed = True
        if changed:
            _aggregate(command, rows, checked_at)
        return HumanDeliveryResult(True, command=command, parts=tuple(rows), changed=changed)


def halt_human_delivery_invalid_plan(command_id, *, now=None):
    """Finite quarantine for malformed metadata; preserve historical receipts."""
    with transaction.atomic():
        command = HumanReplyCommand.objects.select_for_update().filter(pk=command_id).first()
        if command is None:
            return HumanDeliveryResult(reason="human_command_missing")
        if command.state == command.State.SENT:
            return HumanDeliveryResult(reason="human_command_terminal", command=command)
        rows = _part_rows(command)
        checked_at = now or timezone.now()
        started = bool(command.provider_started_at or command.provider_message_ids
            or any(part.provider_started_at or part.provider_message_id for part in rows))
        for part in rows:
            if part.state in {part.State.PLANNED, part.State.CLAIMED} and part.provider_started_at is None:
                part.state, part.failure_code, part.terminal_at = part.State.CANCELLED, "human_plan_changed", checked_at
                part.claim_token, part.lease_until = "", None
                part.save(update_fields=["state", "failure_code", "terminal_at", "claim_token", "lease_until", "updated_at"])
        command.state = command.State.UNKNOWN if started else command.State.CANCELLED
        command.failure_code, command.terminal_at = "human_plan_changed", checked_at
        command.save(update_fields=["state", "failure_code", "terminal_at", "updated_at"])
        return HumanDeliveryResult(True, command=command, parts=tuple(rows), changed=True)
