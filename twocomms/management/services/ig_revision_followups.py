"""Normal follow-up policy tied to one sealed source and confirmed reply.

The legacy after-reply call graph is DB-only, but its denial/replacement paths
cancel unrelated cases and derive time from a mutable client field. This adapter
uses the same policy/gate/quiet-hour functions with narrow writes and a receipt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Mapping

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from management.models import (
    IgBotNotification, IgClient, IgCustomerTurnRevision, IgDeal, IgFollowUpTask,
    IgRevisionDeliveryEffect, InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_revision_authority import (
    CLAIM_PUBLIC_POLICY_INPUTS, build_revision_authority_bindings,
    check_fact_bindings, check_offer_bindings,
)
from management.services.ig_revision_outbox import PublicationBinding, _digest, pre_winner_readiness


RECEIPT_KEY = "normal_followups"
VERSION = "revision-normal-followups-v2"
CURSOR_VERSION = "revision-followup-evaluation-v1"
AUTOMATIC_SALES_KINDS = ("qualification", "payment", "thinking", "rescue", "final")


@dataclass(frozen=True)
class RevisionFollowupResult:
    ready: bool = False
    reason: str = ""
    task_id: int = 0
    receipt: dict = field(default_factory=dict)
    replayed: bool = False
    cancelled_ids: tuple[int, ...] = ()


class _Blocked(Exception):
    pass


def _evaluation_cursor_valid(value) -> bool:
    """Accept only complete v1 cursors on immutable receipt replay."""
    if not isinstance(value, Mapping) or value.get("version") != CURSOR_VERSION:
        return False
    required = (
        "source_message_ids", "source_anchor", "sent_effect_ids",
        "sent_reply_anchor", "inbound_anchor", "meta_window_deadline",
        "evaluated_at", "reason", "task_id", "due_at",
    )
    if any(key not in value for key in required):
        return False
    if not isinstance(value["source_message_ids"], list) or not value["source_message_ids"] or not all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in value["source_message_ids"]
    ):
        return False
    if not isinstance(value["sent_effect_ids"], list) or not value["sent_effect_ids"] or not all(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in value["sent_effect_ids"]
    ):
        return False
    if any(not isinstance(value[key], str) for key in (
        "source_anchor", "sent_reply_anchor", "inbound_anchor",
        "meta_window_deadline", "evaluated_at", "reason", "due_at",
    )) or not value["reason"]:
        return False
    return isinstance(value["task_id"], int) and not isinstance(value["task_id"], bool) and value["task_id"] >= 0


def _existing_receipt_replay(existing) -> RevisionFollowupResult | None:
    """Return a replay result only when the durable cursor is verifiable."""
    if (
        not isinstance(existing, Mapping)
        or not isinstance(existing.get("reason"), str)
        or not isinstance(existing.get("task_id"), int)
        or isinstance(existing.get("task_id"), bool)
        or existing["task_id"] < 0
        or not isinstance(existing.get("due_at"), str)
        or not _evaluation_cursor_valid(existing.get("evaluation_cursor"))
    ):
        return RevisionFollowupResult(reason="followup_cursor_repair_required")
    cursor = existing["evaluation_cursor"]
    if (
        cursor["reason"] != existing["reason"]
        or cursor["task_id"] != existing["task_id"]
        or cursor["due_at"] != existing["due_at"]
    ):
        return RevisionFollowupResult(reason="followup_cursor_repair_required")
    return RevisionFollowupResult(True, existing["reason"], existing["task_id"], dict(existing), True)


def _locked(revision_id, client_id, settings_id):
    settings_row = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=settings_id).first()
    client = IgClient.objects.select_for_update().filter(pk=client_id).first()
    revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id, client_id=client_id).first()
    if settings_row is None or client is None or revision is None:
        raise _Blocked("followup_identity_missing")
    return settings_row, client, revision


def _check_source(revision, client, settings_row, token, epoch, publication, now):
    if not revision.snapshot_digest or _digest(revision.bundle_snapshot) != revision.snapshot_digest:
        raise _Blocked("revision_snapshot_invalid")
    snapshots = revision.bundle_snapshot.get("sources") or []
    if not snapshots or len(snapshots) > 32 or any(row.get("role") != "user" for row in snapshots):
        raise _Blocked("followup_actual_user_source_required")
    sources = list(revision.sources.select_for_update().select_related("message").order_by("ordinal", "id"))
    if len(sources) != len(snapshots):
        raise _Blocked("followup_source_changed")
    for source, snapshot in zip(sources, snapshots, strict=True):
        if (
            source.message_id != snapshot.get("message_id")
            or source.source_digest != snapshot.get("source_digest")
            or source.message.client_id != client.pk
            or source.message.role != InstagramBotMessage.Role.USER
        ):
            raise _Blocked("followup_source_changed")
    authority = build_revision_authority_bindings(client, claims=(CLAIM_PUBLIC_POLICY_INPUTS,), settings_obj=settings_row)
    if not authority.ready:
        raise _Blocked("followup_authority_unavailable")
    readiness = pre_winner_readiness(
        revision.pk, token, settings_id=settings_row.pk,
        settings_permission_epoch=epoch, publication=publication,
        fact_bindings=authority.fact_bindings, fact_checker=check_fact_bindings,
        offer_checker=check_offer_bindings, now=now,
    )
    if not readiness.ready:
        raise _Blocked(readiness.reasons[0])
    anchor = max(source.provider_created_at or source.message.created_at for source in sources)
    return sources, anchor


def _automatic_timers(client):
    from management.services import bot_followups as policy

    reasons = tuple(policy.FOLLOWUP_POLICIES) + tuple(policy.POLICY_REASON_ALIASES) + ("ordinary_price_inquiry", "ordinary_requested_selection")
    return IgFollowUpTask.objects.filter(
        client=client, status=IgFollowUpTask.Status.PENDING,
        kind__in=AUTOMATIC_SALES_KINDS, trigger=IgFollowUpTask.Trigger.TIME,
        reason__in=reasons,
    )


def cancel_revision_sales_timers(
    revision_id, token, *, settings_id, settings_permission_epoch,
    publication: PublicationBinding, now=None,
):
    """Cancel automatic sales timers only; case and parcel queues are separate."""
    if connection.in_atomic_block:
        return RevisionFollowupResult(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionFollowupResult(reason="revision_missing")
    now = now or timezone.now()
    try:
        with transaction.atomic():
            settings_row, client, revision = _locked(revision_id, identity["client_id"], settings_id)
            sources, _anchor = _check_source(revision, client, settings_row, token, settings_permission_epoch, publication, now)
            if RECEIPT_KEY in (revision.action_receipts or {}):
                return RevisionFollowupResult(True, "reply_followups_already_recorded", replayed=True)
            received_at = max(source.message.created_at for source in sources)
            tasks = _automatic_timers(client).filter(
                Q(created_at__lte=received_at) | Q(policy_started_at__lt=_anchor),
            ).select_for_update()
            task_ids = tuple(tasks.values_list("pk", flat=True)[:129])
            if len(task_ids) > 128:
                raise _Blocked("followup_timer_limit")
            tasks.update(status=IgFollowUpTask.Status.CANCELLED, skip_reason="client_reply", updated_at=now)
            from management.services import bot_followups as policy

            policy._resolve_discount_approval_notifications(task_ids, reason="client_reply")
            if task_ids:
                policy._update_client_next(client)
            return RevisionFollowupResult(True, "automatic_sales_timers_cancelled", cancelled_ids=task_ids)
    except _Blocked as exc:
        return RevisionFollowupResult(reason=str(exc))
    except Exception:
        return RevisionFollowupResult(reason="followup_cancellation_failed")


def _sent_reply(revision):
    rows = list(revision.delivery_effects.select_for_update().order_by("order_index", "id"))
    text = [row for row in rows if row.group == "substantive_text"]
    if not text or len(rows) > 16 or len(text) != text[0].part_count:
        raise _Blocked("whole_substantive_reply_not_sent")
    if [row.part_index for row in text] != list(range(len(text))) or any(
        row.state != row.State.SENT or not row.provider_message_id
        or row.part_count != len(text) or row.plan_digest != text[0].plan_digest
        or _digest(row.payload) != row.payload_digest for row in text
    ):
        raise _Blocked("whole_substantive_reply_not_sent")
    if any(row.state in {row.State.PLANNED, row.State.CLAIMED, row.State.PROVIDER_STARTED, row.State.UNKNOWN} for row in rows):
        raise _Blocked("reply_delivery_unresolved")
    proposal = revision.generation_proposal or {}
    input_receipt = (revision.action_receipts or {}).get("input_decision") or {}
    if revision.generation_proposal_digest:
        if _digest(proposal) != revision.generation_proposal_digest:
            raise _Blocked("generation_proposal_changed")
        request_id = (proposal.get("generation") or {}).get("request_id")
        if not request_id or any(row.generation_request_id != request_id for row in text):
            raise _Blocked("followup_generation_identity_mismatch")
        origin = "generate"
        authority = proposal.get("authority") or {}
    elif input_receipt.get("origin") == "static_reply" and input_receipt.get("snapshot_digest") == revision.snapshot_digest:
        origin = "static_reply"
        authority = input_receipt.get("authority") or {}
    else:
        raise _Blocked("followup_origin_ineligible")
    policy_facts = [row for row in authority.get("fact_bindings", ()) if row.get("claim") == CLAIM_PUBLIC_POLICY_INPUTS]
    if len(policy_facts) != 1:
        raise _Blocked("followup_policy_binding_missing")
    return rows, text, origin, policy_facts


def _fulfillment_case(client, deal, revision, anchor, now):
    from management.services.instagram_bot import notify_manager

    key = f"revision-fulfillment:{client.pk}:{deal.pk}"
    task, created = IgFollowUpTask.objects.get_or_create(
        event_key=key,
        defaults={
            "client": client, "deal": deal, "due_at": now,
            "kind": IgFollowUpTask.Kind.MANAGER_TASK,
            "status": IgFollowUpTask.Status.SKIPPED,
            "reason": "revision_case:paid_fulfillment",
            "manager_approval_status": IgFollowUpTask.ManagerApprovalStatus.PENDING,
            "manager_approval_requested_at": now,
            "skip_reason": "human_fulfillment_decision_required",
            "trigger": IgFollowUpTask.Trigger.EVENT, "event_occurred_at": anchor,
            "policy_started_at": anchor,
            "event_payload": {"origin": "normal_followups", "deal_id": deal.pk, "initial_revision_id": revision.pk},
            "message_text": "Перевірити стан оплати та безпечний шлях оформлення доставки через checkout. Не запитувати повну адресу в Direct і не створювати ТТН автоматично.",
            "manager_context": {"case_kind": "paid_fulfillment", "deal_id": deal.pk, "source_revision_id": revision.pk, "fulfillment_started": False},
        },
    )
    if task.client_id != client.pk or task.deal_id != deal.pk:
        raise _Blocked("followup_case_identity_mismatch")
    if created and not notify_manager(
        task.message_text, dedupe_key=key, event_type="paid_delivery_missing",
        client=client, metadata={"case_kind": "paid_fulfillment", "followup_task_id": task.pk, "deal_id": deal.pk},
        deliver_immediately=False, raise_on_error=True,
    ):
        raise _Blocked("followup_case_notification_failed")
    return task


def delivered_price_answer(client, rows):
    """A screenshot request/holding reply is not an answer to a price question."""
    from management.services.ig_reply_authority import build_reply_truth_context
    from management.services.ig_reply_truth import (
        _MONEY_RE, _PREFIX_MONEY_RE, _currency_code, _decimal, _locally_negated,
    )

    context = build_reply_truth_context(client)
    prices = {_decimal(value) for value in context.authorized_prices}
    prices.discard(None)
    if not prices:
        return False
    for row in rows:
        if row.group != "substantive_text":
            continue
        text = str(((row.payload or {}).get("message") or {}).get("text") or "")
        for pattern in (_MONEY_RE, _PREFIX_MONEY_RE):
            for match in pattern.finditer(text):
                if (_currency_code(match.group("currency")) in context.allowed_currency_codes
                    and _decimal(match.group("amount")) in prices
                    and not _locally_negated(text, match.start())):
                    return True
    return False


def _schedule(client, revision, rows, anchor, now, *, include_cursor=False):
    from management.services import bot_followups as policy

    cursor = {
        "version": CURSOR_VERSION,
        "source_message_ids": [
            row.get("message_id")
            for row in ((getattr(revision, "bundle_snapshot", {}) or {}).get("sources") or ())
        ],
        "source_anchor": anchor.isoformat() if anchor else "",
        "sent_effect_ids": [row.pk for row in rows if row.group == "substantive_text"],
        "sent_reply_anchor": "",
        "inbound_anchor": "",
        "meta_window_deadline": "",
        "evaluated_at": now.isoformat(),
    }

    def finish(task, reason, **extra):
        result = task, reason, {**cursor, **extra, "reason": reason, "task_id": task.pk if task else 0,
                               "due_at": task.due_at.isoformat() if task else ""}
        return result if include_cursor else result[:2]

    deal = IgDeal.objects.select_for_update().select_related("active_checkout_proposal").filter(client=client).exclude(status=IgDeal.Status.CANCELLED).order_by("-pk").first()
    scenario = policy.resolve_followup_scenario(client, deal=deal)
    if scenario == "paid_missing_delivery":
        obsolete = IgFollowUpTask.objects.filter(
            client=client, deal=deal, kind=IgFollowUpTask.Kind.FULFILLMENT,
            trigger=IgFollowUpTask.Trigger.TIME, reason="paid_missing_delivery",
            status=IgFollowUpTask.Status.PENDING,
        )
        obsolete_ids = list(obsolete.select_for_update().values_list("pk", flat=True)[:129])
        if len(obsolete_ids) > 128:
            raise _Blocked("followup_timer_limit")
        obsolete.update(status=IgFollowUpTask.Status.CANCELLED, skip_reason="hosted_checkout_only", updated_at=now)
        if obsolete_ids:
            policy._update_client_next(client)
        if deal.order_id:
            return finish(None, "order_already_materialized")
        # A paid receipt is not a recipient-only collection form. Without a
        # proven such form, hand the existing deal to the team; never reuse a
        # ready payment URL which might create a second invoice.
        task = _fulfillment_case(client, deal, revision, anchor, now)
        return finish(task, "paid_fulfillment_case")
    from management.services.ig_turn_intent import build_turn_intent, purpose_blockers, ordinary_next_send_at

    decision = build_turn_intent(client, revision)
    cursor["source_message_ids"] = list(decision.get("source_message_ids") or [])
    purpose = decision["purpose"]
    offsets = {"price_inquiry": timedelta(hours=3), "requested_selection": timedelta(minutes=90)}
    if purpose not in offsets or not decision["commerce_evidence_refs"]:
        return finish(None, "current_purpose_not_followup_eligible")
    if deal and deal.active_checkout_proposal:
        return finish(None, "hosted_checkout_v2_owns_followups" if deal.active_checkout_proposal.assisted_checkout_v2 else "checkout_owns_followups")
    if purpose == "price_inquiry" and not delivered_price_answer(client, rows):
        return finish(None, "price_answer_not_confirmed")
    blocker = purpose_blockers(client, decision, revision=revision)
    if blocker:
        return finish(None, blocker)
    allowed, reason = policy._client_allows_followup(client, deal=deal, kind=IgFollowUpTask.Kind.THINKING)
    if not allowed:
        return finish(None, reason)
    sent_parts = [row for row in rows if row.group == "substantive_text"]
    if not sent_parts or any(row.terminal_at is None for row in sent_parts):
        return finish(None, "sent_reply_timestamp_missing")
    sent_anchor = max(row.terminal_at for row in sent_parts)
    cursor["sent_reply_anchor"] = sent_anchor.isoformat()
    # Channel permission is inbound-based; sending our reply never renews it.
    inbound = InstagramBotMessage.objects.filter(client=client, role="user", pk__lte=max(decision["source_message_ids"])).order_by("-pk").first()
    inbound_anchor = (inbound.provider_created_at or inbound.created_at) if inbound else anchor
    deadline = inbound_anchor + policy.META_REPLY_WINDOW
    cursor.update({"inbound_anchor": inbound_anchor.isoformat(), "meta_window_deadline": deadline.isoformat()})
    # Optional sales use ordinary hours only; passing deadline widens legacy
    # helper hours to its emergency slot, which is inappropriate here.
    due = ordinary_next_send_at(max(sent_anchor + offsets[purpose], now))
    if due >= deadline:
        return finish(None, "ordinary_followup_not_sendable")
    key = f"ordinary-intent-followup:{decision['cycle_key']}"
    task, created = IgFollowUpTask.objects.get_or_create(
        event_key=key,
        defaults={
            "client": client, "deal": deal, "due_at": due, "status": IgFollowUpTask.Status.PENDING,
            "kind": IgFollowUpTask.Kind.THINKING, "level": 0, "reason": f"ordinary_{purpose}",
            "meta_window_deadline": deadline, "trigger": IgFollowUpTask.Trigger.TIME,
            "event_occurred_at": inbound_anchor, "policy_started_at": sent_anchor,
            "policy_version": "ordinary-intent.v1",
            "event_payload": {"origin": "ordinary_intent_followup", "revision_id": revision.pk,
                              "snapshot_digest": revision.snapshot_digest, "purpose": purpose,
                              "cycle_key": decision["cycle_key"], "source_message_ids": decision["source_message_ids"],
                              "commerce_evidence_refs": decision["commerce_evidence_refs"],
                              "route_decision_id": decision["route_decision_id"],
                              "informational_debt_refs": decision.get("informational_debt_refs", []),
                              "client_permission_epoch": client.reply_permission_epoch,
                              "product_id": client.current_product_id,
                              "settings_id": getattr(sent_parts[0], "settings_id_snapshot", 0),
                              "settings_permission_epoch": getattr(sent_parts[0], "settings_permission_epoch", -1),
                              "publication_id": getattr(sent_parts[0], "publication_id", 0),
                              "publication_hash": getattr(sent_parts[0], "publication_hash", ""),
                              "sent_effect_ids": [row.pk for row in sent_parts],
                              "sent_reply_anchor": sent_anchor.isoformat(), "budget": "reserved"},
        },
    )
    if task.client_id != client.pk:
        raise _Blocked("followup_timer_identity_mismatch")
    if not created:
        return finish(task, "ordinary_cycle_already_reserved")
    policy._update_client_next(client)
    return finish(task, "normal_followup_scheduled")


def schedule_revision_normal_followups(
    revision_id, token, *, settings_id, settings_permission_epoch,
    publication: PublicationBinding, now=None,
):
    """Schedule existing normal policy once, only after a complete sent reply."""
    if connection.in_atomic_block:
        return RevisionFollowupResult(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionFollowupResult(reason="revision_missing")
    now = now or timezone.now()
    try:
        with transaction.atomic():
            settings_row, client, revision = _locked(revision_id, identity["client_id"], settings_id)
            sources, anchor = _check_source(revision, client, settings_row, token, settings_permission_epoch, publication, now)
            rows, text, origin, policy_facts = _sent_reply(revision)
            source_ids = {source.message_id for source in sources}
            if any(
                row.settings_id_snapshot != settings_id
                or row.settings_permission_epoch != settings_permission_epoch
                or row.publication_id != publication.publication_id
                or row.publication_version != publication.version
                or row.publication_hash != publication.snapshot_hash
                or row.source_message_id not in source_ids
                or row.revision_snapshot_digest != revision.snapshot_digest
                or row.plan_digest != text[0].plan_digest
                for row in rows
            ):
                raise _Blocked("followup_delivery_binding_changed")
            if not check_fact_bindings(policy_facts, revision=revision, client=client, settings_obj=settings_row):
                raise _Blocked("followup_policy_binding_changed")
            binding = {
                "version": VERSION, "origin": origin, "snapshot_digest": revision.snapshot_digest,
                "source_message_ids": [source.message_id for source in sources], "source_anchor": anchor.isoformat(),
                "settings_id": settings_id, "settings_permission_epoch": settings_permission_epoch,
                "publication": {"id": publication.publication_id, "version": publication.version, "hash": publication.snapshot_hash},
                "plan_digest": text[0].plan_digest, "sent_effect_ids": [row.pk for row in text],
            }
            existing = (revision.action_receipts or {}).get(RECEIPT_KEY)
            if existing is not None:
                if not isinstance(existing, Mapping) or any(existing.get(key) != value for key, value in binding.items()):
                    raise _Blocked("normal_followup_receipt_mismatch")
                return _existing_receipt_replay(existing)
            task, reason, cursor = _schedule(client, revision, rows, anchor, now, include_cursor=True)
            receipt = {**binding, "reason": reason, "task_id": task.pk if task else 0, "due_at": task.due_at.isoformat() if task else "", "evaluation_cursor": cursor, "recorded_at": now.isoformat()}
            revision.action_receipts = {**(revision.action_receipts or {}), RECEIPT_KEY: receipt}
            revision.save(update_fields=["action_receipts", "updated_at"])
            return RevisionFollowupResult(True, reason, task.pk if task else 0, receipt)
    except _Blocked as exc:
        return RevisionFollowupResult(reason=str(exc))
    except Exception:
        return RevisionFollowupResult(reason="normal_followup_failed")


def settle_revision_normal_followups(revision_id, finalization_token, *, now=None):
    """Settle future optional work from SENT receipts after any send deadline.

    Changed permissions, rules, source head or deadline produce a durable
    non-action. Only an eligible current revision may create a fresh timer.
    """
    if connection.in_atomic_block:
        return RevisionFollowupResult(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionFollowupResult(reason="revision_missing")
    now = now or timezone.now()
    try:
        first = IgRevisionDeliveryEffect.objects.filter(revision_id=revision_id).order_by("order_index", "id").first()
        if first is None:
            return RevisionFollowupResult(reason="followup_receipts_missing")
        with transaction.atomic():
            settings_row = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=first.settings_id_snapshot).first()
            client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
            revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
            if client is None or revision is None:
                raise _Blocked("followup_identity_missing")
            if not finalization_token.startswith("finalize:") or revision.claim_token != finalization_token:
                raise _Blocked("followup_finalization_claim_changed")
            rows, text, origin, policy_facts = _sent_reply(revision)
            snapshots = revision.bundle_snapshot.get("sources") or []
            source_ids = [row["message_id"] for row in snapshots]
            if any(row.source_message_id not in source_ids for row in rows):
                raise _Blocked("followup_delivery_binding_changed")
            publication = {"id": first.publication_id, "version": first.publication_version, "hash": first.publication_hash}
            binding = {
                "version": VERSION, "origin": origin, "snapshot_digest": revision.snapshot_digest,
                "source_message_ids": source_ids, "settings_id": first.settings_id_snapshot,
                "settings_permission_epoch": first.settings_permission_epoch, "publication": publication,
                "plan_digest": first.plan_digest, "sent_effect_ids": [row.pk for row in text],
            }
            existing = (revision.action_receipts or {}).get(RECEIPT_KEY)
            if existing is not None:
                if not isinstance(existing, Mapping) or any(existing.get(key) != value for key, value in binding.items()):
                    raise _Blocked("normal_followup_receipt_mismatch")
                replay = _existing_receipt_replay(existing)
                if replay is not None and not replay.ready:
                    return replay
                withdrawn = (
                    revision.active_slot != 1 or settings_row is None or not settings_row.is_enabled
                    or client.hidden_at is not None or client.is_blocked or client.bot_paused or client.manager_takeover
                    or client.privacy_erasure_started_at is not None
                    or client.reply_permission_epoch != first.client_permission_epoch
                    or (settings_row is not None and settings_row.reply_permission_epoch != first.settings_permission_epoch)
                )
                if withdrawn and existing.get("task_id"):
                    IgFollowUpTask.objects.filter(pk=existing["task_id"], client=client, status=IgFollowUpTask.Status.PENDING).exclude(kind=IgFollowUpTask.Kind.MANAGER_TASK).update(status=IgFollowUpTask.Status.CANCELLED, skip_reason="permission_changed_after_reply", updated_at=now)
                return replay
            sources = list(revision.sources.select_related("message").order_by("ordinal", "id"))
            source_valid = len(sources) == len(source_ids) and all(
                row.message_id == source_id and row.message.client_id == client.pk and row.role == "user"
                for row, source_id in zip(sources, source_ids)
            )
            anchor = max((row.provider_created_at or row.message.created_at for row in sources), default=None) if source_valid else None
            inbound = InstagramBotMessage.objects.filter(
                client=client, role="user", pk__lte=max(source_ids, default=0),
            ).order_by("-pk").first()
            inbound_anchor = (inbound.provider_created_at or inbound.created_at) if inbound else anchor
            from management.services import bot_followups as policy
            meta_window_deadline = inbound_anchor + policy.META_REPLY_WINDOW if inbound_anchor else None
            reason = ""
            if client.privacy_erasure_started_at is not None:
                reason = "erasure_private_projection_suppressed"
            elif not source_valid or anchor is None:
                reason = "followup_source_unavailable"
            elif revision.active_slot != 1:
                reason = "newer_customer_head"
            elif settings_row is None or not settings_row.is_enabled:
                reason = "settings_disabled"
            else:
                pub = PublicationBinding(first.publication_id, first.publication_version, first.publication_hash)
                readiness = pre_winner_readiness(
                    revision.pk, finalization_token, settings_id=first.settings_id_snapshot,
                    settings_permission_epoch=first.settings_permission_epoch, publication=pub,
                    fact_bindings=policy_facts, fact_checker=check_fact_bindings,
                    offer_checker=check_offer_bindings, now=now,
                )
                denied = [item for item in readiness.reasons if item != "revision_deadline_exhausted"]
                if denied:
                    # Follow-up timing survives the short answer deadline; all
                    # source, channel, policy and permission denials remain.
                    reason = denied[0]
            task = None
            if not reason:
                task, reason, cursor = _schedule(client, revision, rows, anchor, now, include_cursor=True)
            if reason:
                cursor = {
                    "version": CURSOR_VERSION,
                    "source_message_ids": source_ids,
                    "source_anchor": anchor.isoformat() if anchor else "",
                    "sent_effect_ids": [row.pk for row in text],
                    "sent_reply_anchor": max(row.terminal_at for row in text).isoformat(),
                    "inbound_anchor": inbound_anchor.isoformat() if inbound_anchor else "",
                    "meta_window_deadline": meta_window_deadline.isoformat() if meta_window_deadline else "",
                    "evaluated_at": now.isoformat(), "reason": reason,
                    "task_id": task.pk if task else 0,
                    "due_at": task.due_at.isoformat() if task else "",
                }
            receipt = {**binding, "source_anchor": anchor.isoformat() if anchor else "", "reason": reason,
                       "task_id": task.pk if task else 0, "due_at": task.due_at.isoformat() if task else "",
                       "outcome": "scheduled" if task else "not_scheduled", "evaluation_cursor": cursor, "recorded_at": now.isoformat()}
            revision.action_receipts = {**(revision.action_receipts or {}), RECEIPT_KEY: receipt}
            revision.save(update_fields=["action_receipts", "updated_at"])
            return RevisionFollowupResult(True, reason, task.pk if task else 0, receipt)
    except _Blocked as exc:
        return RevisionFollowupResult(reason=str(exc))
    except Exception:
        return RevisionFollowupResult(reason="normal_followup_failed")
