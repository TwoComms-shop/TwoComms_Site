"""
Сервіс Instagram Direct бота TwoComms (event-driven).

Архітектура (best practice для хостингу без Celery):
- Webhook (подія) — приймає вхідне, перевіряє підпис, кладе в чергу
  (InstagramBotMessage, status=pending) і ВІДРАЗУ повертає 200. Жодної важкої
  логіки в запиті.
- Воркер (демон run_instagram_bot --forever) — забирає pending із черги,
  будує контекст з ЛОКАЛЬНОЇ історії (без read-запитів до IG), генерує
  відповідь Gemini і відправляє через Send API. Ретраї, дедуп.
- Поллінг IG — лише резервний міст до Live (receive_via_poll). Після Live
  його вимикають → бот суто event-driven, read-запитів до IG немає.

Відповідь: AI (Gemini, history+system_prompt) або простий trigger->reply.
Захист: allowed_senders (білий список), дедуп за mid, перевірка підпису
X-Hub-Signature-256 (IG_APP_SECRET), is_enabled-гейт.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.core.cache import cache
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from management.models import (
    IgClient,
    IgBotNotification,
    IgConversationAnalysisJob,
    IgPollCursor,
    InstagramBotLog,
    InstagramBotMessage,
    InstagramBotSettings,
)

GRAPH_VERSION = "v25.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
GENAI = "https://generativelanguage.googleapis.com/v1beta"

LOG_KEEP_ROWS = 500
HISTORY_LIMIT = 12          # скільки останніх реплік даємо моделі
MAX_ATTEMPTS = 3            # ретраї обробки одного повідомлення
PAGE_TOKEN_TTL = 1200
HTTP_TIMEOUT = 12
CONV_LIST_TIMEOUT = 30
CONV_PAGE_LIMIT = 100
CONV_MAX_PAGES = 10
CONV_MAX_IDS = 500
CONV_MIN_INTERVAL = 0.5  # Meta Conversations API: at most 2 requests/second.
CONV_CACHE_TTL = 3600
CONV_REFRESH_LOCK_TTL = CONV_LIST_TIMEOUT * CONV_MAX_PAGES + 30
_CONV_ID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,255}$")
_GRAPH_VERSION_PATH_RE = re.compile(r"^/v\d+(?:\.\d+)?(?:/|$)")
POLL_MESSAGE_TIMEOUT = 5
POLL_MESSAGE_MAX_PAGES = 5
POLL_MAX_REQUESTS = 40
POLL_MAX_SECONDS = 20
MSG_KEEP_ROWS = 2000        # підрізання історії
AUTOMATION_LEASE_TTL = timedelta(minutes=3)

# Керуючі теги, які модель може додавати у відповідь (вирізаються перед
# відправкою клієнту). [STAGE:x] просуває воронку, [MANAGER] кличе людину.
STAGE_VALUES = {s.value for s in IgClient.Stage}
MODEL_HARD_STAGES = {
    IgClient.Stage.PAID,
    IgClient.Stage.ORDER_CREATED,
    IgClient.Stage.DONE,
}
_CONTROL_TAG_RE = re.compile(r"\[([A-Z]+)(?::([^\]]+))?\]")
_SECRET_PARAM_RE = re.compile(
    r"((?:access_token|client_secret|api[_-]?key|password|token)=)[^&\s]+",
    re.IGNORECASE,
)


def _redact_secret_text(value: str) -> str:
    """Remove credential-like query parameters before writing diagnostics."""
    return _SECRET_PARAM_RE.sub(r"\1[REDACTED]", str(value or ""))


def _extract_control(reply: str) -> tuple[str, dict]:
    """Витягує керуючі теги ([MANAGER], [STAGE:x], [SPAM], [PAYLINK:x], [ORDER])
    з відповіді моделі. Повертає (очищений_текст, {tag_lower: value|True}).
    Кирилічні дужки [текст] не чіпаються (матчимо лише латиницю у верхньому регістрі)."""
    tags: dict = {}
    if not reply:
        return reply, tags
    for m in _CONTROL_TAG_RE.finditer(reply):
        name = m.group(1).lower()
        val = (m.group(2) or "").strip().lower()
        tags[name] = val or True
    clean = _CONTROL_TAG_RE.sub("", reply)
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, tags


def _apply_stage(client, stage_value) -> bool:
    """Apply only model-authorized workflow stages.

    Payment and fulfilment stages belong exclusively to verified provider/order
    services.  The model may neither claim them nor regress an existing hard
    stage through a generated control tag.
    """
    if not client or not stage_value or not isinstance(stage_value, str):
        return False
    if stage_value not in STAGE_VALUES:
        return False
    if stage_value in MODEL_HARD_STAGES or client.stage in MODEL_HARD_STAGES:
        return False
    if client.stage == stage_value:
        return False
    try:
        client.set_stage(stage_value, reason="bot")
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Модерація діалогу: стоп/старт, антиспам, перехоплення менеджером (Phase 7)
# ---------------------------------------------------------------------------
SPAM_STRIKES_LIMIT = 3
PHONE_RE = re.compile(r"(?:\+?38)?0\d{9}")


def _client_blocked(client) -> bool:
    """Бот не відповідає, якщо клієнта поставлено на паузу або заблоковано."""
    active_opt_out = bool(
        client
        and client.opted_out_at
        and (not client.opted_in_at or client.opted_in_at < client.opted_out_at)
    )
    return bool(
        client
        and (client.bot_paused or client.is_blocked or client.hidden_at or active_opt_out)
    )


def _register_spam(client) -> bool:
    """+1 спам-страйк. На SPAM_STRIKES_LIMIT — пауза + стадія SPAM + сповіщення.
    Повертає True, якщо клієнта заблоковано цим страйком."""
    client.spam_strikes = (client.spam_strikes or 0) + 1
    fields = ["spam_strikes", "updated_at"]
    blocked = client.spam_strikes >= SPAM_STRIKES_LIMIT
    if blocked:
        client.bot_paused = True
        client.reply_permission_epoch = int(client.reply_permission_epoch or 0) + 1
        client.paused_reason = "spam"
        client.paused_at = timezone.now()
        fields += ["bot_paused", "reply_permission_epoch", "paused_reason", "paused_at"]
    client.save(update_fields=fields)
    if blocked:
        try:
            client.set_stage(IgClient.Stage.SPAM, reason="spam")
        except Exception:
            pass
        notify_manager(
            f"🚫 IG: клієнт {client.username or client.igsid} заблокований "
            f"(3 спам-страйки). Бот зупинено для нього."
        )
        log("warning", "spam_block", f"{client.igsid}: 3 страйки → пауза")
    return blocked


def _maybe_capture_phone(client, text: str) -> bool:
    """Якщо у клієнта ще немає телефону, а в тексті є український номер — зберігає."""
    if not client or client.phone:
        return False
    cleaned = (text or "").replace(" ", "").replace("-", "")
    m = PHONE_RE.search(cleaned)
    if not m:
        return False
    try:
        from management.models import normalize_phone

        if not normalize_phone(m.group(0)):
            return False
    except Exception:
        pass
    client.phone = m.group(0)
    client.save(update_fields=["phone", "phone_normalized", "updated_at"])
    return True


def _bot_sent_key(recipient_id: str, text: str) -> str:
    norm = " ".join((text or "").lower().split())
    h = hashlib.md5((str(recipient_id) + "|" + norm).encode("utf-8")).hexdigest()[:16]
    return "ig_bot_sent:" + h


def _mark_bot_sent(recipient_id: str, text: str) -> None:
    """Позначає текст, який бот шле конкретному отримувачу — щоб відрізнити від
    відлуння повідомлення менеджера (echo). Привʼязка до отримувача прибирає
    хибні збіги між клієнтами з однаковим текстом."""
    try:
        cache.set(_bot_sent_key(recipient_id, text), 1, 1800)
    except Exception:
        pass


def _looks_like_contact_info(text: str) -> bool:
    """Евристика: схоже на контактні дані (телефон / адреса Нової Пошти)."""
    raw = (text or "")
    if PHONE_RE.search(raw.replace(" ", "").replace("-", "")):
        return True
    low = raw.lower()
    keys = ("відділенн", "поштомат", "нова пошта", "новапошта", "нп ", "індекс", "вул.", "вулиц", "м. ")
    return any(k in low for k in keys)


PAYLINK_PHRASES = (
    "посилання на оплат", "посилання на передоплат", "сформую посилання",
    "сформувати посилання", "формую посилання", "ось посилання", "ось пряме посилання",
    "тримай посилання", "надішлю посилання", "надсилаю посилання", "лінк на оплат",
    "ссылка на оплат", "ссылку на оплат", "ссылка на предоплат", "ссылку на предоплат",
    "сформирую ссылку", "вот ссылка", "вот ссылку", "держи ссылку",
)


def _wants_paylink(reply: str, control: dict) -> tuple[bool, str]:
    """Чи треба сформувати посилання на оплату і який тип (full/prepay).
    Тригер: тег [PAYLINK:x] АБО обіцянка посилання у тексті бота (фолбек, якщо
    модель забула тег). Тип беремо з тегу, інакше визначаємо за словом «передопл»."""
    val = control.get("paylink")
    low = (reply or "").lower()
    if val or any(ph in low for ph in PAYLINK_PHRASES):
        if isinstance(val, str) and val in ("full", "prepay"):
            return True, val
        pt = "prepay" if ("передопл" in low or "предопл" in low) else "full"
        return True, pt
    return False, "full"


# Монобанк-подібні URL — щоб прибрати вигадане моделлю платіжне посилання й
# лишити лише реальний invoice. Товарні/каталожні URL (twocomms.shop) не чіпаємо.
_PAY_URL_RE = re.compile(r"https?://[^\s]*(?:mbnk|monobank)[^\s]*", re.I)

# Безпечний холдер, коли лінк не вдалось сформувати: НЕ лишаємо клієнта з
# висячою обіцянкою «ось посилання», а м'яко тримаємо діалог поки підключиться
# менеджер (його одночасно сповіщаємо).
PAYLINK_FALLBACK_TEXT = "Дякую! Уточню деталі щодо оплати і за мить повернуся до вас 🙌"

# Протокол оплати — інжектимо в system_instruction завжди (migration-free), щоб
# модель давала ЯВНИЙ сигнал товару й типу оплати, а не лише обіцяла лінк текстом.
# Не чіпаємо DEFAULT_BOT_SYSTEM_PROMPT (щоб не робити міграцію й не затирати
# правки адміна в UI) — інжект застосовується до будь-яких налаштувань.
PAYMENT_PROTOCOL_NOTE = (
    "[ПРОТОКОЛ ОПЛАТИ — службове, клієнт цього не бачить]\n"
    "Коли клієнт підтвердив КОНКРЕТНИЙ товар і готовий платити — додай у самому "
    "кінці відповіді службові теги: [PAYLINK:prepay] (передоплата 200 грн) або "
    "[PAYLINK:full] (повна оплата), і поряд [PRODUCT:<id>], де <id> — число з "
    "рядка каталогу (формат «id=NN»). НЕ вигадуй і НЕ пиши URL оплати власноруч — "
    "система сама сформує справжнє посилання й додасть його до повідомлення. "
    "Якщо товар ще не визначено однозначно — спершу уточни його, тег [PAYLINK] "
    "поки не став."
)

# Правило точності — інжектимо разом із протоколом оплати. Прямо забороняє
# «вигадану відмову» (типу «такого немає / це кастом»), як це сталось із реальним
# товаром «Харків Edition».
ANTI_HALLUCINATION_NOTE = (
    "[ПРАВИЛО ТОЧНОСТІ — службове]\n"
    "Ніколи не стверджуй, що товару немає або що це «кастом/під замовлення», не "
    "звіривши з каталогом нижче. Якщо точного збігу не видно — НЕ відмовляй і НЕ "
    "вигадуй: запропонуй переглянути каталог або чемно уточни деталі (тип, колір, "
    "принт, місто/напис на принті). Ціни, наявність і назви бери ЛИШЕ з каталогу."
)

SALES_AUTOMATION_GUARDRAILS = (
    "[SALES AUTOMATION GUARDRAILS — службове]\n"
    "Відповідай короткими Instagram-повідомленнями, мовою клієнта (UA/RU). "
    "Не вигадуй SKU, товар, наявність, ціну, оплату, знижку чи фінальну ціну "
    "кастомного принта. Знижку НЕ пропонуй сам: система окремо керує rescue "
    "оферами 5%, максимум 10% лише як фінальний/узгоджений варіант. Якщо клієнт "
    "каже «не буду купувати», «стоп», «не пишіть» — зроби максимум одне коротке "
    "ввічливе закриття без тиску і без повторних follow-up. Для custom print: "
    "коротко поясни, що можливий будь-який DTF-принт, ціна залежить від крою, "
    "розміру принта і готовності файлу, фінальний прорахунок робить менеджер; "
    "збери базове ТЗ і переведи в Telegram менеджера, не називаючи фінальну суму."
)


def _strip_invented_pay_urls(text: str, keep_url: str = "") -> str:
    """Прибирає будь-які платіжні URL (monobank/mbnk), КРІМ keep_url (реального).

    Захищає від ситуації, коли модель сама «вигадала» посилання на оплату —
    клієнт має отримати лише наш справжній invoice, а не фантазійний лінк.
    """
    if not text:
        return text

    def _repl(m):
        u = m.group(0)
        return u if (keep_url and u == keep_url) else ""

    out = _PAY_URL_RE.sub(_repl, text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def _rewrite_failed_paylink(reply: str) -> str:
    """Прибирає висяче обіцяння лінку (фрази-обіцянки) + вигадані платіжні URL.

    Якщо після чистки корисного тексту майже не лишилось — повертає безпечний
    холдер (PAYLINK_FALLBACK_TEXT), щоб не надсилати клієнту порожню обіцянку.
    """
    text = _strip_invented_pay_urls(reply or "", keep_url="")
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    kept = []
    for ch in chunks:
        low = ch.lower()
        if any(ph in low for ph in PAYLINK_PHRASES):
            continue
        if ch.strip():
            kept.append(ch.strip())
    cleaned = re.sub(r"[ \t]{2,}", " ", " ".join(kept)).strip()
    if len(cleaned) < 12:
        return PAYLINK_FALLBACK_TEXT
    return cleaned


def finalize_paylink(reply: str, control: dict, client, sender_id: str = "") -> str:
    """Узгоджує відповідь бота з результатом формування лінку на оплату.

    Гарантія: клієнт НІКОЛИ не отримає обіцянку «ось посилання» без самого лінку
    (це і був баг «скинув, але не скинув і чекає оплату»).

    - лінк не потрібен → reply без змін;
    - потрібен і сформований → реальний URL присутній у тексті, будь-який
      вигаданий моделлю платіжний URL прибраний;
    - потрібен, але НЕ сформований → прибирає висяче обіцяння, ставить безпечний
      холдер, кличе менеджера й піднімає стадію lead_manager.
    """
    if not reply or not client:
        return reply
    want, pt = _wants_paylink(reply, control)
    if not want:
        return reply
    from management.services import bot_orders

    try:
        res = bot_orders.create_deal_and_link(
            client, pay_type=pt, product_id=control.get("product")
        )
    except Exception as exc:
        log("error", "paylink", repr(exc))
        res = {"ok": False, "error": repr(exc)}

    if res.get("ok") and res.get("invoice_url"):
        url = res["invoice_url"]
        reply = _strip_invented_pay_urls(reply, keep_url=url)
        if url not in reply:
            reply = (reply.rstrip() + "\n\n💳 Посилання на оплату: " + url).strip()
        log("success", "paylink", f"{sender_id}: {url}")
        return reply

    # Невдача формування: прибираємо висяче обіцяння й ескалюємо на менеджера.
    log("error", "paylink", f"{sender_id}: НЕ сформовано ({res.get('error')})")
    safe = _rewrite_failed_paylink(reply)
    try:
        client.set_stage(IgClient.Stage.LEAD_TO_MANAGER, reason="paylink_failed")
    except Exception:
        pass
    try:
        notify_manager(
            f"⚠️ IG: бот обіцяв клієнту "
            f"{(client.username or client.display_name or sender_id)} посилання на "
            f"оплату, але НЕ зміг сформувати (причина: {res.get('error')}). "
            f"Підключись вручну."
        )
    except Exception:
        pass
    return safe


def _handle_echo(recipient_igsid: str, text: str) -> None:
    """Echo-подія (повідомлення, надіслане сторінкою). Якщо це НЕ власне відлуння
    бота — значить відповів живий менеджер → ставимо бота на паузу для клієнта."""
    if not recipient_igsid:
        return
    if text and cache.get(_bot_sent_key(recipient_igsid, text)):
        return  # власне відлуння бота — ігноруємо
    from management.services.ig_reply_boundary import pause_reply_boundary

    now = timezone.now()
    # The takeover notification is a state transition, not a per-message
    # event. Lock the client row so two webhook workers cannot both announce
    # the same transition while manager messages are still stored separately.
    with pause_reply_boundary():
        with transaction.atomic():
            client, _ = IgClient.objects.select_for_update().get_or_create(
                igsid=recipient_igsid,
                defaults={"first_contact_at": now, "last_message_at": now},
            )
            takeover_started = not client.manager_takeover
            client.manager_takeover = True
            client.bot_paused = True
            client.reply_permission_epoch = int(client.reply_permission_epoch or 0) + 1
            client.paused_reason = "manager_takeover"
            if takeover_started:
                client.paused_at = now
            client.last_manager_message_at = now
            update_fields = [
                "manager_takeover", "bot_paused", "paused_reason",
                "reply_permission_epoch",
                "last_manager_message_at", "updated_at",
            ]
            if takeover_started:
                update_fields.append("paused_at")
            client.save(update_fields=update_fields)
            InstagramBotMessage.objects.filter(
                client=client,
                role=InstagramBotMessage.Role.USER,
                status__in=[
                    InstagramBotMessage.Status.PENDING,
                    InstagramBotMessage.Status.PROCESSING,
                ],
            ).exclude(send_state="sending").update(
                status=InstagramBotMessage.Status.DONE,
                processed_at=now,
                processing_started_at=None,
            )
        msg = None
        if text:
            try:
                msg = InstagramBotMessage.objects.create(
                    sender_id=recipient_igsid,
                    client=client,
                    role=InstagramBotMessage.Role.MANAGER,
                    text=text,
                    status=InstagramBotMessage.Status.DONE,
                    source="echo",
                    processed_at=timezone.now(),
                )
            except Exception:
                msg = None
        try:
            from management.services import bot_followups, bot_sales_classifier

            bot_followups.cancel_pending(client, reason="manager_takeover")
            if msg:
                bot_sales_classifier.classify_message(
                    client, message=msg, role=InstagramBotMessage.Role.MANAGER
                )
        except Exception:
            pass
    if takeover_started:
        notify_manager(
            f"👤 IG: менеджер підключився до {client.username or client.igsid} — "
            f"бот на паузі для цього клієнта.",
            dedupe_key=f"takeover:{client.pk}:{client.paused_at.isoformat() if client.paused_at else 'unknown'}",
            event_type="takeover",
            client=client,
        )
        log("warning", "takeover", f"{recipient_igsid}: менеджер підключився")
    else:
        log("info", "manager_message", f"{recipient_igsid}: повідомлення менеджера збережено")


def _match_allowed(sender_id: str, limit: int = 15, window: int = 3600) -> bool:
    """Cost-гард: не більше `limit` vision-матчингів на клієнта за `window` секунд
    (матчинг іде через дорожчу management-модель — захист квоти від спаму фото)."""
    key = f"ig_match_cnt:{sender_id}"
    try:
        n = cache.get(key) or 0
        if n >= limit:
            return False
        cache.set(key, n + 1, window)
    except Exception:
        return True
    return True


# ---------------------------------------------------------------------------
# Лог-консоль
# ---------------------------------------------------------------------------
def log(level: str, event: str, detail: str = "") -> None:
    try:
        InstagramBotLog.objects.create(level=level, event=event, detail=(detail or "")[:4000])
        if InstagramBotLog.objects.count() > LOG_KEEP_ROWS + 100:
            ids = list(
                InstagramBotLog.objects.order_by("-id").values_list("id", flat=True)[:LOG_KEEP_ROWS]
            )
            if ids:
                InstagramBotLog.objects.exclude(id__in=ids).delete()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Облікові дані
# ---------------------------------------------------------------------------
def resolve_direct_token(s: InstagramBotSettings) -> str:
    if s.direct_source == InstagramBotSettings.CredSource.CUSTOM:
        return (s.custom_direct_token or "").strip()
    # ENV: пріоритет постійному System User токену (IG_MARKER), потім DIRECT_API.
    return (
        os.environ.get("IG_MARKER", "").strip()
        or os.environ.get("DIRECT_API", "").strip()
    )


def resolve_gemini_key(s: InstagramBotSettings) -> str:
    if s.gemini_source == InstagramBotSettings.CredSource.CUSTOM:
        return (s.custom_gemini_key or "").strip()
    return os.environ.get("GEMINI_API", "").strip()


def app_secret() -> str:
    return os.environ.get("IG_APP_SECRET", "").strip()


def allow_unsigned_webhooks() -> bool:
    """Return the explicit development-only bypass for signature checks."""
    raw = os.environ.get("IG_BOT_ALLOW_UNSIGNED_WEBHOOKS")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from django.conf import settings

        return bool(getattr(settings, "IG_BOT_ALLOW_UNSIGNED_WEBHOOKS", False))
    except Exception:
        return False


def webhook_signature_status() -> dict[str, object]:
    configured = bool(app_secret())
    override = allow_unsigned_webhooks()
    return {
        "configured": configured,
        "unsigned_override": override,
        "healthy": configured or override,
        "state": "configured" if configured else ("development_override" if override else "missing_secret"),
    }


# ---------------------------------------------------------------------------
# Webhook signature (X-Hub-Signature-256)
# ---------------------------------------------------------------------------
def verify_signature(raw_body: bytes, header: str) -> bool:
    """Verify Meta's X-Hub-Signature-256 header.

    Missing credentials fail closed. The unsigned bypass is intentionally
    explicit and exists only for local development/test environments.
    """
    secret = app_secret()
    if not secret:
        return allow_unsigned_webhooks()
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1].strip())


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
GRAPH_SENSITIVE_QUERY_KEYS = frozenset({
    "access_token", "client_secret", "app_secret", "api_key", "password",
})
META_ENDPOINT_CLASSES = ("conversations", "send", "read", "oauth")
META_OBSERVABILITY_TTL = 86400
META_DEGRADED_TTL = 120


def _meta_endpoint_class(url: str) -> str:
    try:
        path = urlsplit(url).path
    except (TypeError, ValueError):
        return "read"
    path = path.removeprefix(f"/{GRAPH_VERSION}/")
    if "/conversations" in path:
        return "conversations"
    if path.endswith("/messages"):
        return "send"
    if path.startswith("oauth/"):
        return "oauth"
    return "read"


def _increment_meta_counter(key: str) -> None:
    try:
        if cache.add(key, 1, META_OBSERVABILITY_TTL):
            return
        cache.incr(key)
        return
    except Exception:
        pass
    try:
        cache.set(key, int(cache.get(key) or 0) + 1, META_OBSERVABILITY_TTL)
    except Exception:
        pass


def _record_meta_http_observation(endpoint: str, code: int, body: str = "") -> None:
    """Record bounded endpoint/rate facts without persisting provider payloads."""
    endpoint = endpoint if endpoint in META_ENDPOINT_CLASSES else "read"
    _increment_meta_counter(f"ig_meta_http_total:{endpoint}")
    if code == -1:
        _increment_meta_counter(f"ig_meta_http_transport:{endpoint}")
    try:
        graph_code, _graph_subcode = _graph_error_codes(body)
    except Exception:
        graph_code = 0
    rate_limited = code == 429 or graph_code in RATE_LIMIT_CODES
    if not rate_limited:
        return
    _increment_meta_counter(f"ig_meta_http_rate:{endpoint}")
    try:
        cache.set(
            "ig_meta_http_last_rate",
            {"endpoint": endpoint, "at": timezone.now().isoformat()},
            META_OBSERVABILITY_TTL,
        )
        cache.set("ig_meta_http_degraded_until", time.time() + META_DEGRADED_TTL, META_DEGRADED_TTL)
    except Exception:
        pass


def meta_rate_limit_status() -> dict[str, object]:
    try:
        until = float(cache.get("ig_meta_http_degraded_until") or 0)
    except (TypeError, ValueError):
        until = 0
    endpoints = {}
    for endpoint in META_ENDPOINT_CLASSES:
        try:
            total = int(cache.get(f"ig_meta_http_total:{endpoint}") or 0)
            rate = int(cache.get(f"ig_meta_http_rate:{endpoint}") or 0)
            transport = int(cache.get(f"ig_meta_http_transport:{endpoint}") or 0)
        except (TypeError, ValueError):
            total = rate = transport = 0
        endpoints[endpoint] = {
            "requests": total,
            "rate_limited": rate,
            "transport_errors": transport,
        }
    last = cache.get("ig_meta_http_last_rate")
    if not isinstance(last, dict):
        last = {}
    return {
        "degraded": until > time.time(),
        "degraded_until": datetime.fromtimestamp(until, tz=dt_timezone.utc).isoformat() if until else "",
        "last_rate_limited_at": str(last.get("at") or ""),
        "last_rate_limited_endpoint": str(last.get("endpoint") or ""),
        "endpoints": endpoints,
    }


def _valid_graph_request_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.netloc != "graph.facebook.com"
        or not parsed.path.startswith(f"/{GRAPH_VERSION}/")
        or parsed.fragment
    ):
        return False
    query_keys = {key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    return not query_keys.intersection(GRAPH_SENSITIVE_QUERY_KEYS)


def _graph_url(path: str, params: dict | None = None) -> str:
    """Build only v25 Graph URLs; credentials belong in headers/body, never query."""
    parsed = urlsplit(str(path or ""))
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        raise ValueError("Graph path must be relative")
    if parsed.fragment or _GRAPH_VERSION_PATH_RE.match(parsed.path):
        raise ValueError("invalid Graph path")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if params:
        query.update({str(key): str(value) for key, value in params.items()})
    if {key.lower() for key in query}.intersection(GRAPH_SENSITIVE_QUERY_KEYS):
        raise ValueError("Graph credentials cannot be placed in query")
    url = urlunsplit(("https", "graph.facebook.com", f"/{GRAPH_VERSION}{parsed.path}", urlencode(query), ""))
    if not _valid_graph_request_url(url):
        raise ValueError("invalid versioned Graph URL")
    return url


def _graph_http(
    url: str,
    *,
    token: str = "",
    data: bytes | None = None,
    timeout: int = HTTP_TIMEOUT,
    headers: dict | None = None,
):
    """Call Graph after enforcing host/version and removing query credentials."""
    if not _valid_graph_request_url(url):
        try:
            parsed = urlsplit(url)
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if not token:
                token = query.pop("access_token", "")
            clean_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
        except (TypeError, ValueError):
            return -1, "graph_url_policy"
    else:
        clean_url = url
    if not _valid_graph_request_url(clean_url):
        return -1, "graph_url_policy"
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    endpoint = _meta_endpoint_class(clean_url)
    code, body = _http(clean_url, data=data, timeout=timeout, headers=request_headers)
    _record_meta_http_observation(endpoint, code, body)
    return code, body


def _http(
    url: str,
    *,
    data: bytes | None = None,
    timeout: int = HTTP_TIMEOUT,
    headers: dict | None = None,
):
    if urlsplit(url).netloc == "graph.facebook.com" and not _valid_graph_request_url(url):
        return -1, "graph_url_policy"
    request_headers = dict(headers or {})
    if data is not None:
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return -1, repr(exc)


APP_ID = os.environ.get("IG_APP_ID", "2120980214971807")


def _exchange_long_lived(user_token: str) -> str:
    """short-lived -> long-lived (60 дн). Потрібен app_secret. Page-токен,
    похідний від long-lived user-токена, не має терміну дії."""
    secret = app_secret()
    if not secret or not user_token:
        return ""
    body = urlencode({
        "grant_type": "fb_exchange_token",
        "client_id": APP_ID,
        "client_secret": secret,
        "fb_exchange_token": user_token,
    }).encode("utf-8")
    code, response_body = _graph_http(
        _graph_url("/oauth/access_token"),
        data=body,
        timeout=HTTP_TIMEOUT,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if code == 200:
        try:
            return json.loads(response_body).get("access_token", "") or ""
        except Exception:
            return ""
    return ""


def _effective_user_token(s: InstagramBotSettings) -> str:
    raw = resolve_direct_token(s)
    if not raw or not app_secret():
        return raw  # без секрету не можемо подовжити — використовуємо як є
    cached = cache.get("ig_bot_ll_user_token")
    if cached:
        return cached
    ll = _exchange_long_lived(raw)
    if ll:
        cache.set("ig_bot_ll_user_token", ll, 50 * 24 * 3600)  # ~50 днів
        return ll
    return raw


def _log_token_error(s: InstagramBotSettings, code, body: str) -> None:
    # Стабільна сигнатура: тіло містить мінливий «current time», тож беремо
    # error.code/error_subcode, щоб не логувати ту саму помилку щохвилини.
    sig = str(code)
    try:
        err = json.loads(body).get("error", {})
        sig = f"{code}:{err.get('code')}:{err.get('error_subcode')}"
    except Exception:
        sig = f"{code}:{(body or '')[:40]}"
    if cache.get("ig_bot_pt_errsig") != sig:
        cache.set("ig_bot_pt_errsig", sig, 3600)
        log("error", "page_token", f"HTTP {code}: {_redact_secret_text(body)[:160]}")
    try:
        s.last_error = (
            f"Direct токен недійсний (HTTP {code}). Онови DIRECT_API в ENV "
            f"(або свій токен у налаштуваннях)."
        )
        s.save(update_fields=["last_error"])
    except Exception:
        pass


NOTIFICATION_STALE_SENDING_SECONDS = 300
NOTIFICATION_MAX_ATTEMPTS = 5


def _notification_retry_at(row, now, *, minimum_delay_seconds=0):
    base = min(3600, 30 * (2 ** max(0, int(row.attempts or 1) - 1)))
    jitter = int(hashlib.sha256(row.dedupe_key.encode("utf-8")).hexdigest()[:2], 16) % 16
    try:
        provider_delay = max(0, min(int(minimum_delay_seconds or 0), 86400))
    except (TypeError, ValueError):
        provider_delay = 0
    return now + timedelta(seconds=max(base, provider_delay) + jitter)


def _finish_notification(
    dedupe_key,
    *,
    status,
    error="",
    failure_kind="",
    message_id="",
    retry_after_seconds=0,
):
    now = timezone.now()
    with transaction.atomic():
        row = IgBotNotification.objects.select_for_update().get(dedupe_key=dedupe_key)
        if row.status != IgBotNotification.Status.SENDING:
            return False
        row.status = status
        row.telegram_message_id = message_id
        row.last_error = (error or "")[:500]
        row.failure_kind = (failure_kind or "")[:32]
        row.sent_at = now if status == IgBotNotification.Status.SENT else None
        row.next_attempt_at = (
            _notification_retry_at(row, now, minimum_delay_seconds=retry_after_seconds)
            if status == IgBotNotification.Status.FAILED
            else None
        )
        if status == IgBotNotification.Status.FAILED and row.attempts >= NOTIFICATION_MAX_ATTEMPTS:
            row.status = IgBotNotification.Status.DEAD_LETTER
            row.failure_kind = "retry_exhausted"
            row.next_attempt_at = None
        row.save(update_fields=[
            "status", "telegram_message_id", "last_error", "failure_kind",
            "sent_at", "next_attempt_at", "updated_at",
        ])
        return row.status == IgBotNotification.Status.SENT


def _deliver_manager_notification(dedupe_key: str) -> bool:
    from management.services.ig_maintenance import notification_send_boundary

    with notification_send_boundary() as send_allowed:
        if not send_allowed:
            return False
        return _deliver_manager_notification_unlocked(dedupe_key)


def _deliver_manager_notification_unlocked(dedupe_key: str) -> bool:
    now = timezone.now()
    row = IgBotNotification.objects.filter(dedupe_key=dedupe_key).first()
    if not row:
        return False
    if row.status == IgBotNotification.Status.SENT:
        return True
    if row.status in {
        IgBotNotification.Status.UNKNOWN,
        IgBotNotification.Status.DEAD_LETTER,
        IgBotNotification.Status.RESOLVED,
    }:
        return False
    if row.status == IgBotNotification.Status.SENDING:
        stale_before = now - timedelta(seconds=NOTIFICATION_STALE_SENDING_SECONDS)
        IgBotNotification.objects.filter(
            pk=row.pk,
            status=IgBotNotification.Status.SENDING,
            last_attempt_at__lte=stale_before,
        ).update(
            status=IgBotNotification.Status.UNKNOWN,
            failure_kind="ambiguous_stale_sending",
            last_error="delivery outcome unknown after interrupted send",
            next_attempt_at=None,
            updated_at=now,
        )
        return False
    eligible = Q(status=IgBotNotification.Status.PENDING) | Q(
        status=IgBotNotification.Status.FAILED,
        next_attempt_at__isnull=True,
    ) | Q(
        status=IgBotNotification.Status.FAILED,
        next_attempt_at__lte=now,
    )
    claimed = IgBotNotification.objects.filter(pk=row.pk).filter(eligible).update(
        status=IgBotNotification.Status.SENDING,
        attempts=F("attempts") + 1,
        last_attempt_at=now,
        next_attempt_at=None,
        last_error="",
        failure_kind="",
        updated_at=now,
    )
    if claimed != 1:
        return False
    row.refresh_from_db()
    payload = dict(row.payload or {})

    token = os.environ.get("MANAGEMENT_TG_BOT_TOKEN", "").strip()
    chat = os.environ.get("MANAGEMENT_TG_ADMIN_CHAT_ID", "").strip() or str(payload.get("chat_id") or "")
    text = str(payload.get("text") or "")[:3500]
    if not token or not chat:
        _finish_notification(
            dedupe_key,
            status=IgBotNotification.Status.FAILED,
            error="telegram_not_configured",
            failure_kind="configuration",
        )
        return False
    reply_markup = payload.get("reply_markup")
    if not isinstance(reply_markup, dict):
        reply_markup = None
    try:
        body = json.dumps(
            {
                "chat_id": chat,
                "text": text,
                "disable_web_page_preview": True,
                **({"reply_markup": reply_markup} if reply_markup is not None else {}),
            }
        ).encode("utf-8")
        code, response_body = _http(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body, timeout=HTTP_TIMEOUT
        )
    except Exception as exc:
        _finish_notification(
            dedupe_key,
            status=IgBotNotification.Status.UNKNOWN,
            error=repr(exc),
            failure_kind="ambiguous_transport",
        )
        return False
    if code < 0:
        _finish_notification(
            dedupe_key,
            status=IgBotNotification.Status.UNKNOWN,
            error=response_body,
            failure_kind="ambiguous_transport",
        )
        return False
    try:
        response = json.loads(response_body or "{}")
    except (TypeError, ValueError):
        response = {}
        if code == 200:
            _finish_notification(
                dedupe_key,
                status=IgBotNotification.Status.UNKNOWN,
                error="Telegram returned an unreadable success response",
                failure_kind="ambiguous_provider_response",
            )
            return False
    if not isinstance(response, dict):
        if code == 200:
            _finish_notification(
                dedupe_key,
                status=IgBotNotification.Status.UNKNOWN,
                error="Telegram returned an invalid success payload",
                failure_kind="ambiguous_provider_response",
            )
            return False
        response = {}
    if code == 200 and bool(response.get("ok")):
        return _finish_notification(
            dedupe_key,
            status=IgBotNotification.Status.SENT,
            message_id=str((response.get("result") or {}).get("message_id") or ""),
        )
    retryable = code == 429 or code >= 500
    parameters = response.get("parameters")
    retry_after = parameters.get("retry_after") if code == 429 and isinstance(parameters, dict) else 0
    _finish_notification(
        dedupe_key,
        status=(IgBotNotification.Status.FAILED if retryable else IgBotNotification.Status.DEAD_LETTER),
        error=str(response.get("description") or f"HTTP {code}"),
        failure_kind=(
            "rate_limited"
            if code == 429
            else ("provider_retryable" if retryable else "provider_permanent")
        ),
        retry_after_seconds=retry_after,
    )
    return False


def drain_manager_notifications(*, limit: int = 20) -> int:
    now = timezone.now()
    stale_before = now - timedelta(seconds=NOTIFICATION_STALE_SENDING_SECONDS)
    stale_ids = list(
        IgBotNotification.objects.filter(
            status=IgBotNotification.Status.SENDING,
            last_attempt_at__lte=stale_before,
        ).values_list("dedupe_key", flat=True)[:limit]
    )
    for dedupe_key in stale_ids:
        _deliver_manager_notification(dedupe_key)
    due_ids = list(
        IgBotNotification.objects.filter(
            status__in=[IgBotNotification.Status.PENDING, IgBotNotification.Status.FAILED]
        ).filter(
            Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
        ).order_by("next_attempt_at", "id").values_list("dedupe_key", flat=True)[:limit]
    )
    sent = 0
    for dedupe_key in due_ids:
        sent += int(_deliver_manager_notification(dedupe_key))
    return sent


def notify_manager(
    text: str,
    *,
    dedupe_key: str | None = None,
    event_type: str = "generic",
    client: IgClient | None = None,
    reply_markup: dict | None = None,
) -> bool:
    """Persist one idempotent notification and attempt immediate delivery."""
    text = (text or "").strip()[:3500]
    if not text:
        return False
    if not dedupe_key:
        dedupe_key = "generic:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    chat = os.environ.get("MANAGEMENT_TG_ADMIN_CHAT_ID", "").strip()
    payload = {"text": text, "chat_id": chat}
    if isinstance(reply_markup, dict):
        payload["reply_markup"] = reply_markup
    try:
        with transaction.atomic():
            row, created = IgBotNotification.objects.select_for_update().get_or_create(
                dedupe_key=dedupe_key,
                defaults={
                    "client": client,
                    "event_type": (event_type or "generic")[:64],
                    "payload": payload,
                },
            )
            if not created and row.status in {
                IgBotNotification.Status.PENDING,
                IgBotNotification.Status.FAILED,
            }:
                row.client = client or row.client
                row.event_type = (event_type or row.event_type or "generic")[:64]
                row.payload = payload
                row.save(update_fields=["client", "event_type", "payload", "updated_at"])
    except Exception:
        return False
    return _deliver_manager_notification(dedupe_key)


def _rate_exceeded(s: InstagramBotSettings, sender_id: str, limit: int = 25, window: int = 3600) -> bool:
    """Анти-спам: не більше `limit` відповідей одному відправнику за `window` c."""
    key = f"ig_bot_rate:{sender_id}"
    try:
        n = cache.get(key) or 0
        if n >= limit:
            return True
        cache.set(key, n + 1, window)
    except Exception:
        return False
    return False


def _repeated_question(sender_id: str, text: str, window: int = 600) -> int:
    """Скільки разів цей самий текст від відправника за вікно (анти-абуз токенів)."""
    import hashlib

    norm = " ".join((text or "").lower().split())
    if not norm:
        return 0
    h = hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]
    key = f"ig_bot_q:{sender_id}:{h}"
    try:
        n = (cache.get(key) or 0) + 1
        cache.set(key, n, window)
        return n
    except Exception:
        return 0


def get_page_token(s: InstagramBotSettings, *, force: bool = False) -> str:
    token = _effective_user_token(s)
    if not token:
        return ""
    ck = "ig_bot_page_token"
    if not force:
        cached = cache.get(ck)
        if cached:
            return cached
        if cache.get("ig_bot_pt_cooldown"):
            return ""
    code, body = _graph_http(
        _graph_url("/me/accounts", {"fields": "name,access_token"}),
        token=token,
        timeout=HTTP_TIMEOUT,
    )
    if code != 200:
        cache.set("ig_bot_pt_cooldown", 1, 60)
        _log_token_error(s, code, body)
        return ""
    try:
        for page in json.loads(body).get("data", []):
            if str(page.get("id")) == s.page_id:
                pt = page.get("access_token") or ""
                if pt:
                    # Якщо токен подовжений (є секрет) — page-токен постійний,
                    # кешуємо надовго; інакше — коротко.
                    ttl = 50 * 24 * 3600 if app_secret() else PAGE_TOKEN_TTL
                    cache.set(ck, pt, ttl)
                    cache.delete("ig_bot_pt_cooldown")
                    cache.delete("ig_bot_pt_errsig")
                return pt
    except Exception as exc:
        log("error", "page_token_parse", repr(exc))
    return ""


def _conv_cache_key(s: InstagramBotSettings) -> str:
    return f"ig_bot_conv_ids:{s.page_id or 'unknown'}"


def _valid_conv_snapshot(value) -> list[str]:
    if not isinstance(value, list) or len(value) > CONV_MAX_IDS:
        return []
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            return []
        item = item.strip()
        if not _CONV_ID_RE.fullmatch(item):
            return []
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _valid_conversation_page_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc == "graph.facebook.com"
        and parsed.path.startswith(f"/{GRAPH_VERSION}/")
        and not parsed.fragment
    )


def refresh_conv_ids(s: InstagramBotSettings, page_token: str) -> list[str]:
    """Refresh a complete bounded conversation snapshot in the background."""
    stale = _valid_conv_snapshot(cache.get(_conv_cache_key(s)))
    lock_key = f"ig_bot_conv_refresh:{s.page_id or 'unknown'}"
    if not cache.add(lock_key, "1", timeout=CONV_REFRESH_LOCK_TTL):
        return stale
    try:
        return _refresh_conv_ids_unlocked(s, page_token, stale)
    finally:
        cache.delete(lock_key)


def _refresh_conv_ids_unlocked(s: InstagramBotSettings, page_token: str, stale: list[str]) -> list[str]:
    """Refresh a complete bounded conversation snapshot in the background.

    A failed later page must never replace a known-good snapshot with partial
    data: polling a partial list silently drops customers from observation.
    """
    page_url = _graph_url(
        f"/{s.page_id}/conversations",
        {
            "platform": "instagram",
            "fields": "id",
            "limit": CONV_PAGE_LIMIT,
        },
    )
    discovered: list[str] = []
    seen: set[str] = set()
    visited_pages: set[str] = set()
    for page_index in range(CONV_MAX_PAGES):
        if page_index:
            # Fixed conservative spacing is easier to reason about than a
            # provider-header guess and remains within the documented limit.
            time.sleep(CONV_MIN_INTERVAL)
        code, body = _graph_http(page_url, token=page_token, timeout=CONV_LIST_TIMEOUT)
        if code != 200:
            log("warning", "conversations", f"page={page_index + 1} HTTP {code}; keeping complete cache")
            return stale
        try:
            if page_url in visited_pages:
                raise ValueError("repeated paging URL")
            visited_pages.add(page_url)
            envelope = json.loads(body)
            if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), list):
                raise ValueError("malformed data")
            for conversation in envelope["data"]:
                if not isinstance(conversation, dict):
                    raise ValueError("malformed conversation")
                conversation_id = conversation.get("id")
                if not isinstance(conversation_id, str) or not _CONV_ID_RE.fullmatch(conversation_id.strip()):
                    raise ValueError("malformed conversation id")
                conversation_id = conversation_id.strip()
                if conversation_id not in seen:
                    seen.add(conversation_id)
                    discovered.append(conversation_id)
                    if len(discovered) > CONV_MAX_IDS:
                        raise ValueError("conversation cap exceeded")
            paging = envelope.get("paging")
            if paging is None:
                paging = {}
            if not isinstance(paging, dict):
                raise ValueError("malformed paging")
            next_url = paging.get("next")
            if not next_url:
                cache.set(_conv_cache_key(s), discovered, CONV_CACHE_TTL)
                return discovered
            if not isinstance(next_url, str) or not _valid_conversation_page_url(next_url):
                raise ValueError("untrusted paging URL")
            page_url = next_url
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            log("warning", "conversations", f"page={page_index + 1} malformed; keeping complete cache ({exc})")
            return stale
    log("warning", "conversations", "page cap reached; keeping complete cache")
    return stale


def get_conv_ids_cached(s: InstagramBotSettings | None = None) -> list[str] | None:
    if s is None:
        return None
    cache_key = _conv_cache_key(s)
    value = cache.get(cache_key)
    if value is None:
        return None
    valid = _valid_conv_snapshot(value)
    if value == []:
        return []
    if not valid:
        cache.delete(cache_key)
        log("warning", "poll_cache", "invalid conversation cache discarded")
        return None
    return valid


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
def send_sender_action(s: InstagramBotSettings, recipient_id: str, action: str) -> None:
    """typing_on / typing_off / mark_seen — для відчуття миттєвості (best practice)."""
    page_token = get_page_token(s)
    if not page_token:
        return
    try:
        body = json.dumps({"recipient": {"id": recipient_id}, "sender_action": action}).encode("utf-8")
        _graph_http(
            _graph_url(f"/{s.page_id}/messages"),
            token=page_token,
            data=body,
            timeout=HTTP_TIMEOUT,
        )
    except Exception:
        pass


def _split_for_send(text: str, limit: int = 950, max_chunks: int = 4) -> list[str]:
    """Ріже текст на частини ≤limit байт (UTF-8). Send API дозволяє 1000 байт."""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    rest = text
    while rest and len(chunks) < max_chunks:
        if len(rest.encode("utf-8")) <= limit:
            chunks.append(rest)
            rest = ""
            break
        # знайти межу різу по байтах, з відкатом до пробілу/переносу
        cut = limit
        while len(rest[:cut].encode("utf-8")) > limit and cut > 0:
            cut -= 1
        slice_ = rest[:cut]
        brk = max(slice_.rfind("\n"), slice_.rfind(". "), slice_.rfind(" "))
        if brk > int(cut * 0.5):
            slice_ = rest[:brk + 1]
        chunks.append(slice_.strip())
        rest = rest[len(slice_):]
    return [c for c in chunks if c]


RATE_LIMIT_CODES = {4, 17, 32, 613, 80007}  # тимчасові ліміти — варто ретраїти
ADVANCED_ACCESS_SUBCODE = 2534048
MESSAGING_WINDOW_CLOSED_SUBCODE = 1545041
PERMANENT_HINT = {
    200: "немає потрібного дозволу Meta для Instagram Messaging",
    190: "токен недійсний (онови DIRECT_API/IG_MARKER)",
    10: "помилка дозволів або політики Meta",
    100: "некоректний параметр запиту",
    551: "отримувач недоступний (блокування, деактивація або обмеження діалогу)",
}


def _graph_error(body: str) -> dict:
    try:
        err = json.loads(body).get("error", {}) or {}
    except Exception:
        return {}
    return err if isinstance(err, dict) else {}


def _graph_error_codes(body: str) -> tuple[int, int]:
    err = _graph_error(body)
    try:
        return int(err.get("code", 0) or 0), int(err.get("error_subcode", 0) or 0)
    except Exception:
        return 0, 0


def _classify_send_error(code: int, body: str) -> tuple[str, str]:
    """Повертає (kind, hint): kind = 'transient' | 'permanent'."""
    if code == -1 or code >= 500:
        return "transient", "тимчасова мережева/серверна помилка"
    ec, sub = _graph_error_codes(body)
    if ec in RATE_LIMIT_CODES:
        return "transient", "ліміт частоти (retry пізніше)"
    if sub == ADVANCED_ACCESS_SUBCODE:
        return (
            "permanent",
            "Meta відхилила нерольового отримувача: немає Advanced Access на "
            "instagram_manage_messages або отримувач не має ролі в застосунку",
        )
    if sub == MESSAGING_WINDOW_CLOSED_SUBCODE:
        return (
            "permanent",
            "24-годинне вікно відповіді Meta закрите; потрібен дозволений message tag "
            "або нове повідомлення від користувача",
        )
    suffix = f" (code {ec}, subcode {sub})" if sub else f" (code {ec})"
    return "permanent", PERMANENT_HINT.get(ec, "відмова Graph API") + suffix


def _delivery_status_for_error(code: int, body: str) -> str:
    graph_code, graph_subcode = _graph_error_codes(body)
    if graph_subcode == ADVANCED_ACCESS_SUBCODE:
        return IgClient.DeliveryStatus.ADVANCED_ACCESS
    if graph_subcode == MESSAGING_WINDOW_CLOSED_SUBCODE:
        return IgClient.DeliveryStatus.WINDOW_CLOSED
    if graph_code == 551:
        # Graph #551 is ambiguous: it can be a blocked/restricted thread as
        # well as an inbox request. Ask the operator to inspect Requests,
        # but never claim we proved that the thread is there.
        return IgClient.DeliveryStatus.MESSAGE_REQUEST_CHECK
    return IgClient.DeliveryStatus.SEND_BLOCKED


def _remember_client_delivery_error(recipient_id: str, hint: str, *, code: int, body: str) -> None:
    """Store only classified, bounded delivery data for the affected CRM card."""
    try:
        client = IgClient.objects.filter(igsid=recipient_id).first()
        if not client:
            return
        graph_code, graph_subcode = _graph_error_codes(body)
        client.delivery_status = _delivery_status_for_error(code, body)
        client.delivery_error = (hint or "")[:500]
        client.delivery_http_code = code if code > 0 else None
        client.delivery_graph_code = graph_code or None
        client.delivery_graph_subcode = graph_subcode or None
        client.delivery_failed_at = timezone.now()
        client.save(update_fields=[
            "delivery_status",
            "delivery_error",
            "delivery_http_code",
            "delivery_graph_code",
            "delivery_graph_subcode",
            "delivery_failed_at",
            "updated_at",
        ])
    except Exception:
        pass


def _clear_client_delivery_error(recipient_id: str) -> None:
    try:
        client = IgClient.objects.filter(igsid=recipient_id).first()
        if not client or not client.delivery_status:
            return
        client.delivery_status = ""
        client.delivery_error = ""
        client.delivery_http_code = None
        client.delivery_graph_code = None
        client.delivery_graph_subcode = None
        client.delivery_failed_at = None
        client.save(update_fields=[
            "delivery_status",
            "delivery_error",
            "delivery_http_code",
            "delivery_graph_code",
            "delivery_graph_subcode",
            "delivery_failed_at",
            "updated_at",
        ])
    except Exception:
        pass


def _remember_send_error(s: InstagramBotSettings, hint: str, *, code: int | None = None) -> None:
    detail = f"Meta Send API: {hint}"
    if code is not None:
        detail += f" (HTTP {code})"
    try:
        s.last_error = detail[:1000]
        s.save(update_fields=["last_error"])
    except Exception:
        pass


def _clear_send_error(s: InstagramBotSettings) -> None:
    try:
        if (s.last_error or "").startswith("Meta Send API:"):
            s.last_error = ""
            s.save(update_fields=["last_error"])
    except Exception:
        pass


def send_text(
    s: InstagramBotSettings,
    recipient_id: str,
    text: str,
    *,
    permission_boundary_factory=None,
) -> tuple[bool, str, str]:
    """Повертає (ok, kind, hint); ``cancelled`` means no provider request ran."""
    page_token = get_page_token(s)
    if not page_token:
        hint = "немає page-token (перевірте DIRECT_API/IG_MARKER)"
        _remember_send_error(s, hint)
        return False, "permanent", hint
    parts = _split_for_send(text)
    if not parts:
        return False, "permanent", "порожня відповідь"
    ok_any = False
    for part in parts:
        boundary = (
            permission_boundary_factory()
            if permission_boundary_factory
            else nullcontext(True)
        )
        with boundary as send_allowed:
            if not send_allowed:
                hint = "permission epoch changed before Meta request"
                if ok_any:
                    return False, "unknown", f"часткова доставка; {hint}"
                return False, "cancelled", hint
            # Позначаємо ДО відправки: echo цього чанка прийде асинхронно і не має
            # сприйнятись за повідомлення менеджера (виправляє хибний авто-стоп).
            _mark_bot_sent(recipient_id, part)
            body = json.dumps(
                {
                    "recipient": {"id": recipient_id},
                    "message": {"text": part},
                    "messaging_type": "RESPONSE",
                }
            ).encode("utf-8")
            code, resp = _graph_http(
                _graph_url(f"/{s.page_id}/messages"),
                token=page_token,
                data=body,
            )
        if code == 200:
            ok_any = True
            _clear_send_error(s)
            _clear_client_delivery_error(recipient_id)
            continue
        kind, hint = _classify_send_error(code, resp)
        if kind == "permanent":
            if ok_any:
                kind = "unknown"
                hint = f"часткова доставка; результат останніх чанків не підтверджено: {hint}"
            else:
                _remember_send_error(s, hint, code=code)
                _remember_client_delivery_error(recipient_id, hint, code=code, body=resp)
        elif kind == "transient":
            # A timeout/5xx can happen after Meta accepted the request. There
            # is no provider idempotency key, so retrying would risk a duplicate.
            kind = "unknown"
            hint = f"результат доставки не підтверджено: {hint}"
        log("error", "send", f"HTTP {code} [{kind}] {hint}")
        return False, kind, hint
    return True, "", ""


def send_text_tagged(
    s: InstagramBotSettings,
    recipient_id: str,
    text: str,
    tag: str = "HUMAN_AGENT",
    *,
    human_authored: bool = False,
) -> tuple[bool, str, str]:
    """Send an explicitly human-authored support reply with ``HUMAN_AGENT``.

    Meta documents this tag for human support beyond the normal response
    window. Automated sales, reminder, and shipment jobs must use the regular
    response window or create an operator task instead.
    """
    if tag != "HUMAN_AGENT" or not human_authored:
        return (
            False,
            "policy",
            "HUMAN_AGENT дозволено лише для явно підтвердженої відповіді human support",
        )
    page_token = get_page_token(s)
    if not page_token:
        hint = "немає page-token"
        _remember_send_error(s, hint)
        return False, "permanent", hint
    parts = _split_for_send(text)
    if not parts:
        return False, "permanent", "порожня відповідь"
    ok_any = False
    for part in parts:
        _mark_bot_sent(recipient_id, part)
        body = json.dumps(
            {
                "recipient": {"id": recipient_id},
                "message": {"text": part},
                "messaging_type": "MESSAGE_TAG",
                "tag": tag,
            }
        ).encode("utf-8")
        code, resp = _graph_http(
            _graph_url(f"/{s.page_id}/messages"),
            token=page_token,
            data=body,
        )
        if code == 200:
            ok_any = True
            _clear_send_error(s)
            _clear_client_delivery_error(recipient_id)
            continue
        kind, hint = _classify_send_error(code, resp)
        if kind == "permanent":
            if ok_any:
                kind = "unknown"
                hint = f"часткова доставка; результат останніх чанків не підтверджено: {hint}"
            else:
                _remember_send_error(s, hint, code=code)
                _remember_client_delivery_error(recipient_id, hint, code=code, body=resp)
        elif kind == "transient":
            kind = "unknown"
            hint = f"результат доставки не підтверджено: {hint}"
        log("error", "send_tag", f"HTTP {code} [{kind}] {hint}")
        return False, kind, hint
    return True, "", ""


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
_CHAT_REASONING_PATTERNS = (
    (
        "payment_decision",
        re.compile(r"\b(оплат\w*|плат\w*|paylink|рахунок\w*|счет\w*)\b", re.I),
    ),
    (
        "order_decision",
        re.compile(
            r"\b(замов\w*|заказ\w*|достав\w*|нова\s+пошт\w*|новая\s+почт\w*|"
            r"відділен\w*|отделен\w*)\b",
            re.I,
        ),
    ),
    (
        "size_fit_decision",
        re.compile(
            r"\b(розмір\w*|размер\w*|oversize|оверсайз\w*|посадк\w*|зріст\w*|рост\w*)\b",
            re.I,
        ),
    ),
    (
        "product_decision",
        re.compile(
            r"\b(товар\w*|футболк\w*|худі|худи|лонгслів\w*|колір\w*|цвет\w*|"
            r"ткан\w*|термохром\w*|наявн\w*|налич\w*|цін\w*|цен\w*)\b",
            re.I,
        ),
    ),
)


def select_chat_reasoning_task(
    history: list[dict], images: list[tuple[str, bytes]] | None = None
) -> str:
    """Choose the provider reasoning task from explicit current-turn evidence."""
    if images:
        return "media_analysis"
    latest_user = ""
    for item in reversed(history or []):
        if item.get("role") == "user" and item.get("text"):
            latest_user = str(item["text"])
            break
    for task, pattern in _CHAT_REASONING_PATTERNS:
        if pattern.search(latest_user):
            return task
    return "customer_chat"


def gemini_generate(
    s: InstagramBotSettings, history: list[dict], images: list[tuple[str, bytes]] | None = None,
    match_hint: str | None = None, memory_note: str | None = None,
    context_note: str | None = None, client=None,
) -> str | None:
    """history: [{'role':'user'|'model','text':str}] хронологічно.
    images: список (mime_type, raw_bytes) для ОСТАННЬОГО (поточного) user-ходу."""
    contents = []
    for h in history:
        if h.get("text"):
            contents.append({"role": h["role"], "parts": [{"text": h["text"]}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "(порожнє повідомлення)"}]}]

    # Зображення додаємо в останній user-хід як inline_data.
    if images:
        last = contents[-1]
        if last.get("role") != "user":
            last = {"role": "user", "parts": [{"text": ""}]}
            contents.append(last)
        for mime, raw in images[:3]:
            try:
                last["parts"].append(
                    {"inline_data": {"mime_type": mime, "data": base64.b64encode(raw).decode()}}
                )
            except Exception:
                pass

    # system_instruction = правило + оперативні директиви + база знань + каталог.
    sys_text = (s.system_prompt or "").strip()
    live = (s.knowledge_base or "").strip()
    if live:
        sys_text += "\n\n[ОПЕРАТИВНІ ДИРЕКТИВИ — найвищий пріоритет, дотримуйся беззаперечно]\n" + live
    try:
        from management.services.bot_knowledge import get_brand_knowledge

        kb = get_brand_knowledge()
        if kb:
            sys_text += "\n\n[БАЗА ЗНАНЬ ПРО БРЕНД]\n" + kb
    except Exception:
        pass
    try:
        from management.services.bot_catalog import get_catalog_context

        catalog = get_catalog_context()
        if catalog:
            sys_text += "\n\n" + catalog
    except Exception:
        pass
    try:
        from management.models import BotQuickLink
        from management.services.bot_playbooks import active_instruction_block

        instr = active_instruction_block(client)
        if instr:
            sys_text += "\n\n[ДОДАТКОВІ PLAYBOOK-ІНСТРУКЦІЇ]\n" + instr
        links = BotQuickLink.active_block()
        if links:
            sys_text += "\n\n[ДОСТУПНІ ПОСИЛАННЯ — надсилай доречне за запитом]\n" + links
    except Exception:
        pass
    sys_text = sys_text.strip()
    # Протокол оплати ([PAYLINK]+[PRODUCT], без вигаданих URL) + правило точності.
    sys_text = (
        (sys_text + "\n\n" + PAYMENT_PROTOCOL_NOTE).strip() if sys_text else PAYMENT_PROTOCOL_NOTE
    )
    sys_text = (sys_text + "\n\n" + ANTI_HALLUCINATION_NOTE).strip()
    sys_text = (sys_text + "\n\n" + SALES_AUTOMATION_GUARDRAILS).strip()
    if context_note:
        sys_text = (sys_text + "\n\n" + context_note).strip()
    if memory_note:
        sys_text = (sys_text + "\n\n" + memory_note).strip()
    if match_hint:
        sys_text = (sys_text + "\n\n" + match_hint).strip()

    payload = {
        "contents": contents,
        # Reasoning level is applied centrally from the task policy. The output
        # budget remains reserved for a concise customer-facing answer.
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 4096,
        },
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_ONLY_HIGH"}
            for c in (
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            )
        ],
    }
    if sys_text:
        payload["system_instruction"] = {"parts": [{"text": sys_text}]}

    # Діалог із клієнтом — найвищий пріоритет (роль 'chat'): пул ключів
    # GEMINI_API/2 → позичання GEMINI_API5/6; selected chat model is primary,
    # then the validated fallback chain.
    # Якщо адмін обрав CUSTOM-ключ — він пробується першим (manual_key).
    manual_key = None
    if s.gemini_source == InstagramBotSettings.CredSource.CUSTOM:
        manual_key = (s.custom_gemini_key or "").strip() or None
    from management.services.call_ai_analysis import (
        gemini_generate_text, CallAIAnalysisError,
    )
    from management.services.gemini_keys import normalize_chat_model
    import time as _time

    def _cb(msg):
        # Реальний час перебору ключів/моделей у консолі бота.
        log("info", "gemini_try", msg)

    effective_model = normalize_chat_model(s.gemini_model)
    reasoning_task = select_chat_reasoning_task(history, images)
    log("info", "gemini_start",
        f"генерую відповідь (chat/{effective_model}; task={reasoning_task}; "
        f"кастом-ключ: {'так' if manual_key else 'ні'})")
    _t0 = _time.monotonic()
    try:
        out = gemini_generate_text(
            payload,
            role="chat",
            manual_key=manual_key,
            log_cb=_cb,
            model_override=effective_model,
            reasoning_task=reasoning_task,
        )
    except CallAIAnalysisError as exc:
        log("error", "gemini", f"({_time.monotonic() - _t0:.1f}с) {str(exc)[:300]}")
        return None
    except Exception as exc:
        log("error", "gemini", f"({_time.monotonic() - _t0:.1f}с) {repr(exc)}")
        return None
    text = (out.get("parsed") or "").strip()
    if not text:
        log("warning", "gemini_empty", f"порожня відповідь ({_time.monotonic() - _t0:.1f}с)")
        return None
    try:
        s.last_gemini_model = str(out.get("model") or effective_model)[:80]
        meta = out.get("meta") or {}
        usage = out.get("usage") or {}
        s.last_gemini_key = str(meta.get("key") or "")[:80]
        s.last_gemini_at = timezone.now()
        s.last_gemini_reasoning_task = str(meta.get("reasoning_task") or reasoning_task)[:64]
        s.last_gemini_reasoning_level = str(meta.get("reasoning_level") or "")[:16]
        s.last_gemini_policy_version = str(meta.get("reasoning_policy_version") or "")[:32]
        s.last_gemini_thoughts_tokens = max(
            0, int(meta.get("thoughts_tokens") or usage.get("thoughtsTokenCount") or 0)
        )
        s.last_gemini_candidates_tokens = max(
            0, int(meta.get("candidates_tokens") or usage.get("candidatesTokenCount") or 0)
        )
        s.save(update_fields=[
            "last_gemini_model", "last_gemini_key", "last_gemini_at",
            "last_gemini_reasoning_task", "last_gemini_reasoning_level",
            "last_gemini_policy_version", "last_gemini_thoughts_tokens",
            "last_gemini_candidates_tokens", "updated_at",
        ])
    except Exception:
        pass
    log("info", "gemini_ok",
        f"{out.get('model')} / {(out.get('meta') or {}).get('key')} за {_time.monotonic() - _t0:.1f}с")
    return text


def download_image(url: str) -> tuple[str, bytes] | None:
    """Завантажує зображення-вкладення для мультимодалу. Ліміт ~6 МБ."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TwoCommsBot/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            if not mime.startswith("image/"):
                return None
            raw = resp.read(6 * 1024 * 1024 + 1)
            if len(raw) > 6 * 1024 * 1024:
                return None
            return mime, raw
    except Exception as exc:
        log("warning", "image_download", repr(exc))
        return None


