"""Operator-owned reply debt, separate from customer reminder delivery.

Reuse the existing durable revision case identity. A parked execution is not an
answered message, and a later unrelated reply is not proof that this debt ended.
"""
import re

from django.db import transaction
from django.db.models import BigIntegerField, CharField, Count, Exists, F, OuterRef, Q, Subquery, Value
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Cast, Concat, Now
from django.utils import timezone

from management.models import IgClient, IgCustomerTurnRevision, IgFollowUpTask


COVERAGE_KEY = "response_coverage"
PAUSED_RESPONSE_CODES = ("waiting_on_customer", "operator_handled", "manager_reply_confirmed")


def paused_response_obligation_q():
    """Durable execution dispositions, scoped to this revision's sources.

    They do not acknowledge customer sources or prove transport delivery. A
    fresh inbound has its own successor and is not paused by this predicate.
    """
    case_key = Concat(Value("ig-rate-handoff:"), Cast(OuterRef("client_id"), CharField()),
        Value(":"), Cast(OuterRef("action_receipts__rate_handoff__window__anchor_revision_id"), CharField()))
    rate_task = IgFollowUpTask.objects.filter(
        pk=Cast(OuterRef("action_receipts__rate_handoff__task_id"), BigIntegerField()),
        client_id=OuterRef("client_id"), kind="manager_task", reason="revision_case:rate_limit_handoff",
        event_key=case_key,
        event_payload__rate_window_digest=OuterRef("action_receipts__rate_handoff__window_digest"))
    rate_receipt = KeyTransform("rate_handoff", "action_receipts")
    rate = Q(recovery_code="rate_limit_handoff",
        action_receipts__rate_handoff__version="revision-rate-handoff-v1",
        action_receipts__rate_handoff__substantive_obligation="open_manager_reply",
        pk=Cast(KeyTextTransform("revision_id", rate_receipt), BigIntegerField()),
        snapshot_digest=KeyTextTransform("snapshot_digest", rate_receipt)) & Q(Exists(rate_task))
    return Q(recovery_state="manual", claim_token="", lease_until__isnull=True) & (
        Q(recovery_code__in=PAUSED_RESPONSE_CODES) | rate)


def response_obligation_paused(revision):
    if (revision.recovery_state == "manual" and revision.recovery_code == "rate_limit_handoff"
        and not revision.claim_token and revision.lease_until is None):
        from management.services.ig_revision_rate_handoff import rate_handoff_scope_valid
        return rate_handoff_scope_valid(revision)
    return (revision.recovery_state == "manual" and revision.recovery_code in PAUSED_RESPONSE_CODES
            and not revision.claim_token and revision.lease_until is None)


def _pause_response_obligation(revision, code):
    """Stop the exact execution owner without rewriting its physical ledger."""
    if (revision.recovery_state == "manual" and revision.recovery_code == code
        and revision.recovery_due_at is None and not revision.claim_token
        and revision.claimed_at is None and revision.lease_until is None):
        return
    revision.recovery_state = "manual"
    revision.recovery_code = code
    revision.recovery_due_at = None
    revision.claim_token = ""
    revision.claimed_at = None
    revision.lease_until = None
    revision.save(update_fields=["recovery_state", "recovery_code", "recovery_due_at",
                                "claim_token", "claimed_at", "lease_until", "updated_at"])


def response_coverage(revision):
    """Read a source-bound semantic receipt; physical SENT alone is not coverage."""
    from management.services.ig_revision_outbox import _digest

    receipts = revision.action_receipts if isinstance(revision.action_receipts, dict) else {}
    receipt = receipts.get(COVERAGE_KEY) or {}
    if not receipt:
        return {}
    if not isinstance(receipt, dict) or not isinstance(revision.bundle_snapshot, dict):
        return {"remaining": ["coverage_unverified"], "disposition": "manual"}
    material = {key: value for key, value in receipt.items() if key != "digest"}
    ids = [row.get("message_id") for row in (revision.bundle_snapshot or {}).get("sources", ())]
    if (receipt.get("version") != 1 or receipt.get("snapshot_digest") != revision.snapshot_digest
        or _digest(revision.bundle_snapshot) != revision.snapshot_digest
        or receipt.get("source_message_ids") != ids or receipt.get("digest") != _digest(material)):
        return {"remaining": ["coverage_unverified"], "disposition": "manual"}
    return receipt


