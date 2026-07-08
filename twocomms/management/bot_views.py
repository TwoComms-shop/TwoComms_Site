"""
Вкладка «Бот» (адміністратори + обмежений Meta reviewer).

UI зі станом агента (запущено/зупинено, очікує повідомлення), кнопками
Start/Stop, вибором джерела ключів і онлайн-консоллю подій.
"""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

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
            s.custom_direct_token = (request.POST.get("custom_direct_token") or "").strip()
        if "custom_gemini_key" in request.POST:
            s.custom_gemini_key = (request.POST.get("custom_gemini_key") or "").strip()

        trigger = (request.POST.get("trigger_text") or "").strip()
        if trigger:
            s.trigger_text = trigger[:255]
        reply = (request.POST.get("reply_text") or "").strip()
        if reply:
            s.reply_text = reply[:1000]

    # AI-режим / модель / правило / білий список.
    s.ai_enabled = (request.POST.get("ai_enabled") or "").strip() in {"1", "true", "on", "yes"}
    s.receive_via_poll = (request.POST.get("receive_via_poll") or "").strip() in {"1", "true", "on", "yes"}
    model = (request.POST.get("gemini_model") or "").strip()
    if model:
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
    }


@login_required(login_url="management_login")
@require_GET
def bot_clients_api(request):
    blocked = _require_bot_json(request)
    if blocked:
        return blocked
    from django.db.models import Q

    from .models import IgClient

    qs = IgClient.objects.all().order_by("-last_message_at", "-id")
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
    })
    return JsonResponse({
        "success": True,
        "client": card,
        "messages": messages,
        "last_message_id": last_message_id,
        "events": events,
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