def _collect_images(attachments_json: str | None, limit: int = 3) -> list[tuple[str, bytes]]:
    """Завантажує вкладення повідомлення у список (mime, bytes) для vision.

    attachments_json — JSON-рядок зі списком URL (як зберігає InstagramBotMessage).
    Невдалі/не-image завантаження тихо пропускаються. Cap на `limit`.
    """
    images: list[tuple[str, bytes]] = []
    if not attachments_json:
        return images
    try:
        urls = json.loads(attachments_json)
    except Exception:
        return images
    for url in (urls or [])[:limit]:
        img = download_image(url)
        if img:
            images.append(img)
    return images


def _match_hint_text(match: dict | None) -> str | None:
    """Формує підказку для моделі за результатом матчингу фото з каталогом.

    Висока впевненість → називаємо конкретний товар і ціну. Низька → просимо
    уточнити/запропонувати каталог і НЕ вигадувати товар.
    """
    if not match:
        return None
    try:
        from management.services.bot_vision import MATCH_THRESHOLD
    except Exception:
        MATCH_THRESHOLD = 0.6
    pid = match.get("product_id")
    try:
        conf = float(match.get("confidence") or 0)
    except Exception:
        conf = 0.0
    if pid and conf >= MATCH_THRESHOLD:
        try:
            from storefront.models import Product

            p = Product.objects.filter(id=pid).first()
        except Exception:
            p = None
        if p:
            try:
                price = int(getattr(p, "final_price", None) or p.price)
            except Exception:
                price = p.price
            url = f"https://twocomms.shop/product/{p.slug}/"
            return (
                f"[ЗБІГ ТОВАРУ ЗА ФОТО — впевненість {int(conf * 100)}%] Клієнт прислав "
                f"фото/пост, і це товар з каталогу: «{p.title}» — {price} грн, {url}. "
                f"Назви саме цей товар, дай ціну і за потреби посилання. Веди до покупки."
            )
    return (
        "[ФОТО БЕЗ ВПЕВНЕНОГО ЗБІГУ] Клієнт прислав фото/пост, але точно зіставити з "
        "каталогом не вдалось. Чемно уточни деталі (тип, колір, принт) або запропонуй "
        "переглянути каталог. НЕ вигадуй товар, ціну чи наявність."
    )


