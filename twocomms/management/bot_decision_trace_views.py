"""Authorized GET-only inspection of existing revision evidence."""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from management.bot_access import (
    OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION,
    has_all_bot_capabilities,
)


def _identity(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def _capabilities(request):
    return has_all_bot_capabilities(request.user, OPERATE_IG_BOT_PERMISSION,
                                    VIEW_IG_CONVERSATION_PII_PERMISSION)


def _denied():
    return JsonResponse({"success": False, "error": "Недостатньо прав для цієї дії."}, status=403)


def _invalid(reason="invalid_identity"):
    return JsonResponse({"success": False, "status": "unavailable", "reason": reason}, status=400)


def _query_integer(request, name, *, default=None, maximum=2**63 - 1):
    values = request.GET.getlist(name)
    if not values:
        return default
    if (len(values) != 1 or not values[0].isascii() or not values[0].isdecimal()
        or len(values[0]) > 19 or values[0].startswith("0")):
        raise ValueError("invalid_pagination")
    value = int(values[0])
    if not 0 < value <= maximum:
        raise ValueError("invalid_pagination")
    return value


def _response(payload, identity):
    available = payload.get("status") == "available"
    status = 200 if available else 404 if payload.get("reason") in {
        "owner_unavailable", "revision_missing", "privacy_erasure",
    } else 409
    return JsonResponse({"success": available, "identity": identity, **payload}, status=status)


@login_required(login_url="management_login")
@require_GET
@never_cache
def bot_decision_trace_index_api(request, client_id):
    """Revision-owned index includes static/no-reply; no latest graph fallback."""
    if not _capabilities(request):
        return _denied()
    if not _identity(client_id):
        return _invalid()
    try:
        before = _query_integer(request, "before_revision_id")
        limit = _query_integer(request, "limit", default=10, maximum=25)
    except ValueError:
        return _invalid("invalid_pagination")
    from management.services.ig_decision_trace_read_model import read_revision_decision_trace_index
    return _response(read_revision_decision_trace_index(
        client_id=client_id, before_revision_id=before, limit=limit,
    ), {"client_id": client_id})


@login_required(login_url="management_login")
@require_GET
@never_cache
def bot_decision_trace_api(request, client_id, revision_id):
    """Exact captured owner/revision; query flags cannot enable private prose."""
    if not _capabilities(request):
        return _denied()
    if not all(_identity(value) for value in (client_id, revision_id)):
        return _invalid()
    from management.services.ig_decision_trace_read_model import read_revision_decision_trace
    return _response(read_revision_decision_trace(client_id=client_id, revision_id=revision_id),
                     {"client_id": client_id, "revision_id": revision_id})
