import hashlib
import json

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from productcolors.models import ProductColorVariant
from storefront.models import Product, RestockSubscription
from storefront.services.restock import create_subscription


RATE_LIMIT = 5
RATE_WINDOW_SECONDS = 15 * 60


def _payload(request):
    try:
        return json.loads((request.body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _ip_hash(request):
    raw = str(request.META.get("REMOTE_ADDR") or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else ""


@require_POST
def restock_subscribe(request):
    data = _payload(request)
    if not isinstance(data, dict):
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    if str(data.get("website") or "").strip():
        return JsonResponse({"ok": True, "accepted": True})

    ip_hash = _ip_hash(request)
    rate_key = f"restock-rate:{ip_hash or 'anonymous'}"
    requests_count = int(cache.get(rate_key, 0) or 0)
    if requests_count >= RATE_LIMIT:
        return JsonResponse({"ok": False, "error": "rate_limited"}, status=429)
    cache.set(rate_key, requests_count + 1, RATE_WINDOW_SECONDS)

    try:
        product_id = int(data.get("product_id"))
        product = Product.objects.get(pk=product_id)
    except (TypeError, ValueError, Product.DoesNotExist):
        return JsonResponse({"ok": False, "error": "product_not_found"}, status=404)

    variant = None
    variant_id = data.get("color_variant_id")
    if variant_id not in (None, "", 0, "0"):
        try:
            variant = ProductColorVariant.objects.get(
                pk=int(variant_id),
                product=product,
            )
        except (TypeError, ValueError, ProductColorVariant.DoesNotExist):
            return JsonResponse({"ok": False, "error": "variant_not_found"}, status=400)

    channel = str(data.get("channel") or "").strip().lower()
    if channel not in RestockSubscription.Channel.values:
        return JsonResponse({"ok": False, "error": "invalid_channel"}, status=400)
    size = str(data.get("size") or "").strip().upper()
    if not size or len(size) > 20:
        return JsonResponse({"ok": False, "error": "invalid_size"}, status=400)

    if not request.session.session_key:
        request.session.create()
    try:
        subscription, created = create_subscription(
            product=product,
            variant=variant,
            size=size,
            option_values=data.get("option_values") or {},
            channel=channel,
            name=data.get("name") or "",
            contact=data.get("contact") or "",
            user=request.user,
            browser_key=request.session.session_key or "",
            ip_hash=ip_hash,
            user_agent=str(request.META.get("HTTP_USER_AGENT") or ""),
        )
    except ValidationError as exc:
        message = exc.messages[0] if exc.messages else "invalid_request"
        status = 409 if message == "SIZE_ALREADY_AVAILABLE" else 400
        return JsonResponse({"ok": False, "error": message}, status=status)

    return JsonResponse(
        {
            "ok": True,
            "created": created,
            "subscription_id": subscription.pk,
            "status": subscription.status,
        },
        status=201 if created else 200,
    )
