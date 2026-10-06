"""Transactional operator reset for an Instagram client's mutable CRM state."""
from __future__ import annotations

import uuid

from django.db import transaction
from django.utils import timezone

from management.models import IgClient, InstagramBotMessage
from management.ig_bot_models import (
    IgCommercialEpisode,
    IgConversationAnalysisJob,
    IgFollowCtaDecision,
    IgFunnelResetAudit,
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


def _preserved_sales_context(client) -> dict:
    try:
        from management.services.bot_memory import preserved_profile

        return preserved_profile(client)
    except Exception:
        return {}


def reset_funnel(*, client_id: int, actor, reason: str = "manual_reset") -> dict:
    """Reset mutable CRM inference while retaining the immutable transcript/truth.

    The caller must be an operator and the endpoint supplies the explicit
    confirmation. Hidden clients are deliberately excluded from this flow.
    """
    reason = (reason or "manual_reset").strip()[:255]
    with pause_reply_boundary():
        with transaction.atomic():
            client = (
                IgClient.objects.select_for_update()
                .select_related("current_commercial_episode")
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
            if verified:
                resulting_stage = (
                    IgClient.Stage.ORDER_CREATED if has_order else IgClient.Stage.PAID
                )
            else:
                resulting_stage = IgClient.Stage.NEW

            # A reset is a hard processing boundary. Existing inbound rows are
            # retained for the transcript but must never be answered later.
            InstagramBotMessage.objects.filter(
                client=client,
                role=InstagramBotMessage.Role.USER,
                status__in=(
                    InstagramBotMessage.Status.PENDING,
                    InstagramBotMessage.Status.PROCESSING,
                ),
            ).exclude(send_state="sending").update(
                status=InstagramBotMessage.Status.DONE,
                processed_at=now,
                processing_started_at=None,
            )
            cancelled_followups = bot_followups.cancel_pending(
                client, reason="funnel_reset"
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
                "sales_context": _preserved_sales_context(client),
                "automation_lease_token": "",
                "automation_lease_until": None,
                # Invalidate a worker that generated before the reset. The
                # pause state itself is intentionally preserved.
                "reply_permission_epoch": int(client.reply_permission_epoch or 0) + 1,
                "updated_at": now,
            }
            for field, value in update.items():
                setattr(client, field, value)
            client.save(update_fields=[*update.keys()])

            current_episode = client.current_commercial_episode
            if verified and current_episode and current_episode.open_slot == 1:
                current_episode.opened_watermark_message_id = boundary
                current_episode.save(update_fields=[
                    "opened_watermark_message_id", "updated_at",
                ])
            elif not verified:
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

            audit = IgFunnelResetAudit.objects.create(
                client=client,
                reset_after_message_id=boundary,
                previous_state=previous_state,
                resulting_stage=resulting_stage,
                reason=reason,
                actor=actor if getattr(actor, "pk", None) else None,
            )
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
    }