def response_coverage_complete(revision):
    """Missing semantic evidence cannot establish a completed customer answer."""
    coverage = response_coverage(revision)
    covered = coverage.get("covered")
    if not (coverage.get("version") == 1 and coverage.get("disposition") == "complete"
            and isinstance(covered, list) and all(isinstance(item, str) and item for item in covered)
            and coverage.get("remaining") == []):
        return False
    if covered:
        return True
    # The native plan deliberately has no commerce obligations for an isolated
    # greeting/thanks. Retain its actual validated plan receipt; an absent
    # receipt, arbitrary empty list, mixed question or media cannot use this.
    from management.services.ig_turn_intent import _NONACTION_GREETING

    plan_digest = coverage.get("plan_digest")
    sources = (revision.bundle_snapshot or {}).get("sources") or []
    return bool(isinstance(plan_digest, str) and re.fullmatch(r"[0-9a-f]{64}", plan_digest)
                and sources and all(isinstance(source, dict) and source.get("role") == "user"
                    and _NONACTION_GREETING.fullmatch(str(source.get("text") or "").strip())
                    and not source.get("media_parts") and not source.get("discovered_media")
                    for source in sources))


def record_response_coverage(revision_id, token, coverage):
    """Persist a validated plan's completion boundary before proposal recording.

    The plan owner validates semantic obligation IDs. This helper validates the
    captured source boundary and execution lease, and never creates send rights.
    """
    from management.services.ig_revision_outbox import _digest
    from management.services.ig_revision_holding import _sources_unchanged

    if not isinstance(coverage, dict):
        return False
    plan_digest = coverage.get("plan_digest", "")
    next_selector = coverage.get("next_selector", "")
    payment_verification = coverage.get("payment_verification")
    if payment_verification not in (None, "unresolved"):
        return False
    if (not isinstance(plan_digest, str) or (plan_digest and (len(plan_digest) != 64 or any(char not in "0123456789abcdef" for char in plan_digest)))
        or not isinstance(next_selector, str)
        or (next_selector not in {"", "model", "size", "color", "fit", "variant", "options", "quantity", "product"}
            and not re.fullmatch(r"option:[a-z][a-z0-9_-]{0,48}", next_selector))):
        return False
    covered, remaining = coverage.get("covered", []), coverage.get("remaining", [])
    if (not isinstance(covered, list) or not isinstance(remaining, list)
        or len(covered) + len(remaining) > 64
        or any(not isinstance(item, str) or not item or len(item) > 160 for item in covered + remaining)
        or set(covered).intersection(remaining)
        or coverage.get("disposition") not in {"complete", "waiting_on_customer", "recovery", "manual"}
        or (bool(remaining) == (coverage.get("disposition") == "complete"))):
        return False
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return False
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        now = timezone.now()
        if (revision is None or client is None or revision.claim_token != token
            or revision.state != "claimed" or revision.active_slot != 1
            or revision.lease_until is None or revision.lease_until <= now
            or revision.permission_epoch != client.reply_permission_epoch
            or client.privacy_erasure_started_at or revision.erasure_started_at_snapshot
            or client.bot_paused or client.manager_takeover or client.is_blocked or client.hidden_at
            or not revision.snapshot_digest or _digest(revision.bundle_snapshot) != revision.snapshot_digest
            or not _sources_unchanged(revision)):
            return False
        source_ids = [row["message_id"] for row in revision.bundle_snapshot.get("sources", ())]
        if payment_verification == "unresolved":
            payment_ids = [int(match.group(1)) for item in covered + remaining
                if (match := re.fullmatch(r"([1-9]\d*):(?:payment:(?:receipt|claim)|info:payment(?:_instructions)?)", item))]
            if not payment_ids or any(identity not in source_ids for identity in payment_ids):
                return False
        receipt = {"version": 1, "snapshot_digest": revision.snapshot_digest,
                   "source_message_ids": source_ids, "covered": list(dict.fromkeys(covered)),
                   "remaining": list(dict.fromkeys(remaining)), "disposition": coverage["disposition"]}
        if "product_inquiry_capture" in coverage:
            from management.services.ig_product_inquiries import ProductInquiryCapture
            captured = coverage["product_inquiry_capture"]
            if type(captured) is not ProductInquiryCapture:
                return False
            inquiry = captured.recompute(revision)
            if inquiry is None:
                return False
            # The backend capability proves resolved facts as well as open
            # questions. Keep both under the same source/lease CAS so a fresh
            # outbox owner does not lose native care/material authority and
            # misclassify the original source as an unknown-product inquiry.
            receipt["product_inquiries"] = inquiry
        if plan_digest:
            receipt["plan_digest"] = plan_digest
        if next_selector:
            receipt["next_selector"] = next_selector
        if payment_verification == "unresolved":
            # A source-bound managerial check still owed after the customer
            # acknowledgement. This marker creates no money or send rights.
            receipt["payment_verification"] = "unresolved"
        receipt["digest"] = _digest(receipt)
        existing = (revision.action_receipts or {}).get(COVERAGE_KEY)
        if existing and existing != receipt:
            return False
        revision.action_receipts = {**(revision.action_receipts or {}), COVERAGE_KEY: receipt}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return True


