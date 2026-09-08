"""Політика Telegram-алертів: не спам, з посиланнями, без загублених подій.

Скарга заказника: «сразу спам из 10 штук». Виміряно по коду:
`drain_manager_notifications(limit=10)` викликається в циклі демона кожні
1.5 секунди і всередині не має ні задержки, ні лічильника — тобто до 20
повідомлень за прохід, поки черга не спорожніє.

Три різні дефекти, які складались у цю картину.

**Потік без обмежень.** Обмеження існували лише в трьох точках із 31, і всі
пер-подієві. Глобального ліміту на **потік** не було, тому відновлення cron
платежів (`poll_pending_deals(limit=50)`) або lifecycle-воркер
(`limit=50`) вистрілювали пачкою.

**Дедуп, який одночасно спамить і губить.** 12 точок із 31 не передавали
`dedupe_key` і отримували `generic:sha256(text)`. Рядки `IgBotNotification`
не видаляються нічим, TTL немає. Наслідок двосторонній: різні клієнти дають
різний текст (пачка), а повтор того самого тексту не дійде **ніколи** — навіть
через місяць. Ключ мусить містити сущність і вікно часу, а не лише текст.

**Алерт без посилання.** Посилання в адмінку було у 2 повідомленнях із 31.
Найчастіше — ескалація «клієнту потрібен менеджер» — містило лише IGSID, тому
менеджер мусив шукати клієнта руками, хоча `client_id` був відомий рядком вище.

Модуль свідомо складається з чистих функцій політики: їх можна перевірити
тестами без черги, БД і Telegram. Інтеграція — на боці `notify_manager`.
"""
from __future__ import annotations

import hashlib
import logging
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta

from django.core.cache import cache

logger = logging.getLogger(__name__)

