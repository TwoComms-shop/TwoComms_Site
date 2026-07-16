"""Capture checkout-form input for abandoned-cart recovery.

The cart/checkout page posts the visitor's contact fields here (debounced
while typing + on page hide). We upsert one row per session so we can
reach out later if the order was never completed.
"""

import json

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from orders.models import CheckoutCapture, Order
from orders.nova_poshta_documents import normalize_checkout_phone

from ..services.checkout_capture import mark_checkout_capture_converted
from .utils import calculate_cart_total, get_validated_cart_from_session

_MAX_LEN = {'full_name': 255, 'phone': 32, 'email': 254}


def _clean(value, field):
    if not isinstance(value, str):
        return ''
    value = value.strip()
    return value[: _MAX_LEN[field]]


def _validated_email(value):
    email = _clean(value, 'email')
    if not email:
        return ''
    try:
        validate_email(email)
    except ValidationError:
        return ''
    return email


def _completed_order_ids_from_session(session):
    order_ids = []
    last_submit = session.get('last_order_submit')
    candidates = [
        last_submit.get('order_id') if isinstance(last_submit, dict) else None,
        session.get('monobank_pending_order_id'),
    ]
    for candidate in candidates:
        try:
            order_id = int(candidate)
        except (TypeError, ValueError, OverflowError):
            continue
        if order_id > 0 and order_id not in order_ids:
            order_ids.append(order_id)
    return order_ids


def _session_has_completed_order(request, session_key):
    order_ids = _completed_order_ids_from_session(request.session)
    if not order_ids:
        return False
    # Order.session_key is not indexed; constrain by indexed primary keys first.
    return Order.objects.filter(
        pk__in=order_ids,
        session_key=session_key,
    ).exists()


# W3-11 (NEW-510): публичный PII-приёмник — точечный rate-limit.
# 30/m хватает для дебаунс-автосейва формы, но душит скриптовый спам.
@csrf_exempt
@require_POST
@ratelimit(key='user_or_ip', rate='30/m', method='POST', block=False)
def capture_checkout(request):
    if getattr(request, 'limited', False):
        return JsonResponse({'ok': False}, status=429)

    # Same-origin guard: browsers send Sec-Fetch-Site for fetch/beacon.
    sfs = request.headers.get('Sec-Fetch-Site')
    if sfs and sfs not in ('same-origin', 'same-site', 'none'):
        return JsonResponse({'ok': False}, status=403)

    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JsonResponse(
                {'ok': False, 'error': 'invalid_payload'},
                status=400,
            )
        if not isinstance(data, dict):
            return JsonResponse(
                {'ok': False, 'error': 'invalid_payload'},
                status=400,
            )
    else:
        data = request.POST

    full_name = _clean(data.get('full_name'), 'full_name')
    phone = normalize_checkout_phone(_clean(data.get('phone'), 'phone'))
    submitted_email = _validated_email(data.get('email'))
    account_email = ''
    if request.user.is_authenticated:
        account_email = _validated_email(request.user.email)

    email = submitted_email
    account_email_fallback = bool(
        not email and account_email and (full_name or phone)
    )
    if account_email_fallback:
        email = account_email

    if not (phone or email):
        return JsonResponse(
            {'ok': False, 'error': 'contact_required'},
            status=400,
        )

    if not request.session.session_key:
        request.session.save()
    session_key = request.session.session_key

    with transaction.atomic():
        locked = CheckoutCapture.objects.select_for_update()
        capture = locked.filter(session_key=session_key).first()
        if capture and capture.converted:
            return JsonResponse({'ok': True})
        if _session_has_completed_order(request, session_key):
            mark_checkout_capture_converted(session_key)
            return JsonResponse({'ok': True})

        cart = get_validated_cart_from_session(request)
        try:
            total = calculate_cart_total(cart)
        except Exception:
            total = 0

        if capture is None:
            capture, _created = locked.get_or_create(
                session_key=session_key,
                defaults={'cart_snapshot': cart or {}},
            )
            if capture.converted:
                return JsonResponse({'ok': True})

        update_fields = []
        # Never blank out previously captured contact data.
        if full_name:
            capture.full_name = full_name
            update_fields.append('full_name')
        if phone:
            capture.phone = phone
            update_fields.append('phone')
        if email and (submitted_email or not capture.email):
            capture.email = email
            update_fields.append('email')
        if cart:
            capture.cart_snapshot = cart
            capture.cart_total = total
            update_fields.extend(('cart_snapshot', 'cart_total'))
        if request.user.is_authenticated:
            capture.user = request.user
            update_fields.append('user')
        capture.save(update_fields=(*update_fields, 'updated_at'))
    return JsonResponse({'ok': True})
