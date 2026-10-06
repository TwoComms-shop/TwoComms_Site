"""Actor-owned private manager drafts/notes and explicit saved-draft submission.

Reads never initialize settings or call document/generation/send producers.
Only explicit draft submission enters the existing committed command dispatcher.
"""
import json
import re
import uuid

from django.contrib.auth.decorators import login_required
from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from management.bot_access import OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION, has_all_bot_capabilities


_INTEGER = re.compile(r"[1-9][0-9]{0,18}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")


class _RequestRejected(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def _number(value):
    if type(value) is int and 0 < value <= 2**63 - 1:
        return value
    if isinstance(value, str) and _INTEGER.fullmatch(value) and int(value) <= 2**63 - 1:
        return int(value)
    raise _RequestRejected("invalid_identity")


def _uuid(value):
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise _RequestRejected("invalid_uuid") from None
    if str(parsed) != str(value) or parsed.int == 0:
        raise _RequestRejected("invalid_uuid")
    return parsed


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _RequestRejected("duplicate_field")
        result[key] = value
    return result


def _body(request, allowed):
    if request.content_type == "application/json":
        try:
            value = json.loads(request.body.decode("utf-8"), object_pairs_hook=_json_pairs)
        except (UnicodeError, ValueError):
            raise _RequestRejected("invalid_request") from None
        if not isinstance(value, dict):
            raise _RequestRejected("invalid_request")
    else:
        if any(len(request.POST.getlist(key)) != 1 for key in request.POST):
            raise _RequestRejected("duplicate_field")
        value = request.POST.dict()
        value.pop("csrfmiddlewaretoken", None)
    if set(value) - set(allowed):
        raise _RequestRejected("unexpected_field")
    return value


def _cas(body):
    version = _number(body.get("expected_version"))
    digest = body.get("expected_hash")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise _RequestRejected("invalid_hash")
    return version, digest


def _authorised_owner(request, client_id):
    if not has_all_bot_capabilities(request.user, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
        raise _RequestRejected("actor_not_authorized", 403)
    from management.models import IgClient
    client = IgClient.objects.filter(pk=_number(client_id), hidden_at__isnull=True,
        privacy_erasure_started_at__isnull=True).first()
    if client is None:
        raise _RequestRejected("client_unavailable", 404)
    return client


def _document(request, client_id, document_id):
    from management.ig_human_reply_models import HumanReplyPrivateDocument
    document = HumanReplyPrivateDocument.objects.filter(document_id=_uuid(document_id),
        client_id=client_id, actor_id=request.user.pk, client__hidden_at__isnull=True,
        client__privacy_erasure_started_at__isnull=True).first()
    if document is None:
        raise _RequestRejected("private_document_missing", 404)
    return document


def _payload(document, *, include_text=False):
    value = {
        "document_id": str(document.document_id), "kind": document.kind, "state": document.state,
        "version": document.version, "text_hash": document.text_hash,
        "context_message_id": document.context_message_id,
        "created_at": document.created_at.isoformat(), "updated_at": document.updated_at.isoformat(),
    }
    if include_text:
        value["text"] = document.text
    return value


def _error(exc):
    if isinstance(exc, _RequestRejected):
        code, status = exc.code, exc.status
    elif isinstance(exc, DatabaseError):
        code, status = "human_document_unavailable", 503
    else:
        code = getattr(exc, "code", "invalid_request")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            code = "invalid_request"
        if code == "actor_not_authorized":
            status = 403
        elif code in {"private_document_missing", "private_document_owned_by_other_actor", "client_not_found", "client_unavailable"}:
            status = 404
        elif code in {"settings_unavailable", "takeover_boundary_busy", "takeover_cleanup_failed", "takeover_transition_failed", "caller_transaction_active", "human_draft_requires_commit"}:
            status = 503
        elif code in {"private_document_stale", "private_document_conflict", "private_document_closed", "private_context_changed",
            "private_command_binding_changed", "permission_epoch_changed", "context_changed", "context_namespace_changed", "context_before_reset",
            "newer_inbound", "reply_window_closed", "operation_conflict", "competing_command", "command_owned_by_other_actor", "private_note_not_sendable", "provider_namespace_changed"}:
            status = 409
        else:
            status = 400
    return JsonResponse({"success": False, "code": code}, status=status)


@login_required(login_url="management_login")
@require_GET
@never_cache
def bot_human_documents_api(request, client_id):
    from management.ig_human_reply_models import HumanReplyPrivateDocument
    try:
        client = _authorised_owner(request, client_id)
        if set(request.GET) - {"kind", "state", "limit", "before_id"} or any(len(request.GET.getlist(key)) != 1 for key in request.GET):
            raise _RequestRejected("invalid_filter")
        kind, state = request.GET.get("kind"), request.GET.get("state", "open")
        if kind is not None and kind not in HumanReplyPrivateDocument.Kind.values:
            raise _RequestRejected("invalid_filter")
        if state not in {*HumanReplyPrivateDocument.State.values, "all"}:
            raise _RequestRejected("invalid_filter")
        limit = _number(request.GET.get("limit", "20"))
        if limit > 50:
            raise _RequestRejected("invalid_filter")
        query = HumanReplyPrivateDocument.objects.filter(client_id=client.pk, actor_id=request.user.pk,
            client__hidden_at__isnull=True, client__privacy_erasure_started_at__isnull=True)
        if kind:
            query = query.filter(kind=kind)
        if state != "all":
            query = query.filter(state=state)
        if "before_id" in request.GET:
            query = query.filter(pk__lt=_number(request.GET["before_id"]))
        rows = list(query.order_by("-pk")[:limit + 1])
        return JsonResponse({"success": True, "documents": [_payload(row) for row in rows[:limit]],
            "next_before_id": rows[limit - 1].pk if len(rows) > limit else None, "bounded": True})
    except (_RequestRejected, DatabaseError) as exc:
        return _error(exc)
    except Exception:
        return _error(_RequestRejected("human_document_unavailable", 503))


@login_required(login_url="management_login")
@require_GET
@never_cache
def bot_human_document_detail_api(request, client_id, document_id):
    try:
        client = _authorised_owner(request, client_id)
        return JsonResponse({"success": True, "document": _payload(_document(request, client.pk, document_id), include_text=True)})
    except (_RequestRejected, DatabaseError) as exc:
        return _error(exc)
    except Exception:
        return _error(_RequestRejected("human_document_unavailable", 503))


@login_required(login_url="management_login")
@require_POST
@never_cache
def bot_human_document_create_api(request, client_id):
    from management.services.ig_human_reply import HumanReplyRejected
    try:
        client = _authorised_owner(request, client_id)
        body = _body(request, {"document_id", "kind", "text", "context_message_id"})
        document_id = _uuid(body.get("document_id"))
        context_id = _number(body.get("context_message_id"))
        from management.models import InstagramBotSettings
        from management.services.instagram_bot import ingress_provider_namespace
        from management.services.ig_human_reply_delivery import create_private_document
        settings_row = InstagramBotSettings.objects.order_by("pk").first()
        if settings_row is None:
            raise _RequestRejected("settings_unavailable", 503)
        document = create_private_document(client.pk, actor=request.user, kind=body.get("kind"),
            text=body.get("text"), context_message_id=context_id,
            provider_namespace=ingress_provider_namespace(settings_row), expected_permission_epoch=client.reply_permission_epoch,
            document_id=document_id)
        return JsonResponse({"success": True, "document": _payload(document, include_text=True)})
    except (_RequestRejected, HumanReplyRejected, DatabaseError) as exc:
        return _error(exc)
    except Exception:
        return _error(_RequestRejected("human_document_unavailable", 503))


@login_required(login_url="management_login")
@require_POST
@never_cache
def bot_human_document_update_api(request, client_id, document_id):
    from management.services.ig_human_reply import HumanReplyRejected
    try:
        client = _authorised_owner(request, client_id)
        document = _document(request, client.pk, document_id)
        body = _body(request, {"expected_version", "expected_hash", "text", "archive"})
        version, digest = _cas(body)
        archive = body.get("archive", False)
        if archive not in (True, False, "1", "0") or type(archive) is int:
            raise _RequestRejected("invalid_request")
        archive = archive is True or archive == "1"
        if (archive and "text" in body) or (not archive and "text" not in body):
            raise _RequestRejected("invalid_request")
        from management.services.ig_human_reply_delivery import update_private_document
        document = update_private_document(document.document_id, actor=request.user,
            expected_version=version, expected_hash=digest, text=body.get("text"), archive=archive)
        return JsonResponse({"success": True, "document": _payload(document, include_text=True)})
    except (_RequestRejected, HumanReplyRejected, DatabaseError) as exc:
        return _error(exc)
    except Exception:
        return _error(_RequestRejected("human_document_unavailable", 503))


@login_required(login_url="management_login")
@require_POST
@never_cache
def bot_human_document_submit_api(request, client_id, document_id):
    from management.services.ig_human_reply import HumanReplyRejected
    try:
        client = _authorised_owner(request, client_id)
        document = _document(request, client.pk, document_id)
        body = _body(request, {"operation_id", "expected_version", "expected_hash"})
        version, digest = _cas(body)
        operation_id = _uuid(body.get("operation_id"))
        if connection.in_atomic_block:
            raise _RequestRejected("caller_transaction_active", 503)
        from management.services.ig_human_reply import create_human_reply_command_from_draft, dispatch_human_reply_command
        result = create_human_reply_command_from_draft(client.pk, actor=request.user, document_id=document.document_id,
            operation_id=operation_id, expected_version=version, expected_hash=digest)
        # Even same-operation retries recover never-started work. Existing
        # immutable part receipts/claims guard started, SENT and UNKNOWN replay.
        try:
            command = dispatch_human_reply_command(result.command.pk)
        except Exception:
            # Consumption already committed. Preserve its operation identity so
            # the caller can recover that command rather than create another.
            return JsonResponse({"success": False, "code": "human_dispatch_unavailable", "accepted": True,
                "command_id": result.command.pk, "operation_id": str(result.command.operation_id)}, status=503)
        return JsonResponse({"success": command.state == command.State.SENT, "accepted": True,
            "command_id": command.pk, "operation_id": str(command.operation_id), "state": command.state,
            "failure_code": command.failure_code, "idempotent": result.idempotent},
            status=200 if command.state == command.State.SENT else 409)
    except (_RequestRejected, HumanReplyRejected, DatabaseError) as exc:
        return _error(exc)
    except Exception:
        return _error(_RequestRejected("human_document_unavailable", 503))
