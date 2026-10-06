"""Списання за замовленням (з токеном з Telegram-кнопки або з UI)."""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from orders.models import Order
from warehouse.models import (
    MovementReason,
    PrintColorVariant,
    StockItem,
    WriteOffRequest,
)
from warehouse.permissions import warehouse_admin_required
from warehouse.services.inventory import (
    adjust_print_variant,
    adjust_stock_item,
    reverse_write_off,
)
from warehouse.services.matching import (
    DTF_GARMENT_WRITEOFF_ERROR,
    all_active_stock_items,
    find_prints_for_order_item,
    find_stock_items_for_order_item,
    group_stock_items_by_category,
    order_has_dtf_film,
)


def _get_or_create_request(order: Order) -> WriteOffRequest:
    pending = order.warehouse_write_off_requests.filter(
        status=WriteOffRequest.STATUS_PENDING
    ).first()
    if pending:
        return pending
    return WriteOffRequest.objects.create(order=order)


def _build_print_rows(item):
    """Готує дані принтів для одного OrderItem.

    Повертає список принтів, кожен зі своїми варіантами та обраним
    за замовчуванням варіантом (is_default → перший наявний).
    """
    prints_data = []
    for pr in find_prints_for_order_item(item):
        variants = list(pr.color_variants.all().order_by("order", "id"))
        if not variants:
            continue
        default_variant = next((v for v in variants if v.is_default), None)
        if default_variant is None:
            # перший варіант із залишком, інакше просто перший
            default_variant = next((v for v in variants if v.quantity > 0), variants[0])
        prints_data.append(
            {
                "print": pr,
                "variants": variants,
                "default_variant_id": default_variant.id if default_variant else None,
            }
        )
    return prints_data


@warehouse_admin_required
def write_off_entry(request, token):
    """Сторінка списання: одяг + усі прив'язані принти (лого на грудь, дизайн на спину…)."""
    try:
        token_uuid = uuid.UUID(str(token))
    except (ValueError, AttributeError):
        return render(request, "warehouse/write_off_invalid.html", status=400)

    wo_request = get_object_or_404(
        WriteOffRequest.objects.select_related("order"), token=token_uuid
    )
    order = wo_request.order

    if order_has_dtf_film(order):
        return HttpResponse(DTF_GARMENT_WRITEOFF_ERROR, status=409, content_type="text/plain; charset=utf-8")

    if not wo_request.opened_at:
        wo_request.opened_at = timezone.now()
        wo_request.opened_by = request.user
        wo_request.save(update_fields=["opened_at", "opened_by", "updated_at"])

    rows = []
    total_prints_linked = 0
    # Повний перелік складу — щоб у кожному рядку був вибір БУДЬ-ЯКОЇ одежі,
    # навіть якщо авто-матчинг не дав збігу (вимога: «відкривати всі списки одягу»).
    all_stock_groups = group_stock_items_by_category(all_active_stock_items())
    for item in order.items.select_related("product", "color_variant__color").all():
        candidates = find_stock_items_for_order_item(item)
        print_rows = _build_print_rows(item)
        total_prints_linked += len(print_rows)
        # авто-вибір кандидата складу: точний збіг або єдиний варіант
        default_stock_id = None
        if len(candidates) == 1:
            default_stock_id = candidates[0].id
        elif candidates:
            default_sub = next(
                (c for c in candidates if c.subcategory.is_default and c.quantity > 0),
                None,
            )
            if default_sub:
                default_stock_id = default_sub.id
            else:
                # перший кандидат із залишком, інакше просто перший
                in_stock = next((c for c in candidates if c.quantity > 0), None)
                default_stock_id = (in_stock or candidates[0]).id
        rows.append(
            {
                "item": item,
                "candidates": candidates,
                "default_stock_id": default_stock_id,
                "print_rows": print_rows,
            }
        )

    context = {
        "wo_request": wo_request,
        "order": order,
        "rows": rows,
        "all_stock_groups": all_stock_groups,
        "total_prints_linked": total_prints_linked,
        "completed": wo_request.status == WriteOffRequest.STATUS_COMPLETED,
        "active_section": "write_off",
    }
    return render(request, "warehouse/write_off.html", context)


