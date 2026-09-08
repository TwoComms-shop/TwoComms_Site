"""
Вкладка «Бот» (адміністратори + обмежений Meta reviewer).

UI зі станом агента (запущено/зупинено, очікує повідомлення), кнопками
Start/Stop, вибором джерела ключів і онлайн-консоллю подій.
"""
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.cache import cache
from django.core import signing
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, DecimalField, Exists, ExpressionWrapper, F, OuterRef, Prefetch, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit

from base64_utils import strict_b64decode
from twocomms.db_resilience import retry_mysql_read

from .bot_access import (
    EDIT_IG_PROMPT_PERMISSION,
    MANAGE_IG_PAYMENTS_PERMISSION,
    OPERATE_IG_BOT_PERMISSION,
    VIEW_IG_CONVERSATION_PII_PERMISSION,
    has_all_bot_capabilities,
    has_any_bot_capability,
    has_bot_capability,
    is_meta_bot_reviewer,
)
from .models import (
    AdminAuditLog,
    IgBotNotification,
    IgBotNotificationAudit,
    IgClient,
    IgDeal,
    IgPaymentProjection,
    InstagramBotLog,
    InstagramBotSettings,
    BotSecretEncryptionUnavailable,
)
from .ig_bot_models import IgCheckoutAccessToken, IgCheckoutProposal, IgCheckoutRevision, IgLifecycleEvent, IgFollowUpTask
from .services import instagram_bot as bot
from .services import bot_followups
from .services import gemini_health, gemini_keys, gemini_probe, gemini_v2_read_model
from .services.bot_payment_truth import (
    CONFIRMED_ORDER_PAYMENT_STATUSES,
    active_client_order_payment_context,
    annotate_confirmed_purchase,
    annotate_verified_payment,
    client_has_confirmed_purchase,
    client_has_verified_payment,
    current_manager_confirmation_review_q,
    current_payment_confirmation,
    historical_purchase_confirmation,
    latest_legacy_payment_truth_deal,
    latest_payment_projection,
    latest_verified_payment_deal,
    verified_payment_q,
)


def _can_use_bot(user) -> bool:
    return is_meta_bot_reviewer(user) or has_any_bot_capability(user)


def _is_reviewer_only(user) -> bool:
    return is_meta_bot_reviewer(user)


_REVIEWER_STATUS_ALLOWED_KEYS = frozenset({
    # Keep external reviewer telemetry closed so future status fields cannot
    # become visible by default.
    "state",
    "running",
    "daemon_online",
    "pending",
    "settings_revision",
})


def _reviewer_safe_status(request):
    """Return PII-bearing diagnostics only to operators who may view PII."""
    status = gemini_health.public_projection(bot.status_snapshot())
    if has_all_bot_capabilities(
        request.user,
        OPERATE_IG_BOT_PERMISSION,
        VIEW_IG_CONVERSATION_PII_PERMISSION,
    ):
        return status
    return {
        key: status[key]
        for key in _REVIEWER_STATUS_ALLOWED_KEYS
        if key in status
    }


def privacy_policy(request):
    response = render(request, "management/privacy_policy.html")
    response["Cache-Control"] = "public, max-age=300"
    return response


def terms_of_service(request):
    response = render(request, "management/terms_of_service.html")
    response["Cache-Control"] = "public, max-age=300"
    return response


def data_deletion(request):
    response = render(request, "management/data_deletion.html")
    response["Cache-Control"] = "public, max-age=300"
    return response


def data_deletion_status(request, confirmation_code):
    from .models import BotDataDeletionRequest

    deletion_request = BotDataDeletionRequest.objects.filter(
        confirmation_code=confirmation_code
    ).first()
    response = render(
        request,
        "management/data_deletion_status.html",
        {
            "deletion_request": deletion_request,
            "confirmation_code": confirmation_code,
        },
        status=200 if deletion_request else 404,
    )
    response["Cache-Control"] = "public, max-age=300"
    return response


def _normalize_deletion_identifier(value: str) -> str:
    ident = (value or "").strip()
    if not ident:
        return ""
    ident = ident.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    match = re.search(r"(?:instagram\.com|instagr\.am)/([^/?#]+)", ident, re.I)
    if match:
        ident = match.group(1)
    ident = ident.strip().lstrip("@").lower()
    return ident


def _new_deletion_code() -> str:
    return secrets.token_hex(8).upper()


# IGSID Instagram — длинное числовое значение. Всё короче этого порога
# не может быть идентификатором отправителя, и сопоставлять по нему
# свободный текст лога запрещено (F-SEC-003).
_MIN_IGSID_LEN = 6


def _log_sender_ids(sender_ids) -> list[str]:
    """Отобрать из идентификаторов только те, что реально похожи на IGSID.

    `InstagramBotLog.detail` — свободный текст без FK на клиента, поэтому
    единственная структурная зацепка — соглашение формата `"{sender_id}: ..."`.
    Сопоставлять по username или подстроке нельзя: идентификатор `"0"` или
    `"a"` снёс бы почти весь операционный лог, включая записи других
    клиентов (F-SEC-003).
    """
    return sorted(
        {
            value
            for value in sender_ids
            if value and value.isdigit() and len(value) >= _MIN_IGSID_LEN
        }
    )


def _log_rows_for_sender_ids(sender_ids):
    """Логи, структурно принадлежащие этим IGSID, и только они."""
    from .models import InstagramBotLog

    ids = _log_sender_ids(sender_ids)
    if not ids:
        return InstagramBotLog.objects.none()
    scope = Q()
    for igsid in ids:
        # Двоеточие после IGSID — якорь: `"100:"` не совпадёт с `"1001: ..."`.
        # Первый вариант покрывает формат `"{sender_id}: ..."`,
        # второй — `"[{source}] {sender_id}: ..."`.
        scope |= Q(detail__startswith=f"{igsid}:") | Q(detail__contains=f" {igsid}:")
    return InstagramBotLog.objects.filter(scope)


def _delete_direct_bot_records(
    identifier: str = "", *, exact_client_ids=None, stale_before=None,
    frozen_message_ids=None, frozen_sender_ids=None, frozen_inbox_ids=None,
    frozen_cutoff_at=None,
) -> dict:
    # The first client/message fence must commit before private-storage I/O.
    if connection.in_atomic_block:
        raise RuntimeError("DIRECT_BOT privacy erase cannot run inside an outer transaction")
    from .models import (
        BotDataDeletionRequest,
        IgClient,
        IgWebhookInboxEvent,
        InstagramBotLog,
        InstagramBotMessage,
        InstagramBotProcessedMessage,
        InstagramBotRawEvent,
    )
    from .ig_bot_models import (
        IgFollowCtaDecision,
        IgFollowObservation,
        IgFollowRefreshJob,
        IgFollowState,
        IgPaymentFollowPreparation,
        IgUgcEvidenceAssessment,
        IgUgcReward,
        IgUgcRewardDelivery,
        IgUgcRewardLifetime,
        IgUgcRewardLifecycleJob,
    )

    normalized = _normalize_deletion_identifier(identifier)
    parsed_locked_ids = set()
    for value in exact_client_ids or ():
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if parsed > 0:
            parsed_locked_ids.add(parsed)
    locked_ids = sorted(parsed_locked_ids)
    frozen_message_ids = tuple(sorted({
        int(value) for value in (frozen_message_ids or ())
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
    }))
    frozen_sender_ids = tuple(sorted({
        str(value) for value in (frozen_sender_ids or ()) if str(value)
    }))
    frozen_inbox_ids = tuple(sorted({
        int(value) for value in (frozen_inbox_ids or ())
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
    }))
    frozen_targets = frozen_cutoff_at is not None
    if frozen_targets and not locked_ids:
        # A verified claim may have matched no existing client.  It must stay
        # no-match on recovery, never resolve a later account by identifier.
        identifier = ""
    result = {
        "normalized_identifier": normalized,
        "status": BotDataDeletionRequest.Status.NO_MATCH,
        "clients": 0,
        "messages": 0,
        "raw_events": 0,
        "logs": 0,
        "detail": "",
    }
    if not normalized and not locked_ids and not frozen_targets:
        result["detail"] = "Empty identifier."
        return result

    # Commit the client + message fence before scanning blobs. Capture obtains
    # its use lease under the same message lock, so either capture finishes
    # first and becomes deletion debt, or this fence wins before any CDN I/O.
    fence_at = timezone.now()
    with transaction.atomic():
        pre_clients_query = IgClient.objects.select_for_update()
        if frozen_targets and not locked_ids:
            pre_clients = []
        elif locked_ids:
            pre_clients_query = pre_clients_query.filter(pk__in=locked_ids)
            if stale_before is not None:
                pre_clients_query = pre_clients_query.filter(
                    last_message_at__isnull=False,
                    last_message_at__lt=stale_before,
                )
            pre_clients = list(pre_clients_query.order_by("pk"))
        else:
            pre_clients_query = pre_clients_query.filter(
                Q(igsid__iexact=normalized)
                | Q(username__iexact=normalized)
                | Q(display_name__iexact=normalized)
                | Q(phone_normalized__iexact=normalized)
            )
            pre_clients = list(pre_clients_query.order_by("pk"))
        for client in pre_clients:
            if client.privacy_erasure_started_at is None:
                client.privacy_erasure_started_at = fence_at
                client.save(update_fields=["privacy_erasure_started_at", "updated_at"])
        pre_sender_ids = set(frozen_sender_ids) if frozen_targets else {
            value for value in (
                normalized, *(c.igsid for c in pre_clients if c.igsid)
            ) if value
        }
        if frozen_targets:
            message_rows = list(
                InstagramBotMessage.objects.select_for_update().filter(pk__in=frozen_message_ids)
            )
        else:
            pre_message_scope = Q(client__in=pre_clients)
            if pre_sender_ids:
                pre_message_scope |= Q(sender_id__in=pre_sender_ids)
            message_rows = list(
                InstagramBotMessage.objects.select_for_update().filter(pre_message_scope)
            )
        private_message_ids = []
        for message in message_rows:
            if message.private_media_state == "deleted":
                continue
            private_message_ids.append(message.pk)
            if message.private_media_state != "deleting":
                message.private_media_state = "delete_pending"
                message.private_media_delete_after = fence_at
                message.private_media_delete_token = ""
                message.private_media_delete_claimed_at = None
                message.save(update_fields=[
                    "private_media_state", "private_media_delete_after",
                    "private_media_delete_token", "private_media_delete_claimed_at",
                ])
        fenced_client_ids = [client.pk for client in pre_clients]
        fenced_sender_ids = set(pre_sender_ids)

    # Blob erasure is its own committed two-phase boundary. Never delete the
    # DB row first: a crash would otherwise orphan an untraceable private file.
    if private_message_ids:
        from management.services.ig_private_media import delete_immediately

        deleted = delete_immediately(private_message_ids)
        if deleted != len(private_message_ids):
            raise RuntimeError("private media is in active use; retry privacy erasure")

    with transaction.atomic():
        clients = list(
            IgClient.objects.select_for_update().filter(pk__in=fenced_client_ids)
        )
        sender_ids = set(fenced_sender_ids)
        if frozen_targets:
            message_filter = Q(pk__in=frozen_message_ids)
        else:
            message_filter = Q(client__in=clients)
            if sender_ids:
                message_filter |= Q(sender_id__in=sender_ids)
        mids = list(
            InstagramBotMessage.objects.filter(message_filter)
            .exclude(mid__isnull=True).values_list("mid", flat=True)
        )
        message_scope = InstagramBotMessage.objects.filter(message_filter)
        messages_count, _ = message_scope.delete()
        raw_event_scope = InstagramBotRawEvent.objects.filter(sender_id__in=sender_ids)
        if frozen_targets:
            raw_event_scope = raw_event_scope.filter(created_at__lte=frozen_cutoff_at)
        raw_events_count, _ = raw_event_scope.delete()
        # Только структурная принадлежность по IGSID, никогда `icontains`
        # по свободному тексту (F-SEC-003).
        log_scope = _log_rows_for_sender_ids(sender_ids)
        if frozen_targets:
            log_scope = log_scope.filter(created_at__lte=frozen_cutoff_at)
        logs_count, _ = InstagramBotLog.objects.filter(
            pk__in=list(log_scope.values_list("pk", flat=True))
        ).delete()
        if mids:
            InstagramBotProcessedMessage.objects.filter(mid__in=mids).delete()
        if frozen_targets:
            # Keep the namespace/event-key/digest uniqueness tombstone.  Meta
            # may replay the same old event after erasure; deleting its receipt
            # would let a fresh inbox row materialize the erased conversation.
            # The customer identity and raw payload are removed, while later
            # or foreign receipts remain outside this exact frozen ID set.
            IgWebhookInboxEvent.objects.filter(pk__in=frozen_inbox_ids).update(
                owner_id="",
                customer_igsid="",
                decision=IgWebhookInboxEvent.Decision.REJECTED,
                reason="privacy_erased",
                payload={},
                processed_at=timezone.now(),
                next_attempt_at=None,
                last_error="",
            )
        client_ids = [client.pk for client in clients]
        if client_ids:
            # Analysis/memory tables use DO_NOTHING relations and append-only
            # guards during normal runtime. The already committed privacy
            # fence is the only boundary allowed to purge those behavioral
            # rows before the client identity disappears.
            from management.services.ig_typed_memory import (
                purge_client_analysis_memory,
            )

            purge_client_analysis_memory(client_ids)
            # Remove private reward payloads but retain a secret-bound,
            # irreversible consumed marker. Without it, deleting and later
            # recreating the same IGSID would mint a second lifetime grant.
            reward_rows = list(
                IgUgcReward.objects.filter(client_id__in=client_ids).values(
                    "pk", "promo_code_id", "client_id", "order_id", "issued_at", "created_at"
                )
            )
            promo_ids = [row["promo_code_id"] for row in reward_rows if row["promo_code_id"]]
            reward_order_ids = [row["order_id"] for row in reward_rows if row["order_id"]]
            IgUgcRewardLifecycleJob.objects.filter(client_id__in=client_ids).delete()
            if reward_order_ids:
                IgUgcRewardLifecycleJob.objects.filter(
                    order_id__in=reward_order_ids,
                ).delete()
            # Backfill the stable slot before deleting a legacy reward that
            # predates the slot migration. This is deliberately fail-closed:
            # a future recreation of the same IGSID must still see consumed
            # lifetime truth even when the original client row is gone.
            if reward_rows:
                from management.services.ig_ugc_rewards import _lifetime_slot_for_client

                rewards_by_client = {}
                for reward_row in reward_rows:
                    rewards_by_client.setdefault(reward_row["client_id"], []).append(reward_row)
                for client in clients:
                    rows_for_client = rewards_by_client.get(client.pk, [])
                    if not rows_for_client:
                        continue
                    lifetime = _lifetime_slot_for_client(client)
                    if lifetime.consumed_at is None:
                        issued_at = min(
                            (
                                row["issued_at"] or row["created_at"]
                                for row in rows_for_client
                            ),
                            default=timezone.now(),
                        )
                        lifetime.consumed_at = issued_at
                        lifetime.save(update_fields=["consumed_at", "updated_at"])
            IgUgcRewardDelivery.objects.filter(
                reward_id__in=[row["pk"] for row in reward_rows]
            ).delete()
            lifetime_rows = list(
                IgUgcRewardLifetime.objects.filter(client_id__in=client_ids).values(
                    "pk", "reward_id", "consumed_at"
                )
            )
            consumed_slot_ids = [
                row["pk"]
                for row in lifetime_rows
                if row["reward_id"] or row["consumed_at"]
            ]
            empty_slot_ids = [
                row["pk"]
                for row in lifetime_rows
                if not row["reward_id"] and not row["consumed_at"]
            ]
            if consumed_slot_ids:
                IgUgcRewardLifetime.objects.filter(pk__in=consumed_slot_ids).update(
                    client=None,
                    reward=None,
                    consumed_at=Coalesce("consumed_at", timezone.now()),
                )
            if empty_slot_ids:
                IgUgcRewardLifetime.objects.filter(pk__in=empty_slot_ids).delete()
            IgUgcReward.objects.filter(pk__in=[row["pk"] for row in reward_rows]).delete()
            IgUgcEvidenceAssessment.objects.filter(client_id__in=client_ids).delete()
            if promo_ids:
                # Keep order/payment audit rows intact, but revoke the private
                # bearer capability and remove its guest reservation metadata.
                from storefront.models import PromoCode, PromoCodeGuestUsage

                PromoCodeGuestUsage.objects.filter(promo_code_id__in=promo_ids).delete()
                PromoCode.objects.filter(pk__in=promo_ids).update(
                    is_active=False,
                    guest_redeemable=False,
                )
            # Follow observations and CTA decisions are intentionally
            # append-only/durable in normal operation. Privacy fulfillment is
            # the audited deletion boundary, so use model table metadata for a
            # scoped SQL purge instead of weakening those runtime invariants.
            placeholders = ", ".join(["%s"] * len(client_ids))
            for model in (IgFollowObservation, IgFollowCtaDecision):
                table = connection.ops.quote_name(model._meta.db_table)
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"DELETE FROM {table} WHERE client_id IN ({placeholders})",
                        client_ids,
                    )
            IgFollowState.objects.filter(client_id__in=client_ids).delete()
            IgFollowRefreshJob.objects.filter(client_id__in=client_ids).delete()
            # Payment lifecycle events are durable business history, but their
            # optional follow-copy preparation is a client-scoped operational
            # job.  Its DO_NOTHING relation would otherwise leave an orphaned
            # client id and deadline after privacy fulfillment.
            IgPaymentFollowPreparation.objects.filter(client_id__in=client_ids).delete()
        # Attribution rows are append-only and already contain only a
        # non-reversible identity digest; no mutable profile snapshot is kept.
        clients_count = len(clients)
        IgClient.objects.filter(id__in=[c.id for c in clients]).delete()

    result.update({
        "status": (
            BotDataDeletionRequest.Status.COMPLETED
            if any([clients_count, messages_count, raw_events_count, logs_count])
            or frozen_targets
            else BotDataDeletionRequest.Status.NO_MATCH
        ),
        "clients": clients_count,
        "messages": messages_count,
        "raw_events": raw_events_count,
        "logs": logs_count,
        "detail": (
            "Matching DIRECT_BOT records were deleted or anonymized."
            if any([clients_count, messages_count, raw_events_count, logs_count])
            else "No matching DIRECT_BOT records were found for the supplied identifier."
        ),
    })
    return result


@require_POST
def data_deletion_submit(request):
    """Публичная форма удаления данных: регистрирует заявку, НЕ удаляет.

    Раньше этот эндпоинт удалял всю переписку и карточку клиента по одному
    анонимному POST с публично известным username (F-SEC-002). Владение
    здесь не подтверждено, поэтому удаление выполняет менеджер после
    проверки — командой `fulfill_ig_data_deletion`.

    Это приводит реализацию в соответствие с уже опубликованной политикой
    на самой странице: «We may ask for limited verification information...
    After verification, we will delete or anonymize eligible bot records».
    """
    from .services.ig_data_deletion import register_public_request

    identifier = (request.POST.get("identifier") or "").strip()
    normalized = _normalize_deletion_identifier(identifier)
    deletion_request = register_public_request(identifier, normalized)

    try:
        from .services.ig_alerts import format_operator_alert

        bot.notify_manager(
            format_operator_alert(
                "🧹 Запит на видалення даних DIRECT_BOT",
                event_type="data_deletion_request",
                task_id=deletion_request.pk,
                status="pending_verification",
                instruction_code="data_deletion_request",
            ),
            dedupe_key=f"data-deletion:{deletion_request.pk}",
            event_type="data_deletion_request",
        )
    except Exception:
        # Заявка уже зарегистрирована и не должна теряться из-за сбоя
        # доставки уведомления. Менеджер увидит её в списке заявок.
        pass

    return redirect(
        "management_data_deletion_status",
        confirmation_code=deletion_request.confirmation_code,
    )


def _base64_url_decode(value: str) -> bytes:
    return strict_b64decode(value)


def _parse_meta_signed_request(signed_request: str) -> dict:
    if not signed_request or "." not in signed_request:
        return {}
    encoded_sig, encoded_payload = signed_request.split(".", 1)
    if not encoded_sig or not encoded_payload:
        return {}

    app_secret = bot.parent_meta_app_secret()
    # This callback is public and can create compliance/audit records.  Never
    # accept an unsigned request when the production secret is missing.
    if not app_secret:
        return {}

    try:
        expected = hmac.new(
            app_secret.encode("utf-8"),
            msg=encoded_payload.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_base64_url_decode(encoded_sig), expected):
            return {}
        payload = json.loads(_base64_url_decode(encoded_payload).decode("utf-8"))
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return {}
    return payload if isinstance(payload, dict) else {}


@csrf_exempt
@require_POST
def data_deletion_callback(request):
    from .models import BotDataDeletionRequest

    payload = _parse_meta_signed_request(request.POST.get("signed_request") or "")
    if not payload:
        return JsonResponse({"error": "Invalid signed_request."}, status=400)
    meta_user_id = str(payload.get("user_id") or "")
    deletion_request = BotDataDeletionRequest.objects.create(
        confirmation_code=_new_deletion_code(),
        source=BotDataDeletionRequest.Source.META_CALLBACK,
        identifier=meta_user_id[:255],
        normalized_identifier=meta_user_id[:255],
        meta_user_id=meta_user_id[:128],
        status=BotDataDeletionRequest.Status.NO_MATCH,
        detail=(
            "Meta deletion callback received. DIRECT_BOT stores Instagram Direct sender "
            "identifiers; no matching local Instagram conversation records were found for "
            "the supplied Meta app-scoped user id."
        ),
    )
    deletion_request.mark_completed(status=BotDataDeletionRequest.Status.NO_MATCH)
    status_url = request.build_absolute_uri(
        reverse("management_data_deletion_status", args=[deletion_request.confirmation_code])
    )
    return JsonResponse({
        "url": status_url,
        "confirmation_code": deletion_request.confirmation_code,
    })


def app_review_info(request):
    response = render(request, "management/app_review_info.html")
    response["Cache-Control"] = "public, max-age=300"
    return response


def _display_band(band, *, verified_payment: bool):
    """Каким состоянием показывать снапшот в карточке клиента (F-SCORE-003).

    Дубль понижения `paid → checkout`, который жил здесь, затирал «оплачено»
    даже тогда, когда оплату подтвердила сама система. Теперь понижаем только
    неподтверждённое утверждение модели.
    """
    from .ig_bot_models import IgConversationAnalysisSnapshot

    if band == IgConversationAnalysisSnapshot.Band.PAID and not verified_payment:
        return IgConversationAnalysisSnapshot.Band.CHECKOUT
    return band


TERMINAL_POST_SALE_STATUSES = ("completed", "rejected", "cancelled")

# DR-002: метрика описывает намерение купить, видимое в сообщениях, и остаётся
# такой. Меняется только подпись: «ймовірність» без контекста читалась как
# «шанс, что этот человек вообще купит», и у уже купившего давала «0%».
POTENTIAL_METRIC_LABEL = "намір купити зараз"
POTENTIAL_METRIC_NOTE_LEAD = (
    "Показує намір купити в поточному циклі, видимий у повідомленнях клієнта. "
    "Факт оплати сюда не входить — він окремий."
)
POTENTIAL_METRIC_NOTE_BUYER = (
    "Клієнт уже купив, тому низький відсоток тут означає «зараз нічого не "
    "обирає», а не «не купить»."
)


def _potential_metric_note(client) -> str:
    """Explain what the number means, and say so louder for a buyer."""
    note = POTENTIAL_METRIC_NOTE_LEAD
    if int(getattr(client, "purchases_count", 0) or 0) > 0:
        note = f"{note} {POTENTIAL_METRIC_NOTE_BUYER}"
    return note


def _buyer_badge_payload(client) -> dict:
    """Purchase facts for the card, with the provenance of the money named.

    ``total_spent`` can be fed by a manager decision rather than by a provider
    ledger, so the badge says so. An unmeasured amount stays empty instead of
    rendering as 0.00, which would read as "bought for free".
    """
    purchases = int(getattr(client, "purchases_count", 0) or 0)
    flags = getattr(client, "conversion_flags", None) or {}
    amount_unknown = bool(flags.get("purchase_amount_unknown"))
    provider_unverified = bool(flags.get("purchase_provider_unverified"))
    total = str(getattr(client, "total_spent", "") or "")
    if purchases <= 0:
        return {
            "is_buyer": False,
            "purchases": 0,
            "total_spent": "",
            "amount_unknown": False,
            "provider_unverified": False,
            "label": "",
            "amount_note": "",
        }
    if amount_unknown:
        total = ""
    label = f"Вже купив · {purchases}"
    if total:
        label = f"{label} · {total} ₴"
    if amount_unknown:
        amount_note = "Сума покупки не зафіксована — уточніть у менеджера."
    elif provider_unverified:
        amount_note = "Суму підтвердив менеджер, не платіжний провайдер."
    else:
        amount_note = "Суму підтвердив платіжний провайдер."
    return {
        "is_buyer": True,
        "purchases": purchases,
        "total_spent": total,
        "amount_unknown": amount_unknown,
        "provider_unverified": provider_unverified,
        "label": label,
        "amount_note": amount_note,
    }


def _display_interaction_type(client, interaction_type):
    """Overlay a confirmed service fact on top of the model's latest opinion.

    F-SCORE-008 proposed picking a different snapshot. That is worse than it
    sounds: the snapshot with the right band is also the *older* one, so the
    card would show stale text. Overlaying keeps the newest analysis and adds
    the fact the analysis cannot know — an exchange case is open right now.

    A real complaint is never masked: a defect during an exchange is a separate
    fact and hiding it would trade one wrong card for another.
    """
    from .ig_bot_models import IgConversationAnalysisSnapshot, IgPostSaleCase

    types = IgConversationAnalysisSnapshot.InteractionType
    if interaction_type == types.SUPPORT_COMPLAINT:
        return interaction_type
    case_type = ""
    if hasattr(client, "latest_post_sale_type") and hasattr(
        client, "latest_post_sale_status"
    ):
        annotated_status = str(client.latest_post_sale_status or "")
        if annotated_status not in TERMINAL_POST_SALE_STATUSES:
            case_type = str(client.latest_post_sale_type or "")
    elif getattr(client, "pk", None):
        try:
            from management.services.ig_post_sale import open_service_case

            case = open_service_case(client)
            case_type = str(case.case_type or "") if case is not None else ""
        except Exception:
            case_type = ""
    if not case_type:
        return interaction_type
    if case_type == IgPostSaleCase.CaseType.EXCHANGE:
        return types.EXCHANGE_REQUEST
    if case_type == IgPostSaleCase.CaseType.RETURN:
        return types.RETURN_REQUEST
    return interaction_type


def _require_bot_capabilities(request, *permissions):
    if not has_all_bot_capabilities(request.user, *permissions):
        return JsonResponse(
            {"success": False, "error": "Недостатньо прав для цієї дії."},
            status=403,
        )
    return None


_GEMINI_HEALTH_PROBE_LOCK_SECONDS = 45
_GEMINI_HEALTH_PROBE_RATE_SECONDS = 15
_GEMINI_HEALTH_MAX_LATENCY_MS = 120_000
_GEMINI_HEALTH_PROBE_STATUSES = frozenset({
    "ok",
    "reachable_empty",
    "reachable_degraded",
    "blocked",
    "forbidden",
    "model_unavailable",
    "quota",
    "provider_error",
    "request_error",
    "malformed_response",
    "timeout",
    "transport_error",
    # ЭБ.4: исходы проверки БЕЗ расхода генерационной квоты (`GET /models/{id}`).
    "metadata_ok",
    "unsupported_generation",
})
_GEMINI_HEALTH_FAILURE_KINDS = {
    "reachable_empty": "empty",
    "reachable_degraded": "degraded",
    "blocked": "blocked",
    "forbidden": "permission_denied",
    "model_unavailable": "model_not_found",
    "quota": "quota_429",
    "provider_error": "provider_error",
    "request_error": "request_error",
    "malformed_response": "invalid_payload",
    "timeout": "read_timeout",
    "transport_error": "transport",
}
_GEMINI_HEALTH_FINISH_REASONS = frozenset({
    "FINISH_REASON_UNSPECIFIED",
    "STOP",
    "MAX_TOKENS",
    "SAFETY",
    "RECITATION",
    "LANGUAGE",
    "OTHER",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "MALFORMED_FUNCTION_CALL",
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
    "NO_IMAGE",
    "IMAGE_OTHER",
    "UNEXPECTED_TOOL_CALL",
    "TOO_MANY_TOOL_CALLS",
    "MISSING_THOUGHT_SIGNATURE",
})
_GEMINI_HEALTH_REACHABLE_STATUSES = frozenset({
    "ok",
    "metadata_ok",
    "reachable_degraded",
    "blocked",
    "reachable_empty",
})


def _gemini_health_cache_key(kind: str, key_name: str, model: str) -> str:
    return f"management:gemini-health:{kind}:v1:{key_name}:{model}"


def _gemini_health_error(code: str, status: int) -> JsonResponse:
    return JsonResponse(
        {"success": False, "code": code, "error": code},
        status=status,
    )


def _normalize_gemini_health_probe(raw_result, model: str) -> dict:
    """Keep provider output to a small, enum-like, non-sensitive shape."""
    result = raw_result if isinstance(raw_result, dict) else {}
    raw_status = result.get("status")
    status = (
        raw_status
        if isinstance(raw_status, str) and raw_status in _GEMINI_HEALTH_PROBE_STATUSES
        else "provider_error"
    )

    raw_http_code = result.get("http_code")
    if isinstance(raw_http_code, bool):
        http_code = 0
    else:
        try:
            http_code = int(raw_http_code or 0)
        except (TypeError, ValueError, OverflowError):
            http_code = 0
    if http_code < 0 or http_code > 599:
        http_code = 0

    raw_latency = result.get("latency_ms")
    if isinstance(raw_latency, bool):
        latency_ms = 0
    else:
        try:
            latency_ms = int(raw_latency or 0)
        except (TypeError, ValueError, OverflowError):
            latency_ms = 0
    latency_ms = min(_GEMINI_HEALTH_MAX_LATENCY_MS, max(0, latency_ms))

    finish_reason = ""
    raw_finish_reason = result.get("finish_reason")
    if isinstance(raw_finish_reason, str):
        candidate = raw_finish_reason.strip().upper()
        if candidate in _GEMINI_HEALTH_FINISH_REASONS:
            finish_reason = candidate

    capability_success = status in {"ok", "metadata_ok"}
    failure_kind = _GEMINI_HEALTH_FAILURE_KINDS.get(status, "")
    return {
        "model": model,
        "status": status,
        "http_code": http_code,
        "finish_reason": finish_reason,
        "latency_ms": latency_ms,
        "outcome": "succeeded" if capability_success else "failed",
        "failure_kind": failure_kind,
    }


def _record_gemini_health_probe(
    *,
    key_name: str,
    model: str,
    request_id: str,
    observed_at,
    normalized: dict,
) -> None:
    """Persist one redacted attempt and only the dedicated probe fields."""
    from .models import GeminiKeyState

    with transaction.atomic():
        gemini_keys.record_attempt(
            request_id=request_id,
            role="health_metadata",
            key_name=key_name,
            model=model,
            outcome=normalized["outcome"],
            failure_kind=normalized["failure_kind"],
            http_code=normalized["http_code"],
            provider_reason=normalized["finish_reason"],
            decision="manual_metadata",
            latency_ms=normalized["latency_ms"],
            remaining_deadline_ms=0,
            usage={},
            error_detail="",
        )
        state = GeminiKeyState.objects.select_for_update().get(key_name=key_name)
        state.last_probe_at = observed_at
        state.last_probe_status = normalized["status"]
        state.last_probe_model = model
        state.last_probe_latency_ms = normalized["latency_ms"]
        state.last_probe_finish_reason = normalized["finish_reason"]
        state.last_probe_http_code = normalized["http_code"] or None
        state.last_probe_error = (
            ""
            if normalized["status"] in _GEMINI_HEALTH_REACHABLE_STATUSES
            else normalized["status"]
        )
        state.save(update_fields=[
            "last_probe_at",
            "last_probe_status",
            "last_probe_model",
            "last_probe_latency_ms",
            "last_probe_finish_reason",
            "last_probe_http_code",
            "last_probe_error",
            "updated_at",
        ])


def _gemini_health_pool_row(key_name: str, now) -> dict:
    for row in gemini_keys.pool_status(now=now):
        if row.get("key_name") == key_name:
            return row
    return {}


def _require_bot_write_json(request):
    return _require_bot_capabilities(
        request,
        OPERATE_IG_BOT_PERMISSION,
        VIEW_IG_CONVERSATION_PII_PERMISSION,
    )


_PUBLIC_BOT_LOG_LEVELS = frozenset({
    "debug", "info", "success", "warning", "error",
})


def _public_bot_log_item(row, *, include_date: bool = False) -> dict:
    """Project current and historical log rows through one safe boundary."""
    payload = {
        "id": row.id,
        "level": row.level if row.level in _PUBLIC_BOT_LOG_LEVELS else "info",
        "event": row.event,
        "detail": row.detail,
        "time": row.created_at.strftime("%H:%M:%S"),
    }
    if include_date:
        payload["date"] = row.created_at.strftime("%d.%m.%Y")
    return gemini_health.public_projection(payload)


def _log_items(limit: int = 80):
    rows = InstagramBotLog.objects.all()[:limit]
    return [_public_bot_log_item(row, include_date=True) for row in rows]


@login_required(login_url="management_login")
def bot_dashboard(request):
    if not _can_use_bot(request.user):
        return redirect("management_home")
    if _is_reviewer_only(request.user):
        return render(
            request,
            "management/bot_reviewer.html",
            {
                "status": _reviewer_safe_status(request),
                "meta_bot_reviewer_mode": True,
            },
        )
    settings_obj = InstagramBotSettings.load()
    can_operate = has_bot_capability(request.user, OPERATE_IG_BOT_PERMISSION)
    can_view_pii = has_bot_capability(
        request.user, VIEW_IG_CONVERSATION_PII_PERMISSION
    )
    can_manage_payments = has_bot_capability(
        request.user, MANAGE_IG_PAYMENTS_PERMISSION
    )
    can_edit_prompt = has_bot_capability(request.user, EDIT_IG_PROMPT_PERMISSION)
    return render(
        request,
        "management/bot.html",
        {
            "settings": settings_obj,
            "status": _reviewer_safe_status(request),
            "log_items": _log_items() if can_operate and can_view_pii else [],
            "cred_env": InstagramBotSettings.CredSource.ENV,
            "cred_custom": InstagramBotSettings.CredSource.CUSTOM,
            "has_custom_direct_token": settings_obj.has_custom_direct_token,
            "has_custom_gemini_key": settings_obj.has_custom_gemini_key,
            "meta_bot_reviewer_mode": False,
            "bot_can_operate": can_operate,
            "bot_can_view_pii": can_view_pii,
            "bot_can_manage_payments": can_manage_payments,
            "bot_can_edit_prompt": can_edit_prompt,
            "bot_can_view_full_status": can_operate and can_view_pii,
            "bot_is_admin": can_operate and can_view_pii,
        },
    )


