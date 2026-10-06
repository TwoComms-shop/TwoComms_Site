"""Hardened ephemeral storage and two-phase deletion for IG customer media."""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.utils import timezone


ACTIVE = "active"
DELETE_PENDING = "delete_pending"
DELETING = "deleting"
DELETE_FAILED = "delete_failed"
DELETED = "deleted"
DELETE_CLAIM_SECONDS = 300
DELETE_RETRY_SECONDS = 60
USE_LEASE_MAX_SECONDS = 300
DEADLINE_CURSOR_KEY = "ig_private_media_deadline_cursor:v1"
_debug_root: Path | None = None


def _require_secure_fd_primitives() -> None:
    """Fail readiness when the host cannot harden paths without following links."""
    missing = []
    for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"):
        if not getattr(os, name, 0):
            missing.append(name)
    for name in ("fstat", "fchmod", "geteuid"):
        if not callable(getattr(os, name, None)):
            missing.append(f"os.{name}")
    supports_dir_fd = getattr(os, "supports_dir_fd", set())
    for function in (os.open, os.mkdir, os.unlink):
        if function not in supports_dir_fd:
            missing.append(f"{function.__name__}(dir_fd=...)")
    if missing:
        raise ImproperlyConfigured(
            "IG private media requires secure fd primitives: "
            + ", ".join(missing)
        )


def _verify_and_harden_fd(
    descriptor: int,
    *,
    directory: bool,
    label: str,
) -> os.stat_result:
    """Validate type/ownership and apply the canonical private mode by fd."""
    info = os.fstat(descriptor)
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode):
        raise PermissionError(f"unsafe private media {label} type")
    if info.st_uid != os.geteuid():
        raise PermissionError(f"unsafe private media {label} ownership")
    expected_mode = 0o700 if directory else 0o600
    os.fchmod(descriptor, expected_mode)
    hardened = os.fstat(descriptor)
    if stat.S_IMODE(hardened.st_mode) != expected_mode:
        raise PermissionError(f"unsafe private media {label} mode")
    return hardened


