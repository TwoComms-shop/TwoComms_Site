import logging
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.db import transaction
from django.http import JsonResponse

from orders.nova_poshta_data import apply_nova_poshta_refs
from orders.models import Order, OrderItem
from orders.nova_poshta_documents import normalize_checkout_phone
from orders.nova_poshta_checkout import NovaPoshtaSelectionError, resolve_delivery_selection
from storefront.models import Product, PromoCode, CustomPrintLead, CustomPrintModerationStatus
from productcolors.models import ProductColorVariant
from accounts.models import UserProfile
from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
from .utils import (
    get_cart_from_session,
    clear_cart,
)
# from .cart import clear_cart
from ..utm_tracking import (
    link_order_to_utm,
    record_initiate_checkout,
    record_order_action,
)

logger = logging.getLogger(__name__)

class _ZeroTotalOrderError(Exception):
    """W1-5б (CRO-047): попытка создать заказ на 0 грн — откат транзакции."""


# W1-14 (NEW-514): окно дедупликации повторного сабмита заказа, сек.
ORDER_DEDUP_WINDOW_SECONDS = 30
ORDER_DEDUP_SESSION_KEY = 'last_order_submit'


def _cart_fingerprint(cart, custom_cart):
    """Стабильный отпечаток содержимого корзины для дедупа double-submit."""
    import hashlib
    import json
    try:
        payload = json.dumps([cart or {}, custom_cart or {}], sort_keys=True, default=str)
    except Exception:
        payload = repr((cart, custom_cart))
    return hashlib.sha256(payload.encode()).hexdigest()


def _find_recent_duplicate_order(request, fingerprint):
    """
    W1-14: если та же корзина уже была отправлена этой сессией за последние
    ORDER_DEDUP_WINDOW_SECONDS секунд — возвращает id созданного заказа.
    """
    import time
    entry = request.session.get(ORDER_DEDUP_SESSION_KEY)
    if not isinstance(entry, dict):
        return None
    try:
        if (
            entry.get('fingerprint') == fingerprint
            and (time.time() - float(entry.get('ts', 0))) < ORDER_DEDUP_WINDOW_SECONDS
            and entry.get('order_id')
        ):
            return entry['order_id']
    except Exception:
        pass
    return None


def _remember_order_submit(request, fingerprint, order):
    import time
    request.session[ORDER_DEDUP_SESSION_KEY] = {
        'fingerprint': fingerprint,
        'ts': time.time(),
        'order_id': order.id,
    }
    request.session.modified = True


def checkout_view(request):
    """
    Redirect to cart, as checkout is now integrated into cart view.
    """
    return redirect('cart')


