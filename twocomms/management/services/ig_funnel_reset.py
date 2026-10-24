"""Transactional operator reset for an Instagram client's mutable CRM state."""
from __future__ import annotations

import uuid
import hashlib
import json
from datetime import datetime
from contextlib import nullcontext
from decimal import Decimal

from django.db import OperationalError, connection, transaction
from django.utils import timezone

from management.models import IgClient, InstagramBotMessage
from management.ig_bot_models import (
    IgCommercialEpisode,
    IgConversationAnalysisJob,
    IgFollowCtaDecision,
    IgFunnelResetAudit,
    IgCustomerTurn,
    IgCustomerTurnRevision,
    IgRevisionDeliveryEffect,
)
from management.services.bot_payment_truth import client_has_confirmed_purchase
from management.services import bot_followups
from management.services.ig_reply_boundary import pause_reply_boundary


def latest_reset_after_message_id(client_or_id) -> int:
    client_id = getattr(client_or_id, "pk", client_or_id)
    if not client_id:
        return 0
    # Э8.5: граница сброса физически не может измениться внутри одного хода, а
    # `current_message_floor` вызывается из истории, языка, памяти и объекций.
    # Замер показал 4 одинаковых запроса к `management_igfunnelresetaudit` на
    # сборку одного промпта.
    from management.services.ig_turn_snapshot import turn_or_prompt_cached

    return turn_or_prompt_cached(
        f"funnel_reset_floor:{client_id}",
        lambda: _query_latest_reset_after_message_id(client_id),
    )


def _query_latest_reset_after_message_id(client_id) -> int:
    return int(
        IgFunnelResetAudit.objects.filter(client_id=client_id)
        .order_by("-id")
        .values_list("reset_after_message_id", flat=True)
        .first()
        or 0
    )


def current_message_floor(client, *, inclusive_episode: bool = True) -> int:
    """Return the first message id visible to current-episode evidence queries."""
    if not hasattr(client, "current_commercial_episode_id"):
        client = (
            IgClient.objects.select_related("current_commercial_episode")
            .filter(pk=getattr(client, "pk", client))
            .first()
        )
        if client is None:
            return latest_reset_after_message_id(getattr(client, "pk", None)) + 1
    annotated_reset = getattr(
        client,
        "materiality_reset_after_message_id",
        None,
    )
    reset_floor = (
        int(annotated_reset or 0)
        if annotated_reset is not None
        else latest_reset_after_message_id(client)
    )
    episode = getattr(client, "current_commercial_episode", None)
    if episode is None and getattr(client, "current_commercial_episode_id", None):
        episode = IgCommercialEpisode.objects.filter(
            pk=client.current_commercial_episode_id,
        ).only("opened_watermark_message_id").first()
    episode_floor = int(getattr(episode, "opened_watermark_message_id", 0) or 0)
    if not inclusive_episode and episode_floor:
        episode_floor += 1
    # Reset is exclusive (the boundary message belongs to the archived run),
    # while an episode watermark is inclusive by convention.
    return max(reset_floor + 1, episode_floor)


def current_message_filter(client, *, field: str = "id") -> dict:
    """Build a strict post-reset filter for current-episode messages."""
    return {f"{field}__gte": current_message_floor(client)}


def analysis_snapshot_is_after_reset(client, snapshot) -> bool:
    annotated_reset = getattr(client, "materiality_reset_after_message_id", None)
    reset_after = int(annotated_reset or 0) if annotated_reset is not None else latest_reset_after_message_id(client)
    watermark = int(snapshot.last_analyzed_message_id or 0)
    if watermark > reset_after:
        return True
    if reset_after:
        return False
    # A full reset of an empty transcript still invalidates a legacy analysis
    # without a source watermark. Preserve legacy no-reset behavior.
    return not IgFunnelResetAudit.objects.filter(
        client_id=getattr(client, "pk", client),
        previous_state__reset_mode="fresh_conversation",
    ).exists()


def _preserved_sales_context(client) -> dict:
    try:
        from management.services.bot_memory import preserved_profile

        return preserved_profile(client)
    except Exception:
        return {}