def delivered_selector_wait(revision, *, effects=None):
    """Only an owned fully SENT selector question can transfer waiting to user."""
    from management.services.ig_revision_execution import _whole_sent_proof
    from management.services.ig_revision_outbox import _revision_namespace
    from management.services.ig_revision_holding import _sources_unchanged
    from management.services.ig_response_plan import asks_next_selector

    coverage = response_coverage(revision)
    selector = coverage.get("next_selector", "")
    if not (coverage.get("remaining") and coverage.get("disposition") == "waiting_on_customer" and selector):
        return False
    effects = list(revision.delivery_effects.order_by("order_index", "id")) if effects is None else list(effects)
    if (not effects or any(row.state != "sent" or row.actor != "bot"
                          or row.provider_namespace != _revision_namespace(revision)
                          or row.client_permission_epoch != revision.permission_epoch for row in effects)
        or not _whole_sent_proof(revision, effects)[0] or not _sources_unchanged(revision)):
        return False
    texts = [str((row.payload.get("message") or {}).get("text") or "")
             for row in effects if row.actor == "bot" and row.purpose == "normal_reply"]
    return asks_next_selector(" ".join(texts), selector)


class UtcObservationNow(Now):
    """Statement observation time independent of the MariaDB session timezone."""

    def as_mysql(self, compiler, connection, **extra_context):
        return self.as_sql(compiler, connection, template="UTC_TIMESTAMP(6)", **extra_context)


DEBT_REASON = "revision_case:execution_debt"
DEBT_KIND = "revision_execution_debt"
REASON_LABELS = {
    "captured_state_exceeds_budget": "Підготовка відповіді зупинилася; потрібно опрацювати запит вручну",
    "semantic_reply_incomplete": "Запит клієнта опрацьовано не повністю",
    "provider_dispatch_budget": "Потрібна відповідь команди: ліміт підготовки відповіді вичерпано",
    "provider_candidates_exhausted": "Не вдалося підготувати відповідь після кількох спроб",
    "generation_failed": "Бот не зміг підготувати відповідь",
    "generation_outcome_unresolved": "Результат підготовки відповіді потребує звірки",
    "generation_result_missing": "Підготовлена відповідь не збережена для відправлення",
    "generation_not_started": "Підготовка відповіді не почалася вчасно",
    "generation_semantic_rejected": "Відповідь потребує перевірки змісту командою",
    "preparation_expired": "Обробка запиту не завершилася вчасно",
    "proposal_not_dispatched": "Відповідь підготовлена, але ще не відправлена",
    "provider_horizon_exhausted": "Час автоматичного відновлення відповіді вичерпано",
    "delivery_unknown": "Потрібно звірити, чи доставлено відповідь",
    "provider_started_unreconciled": "Відправлення почалося, результат ще не підтверджено",
    "partial_definite_failure": "Частину відповіді не доставлено",
    "partial_cancelled_delivery": "Частину відповіді скасовано до відправлення",
    "definite_failure": "Відповідь не доставлено",
    "recovery_delivery_reconciliation": "Потрібно звірити вже відправлені частини",
}


def unresolved_reply_debts():
    return IgFollowUpTask.objects.filter(
        kind=IgFollowUpTask.Kind.MANAGER_TASK, reason=DEBT_REASON,
    ).exclude(status__in=(
        IgFollowUpTask.Status.COMPLETED, IgFollowUpTask.Status.CANCELLED,
    ))


def reply_debt_notification_key(revision, source_ids):
    from management.services.ig_revision_outbox import _digest

    return f"revision-reply-debt:{revision.pk}:{_digest(list(source_ids))[:24]}"