@require_POST
def create_order(request):
    """
    Creates an order from the current cart.
    """
    cart = get_cart_from_session(request)
    custom_cart = request.session.get(SESSION_CUSTOM_CART_KEY) or {}
    if not cart and not (isinstance(custom_cart, dict) and custom_cart):
        messages.error(request, _("Ваш кошик порожній"))
        return redirect('cart')

    # W1-14 (NEW-514): защита от double-submit — та же корзина от той же
    # сессии в течение 30s не создаёт второй заказ, а ведёт на уже созданный.
    submit_fingerprint = _cart_fingerprint(cart, custom_cart)
    duplicate_order_id = _find_recent_duplicate_order(request, submit_fingerprint)
    if duplicate_order_id:
        logger.info('Duplicate order submit deduped -> order %s', duplicate_order_id)
        return redirect('order_success', order_id=duplicate_order_id)

    # Split custom-print items into approved (join the order) and pending
    # (stay in the cart for a later, combined payment). Regular items are paid now.
    approved_custom_leads = []
    pending_custom_keys = []  # custom-cart keys to keep in session after checkout
    if isinstance(custom_cart, dict) and custom_cart:
        key_to_lead_id = {
            k: v.get('lead_id')
            for k, v in custom_cart.items()
            if isinstance(v, dict) and v.get('lead_id')
        }
        lead_ids = [lid for lid in key_to_lead_id.values() if lid]
        leads_qs = list(CustomPrintLead.objects.filter(pk__in=lead_ids)) if lead_ids else []
        leads_by_id = {l.pk: l for l in leads_qs}
        for key, lead_id in key_to_lead_id.items():
            lead = leads_by_id.get(lead_id)
            if lead and lead.moderation_status == CustomPrintModerationStatus.APPROVED:
                approved_custom_leads.append(lead)
            else:
                pending_custom_keys.append(key)
        # If there's nothing payable now (no regulars, no approved customs) — wait.
        if not cart and not approved_custom_leads:
            messages.info(
                request,
                _("Кастомний принт ще очікує на перевірку менеджера. Оплата стане доступною після погодження.")
            )
            return redirect('cart')

    # Get user data
    try:
        delivery_selection = resolve_delivery_selection(request.POST)
    except NovaPoshtaSelectionError as exc:
        messages.error(request, exc.message)
        return redirect('cart')

    delivery_refs = {
        "np_settlement_ref": delivery_selection.settlement_ref,
        "np_city_ref": delivery_selection.city_ref,
        "np_warehouse_ref": delivery_selection.warehouse_ref,
    }

    if request.user.is_authenticated:
        user = request.user
        raw_phone = request.POST.get('phone') or ''
        try:
            profile = user.userprofile
            full_name = (request.POST.get('full_name') or profile.full_name or user.get_full_name() or '').strip()
            raw_phone = request.POST.get('phone') or profile.phone or ''
            phone = normalize_checkout_phone(raw_phone)
            city = delivery_selection.city
            np_office = delivery_selection.np_office
            pay_type = (request.POST.get('pay_type') or profile.pay_type or 'online_full').strip()
            customer_email = (request.POST.get('email') or getattr(profile, 'email', '') or user.email or '').strip()
        except UserProfile.DoesNotExist:
            full_name = request.POST.get('full_name', '')
            raw_phone = request.POST.get('phone', '')
            phone = normalize_checkout_phone(raw_phone)
            city = delivery_selection.city
            np_office = delivery_selection.np_office
            pay_type = request.POST.get('pay_type', 'online_full')
            customer_email = (request.POST.get('email') or user.email or '').strip()
    else:
        user = None
        full_name = request.POST.get('full_name', '')
        raw_phone = request.POST.get('phone', '')
        phone = normalize_checkout_phone(raw_phone)
        city = delivery_selection.city
        np_office = delivery_selection.np_office
        # Direct guest-form submits are a fallback; online payments are started
        # by the Monobank button, which creates its own order and invoice.
        pay_type = request.POST.get('pay_type', 'cod')
        customer_email = (request.POST.get('email') or '').strip()

    if raw_phone and not phone:
        messages.error(request, _("Вкажіть коректний український номер телефону. Можна без +380."))
        return redirect('cart')

    # Validate required fields
    if not all([full_name, phone, city, np_office]):
        messages.error(request, _("Будь ласка, заповніть всі обов'язкові поля"))
        return redirect('cart')

    # Prepay is disabled when custom items are present
    if approved_custom_leads and pay_type == 'prepay_200':
        messages.error(
            request,
            _("Передплата 200 грн недоступна з кастомним принтом. Оберіть повну онлайн-оплату.")
        )
        return redirect('cart')

    # Avoid creating an unpaid online order from a plain form submit. The
    # Monobank button handles online order creation and invoice generation.
    if pay_type in ('online_full', 'prepay_200', 'full', 'partial'):
        messages.info(
            request,
            _("Для онлайн-оплати скористайтеся кнопкою оплати Monobank у кошику.")
        )
        return redirect('cart')

    # W1-5а (CRO-047): проверяем наличие всех товаров ДО создания заказа —
    # исчезнувший товар раньше молча выбрасывался (`if not product: continue`),
    # и покупатель получал заказ без части позиций.
    product_ids = [item['product_id'] for item in cart.values()]
    products_map = Product.objects.in_bulk(product_ids)
    missing_items = [
        item for item in cart.values()
        if not products_map.get(int(item['product_id']))
    ]
    if missing_items:
        # Убираем недоступные позиции из корзины и возвращаем покупателя
        # на корзину с понятным сообщением — заказ НЕ создаём.
        cart_session = request.session.get('cart') or {}
        for key in list(cart_session.keys()):
            entry = cart_session.get(key) or {}
            if not products_map.get(int(entry.get('product_id') or 0)):
                cart_session.pop(key, None)
        request.session['cart'] = cart_session
        request.session.modified = True
        messages.error(
            request,
            _("Деякі товари з кошика більше недоступні та були видалені. Перевірте кошик і спробуйте ще раз.")
        )
        return redirect('cart')

    try:
        with transaction.atomic():
            # Validate optional email
            normalized_email = None
            if customer_email:
                try:
                    from django.core.validators import validate_email as _validate_email
                    from django.core.exceptions import ValidationError as _ValidationError
                    try:
                        _validate_email(customer_email)
                        normalized_email = customer_email
                    except _ValidationError:
                        normalized_email = None
                except Exception:
                    normalized_email = None

            # Create Order
            order = Order(
                user=user,
                full_name=full_name,
                phone=phone,
                email=normalized_email,
                city=city,
                np_office=np_office,
                session_key=request.session.session_key,
                pay_type=pay_type,
                status='new',
                payment_status='unpaid'
            )
            apply_nova_poshta_refs(order, delivery_refs)
            order.save()
            link_order_to_utm(request, order)

            # Брошенная корзина «спасена» — больше не дёргаем покупателя.
            try:
                from orders.models import CheckoutCapture
                if request.session.session_key:
                    CheckoutCapture.objects.filter(
                        session_key=request.session.session_key
                    ).update(converted=True)
            except Exception:
                pass

            # Create Order Items (products_map подготовлен выше, до atomic)
            total_sum = Decimal('0')

            variant_ids = [item.get('color_variant_id') for item in cart.values() if item.get('color_variant_id')]
            variants_map = ProductColorVariant.objects.in_bulk(variant_ids)

            order_items = []
            for item in cart.values():
                product = products_map.get(int(item['product_id']))
                if not product:
                    continue

                qty = int(item['qty'])
                price = product.final_price

                variant_id = item.get('color_variant_id')
                variant = variants_map.get(int(variant_id)) if variant_id else None

                order_items.append(OrderItem(
                    order=order,
                    product=product,
                    color_variant=variant,
                    title=product.title,
                    size=item.get('size', 'S'),
                    fit_option_code=(item.get('fit_option_code') or item.get('fit') or ''),
                    fit_option_label=(item.get('fit_option_label') or item.get('fit_label') or ''),
                    qty=qty,
                    unit_price=price,
                    line_total=price * qty,
                ))
                total_sum += price * qty

            OrderItem.objects.bulk_create(order_items)

            # Attach approved custom-print leads to this order and add their totals
            for lead in approved_custom_leads:
                try:
                    total_sum += Decimal(str(lead.final_price_value))
                except Exception:
                    pass
                lead.order = order
                lead.save(update_fields=["order"])

            order.total_sum = total_sum

            # W1-5б (CRO-047): guard от заказа на 0 грн (пустые/нулевые позиции)
            if total_sum <= 0:
                raise _ZeroTotalOrderError()

            # Apply Promo Code (W1-4а / CRO-046)
            # apply_promo_code кладёт в сессию promo_code_id — читаем его же
            # (старый код читал мёртвый ключ 'promo_code' с несуществующими
            # полями active/is_valid(), из-за чего промо в COD не работало).
            # Промокоды доступны только зарегистрированным пользователям
            # (та же политика, что в apply_promo_code).
            applied_promo = None
            promo_code_id = request.session.get('promo_code_id')
            if promo_code_id and request.user.is_authenticated:
                try:
                    promo = PromoCode.objects.get(id=promo_code_id)
                    can_use, _reason = promo.can_be_used_by_user(request.user)
                    if can_use:
                        discount = promo.calculate_discount(total_sum)
                        if discount > 0:
                            order.discount_amount = discount
                            order.promo_code = promo
                            applied_promo = promo
                except PromoCode.DoesNotExist:
                    pass
                except Exception:
                    logger.warning('Error applying promo code to COD order', exc_info=True)

            order.save()

            # W1-4б: COD-заказ размещён — фиксируем использование промокода
            # (лимиты one_time_per_user / max_uses).
            if applied_promo is not None:
                try:
                    applied_promo.record_usage(request.user, order)
                except Exception:
                    logger.warning('Failed to record promo usage for order %s', order.pk, exc_info=True)
                request.session.pop('promo_code_id', None)
                request.session.pop('promo_code', None)
                request.session.pop('promo_code_data', None)
                request.session.modified = True

            # Clear regular cart — approved custom items are now attached to the order.
            clear_cart(request)
            # Keep only unapproved custom items in session so the user can pay them later.
            current_custom = request.session.get(SESSION_CUSTOM_CART_KEY) or {}
            if isinstance(current_custom, dict):
                remaining = {
                    k: v for k, v in current_custom.items()
                    if k in pending_custom_keys
                }
                request.session[SESSION_CUSTOM_CART_KEY] = remaining
                request.session.modified = True

            # Funnel analytics: checkout initiated + lead.
            # Online pay types never reach this point (redirected above), so the
            # confirmed pay-on-delivery order is the lead.
            record_initiate_checkout(request, float(order.total_sum))
            record_order_action(
                'lead',
                order,
                request=request,
                cart_value=float(order.total_sum),
                metadata={'pay_type': pay_type},
            )
            remember_order_in_session(request, order)
            _remember_order_submit(request, submit_fingerprint, order)
            return redirect('order_success', order_id=order.id)

    except _ZeroTotalOrderError:
        # W1-5б: транзакция откатана — заказ на 0 грн не создан.
        logger.warning('Rejected zero-total order attempt (session %s)', request.session.session_key)
        messages.error(request, _("Сума замовлення дорівнює нулю. Перевірте кошик і спробуйте ще раз."))
        return redirect('cart')
    except Exception as e:
        logger.error(f"Error creating order: {e}", exc_info=True)
        messages.error(request, _("Сталася помилка при оформленні замовлення. Спробуйте ще раз."))
        return redirect('cart')