# Скільки алертів за хвилину допустимо. Шість — це «раз на десять секунд»:
# менеджер устигає читати, а справжній інцидент усе одно доїде за пару хвилин.
DEFAULT_MAX_PER_MINUTE = 6
FLOW_CACHE_KEY = "ig_alert_flow"
# Текст Telegram обрізаємо тим самим лімітом, що й раніше.
MAX_ALERT_CHARS = 3500
_MACHINE_CODE_RE = re.compile(r"[a-z][a-z0-9_:-]{0,63}\Z")
_INSTRUCTION_TEXT = {
    "revision_echo_unresolved": "Відкрийте розмову й перевірте, чи відповідала команда; автоматичну відповідь призупинено до звірки.",
    "takeover_released": "Якщо діалог ще веде менеджер, поставте бота на паузу в CRM.",
    "spam_blocked": "Автоматичні відповіді для клієнта зупинено.",
    "paylink_item_gate": "Перевірте товар, кількість, розмір і крій у CRM.",
    "paylink_no_candidate": "Перевірте purchase candidate і товар у CRM.",
    "paylink_price_gate": "Перевірте погоджену суму та evidence у CRM.",
    "paylink_prepay_gate": "Перевірте суму передоплати та evidence у CRM.",
    "paylink_inventory_unavailable": "Перевірте точну наявність або заміну в CRM.",
    "paylink_failed": "Перевірте причину в CRM та підключіться вручну.",
    "partial_delivery": "Автоматичний повтор вимкнено; звірте CRM з Meta Inbox.",
    "payment_link_delivery_review": "Invoice збережено у завданні менеджеру; перевірте діалог у CRM та надішліть його вручну.",
    "size_gap": "Перевірте товар і точну наявність у CRM.",
    "escalation": "Відкрийте CRM і продовжте діалог вручну.",
    "sender_rate_limited": "Перевірте діалог у CRM перед ручною дією.",
    "generation_failed": "Відкрийте CRM і дайте ручну відповідь.",
    "delivery_unknown": "Автоматичний повтор вимкнено; звірте CRM з Meta Inbox.",
    "send_gave_up": "Відкрийте CRM і завершіть відповідь вручну.",
    "permission_takeover": "Стан діалогу доступний у CRM.",
    "fallback_ready": "Безпечну відповідь підготовлено; перевірте діалог у CRM.",
    "order_created": "Замовлення створено; дані доставки доступні в CRM.",
    "delivery_reference": "Оберіть місто та відділення з довідника в CRM.",
    "context_in_crm": "Бот збирає дані; повний контекст доступний у CRM.",
    "manager_task_ready": "Готовий текст збережено у завданні менеджеру.",
    "data_deletion_request": "Заявку перевірте в захищеному CRM-процесі; код підтвердження не надсилається в Telegram.",
    "reviewer_action": "Дію зовнішнього reviewer зафіксовано; перевірте стан бота в CRM.",
    "ambiguous_order_status": "Уточніть точний номер замовлення або ТТН у захищеному CRM-діалозі.",
    "superseded_invoice_payment": "Платіж за заміненим посиланням потребує ручного розбору в CRM.",
    "ig_checkout_invoice_created": "Платіжне посилання створено; повні дані замовлення доступні в CRM.",
    "ig_lifecycle_window_review": "Підготовлену відповідь і дані замовлення перевірте в CRM.",
    "ig_lifecycle_delivery_review": "Перевірте CRM і Meta Inbox перед ручною відповіддю.",
}
_ALERT_TITLE_TEXT = {
    "revision_echo_unresolved": "⚠️ IG: не вдалося визначити автора повідомлення",
    "ai_reply_fallback": "⚠️ IG: Gemini недоступний; потрібна ручна перевірка",
    "takeover": "👤 IG: менеджер підключився; бот поставлено на паузу",
    "takeover_released": "🤖 IG: бот відновив автоматичні відповіді",
    "spam_blocked": "🚫 IG: клієнта заблоковано за spam policy",
    "paylink_item_gate": "⚠️ IG: платіжне посилання заблоковано",
    "paylink_no_candidate": "⚠️ IG: платіжне посилання заблоковано",
    "paylink_price_gate": "⚠️ IG: платіжне посилання заблоковано",
    "paylink_prepay_gate": "⚠️ IG: платіжне посилання заблоковано",
    "paylink_inventory_unavailable": "📦 IG: checkout заблоковано inventory gate",
    "paylink_failed": "⚠️ IG: платіжне посилання не сформовано",
    "payment_review": "⚠️ Instagram: потрібна перевірка заяви про оплату",
    "payment_link_delivery_review": "⚠️ IG: не вдалося доставити платіжне посилання",
    "size_gap": "📏 IG: потрібна перевірка відсутнього розміру",
    "escalation": "🔔 IG Direct — клієнту потрібен менеджер.",
    "sender_rate_limited": "⚠️ IG бот: перевищено ліміт повідомлень",
    "generation_failed": "⚠️ IG: Gemini не сформував відповідь",
    "delivery_unknown": "⚠️ IG: невідомий результат доставки",
    "send_gave_up": "⚠️ IG: відповідь не доставлена",
    "partial_delivery": "⚠️ IG: часткова доставка відповіді",
    "order_created": "✅ IG: оплачено і створено замовлення",
    "delivery_validation_review": "📦 IG: потрібна перевірка доставки Новою Поштою",
    "payment_received_delivery_pending": "💸 IG: оплата отримана; очікуються дані доставки",
    "shipment_human_review": "📦 IG: потрібна ручна відповідь про відправку",
    "data_deletion_request": "🧹 Запит на видалення даних DIRECT_BOT",
    "reviewer_action": "⚠️ Зовнішній Meta-reviewer виконав дію",
    "ambiguous_order_status": "🧭 IG: у клієнта кілька замовлень",
    "superseded_invoice_payment": "⚠️ IG: оплата за заміненим посиланням",
    "ig_checkout_invoice_created": "💳 IG: платіжне посилання створено",
    "ig_lifecycle_window_review": "⚠️ IG: lifecycle-подія потребує відповіді менеджера",
    "ig_lifecycle_delivery_review": "⚠️ IG: не вдалося доставити lifecycle-подію",
    "ig_task_failure": "⚠️ Помилка IG cron-задачі",
    "discount_approval": "🏷️ IG: потрібне рішення щодо знижки",
}
ALERT_EVENT_CODES = frozenset(_ALERT_TITLE_TEXT) | {"generic", "notification_terminal_monitor"}
HUMAN_REVIEW_EVENT_CODES = frozenset({
    "ai_reply_fallback",
    "ai_reply_recovery_exhausted",
    "ambiguous_order_status",
    "data_deletion_request",
    "delivery_unknown",
    "delivery_validation_review",
    "discount_approval",
    "escalation",
    "fulfillment_missing_delivery",
    "generation_failed",
    "ig_lifecycle_delivery_review",
    "ig_lifecycle_permission_review",
    "ig_lifecycle_window_review",
    "inventory_overbooked_review",
    "partial_delivery",
    "payment_link_delivery_review",
    "payment_review",
    "payment_reversed_review",
    "paylink_failed",
    "paylink_inventory_unavailable",
    "paylink_item_gate",
    "paylink_no_candidate",
    "paylink_prepay_gate",
    "paylink_price_gate",
    "send_gave_up",
    "shipment_human_review",
    "size_gap",
    "superseded_invoice_payment",
})