def _notify_order_writeoff(order):
    """Best-effort: оновлює вихідне Telegram-повідомлення замовлення після списання.

    Редагування — це stateless-виклик ``editMessageText`` через основного бота
    замовлень (не тримаємо окремий процес/polling). Жодних винятків назовні:
    списання вже збережено в БД, повідомлення — вторинне.
    """
    try:
        from orders.telegram_notifications import telegram_notifier

        telegram_notifier.update_order_notification_message(order)
    except Exception:  # pragma: no cover - мережеві/конфіг помилки не блокують списання
        pass


class _WriteOffAbort(Exception):
    """Внутрішній сигнал відкату транзакції при помилках залишку."""


@warehouse_admin_required
@require_POST
def write_off_submit(request, token):
    try:
        token_uuid = uuid.UUID(str(token))
    except (ValueError, AttributeError):
        return HttpResponseBadRequest("invalid token")

    wo_request = get_object_or_404(WriteOffRequest, token=token_uuid)
    if wo_request.status != WriteOffRequest.STATUS_PENDING:
        messages.warning(request, "Цей запит на списання вже оброблено.")
        return redirect("warehouse:write_off", token=wo_request.token)

    order = wo_request.order
    actions = []
    errors = []
    completed = False
    review_reservation_id = None

    try:
        with transaction.atomic():
            # Serialize with order editing so a cached garment form cannot
            # deduct apparel after the order has been changed to DTF film.
            order = Order.objects.select_for_update().get(pk=order.pk)
            if order_has_dtf_film(order):
                return HttpResponse(DTF_GARMENT_WRITEOFF_ERROR, status=409, content_type="text/plain; charset=utf-8")
            for item in order.items.all():
                prefix = f"item_{item.pk}_"

                # --- Одяг ---
                stock_id_raw = request.POST.get(prefix + "stock_id") or ""
                try:
                    garment_qty = int(request.POST.get(prefix + "qty") or "0")
                except ValueError:
                    garment_qty = 0
                if stock_id_raw and stock_id_raw.isdigit() and garment_qty > 0:
                    try:
                        stock_item = StockItem.objects.get(pk=int(stock_id_raw))
                    except StockItem.DoesNotExist:
                        stock_item = None
                    if stock_item is not None:
                        try:
                            adjust_stock_item(
                                stock_item=stock_item,
                                delta=-garment_qty,
                                user=request.user,
                                reason=MovementReason.ORDER_WRITE_OFF,
                                comment=f"Замовлення #{order.order_number}",
                                order=order,
                                write_off_request=wo_request,
                            )
                            actions.append(
                                f"одяг {stock_item.subcategory.name} {stock_item.size} ×{garment_qty}"
                            )
                        except ValueError as exc:
                            errors.append(f"одяг {stock_item.subcategory.name}: {exc}")

                # --- Принти (декілька на виріб) ---
                # Формат: чекбокс name="item_{id}_print_on" value="{variant_id}",
                # кількість name="item_{id}_print_qty_{variant_id}".
                selected_variant_ids = request.POST.getlist(prefix + "print_on")
                for vid_raw in selected_variant_ids:
                    if not str(vid_raw).isdigit():
                        continue
                    try:
                        qty = int(request.POST.get(f"{prefix}print_qty_{vid_raw}") or "0")
                    except ValueError:
                        qty = 0
                    if qty <= 0:
                        continue
                    try:
                        variant = PrintColorVariant.objects.select_related("print").get(pk=int(vid_raw))
                    except PrintColorVariant.DoesNotExist:
                        continue
                    try:
                        adjust_print_variant(
                            variant=variant,
                            delta=-qty,
                            user=request.user,
                            reason=MovementReason.ORDER_WRITE_OFF,
                            comment=f"Замовлення #{order.order_number} · {item.title}",
                            order=order,
                            write_off_request=wo_request,
                        )
                        placement = variant.print.get_placement_display() if variant.print.placement else ""
                        label = f"{variant.print.name}"
                        if placement:
                            label += f" ({placement})"
                        actions.append(f"принт {label}/{variant.color_name} ×{qty}")
                    except ValueError as exc:
                        errors.append(f"принт {variant.print.name}: {exc}")

            # Якщо є помилки залишку — відкочуємо ВСЕ (без часткових списань,
            # щоб повторний сабміт не задублював успішні позиції).
            if errors:
                raise _WriteOffAbort()

            if actions:
                try:
                    from management.services.ig_inventory import (
                        fulfill_order_inventory_reservations,
                    )

                    fulfill_order_inventory_reservations(
                        order,
                        write_off_request=wo_request,
                        user=request.user,
                    )
                except Exception as exc:
                    review_reservation_id = getattr(exc, "reservation_id", None)
                    errors.append(str(exc)[:255] or "warehouse reservation mismatch")
                    raise _WriteOffAbort()
                wo_request.status = WriteOffRequest.STATUS_COMPLETED
                wo_request.completed_at = timezone.now()
                wo_request.completed_by = request.user
                wo_request.save(
                    update_fields=["status", "completed_at", "completed_by", "updated_at"]
                )
                completed = True
    except _WriteOffAbort:
        # Транзакцію відкочено: actions фактично не застосовано.
        actions = []
        if review_reservation_id:
            try:
                from management.models import IgCheckoutInventoryReservation

                IgCheckoutInventoryReservation.objects.filter(
                    pk=review_reservation_id,
                    state=IgCheckoutInventoryReservation.State.PAID_COMMITTED,
                ).update(
                    state=IgCheckoutInventoryReservation.State.OVERBOOKED_REVIEW,
                    release_reason="fulfillment_stock_shortfall",
                    updated_at=timezone.now(),
                )
            except Exception:
                pass

    for err in errors:
        messages.error(request, f"Помилка списання: {err}")

    if completed:
        # Оновлюємо вихідне повідомлення в Telegram (поза транзакцією,
        # після коміту — щоб мережеві затримки не тримали блокування БД).
        _notify_order_writeoff(order)
        messages.success(request, "Списано: " + ", ".join(actions))
        # Повертаємось на сторінку списання — вона покаже банер «вже списано».
        # Це легша відповідь, ніж важкий дашборд (захист від таймауту/503).
        return redirect("warehouse:write_off", token=wo_request.token)

    if errors:
        # Лишаємо на сторінці, щоб виправити кількість.
        return redirect("warehouse:write_off", token=wo_request.token)

    messages.info(request, "Жодних позицій не списано (всі поля пусті).")
    return redirect("warehouse:write_off", token=wo_request.token)