def ensure_reply_debt_notification(revision, task, *, now=None):
    """Queue the exact manager handoff without making a provider request.

    A task is internal ownership, not evidence that its owner was informed.
    Source identity, rather than elapsed time, is the durable dedupe boundary.
    """
    from management.services.instagram_bot import notify_manager
    from management.services.ig_alerts import client_admin_url

    source_ids = list(revision.sources.order_by("ordinal", "id").values_list("message_id", flat=True))
    if (not source_ids or task.status in {task.Status.COMPLETED, task.Status.CANCELLED}
        or task.client_id != revision.client_id or task.event_key != f"ig-revision-debt:{revision.pk}"
        or (task.event_payload or {}).get("source_message_ids") != source_ids):
        return False
    reason = str((task.event_payload or {}).get("reason") or "reply_unresolved")
    return notify_manager(
        f"Потрібна відповідь клієнту. {REASON_LABELS.get(reason, 'Запит залишився незавершеним.')}\n"
        f"Клієнт #{revision.client_id}; запит #{revision.pk}; задача #{task.pk}.\n"
        f"Перевірте запит і надішліть відповідь: {client_admin_url(revision.client_id)}",
        dedupe_key=reply_debt_notification_key(revision, source_ids), event_type="escalation",
        client=revision.client, deliver_immediately=False, raise_on_error=True,
        metadata={"case_kind": DEBT_KIND, "manager_task_id": task.pk,
                  "revision_id": revision.pk, "source_message_ids": source_ids,
                  "snapshot_digest": revision.snapshot_digest, "reason": reason,
                  "customer_reply_confirmed": False, "requires_human_review": True},
    )


