"""Private read model for explicit, exact-item replenishment agreements."""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from management.bot_access import (
    VIEW_IG_CONVERSATION_PII_PERMISSION, has_all_bot_capabilities,
)


@login_required(login_url="management_login")
@require_GET
@never_cache
def bot_restock_waiting_list_api(request, client_id):
    from management.models import IgClient
    from management.services.ig_restock_waiting_list import waiting_list_projection

    if not has_all_bot_capabilities(request.user, VIEW_IG_CONVERSATION_PII_PERMISSION):
        return JsonResponse({"success": False, "code": "restock_permission_denied"}, status=403)
    owner = IgClient.objects.filter(pk=client_id).only("pk", "privacy_erasure_started_at").first()
    if owner is None:
        return JsonResponse({"success": False, "code": "client_missing"}, status=404)
    if owner.privacy_erasure_started_at:
        return JsonResponse({"success": False, "code": "client_erasing"}, status=410)
    return JsonResponse({"success": True, "waiting_list": waiting_list_projection(client_id)})