@warehouse_admin_required
def cancel_sale_entry(request, token):
    """Сторінка підтвердження відміни продажу/списання за замовленням."""
    try:
        token_uuid = uuid.UUID(str(token))
    except (ValueError, AttributeError):
        return render(request, "warehouse/write_off_invalid.html", status=400)

    wo_request = get_object_or_404(
        WriteOffRequest.objects.select_related("order"), token=token_uuid
    )
    order = wo_request.order
    # Позиції, які будуть повернені (рухи-списання за заявкою).
    movements = list(
        wo_request.movements.select_related("content_type").filter(delta__lt=0)
    )
    rows = []
    for mv in movements:
        target = mv.target
        rows.append({
            "name": str(target) if target is not None else "—",
            "qty": abs(mv.delta),
        })
    context = {
        "wo_request": wo_request,
        "order": order,
        "rows": rows,
        "already_cancelled": wo_request.status == WriteOffRequest.STATUS_CANCELLED,
        "is_completed": wo_request.status == WriteOffRequest.STATUS_COMPLETED,
        "active_section": "write_off",
    }
    return render(request, "warehouse/cancel_sale.html", context)


@warehouse_admin_required
@require_POST
def cancel_sale_submit(request, token):
    try:
        token_uuid = uuid.UUID(str(token))
    except (ValueError, AttributeError):
        return HttpResponseBadRequest("invalid token")

    wo_request = get_object_or_404(WriteOffRequest, token=token_uuid)

    if wo_request.status == WriteOffRequest.STATUS_CANCELLED:
        messages.info(request, "Цю продажу вже відмінено.")
        return redirect("warehouse:cancel_sale", token=wo_request.token)
    if wo_request.status != WriteOffRequest.STATUS_COMPLETED:
        messages.warning(request, "Немає завершеної продажі для відміни.")
        return redirect("warehouse:write_off", token=wo_request.token)

    order = wo_request.order
    count = reverse_write_off(write_off_request=wo_request, user=request.user)
    _notify_order_writeoff(order)
    if count:
        messages.success(request, f"Продаж відмінено, повернено позицій: {count}.")
    else:
        messages.info(request, "Нічого повертати — рухів не знайдено.")
    return redirect("warehouse:cancel_sale", token=wo_request.token)
