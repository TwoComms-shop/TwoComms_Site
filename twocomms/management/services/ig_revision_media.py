"""Ephemeral media collector for one claimed immutable turn revision."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Mapping

from django.utils import timezone

from management.models import IgClient, IgCustomerTurnRevision, InstagramBotMessage
from management.services.ig_media_manifest import (
    normalize_attachment_media,
    public_media_manifest,
    is_ad_context_part,
)


BINDING_VERSION = "ig-revision-media-v1"


@dataclass(frozen=True)
class CollectedRevisionMediaPart:
    source_message_id: int
    source_part_id: str
    original_index: int
    identity_origin: str
    mime: str
    content_hash: str
    byte_length: int
    inline_index: int
    data: bytes = field(repr=False)
    ad_context_only: bool = False

    def inline_tuple(self) -> tuple[str, bytes]:
        return self.mime, self.data


@dataclass(frozen=True)
class RevisionMediaCollection:
    readiness: str
    reasons: tuple[str, ...] = ()
    parts: tuple[CollectedRevisionMediaPart, ...] = ()
    binding: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)

    @property
    def inline_media(self) -> list[tuple[str, bytes]]:
        return [part.inline_tuple() for part in self.parts]


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _integer(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _serialized_inline_size(mime: str, byte_length: int) -> int:
    # Exact base64 size plus the JSON envelope used by Gemini inline_data.
    base64_chars = 4 * ((max(0, int(byte_length)) + 2) // 3)
    envelope = len(_canonical({
        "inline_data": {"mime_type": str(mime), "data": ""}
    }))
    return base64_chars + envelope


def _empty(readiness: str, reason: str) -> RevisionMediaCollection:
    return RevisionMediaCollection(
        readiness=readiness,
        reasons=(reason,),
        binding={"version": BINDING_VERSION, "items": [], "outcomes": []},
        coverage={
            "total_parts": 0,
            "sealed_owned": 0,
            "admitted": 0,
            "omitted": 0,
            "unavailable": 0,
        },
    )


def _claimed_revision(revision_id: int, revision_token: str):
    revision = (
        IgCustomerTurnRevision.objects.select_related("client")
        .filter(pk=revision_id)
        .first()
    )
    if revision is None:
        return None, "stale_revision", "revision_missing"
    if not revision.snapshot_digest or not revision.bundle_snapshot:
        return revision, "not_sealed", "revision_not_sealed"
    if _digest(revision.bundle_snapshot) != revision.snapshot_digest:
        return revision, "stale_revision", "snapshot_digest_mismatch"
    if revision.state != revision.State.CLAIMED:
        return revision, "not_claimed", "revision_not_claimed"
    if (
        revision.active_slot != 1
        or not revision_token
        or revision.claim_token != revision_token
        or not revision.lease_until
        or revision.lease_until <= timezone.now()
    ):
        return revision, "stale_revision", "revision_claim_stale"
    return revision, "", ""


def _current_owner(revision):
    client = IgClient.objects.filter(pk=revision.client_id).first()
    if client is None:
        return None, "client_missing"
    if (
        client.privacy_erasure_started_at is not None
        or client.privacy_erasure_started_at
        != revision.erasure_started_at_snapshot
    ):
        return None, "client_erasure_active"
    if client.hidden_at is not None or client.is_blocked:
        return None, "client_owner_unavailable"
    return client, ""


def _current_part(message, source_part_id: str):
    try:
        media = normalize_attachment_media(
            message.attachment_media
            if isinstance(message.attachment_media, list) else [],
            message_scope=message.pk,
        )
    except Exception:
        return None
    matches = [
        item for item in media
        if str(item.get("source_part_id") or "") == source_part_id
    ]
    return matches[0] if len(matches) == 1 else None


def collect_revision_media(
    revision_id: int,
    revision_token: str,
) -> RevisionMediaCollection:
    """Collect exact sealed parts without refetching provider URLs or persisting bytes."""
    revision, readiness, reason = _claimed_revision(revision_id, revision_token)
    if readiness:
        return _empty(readiness, reason)
    client, owner_reason = _current_owner(revision)
    if client is None:
        return _empty("invalidated", owner_reason)

    snapshot_sources = revision.bundle_snapshot.get("sources")
    if not isinstance(snapshot_sources, list):
        return _empty("stale_revision", "snapshot_sources_invalid")
    source_rows = {
        row.message_id: row
        for row in revision.sources.select_related("message").order_by("ordinal", "id")
    }
    if len(source_rows) != revision.source_count:
        return _empty("stale_revision", "revision_sources_changed")

    from management.services.instagram_bot import (
        INLINE_MEDIA_MAX_ITEMS,
        INLINE_MEDIA_RAW_BUDGET,
        INLINE_REQUEST_MAX_BYTES,
        _normalized_inline_mime,
        _owned_media_bytes,
    )

    admitted: list[CollectedRevisionMediaPart] = []
    binding_items: list[dict] = []
    outcomes: list[dict] = []
    reasons: list[str] = []
    total_raw = 0
    total_serialized = 0
    sealed_owned = omitted = unavailable = 0

    def note_reason(value: str) -> None:
        if value and value not in reasons:
            reasons.append(value)

    for source_snapshot in snapshot_sources:
        if not isinstance(source_snapshot, Mapping):
            return _empty("stale_revision", "snapshot_source_invalid")
        try:
            message_id = int(source_snapshot.get("message_id") or 0)
        except (TypeError, ValueError):
            return _empty("stale_revision", "snapshot_source_invalid")
        source_row = source_rows.get(message_id)
        message = getattr(source_row, "message", None) if source_row else None
        source_valid = bool(
            source_row
            and message
            and source_snapshot.get("source_digest") == source_row.source_digest
            and message.client_id == client.pk
            and message.sender_id == client.igsid
            and message.role == InstagramBotMessage.Role.USER
            and message.source == "webhook"
            and message.provider_namespace == source_row.source_namespace
            and source_snapshot.get("source_namespace") == source_row.source_namespace
        )
        media_parts = source_snapshot.get("media_parts")
        if not isinstance(media_parts, list):
            return _empty("stale_revision", "snapshot_media_invalid")
        for raw_part in media_parts:
            if not isinstance(raw_part, Mapping):
                return _empty("stale_revision", "snapshot_media_invalid")
            part_id = str(raw_part.get("source_part_id") or "")
            try:
                original_index = int(raw_part.get("original_index"))
            except (TypeError, ValueError):
                original_index = -1
            header = {
                "source_message_id": message_id,
                "source_part_id": part_id,
                "original_index": original_index,
                "identity_origin": str(raw_part.get("identity_origin") or "")[:32],
                "sealed_capture_outcome": str(
                    raw_part.get("capture_outcome") or "unavailable"
                )[:32],
            }
            ad_context_only = is_ad_context_part(raw_part)
            if ad_context_only:
                header.update(ad_context_only=True, role=str(raw_part["role"]))
            sealed_outcome = header["sealed_capture_outcome"]
            if sealed_outcome != "owned":
                unavailable += 1
                outcomes.append({
                    **header,
                    "collection_outcome": "unavailable",
                    "reason": str(raw_part.get("reason") or "sealed_unavailable")[:64],
                })
                continue
            sealed_owned += 1
            if not source_valid or message.private_media_state != message.PrivateMediaState.ACTIVE:
                unavailable += 1
                note_reason("owner_binding_changed")
                outcomes.append({
                    **header,
                    "collection_outcome": "unavailable",
                    "reason": "owner_binding_changed",
                })
                continue
            current = _current_part(message, part_id)
            sealed_hash = str(raw_part.get("content_hash") or "").casefold()
            sealed_mime = _normalized_inline_mime(raw_part.get("mime"))
            sealed_bytes = _integer(raw_part.get("bytes")) or 0
            current_bytes = _integer(current.get("bytes")) if current else None
            try:
                current_capture_state = (
                    public_media_manifest([current])[0]["capture_state"]
                    if current else ""
                )
            except Exception:
                current_capture_state = ""
            current_matches = bool(
                current
                and current_capture_state == "owned"
                and str(current.get("content_hash") or "").casefold() == sealed_hash
                and current_bytes == sealed_bytes
                and _normalized_inline_mime(current.get("mime")) == sealed_mime
                and current.get("private_storage") is True
                and is_ad_context_part(current) == ad_context_only
                and (not ad_context_only or current.get("role") == raw_part.get("role"))
            )
            if not current_matches:
                unavailable += 1
                note_reason("sealed_part_changed")
                outcomes.append({
                    **header,
                    "collection_outcome": "unavailable",
                    "reason": "sealed_part_changed",
                })
                continue
            owned = _owned_media_bytes(current, message_id=message_id)
            if not owned:
                unavailable += 1
                note_reason("owned_bytes_unavailable")
                outcomes.append({
                    **header,
                    "collection_outcome": "unavailable",
                    "reason": "owned_bytes_unavailable",
                })
                continue
            mime, data = owned
            mime = _normalized_inline_mime(mime)
            actual_hash = hashlib.sha256(data).hexdigest()
            if (
                mime != sealed_mime
                or len(data) != sealed_bytes
                or actual_hash != sealed_hash
            ):
                unavailable += 1
                note_reason("owned_bytes_mismatch")
                outcomes.append({
                    **header,
                    "collection_outcome": "unavailable",
                    "reason": "owned_bytes_mismatch",
                })
                continue

            serialized = _serialized_inline_size(mime, len(data))
            omission_reason = ""
            if len(admitted) >= INLINE_MEDIA_MAX_ITEMS:
                omission_reason = "inline_item_limit"
            elif total_raw + len(data) > INLINE_MEDIA_RAW_BUDGET:
                omission_reason = "inline_raw_budget"
            elif total_serialized + serialized > INLINE_REQUEST_MAX_BYTES:
                omission_reason = "inline_serialized_budget"
            if omission_reason:
                omitted += 1
                note_reason(omission_reason)
                outcomes.append({
                    **header,
                    "collection_outcome": "omitted",
                    "reason": omission_reason,
                    "mime": mime,
                    "bytes": len(data),
                    "content_hash": actual_hash,
                })
                continue

            inline_index = len(admitted)
            part = CollectedRevisionMediaPart(
                source_message_id=message_id,
                source_part_id=part_id,
                original_index=original_index,
                identity_origin=header["identity_origin"],
                mime=mime,
                content_hash=actual_hash,
                byte_length=len(data),
                inline_index=inline_index,
                data=data,
                ad_context_only=ad_context_only,
            )
            admitted.append(part)
            total_raw += len(data)
            total_serialized += serialized
            item = {
                **header,
                "inline_index": inline_index,
                "mime": mime,
                "bytes": len(data),
                "content_hash": actual_hash,
            }
            binding_items.append(item)
            outcomes.append({
                **item,
                "collection_outcome": "admitted",
                "reason": "",
            })

    total_parts = len(outcomes)
    coverage = {
        "total_parts": total_parts,
        "sealed_owned": sealed_owned,
        "admitted": len(admitted),
        "omitted": omitted,
        "unavailable": unavailable,
        "image_admitted": sum(part.mime.startswith("image/") for part in admitted),
        "audio_admitted": sum(part.mime.startswith("audio/") for part in admitted),
        "raw_bytes": total_raw,
        "serialized_inline_bytes": total_serialized,
    }
    binding = {
        "version": BINDING_VERSION,
        "revision_id": revision.pk,
        "revision_snapshot_digest": revision.snapshot_digest,
        "items": binding_items,
        "outcomes": outcomes,
        "actual_content_hashes": [part.content_hash for part in admitted],
        "coverage": coverage,
    }
    binding["digest"] = _digest(binding)
    still_current = IgCustomerTurnRevision.objects.filter(
        pk=revision.pk,
        active_slot=1,
        state=IgCustomerTurnRevision.State.CLAIMED,
        claim_token=revision_token,
        lease_until__gt=timezone.now(),
        snapshot_digest=revision.snapshot_digest,
        client__privacy_erasure_started_at=revision.erasure_started_at_snapshot,
        client__hidden_at__isnull=True,
        client__is_blocked=False,
    ).exists()
    if not still_current:
        return _empty("stale_revision", "revision_changed_during_collection")
    if not total_parts:
        state = "no_media"
    elif unavailable or omitted:
        state = "partial" if admitted else "unavailable"
    else:
        state = "ready"
    return RevisionMediaCollection(
        readiness=state,
        reasons=tuple(reasons),
        parts=tuple(admitted),
        binding=binding,
        coverage=coverage,
    )


__all__ = [
    "CollectedRevisionMediaPart",
    "RevisionMediaCollection",
    "collect_revision_media",
]