def response_obligation(revision, *, task=None, effects=None, manager_notifications=None, _visited=()):
    """Observe exact customer coverage separately from internal handoff state.

    This function only reads. An unrelated outbound or manager notification
    never covers the source; an operator disposition is not a delivered reply.
    """
    from management.models import IgBotNotification
    from management.services.ig_revision_execution import _completion_decision, _whole_sent_proof

    source_ids = [row.message_id for row in revision.sources.all()]
    effects = list(revision.delivery_effects.all()) if effects is None else list(effects)
    coverage = response_coverage(revision)
    receipts = revision.action_receipts if isinstance(revision.action_receipts, dict) else {}
    safe = receipts.get("provider_safe_reply") or {}
    safe = safe if isinstance(safe, dict) else {"reply_mode": "neutral_ack"}
    try:
        proof = bool(effects and _whole_sent_proof(revision, effects)[0])
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        proof = False
    acknowledged = proof and any(row.state == "sent" for row in effects)
    complete = bool(proof and _completion_decision(effects) == (True, False, "delivered")
                    and any(row.state == "sent" and row.purpose == "normal_reply"
                            and row.group == "substantive_text" for row in effects)
                    and response_coverage_complete(revision) and safe.get("reply_mode") != "neutral_ack")
    if task is None:
        task = IgFollowUpTask.objects.filter(client_id=revision.client_id,
            event_key=f"ig-revision-debt:{revision.pk}", kind="manager_task", reason=DEBT_REASON).first()
    payload = task.event_payload if task and isinstance(task.event_payload, dict) else {}
    context = task.manager_context if task and isinstance(task.manager_context, dict) else {}
    source_task = bool(task and task.client_id == revision.client_id
                       and task.kind == "manager_task" and task.reason == DEBT_REASON
                       and task.event_key == f"ig-revision-debt:{revision.pk}"
                       and payload.get("source_message_ids") == source_ids
                       and payload.get("revision_id") == revision.pk
                       and context.get("revision_id") == revision.pk)
    exact_task = source_task and context.get("owner") == "manager"
    # Older genuine review closures predate the optional owner projection.
    # Their exact actor/audit/source proof is authority for disposition only;
    # it must not become manager delivery or customer reply evidence.
    review = (context.get("operator_review") or {}) if source_task else {}
    review = review if isinstance(review, dict) else {}
    reviewed = bool(source_task and task.status == task.Status.CANCELLED
                    and task.skip_reason == "operator_reviewed_no_reply"
                    and context.get("owner") in (None, "", "manager")
                    and review.get("version") == 1
                    and type(review.get("actor_id")) is int and review["actor_id"] > 0
                    and isinstance(review.get("at"), str) and bool(review["at"])
                    and review.get("outcome") == "reviewed_no_reply"
                    and review.get("revision_id") == revision.pk
                    and review.get("source_message_ids") == source_ids and review.get("reply_confirmed") is False)
    if reviewed:
        from management.models import AdminAuditLog

        reviewed = AdminAuditLog.objects.filter(actor_id=review["actor_id"], action="ig_reply_debt_reviewed",
            entity_type="IgFollowUpTask", entity_id=str(task.pk), after__version=1,
            after__actor_id=review["actor_id"], after__at=review["at"],
            after__revision_id=revision.pk, after__source_message_ids=source_ids,
            after__status="cancelled", after__outcome="reviewed_no_reply", after__reply_confirmed=False).exists()
    settled = False
    if exact_task and task.status == task.Status.COMPLETED and task.skip_reason == "manager_reply_confirmed":
        from management.services.ig_lane_health import _manual_owner_tasks

        settled = _manual_owner_tasks().filter(pk=task.pk).exists()
    if manager_notifications is None:
        manager_notifications = IgBotNotification.objects.filter(client_id=revision.client_id,
            payload__manager_task_id=task.pk if exact_task else 0)
    matching_notifications = [row for row in manager_notifications if exact_task
        and row.client_id == revision.client_id and isinstance(row.payload, dict)
        and row.payload.get("manager_task_id") == task.pk and row.payload.get("revision_id") == revision.pk
        and row.payload.get("source_message_ids") == source_ids
        and row.dedupe_key == reply_debt_notification_key(revision, source_ids)]
    informed = any(row.status in {"sent", "resolved"} and row.sent_at and row.telegram_message_id
                   for row in matching_notifications)
    notification = next((row for row in matching_notifications if row.status in {
        "pending", "sending", "failed", "unknown", "dead_letter"} or
        (row.status in {"sent", "resolved"} and row.sent_at and row.telegram_message_id)), None)
    waiting = bool(coverage.get("remaining") and delivered_selector_wait(revision, effects=effects))
    disposition = ("answered" if complete or settled else "reviewed_no_reply" if reviewed
                   else "waiting_on_customer" if waiting else "substantive_reply_owed")
    result = {"source_message_ids": source_ids, "revision_id": revision.pk,
            "task_id": task.pk if exact_task else None, "disposition": disposition,
            "customer_ack_owed": bool(source_ids and not (acknowledged or complete or settled)),
            "substantive_owed": bool(source_ids and not (complete or settled)),
            "manager_informed": informed,
            "manager_notification_recorded": notification is not None,
            "manager_notification_id": notification.pk if notification else None,
            "manager_notification_status": notification.status if notification else "",
            "requires_attention": bool(source_ids and not (complete or settled or reviewed or waiting))}
    if receipts.get("rate_handoff"):
        from management.services.ig_revision_rate_handoff import rate_handoff_scope_valid, rate_handoff_answered

        if rate_handoff_scope_valid(revision):
            scope = receipts["rate_handoff"]
            initial = scope.get("initial_scope") is True
            notice = IgBotNotification.objects.filter(pk=scope["notification_id"],
                client_id=revision.client_id, payload__manager_task_id=scope["task_id"],
                payload__revision_id=revision.pk).first() if initial else None
            if notice and notice.payload.get("source_message_ids") != source_ids:
                notice = None
            human_answered = rate_handoff_answered(revision)
            result.update({"task_id": scope["task_id"], "disposition": "answered" if human_answered else "substantive_reply_owed",
                "substantive_owed": not human_answered, "customer_ack_owed": not (acknowledged or human_answered),
                "manager_informed": bool(notice and notice.status in {"sent", "resolved"}
                    and notice.sent_at and notice.telegram_message_id),
                "manager_notification_recorded": notice is not None,
                "manager_notification_id": notice.pk if notice else None,
                "manager_notification_status": notice.status if notice else "",
                "requires_attention": not human_answered})
    transfer = receipts.get("source_transfer_out") or {}
    if (not effects and isinstance(transfer, dict) and transfer.get("successor_revision_id")
        and revision.pk not in _visited and len(_visited) < 8):
        from management.services.ig_revision_source_coverage import validate_source_transfer

        successor = IgCustomerTurnRevision.objects.select_related("client").filter(
            pk=transfer["successor_revision_id"], client_id=revision.client_id).first()
        try:
            valid_transfer = successor is not None and validate_source_transfer(successor)
        except (AttributeError, KeyError, TypeError, ValueError):
            valid_transfer = False
        if valid_transfer:
            transferred = response_obligation(successor, _visited=(*_visited, revision.pk))
            result.update({key: transferred[key] for key in (
                "customer_ack_owed", "substantive_owed", "manager_informed", "requires_attention")})
            result.update(disposition="transferred", successor_revision_id=successor.pk)
    return result