def _maybe_pin_from_match(client, match: dict | None) -> bool:
    """Закріплює товар за клієнтом, якщо матчинг фото впевнений (≥ поріг).
    Так пересланий пост одразу «прив'язує» товар для майбутньої оплати."""
    if not client or not match:
        return False
    try:
        from management.services.bot_vision import MATCH_THRESHOLD
    except Exception:
        MATCH_THRESHOLD = 0.6
    pid = match.get("product_id")
    try:
        conf = float(match.get("confidence") or 0)
    except Exception:
        conf = 0.0
    if not pid or conf < MATCH_THRESHOLD:
        return False
    try:
        from management.services import bot_orders

        ok = bot_orders.pin_product(client, pid)
        if ok:
            try:
                client.current_product_confidence = conf
                client.save(update_fields=["current_product_confidence", "updated_at"])
            except Exception:
                pass
        return ok
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Профіль клієнта (IG Graph) — ім'я / username / аватар
# ---------------------------------------------------------------------------
def fetch_ig_profile(s: InstagramBotSettings, igsid: str) -> dict:
    """Тягне профіль співрозмовника через Graph (name/username/profile_pic).
    Порожній dict, якщо немає токена або помилка."""
    page_token = get_page_token(s)
    if not page_token or not igsid:
        return {}
    code, body = _graph_http(
        _graph_url(f"/{igsid}", {"fields": "name,username,profile_pic"}),
        token=page_token,
        timeout=HTTP_TIMEOUT,
    )
    if code != 200:
        return {}
    try:
        data = json.loads(body)
    except Exception:
        return {}
    return {
        "name": data.get("name") or "",
        "username": data.get("username") or "",
        "profile_pic": data.get("profile_pic") or "",
    }


