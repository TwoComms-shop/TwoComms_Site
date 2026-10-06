from __future__ import annotations

import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from orders.forms import TelegramNovaPoshtaWaybillForm
from orders.models import Order
from orders.nova_poshta_data import apply_nova_poshta_refs
from orders.nova_poshta_documents import (
    TELEGRAM_CREATE_NP_WAYBILL_ACTION,
    TELEGRAM_DELETE_NP_WAYBILL_ACTION,
    NovaPoshtaDocumentError,
    NovaPoshtaDocumentService,
    build_order_payment_snapshot,
    build_waybill_description,
    normalize_phone,
)
from orders.status_management import (
    _apply_order_status_update_to_order,
    apply_order_status_update,
    can_apply_telegram_status_action,
    first_validation_error,
    get_telegram_status_action,
)
from orders.telegram_notifications import telegram_notifier
from orders.telegram_status_links import (
    build_order_action_url,
    verify_order_action_token,
    verify_order_status_token,
)

logger = logging.getLogger(__name__)

STATUS_ACTION_TEMPLATE = "pages/telegram_order_status_action.html"
WAYBILL_ACTION_TEMPLATE = "pages/telegram_order_nova_poshta_action.html"


def _get_token(request) -> str:
    return (request.POST.get("token") or request.GET.get("token") or "").strip()


def _is_ajax(request) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _render_template(request, template_name: str, context: dict, *, status_code: int = 200):
    return render(request, template_name, context, status=status_code)


def _manual_ship_action_block_reason(order: Order, status: str) -> str | None:
    if not can_apply_telegram_status_action(order, status):
        return (
            f"Для цього замовлення дія більше недоступна. "
            f"Поточний статус: {order.get_status_display()}."
        )
    if status == "ship" and (order.tracking_number or order.nova_poshta_document_ref):
        return "Для цього замовлення ТТН уже збережена. Повторне оновлення через старе посилання заблоковано."
    return None


def _waybill_action_block_reason(order: Order) -> str | None:
    if order.status in {"done", "cancelled"}:
        return (
            f"Створення ТТН більше недоступне. "
            f"Поточний статус: {order.get_status_display()}."
        )
    if order.nova_poshta_document_ref:
        return "Для цього замовлення накладну Нова пошта вже створено."
    if order.tracking_number:
        return "Для цього замовлення ТТН уже збережена вручну або через інший сценарій."
    return None


def _delete_waybill_action_block_reason(order: Order) -> str | None:
    if order.status in {"done", "cancelled"}:
        return (
            f"Видалення ТТН більше недоступне. "
            f"Поточний статус: {order.get_status_display()}."
        )
    if not order.nova_poshta_document_ref:
        return "Для цього замовлення немає створеної через API накладної Нова пошта."
    return None


def _build_delete_waybill_url(order: Order | None) -> str:
    if not order or not getattr(order, "nova_poshta_document_ref", None):
        return ""
    try:
        return build_order_action_url(
            order,
            TELEGRAM_DELETE_NP_WAYBILL_ACTION,
            route_name="telegram_order_np_waybill_action",
            token_scope=order.nova_poshta_document_ref or "",
        )
    except Exception:
        logger.exception("Failed to build Nova Poshta delete action URL for order %s", getattr(order, "pk", None))
        return ""


def _hidden_field_errors(form: TelegramNovaPoshtaWaybillForm | None) -> list[str]:
    if not form:
        return []
    errors: list[str] = []
    for field in form.hidden_fields():
        for error in field.errors:
            errors.append(f"{field.name}: {error}")
    return errors


def _fallback_waybill_initial(service: NovaPoshtaDocumentService, order: Order) -> dict[str, str]:
    payment_snapshot = build_order_payment_snapshot(order)
    return {
        "recipient_full_name": order.full_name or "",
        "recipient_phone": normalize_phone(order.phone or "") or (order.phone or ""),
        "recipient_city": order.city or "",
        "recipient_settlement_ref": getattr(order, "np_settlement_ref", "") or "",
        "recipient_city_ref": getattr(order, "np_city_ref", "") or "",
        "recipient_city_token": "",
        "recipient_warehouse": order.np_office or "",
        "recipient_warehouse_ref": getattr(order, "np_warehouse_ref", "") or "",
        "recipient_warehouse_token": "",
        "sender_city": service.SENDER_CITY_QUERY,
        "sender_settlement_ref": "",
        "sender_city_ref": "",
        "sender_city_token": "",
        "sender_warehouse": service.SENDER_WAREHOUSE_QUERY,
        "sender_warehouse_ref": "",
        "sender_warehouse_token": "",
        "description": build_waybill_description(order),
        "declared_cost": payment_snapshot["declared_cost"],
        "weight": "1.0",
        "seats_amount": "1",
        "length_cm": f"{service.DEFAULT_LENGTH_CM}",
        "width_cm": f"{service.DEFAULT_WIDTH_CM}",
        "height_cm": f"{service.DEFAULT_HEIGHT_CM}",
        "cod_amount": payment_snapshot["cod_amount"] if payment_snapshot["cod_amount_value"] > 0 else "",
        "payer_type": payment_snapshot["delivery_payer_type"],
        "payment_method": "Cash",
    }


