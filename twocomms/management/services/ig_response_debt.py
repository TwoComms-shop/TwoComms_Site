"""Operator-owned reply debt, separate from customer reminder delivery.

Reuse the existing durable revision case identity. A parked execution is not an
answered message, and a later unrelated reply is not proof that this debt ended.
"""
from django.db.models import Count, Exists, OuterRef, Subquery
from django.db.models.functions import Now
from django.utils import timezone

from management.models import IgFollowUpTask


class UtcObservationNow(Now):
    """Statement observation time independent of the MariaDB session timezone."""

    def as_mysql(self, compiler, connection, **extra_context):
        return self.as_sql(compiler, connection, template="UTC_TIMESTAMP(6)", **extra_context)


DEBT_REASON = "revision_case:execution_debt"
DEBT_KIND = "revision_execution_debt"
REASON_LABELS = {
    "provider_dispatch_budget": "Потрібна відповідь команди: ліміт підготовки відповіді вичерпано",
    "provider_candidates_exhausted": "Не вдалося підготувати відповідь після кількох спроб",
    "generation_failed": "Бот не зміг підготувати відповідь",
    "generation_outcome_unresolved": "Результат підготовки відповіді потребує звірки",
    "generation_result_missing": "Підготовлена відповідь не збережена для відправлення",
    "generation_not_started": "Підготовка відповіді не почалася вчасно",
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


def record_reply_debt(revision, reason, *, effects=(), now=None):
    """Called with the client/revision locked; never send or acknowledge a source."""
    now = now or timezone.now()
    source_ids = list(revision.sources.order_by("ordinal", "id").values_list("message_id", flat=True))
    effects = list(effects)
    disposition = "reconcile_delivery" if effects else "manager_reply"
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
    return task


def park_manual_revision(revision, reason, *, now=None):
    """Relinquish the execution lease without inventing reply completion.

    State remains compatible with receipt finalization. recovery_state=manual
    and this receipt are the disposition; CLAIMED without a token is not work
    in progress. Keep active head/source history for explicit successor transfer.
    """
    now = now or timezone.now()
    effects = list(revision.delivery_effects.order_by("order_index", "id"))
    task = record_reply_debt(revision, reason, effects=effects, now=now)
    receipt = {"version": 1, "task_id": task.pk, "owner": "manager",
        "disposition": "reconcile_delivery" if effects else "manager_reply",
        "source_message_ids": list(revision.sources.order_by("ordinal", "id").values_list("message_id", flat=True))}
    revision.action_receipts = {**(revision.action_receipts or {}), "response_debt": (revision.action_receipts or {}).get("response_debt", receipt)}
    revision.claim_token = ""
    revision.claimed_at = None
    revision.lease_until = None
    revision.save(update_fields=["action_receipts", "claim_token", "claimed_at", "lease_until", "updated_at"])
    return task


def resolve_delivered_reply_debt(revision, *, now=None):
    """Only the same fully delivered revision settles its technical case.

    Caller has already checked the entire receipt aggregate under row locks.
    Other revisions' debts require explicit coverage/transfer reconciliation.
    """
    return unresolved_reply_debts().filter(
        client_id=revision.client_id, event_key=f"ig-revision-debt:{revision.pk}",
    ).update(status=IgFollowUpTask.Status.COMPLETED, updated_at=now or timezone.now())


def review_reply_debt(client_id, task_id, *, actor, expected_revision_id, now=None):
    """Close one operator alert without claiming that a reply was delivered.

    The caller checks bot-operation and conversation permissions. Client-first
    locking serializes this action with delivery finalization and new debt.
    Sources, revisions, outbox receipts and customer reminders stay untouched.
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
        if task.status in (task.Status.COMPLETED, task.Status.CANCELLED):
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
        AdminAuditLog.objects.create(
            actor=actor, actor_role="staff", action="ig_reply_debt_reviewed",
            entity_type="IgFollowUpTask", entity_id=str(task.pk),
            before={"status": before_status}, after={"status": task.status, **review},
            reason="Сповіщення опрацьовано командою; доставка відповіді не підтверджується.",
        )
        return {"ok": True, "idempotent": False, "task_id": task.pk}


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
