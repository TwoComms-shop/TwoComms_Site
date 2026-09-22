"""Authorized operator preview; images need no human approval for bot analysis."""
from __future__ import annotations
import hashlib
from collections.abc import Mapping
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from management.bot_access import is_meta_bot_reviewer
from management.models import AdminAuditLog, IgClient, InstagramBotMessage
from management.services.ig_media_manifest import MediaManifestError, normalize_attachment_media
from management.services.ig_private_media import acquire_blob_use, private_media_storage, release_blob_use

VIEW_PII_PERMISSION = "management.view_ig_conversation_pii"
PRIVATE_REVIEW_MAX_BYTES = 6 * 1024 * 1024
_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"})
_DELETED_STATES = frozenset({"delete_pending", "deleting", "deleted"})


def _unavailable_response(*, json_response=False):
    response = HttpResponse("Зображення недоступне.", status=404)
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["X-Content-Type-Options"] = "nosniff"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


class PrivateMediaUnavailable(LookupError):
    pass


def _active_superuser(user) -> bool:
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "is_superuser", False)
    )


def _can_preview(user) -> bool:
    if is_meta_bot_reviewer(user):
        return False
    if _active_superuser(user):
        return True
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and user.has_perm(VIEW_PII_PERMISSION)
    )


def _safe_part(row, client, source_part_id: str, *, use_token: str) -> dict:
    if (
        row is None
        or client is None
        or row.client_id != client.pk
        or row.role != InstagramBotMessage.Role.USER
        or row.sender_id != client.igsid
        or client.privacy_erasure_started_at is not None
        or row.private_media_state in _DELETED_STATES
        or row.private_media_state != InstagramBotMessage.PrivateMediaState.ACTIVE
        or row.private_media_use_token != use_token
        or not row.private_media_use_until
        or row.private_media_use_until <= timezone.now()
    ):
        raise PrivateMediaUnavailable
    try:
        media = normalize_attachment_media(row.attachment_media or [], message_scope=row.pk)
    except MediaManifestError as exc:
        raise PrivateMediaUnavailable from exc
    matches = [
        dict(part)
        for part in media
        if isinstance(part, Mapping)
        and str(part.get("source_part_id") or "") == str(source_part_id or "")
    ]
    if len(matches) != 1:
        raise PrivateMediaUnavailable
    part = matches[0]
    mime = str(part.get("mime") or "").split(";", 1)[0].strip().lower()
    if (
        part.get("status") != "owned"
        or part.get("private_storage") is not True
        or not str(part.get("storage_name") or "").strip()
        or mime not in _IMAGE_MIMES
    ):
        raise PrivateMediaUnavailable
    part["mime"] = mime
    return part


def _read_current_bytes(part: Mapping[str, object]) -> tuple[bytes, str]:
    storage_name = str(part.get("storage_name") or "").strip()
    try:
        with private_media_storage().open(storage_name, "rb") as handle:
            raw = handle.read(PRIVATE_REVIEW_MAX_BYTES + 1)
    except Exception as exc:
        raise PrivateMediaUnavailable from exc
    if not raw or len(raw) > PRIVATE_REVIEW_MAX_BYTES:
        raise PrivateMediaUnavailable
    return raw, hashlib.sha256(raw).hexdigest()


def _identity_snapshot(message_id: int) -> int:
    client_id = (
        InstagramBotMessage.objects.filter(pk=message_id)
        .values_list("client_id", flat=True)
        .first()
    )
    if not client_id:
        raise PrivateMediaUnavailable
    return int(client_id)


@login_required(login_url="management_login")
@require_GET
def private_media_preview(request, message_id: int, source_part_id: str):
    """Buffer one leased private image and return it without a public URL."""
    if not _can_preview(request.user):
        return HttpResponse(status=403)
    token = ""
    try:
        client_id = _identity_snapshot(message_id)
        token = acquire_blob_use(message_id, seconds=60)
        if not token:
            raise PrivateMediaUnavailable
        with transaction.atomic():
            client = IgClient.objects.select_for_update().filter(pk=client_id).first()
            row = (
                InstagramBotMessage.objects.select_for_update()
                .filter(pk=message_id, client_id=client_id)
                .first()
            )
            part = _safe_part(row, client, source_part_id, use_token=token)
            raw, actual_hash = _read_current_bytes(part)
            if str(part.get("content_hash") or "").lower() != actual_hash:
                raise PrivateMediaUnavailable
            mime = part["mime"]
            AdminAuditLog.objects.create(
                actor=request.user, actor_role="ig_media_viewer",
                action="ig_private_media.preview", entity_type="InstagramBotMessage",
                entity_id=str(row.pk), after={"source_part_id": source_part_id},
                reason="authorized_operator_preview",
            )
        response = HttpResponse(raw, content_type=mime)
        response["Content-Disposition"] = "inline; filename=private-media"
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        response["X-Content-Type-Options"] = "nosniff"
        response["X-Frame-Options"] = "DENY"
        response["Referrer-Policy"] = "no-referrer"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response
    except PrivateMediaUnavailable:
        return _unavailable_response(json_response=False)
    finally:
        if token:
            release_blob_use(message_id, token)
