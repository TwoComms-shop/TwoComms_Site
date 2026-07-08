"""Capture checkout-form input for abandoned-cart recovery.

The cart/checkout page posts the visitor's contact fields here (debounced
while typing + on page hide). We upsert one row per session so we can
reach out later if the order was never completed.
"""

import json

from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from orders.models import CheckoutCapture

from .utils import calculate_cart_total, get_cart_from_session

_MAX_LEN = {'full_name': 255, 'phone': 32, 'email': 254}


def _clean(value, field):
    value = (value or '').strip()
    return value[: _MAX_LEN[field]]


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

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST

    full_name = _clean(data.get('full_name'), 'full_name')
    phone = _clean(data.get('phone'), 'phone')
    email = _clean(data.get('email'), 'email')
    if email:
        try:
            validate_email(email)
        except ValidationError:
            email = ''

    cart = get_cart_from_session(request)
    if not (full_name or phone or email) and not cart:
        return JsonResponse({'ok': False})

    if not request.session.session_key:
        request.session.save()
    session_key = request.session.session_key

    try:
        total = calculate_cart_total(cart)
    except Exception:
        total = 0

    capture, _created = CheckoutCapture.objects.get_or_create(
        session_key=session_key,
        defaults={'cart_snapshot': cart or {}},
    )
    # Never blank out previously captured contact data.
    if full_name:
        capture.full_name = full_name
    if phone:
        capture.phone = phone
    if email:
        capture.email = email
    if cart:
        capture.cart_snapshot = cart
        capture.cart_total = total
    if request.user.is_authenticated:
        capture.user = request.user
        if not capture.email and request.user.email:
            capture.email = request.user.email
    capture.converted = False
    capture.save()
    return JsonResponse({'ok': True})