def _build_waybill_initial(service: NovaPoshtaDocumentService, order: Order) -> tuple[dict[str, str], str]:
    try:
        return service.build_initial_payload(order), ""
    except NovaPoshtaDocumentError as exc:
        logger.warning("Failed to prefill Nova Poshta action for order %s: %s", order.pk, exc)
        fallback = _fallback_waybill_initial(service, order)
        return fallback, (
            "Не вдалося повністю підтягнути довідкові дані Нової пошти. "
            "Можна перевірити поля вручну і спробувати створити ТТН повторно."
        )


def _update_telegram_message_after_order_change(order: Order, *, clear_actions: bool = False) -> None:
    try:
        telegram_notifier.update_order_notification_message(order, clear_actions=clear_actions)
    except Exception:
        logger.exception("Failed to update Telegram order notification message for order %s", order.pk)


def _delete_created_waybill_safely(
    service: NovaPoshtaDocumentService,
    *,
    document_ref: str,
    order_id: int,
) -> None:
    if not document_ref:
        return
    try:
        service.delete_waybill(document_ref)
    except Exception:
        logger.exception(
            "Failed to compensate Nova Poshta waybill creation after local order update failed for order %s",
            order_id,
        )


def _payment_snapshot_payload(order: Order) -> dict:
    snapshot = build_order_payment_snapshot(order)
    payload = order.payment_payload or {}
    history = payload.get("history") or []
    last_entry = history[-1] if history else {}
    snapshot["payment_last_status"] = last_entry.get("status") or payload.get("last_status") or ""
    snapshot["payment_last_time"] = (
        last_entry.get("received_at")
        or last_entry.get("ts")
        or payload.get("last_update_at")
        or ""
    )
    return snapshot


def _render_status_action_page(
    request,
    *,
    order: Order | None,
    action: dict | None,
    token: str,
    status_code: int = 200,
    success_message: str = "",
    error_message: str = "",
    is_success: bool = False,
    is_blocked: bool = False,
    tracking_number: str = "",
    can_submit: bool = True,
):
    return _render_template(
        request,
        STATUS_ACTION_TEMPLATE,
        {
            "order": order,
            "action": action,
            "token": token,
            "success_message": success_message,
            "error_message": error_message,
            "is_success": is_success,
            "is_blocked": is_blocked,
            "tracking_number": tracking_number,
            "can_submit": can_submit,
        },
        status_code=status_code,
    )


def _render_waybill_action_page(
    request,
    *,
    order: Order | None,
    token: str,
    form: TelegramNovaPoshtaWaybillForm | None,
    status_code: int = 200,
    success_message: str = "",
    error_message: str = "",
    helper_message: str = "",
    is_success: bool = False,
    is_blocked: bool = False,
    warnings: list[str] | None = None,
    can_submit: bool = True,
    can_delete: bool = False,
):
    delete_url = _build_delete_waybill_url(order) if can_delete else ""
    resolved_can_delete = bool(can_delete and delete_url)

    if _is_ajax(request):
        field_errors = _hidden_field_errors(form)
        if form is not None:
            for bound_field in form.visible_fields():
                for error in bound_field.errors:
                    field_errors.append(f"{bound_field.label or bound_field.name}: {error}")
            for error in form.non_field_errors():
                field_errors.append(str(error))

        effective_error = error_message
        if not effective_error and not is_success and field_errors:
            effective_error = field_errors[0]

        return JsonResponse(
            {
                "success": bool(is_success),
                "is_blocked": bool(is_blocked),
                "can_submit": bool(can_submit),
                "can_delete": resolved_can_delete,
                "delete_url": delete_url,
                "success_message": success_message,
                "error_message": effective_error,
                "helper_message": helper_message,
                "warnings": warnings or [],
                "field_errors": field_errors,
                "tracking_number": getattr(order, "tracking_number", "") or "",
            },
            status=status_code,
        )

    return _render_template(
        request,
        WAYBILL_ACTION_TEMPLATE,
        {
            "order": order,
            "token": token,
            "form": form,
            "success_message": success_message,
            "error_message": error_message,
            "helper_message": helper_message,
            "is_success": is_success,
            "is_blocked": is_blocked,
            "warnings": warnings or [],
            "can_submit": can_submit,
            "can_delete": resolved_can_delete,
            "delete_url": delete_url,
            "hidden_field_errors": _hidden_field_errors(form),
            "payment_snapshot": _payment_snapshot_payload(order) if order else None,
        },
        status_code=status_code,
    )