def alert_requires_human_review(event_type: str) -> bool:
    return str(event_type or "").strip() in HUMAN_REVIEW_EVENT_CODES


def management_base_url() -> str:
    from django.conf import settings

    return str(
        getattr(settings, "MANAGEMENT_BASE_URL", "") or "https://management.twocomms.shop"
    ).rstrip("/")


def client_admin_url(client_id) -> str:
    """Посилання на картку клієнта в CRM бота."""
    try:
        client_id = int(client_id)
    except (TypeError, ValueError):
        return ""
    return f"{management_base_url()}/bot/?client={client_id}" if client_id > 0 else ""


def deal_admin_url(deal_id) -> str:
    try:
        deal_id = int(deal_id)
    except (TypeError, ValueError):
        return ""
    return f"{management_base_url()}/bot/?deal={deal_id}" if deal_id > 0 else ""


def payment_review_admin_url(review_id) -> str:
    try:
        review_id = int(review_id)
    except (TypeError, ValueError):
        return ""
    return f"{management_base_url()}/bot/?payment_review={review_id}" if review_id > 0 else ""


def alert_dedupe_key(
    event_type: str,
    *,
    client_id=None,
    entity_id=None,
    window_minutes: int = 0,
    text: str = "",
) -> str:
    """Стабільний ключ дедупу з сущністю і, за потреби, вікном часу.

    Вікно — головна відмінність від колишнього `generic:sha256(text)`. Без нього
    повторна подія не доходила ніколи: рядок зі статусом `sent` глушив усі
    наступні спроби, а прибиральника в системи немає. З вікном алерт про ту саму
    проблему повертається наступного вікна, але не частіше.
    """
    parts = [str(event_type or "generic")[:40]]
    if client_id:
        parts.append(f"c{client_id}")
    if entity_id:
        parts.append(f"e{entity_id}")
    if window_minutes and window_minutes > 0:
        from django.utils import timezone

        bucket = int(timezone.now().timestamp() // (window_minutes * 60))
        parts.append(f"w{bucket}")
    if not client_id and not entity_id:
        digest = hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]
        parts.append(digest)
    return ":".join(parts)[:255]


def format_alert(title: str, *, lines=(), url: str = "", url_label: str = "") -> str:
    """Єдиний формат: заголовок, факти, посилання останнім рядком.

    `parse_mode` у цьому боті не використовується, тому розмітки тут немає
    свідомо — Markdown відрендерився б дослівно. Посилання переживає обрізку:
    якщо текст не влазить, ріжуться факти, а не найкорисніший рядок.
    """
    title = str(title or "").strip()
    body = [str(line).strip() for line in (lines or []) if str(line).strip()]
    tail = ""
    if url:
        tail = f"{url_label.strip()} {url}".strip() if url_label else str(url)

    def assemble(items):
        chunks = [title] if title else []
        chunks.extend(items)
        if tail:
            chunks.append(tail)
        return "\n".join(chunk for chunk in chunks if chunk)

    text = assemble(body)
    while len(text) > MAX_ALERT_CHARS and body:
        body.pop()
        text = assemble(body)
    return text[:MAX_ALERT_CHARS]


def _safe_machine_code(value, *, default: str = "unknown") -> str:
    """Return one bounded operator code without accepting free-form provider text."""
    candidate = str(value or "").strip().lower()
    return candidate if _MACHINE_CODE_RE.fullmatch(candidate) else default


def safe_machine_code(value, *, allowed=None, default: str = "unknown") -> str:
    """Return a bounded code and reject values outside an optional allowlist."""
    code = _safe_machine_code(value, default=default)
    if allowed is not None and code not in allowed:
        return default
    return code


def _positive_local_id(value):
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return None
    return candidate if candidate > 0 else None


