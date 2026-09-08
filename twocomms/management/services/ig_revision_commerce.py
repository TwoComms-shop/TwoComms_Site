"""Original-source commerce reductions under the sealed revision's authority."""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy

from django.db import connection, transaction
from django.utils import timezone

from management.models import (
    IgClient, IgCommerceTurnDecision, IgCustomerTurnRevision,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_revision_authority import CLAIM_PUBLIC_POLICY_INPUTS, build_revision_authority_bindings, check_fact_bindings, check_offer_bindings
from management.services.ig_revision_outbox import PublicationBinding, _digest, pre_winner_readiness


RECEIPT_KEY = "commerce_reduction"


@dataclass(frozen=True)
class RevisionCommerceResult:
    ready: bool = False
    reason: str = ""
    decisions: tuple[dict, ...] = ()
    replayed: bool = False


class _Blocked(Exception):
    pass


def _apply_source_language(client, source, *, origin):
    from management.services.bot_sales_classifier import (
        _record_language_request, _sticky_language,
        detect_language, detect_language_request,
    )

    requested = detect_language_request(source.text)
    if origin != "inbound" and not requested:
        return
    # apply_turn may have projected selection context through another instance.
    # Preserve those fields while changing only the existing language preference.
    client.refresh_from_db(fields=["language", "sales_context"])
    if requested:
        language = requested
        _record_language_request(client, requested, message=source)
    else:
        language = _sticky_language(client, detect_language(source.text))
    client.language = language
    client.save(update_fields=["language", "sales_context", "updated_at"])


def reduce_revision_commerce(revision_id, token, *, settings_id, settings_permission_epoch, publication):
    if connection.in_atomic_block:
        return RevisionCommerceResult(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionCommerceResult(reason="revision_missing")
    from management.services.ig_commerce_state import apply_turn
    from management.services.ig_commerce_turns import understand_turn

    try:
        with transaction.atomic():
            settings_row = InstagramBotSettings.objects.select_for_update().filter(pk=settings_id).first()
            client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
            revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
            if settings_row is None or client is None or revision is None:
                raise _Blocked("commerce_identity_missing")
            if not revision.snapshot_digest or _digest(revision.bundle_snapshot) != revision.snapshot_digest:
                raise _Blocked("revision_snapshot_invalid")
            authority = build_revision_authority_bindings(client, claims=(CLAIM_PUBLIC_POLICY_INPUTS,), settings_obj=settings_row)
            if not authority.ready:
                raise _Blocked(authority.reasons[0])
            ready = pre_winner_readiness(
                revision.pk, token, settings_id=settings_id, settings_permission_epoch=settings_permission_epoch,
                publication=publication, fact_bindings=authority.fact_bindings,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings,
            )
            if not ready.ready:
                raise _Blocked(ready.reasons[0])
            previous = (revision.action_receipts or {}).get(RECEIPT_KEY)
            if previous:
                if previous.get("snapshot_digest") != revision.snapshot_digest:
                    raise _Blocked("commerce_receipt_changed")
                return RevisionCommerceResult(True, "already_reduced", tuple(previous["decisions"]), True)
            decisions = []
            for snapshot in revision.bundle_snapshot.get("sources") or []:
                # Native button mutation has a separate global source ledger.
                if snapshot.get("quick_reply_payload"):
                    continue
                source = InstagramBotMessage.objects.select_for_update().filter(pk=snapshot["message_id"], client=client, sender_id=client.igsid, role="user", source="webhook").first()
                if source is None or any((
                    source.text != snapshot.get("text"),
                    source.quick_reply_payload != snapshot.get("quick_reply_payload"),
                    source.reply_to_provider_message_id != snapshot.get("reply_to_provider_message_id"),
                    source.provider_namespace != snapshot.get("source_namespace"),
                    str(source.mid or "") != str(snapshot.get("provider_message_id") or ""),
                    (source.provider_created_at.isoformat() if source.provider_created_at else "") != snapshot.get("provider_created_at"),
                )):
                    raise _Blocked("commerce_source_changed")
                existing = IgCommerceTurnDecision.objects.filter(source_message_id=source.pk).first()
                if existing is not None and existing.delivery_required:
                    raise _Blocked("legacy_commerce_delivery_owned")
                request = understand_turn(snapshot.get("text") or "", media_evidence=deepcopy(snapshot.get("media_parts") or []))
                # The source fact is idempotent. Its old delivery fields never
                # become a second send owner, and no source row is cloned.
                decision = existing or apply_turn(client, source, request, reply_payload={})
                if existing is None:
                    _apply_source_language(client, source, origin=revision.origin)
                action = decision.transition.action if decision.transition_id else str((decision.result_payload or {}).get("reason") or "observed")
                decisions.append({
                    "source_message_id": source.pk, "source_digest": snapshot["source_digest"],
                    "decision_id": decision.pk, "transition_id": decision.transition_id,
                    "action": action, "accepted": bool(decision.accepted), "is_stale": bool(decision.is_stale),
                })
            revision.action_receipts = {**(revision.action_receipts or {}), RECEIPT_KEY: {
                "version": "revision-commerce-v1", "snapshot_digest": revision.snapshot_digest,
                "decisions": decisions, "recorded_at": timezone.now().isoformat(),
            }}
            revision.save(update_fields=["action_receipts", "updated_at"])
            return RevisionCommerceResult(True, "reduced", tuple(decisions))
    except _Blocked as exc:
        return RevisionCommerceResult(reason=str(exc))


def synchronize_selected_session(client):
    """Called inside the protected selection transaction, never a source replay."""
    if not connection.in_atomic_block:
        raise ValueError("selection session synchronization requires transaction")
    from management.models import IgCommerceSelectionSession
    from management.services.ig_commerce_projection import bootstrap_session_from_legacy, _legacy_line

    session = IgCommerceSelectionSession.objects.select_for_update().filter(client=client, open_slot=1).order_by("-generation").first()
    if session is None:
        session = bootstrap_session_from_legacy(client)
    before = session.snapshot()
    lines = deepcopy(session.lines or [])
    index = int(session.active_index or 0)
    if not lines:
        lines, index = [{}], 0
    if not 0 <= index < len(lines):
        raise ValueError("selection session active index is invalid")
    selected = _legacy_line(client, session.generation)
    selected["line_id"] = str(lines[index].get("line_id") or selected.get("line_id") or f"line:{index}")
    lines[index] = selected
    session.lines = lines
    session.active_index = index
    session.candidate_product_ids = []
    session.candidate_digest = ""
    session.candidate_prompt_provider_ids = []
    session.candidate_generation = int(session.candidate_generation or 0) + 1
    session.rejected_selection = {}
    session.rejected_reason = ""
    session.pending_field = ""
    session.pending_clarification = ""
    session.revision = int(session.revision or 0) + 1
    session.save(update_fields=["lines", "active_index", "candidate_product_ids", "candidate_digest", "candidate_prompt_provider_ids", "candidate_generation", "rejected_selection", "rejected_reason", "pending_field", "pending_clarification", "revision", "updated_at"])
    return {"session_id": session.pk, "before": before, "after": session.snapshot()}


def selected_session_receipt_current(client, receipt):
    from management.models import IgCommerceSelectionSession

    session = IgCommerceSelectionSession.objects.filter(pk=receipt.get("session_id"), client=client, open_slot=1).first()
    return bool(session is not None and session.snapshot() == receipt.get("after"))