def _open_owned_directory(path, *, dir_fd: int | None = None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path, flags, dir_fd=dir_fd)
    try:
        _verify_and_harden_fd(descriptor, directory=True, label="directory")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_canonical_root(path: Path) -> int:
    """Open an absolute root one component at a time without following symlinks."""
    if not path.is_absolute():
        raise ImproperlyConfigured("IG_PRIVATE_MEDIA_ROOT must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open(os.sep, flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            try:
                if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                    raise PermissionError("unsafe private media root path type")
            except Exception:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
        _verify_and_harden_fd(current_fd, directory=True, label="root")
    except Exception:
        os.close(current_fd)
        raise
    return current_fd


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _configured_root() -> Path:
    _require_secure_fd_primitives()
    raw = str(getattr(settings, "IG_PRIVATE_MEDIA_ROOT", "") or "").strip()
    if not raw:
        if not bool(getattr(settings, "DEBUG", False)):
            raise ImproperlyConfigured(
                "IG_PRIVATE_MEDIA_ROOT is required when DEBUG=False"
            )
        global _debug_root
        if _debug_root is None:
            _debug_root = Path(
                tempfile.mkdtemp(prefix=f"twocomms-private-media-{os.getpid()}-")
            )
            descriptor = _open_canonical_root(_debug_root.resolve(strict=True))
            os.close(descriptor)
        return _debug_root
    configured = Path(raw).expanduser()
    if not configured.is_absolute():
        raise ImproperlyConfigured("IG_PRIVATE_MEDIA_ROOT must be absolute")
    return configured


def validate_private_root(*, require_exists: bool = True) -> Path:
    configured = _configured_root()
    # Reject a symlink at any existing path component before canonicalizing.
    cursor = configured
    while True:
        if cursor.exists() and cursor.is_symlink():
            raise ImproperlyConfigured("IG private media path cannot contain symlinks")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if require_exists and not configured.exists():
        raise ImproperlyConfigured("IG_PRIVATE_MEDIA_ROOT must be pre-created")
    canonical = configured.resolve(strict=require_exists)
    if require_exists and not canonical.is_dir():
        raise ImproperlyConfigured("IG_PRIVATE_MEDIA_ROOT must be a directory")

    forbidden = []
    media_root = str(getattr(settings, "MEDIA_ROOT", "") or "").strip()
    if media_root:
        forbidden.append(Path(media_root).expanduser().resolve())
    base_dir = Path(settings.BASE_DIR).resolve()
    forbidden.extend((base_dir, base_dir.parent))
    if any(_inside(canonical, root) for root in forbidden):
        raise ImproperlyConfigured(
            "IG_PRIVATE_MEDIA_ROOT must be outside MEDIA_ROOT and the checkout"
        )
    if require_exists:
        info = canonical.stat()
        if info.st_uid != os.geteuid():
            raise ImproperlyConfigured("IG private media root must be owned by the worker euid")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise ImproperlyConfigured("IG private media root mode must be 0700")
        descriptor = _open_canonical_root(canonical)
        os.close(descriptor)
    return canonical


class HardenedPrivateMediaStorage(FileSystemStorage):
    def __init__(self):
        root = validate_private_root(require_exists=True)
        super().__init__(
            location=str(root),
            base_url=None,
            file_permissions_mode=0o600,
            directory_permissions_mode=0o700,
        )
        self._canonical_root = root

    def url(self, name):
        raise ValueError("private Instagram media never has a public URL")

    def _open(self, name, mode="rb"):
        from django.core.files import File

        if mode not in {"rb", "r"}:
            raise ValueError("private media storage is read-only through open()")
        parent_fd, leaf = self._open_parent(name, create=False)
        try:
            descriptor = os.open(
                leaf,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | os.O_NONBLOCK
                | getattr(os, "O_BINARY", 0),
                dir_fd=parent_fd,
            )
        finally:
            os.close(parent_fd)
        stream = None
        try:
            _verify_and_harden_fd(descriptor, directory=False, label="file")
            stream = os.fdopen(descriptor, mode)
            return File(stream, name)
        except Exception:
            if stream is not None:
                stream.close()
            else:
                os.close(descriptor)
            raise

    @staticmethod
    def _name_parts(name: str) -> tuple[str, ...]:
        normalized = str(name).replace("\\", "/")
        raw_parts = normalized.split("/")
        candidate = PurePosixPath(normalized)
        if (
            not normalized
            or candidate.is_absolute()
            or any(part in {"", ".", ".."} for part in raw_parts)
            or not candidate.name
        ):
            raise SuspiciousFileOperation("unsafe private media storage name")
        return candidate.parts

    def _open_parent(self, name: str, *, create: bool) -> tuple[int, str]:
        parts = self._name_parts(name)
        current_fd = _open_canonical_root(self._canonical_root)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                next_fd = _open_owned_directory(part, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
        except Exception:
            os.close(current_fd)
            raise
        return current_fd, parts[-1]

    def _save(self, name, content):
        from django.core.files import locks

        while True:
            parent_fd, leaf = self._open_parent(name, create=True)
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | os.O_NOFOLLOW
            )
            try:
                descriptor = os.open(leaf, flags, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                os.close(parent_fd)
                name = self.get_available_name(name)
                continue
            except Exception:
                os.close(parent_fd)
                raise
            stream = None
            locked = False
            succeeded = False
            try:
                _verify_and_harden_fd(descriptor, directory=False, label="file")
                locks.lock(descriptor, locks.LOCK_EX)
                locked = True
                for chunk in content.chunks():
                    if stream is None:
                        stream = os.fdopen(
                            descriptor,
                            "wb" if isinstance(chunk, bytes) else "wt",
                        )
                    stream.write(chunk)
                if stream is not None:
                    stream.flush()
                _verify_and_harden_fd(descriptor, directory=False, label="file")
                succeeded = True
            finally:
                cleanup_failed = False
                try:
                    try:
                        if locked:
                            locks.unlock(descriptor)
                    except Exception:
                        cleanup_failed = True
                        raise
                finally:
                    try:
                        try:
                            if stream is not None:
                                stream.close()
                            else:
                                os.close(descriptor)
                        except Exception:
                            cleanup_failed = True
                            raise
                    finally:
                        try:
                            if not succeeded or cleanup_failed:
                                try:
                                    os.unlink(leaf, dir_fd=parent_fd)
                                except FileNotFoundError:
                                    pass
                        finally:
                            os.close(parent_fd)
            return str(PurePosixPath(*self._name_parts(name)))

    def delete(self, name):
        """Unlink a verified private regular file through its pinned parent fd."""
        try:
            parent_fd, leaf = self._open_parent(name, create=False)
        except FileNotFoundError:
            return
        try:
            try:
                descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=parent_fd)
            except FileNotFoundError:
                return
            try:
                _verify_and_harden_fd(descriptor, directory=False, label="delete target")
                try:
                    os.unlink(leaf, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)


# Imported lazily above to keep the module-level validation side-effect free.
from django.core.exceptions import SuspiciousFileOperation  # noqa: E402


def private_media_storage() -> HardenedPrivateMediaStorage:
    return HardenedPrivateMediaStorage()


def current_private_media_retention_policy():
    """Capture the existing configured policy once for one new verified blob."""
    from management.services.ig_private_media_lifecycle import (
        RETENTION_POLICY_VERSION, MIN_RETENTION_SECONDS, MAX_RETENTION_SECONDS,
    )

    try:
        seconds = int(getattr(settings, "IG_PRIVATE_MEDIA_RETENTION_SECONDS", MAX_RETENTION_SECONDS))
    except (TypeError, ValueError):
        seconds = MAX_RETENTION_SECONDS
    seconds = max(MIN_RETENTION_SECONDS, min(seconds, MAX_RETENTION_SECONDS))
    captured_at = timezone.now()
    return {"version": RETENTION_POLICY_VERSION, "retention_seconds": seconds,
        "captured_at": captured_at.isoformat(),
        "delete_after": (captured_at + timedelta(seconds=seconds)).isoformat()}


@dataclass(frozen=True)
class DeleteClaim:
    message_id: int
    token: str
    storage_names: tuple[str, ...]


def _deadline(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if isinstance(value, datetime) and timezone.is_aware(value) else None


def earliest_private_media_deadline(message, *, media=None):
    """Read the existing earliest boundary; never invent or extend retention.

    All parts share one physical purge, so a newer attachment cannot prolong an
    older private blob. Missing/invalid legacy boundaries remain unknown.
    """
    deadlines = [_deadline(getattr(message, "private_media_delete_after", None))]
    parts = media if media is not None else getattr(message, "attachment_media", ())
    for part in parts or ():
        if isinstance(part, dict) and part.get("private_storage") is True:
            deadlines.append(_deadline(part.get("delete_after")))
    return min((value for value in deadlines if value is not None), default=None)


def private_media_expired(message, *, now=None, media=None):
    deadline = earliest_private_media_deadline(message, media=media)
    return deadline is not None and deadline <= (now or timezone.now())


def acquire_blob_use(message_id: int, *, seconds: int = 120) -> str:
    from management.models import InstagramBotMessage

    seconds = max(10, min(int(seconds or 0), USE_LEASE_MAX_SECONDS))
    with transaction.atomic():
        row = InstagramBotMessage.objects.select_for_update().filter(pk=message_id).first()
        if row is None or row.private_media_state in {
            DELETE_PENDING, DELETING, DELETE_FAILED, DELETED,
        }:
            return ""
        # A lock wait can cross expiry. Check a fresh clock after ownership is
        # locked, before creating a lease or reaching filesystem I/O.
        now = timezone.now()
        deadline = earliest_private_media_deadline(row)
        if deadline is None or deadline <= now:
            return ""
        if row.private_media_use_until and row.private_media_use_until > now:
            return ""
        token = secrets.token_hex(16)
        row.private_media_use_token = token
        row.private_media_use_until = now + timedelta(seconds=seconds)
        row.save(update_fields=[
            "private_media_use_token", "private_media_use_until",
        ])
        return token


def release_blob_use(message_id: int, token: str) -> None:
    if not message_id or not token:
        return
    from management.models import InstagramBotMessage

    InstagramBotMessage.objects.filter(
        pk=message_id,
        private_media_use_token=token,
    ).update(private_media_use_token="", private_media_use_until=None)


@contextlib.contextmanager
def blob_use_lease(message_id: int, *, seconds: int = 120):
    token = acquire_blob_use(message_id, seconds=seconds)
    try:
        yield bool(token)
    finally:
        release_blob_use(message_id, token)


def request_deletion(message_ids, *, now=None, immediate: bool = False) -> int:
    from management.models import InstagramBotMessage

    now = now or timezone.now()
    requested = 0
    for message_id in dict.fromkeys(int(value) for value in message_ids if value):
        with transaction.atomic():
            row = InstagramBotMessage.objects.select_for_update().filter(pk=message_id).first()
            if row is None or row.private_media_state == DELETED:
                continue
            if row.private_media_state == DELETING:
                if immediate:
                    row.private_media_delete_after = now
                    row.save(update_fields=["private_media_delete_after"])
                requested += 1
                continue
            row.private_media_state = DELETE_PENDING
            if immediate or not row.private_media_delete_after:
                row.private_media_delete_after = now
            row.private_media_delete_token = ""
            row.private_media_delete_claimed_at = None
            row.save(update_fields=[
                "private_media_state", "private_media_delete_after",
                "private_media_delete_token", "private_media_delete_claimed_at",
            ])
            requested += 1
    return requested


def _prepared_deletion_names(row):
    """Validate durable capture debt without trusting arbitrary JSON paths.

    The existing capture writer binds the target to this message and the
    descriptor hash. Unknown/cross-owner descriptors remain deletion debt.
    """
    from management.models import IgClient
    from management.services.ig_media_recovery import RECOVERY_VERSION, prepared_part_updates

    parts = [part for part in (row.attachment_media or []) if isinstance(part, dict)]
    prepared_parts = [part for part in parts if part.get("prepared_blob")]
    if not prepared_parts:
        return ()
    if (row.role not in {"user", "manager"} or row.source not in {"webhook", "echo"}
        or not row.client_id or not row.sender_id
        or not IgClient.objects.filter(pk=row.client_id, igsid=row.sender_id).exists()):
        raise ValueError("prepared_owner_unknown")
    suffixes = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/heic": ".heic", "image/heif": ".heif", "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3", "audio/m4a": ".m4a", "audio/webm": ".webm",
        "video/mp4": ".mp4", "video/webm": ".webm"}
    names = []
    for part in prepared_parts:
        identity, index = part.get("source_part_id"), part.get("original_index")
        raw = part["prepared_blob"]
        if (not isinstance(identity, str) or not re.fullmatch(r"mp1_[0-9a-f]{32}", identity)
            or type(index) is not int or index < 0
            or sum(item.get("source_part_id") == identity for item in parts) != 1
            or sum(item.get("original_index") == index for item in parts) != 1
            or part.get("provenance") != "live_webhook" or part.get("recovery_version") != RECOVERY_VERSION
            or not isinstance(raw, dict)
            or set(raw) != {"version", "storage_name", "content_hash", "mime", "bytes"}
            or type(raw.get("bytes")) is not int):
            raise ValueError("prepared_part_unknown")
        prepared = prepared_part_updates(raw)["prepared_blob"]
        expected = f"ig_message_media/{row.pk}/{prepared['content_hash'][:32]}{suffixes.get(prepared['mime'], '.bin')}"
        if (raw != prepared or prepared["storage_name"] != expected
            or (part.get("content_hash") and part["content_hash"] != prepared["content_hash"])):
            raise ValueError("prepared_path_binding_unknown")
        names.append(expected)
    return tuple(dict.fromkeys(names))


def claim_deletion(message_id: int, *, now=None) -> DeleteClaim | None:
    from management.models import InstagramBotMessage

    with transaction.atomic():
        row = InstagramBotMessage.objects.select_for_update().filter(pk=message_id).first()
        if row is None or row.private_media_state == DELETED:
            return None
        now = now or timezone.now()
        stale_before = now - timedelta(seconds=DELETE_CLAIM_SECONDS)
        if row.private_media_use_until and row.private_media_use_until > now:
            return None
        # Retry cadence remains owned by DELETE_FAILED; the original part due
        # must not cause a failed deletion to run on every worker cycle.
        due = (row.private_media_delete_after and row.private_media_delete_after <= now
               if row.private_media_state == DELETE_FAILED else private_media_expired(row, now=now))
        reclaim = (
            row.private_media_state == DELETING
            and row.private_media_delete_claimed_at
            and row.private_media_delete_claimed_at <= stale_before
        )
        if not reclaim and (
            row.private_media_state not in {DELETE_PENDING, DELETE_FAILED, ACTIVE, ""}
            or not due
        ):
            return None
        try:
            prepared_names = _prepared_deletion_names(row)
        except (ValueError, TypeError, OverflowError):
            # Never turn an unproven nested path into unlink authority, or
            # claim the whole message has been deleted while debt is unknown.
            row.private_media_state = DELETE_FAILED
            row.private_media_delete_after = now + timedelta(seconds=DELETE_RETRY_SECONDS)
            row.private_media_delete_token = ""
            row.private_media_delete_claimed_at = None
            row.save(update_fields=["private_media_state", "private_media_delete_after",
                                    "private_media_delete_token", "private_media_delete_claimed_at"])
            return None
        token = secrets.token_hex(16)
        row.private_media_state = DELETING
        row.private_media_delete_token = token
        row.private_media_delete_claimed_at = now
        row.save(update_fields=[
            "private_media_state", "private_media_delete_token",
            "private_media_delete_claimed_at",
        ])
        names = tuple(
            dict.fromkeys(
                str(item.get("storage_name") or "")
                for item in (row.attachment_media or [])
                if isinstance(item, dict)
                and item.get("private_storage")
                and item.get("storage_name")
            )
        )
        return DeleteClaim(row.pk, token, tuple(dict.fromkeys((*names, *prepared_names))))


def _finalize(claim: DeleteClaim, *, error: str = "", now=None) -> bool:
    from management.models import InstagramBotMessage

    now = now or timezone.now()
    with transaction.atomic():
        row = InstagramBotMessage.objects.select_for_update().filter(
            pk=claim.message_id,
            private_media_state=DELETING,
            private_media_delete_token=claim.token,
        ).first()
        if row is None:
            return False
        try:
            prepared_names = _prepared_deletion_names(row)
            if not set(prepared_names).issubset(claim.storage_names):
                error = error or "prepared_debt_changed"
        except (ValueError, TypeError, OverflowError):
            error = error or "prepared_debt_unknown"
        if error:
            row.private_media_state = DELETE_FAILED
            row.private_media_delete_after = now + timedelta(seconds=DELETE_RETRY_SECONDS)
            row.private_media_delete_token = ""
            row.private_media_delete_claimed_at = None
            row.save(update_fields=[
                "private_media_state", "private_media_delete_after",
                "private_media_delete_token", "private_media_delete_claimed_at",
            ])
            return False
        media = [dict(item) for item in (row.attachment_media or [])]
        for item in media:
            if isinstance(item, dict) and item.get("prepared_blob"):
                item.pop("prepared_blob", None)
                item["status"] = "expired"
                item["capture_retryable"] = False
                item["capture_terminal"] = True
            if not isinstance(item, dict) or not item.get("private_storage"):
                continue
            item["status"] = "expired"
            for key in (
                "storage_name", "local_url", "private_storage", "delete_after"
            ):
                item.pop(key, None)
        row.attachment_media = media
        row.private_media_state = DELETED
        row.private_media_delete_after = None
        row.private_media_delete_token = ""
        row.private_media_delete_claimed_at = None
        row.private_media_use_token = ""
        row.private_media_use_until = None
        row.save(update_fields=[
            "attachment_media", "private_media_state", "private_media_delete_after",
            "private_media_delete_token", "private_media_delete_claimed_at",
            "private_media_use_token", "private_media_use_until",
        ])
        return True


def delete_claimed_blob(claim: DeleteClaim, *, now=None) -> bool:
    try:
        storage = private_media_storage()
        for name in claim.storage_names:
            if storage.exists(name):
                storage.delete(name)
    except Exception as exc:
        return _finalize(claim, error=type(exc).__name__, now=now)
    return _finalize(claim, now=now)


def reconcile_private_media_deadlines(*, limit=100, can_work=None):
    """Repair one bounded ascending page of legacy extended message deadlines.

    Advance only after every selected row has been inspected successfully under
    its lock. A crash/database failure replays the page; unknown dates are left
    unchanged. The cursor is merely fairness, never authority to delete a blob.
    """
    from management.models import InstagramBotMessage

    if can_work is not None and not can_work():
        return 0
    page_size = max(1, min(int(limit or 0), 500))
    try:
        cursor = max(0, int(cache.get(DEADLINE_CURSOR_KEY, 0) or 0))
    except (TypeError, ValueError):
        cursor = 0
    scope = InstagramBotMessage.objects.filter(private_media_state__in=[ACTIVE, ""]).exclude(attachment_media=[])
    ids = list(scope.filter(pk__gt=cursor).order_by("pk").values_list("pk", flat=True)[:page_size])
    if not ids and cursor:
        ids = list(scope.order_by("pk").values_list("pk", flat=True)[:page_size])
    changed = 0
    for identity in ids:
        if can_work is not None and not can_work():
            return changed
        with transaction.atomic():
            row = InstagramBotMessage.objects.select_for_update().filter(pk=identity).first()
            if can_work is not None and not can_work():
                return changed
            if row is None or row.private_media_state not in {ACTIVE, ""}:
                continue
            deadline = earliest_private_media_deadline(row)
            if deadline is not None and deadline != row.private_media_delete_after:
                row.private_media_delete_after = deadline
                row.save(update_fields=["private_media_delete_after"])
                changed += 1
    if ids:
        # Concurrent duplicate pages may replay work, but must not move a newer
        # observed cursor backwards. Every deadline mutation itself is locked.
        if cache.get(DEADLINE_CURSOR_KEY, 0) == cursor:
            cache.set(DEADLINE_CURSOR_KEY, ids[-1], timeout=7 * 24 * 3600)
    return changed


def purge_due(*, now=None, limit: int = 100, can_work=None) -> int:
    from management.models import InstagramBotMessage

    if can_work is not None and not can_work():
        return 0
    reconcile_private_media_deadlines(limit=limit, can_work=can_work)
    if can_work is not None and not can_work():
        return 0
    now = now or timezone.now()
    ids = list(
        InstagramBotMessage.objects.filter(
            private_media_delete_after__isnull=False,
            private_media_delete_after__lte=now,
        )
        .exclude(private_media_state=DELETED)
        .order_by("private_media_delete_after", "id")
        .values_list("id", flat=True)[: max(1, min(int(limit or 0), 500))]
    )
    deleted = 0
    for message_id in ids:
        if can_work is not None and not can_work():
            break
        claim = claim_deletion(message_id, now=now)
        if can_work is not None and not can_work():
            break
        if claim and delete_claimed_blob(claim, now=now):
            deleted += 1
    return deleted


def delete_immediately(message_ids, *, now=None) -> int:
    now = now or timezone.now()
    ids = list(dict.fromkeys(int(value) for value in message_ids if value))
    request_deletion(ids, now=now, immediate=True)
    deleted = 0
    for message_id in ids:
        claim = claim_deletion(message_id, now=now)
        if claim and delete_claimed_blob(claim, now=now):
            deleted += 1
    return deleted