def record_reply_debt(revision, reason, *, effects=(), now=None):
    """Called with the client/revision locked; never send or acknowledge a source."""
    now = now or timezone.now()
    source_ids = list(revision.sources.order_by("ordinal", "id").values_list("message_id", flat=True))
    effects = list(effects)
    semantic_pending = reason == "semantic_reply_incomplete" and all(row.state == "sent" for row in effects)
    disposition = ("manager_reply_uncovered" if semantic_pending else
                   "reconcile_delivery" if effects else "manager_reply")
    task, _created = IgFollowUpTask.objects.get_or_create(
        event_key=f"ig-revision-debt:{revision.pk}",
        defaults={
            "client_id": revision.client_id, "due_at": now,
            "kind": IgFollowUpTask.Kind.MANAGER_TASK,
            # This case is deliberately excluded from the customer send queue.
            "status": IgFollowUpTask.Status.SKIPPED, "reason": DEBT_REASON,
            "manager_approval_status": IgFollowUpTask.ManagerApprovalStatus.PENDING,
            "manager_approval_requested_at": now,
            "skip_reason": "execution_recovery_required",
            "trigger": IgFollowUpTask.Trigger.EVENT,
            "event_occurred_at": revision.overall_deadline,
            "policy_started_at": now, "policy_version": "revision-debt-v2",
            "message_text": "Потрібна відповідь команди: перевірте незавершений запит і вже доставлені частини.",
            "event_payload": {"revision_id": revision.pk, "reason": reason,
                "source_message_ids": source_ids, "effect_ids": [row.pk for row in effects]},
            "manager_context": {"case_kind": DEBT_KIND, "revision_id": revision.pk,
                "reason": reason, "owner": "manager", "disposition": disposition,
                "automatic_http_retry": False},
        },
    )
    ensure_reply_debt_notification(revision, task, now=now)
    return task


def park_manual_revision(revision, reason, *, now=None):
    """Relinquish the execution lease without inventing reply completion.

    State remains compatible with receipt finalization. recovery_state=manual
    and this receipt are the disposition; CLAIMED without a token is not work
    in progress. Keep active head/source history for explicit successor transfer.
    """
    now = now or timezone.now()
    coverage = response_coverage(revision)
    if reason == "semantic_reply_incomplete" and delivered_selector_wait(revision):
        receipt = {"version": 1, "owner": "customer", "disposition": "waiting_on_customer",
                   "source_message_ids": coverage.get("source_message_ids", []),
                   "remaining": coverage["remaining"]}
        revision.action_receipts = {**(revision.action_receipts or {}),
                                   "response_debt": (revision.action_receipts or {}).get("response_debt", receipt)}
        revision.recovery_state = "manual"
        revision.recovery_code = "waiting_on_customer"
        revision.recovery_due_at = None
        revision.claim_token = ""
        revision.claimed_at = None
        revision.lease_until = None
        revision.save(update_fields=["action_receipts", "recovery_state", "recovery_code", "recovery_due_at",
                                     "claim_token", "claimed_at", "lease_until", "updated_at"])
        return None
    effects = list(revision.delivery_effects.order_by("order_index", "id"))
    task = record_reply_debt(revision, reason, effects=effects, now=now)
    receipt = {"version": 1, "task_id": task.pk, "owner": "manager",
        "disposition": (task.manager_context or {}).get("disposition", "manager_reply"),
        "source_message_ids": list(revision.sources.order_by("ordinal", "id").values_list("message_id", flat=True))}
    revision.action_receipts = {**(revision.action_receipts or {}), "response_debt": (revision.action_receipts or {}).get("response_debt", receipt)}
    revision.save(update_fields=["action_receipts", "updated_at"])
    context = task.manager_context or {}
    code = ("operator_handled" if context.get("operator_review") else
            "manager_reply_confirmed" if context.get("reply_resolution") else reason)
    _pause_response_obligation(revision, code)
    return task


def resolve_delivered_reply_debt(revision, *, now=None):
    """Only the same fully delivered revision settles its technical case.

    Caller has already checked the entire receipt aggregate under row locks.
    Other revisions' debts require explicit coverage/transfer reconciliation.
    """
    if response_coverage(revision).get("remaining"):
        return 0
    safe_reply = (revision.action_receipts or {}).get("provider_safe_reply") or {}
    if safe_reply.get("reply_mode") == "neutral_ack":
        return 0
    return unresolved_reply_debts().filter(
        client_id=revision.client_id, event_key=f"ig-revision-debt:{revision.pk}",
    ).update(status=IgFollowUpTask.Status.COMPLETED, updated_at=now or timezone.now())


