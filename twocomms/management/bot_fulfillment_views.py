"""Authenticated staff decisions; customer requirements never mint stock."""
import json
import uuid

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from management.bot_access import (
    OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION,
    has_all_bot_capabilities,
)


@login_required(login_url="management_login")
@require_http_methods(["GET", "POST"])
@never_cache
@csrf_protect
def bot_fulfillment_review_api(request, client_id):
    from management.models import IgClient, IgFulfillmentReviewRequest
    from management.services.ig_fulfillment_reviews import (
        fulfillment_review_projection, apply_fulfillment_decision,
    )
    from management.services.ig_fulfillment_stock_sync import projection, apply_stock_sync
    capabilities = [VIEW_IG_CONVERSATION_PII_PERMISSION]
    if request.method == "POST":
        capabilities.append(OPERATE_IG_BOT_PERMISSION)
    if not has_all_bot_capabilities(request.user, *capabilities):
        return JsonResponse({"success": False, "code": "fulfillment_permission_denied"}, status=403)
    owner = IgClient.objects.filter(pk=client_id).only("pk", "privacy_erasure_started_at").first()
    if owner is None:
        return JsonResponse({"success": False, "code": "client_missing"}, status=404)
    if owner.privacy_erasure_started_at:
        return JsonResponse({"success": False, "code": "client_erasing"}, status=410)
    if request.method == "GET":
        try:
            selected = uuid.UUID(request.GET["request_id"]) if request.GET.get("request_id") else None
        except (ValueError, TypeError):
            return JsonResponse({"success": False, "code": "fulfillment_request_invalid"}, status=400)
        review = fulfillment_review_projection(client_id, request_id=selected)
        review["stock_sync"] = projection(client_id, request_id=selected)
        return JsonResponse({"success": True, "review": review})
    try:
        if len(request.body) > 24 * 1024:
            raise ValueError("request too large")
        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate field")
                result[key] = value
            return result
        payload = json.loads(request.body, object_pairs_hook=unique_fields,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid constant")))
        syncing = isinstance(payload, dict) and payload.get("action") == "stock_sync"
        keys = ({"action", "request_id", "expected_decision_digest", "expected_preview_digest", "operation_id", "counts_by_pool"}
            if syncing else {"request_id", "expected_request_digest", "operation_id", "decisions_by_line"})
        if not isinstance(payload, dict) or set(payload) != keys:
            raise ValueError("invalid fields")
        if any(not isinstance(payload[name], str) or len(payload[name]) != 36
               for name in ("request_id", "operation_id")):
            raise ValueError("invalid identity")
        request_id, operation_id = uuid.UUID(payload["request_id"]), uuid.UUID(payload["operation_id"])
        if not IgFulfillmentReviewRequest.objects.filter(pk=request_id, client_id=client_id).exists():
            return JsonResponse({"success": False, "code": "fulfillment_request_missing"}, status=404)
        if syncing:
            result = apply_stock_sync(request_id, actor=request.user,
                expected_decision_digest=payload["expected_decision_digest"],
                expected_preview_digest=payload["expected_preview_digest"],
                operation_id=operation_id, counts_by_pool=payload["counts_by_pool"])
        else:
            result = apply_fulfillment_decision(request_id, actor=request.user,
                expected_request_digest=payload["expected_request_digest"], operation_id=operation_id,
                decisions_by_line=payload["decisions_by_line"])
    except (ValueError, TypeError, UnicodeDecodeError, KeyError):
        return JsonResponse({"success": False, "code": "fulfillment_request_invalid"}, status=400)
    ready = bool(result.get("ready", result.get("ok")))
    return JsonResponse({"success": ready, **result}, status=200 if ready else 409)
