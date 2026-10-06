"""Current admin state API; historical metadata is not reconstructed policy."""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from management.bot_access import VIEW_IG_CONVERSATION_PII_PERMISSION, has_all_bot_capabilities
from management.services.ig_admin_state_capture import current_admin_state, historical_admin_state, valid_identifier


@login_required(login_url="management_login")
@require_GET
@never_cache
def bot_client_state_api(request, client_id):
    if not has_all_bot_capabilities(request.user, VIEW_IG_CONVERSATION_PII_PERMISSION):
        return JsonResponse({"success": False, "code": "state_permission_denied"}, status=403)
    if not valid_identifier(client_id):
        return JsonResponse({"success": False, "code": "state_request_invalid"}, status=400)
    raw_revision = request.GET.get("revision_id")
    raw_selection = request.GET.get("expected_selection_revision")
    try:
        if raw_revision is not None:
            revision_id = int(raw_revision)
            if not valid_identifier(revision_id) or raw_selection is not None:
                raise ValueError()
            result = historical_admin_state(client_id, revision_id)
        else:
            expected = int(raw_selection) if raw_selection is not None else None
            if expected is not None and not valid_identifier(expected):
                raise ValueError()
            result = current_admin_state(client_id, expected_selection_revision=expected)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "code": "state_request_invalid"}, status=400)
    http_status = 409 if result.status == "conflict" else 200
    if result.reason in {"client_missing", "revision_missing"}:
        http_status = 404
    elif result.reason == "client_erasing":
        http_status = 410
    elif result.reason in {"client_id_invalid", "revision_id_invalid", "selection_revision_invalid"}:
        http_status = 400
    return JsonResponse({"success": http_status == 200, **result.as_dict()}, status=http_status)
