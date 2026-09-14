"""Bounded ownership transfer for a new inbound before provider execution.

The caller holds the client and active predecessor locks. This module neither
answers messages nor closes legacy turns or manager cases. Provider-generation
budget transfer and historical debt reconciliation are separate consumers.
"""
from dataclasses import dataclass

from management.models import (
    GeminiRequest, IgCustomerTurnRevision, IgFollowUpTask, IgTurnRevisionSource,
)


TRANSFER_IN = "source_transfer_in"
TRANSFER_OUT = "source_transfer_out"
VERSION = "revision-source-transfer-v1"


def source_transfer_owned_q():
    """Sticky ownership from the only writer's atomic, append-only receipt pair."""
    from django.db.models import Q

    return Q(
        action_receipts__source_transfer_in__version=VERSION,
        action_receipts__source_transfer_in__successor_revision_id__isnull=False,
        action_receipts__source_transfer_in__predecessor_revision_id__isnull=False,
    ) | Q(
        action_receipts__source_transfer_out__version=VERSION,
        action_receipts__source_transfer_out__successor_revision_id__isnull=False,
        action_receipts__source_transfer_out__predecessor_revision_id__isnull=False,
    )


@dataclass(frozen=True)
class SourceTransferPlan:
    payloads: tuple = ()
    root_revision_id: int = 0
    reason: str = ""
    source_refs: tuple = ()

    @property
    def ready(self):
        return bool(self.payloads) and not self.reason


def validate_source_transfer(revision):
    """Read back the exact paired handoff, without claiming it was fulfilled."""
    from management.services.ig_turn_revisions import _digest

    receipt = (revision.action_receipts or {}).get(TRANSFER_IN) or {}
    if not isinstance(receipt, dict) or receipt.get("version") != VERSION:
        return False
    if receipt.get("digest") != _digest({key: value for key, value in receipt.items() if key != "digest"}):
        return False
    previous = IgCustomerTurnRevision.objects.filter(pk=receipt.get("predecessor_revision_id"), client_id=revision.client_id).first()
    if (previous is None or revision.parent_id != previous.pk
        or receipt.get("client_id") != revision.client_id
        or receipt.get("successor_revision_id") != revision.pk
        or (previous.action_receipts or {}).get(TRANSFER_OUT) != receipt
        or receipt.get("outcome") != "transferred"
        or receipt.get("permission_epoch") != revision.permission_epoch
        or receipt.get("permission_epoch") != previous.permission_epoch):
        return False
    refs = receipt.get("source_refs") or []
    previous_refs = [{"message_id": row.message_id, "source_digest": row.source_digest,
                      "predecessor_source_id": row.pk}
                     for row in previous.sources.order_by("ordinal", "id")]
    current_rows = list(revision.sources.order_by("ordinal", "id"))
    if (not refs or refs != previous_refs
        or receipt.get("source_message_ids") != [row["message_id"] for row in refs]
        or receipt.get("successor_source_message_ids") != [row.message_id for row in current_rows]
        or [(row.message_id, row.source_digest) for row in current_rows[:len(refs)]]
        != [(row["message_id"], row["source_digest"]) for row in refs]):
        return False
    root = IgCustomerTurnRevision.objects.filter(pk=receipt.get("root_revision_id"), client_id=revision.client_id).first()
    return bool(root and root.origin == "inbound" and all(
        receipt.get(key) == getattr(revision, attribute).isoformat()
        == getattr(previous, attribute).isoformat() == getattr(root, attribute).isoformat()
        for key, attribute in (("first_unanswered_at", "quiet_started_at"),
                               ("quiet_cap_at", "quiet_cap_at"),
                               ("overall_deadline", "overall_deadline"))
    ))