def _localize_avatar(igsid: str, url: str) -> str:
    """Качає аватар і зберігає у себе (media/ig_avatars/<igsid>.jpg), повертає
    локальний URL. Так аватар не «протухає» й рендериться з нашого домену.
    Порожній рядок — якщо не вдалось завантажити."""
    if not igsid or not url:
        return ""
    img = download_image(url)
    if not img:
        return ""
    _mime, raw = img
    try:
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        path = f"ig_avatars/{igsid}.jpg"
        if default_storage.exists(path):
            default_storage.delete(path)
        saved = default_storage.save(path, ContentFile(raw))
        return default_storage.url(saved)
    except Exception as exc:
        log("warning", "avatar_store", repr(exc))
        return ""


def ensure_profile(s: InstagramBotSettings, client, force: bool = False) -> bool:
    """Підвантажує профіль у картку (ім'я/username/аватар) і локалізує аватарку.

    Оновлюється: якщо профіль ще не тягнули, або застарів (>7 днів), або немає
    локальної копії аватара (легасі-картки). На невдачі — короткий кулдаун."""
    from datetime import timedelta

    if not client:
        return False
    fresh = bool(
        client.profile_fetched_at
        and (timezone.now() - client.profile_fetched_at) < timedelta(days=7)
        and client.avatar_local
    )
    if fresh and not force:
        return False
    cd_key = f"ig_profile_cd:{client.igsid}"
    if not force and cache.get(cd_key):
        return False
    prof = fetch_ig_profile(s, client.igsid)
    if not prof or not any(prof.values()):
        try:
            cache.set(cd_key, 1, 3600)
        except Exception:
            pass
        return False
    client.display_name = (prof.get("name") or client.display_name or "")[:255]
    client.username = (prof.get("username") or client.username or "")[:120]
    pic = (prof.get("profile_pic") or "")
    if pic:
        client.profile_pic_url = pic[:600]
        local = _localize_avatar(client.igsid, pic)
        if local:
            client.avatar_local = local[:300]
    client.profile_fetched_at = timezone.now()
    client.save(update_fields=[
        "display_name", "username", "profile_pic_url", "avatar_local",
        "profile_fetched_at", "updated_at",
    ])
    return True


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------
def allowed_sender_ids(s: InstagramBotSettings) -> set[str]:
    raw = s.allowed_senders or ""
    return {p.strip() for p in raw.replace(",", " ").replace("\n", " ").split() if p.strip()}


