"""Pure, redacted per-part media-manifest helpers.

The durable ``attachment_media`` JSON remains the owner of transport details.
This module only derives stable local part identities and provider-safe evidence;
it never returns a URL, storage name, provider identifier, or media bytes.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence


MANIFEST_VERSION = "ig-media-manifest-v1"
SOURCE_PART_PREFIX = "mp1_"
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_PART_ID_RE = re.compile(r"mp1_[0-9a-f]{32}")
IMAGE_OBSERVATION_OUTCOMES = frozenset({"understood", "unreadable", "uncertain"})
IMAGE_EVIDENCE_CODES = frozenset({
    "visual_content", "text_visible", "text_unreadable", "insufficient_detail",
})
IMAGE_TYPE_CODES = frozenset({
    "product", "custom_reference", "receipt", "payment_screenshot", "selfie",
    "certificate", "document", "other", "unknown",
})
_TERMINAL_MISSING_CAPTURE_STATES = frozenset({
    "failed", "expired", "blocked", "metadata_only", "delete_pending", "deleted",
})


class MediaManifestError(ValueError):
    """The caller supplied a manifest that cannot prove an image binding."""


AD_CONTEXT_ROLES = frozenset({"ad_context", "ad_photo", "ad_video_thumbnail"})


def is_ad_context_part(value):
    """Purpose is inherited from the immutable source role, never model text."""
    return isinstance(value, Mapping) and str(value.get("role") or "") in AD_CONTEXT_ROLES


def _nonnegative_int(value, *, default: int | None = None) -> int:
    if isinstance(value, bool):
        if default is None:
            raise MediaManifestError("invalid_index")
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        if default is None:
            raise MediaManifestError("invalid_index")
        return default
    if parsed < 0:
        if default is None:
            raise MediaManifestError("invalid_index")
        return default
    return parsed


def _opaque_source_part_id(message_scope: object, original_index: int) -> str:
    """Create a local opaque ID; the scope is never copied into the value."""
    scope = str(message_scope or "").strip()
    if not scope:
        raise MediaManifestError("missing_message_scope")
    material = f"{MANIFEST_VERSION}\x1f{scope}\x1f{original_index}".encode("utf-8")
    return SOURCE_PART_PREFIX + hashlib.sha256(material).hexdigest()[:32]


def _is_opaque_source_part_id(value: object) -> bool:
    return bool(_SOURCE_PART_ID_RE.fullmatch(str(value or "").strip()))


def _normalized_hash(value: object) -> str:
    digest = str(value or "").strip().lower()
    return digest if _HASH_RE.fullmatch(digest) else ""


def _capture_state(part: Mapping[str, object]) -> str:
    state = str(part.get("capture_state") or "").strip().lower()
    if state:
        return state
    status = str(part.get("status") or "").strip().lower()
    return {
        "pending": "discovered",
        "acquiring": "fetching",
        "owned": "owned",
        "unavailable": "failed",
        "metadata_only": "metadata_only",
    }.get(status, "discovered")


def normalize_attachment_media(
    media: Sequence[Mapping[str, object]] | None,
    *,
    message_scope: object,
    identity_origin: str = "legacy_positional",
) -> list[dict]:
    """Add stable local identity without changing a part's transport metadata.

    Missing identities on historical JSON receive a positional legacy identity.
    That preserves the only order still available; it does not pretend to
    reconstruct duplicate parts already collapsed by older URL-based ingress.
    """
    if identity_origin not in {"ingress", "legacy_positional"}:
        raise MediaManifestError("invalid_identity_origin")
    normalized: list[dict] = []
    seen_part_ids: set[str] = set()
    seen_original_indexes: set[int] = set()
    for position, raw in enumerate(media or ()):
        if not isinstance(raw, Mapping):
            continue
        part = dict(raw)
        existing_index = part.get("original_index")
        try:
            original_index = _nonnegative_int(existing_index)
            legacy_positional = identity_origin != "ingress"
        except MediaManifestError:
            original_index = position
            legacy_positional = identity_origin != "ingress"
        source_part_id = str(part.get("source_part_id") or "").strip()
        if not _is_opaque_source_part_id(source_part_id):
            source_part_id = _opaque_source_part_id(message_scope, original_index)
            legacy_positional = identity_origin != "ingress"
        if source_part_id in seen_part_ids or original_index in seen_original_indexes:
            raise MediaManifestError("duplicate_part_identity")
        seen_part_ids.add(source_part_id)
        seen_original_indexes.add(original_index)
        part["source_part_id"] = source_part_id
        part["original_index"] = original_index
        if legacy_positional:
            part["identity_origin"] = "legacy_positional"
        else:
            part.setdefault("identity_origin", identity_origin)
        digest = _normalized_hash(part.get("content_hash"))
        if digest:
            part["content_hash"] = digest
        normalized.append(part)
    return normalized


def public_media_manifest(media: Sequence[Mapping[str, object]] | None) -> list[dict]:
    """Return only redacted, provider-safe part state; no URL or byte payload."""
    output: list[dict] = []
    for raw in media or ():
        if not isinstance(raw, Mapping):
            continue
        source_part_id = str(raw.get("source_part_id") or "").strip()
        if not _is_opaque_source_part_id(source_part_id):
            raise MediaManifestError("missing_source_part_id")
        inspection = raw.get("inspection")
        inspection = inspection if isinstance(inspection, Mapping) else {}
        entry = {
            "source_part_id": source_part_id,
            "original_index": _nonnegative_int(raw.get("original_index")),
            "identity_origin": str(raw.get("identity_origin") or "ingress")[:32],
            "capture_state": _capture_state(raw),
            "inspection_state": str(inspection.get("state") or "uninspected")[:32],
            "inspection_outcome": str(inspection.get("outcome") or "")[:32],
        }
        if is_ad_context_part(raw):
            entry.update(ad_context_only=True, role=str(raw["role"]))
        digest = _normalized_hash(raw.get("content_hash"))
        if digest:
            entry["content_hash"] = digest
        output.append(entry)
    return output


def media_coverage(media: Sequence[Mapping[str, object]] | None) -> dict:
    """Summarize coverage without conflating owned, inspected, and unreadable."""
    parts = public_media_manifest(media)
    owned = inspected = unreadable = uncertain = missing = 0
    for part in parts:
        capture_state = part["capture_state"]
        outcome = part["inspection_outcome"]
        if capture_state == "owned":
            owned += 1
        if outcome == "unreadable":
            unreadable += 1
        elif outcome == "uncertain":
            uncertain += 1
        elif part["inspection_state"] == "inspected":
            inspected += 1
        if capture_state in _TERMINAL_MISSING_CAPTURE_STATES:
            missing += 1
    return {
        "version": MANIFEST_VERSION,
        "total": len(parts),
        "capture_owned": owned,
        "inspected": inspected,
        "unreadable": unreadable,
        "uncertain": uncertain,
        "missing": missing,
        "parts": parts,
    }


def inline_part_evidence(
    parts: Sequence[Mapping[str, object]],
    *,
    image_count: int,
    actual_inline_count: int,
    actual_content_hashes: Sequence[object] | None = None,
) -> list[dict]:
    """Map the exact provider-admitted prefix of submitted owned parts.

    ``parts`` must already be the ordered image-part list prepared for this
    provider request; failed or unselected bundle siblings do not belong here.
    """
    image_count = _nonnegative_int(image_count)
    actual_inline_count = _nonnegative_int(actual_inline_count)
    if image_count != len(parts) or actual_inline_count > image_count:
        raise MediaManifestError("inline_count_mismatch")
    selected = public_media_manifest(parts[:actual_inline_count])
    if any(part["capture_state"] != "owned" for part in selected):
        raise MediaManifestError("non_owned_inline_part")
    hashes = [_normalized_hash(part.get("content_hash")) for part in selected]
    if not all(hashes):
        raise MediaManifestError("missing_content_hash")
    if actual_content_hashes is not None:
        supplied = [_normalized_hash(value) for value in actual_content_hashes]
        if supplied != hashes:
            raise MediaManifestError("inline_hash_mismatch")
    for inline_index, part in enumerate(selected):
        part["source_image_index"] = inline_index
    return selected


def map_image_observations(
    parts: Sequence[Mapping[str, object]],
    observations: Sequence[object] | None,
    *,
    image_count: int,
    actual_inline_count: int,
    actual_content_hashes: Sequence[object] | None = None,
    prize_programme=None,
) -> list[dict]:
    """Validate model image indexes and map them to redacted local evidence.

    An empty observation list intentionally produces no inspected parts.  The
    caller may persist these records only after a successful provider result.
    """
    admitted = inline_part_evidence(
        parts,
        image_count=image_count,
        actual_inline_count=actual_inline_count,
        actual_content_hashes=actual_content_hashes,
    )
    by_index = {part["source_image_index"]: part for part in admitted}
    result: list[dict] = []
    seen_indexes: set[int] = set()
    for raw in observations or ():
        if isinstance(raw, Mapping):
            value = raw
        else:
            value = {
                "source_image_index": getattr(raw, "source_image_index", None),
                "outcome": getattr(raw, "outcome", None),
                "evidence_code": getattr(raw, "evidence_code", ""),
                "type_code": getattr(raw, "type_code", ""),
                "prize_certificate": getattr(raw, "prize_certificate", None),
            }
        index = _nonnegative_int(value.get("source_image_index"))
        if index < len(parts) and str(parts[index].get("mime") or "").startswith("audio/"):
            raise MediaManifestError("audio_is_not_image_observation")
        outcome = str(value.get("outcome") or "").strip().lower()
        evidence_code = str(value.get("evidence_code") or "").strip().lower()
        type_code = str(value.get("type_code") or "").strip().lower()
        if (
            index not in by_index
            or index in seen_indexes
            or outcome not in IMAGE_OBSERVATION_OUTCOMES
            or evidence_code not in IMAGE_EVIDENCE_CODES
            or type_code not in IMAGE_TYPE_CODES
        ):
            raise MediaManifestError("invalid_image_observation")
        prize_certificate = value.get("prize_certificate")
        if index < len(parts) and is_ad_context_part(parts[index]):
            prize_certificate = None
        if prize_certificate is not None:
            if type_code != "certificate":
                raise MediaManifestError("prize_requires_certificate")
            from management.services.ig_prize_programme import validate_prize_observation

            prize_certificate = validate_prize_observation(
                prize_certificate, programme=prize_programme,
            )
            if prize_certificate is None:
                raise MediaManifestError("invalid_prize_observation")
        seen_indexes.add(index)
        part = dict(by_index[index])
        part.update({
            "outcome": outcome,
            "evidence_code": evidence_code,
            "type_code": type_code,
        })
        if prize_certificate is not None:
            part["prize_certificate"] = prize_certificate.public_value()
        result.append(part)
    return result
