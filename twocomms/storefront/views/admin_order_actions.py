"""Staff-only entry points into the canonical order operation pages."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_GET

from orders.models import Order
from orders.nova_poshta_documents import (
    TELEGRAM_CREATE_NP_WAYBILL_ACTION,
    TELEGRAM_DELETE_NP_WAYBILL_ACTION,
)
from orders.telegram_status_links import build_order_action_url
from warehouse.services.order_links import (
    build_storage_cancel_sale_url,
    build_storage_writeoff_url,
    get_completed_write_off,
)


def _orders_return_url(order: Order) -> str:
    return f"{reverse('admin_panel')}?section=orders&edit_order={order.pk}"


@staff_member_required
@require_GET
def admin_order_nova_poshta_action(request, order_id: int):
    order = get_object_or_404(Order, pk=order_id)

    if order.status in {"done", "cancelled"}:
        messages.warning(
            request,
            f"Дія з ТТН недоступна для замовлення #{order.order_number} у поточному статусі.",
        )
        return redirect(_orders_return_url(order))

    document_ref = (order.nova_poshta_document_ref or "").strip()
    if document_ref:
        url = build_order_action_url(
            order,
            TELEGRAM_DELETE_NP_WAYBILL_ACTION,
            route_name="telegram_order_np_waybill_action",
            token_scope=document_ref,
        )
        return redirect(url)

    if (order.tracking_number or "").strip():
        messages.warning(
            request,
            f"У замовлення #{order.order_number} вже вказана ручна ТТН без API-документа.",
        )
        return redirect(_orders_return_url(order))

    url = build_order_action_url(
        order,
        TELEGRAM_CREATE_NP_WAYBILL_ACTION,
        route_name="telegram_order_np_waybill_action",
    )
    return redirect(url)


@staff_member_required
@require_GET
def admin_order_warehouse_action(request, order_id: int):
    order = get_object_or_404(Order, pk=order_id)

    if order.status == "cancelled":
        messages.warning(
            request,
            f"Списання недоступне для скасованого замовлення #{order.order_number}.",
        )
        return redirect(_orders_return_url(order))

    completed = get_completed_write_off(order)
    if completed is not None:
        url = build_storage_cancel_sale_url(order)
    else:
        url = build_storage_writeoff_url(order)

    if not url:
        messages.error(
            request,
            f"Не вдалося відкрити складську дію для замовлення #{order.order_number}.",
        )
        return redirect(_orders_return_url(order))
    return redirect(url)