def payment_method(request):
    return redirect('cart')


# NOTE (W1-3/W1-5г): стаб monobank_webhook удалён — реальный обработчик с
# проверкой подписи живёт в storefront/views/monobank.py.


def payment_callback(request):
    return redirect('home')


RECENT_ORDER_IDS_SESSION_KEY = 'recent_order_ids'
RECENT_ORDER_IDS_LIMIT = 10


def remember_order_in_session(request, order):
    """
    W1-2 (CRO-044): remember the order id in the visitor's session so the
    success page stays reachable for the buyer even if session_key rotates
    or was empty at order creation time.
    """
    try:
        recent = request.session.get(RECENT_ORDER_IDS_SESSION_KEY) or []
        if not isinstance(recent, list):
            recent = []
        if order.id not in recent:
            recent.append(order.id)
        request.session[RECENT_ORDER_IDS_SESSION_KEY] = recent[-RECENT_ORDER_IDS_LIMIT:]
        request.session.modified = True
    except Exception:
        logger.warning('Failed to remember order %s in session', getattr(order, 'id', None))


def _can_view_order(request, order):
    """
    W1-2 (CRO-044): only the order owner (by user account or by session)
    or staff may view the success page — it exposes PII (name/phone/address).
    """
    if request.user.is_authenticated:
        if request.user.is_staff:
            return True
        if order.user_id and order.user_id == request.user.id:
            return True
        # Authenticated user must not see other accounts' orders.
        if order.user_id:
            return False
    # Guest (or owner-by-session) access: match the session that created the order.
    session_key = request.session.session_key
    if session_key and order.session_key and order.session_key == session_key:
        return True
    recent = request.session.get(RECENT_ORDER_IDS_SESSION_KEY) or []
    if isinstance(recent, list) and order.id in recent:
        return True
    return False