@require_http_methods(["GET", "POST"])
def telegram_order_status_action(request, order_id: int, status: str):
    action = get_telegram_status_action(status)
    if not action:
        raise Http404("Unknown Telegram order action")

    token = _get_token(request)
    if not verify_order_status_token(token, order_id=order_id, status=status):
        return _render_status_action_page(
            request,
            order=None,
            action=action,
            token=token,
            status_code=403,
            error_message="Посилання недійсне або вже застаріло. Відкрийте актуальне повідомлення в Telegram.",
            can_submit=False,
        )

    order = get_object_or_404(Order, pk=order_id)
    block_reason = _manual_ship_action_block_reason(order, status)
    if block_reason:
        return _render_status_action_page(
            request,
            order=order,
            action=action,
            token=token,
            status_code=409,
            is_blocked=True,
            tracking_number=order.tracking_number or "",
            error_message=block_reason,
            can_submit=False,
        )

    tracking_number = (request.POST.get("tracking_number") or order.tracking_number or "").strip()
    if request.method == "POST":
        try:
            result = apply_order_status_update(
                order.pk,
                status=status,
                tracking_number=tracking_number,
                require_tracking_number=bool(action.get("requires_tracking_number")),
            )
            updated_order = result["order"]
            # Не стираємо всі кнопки: лишаємо керування (зокрема складську
            # кнопку «продати/відмінити продаж»). Меню перебудується під
            # новий статус замовлення.
            _update_telegram_message_after_order_change(updated_order, clear_actions=False)
            return _render_status_action_page(
                request,
                order=updated_order,
                action=action,
                token=token,
                is_success=True,
                tracking_number=updated_order.tracking_number or "",
                success_message=(
                    f"Замовлення #{updated_order.order_number} переведено у статус "
                    f"«{updated_order.get_status_display()}»."
                ),
            )
        except ValidationError as exc:
            return _render_status_action_page(
                request,
                order=order,
                action=action,
                token=token,
                tracking_number=tracking_number,
                error_message=first_validation_error(exc),
            )

    return _render_status_action_page(
        request,
        order=order,
        action=action,
        token=token,
        tracking_number=tracking_number,
    )