def _is_allowed(s: InstagramBotSettings, sender_id: str) -> bool:
    ids = allowed_sender_ids(s)
    return True if not ids else sender_id in ids


# ---------------------------------------------------------------------------
# Черга: постановка вхідних
# ---------------------------------------------------------------------------
def enqueue_inbound(
    s: InstagramBotSettings, *, sender_id: str, text: str, mid: str,
    source: str = "webhook", attachments: list[str] | None = None,
    received_at: datetime | None = None,
) -> bool:
    """Кладе вхідне в чергу (pending). Повертає True, якщо додано нове."""
    text = (text or "").strip()
    sender_id = (sender_id or "").strip()
    attachments = attachments or []
    if not sender_id:
        return False
    if not text and not attachments:
        return False  # ні тексту, ні зображення
    if sender_id == s.ig_user_id:
        return False
    if not _is_allowed(s, sender_id):
        log("info", "skip_not_allowed", f"[{source}] {sender_id} поза білим списком")
        return False
    from management.services import bot_followups, bot_sales_classifier
    from management.services.ig_reply_boundary import pause_reply_boundary

    explicit_opt_out = bot_sales_classifier.is_explicit_opt_out(text)
    permission_transition = pause_reply_boundary() if explicit_opt_out else nullcontext()
    client = IgClient.get_or_create_for_sender(sender_id)
    try:
        # Opt-out follows the same lock order as send/pause: permission file
        # lock first, then database rows. Normal ingress takes no global lock.
        with permission_transition, transaction.atomic():
            current_settings = InstagramBotSettings.objects.select_for_update().get(pk=s.pk)
            # Серіалізуємо ingress із hide: або вхідне повністю оброблено до
            # приховування, або приховування вже виграло і жодного side effect
            # (черги, CRM, classifier, follow-up) не буде.
            client = IgClient.objects.select_for_update().get(pk=client.pk)
            if client.hidden_at:
                log("info", "skip_hidden", f"[{source}] {sender_id}: прихований клієнт")
                return False
            after_resume_cutoff = bool(
                not received_at
                or not current_settings.reply_after
                or received_at > current_settings.reply_after
            )
            reply_eligible = bool(
                current_settings.is_enabled
                and after_resume_cutoff
                and not _client_blocked(client)
            )
            msg = InstagramBotMessage.objects.create(
                sender_id=sender_id,
                client=client,
                role=InstagramBotMessage.Role.USER,
                text=text or "(зображення)",
                mid=mid or None,
                status=(
                    InstagramBotMessage.Status.PENDING
                    if reply_eligible
                    else InstagramBotMessage.Status.DONE
                ),
                source=source,
                attachments=json.dumps(attachments) if attachments else "",
                processed_at=None if reply_eligible else timezone.now(),
            )
            client.touch_inbound()
            # Consent is a routing barrier, not best-effort CRM enrichment. If
            # later classification fails, an explicit stop must already be
            # durable and impossible to reach Gemini or customer transport.
            if explicit_opt_out:
                opted_out_at = timezone.now()
                client.opted_out_at = opted_out_at
                client.opt_out_message_id = msg.pk
                client.bot_paused = True
                client.reply_permission_epoch = int(client.reply_permission_epoch or 0) + 1
                client.paused_reason = "opt_out"
                client.paused_at = client.paused_at or opted_out_at
                client.save(update_fields=[
                    "opted_out_at",
                    "opt_out_message_id",
                    "bot_paused",
                    "reply_permission_epoch",
                    "paused_reason",
                    "paused_at",
                    "updated_at",
                ])
                if msg.status == InstagramBotMessage.Status.PENDING:
                    msg.status = InstagramBotMessage.Status.DONE
                    msg.processed_at = opted_out_at
                    msg.save(update_fields=["status", "processed_at"])
                reply_eligible = False
                try:
                    bot_followups.cancel_pending(client, reason="opt_out")
                except DatabaseError:
                    raise
                except Exception:
                    pass
            try:
                classified = bot_sales_classifier.classify_message(client, message=msg)
                interaction_type = classified.get("interaction_type")
                terminal_followup_reasons = {
                    "explicit_no_buy": "explicit_no_buy",
                    "opt_out": "opt_out",
                    "spam_abuse": "spam_abuse",
                    "paid_order_waiting": "already_converted",
                }
                if interaction_type in terminal_followup_reasons:
                    bot_followups.cancel_pending(
                        client,
                        reason=terminal_followup_reasons[interaction_type],
                    )
                no_reply_interactions = {
                    "reaction_only",
                    "explicit_no_buy",
                    "opt_out",
                    "spam_abuse",
                }
                if interaction_type in no_reply_interactions and msg.status == InstagramBotMessage.Status.PENDING:
                    msg.status = InstagramBotMessage.Status.DONE
                    msg.processed_at = timezone.now()
                    msg.save(update_fields=["status", "processed_at"])
                    reply_eligible = False
                elif reply_eligible:
                    bot_followups.schedule_after_inbound(client)
            except DatabaseError:
                raise
            except Exception:
                pass
    except IntegrityError:
        return False  # вже у черзі/оброблено (mid unique)
    inbound_at = timezone.now()
    InstagramBotSettings.objects.filter(pk=s.pk).update(last_inbound_at=inbound_at)
    s.last_inbound_at = inbound_at
    extra = f" (+{len(attachments)} фото)" if attachments else ""
    event = "queued" if msg.status == InstagramBotMessage.Status.PENDING else "observed"
    log("info", event, f"[{source}] {sender_id}: {text[:140]}{extra}")
    return True


# ---------------------------------------------------------------------------
# Воркер: обробка черги
# ---------------------------------------------------------------------------
def _build_history(sender_id: str) -> list[dict]:
    rows = list(
        InstagramBotMessage.objects.filter(sender_id=sender_id)
        .exclude(status=InstagramBotMessage.Status.FAILED)
        .order_by("-id")[:HISTORY_LIMIT]
    )
    rows.reverse()
    hist = []
    for r in rows:
        t = (r.text or "").strip()
        if t:
            if r.role == InstagramBotMessage.Role.MANAGER:
                hist.append({"role": "model", "text": "Менеджер: " + t})
            elif r.role in (InstagramBotMessage.Role.USER, InstagramBotMessage.Role.MODEL):
                hist.append({"role": r.role, "text": t})
    return hist


def _claim_next() -> InstagramBotMessage | None:
    """Атомарно (умовний UPDATE) забирає найстаріше pending-вхідне."""
    row = (
        InstagramBotMessage.objects.filter(
            role=InstagramBotMessage.Role.USER,
            status=InstagramBotMessage.Status.PENDING,
            client__hidden_at__isnull=True,
        )
        .order_by("id")
        .first()
    )
    if not row:
        return None
    claimed_at = timezone.now()
    claimed = InstagramBotMessage.objects.filter(
        id=row.id, status=InstagramBotMessage.Status.PENDING
    ).update(
        status=InstagramBotMessage.Status.PROCESSING,
        attempts=row.attempts + 1,
        processing_started_at=claimed_at,
    )
    if claimed == 1:
        row.status = InstagramBotMessage.Status.PROCESSING
        row.attempts += 1
        row.processing_started_at = claimed_at
        return row
    return None  # гонка — забрав хтось інший


STALE_PROCESSING_SECONDS = 300  # повідомлення «зависло» у processing довше — реанімуємо


def reclaim_stale_processing(max_age_seconds: int = STALE_PROCESSING_SECONDS) -> int:
    """Повертає в чергу повідомлення, що «зависли» у processing довше за поріг.

    Причини зависання: демона вбили під час обробки (status лишився processing і
    рядок ніколи не переклеймиться), або виклик Gemini тривав надто довго. Без
    цього бот може заморозитись назовсім. attempts<MAX → знову pending, інакше
    failed. Повертає к-сть повернутих у чергу."""
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(seconds=max_age_seconds)
    stale = list(
        InstagramBotMessage.objects.select_related("client").filter(
            role=InstagramBotMessage.Role.USER,
            status=InstagramBotMessage.Status.PROCESSING,
            processing_started_at__lt=cutoff,
        ).order_by("id")[:50]
    )
    requeued = 0
    for row in stale:
        # Короткі locks тільки для рішення. Gemini/Meta I/O тут немає.
        # Порядок lock-ів збігається з Hide та lease: спершу клієнт, потім row.
        with transaction.atomic():
            client = None
            if row.client_id:
                client = IgClient.objects.select_for_update().filter(pk=row.client_id).first()
                if client and client_automation_busy(client):
                    continue
            locked = InstagramBotMessage.objects.select_for_update().filter(
                id=row.id,
                role=InstagramBotMessage.Role.USER,
                status=InstagramBotMessage.Status.PROCESSING,
                processing_started_at__lt=cutoff,
            ).first()
            if not locked:
                continue
            if locked.send_state in {"sending", "sent", "unknown"}:
                locked.status = InstagramBotMessage.Status.FAILED
                locked.send_state = "unknown"
                locked.processed_at = timezone.now()
                locked.save(update_fields=["status", "send_state", "processed_at"])
                log(
                    "error",
                    "send_unknown",
                    f"{locked.sender_id}: stale row crossed Meta send boundary; automatic retry disabled",
                )
                continue
            if locked.attempts >= MAX_ATTEMPTS:
                locked.status = InstagramBotMessage.Status.FAILED
                locked.processed_at = timezone.now()
                locked.save(update_fields=["status", "processed_at"])
                log("error", "stale_failed", f"{locked.sender_id}: завис у processing, спроби вичерпано")
            else:
                locked.status = InstagramBotMessage.Status.PENDING
                locked.processing_started_at = None
                locked.save(update_fields=["status", "processing_started_at"])
                log("warning", "stale_requeue", f"{locked.sender_id}: завис у processing → повертаю в чергу")
                requeued += 1
    return requeued


def _own_processing_claim(row: InstagramBotMessage):
    """Return a conditional update queryset for exactly this worker claim."""
    claim = InstagramBotMessage.objects.filter(
        pk=row.pk,
        status=InstagramBotMessage.Status.PROCESSING,
    )
    if row.processing_started_at:
        return claim.filter(processing_started_at=row.processing_started_at)
    return claim.filter(processing_started_at__isnull=True)


def _skip_blocked_row(row: InstagramBotMessage, client: IgClient) -> bool:
    processed_at = timezone.now()
    if _own_processing_claim(row).update(
        status=InstagramBotMessage.Status.DONE,
        processed_at=processed_at,
    ):
        row.status = InstagramBotMessage.Status.DONE
        row.processed_at = processed_at
        log("info", "paused_skip", f"{row.sender_id}: на паузі ({client.paused_reason or 'manual'})")
    return False


def _skip_observed_row(row: InstagramBotMessage, *, reason: str) -> bool:
    processed_at = timezone.now()
    if _own_processing_claim(row).update(
        status=InstagramBotMessage.Status.DONE,
        processed_at=processed_at,
    ):
        row.status = InstagramBotMessage.Status.DONE
        row.processed_at = processed_at
        log("info", "observed_skip", f"{row.sender_id}: {reason}")
    return False


def client_automation_busy(client: IgClient | None, *, now: datetime | None = None) -> bool:
    now = now or timezone.now()
    return bool(
        client
        and client.automation_lease_token
        and client.automation_lease_until
        and client.automation_lease_until > now
    )