def order_success(request, order_id):
    order = get_object_or_404(
        Order.objects.prefetch_related('items__product', 'items__color_variant', 'custom_print_leads'),
        id=order_id
    )
    if not _can_view_order(request, order):
        # 404 (not 403) so order ids can't be enumerated.
        from django.http import Http404
        raise Http404("Order not found")
    return render(request, 'pages/order_success.html', {'order': order})


@staff_member_required
def order_success_preview(request):
    """
    Preview for order success page. Staff-only: it used to publicly render
    the LAST real order with the customer's PII (W1-2 / CRO-044).
    """
    try:
        last_order = Order.objects.last()
    except Exception:
        last_order = None

    return render(request, 'pages/order_success.html', {'order': last_order})


def order_failed(request):
    return render(request, 'pages/order_failed.html')


@login_required
@require_POST
def update_payment_method(request):
    """
    W1-6 (Находка 3): AJAX-смена метода оплаты заказа из кабинета.
    Раньше — заглушка (redirect вместо JSON), фронт my_orders.html молча
    ломался. Восстановлено из views.py.backup:2679 + проверка владельца.
    """
    order_id = request.POST.get('order_id')
    payment_method = request.POST.get('payment_method')

    if not order_id or not payment_method:
        return JsonResponse({'success': False, 'error': _('Відсутні необхідні дані')}, status=400)

    # Фронт my_orders.html шлёт legacy-значения 'full'/'partial' —
    # маппим на канонические pay_type.
    pay_type_map = {
        'full': 'online_full',
        'partial': 'prepay_200',
        'online_full': 'online_full',
        'prepay_200': 'prepay_200',
    }
    canonical_pay_type = pay_type_map.get(payment_method)
    if not canonical_pay_type:
        return JsonResponse({'success': False, 'error': _('Невірний метод оплати')}, status=400)

    try:
        # Проверка владельца: заказ ищется строго по user=request.user
        order = Order.objects.get(id=order_id, user=request.user)
    except (Order.DoesNotExist, ValueError):
        return JsonResponse({'success': False, 'error': _('Замовлення не знайдено')}, status=404)

    # Оплаченный/находящийся на проверке заказ менять нельзя
    if order.payment_status in ('paid', 'prepaid', 'checking'):
        return JsonResponse({
            'success': False,
            'error': _('Спосіб оплати не можна змінити після оплати або під час перевірки.')
        }, status=409)

    order.pay_type = canonical_pay_type
    order.save(update_fields=['pay_type'])

    method_display = (
        _('Повна передоплата') if canonical_pay_type == 'online_full' else _('Часткова передоплата')
    )
    return JsonResponse({
        'success': True,
        'payment_method': payment_method,
        'method_display': method_display,
    })