def pre_reset_source_is_historical(
    client, *, provider_created_at=None, provider_namespace: str = "",
    inbox_event_id: int | None = None, mid: str = "", _current_read: bool = False,
) -> bool:
    """Fence authenticated customer ingress without creating a new source.

    Callers validate the sender/owner and supply the provider's event clock,
    never a server-time substitute. Unknown clocks need the accepted inbox
    highwater or an existing owned source identity before the reset. This
    deliberately does not mutate consent,
    inbound clocks, classifications, memory, or the retained transcript.
    """
    audits = IgFunnelResetAudit.objects.filter(
        client_id=client.pk,
        previous_state__reset_mode="fresh_conversation",
    ).order_by("-id")
    if _current_read:
        # The caller holds the commercial client lock. A locking read sees a
        # reset committed while an outer inbox transaction had an older read
        # view; a normal consistent read could miss that boundary.
        with transaction.atomic():
            audit = audits.select_for_update().values_list("previous_state", flat=True).first()
    else:
        audit = audits.values_list("previous_state", flat=True).first()
    boundary = (audit or {}).get("reset_boundary") or {}
    if boundary.get("schema") != "fresh-conversation-reset.v1":
        return False
    namespace = str(boundary.get("provider_namespace") or "")
    if not namespace or namespace != str(provider_namespace or ""):
        return False
    if inbox_event_id and int(inbox_event_id) <= int(boundary.get("inbox_highwater") or 0):
        return True
    if mid and InstagramBotMessage.objects.filter(
        client_id=client.pk, sender_id=client.igsid, provider_namespace=namespace,
        mid=mid, pk__lte=int(boundary.get("message_id") or 0),
    ).exists():
        return True
    if not isinstance(provider_created_at, datetime) or timezone.is_naive(provider_created_at):
        return False
    try:
        cutoff = datetime.fromisoformat(boundary["reset_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return timezone.is_aware(cutoff) and provider_created_at <= cutoff


def _fresh_boundary(client, *, now, message_id):
    from management.models import InstagramBotSettings, IgWebhookInboxEvent
    from management.services.instagram_bot import ingress_provider_namespace

    settings_obj = InstagramBotSettings.objects.filter(pk=1).first()
    namespace = ingress_provider_namespace(settings_obj) if settings_obj else ""
    highwater = IgWebhookInboxEvent.objects.filter(
        namespace=namespace, customer_igsid=client.igsid,
        decision=IgWebhookInboxEvent.Decision.ACCEPTED,
    ).order_by("-id").values_list("id", flat=True).first() or 0
    # JSON v1 is durable audit data, not a second authority source. Its owner
    # namespace and highwater identify only this client's accepted ingress.
    return {"schema": "fresh-conversation-reset.v1", "reset_at": now.isoformat(),
            "message_id": int(message_id), "provider_namespace": namespace,
            "inbox_highwater": int(highwater)}


def handle_historical_reset_ingress(
    client, *, text: str, mid: str, provider_created_at=None,
    provider_namespace: str = "", inbox_event_id: int | None = None,
    payload_digest: str = "", _reply_boundary_held: bool = False,
    _current_boundary_read: bool = False,
) -> bool:
    """Archive old context, retaining the higher-priority withdrawal of consent.

    Sender and namespace ownership must already be authenticated by ingress.
    No source row, conversation clock, classifier, memory or reply is created.
    The existing DB console stores only a bounded hashed consent receipt.
    """
    if not pre_reset_source_is_historical(client, provider_created_at=provider_created_at,
                                         provider_namespace=provider_namespace, inbox_event_id=inbox_event_id, mid=mid,
                                         _current_read=_current_boundary_read):
        return False
    from management.services.bot_sales_classifier import is_explicit_opt_out

    if not is_explicit_opt_out(text):
        return True
    from management.models import InstagramBotLog
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    from management.services.ig_permission_transitions import cancel_client_unstarted_automation

    send_boundary = nullcontext() if _reply_boundary_held else pause_reply_boundary()
    with commercial_episode_client_lock(client.pk), send_boundary, transaction.atomic():
        locked = IgClient.objects.select_for_update().get(pk=client.pk)
        if not pre_reset_source_is_historical(locked, provider_created_at=provider_created_at,
                                             provider_namespace=provider_namespace, inbox_event_id=inbox_event_id, mid=mid,
                                             _current_read=_current_boundary_read):
            return False
        now = timezone.now()
        provider_clock = provider_created_at if (
            isinstance(provider_created_at, datetime) and timezone.is_aware(provider_created_at)
        ) else None
        if provider_clock is None and mid:
            provider_clock = InstagramBotMessage.objects.filter(
                client_id=locked.pk, sender_id=locked.igsid, mid=mid,
                provider_namespace=provider_namespace,
            ).values_list("provider_created_at", flat=True).first()
        superseded = bool(provider_clock and locked.opted_in_at and locked.opted_in_at >= provider_clock)
        outcome = "superseded_by_newer_opt_in" if superseded else "opt_out_retained"
        receipt = {
            "schema": "fresh-reset-consent.v1", "client_id": locked.pk,
            "namespace_sha256": hashlib.sha256(provider_namespace.encode()).hexdigest(),
            "mid_sha256": hashlib.sha256(str(mid or "").encode()).hexdigest(),
            "text_sha256": hashlib.sha256(str(text or "").encode()).hexdigest(),
            "inbox_event_id": inbox_event_id,
            "payload_digest": str(payload_digest or "")[:64],
            "provider_created_at": provider_clock.isoformat() if provider_clock else None,
            "outcome": outcome,
        }
        InstagramBotLog.objects.get_or_create(
            level=InstagramBotLog.Level.INFO, event="fresh_reset_historical_opt_out",
            detail=json.dumps(receipt, sort_keys=True, separators=(",", ":")),
        )
        if not superseded:
            # Keep the newest existing withdrawal clock. An accepted receipt
            # with no provider clock conservatively withdraws consent now.
            opted_out_at = provider_clock or now
            if locked.opted_out_at and locked.opted_out_at > opted_out_at:
                opted_out_at = locked.opted_out_at
            if (locked.opted_out_at != opted_out_at or not locked.bot_paused
                or locked.paused_reason != "opt_out"):
                locked.opted_out_at = opted_out_at
                locked.bot_paused = True
                locked.paused_reason = "opt_out"
                locked.paused_at = locked.paused_at or now
                locked.reply_permission_epoch = int(locked.reply_permission_epoch or 0) + 1
                locked.save(update_fields=["opted_out_at", "bot_paused", "paused_reason",
                    "paused_at", "reply_permission_epoch", "updated_at"])
            cancel_client_unstarted_automation(locked, reason="historical_reset_opt_out", now=now, nowait=False)
            _retire_fresh_work(locked, now=now)
        return True


def _archive_fresh_reply_attention(client, *, boundary, actor, now):
    """Retire proved old conversation alerts without acknowledging a reply.

    Financial/service work and uncertain delivery retain their independent
    owner. A confirmed technical holding is physical history, not a useful
    answer, but it need not keep the archived conversation in the current queue.
    """
    from management.models import IgPaymentConfirmationReview, IgPostSaleCase
    from management.services.ig_response_debt import DEBT_KIND, REASON_LABELS, unresolved_reply_debts
    from management.services.ig_revision_holding import confirmed_holding_effect_ids, RECEIPT_KEY, DELIVERY_KEY
    from management.services.ig_revision_outbox import _digest, revision_sources_current
    from management.services.ig_revision_execution import _whole_sent_proof

    archived = []
    tasks = _reset_financial_rows(unresolved_reply_debts().filter(
        client=client, status="skipped", deal__isnull=True,
        claim_token="", provider_message_id="", sent_message__isnull=True,
        sent_at__isnull=True,
    ).order_by("id"))
    for task in tasks:
        payload = task.event_payload if isinstance(task.event_payload, dict) else {}
        context = task.manager_context if isinstance(task.manager_context, dict) else {}
        revision_id = payload.get("revision_id")
        ids = payload.get("source_message_ids")
        effect_ids = payload.get("effect_ids")
        if (type(revision_id) is not int or task.event_key != f"ig-revision-debt:{revision_id}"
            or context.get("case_kind") != DEBT_KIND or context.get("revision_id") != revision_id
            or context.get("owner") != "manager" or context.get("reason") != payload.get("reason")
            or payload.get("reason") not in {*REASON_LABELS, "technical_holding_sent"}
            or context.get("operator_review") or context.get("reply_resolution")
            or not isinstance(ids, list) or not ids
            or any(type(value) is not int or not 0 < value <= boundary for value in ids)
            or len(set(ids)) != len(ids) or not isinstance(effect_ids, list)
            or any(type(value) is not int or value <= 0 for value in effect_ids)
            or len(set(effect_ids)) != len(effect_ids)):
            continue
        revision = _reset_financial_rows(IgCustomerTurnRevision.objects.filter(
            pk=revision_id, client=client,
        )).first()
        if (revision is None or not isinstance(revision.bundle_snapshot, dict)
            or not isinstance(revision.action_receipts, dict)):
            continue
        sources = list(revision.sources.select_related("message__client").order_by("ordinal", "id"))
        if ([row.message_id for row in sources] != ids
            or any(row.role != "user" or row.message.role != "user" for row in sources)
            or _digest(revision.bundle_snapshot) != revision.snapshot_digest
            or not revision_sources_current(revision, source_rows=sources)):
            continue
        coverage = (revision.action_receipts or {}).get("response_coverage") or {}
        if (not isinstance(coverage, dict)
            or any(not isinstance(coverage.get(key, []), list) for key in ("covered", "remaining"))
            or coverage.get("payment_verification")
            or any(":payment" in str(item) for key in ("covered", "remaining")
                   for item in coverage.get(key, ()))
            or IgPaymentConfirmationReview.objects.filter(client=client, watermark_message_id__in=ids).exists()
            or IgPostSaleCase.objects.filter(client=client, source_message_id__in=ids).exists()):
            continue
        effects = list(_reset_financial_rows(revision.delivery_effects.order_by("order_index", "id")))
        disposition = context.get("disposition")
        actual_effect_ids = tuple(row.pk for row in effects)
        admission = revision.action_receipts.get(RECEIPT_KEY) or {}
        delivery = revision.action_receipts.get(DELIVERY_KEY) or {}
        holding_ids = (confirmed_holding_effect_ids(revision)
                       if isinstance(admission, dict) and isinstance(delivery, dict)
                       and effects and all(row.actor == "bot" and row.purpose == "technical_holding"
                                          for row in effects) else ())
        # Current holding admission records its manager case before dispatch;
        # the immutable task manifest is then empty. Its native admission must
        # identify this exact task before the later holding can cover that gap.
        pre_dispatch_holding = bool(
            not effect_ids and disposition == "manager_reply"
            and payload.get("reason") == "provider_candidates_exhausted"
            and isinstance(admission, dict) and admission.get("task_id") == task.pk
            and holding_ids and holding_ids == actual_effect_ids)
        if actual_effect_ids != tuple(effect_ids) and not pre_dispatch_holding:
            continue
        if disposition == "reconcile_delivery":
            if (payload.get("reason") != "technical_holding_sent" or not effects
                or tuple(effect_ids) != holding_ids):
                continue
        elif disposition in {"manager_reply", "manager_reply_uncovered"}:
            if effects and not pre_dispatch_holding and (any(row.state != "sent" or row.actor != "bot"
                                   or row.purpose != "normal_reply" for row in effects)
                            or not _whole_sent_proof(revision, effects)[0]):
                continue
        else:
            continue
        archive = {"version": 1, "outcome": "archived_by_fresh_reset", "reply_confirmed": False,
                   "reset_after_message_id": boundary, "revision_id": revision_id,
                   "actor_id": getattr(actor, "pk", None), "at": now.isoformat()}
        task.manager_context = {**context, "fresh_reset_archive": archive}
        task.status = task.Status.CANCELLED
        task.skip_reason = "fresh_conversation_reset"
        task.manager_approval_status = task.ManagerApprovalStatus.REJECTED
        task.manager_approval_actor = actor if getattr(actor, "pk", None) else None
        task.manager_approval_decided_at = now
        task.save(update_fields=["manager_context", "status", "skip_reason", "manager_approval_status",
                                 "manager_approval_actor", "manager_approval_decided_at", "updated_at"])
        archived.append(task.pk)
    return {"archived_reply_attention_count": len(archived), "archived_reply_attention_task_ids": archived}


def _retire_fresh_work(client, *, now):
    # The operator owns the client lock and the global reply boundary. Retain
    # started/unknown/sent effects unchanged for reconciliation and accounting.
    # Match the executor's revision-before-effect lock order. Completed
    # recovery classifications remain history; active ownership is released.
    IgCustomerTurnRevision.objects.filter(client=client).update(
        active_slot=None, claim_token="",
        claimed_at=None, lease_until=None, updated_at=now,
    )
    from django.db.models import Q

    IgCustomerTurnRevision.objects.filter(client=client).filter(
        Q(recovery_due_at__isnull=False)
        | Q(recovery_state__in=("waiting", "manual", "execution", "pending", "processing")),
    ).update(recovery_state="cancelled", recovery_due_at=None,
             recovery_code="fresh_conversation_reset", updated_at=now)
    revisions = IgCustomerTurnRevision.objects.filter(client=client).exclude(
        state__in=(IgCustomerTurnRevision.State.PROCESSED, IgCustomerTurnRevision.State.SUPERSEDED),
    ).update(state=IgCustomerTurnRevision.State.SUPERSEDED, active_slot=None,
             recovery_state="cancelled", recovery_due_at=None,
             recovery_code="fresh_conversation_reset", claim_token="",
             claimed_at=None, lease_until=None, processed_at=now, updated_at=now)
    effects = IgRevisionDeliveryEffect.objects.filter(
        revision__client=client,
        state__in=(IgRevisionDeliveryEffect.State.PLANNED, IgRevisionDeliveryEffect.State.CLAIMED),
    ).update(state=IgRevisionDeliveryEffect.State.CANCELLED,
             failure_code="fresh_conversation_reset", terminal_at=now,
             claim_token="", lease_until=None, updated_at=now)
    turns = IgCustomerTurn.objects.filter(client=client, claim_state__in=(
        IgCustomerTurn.ClaimState.OPEN, IgCustomerTurn.ClaimState.CLAIMED,
    )).update(claim_state=IgCustomerTurn.ClaimState.SUPERSEDED,
              terminal_reason=IgCustomerTurn.TerminalReason.SUPERSEDED,
              claim_token="", claimed_at=None, processed_at=now, updated_at=now)
    return {"cancelled_delivery_effects": effects, "retired_revisions": revisions,
            "retired_turns": turns}


class _FreshResetFinancialBusy(Exception):
    pass


def _reset_financial_rows(queryset):
    """Reset may own Client while native money owns Deal: never wait back."""
    if connection.vendor == "sqlite":
        return queryset.select_for_update()  # SQLite has no row-lock primitive.
    if not connection.features.has_select_for_update_nowait:
        raise _FreshResetFinancialBusy
    return queryset.select_for_update(nowait=True)


def _prelock_reset_assisted_owner(proposal_id, client_id):
    from management.services.ig_assisted_commerce import _models

    Request, Decision, Execution, _Image = _models()
    identity = Execution.objects.filter(proposal_id=proposal_id).values("pk", "client_id", "request_id", "decision_id").first()
    if identity is None:
        return
    if identity["client_id"] != client_id:
        raise ValueError("reset_checkout_owner_mismatch")
    _reset_financial_rows(Request.objects.all()).get(pk=identity["request_id"], client_id=client_id)
    _reset_financial_rows(Decision.objects.all()).get(pk=identity["decision_id"], client_id=client_id)
    _reset_financial_rows(Execution.objects.all()).get(pk=identity["pk"], client_id=client_id)


def _prelock_reset_promo_rows(attempts):
    from django.db.models import Q
    from storefront.models import PromoCode, PromoCodeGuestUsage

    promo_ids, guest_ids, guest_keys = set(), set(), set()
    for attempt in attempts:
        state = attempt.event_state or {}
        reservations = state.get("promo_reservations") or [state.get("promo_reservation") or {}]
        for row in reservations:
            if not isinstance(row, dict) or row.get("state") != "reserved":
                continue
            if row.get("promo_id") or attempt.promo_code_id:
                promo_ids.add(row.get("promo_id") or attempt.promo_code_id)
            if row.get("guest_usage_id"):
                guest_ids.add(row["guest_usage_id"])
            if row.get("guest_reservation_key"):
                guest_keys.add(row["guest_reservation_key"])
    list(_reset_financial_rows(PromoCode.objects.filter(pk__in=promo_ids)).order_by("pk").values_list("pk", flat=True))
    list(_reset_financial_rows(PromoCodeGuestUsage.objects.filter(
        Q(pk__in=guest_ids) | Q(reservation_key__in=guest_keys, promo_code_id__in=promo_ids),
    )).order_by("pk").values_list("pk", flat=True))


def _retire_fresh_checkout_links(client, *, now):
    """Retire local unpaid access without inventing provider cancellation.

    Caller owns the client and reply boundaries. Native checkout's owner lock
    prefix and terminalizer retain paid/uncertain money and reservation proof.
    An existing bank URL is evidence for review, never a locally cancelled bill.
    """
    from management.models import IgCheckoutProposal, IgCheckoutInvoiceGeneration, IgCheckoutInventoryReservation, IgDeal
    from management.services.ig_checkout_terminalization import terminalize_payment_attempt
    from management.services.ig_inventory import release_proposal_inventory
    from orders.models import PaymentAttempt

    result = {"retired_checkout_proposals": 0, "revoked_checkout_tokens": 0,
              "cancelled_checkout_attempts": 0, "released_checkout_reservations": 0,
              "checkout_payment_review": 0, "preserved_paid_checkout_proposals": 0}
    locators = list(IgCheckoutProposal.objects.filter(client=client)
                    .order_by("deal_id", "pk").values("pk", "deal_id"))
    paid_states = {PaymentAttempt.Status.PAID, PaymentAttempt.Status.PREPAID, PaymentAttempt.Status.CONVERTED}
    paid_generation_states = {"winner_claimed", "paid_winner", "late_paid_review", "resource_review"}
    for locator in locators:
        _prelock_reset_assisted_owner(locator["pk"], client.pk)
        deal = _reset_financial_rows(IgDeal.objects.all()).get(pk=locator["deal_id"], client=client)
        proposal = _reset_financial_rows(IgCheckoutProposal.objects.select_related(
            "client", "commercial_episode",
        )).get(
            pk=locator["pk"], client=client, deal=deal,
        )
        generations = list(_reset_financial_rows(IgCheckoutInvoiceGeneration.objects.all())
                           .filter(proposal=proposal).order_by("generation", "pk"))
        generation_by_attempt = {row.payment_attempt_id: row for row in generations if row.payment_attempt_id}
        attempt_ids = set(generation_by_attempt)
        if proposal.payment_attempt_id:
            attempt_ids.add(proposal.payment_attempt_id)
        attempts = list(_reset_financial_rows(PaymentAttempt.objects.filter(pk__in=attempt_ids)).order_by("pk"))
        protected_payment = bool(
            proposal.paid_at or proposal.winner_invoice_generation_id
            or deal.paid_at or Decimal(str(deal.paid_amount or 0)) > 0 or deal.payment_status == "paid"
            or deal.payment_truth in {"confirmed", "partially_refunded", "refunded", "reversed"}
            or any(row.winner_slot == 1 or row.state in paid_generation_states for row in generations)
            or any(row.status in paid_states or row.checkout_winner_claimed
                   or Decimal(str(row.paid_amount or 0)) > 0 for row in attempts)
        )
        if proposal.status == proposal.Status.PAID:
            result["preserved_paid_checkout_proposals"] += 1
            continue
        # Parent -> token and parent -> reservation must fail fast too: a
        # token-entry join or settlement may already own the child row.
        token_ids = list(_reset_financial_rows(proposal.access_tokens.filter(revoked_at__isnull=True))
                         .order_by("pk").values_list("pk", flat=True))
        list(_reset_financial_rows(IgCheckoutInventoryReservation.objects.filter(proposal=proposal))
             .order_by("pk").values_list("pk", flat=True))
        _prelock_reset_promo_rows(attempts)
        already_retired = proposal.status in {
            proposal.Status.REVOKED, proposal.Status.EXPIRED,
            proposal.Status.CANCELLED, proposal.Status.SUPERSEDED,
        }

        # Deal-level buyer truth cannot grant an unpaid proposal a live token.
        # Retain that financial graph for review while retiring local access.
        safe = not (protected_payment or deal.order_id or deal.invoice_id or deal.invoice_url or deal.payment_payload)
        safe = safe and all(row.payment_attempt_id for row in generations)
        for attempt in attempts:
            generation = generation_by_attempt.get(attempt.pk)
            state = attempt.event_state or {}
            no_provider_effect = not (
                attempt.order_id or attempt.monobank_invoice_id or attempt.invoice_url
                or attempt.invoice_payload or attempt.payment_history or state.get("invoice_creation_ambiguous")
                or (generation and (generation.provider_request_digest or generation.provider_invoice_id))
            )
            if generation is None:
                no_provider_effect = bool(no_provider_effect and not state.get("invoice_creation_lease")
                    and attempt.status in {PaymentAttempt.Status.INITIATED, PaymentAttempt.Status.FAILED,
                                           PaymentAttempt.Status.CANCELLED, PaymentAttempt.Status.EXPIRED})
            else:
                no_provider_effect = bool(no_provider_effect and generation.state in {
                    "planned", "provider_inflight", "failed", "cancelled", "expired",
                })
            safe = safe and no_provider_effect
        if safe:
            for attempt in attempts:
                if attempt.status not in {PaymentAttempt.Status.INITIATED, PaymentAttempt.Status.PROCESSING}:
                    continue
                outcome = terminalize_payment_attempt(attempt.pk,
                    terminal_status=PaymentAttempt.Status.CANCELLED, reason="fresh_conversation_reset",
                    source="checkout_session_reset", now=now)
                if outcome.outcome not in {"terminalized", "already_terminal"}:
                    safe = False
                    break
                result["cancelled_checkout_attempts"] += int(outcome.outcome == "terminalized")
                result["released_checkout_reservations"] += outcome.released_inventory
            proposal.refresh_from_db()
            deal.refresh_from_db()
        if safe:
            if not already_retired:
                proposal.status = proposal.Status.REVOKED
                proposal.save(update_fields=["status", "updated_at"])
                result["retired_checkout_proposals"] += 1
            result["released_checkout_reservations"] += int(
                release_proposal_inventory(proposal, reason="fresh_conversation_reset") or 0)
        else:
            # No financial/provider status, receipt, generation or scarce hold
            # changes here. Keep the old invoice graph visible for review.
            if not already_retired and proposal.status != proposal.Status.MANAGER_REVIEW:
                proposal.status = proposal.Status.MANAGER_REVIEW
                proposal.save(update_fields=["status", "updated_at"])
            result["checkout_payment_review"] += 1
        result["revoked_checkout_tokens"] += proposal.access_tokens.filter(pk__in=token_ids, revoked_at__isnull=True).update(revoked_at=now)
        if deal.active_checkout_proposal_id == proposal.pk:
            deal.active_checkout_proposal_id = None
            deal.save(update_fields=["active_checkout_proposal", "updated_at"])
    # Legacy direct bank links may predate hosted proposals altogether. They
    # cannot be revoked locally; surface their unpaid evidence for review too.
    result["checkout_payment_review"] += IgDeal.objects.filter(
        client=client, checkout_proposals__isnull=True, paid_at__isnull=True, paid_amount__lte=0,
    ).exclude(invoice_id="", invoice_url="").exclude(payment_status="paid").exclude(
        payment_truth__in={"confirmed", "partially_refunded", "refunded", "reversed"},
    ).count()
    return result


def reset_funnel(*, client_id: int, actor, reason: str = "manual_reset",
                 mode: str = "funnel", expected_permission_epoch: int | None = None) -> dict:
    try:
        return _reset_funnel(client_id=client_id, actor=actor, reason=reason,
                             mode=mode, expected_permission_epoch=expected_permission_epoch)
    except (OperationalError, _FreshResetFinancialBusy) as exc:
        # Native NOWAIT/deadlock codes only. Translation occurs after the
        # complete transaction unwinds; no partial context or token retirement.
        code = exc.args[0] if isinstance(exc, OperationalError) and exc.args else None
        if mode != "fresh_conversation" or (
            isinstance(exc, OperationalError) and code not in {1205, 1213, 3572}
        ):
            raise
        return {"ok": False, "status": 409, "code": "reset_busy",
                "error": "Зараз триває операція з оплатою або посиланням. Скидання не виконано. Спробуйте ще раз після її завершення."}


def _reset_funnel(*, client_id: int, actor, reason: str,
                  mode: str, expected_permission_epoch: int | None) -> dict:
    """Reset mutable CRM inference while retaining the immutable transcript/truth.

    The caller must be an operator and the endpoint supplies the explicit
    confirmation. Hidden clients are deliberately excluded from this flow.
    """
    reason = (reason or "manual_reset").strip()[:255]
    if mode not in {"funnel", "fresh_conversation"}:
        return {"ok": False, "status": 400, "error": "Невідомий режим скидання."}
    fresh = mode == "fresh_conversation"
    retired = {}
    new_session = None
    from management.services.ig_commercial_episodes import commercial_episode_client_lock

    client_boundary = commercial_episode_client_lock(client_id) if fresh else nullcontext()
    with client_boundary, pause_reply_boundary():
        with transaction.atomic():
            clients = IgClient.objects.select_related("current_commercial_episode")
            clients = _reset_financial_rows(clients) if fresh else clients.select_for_update()
            client = (
                clients
                .filter(pk=client_id)
                .first()
            )
            if not client:
                return {"ok": False, "status": 404, "error": "Клієнта не знайдено."}
            if client.hidden_at:
                return {
                    "ok": False,
                    "status": 409,
                    "error": "Прихованого клієнта не можна скидати: бот його не обробляє.",
                }
            if expected_permission_epoch is not None and int(client.reply_permission_epoch or 0) != expected_permission_epoch:
                return {"ok": False, "status": 409,
                        "error": "Стан діалогу змінився. Оновіть картку й підтвердіть скидання ще раз."}
            now = timezone.now()
            boundary = (
                InstagramBotMessage.objects.filter(client=client)
                .order_by("-id")
                .values_list("id", flat=True)
                .first()
                or 0
            )
            previous_state = {
                "stage": client.stage,
                "intent": client.intent,
                "language": client.language,
                "buying_readiness": int(client.buying_readiness or 0),
                "primary_objection": client.primary_objection,
                "current_product_id": client.current_product_id,
                "current_size": client.current_size,
                "current_color": client.current_color,
                "current_qty": int(client.current_qty or 1),
                "discount_offered_percent": int(client.discount_offered_percent or 0),
                "bot_paused": bool(client.bot_paused),
                "manager_takeover": bool(client.manager_takeover),
                "opted_out": bool(
                    client.opted_out_at
                    and (not client.opted_in_at or client.opted_in_at < client.opted_out_at)
                ),
                "current_commercial_episode_id": client.current_commercial_episode_id,
            }
            if fresh:
                previous_state["reset_mode"] = mode
                previous_state["reset_boundary"] = _fresh_boundary(
                    client, now=now, message_id=boundary,
                )
                checkout_retirement = _retire_fresh_checkout_links(client, now=now)
                previous_state["checkout_retirement"] = checkout_retirement

            verified = client_has_confirmed_purchase(client)
            # A reset must not erase the fact of a purchase. Deals are only one
            # of the ways an order becomes ours: on production the exchange case
            # of client #59 hangs on an assignment with no deal at all.
            has_order = (
                client.deals.filter(order_id__isnull=False).exists()
                or client.order_assignments.filter(
                    unassigned_at__isnull=True, order_id__isnull=False
                ).exists()
                or client.order_attributions.filter(order_id__isnull=False).exists()
            )
            if verified and not fresh:
                resulting_stage = (
                    IgClient.Stage.ORDER_CREATED if has_order else IgClient.Stage.PAID
                )
            else:
                resulting_stage = IgClient.Stage.NEW

            # A reset is a hard processing boundary. Existing inbound rows are
            # retained for the transcript but must never be answered later.
            pending_messages = InstagramBotMessage.objects.filter(
                client=client,
                role=InstagramBotMessage.Role.USER,
                status__in=(
                    InstagramBotMessage.Status.PENDING,
                    InstagramBotMessage.Status.PROCESSING,
                ),
            ).exclude(send_state__in=("sending", "unknown", "ambiguous", "sent") if fresh else ("sending",))
            retired_messages = pending_messages.update(
                status=InstagramBotMessage.Status.DONE,
                processed_at=now,
                processing_started_at=None,
            )
            if fresh:
                from management.services.ig_permission_transitions import cancel_client_unstarted_automation

                cancelled_automation = cancel_client_unstarted_automation(
                    client, reason="fresh_conversation_reset", now=now, nowait=False,
                )
                attention_retirement = _archive_fresh_reply_attention(
                    client, boundary=boundary, actor=actor, now=now,
                )
                previous_state["reply_attention_retirement"] = attention_retirement
                retired = _retire_fresh_work(client, now=now)
                retired.update(attention_retirement)
                retired["retired_pending_messages"] = retired_messages
                retired["cancelled_automation"] = cancelled_automation
                retired.update(checkout_retirement)
            # Independent service/reconciliation obligations survive; proved
            # old conversational alerts have a separate archival receipt.
            # Legacy funnel reset keeps its existing cancellation contract.
            cancelled_followups = (
                cancelled_automation["followups"] if fresh
                else bot_followups.cancel_pending(client, reason="funnel_reset")
            )
            current_episode_id = client.current_commercial_episode_id
            cancelled_follow_ctas = 0
            if current_episode_id:
                # A reset changes the conversation watermark and therefore
                # invalidates every unreserved optional CTA from this episode.
                # Sent/ambiguous rows remain durable history and keep their
                # lifetime cooldown semantics.
                cancelled_follow_ctas = IgFollowCtaDecision.objects.filter(
                    client_id=client.pk,
                    commercial_episode_id=current_episode_id,
                    state__in=(
                        IgFollowCtaDecision.State.WAITING_FOLLOW,
                        IgFollowCtaDecision.State.PREPARING,
                        IgFollowCtaDecision.State.PREPARED,
                        IgFollowCtaDecision.State.RESERVED,
                    ),
                ).update(
                    state=IgFollowCtaDecision.State.CANCELLED,
                    episode_slot_key=None,
                    sent_scope_key=None,
                    lease_token="",
                    lease_expires_at=None,
                    completed_at=now,
                    suppression_reason="funnel_reset",
                    last_error_kind="",
                    last_error_code="",
                    updated_at=now,
                )

            # In-flight high-reasoning work is stale after the boundary. Keep
            # the row for operational accounting, but make it terminal.
            IgConversationAnalysisJob.objects.filter(
                client=client,
                status__in=(
                    IgConversationAnalysisJob.Status.PENDING,
                    IgConversationAnalysisJob.Status.PROCESSING,
                ),
            ).update(
                status=IgConversationAnalysisJob.Status.SKIPPED,
                skip_reason="funnel_reset",
                lease_token="",
                lease_until=None,
                claimed_watermark_message_id=0,
                claimed_revision=0,
                claimed_materiality_event_highwater=0,
                claimed_materiality_digest="",
                claimed_authority_digest="",
                claimed_artifact_digest="",
                last_error="",
                updated_at=now,
            )
            from management.services.ig_memory_producer import memory_invalidation_updates

            update = {
                **memory_invalidation_updates(client),
                "stage": resulting_stage,
                "stage_updated_at": now,
                "language": "",
                "intent": IgClient.Intent.UNKNOWN,
                "buying_readiness": 0,
                "primary_objection": IgClient.Objection.NONE,
                "lost_reason": "",
                "current_product": None,
                "current_size": "",
                "current_color": "",
                "current_qty": 1,
                "current_product_confidence": 0,
                "discount_offered_percent": 0,
                "next_followup_at": None,
                "followup_level": 0,
                "delivery_status": "",
                "delivery_error": "",
                "delivery_http_code": None,
                "delivery_graph_code": None,
                "delivery_graph_subcode": None,
                "delivery_failed_at": None,
                "memory_summary": "",
                "memory_updated_at": None,
                # Сброс обнуляет выводы системы, но не подтверждённые факты:
                # названный клиентом размер и высказанное возражение не перестают
                # быть правдой от того, что оператор перезапустил воронку.
                "sales_context": {} if fresh else _preserved_sales_context(client),
                "automation_lease_token": "",
                "automation_lease_until": None,
                # Invalidate a worker that generated before the reset. The
                # pause state itself is intentionally preserved.
                "reply_permission_epoch": int(client.reply_permission_epoch or 0) + 1,
                "updated_at": now,
            }
            if fresh:
                update.update(ad_ref="", ad_id="", ad_source="", ad_title="",
                              ad_creative_url="", referral_payload={}, phone="", phone_normalized="")
            for field, value in update.items():
                setattr(client, field, value)
            client.save(update_fields=[*update.keys()])

            current_episode = client.current_commercial_episode
            if verified and not fresh and current_episode and current_episode.open_slot == 1:
                current_episode.opened_watermark_message_id = boundary
                current_episode.save(update_fields=[
                    "opened_watermark_message_id", "updated_at",
                ])
            elif fresh or not verified:
                if current_episode and current_episode.open_slot == 1:
                    current_episode.open_slot = None
                    current_episode.save(update_fields=["open_slot", "updated_at"])
                previous = client.commercial_episodes.order_by("-sequence", "-id").first()
                sequence = int(getattr(previous, "sequence", 0) or 0) + 1
                episode = IgCommercialEpisode.objects.create(
                    client=client,
                    sequence=sequence,
                    open_slot=1,
                    materialization_key=f"ig-reset:{client.pk}:{uuid.uuid4().hex}",
                    opened_watermark_message_id=boundary,
                )
                client.current_commercial_episode = episode
                client.save(update_fields=["current_commercial_episode", "updated_at"])
                if fresh:
                    from management.services.ig_commerce_projection import start_new_session_for_episode

                    new_session = start_new_session_for_episode(client, episode)

            audit = IgFunnelResetAudit.objects.create(
                client=client,
                reset_after_message_id=boundary,
                previous_state=previous_state,
                resulting_stage=resulting_stage,
                reason=reason,
                actor=actor if getattr(actor, "pk", None) else None,
            )
            if fresh:
                from management.services.ig_webhook_inbox import archive_pre_reset_inbox

                retired["archived_inbox_events"] = archive_pre_reset_inbox(
                    client, boundary=previous_state["reset_boundary"], now=now,
                )
                client.refresh_from_db(fields=["reply_permission_epoch"])
    # Э8.5: сброс меняет границу эпизода, поэтому снимок хода обязан её забыть.
    # Иначе кэш отдал бы старую границу уже после записи новой.
    from management.services.ig_turn_snapshot import invalidate

    invalidate(f"funnel_reset_floor:{client.pk}")
    def invalidate_typed_memory_after_commit():
        try:
            from management.services.ig_typed_memory import (
                invalidate_memory_for_reset,
            )

            invalidate_memory_for_reset(
                client_id=client.pk,
                reset_after_message_id=boundary,
            )
        except Exception:
            pass

    transaction.on_commit(invalidate_typed_memory_after_commit)
    return {
        "ok": True,
        "status": 200,
        "audit_id": audit.pk,
        "reset_after_message_id": boundary,
        "stage": resulting_stage,
        "cancelled_followups": cancelled_followups,
        "cancelled_follow_ctas": cancelled_follow_ctas,
        "mode": mode,
        "reply_permission_epoch": client.reply_permission_epoch,
        "commercial_episode_id": client.current_commercial_episode_id,
        "selection_session_id": getattr(new_session, "pk", None),
        **retired,
    }