def _lease_client_automation(
    client_id: int | None, *, token: str = ""
) -> tuple[IgClient | None, str, str]:
    """Atomically acquire or renew the short lease shared by all bot sends.

    Returns ``(client, token, state)`` where state is one of ``acquired``,
    ``renewed``, ``blocked``, ``busy``, ``token_lost`` or ``missing``. The
    transaction contains only the state transition, never external I/O.
    """
    if not client_id:
        return None, "", "missing"
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=client_id).first()
        if not client:
            return None, "", "missing"
        if _client_blocked(client):
            return client, "", "blocked"
        now = timezone.now()
        if token:
            if client.automation_lease_token != token:
                return client, "", "token_lost"
            client.automation_lease_until = now + AUTOMATION_LEASE_TTL
            client.save(update_fields=["automation_lease_until", "updated_at"])
            return client, token, "renewed"
        if client_automation_busy(client, now=now):
            return client, "", "busy"
        lease_token = secrets.token_hex(16)
        client.automation_lease_token = lease_token
        client.automation_lease_until = now + AUTOMATION_LEASE_TTL
        client.save(update_fields=[
            "automation_lease_token", "automation_lease_until", "updated_at",
        ])
        return client, lease_token, "acquired"


def acquire_client_automation_lease(client_id: int | None) -> tuple[IgClient | None, str]:
    """Lease one client for a bot send; returns no token when it is unavailable."""
    client, token, state = _lease_client_automation(client_id)
    return (client, token) if state == "acquired" else (None, "")


def renew_client_automation_lease(client_id: int | None, token: str) -> IgClient | None:
    """Renew a held client lease immediately before a send boundary."""
    client, _token, state = _lease_client_automation(client_id, token=token)
    return client if state == "renewed" else None


def _requeue_for_active_lease(row: InstagramBotMessage) -> bool:
    if _own_processing_claim(row).update(
        status=InstagramBotMessage.Status.PENDING,
        processed_at=None,
        processing_started_at=None,
    ):
        row.status = InstagramBotMessage.Status.PENDING
        row.processed_at = None
        row.processing_started_at = None
        log("info", "lease_busy", f"{row.sender_id}: інший worker ще обробляє клієнта")
    else:
        log("info", "claim_lost", f"{row.sender_id}: row уже належить іншому worker-у")
    return False


def _acquire_client_automation_lease(
    row: InstagramBotMessage,
) -> tuple[IgClient | None, str]:
    if not row.client_id:
        return None, ""
    client, token, state = _lease_client_automation(row.client_id)
    if state == "acquired":
        # Reclaim may have won just before we acquired the client lease. Do not
        # let this stale Python object send after its DB row returned to pending.
        if not _own_processing_claim(row).exists():
            release_client_automation_lease(client.id, token)
            log("info", "claim_lost", f"{row.sender_id}: row вже повернуто в чергу")
            return None, ""
        row.client = client  # не використовуємо застарілий relation-cache після claim.
        return client, token
    if state == "blocked" and client:
        _skip_blocked_row(row, client)
    elif state in {"busy", "token_lost"}:
        _requeue_for_active_lease(row)
    else:
        processed_at = timezone.now()
        if _own_processing_claim(row).update(
            status=InstagramBotMessage.Status.DONE,
            processed_at=processed_at,
        ):
            row.status = InstagramBotMessage.Status.DONE
            row.processed_at = processed_at
            log("warning", "client_missing", f"{row.sender_id}: картку клієнта не знайдено")
    return None, ""


def _renew_client_automation_lease(row: InstagramBotMessage, token: str) -> bool:
    """Refresh a short lease before each automation boundary, never over I/O."""
    if not row.client_id:
        return True
    client, _token, state = _lease_client_automation(row.client_id, token=token)
    if state == "renewed":
        row.client = client
        return True
    if state == "blocked" and client:
        return _skip_blocked_row(row, client)
    return _requeue_for_active_lease(row)


def release_client_automation_lease(client_id: int | None, token: str) -> None:
    if not client_id or not token:
        return
    IgClient.objects.filter(pk=client_id, automation_lease_token=token).update(
        automation_lease_token="", automation_lease_until=None
    )


def _release_client_automation_lease(client_id: int | None, token: str) -> None:
    """Backward-compatible internal alias for the inbound worker."""
    release_client_automation_lease(client_id, token)


def _process_one(s: InstagramBotSettings, row: InstagramBotMessage) -> bool:
    client, lease_token = _acquire_client_automation_lease(row)
    if row.client_id and not client:
        return False
    try:
        return _process_one_unlocked(s, row, lease_token)
    finally:
        _release_client_automation_lease(row.client_id, lease_token)


def _process_one_unlocked(s: InstagramBotSettings, row: InstagramBotMessage, lease_token: str = "") -> bool:
    from management.services.ig_reply_boundary import reply_execution_boundary

    with reply_execution_boundary(s.pk, row.client_id) as permission:
        if not permission:
            return _skip_observed_row(row, reason="reply_paused")
        return _process_one_inside_reply_boundary(s, row, lease_token, permission)


def _process_one_inside_reply_boundary(
    s: InstagramBotSettings,
    row: InstagramBotMessage,
    lease_token: str = "",
    permission=None,
) -> bool:
    if not InstagramBotSettings.objects.filter(pk=s.pk, is_enabled=True).exists():
        return _skip_observed_row(row, reason="global_reply_paused")
    if not row.attachments:
        try:
            from management.services.bot_sales_classifier import is_reaction_only

            if is_reaction_only(row.text):
                processed_at = timezone.now()
                updated = _own_processing_claim(row).update(
                    status=InstagramBotMessage.Status.DONE,
                    processed_at=processed_at,
                )
                if updated:
                    row.status = InstagramBotMessage.Status.DONE
                    row.processed_at = processed_at
                    log("info", "reaction_observed", f"{row.sender_id}: реакція без auto-reply")
                    return True
                return False
        except Exception:
            pass
    # Захоплення телефону клієнта (лід), якщо ще немає.
    if row.client_id:
        try:
            _maybe_capture_phone(row.client, row.text)
        except Exception:
            pass
    # Анти-спам: ліміт відповідей на одного відправника.
    if _rate_exceeded(s, row.sender_id):
        row.status = InstagramBotMessage.Status.DONE
        row.processed_at = timezone.now()
        row.save(update_fields=["status", "processed_at"])
        log("warning", "rate_limited", f"{row.sender_id}: перевищено ліміт відповідей")
        if not cache.get(f"ig_bot_rate_notified:{row.sender_id}"):
            cache.set(f"ig_bot_rate_notified:{row.sender_id}", 1, 3600)
            notify_manager(f"⚠️ IG бот: відправник {row.sender_id} перевищив ліміт повідомлень (можливий спам).")
        return False

    if s.ai_enabled:
        # Відразу показуємо клієнту, що бот побачив і «друкує» (best practice).
        if not _renew_client_automation_lease(row, lease_token):
            return False
        send_sender_action(s, row.sender_id, "mark_seen")
        send_sender_action(s, row.sender_id, "typing_on")
        # Підвантажуємо профіль клієнта (раз на картку) для CRM.
        if row.client_id and not row.client.profile_fetched_at:
            try:
                ensure_profile(s, row.client)
            except Exception:
                pass
        # Анти-абуз: однакове питання багато разів — не жжемо токени Gemini.
        rep = _repeated_question(row.sender_id, row.text)
        if rep > 3 and not row.attachments:
            reply = "Я вже відповів(-ла) на це трохи вище 🙂 Якщо потрібно щось інше — уточніть, будь ласка."
            log("info", "repeat_guard", f"{row.sender_id}: повтор #{rep}, без Gemini")
        else:
            if not _renew_client_automation_lease(row, lease_token):
                return False
            history = _build_history(row.sender_id)
            if not history:
                history = [{"role": "user", "text": row.text}]
            # Зображення-вкладення (фото, пересланий пост, reels, сторіс) ->
            # мультимодальний вхід Gemini.
            images = _collect_images(row.attachments)
            if not _renew_client_automation_lease(row, lease_token):
                return False
            # Якщо є фото/пост — матчимо з каталогом і даємо моделі підказку.
            match_hint = None
            if images and _match_allowed(row.sender_id):
                try:
                    from management.services import bot_vision

                    match = bot_vision.match(images)
                    if not _renew_client_automation_lease(row, lease_token):
                        return False
                    match_hint = _match_hint_text(match)
                    # Впевнений матчинг → закріплюємо товар за клієнтом.
                    if row.client_id:
                        _maybe_pin_from_match(row.client, match)
                except Exception as exc:
                    log("warning", "match", repr(exc))
            # Пам'ять про клієнта (rolling summary) + контекст (реклама/постійний) —
            # щоб бот одразу орієнтувався.
            mem_note = None
            ctx_note = None
            if row.client_id:
                try:
                    from management.services import bot_memory

                    mem_note = bot_memory.memory_note(row.client)
                    ctx_note = bot_memory.client_context_note(row.client)
                except Exception:
                    pass
            reply = gemini_generate(
                s, history, images=images or None, match_hint=match_hint,
                memory_note=mem_note, context_note=ctx_note, client=row.client if row.client_id else None,
            )
    else:
        if (row.text or "").strip() != s.trigger_text:
            row.status = InstagramBotMessage.Status.DONE
            row.processed_at = timezone.now()
            row.save(update_fields=["status", "processed_at"])
            log("info", "ignored", f"{row.sender_id}: не тригер")
            return False
        reply = s.reply_text

    if not _renew_client_automation_lease(row, lease_token):
        return False

    if not InstagramBotSettings.objects.filter(pk=s.pk, is_enabled=True).exists():
        return _skip_observed_row(row, reason="global_reply_paused_before_send")

    # Керуючі теги моделі: [MANAGER] (ескалація), [STAGE:x] (воронка) тощо.
    control = {}
    if reply:
        reply, control = _extract_control(reply)
    needs_manager = bool(control.get("manager"))

    # Закріплюємо товар, якщо модель явно вказала [PRODUCT:id] — щоб подальша
    # оплата формувалась детерміновано саме на нього.
    if reply and row.client_id and control.get("product"):
        try:
            from management.services import bot_orders

            bot_orders.pin_product(row.client, control.get("product"))
        except Exception:
            pass

    # [SPAM] — модель розпізнала спам/провокацію: рахуємо страйк (на 3-й — пауза).
    if reply and row.client_id and control.get("spam"):
        try:
            _register_spam(row.client)
        except Exception:
            pass

    # Формування посилання на оплату (guard «обіцяв → надішли або не обіцяй»):
    # finalize_paylink гарантує, що клієнт НЕ лишиться з обіцянкою без лінку —
    # на успіх додає реальний URL (вирізаючи вигаданий моделлю), на невдачу
    # прибирає висяче обіцяння й кличе менеджера.
    if reply and row.client_id:
        reply = finalize_paylink(reply, control, row.client, row.sender_id)

    if not reply:
        # невдача генерації — ретрай або failed
        if row.attempts >= MAX_ATTEMPTS:
            row.status = InstagramBotMessage.Status.FAILED
            row.save(update_fields=["status"])
            log("error", "give_up", f"{row.sender_id}: не вдалося згенерувати після {row.attempts} спроб")
            notify_manager(
                f"⚠️ IG бот не зміг згенерувати відповідь клієнту {row.sender_id} "
                f"(3 спроби). Питання: {row.text[:300]}"
            )
        else:
            row.status = InstagramBotMessage.Status.PENDING
            row.processing_started_at = None
            row.save(update_fields=["status", "processing_started_at"])
        return False

    # Останнє продовження lease прямо перед Meta Send API. Поки send триває,
    # hide не поверне помилковий success: UI отримає чесний retryable-конфлікт.
    if not _renew_client_automation_lease(row, lease_token):
        return False
    from management.services.ig_reply_boundary import customer_send_boundary

    # The global lock is held only across the claim/revalidation.  Each Meta
    # chunk below takes its own short send boundary, so slow generation and
    # unrelated chunks never block a stop for the whole response.
    with customer_send_boundary(s.pk, row.client_id, permission) as send_allowed:
        if not send_allowed:
            return _skip_observed_row(row, reason="permission_epoch_changed")
        send_started_at = timezone.now()
        if not _own_processing_claim(row).update(
            send_state="sending", send_started_at=send_started_at, send_completed_at=None,
        ):
            log("warning", "claim_lost", f"{row.sender_id}: send claim lost before Meta request")
            return False
        row.send_state = "sending"
        row.send_started_at = send_started_at
        row.send_completed_at = None
    ok, kind, hint = send_text(
        s,
        row.sender_id,
        reply,
        permission_boundary_factory=lambda: customer_send_boundary(
            s.pk, row.client_id, permission
        ),
    )
    if kind == "cancelled":
        cancelled_at = timezone.now()
        if _own_processing_claim(row).update(
            send_state="cancelled",
            processed_at=cancelled_at,
        ):
            row.send_state = "cancelled"
            row.processed_at = cancelled_at
        return _skip_observed_row(row, reason="permission_epoch_changed")
    if not ok:
        if kind == "permanent":
            # Перманентна помилка (напр. #200 немає Advanced Access) — ретраї
            # безглузді. Падаємо одразу з чіткою причиною.
            row.status = InstagramBotMessage.Status.FAILED
            row.send_state = "failed"
            row.processed_at = timezone.now()
            row.save(update_fields=["status", "send_state", "processed_at"])
            log("error", "send_blocked", f"{row.sender_id}: {hint}")
            # Системну причину (одна на всіх) не спамимо — алерт раз на годину.
            if not cache.get("ig_bot_perm_alert"):
                cache.set("ig_bot_perm_alert", 1, 3600)
                notify_manager(
                    f"❗️ IG бот не може відповідати неролевим користувачам.\n"
                    f"Причина: {hint}.\n\n"
                    f"Щоб відповідати ВСІМ — подай instagram_manage_messages на "
                    f"App Review (Advanced Access). Для тесту — додай користувача "
                    f"в тестувальники. (Це системне; алерт раз на годину.)"
                )
        elif kind == "unknown":
            # Never replay a request whose provider result is ambiguous.
            row.status = InstagramBotMessage.Status.FAILED
            row.send_state = "unknown"
            row.processed_at = timezone.now()
            row.save(update_fields=["status", "send_state", "processed_at"])
            log("error", "send_unknown", f"{row.sender_id}: {hint}; automatic retry disabled")
            notify_manager(
                f"⚠️ IG бот: результат доставки клієнту {row.sender_id} не підтверджено. "
                "Автоматичний повтор вимкнено, перевірте Meta Inbox."
            )
        elif row.attempts >= MAX_ATTEMPTS:
            row.status = InstagramBotMessage.Status.FAILED
            row.send_state = "failed"
            row.processed_at = timezone.now()
            row.save(update_fields=["status", "send_state", "processed_at"])
            log("error", "give_up", f"{row.sender_id}: не вдалося відправити після {row.attempts} спроб ({hint})")
            notify_manager(
                f"⚠️ IG бот не зміг відповісти клієнту {row.sender_id} після {row.attempts} спроб. "
                f"Причина: {hint}. Питання: {row.text[:300]}"
            )
        else:
            row.status = InstagramBotMessage.Status.PENDING
            row.processing_started_at = None
            row.save(update_fields=["status", "processing_started_at"])
        return False

    # успіх: фіксуємо відповідь у локальній історії
    processed_at = timezone.now()
    claimed = _own_processing_claim(row).update(
        status=InstagramBotMessage.Status.DONE,
        send_state="sent",
        send_completed_at=processed_at,
        processed_at=processed_at,
    )
    if not claimed:
        # The provider already received the message; never run the row again.
        log("warning", "claim_lost_after_send", f"{row.sender_id}: Meta send succeeded")
        return True
    row.status = InstagramBotMessage.Status.DONE
    row.send_state = "sent"
    row.send_completed_at = processed_at
    row.processed_at = processed_at
    InstagramBotMessage.objects.create(
        sender_id=row.sender_id,
        client=row.client,
        role=InstagramBotMessage.Role.MODEL,
        text=reply,
        status=InstagramBotMessage.Status.DONE,
        source=row.source,
        processed_at=timezone.now(),
    )
    s.replies_count = (s.replies_count or 0) + 1
    s.last_reply_at = timezone.now()
    s.save(update_fields=["replies_count", "last_reply_at"])
    log("success", "reply_sent", f"→ {row.sender_id}: {reply[:240]}")
    # Періодично оновлюємо стислу пам'ять про клієнта.
    if row.client_id:
        post_send_client = renew_client_automation_lease(row.client_id, lease_token)
        if not post_send_client:
            return True
        row.client = post_send_client
        try:
            from management.services.bot_memory import maybe_update_memory

            maybe_update_memory(row.client)
        except Exception:
            pass
        post_send_client = renew_client_automation_lease(row.client_id, lease_token)
        if not post_send_client:
            return True
        row.client = post_send_client
        # Просування воронки за тегом [STAGE:x].
        _apply_stage(row.client, control.get("stage"))
        try:
            from management.services import bot_followups

            row.client.refresh_from_db()
            bot_followups.schedule_after_bot_reply(row.client, reply=reply, control=control)
        except Exception as exc:
            log("warning", "followup_schedule", repr(exc))
        # [ORDER] або safety-net: оплачений клієнт надіслав контактні дані, а
        # модель не виставила тег — все одно намагаємось зібрати дані й створити заказ.
        from management.services.bot_payment_truth import client_has_verified_payment

        if control.get("order") or (
            _looks_like_contact_info(row.text)
            and client_has_verified_payment(row.client)
        ):
            try:
                from management.services import bot_orders

                bot_orders.collect_np_and_fulfill(row.client)
            except Exception:
                pass
    if needs_manager:
        if row.client_id:
            try:
                _apply_stage(row.client, IgClient.Stage.LEAD_TO_MANAGER)
            except Exception:
                pass
        notify_manager(
            f"🔔 IG Direct — клієнту потрібен менеджер.\n"
            f"IGSID: {row.sender_id}\nПитання: {row.text[:400]}"
        )
        log("warning", "escalation", f"{row.sender_id}: викликано менеджера")
    _trim_messages()
    return True