@login_required(login_url="management_login")
@require_POST
def bot_start_api(request):
    blocked = _require_bot_capabilities(request, OPERATE_IG_BOT_PERMISSION)
    if blocked:
        return blocked
    bot.start_bot()
    return JsonResponse({"success": True, "status": _reviewer_safe_status(request)})


@login_required(login_url="management_login")
@require_POST
def bot_stop_api(request):
    blocked = _require_bot_capabilities(request, OPERATE_IG_BOT_PERMISSION)
    if blocked:
        return blocked
    bot.stop_bot()
    return JsonResponse({"success": True, "status": _reviewer_safe_status(request)})


@login_required(login_url="management_login")
@require_GET
def bot_status_api(request):
    if not (_can_use_bot(request.user)):
        return JsonResponse(
            {"success": False, "error": "Недостатньо прав для цієї дії."},
            status=403,
        )
    try:
        after_id = int(request.GET.get("after_id") or 0)
    except (TypeError, ValueError):
        after_id = 0

    if not has_all_bot_capabilities(
        request.user,
        OPERATE_IG_BOT_PERMISSION,
        VIEW_IG_CONVERSATION_PII_PERMISSION,
    ):
        return JsonResponse({
            "success": True,
            "status": _reviewer_safe_status(request),
            "log": [],
        })

    def load_rows():
        rows = InstagramBotLog.objects.all()
        if after_id:
            rows = rows.filter(id__gt=after_id)
        return list(rows[:120])

    rows = retry_mysql_read(load_rows, fallback=[])
    rows.reverse()  # від старіших до новіших для дозапису в консоль
    items = [_public_bot_log_item(row) for row in rows]
    return JsonResponse({"success": True, "status": _reviewer_safe_status(request), "log": items})


@login_required(login_url="management_login")
@require_GET
def bot_gemini_health_api(request):
    blocked = _require_bot_capabilities(request, OPERATE_IG_BOT_PERMISSION)
    if blocked:
        return blocked
    # This endpoint is intentionally passive: build_snapshot reads only the
    # bounded local attempt ledger and key-state projection.
    return JsonResponse(gemini_health.build_snapshot())


@login_required(login_url="management_login")
@require_GET
def bot_gemini_v2_quotas_api(request):
    blocked = _require_bot_capabilities(request, OPERATE_IG_BOT_PERMISSION)
    if blocked:
        return blocked
    try:
        payload = gemini_v2_read_model.build_quotas_payload()
    except gemini_v2_read_model.PublicProjectionError:
        return JsonResponse(
            {"success": False, "error": "projection_unavailable"}, status=503
        )
    return JsonResponse(payload)


@login_required(login_url="management_login")
@require_GET
def bot_gemini_v2_routes_api(request):
    blocked = _require_bot_capabilities(request, OPERATE_IG_BOT_PERMISSION)
    if blocked:
        return blocked
    try:
        payload = gemini_v2_read_model.build_routes_payload()
    except gemini_v2_read_model.PublicProjectionError:
        return JsonResponse(
            {"success": False, "error": "projection_unavailable"}, status=503
        )
    return JsonResponse(payload)


@login_required(login_url="management_login")
@require_GET
def bot_gemini_v2_attempts_api(request):
    blocked = _require_bot_capabilities(request, OPERATE_IG_BOT_PERMISSION)
    if blocked:
        return blocked
    try:
        payload = gemini_v2_read_model.build_attempts_payload(
            cursor=request.GET.get("cursor", ""),
            limit=request.GET.get("limit"),
        )
    except gemini_v2_read_model.InvalidCursor:
        return JsonResponse(
            {"success": False, "error": "invalid_cursor"}, status=400
        )
    except gemini_v2_read_model.PublicProjectionError:
        return JsonResponse(
            {"success": False, "error": "projection_unavailable"}, status=503
        )
    return JsonResponse(payload)


@login_required(login_url="management_login")
@require_POST
def bot_gemini_health_probe_api(request):
    blocked = _require_bot_capabilities(request, OPERATE_IG_BOT_PERMISSION)
    if blocked:
        return blocked

    slot_id = request.POST.get("slot_id")
    model = request.POST.get("model")
    slot_values = request.POST.getlist("slot_id")
    model_values = request.POST.getlist("model")
    key_name = gemini_health.ALIAS_BY_SLOT.get(str(slot_id or ""))
    if (
        len(slot_values) != 1
        or len(model_values) != 1
        or key_name not in gemini_keys.ALL_KEYS
        or not isinstance(model, str)
        or model not in gemini_health.DISPLAY_MODELS
    ):
        return _gemini_health_error("invalid_probe_request", 400)

    key_value = (os.environ.get(key_name) or "").strip()
    if not key_value:
        return _gemini_health_error("key_unconfigured", 409)

    now = timezone.now()
    pool_row = _gemini_health_pool_row(key_name, now)
    health_state = str(pool_row.get("health_state") or "")
    if health_state == "cooldown" or not pool_row.get("available", True):
        return _gemini_health_error("key_cooldown", 409)
    if health_state == "busy":
        return _gemini_health_error("key_busy", 409)

    lock_key = _gemini_health_cache_key("lock", key_name, model)
    rate_key = _gemini_health_cache_key("rate", key_name, model)
    lock_token = secrets.token_hex(16)
    if not cache.add(
        lock_key,
        lock_token,
        timeout=_GEMINI_HEALTH_PROBE_LOCK_SECONDS,
    ):
        return _gemini_health_error("probe_in_progress", 409)

    lease_token = None
    try:
        lease_token = gemini_keys.acquire_key_lease(
            key_name,
            role="health_probe",
            seconds=_GEMINI_HEALTH_PROBE_LOCK_SECONDS,
        )
        if not lease_token:
            return _gemini_health_error("key_busy", 409)

        # Recheck cooldown after the atomic lease claim to close the
        # check-then-claim race with quota/circuit workers.
        if not gemini_keys.is_available(key_name, now=timezone.now()):
            return _gemini_health_error("key_cooldown", 409)
        if not cache.add(
            rate_key,
            True,
            timeout=_GEMINI_HEALTH_PROBE_RATE_SECONDS,
        ):
            return _gemini_health_error("probe_rate_limited", 429)

        try:
            # ЭБ.4: проверка НЕ тратит генерационную квоту. Прежний probe_key()
            # отправлял настоящий generateContent, то есть 1 запрос из 20
            # суточных для этой пары; шесть ключей на двух моделях — десятая
            # часть дня. Ключ считается рабочим, пока реальный трафик не вернёт
            # ошибку; кнопка лишь подтверждает валидность ключа и модели.
            raw_result = gemini_probe.probe_key_metadata(
                model,
                key_value,
                timeout=gemini_probe.PROBE_TIMEOUT,
            )
        except Exception:
            # The probe client normally converts transport failures to a
            # bounded result. Keep the API fail-closed if an unexpected
            # adapter error escapes, without persisting its text.
            raw_result = {}
        normalized = _normalize_gemini_health_probe(raw_result, model)
        observed_at = timezone.now()
        request_id = f"health-probe-{secrets.token_hex(12)}"
        _record_gemini_health_probe(
            key_name=key_name,
            model=model,
            request_id=request_id,
            observed_at=observed_at,
            normalized=normalized,
        )
        return JsonResponse({
            "success": True,
            "probe": {
                "alias": gemini_health.DISPLAY_ALIASES.get(key_name, "API key"),
                "model": normalized["model"],
                "status": normalized["status"],
                "failure_kind": normalized["failure_kind"],
                "http_code": normalized["http_code"],
                "finish_reason": normalized["finish_reason"],
                "latency_ms": normalized["latency_ms"],
                "observed_at": observed_at.isoformat(),
            },
        })
    finally:
        if lease_token:
            try:
                gemini_keys.release_key_lease(key_name, lease_token)
            except Exception:
                pass
        try:
            if cache.get(lock_key) == lock_token:
                cache.delete(lock_key)
        except Exception:
            pass


@require_GET
def bot_health(request):
    """Public, non-sensitive readiness probe for external uptime monitors."""
    from .services.ig_task_health import release_queue_snapshot, task_health_snapshot

    status = bot.status_snapshot()
    tasks = task_health_snapshot()
    queues = release_queue_snapshot()
    enabled = bool(InstagramBotSettings.load().is_enabled)
    bot_healthy = not enabled or status.get("state") == "running"
    healthy = bool(
        tasks.get("available")
        and tasks.get("healthy")
        and queues.get("available")
        and queues.get("dangerous_backlog") == 0
        and bot_healthy
    )
    response = JsonResponse(
        {
            "status": "ok" if healthy else "degraded",
            "service": "instagram-bot",
            "bot_state": status.get("state") or "unknown",
            "cron_unhealthy": int(tasks.get("unhealthy_count") or 0),
            "queues": queues,
            "checked_at": timezone.now().isoformat(),
        },
        status=200 if healthy else 503,
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@login_required(login_url="management_login")
@require_POST
def bot_inbox_refresh_start_api(request):
    blocked = _require_bot_capabilities(request, OPERATE_IG_BOT_PERMISSION)
    if blocked:
        return blocked
    from .services.ig_inbox_refresh import create_refresh_run, serialize_refresh_run

    try:
        run, created = create_refresh_run(request.user)
    except ValueError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=409)
    return JsonResponse(
        {"success": created, "run": serialize_refresh_run(run)},
        status=202 if created else 409,
    )


@login_required(login_url="management_login")
@require_GET
def bot_inbox_refresh_status_api(request):
    blocked = _require_bot_capabilities(request, OPERATE_IG_BOT_PERMISSION)
    if blocked:
        return blocked
    from .services.ig_inbox_refresh import latest_refresh_run, serialize_refresh_run

    return JsonResponse({
        "success": True,
        "run": serialize_refresh_run(latest_refresh_run()),
    })


@login_required(login_url="management_login")
@require_POST
def bot_inbox_refresh_cancel_api(request, run_id):
    blocked = _require_bot_capabilities(request, OPERATE_IG_BOT_PERMISSION)
    if blocked:
        return blocked
    from .services.ig_inbox_refresh import request_refresh_cancel, serialize_refresh_run

    run = request_refresh_cancel(run_id)
    if run is None:
        return JsonResponse({"success": False, "error": "Запуск не знайдено."}, status=404)
    if run.status not in {
        run.Status.CANCELLING,
        run.Status.CANCELLED,
    }:
        return JsonResponse(
            {"success": False, "error": "Цей запуск уже завершено.", "run": serialize_refresh_run(run)},
            status=409,
        )
    return JsonResponse({"success": True, "run": serialize_refresh_run(run)})


@login_required(login_url="management_login")
@require_POST
def bot_inbox_refresh_retry_api(request, run_id):
    blocked = _require_bot_capabilities(request, OPERATE_IG_BOT_PERMISSION)
    if blocked:
        return blocked
    from .models import IgInboxRefreshRun
    from .services.ig_inbox_refresh import (
        refresh_run_for_current_owner,
        retry_refresh_failures,
        serialize_refresh_run,
    )

    before = refresh_run_for_current_owner(run_id)
    if before is None:
        return JsonResponse({"success": False, "error": "Запуск не знайдено."}, status=404)
    if before.status not in {
        IgInboxRefreshRun.Status.COMPLETED_ERRORS,
        IgInboxRefreshRun.Status.FAILED,
    }:
        return JsonResponse(
            {"success": False, "error": "Немає помилок для повтору.", "run": serialize_refresh_run(before)},
            status=409,
        )
    try:
        run = retry_refresh_failures(run_id)
    except IntegrityError:
        return JsonResponse({"success": False, "error": "Інше оновлення вже виконується."}, status=409)
    if run is None:
        return JsonResponse({"success": False, "error": "Запуск не знайдено."}, status=404)
    return JsonResponse({"success": True, "run": serialize_refresh_run(run)}, status=202)


def _notification_preview(value, limit=280):
    return bot._redact_secret_text(str(value or "")).replace("\n", " ")[:limit]


_NOTIFICATION_STATUS_LABELS = {
    IgBotNotification.Status.UNKNOWN: "Результат доставки невідомий",
    IgBotNotification.Status.DEAD_LETTER: "Спроби вичерпано",
}
_NOTIFICATION_EVENT_LABELS = {
    "takeover": "Менеджер підключився",
    "payment": "Оплата",
    "payment_link": "Посилання на оплату",
    "shipment": "Відправлення",
    "shipment_human_review": "Потрібна ручна перевірка відправлення",
    "payment_reversed_review": "Перевірка повернення або скасування оплати",
    "payment_confirmation_review": "Потрібно підтвердити оплату клієнта",
    "delivery_block": "Доставка повідомлення заблокована",
    "ai_unavailable": "ШІ тимчасово недоступний",
    "spam": "Антиспам",
    "generic": "Системне сповіщення",
}
_NOTIFICATION_FAILURE_LABELS = {
    "ambiguous_transport": "Невідомий результат мережевого запиту",
    "ambiguous_stale_sending": "Відправлення перервано до фіксації результату",
    "ambiguous_provider_response": "Неможливо прочитати відповідь Telegram",
    "retry_exhausted": "Вичерпано автоматичні спроби",
    "provider_permanent": "Telegram відхилив повідомлення",
    "configuration": "Не налаштовано Telegram",
    "rate_limited": "Telegram обмежив частоту запитів",
}


@login_required(login_url="management_login")
@require_GET
def bot_notification_review_api(request):
    blocked = _require_bot_write_json(request)
    if blocked:
        return blocked
    rows = IgBotNotification.objects.filter(
        status__in=[IgBotNotification.Status.UNKNOWN, IgBotNotification.Status.DEAD_LETTER]
    ).select_related("client").order_by("created_at", "id")[:100]
    items = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        items.append({
            "id": row.id,
            "event_type": row.event_type,
            "client_id": row.client_id,
            "client": (row.client.display_name or row.client.username or row.client.igsid) if row.client else "",
            "status": row.status,
            "status_label": _NOTIFICATION_STATUS_LABELS.get(row.status, "Потрібна ручна перевірка"),
            "event_label": _NOTIFICATION_EVENT_LABELS.get(row.event_type, "Системне сповіщення"),
            "attempts": row.attempts,
            "failure_kind": row.failure_kind,
            "failure_label": _NOTIFICATION_FAILURE_LABELS.get(row.failure_kind, "Потрібна ручна перевірка"),
            "error": _notification_preview(row.last_error),
            "text_preview": _notification_preview(payload.get("text")),
            "created_at": row.created_at.isoformat(),
            "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else "",
        })
    return JsonResponse({"success": True, "items": items, "count": len(items)})


@login_required(login_url="management_login")
@require_POST
def bot_notification_review_action_api(request, notification_id):
    blocked = _require_bot_write_json(request)
    if blocked:
        return blocked
    action = (request.POST.get("action") or "").strip()
    if action not in {"resolve", "requeue"}:
        return JsonResponse({"success": False, "error": "Невідома дія."}, status=400)
    note = (request.POST.get("note") or "").strip()[:500]
    with transaction.atomic():
        row = IgBotNotification.objects.select_for_update().filter(pk=notification_id).first()
        if not row:
            return JsonResponse({"success": False, "error": "Сповіщення не знайдено."}, status=404)
        if row.status not in {IgBotNotification.Status.UNKNOWN, IgBotNotification.Status.DEAD_LETTER}:
            return JsonResponse(
                {"success": False, "error": "Сповіщення вже опрацьоване або виконується."},
                status=409,
            )
        old_status = row.status
        if action == "resolve":
            row.status = IgBotNotification.Status.RESOLVED
            row.next_attempt_at = None
            row.failure_kind = "operator_resolved"
        else:
            row.status = IgBotNotification.Status.PENDING
            row.next_attempt_at = timezone.now()
            row.attempts = 0
            row.failure_kind = "operator_requeued"
        row.save(update_fields=[
            "status", "next_attempt_at", "attempts", "failure_kind", "updated_at",
        ])
        IgBotNotificationAudit.objects.create(
            notification=row,
            actor=request.user,
            action=action,
            from_status=old_status,
            to_status=row.status,
            note=note,
        )
    bot.log(
        "warning" if action == "requeue" else "info",
        "notification_operator_action",
        f"notification={row.id}; action={action}; actor={request.user.pk}",
    )
    return JsonResponse({"success": True, "id": row.id, "status": row.status})


@login_required(login_url="management_login")
@require_GET
def bot_payment_reviews_api(request):
    blocked = _require_bot_capabilities(
        request, VIEW_IG_CONVERSATION_PII_PERMISSION
    )
    if blocked:
        return blocked
    from .ig_bot_models import IgPaymentConfirmationReview
    from management.services.ig_payment_review import payment_review_order_url

    selected_id = (request.GET.get("id") or request.GET.get("payment_review") or "").strip()
    rows_qs = IgPaymentConfirmationReview.objects.filter(
        client__hidden_at__isnull=True,
    ).select_related("client", "deal", "deal__payment_projection", "order").prefetch_related(
        "decisions__actor"
    )
    if selected_id.isdigit():
        selected_row = rows_qs.filter(pk=int(selected_id)).first()
        if (
            selected_row
            and selected_row.status == IgPaymentConfirmationReview.Status.SUPERSEDED
            and selected_row.superseded_by_id
        ):
            selected_id = str(selected_row.superseded_by_id)
        rows_qs = rows_qs.filter(pk=int(selected_id))
    else:
        rows_qs = rows_qs.filter(status=IgPaymentConfirmationReview.Status.PENDING)
    rows = rows_qs.order_by("created_at", "id")[:100]
    items = []
    for row in rows:
        evidence = row.evidence if isinstance(row.evidence, dict) else {}
        client = row.client
        draft = evidence.get("order_draft") if isinstance(evidence.get("order_draft"), dict) else {}
        decisions = [_payment_review_decision_payload(item) for item in row.decisions.all()]
        latest_decision = decisions[0] if decisions else {}
        items.append({
            "id": row.id,
            "client_id": row.client_id,
            "client": client.display_name or client.username or client.igsid,
            "status": row.status,
            "status_label": row.get_status_display(),
            "selected": selected_id.isdigit() and row.pk == int(selected_id),
            "created_at": row.created_at.isoformat(),
            "evidence": evidence.get("messages", [])[-8:],
            "media": evidence.get("media", []),
            "catalog_match": evidence.get("catalog_match", {}),
            "catalog_matches": evidence.get("catalog_matches", []),
            "deal": evidence.get("deal", {}),
            "order_draft": draft,
            "uncertainty_reasons": draft.get("uncertainty_reasons", []),
            "quoted_total": draft.get("quoted_total", ""),
            "manual_payment_truth": latest_decision.get("decision", ""),
            "provider_payment_truth": _payment_review_truth_payload(row)["provider_truth"],
            "latest_decision": latest_decision,
            "decisions": decisions,
            "confirm_url": reverse("management_bot_payment_review_action_api", args=[row.id]),
            "order_url": _existing_order_admin_url(row.order_id) if row.order_id else "",
            "create_order_url": (
                _payment_review_order_link(row, payment_review_order_url)
                if not row.order_id
                else ""
            ),
            "needs_order_resolution": bool(
                row.status == row.Status.CONFIRMED and not row.order_id
            ),
            "order_id": row.order_id,
            "order_number": row.order.order_number if row.order_id else "",
        })
    return JsonResponse({"success": True, "items": items, "count": len(items)})


def _payment_review_order_link(review, create_url_factory):
    """Link to the existing order when present, otherwise to the create form."""
    if review.order_id:
        return _existing_order_admin_url(review.order_id)
    if (
        review.status == review.Status.CONFIRMED
        and _latest_payment_review_decision(review)
        and _latest_payment_review_decision(review).decision == "manager_verified"
    ):
        return create_url_factory(review.id)
    return ""


def _existing_order_admin_url(order_id):
    """Return the staff order editor URL used by the custom admin panel."""
    try:
        path = reverse("admin_panel", urlconf="twocomms.urls")
    except Exception:
        return ""
    base = (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
    return f"{base}{path}?section=orders&edit_order={int(order_id)}"


def _payment_review_decision_payload(decision) -> dict:
    if not decision:
        return {}
    actor = getattr(decision, "actor", None)
    return {
        "id": decision.pk,
        "decision": decision.decision,
        "decision_label": decision.get_decision_display(),
        "verification_source": decision.verification_source,
        "verification_scope": decision.verification_scope,
        "confirmed_amount": (
            f"{decision.confirmed_amount:.2f}"
            if decision.confirmed_amount is not None
            else ""
        ),
        "order_total_amount": (
            f"{decision.order_total_amount:.2f}"
            if decision.order_total_amount is not None
            else ""
        ),
        "order_total_source": decision.order_total_source or "",
        "currency": decision.currency or "UAH",
        "amount_source": decision.amount_source or "",
        "amount_evidence_message_ids": decision.amount_evidence_message_ids or [],
        "reason_code": decision.reason_code,
        "reason_text": decision.reason_text,
        "actor_id": decision.actor_id,
        "actor": getattr(actor, "get_username", lambda: "")(),
        "actor_source": decision.actor_source,
        "actor_external_id": decision.actor_external_id,
        "actor_label": decision.actor_label,
        "evidence_watermark_message_id": decision.evidence_watermark_message_id,
        "review_status_before": decision.review_status_before,
        "review_status_after": decision.review_status_after,
        "stage_before": decision.stage_before,
        "stage_after": decision.stage_after,
        "created_at": decision.created_at.isoformat(),
    }


def _latest_payment_review_decision(review):
    workspace_decisions = getattr(review, "_workspace_decisions", None)
    if workspace_decisions is not None:
        return workspace_decisions[0] if workspace_decisions else None
    prefetched = getattr(review, "_prefetched_objects_cache", {}).get("decisions")
    if prefetched is not None:
        return prefetched[0] if prefetched else None
    return review.decisions.select_related("actor").order_by("-id").first()


def _payment_review_decisions(review) -> list:
    workspace_decisions = getattr(review, "_workspace_decisions", None)
    if workspace_decisions is not None:
        return workspace_decisions
    prefetched = getattr(review, "_prefetched_objects_cache", {}).get("decisions")
    if prefetched is not None:
        return list(prefetched)
    return list(review.decisions.select_related("actor").order_by("-id")[:20])


def _payment_review_workspace_queryset():
    from .ig_bot_models import (
        IgOrderAttribution,
        IgPaymentConfirmationReview,
        IgPaymentReviewDecision,
    )

    return IgPaymentConfirmationReview.objects.select_related(
        "client", "deal", "deal__payment_projection", "order",
    ).prefetch_related(
        Prefetch(
            "decisions",
            queryset=IgPaymentReviewDecision.objects.select_related("actor").order_by("-id")[:20],
            to_attr="_workspace_decisions",
        ),
        Prefetch(
            "order_attributions",
            queryset=IgOrderAttribution.objects.select_related(
                "order", "deal", "deal__payment_projection", "client",
            ).order_by("-id")[:1],
            to_attr="_workspace_attributions",
        ),
    )


def _order_attribution_workspace_queryset():
    from .ig_bot_models import IgOrderAttribution

    return IgOrderAttribution.objects.select_related(
        "client", "order", "deal", "deal__payment_projection", "payment_review",
    )


def _orders_workspace_url(*, view="all", review_id=None, client_id=None) -> str:
    base = (getattr(settings, "MANAGEMENT_BASE_URL", "") or "https://management.twocomms.shop").rstrip("/")
    params = ["section=orders", f"view={view}"]
    if review_id:
        params.append(f"review={int(review_id)}")
    if client_id:
        params.append(f"client_id={int(client_id)}")
    return f"{base}/bot/?{'&'.join(params)}"


def _payment_review_truth_payload(review, decision=None) -> dict:
    from management.services.ig_payment_review import payment_confirmation_candidate
    from management.services.ig_commercial_episodes import payment_truth_snapshot

    decision = decision or _latest_payment_review_decision(review)
    payload = payment_truth_snapshot(
        deal=review.deal if review.deal_id else None,
        review=review,
        order=review.order if review.order_id else None,
        decision=decision,
    )
    payload.update({
        "provider_source": (
            "provider_projection" if payload.get("projection_id") else "none"
        ),
        "verification_source": payload.get("manager_source", ""),
        "verification_scope": payload.get("manager_scope", ""),
        "confirmation_candidate": payment_confirmation_candidate(review),
    })
    return payload


def _bounded_text(value, limit: int) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)) or isinstance(value, bool):
        return ""
    return str(value).strip()[:limit]


def _bounded_int(value, *, minimum=0, maximum=None):
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return None
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if result < minimum or (maximum is not None and result > maximum):
        return None
    return result


def _bounded_int_list(value, *, limit=20) -> list[int]:
    if not isinstance(value, list):
        return []
    result = []
    for raw in value:
        item = _bounded_int(raw)
        if item is None:
            continue
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _bounded_scalar_map(value, *, limit=20) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for raw_key, raw_value in value.items():
        key = _bounded_text(raw_key, 64)
        scalar = _bounded_text(raw_value, 240)
        if not key or not scalar:
            continue
        result[key] = scalar
        if len(result) >= limit:
            break
    return result


def _review_media_groups(evidence: dict) -> dict:
    groups = {"receipts": [], "products": [], "custom_print": [], "unknown": []}
    rows = evidence.get("media", []) if isinstance(evidence, dict) else []
    for raw in (rows[:80] if isinstance(rows, list) else []):
        if not isinstance(raw, dict):
            continue
        item = {}
        role = _bounded_text(raw.get("role"), 32).lower()
        if role:
            item["role"] = role
        for key in ("message_id", "source_message_id", "product_id"):
            value = _bounded_int(raw.get(key))
            if value is not None:
                item[key] = value
        product_title = _bounded_text(raw.get("product_title"), 240)
        if product_title:
            item["product_title"] = product_title
        confidence = _bounded_text(raw.get("confidence"), 40)
        if confidence:
            item["confidence"] = confidence
        for key in ("url", "local_url"):
            safe_url = _safe_media_url(raw.get(key))
            if safe_url:
                item[key] = safe_url
        safe_product_url = _safe_storefront_url(raw.get("product_url"))
        if safe_product_url:
            item["product_url"] = safe_product_url
        group_role = role or "other"
        if group_role in {"receipt", "payment_candidate"}:
            groups["receipts"].append(item)
        elif group_role in {"product", "purchase_candidate", "interest"}:
            groups["products"].append(item)
        elif group_role in {"custom_reference", "custom_print", "custom_candidate"}:
            groups["custom_print"].append(item)
        else:
            groups["unknown"].append(item)
    return {key: values[:20] for key, values in groups.items()}


def _safe_relative_url(value: str) -> str:
    value = _bounded_text(value, 1200)
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return ""


def _safe_media_url(value) -> str:
    value = _bounded_text(value, 1200)
    relative = _safe_relative_url(value)
    if relative:
        return relative
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return value


# Ссылки Meta на вложения подписаны и живут недолго: один и тот же ассет
# приходит каждый раз с новой `signature`, а через некоторое время перестаёт
# открываться вовсе. Локальных копий нет (F-DATA-011: 100% HTTP 404 при
# скачивании), поэтому ассет опознаём по `asset_id`.
_PROVIDER_MEDIA_HOSTS = ("lookaside.fbsbx.com", "scontent.cdninstagram.com")
_MESSAGE_MEDIA_LIMIT = 8


def _media_asset_key(url: str) -> str:
    """Stable identity of one attachment across re-signed provider links."""
    value = str(url or "")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.hostname in _PROVIDER_MEDIA_HOSTS:
        for chunk in parsed.query.split("&"):
            if chunk.startswith("asset_id="):
                return f"{parsed.hostname}:{chunk}"
    return value


def _is_provider_media_link(url: str) -> bool:
    try:
        return urlsplit(str(url or "")).hostname in _PROVIDER_MEDIA_HOSTS
    except ValueError:
        return False