PAYMENT_SCREENSHOT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


@login_required
@require_POST
def confirm_payment(request):
    """
    W1-6 (Находка 3): AJAX-загрузка скриншота оплаты из кабинета.
    Восстановлено из views.py.backup:3831 + проверка владельца + валидация
    файла (тип изображения через ImageField + лимит размера).
    """
    order_id = request.POST.get('order_id')
    payment_screenshot = request.FILES.get('payment_screenshot')

    if not order_id:
        return JsonResponse({'success': False, 'error': _('Відсутній ID замовлення')}, status=400)

    if not payment_screenshot:
        return JsonResponse({'success': False, 'error': _('Будь ласка, заван��ажте скріншот оплати')}, status=400)

    try:
        order = Order.objects.get(id=order_id, user=request.user)
    except (Order.DoesNotExist, ValueError):
        return JsonResponse({'success': False, 'error': _('Замовлення не знайдено')}, status=404)

    if order.payment_status == 'paid':
        return JsonResponse({'success': False, 'error': _('Замовлення вже оплачено.')}, status=409)

    # Валидация файла: реальное изображение + разумный размер
    from django import forms as dj_forms
    from django.core.exceptions import ValidationError as DjValidationError
    if getattr(payment_screenshot, 'size', 0) > PAYMENT_SCREENSHOT_MAX_BYTES:
        return JsonResponse({'success': False, 'error': _('Файл завеликий. Максимум 10 МБ.')}, status=400)
    try:
        dj_forms.ImageField().clean(payment_screenshot)
    except DjValidationError:
        return JsonResponse({'success': False, 'error': _('Файл не є зображенням.')}, status=400)

    order.payment_screenshot = payment_screenshot
    order.payment_status = 'checking'
    order.save(update_fields=['payment_screenshot', 'payment_status'])

    return JsonResponse({
        'success': True,
        'message': _('Скріншот оплати успішно завантажено')
    })


def calculate_shipping(request):
    return JsonResponse({'price': 0})  # Stub


def handle_payment(request):
    return redirect('cart')
