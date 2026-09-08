"""Explicit, client-scoped return from manager ownership to automation."""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from management.models import (
    AdminAuditLog,
    IgAiReplyRecoveryJob,
    IgClient,
    IgCommerceTurnDecision,
    IgCustomerTurn,
    IgPermissionTransitionJob,
    IgRevisionDeliveryEffect,
    IgTurnMessage,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services.ig_permission_transitions import (
    ACTIVE_STATUSES,
    cancel_client_unstarted_automation,
    supersede_permission_transitions,
)
from management.services.ig_reply_boundary import pause_reply_boundary


@dataclass(frozen=True)
class ManualResumeResult:
    client_id: int
    changed: bool
    permission_epoch: int
    successor_turn_id: int | None = None
    successor_revision_id: int | None = None
    successor_source_message_id: int | None = None
    successor_created: bool = False
    unresolved_turn_id: int | None = None
    unresolved_source_message_id: int | None = None
    successor_reason: str = ""
    cancelled: dict[str, int] = field(default_factory=dict)


class ManualResumeRejected(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def manual_resume_message(result: ManualResumeResult) -> str:
    if not result.changed:
        return "Бот уже веде цього клієнта."
    if result.successor_created:
        return "Клієнта повернуто боту. Нерозв’язаний запит поставлено в чергу з актуальним контекстом."
    if result.successor_reason in {"delivery_reconciliation_required", "manual_delivery_reconciliation_required"}:
        return "Клієнта повернуто боту. Попередню відправку потрібно звірити; автоматичного повтору не створено."
    if result.successor_reason == "manual_source_window_expired":
        return "Клієнта повернуто боту. Запит застарів для автоматичного повтору; бот чекатиме нового повідомлення."
    if result.successor_reason == "global_reply_paused":
        return "Клієнта повернуто боту, але автоматичні відповіді глобально вимкнено."
    if result.successor_reason in {"manager_answered", "model_answered"}:
        return "Клієнта повернуто боту. Після запиту вже є відповідь у переписці; повтор не створено."
    if result.successor_reason == "successor_revision_unavailable":
        return "Клієнта повернуто боту. Останній нерозв’язаний запит залишено в історії без автоматичного повтору."
    return "Клієнта повернуто боту; повторної відповіді не створено."


def _active_opt_out(client: IgClient) -> bool:
    recorded = bool(
        client.opted_out_at
        and (not client.opted_in_at or client.opted_in_at < client.opted_out_at)
    )
    pending = IgPermissionTransitionJob.objects.filter(
        client_id=client.pk,
        kind=IgPermissionTransitionJob.Kind.OPT_OUT,
        status__in=ACTIVE_STATUSES,
    ).exists()
    return recorded or pending


def _reject_ineligible_client(client: IgClient) -> None:
    if client.hidden_at:
        raise ManualResumeRejected(
            "client_hidden",
            "Прихованого клієнта спочатку потрібно окремо повернути до активних.",
        )
    if client.privacy_erasure_started_at:
        raise ManualResumeRejected(
            "privacy_erasure_active",
            "Відновлення недоступне під час видалення даних клієнта.",
        )
    if client.is_blocked:
        raise ManualResumeRejected(
            "client_blocked",
            "Заблокованого клієнта не можна передати автоматизації.",
        )
    if _active_opt_out(client):
        raise ManualResumeRejected(
            "active_opt_out",
            (
                "Клієнт відмовився від автоматичних повідомлень. "
                "Щоб повернути бота, спочатку потрібно окремо "
                "підтвердити нову згоду клієнта."
            ),
        )


def _dangerous_delivery_exists(client_id: int, source_ids: list[int]) -> bool:
    dangerous_send_states = ("sending", "unknown", "ambiguous")
    if IgRevisionDeliveryEffect.objects.filter(
        revision__client_id=client_id,
        revision__sources__message_id__in=source_ids,
        state__in=(
            IgRevisionDeliveryEffect.State.SENT,
            IgRevisionDeliveryEffect.State.PROVIDER_STARTED,
            IgRevisionDeliveryEffect.State.UNKNOWN,
        ),
    ).exists():
        return True
    if InstagramBotMessage.objects.filter(
        client_id=client_id,
        send_state__in=dangerous_send_states,
    ).filter(Q(pk__in=source_ids) | Q(role__in=(
        InstagramBotMessage.Role.MODEL,
        InstagramBotMessage.Role.MANAGER,
    ), id__gt=max(source_ids))).exists():
        return True
    if IgAiReplyRecoveryJob.objects.filter(
        client_id=client_id,
        source_message_id__in=source_ids,
    ).filter(
        Q(status__in=(
            IgAiReplyRecoveryJob.Status.PROCESSING,
            IgAiReplyRecoveryJob.Status.SENDING,
            IgAiReplyRecoveryJob.Status.AMBIGUOUS,
        ))
        | Q(reply_message__send_state__in=dangerous_send_states)
    ).exists():
        return True
    return IgCommerceTurnDecision.objects.filter(
        source_message_id__in=source_ids,
        delivery_state__in=(
            IgCommerceTurnDecision.DeliveryState.SENDING,
            IgCommerceTurnDecision.DeliveryState.UNKNOWN,
            IgCommerceTurnDecision.DeliveryState.PARTIAL,
        ),
    ).exists()


def _latest_unanswered_reference(client: IgClient):
    source = (
        InstagramBotMessage.objects.filter(
            client_id=client.pk,
            role=InstagramBotMessage.Role.USER,
        )
        .order_by("-id")
        .first()
    )
    if source is None:
        return None, None, "no_customer_request"

    from management.services.ig_reply_expectation import classify

    membership = (
        IgTurnMessage.objects.select_related("turn")
        .filter(message_id=source.pk)
        .first()
    )
    if not classify(source).substantive_reply_owed:
        # A trailing thank-you or emoji does not erase the question in the
        # same customer turn. Keep the latest source as its immutable watermark.
        from management.services.ig_turn_revisions import MAX_SOURCES

        siblings = list(InstagramBotMessage.objects.filter(
            turn_membership__turn_id=membership.turn_id,
            client=client, role=InstagramBotMessage.Role.USER,
        ).order_by("id")[:MAX_SOURCES + 1]) if membership else []
        if len(siblings) > MAX_SOURCES or not any(classify(row).substantive_reply_owed for row in siblings):
            return None, None, "latest_turn_needs_no_reply"

    manager_answered = InstagramBotMessage.objects.filter(
        client_id=client.pk,
        role=InstagramBotMessage.Role.MANAGER,
        id__gt=source.pk,
    ).exclude(status=InstagramBotMessage.Status.FAILED).exists()
    if manager_answered:
        return None, None, "manager_answered"

    model_answered = InstagramBotMessage.objects.filter(
        client_id=client.pk,
        role=InstagramBotMessage.Role.MODEL,
        id__gt=source.pk,
    ).exclude(status=InstagramBotMessage.Status.FAILED).filter(
        Q(provider_message_id__gt="") | Q(send_state="sent")
    ).exists()
    if model_answered:
        return None, None, "model_answered"

    if membership is None:
        turn = None
    else:
        turn = membership.turn
    if turn is None:
        return None, source, "customer_turn_unavailable"
    if turn.claim_state == IgCustomerTurn.ClaimState.CLAIMED:
        return None, None, "turn_execution_in_progress"
    if turn.claim_state == IgCustomerTurn.ClaimState.SUPERSEDED:
        return None, None, "turn_superseded"
    if turn.terminal_reason in {
        IgCustomerTurn.TerminalReason.REPLIED,
        IgCustomerTurn.TerminalReason.SEND_UNKNOWN,
    }:
        return None, None, "turn_delivery_already_crossed"

    from management.services.ig_customer_turns import (
        claimable_row_id,
        turn_message_ids,
    )

    source_ids = turn_message_ids(turn) or [source.pk]
    claimable_id = claimable_row_id(turn)
    claimable = InstagramBotMessage.objects.filter(
        pk=claimable_id,
        client_id=client.pk,
        role=InstagramBotMessage.Role.USER,
        status=InstagramBotMessage.Status.DONE,
        send_state="",
    ).first()
    if claimable is None:
        return None, None, "turn_not_safely_replayable"
    if _dangerous_delivery_exists(client.pk, source_ids):
        return None, None, "delivery_reconciliation_required"
    # These one-to-one historical rows remain unchanged. Only the separately
    # enabled, audited manual-revision consumer may create fresh execution.
    return turn, source, "successor_revision_unavailable"


def resume_client_automation(client_id: int, *, actor=None) -> ManualResumeResult:
    """Resume one client only after an explicit authorized operator command.

    Consent is deliberately outside this transition.  Existing valid consent is
    preserved, while an active opt-out always rejects the command.
    """
    with pause_reply_boundary():
        with transaction.atomic():
            settings_id = InstagramBotSettings.load().pk
            settings_obj = InstagramBotSettings.objects.select_for_update().get(pk=settings_id)
            client = (
                IgClient.objects.select_for_update()
                .filter(pk=client_id)
                .first()
            )
            if client is None:
                raise ManualResumeRejected(
                    "client_not_found", "Клієнта не знайдено.", status=404
                )
            _reject_ineligible_client(client)
            if not client.bot_paused and not client.manager_takeover:
                return ManualResumeResult(
                    client_id=client.pk,
                    changed=False,
                    permission_epoch=int(client.reply_permission_epoch or 0),
                    successor_reason="already_resumed",
                )

            now = timezone.now()
            before = {
                "permission_epoch": int(client.reply_permission_epoch or 0),
                "bot_paused": bool(client.bot_paused),
                "manager_takeover": bool(client.manager_takeover),
            }
            supersede_permission_transitions(
                client_id=client.pk,
                kinds=(
                    IgPermissionTransitionJob.Kind.MANAGER_TAKEOVER,
                    IgPermissionTransitionJob.Kind.CLIENT_PAUSE,
                ),
            )
            cancelled = cancel_client_unstarted_automation(
                client,
                reason="manual_resume_reconcile",
                now=now,
                nowait=False,
            )

            client.bot_paused = False
            client.manager_takeover = False
            client.paused_reason = ""
            client.paused_at = None
            client.reply_permission_epoch = int(client.reply_permission_epoch or 0) + 1
            client.save(update_fields=[
                "bot_paused",
                "manager_takeover",
                "paused_reason",
                "paused_at",
                "reply_permission_epoch",
                "updated_at",
            ])

            turn = source = None
            successor = None
            successor_reason = "global_reply_paused"
            audit = AdminAuditLog.objects.create(
                actor=actor if getattr(actor, "pk", None) else None,
                actor_role="operator",
                action="ig_bot.manual_resume", entity_type="IgClient",
                entity_id=str(client.pk), before=before,
                after={"permission_epoch": client.reply_permission_epoch, "bot_paused": False, "manager_takeover": False},
                reason="explicit_operator_resume",
            )
            if settings_obj.is_enabled:
                turn, source, successor_reason = _latest_unanswered_reference(client)
                from management.services.ig_revision_live import revision_execution_enabled

                if source is not None and turn is not None and revision_execution_enabled():
                    from management.bot_access import (
                        OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION,
                        has_all_bot_capabilities,
                    )
                    if has_all_bot_capabilities(actor, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
                        from management.services.ig_revision_manual_resume import create_manual_resume_successor

                        successor = create_manual_resume_successor(
                            client, settings_obj=settings_obj, audit_id=audit.pk,
                            source_message_id=source.pk, now=now,
                        )
                        successor_reason = successor.reason
                    else:
                        successor_reason = "successor_operator_not_authorized"

            from management.services.instagram_bot import log

            actor_id = int(getattr(actor, "pk", 0) or 0)
            previous_epoch = int(client.reply_permission_epoch or 0) - 1
            log(
                "info",
                "manual_resume",
                (
                    f"client={client.pk}; user={actor_id}; "
                    f"epoch={previous_epoch}->{client.reply_permission_epoch}; "
                    f"successor={successor_reason}"
                ),
            )

            return ManualResumeResult(
                client_id=client.pk,
                changed=True,
                permission_epoch=int(client.reply_permission_epoch or 0),
                successor_created=bool(successor and successor.created),
                successor_revision_id=successor.revision_id if successor and successor.created else None,
                successor_turn_id=turn.pk if successor and successor.created else None,
                successor_source_message_id=source.pk if successor and successor.created else None,
                unresolved_turn_id=turn.pk if turn is not None else None,
                unresolved_source_message_id=source.pk if source is not None else None,
                successor_reason=successor_reason,
                cancelled=cancelled,
            )