def review_reply_debt(client_id, task_id, *, actor, expected_revision_id, now=None):
    """Close one operator alert without claiming that a reply was delivered.

    The caller checks bot-operation and conversation permissions. Client-first
    locking serializes this action with delivery finalization and new debt.
    Sources, outbox receipts and customer reminders stay untouched. The exact
    revision relinquishes execution ownership with an operator disposition.
    """
    from django.db import transaction
    from management.models import AdminAuditLog, IgClient

    now = now or timezone.now()
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(
            pk=client_id, hidden_at__isnull=True, privacy_erasure_started_at__isnull=True,
        ).first()
        if client is None:
            return {"ok": False, "status": 404, "error": "Клієнта не знайдено."}
        task = IgFollowUpTask.objects.select_for_update().filter(
            pk=task_id, client=client, kind=IgFollowUpTask.Kind.MANAGER_TASK,
            reason=DEBT_REASON,
        ).first()
        if task is None:
            return {"ok": False, "status": 404, "error": "Сповіщення не знайдено."}
        payload = task.event_payload or {}
        if payload.get("revision_id") != expected_revision_id:
            return {"ok": False, "status": 409, "error": "Сповіщення змінилося. Оновіть картку."}
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=expected_revision_id, client=client,
        ).first()
        if task.status in (task.Status.COMPLETED, task.Status.CANCELLED):
            # Older alert-only reviews can be replayed once to release their
            # stale execution lease; an unchanged durable disposition is a no-op.
            review = (task.manager_context or {}).get("operator_review") or {}
            if revision is not None and review.get("revision_id") == expected_revision_id:
                _pause_response_obligation(revision, "operator_handled")
            return {"ok": True, "idempotent": True, "task_id": task.pk}
        before_status = task.status
        review = {"version": 1, "outcome": "reviewed_no_reply", "actor_id": actor.pk,
                  "at": now.isoformat(), "revision_id": expected_revision_id,
                  "source_message_ids": payload.get("source_message_ids") or [],
                  "reply_confirmed": False}
        task.manager_context = {**(task.manager_context or {}), "operator_review": review}
        task.status = task.Status.CANCELLED
        task.skip_reason = "operator_reviewed_no_reply"
        task.manager_approval_status = task.ManagerApprovalStatus.REJECTED
        task.manager_approval_actor = actor
        task.manager_approval_decided_at = now
        task.save(update_fields=["manager_context", "status", "skip_reason", "manager_approval_status",
                                 "manager_approval_actor", "manager_approval_decided_at", "updated_at"])
        if revision is not None:
            _pause_response_obligation(revision, "operator_handled")
        AdminAuditLog.objects.create(
            actor=actor, actor_role="staff", action="ig_reply_debt_reviewed",
            entity_type="IgFollowUpTask", entity_id=str(task.pk),
            before={"status": before_status}, after={"status": task.status, **review},
            reason="Сповіщення опрацьовано командою; доставка відповіді не підтверджується.",
        )
        return {"ok": True, "idempotent": False, "task_id": task.pk}


def confirmed_human_reply_source(command, *, lock_parts=False):
    """Read the same exact physical proof for settlement and later observation."""
    from management.services.ig_human_reply_delivery import _part_rows, _valid_plan, _current_context_reason
    from management.services.ig_human_reply_transport import has_planned_human_delivery
    from management.ig_human_reply_models import HumanReplyPart
    from management.services.ig_delivery_plan import DEFAULT_MAX_CHUNKS

    client = command.client
    if (client.hidden_at or client.privacy_erasure_started_at or command.state != command.State.SENT
        or not command.context_message_id or not command.provider_started_at or not command.terminal_at
        or not command.provider_message_ids
        or len(set(command.provider_message_ids)) != len(command.provider_message_ids)):
        return None
    source = command.context_message
    if (source is None or source.client_id != client.pk or source.role != "user"
        or source.sender_id != client.igsid or command.recipient_igsid != client.igsid
        or (source.provider_namespace and source.provider_namespace != command.provider_namespace)):
        return None
    if has_planned_human_delivery(command):
        parts = (_part_rows(command) if lock_parts else list(HumanReplyPart.objects.filter(
            command_id=command.pk).order_by("ordinal")[:DEFAULT_MAX_CHUNKS + 1]))
        if (not _valid_plan(command, parts, current_owner=False) or _current_context_reason(command)
            or any(part.state != part.State.SENT or not part.provider_started_at
                   or not part.terminal_at or not part.provider_message_id for part in parts)
            or [part.provider_message_id for part in parts] != command.provider_message_ids):
            return None
    else:
        reply = command.reply_message
        if (reply is None or reply.client_id != client.pk or reply.role != "manager"
            or reply.source != "human_reply" or reply.status != "done" or reply.send_state != "sent"
            or reply.provider_namespace != command.provider_namespace or reply.text != command.text
            or reply.delivery_provider_message_ids != command.provider_message_ids
            or reply.provider_message_id != command.provider_message_ids[0]
            or command.context_revision != str(source.pk)):
            return None
    return source


