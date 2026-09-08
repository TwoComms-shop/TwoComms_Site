"""Fail-closed activation contract for the live revision worker."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
import os

from django.conf import settings
from django.utils import timezone


EXECUTION_FLAG = "IG_REVISION_EXECUTION_ENABLED"
CUTOVER_SETTING = "IG_REVISION_EXECUTION_CUTOVER_AT"
_UNSET = object()


@dataclass(frozen=True)
class RevisionExecutionRollout:
    requested: bool = False
    enabled: bool = False
    cutover_at: datetime | None = None
    reason: str = "flag_off"


def _requested(value) -> bool:
    return value is True or str(value).strip().casefold() in {
        "1", "true", "yes", "on",
    }


def parse_cutover(value) -> datetime | None:
    """Return one aware UTC timestamp; malformed or naive values fail closed."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if not timezone.is_aware(parsed):
        return None
    return parsed.astimezone(datetime_timezone.utc)


def revision_execution_rollout(
    *, now=None, flag_value=_UNSET, cutover_value=_UNSET,
) -> RevisionExecutionRollout:
    """Resolve the runtime switch without consulting or mutating application data."""
    if flag_value is _UNSET:
        flag_value = getattr(
            settings, EXECUTION_FLAG, os.environ.get(EXECUTION_FLAG, False)
        )
    if not _requested(flag_value):
        return RevisionExecutionRollout()
    if cutover_value is _UNSET:
        cutover_value = getattr(
            settings, CUTOVER_SETTING, os.environ.get(CUTOVER_SETTING, "")
        )
    cutover_at = parse_cutover(cutover_value)
    if cutover_at is None:
        return RevisionExecutionRollout(
            requested=True, reason="cutover_missing_or_invalid"
        )
    current = now or timezone.now()
    if not timezone.is_aware(current):
        return RevisionExecutionRollout(
            requested=True, cutover_at=cutover_at, reason="clock_not_aware"
        )
    if current < cutover_at:
        return RevisionExecutionRollout(
            requested=True, cutover_at=cutover_at, reason="cutover_pending"
        )
    return RevisionExecutionRollout(
        requested=True, enabled=True, cutover_at=cutover_at, reason="enabled"
    )


__all__ = [
    "CUTOVER_SETTING", "EXECUTION_FLAG", "RevisionExecutionRollout",
    "parse_cutover", "revision_execution_rollout",
]
