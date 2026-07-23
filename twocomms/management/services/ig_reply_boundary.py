"""Cross-process boundary between observation and customer-facing automation."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from management.services.ig_maintenance import _exclusive_file_lock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPLY_BOUNDARY_LOCK_FILE = str(PROJECT_ROOT / "tmp" / "ig_bot_reply_boundary.lock")


@contextmanager
def pause_reply_boundary(*, lock_path: str = REPLY_BOUNDARY_LOCK_FILE):
    """Wait for any in-flight reply/follow-up boundary, then own the pause edge."""
    with _exclusive_file_lock(lock_path):
        yield


@contextmanager
def reply_execution_boundary(
    settings_id: int | None,
    client_id: int | None,
    *,
    lock_path: str = REPLY_BOUNDARY_LOCK_FILE,
):
    """Serialize final permission truth with all customer-facing reply work."""
    with _exclusive_file_lock(lock_path):
        from management.models import IgClient, InstagramBotSettings

        enabled = bool(
            settings_id
            and InstagramBotSettings.objects.filter(pk=settings_id, is_enabled=True).exists()
        )
        client_allowed = True
        if client_id:
            client = IgClient.objects.filter(pk=client_id).only(
                "bot_paused", "manager_takeover", "is_blocked", "hidden_at"
            ).first()
            client_allowed = bool(
                client
                and not client.bot_paused
                and not client.manager_takeover
                and not client.is_blocked
                and not client.hidden_at
            )
        yield bool(enabled and client_allowed)
