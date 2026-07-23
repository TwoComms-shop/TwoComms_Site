"""
Вкладка «Бот» (адміністратори + обмежений Meta reviewer).

UI зі станом агента (запущено/зупинено, очікує повідомлення), кнопками
Start/Stop, вибором джерела ключів і онлайн-консоллю подій.
"""
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
import base64
import hashlib
import hmac
import json
import os
import re
import secrets

from .bot_access import is_meta_bot_reviewer
from .models import InstagramBotLog, InstagramBotSettings
from .services import instagram_bot as bot


def _is_admin(user) -> bool:
    return bool(user.is_authenticated and (user.is_staff or user.is_superuser))


def _can_use_bot(user) -> bool:
    return _is_admin(user) or is_meta_bot_reviewer(user)


def _is_reviewer_only(user) -> bool:
    return is_meta_bot_reviewer(user) and not _is_admin(user)


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


def _delete_direct_bot_records(identifier: str) -> dict:
    from .models import (
        BotDataDeletionRequest,
        IgClient,
        InstagramBotLog,
        InstagramBotMessage,
        InstagramBotProcessedMessage,
        InstagramBotRawEvent,
    )

    normalized = _normalize_deletion_identifier(identifier)
    result = {
        "normalized_identifier": normalized,
        "status": BotDataDeletionRequest.Status.NO_MATCH,
        "clients": 0,
        "messages": 0,
        "raw_events": 0,
        "logs": 0,
        "detail": "",
    }
    if not normalized:
        result["detail"] = "Empty identifier."
        return result

    with transaction.atomic():
        clients = list(
            IgClient.objects.filter(
                Q(igsid__iexact=normalized)
                | Q(username__iexact=normalized)
                | Q(display_name__iexact=normalized)
                | Q(phone_normalized__iexact=normalized)
            )
        )
        sender_ids = {normalized}
        sender_ids.update(c.igsid for c in clients if c.igsid)
        mids = list(
            InstagramBotMessage.objects.filter(
                Q(sender_id__in=sender_ids) | Q(client__in=clients)
            ).exclude(mid__isnull=True).values_list("mid", flat=True)
        )
        messages_count, _ = InstagramBotMessage.objects.filter(
            Q(sender_id__in=sender_ids) | Q(client__in=clients)
        ).delete()
        raw_events_count, _ = InstagramBotRawEvent.objects.filter(sender_id__in=sender_ids).delete()
        logs_count, _ = InstagramBotLog.objects.filter(detail__icontains=normalized).delete()
        if mids:
            InstagramBotProcessedMessage.objects.filter(mid__in=mids).delete()
        clients_count = len(clients)
        IgClient.objects.filter(id__in=[c.id for c in clients]).delete()

    result.update({
        "status": (
            BotDataDeletionRequest.Status.COMPLETED
            if any([clients_count, messages_count, raw_events_count, logs_count])
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
    from .models import BotDataDeletionRequest

    identifier = (request.POST.get("identifier") or "").strip()
    deletion = _delete_direct_bot_records(identifier)
    deletion_request = BotDataDeletionRequest.objects.create(
        confirmation_code=_new_deletion_code(),
        source=BotDataDeletionRequest.Source.MANUAL_FORM,
        identifier=identifier[:255],
        normalized_identifier=deletion["normalized_identifier"][:255],
        status=deletion["status"],
        deleted_clients_count=deletion["clients"],
        deleted_messages_count=deletion["messages"],
        deleted_raw_events_count=deletion["raw_events"],
        deleted_logs_count=deletion["logs"],
        detail=deletion["detail"],
    )
    deletion_request.mark_completed()
    return redirect("management_data_deletion_status", confirmation_code=deletion_request.confirmation_code)


def _base64_url_decode(value: str) -> bytes:
    value += "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value.encode("utf-8"))


def _parse_meta_signed_request(signed_request: str) -> dict:
    if not signed_request or "." not in signed_request:
        return {}
    encoded_sig, encoded_payload = signed_request.split(".", 1)
    payload = json.loads(_base64_url_decode(encoded_payload).decode("utf-8"))

    app_secret = (
        os.environ.get("IG_APP_SECRET")
        or os.environ.get("FACEBOOK_APP_SECRET")
        or getattr(settings, "IG_APP_SECRET", "")
        or getattr(settings, "FACEBOOK_APP_SECRET", "")
    )
    if app_secret:
        expected = hmac.new(
            app_secret.encode("utf-8"),
            msg=encoded_payload.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_base64_url_decode(encoded_sig), expected):
            return {}
    return payload


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


def _require_admin_json(request):
    if not _is_admin(request.user):
        return JsonResponse({"success": False, "error": "Доступ лише для адміністраторів."}, status=403)
    return None


def _require_bot_json(request):
    if not _can_use_bot(request.user):
        return JsonResponse({"success": False, "error": "Доступ лише до вкладки бота."}, status=403)
    return None


def _log_items(limit: int = 80):
    rows = InstagramBotLog.objects.all()[:limit]
    return [
        {
            "id": r.id,
            "level": r.level,
            "event": r.event,
            "detail": r.detail,
            "time": r.created_at.strftime("%H:%M:%S"),
            "date": r.created_at.strftime("%d.%m.%Y"),
        }
        for r in rows
    ]


@login_required(login_url="management_login")
def bot_dashboard(request):
    if not _can_use_bot(request.user):
        return redirect("management_home")
    settings_obj = InstagramBotSettings.load()
    reviewer_mode = _is_reviewer_only(request.user)
    return render(
        request,
        "management/bot.html",
        {
            "settings": settings_obj,
            "status": bot.status_snapshot(),
            "log_items": _log_items(),
            "cred_env": InstagramBotSettings.CredSource.ENV,
            "cred_custom": InstagramBotSettings.CredSource.CUSTOM,
            "has_custom_direct_token": bool(settings_obj.custom_direct_token),
            "has_custom_gemini_key": bool(settings_obj.custom_gemini_key),
            "meta_bot_reviewer_mode": reviewer_mode,
        },
    )


@login_required(login_url="management_login")
@require_POST
def bot_start_api(request):
    blocked = _require_bot_json(request)
    if blocked:
        return blocked
    bot.start_bot()
    return JsonResponse({"success": True, "status": bot.status_snapshot()})


@login_required(login_url="management_login")
@require_POST
def bot_stop_api(request):
    blocked = _require_bot_json(request)
    if blocked:
        return blocked
    bot.stop_bot()
    return JsonResponse({"success": True, "status": bot.status_snapshot()})


@login_required(login_url="management_login")
@require_GET
def bot_status_api(request):
    blocked = _require_bot_json(request)
    if blocked:
        return blocked
    try:
        after_id = int(request.GET.get("after_id") or 0)
    except (TypeError, ValueError):
        after_id = 0

    rows = InstagramBotLog.objects.all()
    if after_id:
        rows = rows.filter(id__gt=after_id)
    rows = list(rows[:120])
    rows.reverse()  # від старіших до новіших для дозапису в консоль
    items = [
        {
            "id": r.id,
            "level": r.level,
            "event": r.event,
            "detail": r.detail,
            "time": r.created_at.strftime("%H:%M:%S"),
        }
        for r in rows
    ]
    return JsonResponse({"success": True, "status": bot.status_snapshot(), "log": items})


@login_required(login_url="management_login")
@require_POST
def bot_settings_save_api(request):
    blocked = _require_bot_json(request)
    if blocked:
        return blocked
    s = InstagramBotSettings.load()

    reviewer_mode = _is_reviewer_only(request.user)
    if not reviewer_mode:
        direct_source = (request.POST.get("direct_source") or "").strip()
        if direct_source in InstagramBotSettings.CredSource.values:
            s.direct_source = direct_source
        gemini_source = (request.POST.get("gemini_source") or "").strip()
        if gemini_source in InstagramBotSettings.CredSource.values:
            s.gemini_source = gemini_source

        if "custom_direct_token" in request.POST:
            value = (request.POST.get("custom_direct_token") or "").strip()
            if value:
                s.custom_direct_token = value
        if _truthy(request.POST.get("clear_custom_direct_token")):
            s.custom_direct_token = ""
        if "custom_gemini_key" in request.POST:
            value = (request.POST.get("custom_gemini_key") or "").strip()
            if value:
                s.custom_gemini_key = value
        if _truthy(request.POST.get("clear_custom_gemini_key")):
            s.custom_gemini_key = ""

        trigger = (request.POST.get("trigger_text") or "").strip()
        if trigger:
            s.trigger_text = trigger[:255]
        reply = (request.POST.get("reply_text") or "").strip()
        if reply:
            s.reply_text = reply[:1000]

    # AI-режим / модель / правило / білий список.
    s.ai_enabled = (request.POST.get("ai_enabled") or "").strip() in {"1", "true", "on", "yes"}
    s.receive_via_poll = (request.POST.get("receive_via_poll") or "").strip() in {"1", "true", "on", "yes"}
    if not reviewer_mode:
        s.meta_feedback_enabled = _truthy(request.POST.get("meta_feedback_enabled"))
        if "meta_feedback_test_event_code" in request.POST:
            s.meta_feedback_test_event_code = (request.POST.get("meta_feedback_test_event_code") or "")[:120]
    model = (request.POST.get("gemini_model") or "").strip()
    if model:
        from management.services.gemini_keys import is_allowed_chat_model

        if not is_allowed_chat_model(model):
            return JsonResponse({"success": False, "error": "Недозволена модель Gemini."}, status=400)
        s.gemini_model = model[:80]
    if "system_prompt" in request.POST:
        if not reviewer_mode:
            s.system_prompt = (request.POST.get("system_prompt") or "").strip()
    if "knowledge_base" in request.POST:
        if not reviewer_mode:
            s.knowledge_base = (request.POST.get("knowledge_base") or "").strip()
    if "allowed_senders" in request.POST:
        if not reviewer_mode:
            s.allowed_senders = (request.POST.get("allowed_senders") or "").strip()

    if not reviewer_mode:
        try:
            interval = int(request.POST.get("poll_interval_seconds") or s.poll_interval_seconds)
            s.poll_interval_seconds = max(2, min(60, interval))
        except (TypeError, ValueError):
            pass

    s.save()
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
    return JsonResponse({"success": True, "status": bot.status_snapshot()})


# ---------------------------------------------------------------------------
# Вкладка «Клиенти» — CRM IG-клієнтів (Task 13)
# ---------------------------------------------------------------------------
def _client_card(c) -> dict:
    product = getattr(c, "current_product", None)
    next_followup = getattr(c, "next_followup_at", None)
    latest_analysis = getattr(c, "_latest_analysis", None)
    if isinstance(latest_analysis, (list, tuple)):
        latest_analysis = latest_analysis[0] if latest_analysis else None
    if latest_analysis is None:
        try:
            latest_analysis = c.analysis_snapshots.order_by("-id").first()
        except Exception:
            latest_analysis = None
    payment_status = ""
    try:
        latest_deal = c.deals.order_by("-id").first()
        payment_status = latest_deal.payment_status if latest_deal else ""
    except Exception:
        latest_deal = None
    return {
        "id": c.id,
        "igsid": c.igsid,
        "username": c.username,
        "name": c.display_name or c.username or c.igsid,
        "avatar": c.avatar_local or c.profile_pic_url,
        "stage": c.stage,
        "stage_label": c.get_stage_display(),
        "last_message_at": c.last_message_at.isoformat() if c.last_message_at else "",
        "purchases": c.purchases_count,
        "total_spent": str(c.total_spent),
        "bot_paused": c.bot_paused,
        "manager_takeover": c.manager_takeover,
        "spam_strikes": c.spam_strikes,
        "ad_title": c.ad_title,
        "ad_id": c.ad_id,
        "ad_ref": c.ad_ref,
        "language": c.language,
        "intent": c.intent,
        "buying_readiness": c.buying_readiness,
        "analysis_band": latest_analysis.score_band if latest_analysis else "",
        "analysis_probability": str(latest_analysis.purchase_probability) if latest_analysis else "",
        "analysis_confidence": str(latest_analysis.confidence) if latest_analysis else "",
        "analysis_evidence": latest_analysis.evidence if latest_analysis else [],
        "analysis_uncertainties": latest_analysis.uncertainties if latest_analysis else [],
        "analysis_at": latest_analysis.created_at.isoformat() if latest_analysis else "",
        "primary_objection": c.primary_objection,
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
        "delivery_status": c.delivery_status,
        "delivery_status_label": c.get_delivery_status_display() if c.delivery_status else "",
        "delivery_error": c.delivery_error,
        "delivery_failed_at": c.delivery_failed_at.isoformat() if c.delivery_failed_at else "",
    }


@login_required(login_url="management_login")
@require_GET
def bot_clients_api(request):
    blocked = _require_bot_json(request)
    if blocked:
        return blocked
    from django.db.models import Q

    from .models import IgClient

    view = (request.GET.get("view") or "active").strip().lower()
    from django.db.models import Prefetch
    from .ig_bot_models import IgConversationAnalysisSnapshot

    qs = IgClient.objects.select_related("current_product").prefetch_related(
        Prefetch(
            "analysis_snapshots",
            queryset=IgConversationAnalysisSnapshot.objects.order_by("-id")[:1],
            to_attr="_latest_analysis",
        )
    ).all()
    if view in {"hidden"}:
        qs = qs.filter(hidden_at__isnull=False)
    else:
        qs = qs.filter(hidden_at__isnull=True)
    if view in {"spam", "cold", "spam-cold", "spam_cold"}:
        qs = qs.filter(Q(stage__in=[IgClient.Stage.SPAM, IgClient.Stage.COLD]) | Q(spam_strikes__gt=0))
    elif view == "paid":
        qs = qs.filter(stage__in=[IgClient.Stage.PAID, IgClient.Stage.ORDER_CREATED, IgClient.Stage.DONE])
    elif view == "due":
        qs = qs.filter(followup_tasks__status="pending", followup_tasks__due_at__lte=timezone.now()).distinct()
    elif view == "ads":
        qs = qs.filter(Q(ad_id__gt="") | Q(ad_ref__gt="") | Q(ad_title__gt=""))
    elif view in {"delivery-blocked", "delivery_blocked"}:
        qs = qs.filter(delivery_status__gt="")
    elif view == "active":
        qs = qs.exclude(stage__in=[IgClient.Stage.SPAM, IgClient.Stage.COLD, IgClient.Stage.PAID, IgClient.Stage.ORDER_CREATED, IgClient.Stage.DONE])
    qs = qs.order_by("-last_message_at", "-id")
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(username__icontains=q)
            | Q(display_name__icontains=q)
            | Q(igsid__icontains=q)
            | Q(phone__icontains=q)
        )
    total = qs.count()
    rows = [_client_card(c) for c in qs[:200]]
    return JsonResponse({"success": True, "clients": rows, "total": total})


@login_required(login_url="management_login")
@require_GET
def bot_client_detail_api(request, client_id):
    blocked = _require_bot_json(request)
    if blocked:
        return blocked
    from .models import IgClient

    c = IgClient.objects.filter(id=client_id).first()
    if not c:
        return JsonResponse({"success": False, "error": "Клієнта не знайдено."}, status=404)

    try:
        after_id = int(request.GET.get("after_id") or 0)
    except (TypeError, ValueError):
        after_id = 0

    if after_id:
        msg_rows = list(c.messages.filter(id__gt=after_id).order_by("id")[:100])
    else:
        # Останні 300 (а не найстаріші) у хронологічному порядку — для live chat.
        msg_rows = list(c.messages.order_by("-id")[:300])
        msg_rows.reverse()
    messages = [
        {
            "id": m.id,
            "role": m.role,
            "text": m.text,
            "attachments": m.attachments or "",
            "time": m.created_at.isoformat() if m.created_at else "",
        }
        for m in msg_rows
    ]
    last_message_id = msg_rows[-1].id if msg_rows else after_id

    # Інкрементальний режим (live chat): лише нові повідомлення + прапори стану,
    # без важких events/deals/funnel — щоб не вантажити сервер на кожному поллі.
    if after_id:
        return JsonResponse({
            "success": True,
            "messages": messages,
            "last_message_id": last_message_id,
            "bot_paused": c.bot_paused,
            "manager_takeover": c.manager_takeover,
            "stage": c.stage,
            "stage_label": c.get_stage_display(),
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
    signals = [
        {
            "type": s.signal_type,
            "confidence": str(s.confidence),
            "value": s.value,
            "payload": s.payload,
            "time": s.created_at.isoformat() if s.created_at else "",
        }
        for s in c.conversation_signals.all()[:80]
    ]
    followups = [
        {
            "id": f.id,
            "kind": f.kind,
            "status": f.status,
            "reason": f.reason,
            "discount_percent": f.discount_percent,
            "due_at": f.due_at.isoformat() if f.due_at else "",
            "meta_window_deadline": f.meta_window_deadline.isoformat() if f.meta_window_deadline else "",
            "skip_reason": f.skip_reason,
        }
        for f in c.followup_tasks.all()[:50]
    ]
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
        for d in c.deals.all()[:20]
    ]
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
    return JsonResponse({
        "success": True,
        "client": card,
        "messages": messages,
        "last_message_id": last_message_id,
        "events": events,
        "signals": signals,
        "followups": followups,
        "deals": deals,
        "funnel": c.funnel_progress(),
    })


@login_required(login_url="management_login")
@require_POST
def bot_client_pause_api(request, client_id):
    """Зупинити бота для клієнта (менеджер бере діалог на себе)."""
    blocked = _require_bot_json(request)
    if blocked:
        return blocked
    from django.utils import timezone

    from .models import IgClient

    c = IgClient.objects.filter(id=client_id).first()
    if not c:
        return JsonResponse({"success": False, "error": "Клієнта не знайдено."}, status=404)
    c.bot_paused = True
    c.paused_reason = "manual"
    c.paused_at = timezone.now()
    c.save(update_fields=["bot_paused", "paused_reason", "paused_at", "updated_at"])
    return JsonResponse({"success": True, "bot_paused": True})


@login_required(login_url="management_login")
@require_POST
def bot_client_resume_api(request, client_id):
    """Повернути бота клієнту (зняти паузу/перехоплення)."""
    blocked = _require_bot_json(request)
    if blocked:
        return blocked
    from .models import IgClient

    c = IgClient.objects.filter(id=client_id).first()
    if not c:
        return JsonResponse({"success": False, "error": "Клієнта не знайдено."}, status=404)
    c.bot_paused = False
    c.manager_takeover = False
    c.paused_reason = ""
    c.save(update_fields=["bot_paused", "manager_takeover", "paused_reason", "updated_at"])
    return JsonResponse({"success": True, "bot_paused": False})


@login_required(login_url="management_login")
@require_POST
def bot_client_hide_api(request, client_id):
    blocked = _require_bot_json(request)
    if blocked:
        return blocked
    from django.utils import timezone

    from .models import IgClient, InstagramBotMessage
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
        c.hidden_reason = (request.POST.get("reason") or "manual")[:255]
        c.save(update_fields=[
            "automation_lease_token", "automation_lease_until",
            "hidden_at", "hidden_reason", "updated_at",
        ])
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
    blocked = _require_bot_json(request)
    if blocked:
        return blocked
    from .models import IgClient

    with transaction.atomic():
        c = IgClient.objects.select_for_update().filter(id=client_id).first()
        if not c:
            return JsonResponse({"success": False, "error": "Клієнта не знайдено."}, status=404)
        c.hidden_at = None
        c.hidden_reason = ""
        c.save(update_fields=["hidden_at", "hidden_reason", "updated_at"])
    return JsonResponse({
        "success": True,
        "hidden": False,
        "message": "Клієнта повернено до активного списку.",
    })


@login_required(login_url="management_login")
@require_POST
def bot_client_mark_lost_api(request, client_id):
    blocked = _require_bot_json(request)
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
@require_GET
def bot_stats_api(request):
    blocked = _require_bot_json(request)
    if blocked:
        return blocked
    from datetime import timedelta

    from .models import IgClient, IgConversationSignal, IgDeal, IgFollowUpTask

    try:
        range_days = int(request.GET.get("days") or 0)
    except (TypeError, ValueError):
        range_days = 0
    if range_days not in {0, 1, 7, 30}:
        range_days = 0
    since = None
    if range_days == 1:
        local_now = timezone.localtime()
        since = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif range_days:
        since = timezone.now() - timedelta(days=range_days)

    active_clients = IgClient.objects.filter(hidden_at__isnull=True)
    if since:
        active_clients = active_clients.filter(
            Q(last_message_at__gte=since)
            | Q(last_message_at__isnull=True, created_at__gte=since)
        )
    conversations = active_clients.count()
    stage_counts = {
        row["stage"]: row["count"]
        for row in active_clients.values("stage").annotate(count=Count("id")).order_by()
    }
    signal_qs = IgConversationSignal.objects.filter(client__hidden_at__isnull=True)
    if since:
        signal_qs = signal_qs.filter(created_at__gte=since)
    signals = {
        row["signal_type"]: row["count"]
        for row in signal_qs.values("signal_type").annotate(count=Count("id")).order_by()
    }
    objections = {
        row["primary_objection"]: row["count"]
        for row in active_clients.exclude(primary_objection=IgClient.Objection.NONE)
        .values("primary_objection").annotate(count=Count("id")).order_by()
    }
    # Keep signal names too; the frontend can show both high-level client state
    # and granular event breakdown.
    objections.update({k: v for k, v in signals.items() if "objection" in k or k in {"no_reply", "lost"}})
    product_interest = [
        {
            "product_id": row["current_product_id"],
            "product_title": row["current_product__title"] or "",
            "count": row["count"],
        }
        for row in active_clients.exclude(current_product__isnull=True)
        .values("current_product_id", "current_product__title").annotate(count=Count("id"))
        .order_by("-count")[:25]
    ]
    revenue_filter = Q(deals__status__in=[IgDeal.Status.PAID, IgDeal.Status.ORDER_CREATED])
    if since:
        revenue_filter &= (
            Q(deals__paid_at__gte=since)
            | Q(deals__paid_at__isnull=True, deals__created_at__gte=since)
        )
    ad_rows = []
    for row in (
        active_clients.exclude(Q(ad_id="") & Q(ad_ref="") & Q(ad_title=""))
        .values("ad_id", "ad_ref", "ad_title")
        .annotate(
            chats=Count("id", distinct=True),
            paid=Count(
                "id",
                filter=Q(stage__in=[
                    IgClient.Stage.PAID,
                    IgClient.Stage.ORDER_CREATED,
                    IgClient.Stage.DONE,
                ]),
                distinct=True,
            ),
            revenue=Sum("deals__amount", filter=revenue_filter),
        )
        .order_by("-chats")[:50]
    ):
        ad_rows.append({
            "ad_id": row["ad_id"],
            "ad_ref": row["ad_ref"],
            "ad_title": row["ad_title"],
            "chats": row["chats"],
            "paid": row["paid"],
            "revenue": str(row["revenue"] or 0),
        })
    totals = {
        "conversations": conversations,
        "qualified": active_clients.filter(buying_readiness__gte=40).count(),
        "product_matched": active_clients.filter(current_product__isnull=False).count(),
        "checkout_or_payment": active_clients.filter(stage__in=[IgClient.Stage.CHECKOUT, IgClient.Stage.PAYMENT_PENDING]).count(),
        "paid": active_clients.filter(stage__in=[IgClient.Stage.PAID, IgClient.Stage.ORDER_CREATED, IgClient.Stage.DONE]).count(),
        "hidden": IgClient.objects.filter(hidden_at__isnull=False).count(),
        "pending_followups": IgFollowUpTask.objects.filter(status=IgFollowUpTask.Status.PENDING, client__hidden_at__isnull=True).count(),
        "followup_recoveries": IgFollowUpTask.objects.filter(status=IgFollowUpTask.Status.SENT, client__hidden_at__isnull=True, client__stage__in=[IgClient.Stage.PAID, IgClient.Stage.ORDER_CREATED, IgClient.Stage.DONE]).count(),
        "discount_conversions": active_clients.filter(discount_offered_percent__gt=0, stage__in=[IgClient.Stage.PAID, IgClient.Stage.ORDER_CREATED, IgClient.Stage.DONE]).count(),
        "manager_takeovers": active_clients.filter(manager_takeover=True).count(),
        "custom_print_handoffs": active_clients.filter(intent=IgClient.Intent.CUSTOM_PRINT, stage=IgClient.Stage.LEAD_TO_MANAGER).count(),
    }
    return JsonResponse({
        "success": True,
        "range_days": range_days,
        "range_from": since.isoformat() if since else "",
        "totals": totals,
        "stages": stage_counts,
        "objections": objections,
        "signals": signals,
        "products": product_interest,
        "ads": ad_rows,
    })


# ---------------------------------------------------------------------------
# Інструкції / швидкі посилання / реклама — CRUD у вкладці «Бот» (Task 23)
# ---------------------------------------------------------------------------
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "on", "yes"}


@login_required(login_url="management_login")
@require_GET
def bot_kb_api(request):
    blocked = _require_admin_json(request)
    if blocked:
        return blocked
    from .models import BotAdCampaign, BotInstruction, BotQuickLink

    instructions = [
        {"id": i.id, "title": i.title, "body": i.body, "intent_tags": i.intent_tags,
         "is_active": i.is_active, "priority": i.priority}
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
    })


@login_required(login_url="management_login")
@require_POST
def bot_kb_save_api(request):
    blocked = _require_admin_json(request)
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

    if op == "delete":
        if obj_id:
            model.objects.filter(id=obj_id).delete()
        return JsonResponse({"success": True})

    obj = model.objects.filter(id=obj_id).first() if obj_id else model()
    p = request.POST
    if kind == "instruction":
        obj.title = (p.get("title") or "")[:200]
        obj.body = p.get("body") or ""
        obj.intent_tags = (p.get("intent_tags") or "")[:400]
        obj.is_active = _truthy(p.get("is_active", "1"))
        try:
            obj.priority = int(p.get("priority") or 100)
        except (TypeError, ValueError):
            obj.priority = 100
    elif kind == "quicklink":
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
    return JsonResponse({"success": True, "id": obj.id})