def settle_human_reply_obligation(command_id, *, now=None):
    """Settle only a confirmed human command's exact single-source reply debt.

    A later message is not evidence that unrelated questions were answered.
    Ambiguous bot transport remains a separate reconciliation obligation even
    when a manager subsequently sends a useful answer.
    """
    from management.models import AdminAuditLog, InstagramBotMessage
    from management.ig_bot_models import HumanReplyCommand

    identity = HumanReplyCommand.objects.filter(pk=command_id).values("client_id").first()
    if identity is None:
        return 0
    now = now or timezone.now()
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        command = HumanReplyCommand.objects.select_for_update().select_related("context_message", "reply_message").filter(pk=command_id).first()
        if client is None or command is None:
            return 0
        command.client = client
        source = confirmed_human_reply_source(command, lock_parts=True)
        if source is None:
            return 0
        tasks = list(unresolved_reply_debts().filter(
            client=client, event_payload__source_message_ids=[source.pk],
        ).order_by("pk"))
        settled = 0
        for candidate in tasks:
            revision_id = (candidate.event_payload or {}).get("revision_id")
            revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id, client=client).first()
            task = IgFollowUpTask.objects.select_for_update().get(pk=candidate.pk)
            if (revision is None or task.event_key != f"ig-revision-debt:{revision.pk}"
                or list(revision.sources.order_by("ordinal", "id").values_list("message_id", flat=True)) != [source.pk]
                or task.status in (task.Status.COMPLETED, task.Status.CANCELLED)
                or (task.event_payload or {}).get("reason") in {
                    "delivery_unknown", "provider_started_unreconciled", "recovery_delivery_reconciliation",
                }
                or revision.delivery_effects.filter(state__in=("unknown", "provider_started", "claimed")).exists()
                or InstagramBotMessage.objects.filter(pk=source.pk, send_state__in=("unknown", "sending", "ambiguous")).exists()):
                continue
            resolution = {"version": 1, "outcome": "manager_replied", "command_id": command.pk,
                          "source_message_ids": [source.pk], "provider_message_ids": command.provider_message_ids,
                          "reply_confirmed": True, "at": now.isoformat()}
            task.manager_context = {**(task.manager_context or {}), "reply_resolution": resolution}
            task.status = task.Status.COMPLETED
            task.skip_reason = "manager_reply_confirmed"
            task.save(update_fields=["manager_context", "status", "skip_reason", "updated_at"])
            _pause_response_obligation(revision, "manager_reply_confirmed")
            AdminAuditLog.objects.create(actor=command.actor, actor_role="staff",
                action="ig_reply_debt_manager_replied", entity_type="IgFollowUpTask", entity_id=str(task.pk),
                after=resolution, reason="Confirmed human reply to the exact customer source.")
            settled += 1
        from management.services.ig_revision_rate_handoff import settle_rate_handoff_source
        return settled + settle_rate_handoff_source(command, source, now=now)


def with_reply_debt(queryset):
    debts = unresolved_reply_debts().filter(client_id=OuterRef("pk")).order_by("event_occurred_at", "id")
    counts = debts.order_by().values("client_id").annotate(total=Count("pk"))
    return queryset.annotate(
        # Keep the legacy clock until already-open D074 pages are retired:
        # changing its MariaDB wall time to UTC would freeze their ordering.
        reply_debt_observed_at=Now(),
        reply_debt_observation_cursor=UtcObservationNow(),
        has_reply_debt=Exists(debts),
        reply_debt_count=Subquery(counts.values("total")[:1]),
        reply_debt_task_id=Subquery(debts.values("pk")[:1]),
        reply_debt_since=Subquery(debts.values("event_occurred_at")[:1]),
        reply_debt_payload=Subquery(debts.values("event_payload")[:1]),
    )


def reply_debt_payload(client):
    if not hasattr(client, "has_reply_debt"):
        client = with_reply_debt(type(client).objects.filter(pk=client.pk)).first()
    observed_at = getattr(client, "reply_debt_observed_at", None)
    observation = {"observed_at": observed_at.isoformat()} if observed_at else {}
    cursor = getattr(client, "reply_debt_observation_cursor", None)
    if cursor:
        observation["observation_cursor"] = cursor.isoformat()
    if client is None or not client.has_reply_debt:
        return {"required": False, **observation}
    payload = client.reply_debt_payload or {}
    reason = str(payload.get("reason") or "reply_unresolved")
    return {"required": True, **observation, "owner": "manager", "count": client.reply_debt_count or 1,
        "label": "Потрібна відповідь команди", "reason": reason,
        "reason_label": REASON_LABELS.get(reason, "Запит залишився без підтвердженої відповіді"),
        "since": client.reply_debt_since.isoformat() if client.reply_debt_since else "",
        "task_id": client.reply_debt_task_id, "revision_id": payload.get("revision_id"),
        "source_message_ids": payload.get("source_message_ids") or [],
        "next_action": "Перевірити незавершений запит і відповісти клієнту"}
