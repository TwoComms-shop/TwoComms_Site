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
VERSION = "revision-normal-followups-v1"
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

    reasons = tuple(policy.FOLLOWUP_POLICIES) + tuple(policy.POLICY_REASON_ALIASES)
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


def _pending_case(client):
    # Human cases use SKIPPED + approval=PENDING so a timer worker cannot send
    # operator copy to the customer. SENT is not necessarily human resolution.
    return IgFollowUpTask.objects.filter(client=client, kind=IgFollowUpTask.Kind.MANAGER_TASK).exclude(
        status__in=(IgFollowUpTask.Status.COMPLETED, IgFollowUpTask.Status.CANCELLED),
    ).exclude(reason__startswith="parcel_reminder:").exists()


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


def _schedule(client, revision, rows, anchor, now):
    from management.services import bot_followups as policy

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
            return None, "order_already_materialized"
        # A paid receipt is not a recipient-only collection form. Without a
        # proven such form, hand the existing deal to the team; never reuse a
        # ready payment URL which might create a second invoice.
        return _fulfillment_case(client, deal, revision, anchor, now), "paid_fulfillment_case"
    if _pending_case(client):
        return None, "pending_manager_case"
    policy_row = policy.FOLLOWUP_POLICIES.get(scenario)
    step = next((item for item in policy_row.steps if item.trigger == "time" and item.offset is not None), None) if policy_row else None
    if step is None:
        return None, "normal_policy_unavailable"
    kind = policy._persisted_step_kind(step)
    allowed, reason = policy._client_allows_followup(client, deal=deal, kind=kind)
    if not allowed:
        return None, reason
    if not policy._policy_condition_holds(step.condition, client, deal=deal):
        return None, "normal_policy_condition_absent"
    proposal = deal.active_checkout_proposal if deal else None
    offset = step.offset
    task_reason = scenario
    if proposal and proposal.status in {proposal.Status.READY, proposal.Status.VIEWED}:
        if proposal.assisted_checkout_v2:
            return None, "hosted_checkout_v2_owns_followups"
        policy_row = policy.FOLLOWUP_POLICIES["payment_link_unpaid"]
        step = next(item for item in policy_row.steps if item.trigger == "time" and item.offset is not None)
        kind = policy._persisted_step_kind(step)
        offset = max(proposal.expires_at - anchor, timedelta(0))
        task_reason = "checkout_proposal_abandoned"
    deadline = anchor + policy.META_REPLY_WINDOW
    due = policy.next_allowed_send_at(anchor + offset, deadline=deadline)
    if due <= now:
        return None, "normal_followup_due_elapsed"
    skip_reason = ""
    if due > deadline and kind != IgFollowUpTask.Kind.MANAGER_TASK:
        kind = IgFollowUpTask.Kind.MANAGER_TASK
        skip_reason = "meta_window_closed"
    task, _created = IgFollowUpTask.objects.get_or_create(
        event_key=f"revision-normal-followup:{revision.pk}:{task_reason}",
        defaults={
            "client": client, "deal": deal, "due_at": due, "status": IgFollowUpTask.Status.PENDING,
            "kind": kind, "level": step.index, "reason": task_reason,
            "discount_percent": step.discount_percent,
            "manager_approval_status": (IgFollowUpTask.ManagerApprovalStatus.PENDING if step.discount_percent else IgFollowUpTask.ManagerApprovalStatus.NOT_REQUIRED),
            "meta_window_deadline": deadline, "trigger": IgFollowUpTask.Trigger.TIME,
            "event_occurred_at": anchor, "policy_started_at": anchor, "policy_version": "followup-v1",
            "event_payload": {"origin": "normal_followups", "revision_id": revision.pk, "snapshot_digest": revision.snapshot_digest},
            "skip_reason": skip_reason,
        },
    )
    if task.client_id != client.pk or task.due_at != due:
        raise _Blocked("followup_timer_identity_mismatch")
    if task.kind == IgFollowUpTask.Kind.MANAGER_TASK and not task.message_text:
        task.message_text = policy.compose_followup(task, now=due)
        task.save(update_fields=["message_text", "updated_at"])
    policy._update_client_next(client)
    return task, "normal_followup_scheduled"


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
                return RevisionFollowupResult(True, existing["reason"], existing["task_id"], dict(existing), True)
            task, reason = _schedule(client, revision, rows, anchor, now)
            receipt = {**binding, "reason": reason, "task_id": task.pk if task else 0, "due_at": task.due_at.isoformat() if task else "", "recorded_at": now.isoformat()}
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
                withdrawn = (
                    revision.active_slot != 1 or settings_row is None or not settings_row.is_enabled
                    or client.hidden_at is not None or client.is_blocked or client.bot_paused or client.manager_takeover
                    or client.privacy_erasure_started_at is not None
                    or client.reply_permission_epoch != first.client_permission_epoch
                    or (settings_row is not None and settings_row.reply_permission_epoch != first.settings_permission_epoch)
                )
                if withdrawn and existing.get("task_id"):
                    IgFollowUpTask.objects.filter(pk=existing["task_id"], client=client, status=IgFollowUpTask.Status.PENDING).exclude(kind=IgFollowUpTask.Kind.MANAGER_TASK).update(status=IgFollowUpTask.Status.CANCELLED, skip_reason="permission_changed_after_reply", updated_at=now)
                return RevisionFollowupResult(True, existing["reason"], existing["task_id"], dict(existing), True)
            sources = list(revision.sources.select_related("message").order_by("ordinal", "id"))
            source_valid = len(sources) == len(source_ids) and all(
                row.message_id == source_id and row.message.client_id == client.pk and row.role == "user"
                for row, source_id in zip(sources, source_ids)
            )
            anchor = max((row.provider_created_at or row.message.created_at for row in sources), default=None) if source_valid else None
            reason = ""
            if client.privacy_erasure_started_at is not None:
                reason = "erasure_private_projection_suppressed"
            elif not source_valid or anchor is None:
                reason = "followup_source_unavailable"
            elif revision.overall_deadline <= now:
                reason = "reply_deadline_elapsed"
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
                if not readiness.ready:
                    # These are current policy/permission denials, not a reason
                    # to retry delivery or rewrite the accepted generation.
                    reason = readiness.reasons[0]
            task = None
            if not reason:
                task, reason = _schedule(client, revision, rows, anchor, now)
            receipt = {**binding, "source_anchor": anchor.isoformat() if anchor else "", "reason": reason,
                       "task_id": task.pk if task else 0, "due_at": task.due_at.isoformat() if task else "",
                       "outcome": "scheduled" if task else "not_scheduled", "recorded_at": now.isoformat()}
            revision.action_receipts = {**(revision.action_receipts or {}), RECEIPT_KEY: receipt}
            revision.save(update_fields=["action_receipts", "updated_at"])
            return RevisionFollowupResult(True, reason, task.pk if task else 0, receipt)
    except _Blocked as exc:
        return RevisionFollowupResult(reason=str(exc))
    except Exception:
        return RevisionFollowupResult(reason="normal_followup_failed")