def _message_media_rows(message, media_evidence) -> list[dict]:
    """Redacted per-part capture and inspection state for one message.

    ``sales_context["_media_evidence"]`` is scoring telemetry, not a transcript:
    its ``source_message_id`` is whatever message ``classify_message`` was called
    with, so on re-analysis it points at the message being processed rather than
    at the one the attachment belongs to. That is how two product images ended up
    glued to a bare «Дякую» on production.

    The evidence still answers a question it can answer — what role and intent
    were inferred for that asset — and is looked up by asset identity.
    """
    from management.services.ig_media_manifest import (
        MediaManifestError,
        normalize_attachment_media,
        public_media_manifest,
    )

    try:
        media = normalize_attachment_media(
            getattr(message, "attachment_media", None) or [],
            message_scope=getattr(message, "pk", ""),
        )
        manifest = public_media_manifest(media)
    except MediaManifestError:
        return []
    artifact = getattr(message, "turn_intelligence_artifact", None)
    artifact = artifact if isinstance(artifact, dict) else {}
    request = artifact.get("media_request") if isinstance(artifact.get("media_request"), dict) else {}
    rows = []
    for item, public in list(zip(media, manifest, strict=True))[:_MESSAGE_MEDIA_LIMIT]:
        inspection = item.get("inspection") if isinstance(item.get("inspection"), dict) else {}
        preview_url = ""
        if (
            item.get("status") == "owned"
            and item.get("private_storage") is True
            and str(item.get("mime") or "").startswith("image/")
        ):
            try:
                preview_url = reverse(
                    "management_bot_private_media_preview",
                    args=[message.pk, public["source_part_id"]],
                    urlconf="management.urls",
                )
            except Exception:
                preview_url = ""
        rows.append({
            **public,
            "type_code": _bounded_text(inspection.get("type_code"), 32),
            "error_kind": _bounded_text(item.get("error_kind"), 64),
            "preview_url": preview_url,
            "model": _bounded_text(inspection.get("provider_model"), 80),
            "effort": "unknown",
            "request_id": _bounded_text(inspection.get("request_id"), 40),
            "request_model": _bounded_text(request.get("provider_model"), 80),
        })
    if rows:
        return rows
    # Older transcript rows can have only the legacy attachment JSON. Never
    # revive an incoming signed CDN URL: show its bounded missing-part state.
    try:
        legacy_attachments = json.loads(str(getattr(message, "attachments", "") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        legacy_attachments = []
    if not isinstance(legacy_attachments, list):
        return rows
    if (
        getattr(message, "role", "") == "model"
        and getattr(message, "source", "") == "catalog_media"
    ):
        for candidate in legacy_attachments[:_MESSAGE_MEDIA_LIMIT]:
            public_url = _safe_storefront_url(candidate)
            if any(part in str(public_url).casefold() for part in (
                "/ig_message_media/", "/ig_payment_reviews/", "/bot/private-media/",
            )):
                public_url = ""
            if public_url:
                rows.append({
                    "public_url": public_url,
                    "role": "product",
                    "capture_state": "not_applicable",
                    "inspection_state": "not_applicable",
                    "inspection_outcome": "",
                    "effort": "unknown",
                })
        return rows
    for original_index, _candidate in enumerate(legacy_attachments[:_MESSAGE_MEDIA_LIMIT]):
        rows.append({
            "original_index": original_index,
            "capture_state": "unknown",
            "inspection_state": "uninspected",
            "inspection_outcome": "not_captured",
            "error_kind": "legacy_media_unavailable",
            "effort": "unknown",
        })
    return rows


def _safe_storefront_url(value) -> str:
    value = _bounded_text(value, 1200)
    relative = _safe_relative_url(value)
    if relative:
        base = (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
        return f"{base}{relative}"[:1200]
    try:
        parsed = urlsplit(value)
        configured_host = urlsplit(
            getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop"
        ).hostname
    except ValueError:
        return ""
    trusted_hosts = {"twocomms.shop", "www.twocomms.shop"}
    if configured_host:
        trusted_hosts.add(configured_host.lower())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() not in trusted_hosts
        or parsed.username
        or parsed.password
    ):
        return ""
    return value


def _catalog_workspace_candidate(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    item = {}
    for key in ("product_id", "color_variant_id"):
        value = _bounded_int(raw.get(key))
        if value is not None:
            item[key] = value
    for key, limit in (
        ("status", 40),
        ("title", 240),
        ("slug", 180),
        ("catalog_price", 40),
        ("confidence", 40),
        ("reason", 240),
    ):
        value = _bounded_text(raw.get(key), limit)
        if value:
            item[key] = value
    for key in ("url", "product_url"):
        safe_url = _safe_storefront_url(raw.get(key))
        if safe_url:
            item[key] = safe_url
    for key in ("source_media_indexes", "source_message_ids"):
        values = _bounded_int_list(raw.get(key))
        if values:
            item[key] = values
    variants = []
    raw_variants = raw.get("variant_candidates")
    for raw_variant in (raw_variants[:20] if isinstance(raw_variants, list) else []):
        if not isinstance(raw_variant, dict):
            continue
        variant = {}
        variant_id = _bounded_int(raw_variant.get("id"))
        if variant_id is not None:
            variant["id"] = variant_id
        for key in ("color", "sku"):
            value = _bounded_text(raw_variant.get(key), 80)
            if value:
                variant[key] = value
        if variant:
            variants.append(variant)
    if variants:
        item["variant_candidates"] = variants
    return item


def _catalog_workspace_candidates(evidence: dict) -> list:
    rows = evidence.get("catalog_matches", []) if isinstance(evidence, dict) else []
    result = []
    for raw in (rows[:20] if isinstance(rows, list) else []):
        if not isinstance(raw, dict):
            continue
        item = _catalog_workspace_candidate(raw)
        if item:
            result.append(item)
    return result


def _draft_workspace_items(raw_items) -> list:
    result = []
    for raw in (raw_items[:20] if isinstance(raw_items, list) else []):
        if not isinstance(raw, dict):
            continue
        item = {}
        for key in ("product_id", "color_variant_id", "source_message_id"):
            value = _bounded_int(raw.get(key))
            if value is not None:
                item[key] = value
        qty = _bounded_int(raw.get("qty"), minimum=1, maximum=999)
        if qty is not None:
            item["qty"] = qty
        for key, limit in (
            ("title", 240),
            ("size", 40),
            ("fit", 40),
            ("fit_option_code", 80),
            ("fit_option_label", 160),
            ("unit_price", 40),
            ("line_total", 40),
            ("price_source", 80),
        ):
            value = _bounded_text(raw.get(key), limit)
            if value:
                item[key] = value
        evidence_ids = _bounded_int_list(raw.get("price_evidence_message_ids"))
        if evidence_ids:
            item["price_evidence_message_ids"] = evidence_ids
        for key in ("option_values", "option_labels"):
            values = _bounded_scalar_map(raw.get(key))
            if values:
                item[key] = values
        catalog = _catalog_workspace_candidate(raw.get("catalog"))
        if catalog:
            item["catalog"] = catalog
        if item:
            result.append(item)
    return result


def _draft_workspace_delivery(raw_delivery) -> dict:
    if not isinstance(raw_delivery, dict):
        return {}
    result = {}
    for key, limit in (
        ("full_name", 180),
        ("phone", 40),
        ("email", 254),
        ("city", 120),
        ("office", 180),
        ("address", 240),
        ("np_settlement_ref", 64),
        ("np_city_ref", 64),
        ("np_warehouse_ref", 64),
    ):
        value = _bounded_text(raw_delivery.get(key), limit)
        if value:
            result[key] = value
    return result


def _draft_workspace_reasons(raw_reasons) -> list[str]:
    if not isinstance(raw_reasons, list):
        return []
    result = []
    for raw in raw_reasons:
        reason = _bounded_text(raw, 240)
        if reason:
            result.append(reason)
        if len(result) >= 20:
            break
    return result


def _amount_workspace_evidence(evidence) -> list[dict]:
    if not isinstance(evidence, dict):
        return []
    result = []
    for raw in evidence.get("amount_evidence") or []:
        if not isinstance(raw, dict):
            continue
        message_id = _bounded_int(raw.get("message_id"))
        amount = _bounded_text(raw.get("amount"), 40)
        if not message_id or not amount:
            continue
        result.append({
            "message_id": message_id,
            "amount": amount,
            "kind": _bounded_text(raw.get("kind"), 40),
            "role": _bounded_text(raw.get("role"), 24),
            "quote": _bounded_text(raw.get("quote"), 300),
        })
        if len(result) >= 20:
            break
    return result


def _order_workspace_order_payload(order) -> dict:
    if not order:
        return {}
    from management.services.ig_order_amounts import order_amounts

    amounts = order_amounts(order)
    return {
        "id": order.pk,
        "number": order.order_number,
        "status": order.status,
        "status_label": order.get_status_display(),
        "payment_status": order.payment_status,
        "payment_status_label": order.get_payment_status_display(),
        "amount": f"{amounts['payable']:.2f}",
        "subtotal": f"{amounts['subtotal']:.2f}",
        "discount_amount": f"{amounts['discount']:.2f}",
        "sale_source": order.sale_source,
        "tracking_number": order.tracking_number or "",
        "url": _existing_order_admin_url(order.pk),
    }


def _manual_order_url_for_client(client_id: int) -> str:
    """Return the signed storefront manual-order URL from management."""

    token = signing.dumps(
        {"client_id": int(client_id)},
        salt="storefront.manual-order.ig-client",
    )
    try:
        path = reverse("manual_order_create", urlconf="twocomms.urls")
    except Exception:
        return ""
    base = (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
    return f"{base}{path}?" + urlencode({
        "ig_client": int(client_id),
        "ig_client_token": token,
    })


def _order_ttn_action_url(order_id: int) -> str:
    """Return the absolute storefront TTN action URL from management."""

    try:
        path = reverse(
            "admin_order_nova_poshta_action",
            args=[int(order_id)],
            urlconf="twocomms.urls",
        )
    except Exception:
        return ""
    base = (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
    return f"{base}{path}"


def _assignment_workspace_payload(assignment, *, client_id=None) -> dict:
    """Serialize current ownership and staff-only fulfillment capabilities."""

    order = assignment.order
    actor = assignment.assigned_by
    actor_name = ""
    if actor is not None:
        actor_name = actor.get_full_name() or actor.get_username()
    active = bool(
        assignment.client_id
        and assignment.unassigned_at is None
        and (client_id is None or assignment.client_id == client_id)
    )
    ttn_action_url = _order_ttn_action_url(order.pk)
    tracking_number = (order.tracking_number or "").strip()
    terminal = order.status in {"done", "cancelled"}
    source_label = assignment.get_source_display()
    context_client_id = client_id or assignment.client_id
    manual_order_url = (
        _manual_order_url_for_client(context_client_id)
        if context_client_id
        else ""
    )
    return {
        "id": assignment.pk,
        "state": "active" if active else "unassigned",
        "client_id": assignment.client_id,
        "version": assignment.version,
        "source": assignment.source,
        "source_label": source_label,
        "actor": {
            "id": assignment.assigned_by_id,
            "name": actor_name,
        },
        "linked_at": assignment.assigned_at.isoformat() if assignment.assigned_at else "",
        "unassigned_at": assignment.unassigned_at.isoformat() if assignment.unassigned_at else "",
        "reason_code": assignment.last_reason_code or "",
        "reason": assignment.last_reason or "",
        "order": _order_workspace_order_payload(order),
        "can_unlink": active,
        "capabilities": {
            "can_create_ttn": bool(active and not tracking_number and not terminal),
            "can_unlink_api_ttn": bool(active and tracking_number and not terminal),
            "can_edit_manual_ttn": bool(active and not terminal),
            "ttn_action_url": ttn_action_url,
            "tracking_url": (
                f"https://novaposhta.ua/tracking/?cargo_number={tracking_number}"
                if tracking_number
                else ""
            ),
            "manual_order_url": manual_order_url,
        },
    }


def _assignment_event_workspace_payload(event) -> dict:
    actor = event.actor
    actor_name = ""
    if actor is not None:
        actor_name = actor.get_full_name() or actor.get_username()
    return {
        "id": event.pk,
        "kind": event.kind,
        "kind_label": event.get_kind_display(),
        "actor": actor_name or event.get_actor_source_display(),
        "source": event.get_assignment_source_display(),
        "version": event.assignment_version,
        "created_at": event.created_at.isoformat() if event.created_at else "",
        "reason_code": event.reason_code or "",
        "reason": event.reason or "",
        "order": _order_workspace_order_payload(event.order),
    }


def _post_sale_case_payload(case) -> dict:
    from management.services.ig_shipments import order_shipment_rows

    order = case.order if case.order_id else None
    shipments = order_shipment_rows(order)
    return {
        "id": case.pk,
        "case_type": case.case_type,
        "case_type_label": case.get_case_type_display(),
        "status": case.status,
        "status_label": case.get_status_display(),
        "needs_action": case.status in {"needs_details", "open"},
        "order": (_order_workspace_order_payload(order) if order else None),
        "commercial_episode_id": case.commercial_episode_id,
        "source_message_id": case.source_message_id,
        "evidence_message_ids": list(case.evidence_message_ids or [])[:20],
        "source_item_title": case.source_item_title,
        "source_fit": case.source_fit,
        "source_size": case.source_size,
        "requested_fit": case.requested_fit,
        "requested_size": case.requested_size,
        "reason": case.reason,
        "manager_note": case.manager_note,
        # Таймлайн отправок отвечает на вопрос, который менеджер задавал глазами:
        # это тот же заказ, по нему был обмен, вот что уехало и что вернулось.
        "shipments": shipments,
        "has_return_shipment": any(
            row["purpose"] == "return_inbound" for row in shipments
        ),
        "has_replacement_shipment": any(
            row["purpose"] == "exchange_replacement" for row in shipments
        ),
        "created_at": case.created_at.isoformat(),
        "updated_at": case.updated_at.isoformat(),
        "resolved_at": case.resolved_at.isoformat() if case.resolved_at else "",
        "action_url": reverse(
            "management_bot_post_sale_case_api",
            args=[case.client_id, case.pk],
        ),
    }


def _post_sale_workspace_payload(client) -> dict:
    from .ig_bot_models import IgOrderAssignment, IgOrderAttribution, IgPostSaleCase

    cases = list(
        IgPostSaleCase.objects.filter(client=client)
        .select_related("order", "commercial_episode", "source_message")
        .order_by("-updated_at", "-id")[:20]
    )
    assignments = list(
        IgOrderAssignment.objects.filter(client=client, unassigned_at__isnull=True)
        .select_related("order")
        .order_by("-assigned_at", "-id")[:30]
    )
    assigned_order_ids = {row.order_id for row in assignments}
    attributions = list(
        IgOrderAttribution.objects.filter(
            client=client,
            order__instagram_assignment__isnull=True,
        )
        .select_related("order")
        .order_by("-created_at", "-id")[:30]
    )
    seen = set()
    choices = []
    for assignment in assignments:
        if assignment.order_id in seen:
            continue
        seen.add(assignment.order_id)
        choices.append(_order_workspace_order_payload(assignment.order))
    for attribution in attributions:
        if attribution.order_id in seen or attribution.order_id in assigned_order_ids:
            continue
        seen.add(attribution.order_id)
        choices.append(_order_workspace_order_payload(attribution.order))
    actionable = {IgPostSaleCase.Status.NEEDS_DETAILS, IgPostSaleCase.Status.OPEN}
    return {
        "action_count": sum(case.status in actionable for case in cases),
        "items": [_post_sale_case_payload(case) for case in cases],
        "order_choices": choices,
    }


def _order_attribution_workspace_payload(attribution) -> dict:
    order = getattr(attribution, "order", None)
    deal = getattr(attribution, "deal", None)
    projection = None
    if deal:
        try:
            projection = deal.payment_projection
        except IgPaymentProjection.DoesNotExist:
            projection = None
    provider_source = attribution.payment_source if attribution.payment_source.startswith("provider_") else "none"
    provider_truth = projection.truth if projection else IgDeal.PaymentTruth.UNVERIFIED
    if (
        attribution.payment_source == "provider_projection"
        and provider_truth == IgDeal.PaymentTruth.UNVERIFIED
    ):
        provider_truth = IgDeal.PaymentTruth.CONFIRMED
    manager_truth = (
        "manager_verified"
        if attribution.payment_source == "manager_verified"
        else ""
    )
    if attribution.creation_mode == "linked_existing":
        approval_state = "linked_existing"
    elif attribution.creation_mode == "manager_review":
        approval_state = "created_new"
    else:
        approval_state = "confirmed"
    client = attribution.client
    items = _draft_workspace_items(attribution.item_provenance)
    from management.services.ig_order_amounts import order_amounts

    order_payment_total = order_amounts(order)["payable"]
    return {
        "id": f"order-{order.pk}",
        "card_key": f"order:{order.pk}",
        "attribution_id": attribution.pk,
        "review_id": attribution.payment_review_id,
        "client": {
            "id": client.pk,
            "name": client.display_name or client.username or client.igsid,
            "username": client.username or "",
            "igsid": client.igsid,
            "avatar": client.avatar_local or client.profile_pic_url or "",
        },
        "approval": {
            "state": approval_state,
            "status": "confirmed",
            "status_label": "Підтверджено",
            "needs_action": False,
            "can_confirm": False,
            "can_reject": False,
            "can_link_existing": False,
            "can_create": False,
            "action_url": "",
            "create_order_url": "",
            "link_existing": {},
        },
        "draft": {
            "items": items,
            "quoted_total": (
                str(attribution.negotiated_total)
                if attribution.negotiated_total is not None
                else f"{order_payment_total:.2f}"
            ),
            "packaging_preference": "",
            "delivery": {},
            "uncertainty_reasons": [],
            "catalog_candidates": [],
        },
        "media": {"receipts": [], "products": [], "custom_print": [], "unknown": []},
        "payment": {
            "order_subtotal": f"{order_amounts(order)['subtotal']:.2f}",
            "order_discount_amount": f"{order_amounts(order)['discount']:.2f}",
            "order_total": f"{order_payment_total:.2f}",
            "provider_truth": provider_truth,
            "provider_source": provider_source,
            "manager_truth": manager_truth,
            "verification_source": "manager" if manager_truth else "",
            "verification_scope": "",
            "authoritative_for_fulfillment": bool(
                manager_truth == "manager_verified"
                or provider_truth in {
                    IgDeal.PaymentTruth.CONFIRMED,
                    IgDeal.PaymentTruth.PARTIALLY_REFUNDED,
                }
            ),
        },
        "order": _order_workspace_order_payload(order),
        "fulfillment": {
            "deal_id": attribution.deal_id,
            "delivery_status": getattr(deal, "delivery_status", "") if deal else "",
            "delivery_status_label": (
                deal.get_delivery_status_display()
                if deal and deal.delivery_status
                else ""
            ),
            "delivery_error": getattr(deal, "delivery_error", "") if deal else "",
            "has_validated_refs": bool(
                (deal and deal.np_settlement_ref and deal.np_city_ref and deal.np_warehouse_ref)
                or (order and order.np_settlement_ref and order.np_city_ref and order.np_warehouse_ref)
            ),
        },
        "decision": {},
        "decision_history": [],
        "order_url": _existing_order_admin_url(order.pk),
        "workspace_url": _orders_workspace_url(
            view="confirmed",
            client_id=client.pk,
        ),
        "origin": {
            "creation_mode": attribution.creation_mode,
            "creation_mode_label": attribution.get_creation_mode_display(),
            "payment_source": attribution.payment_source,
            "payment_source_label": attribution.get_payment_source_display(),
            "price_source": attribution.price_source,
        },
        "created_at": attribution.created_at.isoformat(),
        "updated_at": attribution.created_at.isoformat(),
    }


def _payment_review_workspace_payload(review) -> dict:
    from management.services.ig_payment_review import (
        is_legacy_historical_payment_review,
        payment_review_order_url,
    )

    evidence = review.evidence if isinstance(review.evidence, dict) else {}
    draft = evidence.get("order_draft") if isinstance(evidence.get("order_draft"), dict) else {}
    decisions = _payment_review_decisions(review)
    decision = decisions[0] if decisions else None
    payment = _payment_review_truth_payload(review, decision)
    order = getattr(review, "order", None) if review.order_id else None
    deal = getattr(review, "deal", None) if review.deal_id else None
    attributions = getattr(review, "_workspace_attributions", None)
    if attributions is None:
        attributions = list(review.order_attributions.select_related("order").order_by("-id")[:1])
    attribution = attributions[0] if attributions else None
    status = review.status
    historical_paid_archived = bool(
        review.resolution_kind
        == review.ResolutionKind.HISTORICAL_PAID_ARCHIVED
    )
    provider_terminal_conflict = payment.get("provider_truth") in {
        "partially_refunded",
        "refunded",
        "reversed",
        "failed",
        "cancelled",
    }
    can_historical_complete = bool(
        status == review.Status.PENDING
        and is_legacy_historical_payment_review(review)
        and review.client.hidden_at is None
        and not review.order_id
        and not (deal and deal.active_checkout_proposal_id)
        and not provider_terminal_conflict
    )
    historical_outcomes = (
        [
            {"value": value, "label": str(label)}
            for value, label in review.ResolutionOutcome.choices
        ]
        if can_historical_complete or historical_paid_archived
        else []
    )
    if historical_paid_archived:
        approval_state = "historical_paid_archived"
    elif status == review.Status.SUPERSEDED:
        approval_state = "superseded"
    elif status == review.Status.CONFIRMED and payment["needs_reconciliation"]:
        approval_state = "payment_reconciliation"
    elif review.order_id:
        if attribution and attribution.creation_mode == "linked_existing":
            approval_state = "linked_existing"
        elif attribution and attribution.creation_mode in {"manager_review", "provider_auto"}:
            approval_state = "created_new"
        else:
            approval_state = "order_created"
    elif status == review.Status.CONFIRMED:
        approval_state = "needs_order_resolution"
    elif status == review.Status.CANCELLED:
        approval_state = "rejected"
    else:
        approval_state = "pending"
    action_url = reverse("management_bot_payment_review_action_api", args=[review.pk])
    create_order_url = _payment_review_order_link(review, payment_review_order_url)
    order_url = _existing_order_admin_url(review.order_id) if review.order_id else ""
    needs_order_resolution = bool(
        status == review.Status.CONFIRMED
        and not review.order_id
        and not historical_paid_archived
        and payment["authoritative_for_fulfillment"]
    )
    needs_amount_clarification = bool(
        status == review.Status.CONFIRMED
        and not review.order_id
        and not historical_paid_archived
        and not payment["needs_reconciliation"]
        and not payment["authoritative_for_fulfillment"]
        and decision
        and decision.decision == "manager_verified"
    )
    if needs_amount_clarification:
        approval_state = "amount_clarification"
    return {
        "id": review.pk,
        "card_key": f"review:{review.pk}",
        "review_id": review.pk,
        "client": {
            "id": review.client_id,
            "name": review.client.display_name or review.client.username or review.client.igsid,
            "username": review.client.username or "",
            "igsid": review.client.igsid,
            "avatar": review.client.avatar_local or review.client.profile_pic_url or "",
        },
        "approval": {
            "state": approval_state,
            "status": status,
            "status_label": review.get_status_display(),
            "needs_action": (
                not historical_paid_archived
                and (
                    status == review.Status.PENDING
                    or needs_order_resolution
                    or needs_amount_clarification
                    or payment["needs_reconciliation"]
                )
            ),
            "can_confirm": status == review.Status.PENDING,
            "can_reject": status == review.Status.PENDING,
            "can_link_existing": needs_order_resolution,
            "can_create": needs_order_resolution,
            "can_clarify_amount": needs_amount_clarification,
            "can_historical_complete": can_historical_complete,
            "historical_outcomes": historical_outcomes,
            "superseded_by_review_id": review.superseded_by_id if status == review.Status.SUPERSEDED else None,
            "resolution_kind": review.resolution_kind,
            "resolution_outcome": review.resolution_outcome,
            "resolution_note": review.resolution_note,
            "resolved_at": review.resolved_at.isoformat() if review.resolved_at else "",
            "action_url": action_url,
            "create_order_url": create_order_url if needs_order_resolution else "",
            "link_existing": {
                "action": "link_order",
                "action_url": action_url if needs_order_resolution else "",
                "requires_exact_order_identifier": True,
            },
        },
        "draft": {
            "items": _draft_workspace_items(draft.get("items")),
            "quoted_total": _bounded_text(draft.get("quoted_total"), 40),
            "packaging_preference": _bounded_text(
                draft.get("packaging_preference"), 160,
            ),
            "delivery": _draft_workspace_delivery(draft.get("delivery")),
            "uncertainty_reasons": _draft_workspace_reasons(
                draft.get("uncertainty_reasons"),
            ),
            "catalog_candidates": _catalog_workspace_candidates(evidence),
            "amount_evidence": _amount_workspace_evidence(evidence),
        },
        "media": _review_media_groups(evidence),
        "payment": payment,
        "fulfillment": {
            "deal_id": review.deal_id,
            "delivery_status": getattr(deal, "delivery_status", "") if deal else "",
            "delivery_status_label": deal.get_delivery_status_display() if deal and deal.delivery_status else "",
            "delivery_error": getattr(deal, "delivery_error", "") if deal else "",
            "has_validated_refs": bool(
                deal
                and deal.np_settlement_ref
                and deal.np_city_ref
                and deal.np_warehouse_ref
            ),
        },
        "decision": _payment_review_decision_payload(decision),
        "decision_history": [_payment_review_decision_payload(item) for item in decisions[:20]],
        "order": _order_workspace_order_payload(order),
        "order_url": order_url,
        "workspace_url": _orders_workspace_url(
            view=(
                "action"
                if status == review.Status.PENDING
                or needs_order_resolution
                or needs_amount_clarification
                or payment["needs_reconciliation"]
                else "confirmed"
            ),
            review_id=review.pk,
        ),
        "origin": {
            "creation_mode": attribution.creation_mode if attribution else "",
            "creation_mode_label": attribution.get_creation_mode_display() if attribution else "",
            "payment_source": attribution.payment_source if attribution else "",
            "payment_source_label": attribution.get_payment_source_display() if attribution else "",
            "price_source": attribution.price_source if attribution else "",
        },
        "created_at": review.created_at.isoformat(),
        "updated_at": review.updated_at.isoformat(),
    }


def _canonical_order_workspace_cards(review_rows, attribution_rows, *, limit=100) -> list:
    review_cards = []
    represented_order_ids = set()
    for row in review_rows:
        card = _payment_review_workspace_payload(row)
        order_id = (card.get("order") or {}).get("id")
        # A superseded review may retain the canonical order pointer for audit
        # history. It must not render as a second physical-order card.
        if order_id and order_id in represented_order_ids:
            continue
        review_cards.append(card)
        if order_id:
            represented_order_ids.add(order_id)
    attribution_cards = [
        _order_attribution_workspace_payload(row)
        for row in attribution_rows
        if row.order_id not in represented_order_ids
    ]
    cards = review_cards + attribution_cards
    cards.sort(key=lambda item: (item.get("created_at", ""), str(item.get("id", ""))), reverse=True)
    return cards[:limit]


@login_required(login_url="management_login")
@require_GET
def bot_orders_workspace_api(request):
    """Bounded source of truth for the dedicated ``Замовлення`` workspace."""
    blocked = _require_bot_capabilities(
        request, VIEW_IG_CONVERSATION_PII_PERMISSION
    )
    if blocked:
        return blocked
    from .ig_bot_models import IgPaymentConfirmationReview

    view = (request.GET.get("view") or "action").strip().lower()
    if view not in {"action", "confirmed", "all"}:
        view = "action"
    try:
        client_id = int(request.GET.get("client_id") or 0)
    except (TypeError, ValueError):
        client_id = 0
    try:
        limit = max(1, min(int(request.GET.get("limit") or 50), 100))
    except (TypeError, ValueError):
        limit = 50
    try:
        selected_review_id = int(
            request.GET.get("review")
            or request.GET.get("payment_review")
            or 0
        )
    except (TypeError, ValueError):
        selected_review_id = 0

    base = _payment_review_workspace_queryset().filter(
        client__hidden_at__isnull=True,
    ).exclude(status=IgPaymentConfirmationReview.Status.SUPERSEDED)
    attribution_base = _order_attribution_workspace_queryset().filter(
        client__hidden_at__isnull=True,
    )
    if client_id:
        base = base.filter(client_id=client_id)
        attribution_base = attribution_base.filter(client_id=client_id)
    # Keep payment conflicts actionable without performing the old implicit
    # reconciliation writes from this GET endpoint.
    reconciliation_review_ids = [
        row.pk
        for row in base.filter(
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
            order_id__isnull=False,
        ).exclude(
            resolution_kind=(
                IgPaymentConfirmationReview.ResolutionKind.HISTORICAL_PAID_ARCHIVED
            )
        ).select_related("deal", "order", "deal__payment_projection").prefetch_related("decisions")
        if _payment_review_truth_payload(
            row,
            decision=max(row.decisions.all(), key=lambda decision: decision.pk, default=None),
        )["needs_reconciliation"]
    ]
    represented_order_ids = base.exclude(order_id__isnull=True).values("order_id")
    attribution_base = attribution_base.exclude(order_id__in=Subquery(represented_order_ids))
    attributed_count = attribution_base.count()
    action_filter = (
        Q(status=IgPaymentConfirmationReview.Status.PENDING)
        | (
            Q(
                status=IgPaymentConfirmationReview.Status.CONFIRMED,
                order_id__isnull=True,
            )
            & ~Q(
                resolution_kind=(
                    IgPaymentConfirmationReview.ResolutionKind.HISTORICAL_PAID_ARCHIVED
                )
            )
        )
        | Q(pk__in=reconciliation_review_ids)
    )
    canonical_order_count = base.exclude(order_id__isnull=True).values("order_id").distinct().count()
    canonical_orderless_count = base.filter(order_id__isnull=True).count()
    canonical_confirmed_order_count = base.filter(
        status=IgPaymentConfirmationReview.Status.CONFIRMED,
    ).exclude(order_id__isnull=True).values("order_id").distinct().count()
    canonical_confirmed_orderless_count = base.filter(
        status=IgPaymentConfirmationReview.Status.CONFIRMED,
        order_id__isnull=True,
    ).count()
    counts = {
        "action": base.filter(action_filter).count(),
        "confirmed": (
            canonical_confirmed_order_count
            + canonical_confirmed_orderless_count
            + attributed_count
        ),
        "all": canonical_order_count + canonical_orderless_count + attributed_count,
    }
    attribution_rows = []
    if selected_review_id:
        selected_row = base.filter(pk=selected_review_id).first()
        if selected_row is None:
            superseded = IgPaymentConfirmationReview.objects.filter(
                pk=selected_review_id,
                status=IgPaymentConfirmationReview.Status.SUPERSEDED,
            ).values_list("superseded_by_id", flat=True).first()
            if superseded:
                selected_row = base.filter(pk=superseded).first()
        rows = base.filter(pk=selected_row.pk) if selected_row else base.none()
        selected_review_id = selected_row.pk if selected_row else 0
    elif view == "action":
        rows = base.filter(action_filter)
    elif view == "confirmed":
        rows = base.filter(status=IgPaymentConfirmationReview.Status.CONFIRMED)
        attribution_rows = list(attribution_base.order_by("-created_at", "-id")[:limit])
    else:
        rows = base
        attribution_rows = list(attribution_base.order_by("-created_at", "-id")[:limit])
    review_rows = list(rows.order_by("-created_at", "-id")[:limit])
    items = _canonical_order_workspace_cards(
        review_rows,
        attribution_rows,
        limit=limit,
    )
    return JsonResponse({
        "success": True,
        "section": "orders",
        "view": view,
        "selected_review_id": selected_review_id or None,
        "counts": counts,
        "items": items,
    })


def _mask_checkout_contact(value, *, kind):
    value = str(value or "")
    if kind == "email" and "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:1]}***@{domain}"
    if kind == "name":
        parts = [part for part in value.split() if part]
        return " ".join(f"{part[:1]}***" for part in parts) if parts else "—"
    if kind == "location":
        parts = [part for part in value.split() if part]
        if not parts:
            return "—"
        masked = []
        for part in parts:
            if part.startswith("№"):
                masked.append("№***")
            elif part[:1].isalpha():
                masked.append(f"{part[:1]}***")
            else:
                masked.append("***")
        return " ".join(masked)
    digits = "".join(ch for ch in value if ch.isdigit())
    return f"+380 ** *** ** {digits[-4:]}" if digits else "—"


def _checkout_resend_window_deadline(proposal):
    from management.services.bot_followups import meta_window_deadline

    return meta_window_deadline(proposal.client)


def _checkout_proposal_action_capabilities(proposal, *, now=None):
    now = now or timezone.now()
    state = proposal.status
    if proposal.expires_at <= now and state in {
        proposal.Status.READY,
        proposal.Status.VIEWED,
    }:
        state = proposal.Status.EXPIRED

    link_states = {
        proposal.Status.READY,
        proposal.Status.VIEWED,
        proposal.Status.DETAILS_LOCKED,
        proposal.Status.INVOICE_CREATED,
    }
    link_available = proposal.expires_at > now and state in link_states
    provider_cancellation_required = bool(
        proposal.payment_attempt_id
        and state in {
            proposal.Status.DETAILS_LOCKED,
            proposal.Status.INVOICE_CREATED,
        }
        and not proposal.has_provider_confirmed_cancellation()
    )
    resend_window_deadline = _checkout_resend_window_deadline(proposal)
    resend_window_closed = bool(
        link_available
        and resend_window_deadline
        and resend_window_deadline <= now
    )
    can_revoke = state in {
        proposal.Status.READY,
        proposal.Status.VIEWED,
    } or (
        state == proposal.Status.DETAILS_LOCKED
        and not provider_cancellation_required
    )
    return {
        "can_copy_token": link_available,
        "can_resend": link_available and not resend_window_closed,
        "resend_blocked_reason": (
            "response_window_closed" if resend_window_closed else ""
        ),
        "can_revoke": can_revoke,
        "revoke_blocked_reason": (
            "provider_cancellation_required" if provider_cancellation_required else ""
        ),
    }


def _checkout_lifecycle_history_entry(event):
    error_code = ""
    if event.last_error:
        error_code = {
            IgLifecycleEvent.State.WAITING_WINDOW: "response_window_closed",
            IgLifecycleEvent.State.MANAGER_REVIEW: "manager_review_required",
            IgLifecycleEvent.State.AMBIGUOUS: "delivery_ambiguous",
            IgLifecycleEvent.State.FAILED: "delivery_failed",
            IgLifecycleEvent.State.CANCELLED: "delivery_cancelled",
        }.get(event.state, "delivery_issue")
    return {
        "kind": event.kind,
        "state": event.state,
        "attempts": event.attempts,
        "has_provider_receipt": bool(event.provider_message_id),
        "error_code": error_code,
        "created_at": event.created_at,
    }


def _checkout_proposal_workspace_payload(proposal, *, include_history=False):
    attempt = proposal.payment_attempt
    state = proposal.status
    if proposal.is_expired and state in {proposal.Status.READY, proposal.Status.VIEWED}:
        state = proposal.Status.EXPIRED
    items = [
        {
            "title": item.product_title,
            "sku": item.sku,
            "color": item.color_label,
            "fit": item.fit_label,
            "size": item.size,
            "quantity": item.quantity,
            "line_total": str(item.quoted_line_total),
        }
        for item in proposal.items.order_by("position", "id")
    ]
    capabilities = _checkout_proposal_action_capabilities(proposal)
    payload = {
        "id": proposal.pk,
        "public_id": str(proposal.public_id),
        "state": state,
        "state_label": dict(proposal.Status.choices).get(state, proposal.get_status_display()),
        "revision": proposal.revision,
        "client": {
            "id": proposal.client_id,
            "label": f"IG client #{proposal.client_id}",
        },
        "amount": str(proposal.requested_payment_amount),
        "currency": proposal.currency,
        "item_count": len(items),
        "items": items,
        "expires_at": proposal.expires_at.isoformat(),
        "delivery": None,
        "invoice": {
            "status": attempt.status if attempt else "",
            "reference": attempt.reference if attempt else "",
            "has_invoice": bool(attempt and attempt.invoice_url),
        },
        "actions": capabilities,
    }
    if attempt and attempt.full_name:
        payload["delivery"] = {
            "recipient": _mask_checkout_contact(attempt.full_name, kind="name"),
            "phone": _mask_checkout_contact(attempt.phone, kind="phone"),
            "email": _mask_checkout_contact(attempt.email, kind="email"),
            "city": _mask_checkout_contact(attempt.city, kind="location"),
            "office": _mask_checkout_contact(attempt.np_office, kind="location"),
        }
    if include_history:
        payload["history"] = {
            "revisions": list(
                IgCheckoutRevision.objects.filter(proposal=proposal)
                .order_by("revision", "id")
                .values("revision", "source", "created_at")
            ),
            "lifecycle": [
                _checkout_lifecycle_history_entry(event)
                for event in IgLifecycleEvent.objects.filter(proposal=proposal)
                .only(
                    "kind",
                    "state",
                    "attempts",
                    "provider_message_id",
                    "last_error",
                    "created_at",
                )
                .order_by("created_at", "id")
            ],
        }
    return payload


@login_required(login_url="management_login")
@require_GET
def bot_checkout_proposals_api(request):
    """Staff observability for proposal links awaiting payment."""
    blocked = _require_bot_capabilities(
        request, VIEW_IG_CONVERSATION_PII_PERMISSION
    )
    if blocked:
        return blocked
    state = (request.GET.get("state") or "awaiting_payment").strip().lower()
    now = timezone.now()
    base_queryset = IgCheckoutProposal.objects.select_related("client", "payment_attempt").prefetch_related("items")
    awaiting_filter = Q(
        status__in=[
            IgCheckoutProposal.Status.DETAILS_LOCKED,
            IgCheckoutProposal.Status.INVOICE_CREATED,
        ]
    ) | Q(
        status__in=[IgCheckoutProposal.Status.READY, IgCheckoutProposal.Status.VIEWED],
        expires_at__gt=now,
    )
    expired_filter = Q(status=IgCheckoutProposal.Status.EXPIRED) | Q(
        status__in=[IgCheckoutProposal.Status.READY, IgCheckoutProposal.Status.VIEWED],
        expires_at__lte=now,
    )
    if state == "awaiting_payment":
        queryset = base_queryset.filter(awaiting_filter)
    elif state == IgCheckoutProposal.Status.EXPIRED:
        queryset = base_queryset.filter(expired_filter)
    elif state in {IgCheckoutProposal.Status.READY, IgCheckoutProposal.Status.VIEWED}:
        queryset = base_queryset.filter(status=state, expires_at__gt=now)
    elif state in {choice for choice, _label in IgCheckoutProposal.Status.choices}:
        queryset = base_queryset.filter(status=state)
    else:
        return JsonResponse({"error": "invalid_state"}, status=400)
    try:
        limit = max(1, min(int(request.GET.get("limit") or 50), 100))
    except (TypeError, ValueError):
        limit = 50
    items = [_checkout_proposal_workspace_payload(row) for row in queryset.order_by("expires_at", "id")[:limit]]
    counts = {
        "awaiting_payment": base_queryset.filter(awaiting_filter).count(),
        "ready": base_queryset.filter(status=IgCheckoutProposal.Status.READY, expires_at__gt=now).count(),
        "paid": base_queryset.filter(status=IgCheckoutProposal.Status.PAID).count(),
        "expired": base_queryset.filter(expired_filter).count(),
    }
    return JsonResponse({
        "success": True,
        "state": state,
        "count": queryset.count(),
        "counts": counts,
        "items": items,
    })


@login_required(login_url="management_login")
@require_GET
def bot_checkout_proposal_preview_api(request, proposal_id):
    blocked = _require_bot_capabilities(
        request, VIEW_IG_CONVERSATION_PII_PERMISSION
    )
    if blocked:
        return blocked
    proposal = get_object_or_404(
        IgCheckoutProposal.objects.select_related("client", "payment_attempt").prefetch_related("items"),
        public_id=proposal_id,
    )
    return JsonResponse({"success": True, "proposal": _checkout_proposal_workspace_payload(proposal, include_history=True)})


@login_required(login_url="management_login")
@require_POST
def bot_checkout_proposal_action_api(request, proposal_id):
    blocked = _require_bot_capabilities(
        request,
        MANAGE_IG_PAYMENTS_PERMISSION,
        VIEW_IG_CONVERSATION_PII_PERMISSION,
    )
    if blocked:
        return blocked
    proposal = get_object_or_404(
        IgCheckoutProposal.objects.select_related("client", "payment_attempt", "deal").prefetch_related("items"),
        public_id=proposal_id,
    )
    action = str(request.POST.get("action") or "").strip().lower()
    now = timezone.now()
    capabilities = _checkout_proposal_action_capabilities(proposal, now=now)
    if action == "copy_token":
        if not capabilities["can_copy_token"]:
            return JsonResponse({"error": "unavailable"}, status=409)
        from django.conf import settings
        raw_token, token = IgCheckoutAccessToken.issue(proposal=proposal, kind=IgCheckoutAccessToken.Kind.SHARE)
        base = (getattr(settings, "SITE_BASE_URL", "") or request.build_absolute_uri("/")).rstrip("/")
        url = f"{base}/offer/a/{raw_token}/"
        return JsonResponse({"success": True, "url": url, "expires_at": token.expires_at.isoformat()})
    if action == "resend":
        if not capabilities["can_resend"]:
            return JsonResponse({
                "error": capabilities["resend_blocked_reason"] or "unavailable",
            }, status=409)
        window_deadline = _checkout_resend_window_deadline(proposal)
        raw_token, token = IgCheckoutAccessToken.issue(proposal=proposal, kind=IgCheckoutAccessToken.Kind.BOT)
        from django.conf import settings
        base = (getattr(settings, "SITE_BASE_URL", "") or request.build_absolute_uri("/")).rstrip("/")
        url = f"{base}/offer/a/{raw_token}/"
        task, created = IgFollowUpTask.objects.get_or_create(
            client=proposal.client,
            deal=proposal.deal,
            kind=IgFollowUpTask.Kind.PAYMENT,
            reason=f"ig_checkout_resend:{proposal.public_id}",
            status=IgFollowUpTask.Status.PENDING,
            defaults={
                "due_at": now,
                "message_text": url,
                "meta_window_deadline": window_deadline,
            },
        )
        if not created:
            changed = []
            if task.message_text != url:
                task.message_text = url
                changed.append("message_text")
            if task.due_at != now:
                task.due_at = now
                changed.append("due_at")
            if task.meta_window_deadline != window_deadline:
                task.meta_window_deadline = window_deadline
                changed.append("meta_window_deadline")
            if changed:
                task.save(update_fields=[*changed, "updated_at"])
        return JsonResponse({"success": True, "queued": True, "url": url, "task_id": task.pk, "expires_at": token.expires_at.isoformat()})
    if action == "revoke":
        if proposal.status == proposal.Status.REVOKED:
            return JsonResponse({"success": True, "revoked": True, "idempotent": True})
        if proposal.status in {proposal.Status.PAID, proposal.Status.SUPERSEDED}:
            return JsonResponse({"error": "paid_or_superseded"}, status=409)
        if not capabilities["can_revoke"]:
            error = capabilities["revoke_blocked_reason"] or "unavailable"
            return JsonResponse({"error": error}, status=409)
        with transaction.atomic():
            # Keep the same deal -> proposal lock order used by proposal
            # creation/replacement. This prevents a concurrent payment or
            # replacement from leaving the deal pointer behind a revoke.
            locked_deal = IgDeal.objects.select_for_update().get(pk=proposal.deal_id)
            locked = IgCheckoutProposal.objects.select_for_update().get(pk=proposal.pk)
            locked.deal = locked_deal
            locked_capabilities = _checkout_proposal_action_capabilities(locked, now=now)
            if locked.status == locked.Status.REVOKED:
                return JsonResponse({"success": True, "revoked": True, "idempotent": True})
            if locked.status in {locked.Status.PAID, locked.Status.SUPERSEDED}:
                return JsonResponse({"error": "paid_or_superseded"}, status=409)
            if not locked_capabilities["can_revoke"]:
                error = locked_capabilities["revoke_blocked_reason"] or "unavailable"
                return JsonResponse({"error": error}, status=409)
            previous_status = locked.status
            was_revoked = previous_status == locked.Status.REVOKED
            locked.status = locked.Status.REVOKED
            locked.save(update_fields=["status", "updated_at"])
            locked.access_tokens.filter(revoked_at__isnull=True).update(revoked_at=now)
            if locked_deal.active_checkout_proposal_id == locked.pk:
                locked_deal.active_checkout_proposal_id = None
                locked_deal.save(update_fields=["active_checkout_proposal", "updated_at"])

            from management.services.ig_inventory import release_proposal_inventory

            release_proposal_inventory(locked, reason="proposal_revoked")

            # A revoked proposal must not leave a queued payment reminder
            # capable of sending a dead bearer link. Keep unrelated
            # qualification/final follow-ups intact for the same client.
            IgFollowUpTask.objects.filter(
                deal_id=locked_deal.pk,
                kind=IgFollowUpTask.Kind.PAYMENT,
                status=IgFollowUpTask.Status.PENDING,
            ).update(
                status=IgFollowUpTask.Status.CANCELLED,
                skip_reason="proposal_revoked",
                updated_at=now,
            )
            next_due = (
                IgFollowUpTask.objects.filter(
                    client_id=locked_deal.client_id,
                    status=IgFollowUpTask.Status.PENDING,
                )
                .order_by("due_at", "id")
                .values_list("due_at", flat=True)
                .first()
            )
            IgClient.objects.filter(pk=locked_deal.client_id).update(
                next_followup_at=next_due,
                updated_at=now,
            )
            if not was_revoked and not AdminAuditLog.objects.filter(
                action="ig_checkout.revoke",
                entity_type="IgCheckoutProposal",
                entity_id=str(locked.public_id),
            ).exists():
                AdminAuditLog.objects.create(
                    actor=request.user,
                    actor_role="staff",
                    action="ig_checkout.revoke",
                    entity_type="IgCheckoutProposal",
                    entity_id=str(locked.public_id),
                    before={"state": previous_status},
                    after={"state": locked.Status.REVOKED},
                    reason="management_workspace_revoke",
                )
        return JsonResponse({"success": True, "state": IgCheckoutProposal.Status.REVOKED})
    return JsonResponse({"error": "invalid_action"}, status=400)


@login_required(login_url="management_login")
@require_GET
def bot_order_candidates_api(request):
    """Compact, searchable staff selector for linking an existing order."""
    blocked = _require_bot_capabilities(
        request, VIEW_IG_CONVERSATION_PII_PERMISSION
    )
    if blocked:
        return blocked
    from orders.models import Order

    try:
        client_id = int(request.GET.get("client_id") or 0)
    except (TypeError, ValueError):
        client_id = 0
    if not client_id:
        return JsonResponse(
            {"success": False, "error": "Потрібен Instagram-клієнт."},
            status=400,
        )
    client = IgClient.objects.filter(pk=client_id).first()
    if not client or client.hidden_at:
        return JsonResponse(
            {"success": False, "error": "Instagram-клієнта не знайдено."},
            status=404,
        )
    try:
        review_id = int(request.GET.get("review_id") or 0)
    except (TypeError, ValueError):
        review_id = 0
    review = None
    if review_id:
        from .ig_bot_models import IgPaymentConfirmationReview
        from management.services.ig_order_links import authoritative_manager_decision

        review = IgPaymentConfirmationReview.objects.select_related("client").filter(
            pk=review_id,
            client=client,
        ).first()
        if not review:
            return JsonResponse(
                {"success": False, "error": "Перевірку оплати для цього клієнта не знайдено."},
                status=404,
            )
        if not authoritative_manager_decision(review) and not (
            review.status == review.Status.SUPERSEDED and review.order_id
        ):
            return JsonResponse(
                {"success": False, "error": "Спочатку потрібне підтверджене рішення менеджера."},
                status=409,
            )
    q = str(request.GET.get("q") or "").strip()[:120]
    try:
        limit = max(1, min(int(request.GET.get("limit") or 20), 40))
    except (TypeError, ValueError):
        limit = 20

    queryset = Order.objects.select_related(
        "instagram_attribution",
        "instagram_attribution__client",
        "instagram_assignment",
        "instagram_assignment__client",
        "instagram_assignment__assigned_by",
    ).prefetch_related("items").order_by("-created", "-id")
    if q:
        queryset = queryset.filter(
            Q(order_number__icontains=q)
            | Q(tracking_number__icontains=q)
            | Q(full_name__icontains=q)
            | Q(phone__icontains=q)
            | Q(items__title__icontains=q)
            | Q(items__product__title__icontains=q)
        ).distinct()
    rows = list(queryset[:limit])
    items = []
    from management.services.ig_order_links import order_link_override_requirements
    from management.services.ig_order_amounts import order_amounts

    for order in rows:
        amounts = order_amounts(order)
        try:
            attribution = order.instagram_attribution
        except Exception:
            attribution = None
        assignment = getattr(order, "instagram_assignment", None)
        assignment_active = bool(
            assignment
            and assignment.client_id
            and assignment.unassigned_at is None
        )
        blocked_reason = ""
        if order.status == "cancelled":
            blocked_reason = "cancelled"
        elif assignment_active and assignment.client_id != client.pk:
            blocked_reason = "owned_by_other_client"
        elif attribution and attribution.client_id != client.pk:
            blocked_reason = "owned_by_other_client"
        if not blocked_reason:
            from .ig_bot_models import (
                IgCommercialEpisode,
                IgDeal,
                IgPaymentConfirmationReview,
            )

            deal_owners = set(
                IgDeal.objects.filter(order=order).values_list("client_id", flat=True)
            )
            review_owners = set(
                IgPaymentConfirmationReview.objects.filter(order=order).values_list(
                    "client_id", flat=True
                )
            )
            episode_owners = set(
                IgCommercialEpisode.objects.filter(intended_order=order).values_list(
                    "client_id", flat=True
                )
            )
            all_owners = deal_owners | review_owners | episode_owners
            if any(owner_id != client.pk for owner_id in all_owners):
                blocked_reason = "owned_by_other_client"
            elif review and review.status == IgPaymentConfirmationReview.Status.SUPERSEDED and review.order_id == order.pk:
                blocked_reason = "already_linked_payment"
            elif review and IgCommercialEpisode.objects.filter(
                intended_order=order
            ).exclude(primary_payment_review=review).exists():
                blocked_reason = "owned_by_other_episode"
        override_conflicts = []
        allowed_override_codes = []
        if review and not blocked_reason:
            try:
                requirements = order_link_override_requirements(review, order)
            except ValueError:
                blocked_reason = "review_not_confirmed"
            else:
                override_conflicts = requirements["conflicts"]
                allowed_override_codes = requirements["allowed_codes"]
        elif order.status in {"ship", "done"}:
            override_conflicts = ["terminal_order"]
            allowed_override_codes = ["historical_fulfilled_order", "historical_import"]
        items.append({
            "id": order.pk,
            "number": order.order_number,
            "created_at": order.created.isoformat() if order.created else "",
            "client_name": order.full_name,
            "phone_masked": (
                ("***" + str(order.phone)[-4:]) if order.phone else ""
            ),
            "items": [
                {
                    "title": item.title,
                    "size": item.size or "",
                    "qty": item.qty,
                    "unit_price": f"{item.unit_price:.2f}",
                    "line_total": f"{item.line_total:.2f}",
                }
                for item in list(order.items.all())[:8]
            ],
            "amount": f"{amounts['payable']:.2f}",
            "subtotal": f"{amounts['subtotal']:.2f}",
            "discount_amount": f"{amounts['discount']:.2f}",
            "payment_status": order.payment_status,
            "payment_status_label": order.get_payment_status_display(),
            "status": order.status,
            "status_label": order.get_status_display(),
            "tracking_number": order.tracking_number or "",
            "shipment_status": order.shipment_status or "",
            "source": order.source,
            "sale_source": order.sale_source,
            "selectable": not blocked_reason,
            "blocked_reason": blocked_reason,
            "blocked_reason_label": {
                "already_linked_payment": "Ця оплата вже прив'язана до цього замовлення",
                "owned_by_other_episode": "Замовлення належить іншому циклу клієнта",
                "owned_by_other_client": "Замовлення вже прив'язане до іншого клієнта",
                "cancelled": "Замовлення скасоване",
            }.get(blocked_reason, ""),
            "requires_override": bool(override_conflicts),
            "override_conflicts": override_conflicts,
            "allowed_override_codes": allowed_override_codes,
            "linked_client_id": attribution.client_id if attribution else None,
            "current_assignment_id": assignment.pk if assignment_active else None,
            "assignment_version": assignment.version if assignment_active else None,
            "assignment_source": assignment.source if assignment_active else "",
            "assignment": (
                _assignment_workspace_payload(assignment, client_id=client.pk)
                if assignment
                else None
            ),
            "admin_url": _existing_order_admin_url(order.pk),
        })
    return JsonResponse({
        "success": True,
        "client_id": client.pk,
        "review_id": review.pk if review else None,
        "query": q,
        "items": items,
        "limit": limit,
    })


@login_required(login_url="management_login")
@require_POST
def bot_client_order_link_api(request, client_id):
    blocked = _require_bot_capabilities(
        request,
        MANAGE_IG_PAYMENTS_PERMISSION,
        VIEW_IG_CONVERSATION_PII_PERMISSION,
    )
    if blocked:
        return blocked
    from .ig_bot_models import IgClient, IgOrderAssignment, IgOrderAssignmentEvent
    from management.services.ig_order_assignments import (
        AssignmentConflict,
        AssignmentVersionConflict,
        link_order_to_client,
    )
    from orders.models import Order

    client = IgClient.objects.filter(pk=client_id).first()
    if not client or client.hidden_at:
        return JsonResponse(
            {"success": False, "error_code": "client_not_found", "error": "Instagram-клієнта не знайдено."},
            status=404,
        )
    order_identifier = str(request.POST.get("order_identifier") or "").strip()
    order = Order.objects.filter(order_number=order_identifier).first()
    if not order:
        return JsonResponse(
            {"success": False, "error_code": "order_not_found", "error": "Замовлення з таким точним номером не знайдено."},
            status=404,
        )
    if order.status == "cancelled":
        return JsonResponse(
            {"success": False, "error_code": "order_not_linkable", "error": "Скасоване замовлення не можна прив'язати."},
            status=409,
        )
    expected_version = request.POST.get("expected_version")
    try:
        expected_version = int(expected_version) if expected_version not in (None, "") else None
    except (TypeError, ValueError):
        return JsonResponse(
            {"success": False, "error_code": "invalid_version", "error": "Некоректна версія прив'язки."},
            status=400,
        )
    try:
        assignment = link_order_to_client(
            order,
            client=client,
            actor=request.user,
            source=IgOrderAssignment.Source.MANAGER_MANUAL,
            actor_source=IgOrderAssignmentEvent.ActorSource.MANAGEMENT_USER,
            operation_id=request.POST.get("operation_id") or None,
            expected_version=expected_version,
            reason_code=str(request.POST.get("reason_code") or "").strip()[:64],
            reason=str(request.POST.get("reason") or request.POST.get("reason_text") or "").strip()[:500],
        )
    except AssignmentConflict as exc:
        return JsonResponse(
            {"success": False, "error_code": "assignment_conflict", "error": str(exc)},
            status=409,
        )
    except AssignmentVersionConflict as exc:
        return JsonResponse(
            {"success": False, "error_code": "assignment_version_conflict", "error": str(exc)},
            status=409,
        )
    except ValueError as exc:
        return JsonResponse(
            {"success": False, "error_code": "invalid_assignment", "error": str(exc)},
            status=400,
        )
    assignment = IgOrderAssignment.objects.select_related("order", "assigned_by").get(pk=assignment.pk)
    return JsonResponse({
        "success": True,
        "assignment": _assignment_workspace_payload(assignment, client_id=client.pk),
    })


@login_required(login_url="management_login")
@require_POST
def bot_client_order_unlink_api(request, client_id, assignment_id):
    blocked = _require_bot_capabilities(
        request,
        MANAGE_IG_PAYMENTS_PERMISSION,
        VIEW_IG_CONVERSATION_PII_PERMISSION,
    )
    if blocked:
        return blocked
    from .ig_bot_models import IgClient, IgOrderAssignment, IgOrderAssignmentEvent
    from management.services.ig_order_assignments import (
        AssignmentConflict,
        AssignmentNotFound,
        AssignmentVersionConflict,
        unlink_order_from_client,
    )

    client = IgClient.objects.filter(pk=client_id).first()
    assignment = (
        IgOrderAssignment.objects.select_related("order", "client", "assigned_by")
        .filter(pk=assignment_id)
        .first()
    )
    if not client or client.hidden_at:
        return JsonResponse(
            {"success": False, "error_code": "client_not_found", "error": "Instagram-клієнта не знайдено."},
            status=404,
        )
    if not assignment or assignment.client_id != client.pk or assignment.unassigned_at is not None:
        return JsonResponse(
            {"success": False, "error_code": "assignment_not_active", "error": "Активну прив'язку не знайдено."},
            status=404,
        )
    try:
        expected_version = int(request.POST.get("expected_version"))
    except (TypeError, ValueError):
        return JsonResponse(
            {"success": False, "error_code": "invalid_version", "error": "Некоректна версія прив'язки."},
            status=400,
        )
    try:
        updated = unlink_order_from_client(
            assignment.order_id,
            client=client,
            actor=request.user,
            actor_source=IgOrderAssignmentEvent.ActorSource.MANAGEMENT_USER,
            operation_id=request.POST.get("operation_id") or None,
            expected_version=expected_version,
            reason_code=str(request.POST.get("reason_code") or "").strip()[:64],
            reason=str(request.POST.get("reason") or request.POST.get("reason_text") or "").strip()[:500],
        )
    except AssignmentVersionConflict as exc:
        return JsonResponse(
            {"success": False, "error_code": "assignment_version_conflict", "error": str(exc)},
            status=409,
        )
    except AssignmentConflict as exc:
        return JsonResponse(
            {"success": False, "error_code": "assignment_conflict", "error": str(exc)},
            status=409,
        )
    except AssignmentNotFound as exc:
        return JsonResponse(
            {"success": False, "error_code": "assignment_not_active", "error": str(exc)},
            status=404,
        )
    except ValueError as exc:
        return JsonResponse(
            {"success": False, "error_code": "invalid_assignment", "error": str(exc)},
            status=400,
        )
    updated = IgOrderAssignment.objects.select_related("order", "assigned_by").get(pk=updated.pk)
    return JsonResponse({
        "success": True,
        "assignment": _assignment_workspace_payload(updated, client_id=client.pk),
    })


@login_required(login_url="management_login")
@require_POST
def bot_payment_review_action_api(request, review_id):
    blocked = _require_bot_capabilities(
        request,
        MANAGE_IG_PAYMENTS_PERMISSION,
        VIEW_IG_CONVERSATION_PII_PERMISSION,
    )
    if blocked:
        return blocked
    from .ig_bot_models import IgPaymentConfirmationReview
    from management.services.ig_payment_review import (
        payment_review_order_url,
        reconcile_duplicate_payment_review,
        record_review_decision,
        resolve_historical_paid_review,
    )

    action = (request.POST.get("action") or "").strip().lower()
    verification_scope = (request.POST.get("verification_scope") or "").strip()
    review = IgPaymentConfirmationReview.objects.select_related(
        "client", "deal", "deal__payment_projection", "order"
    ).filter(pk=review_id).first()
    if not review:
        return JsonResponse({"success": False, "error": "Перевірку оплати не знайдено."}, status=404)
    if review.client.hidden_at:
        return JsonResponse({"success": False, "error": "Прихований клієнт виключений з операцій."}, status=409)
    canonical_review = reconcile_duplicate_payment_review(review)
    if (
        review.status == IgPaymentConfirmationReview.Status.SUPERSEDED
        and review.order_id
        and action in {"link_order", "confirm", "manager_verify", "clarify_amount"}
    ):
        order = review.order
        return JsonResponse({
            "success": True,
            "id": review.id,
            "status": review.status,
            "status_label": review.get_status_display(),
            "canonical_review_id": canonical_review.pk if canonical_review else review.superseded_by_id,
            "idempotent_replay": True,
            "next_action": "order_linked",
            "order_id": order.pk,
            "order_number": order.order_number,
            "order_url": _existing_order_admin_url(order.pk),
            "order_resolution": {
                "required": False,
                "link_existing": {"action": "", "action_url": ""},
                "create_new": {"url": "", "editable": False},
            },
        })
    if action == "link_order":
        from management.services.ig_order_links import link_existing_order_to_review

        try:
            order = link_existing_order_to_review(
                review,
                order_identifier=request.POST.get("order_identifier"),
                actor=request.user,
                override_code=request.POST.get("override_code", ""),
                override_reason=request.POST.get("override_reason", ""),
            )
        except ValueError as exc:
            return JsonResponse({"success": False, "error": str(exc)}, status=409)
        return JsonResponse({
            "success": True,
            "id": review.id,
            "status": review.status,
            "next_action": "order_linked",
            "order_id": order.pk,
            "order_number": order.order_number,
            "order_url": _existing_order_admin_url(order.pk),
        })
    if action == "historical_paid_fulfilled":
        amount_unrecoverable = str(
            request.POST.get("amount_unrecoverable") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        try:
            review = resolve_historical_paid_review(
                review,
                actor=request.user,
                outcome=request.POST.get("outcome"),
                reason=request.POST.get("reason") or request.POST.get("reason_text") or "",
                confirmed_amount=request.POST.get("confirmed_amount"),
                amount_unrecoverable=amount_unrecoverable,
            )
        except ValueError as exc:
            error = str(exc)
            field_errors = {}
            if "Причина" in error:
                field_errors["reason"] = error
            elif "Результат" in error:
                field_errors["outcome"] = error
            elif "сума" in error.casefold() or "суму" in error.casefold():
                field_errors["confirmed_amount"] = error
            return JsonResponse(
                {
                    "success": False,
                    "error_code": "historical_resolution_invalid",
                    "error": error,
                    "field_errors": field_errors,
                },
                status=409 if "вже" in error else 400,
            )
        decision = _latest_payment_review_decision(review)
        return JsonResponse({
            "success": True,
            "id": review.id,
            "status": review.status,
            "status_label": review.get_status_display(),
            "resolution_kind": review.resolution_kind,
            "resolution_outcome": review.resolution_outcome or "",
            "resolution_note": review.resolution_note,
            "decision": _payment_review_decision_payload(decision),
            "payment": _payment_review_truth_payload(review, decision),
            "idempotent_replay": not bool(getattr(review, "_transitioned", False)),
            "next_action": "historical_completed",
            "order_url": "",
            "order_resolution": {
                "required": False,
                "link_existing": {
                    "action": "",
                    "action_url": "",
                    "requires_exact_order_identifier": False,
                },
                "create_new": {"url": "", "editable": False},
            },
        })
    clarify_amount = action == "clarify_amount"
    if action in {"confirm", "manager_verify", "clarify_amount"}:
        decision_name = "manager_verified"
        reason_code = ""
        reason_text = ""
    elif action in {"cancel", "manager_reject"}:
        decision_name = "manager_rejected"
        reason_code = (request.POST.get("reason_code") or "").strip()
        reason_text = (
            request.POST.get("reason_text") or request.POST.get("reason") or ""
        ).strip()
        if not reason_code:
            return JsonResponse({
                "success": False,
                "error": "Причина відхилення обов'язкова.",
            }, status=400)
    else:
        return JsonResponse({"success": False, "error": "Невідома дія."}, status=400)
    try:
        review = record_review_decision(
            review,
            actor=request.user,
            decision=decision_name,
            verification_scope=verification_scope,
            order_total_amount=request.POST.get("order_total_amount"),
            confirmed_amount=request.POST.get("confirmed_amount"),
            reason_code=reason_code,
            reason_text=reason_text,
            allow_amount_clarification=clarify_amount,
        )
    except ValueError as exc:
        error = str(exc)
        conflict = bool(
            review.client.hidden_at
            or "журналу рішення" in error
            or "вже містить точну суму" in error
            or "замовлення вже прив’язано" in error
        )
        field_errors = {}
        if "Повна вартість" in error or "узгоджена сума" in error:
            field_errors["order_total_amount"] = error
        elif (
            "Підтверджена сума" in error
            or "Сума підтвердженого платежу" in error
            or "Передоплата" in error
        ):
            field_errors["confirmed_amount"] = error
        return JsonResponse(
            {"success": False, "error": error, "field_errors": field_errors},
            status=409 if conflict else 400,
        )
    decision = _latest_payment_review_decision(review)
    decision_payload = _payment_review_decision_payload(decision)
    payment_payload = _payment_review_truth_payload(review, decision)
    if decision and decision.decision != decision_name:
        return JsonResponse({
            "success": False,
            "error": "Перевірку вже завершено іншим рішенням.",
            "status": review.status,
            "current_decision": decision.decision,
            "decision": decision_payload,
            "payment": payment_payload,
        }, status=409)
    notification = IgBotNotification.objects.filter(dedupe_key=review.dedupe_key).first()
    if notification:
        if notification.status == IgBotNotification.Status.SENT:
            notification.status = IgBotNotification.Status.RESOLVED
            notification.failure_kind = "payment_review_" + review.status
        notification.payload = {
            **(notification.payload if isinstance(notification.payload, dict) else {}),
            "review_status": review.status,
            "actor_id": request.user.pk,
            "manager_payment_truth": payment_payload["manager_truth"],
            "verification_source": payment_payload["verification_source"],
            "decision_reason_code": decision_payload.get("reason_code", ""),
            "decision_reason_text": decision_payload.get("reason_text", ""),
        }
        notification.save(update_fields=["status", "failure_kind", "payload", "updated_at"])
    bot.log(
        "warning" if review.status == IgPaymentConfirmationReview.Status.CANCELLED else "info",
        "payment_review_operator_action",
        f"review={review.id}; status={review.status}; actor={request.user.pk}",
    )
    needs_order_resolution = bool(
        review.status == IgPaymentConfirmationReview.Status.CONFIRMED
        and not review.order_id
        and payment_payload["authoritative_for_fulfillment"]
    )
    create_order_url = (
        _payment_review_order_link(review, payment_review_order_url)
        if needs_order_resolution
        else ""
    )
    return JsonResponse({
        "success": True,
        "id": review.id,
        "status": review.status,
        "status_label": review.get_status_display(),
        "payment": payment_payload,
        "decision": decision_payload,
        "idempotent_replay": not bool(getattr(review, "_transitioned", False)),
        "next_action": (
            "resolve_order"
            if needs_order_resolution
            else "reconcile_payment"
            if payment_payload["needs_reconciliation"]
            else "review_conversation"
        ),
        "order_url": _existing_order_admin_url(review.order_id) if review.order_id else "",
        "order_resolution": {
            "required": needs_order_resolution,
            "link_existing": {
                "action": "link_order",
                "action_url": (
                    reverse(
                        "management_bot_payment_review_action_api",
                        args=[review.pk],
                    )
                    if needs_order_resolution
                    else ""
                ),
                "requires_exact_order_identifier": True,
            },
            "create_new": {
                "url": create_order_url,
                "editable": True,
            },
        },
    })


@login_required(login_url="management_login")
@require_POST
def bot_settings_save_api(request):
    prompt_fields = {
        "trigger_text",
        "reply_text",
        "system_prompt",
        "knowledge_base",
    }
    operate_fields = {
        "direct_source",
        "gemini_source",
        "custom_direct_token",
        "clear_custom_direct_token",
        "custom_gemini_key",
        "clear_custom_gemini_key",
        "ai_enabled",
        "receive_via_poll",
        "meta_feedback_enabled",
        "meta_feedback_test_event_code",
        "gemini_model",
        "gemini_routing_mode",
        "pinned_chat_model",
        "pinned_minutes",
        "allowed_senders",
        "poll_interval_seconds",
    }
    submitted = set(request.POST)
    requested_prompt_fields = submitted & prompt_fields
    requested_operate_fields = submitted & operate_fields
    if not requested_prompt_fields and not requested_operate_fields:
        return JsonResponse(
            {"success": False, "error": "Немає налаштувань для збереження."},
            status=400,
        )
    if requested_prompt_fields:
        blocked = _require_bot_capabilities(request, EDIT_IG_PROMPT_PERMISSION)
        if blocked:
            return blocked
    if requested_operate_fields:
        blocked = _require_bot_capabilities(request, OPERATE_IG_BOT_PERMISSION)
        if blocked:
            return blocked
    s = InstagramBotSettings.load()
    if {"system_prompt", "knowledge_base"} & submitted:
        from management.services.ig_policy_compiler import PolicyReadinessError

        merged_system_prompt = (
            (request.POST.get("system_prompt") or "").strip()
            if "system_prompt" in request.POST
            else s.system_prompt
        )
        merged_live_directives = (
            (request.POST.get("knowledge_base") or "").strip()
            if "knowledge_base" in request.POST
            else s.knowledge_base
        )
        try:
            bot.validate_core_policy_for_publication(
                merged_system_prompt,
                merged_live_directives,
            )
        except PolicyReadinessError as exc:
            return JsonResponse(
                {
                    "success": False,
                    "code": exc.code,
                    "error": "Обов'язкові правила не готові до публікації.",
                },
                status=400,
            )
    posted_update_fields: set[str] = set()
    requested_allowed_senders = (
        (request.POST.get("allowed_senders") or "").strip()
        if "allowed_senders" in request.POST
        else s.allowed_senders
    )
    routing_before = {
        "mode": s.gemini_routing_mode,
        "pinned_model": s.pinned_chat_model,
        "pinned_until": s.pinned_until.isoformat() if s.pinned_until else "",
    }
    routing_requested = "gemini_routing_mode" in request.POST
    direct_source = (request.POST.get("direct_source") or "").strip()
    if "direct_source" in request.POST and direct_source in InstagramBotSettings.CredSource.values:
        s.direct_source = direct_source
        posted_update_fields.add("direct_source")
    gemini_source = (request.POST.get("gemini_source") or "").strip()
    if "gemini_source" in request.POST and gemini_source in InstagramBotSettings.CredSource.values:
        s.gemini_source = gemini_source
        posted_update_fields.add("gemini_source")

    try:
        if "custom_direct_token" in request.POST:
            value = (request.POST.get("custom_direct_token") or "").strip()
            if value:
                s.custom_direct_token = value
                posted_update_fields.add("custom_direct_token_encrypted")
        if _truthy(request.POST.get("clear_custom_direct_token")):
            s.custom_direct_token = ""
            posted_update_fields.add("custom_direct_token_encrypted")
        if "custom_gemini_key" in request.POST:
            value = (request.POST.get("custom_gemini_key") or "").strip()
            if value:
                s.custom_gemini_key = value
                posted_update_fields.add("custom_gemini_key_encrypted")
        if _truthy(request.POST.get("clear_custom_gemini_key")):
            s.custom_gemini_key = ""
            posted_update_fields.add("custom_gemini_key_encrypted")
    except BotSecretEncryptionUnavailable:
        return JsonResponse(
            {
                "success": False,
                "error": "Не налаштовано захист для збереження ключа. Зверніться до адміністратора.",
            },
            status=503,
        )

    trigger = (request.POST.get("trigger_text") or "").strip()
    if "trigger_text" in request.POST and trigger:
        s.trigger_text = trigger[:255]
        posted_update_fields.add("trigger_text")
    reply = (request.POST.get("reply_text") or "").strip()
    if "reply_text" in request.POST and reply:
        s.reply_text = reply[:1000]
        posted_update_fields.add("reply_text")

    # AI-режим / модель / правило / білий список.
    if "ai_enabled" in request.POST:
        s.ai_enabled = (request.POST.get("ai_enabled") or "").strip() in {"1", "true", "on", "yes"}
        posted_update_fields.add("ai_enabled")
    if "receive_via_poll" in request.POST:
        s.receive_via_poll = (request.POST.get("receive_via_poll") or "").strip() in {"1", "true", "on", "yes"}
        posted_update_fields.add("receive_via_poll")
    if "meta_feedback_enabled" in request.POST:
        s.meta_feedback_enabled = _truthy(request.POST.get("meta_feedback_enabled"))
        posted_update_fields.add("meta_feedback_enabled")
    if "meta_feedback_test_event_code" in request.POST:
        s.meta_feedback_test_event_code = (request.POST.get("meta_feedback_test_event_code") or "")[:120]
        posted_update_fields.add("meta_feedback_test_event_code")
    model = (request.POST.get("gemini_model") or "").strip()
    if model:
        from management.services.gemini_keys import is_allowed_chat_model

        if not is_allowed_chat_model(model):
            return JsonResponse({"success": False, "error": "Недозволена модель Gemini."}, status=400)
        s.gemini_model = model[:80]
        posted_update_fields.add("gemini_model")
    if routing_requested:
        requested_mode = str(request.POST.get("gemini_routing_mode") or "").strip()
        if requested_mode not in InstagramBotSettings.GeminiRoutingMode.values:
            return JsonResponse(
                {"success": False, "error": "Недозволений режим маршрутизації."},
                status=400,
            )
        if requested_mode == InstagramBotSettings.GeminiRoutingMode.PINNED:
            pinned_model = str(
                request.POST.get("pinned_chat_model") or model or ""
            ).strip()
            from management.services.gemini_keys import is_allowed_chat_model

            if not is_allowed_chat_model(pinned_model):
                return JsonResponse(
                    {"success": False, "error": "Недозволена закріплена модель Gemini."},
                    status=400,
                )
            try:
                pin_minutes = int(request.POST.get("pinned_minutes") or 0)
            except (TypeError, ValueError):
                pin_minutes = 0
            if pin_minutes < 1 or pin_minutes > 60:
                return JsonResponse(
                    {"success": False, "error": "Закріплення дозволене на 1–60 хвилин."},
                    status=400,
                )
            s.gemini_routing_mode = requested_mode
            s.pinned_chat_model = pinned_model
            s.pinned_until = timezone.now() + timedelta(minutes=pin_minutes)
        else:
            s.gemini_routing_mode = InstagramBotSettings.GeminiRoutingMode.ADAPTIVE
            s.pinned_chat_model = ""
            s.pinned_until = None
        posted_update_fields.update({
            "gemini_routing_mode", "pinned_chat_model", "pinned_until"
        })
    if "system_prompt" in request.POST:
        s.system_prompt = (request.POST.get("system_prompt") or "").strip()
        posted_update_fields.add("system_prompt")
    if "knowledge_base" in request.POST:
        s.knowledge_base = (request.POST.get("knowledge_base") or "").strip()
        posted_update_fields.add("knowledge_base")
    if "allowed_senders" in request.POST:
        s.allowed_senders = (request.POST.get("allowed_senders") or "").strip()
        posted_update_fields.add("allowed_senders")

    if "poll_interval_seconds" in request.POST:
        try:
            interval = int(request.POST.get("poll_interval_seconds") or s.poll_interval_seconds)
            s.poll_interval_seconds = max(2, min(60, interval))
            posted_update_fields.add("poll_interval_seconds")
        except (TypeError, ValueError):
            pass

    class RoutingPolicyConflict(Exception):
        pass

    class SettingsRevisionConflict(Exception):
        pass

    expected_revision = None
    if "settings_revision" in request.POST:
        try:
            expected_revision = int(request.POST.get("settings_revision"))
        except (TypeError, ValueError):
            return JsonResponse(
                {"success": False, "error": "Некоректна ревізія налаштувань."},
                status=400,
            )

    def persist_settings_and_audit():
        with transaction.atomic():
            locked_settings = InstagramBotSettings.objects.select_for_update().get(
                pk=s.pk
            )
            locked_routing = {
                "mode": locked_settings.gemini_routing_mode,
                "pinned_model": locked_settings.pinned_chat_model,
                "pinned_until": (
                    locked_settings.pinned_until.isoformat()
                    if locked_settings.pinned_until
                    else ""
                ),
            }
            if (
                expected_revision is not None
                and int(locked_settings.settings_revision or 0) != expected_revision
            ):
                raise SettingsRevisionConflict
            if routing_requested and locked_routing != routing_before:
                raise RoutingPolicyConflict
            allowlist_changed_locked = bool(
                "allowed_senders" in posted_update_fields
                and locked_settings.allowed_senders != requested_allowed_senders
            )
            for field in posted_update_fields:
                setattr(locked_settings, field, getattr(s, field))
            if allowlist_changed_locked:
                locked_settings.allowed_senders = requested_allowed_senders
                locked_settings.reply_permission_epoch = (
                    int(locked_settings.reply_permission_epoch or 0) + 1
                )
            save_fields = set(posted_update_fields)
            if allowlist_changed_locked:
                save_fields.add("reply_permission_epoch")
            if save_fields:
                locked_settings.settings_revision = (
                    int(locked_settings.settings_revision or 0) + 1
                )
                save_fields.update({"settings_revision", "updated_at"})
                locked_settings.save(update_fields=save_fields)
            locked_after = {
                "mode": locked_settings.gemini_routing_mode,
                "pinned_model": locked_settings.pinned_chat_model,
                "pinned_until": (
                    locked_settings.pinned_until.isoformat()
                    if locked_settings.pinned_until
                    else ""
                ),
            }
            routing_changed_locked = locked_routing != locked_after
            if not routing_changed_locked:
                return locked_settings
            AdminAuditLog.objects.create(
                actor=request.user,
                actor_role="staff",
                action="ig_gemini.routing_policy_changed",
                entity_type="InstagramBotSettings",
                entity_id=str(s.pk),
                before=locked_routing,
                after=locked_after,
                reason=(
                    "temporary_emergency_pin"
                    if locked_settings.gemini_routing_mode
                    == InstagramBotSettings.GeminiRoutingMode.PINNED
                    else "adaptive_routing_restored"
                ),
                ip=request.META.get("REMOTE_ADDR") or None,
                user_agent=str(request.META.get("HTTP_USER_AGENT") or "")[:512],
            )
            return locked_settings

    try:
        if "allowed_senders" in posted_update_fields:
            # Permission generation and routing audit share one row lock and
            # transaction. An audit failure rolls the settings change back.
            from management.services.ig_reply_boundary import pause_reply_boundary

            with pause_reply_boundary():
                s = persist_settings_and_audit()
        else:
            s = persist_settings_and_audit()
    except RoutingPolicyConflict:
        return JsonResponse(
            {"success": False, "error": "Політика маршрутизації вже змінилась."},
            status=409,
        )
    except SettingsRevisionConflict:
        return JsonResponse(
            {"success": False, "error": "Налаштування вже змінено в іншому вікні."},
            status=409,
        )
    # Скинути кеш токена/кулдаун, щоб новий токен підхопився одразу.
    try:
        from django.core.cache import cache
        cache.delete("ig_bot_page_token")
        cache.delete("ig_bot_pt_cooldown")
        cache.delete("ig_bot_ll_user_token")
        cache.delete("ig_bot_pt_errsig")
    except Exception:
        pass
    bot.log(
        "info",
        "settings_saved",
        f"ai={s.ai_enabled}, model={s.gemini_model}, direct={s.direct_source}, gemini={s.gemini_source}",
    )
    return JsonResponse({"success": True, "status": _reviewer_safe_status(request)})


# ---------------------------------------------------------------------------
# Вкладка «Клиенти» — CRM IG-клієнтів (Task 13)
# ---------------------------------------------------------------------------
def _interaction_tone(interaction_type: str) -> str:
    from .ig_bot_models import IgConversationAnalysisSnapshot

    types = IgConversationAnalysisSnapshot.InteractionType
    if interaction_type in {types.EXCHANGE_REQUEST, types.RETURN_REQUEST}:
        # Not "support": a red complaint badge on a size exchange is exactly the
        # misreading the customer complained about.
        return "service"
    if interaction_type == types.SUPPORT_COMPLAINT:
        return "support"
    if interaction_type in {types.WHOLESALE_B2B, types.COLLABORATION}:
        return "business"
    if interaction_type in {types.HIGH_INTENT, types.PAYMENT_PENDING}:
        return "intent"
    if interaction_type == types.PAID_ORDER_WAITING:
        return "success"
    if interaction_type in {types.EXPLICIT_NO_BUY, types.OPT_OUT, types.SPAM_ABUSE}:
        return "negative"
    return "neutral"


def _group_signal_rows(rows) -> list[dict]:
    """Collapse repeated event rows into an auditable per-type summary."""
    from .ig_bot_models import IgConversationSignal

    labels = dict(IgConversationSignal.Type.choices)
    grouped = {}
    for raw in rows or ():
        signal_type = str(raw.get("type") or "unknown")
        time_value = str(raw.get("time") or "")
        current = grouped.get(signal_type)
        if current is None:
            current = {
                "type": signal_type,
                "type_label": str(labels.get(signal_type, "Інший сигнал")),
                "count": 0,
                "latest_time": "",
                "latest_value": "",
                "latest_confidence": "",
            }
            grouped[signal_type] = current
        current["count"] += 1
        if time_value >= current["latest_time"]:
            current["latest_time"] = time_value
            current["latest_value"] = str(raw.get("value") or "")
            current["latest_confidence"] = str(raw.get("confidence") or "")
    return sorted(
        grouped.values(),
        key=lambda item: (-int(item["count"]), item["type"]),
    )


def _with_latest_interaction(queryset):
    from .ig_bot_models import (
        IgConversationAnalysisSnapshot,
        IgPaymentConfirmationReview,
        IgPostSaleCase,
    )
    from .models import InstagramBotMessage

    latest = IgConversationAnalysisSnapshot.objects.filter(
        client_id=OuterRef("pk")
    ).order_by("-id")
    latest_customer = latest.exclude(
        interaction_type=IgConversationAnalysisSnapshot.InteractionType.MANAGER_OBSERVATION
    )
    latest_message = InstagramBotMessage.objects.filter(
        client_id=OuterRef("pk")
    ).order_by("-id")
    action_review = IgPaymentConfirmationReview.objects.filter(
        client_id=OuterRef("pk")
    ).filter(
        Q(status=IgPaymentConfirmationReview.Status.PENDING)
        | Q(
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
            order_id__isnull=True,
        )
    ).exclude(
        status=IgPaymentConfirmationReview.Status.SUPERSEDED
    ).exclude(
        resolution_kind=(
            IgPaymentConfirmationReview.ResolutionKind.HISTORICAL_PAID_ARCHIVED
        )
    )
    action_post_sale = IgPostSaleCase.objects.filter(
        client_id=OuterRef("pk"),
        status__in=[IgPostSaleCase.Status.NEEDS_DETAILS, IgPostSaleCase.Status.OPEN],
    )
    latest_post_sale = IgPostSaleCase.objects.filter(
        client_id=OuterRef("pk")
    ).order_by("-updated_at", "-id")
    return queryset.annotate(
        latest_interaction_type=Coalesce(
            Subquery(latest_customer.values("interaction_type")[:1]),
            Subquery(latest.values("interaction_type")[:1]),
            Value(""),
        ),
        latest_conversation_message_id=Coalesce(
            Subquery(latest_message.values("id")[:1]),
            Value(0),
        ),
        has_post_sale_action=Exists(action_post_sale),
        has_manager_action=Exists(action_review) | Exists(action_post_sale),
        latest_post_sale_type=Coalesce(
            Subquery(latest_post_sale.values("case_type")[:1]),
            Value(""),
        ),
        latest_post_sale_status=Coalesce(
            Subquery(latest_post_sale.values("status")[:1]),
            Value(""),
        ),
        latest_post_sale_order_id=Subquery(
            latest_post_sale.values("order_id")[:1]
        ),
    )


def _client_potential_payload(c, latest_analysis, *, latest_message_id=None) -> dict:
    """Evidence-bound purchase potential, independent from payment/order truth."""
    from .ig_bot_models import IgClient, IgConversationAnalysisSnapshot

    current_episode = getattr(c, "current_commercial_episode", None)
    from management.services.ig_funnel_reset import current_message_floor

    current_floor = current_message_floor(c)
    current_episode_id = getattr(c, "current_commercial_episode_id", None)
    latest_message_id = int(
        latest_message_id
        if latest_message_id is not None
        else getattr(c, "latest_conversation_message_id", 0)
        or 0
    )
    label_by_band = {
        "cold": "Холодний",
        "exploring": "Вивчає варіанти",
        "qualified": "Кваліфікований інтерес",
        "high_intent": "Високий намір",
        "checkout": "Готовий оформлювати",
        "lost": "Втрачений",
        "opted_out": "Відмовився від повідомлень",
        "spam": "Спам",
        "unknown": "Ще не оцінено",
    }
    active_opt_out = bool(
        c.opted_out_at
        and (not c.opted_in_at or c.opted_in_at < c.opted_out_at)
    )
    policy_band = ""
    policy_source = ""
    if c.stage == IgClient.Stage.SPAM or (
        latest_analysis
        and latest_analysis.interaction_type
        == IgConversationAnalysisSnapshot.InteractionType.SPAM_ABUSE
    ):
        policy_band, policy_source = "spam", "communication_policy"
    elif active_opt_out:
        policy_band, policy_source = "opted_out", "opt_out_policy"
    elif c.lost_reason:
        policy_band, policy_source = "lost", "client_stage"
    elif c.stage == IgClient.Stage.COLD:
        policy_band, policy_source = "cold", "client_stage"

    evidence = latest_analysis.evidence if latest_analysis else []
    if not isinstance(evidence, list):
        evidence = []
    evidence = evidence[:20]
    evidence_message_ids = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        try:
            message_id = int(item.get("message_id") or 0)
        except (TypeError, ValueError):
            continue
        if message_id and message_id not in evidence_message_ids:
            evidence_message_ids.append(message_id)

    if policy_band:
        return {
            "state": "policy",
            "band": policy_band,
            "label": label_by_band[policy_band],
            "probability": None,
            "confidence": None,
            "source": policy_source,
            "scope": "client_policy",
            "episode_id": current_episode_id,
            "current_episode_id": current_episode_id,
            "watermark_message_id": 0,
            "latest_message_id": latest_message_id,
            "analyzed_at": "",
            "fresh": True,
            "model": "",
            "prompt_version": "",
            "rules_version": "",
            "interaction_type": (
                latest_analysis.interaction_type if latest_analysis else ""
            ),
            "interaction_label": (
                latest_analysis.get_interaction_type_display()
                if latest_analysis
                else "Не визначено"
            ),
            "evidence": evidence,
            "evidence_message_ids": evidence_message_ids,
            "uncertainties": [],
            "metric_label": POTENTIAL_METRIC_LABEL,
            "metric_note": _potential_metric_note(c),
        }

    if not latest_analysis:
        return {
            "state": "unknown",
            "band": "unknown",
            "label": label_by_band["unknown"],
            "probability": None,
            "confidence": None,
            "source": "none",
            "scope": "unknown",
            "episode_id": None,
            "current_episode_id": current_episode_id,
            "watermark_message_id": 0,
            "latest_message_id": latest_message_id,
            "analyzed_at": "",
            "fresh": False,
            "model": "",
            "prompt_version": "",
            "rules_version": "",
            "interaction_type": "unknown",
            "interaction_label": "Не визначено",
            "evidence": [],
            "evidence_message_ids": [],
            "uncertainties": ["analysis_missing"],
            "metric_label": POTENTIAL_METRIC_LABEL,
            "metric_note": _potential_metric_note(c),
        }

    watermark = int(latest_analysis.last_analyzed_message_id or 0)
    episode_id = latest_analysis.commercial_episode_id
    boundary = int(getattr(current_episode, "opened_watermark_message_id", 0) or 0)
    matches_episode = bool(
        not current_episode_id
        or (episode_id and episode_id == current_episode_id)
        or (not episode_id and boundary == 0)
    )
    fresh = bool(
        watermark >= latest_message_id
        and matches_episode
        and (not boundary or watermark >= boundary)
        and (not current_floor or watermark >= current_floor)
    )
    state = "current" if fresh else "stale"
    scope = (
        "current_episode"
        if current_episode_id and episode_id == current_episode_id
        else "conversation"
        if not current_episode_id
        else "historical_episode"
    )
    band = _display_band(
        latest_analysis.score_band,
        verified_payment=client_has_confirmed_purchase(c),
    )
    if band not in label_by_band:
        band = "unknown"
    return {
        "state": state,
        "band": band,
        "label": label_by_band[band],
        "probability": str(latest_analysis.purchase_probability),
        "confidence": str(latest_analysis.confidence),
        "source": "conversation_analysis",
        "scope": scope,
        "episode_id": episode_id,
        "current_episode_id": current_episode_id,
        "watermark_message_id": watermark,
        "latest_message_id": latest_message_id,
        "analyzed_at": latest_analysis.analyzed_at.isoformat(),
        "fresh": fresh,
        "model": latest_analysis.analysis_model,
        "prompt_version": latest_analysis.analysis_prompt_version,
        "rules_version": latest_analysis.rules_version,
        "interaction_type": latest_analysis.interaction_type,
        "interaction_label": latest_analysis.get_interaction_type_display(),
        "evidence": evidence,
        "evidence_message_ids": evidence_message_ids,
        "uncertainties": (
            latest_analysis.uncertainties
            if isinstance(latest_analysis.uncertainties, list)
            else []
        )[:20],
        "metric_label": POTENTIAL_METRIC_LABEL,
        "metric_note": _potential_metric_note(c),
    }


def _operational_client_stage(c) -> tuple[str, str]:
    """Project the visible funnel stage from the current canonical episode.

    ``IgClient.stage`` remains the append-only conversation-analysis fact. A
    bound physical order is stronger operational evidence, but an explicitly
    opened repeat episode must be allowed to restart its own funnel.
    """
    from .ig_bot_models import IgCommercialEpisode

    episode = getattr(c, "current_commercial_episode", None)
    if episode is None:
        prefetched = getattr(c, "_latest_commercial_episode", None)
        if isinstance(prefetched, (list, tuple)):
            episode = prefetched[0] if prefetched else None
        elif prefetched is not None:
            episode = prefetched
    if episode is None and not hasattr(c, "_latest_commercial_episode"):
        episode = c.commercial_episodes.select_related("intended_order").order_by(
            "-sequence", "-id"
        ).first()

    stage = c.stage
    if episode is not None:
        if episode.state == IgCommercialEpisode.State.FULFILLED:
            stage = IgClient.Stage.DONE
        elif (
            episode.state == IgCommercialEpisode.State.ORDER_CREATED
            and episode.intended_order_id
        ):
            stage = IgClient.Stage.ORDER_CREATED
    elif getattr(c, "has_physical_order", False):
        stage = IgClient.Stage.ORDER_CREATED
    return stage, str(IgClient.Stage(stage).label)


def _funnel_progress_for_stage(c, stage: str) -> list[dict]:
    order = list(c.FUNNEL_ORDER)
    try:
        current_index = [item.value for item in order].index(stage)
    except ValueError:
        current_index = -1

    # IMP-034 / F-STATE-005: сервисное обращение (обмен, возврат) — это
    # параллельная ветвь, а не сброс воронки. Раньше такой клиент выглядел как
    # «прогресс 0%», хотя он уже оплатил и получил заказ: карточка клиента #59
    # показывала «cold · Підтримка / скарга · 0%» человеку с оплаченным заказом
    # и обменом в пути. Прогресс сохраняется, а ветвь помечается отдельным
    # признаком — тогда видно и то, что путь пройден, и то, где сейчас внимание.
    side_flow = ""
    side_flow_label = ""
    try:
        from management.services.ig_client_state import resolve_client_state

        state = resolve_client_state(c)
        side_flow = state.side_flow
        side_flow_label = state.side_flow_label or state.side_flow
    except Exception:
        side_flow = ""
        side_flow_label = ""

    # Ветвь привязывается к тому шагу, на котором она реально происходит:
    # обмен и возврат живут после создания заказа, а не в начале воронки.
    side_flow_stage = c.Stage.ORDER_CREATED.value if side_flow else ""
    return [
        {
            "stage": item.value,
            "label": str(item.label),
            "done": current_index >= 0 and index <= current_index,
            "current": item.value == stage,
            "side_flow": side_flow_label if side_flow and item.value == side_flow_stage else "",
        }
        for index, item in enumerate(order)
    ]


def _commercial_visual_state(c, *, payment_confirmation: dict) -> tuple[str, str, str, str]:
    """Return the compact list treatment backed by current commercial truth.

    A Direct delivery status says whether the bot could transport a message; it
    is deliberately not evidence that a parcel moved. Only a current order
    assignment in transit with a tracking number can make the client row
    violet. A delivered order stays green, keeping completed purchases distinct
    from parcels that are still on the way.
    """
    current_episode = getattr(c, "current_commercial_episode", None)
    if current_episode is not None and getattr(c, "pk", None):
        # A previous shipped order is historical evidence only.  Once a new
        # commercial episode is open, shipment colour must be backed by that
        # episode's intended order; otherwise a repeat buyer appears shipped
        # before the new order exists.
        has_shipped_order = getattr(
            c, "has_current_episode_shipped_order", None
        )
        if has_shipped_order is None:
            from .ig_bot_models import IgOrderAssignment

            current_order_id = getattr(current_episode, "intended_order_id", None)
            has_shipped_order = bool(
                current_order_id
                and IgOrderAssignment.objects.filter(
                    client_id=c.pk,
                    order_id=current_order_id,
                    unassigned_at__isnull=True,
                    order__status="ship",
                    order__tracking_number__isnull=False,
                ).exclude(order__tracking_number="").exists()
            )
    else:
        has_shipped_order = getattr(c, "has_shipped_linked_order", None)
    if has_shipped_order is None and getattr(c, "pk", None):
        from .ig_bot_models import IgOrderAssignment

        has_shipped_order = IgOrderAssignment.objects.filter(
            client_id=c.pk,
            unassigned_at__isnull=True,
            order__status="ship",
            order__tracking_number__isnull=False,
        ).exclude(order__tracking_number="").exists()
    if has_shipped_order:
        return (
            "shipped",
            "Відправлено",
            "tracking",
            "Поточне прив'язане замовлення передано перевізнику та має трекінг.",
        )
    if payment_confirmation.get("confirmed"):
        return (
            "paid",
            "Оплачено",
            str(payment_confirmation.get("source") or ""),
            str(payment_confirmation.get("note") or ""),
        )
    return "", "", "", ""


def _client_follow_payload(c, *, settings_obj=None, now=None) -> dict:
    from management.services.ig_follow_state import follow_state_payload

    payload = follow_state_payload(
        c,
        settings_obj=settings_obj,
        now=now,
    )
    payload["refresh_url"] = reverse(
        "management_bot_client_follow_refresh_api",
        args=[c.pk],
    )
    return payload


def _client_card(c, *, follow_settings=None, follow_now=None) -> dict:
    from .ig_bot_models import IgConversationAnalysisSnapshot, IgPostSaleCase

    product = getattr(c, "current_product", None)
    next_followup = getattr(c, "next_followup_at", None)
    from management.services.ig_analysis_materiality import (
        current_analysis_snapshot,
        selector_enforced,
    )

    if not selector_enforced():
        # Preserve the exact pre-materiality projection until the independent
        # selector rollout is explicitly enabled. Shadow ledger writes must
        # not change a card, CTA or follow-up decision.
        missing_prefetch = object()
        customer_analysis = getattr(c, "_latest_customer_analysis", missing_prefetch)
        if customer_analysis is missing_prefetch:
            try:
                latest_analysis = c.analysis_snapshots.exclude(
                    interaction_type=IgConversationAnalysisSnapshot.InteractionType.MANAGER_OBSERVATION
                ).order_by("-id").first()
            except Exception:
                latest_analysis = None
        else:
            latest_analysis = customer_analysis[0] if customer_analysis else None
        all_analysis = getattr(c, "_latest_analysis", missing_prefetch)
        if latest_analysis is None and all_analysis is not missing_prefetch:
            latest_analysis = all_analysis[0] if all_analysis else None
        if latest_analysis is None:
            if all_analysis is missing_prefetch:
                try:
                    latest_analysis = c.analysis_snapshots.order_by("-id").first()
                except Exception:
                    latest_analysis = None
    else:
        prefetched = []
        seen_snapshot_ids = set()
        for attr in ("_latest_customer_analysis", "_latest_analysis"):
            value = getattr(c, attr, None)
            if isinstance(value, (list, tuple)):
                for item in value:
                    if item.pk not in seen_snapshot_ids:
                        prefetched.append(item)
                        seen_snapshot_ids.add(item.pk)
        latest_analysis = current_analysis_snapshot(
            c,
            candidates=prefetched,
        )
    payment_status = ""
    try:
        verified_deal = latest_verified_payment_deal(c)
        truth_projection = latest_payment_projection(c)
        missing_legacy_prefetch = object()
        prefetched_legacy_truth = getattr(
            c,
            "_legacy_payment_truth_deals",
            missing_legacy_prefetch,
        )
        if prefetched_legacy_truth is missing_legacy_prefetch:
            legacy_truth_deal = latest_legacy_payment_truth_deal(c)
        elif prefetched_legacy_truth:
            legacy_truth_deal = prefetched_legacy_truth[0]
        else:
            legacy_truth_deal = None
        truth_deal = (
            truth_projection.deal
            if truth_projection
            else (legacy_truth_deal or verified_deal)
        )
        payment_status = (
            truth_projection.truth
            if truth_projection
            else (
                truth_deal.payment_truth
                if truth_deal and truth_deal.payment_truth != IgDeal.PaymentTruth.UNVERIFIED
                else (truth_deal.payment_status if truth_deal else "unpaid")
            )
        )
    except Exception:
        verified_deal = None
        truth_projection = None
        truth_deal = None
    has_verified_payment = bool(verified_deal)
    manager_verified_order = getattr(c, "has_manager_verified_order", None)
    if manager_verified_order is None:
        from .ig_bot_models import (
            IgOrderAttribution,
            IgPaymentConfirmationReview,
            IgPaymentReviewDecision,
        )

        manager_verified_order = IgOrderAttribution.objects.filter(
            client=c,
            order_id__isnull=False,
            payment_source="manager_verified",
        ).filter(
            Q(
                manager_decision__decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
                manager_decision__verification_source="manager",
                manager_decision__actor_source__in=(
                    IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
                    IgPaymentReviewDecision.ActorSource.TELEGRAM_USER,
                ),
            )
            | Q(
                payment_review__status="confirmed",
                payment_review__decisions__decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
                payment_review__decisions__verification_source="manager",
                payment_review__decisions__actor_source__in=(
                    IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
                    IgPaymentReviewDecision.ActorSource.TELEGRAM_USER,
                ),
            )
        ).exists()
    payment_confirmation = current_payment_confirmation(c)
    linked_order_payment_context = active_client_order_payment_context(c)
    commercially_confirmed = bool(payment_confirmation["confirmed"])
    purchase_history = historical_purchase_confirmation(c)
    payment_truth = (
        truth_projection.truth
        if truth_projection
        else (
            truth_deal.payment_truth
            if truth_deal and truth_deal.payment_truth != IgDeal.PaymentTruth.UNVERIFIED
            else (IgDeal.PaymentTruth.CONFIRMED if has_verified_payment else IgDeal.PaymentTruth.UNVERIFIED)
        )
    )
    hard_stages = {IgClient.Stage.PAID, IgClient.Stage.ORDER_CREATED, IgClient.Stage.DONE}
    displayed_stage, displayed_stage_label = _operational_client_stage(c)
    if displayed_stage in hard_stages and not has_verified_payment:
        if payment_truth in {IgDeal.PaymentTruth.REFUNDED, IgDeal.PaymentTruth.REVERSED}:
            displayed_stage = "payment_reversed"
            displayed_stage_label = "Оплату повернено / скасовано"
        elif commercially_confirmed:
            if displayed_stage == IgClient.Stage.PAID:
                displayed_stage_label = "Оплачено менеджером"
        elif purchase_history["confirmed"]:
            # An archived completed sale keeps its historical funnel position,
            # but receives no current-payment color or wording.
            pass
        else:
            displayed_stage = "unverified"
            displayed_stage_label = "Потребує звірки оплати"
    active_opt_out = bool(
        c.opted_out_at
        and (not c.opted_in_at or c.opted_in_at < c.opted_out_at)
    )
    interaction_type = _display_interaction_type(
        c, latest_analysis.interaction_type if latest_analysis else ""
    )
    post_sale_type = str(getattr(c, "latest_post_sale_type", "") or "")
    post_sale_status = str(getattr(c, "latest_post_sale_status", "") or "")
    post_sale_order_id = getattr(c, "latest_post_sale_order_id", None)
    post_sale_needs_action = bool(getattr(c, "has_post_sale_action", False))
    if not hasattr(c, "latest_post_sale_type"):
        latest_post_sale = c.post_sale_cases.order_by("-updated_at", "-id").first()
        if latest_post_sale:
            post_sale_type = latest_post_sale.case_type
            post_sale_status = latest_post_sale.status
            post_sale_order_id = latest_post_sale.order_id
            post_sale_needs_action = latest_post_sale.status in {
                IgPostSaleCase.Status.NEEDS_DETAILS,
                IgPostSaleCase.Status.OPEN,
            }
    # Раньше бейдж «Обмін» висел вечно: `latest_post_sale` берёт последний кейс
    # любого статуса, включая `completed` и `cancelled`. Закрытый обмен — это
    # история заказа, а не текущее состояние диалога.
    if post_sale_status in TERMINAL_POST_SALE_STATUSES:
        post_sale_type = ""
        post_sale_status = ""
        post_sale_order_id = None
        post_sale_needs_action = False
    post_sale_labels = dict(IgPostSaleCase.CaseType.choices)
    post_sale_status_labels = dict(IgPostSaleCase.Status.choices)
    post_sale_type_label = str(post_sale_labels.get(post_sale_type, ""))
    post_sale_status_label = str(post_sale_status_labels.get(post_sale_status, ""))
    potential = _client_potential_payload(c, latest_analysis)
    (
        commercial_visual_state,
        commercial_visual_state_label,
        commercial_visual_state_source,
        commercial_visual_state_note,
    ) = _commercial_visual_state(
        c,
        payment_confirmation=payment_confirmation,
    )
    return {
        "id": c.id,
        "igsid": c.igsid,
        "username": c.username,
        "name": c.display_name or c.username or c.igsid,
        "avatar": c.avatar_local or c.profile_pic_url,
        "stage": displayed_stage,
        "stage_raw": c.stage,
        "stage_label": displayed_stage_label,
        "last_message_at": c.last_message_at.isoformat() if c.last_message_at else "",
        "purchases": c.purchases_count,
        "total_spent": str(c.total_spent),
        "bot_paused": c.bot_paused,
        "opted_out": active_opt_out,
        "opted_out_at": c.opted_out_at.isoformat() if c.opted_out_at else "",
        "manager_takeover": c.manager_takeover,
        "spam_strikes": c.spam_strikes,
        "ad_title": c.ad_title,
        "ad_id": c.ad_id,
        "ad_ref": c.ad_ref,
        "language": c.language,
        "intent": c.intent,
        "buying_readiness": c.buying_readiness,
        "analysis_band": latest_analysis.score_band if latest_analysis else "",
        "analysis_band_label": latest_analysis.get_score_band_display() if latest_analysis else "",
        "interaction_type": interaction_type,
        "interaction_type_label": latest_analysis.get_interaction_type_display() if latest_analysis else "Не визначено",
        "interaction_tone": _interaction_tone(interaction_type),
        "analysis_probability": str(latest_analysis.purchase_probability) if latest_analysis else "",
        "analysis_confidence": str(latest_analysis.confidence) if latest_analysis else "",
        "analysis_evidence": latest_analysis.evidence if latest_analysis else [],
        "analysis_uncertainties": latest_analysis.uncertainties if latest_analysis else [],
        "analysis_at": latest_analysis.analyzed_at.isoformat() if latest_analysis else "",
        "potential": potential,
        "manager_action_required": bool(getattr(c, "has_manager_action", False)),
        "post_sale_type": post_sale_type,
        "post_sale_type_label": post_sale_type_label,
        "post_sale_status": post_sale_status,
        "post_sale_status_label": post_sale_status_label,
        # Тип без статуса заставлял менеджера открывать карточку, чтобы понять,
        # ждёт ли обмен его действия или уже едет к клиенту.
        "post_sale_badge_label": (
            f"{post_sale_type_label} · {post_sale_status_label}"
            if post_sale_type_label and post_sale_status_label
            else post_sale_type_label
        ),
        "post_sale_needs_action": post_sale_needs_action,
        "buyer": _buyer_badge_payload(c),
        "post_sale_order_id": post_sale_order_id,
        "intent_label": c.get_intent_display(),
        "primary_objection": c.primary_objection,
        "primary_objection_label": c.get_primary_objection_display(),
        "lost_reason": c.lost_reason,
        "hidden": bool(c.hidden_at),
        "hidden_reason": c.hidden_reason,
        "current_product_id": c.current_product_id,
        "current_product_title": getattr(product, "title", "") if product else "",
        "current_size": c.current_size,
        "current_color": c.current_color,
        "current_qty": c.current_qty,
        "product_confidence": str(c.current_product_confidence),
        "next_followup_at": next_followup.isoformat() if next_followup else "",
        "followup_level": c.followup_level,
        "discount_offered_percent": c.discount_offered_percent,
        "payment_status": payment_status,
        "payment_truth": payment_truth,
        "commercially_confirmed": bool(commercially_confirmed),
        "commercial_confirmation_source": (
            "manager_verified_order"
            if manager_verified_order and commercially_confirmed
            else str(payment_confirmation.get("source") or "")
        ),
        "commercial_visual_state": commercial_visual_state,
        "commercial_visual_state_label": commercial_visual_state_label,
        "commercial_visual_state_source": commercial_visual_state_source,
        "commercial_visual_state_note": commercial_visual_state_note,
        "linked_order_payment_context": linked_order_payment_context,
        "purchase_history": purchase_history,
        "delivery_status": c.delivery_status,
        "delivery_status_label": c.get_delivery_status_display() if c.delivery_status else "",
        "delivery_error": c.delivery_error,
        "delivery_failed_at": c.delivery_failed_at.isoformat() if c.delivery_failed_at else "",
        "follow": _client_follow_payload(
            c,
            settings_obj=follow_settings,
            now=follow_now,
        ),
    }


@login_required(login_url="management_login")
@require_GET
def bot_clients_api(request):
    blocked = _require_bot_capabilities(
        request, VIEW_IG_CONVERSATION_PII_PERMISSION
    )
    if blocked:
        return blocked
    from django.db.models import Q

    from .models import IgClient, IgDeal

    # The overview badge does not need the expensive CRM card projection.  It
    # used to build up to 100 full cards (including analysis/payment prefetches)
    # on every page load even when the Clients tab was never opened.
    if request.GET.get("summary") == "1":
        return JsonResponse({
            "success": True,
            "clients": [],
            "total": IgClient.objects.filter(hidden_at__isnull=True).count(),
            "summary_only": True,
        })

    follow_settings = InstagramBotSettings.load()

    view = (request.GET.get("view") or "all").strip().lower()
    from django.db.models import PositiveBigIntegerField, Prefetch
    from .ig_bot_models import (
        IgCommercialEpisode,
        IgConversationAnalysisSnapshot,
        IgFunnelResetAudit,
        IgOrderAssignment,
        IgOrderAttribution,
        IgPaymentConfirmationReview,
        IgPaymentReviewDecision,
    )

    manager_verified_orders = IgOrderAttribution.objects.filter(
        client_id=OuterRef("pk"),
        order_id__isnull=False,
        payment_source="manager_verified",
    ).filter(
        Q(
            manager_decision__decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
            manager_decision__verification_source="manager",
            manager_decision__actor_source__in=(
                IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
                IgPaymentReviewDecision.ActorSource.TELEGRAM_USER,
            ),
        )
        | Q(
            payment_review__status=IgPaymentConfirmationReview.Status.CONFIRMED,
            payment_review__decisions__decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
            payment_review__decisions__verification_source="manager",
            payment_review__decisions__actor_source__in=(
                IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
                IgPaymentReviewDecision.ActorSource.TELEGRAM_USER,
            ),
        )
    )
    historical_paid_reviews = IgPaymentConfirmationReview.objects.filter(
        client_id=OuterRef("pk"),
        status=IgPaymentConfirmationReview.Status.CONFIRMED,
        resolution_kind=(
            IgPaymentConfirmationReview.ResolutionKind.HISTORICAL_PAID_ARCHIVED
        ),
    )
    current_manager_reviews = IgPaymentConfirmationReview.objects.filter(
        current_manager_confirmation_review_q(),
        client_id=OuterRef("pk"),
    )
    current_episode_deals = IgDeal.objects.filter(
        client_id=OuterRef("pk"),
        commercial_episode__pk=OuterRef("current_commercial_episode_id"),
    ).filter(verified_payment_q())
    current_episode_review_id = IgCommercialEpisode.objects.filter(
        pk=OuterRef(OuterRef("current_commercial_episode_id")),
    ).values("primary_payment_review_id")[:1]
    current_episode_manager_reviews = IgPaymentConfirmationReview.objects.filter(
        current_manager_confirmation_review_q(),
        client_id=OuterRef("pk"),
        pk=Subquery(current_episode_review_id),
    )
    current_episode_order_id = IgCommercialEpisode.objects.filter(
        pk=OuterRef(OuterRef("current_commercial_episode_id")),
    ).values("intended_order_id")[:1]
    current_episode_paid_order_assignments = IgOrderAssignment.objects.filter(
        client_id=OuterRef("pk"),
        unassigned_at__isnull=True,
        order_id=Subquery(current_episode_order_id),
        order__payment_status__in=CONFIRMED_ORDER_PAYMENT_STATUSES,
    )
    current_episode_paid_order_attributions = IgOrderAttribution.objects.filter(
        client_id=OuterRef("pk"),
        order_id=Subquery(current_episode_order_id),
        order__payment_status__in=CONFIRMED_ORDER_PAYMENT_STATUSES,
    )
    current_episode_shipped_order_assignments = IgOrderAssignment.objects.filter(
        client_id=OuterRef("pk"),
        unassigned_at__isnull=True,
        order_id=Subquery(current_episode_order_id),
        order__status="ship",
        order__tracking_number__isnull=False,
    ).exclude(order__tracking_number="")
    paid_order_assignments = IgOrderAssignment.objects.filter(
        client_id=OuterRef("pk"),
        unassigned_at__isnull=True,
        order__payment_status__in=CONFIRMED_ORDER_PAYMENT_STATUSES,
    )
    active_assigned_paid_orders = paid_order_assignments.order_by(
        "-assigned_at", "-id"
    )
    paid_order_attributions = IgOrderAttribution.objects.filter(
        client_id=OuterRef("pk"),
        order__payment_status__in=CONFIRMED_ORDER_PAYMENT_STATUSES,
    )
    physical_orders = IgOrderAttribution.objects.filter(
        client_id=OuterRef("pk"),
        order_id__isnull=False,
    )
    shipped_linked_orders = IgOrderAssignment.objects.filter(
        client_id=OuterRef("pk"),
        unassigned_at__isnull=True,
        order__status="ship",
        order__tracking_number__isnull=False,
    ).exclude(order__tracking_number="")

    client_rows = IgClient.objects.select_related(
        "current_product",
        "current_commercial_episode",
        "follow_state_projection",
        "analysis_job",
    )
    from management.services.ig_analysis_materiality import RESET_FLOOR_ANNOTATION

    # `_client_potential_payload()` applies the reset boundary in both legacy
    # and enforced selector modes.  Annotating it unconditionally avoids one
    # IgFunnelResetAudit query per card when the selector is still in shadow.
    reset_after = (
        IgFunnelResetAudit.objects.filter(client_id=OuterRef("pk"))
        .order_by("-id")
        .values("reset_after_message_id")[:1]
    )
    client_rows = client_rows.annotate(**{
        RESET_FLOOR_ANNOTATION: Coalesce(
            Subquery(
                reset_after,
                output_field=PositiveBigIntegerField(),
            ),
            Value(0),
            output_field=PositiveBigIntegerField(),
        ),
    })

    client_rows = client_rows.prefetch_related(
        Prefetch(
            "analysis_snapshots",
            queryset=IgConversationAnalysisSnapshot.objects.select_related("commercial_episode").exclude(
                interaction_type=IgConversationAnalysisSnapshot.InteractionType.MANAGER_OBSERVATION
            ).order_by("-id")[:1],
            to_attr="_latest_customer_analysis",
        ),
        Prefetch(
            "analysis_snapshots",
            queryset=IgConversationAnalysisSnapshot.objects.select_related("commercial_episode").order_by("-id")[:1],
            to_attr="_latest_analysis",
        ),
        Prefetch(
            "deals",
            queryset=IgDeal.objects.filter(verified_payment_q()).order_by("-paid_at", "-id"),
            to_attr="_verified_payment_deals",
        ),
        Prefetch(
            "deals",
            queryset=IgDeal.objects.exclude(
                payment_truth=IgDeal.PaymentTruth.UNVERIFIED,
            ).order_by("-payment_truth_updated_at", "-id")[:1],
            to_attr="_legacy_payment_truth_deals",
        ),
        Prefetch(
            "payment_projections",
            queryset=IgPaymentProjection.objects.select_related("deal").order_by("-updated_at", "-id"),
            to_attr="_payment_projections",
        ),
        Prefetch(
            "commercial_episodes",
            queryset=IgCommercialEpisode.objects.select_related("intended_order").order_by(
                "-sequence", "-id"
            )[:1],
            to_attr="_latest_commercial_episode",
        ),
    ).annotate(
            has_manager_verified_order=Exists(manager_verified_orders),
            has_current_manager_confirmation=Exists(current_manager_reviews),
            has_current_paid_linked_order=(
                Exists(paid_order_assignments) | Exists(paid_order_attributions)
            ),
            active_assigned_paid_order_id=Subquery(
                active_assigned_paid_orders.values("order_id")[:1]
            ),
            active_assigned_paid_order_payment_status=Subquery(
                active_assigned_paid_orders.values("order__payment_status")[:1]
            ),
            active_assigned_paid_order_source=Subquery(
                active_assigned_paid_orders.values("order__source")[:1]
            ),
            has_current_episode_provider_payment=Exists(current_episode_deals),
            has_current_episode_manager_confirmation=Exists(current_episode_manager_reviews),
            has_current_episode_paid_linked_order=(
                Exists(current_episode_paid_order_assignments)
                | Exists(current_episode_paid_order_attributions)
            ),
            has_current_episode_shipped_order=Exists(
                current_episode_shipped_order_assignments
            ),
            has_commercial_confirmation=(
                Exists(current_manager_reviews)
                | Exists(paid_order_assignments)
                | Exists(paid_order_attributions)
            ),
            has_historical_paid_archive=Exists(historical_paid_reviews),
            has_physical_order=Exists(physical_orders),
            has_shipped_linked_order=Exists(shipped_linked_orders),
        )
    qs = _with_latest_interaction(annotate_verified_payment(
        client_rows
    ))
    # `client_has_confirmed_purchase` is called once per row further down.
    # Annotating it here keeps the 200-client list at a constant query count.
    qs = annotate_confirmed_purchase(qs)
    unfiltered_qs = qs
    if view in {"hidden"}:
        qs = qs.filter(hidden_at__isnull=False)
    else:
        qs = qs.filter(hidden_at__isnull=True)
    if view in {"spam", "cold", "spam-cold", "spam_cold"}:
        qs = qs.filter(Q(stage__in=[IgClient.Stage.SPAM, IgClient.Stage.COLD]) | Q(spam_strikes__gt=0))
    elif view == "paid":
        qs = qs.filter(
            (
                Q(current_commercial_episode_id__isnull=False)
                & (
                    Q(has_current_episode_provider_payment=True)
                    | Q(has_current_episode_manager_confirmation=True)
                    | Q(has_current_episode_paid_linked_order=True)
                )
            )
            | (
                Q(current_commercial_episode_id__isnull=True)
                & (
                    Q(has_verified_payment=True)
                    | Q(has_current_manager_confirmation=True)
                    | Q(has_current_paid_linked_order=True)
                )
            )
        )
    elif view == "due":
        qs = qs.filter(followup_tasks__status="pending", followup_tasks__due_at__lte=timezone.now()).distinct()
    elif view == "ads":
        qs = qs.filter(Q(ad_id__gt="") | Q(ad_ref__gt="") | Q(ad_title__gt=""))
    elif view in {"delivery-blocked", "delivery_blocked"}:
        qs = qs.filter(delivery_status__gt="")
    elif view in {"complaints", "support"}:
        qs = qs.filter(
            latest_interaction_type=IgConversationAnalysisSnapshot.InteractionType.SUPPORT_COMPLAINT
        )
    elif view == "wholesale":
        qs = qs.filter(
            latest_interaction_type=IgConversationAnalysisSnapshot.InteractionType.WHOLESALE_B2B
        )
    elif view == "collaboration":
        qs = qs.filter(
            latest_interaction_type=IgConversationAnalysisSnapshot.InteractionType.COLLABORATION
        )
    elif view in {"reactions", "community"}:
        qs = qs.filter(latest_interaction_type__in=[
            IgConversationAnalysisSnapshot.InteractionType.REACTION_ONLY,
            IgConversationAnalysisSnapshot.InteractionType.COMMUNITY_CASUAL,
        ])
    elif view == "active":
        qs = qs.exclude(stage__in=[IgClient.Stage.SPAM, IgClient.Stage.COLD])
        qs = qs.filter(
            Q(has_post_sale_action=True)
            | Q(
                current_commercial_episode_id__isnull=False,
                has_current_episode_provider_payment=False,
                has_current_episode_manager_confirmation=False,
                has_current_episode_paid_linked_order=False,
            )
            | Q(
                current_commercial_episode_id__isnull=True,
                has_verified_payment=False,
                has_commercial_confirmation=False,
                has_historical_paid_archive=False,
            )
        )
    qs = qs.order_by("-last_message_at", "-id")
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(username__icontains=q)
            | Q(display_name__icontains=q)
            | Q(igsid__icontains=q)
            | Q(phone__icontains=q)
        )
    from django.core.paginator import Paginator

    # This is a live-polled workspace, not a bulk export.  Keep the server-side
    # cap fixed even if an old client asks for the former 100/200-row pages.
    page_size = 20
    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(request.GET.get("page") or 1)
    clients = list(page_obj.object_list)
    total = paginator.count
    try:
        requested_client_id = int(request.GET.get("client_id") or 0)
    except (TypeError, ValueError):
        requested_client_id = 0
    requested_client_injected = False
    if requested_client_id and all(c.pk != requested_client_id for c in clients):
        requested_client = unfiltered_qs.filter(pk=requested_client_id).first()
        if requested_client:
            clients.insert(0, requested_client)
            requested_client_injected = True
    follow_now = timezone.now()
    rows = [
        _client_card(c, follow_settings=follow_settings, follow_now=follow_now)
        for c in clients
    ]
    return JsonResponse({
        "success": True,
        "clients": rows,
        "total": total,
        "requested_client_injected": requested_client_injected,
        "pagination": {
            "page": page_obj.number,
            "page_size": page_size,
            "total_items": total,
            "total_pages": paginator.num_pages,
            "start_item": page_obj.start_index(),
            "end_item": page_obj.end_index(),
            "has_previous": page_obj.has_previous(),
            "has_next": page_obj.has_next(),
        },
    })


@login_required(login_url="management_login")
@require_POST
def bot_client_follow_refresh_api(request, client_id):
    """Explicit manager refresh for one stale/unknown follow projection."""
    blocked = _require_bot_write_json(request)
    if blocked:
        return blocked
    client = IgClient.objects.select_related("follow_state_projection").filter(
        pk=client_id,
    ).first()
    if client is None:
        return JsonResponse({"success": False, "error": "Клієнта не знайдено."}, status=404)
    from management.services.ig_follow_state import (
        follow_state_payload,
        refresh_follow_state_if_due,
    )

    result = refresh_follow_state_if_due(
        client,
        trigger="manager_request",
    )
    # Re-read the projection after provider publication. This keeps the
    # response authoritative while the manager UI remains the only caller of
    # this synchronous endpoint.
    client = IgClient.objects.select_related("follow_state_projection").get(pk=client.pk)
    follow = follow_state_payload(client)
    follow["refresh_url"] = reverse(
        "management_bot_client_follow_refresh_api",
        args=[client.pk],
    )
    return JsonResponse({"success": True, "result": result, "follow": follow})


@login_required(login_url="management_login")
@require_POST
def bot_client_followup_delivery_resolve_api(request, client_id, task_id):
    blocked = _require_bot_write_json(request)
    if blocked:
        return blocked
    task = IgFollowUpTask.objects.filter(pk=task_id, client_id=client_id).first()
    if task is None:
        return JsonResponse({"success": False, "error": "Клієнта або follow-up не знайдено."}, status=404)
    with transaction.atomic():
        result = bot_followups.resolve_ambiguous_followup(
            task.pk,
            outcome=request.POST.get("outcome"),
            actor_id=request.user.pk,
            note=request.POST.get("note", ""),
            now=timezone.now(),
        )
        if not result.get("ok"):
            return JsonResponse({"success": False, **result}, status=result.get("status", 400))
        if not result.get("idempotent"):
            review_id = IgFollowUpTask.objects.filter(
                delivery_review_for_id=task.pk,
            ).values_list("pk", flat=True).first()
            AdminAuditLog.objects.create(
                actor=request.user,
                actor_role="staff",
                action="ig_followup_delivery_resolved",
                entity_type="IgFollowUpTask",
                entity_id=str(task.pk),
                before={"status": IgFollowUpTask.Status.AMBIGUOUS},
                after={
                    "status": result.get("status", ""),
                    "outcome": result.get("outcome", ""),
                    "review_id": review_id,
                },
                reason=str(request.POST.get("note") or "").strip()[:500],
            )
    return JsonResponse({"success": True, **result})


@login_required(login_url="management_login")
@require_POST
def bot_client_followup_continue_api(request, client_id, task_id):
    """Audited staff continuation for an event-triggered policy task."""
    blocked = _require_bot_write_json(request)
    if blocked:
        return blocked
    task = IgFollowUpTask.objects.filter(pk=task_id, client_id=client_id).first()
    if task is None:
        return JsonResponse(
            {"success": False, "error": "Клієнта або follow-up не знайдено."},
            status=404,
        )
    before_status = task.status
    with transaction.atomic():
        result = bot_followups.continue_event_followup(
            task.pk,
            actor_id=request.user.pk,
            note=request.POST.get("note", ""),
            now=timezone.now(),
        )
        if not result.get("ok"):
            return JsonResponse(
                {"success": False, **result}, status=result.get("status", 400)
            )
        if not result.get("idempotent"):
            AdminAuditLog.objects.create(
                actor=request.user,
                actor_role="staff",
                action="ig_event_followup_continued",
                entity_type="IgFollowUpTask",
                entity_id=str(task.pk),
                before={"status": before_status},
                after={
                    "status": before_status,
                    "next_task_id": result.get("next_task_id"),
                },
                reason=str(request.POST.get("note") or "").strip()[:500],
            )
    return JsonResponse({"success": True, **result})


@login_required(login_url="management_login")
@require_GET
def bot_client_detail_api(request, client_id):
    blocked = _require_bot_capabilities(
        request, VIEW_IG_CONVERSATION_PII_PERMISSION
    )
    if blocked:
        return blocked
    from .models import IgClient, InstagramBotMessage
    from .ig_bot_models import (
        IgConversationAnalysisSnapshot,
        IgPaymentConfirmationReview,
        IgPaymentReviewDecision,
    )
    from management.services.ig_commercial_episodes import client_episode_payload
    from management.services.ig_funnel_reset import current_message_floor

    c = IgClient.objects.select_related(
        "current_product",
        "current_commercial_episode",
        "current_commercial_episode__intended_order",
        "follow_state_projection",
    ).filter(id=client_id).first()
    if not c:
        return JsonResponse({"success": False, "error": "Клієнта не знайдено."}, status=404)

    from management.services.ig_journey_snapshot import (
        InvalidJourneyEpisode, build_journey_snapshot,
    )

    viewed_episode_id = request.GET.get("view_episode_id") or None
    journey = None
    if request.GET.get("journey_only") == "1" or not request.GET.get("before_id"):
        try:
            journey = build_journey_snapshot(c, view_episode_id=viewed_episode_id)
        except InvalidJourneyEpisode:
            return JsonResponse(
                {"success": False, "error": "Покупку не знайдено."}, status=404,
            )
    if request.GET.get("journey_only") == "1":
        return JsonResponse({"success": True, "journey": journey})

    try:
        after_id = int(request.GET.get("after_id") or 0)
    except (TypeError, ValueError):
        after_id = 0
    try:
        before_id = int(request.GET.get("before_id") or 0)
    except (TypeError, ValueError):
        before_id = 0

    if before_id:
        # Older history is loaded from the local immutable transcript. Keep
        # the page bounded and expose a cursor so the UI can walk backwards.
        msg_rows = list(c.messages.filter(id__lt=before_id).order_by("-id")[:100])
        msg_rows.reverse()
    elif after_id:
        msg_rows = list(c.messages.filter(id__gt=after_id).order_by("id")[:100])
    else:
        # Останні 300 (а не найстаріші) у хронологічному порядку — для live chat.
        msg_rows = list(c.messages.order_by("-id")[:300])
        msg_rows.reverse()
    media_evidence = (c.sales_context or {}).get("_media_evidence", []) if isinstance(c.sales_context, dict) else []
    messages = [
        {
            "id": m.id,
            "role": m.role,
            "text": m.text,
            "gemini_model": m.gemini_model,
            "attachments": m.attachments or "",
            "media": _message_media_rows(m, media_evidence),
            "time": (m.provider_created_at or m.created_at).isoformat()
            if (m.provider_created_at or m.created_at)
            else "",
        }
        for m in msg_rows
    ]
    last_message_id = msg_rows[-1].id if msg_rows else after_id
    oldest_message_id = msg_rows[0].id if msg_rows else (before_id or last_message_id)
    newest_message_id = msg_rows[-1].id if msg_rows else (after_id or oldest_message_id)
    has_older = bool(
        oldest_message_id
        and c.messages.filter(id__lt=oldest_message_id).exists()
    )

    # Інкрементальний режим (live chat): лише нові повідомлення + прапори стану,
    # без важких events/deals/funnel — щоб не вантажити сервер на кожному поллі.
    if before_id:
        return JsonResponse({
            "success": True,
            "messages": messages,
            "oldest_message_id": oldest_message_id,
            "newest_message_id": newest_message_id,
            "has_older": has_older,
        })

    if after_id:
        operational_stage, operational_stage_label = _operational_client_stage(c)
        return JsonResponse({
            "success": True,
            "messages": messages,
            "oldest_message_id": oldest_message_id,
            "newest_message_id": newest_message_id,
            "has_older": has_older,
            "last_message_id": last_message_id,
            "bot_paused": c.bot_paused,
            "manager_takeover": c.manager_takeover,
            "stage": operational_stage,
            "stage_label": operational_stage_label,
            "funnel": _funnel_progress_for_stage(c, operational_stage),
            "journey": journey,
            "follow": _client_follow_payload(c),
        })

    events = [
        {
            "from": e.from_stage,
            "to": e.to_stage,
            "reason": e.reason,
            "time": e.created_at.isoformat() if e.created_at else "",
        }
        for e in c.stage_events.all()[:50]
    ]
    current_floor = current_message_floor(c)
    signal_rows = [
        {
            "type": s.signal_type,
            "confidence": str(s.confidence),
            "value": s.value,
            "time": s.created_at.isoformat() if s.created_at else "",
        }
        for s in c.conversation_signals.filter(
            message_id__gte=current_floor,
        ).order_by("-created_at", "-id")[:120]
    ]
    signals = _group_signal_rows(signal_rows)
    pattern_episode = c.current_commercial_episode
    if pattern_episode is None:
        pattern_episode = c.commercial_episodes.order_by("-sequence", "-id").first()
    pattern_messages = c.messages.filter(id__gte=current_floor)
    if pattern_episode and pattern_episode.opened_watermark_message_id:
        pattern_messages = pattern_messages.filter(
            id__gte=pattern_episode.opened_watermark_message_id
        )
    role_counts = {
        "customer": 0,
        "manager": 0,
        "agent": 0,
    }
    role_map = {
        InstagramBotMessage.Role.USER: "customer",
        InstagramBotMessage.Role.MANAGER: "manager",
        InstagramBotMessage.Role.MODEL: "agent",
    }
    for row in pattern_messages.values("role").annotate(count=Count("id")):
        bucket = role_map.get(row["role"])
        if bucket:
            role_counts[bucket] = int(row["count"] or 0)
    pattern_evidence_ids = list(
        pattern_messages.order_by("-id").values_list("id", flat=True)[:20]
    )
    delivery_review_ids = IgFollowUpTask.objects.filter(
        delivery_review_for_id=OuterRef("pk"),
    ).values("pk")[:1]
    followups = []
    for f in c.followup_tasks.annotate(
        delivery_review_id=Subquery(delivery_review_ids),
    ).order_by("-created_at", "-id")[:50]:
        review_id = getattr(f, "delivery_review_id", None)
        continuation_exists = (
            f.trigger == IgFollowUpTask.Trigger.EVENT
            and IgFollowUpTask.objects.filter(
                event_key__startswith=f"event_policy_continue:{f.pk}:"
            ).exists()
        )
        followups.append({
            "id": f.id,
            "kind": f.kind,
            "status": f.status,
            "status_label": (
                "Потребує перевірки"
                if f.kind == IgFollowUpTask.Kind.MANAGER_TASK
                and f.reason.startswith("prize_review:")
                and f.manager_approval_status
                == IgFollowUpTask.ManagerApprovalStatus.PENDING
                else f.get_status_display()
            ),
            "reason": f.reason,
            "reason_label": (
                "Перевірка призового сертифіката"
                if f.reason.startswith("prize_review:")
                else ""
            ),
            "discount_percent": f.discount_percent,
            "due_at": f.due_at.isoformat() if f.due_at else "",
            "meta_window_deadline": f.meta_window_deadline.isoformat() if f.meta_window_deadline else "",
            "message_text": f.message_text,
            "skip_reason": f.skip_reason,
            "last_error": f.last_error,
            "attempt_count": f.attempt_count,
            "provider_message_id": f.provider_message_id,
            "trigger": f.trigger,
            "event_key": f.event_key,
            "event_occurred_at": f.event_occurred_at.isoformat() if f.event_occurred_at else "",
            "policy_started_at": f.policy_started_at.isoformat() if f.policy_started_at else "",
            "event_payload": f.event_payload if isinstance(f.event_payload, dict) else {},
            "delivery_review_id": review_id,
            "allowed_outcomes": (
                ["delivered", "not_delivered"]
                if f.status == IgFollowUpTask.Status.AMBIGUOUS and review_id
                else []
            ),
            "resolution_url": (
                reverse(
                    "management_bot_client_followup_delivery_resolve_api",
                    args=[c.pk, f.pk],
                )
                if f.status == IgFollowUpTask.Status.AMBIGUOUS and review_id
                else ""
            ),
            "continue_url": (
                reverse(
                    "management_bot_client_followup_continue_api",
                    args=[c.pk, f.pk],
                )
                if f.trigger == IgFollowUpTask.Trigger.EVENT
                and f.event_key
                and not continuation_exists
                and f.status in {
                    IgFollowUpTask.Status.SENT,
                    IgFollowUpTask.Status.COMPLETED,
                    IgFollowUpTask.Status.CANCELLED,
                    IgFollowUpTask.Status.SKIPPED,
                }
                else ""
            ),
        })
    deal_rows = list(c.deals.select_related("payment_projection", "order").all()[:20])
    deals = [
        {
            "id": d.id,
            "status": d.status,
            "amount": str(d.amount),
            "pay_type": d.pay_type,
            "payment_status": d.payment_status,
            "invoice_url": d.invoice_url,
            "order_id": d.order_id,
        }
        for d in deal_rows
    ]
    review_base = _payment_review_workspace_queryset().filter(
        client=c,
    ).exclude(status=IgPaymentConfirmationReview.Status.SUPERSEDED)
    review_counts = review_base.aggregate(
        total=Count("id"),
        pending=Count(
            "id",
            filter=Q(status=IgPaymentConfirmationReview.Status.PENDING),
        ),
        confirmed=Count(
            "id",
            filter=Q(status=IgPaymentConfirmationReview.Status.CONFIRMED),
        ),
        rejected=Count(
            "id",
            filter=Q(status=IgPaymentConfirmationReview.Status.CANCELLED),
        ),
        order_resolution=Count(
            "id",
            filter=Q(
                status=IgPaymentConfirmationReview.Status.CONFIRMED,
                order_id__isnull=True,
            )
            & ~Q(
                resolution_kind=(
                    IgPaymentConfirmationReview.ResolutionKind.HISTORICAL_PAID_ARCHIVED
                )
            ),
        ),
    )
    actionable_review_row = review_base.filter(
        Q(status=IgPaymentConfirmationReview.Status.PENDING)
        | (
            Q(
                status=IgPaymentConfirmationReview.Status.CONFIRMED,
                order_id__isnull=True,
            )
            & ~Q(
                resolution_kind=(
                    IgPaymentConfirmationReview.ResolutionKind.HISTORICAL_PAID_ARCHIVED
                )
            )
        )
    ).order_by("-created_at", "-id").first()
    review_rows = list(review_base.order_by("-created_at", "-id")[:20])
    review_payloads = [_payment_review_workspace_payload(row) for row in review_rows]
    active_review = None
    if actionable_review_row is not None:
        active_review = next(
            (
                item for item in review_payloads
                if item["review_id"] == actionable_review_row.pk
            ),
            None,
        )
        if active_review is None:
            active_review = _payment_review_workspace_payload(actionable_review_row)
    all_attribution_base = _order_attribution_workspace_queryset().filter(
        client=c,
    )
    attribution_base = all_attribution_base.exclude(
        order_id__in=Subquery(
            review_base.exclude(order_id__isnull=True).values("order_id")
        )
    )
    # The card list intentionally hides attribution rows already represented by
    # a payment review, but the client contract must report every physical
    # Instagram-attributed order, including review-linked orders.
    attribution_total = all_attribution_base.values("order_id").distinct().count()
    attribution_rows = list(attribution_base.order_by("-created_at", "-id")[:20])
    order_cards = _canonical_order_workspace_cards(
        review_rows,
        attribution_rows,
        limit=20,
    )
    from .ig_bot_models import (
        IgOrderAssignment,
        IgOrderAssignmentEvent,
        IgUgcEvidenceAssessment,
        IgUgcReward,
    )
    from management.services.ig_ugc_rewards import (
        reward_payload,
        ugc_reward_eligibility,
    )
    from orders.fulfillment_truth import nova_poshta_order_fulfillment_confirmed

    assignment_rows = list(
        IgOrderAssignment.objects.filter(
            client=c,
            unassigned_at__isnull=True,
        )
        .select_related("order", "assigned_by")
        .order_by("-assigned_at", "-id")[:20]
    )
    assignment_payloads = [
        _assignment_workspace_payload(row, client_id=c.pk)
        for row in assignment_rows
    ]
    assignment_event_rows = list(
        IgOrderAssignmentEvent.objects.filter(
            Q(to_client=c) | Q(from_client=c),
        )
        .select_related("order", "actor")
        .order_by("-created_at", "-id")[:30]
    )
    assignment_event_payloads = [
        _assignment_event_workspace_payload(row)
        for row in assignment_event_rows
    ]
    ugc_reward_rows = list(
        IgUgcReward.objects.filter(client=c)
        .select_related("order", "assignment", "promo_code", "reviewed_by")
        .order_by("-reviewed_at", "-id")[:20]
    )
    ugc_assessment_rows = list(
        IgUgcEvidenceAssessment.objects.filter(client=c)
        .select_related("reviewed_by")
        .order_by("-created_at", "-id")[:20]
    )
    reward_eligible, eligibility_reason = ugc_reward_eligibility(
        c,
        assignments=assignment_rows,
    )
    for assignment_payload, assignment in zip(assignment_payloads, assignment_rows):
        order_delivered = nova_poshta_order_fulfillment_confirmed(assignment.order)
        assignment_payload["reward_eligible"] = bool(
            reward_eligible and order_delivered
        )
        assignment_payload["eligibility_reason"] = (
            eligibility_reason
            if not reward_eligible
            else (
                "delivered_order_eligible"
                if order_delivered
                else "order_not_delivered"
            )
        )
    ugc_assessment_payloads = [
        {
            "id": row.pk,
            "source_message_id": row.source_message_id,
            "provider_object_key": row.provider_object_key,
            "provider_media_id": row.provider_media_id,
            "target_username": row.target_username,
            "decision": row.decision,
            "decision_source": row.decision_source,
            "policy_version": row.policy_version,
            "reason_codes": list(row.reason_codes or [])[:20],
            "catalog_candidates": list(row.catalog_candidates or [])[:8],
            "confidence": str(row.confidence),
            "people_count": row.people_count,
            "garment_count": row.garment_count,
            "generation": row.generation,
            "reviewed_by": (
                row.reviewed_by.get_full_name() or row.reviewed_by.get_username()
                if row.reviewed_by else ""
            ),
            "created_at": row.created_at.isoformat(),
            "review_url": reverse(
                "management_bot_client_ugc_assessment_review_api",
                args=[c.pk, row.pk],
            ),
        }
        for row in ugc_assessment_rows
    ]
    manual_order_url = _manual_order_url_for_client(c.pk)
    card = _client_card(c)
    card.update({
        "memory": c.memory_summary,
        "phone": c.phone,
        "ad_source": c.ad_source,
        "ad_id": c.ad_id,
        "first_contact_at": c.first_contact_at.isoformat() if c.first_contact_at else "",
        "sales_context": c.sales_context,
        "hidden": bool(c.hidden_at),
        "hidden_reason": c.hidden_reason,
    })
    automation_owner = "manager" if c.manager_takeover or c.bot_paused else "bot"
    latest_projection = c.payment_projections.select_related("deal").order_by(
        "-updated_at", "-id",
    ).first()
    latest_manager_decision = IgPaymentReviewDecision.objects.filter(
        client=c,
    ).select_related("actor").order_by("-id").first()
    provider_truth = (
        latest_projection.truth
        if latest_projection
        else card.get("payment_truth", IgDeal.PaymentTruth.UNVERIFIED)
    )
    manager_truth = latest_manager_decision.decision if latest_manager_decision else ""
    payment_workspace = {
        "provider_truth": provider_truth,
        "provider_source": "provider_projection" if latest_projection else "client_payment_summary",
        "provider_deal_id": latest_projection.deal_id if latest_projection else None,
        "manager_truth": manager_truth,
        "verification_source": (
            latest_manager_decision.verification_source
            if latest_manager_decision
            else ""
        ),
        "verification_scope": (
            latest_manager_decision.verification_scope
            if latest_manager_decision
            else ""
        ),
        "manager_decision": _payment_review_decision_payload(latest_manager_decision),
        "authoritative_for_fulfillment": bool(
            manager_truth == "manager_verified"
            or provider_truth in {
                IgDeal.PaymentTruth.CONFIRMED,
                IgDeal.PaymentTruth.PARTIALLY_REFUNDED,
            }
        ),
    }
    current_deal = next(
        (deal for deal in deal_rows if deal.status != IgDeal.Status.CANCELLED),
        deal_rows[0] if deal_rows else None,
    )
    fulfillment_workspace = {
        "deal_id": current_deal.pk if current_deal else None,
        "delivery_status": current_deal.delivery_status if current_deal else "",
        "delivery_status_label": (
            current_deal.get_delivery_status_display()
            if current_deal and current_deal.delivery_status
            else ""
        ),
        "delivery_error": current_deal.delivery_error if current_deal else "",
        "has_validated_refs": bool(
            current_deal
            and current_deal.np_settlement_ref
            and current_deal.np_city_ref
            and current_deal.np_warehouse_ref
        ),
        "current_episode_source": "latest_non_cancelled_deal" if current_deal else "none",
    }
    episodes = client_episode_payload(c)
    physical_order_count = episodes["physical_order_count"]
    from management.services.ig_analysis_materiality import (
        current_analysis_snapshot,
    )

    potential = _client_potential_payload(
        c,
        current_analysis_snapshot(c),
        latest_message_id=last_message_id,
    )
    return JsonResponse({
        "success": True,
        "client": card,
        "messages": messages,
        "last_message_id": last_message_id,
        "oldest_message_id": oldest_message_id,
        "newest_message_id": newest_message_id,
        "has_older": has_older,
        "events": events,
        "signals": signals,
        "signal_event_count": len(signal_rows),
        "followups": followups,
        "deals": deals,
        "funnel": _funnel_progress_for_stage(c, card["stage"]),
        "journey": journey,
        "automation": {
            "owner": automation_owner,
            "bot_paused": c.bot_paused,
            "paused_reason": c.paused_reason,
            "paused_at": c.paused_at.isoformat() if c.paused_at else "",
            "manager_takeover": c.manager_takeover,
            "opted_out": card.get("opted_out", False),
            "opted_out_at": card.get("opted_out_at", ""),
            "reply_permission_epoch": c.reply_permission_epoch,
        },
        "interaction": {
            "stage": card["stage"],
            "stage_raw": card["stage_raw"],
            "stage_label": card["stage_label"],
            "intent": card["intent"],
            "intent_label": card["intent_label"],
            "buying_readiness": card["buying_readiness"],
            "interaction_type": card["interaction_type"],
            "interaction_type_label": card["interaction_type_label"],
            "analysis_band": card["analysis_band"],
            "analysis_band_label": card["analysis_band_label"],
            "analysis_confidence": card["analysis_confidence"],
            "analysis_evidence": (
                card["analysis_evidence"][:20]
                if isinstance(card["analysis_evidence"], list)
                else []
            ),
            "analysis_uncertainties": (
                card["analysis_uncertainties"][:20]
                if isinstance(card["analysis_uncertainties"], list)
                else []
            ),
            "last_manager_message_at": (
                c.last_manager_message_at.isoformat()
                if c.last_manager_message_at
                else ""
            ),
        },
        "potential": potential,
        "commercial_episodes": episodes,
        "payment": payment_workspace,
        "fulfillment": fulfillment_workspace,
        "review": {
            "pending_count": review_counts["pending"],
            "confirmed_count": review_counts["confirmed"],
            "rejected_count": review_counts["rejected"],
            "order_resolution_count": review_counts["order_resolution"],
            "total_count": review_counts["total"],
            "active": active_review,
            "history": review_payloads,
            "queue_url": reverse("management_bot_orders_workspace_api"),
        },
        "orders": {
            "count": physical_order_count,
            "physical_count": physical_order_count,
            "review_count": review_counts["total"],
            "attribution_count": attribution_total,
            "items": order_cards,
            "assignments": assignment_payloads,
            "assignment_count": len(assignment_payloads),
            "assignment_history": assignment_event_payloads,
            "manual_order_url": manual_order_url,
            "queue_url": _orders_workspace_url(view="all", client_id=c.pk),
        },
        "ugc_rewards": {
            "items": [reward_payload(row) for row in ugc_reward_rows],
            "award_url": reverse("management_bot_client_ugc_reward_api", args=[c.pk]),
            "assessments": ugc_assessment_payloads,
            "reward_eligible": reward_eligible,
            "eligibility_reason": eligibility_reason,
        },
        "post_sale": _post_sale_workspace_payload(c),
        "patterns": {
            "source": "episode_message_roles",
            "episode_id": pattern_episode.pk if pattern_episode else None,
            "message_counts": role_counts,
            "evidence_message_ids": list(reversed(pattern_evidence_ids)),
            "event_count": len(signal_rows),
            "groups": signals,
            "bounded": True,
        },
})


@login_required(login_url="management_login")
@require_POST
def bot_client_ugc_reward_api(request, client_id):
    blocked = _require_bot_capabilities(
        request,
        MANAGE_IG_PAYMENTS_PERMISSION,
        VIEW_IG_CONVERSATION_PII_PERMISSION,
    )
    if blocked:
        return blocked

    from .models import IgClient
    from management.services.ig_ugc_rewards import (
        UgcRewardConflict,
        award_ugc_reward,
        reward_payload,
        ugc_reward_eligibility,
    )
    from orders.models import Order

    client = IgClient.objects.filter(pk=client_id).first()
    if client is None:
        return JsonResponse({"success": False, "error": "Клієнта не знайдено."}, status=404)
    try:
        order_id = int(request.POST.get("order_id") or 0)
    except (TypeError, ValueError):
        order_id = 0
    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        return JsonResponse({"success": False, "error": "Замовлення не знайдено."}, status=404)

    try:
        reward, created = award_ugc_reward(
            client=client,
            order=order,
            actor=request.user,
            evidence_message_id=request.POST.get("evidence_message_id"),
            evidence_url=request.POST.get("evidence_url", ""),
            review_note=request.POST.get("review_note", ""),
        )
    except UgcRewardConflict as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    reward_eligible, eligibility_reason = ugc_reward_eligibility(client)
    return JsonResponse({
        "success": True,
        "created": created,
        "reward": reward_payload(reward),
        "reward_eligible": reward_eligible,
        "eligibility_reason": eligibility_reason,
    })


@login_required(login_url="management_login")
@require_POST
def bot_client_ugc_assessment_review_api(request, client_id, assessment_id):
    """Approve/reject one generation-bound external UGC assessment."""
    blocked = _require_bot_capabilities(
        request,
        MANAGE_IG_PAYMENTS_PERMISSION,
        VIEW_IG_CONVERSATION_PII_PERMISSION,
    )
    if blocked:
        return blocked
    from .ig_bot_models import IgUgcEvidenceAssessment
    from .models import IgClient
    from management.services.ig_ugc_rewards import (
        UgcRewardConflict,
        award_external_ugc_reward,
        queue_external_ugc_reward_delivery,
        reward_payload,
        ugc_reward_eligibility,
    )

    client = IgClient.objects.filter(pk=client_id).first()
    if client is None:
        return JsonResponse({"success": False, "error": "Клієнта не знайдено."}, status=404)
    try:
        expected_generation = int(request.POST.get("generation") or 0)
    except (TypeError, ValueError):
        expected_generation = 0
    decision = str(request.POST.get("decision") or "").strip().lower()
    note = str(request.POST.get("note") or "").strip()[:1000]
    try:
        with transaction.atomic():
            assessment = (
                IgUgcEvidenceAssessment.objects.select_for_update()
                .filter(pk=assessment_id, client_id=client.pk)
                .first()
            )
            if assessment is None:
                return JsonResponse({"success": False, "error": "Оцінку не знайдено."}, status=404)
            if expected_generation != assessment.generation:
                return JsonResponse({"success": False, "error": "Оцінка вже змінилась."}, status=409)
            # Review decisions are terminal.  An approved assessment may be
            # replayed idempotently to recover its existing reward/outbox, but
            # a rejected assessment must never be promoted later.
            if assessment.decision == IgUgcEvidenceAssessment.Decision.REJECTED:
                return JsonResponse({
                    "success": False,
                    "error": "Оцінка вже відхилена й не може бути змінена.",
                }, status=409)
            if (
                assessment.decision == IgUgcEvidenceAssessment.Decision.MANAGER_APPROVED
                and decision != "approve"
            ):
                return JsonResponse({
                    "success": False,
                    "error": "Оцінка вже підтверджена й не може бути змінена.",
                }, status=409)
            if decision == "reject":
                assessment.decision = IgUgcEvidenceAssessment.Decision.REJECTED
                assessment.decision_source = "manager"
                assessment.reviewed_by = request.user
                assessment.reviewed_at = timezone.now()
                assessment.reason_codes = ["manager_rejected", note[:64]] if note else ["manager_rejected"]
                assessment.generation += 1
                assessment.save(update_fields=[
                    "decision", "decision_source", "reviewed_by", "reviewed_at",
                    "reason_codes", "generation", "updated_at",
                ])
                return JsonResponse({
                    "success": True,
                    "decision": assessment.decision,
                    "generation": assessment.generation,
                })
            if decision != "approve":
                return JsonResponse({"success": False, "error": "Невідоме рішення."}, status=400)
            if not note:
                return JsonResponse({
                    "success": False,
                    "error": "Додайте причину підтвердження UGC.",
                }, status=400)
            if assessment.decision == IgUgcEvidenceAssessment.Decision.MANAGER_APPROVED:
                # A successful approval is terminal.  Replaying the same
                # generation recovers the exact existing reward and outbox;
                # the reward service never mints a second promo.
                reward, created = award_external_ugc_reward(
                    client=client,
                    assessment=assessment,
                    actor=request.user,
                    review_note=note,
                )
            else:
                # A manager may resolve uncertainty, but cannot manufacture
                # provider ownership or a brand target from a plain URL/OCR
                # claim.  The reward service revalidates the original webhook
                # and owned media again under lock before issuing the code.
                if (
                    not assessment.provider_object_key
                    or not assessment.evidence_fingerprint
                    or assessment.target_username.casefold().lstrip("@").strip() != "twocomms"
                ):
                    return JsonResponse({"success": False, "error": "Недостатня provider provenance."}, status=400)
                assessment.decision = IgUgcEvidenceAssessment.Decision.MANAGER_APPROVED
                assessment.decision_source = "manager"
                assessment.reviewed_by = request.user
                assessment.reviewed_at = timezone.now()
                assessment.generation += 1
                assessment.save(update_fields=[
                    "decision", "decision_source", "reviewed_by", "reviewed_at",
                    "generation", "updated_at",
                ])
                reward, created = award_external_ugc_reward(
                    client=client,
                    assessment=assessment,
                    actor=request.user,
                    review_note=note,
                )
            delivery = queue_external_ugc_reward_delivery(reward)
    except UgcRewardConflict as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)
    reward_eligible, eligibility_reason = ugc_reward_eligibility(client)
    return JsonResponse({
        "success": True,
        "created": created,
        "reward": reward_payload(reward),
        "reward_eligible": reward_eligible,
        "eligibility_reason": eligibility_reason,
        "delivery": {
            "id": delivery.pk,
            "state": delivery.state,
            "message_snapshot": delivery.message_snapshot,
        },
    })


@login_required(login_url="management_login")
@require_POST
def bot_post_sale_case_api(request, client_id, case_id):
    blocked = _require_bot_write_json(request)
    if blocked:
        return blocked
    from .ig_bot_models import IgCommercialEpisode, IgOrderAttribution, IgPostSaleCase

    with transaction.atomic():
        case = (
            IgPostSaleCase.objects.select_for_update()
            .select_related("client", "order")
            .filter(pk=case_id, client_id=client_id)
            .first()
        )
        if not case:
            return JsonResponse({"success": False, "error": "Звернення не знайдено."}, status=404)

        order_value = str(request.POST.get("order_id") or "").strip()
        if order_value:
            try:
                order_id = int(order_value)
            except (TypeError, ValueError):
                return JsonResponse({"success": False, "error": "Некоректне замовлення."}, status=400)
            attribution = (
                IgOrderAttribution.objects.select_related("order")
                .filter(client_id=client_id, order_id=order_id)
                .first()
            )
            if not attribution:
                return JsonResponse({
                    "success": False,
                    "error": "Це замовлення не прив’язане до Instagram-клієнта.",
                }, status=400)
            case.order = attribution.order
            case.commercial_episode = (
                IgCommercialEpisode.objects.filter(order_attribution=attribution).first()
                or IgCommercialEpisode.objects.filter(intended_order_id=order_id).first()
            )

        case_type = str(request.POST.get("case_type") or case.case_type).strip()
        valid_types = {value for value, _label in IgPostSaleCase.CaseType.choices}
        if case_type not in valid_types:
            return JsonResponse({"success": False, "error": "Некоректний тип звернення."}, status=400)
        status = str(request.POST.get("status") or case.status).strip()
        valid_statuses = {value for value, _label in IgPostSaleCase.Status.choices}
        if status not in valid_statuses:
            return JsonResponse({"success": False, "error": "Некоректний статус звернення."}, status=400)
        if status != IgPostSaleCase.Status.NEEDS_DETAILS and not case.order_id:
            return JsonResponse({
                "success": False,
                "error": "Спочатку оберіть замовлення цього клієнта.",
            }, status=400)

        case.case_type = case_type
        case.status = status
        for field, limit in (
            ("source_item_title", 255), ("source_fit", 64),
            ("source_size", 32), ("requested_fit", 64),
            ("requested_size", 32), ("manager_note", 4000),
        ):
            if field in request.POST:
                setattr(case, field, str(request.POST.get(field) or "").strip()[:limit])
        terminal = {
            IgPostSaleCase.Status.COMPLETED, IgPostSaleCase.Status.REJECTED,
            IgPostSaleCase.Status.CANCELLED,
        }
        case.resolved_at = timezone.now() if status in terminal else None
        case.save()

        # ТТН обратной посылки приходит текстом от клиента, поэтому её нельзя
        # вывести из состояния заказа — менеджер подтверждает её одним полем.
        return_tracking = str(request.POST.get("return_tracking_number") or "").strip()
        if return_tracking:
            from management.services.ig_post_sale import record_return_shipment

            evidence_raw = str(
                request.POST.get("return_tracking_message_id") or ""
            ).strip()
            try:
                evidence_message_id = int(evidence_raw) if evidence_raw else None
            except (TypeError, ValueError):
                evidence_message_id = None
            try:
                record_return_shipment(
                    case,
                    return_tracking,
                    actor=request.user,
                    evidence_message_id=evidence_message_id,
                )
            except ValueError as exc:
                return JsonResponse(
                    {"success": False, "error": str(exc)}, status=400
                )

        # ТТН замены, отправленной вручную. Автовывод ноги обмена работает,
        # только пока кейс открыт, а реальный обмен часто закрывают раньше,
        # чем кто-то фиксирует номер.
        replacement_tracking = str(
            request.POST.get("replacement_tracking_number") or ""
        ).strip()
        if replacement_tracking:
            from management.services.ig_post_sale import record_replacement_shipment

            try:
                record_replacement_shipment(
                    case, replacement_tracking, actor=request.user
                )
            except ValueError as exc:
                return JsonResponse(
                    {"success": False, "error": str(exc)}, status=400
                )

    return JsonResponse({
        "success": True,
        "case": _post_sale_case_payload(case),
        "post_sale": _post_sale_workspace_payload(case.client),
    })


@login_required(login_url="management_login")
@require_POST
def bot_client_pause_api(request, client_id):
    """Зупинити бота для клієнта (менеджер бере діалог на себе)."""
    blocked = _require_bot_write_json(request)
    if blocked:
        return blocked
    from .models import IgClient, IgPermissionTransitionJob, InstagramBotSettings
    from .services.ig_permission_transitions import (
        ACTIVE_STATUSES,
        attempt_permission_transition,
        create_permission_transition,
    )

    c = IgClient.objects.filter(id=client_id).first()
    if not c:
        return JsonResponse({"success": False, "error": "Клієнта не знайдено."}, status=404)
    job = create_permission_transition(
        kind=IgPermissionTransitionJob.Kind.CLIENT_PAUSE,
        dedupe_key=(
            f"permission:client_pause:client:{c.pk}:"
            f"epoch:{int(c.reply_permission_epoch or 0)}"
        ),
        client=c,
        settings=InstagramBotSettings.load(),
    )
    attempt_permission_transition(job.pk)
    job.refresh_from_db()
    c.refresh_from_db()
    pause_pending = job.status in ACTIVE_STATUSES
    return JsonResponse({
        "success": True,
        "bot_paused": bool(c.bot_paused or pause_pending),
        "pause_pending": pause_pending,
    })


@login_required(login_url="management_login")
@require_POST
def bot_client_resume_api(request, client_id):
    """Explicitly return one eligible client from manager ownership to the bot."""
    blocked = _require_bot_write_json(request)
    if blocked:
        return blocked
    from .services.ig_manual_resume import (
        ManualResumeRejected,
        manual_resume_message,
        resume_client_automation,
    )

    try:
        result = resume_client_automation(client_id, actor=request.user)
    except ManualResumeRejected as exc:
        return JsonResponse({
            "success": False,
            "error": exc.message,
            "code": exc.code,
            "requires_verified_reconsent": exc.code == "active_opt_out",
        }, status=exc.status)
    return JsonResponse({
        "success": True,
        "bot_paused": False,
        "changed": result.changed,
        "permission_epoch": result.permission_epoch,
        "successor_created": result.successor_created,
        "successor_revision_id": result.successor_revision_id,
        "successor_turn_id": result.successor_turn_id,
        "successor_source_message_id": result.successor_source_message_id,
        "unresolved_turn_id": result.unresolved_turn_id,
        "unresolved_source_message_id": result.unresolved_source_message_id,
        "successor_reason": result.successor_reason,
        "message": manual_resume_message(result),
    })


@login_required(login_url="management_login")
@require_POST
def bot_client_hide_api(request, client_id):
    blocked = _require_bot_write_json(request)
    if blocked:
        return blocked
    from django.utils import timezone

    from .models import IgClient, IgPollCursor, InstagramBotMessage
    from .services import bot_followups

    with transaction.atomic():
        c = IgClient.objects.select_for_update().filter(id=client_id).first()
        if not c:
            return JsonResponse({"success": False, "error": "Клієнта не знайдено."}, status=404)
        now = timezone.now()
        if bot.client_automation_busy(c, now=now):
            return JsonResponse({
                "success": False,
                "retryable": True,
                "error": "Бот завершує поточну відповідь. Зачекайте кілька секунд і повторіть приховування.",
            }, status=409)
        # Прострочена lease не є активною автоматизацією і не повинна заважати
        # модерації після аварійного завершення worker-а.
        c.automation_lease_token = ""
        c.automation_lease_until = None
        c.hidden_at = now
        c.reply_permission_epoch = int(c.reply_permission_epoch or 0) + 1
        c.hidden_reason = (request.POST.get("reason") or "manual")[:255]
        c.save(update_fields=[
            "automation_lease_token", "automation_lease_until",
            "hidden_at", "reply_permission_epoch", "hidden_reason", "updated_at",
        ])
        IgPollCursor.objects.filter(participant_igsid=c.igsid).update(
            excluded_at=now,
            excluded_reason="client_hidden",
            next_attempt_at=None,
            updated_at=now,
        )
        cancelled_followups = bot_followups.cancel_pending(c, reason="hidden")
        # Не залишаємо legacy pending rows, які могли потрапити в чергу до
        # натискання Hide: після успішного Hide вони не мають чекати worker-а.
        cancelled_messages = InstagramBotMessage.objects.filter(
            client=c,
            role=InstagramBotMessage.Role.USER,
            status__in=[
                InstagramBotMessage.Status.PENDING,
                InstagramBotMessage.Status.PROCESSING,
            ],
        ).update(
            status=InstagramBotMessage.Status.DONE,
            processed_at=now,
            processing_started_at=None,
        )
    return JsonResponse({
        "success": True,
        "hidden": True,
        "automation_disabled": True,
        "cancelled_followups": cancelled_followups,
        "cancelled_messages": cancelled_messages,
        "message": "Клієнта приховано: бот не оброблятиме його повідомлення, а статистика не враховуватиме цей діалог.",
    })


@login_required(login_url="management_login")
@require_POST
def bot_client_unhide_api(request, client_id):
    blocked = _require_bot_write_json(request)
    if blocked:
        return blocked
    from .models import IgClient, IgPollCursor

    with transaction.atomic():
        c = IgClient.objects.select_for_update().filter(id=client_id).first()
        if not c:
            return JsonResponse({"success": False, "error": "Клієнта не знайдено."}, status=404)
        c.hidden_at = None
        c.reply_permission_epoch = int(c.reply_permission_epoch or 0) + 1
        c.hidden_reason = ""
        c.save(update_fields=[
            "hidden_at", "reply_permission_epoch", "hidden_reason", "updated_at",
        ])
        IgPollCursor.objects.filter(
            participant_igsid=c.igsid,
            excluded_reason="client_hidden",
        ).update(
            excluded_at=None,
            excluded_reason="",
            synced_provider_updated_at=None,
            next_attempt_at=None,
            failure_count=0,
            last_error="",
            updated_at=timezone.now(),
        )
    return JsonResponse({
        "success": True,
        "hidden": False,
        "message": "Клієнта повернено до активного списку.",
    })


@login_required(login_url="management_login")
@require_POST
def bot_client_mark_lost_api(request, client_id):
    blocked = _require_bot_write_json(request)
    if blocked:
        return blocked
    from .models import IgClient
    from .services import bot_followups

    c = IgClient.objects.filter(id=client_id).first()
    if not c:
        return JsonResponse({"success": False, "error": "Клієнта не знайдено."}, status=404)
    c.lost_reason = (request.POST.get("reason") or "manual_lost")[:64]
    c.primary_objection = IgClient.Objection.NO_BUY
    c.set_stage(IgClient.Stage.COLD, reason=c.lost_reason)
    c.save(update_fields=["lost_reason", "primary_objection", "updated_at"])
    bot_followups.cancel_pending(c, reason="lost")
    return JsonResponse({"success": True, "stage": c.stage, "lost_reason": c.lost_reason})


@login_required(login_url="management_login")
@require_POST
def bot_client_reset_funnel_api(request, client_id):
    """Reset mutable CRM inference only after an explicit operator confirm."""
    blocked = _require_bot_write_json(request)
    if blocked:
        return blocked
    if str(request.POST.get("confirm_reset") or "").lower() not in {"1", "true", "yes"}:
        return JsonResponse({
            "success": False,
            "requires_confirmation": True,
            "error": "Підтвердіть скидання воронки для цього клієнта.",
        }, status=409)
    from .services.ig_funnel_reset import reset_funnel

    result = reset_funnel(
        client_id=client_id,
        actor=request.user,
        reason=request.POST.get("reason") or "manual_reset",
    )
    if not result.get("ok"):
        return JsonResponse({
            "success": False,
            "error": result.get("error") or "Не вдалося скинути воронку.",
        }, status=int(result.get("status") or 400))
    return JsonResponse({
        "success": True,
        "message": "Воронку скинуто. Переписка, оплати та замовлення збережені.",
        **{key: value for key, value in result.items() if key not in {"ok", "status"}},
    })


@login_required(login_url="management_login")
@require_GET
def bot_stats_api(request):
    blocked = _require_bot_capabilities(
        request, VIEW_IG_CONVERSATION_PII_PERMISSION
    )
    if blocked:
        return blocked
    query_count_start = (
        len(connection.queries)
        if (settings.DEBUG or connection.force_debug_cursor)
        else None
    )
    from datetime import date, datetime, time, timedelta, timezone as datetime_timezone

    from .models import (
        IgClient,
        IgConversationSignal,
        IgDeal,
        IgDealItem,
        IgFunnelDropOff,
        IgFunnelStepEvent,
        IgFollowUpTask,
        InstagramBotMessage,
    )

    def metric_contract(
        *,
        unit,
        basis,
        time_field,
        population,
        numerator="",
        denominator="",
        completeness="complete",
        source_kind,
        available=True,
    ):
        return {
            "unit": unit,
            "basis": basis,
            "time_field": time_field,
            "population": population,
            "numerator": numerator,
            "denominator": denominator,
            "completeness": completeness,
            "source_kind": source_kind,
            "available": bool(available),
        }

    date_from_raw = (request.GET.get("date_from") or "").strip()
    date_to_raw = (request.GET.get("date_to") or "").strip()
    range_mode = "all"
    range_days = 0
    since = until = None
    if bool(date_from_raw) != bool(date_to_raw):
        return JsonResponse(
            {"success": False, "error": "Потрібно вказати обидві межі діапазону."},
            status=400,
        )
    if date_from_raw and date_to_raw:
        try:
            date_from = date.fromisoformat(date_from_raw)
            date_to = date.fromisoformat(date_to_raw)
        except ValueError:
            return JsonResponse(
                {"success": False, "error": "Дата має бути у форматі YYYY-MM-DD."},
                status=400,
            )
        if date_from > date_to:
            return JsonResponse(
                {"success": False, "error": "Початкова дата не може бути пізнішою за кінцеву."},
                status=400,
            )
        local_tz = timezone.get_current_timezone()
        since = timezone.make_aware(datetime.combine(date_from, time.min), local_tz)
        until = timezone.make_aware(
            datetime.combine(date_to + timedelta(days=1), time.min), local_tz
        )
        range_mode = "custom"
    else:
        try:
            range_days = int(request.GET.get("days") or 0)
        except (TypeError, ValueError):
            range_days = 0
        if range_days not in {0, 1, 7, 30}:
            range_days = 0
        if range_days:
            local_now = timezone.localtime()
            local_tomorrow = (local_now + timedelta(days=1)).date()
            local_start_date = local_now.date() - timedelta(days=range_days - 1)
            local_tz = timezone.get_current_timezone()
            since = timezone.make_aware(
                datetime.combine(local_start_date, time.min), local_tz
            )
            until = timezone.make_aware(
                datetime.combine(local_tomorrow, time.min), local_tz
            )
            range_mode = "preset"

    local_tz = timezone.get_current_timezone()
    period_date_from = date_from_raw or (since.astimezone(local_tz).date().isoformat() if since else "")
    period_date_to = date_to_raw or (
        (until.astimezone(local_tz) - timedelta(days=1)).date().isoformat()
        if until
        else ""
    )
    if range_mode == "custom":
        period_label = f"{date.fromisoformat(period_date_from).strftime('%d.%m.%Y')} — {date.fromisoformat(period_date_to).strftime('%d.%m.%Y')}"
    elif range_days == 1:
        period_label = "Сьогодні"
    elif range_days:
        period_label = f"Останні {range_days} днів"
    else:
        period_label = "Увесь час"

    visible_messages = InstagramBotMessage.objects.filter(
        client__hidden_at__isnull=True
    ).annotate(
        stats_occurred_at=Coalesce("provider_created_at", "created_at")
    )
    if since:
        visible_messages = visible_messages.filter(stats_occurred_at__gte=since)
    if until:
        visible_messages = visible_messages.filter(stats_occurred_at__lt=until)
    message_totals = visible_messages.aggregate(
        messages=Count("id"),
        inbound_messages=Count(
            "id", filter=Q(role=InstagramBotMessage.Role.USER)
        ),
        bot_replies=Count(
            "id", filter=Q(role=InstagramBotMessage.Role.MODEL)
        ),
        manager_messages=Count(
            "id", filter=Q(role=InstagramBotMessage.Role.MANAGER)
        ),
        unique_conversations=Count("sender_id", distinct=True),
    )

    materialized_message_rows = 0

    def build_message_series(message_queryset, *, expected_total):
        nonlocal materialized_message_rows
        # Bucket in application time. MariaDB's CONVERT_TZ returns NULL when
        # timezone tables are unavailable, which previously hid real messages.
        source_rows = list(
            message_queryset.values_list(
                "provider_created_at",
                "created_at",
                "role",
            ).iterator(chunk_size=2000)
        )
        materialized_message_rows = max(
            materialized_message_rows,
            len(source_rows),
        )
        occurred_rows = []
        for provider_created_at, created_at, role in source_rows:
            occurred_at = provider_created_at or created_at
            if occurred_at is None:
                continue
            if timezone.is_naive(occurred_at):
                occurred_at = timezone.make_aware(
                    occurred_at,
                    datetime_timezone.utc,
                )
            occurred_rows.append((occurred_at, role))

        first = min((row[0] for row in occurred_rows), default=None)
        last = max((row[0] for row in occurred_rows), default=None)
        series_start = since or first
        series_until = until or (
            last + timedelta(microseconds=1) if last else None
        )
        if not series_start or not series_until:
            return {
                "granularity": "day",
                "density": "single",
                "has_data": False,
                "max_total": 0,
                "series_total": 0,
                "expected_total": int(expected_total or 0),
                "reconciled": int(expected_total or 0) == 0,
                "hourly_items": [],
                "hourly_reconciled": int(expected_total or 0) == 0,
                "items": [],
            }
        local_start = series_start.astimezone(local_tz).date()
        local_until = series_until.astimezone(local_tz).date()
        if series_until.astimezone(local_tz).time() != time.min:
            local_until += timedelta(days=1)
        span_days = max(1, (local_until - local_start).days)
        if span_days <= 31:
            granularity = "day"
        elif span_days <= 180:
            granularity = "week"
        else:
            granularity = "month"

        if span_days == 1:
            density = "single"
        elif span_days <= 7:
            density = "compact"
        elif span_days <= 31:
            density = "daily"
        elif span_days <= 180:
            density = "weekly"
        else:
            density = "monthly"

        def align_bucket(value):
            if granularity == "week":
                return value - timedelta(days=value.weekday())
            if granularity == "month":
                return value.replace(day=1)
            return value

        def next_bucket(value):
            if granularity == "week":
                return value + timedelta(days=7)
            if granularity == "month":
                if value.month == 12:
                    return value.replace(year=value.year + 1, month=1, day=1)
                return value.replace(month=value.month + 1, day=1)
            return value + timedelta(days=1)

        rows_by_bucket = {}
        for occurred_at, role in occurred_rows:
            bucket = align_bucket(occurred_at.astimezone(local_tz).date())
            row = rows_by_bucket.setdefault(
                bucket,
                {
                    "messages": 0,
                    "inbound_messages": 0,
                    "bot_replies": 0,
                    "manager_messages": 0,
                },
            )
            row["messages"] += 1
            if role == InstagramBotMessage.Role.USER:
                row["inbound_messages"] += 1
            elif role == InstagramBotMessage.Role.MODEL:
                row["bot_replies"] += 1
            elif role == InstagramBotMessage.Role.MANAGER:
                row["manager_messages"] += 1
        hourly_items = []
        hourly_reconciled = True
        if span_days == 1:
            hourly_start = timezone.make_aware(
                datetime.combine(local_start, time.min),
                local_tz,
            )
            hourly_rows = {}
            for occurred_at, role in occurred_rows:
                local_value = occurred_at.astimezone(local_tz)
                if local_value.date() != local_start:
                    continue
                bucket = hourly_rows.setdefault(
                    local_value.hour,
                    {
                        "messages": 0,
                        "inbound_messages": 0,
                        "bot_replies": 0,
                        "manager_messages": 0,
                    },
                )
                bucket["messages"] += 1
                if role == InstagramBotMessage.Role.USER:
                    bucket["inbound_messages"] += 1
                elif role == InstagramBotMessage.Role.MODEL:
                    bucket["bot_replies"] += 1
                elif role == InstagramBotMessage.Role.MANAGER:
                    bucket["manager_messages"] += 1
            for offset in range(24):
                bucket_at = hourly_start + timedelta(hours=offset)
                row = hourly_rows.get(offset, {})
                hourly_items.append({
                    "bucket": bucket_at.isoformat(),
                    "messages": int(row.get("messages") or 0),
                    "inbound_messages": int(row.get("inbound_messages") or 0),
                    "bot_replies": int(row.get("bot_replies") or 0),
                    "manager_messages": int(row.get("manager_messages") or 0),
                })
            hourly_reconciled = (
                sum(item["messages"] for item in hourly_items)
                == int(expected_total or 0)
            )
        cursor = align_bucket(local_start)
        end_cursor = align_bucket(local_until)
        if end_cursor < local_until:
            end_cursor = next_bucket(end_cursor)
        items = []
        while cursor < end_cursor:
            row = rows_by_bucket.get(cursor, {})
            items.append({
                "bucket": cursor.isoformat(),
                "messages": int(row.get("messages") or 0),
                "inbound_messages": int(row.get("inbound_messages") or 0),
                "bot_replies": int(row.get("bot_replies") or 0),
                "manager_messages": int(row.get("manager_messages") or 0),
            })
            cursor = next_bucket(cursor)
        series_total = sum(item["messages"] for item in items)
        expected_total = int(expected_total or 0)
        return {
            "granularity": granularity,
            "density": density,
            "has_data": series_total > 0,
            "max_total": max((item["messages"] for item in items), default=0),
            "series_total": series_total,
            "expected_total": expected_total,
            "reconciled": series_total == expected_total,
            "hourly_items": hourly_items,
            "hourly_reconciled": hourly_reconciled,
            "items": items,
        }

    message_series = build_message_series(
        visible_messages,
        expected_total=message_totals["messages"],
    )
    ad_client_identity = ~(Q(ad_id="") & Q(ad_ref="") & Q(ad_title=""))
    ad_message_identity = ~(
        Q(client__ad_id="") & Q(client__ad_ref="") & Q(client__ad_title="")
    )
    ad_client_ids = list(
        IgClient.objects.filter(
            hidden_at__isnull=True,
        ).filter(ad_client_identity).values_list("id", flat=True)
    )
    ad_messages = visible_messages.filter(
        client_id__in=ad_client_ids,
    ).filter(ad_message_identity)
    ad_message_totals = ad_messages.aggregate(
        messages=Count("id"),
        inbound_messages=Count(
            "id", filter=Q(role=InstagramBotMessage.Role.USER)
        ),
        bot_replies=Count(
            "id", filter=Q(role=InstagramBotMessage.Role.MODEL)
        ),
        manager_messages=Count(
            "id", filter=Q(role=InstagramBotMessage.Role.MANAGER)
        ),
        conversations=Count("sender_id", distinct=True),
    )
    ad_message_series = build_message_series(
        ad_messages,
        expected_total=ad_message_totals["messages"],
    )
    message_client_ids = visible_messages.exclude(client_id__isnull=True).values(
        "client_id"
    ).distinct()

    canonical_losses = IgFunnelDropOff.objects.filter(
        episode__client__hidden_at__isnull=True,
        recovered_at__isnull=True,
        kind__in=[
            IgFunnelDropOff.Kind.EXPLICIT_REFUSAL,
            IgFunnelDropOff.Kind.SILENCE,
        ],
    )
    if since:
        canonical_losses = canonical_losses.filter(occurred_at__gte=since)
    if until:
        canonical_losses = canonical_losses.filter(occurred_at__lt=until)
    lost_or_refused = canonical_losses.values(
        "episode__client_id"
    ).distinct().count()

    active_clients = _with_latest_interaction(annotate_verified_payment(
        IgClient.objects.filter(hidden_at__isnull=True)
    ))
    if since:
        active_clients = active_clients.filter(
            Q(last_message_at__gte=since)
            | Q(last_message_at__isnull=True, created_at__gte=since)
        )
    if until:
        active_clients = active_clients.filter(
            Q(last_message_at__lt=until)
            | Q(last_message_at__isnull=True, created_at__lt=until)
        )
    conversations = active_clients.count()
    from .ig_bot_models import IgConversationAnalysisSnapshot

    interaction_labels = dict(IgConversationAnalysisSnapshot.InteractionType.choices)
    interaction_counts = [
        {
            "type": row["latest_interaction_type"],
            "label": str(interaction_labels.get(row["latest_interaction_type"], "Не визначено")),
            "count": row["count"],
        }
        for row in active_clients.exclude(latest_interaction_type__isnull=True)
        .exclude(latest_interaction_type="")
        .values("latest_interaction_type")
        .annotate(count=Count("id"))
        .order_by("-count", "latest_interaction_type")
    ]
    stage_counts = {}
    for row in active_clients.values("stage", "has_verified_payment").annotate(
        count=Count("id")
    ).order_by():
        stage = row["stage"]
        if stage in {IgClient.Stage.PAID, IgClient.Stage.ORDER_CREATED, IgClient.Stage.DONE} and not row["has_verified_payment"]:
            stage = "unverified"
        stage_counts[stage] = stage_counts.get(stage, 0) + row["count"]
    signal_qs = IgConversationSignal.objects.filter(client__hidden_at__isnull=True)
    if since:
        signal_qs = signal_qs.filter(created_at__gte=since)
    if until:
        signal_qs = signal_qs.filter(created_at__lt=until)
    signals = {
        row["signal_type"]: row["count"]
        for row in signal_qs.values("signal_type").annotate(count=Count("id")).order_by()
    }
    objection_clients = {
        row["primary_objection"]: row["count"]
        for row in active_clients.exclude(primary_objection=IgClient.Objection.NONE)
        .values("primary_objection").annotate(count=Count("id")).order_by()
    }
    objection_signals = {
        key: value
        for key, value in signals.items()
        if "objection" in key or key in {"no_reply", "lost"}
    }
    # Keep signal names too; the frontend can show both high-level client state
    # and granular event breakdown.
    objections = dict(objection_clients)
    objections.update(objection_signals)
    payment_deals = IgDeal.objects.all()
    if since:
        payment_deals = payment_deals.filter(
            Q(payment_projection__paid_at__gte=since)
            | Q(payment_projection__isnull=True, paid_at__gte=since)
        )
    if until:
        payment_deals = payment_deals.filter(
            Q(payment_projection__paid_at__lt=until)
            | Q(payment_projection__isnull=True, paid_at__lt=until)
        )

    interest_rows = list(
        active_clients.exclude(current_product__isnull=True)
        .values("current_product_id", "current_product__title")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    paid_item_filter = verified_payment_q("deal__")
    if since:
        paid_item_filter &= (
            Q(deal__payment_projection__paid_at__gte=since)
            | Q(
                deal__payment_projection__isnull=True,
                deal__paid_at__gte=since,
            )
        )
    if until:
        paid_item_filter &= (
            Q(deal__payment_projection__paid_at__lt=until)
            | Q(
                deal__payment_projection__isnull=True,
                deal__paid_at__lt=until,
            )
        )
    paid_product_rows = list(
        IgDealItem.objects.filter(
            deal__client__hidden_at__isnull=True,
            product__isnull=False,
        )
        .filter(paid_item_filter)
        .values("product_id")
        .annotate(
            verified_paid_orders=Count("deal_id", distinct=True),
            verified_paid_qty=Sum("qty"),
        )
    )
    interest_by_product = {
        row["current_product_id"]: row for row in interest_rows
    }
    paid_by_product = {row["product_id"]: row for row in paid_product_rows}
    product_ids = set(interest_by_product) | set(paid_by_product)
    from storefront.models import Product

    products_by_id = Product.objects.in_bulk(product_ids)
    product_interest = []
    for product_id in product_ids:
        product = products_by_id.get(product_id)
        interest = interest_by_product.get(product_id, {})
        paid = paid_by_product.get(product_id, {})
        image_url = ""
        thumbnail_url = ""
        image_field = None
        if product:
            image_field = product.home_card_image or product.main_image
        if image_field:
            try:
                thumbnail_url = image_field.url
                image_url = request.build_absolute_uri(thumbnail_url)
            except (ValueError, OSError):
                thumbnail_url = ""
                image_url = ""
        image_alt = (
            getattr(product, "main_image_alt", "")
            or getattr(product, "title", "")
            or "Товар"
        )
        product_interest.append({
            "product_id": product_id,
            "product_title": (
                getattr(product, "title", "")
                or interest.get("current_product__title")
                or ""
            ),
            "count": int(interest.get("count") or 0),
            "interest_count": int(interest.get("count") or 0),
            "verified_paid_orders": int(paid.get("verified_paid_orders") or 0),
            "verified_paid_qty": int(paid.get("verified_paid_qty") or 0),
            "image_url": image_url,
            "image_alt": image_alt,
            "thumbnail_url": thumbnail_url,
            "thumbnail_alt": image_alt,
        })
    product_interest.sort(
        key=lambda row: (
            -row["interest_count"],
            -row["verified_paid_qty"],
            row["product_title"],
        )
    )
    product_rows_by_id = {
        row["product_id"]: row for row in product_interest
    }
    product_interest = product_interest[:25]
    active_clients = annotate_verified_payment(
        active_clients,
        alias="paid_in_range",
        deal_queryset=payment_deals,
    )
    funnel_clients = annotate_verified_payment(
        IgClient.objects.filter(
            hidden_at__isnull=True,
            pk__in=Subquery(message_client_ids),
        ),
        alias="paid_in_range",
        deal_queryset=payment_deals,
    )
    followup_payment_filter = verified_payment_q("client__deals__")
    if since:
        followup_payment_filter &= (
            Q(client__deals__payment_projection__paid_at__gte=since)
            | Q(client__deals__payment_projection__isnull=True, client__deals__paid_at__gte=since)
        )
    if until:
        followup_payment_filter &= (
            Q(client__deals__payment_projection__paid_at__lt=until)
            | Q(client__deals__payment_projection__isnull=True, client__deals__paid_at__lt=until)
        )
    hidden_clients = IgClient.objects.filter(hidden_at__isnull=False)
    pending_followups = IgFollowUpTask.objects.filter(
        status=IgFollowUpTask.Status.PENDING,
        client__hidden_at__isnull=True,
    )
    sent_followups = IgFollowUpTask.objects.filter(
        status=IgFollowUpTask.Status.SENT,
        client__hidden_at__isnull=True,
    )
    if since:
        hidden_clients = hidden_clients.filter(hidden_at__gte=since)
        pending_followups = pending_followups.filter(due_at__gte=since)
        sent_followups = sent_followups.filter(sent_at__gte=since)
    if until:
        hidden_clients = hidden_clients.filter(hidden_at__lt=until)
        pending_followups = pending_followups.filter(due_at__lt=until)
        sent_followups = sent_followups.filter(sent_at__lt=until)

    ad_active_clients = active_clients.filter(ad_client_identity)

    def campaign_identity(ad_id, ad_ref, ad_title):
        ad_id = str(ad_id or "").strip()
        ad_ref = str(ad_ref or "").strip()
        ad_title = str(ad_title or "").strip()
        if ad_id:
            return "ad_id", ad_id
        if ad_ref:
            return "ad_ref", ad_ref
        return "ad_title", ad_title

    campaign_buckets = {}

    def campaign_bucket(
        *,
        ad_id="",
        ad_ref="",
        ad_title="",
        ad_source="",
        ad_creative_url="",
    ):
        key_type, key_value = campaign_identity(ad_id, ad_ref, ad_title)
        if not key_value:
            return None
        bucket = campaign_buckets.setdefault(
            (key_type, key_value),
            {
                "attribution_key_type": key_type,
                "attribution_key": key_value,
                "ad_id": str(ad_id or ""),
                "ad_ref": str(ad_ref or ""),
                "ad_title": str(ad_title or ""),
                "ad_source": str(ad_source or ""),
                "ad_creative_url": str(ad_creative_url or ""),
                "chats": 0,
                "qualified": 0,
                "product_matched": 0,
                "paid": 0,
                "gross_revenue": Decimal("0"),
                "refunded_revenue": Decimal("0"),
                "revenue": Decimal("0"),
                "revenue_unpriced_payments": 0,
                "lost_or_refused": 0,
                "event_counts": {},
                "product_counts": {},
            },
        )
        for field, value in (
            ("ad_id", ad_id),
            ("ad_ref", ad_ref),
            ("ad_title", ad_title),
            ("ad_source", ad_source),
            ("ad_creative_url", ad_creative_url),
        ):
            if value and not bucket[field]:
                bucket[field] = str(value)
        return bucket

    ad_events = IgFunnelStepEvent.objects.filter(
        episode__client_id__in=ad_client_ids,
        episode__client__hidden_at__isnull=True,
    )
    if since:
        ad_events = ad_events.filter(occurred_at__gte=since)
    if until:
        ad_events = ad_events.filter(occurred_at__lt=until)
    for row in ad_events.values(
        "episode__client__ad_id",
        "episode__client__ad_ref",
        "episode__client__ad_title",
        "episode__client__ad_source",
        "episode__client__ad_creative_url",
        "event_type",
    ).annotate(episodes=Count("episode_id", distinct=True)).order_by():
        bucket = campaign_bucket(
            ad_id=row["episode__client__ad_id"],
            ad_ref=row["episode__client__ad_ref"],
            ad_title=row["episode__client__ad_title"],
            ad_source=row["episode__client__ad_source"],
            ad_creative_url=row["episode__client__ad_creative_url"],
        )
        if bucket:
            bucket["event_counts"][row["event_type"]] = (
                bucket["event_counts"].get(row["event_type"], 0)
                + int(row["episodes"] or 0)
            )

    ad_losses = canonical_losses.filter(episode__client_id__in=ad_client_ids)
    for row in ad_losses.values(
        "episode__client__ad_id",
        "episode__client__ad_ref",
        "episode__client__ad_title",
        "episode__client__ad_source",
        "episode__client__ad_creative_url",
    ).annotate(count=Count("episode__client_id", distinct=True)).order_by():
        bucket = campaign_bucket(
            ad_id=row["episode__client__ad_id"],
            ad_ref=row["episode__client__ad_ref"],
            ad_title=row["episode__client__ad_title"],
            ad_source=row["episode__client__ad_source"],
            ad_creative_url=row["episode__client__ad_creative_url"],
        )
        if bucket:
            bucket["lost_or_refused"] += int(row["count"] or 0)

    campaign_product_rows = ad_active_clients.exclude(
        current_product__isnull=True
    ).values(
        "ad_id",
        "ad_ref",
        "ad_title",
        "ad_source",
        "ad_creative_url",
        "current_product__title",
    ).annotate(count=Count("id", distinct=True)).order_by(
        "ad_id", "ad_ref", "ad_title", "-count", "current_product__title"
    )
    for row in campaign_product_rows:
        bucket = campaign_bucket(
            ad_id=row["ad_id"],
            ad_ref=row["ad_ref"],
            ad_title=row["ad_title"],
            ad_source=row["ad_source"],
            ad_creative_url=row["ad_creative_url"],
        )
        if bucket:
            title = row["current_product__title"] or ""
            bucket["product_counts"][title] = (
                bucket["product_counts"].get(title, 0)
                + int(row["count"] or 0)
            )

    for row in ad_active_clients.values(
        "ad_id",
        "ad_ref",
        "ad_title",
        "ad_source",
        "ad_creative_url",
    ).annotate(
        chats=Count("id", distinct=True),
        qualified=Count(
            "id", filter=Q(buying_readiness__gte=40), distinct=True
        ),
        product_matched=Count(
            "id", filter=Q(current_product__isnull=False), distinct=True
        ),
    ).order_by():
        bucket = campaign_bucket(
            ad_id=row["ad_id"],
            ad_ref=row["ad_ref"],
            ad_title=row["ad_title"],
            ad_source=row["ad_source"],
            ad_creative_url=row["ad_creative_url"],
        )
        if bucket:
            bucket["chats"] += int(row["chats"] or 0)
            bucket["qualified"] += int(row["qualified"] or 0)
            bucket["product_matched"] += int(row["product_matched"] or 0)

    money_field = DecimalField(max_digits=14, decimal_places=2)
    gross_expression = Coalesce(
        F("payment_projection__gross_amount"),
        F("paid_amount"),
        Value(Decimal("0")),
        output_field=money_field,
    )
    refunded_expression = Coalesce(
        F("payment_projection__refunded_amount"),
        F("refunded_amount"),
        Value(Decimal("0")),
        output_field=money_field,
    )
    net_expression = ExpressionWrapper(
        gross_expression - refunded_expression,
        output_field=money_field,
    )
    ad_deal_identity = ~(
        Q(client__ad_id="")
        & Q(client__ad_ref="")
        & Q(client__ad_title="")
    )
    ad_paid_deals = payment_deals.filter(
        client__hidden_at__isnull=True,
    ).filter(ad_deal_identity).filter(verified_payment_q())
    for row in ad_paid_deals.values(
        "client__ad_id",
        "client__ad_ref",
        "client__ad_title",
        "client__ad_source",
        "client__ad_creative_url",
    ).annotate(
        paid=Count("id", distinct=True),
        gross_revenue=Sum(gross_expression),
        refunded_revenue=Sum(refunded_expression),
        revenue=Sum(net_expression),
        revenue_unpriced_payments=Count(
            "id",
            filter=Q(payment_projection__isnull=True, paid_amount=0),
        ),
    ).order_by():
        bucket = campaign_bucket(
            ad_id=row["client__ad_id"],
            ad_ref=row["client__ad_ref"],
            ad_title=row["client__ad_title"],
            ad_source=row["client__ad_source"],
            ad_creative_url=row["client__ad_creative_url"],
        )
        if bucket:
            bucket["paid"] += int(row["paid"] or 0)
            bucket["gross_revenue"] += row["gross_revenue"] or Decimal("0")
            bucket["refunded_revenue"] += (
                row["refunded_revenue"] or Decimal("0")
            )
            bucket["revenue"] += row["revenue"] or Decimal("0")
            bucket["revenue_unpriced_payments"] += int(
                row["revenue_unpriced_payments"] or 0
            )

    ad_rows = []
    for bucket in campaign_buckets.values():
        top_product_title = ""
        if bucket["product_counts"]:
            top_product_title = sorted(
                bucket["product_counts"].items(),
                key=lambda item: (-item[1], item[0]),
            )[0][0]
        event_counts = bucket["event_counts"]
        ad_rows.append({
            key: bucket[key]
            for key in (
                "attribution_key_type",
                "attribution_key",
                "ad_id",
                "ad_ref",
                "ad_title",
                "ad_source",
                "ad_creative_url",
                "chats",
                "qualified",
                "product_matched",
                "paid",
                "lost_or_refused",
                "revenue_unpriced_payments",
            )
        } | {
            "paylinks_issued": event_counts.get(
                IgFunnelStepEvent.Type.PAYLINK_ISSUED, 0
            ),
            "paylinks_viewed": event_counts.get(
                IgFunnelStepEvent.Type.PAYLINK_VIEWED, 0
            ),
            "top_product_title": top_product_title,
            "gross_revenue": str(bucket["gross_revenue"]),
            "refunded_revenue": str(bucket["refunded_revenue"]),
            "revenue": str(bucket["revenue"]),
        })
    ad_rows.sort(
        key=lambda row: (
            -row["paid"],
            -Decimal(row["revenue"]),
            -row["chats"],
            row["ad_title"] or row["ad_id"] or row["ad_ref"],
        )
    )
    ad_rows = ad_rows[:50]

    ad_interest_rows = list(
        ad_active_clients.exclude(current_product__isnull=True)
        .values("current_product_id")
        .annotate(count=Count("id", distinct=True))
        .order_by()
    )
    ad_paid_product_rows = list(
        IgDealItem.objects.filter(
            deal__client_id__in=ad_client_ids,
            deal__client__hidden_at__isnull=True,
            product__isnull=False,
        )
        .filter(paid_item_filter)
        .values("product_id")
        .annotate(
            verified_paid_orders=Count("deal_id", distinct=True),
            verified_paid_qty=Sum("qty"),
        )
        .order_by()
    )
    ad_interest_by_product = {
        row["current_product_id"]: row for row in ad_interest_rows
    }
    ad_paid_by_product = {row["product_id"]: row for row in ad_paid_product_rows}
    ad_products = []
    for product_id in set(ad_interest_by_product) | set(ad_paid_by_product):
        base = dict(product_rows_by_id.get(product_id, {}))
        interest = int(
            ad_interest_by_product.get(product_id, {}).get("count") or 0
        )
        paid = ad_paid_by_product.get(product_id, {})
        base.update({
            "product_id": product_id,
            "count": interest,
            "interest_count": interest,
            "verified_paid_orders": int(paid.get("verified_paid_orders") or 0),
            "verified_paid_qty": int(paid.get("verified_paid_qty") or 0),
        })
        ad_products.append(base)
    ad_products.sort(
        key=lambda row: (
            -row["interest_count"],
            -row["verified_paid_qty"],
            row.get("product_title", ""),
        )
    )

    from management.services.ig_funnel_analytics import build_funnel_analytics

    ad_funnel_analytics = build_funnel_analytics(
        since=since,
        until=until,
        client_ids=ad_client_ids,
    )
    ad_paylinks_issued = next(
        (
            row["entered"]
            for row in ad_funnel_analytics["steps"]
            if row["step"] == IgFunnelStepEvent.Type.PAYLINK_ISSUED
        ),
        0,
    )
    ad_paylinks_viewed = next(
        (
            row["entered"]
            for row in ad_funnel_analytics["steps"]
            if row["step"] == IgFunnelStepEvent.Type.PAYLINK_VIEWED
        ),
        0,
    )
    ad_verified_paid = sum(
        bucket["paid"] for bucket in campaign_buckets.values()
    )
    ad_gross_revenue = sum(
        (bucket["gross_revenue"] for bucket in campaign_buckets.values()),
        Decimal("0"),
    )
    ad_refunded_revenue = sum(
        (bucket["refunded_revenue"] for bucket in campaign_buckets.values()),
        Decimal("0"),
    )
    ad_revenue = sum(
        (bucket["revenue"] for bucket in campaign_buckets.values()),
        Decimal("0"),
    )
    ad_revenue_unpriced_payments = sum(
        bucket["revenue_unpriced_payments"]
        for bucket in campaign_buckets.values()
    )
    attribution_counts = active_clients.aggregate(
        conversation_population=Count("id"),
        confirmed_conversations=Count("id", filter=ad_client_identity),
        partial_conversations=Count(
            "id",
            filter=(
                Q(ad_id="", ad_ref="", ad_title="")
                & (Q(ad_source__gt="") | Q(ad_creative_url__gt=""))
            ),
        ),
    )
    attribution_population = int(
        attribution_counts["conversation_population"] or 0
    )
    attributed_conversations = int(
        attribution_counts["confirmed_conversations"] or 0
    )
    partial_attribution_conversations = int(
        attribution_counts["partial_conversations"] or 0
    )
    unattributed_conversations = max(
        0,
        attribution_population
        - attributed_conversations
        - partial_attribution_conversations,
    )
    if attribution_population == 0:
        attribution_status = "empty"
    elif attributed_conversations == attribution_population:
        attribution_status = "full"
    elif attributed_conversations or partial_attribution_conversations:
        attribution_status = "partial"
    else:
        attribution_status = "missing"
    attribution_coverage_percent = (
        round(attributed_conversations * 100 / attribution_population)
        if attribution_population
        else 0
    )
    ad_analytics = {
        "attribution": {
            "basis": "current_client_snapshot",
            "historical": False,
            "label": "Поточна рекламна прив'язка клієнта",
            "conversation_population": attribution_population,
            "confirmed_conversations": attributed_conversations,
            "partial_conversations": partial_attribution_conversations,
            "unattributed_conversations": unattributed_conversations,
            "coverage_percent": attribution_coverage_percent,
            "status": attribution_status,
            "campaign_count": len(campaign_buckets),
        },
        "totals": {
            **{key: int(value or 0) for key, value in ad_message_totals.items()},
            "qualified": ad_active_clients.filter(
                buying_readiness__gte=40
            ).count(),
            "product_matched": ad_active_clients.filter(
                current_product__isnull=False
            ).count(),
            "paylinks_issued": int(ad_paylinks_issued),
            "paylinks_viewed": int(ad_paylinks_viewed),
            "verified_paid": int(ad_verified_paid),
            "lost_or_refused": ad_losses.values(
                "episode__client_id"
            ).distinct().count(),
            "gross_revenue": str(ad_gross_revenue),
            "refunded_revenue": str(ad_refunded_revenue),
            "revenue": str(ad_revenue),
            "revenue_unpriced_payments": int(
                ad_revenue_unpriced_payments
            ),
        },
        "message_series": ad_message_series,
        "funnel": ad_funnel_analytics["steps"],
        "drop_off_reasons": ad_funnel_analytics["drop_off_reasons"],
        "campaigns": ad_rows,
        "products": ad_products[:25],
    }
    totals = {
        **{key: int(value or 0) for key, value in message_totals.items()},
        "conversations": conversations,
        "qualified": active_clients.filter(buying_readiness__gte=40).count(),
        "product_matched": active_clients.filter(current_product__isnull=False).count(),
        "checkout_or_payment": active_clients.filter(stage__in=[IgClient.Stage.CHECKOUT, IgClient.Stage.PAYMENT_PENDING]).count(),
        "paid": active_clients.filter(paid_in_range=True).count(),
        "hidden": hidden_clients.count(),
        "pending_followups": pending_followups.count(),
        "followup_recoveries": sent_followups.filter(followup_payment_filter).distinct().count(),
        "discount_conversions": active_clients.filter(discount_offered_percent__gt=0, paid_in_range=True).count(),
        "manager_takeovers": active_clients.filter(manager_takeover=True).count(),
        "custom_print_handoffs": active_clients.filter(intent=IgClient.Intent.CUSTOM_PRINT, stage=IgClient.Stage.LEAD_TO_MANAGER).count(),
        "lost_or_refused": lost_or_refused,
        "funnel_conversations": funnel_clients.count(),
        "funnel_qualified": funnel_clients.filter(buying_readiness__gte=40).count(),
        "funnel_product_matched": funnel_clients.filter(
            current_product__isnull=False
        ).count(),
        "funnel_checkout_or_payment": funnel_clients.filter(
            stage__in=[IgClient.Stage.CHECKOUT, IgClient.Stage.PAYMENT_PENDING]
        ).count(),
        "funnel_paid": funnel_clients.filter(paid_in_range=True).count(),
    }
    funnel_analytics = build_funnel_analytics(since=since, until=until)
    revenue_deals = payment_deals.filter(verified_payment_q())
    revenue_rows = list(
        revenue_deals.values(
            "paid_amount",
            "amount",
            "refunded_amount",
            "payment_projection__gross_amount",
            "payment_projection__refunded_amount",
        )
    )
    verified_payment_count = len(revenue_rows)
    priced_payment_count = 0
    known_net_revenue = Decimal("0.00")
    for payment in revenue_rows:
        projection_gross = payment["payment_projection__gross_amount"]
        projection_refunded = payment["payment_projection__refunded_amount"]
        legacy_amount = payment["paid_amount"]
        if projection_gross is not None and projection_gross > 0:
            gross = projection_gross
            refunded = projection_refunded or Decimal("0")
        elif legacy_amount is not None and legacy_amount > 0:
            gross = legacy_amount
            refunded = payment["refunded_amount"] or Decimal("0")
        else:
            continue
        priced_payment_count += 1
        known_net_revenue += max(Decimal("0"), gross - refunded)
    amount_coverage_percent = (
        round(priced_payment_count * 100 / verified_payment_count)
        if verified_payment_count
        else 0
    )
    revenue_status = (
        "empty"
        if verified_payment_count == 0
        else "complete"
        if priced_payment_count == verified_payment_count
        else "partial"
    )
    revenue = {
        "verified_payment_count": verified_payment_count,
        "priced_payment_count": priced_payment_count,
        "unpriced_payment_count": verified_payment_count - priced_payment_count,
        "known_net_revenue": f"{known_net_revenue:.2f}",
        "amount_coverage_percent": amount_coverage_percent,
        "status": revenue_status,
    }
    modules = {
        "activity": {
            "time_basis": "message_event",
            "metrics": {
                "messages": metric_contract(
                    unit="events",
                    basis="visible_messages_in_period",
                    time_field="provider_created_at_or_created_at",
                    population="visible message events",
                    numerator="messages",
                    source_kind="message_event",
                ),
                "conversations": metric_contract(
                    unit="conversations",
                    basis="visible_senders_in_period",
                    time_field="provider_created_at_or_created_at",
                    population="distinct visible senders",
                    numerator="unique_conversations",
                    source_kind="message_event",
                ),
            },
        },
        "funnel": {
            "time_basis": "event_cohort",
            "metrics": {
                "conversion": metric_contract(
                    unit="percent",
                    basis="entry_event_same_window",
                    time_field="occurred_at",
                    population="entered episodes",
                    numerator="advanced",
                    denominator="entered",
                    completeness="row_level",
                    source_kind="immutable_event",
                ),
            },
        },
        "current_stages": {
            "time_basis": "current_snapshot",
            "metrics": {
                "count": metric_contract(
                    unit="clients",
                    basis="current_client_snapshot",
                    time_field="last_interaction_or_created_at",
                    population="scoped active clients",
                    numerator="clients",
                    source_kind="mutable_snapshot",
                ),
            },
        },
        "objection_clients": {
            "time_basis": "current_snapshot",
            "metrics": {
                "count": metric_contract(
                    unit="clients",
                    basis="primary_objection_snapshot",
                    time_field="last_interaction_or_created_at",
                    population="clients with a primary objection",
                    numerator="clients",
                    source_kind="mutable_snapshot",
                ),
            },
        },
        "objection_signals": {
            "time_basis": "signal_event",
            "metrics": {
                "count": metric_contract(
                    unit="events",
                    basis="classified_signal_events",
                    time_field="created_at",
                    population="objection signal events",
                    numerator="events",
                    source_kind="signal_event",
                ),
            },
        },
        "revenue": {
            "time_basis": "payment_event",
            "metrics": {
                "known_net_revenue": metric_contract(
                    unit="currency",
                    basis="verified_priced_payments",
                    time_field="paid_at",
                    population="verified payments",
                    numerator="known_net_revenue",
                    denominator="",
                    completeness=revenue_status,
                    source_kind="payment_projection_or_legacy_truth",
                    available=priced_payment_count > 0,
                ),
            },
        },
    }
    payload = {
        "success": True,
        "schema_version": 3,
        "generated_at": timezone.now().isoformat(),
        "scope": {
            "timezone": timezone.get_current_timezone_name(),
            "mode": range_mode,
            "date_from": period_date_from,
            "date_to": period_date_to,
        },
        "modules": modules,
        "revenue": revenue,
        "period": {
            "mode": range_mode,
            "label": period_label,
            "timezone": timezone.get_current_timezone_name(),
            "date_from": period_date_from,
            "date_to": period_date_to,
            "event_time": "provider_created_at_or_created_at",
            "local": {
                "from": since.astimezone(local_tz).isoformat() if since else "",
                "to_exclusive": until.astimezone(local_tz).isoformat() if until else "",
            },
            "utc": {
                "from": since.astimezone(datetime_timezone.utc).isoformat() if since else "",
                "to_exclusive": until.astimezone(datetime_timezone.utc).isoformat() if until else "",
            },
        },
        "message_series": message_series,
        "ad_analytics": ad_analytics,
        "range_mode": range_mode,
        "range_days": range_days,
        "range_from": since.isoformat() if since else "",
        "range_to": until.isoformat() if until else "",
        "date_from": period_date_from,
        "date_to": period_date_to,
        "totals": totals,
        "stages": stage_counts,
        "interactions": interaction_counts,
        "objection_clients": objection_clients,
        "objection_signals": objection_signals,
        "objections": objections,
        "signals": signals,
        "products": product_interest,
        "ads": ad_rows,
        "funnel": funnel_analytics["steps"],
        "funnel_meta": {
            "source": "event_cohort",
            "backfilled": funnel_analytics["backfilled"],
            "event_types": funnel_analytics["event_types"],
            "drop_off_reasons": funnel_analytics["drop_off_reasons"],
            "followup_effectiveness": funnel_analytics["followup_effectiveness"],
            "discounts": funnel_analytics["discounts"],
            "manager_vs_bot": funnel_analytics["manager_vs_bot"],
            "time_on_step": funnel_analytics["time_on_step"],
        },
    }
    from .services.ig_stats_cockpit import build_performance_contract

    query_count_available = query_count_start is not None
    query_count = (
        len(connection.queries) - query_count_start
        if query_count_available
        else None
    )
    serialized_payload_bytes = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    payload["performance"] = build_performance_contract(
        query_count=query_count,
        query_count_available=query_count_available,
        materialized_message_rows=materialized_message_rows,
        serialized_payload_bytes=serialized_payload_bytes,
    )
    return JsonResponse(payload)


# ---------------------------------------------------------------------------
# Інструкції / швидкі посилання / реклама — CRUD у вкладці «Бот» (Task 23)
# ---------------------------------------------------------------------------
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "on", "yes"}


@login_required(login_url="management_login")
@require_GET
def bot_kb_api(request):
    blocked = _require_bot_capabilities(request, EDIT_IG_PROMPT_PERMISSION)
    if blocked:
        return blocked
    from .models import BotAdCampaign, BotInstruction, BotQuickLink
    from .services.ig_policy_publication import PolicyPublicationError, draft_state

    try:
        draft = draft_state()
    except PolicyPublicationError as exc:
        return JsonResponse({
            "success": False,
            "error": str(exc),
            "code": exc.code,
        }, status=409)
    settings_obj = InstagramBotSettings.objects.select_related(
        "active_instruction_publication"
    ).get(pk=1)
    head = settings_obj.active_instruction_publication

    def instruction_routing(instruction):
        from .services.bot_instruction_routing import split_instruction_tags

        parts = split_instruction_tags(instruction.intent_tags)
        tags = set(parts["plain"])
        reserved_programme = "programme:shooting_prize" in tags
        tags.discard("programme:shooting_prize")
        tags.update(f"not:{value}" for value in parts["excludes"])
        triggers = set(parts["triggers"])
        triggers.update(str(value) for value in instruction.trigger_codes or [])
        programme_kind = str(
            (instruction.programme_metadata or {}).get("kind") or ""
        )
        if not programme_kind and reserved_programme:
            programme_kind = "shooting_prize"
        return ", ".join(sorted(tags)), sorted(triggers), programme_kind

    def instruction_payload(instruction):
        tags, triggers, programme_kind = instruction_routing(instruction)
        return {
            "id": instruction.id,
            "title": instruction.title,
            "body": instruction.body,
            "intent_tags": tags,
            "is_active": instruction.is_active,
            "priority": instruction.priority,
            "locale": instruction.locale,
            "trigger_codes": triggers,
            "programme_kind": programme_kind,
            "allowed_actions": list(instruction.allowed_actions or []),
            "trust_scope": instruction.trust_scope,
        }

    instructions = [
        instruction_payload(i)
        for i in BotInstruction.objects.all().order_by("priority", "id")[:300]
    ]
    quick_links = [
        {"id": q.id, "kind": q.kind, "label": q.label, "url": q.url,
         "garment_type": q.garment_type, "trigger_keywords": q.trigger_keywords,
         "is_active": q.is_active, "order": q.order}
        for q in BotQuickLink.objects.all().order_by("order", "id")[:300]
    ]
    ad_campaigns = [
        {"id": a.id, "ad_id": a.ad_id, "ref": a.ref, "title": a.title, "theme": a.theme,
         "landing_note": a.landing_note, "is_active": a.is_active}
        for a in BotAdCampaign.objects.all().order_by("-id")[:300]
    ]
    return JsonResponse({
        "success": True,
        "instructions": instructions,
        "quick_links": quick_links,
        "ad_campaigns": ad_campaigns,
        "policy": {
            "draft_revision": draft.revision,
            "draft_hash": draft.snapshot_hash,
            "active_publication": (
                {
                    "id": head.pk,
                    "version": int(head.version),
                    "snapshot_hash": head.snapshot_hash,
                    "instruction_count": int(head.instruction_count),
                    "created_at": head.created_at.isoformat(),
                }
                if head is not None else None
            ),
            "ready": head is not None,
            "readiness_code": "" if head is not None else "active_publication_missing",
            "has_unpublished_changes": bool(
                head is None or head.snapshot_hash != draft.snapshot_hash
            ),
        },
    })


@login_required(login_url="management_login")
@require_POST
def bot_kb_save_api(request):
    blocked = _require_bot_capabilities(request, EDIT_IG_PROMPT_PERMISSION)
    if blocked:
        return blocked
    from .models import BotAdCampaign, BotInstruction, BotQuickLink

    kind = (request.POST.get("type") or "").strip()
    op = (request.POST.get("op") or "save").strip()
    obj_id = request.POST.get("id") or None

    model = {
        "instruction": BotInstruction,
        "quicklink": BotQuickLink,
        "adcampaign": BotAdCampaign,
    }.get(kind)
    if not model:
        return JsonResponse({"success": False, "error": "Невідомий тип."}, status=400)

    if kind == "instruction":
        from .services.ig_policy_publication import (
            DraftRevisionConflict,
            PolicyPublicationError,
            delete_instruction_draft,
            save_instruction_draft,
        )

        try:
            expected_revision = int(request.POST.get("draft_revision"))
        except (TypeError, ValueError):
            return JsonResponse({
                "success": False,
                "error": "Оновіть чернетку перед збереженням.",
                "code": "draft_revision_required",
            }, status=409)
        expected_hash = str(request.POST.get("draft_hash") or "")
        try:
            if op == "delete":
                state = delete_instruction_draft(
                    expected_revision=expected_revision,
                    expected_snapshot_hash=expected_hash,
                    instruction_id=int(obj_id or 0),
                    actor=request.user,
                )
                return JsonResponse({
                    "success": True,
                    "draft_revision": state.revision,
                    "draft_hash": state.snapshot_hash,
                })
            try:
                priority = int(request.POST.get("priority") or 100)
            except (TypeError, ValueError):
                priority = 100

            def split_values(name):
                return [
                    value.strip()
                    for value in str(request.POST.get(name) or "")
                    .replace(";", ",").split(",")
                    if value.strip()
                ]

            programme_kind = str(
                request.POST.get("programme_kind") or ""
            ).strip()
            programme_metadata = (
                {
                    "kind": "shooting_prize",
                    "programme_id": "shooting_prize",
                    "manager_required": True,
                    "confirmed_visual_sample": False,
                }
                if programme_kind == "shooting_prize" else {}
            )
            tags = split_values("intent_tags")
            if programme_kind == "shooting_prize":
                tags.append("programme:shooting_prize")
            instruction, state = save_instruction_draft(
                expected_revision=expected_revision,
                expected_snapshot_hash=expected_hash,
                instruction_id=int(obj_id) if obj_id else None,
                actor=request.user,
                values={
                    "title": request.POST.get("title") or "",
                    "body": request.POST.get("body") or "",
                    "active": _truthy(request.POST.get("is_active", "1")),
                    "priority": priority,
                    "locale": request.POST.get("locale") or "all",
                    "tags": tags,
                    "triggers": split_values("trigger_codes"),
                    "programme_metadata": programme_metadata,
                    "allowed_actions": split_values("allowed_actions"),
                    "trust_scope": request.POST.get("trust_scope") or "public_policy",
                },
            )
            return JsonResponse({
                "success": True,
                "id": instruction.pk,
                "draft_revision": state.revision,
                "draft_hash": state.snapshot_hash,
            })
        except DraftRevisionConflict:
            return JsonResponse({
                "success": False,
                "error": "Чернетку вже змінено. Оновіть сторінку.",
                "code": "draft_revision_conflict",
            }, status=409)
        except (PolicyPublicationError, TypeError, ValueError) as exc:
            return JsonResponse({
                "success": False,
                "error": str(exc),
                "code": getattr(exc, "code", "invalid_instruction"),
            }, status=400)

    if op == "delete":
        if obj_id:
            model.objects.filter(id=obj_id).delete()
        return JsonResponse({"success": True})

    obj = model.objects.filter(id=obj_id).first() if obj_id else model()
    p = request.POST
    warnings: list[str] = []
    if kind == "quicklink":
        obj.kind = (p.get("kind") or "other")[:20]
        obj.label = (p.get("label") or "")[:200]
        obj.url = (p.get("url") or "")[:600]
        obj.garment_type = (p.get("garment_type") or "")[:40]
        obj.trigger_keywords = (p.get("trigger_keywords") or "")[:400]
        obj.is_active = _truthy(p.get("is_active", "1"))
        try:
            obj.order = int(p.get("order") or 100)
        except (TypeError, ValueError):
            obj.order = 100
    else:  # adcampaign
        obj.ad_id = (p.get("ad_id") or "")[:64]
        obj.ref = (p.get("ref") or "")[:255]
        obj.title = (p.get("title") or "")[:255]
        obj.theme = (p.get("theme") or "")[:120]
        obj.landing_note = p.get("landing_note") or ""
        obj.is_active = _truthy(p.get("is_active", "1"))
    obj.save()
    payload = {"success": True, "id": obj.id}
    if warnings:
        payload["warnings"] = warnings
    return JsonResponse(payload)


_POLICY_OMISSION_LABELS = {
    "operator_only": "Лише для операторів — не потрапляє у відповідь бота",
    "inactive": "Вимкнено у чернетці",
    "empty_body": "Порожній текст",
    "locale_mismatch": "Інша мова",
    "not_relevant": "Не відповідає тегам або тригеру",
    "budget_exhausted": "Не вмістилося у ліміт інструкцій",
}


def _policy_error_response(exc, *, default_status=400):
    from .services.ig_policy_publication import (
        DraftRevisionConflict,
        PolicyPublicationError,
        PublicationHeadConflict,
    )

    if not isinstance(exc, (PolicyPublicationError, TypeError, ValueError)):
        return JsonResponse({
            "success": False,
            "error": "Не вдалося виконати операцію з інструкціями.",
            "code": "policy_publication_unavailable",
        }, status=500)
    status = 409 if isinstance(
        exc, (DraftRevisionConflict, PublicationHeadConflict)
    ) else default_status
    return JsonResponse({
        "success": False,
        "error": (
            "Дані вже змінилися. Оновіть чернетку й повторіть дію."
            if status == 409
            else str(exc) if isinstance(exc, PolicyPublicationError)
            else "Некоректні параметри запиту."
        ),
        "code": getattr(exc, "code", "policy_publication_error"),
    }, status=status)


@login_required(login_url="management_login")
@require_POST
def bot_policy_preview_api(request):
    blocked = _require_bot_capabilities(request, EDIT_IG_PROMPT_PERMISSION)
    if blocked:
        return blocked
    from .services.bot_instruction_routing import turn_triggers
    from .services.ig_policy_publication import preview_instruction_draft

    try:
        expected_revision = int(request.POST.get("draft_revision"))
        expected_hash = str(request.POST.get("draft_hash") or "")
        raw_tags = str(request.POST.get("audience_tags") or "")
        audience_tags = {
            value.strip().casefold()
            for value in raw_tags.replace(";", ",").split(",")
            if value.strip()
        } or None
        turn_text = str(request.POST.get("turn_text") or "")[:1000]
        preview = preview_instruction_draft(
            expected_revision=expected_revision,
            expected_snapshot_hash=expected_hash,
            locale=request.POST.get("locale") or "all",
            client_tags=audience_tags,
            active_triggers=turn_triggers(turn_text),
        )
    except Exception as exc:
        return _policy_error_response(exc)
    readiness = (
        ["active_instruction_empty"]
        if any(item["reason"] == "empty_body" for item in preview["omitted"])
        else []
    )
    return JsonResponse({
        "success": True,
        "draft_revision": preview["draft_revision"],
        "draft_hash": preview["snapshot_hash"],
        "instruction_count": preview["instruction_count"],
        "used_chars": preview["used_chars"],
        "ready": not readiness,
        "readiness": readiness,
        "selected": [
            {
                "id": item["id"],
                "title": item.get("title") or "Без назви",
                "priority": item["priority"],
                "locale": item["locale"],
            }
            for item in preview["selected"]
        ],
        "omitted": [
            {
                "id": item["id"],
                "reason": item["reason"],
                "label": _POLICY_OMISSION_LABELS.get(
                    item["reason"], "Не вибрано"
                ),
            }
            for item in preview["omitted"]
        ],
        "effective_proposal_actions": preview["effective_proposal_actions"],
    })


@login_required(login_url="management_login")
@require_POST
def bot_policy_publish_api(request):
    blocked = _require_bot_capabilities(request, EDIT_IG_PROMPT_PERMISSION)
    if blocked:
        return blocked
    from .services.ig_policy_publication import publish_instruction_policy

    settings_obj = InstagramBotSettings.load()
    if not settings_obj.active_instruction_publication_id:
        return JsonResponse({
            "success": False,
            "error": "Опублікований знімок інструкцій ще не ініціалізовано.",
            "code": "active_publication_missing",
        }, status=503)
    try:
        result = publish_instruction_policy(
            expected_draft_revision=int(request.POST.get("draft_revision")),
            expected_draft_hash=str(request.POST.get("draft_hash") or ""),
            expected_head_id=int(request.POST.get("head_id")),
            expected_head_hash=str(request.POST.get("head_hash") or ""),
            actor=request.user,
            note=str(request.POST.get("note") or "")[:500],
        )
    except Exception as exc:
        return _policy_error_response(exc)
    publication = result.publication
    return JsonResponse({
        "success": True,
        "changed": result.changed,
        "publication": {
            "id": publication.pk,
            "version": int(publication.version),
            "snapshot_hash": publication.snapshot_hash,
            "instruction_count": int(publication.instruction_count),
        },
    })


@login_required(login_url="management_login")
@require_GET
def bot_policy_history_api(request):
    blocked = _require_bot_capabilities(request, EDIT_IG_PROMPT_PERMISSION)
    if blocked:
        return blocked
    from .services.ig_policy_publication import publication_history

    settings_obj = InstagramBotSettings.load()
    return JsonResponse({
        "success": True,
        "active_publication_id": settings_obj.active_instruction_publication_id,
        "history": publication_history(limit=50),
    })


@login_required(login_url="management_login")
@require_POST
def bot_policy_rollback_api(request):
    blocked = _require_bot_capabilities(request, EDIT_IG_PROMPT_PERMISSION)
    if blocked:
        return blocked
    from .services.ig_policy_publication import rollback_instruction_policy

    try:
        result = rollback_instruction_policy(
            target_publication_id=int(request.POST.get("target_publication_id")),
            expected_head_id=int(request.POST.get("head_id")),
            expected_head_hash=str(request.POST.get("head_hash") or ""),
            actor=request.user,
            note=str(request.POST.get("note") or "")[:500],
        )
    except Exception as exc:
        return _policy_error_response(exc)
    publication = result.publication
    return JsonResponse({
        "success": True,
        "publication": {
            "id": publication.pk,
            "version": int(publication.version),
            "snapshot_hash": publication.snapshot_hash,
            "restored_from_id": publication.restored_from_id,
        },
    })
