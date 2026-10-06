"""Protected, explicit commerce-scope read; no generation or materialization."""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from management.bot_access import OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION, has_all_bot_capabilities
from management.services.ig_admin_state_capture import valid_identifier
from management.services.ig_commerce_scope_read_model import capture_commerce_scope


@login_required(login_url="management_login")
@require_GET
@never_cache
def bot_client_commerce_scope_api(request, client_id):
    if not has_all_bot_capabilities(request.user, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
        return JsonResponse({"success": False, "code": "commerce_scope_permission_denied"}, status=403)
    try:
        if not valid_identifier(client_id) or set(request.GET) - {"order_id"} or len(request.GET.getlist("order_id")) > 1:
            raise ValueError()
        raw = request.GET.get("order_id")
        selected = int(raw) if raw is not None else None
        if selected is not None and not valid_identifier(selected):
            raise ValueError()
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "code": "commerce_scope_request_invalid"}, status=400)
    result = capture_commerce_scope(client_id, selected_order_id=selected)
    reason = result["reason"]
    status = 409 if result["status"] == "conflict" else 200
    if reason == "client_missing": status = 404
    elif reason == "client_erasing": status = 410
    elif reason == "commerce_scope_identity_invalid": status = 400
    return JsonResponse({"success": result["status"] == "captured", **result}, status=status)