def process_pending(s: InstagramBotSettings | None = None, max_items: int = 15) -> int:
    s = s or InstagramBotSettings.load()
    if not s.is_enabled:
        return 0
    # Реанімація «зависань» у processing (вбитий демон / надто довгий виклик).
    try:
        reclaim_stale_processing()
    except Exception as exc:
        log("warning", "reclaim", repr(exc))
    handled = 0
    for _ in range(max_items):
        row = _claim_next()
        if not row:
            break
        try:
            if _process_one(s, row):
                handled += 1
        except Exception as exc:
            log("error", "process", repr(exc))
            # Після успішного Meta Send рядок уже позначено done. Не можна
            # повертати його в pending через пізній збій CRM/телеметрії — це
            # призведе до дубльованої відповіді клієнту.
            if row.send_state == "sending":
                _own_processing_claim(row).update(
                    status=InstagramBotMessage.Status.FAILED,
                    send_state="unknown",
                    processed_at=timezone.now(),
                )
            else:
                _own_processing_claim(row).update(
                    status=InstagramBotMessage.Status.PENDING,
                    processing_started_at=None,
                )
            break
    return handled


def pending_count() -> int:
    return InstagramBotMessage.objects.filter(
        role=InstagramBotMessage.Role.USER,
        status=InstagramBotMessage.Status.PENDING,
        client__hidden_at__isnull=True,
    ).count()


def unique_senders_count() -> int:
    """Кількість активних співрозмовників у роботі бота.

    Приховані картки не є частиною робочої черги чи overview-метрики.
    """
    return IgClient.objects.filter(hidden_at__isnull=True).count()


def link_orphan_messages_to_clients() -> int:
    """Прив'язує повідомлення без картки до IgClient (бекофіл легасі історії).

    Для кожного унікального sender_id без картки створює/знаходить IgClient,
    проставляє first_contact_at/last_message_at з історії і лінкує повідомлення.
    Повертає кількість задіяних карток. Ідемпотентна (другий запуск → 0).
    """
    from django.db.models import Max, Min

    sender_ids = list(
        InstagramBotMessage.objects.filter(client__isnull=True)
        .exclude(sender_id="")
        .order_by("sender_id")  # скидаємо Meta.ordering=['id'], інакше distinct ламається
        .values_list("sender_id", flat=True)
        .distinct()
    )
    count = 0
    for sid in sender_ids:
        client = IgClient.get_or_create_for_sender(sid)
        agg = InstagramBotMessage.objects.filter(sender_id=sid).aggregate(
            first=Min("created_at"), last=Max("created_at")
        )
        fields = []
        if not client.first_contact_at and agg["first"]:
            client.first_contact_at = agg["first"]
            fields.append("first_contact_at")
        if agg["last"]:
            client.last_message_at = agg["last"]
            fields.append("last_message_at")
        if fields:
            fields.append("updated_at")
            client.save(update_fields=fields)
        InstagramBotMessage.objects.filter(sender_id=sid, client__isnull=True).update(client=client)
        count += 1
    return count


def _trim_messages() -> None:
    try:
        if InstagramBotMessage.objects.count() > MSG_KEEP_ROWS + 200:
            ids = list(
                InstagramBotMessage.objects.order_by("-id").values_list("id", flat=True)[:MSG_KEEP_ROWS]
            )
            if ids:
                InstagramBotMessage.objects.exclude(id__in=ids).delete()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Сире логування подій (Phase 0 / Task 1) — діагностика форматів вебхуків
# ---------------------------------------------------------------------------
RAW_EVENT_KEEP_ROWS = 400


def _iter_events(payload: dict):
    """Yield (sender_id, recipient_id, message_dict, referral_dict) з payload.

    Покриває обидва канали доставки Meta: entry[].messaging[] (Send/Receive)
    та entry[].changes[] з field=messages (деякі IG-події). Referral береться
    і з події, і з postback.referral (перший контакт із Click-to-IG реклами).
    recipient_id потрібен для echo (повідомлення сторінки/менеджера клієнту).
    """
    if not isinstance(payload, dict):
        return
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        messaging = entry.get("messaging")
        if isinstance(messaging, list):
            for event in messaging:
                if not isinstance(event, dict):
                    continue
                raw_message = event.get("message")
                message = dict(raw_message) if isinstance(raw_message, dict) else {}
                raw_referral = event.get("referral")
                postback = event.get("postback")
                postback_referral = postback.get("referral") if isinstance(postback, dict) else None
                message["_event_created_at"] = _provider_event_datetime(
                    event.get("timestamp") or entry.get("time")
                )
                sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
                recipient = event.get("recipient") if isinstance(event.get("recipient"), dict) else {}
                ref = raw_referral if isinstance(raw_referral, dict) else (
                    postback_referral if isinstance(postback_referral, dict) else {}
                )
                yield (
                    sender.get("id", ""),
                    recipient.get("id", ""),
                    message,
                    ref,
                )
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            raw_message = value.get("message")
            message = dict(raw_message) if isinstance(raw_message, dict) else {}
            message["_event_created_at"] = _provider_event_datetime(
                value.get("timestamp") or entry.get("time")
            )
            sender = value.get("sender") if isinstance(value.get("sender"), dict) else {}
            recipient = value.get("recipient") if isinstance(value.get("recipient"), dict) else {}
            ref = value.get("referral") if isinstance(value.get("referral"), dict) else {}
            yield (sender.get("id", ""), recipient.get("id", ""), message, ref)


def _provider_event_datetime(raw) -> datetime | None:
    try:
        value = float(raw)
        if value > 10_000_000_000:
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


_WEBHOOK_EVENT_KEYS = frozenset({
    "sender", "recipient", "timestamp", "message", "postback", "referral",
    "reaction", "read", "delivery", "attachments", "is_echo", "is_deleted",
    "is_unsupported", "optin", "account_linking", "standby",
})
_WEBHOOK_MESSAGE_KEYS = frozenset({
    "mid", "text", "attachments", "is_echo", "is_deleted", "is_unsupported",
    "reply_to", "quick_reply", "nfm_reply", "story", "referral",
})


def _webhook_observation_summary(payload: dict) -> str:
    """Return bounded evidence counters for valid and ignored webhook shapes."""
    counts: dict[str, int] = {}
    unknown_fields = 0
    malformed = 0

    def bump(kind: str, amount: int = 1) -> None:
        counts[kind] = counts.get(kind, 0) + amount

    if not isinstance(payload, dict) or not isinstance(payload.get("entry"), list):
        return "malformed=1"
    for entry in payload["entry"]:
        if not isinstance(entry, dict):
            malformed += 1
            continue
        messaging = entry.get("messaging")
        if messaging is not None and not isinstance(messaging, list):
            malformed += 1
        for event in messaging if isinstance(messaging, list) else []:
            if not isinstance(event, dict):
                malformed += 1
                continue
            unknown_fields += len(set(event) - _WEBHOOK_EVENT_KEYS)
            message = event.get("message")
            if isinstance(message, dict):
                unknown_fields += len(set(message) - _WEBHOOK_MESSAGE_KEYS)
                if message.get("is_echo"):
                    bump("echo")
                elif message.get("is_deleted"):
                    bump("delete")
                elif message.get("is_unsupported"):
                    bump("unsupported")
                else:
                    bump("message")
            elif isinstance(event.get("postback"), dict):
                bump("postback")
            elif isinstance(event.get("reaction"), dict):
                bump("reaction")
            elif any(key in event for key in ("read", "delivery", "optin", "account_linking")):
                bump("control")
            else:
                bump("unknown")
        changes = entry.get("changes")
        if changes is not None and not isinstance(changes, list):
            malformed += 1
        for change in changes if isinstance(changes, list) else []:
            if not isinstance(change, dict):
                malformed += 1
                continue
            unknown_fields += len(set(change) - {"field", "value"})
            field = str(change.get("field") or "unknown")
            if field == "messages" and isinstance(change.get("value"), dict):
                bump("message")
            elif field in {"messaging_postbacks", "postbacks"}:
                bump("postback")
            elif field in {"message_reactions", "reactions"}:
                bump("reaction")
            else:
                bump("unknown_change")
    if unknown_fields:
        bump("unknown_fields", unknown_fields)
    if malformed:
        bump("malformed", malformed)
    return ",".join(f"{key}={counts[key]}" for key in sorted(counts))[:255]


def record_raw_event(payload: dict):
    """Зберігає сирий вебхук + витягнуті ознаки (типи вкладень, referral, echo).

    Best-effort: ніколи не кидає, щоб не зламати прийом вебхука. Підрізає
    найстаріші рядки, щоб не накопичувати нескінченно.
    """
    from management.models import InstagramBotRawEvent

    sender_id = ""
    att_types: list[str] = []
    has_referral = False
    has_echo = False
    try:
        for sid, _rid, msg, ref in _iter_events(payload):
            if sid and not sender_id:
                sender_id = sid
            if msg.get("is_echo"):
                has_echo = True
            for att in (msg.get("attachments") or []):
                t = att.get("type") or "unknown"
                if t not in att_types:
                    att_types.append(t)
            if ref or msg.get("referral"):
                has_referral = True
    except Exception:
        pass
    try:
        raw = json.dumps(payload, ensure_ascii=False)[:20000]
    except Exception:
        raw = str(payload)[:20000]
    ev = InstagramBotRawEvent.objects.create(
        sender_id=(sender_id or "")[:64],
        attachment_types=",".join(att_types)[:255],
        has_referral=has_referral,
        has_echo=has_echo,
        note=_webhook_observation_summary(payload),
        payload=raw,
    )
    try:
        if InstagramBotRawEvent.objects.count() > RAW_EVENT_KEEP_ROWS + 100:
            ids = list(
                InstagramBotRawEvent.objects.order_by("-id").values_list("id", flat=True)[:RAW_EVENT_KEEP_ROWS]
            )
            if ids:
                InstagramBotRawEvent.objects.exclude(id__in=ids).delete()
    except Exception:
        pass
    return ev


# ---------------------------------------------------------------------------
# Webhook payload -> черга (швидко, без важкої логіки)
# ---------------------------------------------------------------------------
MEDIA_ATTACH_TYPES = {
    "image", "share", "ig_reel", "reel", "story_mention", "story", "video", "file", "link",
}
MEDIA_MAX = 3


def _extract_media_urls(msg: dict) -> list[str]:
    """Збирає завантажувані URL з повідомлення: вкладення будь-якого медіа-типу
    (а не лише image) + відповідь на сторіс (reply_to.story.url). Дедуп, cap.

    download_image() сам відсіє не-image (відео/файл), тож їх URL безпечні.
    """
    urls: list[str] = []
    for att in (msg.get("attachments") or []):
        t = (att.get("type") or "").lower()
        if t not in MEDIA_ATTACH_TYPES:
            continue
        u = (att.get("payload") or {}).get("url")
        if u:
            urls.append(u)
    story = (msg.get("reply_to") or {}).get("story") or {}
    if story.get("url"):
        urls.append(story["url"])
    out: list[str] = []
    for u in urls:
        if u and u not in out:
            out.append(u)
    return out[:MEDIA_MAX]


def _apply_referral(sender_id: str, ref: dict) -> None:
    """Зберігає атрибуцію реклами (Click-to-IG-Direct) у картку клієнта.

    ref містить ref/ad_id/source та ads_context_data (ad_title, photo_url/
    video_url). Це дає боту зрозуміти, ЩО продавала реклама, ще до питань.
    """
    if not ref:
        return
    client = IgClient.get_or_create_for_sender(sender_id)
    acd = ref.get("ads_context_data") or {}
    client.ad_ref = (str(ref.get("ref") or ""))[:255]
    client.ad_id = (str(ref.get("ad_id") or ""))[:64]
    client.ad_source = (str(ref.get("source") or ""))[:64]
    client.ad_title = (str(acd.get("ad_title") or ""))[:255]
    client.ad_creative_url = (str(acd.get("photo_url") or acd.get("video_url") or ""))[:600]
    try:
        client.referral_payload = ref
    except Exception:
        client.referral_payload = {}
    client.save(update_fields=[
        "ad_ref", "ad_id", "ad_source", "ad_title", "ad_creative_url",
        "referral_payload", "updated_at",
    ])


def handle_webhook_payload(s: InstagramBotSettings, payload: dict) -> int:
    """Розбирає payload вебхука і кладе вхідні в чергу. Повертає к-сть доданих.

    Echo (повідомлення сторінки/менеджера) поки пропускаємо для черги — їх
    використає авто-перехоплення менеджером (Task 21).
    """
    enq = 0
    for sender_id, recipient_id, msg, ref in _iter_events(payload):
        if not msg:
            continue
        if msg.get("is_deleted") or msg.get("is_unsupported"):
            continue
        # Echo (повідомлення сторінки/менеджера) → перехоплення менеджером.
        if msg.get("is_echo"):
            try:
                _handle_echo(recipient_id, msg.get("text", ""))
            except Exception as exc:
                log("warning", "echo", repr(exc))
            continue
        if not sender_id:
            continue
        if ref:
            try:
                _apply_referral(sender_id, ref)
            except Exception as exc:
                log("warning", "referral", repr(exc))
        media = _extract_media_urls(msg)
        if enqueue_inbound(
            s,
            sender_id=sender_id,
            text=msg.get("text", ""),
            mid=msg.get("mid", ""),
            source="webhook",
            attachments=media,
            received_at=msg.get("_event_created_at"),
        ):
            enq += 1
    return enq


# ---------------------------------------------------------------------------
# Polling (резервний міст до Live) -> кладе в чергу
# ---------------------------------------------------------------------------
def _parse_ig_time(raw: str):
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None


