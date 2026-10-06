"""Pure, content-free lifecycle projection; never an access or file proof."""
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import re

SCHEMA = "private-media-lifecycle.v1"
RETENTION_POLICY_VERSION = "ig-private-media-retention-v1"
MIN_RETENTION_SECONDS = 3600
MAX_RETENTION_SECONDS = 60 * 86400
_PART = re.compile(r"mp1_[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_STATES = {"", "active", "delete_pending", "deleting", "delete_failed", "deleted"}
_PREVIEW_MIMES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif",
    "audio/wav", "audio/mpeg", "audio/mp3", "audio/aiff", "audio/aac", "audio/ogg", "audio/flac",
    "audio/m4a", "audio/opus", "audio/webm", "audio/l16", "audio/alaw", "audio/mulaw",
    "audio/mp4", "audio/x-m4a", "audio/x-wav", "audio/x-aiff", "video/mp4", "video/webm"}


def _date(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _identity(part):
    return (isinstance(part.get("source_part_id"), str) and bool(_PART.fullmatch(part["source_part_id"]))
            and isinstance(part.get("content_hash"), str) and bool(_HASH.fullmatch(part["content_hash"])))


def _policy(part, *, owner_verified):
    unknown = {"state": "unknown", "version": None, "retention_seconds": None}
    raw = part.get("retention_policy")
    captured = ((part.get("status") == "owned" and part.get("private_storage") is True
                 and isinstance(part.get("storage_name"), str) and bool(part["storage_name"].strip()))
                or part.get("status") == "expired")
    capture = part.get("capture_state")
    capture_valid = capture is None or (isinstance(capture, str) and capture in {"", "owned"})
    if (not owner_verified or not captured or not capture_valid or not _identity(part) or not isinstance(raw, Mapping)
        or set(raw) != {"version", "retention_seconds", "captured_at", "delete_after", "source_part_id", "content_hash"}
        or raw.get("version") != RETENTION_POLICY_VERSION
        or raw.get("source_part_id") != part.get("source_part_id")
        or raw.get("content_hash") != part.get("content_hash")):
        return unknown
    seconds = raw.get("retention_seconds")
    start, end = _date(raw.get("captured_at")), _date(raw.get("delete_after"))
    part_due = _date(part.get("delete_after"))
    if (type(seconds) is not int or not MIN_RETENTION_SECONDS <= seconds <= MAX_RETENTION_SECONDS
        or start is None or end is None
        or (part.get("delete_after") and part_due != end)):
        return unknown
    try:
        if end != start + timedelta(seconds=seconds):
            return unknown
    except OverflowError:
        return unknown
    return {"state": "verified", "version": RETENTION_POLICY_VERSION, "retention_seconds": seconds}


def project_private_media_lifecycle(part, *, message_state="", message_delete_after=None,
                                   message_parts=(), owner_verified=False,
                                   erasure_started=False, now=None):
    """Project only explicit stored evidence, without ORM/filesystem/provider I/O.

    ``owner_verified`` is the caller's existing owner/permission scope proof.
    ``readable`` means eligibility for the separately guarded preview endpoint,
    never proof that the file still exists or that bytes were just inspected.
    Unknown expiry cannot create an evergreen preview or a fresh 60-day timer.
    """
    part = part if isinstance(part, Mapping) else {}
    owner_verified = owner_verified is True
    erasure_started = erasure_started is not False
    clock = _date(now) if now is not None else datetime.now(timezone.utc)
    due_values = [_date(message_delete_after), _date(part.get("delete_after"))]
    for sibling in message_parts if isinstance(message_parts, (list, tuple)) else ():
        if isinstance(sibling, Mapping) and sibling.get("private_storage") is True:
            due_values.append(_date(sibling.get("delete_after")))
    due = min((value for value in due_values if value is not None), default=None)
    status = part.get("status") if isinstance(part.get("status"), str) else ""
    raw_capture = part.get("capture_state")
    capture = raw_capture if isinstance(raw_capture, str) or raw_capture is None else "invalid"
    owned = (status == "owned" and capture in {None, "", "owned"} and _identity(part)
             and part.get("private_storage") is True
             and isinstance(part.get("storage_name"), str) and bool(part["storage_name"].strip()))
    tombstone = status == "expired" and _identity(part)
    state, reason, readable = "unverified", "capture_unverified", False
    if not isinstance(message_state, str) or message_state not in _STATES:
        reason = "lifecycle_unverified"
    elif not owner_verified:
        reason = "owner_unverified"
    elif message_state == "deleted" and tombstone:
        state, reason = "deleted", "deletion_confirmed"
    elif erasure_started or message_state == "delete_pending":
        state, reason = "pending", "privacy_erasure" if erasure_started else "deletion_requested"
    elif message_state in {"deleting", "delete_failed"}:
        state, reason = message_state, "deletion_in_progress" if message_state == "deleting" else "deletion_retry"
    elif status in {"unavailable", "failed", "expired", "blocked", "metadata_only"}:
        state, reason = "missing", "capture_missing"
    elif status in {"pending", "acquiring"} or capture in {"discovered", "fetching"}:
        state, reason = "pending", "capture_pending"
    elif owned:
        mime = part.get("mime")
        if not isinstance(mime, str) or mime.split(";", 1)[0].strip().lower() not in _PREVIEW_MIMES:
            reason = "preview_unsupported"
        elif clock is None:
            reason = "clock_unverified"
        elif due is None:
            reason = "expiry_unknown"
        elif due <= clock:
            state, reason = "expired", "retention_elapsed"
        elif message_state == "active":
            state, reason, readable = "active", "owned_capture", True
        else:
            reason = "lifecycle_unverified"
    if state == "deleted":
        due = None  # No countdown or synthetic completion timestamp after purge.
    retry = _date(message_delete_after) if state == "delete_failed" else None
    return {"schema": SCHEMA, "state": state, "reason": reason,
        "deletion_due": due.isoformat() if due else None, "expiry_known": due is not None,
        "readable": readable, "readability": "preview_eligible" if readable else "unavailable",
        "retry_due": retry.isoformat() if retry else None,
        "policy": _policy(part, owner_verified=owner_verified)}
