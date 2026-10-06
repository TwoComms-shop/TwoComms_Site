"""Authenticated, bounded inspection of one captured customer request.

This endpoint reads existing records only. It never assembles current customer
context, initializes settings, schedules analysis, or contacts a provider.
"""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from management.bot_access import (
    OPERATE_IG_BOT_PERMISSION,
    VIEW_IG_CONVERSATION_PII_PERMISSION,
    has_all_bot_capabilities,
)


def _unavailable(reason):
    return {
        "mode": "actual_request", "reconstruction": "not_reconstructable",
        "reason": reason, "manifest": {}, "attempts": [], "actual_model": "",
    }


@login_required(login_url="management_login")
@require_GET
@never_cache
def bot_request_revision_index_api(request, client_id):
    """List bounded captured revision references; never infer another request."""
    if not has_all_bot_capabilities(
        request.user, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION,
    ):
        return JsonResponse({"success": False, "error": "Недостатньо прав для цієї дії."}, status=403)
    if type(client_id) is not int or not 0 < client_id <= 2**63 - 1:
        return JsonResponse({"success": False, "reason": "invalid_identity", "revisions": []}, status=400)
    from django.db.models import F
    from management.models import GeminiRequest, IgClient
    owner = IgClient.objects.only("pk", "privacy_erasure_started_at").filter(pk=client_id).first()
    if owner is None or owner.privacy_erasure_started_at:
        return JsonResponse({"success": False, "reason": "owner_unavailable", "revisions": []}, status=404)
    rows = list(GeminiRequest.objects.filter(
        client_id=client_id, lane="live", source_execution_key=F("logical_turn_id"),
        logical_turn_id__startswith="ig-revision:",
    ).values("logical_turn_id", "created_at", "terminal_resolution").order_by("-pk")[:11])
    revisions = []
    for row in rows[:10]:
        suffix = row["logical_turn_id"].removeprefix("ig-revision:")
        if not suffix.isascii() or not suffix.isdecimal() or len(suffix) > 19:
            continue
        identity = int(suffix)
        if not 0 < identity <= 2**63 - 1 or suffix != str(identity):
            continue
        revisions.append({"revision_id": identity, "created_at": row["created_at"].isoformat(),
                          "terminal_resolution": row["terminal_resolution"]})
    return JsonResponse({"success": True, "mode": "actual_request_index", "revisions": revisions,
                         "coverage_complete": len(rows) <= 10, "limit": 10})


@login_required(login_url="management_login")
@require_GET
@never_cache
def bot_request_preview_api(request, client_id, revision_id):
    """Return captured evidence for exactly the requested client/revision."""
    if not has_all_bot_capabilities(
        request.user, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION,
    ):
        return JsonResponse({"success": False, "error": "Недостатньо прав для цієї дії."}, status=403)
    if any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in (client_id, revision_id)):
        return JsonResponse({"success": False, **_unavailable("invalid_identity")}, status=400)

    from management.models import GeminiRequest
    from management.services.ig_request_manifest import actual_request_preview

    execution = f"ig-revision:{revision_id}"
    # No latest/current-client fallback. Two rows suffice to detect ambiguity;
    # accepting either one would conceal a broken captured request identity.
    graphs = list(GeminiRequest.objects.filter(
        client_id=client_id, lane="live", logical_turn_id=execution,
        source_execution_key=execution,
    ).only("pk", "request_id").order_by("pk")[:2])
    if not graphs:
        preview, status = _unavailable("request_missing"), 404
    elif len(graphs) != 1:
        preview, status = _unavailable("request_ambiguous"), 409
    else:
        # Query parameters cannot grant digest access. This endpoint's default
        # response is suitable for the existing customer diagnostics UI.
        preview = actual_request_preview(
            graphs[0], owner_id=client_id, revision_id=revision_id,
            allow_protected_context=False,
        )
        status = 200
    return JsonResponse({
        "success": status == 200, "identity": {"client_id": client_id, "revision_id": revision_id},
        **preview,
    }, status=status)