def _valid_message_id(value) -> bool:
    return bool(
        isinstance(value, str)
        and 0 < len(value.strip()) <= 512
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def _poll_offset_cache_key(s: InstagramBotSettings) -> str:
    return f"ig_bot_poll_offset:{s.page_id or 'unknown'}"


def _poll_conversation_order(s: InstagramBotSettings, conv_ids: list[str]) -> tuple[list[str], int]:
    if not conv_ids:
        return [], 0
    raw_offset = cache.get(_poll_offset_cache_key(s))
    try:
        offset = int(raw_offset or 0) % len(conv_ids)
    except (TypeError, ValueError):
        offset = 0
    return conv_ids[offset:] + conv_ids[:offset], offset


def _polled_message_key(message: dict) -> tuple[datetime, str]:
    return (
        _parse_ig_time(message.get("created_time", ""))
        or datetime.min.replace(tzinfo=dt_timezone.utc),
        message["id"],
    )


def _validate_polled_page(envelope) -> tuple[list[dict], str]:
    if not isinstance(envelope, dict):
        raise ValueError("malformed envelope")
    messages_block = envelope.get("messages")
    if messages_block is None:
        messages_block = {}
    if not isinstance(messages_block, dict):
        raise ValueError("malformed messages")
    messages = messages_block.get("data", [])
    if not isinstance(messages, list):
        raise ValueError("malformed message data")
    for message in messages:
        if not isinstance(message, dict) or not _valid_message_id(message.get("id")):
            raise ValueError("malformed message id")
        created_time = message.get("created_time")
        if not isinstance(created_time, str) or _parse_ig_time(created_time) is None:
            raise ValueError("malformed message time")
        sender = message.get("from")
        if not isinstance(sender, dict):
            raise ValueError("malformed message sender")
        sender_id = sender.get("id")
        if (
            not isinstance(sender_id, str)
            or not _CONV_ID_RE.fullmatch(sender_id.strip())
        ):
            raise ValueError("malformed sender id")
        text = message.get("message")
        if text is not None and not isinstance(text, str):
            raise ValueError("malformed message text")
        attachments = message.get("attachments")
        if attachments is not None and (
            not isinstance(attachments, list)
            or any(
                not isinstance(item, dict)
                or (
                    item.get("payload") is not None
                    and not isinstance(item.get("payload"), dict)
                )
                for item in attachments
            )
        ):
            raise ValueError("malformed attachments")
    paging = messages_block.get("paging")
    if paging is None:
        paging = {}
    if not isinstance(paging, dict):
        raise ValueError("malformed message paging")
    next_url = paging.get("next") or ""
    if next_url and (
        not isinstance(next_url, str) or not _valid_conversation_page_url(next_url)
    ):
        raise ValueError("untrusted message paging URL")
    return messages, next_url


def _fetch_polled_conversation(
    conversation_id: str,
    page_token: str,
    *,
    cursor_at: datetime | None,
    cursor_id: str,
    deadline: float,
    request_limit: int,
) -> dict:
    page_url = _graph_url(
        f"/{conversation_id}",
        {"fields": "messages.limit(50){message,from,created_time,id,attachments}"},
    )
    all_messages: list[dict] = []
    visited_pages: set[str] = set()
    requests_used = 0
    for _page in range(POLL_MESSAGE_MAX_PAGES):
        if requests_used >= request_limit or time.monotonic() >= deadline:
            return {
                "messages": [],
                "requests": requests_used,
                "complete": False,
                "budget_exhausted": True,
                "reason": "poll_budget",
            }
        if page_url in visited_pages:
            return {
                "messages": [],
                "requests": requests_used,
                "complete": False,
                "budget_exhausted": False,
                "reason": "page_cycle",
            }
        visited_pages.add(page_url)
        remaining_seconds = max(1, int(deadline - time.monotonic()))
        timeout = min(POLL_MESSAGE_TIMEOUT, remaining_seconds)
        code, body = _graph_http(page_url, token=page_token, timeout=timeout)
        requests_used += 1
        if code != 200:
            return {
                "messages": [],
                "requests": requests_used,
                "complete": False,
                "budget_exhausted": False,
                "reason": f"http_{code}",
            }
        try:
            messages, next_url = _validate_polled_page(json.loads(body))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return {
                "messages": [],
                "requests": requests_used,
                "complete": False,
                "budget_exhausted": False,
                "reason": f"malformed:{exc}",
            }
        all_messages.extend(messages)
        if cursor_at and any(
            _polled_message_key(message) <= (cursor_at, cursor_id)
            for message in messages
        ):
            return {
                "messages": all_messages,
                "requests": requests_used,
                "complete": True,
                "budget_exhausted": False,
                "reason": "cursor_reached",
            }
        if not next_url:
            return {
                "messages": all_messages,
                "requests": requests_used,
                "complete": True,
                "budget_exhausted": False,
                "reason": "complete",
            }
        if next_url in visited_pages:
            return {
                "messages": [],
                "requests": requests_used,
                "complete": False,
                "budget_exhausted": False,
                "reason": "page_cycle",
            }
        page_url = next_url
    return {
        "messages": [],
        "requests": requests_used,
        "complete": False,
        "budget_exhausted": False,
        "reason": "page_cap",
    }


def poll_ingest(s: InstagramBotSettings) -> dict:
    """Читає інбокс IG і кладе нові вхідні в чергу. Лише коли receive_via_poll."""
    if not s.receive_via_poll:
        return {"ok": True, "enqueued": 0, "skipped": True}
    page_token = get_page_token(s)
    if not page_token:
        return {"ok": False, "error": "no_page_token"}
    conv_ids = get_conv_ids_cached(s)
    if conv_ids is None:
        return {"ok": True, "enqueued": 0, "conversations": 0, "refresh_pending": True}
    if not conv_ids:
        return {"ok": True, "enqueued": 0, "conversations": 0}
    if s.last_error:
        s.last_error = ""
        s.save(update_fields=["last_error"])
    reply_after = s.reply_after or s.last_started_at
    enq = 0
    ordered_conv_ids, start_offset = _poll_conversation_order(s, conv_ids)
    deadline = time.monotonic() + POLL_MAX_SECONDS
    requests_used = 0
    conversations_checked = 0
    budget_exhausted = False
    for cid in ordered_conv_ids:
        if requests_used >= POLL_MAX_REQUESTS or time.monotonic() >= deadline:
            budget_exhausted = True
            break
        conversations_checked += 1
        cursor, _created = IgPollCursor.objects.get_or_create(conversation_id=cid)
        cursor_at = cursor.last_message_at
        cursor_id = cursor.last_message_id or ""
        fetched = _fetch_polled_conversation(
            cid,
            page_token,
            cursor_at=cursor_at,
            cursor_id=cursor_id,
            deadline=deadline,
            request_limit=POLL_MAX_REQUESTS - requests_used,
        )
        requests_used += fetched["requests"]
        if not fetched["complete"]:
            log(
                "warning",
                "poll_messages",
                f"conversation skipped: {fetched['reason']}",
            )
            if fetched["budget_exhausted"]:
                budget_exhausted = True
                break
            continue

        unique: dict[str, dict] = {}
        for message in fetched["messages"]:
            unique[message["id"]] = message

        ordered = sorted(unique.values(), key=_polled_message_key)
        for message in ordered:
            mid = message["id"]
            created = _parse_ig_time(message.get("created_time", ""))
            if cursor_at and _polled_message_key(message) <= (cursor_at, cursor_id):
                continue
            sender = (message.get("from") or {}).get("id", "")
            if not sender or sender == s.ig_user_id:
                continue
            if reply_after and created and created <= reply_after:
                continue
            if enqueue_inbound(
                s,
                sender_id=sender,
                text=message.get("message", ""),
                mid=mid,
                source="poll",
                attachments=_extract_media_urls(message),
                received_at=created,
            ):
                enq += 1

        if ordered:
            newest = max(ordered, key=_polled_message_key)
            newest_at, newest_id = _polled_message_key(newest)
            cursor.last_message_at = newest_at if newest_at != datetime.min.replace(tzinfo=dt_timezone.utc) else cursor.last_message_at
            cursor.last_message_id = newest_id
            cursor.save(update_fields=["last_message_at", "last_message_id", "updated_at"])
    if conversations_checked < len(conv_ids):
        budget_exhausted = True
    next_offset = (start_offset + conversations_checked) % len(conv_ids)
    if budget_exhausted:
        cache.set(_poll_offset_cache_key(s), next_offset, CONV_CACHE_TTL)
    else:
        cache.delete(_poll_offset_cache_key(s))
    return {
        "ok": True,
        "enqueued": enq,
        "conversations": len(conv_ids),
        "conversations_checked": conversations_checked,
        "requests_used": requests_used,
        "budget_exhausted": budget_exhausted,
    }


# Зворотна сумісність для --once: інгест + обробка.
def poll_once(s: InstagramBotSettings | None = None) -> dict:
    s = s or InstagramBotSettings.load()
    s.heartbeat_at = timezone.now()
    s.last_poll_at = timezone.now()
    s.save(update_fields=["heartbeat_at", "last_poll_at"])
    res = poll_ingest(s)
    res["handled"] = process_pending(s)
    return res


# ---------------------------------------------------------------------------
# Start / Stop / Status
# ---------------------------------------------------------------------------
def start_bot() -> InstagramBotSettings:
    from management.services.ig_reply_boundary import pause_reply_boundary

    with pause_reply_boundary():
        with transaction.atomic():
            s = InstagramBotSettings.objects.select_for_update().get(
                pk=InstagramBotSettings.load().pk
            )
            was = s.is_enabled
            s.is_enabled = True
            s.reply_permission_epoch = int(s.reply_permission_epoch or 0) + 1
            s.last_started_at = timezone.now()
            s.reply_after = timezone.now()
            s.last_error = ""
            s.save(update_fields=[
                "is_enabled", "reply_permission_epoch", "last_started_at",
                "reply_after", "last_error",
            ])
    if not was:
        log("success", "start", "Бот запущено, очікую повідомлення.")
    return s


def stop_bot() -> InstagramBotSettings:
    from management.models import IgFollowUpTask
    from management.services.ig_reply_boundary import pause_reply_boundary

    now = timezone.now()
    with pause_reply_boundary():
        with transaction.atomic():
            s = InstagramBotSettings.objects.select_for_update().get(
                pk=InstagramBotSettings.load().pk
            )
            was = s.is_enabled
            s.is_enabled = False
            s.reply_permission_epoch = int(s.reply_permission_epoch or 0) + 1
            s.last_stopped_at = now
            s.save(update_fields=["is_enabled", "reply_permission_epoch", "last_stopped_at"])
            InstagramBotMessage.objects.filter(
                role=InstagramBotMessage.Role.USER,
                status__in=[
                    InstagramBotMessage.Status.PENDING,
                    InstagramBotMessage.Status.PROCESSING,
                ],
            ).exclude(send_state="sending").update(
                status=InstagramBotMessage.Status.DONE,
                processed_at=now,
                processing_started_at=None,
            )
            IgFollowUpTask.objects.filter(status=IgFollowUpTask.Status.PENDING).update(
                status=IgFollowUpTask.Status.CANCELLED,
                skip_reason="global_reply_stopped",
                updated_at=now,
            )
            IgClient.objects.filter(next_followup_at__isnull=False).update(
                next_followup_at=None
            )
    if was:
        log("warning", "stop", "Бот зупинено.")
    return s


def meta_capability_status(s: InstagramBotSettings) -> dict[str, object]:
    """Expose independent Meta facts without implying public delivery access."""
    return {
        "local_allowlist": "restricted" if allowed_sender_ids(s) else "all_allowed",
        "token_configured": bool(resolve_direct_token(s)),
        "token_permission": "unknown",
        "account_access": "unknown",
        "recipient_delivery": "per_recipient",
    }


def status_snapshot() -> dict:
    from management.services.ig_maintenance import maintenance_status
    from management.services.ig_reply_boundary import reply_barrier_telemetry

    s = InstagramBotSettings.load()
    maintenance = maintenance_status()
    now = timezone.now()
    hb = s.heartbeat_at
    db_heartbeat_age = (now - hb).total_seconds() if hb else None
    db_heartbeat_fresh = bool(db_heartbeat_age is not None and db_heartbeat_age < 90)
    dhb = cache.get("ig_bot_daemon_hb")
    try:
        if isinstance(dhb, dict):
            dhb = dhb.get("at")
        daemon_heartbeat_age = time.time() - float(dhb) if dhb else None
    except (TypeError, ValueError):
        daemon_heartbeat_age = None
    daemon_online = bool(daemon_heartbeat_age is not None and daemon_heartbeat_age < 45)
    if maintenance["active"]:
        state = "maintenance"
    elif not s.is_enabled:
        state = "disabled"
    elif daemon_online:
        state = "running"
    elif db_heartbeat_fresh:
        state = "worker_error"
    else:
        state = "enabled_but_worker_missing"
    try:
        notification_pending = IgBotNotification.objects.filter(
            status__in=[IgBotNotification.Status.PENDING, IgBotNotification.Status.SENDING]
        ).count()
        notification_failed = IgBotNotification.objects.filter(
            status=IgBotNotification.Status.FAILED
        ).count()
        notification_unknown = IgBotNotification.objects.filter(
            status=IgBotNotification.Status.UNKNOWN
        ).count()
        notification_dead_letter = IgBotNotification.objects.filter(
            status=IgBotNotification.Status.DEAD_LETTER
        ).count()
    except Exception:
        notification_pending = None
        notification_failed = None
        notification_unknown = None
        notification_dead_letter = None
    try:
        analysis_pending = IgConversationAnalysisJob.objects.filter(
            status__in=[
                IgConversationAnalysisJob.Status.PENDING,
                IgConversationAnalysisJob.Status.PROCESSING,
            ]
        ).count()
        analysis_failed = IgConversationAnalysisJob.objects.filter(
            status=IgConversationAnalysisJob.Status.FAILED
        ).count()
    except Exception:
        analysis_pending = None
        analysis_failed = None
    try:
        from management.services.gemini_keys import (
            ALL_KEYS,
            key_project_groups,
            normalize_chat_model,
        )

        effective_model = normalize_chat_model(s.gemini_model)
        project_groups = key_project_groups()
        project_mapping_count = len(project_groups)
        project_mapping_complete = all(alias in project_groups for alias in ALL_KEYS)
    except Exception:
        effective_model = s.gemini_model
        project_mapping_count = 0
        project_mapping_complete = False
    return {
        "is_enabled": s.is_enabled,
        # Backwards-compatible alias: only the daemon heartbeat proves a
        # worker is alive. A fresh DB timestamp alone is not liveness proof.
        "alive": daemon_online,
        "daemon_online": daemon_online,
        "running": s.is_enabled and daemon_online and not maintenance["active"],
        "state": state,
        "recovery_expected": bool(s.is_enabled and not daemon_online and not maintenance["active"]),
        "maintenance": maintenance,
        "db_heartbeat_fresh": db_heartbeat_fresh,
        "db_heartbeat_age_seconds": round(db_heartbeat_age, 1) if db_heartbeat_age is not None else None,
        "daemon_heartbeat_age_seconds": round(daemon_heartbeat_age, 1) if daemon_heartbeat_age is not None else None,
        "heartbeat_at": hb.isoformat() if hb else "",
        "last_inbound_at": s.last_inbound_at.isoformat() if s.last_inbound_at else "",
        "last_reply_at": s.last_reply_at.isoformat() if s.last_reply_at else "",
        "replies_count": s.replies_count,
        "pending": pending_count(),
        "notification_pending": notification_pending,
        "notification_failed": notification_failed,
        "notification_unknown": notification_unknown,
        "notification_dead_letter": notification_dead_letter,
        "analysis_pending": analysis_pending,
        "analysis_failed": analysis_failed,
        "analysis_reconcile_cursor": s.analysis_reconcile_cursor,
        "analysis_reconcile_after": s.analysis_reconcile_after.isoformat(),
        "analysis_backfill_enabled": s.analysis_backfill_enabled,
        "analysis_backfill_allowed": bool(
            s.analysis_backfill_enabled and project_mapping_complete
        ),
        "unique_senders": unique_senders_count(),
        "allow_all": not bool(allowed_sender_ids(s)),
        "last_error": s.last_error,
        "direct_source": s.direct_source,
        "gemini_source": s.gemini_source,
        "ai_enabled": s.ai_enabled,
        "gemini_model": s.gemini_model,
        "gemini_effective_model": effective_model,
        "gemini_project_mapping_count": project_mapping_count,
        "gemini_project_mapping_complete": project_mapping_complete,
        "last_gemini_model": s.last_gemini_model,
        "last_gemini_key": s.last_gemini_key,
        "last_gemini_at": s.last_gemini_at.isoformat() if s.last_gemini_at else "",
        "last_gemini_reasoning_task": s.last_gemini_reasoning_task,
        "last_gemini_reasoning_level": s.last_gemini_reasoning_level,
        "last_gemini_policy_version": s.last_gemini_policy_version,
        "last_gemini_thoughts_tokens": s.last_gemini_thoughts_tokens,
        "last_gemini_candidates_tokens": s.last_gemini_candidates_tokens,
        "receive_via_poll": s.receive_via_poll,
        "app_secret_set": bool(app_secret()),
        "webhook_signature": webhook_signature_status(),
        "meta_capability": meta_capability_status(s),
        "meta_rate_limits": meta_rate_limit_status(),
        "trigger_text": s.trigger_text,
        "reply_text": s.reply_text,
        "poll_interval_seconds": s.poll_interval_seconds,
        "reply_barrier": reply_barrier_telemetry(),
    }
