"""Pure recovery contracts for privately captured Instagram media.

This module performs no network, storage, database, notification, or customer
send operations.  The live capture caller owns those boundaries and persists
the returned JSON-safe values on the existing per-part attachment record.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Mapping

from django.utils import timezone

from management.services import ig_media_url_policy as media_policy


RECOVERY_VERSION = "ig-media-recovery-v1"
PREPARED_BLOB_VERSION = "ig-prepared-media-v1"
MAX_CAPTURE_ATTEMPTS = 2
RETRY_BASE_SECONDS = 30
RECOVERY_DEADLINE_SECONDS = 10 * 60
RESOLUTION_RETRY = "retry_background"
RESOLUTION_RESEND = "request_resend"
REASON_ADDRESS_BLOCKED = "address_blocked"
REASON_REDIRECT_BLOCKED = "redirect_blocked"
REASON_HTTP_5XX = "http_5xx"
REASON_HTTP_NONRETRYABLE = "http_nonretryable"

FAILURE_TEMPORARY = "temporary"
FAILURE_EXPIRED = "expired"
FAILURE_PERMANENT = "permanent"
FAILURE_CLASSES = frozenset({
    FAILURE_TEMPORARY,
    FAILURE_EXPIRED,
    FAILURE_PERMANENT,
})

_TEMPORARY_REASONS = frozenset({
    media_policy.REASON_DNS_FAILED,
    media_policy.REASON_DNS_EMPTY,
    media_policy.REASON_TRANSPORT,
    media_policy.REASON_DEADLINE,
    media_policy.REASON_EMPTY_BODY,
    media_policy.REASON_STATUS,
})
_PERMANENT_SECURITY_REASONS = frozenset({
    REASON_ADDRESS_BLOCKED,
    REASON_REDIRECT_BLOCKED,
    media_policy.REASON_EMPTY,
    media_policy.REASON_TOO_LONG,
    media_policy.REASON_MALFORMED,
    media_policy.REASON_CONTROL_CHARS,
    media_policy.REASON_SCHEME,
    media_policy.REASON_USERINFO,
    media_policy.REASON_NO_HOST,
    media_policy.REASON_NON_ASCII_HOST,
    media_policy.REASON_IP_LITERAL,
    media_policy.REASON_PORT,
    media_policy.REASON_HOST_NOT_ALLOWED,
    media_policy.REASON_UNSPECIFIED_ADDRESS,
    media_policy.REASON_LOOPBACK,
    media_policy.REASON_LINK_LOCAL,
    media_policy.REASON_MULTICAST,
    media_policy.REASON_PRIVATE,
    media_policy.REASON_RESERVED,
    media_policy.REASON_NOT_GLOBAL,
    media_policy.REASON_BAD_ADDRESS,
    media_policy.REASON_REDIRECT_LIMIT,
    media_policy.REASON_REDIRECT_NO_LOCATION,
})
_PERMANENT_FORMAT_REASONS = frozenset({
    media_policy.REASON_CONTENT_TYPE,
    media_policy.REASON_DECLARED_TOO_LARGE,
    media_policy.REASON_STREAM_TOO_LARGE,
    media_policy.REASON_SIGNATURE,
    media_policy.REASON_IMAGE_DECODE,
    media_policy.REASON_IMAGE_PIXELS,
    media_policy.REASON_UNVERIFIABLE_MIME,
})
_KNOWN_REASONS = (
    _TEMPORARY_REASONS
    | _PERMANENT_SECURITY_REASONS
    | _PERMANENT_FORMAT_REASONS
)
_TEMPORARY_HTTP_STATUSES = frozenset({408, 425, 429})
_EXPIRED_HTTP_STATUSES = frozenset({401, 403, 404, 410})
_HASH_RE = re.compile(r"[0-9a-f]{64}")


class MediaRecoveryError(ValueError):
    """The caller supplied unsafe or internally inconsistent recovery data."""


@dataclass(frozen=True)
class CaptureFailurePlan:
    reason_code: str
    failure_class: str
    status: str
    retryable: bool
    terminal: bool
    deadline_at: datetime
    next_attempt_at: datetime | None
    resolution_action: str

    def part_updates(self) -> dict:
        """Return bounded JSON values for one attachment-media part."""
        return {
            "status": self.status,
            "error_kind": self.reason_code,
            "capture_failure_class": self.failure_class,
            "capture_retryable": self.retryable,
            "capture_deadline_at": self.deadline_at.isoformat(),
            "capture_next_attempt_at": (
                self.next_attempt_at.isoformat() if self.next_attempt_at else ""
            ),
            "capture_terminal": self.terminal,
            "resolution_required": self.terminal,
            "resolution_action": self.resolution_action,
            "recovery_version": RECOVERY_VERSION,
        }


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise MediaRecoveryError("invalid_datetime")
    if timezone.is_naive(value):
        raise MediaRecoveryError("naive_datetime")
    return value


def parse_recovery_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if timezone.is_aware(parsed) else None


def initial_capture_deadline(
    *,
    now: datetime,
    recovery_seconds: int = RECOVERY_DEADLINE_SECONDS,
) -> datetime:
    now = _aware(now)
    try:
        seconds = int(recovery_seconds)
    except (TypeError, ValueError):
        seconds = RECOVERY_DEADLINE_SECONDS
    seconds = max(RETRY_BASE_SECONDS, min(seconds, 60 * 60))
    return now + timedelta(seconds=seconds)


def _normalized_reason(reason: object) -> str:
    value = str(reason or "").strip().casefold()
    if value.startswith(media_policy.REDIRECT_REASON_PREFIX):
        redirect_reason = value.removeprefix(media_policy.REDIRECT_REASON_PREFIX)
        if redirect_reason in _PERMANENT_SECURITY_REASONS:
            return value
        return REASON_REDIRECT_BLOCKED
    if value.startswith("embedded_"):
        return REASON_ADDRESS_BLOCKED
    return value if value in _KNOWN_REASONS else "unknown"


def classify_fetch_failure(
    outcome: media_policy.FetchOutcome,
) -> tuple[str, str]:
    """Return finite ``(reason_code, failure_class)`` without refetching."""
    if not isinstance(outcome, media_policy.FetchOutcome) or outcome.success:
        raise MediaRecoveryError("expected_failed_fetch")
    reason = _normalized_reason(outcome.reason)
    status = outcome.status_code
    if reason == media_policy.REASON_STATUS:
        if status in _EXPIRED_HTTP_STATUSES:
            return f"http_{status}", FAILURE_EXPIRED
        if status in _TEMPORARY_HTTP_STATUSES or (
            isinstance(status, int) and 500 <= status <= 599
        ):
            return (
                REASON_HTTP_5XX if int(status) >= 500 else f"http_{status}"
            ), FAILURE_TEMPORARY
        if isinstance(status, int):
            return REASON_HTTP_NONRETRYABLE, FAILURE_PERMANENT
        return media_policy.REASON_STATUS, FAILURE_TEMPORARY
    if reason.startswith(media_policy.REDIRECT_REASON_PREFIX):
        return reason, FAILURE_PERMANENT
    if reason in _PERMANENT_SECURITY_REASONS | _PERMANENT_FORMAT_REASONS:
        return reason, FAILURE_PERMANENT
    if reason in _TEMPORARY_REASONS:
        return reason, FAILURE_TEMPORARY
    return "unknown", FAILURE_PERMANENT


def plan_capture_failure(
    outcome: media_policy.FetchOutcome,
    *,
    attempts: int,
    now: datetime,
    deadline_at: datetime | None = None,
    max_attempts: int = MAX_CAPTURE_ATTEMPTS,
    retry_base_seconds: int = RETRY_BASE_SECONDS,
) -> CaptureFailurePlan:
    """Apply the shared two-attempt, one-deadline capture policy."""
    now = _aware(now)
    deadline = _aware(deadline_at) if deadline_at else initial_capture_deadline(now=now)
    try:
        attempts = max(1, int(attempts))
    except (TypeError, ValueError):
        attempts = 1
    try:
        max_attempts = max(1, min(int(max_attempts), MAX_CAPTURE_ATTEMPTS))
    except (TypeError, ValueError):
        max_attempts = MAX_CAPTURE_ATTEMPTS
    try:
        retry_base_seconds = max(1, min(int(retry_base_seconds), 5 * 60))
    except (TypeError, ValueError):
        retry_base_seconds = RETRY_BASE_SECONDS

    reason, failure_class = classify_fetch_failure(outcome)
    retryable_class = failure_class == FAILURE_TEMPORARY
    retry_at = now + timedelta(
        seconds=retry_base_seconds * (2 ** max(0, attempts - 1))
    )
    retryable = bool(
        retryable_class
        and attempts < max_attempts
        and now < deadline
        and retry_at <= deadline
    )
    terminal = not retryable
    if failure_class == FAILURE_EXPIRED:
        status = "expired"
    elif failure_class == FAILURE_PERMANENT and (
        reason in _PERMANENT_SECURITY_REASONS
        or reason.startswith(media_policy.REDIRECT_REASON_PREFIX)
    ):
        status = "blocked"
    else:
        status = "unavailable"
    return CaptureFailurePlan(
        reason_code=reason,
        failure_class=failure_class,
        status=status,
        retryable=retryable,
        terminal=terminal,
        deadline_at=deadline,
        next_attempt_at=retry_at if retryable else None,
        resolution_action=RESOLUTION_RETRY if retryable else RESOLUTION_RESEND,
    )


def retry_due(part: Mapping[str, object], *, now: datetime) -> bool:
    """Return whether an explicitly retryable part is due inside its deadline."""
    now = _aware(now)
    if not isinstance(part, Mapping) or part.get("capture_retryable") is not True:
        return False
    if part.get("capture_terminal") is True:
        return False
    deadline = parse_recovery_datetime(part.get("capture_deadline_at"))
    retry_at = parse_recovery_datetime(part.get("capture_next_attempt_at"))
    return bool(deadline and retry_at and now <= deadline and retry_at <= now)


def pending_retry_at(
    parts: list[Mapping[str, object]] | tuple[Mapping[str, object], ...],
    *,
    now: datetime,
) -> datetime | None:
    """Return the earliest future retry for background analysis deferral."""
    now = _aware(now)
    candidates = []
    for part in parts or ():
        if not isinstance(part, Mapping) or part.get("capture_retryable") is not True:
            continue
        if part.get("capture_terminal") is True:
            continue
        deadline = parse_recovery_datetime(part.get("capture_deadline_at"))
        retry_at = parse_recovery_datetime(part.get("capture_next_attempt_at"))
        if deadline and retry_at and now < retry_at <= deadline:
            candidates.append(retry_at)
    return min(candidates) if candidates else None


def _normalized_storage_name(value: object) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 500 or "\\" in name or any(ord(ch) < 32 for ch in name):
        raise MediaRecoveryError("invalid_storage_name")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MediaRecoveryError("invalid_storage_name")
    return str(path)


def prepared_blob_descriptor(
    *,
    storage_name: object,
    mime_type: object,
    body_bytes: bytes,
) -> dict:
    """Describe intended private bytes for persistence before storage save."""
    if not isinstance(body_bytes, bytes) or not body_bytes:
        raise MediaRecoveryError("invalid_blob_bytes")
    mime = str(mime_type or "").strip().casefold()
    if mime not in (
        media_policy.SUPPORTED_INLINE_IMAGE_MIMES
        | media_policy.SUPPORTED_INLINE_AUDIO_MIMES
        | media_policy.SUPPORTED_INLINE_VIDEO_MIMES
    ):
        raise MediaRecoveryError("invalid_blob_mime")
    return {
        "version": PREPARED_BLOB_VERSION,
        "storage_name": _normalized_storage_name(storage_name),
        "content_hash": hashlib.sha256(body_bytes).hexdigest(),
        "mime": mime,
        "bytes": len(body_bytes),
    }


def prepared_blob_matches(descriptor: Mapping[str, object], body_bytes: bytes) -> bool:
    """Verify bytes read from private storage before an owned finalization."""
    if not isinstance(descriptor, Mapping) or not isinstance(body_bytes, bytes):
        return False
    try:
        storage_name = _normalized_storage_name(descriptor.get("storage_name"))
        byte_count = int(descriptor.get("bytes") or 0)
    except (MediaRecoveryError, TypeError, ValueError):
        return False
    digest = str(descriptor.get("content_hash") or "").strip().casefold()
    mime = str(descriptor.get("mime") or "").strip().casefold()
    return bool(
        descriptor.get("version") == PREPARED_BLOB_VERSION
        and storage_name
        and _HASH_RE.fullmatch(digest)
        and mime in (
            media_policy.SUPPORTED_INLINE_IMAGE_MIMES
            | media_policy.SUPPORTED_INLINE_AUDIO_MIMES
            | media_policy.SUPPORTED_INLINE_VIDEO_MIMES
        )
        and byte_count == len(body_bytes)
        and hashlib.sha256(body_bytes).hexdigest() == digest
    )


def prepared_part_updates(descriptor: Mapping[str, object]) -> dict:
    """Return the marker a caller must persist before writing private bytes."""
    if not isinstance(descriptor, Mapping):
        raise MediaRecoveryError("invalid_prepared_blob")
    try:
        storage_name = _normalized_storage_name(descriptor.get("storage_name"))
        byte_count = int(descriptor.get("bytes") or 0)
    except (MediaRecoveryError, TypeError, ValueError) as exc:
        raise MediaRecoveryError("invalid_prepared_blob") from exc
    digest = str(descriptor.get("content_hash") or "").strip().casefold()
    mime = str(descriptor.get("mime") or "").strip().casefold()
    if not (
        descriptor.get("version") == PREPARED_BLOB_VERSION
        and _HASH_RE.fullmatch(digest)
        and mime in (
            media_policy.SUPPORTED_INLINE_IMAGE_MIMES
            | media_policy.SUPPORTED_INLINE_AUDIO_MIMES
            | media_policy.SUPPORTED_INLINE_VIDEO_MIMES
        )
        and byte_count > 0
    ):
        raise MediaRecoveryError("invalid_prepared_blob")
    return {
        "status": "storing",
        "prepared_blob": {
            "version": PREPARED_BLOB_VERSION,
            "storage_name": storage_name,
            "content_hash": digest,
            "mime": mime,
            "bytes": byte_count,
        },
        "recovery_version": RECOVERY_VERSION,
    }


def owned_part_updates(
    descriptor: Mapping[str, object],
    *,
    verified_body_bytes: bytes,
) -> dict:
    """Return owned fields only after the prepared private blob is verified."""
    if not prepared_blob_matches(descriptor, verified_body_bytes):
        raise MediaRecoveryError("prepared_blob_mismatch")
    return {
        "status": "owned",
        "storage_name": str(descriptor["storage_name"]),
        "private_storage": True,
        "mime": str(descriptor["mime"]),
        "bytes": int(descriptor["bytes"]),
        "content_hash": str(descriptor["content_hash"]),
        "error_kind": "",
        "capture_failure_class": "",
        "capture_retryable": False,
        "capture_deadline_at": "",
        "capture_next_attempt_at": "",
        "capture_terminal": False,
        "resolution_required": False,
        "resolution_action": "",
        "prepared_blob": {},
        "recovery_version": RECOVERY_VERSION,
    }


__all__ = [
    "CaptureFailurePlan",
    "FAILURE_EXPIRED",
    "FAILURE_PERMANENT",
    "FAILURE_TEMPORARY",
    "MAX_CAPTURE_ATTEMPTS",
    "MediaRecoveryError",
    "PREPARED_BLOB_VERSION",
    "RECOVERY_DEADLINE_SECONDS",
    "RECOVERY_VERSION",
    "RESOLUTION_RESEND",
    "RESOLUTION_RETRY",
    "RETRY_BASE_SECONDS",
    "classify_fetch_failure",
    "initial_capture_deadline",
    "owned_part_updates",
    "pending_retry_at",
    "plan_capture_failure",
    "prepared_blob_descriptor",
    "prepared_blob_matches",
    "prepared_part_updates",
    "retry_due",
]
