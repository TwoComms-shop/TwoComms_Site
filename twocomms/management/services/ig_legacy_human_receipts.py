"""Read-only proof of already checkpointed whole-command human receipts.

The old sender stored one transcript with a blank provider namespace, plus an
ordered command receipt list. Its linked transcript/op binding can prove an
exact MID without creating parts, restoring history, or sending anything.
Callers that apply attribution compose this reader under their existing client
lock; this module neither starts transactions nor changes historical records.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import uuid

from django.db import DatabaseError
from django.db.models import Exists, OuterRef, Q


LEGACY_MAX_RECEIPTS = 4  # The historical whole-command sender's fixed bound.
MAX_MATCHING_COMMANDS = 2
_NAMESPACE = re.compile(r"(?:instagram_login|legacy_page):[A-Za-z0-9_.-]{1,96}\Z")
_RECIPIENT = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_INTEGER = re.compile(r"[1-9][0-9]{0,18}\Z")


@dataclass(frozen=True)
class LegacyHumanReceipt:
    accepted: bool = False
    classification: str = "unproven"
    reason: str = "legacy_human_receipt_missing"
    command_id: int = 0
    manager_message_id: int = 0
    operation_id: str = ""
    retryable: bool = False


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    return int(text) if _INTEGER.fullmatch(text) and int(text) <= 2**63 - 1 else None


def _mid(value):
    return (isinstance(value, str) and 0 < len(value) <= 255
        and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value))


def _scope(namespace, recipient):
    return (isinstance(namespace, str) and _NAMESPACE.fullmatch(namespace)
        and isinstance(recipient, str) and _RECIPIENT.fullmatch(recipient))


def _receipt_match(mid):
    # Exact bounded JSON indexes work on both SQLite and MariaDB. JSON contains
    # is unsupported on SQLite and an unbounded namespace scan is not proof.
    query = Q()
    for index in range(LEGACY_MAX_RECEIPTS):
        query |= Q(**{f"provider_message_ids__{index}": mid})
    return query


def _legacy_commands(namespace):
    from management.ig_bot_models import HumanReplyCommand
    from management.ig_human_reply_models import HumanReplyPart

    return HumanReplyCommand.objects.filter(provider_namespace=namespace).annotate(
        _has_human_parts=Exists(HumanReplyPart.objects.filter(command_id=OuterRef("pk")))
    ).filter(_has_human_parts=False).exclude(operation_context__has_key="human_delivery")


def uses_legacy_human_receipt_scope(namespace, recipient=None, *, mid=""):
    """Keep known legacy identities in exact attribution after flag rollback.

    A foreign recipient's same MID also enters the guard. A database failure
    keeps the finite guarded lane active instead of granting a cache shortcut.
    """
    if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
        return False
    if recipient is not None and (not isinstance(recipient, str) or not _RECIPIENT.fullmatch(recipient)):
        return False
    if mid and not _mid(mid):
        return False
    try:
        query = _legacy_commands(namespace)
        if mid and query.filter(_receipt_match(mid)).exists():
            return True
        return (query.filter(recipient_igsid=recipient) if recipient is not None else query).exists()
    except DatabaseError:
        return True


def legacy_human_wait_reason(*, client_id, namespace, recipient):
    """An uncheckpointed old call can defer attribution, never prove a MID."""
    client_id = _id(client_id)
    if not client_id or not _scope(namespace, recipient):
        return "legacy_human_scope_invalid"
    try:
        query = _legacy_commands(namespace).filter(client_id=client_id, recipient_igsid=recipient)
        if query.filter(state="unknown").exists():
            return "legacy_human_provider_result_unknown"
        if query.filter(state="provider_started").exists():
            return "legacy_human_waiting_receipt"
        return ""
    except DatabaseError:
        return "legacy_human_receipt_unavailable"


def legacy_projection_matches(command, message, *, client_id, namespace, recipient, mid):
    """Strict already-retained graph parity; echo content is never an input.

    Parts/plan exclusion is supplied by the finder, rather than inferred from
    blank legacy fields. Text/hash parity corroborates the transcript binding;
    the actual receipt list and exact scope are the delivery evidence.
    """
    client_id = _id(client_id)
    if not client_id or not _scope(namespace, recipient) or not _mid(mid) or command is None or message is None:
        return False
    try:
        operation = uuid.UUID(str(command.operation_id))
        if not operation.int or str(operation) != str(command.operation_id):
            return False
        ids = command.provider_message_ids
        if (not isinstance(ids, list) or not 1 <= len(ids) <= LEGACY_MAX_RECEIPTS
            or not all(_mid(value) for value in ids) or len(set(ids)) != len(ids) or mid not in ids):
            return False
        context = command.operation_context
        if not isinstance(context, dict) or "human_delivery" in context:
            return False
        if (not isinstance(command.text, str) or not command.text or len(command.text) > 4000
            or command.draft_hash != hashlib.sha256(command.text.encode("utf-8")).hexdigest()):
            return False
        return bool(command.client_id == client_id and command.recipient_igsid == recipient
            and command.provider_namespace == namespace and command.purpose == "human_reply"
            and command.state == "sent" and command.provider_started_at is not None
            and command.terminal_at is not None and not command.failure_code
            and command.reply_message_id == message.pk and message.client_id == client_id
            and message.sender_id == recipient and message.provider_namespace in {"", namespace}
            and message.role == "manager" and message.source == "human_reply"
            and message.status == "done" and message.send_state == "sent"
            and message.send_idempotency_key == f"human:{operation}"
            and message.text == command.text and message.provider_message_id == ids[0]
            and message.delivery_provider_message_ids == ids and not message.delivery_failure_boundary)
    except (AttributeError, TypeError, ValueError, UnicodeError):
        return False


def find_legacy_human_receipt(*, client_id, namespace, recipient, mid, command_id=None, manager_message_id=None):
    """Return content-free evidence only for one exact, unambiguous old send.

    Optional replay IDs narrow acceptance, never the collision query. Old
    permission epochs, source resets and actor deletion do not revoke an actual
    historical receipt. Hidden/erased clients never export a usable proof.
    """
    client_id = _id(client_id)
    expected_command = _id(command_id) if command_id is not None else None
    expected_message = _id(manager_message_id) if manager_message_id is not None else None
    if (not client_id or not _scope(namespace, recipient) or not _mid(mid)
        or (command_id is not None and not expected_command) or (manager_message_id is not None and not expected_message)):
        return LegacyHumanReceipt(classification="blocked", reason="legacy_human_scope_invalid")
    from management.models import IgClient, IgRevisionDeliveryEffect
    from management.ig_human_reply_models import HumanReplyPart
    try:
        if not IgClient.objects.filter(pk=client_id, igsid=recipient,
                hidden_at__isnull=True, privacy_erasure_started_at__isnull=True).exists():
            return LegacyHumanReceipt(classification="blocked", reason="legacy_human_client_unavailable")
        rows = list(_legacy_commands(namespace).filter(_receipt_match(mid))
            .select_related("reply_message").order_by("pk")[:MAX_MATCHING_COMMANDS + 1])
        if not rows:
            return LegacyHumanReceipt()
        if len(rows) != 1:
            return LegacyHumanReceipt(classification="ambiguous", reason="legacy_human_receipt_conflict", retryable=True)
        command = rows[0]
        if command.client_id != client_id or command.recipient_igsid != recipient:
            return LegacyHumanReceipt(classification="ambiguous", reason="legacy_human_receipt_foreign", retryable=True)
        message = command.reply_message
        if (not legacy_projection_matches(command, message, client_id=client_id,
                namespace=namespace, recipient=recipient, mid=mid)
            or (expected_command is not None and command.pk != expected_command)
            or (expected_message is not None and (message is None or message.pk != expected_message))):
            return LegacyHumanReceipt(classification="ambiguous", reason="legacy_human_projection_mismatch", retryable=True)
        if (HumanReplyPart.objects.filter(provider_namespace=namespace, provider_message_id=mid).exists()
            or IgRevisionDeliveryEffect.objects.filter(provider_namespace=namespace, provider_message_id=mid).exists()):
            return LegacyHumanReceipt(classification="ambiguous", reason="legacy_human_cross_lane_conflict", retryable=True)
        # A fresh final owner fence prevents a fence committed between reads
        # from exporting a usable proof. Do not inner-join candidate clients:
        # even a dangling foreign command must remain an identity collision.
        if not IgClient.objects.filter(pk=client_id, igsid=recipient,
                hidden_at__isnull=True, privacy_erasure_started_at__isnull=True).exists():
            return LegacyHumanReceipt(classification="blocked", reason="legacy_human_client_unavailable")
        return LegacyHumanReceipt(True, "own", "exact_legacy_human_provider_receipt",
            command.pk, message.pk, str(command.operation_id))
    except DatabaseError:
        return LegacyHumanReceipt(classification="blocked", reason="legacy_human_receipt_unavailable", retryable=True)