def _append_safe_counts(lines, counts):
    for key, value in sorted((counts or {}).items()):
        safe_key = _safe_machine_code(key, default="")
        try:
            safe_value = max(0, int(value))
        except (TypeError, ValueError):
            continue
        if safe_key:
            lines.append(f"{safe_key.upper()}: {safe_value}")


def _safe_alert_title(event_code: str) -> str:
    return _ALERT_TITLE_TEXT.get(event_code, "⚠️ IG: потрібна перевірка оператора")


def format_technical_alert(
    title: str,
    *,
    event_type: str,
    client_id=None,
    message_id=None,
    job_id=None,
    notification_id=None,
    actor_id=None,
    failure_kind: str = "",
    attempts=None,
    counts: dict | None = None,
    instruction_code: str = "",
) -> str:
    """Render a minimum-necessary technical alert from typed local facts.

    Customer text, provider bodies and remote account identifiers are not
    accepted by this API. The restricted CRM remains the evidence boundary.
    """
    event_code = _safe_machine_code(event_type, default="technical_event")
    lines = [f"Подія: {event_code}"]
    local_ids = (
        ("Клієнт ID", client_id),
        ("Повідомлення ID", message_id),
        ("Завдання ID", job_id),
        ("Сповіщення ID", notification_id),
        ("Актор ID", actor_id),
    )
    for label, value in local_ids:
        safe_value = _positive_local_id(value)
        if safe_value is not None:
            lines.append(f"{label}: {safe_value}")
    if failure_kind:
        lines.append(f"Тип збою: {_safe_machine_code(failure_kind)}")
    safe_attempts = _positive_local_id(attempts)
    if safe_attempts is not None:
        lines.append(f"Спроби: {safe_attempts}")
    _append_safe_counts(lines, counts)
    instruction = _INSTRUCTION_TEXT.get(_safe_machine_code(instruction_code, default=""))
    if instruction:
        lines.append(instruction)
    return format_alert(
        _safe_alert_title(event_code),
        lines=lines,
        url=client_admin_url(client_id),
        url_label="CRM:",
    )


def _safe_amount(value):
    """Return a bounded decimal amount, or nothing for untyped input."""
    candidate = str(value or "").strip().replace(",", ".")
    if not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", candidate):
        return ""
    try:
        amount = Decimal(candidate).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return ""
    return f"{amount:.2f}"


def format_operator_alert(
    title: str,
    *,
    event_type: str,
    client_id=None,
    deal_id=None,
    review_id=None,
    task_id=None,
    proposal_id=None,
    attempt_id=None,
    lifecycle_event_id=None,
    amount=None,
    status: str = "",
    counts: dict | None = None,
    instruction_code: str = "",
) -> str:
    """Render an actionable operator alert without customer/provider content."""
    lines = [f"Подія: {_safe_machine_code(event_type, default='operator_event')}"]
    for label, value in (
        ("Клієнт ID", client_id),
        ("Угода ID", deal_id),
        ("Review ID", review_id),
        ("Завдання ID", task_id),
        ("Пропозиція ID", proposal_id),
        ("Спроба оплати ID", attempt_id),
        ("Lifecycle-подія ID", lifecycle_event_id),
    ):
        safe_value = _positive_local_id(value)
        if safe_value is not None:
            lines.append(f"{label}: {safe_value}")
    safe_amount = _safe_amount(amount)
    if safe_amount:
        lines.append(f"Сума: {safe_amount}")
    if status:
        lines.append(f"Статус: {_safe_machine_code(status)}")
    _append_safe_counts(lines, counts)
    instruction = _INSTRUCTION_TEXT.get(_safe_machine_code(instruction_code, default=""))
    if instruction:
        lines.append(instruction)
    url = payment_review_admin_url(review_id) or deal_admin_url(deal_id) or client_admin_url(client_id)
    return format_alert(_safe_alert_title(_safe_machine_code(event_type, default="operator_event")), lines=lines, url=url, url_label="CRM:")


def should_send_now(sent_timestamps, *, now: datetime, max_per_minute: int = DEFAULT_MAX_PER_MINUTE) -> bool:
    """Чи можна відправити ще один алерт прямо зараз.

    Чиста функція: приймає мітки попередніх відправок і рішення приймає лише за
    ними. Так політику можна перевірити тестом без черги й кеша.
    """
    if max_per_minute <= 0:
        return True
    window_start = now - timedelta(minutes=1)
    recent = [item for item in (sent_timestamps or []) if item and item >= window_start]
    return len(recent) < max_per_minute


