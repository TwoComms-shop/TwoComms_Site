"""
Пост-оплатний потік IG-бота: збір даних НП, створення замовлення, формування
посилання на оплату за тегами моделі.

Потік (повний автопілот, Q1; замовлення після оплати, Q2; дані НП текстом, Q3):
- [PAYLINK:full|prepay] (+опц. [PRODUCT:id]) → create_deal_and_link → надсилаємо лінк.
- оплата підтверджена (вебхук/поллінг) → on_deal_paid: якщо є дані НП — створюємо
  замовлення; якщо ні — сповіщаємо й бот збирає дані в діалозі.
- [ORDER] → collect_np_and_fulfill: витягуємо дані НП з діалогу й створюємо заказ.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q

from management.services.bot_payments import create_payment_link
from management.services.bot_payment_truth import (
    verified_payment_deals,
    verified_payment_q,
)
from management.services.call_ai_analysis import gemini_generate_text
from management.services.instagram_bot import notify_manager, send_text
from orders.services.order_builder import create_order_from_deal

logger = logging.getLogger(__name__)

NP_EXTRACT_INSTRUCTION = (
    "З наведеного діалогу витягни дані доставки Новою Поштою у форматі JSON без "
    "markdown: {\"full_name\":\"ПІБ\",\"phone\":\"телефон\",\"city\":\"місто\","
    "\"office\":\"відділення/поштомат\"}. Якщо чогось немає — порожній рядок."
)


def deal_has_np_data(deal) -> bool:
    return all([
        (deal.np_full_name or "").strip(),
        (deal.np_phone or "").strip(),
        (deal.np_city or "").strip(),
        (deal.np_office or "").strip(),
    ])


def extract_np_data(client) -> dict:
    """Витягує дані НП з останніх повідомлень клієнта (management-модель)."""
    from management.models import InstagramBotMessage

    rows = list(
        InstagramBotMessage.objects.filter(client=client).order_by("-id")[:14]
    )
    rows.reverse()
    transcript = "\n".join(
        f"{'Клієнт' if r.role == 'user' else 'Бот'}: {r.text}"
        for r in rows
        if (r.text or "").strip()
    )
    if not transcript.strip():
        return {}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": NP_EXTRACT_INSTRUCTION + "\n\nДІАЛОГ:\n" + transcript}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        },
    }
    try:
        out = gemini_generate_text(
            payload, role="management", reasoning_task="order_decision"
        )
    except Exception:
        return {}
    from management.services.bot_vision import _parse_fingerprint

    return _parse_fingerprint(out.get("parsed") or "")


def fulfill_if_ready(deal, created_by=None) -> bool:
    """Створює замовлення, якщо угода оплачена, ще без замовлення і дані НП повні."""
    if deal.order_id:
        return False
    if not verified_payment_deals(deal.__class__.objects.filter(pk=deal.pk)).exists():
        return False
    if not deal_has_np_data(deal):
        return False
    order = create_order_from_deal(deal, created_by=created_by)
    try:
        notify_manager(
            f"✅ IG: оплачено і створено замовлення {order.order_number} "
            f"({deal.amount} грн) — {deal.np_full_name}, {deal.np_city}, {deal.np_office}."
        )
    except Exception:
        pass
    return True


def collect_np_and_fulfill(client, created_by=None) -> bool:
    """Знаходить оплачену угоду без замовлення, витягує дані НП з діалогу (якщо
    бракує), зберігає й створює замовлення. True, якщо замовлення створено."""
    from management.models import IgDeal

    deal = (
        verified_payment_deals(
            IgDeal.objects.filter(client=client, order__isnull=True)
        )
        .order_by("-id")
        .first()
    )
    if not deal:
        return False
    if not deal_has_np_data(deal):
        data = extract_np_data(client) or {}
        deal.np_full_name = (data.get("full_name") or deal.np_full_name or "")[:255]
        deal.np_phone = (data.get("phone") or deal.np_phone or "")[:50]
        deal.np_city = (data.get("city") or deal.np_city or "")[:160]
        deal.np_office = (data.get("office") or deal.np_office or "")[:255]
        deal.save(update_fields=["np_full_name", "np_phone", "np_city", "np_office", "updated_at"])
    return fulfill_if_ready(deal, created_by=created_by)


def on_deal_paid(deal) -> None:
    """Хук «оплачено»: якщо дані НП є — створюємо замовлення; інакше сповіщаємо,
    і бот збере дані в діалозі."""
    if deal_has_np_data(deal):
        fulfill_if_ready(deal)
    else:
        try:
            notify_manager(
                f"💸 IG: оплата отримана (угода #{deal.id}, {deal.amount} грн), "
                f"але бракує даних доставки — бот збирає ПІБ/телефон/місто/відділення."
            )
        except Exception:
            pass


def pin_product(client, product_id) -> bool:
    """Закріплює товар за клієнтом (current_product), якщо він опублікований.

    Викликається, коли модель дала [PRODUCT:id] або матчинг фото впевнено
    визначив товар. Робить наступне формування лінку детермінованим.
    """
    if not client or not product_id:
        return False
    from storefront.models import Product, ProductStatus

    try:
        p = Product.objects.filter(id=int(product_id), status=ProductStatus.PUBLISHED).first()
    except (TypeError, ValueError):
        p = None
    if not p:
        return False
    if client.current_product_id == p.id:
        if not client.current_product_confidence:
            client.current_product_confidence = 1
            client.save(update_fields=["current_product_confidence", "updated_at"])
        return True
    client.current_product = p
    client.current_product_confidence = 1
    client.save(update_fields=["current_product", "current_product_confidence", "updated_at"])
    return True


def resolve_product_for_payment(client, product_id=None):
    """Визначає товар для оплати НАДІЙНО (не підставляє випадковий товар).

    Пріоритет:
      1) явний product_id (з тегу [PRODUCT:id]) → товар;
      2) інакше — management-модель за діалогом + каталогом обирає id товару з
         урахуванням типу (футболка/худі/лонгслів) і впевненості. Це переживає
         різницю «з/с», скорочення й розмовні назви, чого не вміє підрядковий матч.
    Якщо впевненості немає — повертає None (краще покликати менеджера, ніж
    виставити рахунок не за той товар)."""
    from storefront.models import Product, ProductStatus

    if product_id:
        try:
            p = Product.objects.filter(id=int(product_id), status=ProductStatus.PUBLISHED).first()
        except (TypeError, ValueError):
            p = None
        if p:
            return p

    # 2) закріплений товар діалогу (швидко й детерміновано, без виклику моделі).
    cur = getattr(client, "current_product", None)
    if cur is not None and getattr(cur, "status", None) == ProductStatus.PUBLISHED:
        return cur

    from management.models import InstagramBotMessage

    rows = list(InstagramBotMessage.objects.filter(client=client).order_by("-id")[:16])
    rows.reverse()
    transcript = "\n".join(
        f"{'Клієнт' if r.role == 'user' else 'Бот'}: {r.text}"
        for r in rows
        if (r.text or "").strip()
    )
    if not transcript.strip():
        return None

    cat_lines = []
    for p in Product.objects.filter(status=ProductStatus.PUBLISHED).only("id", "title", "price")[:300]:
        try:
            price = int(getattr(p, "final_price", None) or p.price)
        except Exception:
            price = p.price
        cat_lines.append(f"{p.id}|{p.title}|{price}")
    if not cat_lines:
        return None

    instruction = (
        "За діалогом визнач, ЯКИЙ САМЕ товар з каталогу клієнт хоче оплатити. "
        "Враховуй ТИП (футболка / худі / лонгслів) і назву принта. Поверни лише JSON "
        'без markdown: {"product_id": <id з каталогу або null>, "confidence": <0..1>}. '
        "Якщо не зрозуміло однозначно — product_id:null, confidence:0. "
        "Каталог (формат id|назва|ціна_грн):\n" + "\n".join(cat_lines)
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": instruction + "\n\nДІАЛОГ:\n" + transcript}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        },
    }
    try:
        out = gemini_generate_text(
            payload, role="management", reasoning_task="product_decision"
        )
    except Exception:
        return None
    from management.services.bot_vision import _parse_fingerprint

    data = _parse_fingerprint(out.get("parsed") or "")
    pid = data.get("product_id")
    try:
        pid = int(pid) if pid not in (None, "", "null") else None
    except (TypeError, ValueError):
        pid = None
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if not pid or conf < 0.6:
        return None
    return Product.objects.filter(id=pid, status=ProductStatus.PUBLISHED).first()


def create_deal_and_link(client, pay_type: str = "full", product_id=None, qty: int = 1, size: str = "") -> dict:
    """Формує/переюзає угоду клієнта і повертає посилання на оплату Monobank.

    Товар визначається серверно (resolve_product_for_payment) — навіть якщо модель
    не передала [PRODUCT:id]. Гроші коректні: посилання на правильний товар/суму,
    стара чернетка/invoice скидаються при зміні товару чи типу оплати.
    """
    from management.models import IgDeal, IgDealItem

    pt = IgDeal.PayType.PREPAY_200 if pay_type == "prepay" else IgDeal.PayType.ONLINE_FULL
    product = resolve_product_for_payment(client, product_id)
    open_deals = list(
        IgDeal.objects.filter(client=client, order__isnull=True)
        .exclude(status=IgDeal.Status.PAID)
        .order_by("-id")
    )

    deal = None
    if product is not None:
        # 1) точний реюз: та сама угода (товар + тип оплати) вже має лінк
        for d in open_deals:
            if (
                d.pay_type == pt
                and d.items.count() == 1
                and d.items.filter(product_id=product.id).exists()
                and d.invoice_id
                and d.invoice_url
            ):
                return create_payment_link(d)
        # 2) свіжа угода саме під цей товар
        deal = IgDeal.objects.create(client=client, pay_type=pt)
        try:
            price = Decimal(str(int(getattr(product, "final_price", None) or product.price)))
        except Exception:
            price = Decimal(str(product.price or 0))
        IgDealItem.objects.create(
            deal=deal, product=product, title=product.title, size=size or "",
            qty=max(1, int(qty or 1)), unit_price=price,
        )
    else:
        # Товар не визначено → остання відкрита угода з позиціями.
        for d in open_deals:
            if d.items.exists():
                deal = d
                break
        if deal is None:
            return {"ok": False, "error": "no_product"}
        if deal.pay_type == pt and deal.invoice_id and deal.invoice_url:
            return create_payment_link(deal)
        deal.pay_type = pt
        deal.invoice_id = ""
        deal.invoice_url = ""
        deal.save(update_fields=["pay_type", "invoice_id", "invoice_url", "updated_at"])

    deal.recalc_total()
    res = create_payment_link(deal)
    if res.get("ok"):
        try:
            from management.models import IgClient

            deal.client.set_stage(IgClient.Stage.PAYMENT_PENDING, reason="paylink")
        except Exception:
            pass
    return res


def fulfill_ready_paid_deals(limit: int = 50) -> int:
    """Safety-net: створює замовлення для ОПЛАЧЕНИХ угод без замовлення, у яких
    уже є повні дані НП (якщо модель не виставила тег [ORDER]). Для крону."""
    from management.models import IgDeal
    from storefront.models import UserAction

    purchase_order_ids = UserAction.objects.filter(
        action_type='purchase',
        order_id__isnull=False,
    ).values('order_id')
    missing_order = verified_payment_q() & Q(order__isnull=True)
    missing_purchase = (
        Q(
            status=IgDeal.Status.ORDER_CREATED,
            payment_status__in=("paid", "prepaid"),
            paid_at__isnull=False,
            order__isnull=False,
            order__payment_status__in=('paid', 'prepaid', 'partial'),
        )
        & ~Q(order_id__in=purchase_order_ids)
    )
    qs = (
        IgDeal.objects.filter(missing_order | missing_purchase)
        .select_related('order')
        .order_by('id')[:limit]
    )
    created = 0
    for deal in qs:
        try:
            if deal.order_id:
                # Four-minute cron retry for a post-commit analytics failure.
                create_order_from_deal(deal)
            elif deal_has_np_data(deal) and fulfill_if_ready(deal):
                created += 1
        except Exception:
            # One broken deal must not abort healing/creation for the batch.
            logger.exception('Failed to fulfil or heal paid IG deal %s', deal.pk)
    return created


NP_TRACK_URL = "https://novaposhta.ua/tracking/?cargo_number="
SHIPMENT_RESPONSE_WINDOW = timedelta(hours=23)
SHIPMENT_REVIEW_REASONS = {"shipment_human_review", "shipment_delivery_review"}


def _shipment_message(ttn: str) -> str:
    return (
        "📦 Гарна новина — ваше замовлення вже відправлено Новою Поштою! 🚚\n"
        f"ТТН: {ttn}\n"
        f"Відстежити: {NP_TRACK_URL}{ttn}\n"
        "Дякуємо за покупку 💛 Будуть питання — пишіть, я на зв'язку!"
    )


def _queue_shipment_manager_review(deal, text: str, *, reason: str, hint: str = ""):
    from django.utils import timezone

    from management.models import IgFollowUpTask

    task, created = IgFollowUpTask.objects.get_or_create(
        client=deal.client,
        deal=deal,
        kind=IgFollowUpTask.Kind.MANAGER_TASK,
        reason=reason,
        defaults={
            "due_at": timezone.now(),
            # Manager tasks must never enter the automatic send worker.
            "status": IgFollowUpTask.Status.SKIPPED,
            "skip_reason": "human_agent_required",
            "message_text": text,
            "last_error": (hint or "")[:500],
        },
    )
    if not created:
        changed = []
        if task.message_text != text:
            task.message_text = text
            changed.append("message_text")
        bounded_hint = (hint or "")[:500]
        if task.last_error != bounded_hint:
            task.last_error = bounded_hint
            changed.append("last_error")
        if changed:
            changed.append("updated_at")
            task.save(update_fields=changed)
    client_label = deal.client.username or deal.client.display_name or deal.client.igsid
    notify_manager(
        f"📦 IG: потрібна ручна відповідь про відправку для {client_label}. "
        f"Угода #{deal.pk}; готовий текст збережено у завданні менеджеру.",
        dedupe_key=f"{reason}:{deal.pk}",
        event_type="shipment_human_review",
        client=deal.client,
    )
    return task


def notify_shipped_deals(limit: int = 50) -> int:
    """Сповіщає IG-клієнта в Direct, що замовлення відправлено (з ТТН).

    Лише для IG-угод, чиє замовлення в статусі 'ship' і має tracking_number.
    Усередині response window надсилає звичайний RESPONSE. Поза вікном або
    після непідтвердженої доставки створює завдання менеджеру; автоматичний
    HUMAN_AGENT tag не використовується.
    """
    from django.utils import timezone

    from management.models import IgDeal, InstagramBotSettings
    from management.services.bot_payment_truth import verified_payment_q

    s = InstagramBotSettings.load()
    qs = (
        IgDeal.objects.filter(
            order__isnull=False, order__status="ship", shipped_notified_at__isnull=True
        )
        .filter(verified_payment_q())
        .exclude(order__tracking_number__isnull=True)
        .exclude(order__tracking_number="")
        .select_related("order", "client")[:limit]
    )
    sent = 0
    for deal in qs:
        ttn = (deal.order.tracking_number or "").strip()
        if not ttn or not deal.client_id:
            continue
        text = _shipment_message(ttn)
        existing_review = deal.followup_tasks.filter(
            reason__in=SHIPMENT_REVIEW_REASONS
        ).first()
        if existing_review:
            _queue_shipment_manager_review(
                deal,
                text,
                reason=existing_review.reason,
                hint=existing_review.last_error,
            )
            continue
        response_deadline = (
            deal.client.last_message_at + SHIPMENT_RESPONSE_WINDOW
            if deal.client.last_message_at
            else None
        )
        if not response_deadline or timezone.now() > response_deadline:
            _queue_shipment_manager_review(
                deal,
                text,
                reason="shipment_human_review",
                hint="standard_response_window_closed",
            )
            continue
        try:
            ok, kind, hint = send_text(s, deal.client.igsid, text)
        except Exception as exc:
            ok, kind, hint = False, "unknown", repr(exc)
        if ok:
            deal.shipped_notified_at = timezone.now()
            deal.save(update_fields=["shipped_notified_at", "updated_at"])
            sent += 1
        else:
            _queue_shipment_manager_review(
                deal,
                text,
                reason="shipment_delivery_review",
                hint=f"{kind}:{hint}",
            )
    return sent