def plan_source_transfer(head, client, fresh_messages, *, now):
    from management.services.ig_customer_turns import MAX_TURN_WAIT, bypasses_debounce
    from management.services.ig_turn_revisions import (
        MAX_SOURCES, _copied_source_payloads, _digest, _source_payload,
    )

    denied = lambda reason: SourceTransferPlan(reason=reason)
    if head is None or not fresh_messages:
        return denied("predecessor_or_new_source_missing")
    from management.services.ig_revision_rollout import revision_execution_rollout

    owned = bool(head.media_prepare_deadline or head.sealed_at or head.snapshot_digest
                 or head.generation_proposal_digest or head.delivery_effects.exists()
                 or IgCustomerTurnRevision.objects.filter(pk=head.pk).filter(source_transfer_owned_q()).exists())
    rollout = revision_execution_rollout(now=now)
    if not owned and not (rollout.enabled and head.created_at >= rollout.cutover_at):
        return denied("legacy_shadow_only")
    if (head.active_slot != 1 or head.origin != "inbound"
        or head.state not in {"collecting", "preparing", "sealed", "claimed"}
        or head.recovery_state or (head.action_receipts or {}).get("response_debt")):
        return denied("predecessor_not_ordinary")
    opted_out = client.opted_out_at and (not client.opted_in_at or client.opted_out_at > client.opted_in_at)
    if (client.hidden_at or client.bot_paused or client.manager_takeover or opted_out
        or client.is_blocked or client.privacy_erasure_started_at
        or client.reply_permission_epoch != head.permission_epoch
        or any(bypasses_debounce(row) for row in fresh_messages)):
        return denied("permission_or_explicit_boundary")
    # Canonical CLAIMED is transferable here; a separate legacy worker claim
    # cannot be revoked by a revision receipt and must retain its ownership.
    if head.turn.terminal_reason or head.turn.claim_state != "open":
        return denied("predecessor_turn_not_open")
    if not head.quiet_started_at <= now <= head.quiet_started_at + MAX_TURN_WAIT or now >= head.overall_deadline:
        return denied("burst_horizon_elapsed")
    if head.delivery_effects.exists():
        return denied("delivery_already_planned")
    receipts = head.action_receipts or {}
    # Even definite provider failures need the shared generation budget adapter;
    # a changed snapshot must not silently mint a second HTTP/repair budget.
    if (head.generation_proposal_digest or "provider_execution_manifest" in receipts
        or "provider_execution_reference" in receipts
        or GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{head.pk}").exists()):
        return denied("provider_generation_started")
    if set(receipts) - {TRANSFER_IN, "input_decision", "generation_admission"}:
        return denied("existing_action_ownership")
    if receipts.get("input_decision") and receipts["input_decision"].get("origin") != "generate":
        return denied("existing_input_disposition")
    if IgFollowUpTask.objects.filter(client=client, event_key=f"ig-revision-debt:{head.pk}").exists():
        return denied("existing_manual_debt")
    if TRANSFER_IN in receipts and not validate_source_transfer(head):
        return denied("predecessor_transfer_invalid")
    rows = list(head.sources.select_for_update().select_related("message").order_by("ordinal", "id"))
    if not rows or len(rows) != head.source_count or len(rows) + len(fresh_messages) > MAX_SOURCES:
        return denied("source_count_limit")
    payloads = _copied_source_payloads(rows)
    for row, payload in zip(rows, payloads, strict=True):
        message = row.message
        if (row.role != "user" or message.role != "user" or message.client_id != client.pk
            or message.source != "webhook" or message.sender_id != client.igsid
            or message.status != "pending" or message.send_state
            or message.quick_reply_payload
            or row.source_digest != _digest({key: value for key, value in payload.items() if key != "source_digest"})
            or _source_payload(message, previous=row, ordinal=row.ordinal)["source_digest"] != row.source_digest):
            return denied("predecessor_source_changed_or_terminal")
    namespaces = {row.source_namespace for row in rows} | {str(row.provider_namespace or "") for row in fresh_messages}
    same_legacy_turn = all(row.turn_membership.turn_id == head.turn_id for row in fresh_messages)
    if len(namespaces) != 1 or (not next(iter(namespaces), "") and not same_legacy_turn):
        return denied("source_namespace_unproven")
    refs = tuple({"message_id": row.message_id, "source_digest": row.source_digest,
                  "predecessor_source_id": row.pk} for row in rows)
    return SourceTransferPlan(tuple(payloads), (receipts.get(TRANSFER_IN) or {}).get("root_revision_id", head.pk), source_refs=refs)


def record_source_transfer(previous, successor, plan):
    """Pair receipts before releasing the old claim; enclosing transaction owns CAS."""
    from management.services.ig_turn_revisions import _digest

    if not plan.ready or previous.active_slot is not None or successor.parent_id != previous.pk:
        raise ValueError("source_transfer_owner_changed")
    receipt = {"version": VERSION, "outcome": "transferred", "client_id": previous.client_id,
        "root_revision_id": plan.root_revision_id, "predecessor_revision_id": previous.pk,
        "successor_revision_id": successor.pk, "permission_epoch": previous.permission_epoch,
        "source_message_ids": [row["message_id"] for row in plan.source_refs],
        "source_refs": list(plan.source_refs),
        "successor_source_message_ids": list(successor.sources.order_by("ordinal", "id").values_list("message_id", flat=True)),
        "first_unanswered_at": previous.quiet_started_at.isoformat(),
        "quiet_cap_at": previous.quiet_cap_at.isoformat(),
        "overall_deadline": previous.overall_deadline.isoformat()}
    receipt["digest"] = _digest(receipt)
    if TRANSFER_OUT in (previous.action_receipts or {}) or TRANSFER_IN in (successor.action_receipts or {}):
        raise ValueError("source_transfer_already_recorded")
    previous.action_receipts = {**(previous.action_receipts or {}), TRANSFER_OUT: receipt}
    successor.action_receipts = {**(successor.action_receipts or {}), TRANSFER_IN: receipt}
    successor.save(update_fields=["action_receipts", "updated_at"])
    previous.state = previous.State.SUPERSEDED
    previous.claim_token = ""
    previous.claimed_at = None
    previous.lease_until = None
    previous.save(update_fields=["action_receipts", "state", "claim_token", "claimed_at", "lease_until", "updated_at"])
    if not validate_source_transfer(successor):
        raise ValueError("source_transfer_receipt_invalid")