# Мінімальний інтервал у bounded safe mode. Використовується ЛИШЕ коли
# durable-лічильник недоступний: тоді потік обмежується грубо, але обмежується.
SAFE_MODE_MIN_INTERVAL_SECONDS = 20.0
_safe_mode_last_allowed_at: list = [0.0]


def _safe_mode_gate() -> tuple[bool, int]:
    """Обмежений режим, коли durable-лічильник недоступний.

    Раніше в цьому місці був `return True, 0` — fail-open. Тобто найгірший
    момент (сховище ліміту зламалось) давав саме необмежений потік алертів.
    Розділ 11 джерела вимагає протилежного: обмеженої частоти, а не свободи.

    Обмеження процес-локальне і свідомо грубе: його задача — не дати всплеск, а
    не порахувати точно. Повна відмова тут була б гіршою за всплеск: алерт про
    інцидент не можна втратити повністю.
    """
    import time

    elapsed = time.monotonic() - _safe_mode_last_allowed_at[0]
    if elapsed < SAFE_MODE_MIN_INTERVAL_SECONDS:
        return False, max(1, int(SAFE_MODE_MIN_INTERVAL_SECONDS - elapsed))
    _safe_mode_last_allowed_at[0] = time.monotonic()
    return True, 0


def throttle_gate(
    cache_key: str = FLOW_CACHE_KEY,
    *,
    max_per_minute: int = DEFAULT_MAX_PER_MINUTE,
    now: datetime | None = None,
) -> tuple[bool, int]:
    """Атомарний ліміт потоку алертів на одному рядку БД (ЭА.16).

    **Чому не кеш.** Попередня реалізація робила `cache.get()` і `cache.set()`
    окремими операціями, а production-кеш — `FileBasedCache`: ні `incr` під
    блокуванням, ні транзакції. Два одночасні виклики читали один знімок вікна і
    обидва вважали, що місце є. Демон крутить drain кожні 1.5 секунди, тому
    гонка була не теоретичною.

    **Чому не fail-open.** Збій сховища раніше повертав «дозволено». Тепер він
    переводить у `_safe_mode_gate()` — обмежену частоту. Ліміт, який зникає саме
    тоді, коли щось зламалось, не є лімітом.

    Черга не втрачається: відмова означає «пізніше», рядок лишається в
    `pending`, і наступне вікно його забере. Саме тому відмова інкрементує
    окремий лічильник `denied` — щоб «тихо задушений потік» був видимий.
    """
    from django.utils import timezone

    now = now or timezone.now()
    limit = max(1, int(max_per_minute))
    window = timedelta(minutes=1)
    try:
        from django.db import transaction

        from management.models import IgAlertRateBucket

        with transaction.atomic():
            bucket, _created = (
                IgAlertRateBucket.objects.select_for_update().get_or_create(
                    bucket_key=str(cache_key or FLOW_CACHE_KEY)[:64],
                    defaults={"window_started_at": now, "used": 0},
                )
            )
            if now - bucket.window_started_at >= window:
                bucket.window_started_at = now
                bucket.used = 0
            if bucket.used >= limit:
                bucket.denied += 1
                bucket.save(update_fields=["denied", "updated_at"])
                elapsed = (now - bucket.window_started_at).total_seconds()
                return False, max(1, int(window.total_seconds() - elapsed))
            bucket.used += 1
            bucket.save(update_fields=["window_started_at", "used", "updated_at"])
            return True, 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("ig alert throttle durable counter unavailable: %r", exc)
        return _safe_mode_gate()


def summarize_batch(event_type: str, items, *, limit: int = 5) -> str:
    """N однотипних подій — одне повідомлення зі зведенням.

    Потрібно там, де воркер обходить накопичену чергу: відновлення cron
    платежів і lifecycle віддають до 50 подій за прохід, і кожна ставала
    окремим повідомленням у Telegram.
    """
    values = [str(item).strip() for item in (items or []) if str(item).strip()]
    if not values:
        return ""
    head = values[:max(1, limit)]
    rest = len(values) - len(head)
    lines = [f"• {value}" for value in head]
    if rest > 0:
        lines.append(f"• і ще {rest}")
    return "\n".join(lines)
