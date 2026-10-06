"""Durable, bounded reply-permission transitions for HTTP ingress."""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone

from management.models import (
    IgAiReplyRecoveryJob,
    IgClient,
    IgCommerceTurnDecision,
    IgFollowUpTask,
    IgPermissionTransitionJob,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services import ig_reply_boundary


logger = logging.getLogger("management.ig_permission_transitions")

JOB_LEASE_DURATION = timedelta(minutes=2)
HTTP_JOB_LEASE_DURATION = timedelta(seconds=5)
MAX_ATTEMPTS = 5
WEB_LOCK_TIMEOUT_SECONDS = 0.25
ACTIVE_STATUSES = frozenset({
    IgPermissionTransitionJob.Status.PENDING,
    IgPermissionTransitionJob.Status.PROCESSING,
    IgPermissionTransitionJob.Status.FAILED,
})


def active_permission_transition_exists(
    *,
    settings_id: int | None = None,
    client_id: int | None = None,
    kinds: list[str] | tuple[str, ...] | None = None,
) -> bool:
    rows = IgPermissionTransitionJob.objects.filter(status__in=ACTIVE_STATUSES)
    if settings_id is not None:
        rows = rows.filter(settings_id=settings_id)
    if client_id is not None:
        rows = rows.filter(client_id=client_id)
    if kinds:
        rows = rows.filter(kind__in=kinds)
    return rows.exists()


def supersede_permission_transitions(
    *,
    settings_id: int | None = None,
    client_id: int | None = None,
    kinds: list[str] | tuple[str, ...] | None = None,
) -> int:
    """Cancel active transitions after an authoritative operator reversal."""
    rows = IgPermissionTransitionJob.objects.filter(status__in=ACTIVE_STATUSES)
    if settings_id is not None:
        rows = rows.filter(settings_id=settings_id)
    if client_id is not None:
        rows = rows.filter(client_id=client_id)
    if kinds:
        rows = rows.filter(kind__in=kinds)
    now = timezone.now()
    return rows.update(
        status=IgPermissionTransitionJob.Status.SUPERSEDED,
        next_attempt_at=None,
        lease_token="",
        lease_until=None,
        last_error_kind="",
        completed_at=now,
        updated_at=now,
    )


def permission_transition_blocks(*, settings_id: int | None, client_id: int | None) -> bool:
    filters = Q()
    if settings_id:
        filters |= Q(
            kind=IgPermissionTransitionJob.Kind.GLOBAL_PAUSE,
            settings_id=settings_id,
        )
    if client_id:
        filters |= Q(client_id=client_id)
    if not filters:
        return False
    return IgPermissionTransitionJob.objects.filter(
        filters,
        status__in=ACTIVE_STATUSES,
    ).exists()


def create_permission_transition(
    *,
    kind: str,
    dedupe_key: str,
    client: IgClient | None = None,
    settings: InstagramBotSettings | None = None,
    source_message: InstagramBotMessage | None = None,
) -> IgPermissionTransitionJob:
    """Persist the fail-closed marker before any file-lock acquisition."""
    job, created = IgPermissionTransitionJob.objects.get_or_create(
        dedupe_key=str(dedupe_key)[:255],
        defaults={
            "kind": kind,
            "client": client,
            "settings": settings,
            "source_message": source_message,
            "status": IgPermissionTransitionJob.Status.PENDING,
            "next_attempt_at": timezone.now(),
        },
    )
    if not created:
        expected = (
            kind,
            getattr(client, "pk", None),
            getattr(settings, "pk", None),
            getattr(source_message, "pk", None),
        )
        actual = (job.kind, job.client_id, job.settings_id, job.source_message_id)
        if actual != expected:
            raise ValueError("permission transition dedupe ownership mismatch")
    return job


def _claim_job(
    job_id: int | None = None,
    *,
    nowait: bool = False,
    lease_duration: timedelta = JOB_LEASE_DURATION,
) -> IgPermissionTransitionJob | None:
    now = timezone.now()
    due = (
        Q(
            status__in=[
                IgPermissionTransitionJob.Status.PENDING,
                IgPermissionTransitionJob.Status.FAILED,
            ],
            next_attempt_at__isnull=False,
            next_attempt_at__lte=now,
        )
        | Q(
            status=IgPermissionTransitionJob.Status.PROCESSING,
            lease_until__lt=now,
        )
    )
    with transaction.atomic():
        rows = IgPermissionTransitionJob.objects.select_for_update(
            nowait=nowait
        ).filter(due)
        if job_id is not None:
            rows = rows.filter(pk=job_id)
        job = rows.order_by("next_attempt_at", "id").first()
        if job is None:
            return None
        job.status = IgPermissionTransitionJob.Status.PROCESSING
        job.lease_token = secrets.token_hex(16)
        job.lease_until = now + lease_duration
        job.attempts = int(job.attempts or 0) + 1
        job.last_error_kind = ""
        job.save(update_fields=[
            "status",
            "lease_token",
            "lease_until",
            "attempts",
            "last_error_kind",
            "updated_at",
        ])
        return job


def _mark_retry(
    job: IgPermissionTransitionJob,
    *,
    error_kind: str,
    retry_delay_seconds: int | None = None,
    nowait: bool = False,
) -> None:
    now = timezone.now()
    exhausted = int(job.attempts or 0) >= MAX_ATTEMPTS
    values = {
        "status": (
            IgPermissionTransitionJob.Status.FAILED
            if exhausted
            else IgPermissionTransitionJob.Status.PENDING
        ),
        "next_attempt_at": (
            None
            if exhausted
            else now + timedelta(
                seconds=(
                    min(30, 2 ** job.attempts)
                    if retry_delay_seconds is None
                    else max(0, int(retry_delay_seconds))
                )
            )
        ),
        "lease_token": "",
        "lease_until": None,
        "last_error_kind": str(error_kind or "transition_error")[:64],
        "updated_at": now,
    }
    if not nowait:
        IgPermissionTransitionJob.objects.filter(
            pk=job.pk,
            status=IgPermissionTransitionJob.Status.PROCESSING,
            lease_token=job.lease_token,
        ).update(**values)
        return
    with transaction.atomic():
        current = (
            IgPermissionTransitionJob.objects.select_for_update(nowait=True)
            .filter(
                pk=job.pk,
                status=IgPermissionTransitionJob.Status.PROCESSING,
                lease_token=job.lease_token,
            )
            .first()
        )
        if current is not None:
            for field, value in values.items():
                setattr(current, field, value)
            current.save(update_fields=[*values])


def _finish_job(job, *, status: str, now) -> None:
    job.status = status
    job.next_attempt_at = None
    job.lease_token = ""
    job.lease_until = None
    job.last_error_kind = ""
    job.completed_at = now
    job.save(update_fields=[
        "status",
        "next_attempt_at",
        "lease_token",
        "lease_until",
        "last_error_kind",
        "completed_at",
        "updated_at",
    ])


def _bind_source_message(
    job: IgPermissionTransitionJob,
    client: IgClient,
    *,
    nowait: bool,
    required: bool,
) -> InstagramBotMessage | None:
    source = None
    if job.source_message_id:
        source = (
            InstagramBotMessage.objects.select_for_update(nowait=nowait)
            .filter(pk=job.source_message_id)
            .first()
        )
    if source is None:
        if required:
            raise RuntimeError("permission_transition_source_missing")
        return None
    if source.sender_id != client.igsid or source.client_id not in {None, client.pk}:
        raise RuntimeError("permission_transition_source_client_mismatch")
    if source.client_id is None:
        source.client_id = client.pk
        source.save(update_fields=["client"])
    if source.client_id != client.pk:
        raise RuntimeError("permission_transition_source_not_bound")
    return source


def _finish_source_message(source: InstagramBotMessage | None, *, now) -> None:
    if source is None:
        return
    update_fields = []
    if source.status != InstagramBotMessage.Status.DONE:
        source.status = InstagramBotMessage.Status.DONE
        update_fields.append("status")
    if source.processing_started_at is not None:
        source.processing_started_at = None
        update_fields.append("processing_started_at")
    if source.processed_at is None:
        source.processed_at = now
        update_fields.append("processed_at")
    if update_fields:
        source.save(update_fields=update_fields)


def _locked_ids(rows, *, nowait: bool) -> list[int]:
    return list(
        rows.select_for_update(nowait=nowait)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def cancel_client_unstarted_automation(
    client: IgClient,
    *,
    reason: str,
    now,
    nowait: bool,
) -> dict[str, int]:
    """Cancel only automation that has not crossed an execution/send boundary.

    Manager work and anything that crossed the provider-send boundary, was
    partially delivered, or has an ambiguous result remains durable for
    reconciliation.  Unsent processing claims are cancelled after the caller
    has acquired the reply boundary.  Analytical and entitlement records are
    outside this send-cancellation scope.
    """
    cancelled = {
        "followups": 0,
        "inbound_rows": 0,
        "recovery_jobs": 0,
        "commerce_decisions": 0,
    }
    followups = IgFollowUpTask.objects.filter(
        client_id=client.pk,
        status=IgFollowUpTask.Status.PENDING,
    ).exclude(kind=IgFollowUpTask.Kind.MANAGER_TASK)
    followup_ids = _locked_ids(followups, nowait=nowait)
    if followup_ids:
        cancelled["followups"] = IgFollowUpTask.objects.filter(
            pk__in=followup_ids
        ).update(
            status=IgFollowUpTask.Status.CANCELLED,
            skip_reason=reason[:255],
            updated_at=now,
        )
    messages = InstagramBotMessage.objects.filter(
        client_id=client.pk,
        role=InstagramBotMessage.Role.USER,
        status__in=(
            InstagramBotMessage.Status.PENDING,
            InstagramBotMessage.Status.PROCESSING,
        ),
    ).exclude(send_state__in=("sending", "unknown", "ambiguous", "sent"))
    message_ids = _locked_ids(messages, nowait=nowait)
    if message_ids:
        cancelled["inbound_rows"] = InstagramBotMessage.objects.filter(
            pk__in=message_ids
        ).update(
            status=InstagramBotMessage.Status.DONE,
            processed_at=now,
            processing_started_at=None,
        )

    recoveries = IgAiReplyRecoveryJob.objects.filter(
        client_id=client.pk,
        status__in=(
            IgAiReplyRecoveryJob.Status.PENDING,
            IgAiReplyRecoveryJob.Status.PROCESSING,
        ),
        sending_started_at__isnull=True,
    ).exclude(
        reply_message__send_state__in=("sending", "unknown", "ambiguous", "sent")
    )
    recovery_ids = _locked_ids(recoveries, nowait=nowait)
    if recovery_ids:
        cancelled["recovery_jobs"] = IgAiReplyRecoveryJob.objects.filter(
            pk__in=recovery_ids,
            status__in=(
                IgAiReplyRecoveryJob.Status.PENDING,
                IgAiReplyRecoveryJob.Status.PROCESSING,
            ),
            sending_started_at__isnull=True,
        ).update(
            status=IgAiReplyRecoveryJob.Status.CANCELLED,
            active_cursor_key=None,
            next_attempt_at=None,
            lease_token="",
            lease_until=None,
            completed_at=now,
            last_error=reason[:1000],
            updated_at=now,
        )

    client_message_ids = InstagramBotMessage.objects.filter(
        client_id=client.pk
    ).values("pk")
    commerce = IgCommerceTurnDecision.objects.filter(
        source_message_id__in=client_message_ids,
        delivery_state=IgCommerceTurnDecision.DeliveryState.PENDING,
        attempts=0,
        delivery_started_at__isnull=True,
    )
    commerce_ids = _locked_ids(commerce, nowait=nowait)
    if commerce_ids:
        cancelled["commerce_decisions"] = IgCommerceTurnDecision.objects.filter(
            pk__in=commerce_ids,
            delivery_state=IgCommerceTurnDecision.DeliveryState.PENDING,
            attempts=0,
            delivery_started_at__isnull=True,
        ).update(
            delivery_state=IgCommerceTurnDecision.DeliveryState.NOT_REQUIRED,
            delivery_error=reason[:1000],
            updated_at=now,
        )
    if client.next_followup_at is not None:
        client.next_followup_at = None
        client.save(update_fields=["next_followup_at", "updated_at"])
    # Незавершений епізод деградації теж є автоматизацією: після takeover або
    # opt-out він не має права ані надіслати holding, ані відновити відповідь.
    protected_recovery_exists = IgAiReplyRecoveryJob.objects.filter(
        client_id=client.pk,
    ).filter(
        Q(status__in=(
            IgAiReplyRecoveryJob.Status.SENDING,
            IgAiReplyRecoveryJob.Status.AMBIGUOUS,
        ))
        | Q(sending_started_at__isnull=False)
        | Q(reply_message__send_state__in=("sending", "unknown", "ambiguous", "sent"))
    ).exists()
    if not protected_recovery_exists:
        try:
            from management.services.ig_provider_incidents import (
                cancel_episodes_for_client,
            )

            cancel_episodes_for_client(client.pk, reason=reason)
        except Exception:
            logger.debug("degradation episode cancellation unavailable", exc_info=True)
    return cancelled


def _cancel_client_automation(
    client: IgClient,
    *,
    reason: str,
    now,
    nowait: bool,
) -> None:
    cancel_client_unstarted_automation(
        client,
        reason=reason,
        now=now,
        nowait=nowait,
    )


def _cancel_global_automation(*, now, nowait: bool) -> None:
    messages = InstagramBotMessage.objects.filter(
        role=InstagramBotMessage.Role.USER,
        status__in=[
            InstagramBotMessage.Status.PENDING,
            InstagramBotMessage.Status.PROCESSING,
        ],
    ).exclude(send_state="sending")
    message_ids = _locked_ids(messages, nowait=nowait)
    if message_ids:
        InstagramBotMessage.objects.filter(pk__in=message_ids).update(
            status=InstagramBotMessage.Status.DONE,
            processed_at=now,
            processing_started_at=None,
        )
    followups = IgFollowUpTask.objects.filter(
        status=IgFollowUpTask.Status.PENDING,
    ).exclude(
        kind=IgFollowUpTask.Kind.MANAGER_TASK,
        reason="followup_delivery_review",
    )
    followup_ids = _locked_ids(followups, nowait=nowait)
    if followup_ids:
        IgFollowUpTask.objects.filter(pk__in=followup_ids).update(
            status=IgFollowUpTask.Status.CANCELLED,
            skip_reason="global_reply_stopped",
            updated_at=now,
        )
    clients = IgClient.objects.filter(next_followup_at__isnull=False)
    client_ids = _locked_ids(clients, nowait=nowait)
    if client_ids:
        IgClient.objects.filter(pk__in=client_ids).update(next_followup_at=None)


def _apply_claimed_job(
    claimed: IgPermissionTransitionJob,
    *,
    lock_path: str | None = None,
    timeout_seconds: float | None = None,
    nowait: bool = False,
) -> bool:
    boundary_kwargs = {}
    if lock_path is not None:
        boundary_kwargs["lock_path"] = lock_path
    if timeout_seconds is not None:
        boundary_kwargs["timeout_seconds"] = timeout_seconds

    with ig_reply_boundary.pause_reply_boundary(**boundary_kwargs):
        with transaction.atomic():
            job = IgPermissionTransitionJob.objects.select_for_update(
                nowait=nowait
            ).filter(
                pk=claimed.pk,
                status=IgPermissionTransitionJob.Status.PROCESSING,
                lease_token=claimed.lease_token,
            ).first()
            if job is None:
                return False
            now = timezone.now()
            if job.kind == IgPermissionTransitionJob.Kind.OPT_OUT:
                client = IgClient.objects.select_for_update(nowait=nowait).filter(
                    pk=job.client_id
                ).first()
                if client is None:
                    _finish_job(job, status=IgPermissionTransitionJob.Status.SUPERSEDED, now=now)
                    return True
                source = _bind_source_message(
                    job,
                    client,
                    nowait=nowait,
                    required=True,
                )
                _finish_source_message(source, now=now)
                already_applied = bool(
                    client.opted_out_at
                    and client.opt_out_message_id == source.pk
                    and client.bot_paused
                    and client.paused_reason == "opt_out"
                )
                if already_applied:
                    _cancel_client_automation(
                        client,
                        reason="opt_out",
                        now=now,
                        nowait=nowait,
                    )
                    _finish_job(job, status=IgPermissionTransitionJob.Status.APPLIED, now=now)
                    return True
                if client.opted_in_at and source.provider_created_at and client.opted_in_at >= source.provider_created_at:
                    _finish_job(job, status=IgPermissionTransitionJob.Status.SUPERSEDED, now=now)
                    return True
                client.opted_out_at = source.provider_created_at or source.created_at or now
                client.opt_out_message_id = source.pk
                client.bot_paused = True
                client.reply_permission_epoch = int(client.reply_permission_epoch or 0) + 1
                client.paused_reason = "opt_out"
                client.paused_at = client.paused_at or now
                if client.first_contact_at is None:
                    client.first_contact_at = now
                client.last_message_at = now
                # Opt-out приходит входящим сообщением клиента, поэтому якорь
                # окна Meta двигается вместе с ним (Э2.6).
                client.last_user_message_at = now
                client.save(update_fields=[
                    "opted_out_at",
                    "opt_out_message_id",
                    "bot_paused",
                    "reply_permission_epoch",
                    "paused_reason",
                    "paused_at",
                    "first_contact_at",
                    "last_message_at",
                    "last_user_message_at",
                    "updated_at",
                ])
                _cancel_client_automation(
                    client,
                    reason="opt_out",
                    now=now,
                    nowait=nowait,
                )
                try:
                    from management.services.ig_funnel_analytics import (
                        record_client_step_event_in_transaction,
                        record_drop_off_for_client_in_transaction,
                    )
                    from management.models import IgFunnelStepEvent

                    occurred_at = source.provider_created_at or source.created_at or now
                    record_client_step_event_in_transaction(
                        client,
                        event_type=IgFunnelStepEvent.Type.CONVERSATION_STARTED,
                        event_key=f"ig-inbound:{source.pk}",
                        occurred_at=occurred_at,
                        stage=client.stage,
                        actor="customer",
                        evidence={
                            "message_id": source.pk,
                            "mid": source.mid or "",
                            "source": source.source,
                        },
                    )
                    record_drop_off_for_client_in_transaction(
                        client,
                        kind="opt_out",
                        reason_code="explicit_opt_out",
                        occurred_at=occurred_at,
                        stage=client.stage,
                        actor="customer",
                        evidence={"message_id": source.pk},
                        is_recoverable=False,
                    )
                except Exception:
                    pass
            elif job.kind == IgPermissionTransitionJob.Kind.MANAGER_TAKEOVER:
                client = IgClient.objects.select_for_update(nowait=nowait).filter(
                    pk=job.client_id
                ).first()
                if client is None:
                    _finish_job(job, status=IgPermissionTransitionJob.Status.SUPERSEDED, now=now)
                    return True
                source = _bind_source_message(
                    job,
                    client,
                    nowait=nowait,
                    required=bool(job.source_message_id),
                )
                _finish_source_message(source, now=now)
                transition_at = (
                    getattr(source, "provider_created_at", None)
                    or getattr(source, "created_at", None)
                    or now
                )
                takeover_started = not (client.manager_takeover and client.bot_paused)
                if takeover_started:
                    client.manager_takeover = True
                    client.bot_paused = True
                    client.reply_permission_epoch = int(client.reply_permission_epoch or 0) + 1
                    client.paused_reason = "manager_takeover"
                    client.paused_at = transition_at
                if (
                    client.last_manager_message_at is None
                    or client.last_manager_message_at < transition_at
                ):
                    client.last_manager_message_at = transition_at
                client.save(update_fields=[
                    "manager_takeover",
                    "bot_paused",
                    "reply_permission_epoch",
                    "paused_reason",
                    "paused_at",
                    "last_manager_message_at",
                    "updated_at",
                ])
                _cancel_client_automation(
                    client,
                    reason="manager_takeover",
                    now=now,
                    nowait=nowait,
                )
                if source is not None:
                    try:
                        from management.services.bot_conversation_analysis import schedule_analysis

                        schedule_analysis(client, source, trigger="manager_message")
                    except Exception:
                        # Queue durability is part of the manager takeover
                        # boundary. Let the caller roll back the staged echo
                        # and return a provider retry instead of applying a
                        # partial takeover.
                        raise
                if takeover_started:
                    try:
                        from management.models import IgFunnelStepEvent
                        from management.services.ig_funnel_analytics import (
                            record_client_step_event_in_transaction,
                        )

                        record_client_step_event_in_transaction(
                            client,
                            event_type=IgFunnelStepEvent.Type.MANAGER_ENGAGED,
                            event_key=f"ig-manager-engaged:{client.pk}:{job.pk}",
                            occurred_at=transition_at,
                            stage=client.stage,
                            actor="manager",
                            evidence={
                                "manager_message_id": source.pk if source else None,
                                "provider_mid": source.mid if source else "",
                                "takeover_started": True,
                            },
                        )
                    except Exception:
                        pass
                    from management.services.instagram_bot import notify_manager
                    from management.services.ig_alerts import format_technical_alert

                    notification_persisted = notify_manager(
                        format_technical_alert(
                            "👤 IG: менеджер підключився; бот поставлено на паузу",
                            event_type="takeover",
                            client_id=client.pk,
                            message_id=source.pk if source else None,
                            job_id=job.pk,
                            instruction_code="permission_takeover",
                        ),
                        dedupe_key=f"takeover:{client.pk}:{job.pk}",
                        event_type="takeover",
                        client=client,
                        deliver_immediately=False,
                    )
                    if not notification_persisted:
                        raise RuntimeError(
                            "manager takeover notification was not persisted"
                        )
            elif job.kind == IgPermissionTransitionJob.Kind.CLIENT_PAUSE:
                client = IgClient.objects.select_for_update(nowait=nowait).filter(
                    pk=job.client_id
                ).first()
                if client is None:
                    _finish_job(job, status=IgPermissionTransitionJob.Status.SUPERSEDED, now=now)
                    return True
                if client.bot_paused and client.paused_reason == "manual":
                    _finish_job(job, status=IgPermissionTransitionJob.Status.APPLIED, now=now)
                    return True
                client.bot_paused = True
                client.reply_permission_epoch = int(client.reply_permission_epoch or 0) + 1
                client.paused_reason = "manual"
                client.paused_at = now
                client.save(update_fields=[
                    "bot_paused",
                    "reply_permission_epoch",
                    "paused_reason",
                    "paused_at",
                    "updated_at",
                ])
                _cancel_client_automation(
                    client,
                    reason="manual_pause",
                    now=now,
                    nowait=nowait,
                )
            elif job.kind == IgPermissionTransitionJob.Kind.GLOBAL_PAUSE:
                settings_obj = InstagramBotSettings.objects.select_for_update(
                    nowait=nowait
                ).filter(
                    pk=job.settings_id
                ).first()
                if settings_obj is None:
                    _finish_job(job, status=IgPermissionTransitionJob.Status.SUPERSEDED, now=now)
                    return True
                if settings_obj.is_enabled:
                    settings_obj.is_enabled = False
                    settings_obj.reply_permission_epoch = int(settings_obj.reply_permission_epoch or 0) + 1
                    settings_obj.last_stopped_at = now
                    settings_obj.save(update_fields=[
                        "is_enabled",
                        "reply_permission_epoch",
                        "last_stopped_at",
                    ])
                _cancel_global_automation(now=now, nowait=nowait)
            else:
                _finish_job(job, status=IgPermissionTransitionJob.Status.SUPERSEDED, now=now)
                return True
            _finish_job(job, status=IgPermissionTransitionJob.Status.APPLIED, now=now)
    return True


def attempt_permission_transition(
    job_id: int,
    *,
    lock_path: str | None = None,
    timeout_seconds: float = WEB_LOCK_TIMEOUT_SECONDS,
) -> bool:
    try:
        claimed = _claim_job(
            job_id,
            nowait=True,
            lease_duration=HTTP_JOB_LEASE_DURATION,
        )
    except DatabaseError:
        return False
    if claimed is None:
        return False
    try:
        return _apply_claimed_job(
            claimed,
            lock_path=lock_path,
            timeout_seconds=timeout_seconds,
            nowait=True,
        )
    except ig_reply_boundary.ReplyBoundaryTimeout:
        _mark_retry(
            claimed,
            error_kind="reply_boundary_busy",
            retry_delay_seconds=0,
        )
        return False
    except DatabaseError:
        try:
            _mark_retry(
                claimed,
                error_kind="database_busy",
                retry_delay_seconds=0,
                nowait=True,
            )
        except DatabaseError:
            pass
        return False
    except Exception as exc:
        _mark_retry(claimed, error_kind=exc.__class__.__name__)
        raise


def process_due_permission_transitions(
    *,
    limit: int = 10,
    lock_path: str | None = None,
) -> int:
    processed = 0
    for _index in range(max(0, int(limit))):
        claimed = _claim_job()
        if claimed is None:
            break
        try:
            if _apply_claimed_job(claimed, lock_path=lock_path):
                processed += 1
        except Exception as exc:
            _mark_retry(claimed, error_kind=exc.__class__.__name__)
    return processed


def permission_transition_snapshot(*, compact=False) -> dict[str, object]:
    """Return redacted operational state for the management status surface."""
    rows = IgPermissionTransitionJob.objects.all()
    if compact:
        return {
            "global_pause_pending": rows.filter(
                kind=IgPermissionTransitionJob.Kind.GLOBAL_PAUSE,
                status__in=ACTIVE_STATUSES,
            ).exists(),
            "pending": None, "processing": None, "failed": None,
            "error_kinds": None, "detail_coverage_complete": False,
        }
    counts = {
        status: rows.filter(status=status).count()
        for status in (
            IgPermissionTransitionJob.Status.PENDING,
            IgPermissionTransitionJob.Status.PROCESSING,
            IgPermissionTransitionJob.Status.FAILED,
        )
    }
    error_kinds = sorted(
        set(
            rows.filter(status__in=ACTIVE_STATUSES)
            .exclude(last_error_kind="")
            .values_list("last_error_kind", flat=True)
        )
    )
    global_pause_pending = rows.filter(
        kind=IgPermissionTransitionJob.Kind.GLOBAL_PAUSE,
        status__in=ACTIVE_STATUSES,
    ).exists()
    return {
        "pending": counts[IgPermissionTransitionJob.Status.PENDING],
        "processing": counts[IgPermissionTransitionJob.Status.PROCESSING],
        "failed": counts[IgPermissionTransitionJob.Status.FAILED],
        "error_kinds": error_kinds,
        "global_pause_pending": global_pause_pending,
    }
