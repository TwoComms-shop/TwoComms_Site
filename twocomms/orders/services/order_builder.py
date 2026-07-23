"""
Створення замовлення (orders.Order) з угоди IG-бота (management.IgDeal).

Викликається ПІСЛЯ підтвердженої оплати (рішення Q2). Ідемпотентно: одна угода →
одне замовлення. Дані Нової Пошти зберігаються текстом (Q3=a), ТТН оформлює
менеджер. Спільного сервісу створення замовлень у проєкті не було (логіка
дублювалась у checkout/monobank-в'ю) — це перша переюзабельна точка для бота.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction


def _ensure_purchase_action(order, deal_id):
    from storefront.utm_tracking import ensure_order_purchase_action

    return ensure_order_purchase_action(
        order,
        metadata={
            'source': 'instagram_deal',
            'ig_deal_id': deal_id,
        },
    )


def create_order_from_deal(deal, *, created_by=None):
    """Створює Order + OrderItem з оплаченої угоди. Повертає Order.
    Якщо замовлення для угоди вже є — повертає його (ідемпотентність)."""
    if deal.order_id:
        _ensure_purchase_action(deal.order, deal.pk)
        return deal.order

    # This is the materialization boundary, so callers cannot bypass payment
    # truth by invoking the order builder directly with a forged deal status.
    from management.services.bot_payment_truth import verified_payment_deals

    if not verified_payment_deals(deal.__class__.objects.filter(pk=deal.pk)).exists():
        raise ValueError("IG order requires provider-confirmed payment")

    from orders.models import Order, OrderItem

    is_prepay = deal.pay_type == deal.PayType.PREPAY_200
    payment_status = "prepaid" if is_prepay else "paid"

    full_name = (
        deal.np_full_name
        or deal.client.display_name
        or deal.client.username
        or "IG клієнт"
    )
    phone = deal.np_phone or deal.client.phone or ""

    with transaction.atomic():
        # Блокування рядка угоди + повторна перевірка: захист від дубля замовлення
        # при гонці (вебхук Monobank + cron-поллінг одночасно).
        locked = deal.__class__.objects.select_for_update().get(pk=deal.pk)
        if locked.order_id:
            _ensure_purchase_action(locked.order, locked.pk)
            deal.order_id = locked.order_id
            deal.status = locked.status
            return locked.order
        order = Order(
            full_name=full_name[:200],
            phone=phone[:32],
            city=(deal.np_city or "")[:100],
            np_office=(deal.np_office or "")[:200],
            pay_type=deal.pay_type,
            payment_status=payment_status,
            status="new",
            source="manual",
            sale_source="Instagram",
            created_by=created_by,
            payment_provider="monobank",
            payment_invoice_id=deal.invoice_id or "",
            total_sum=deal.amount or Decimal("0"),
        )
        order.save()

        items = []
        for it in deal.items.all():
            items.append(
                OrderItem(
                    order=order,
                    product=it.product,
                    color_variant=it.color_variant,
                    title=it.title,
                    size=it.size or "",
                    qty=it.qty,
                    unit_price=it.unit_price,
                    line_total=it.line_total,
                    is_custom=(it.product_id is None),
                )
            )
        if items:
            OrderItem.objects.bulk_create(items)

        _ensure_purchase_action(order, locked.pk)

        locked.order = order
        locked.status = locked.Status.ORDER_CREATED
        locked.save(update_fields=["order", "status", "updated_at"])
        deal.order_id = order.id
        deal.status = locked.Status.ORDER_CREATED

    # Лічильники й стадія клієнта (best-effort, поза транзакцією замовлення).
    try:
        from management.models import IgClient

        c = deal.client
        c.purchases_count = (c.purchases_count or 0) + 1
        c.total_spent = (c.total_spent or Decimal("0")) + (deal.amount or Decimal("0"))
        flags = dict(c.conversion_flags or {})
        flags["is_buyer"] = True
        c.conversion_flags = flags
        # Скидаємо закріплений товар: наступна покупка почнеться «з чистого аркуша».
        c.current_product = None
        c.save(update_fields=[
            "purchases_count", "total_spent", "conversion_flags", "current_product", "updated_at",
        ])
        c.set_stage(IgClient.Stage.ORDER_CREATED, reason="order")
    except Exception:
        pass

    return order
