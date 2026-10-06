"""Protected, bounded JSON/aggregate CSV observations. No report persistence."""
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from management.bot_access import OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION, has_all_bot_capabilities
from management.bot_decision_trace_views import _query_integer


def _authorized(request):
    return has_all_bot_capabilities(request.user, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)


def _page(request):
    from management.services.ig_quality_observations import build_quality_observation_page
    limit = _query_integer(request, "limit", default=5, maximum=5)
    cursor = _query_integer(request, "before_revision_id")
    if request.GET.getlist("since") or request.GET.getlist("until"):
        if "days" in request.GET or len(request.GET.getlist("since")) != 1 or len(request.GET.getlist("until")) != 1:
            raise ValueError("window_invalid")
        try:
            since, until = (datetime.fromisoformat(request.GET[key]) for key in ("since", "until"))
        except (TypeError, ValueError):
            raise ValueError("window_invalid") from None
    else:
        days = _query_integer(request, "days", default=7, maximum=31)
        until = timezone.now()
        since = until - timedelta(days=days)
    return build_quality_observation_page(since=since, until=until, before_revision_id=cursor, limit=limit)


def _error(reason, status=400):
    return JsonResponse({"success": False, "status": "unavailable", "reason": reason}, status=status)


@login_required(login_url="management_login")
@require_GET
@never_cache
def bot_quality_observations_api(request):
    if not _authorized(request):
        return JsonResponse({"success": False, "error": "Недостатньо прав для цієї дії."}, status=403)
    try:
        payload = _page(request)
    except ValueError as exc:
        # All outward errors are finite; never return parser text or values.
        code = str(exc) if str(exc) in {"window_invalid", "page_limit_invalid", "cursor_invalid", "invalid_pagination"} else "invalid_query"
        return _error(code)
    available = payload.get("status") == "available"
    return JsonResponse({"success": available, **payload}, status=200 if available else 409)


@login_required(login_url="management_login")
@require_GET
@never_cache
def bot_quality_observations_export_api(request):
    if not _authorized(request):
        return JsonResponse({"success": False, "error": "Недостатньо прав для цієї дії."}, status=403)
    try:
        payload = _page(request)
    except ValueError:
        return _error("invalid_query")
    if payload.get("status") != "available":
        return _error("read_scope_changed", status=409)
    from management.services.ig_quality_observations import quality_observation_csv
    response = HttpResponse(quality_observation_csv(payload), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="ig-quality-observations.csv"'
    response["X-Content-Type-Options"] = "nosniff"
    return response
