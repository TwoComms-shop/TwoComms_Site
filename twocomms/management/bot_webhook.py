"""
Instagram webhook приймач TwoComms (event-driven).

- GET  /bot/webhook/  — верифікація підписки Meta (echo hub.challenge).
- POST /bot/webhook/  — перевіряє підпис X-Hub-Signature-256, кладе вхідні в
  чергу і ВІДРАЗУ повертає 200 (best practice). Обробку (Gemini+Send) робить
  воркер-демон; додатково тут стартує фоновий потік, щоб відповісти миттєво,
  не чекаючи циклу демона. Дедуп за mid гарантує відсутність подвійних
  відповідей між потоком і демоном.

Verify token і APP_SECRET — лише з ENV (IG_BOT_VERIFY_TOKEN, IG_APP_SECRET).
"""
import json
import logging
import os
import threading

from django.core.cache import cache
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from management.models import InstagramBotSettings
from management.services import instagram_bot as bot

logger = logging.getLogger("ig_bot")
_CONFIG_WARNING_LOCK = threading.Lock()
_CONFIG_WARNING_EMITTED = False


def _verify_token() -> str:
    return os.environ.get("IG_BOT_VERIFY_TOKEN", "").strip()


def _process_async():
    """Обробити чергу у фоновому потоці (швидка відповідь, не блокує 200)."""
    try:
        from django.db import close_old_connections

        close_old_connections()
        bot.process_pending(InstagramBotSettings.load())
    except Exception:
        logger.exception("ig_bot: async process error")
    finally:
        try:
            from django.db import close_old_connections

            close_old_connections()
        except Exception:
            pass


def _warn_signature_configuration_once():
    """Record one bounded warning when production cannot verify signatures."""
    global _CONFIG_WARNING_EMITTED
    if bot.webhook_signature_status()["healthy"]:
        return
    with _CONFIG_WARNING_LOCK:
        if _CONFIG_WARNING_EMITTED:
            return
        try:
            if not cache.add("ig_bot_webhook_signature_warning", 1, 3600):
                _CONFIG_WARNING_EMITTED = True
                return
        except Exception:
            pass
        _CONFIG_WARNING_EMITTED = True
    bot.log("warning", "no_app_secret", "IG_APP_SECRET не заданий — підпис webhook відхиляється")


@csrf_exempt
def ig_webhook(request):
    if request.method == "GET":
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge", "")
        expected = _verify_token()
        if mode == "subscribe" and expected and token == expected:
            return HttpResponse(challenge, content_type="text/plain")
        return HttpResponse("forbidden", status=403)

    if request.method == "POST":
        raw = request.body  # bytes — потрібні для перевірки підпису
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not bot.verify_signature(raw, sig):
            _warn_signature_configuration_once()
            logger.warning("ig_bot: bad signature")
            bot.log("warning", "bad_signature", "Невірний підпис webhook — відхилено")
            return HttpResponse("forbidden", status=403)

        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return HttpResponse("ok")  # все одно 200, щоб Meta не ретраїла

        try:
            settings_obj = InstagramBotSettings.load()
            # Phase 0 / Task 1 — сире логування подій (діагностика форматів).
            try:
                bot.record_raw_event(payload)
            except Exception:
                logger.exception("ig_bot: record_raw_event error")
            enq = bot.handle_webhook_payload(settings_obj, payload)
            if enq:
                # миттєва обробка у фоні, не блокуючи відповідь 200
                threading.Thread(target=_process_async, daemon=True).start()
        except Exception:
            logger.exception("ig_bot: webhook handler error")

        # ВІДРАЗУ 200 — головна вимога Meta (інакше повторні доставки).
        return HttpResponse("ok")

    return HttpResponse(status=405)