@require_http_methods(["GET", "POST"])
def telegram_order_np_waybill_action(request, order_id: int, action: str):
    if action not in {TELEGRAM_CREATE_NP_WAYBILL_ACTION, TELEGRAM_DELETE_NP_WAYBILL_ACTION}:
        raise Http404("Unknown Telegram order action")

    token = _get_token(request)
    if action == TELEGRAM_DELETE_NP_WAYBILL_ACTION:
        order = Order.objects.filter(pk=order_id).first()
        if not order or not verify_order_action_token(
            token,
            order_id=order_id,
            action=action,
            scope=order.nova_poshta_document_ref or "",
        ):
            return _render_waybill_action_page(
                request,
                order=None,
                token=token,
                form=None,
                status_code=403,
                error_message="Посилання недійсне або вже застаріло. Відкрийте актуальне повідомлення в Telegram.",
                can_submit=False,
            )
    elif not verify_order_action_token(token, order_id=order_id, action=action):
        return _render_waybill_action_page(
            request,
            order=None,
            token=token,
            form=None,
            status_code=403,
            error_message="Посилання недійсне або вже застаріло. Відкрийте актуальне повідомлення в Telegram.",
            can_submit=False,
        )
    else:
        order = get_object_or_404(Order, pk=order_id)

    service = NovaPoshtaDocumentService()
    if not service.is_configured():
        return _render_waybill_action_page(
            request,
            order=order,
            token=token,
            form=None,
            status_code=503,
            error_message="Інтеграція з Nova Poshta API не налаштована в цьому середовищі.",
            can_submit=False,
        )

    if action == TELEGRAM_DELETE_NP_WAYBILL_ACTION:
        block_reason = _delete_waybill_action_block_reason(order)
        if block_reason:
            return _render_waybill_action_page(
                request,
                order=order,
                token=token,
                form=None,
                status_code=409,
                error_message=block_reason,
                is_blocked=True,
                can_submit=False,
            )

        if request.method == "POST":
            tracking_number = order.tracking_number or ""
            try:
                with transaction.atomic():
                    locked_order = Order.objects.select_for_update().get(pk=order.pk)
                    block_reason = _delete_waybill_action_block_reason(locked_order)
                    if block_reason:
                        return _render_waybill_action_page(
                            request,
                            order=locked_order,
                            token=token,
                            form=None,
                            status_code=409,
                            error_message=block_reason,
                            is_blocked=True,
                            can_submit=False,
                        )

                    document_ref = locked_order.nova_poshta_document_ref or ""
                    if not verify_order_action_token(
                        token,
                        order_id=locked_order.pk,
                        action=action,
                        scope=document_ref,
                    ):
                        return _render_waybill_action_page(
                            request,
                            order=None,
                            token=token,
                            form=None,
                            status_code=403,
                            error_message=(
                                "Посилання недійсне або вже застаріло. "
                                "Відкрийте актуальне повідомлення в Telegram."
                            ),
                            can_submit=False,
                        )

                    tracking_number = locked_order.tracking_number or tracking_number
                    deleted = service.delete_waybill(document_ref)
                    locked_order.tracking_number = None
                    locked_order.shipment_status = None
                    locked_order.shipment_status_updated = None
                    locked_order.tracking_status_code = None
                    locked_order.tracking_checked_at = None
                    locked_order.tracking_provider_event_at = None
                    locked_order.tracking_next_check_at = None
                    locked_order.tracking_failure_count = 0
                    locked_order.tracking_terminal_at = None
                    locked_order.nova_poshta_document_ref = None
                    locked_order.nova_poshta_recipient_ref = None
                    locked_order.nova_poshta_recipient_contact_ref = None
                    if locked_order.status == "ship":
                        locked_order.status = "prep"
                    locked_order.save(
                        update_fields=[
                            "tracking_number",
                            "shipment_status",
                            "shipment_status_updated",
                            "tracking_status_code",
                            "tracking_checked_at",
                            "tracking_provider_event_at",
                            "tracking_next_check_at",
                            "tracking_failure_count",
                            "tracking_terminal_at",
                            "nova_poshta_document_ref",
                            "nova_poshta_recipient_ref",
                            "nova_poshta_recipient_contact_ref",
                            "status",
                        ]
                    )

                _update_telegram_message_after_order_change(locked_order)
                return _render_waybill_action_page(
                    request,
                    order=locked_order,
                    token=token,
                    form=None,
                    is_success=True,
                    success_message=(
                        f"ТТН {tracking_number} видалено в Nova Poshta API і очищено в замовленні "
                        f"#{locked_order.order_number}."
                    ),
                    warnings=deleted.get("warnings") or [],
                    can_submit=False,
                )
            except NovaPoshtaDocumentError as exc:
                error_message = str(exc)
            except Exception:
                logger.exception("Unexpected error while deleting Nova Poshta waybill for order %s", order.pk)
                error_message = "Не вдалося видалити ТТН через внутрішню помилку. Спробуйте ще раз."

            return _render_waybill_action_page(
                request,
                order=order,
                token=token,
                form=None,
                error_message=error_message,
                can_submit=False,
                can_delete=True,
            )

        return _render_waybill_action_page(
            request,
            order=order,
            token=token,
            form=None,
            helper_message="Перевірте ТТН перед видаленням. Після видалення накладну можна буде створити заново.",
            can_submit=False,
            can_delete=True,
        )

    block_reason = _waybill_action_block_reason(order)
    if block_reason:
        return _render_waybill_action_page(
            request,
            order=order,
            token=token,
            form=None,
            status_code=409,
            error_message=block_reason,
            is_blocked=True,
            can_submit=False,
        )

    if request.method == "GET":
        initial, helper_message = _build_waybill_initial(service, order)
        form = TelegramNovaPoshtaWaybillForm(initial=initial)
    else:
        helper_message = ""
        form = TelegramNovaPoshtaWaybillForm(request.POST)

    if request.method == "POST" and form.is_valid():
        created: dict | None = None
        try:
            with transaction.atomic():
                locked_order = (
                    Order.objects.select_for_update()
                    .select_related("user")
                    .prefetch_related("items__product")
                    .get(pk=order.pk)
                )
                block_reason = _waybill_action_block_reason(locked_order)
                snapshot = build_order_payment_snapshot(locked_order)
                if not block_reason and (
                    snapshot['delivery_payment_requires_manual']
                    or form.cleaned_data['payer_type'] != snapshot['delivery_payer_type']
                ):
                    block_reason = 'Збережіть правильний спосіб оплати доставки в замовленні перед створенням ТТН.'
                if block_reason:
                    return _render_waybill_action_page(
                        request,
                        order=locked_order,
                        token=token,
                        form=form,
                        status_code=409,
                        error_message=block_reason,
                        is_blocked=True,
                        can_submit=False,
                    )

                created = service.create_waybill(locked_order, form.cleaned_data)
                recipient_point = created["recipient_point"]
                locked_order.full_name = form.cleaned_data["recipient_full_name"]
                locked_order.phone = form.cleaned_data["recipient_phone"]
                locked_order.city = recipient_point.city_label or form.cleaned_data["recipient_city"]
                locked_order.np_office = recipient_point.warehouse_label or form.cleaned_data["recipient_warehouse"]
                apply_nova_poshta_refs(
                    locked_order,
                    {
                        "np_settlement_ref": recipient_point.settlement_ref,
                        "np_city_ref": recipient_point.city_ref,
                        "np_warehouse_ref": recipient_point.warehouse_ref,
                    },
                )
                locked_order.nova_poshta_recipient_ref = created["recipient_ref"]
                locked_order.nova_poshta_recipient_contact_ref = created["recipient_contact_ref"]
                locked_order.nova_poshta_document_ref = created["document_ref"] or None

                update_result = _apply_order_status_update_to_order(
                    locked_order,
                    status="ship",
                    tracking_number=created["tracking_number"],
                    require_tracking_number=True,
                )
                updated_order = update_result["order"]
        except NovaPoshtaDocumentError as exc:
            error_message = str(exc)
        except ValidationError as exc:
            _delete_created_waybill_safely(
                service,
                document_ref=str((created or {}).get("document_ref") or ""),
                order_id=order.pk,
            )
            error_message = first_validation_error(exc)
        except Exception:
            _delete_created_waybill_safely(
                service,
                document_ref=str((created or {}).get("document_ref") or ""),
                order_id=order.pk,
            )
            logger.exception("Unexpected error while creating Nova Poshta waybill for order %s", order.pk)
            error_message = "Не вдалося створити ТТН через внутрішню помилку. Спробуйте ще раз."
        else:
            _update_telegram_message_after_order_change(updated_order)
            return _render_waybill_action_page(
                request,
                order=updated_order,
                token=token,
                form=TelegramNovaPoshtaWaybillForm(initial=form.cleaned_data),
                is_success=True,
                success_message=(
                    f"ТТН {updated_order.tracking_number} створено і прив’язано до замовлення "
                    f"#{updated_order.order_number}."
                ),
                warnings=created.get("warnings") or [],
                helper_message=helper_message,
                can_submit=False,
                can_delete=True,
            )

        return _render_waybill_action_page(
            request,
            order=order,
            token=token,
            form=form,
            error_message=error_message,
            helper_message=helper_message,
        )

    return _render_waybill_action_page(
        request,
        order=order,
        token=token,
        form=form,
        helper_message=helper_message,
    )


@require_http_methods(["GET"])
def telegram_order_payment_snapshot(request, order_id: int, action: str):
    if action != TELEGRAM_CREATE_NP_WAYBILL_ACTION:
        raise Http404("Unknown Telegram order action")

    token = _get_token(request)
    if not verify_order_action_token(token, order_id=order_id, action=action):
        return JsonResponse(
            {
                "success": False,
                "error": "Посилання недійсне або вже застаріло.",
            },
            status=403,
        )

    order = get_object_or_404(Order, pk=order_id)
    return JsonResponse(
        {
            "success": True,
            "order_id": order.pk,
            "snapshot": _payment_snapshot_payload(order),
        }
    )
