"""Staff shipping agreement/execution; consent is always the native customer action."""
import json
import uuid

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from management.bot_access import (OPERATE_IG_BOT_PERMISSION,
    VIEW_IG_CONVERSATION_PII_PERMISSION, has_all_bot_capabilities)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_field")
        result[key] = value
    return result


def _projection(client):
    from management.services.ig_split_shipping import shipping_projection
    from management.services.ig_split_execution import execution_projection
    result = shipping_projection(client)
    result.update(execution_projection(client.pk))
    return result


@login_required(login_url="management_login")
@require_http_methods(["GET", "POST"])
@never_cache
@csrf_protect
def bot_split_shipping_api(request, client_id):
    from management.models import IgClient, IgSplitShippingChoice, IgSplitParcel
    capabilities = [VIEW_IG_CONVERSATION_PII_PERMISSION]
    if request.method == "POST":
        capabilities.append(OPERATE_IG_BOT_PERMISSION)
    if not has_all_bot_capabilities(request.user, *capabilities):
        return JsonResponse({"success": False, "code": "shipping_permission_denied"}, status=403)
    client = IgClient.objects.filter(pk=client_id).first()
    if not client:
        return JsonResponse({"success": False, "code": "client_missing"}, status=404)
    if client.privacy_erasure_started_at:
        return JsonResponse({"success": False, "code": "client_erasing"}, status=410)
    if request.method == "GET":
        return JsonResponse({"success": True, "shipping": _projection(client)})
    try:
        if len(request.body) > 32 * 1024:
            raise ValueError("body_bound")
        payload = json.loads(request.body, object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_constant")))
        if not isinstance(payload, dict):
            raise ValueError("invalid_body")
        action = payload.get("action")
        schemas = {
            "issue_offer": {"action", "expected_decision_digest", "operation_id", "options"},
            "bind_order": {"action", "choice_id", "order_id", "operation_id"},
            "dispatch": {"action", "parcel_id", "operation_id", "tracking_number", "customer_delivery_amount", "cod_amount"},
        }
        if action not in schemas or set(payload) != schemas[action]:
            raise ValueError("invalid_fields")
        operation_id = uuid.UUID(payload["operation_id"])
        if action == "issue_offer":
            from management.services.ig_split_shipping import issue_offer
            offer = issue_offer(client_id, actor=request.user,
                expected_decision_digest=payload["expected_decision_digest"],
                operation_id=operation_id, options=payload["options"])
            result = {"ready": True, "offer_id": str(offer.pk)}
        elif action == "bind_order":
            from management.services.ig_split_execution import bind_split_plan
            choice_id = uuid.UUID(payload["choice_id"])
            if not IgSplitShippingChoice.objects.filter(pk=choice_id, client_id=client_id).exists():
                return JsonResponse({"success": False, "code": "choice_missing"}, status=404)
            if type(payload["order_id"]) is not int or payload["order_id"] <= 0:
                raise ValueError("order_invalid")
            result = bind_split_plan(choice_id, payload["order_id"], actor=request.user,
                operation_id=operation_id)
        else:
            from management.services.ig_split_execution import record_split_dispatch
            parcel_id = uuid.UUID(payload["parcel_id"])
            if not IgSplitParcel.objects.filter(pk=parcel_id, plan__client_id=client_id).exists():
                return JsonResponse({"success": False, "code": "parcel_missing"}, status=404)
            result = record_split_dispatch(parcel_id, actor=request.user, operation_id=operation_id,
                tracking_number=payload["tracking_number"],
                customer_delivery_amount=payload["customer_delivery_amount"], cod_amount=payload["cod_amount"])
        ready = result.get("ready") is True
        return JsonResponse({"success": ready, **result,
            "shipping": _projection(client) if ready else None}, status=200 if ready else 409)
    except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        reason = getattr(exc, "reason", "shipping_request_invalid")
        return JsonResponse({"success": False, "code": reason}, status=400)
