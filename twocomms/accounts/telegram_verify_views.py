"""
Універсальні endpoints для верифікації / привʼязки / логіну через Telegram.

Сценарій (єдиний для всіх):
    1. JS на клієнті викликає POST /accounts/telegram-verify/start/?purpose=...
    2. Сервер створює `TelegramVerificationSession`, віддає `deep_link_url` на бота.
    3. Користувач у боті тапає «📱 Поділитися номером».
    4. Бот зберігає phone+telegram_id у сесії (статус verified).
    5. JS поллить /accounts/telegram-verify/status/?token=... → бачить `verified`.
    6. Якщо purpose=login — JS викликає POST /accounts/telegram-login/complete/.
       Якщо purpose=profile_link / dropshipper_link — сервер уже привʼязав
       UserProfile (у `process_contact_message`), а UI просто показує success.

Підтримувані purpose:
    - profile_link        — привʼязка Telegram до профілю авторизованого юзера.
    - login               — вхід / реєстрація через Telegram.
    - dropshipper_link    — привʼязка телеграма дропшипера.
    - management_bind     — привʼязка менеджмент-бота (інший бот; верифікація
                            робиться через основний бот, але далі сервер кладе
                            chat_id в спеціальне поле).
    - custom_print        — legacy (форма кастомного принта). Підтримується
                            окремими endpoints у storefront.views.telegram_verify
                            заради backward compat.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from datetime import timedelta
from urllib.parse import quote

import requests
from django.conf import settings
from django.contrib.auth import login as auth_login
from django.contrib.auth.models import User
from django.db import transaction
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from .models import TelegramVerificationSession, UserProfile


logger = logging.getLogger(__name__)


VERIFICATION_TTL_SECONDS = 5 * 60
MAX_ACTIVE_SESSIONS_PER_KEY = 5


_BOT_USERNAME_CACHE = {"value": "", "fetched_at": 0.0, "token": ""}


# ──────────────────────────────────────────────────────────────────────
# Helpers (shared)
# ──────────────────────────────────────────────────────────────────────


def _get_bot_token() -> str:
    return (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or getattr(settings, "TELEGRAM_BOT_TOKEN", "")
        or ""
    )


def get_bot_username() -> str:
    """Повертає username основного бота. Кешує на 10 хв."""
    name = (
        os.environ.get("TELEGRAM_BOT_USERNAME")
        or getattr(settings, "TELEGRAM_BOT_USERNAME", "")
        or ""
    ).strip().lstrip("@")
    if name:
        return name

    token = _get_bot_token()
    if not token:
        return ""

    now = time.time()
    cache = _BOT_USERNAME_CACHE
    if cache["value"] and cache["token"] == token and now - cache["fetched_at"] < 600:
        return cache["value"]

    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=4)
        data = resp.json()
        if data.get("ok") and data.get("result", {}).get("username"):
            username = data["result"]["username"].lstrip("@")
            _BOT_USERNAME_CACHE.update({"value": username, "fetched_at": now, "token": token})
            return username
    except Exception as exc:
        logger.warning("getMe failed: %s", exc)
    return ""


def _ensure_session_key(request) -> str:
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key or ""


def _expire_old_sessions(session_key: str):
    now = timezone.now()
    TelegramVerificationSession.objects.filter(
        status__in=[
            TelegramVerificationSession.STATUS_PENDING,
            TelegramVerificationSession.STATUS_BOT_OPENED,
        ],
        expires_at__lt=now,
    ).update(status=TelegramVerificationSession.STATUS_EXPIRED)

    if session_key:
        active_qs = (
            TelegramVerificationSession.objects.filter(
                session_key=session_key,
                status__in=[
                    TelegramVerificationSession.STATUS_PENDING,
                    TelegramVerificationSession.STATUS_BOT_OPENED,
                ],
            )
            .order_by("-created_at")
        )
        excess = list(active_qs[MAX_ACTIVE_SESSIONS_PER_KEY:])
        if excess:
            ids = [obj.pk for obj in excess]
            TelegramVerificationSession.objects.filter(pk__in=ids).update(
                status=TelegramVerificationSession.STATUS_CANCELLED,
            )


# Зберігаємо як рядкові літерали (а не як атрибути класу), щоб уникнути
# проблем з порядком імпорту модулів і кешуванням .pyc на проді.
_ALLOWED_PURPOSES = {
    "profile_link",
    "login",
    "dropshipper_link",
    "management_bind",
    "restock",
}


def _read_payload(request) -> dict:
    payload = {}
    try:
        if request.content_type and "application/json" in request.content_type:
            payload = json.loads((request.body or b"{}").decode("utf-8") or "{}")
        else:
            payload = {k: v for k, v in request.POST.items()}
    except Exception:
        payload = {}
    return payload or {}


# ──────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────


@csrf_protect
@require_POST
def telegram_verify_start(request):
    """Створює нову сесію верифікації Telegram-контакту.

    POST body (JSON або form):
        - purpose: profile_link | login | dropshipper_link | management_bind
        - next: URL для redirect після login (тільки для purpose=login)
        - bind_code: для management_bind (не обовʼязково — сервер сам згенерує)
    """
    bot_token = _get_bot_token()
    if not bot_token:
        return JsonResponse(
            {"ok": False, "error": "Telegram-бот не налаштований."},
            status=503,
        )

    bot_username = get_bot_username()
    if not bot_username:
        return JsonResponse(
            {"ok": False, "error": "Не вдалося визначити username бота."},
            status=503,
        )

    payload = _read_payload(request)
    purpose = (payload.get("purpose") or "").strip().lower()
    if purpose not in _ALLOWED_PURPOSES:
        return JsonResponse({"ok": False, "error": "Невідомий purpose."}, status=400)

    # Авторизаційні гарантії
    if purpose in {"profile_link", "dropshipper_link", "management_bind"}:
        if not request.user.is_authenticated:
            return JsonResponse(
                {"ok": False, "error": "Потрібна авторизація."}, status=401
            )

    if purpose == "login" and request.user.is_authenticated:
        return JsonResponse(
            {"ok": False, "error": "Ви вже авторизовані. Перезавантажте сторінку."},
            status=400,
        )

    session_key = _ensure_session_key(request)
    _expire_old_sessions(session_key)

    metadata = {}
    next_url = (payload.get("next") or "").strip()
    if next_url:
        metadata["next"] = next_url[:512]
    bind_code = (payload.get("bind_code") or "").strip()
    if bind_code:
        metadata["bind_code"] = bind_code[:64]
    if purpose == "restock":
        try:
            restock_id = int(payload.get("restock_id"))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Невідома заявка."}, status=400)
        from storefront.models import RestockSubscription

        subscription = RestockSubscription.objects.filter(
            pk=restock_id,
            channel=RestockSubscription.Channel.TELEGRAM,
            status=RestockSubscription.Status.DRAFT,
            browser_session_key=session_key,
        ).first()
        if subscription is None:
            return JsonResponse({"ok": False, "error": "Невідома заявка."}, status=404)
        metadata["restock_id"] = subscription.pk

    token = secrets.token_urlsafe(24)
    now = timezone.now()
    expires_at = now + timedelta(seconds=VERIFICATION_TTL_SECONDS)

    session = TelegramVerificationSession.objects.create(
        token=token,
        purpose=purpose,
        status=TelegramVerificationSession.STATUS_PENDING,
        session_key=session_key,
        user=request.user if request.user.is_authenticated else None,
        expires_at=expires_at,
        metadata=metadata,
    )

    deep_link_url = f"https://t.me/{bot_username}?start=verify_{quote(token)}"
    deep_link_app = f"tg://resolve?domain={bot_username}&start=verify_{quote(token)}"

    return JsonResponse(
        {
            "ok": True,
            "token": session.token,
            "purpose": purpose,
            "expires_at": session.expires_at.isoformat(),
            "ttl_seconds": VERIFICATION_TTL_SECONDS,
            "deep_link_url": deep_link_url,
            "deep_link_app": deep_link_app,
            "bot_username": bot_username,
        }
    )


@require_GET
def telegram_verify_status(request):
    """Повертає поточний статус сесії."""
    token = (request.GET.get("token") or "").strip()
    if not token:
        return JsonResponse({"ok": False, "error": "missing token"}, status=400)

    session = TelegramVerificationSession.objects.filter(token=token).first()
    if not session:
        return JsonResponse({"ok": False, "error": "session not found"}, status=404)

    session_key = request.session.session_key or ""
    own_session = (
        (session.session_key and session.session_key == session_key)
        or (
            session.user_id
            and request.user.is_authenticated
            and session.user_id == request.user.id
        )
    )
    if not own_session and not session.is_verified:
        return JsonResponse(
            {"ok": True, "status": session.status, "verified": session.is_verified}
        )

    if session.is_expired and session.status in {
        TelegramVerificationSession.STATUS_PENDING,
        TelegramVerificationSession.STATUS_BOT_OPENED,
    }:
        session.status = TelegramVerificationSession.STATUS_EXPIRED
        session.save(update_fields=["status"])

    session.mark_polled()

    payload = session.to_public_dict()
    payload["purpose"] = session.purpose
    return JsonResponse({"ok": True, **payload})


@csrf_protect
@require_POST
def telegram_verify_cancel(request):
    """Скасовує сесію."""
    payload = _read_payload(request)
    token = (payload.get("token") or "").strip()
    if not token:
        return JsonResponse({"ok": False, "error": "missing token"}, status=400)

    session_key = request.session.session_key or ""
    session = TelegramVerificationSession.objects.filter(token=token).first()
    if not session:
        return JsonResponse({"ok": True, "skipped": True})

    own = (
        (session.session_key and session.session_key == session_key)
        or (
            session.user_id
            and request.user.is_authenticated
            and session.user_id == request.user.id
        )
    )
    if not own:
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    if session.status in {
        TelegramVerificationSession.STATUS_PENDING,
        TelegramVerificationSession.STATUS_BOT_OPENED,
    }:
        session.status = TelegramVerificationSession.STATUS_CANCELLED
        session.save(update_fields=["status"])
    return JsonResponse({"ok": True})


# ──────────────────────────────────────────────────────────────────────
# Login flow (purpose = login)
# ──────────────────────────────────────────────────────────────────────


def _generate_unique_username(base: str) -> str:
    base = (base or "").strip().lstrip("@")
    base = "".join(c for c in base if c.isalnum() or c in "_-").lower() or "user"
    if not User.objects.filter(username=base).exists():
        return base
    suffix = get_random_string(4, allowed_chars="0123456789abcdef")
    candidate = f"{base}_{suffix}"
    while User.objects.filter(username=candidate).exists():
        suffix = get_random_string(4, allowed_chars="0123456789abcdef")
        candidate = f"{base}_{suffix}"
    return candidate


def _resolve_or_create_user(session: TelegramVerificationSession):
    """Знаходить existing user за telegram_id, або створює нового.

    Повертає кортеж ``(user, created)``, де ``created`` == True означає, що
    це нова реєстрація (а не вхід наявного користувача). Це потрібно для
    коректної аналітики: CompleteRegistration шлемо лише для нових акаунтів.
    """
    tg_id = session.telegram_user_id
    phone = (session.phone or "").strip()
    username = (session.telegram_username or "").lstrip("@")
    first_name = (session.telegram_first_name or "").strip()
    last_name = (session.telegram_last_name or "").strip()

    profile = None
    if tg_id:
        profile = UserProfile.objects.filter(telegram_id=tg_id).first()
    if profile:
        # Освіжаємо phone/telegram username, якщо порожні
        updates = []
        if phone and not profile.phone:
            profile.phone = phone
            updates.append("phone")
        if username and (not profile.telegram or not profile.telegram.lstrip("@")):
            profile.telegram = f"@{username}"
            updates.append("telegram")
        if updates:
            profile.save(update_fields=updates)
        return profile.user, False

    # Шукаємо за phone (якщо є). Це дозволяє «зливати» вхід через TG з ручним
    # акаунтом, де користувач уже вказав цей телефон.
    if phone:
        profile = UserProfile.objects.filter(phone=phone).first()
        if profile and not profile.telegram_id:
            profile.telegram_id = tg_id
            if username and not profile.telegram:
                profile.telegram = f"@{username}"
            profile.save(update_fields=["telegram_id", "telegram"])
            return profile.user, False

    # Створюємо нового user. Username — базується на telegram username або id.
    base_username = username or (f"tg{tg_id}" if tg_id else "tguser")
    new_username = _generate_unique_username(base_username)

    with transaction.atomic():
        user = User.objects.create_user(
            username=new_username,
            email="",
            password=None,  # без пароля; пізніше можна установити в профілі
        )
        user.first_name = first_name[:30]
        user.last_name = last_name[:30]
        user.save(update_fields=["first_name", "last_name"])

        profile, _ = UserProfile.objects.get_or_create(user=user)
        if tg_id:
            profile.telegram_id = tg_id
        if username:
            profile.telegram = f"@{username}"
        if phone and not profile.phone:
            profile.phone = phone
        profile.save()

    return user, True


@csrf_protect
@require_POST
def telegram_login_complete(request):
    """Виконує логін за token TelegramVerificationSession (purpose=login)."""
    if request.user.is_authenticated:
        return JsonResponse(
            {"ok": False, "error": "already_authenticated"}, status=400
        )

    payload = _read_payload(request)
    token = (payload.get("token") or "").strip()
    if not token:
        return JsonResponse({"ok": False, "error": "missing token"}, status=400)

    session = TelegramVerificationSession.objects.filter(token=token).first()
    if not session:
        return JsonResponse({"ok": False, "error": "session not found"}, status=404)
    if session.purpose != "login":
        return JsonResponse({"ok": False, "error": "wrong purpose"}, status=400)
    if not session.is_verified:
        return JsonResponse({"ok": False, "error": "not verified yet"}, status=409)
    if session.consumed_at:
        return JsonResponse({"ok": False, "error": "already used"}, status=410)

    session_key = request.session.session_key or ""
    if session.session_key and session.session_key != session_key:
        return JsonResponse({"ok": False, "error": "session mismatch"}, status=403)

    user, created = _resolve_or_create_user(session)
    auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")

    session.resolved_user = user
    session.consumed_at = timezone.now()
    session.save(update_fields=["resolved_user", "consumed_at"])

    next_url = ""
    try:
        if isinstance(session.metadata, dict):
            next_url = session.metadata.get("next") or ""
    except Exception:
        next_url = ""

    if not next_url or next_url.startswith(("http://", "https://", "//")):
        # Не дозволяємо external redirect
        next_url = ""
    if not next_url:
        try:
            next_url = reverse("profile_setup")
        except Exception:
            next_url = "/"

    return JsonResponse(
        {
            "ok": True,
            "redirect": next_url,
            "username": user.username,
            "created": bool(created),
        }
    )
