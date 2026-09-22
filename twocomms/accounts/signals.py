"""Admin Telegram notifications for new user registrations.

Owner request (2026-06-11): notify admins about EVERY new account,
regardless of the registration path (site form, Google OAuth,
Telegram login).

Implementation: ``post_save`` on ``User`` with ``created=True`` covers
all paths uniformly. The signal writes an idempotent outbox intent as a
best-effort fast path, while the existing drain owner reconciles missed
intents from a durable cursor. The outbox worker resolves the user and
social-auth association immediately before delivery, so the signup method
is not guessed in the request.
"""

from datetime import UTC, datetime, timedelta
from html import escape

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

_PROVIDER_LABELS = {
    "google-oauth2": "Google",
    "telegram": "Telegram",
}

_SOCIAL_AUTH_ASSOCIATION_DELAY_SECONDS = 5
REGISTRATION_RECONCILE_INITIAL_WINDOW = 100
REGISTRATION_RECONCILE_RECENT_MINUTES = 15
_REGISTRATION_RECONCILE_CHECKPOINT_KEY = (
    "internal:registration-reconcile-checkpoint"
)
_REGISTRATION_RECONCILE_CUTOFF_KEY = "bootstrap_recent_cutoff_us"


def _detect_method(user_id):
    try:
        from social_django.models import UserSocialAuth

        social = (
            UserSocialAuth.objects.filter(user_id=user_id)
            .values_list("provider", flat=True)
            .first()
        )
        if social:
            return _PROVIDER_LABELS.get(social, social)
    except Exception:
        pass
    return "сайт (email/пароль)"


def registration_notification_text(user_id):
    """Build registration text from committed rows at delivery time.

    The outbox payload intentionally contains only ``user_id``. This keeps
    personal data out of deferred task metadata and lets social-auth finish
    associating the provider before the method is classified.
    """
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return None
    method = _detect_method(user.pk)
    total = User.objects.count()
    username = escape(user.username or "—")
    email = escape(user.email or "—")
    method = escape(method)
    return (
        "👤 <b>Нова реєстрація на сайті</b>\n"
        f"Користувач: <b>{username}</b>\n"
        f"Email: {email}\n"
        f"Спосіб: {method}\n"
        f"Всього акаунтів: {total}"
    )


def _notify_admins(user_id, *_legacy_args, raise_errors=False):
    try:
        from management.services.instagram_bot import notify_manager

        queued = notify_manager(
            "registration pending",
            dedupe_key=f"registration:{int(user_id)}",
            event_type="registration",
            metadata={
                "transport": "site_registration",
                "registration_user_id": str(user_id),
            },
            deliver_immediately=False,
            not_before_seconds=_SOCIAL_AUTH_ASSOCIATION_DELAY_SECONDS,
            raise_on_error=raise_errors,
        )
        if raise_errors and not queued:
            raise RuntimeError("registration notification persistence failed")
        return bool(queued)
    except Exception:
        if raise_errors:
            raise
        # Notifications must never break registration.
        return False


def reconcile_registration_notification_intents(*, limit=20):
    """Recover a bounded batch of registration intents missed by fast path.

    The checkpoint lives in the existing notification outbox, so process or
    cache restarts cannot reset it. The first pass considers only the newest
    bounded window; later passes advance monotonically by scalar User PK.
    """
    if getattr(settings, "TESTING", False):
        return 0

    from management.models import IgBotNotification

    try:
        requested_limit = int(limit)
    except (TypeError, ValueError):
        requested_limit = 20
    if requested_limit <= 0:
        return 0
    batch_limit = min(requested_limit, 500)

    notification_error = None
    try:
        checkpoint, _ = IgBotNotification.objects.get_or_create(
            dedupe_key=_REGISTRATION_RECONCILE_CHECKPOINT_KEY,
            defaults={
                "event_type": "registration_reconcile_checkpoint",
                "payload": {},
                "status": IgBotNotification.Status.RESOLVED,
            },
        )
        with transaction.atomic():
            checkpoint = IgBotNotification.objects.select_for_update().get(
                pk=checkpoint.pk
            )
            checkpoint_update_fields = []
            if checkpoint.event_type != "registration_reconcile_checkpoint":
                checkpoint.event_type = "registration_reconcile_checkpoint"
                checkpoint_update_fields.append("event_type")
            if checkpoint.status != IgBotNotification.Status.RESOLVED:
                checkpoint.status = IgBotNotification.Status.RESOLVED
                checkpoint_update_fields.append("status")
            if checkpoint.next_attempt_at is not None:
                checkpoint.next_attempt_at = None
                checkpoint_update_fields.append("next_attempt_at")
            if checkpoint_update_fields:
                checkpoint.save(
                    update_fields=[*checkpoint_update_fields, "updated_at"]
                )
            payload = checkpoint.payload if isinstance(checkpoint.payload, dict) else {}
            current_max_user_id = (
                User.objects.order_by("-pk")
                .values_list("pk", flat=True)
                .first()
                or 0
            )

            def new_bootstrap_state():
                newest_ids = list(
                    User.objects.order_by("-pk").values_list(
                        "pk", flat=True
                    )[:REGISTRATION_RECONCILE_INITIAL_WINDOW]
                )
                initial_cursor = min(newest_ids) - 1 if newest_ids else 0
                high_watermark = max(newest_ids) if newest_ids else 0
                recent_cutoff = timezone.now() - timedelta(
                    minutes=REGISTRATION_RECONCILE_RECENT_MINUTES
                )
                recent_cutoff_us = int(recent_cutoff.timestamp() * 1_000_000)
                return initial_cursor, high_watermark, recent_cutoff_us

            try:
                last_user_id = payload["last_user_id"]
                if type(last_user_id) is not int:
                    raise ValueError
                if last_user_id < 0 or last_user_id > current_max_user_id:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                (
                    initial_cursor,
                    bootstrap_until_user_id,
                    bootstrap_recent_cutoff_us,
                ) = new_bootstrap_state()
                last_user_id = initial_cursor
                checkpoint.payload = {
                    "last_user_id": initial_cursor,
                    "bootstrap_until_user_id": bootstrap_until_user_id,
                    _REGISTRATION_RECONCILE_CUTOFF_KEY: bootstrap_recent_cutoff_us,
                }
                checkpoint.event_type = "registration_reconcile_checkpoint"
                checkpoint.status = IgBotNotification.Status.RESOLVED
                checkpoint.save(
                    update_fields=["payload", "event_type", "status", "updated_at"]
                )
                payload = checkpoint.payload

            bootstrap_key_present = "bootstrap_until_user_id" in payload
            try:
                bootstrap_until_user_id = payload["bootstrap_until_user_id"]
                if type(bootstrap_until_user_id) is not int:
                    raise ValueError
                if not (
                    last_user_id
                    < bootstrap_until_user_id
                    <= current_max_user_id
                ):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                if not bootstrap_key_present:
                    bootstrap_until_user_id = None
                else:
                    (
                        initial_cursor,
                        bootstrap_until_user_id,
                        bootstrap_recent_cutoff_us,
                    ) = new_bootstrap_state()
                    last_user_id = initial_cursor
                    checkpoint.payload = {
                        "last_user_id": initial_cursor,
                        "bootstrap_until_user_id": bootstrap_until_user_id,
                        _REGISTRATION_RECONCILE_CUTOFF_KEY: bootstrap_recent_cutoff_us,
                    }
                    checkpoint.save(update_fields=["payload", "updated_at"])
                    payload = checkpoint.payload

            bootstrap_recent_cutoff = None
            if bootstrap_until_user_id is not None:
                try:
                    bootstrap_recent_cutoff_us = payload[
                        _REGISTRATION_RECONCILE_CUTOFF_KEY
                    ]
                    current_time_us = int(timezone.now().timestamp() * 1_000_000)
                    if (
                        type(bootstrap_recent_cutoff_us) is not int
                        or bootstrap_recent_cutoff_us <= 0
                        or bootstrap_recent_cutoff_us > current_time_us
                    ):
                        raise ValueError
                    bootstrap_recent_cutoff = datetime.fromtimestamp(
                        bootstrap_recent_cutoff_us / 1_000_000,
                        tz=UTC,
                    )
                except (KeyError, OSError, OverflowError, TypeError, ValueError):
                    (
                        initial_cursor,
                        bootstrap_until_user_id,
                        bootstrap_recent_cutoff_us,
                    ) = new_bootstrap_state()
                    last_user_id = initial_cursor
                    checkpoint.payload = {
                        "last_user_id": initial_cursor,
                        "bootstrap_until_user_id": bootstrap_until_user_id,
                        _REGISTRATION_RECONCILE_CUTOFF_KEY: bootstrap_recent_cutoff_us,
                    }
                    checkpoint.save(update_fields=["payload", "updated_at"])
                    bootstrap_recent_cutoff = datetime.fromtimestamp(
                        bootstrap_recent_cutoff_us / 1_000_000,
                        tz=UTC,
                    )

            users = User.objects.filter(pk__gt=last_user_id)
            if bootstrap_until_user_id is not None:
                users = users.filter(pk__lte=bootstrap_until_user_id)
            users = list(
                users
                .order_by("pk")
                .values_list("pk", "date_joined")[:batch_limit]
            )
            reconciled = 0
            for user_id, date_joined in users:
                should_notify = (
                    bootstrap_until_user_id is None
                    or date_joined >= bootstrap_recent_cutoff
                )
                if should_notify:
                    try:
                        _notify_admins(user_id, raise_errors=True)
                    except Exception as exc:
                        notification_error = exc
                        break
                next_payload = {"last_user_id": int(user_id)}
                if (
                    bootstrap_until_user_id is not None
                    and user_id < bootstrap_until_user_id
                ):
                    next_payload["bootstrap_until_user_id"] = bootstrap_until_user_id
                    next_payload[
                        _REGISTRATION_RECONCILE_CUTOFF_KEY
                    ] = bootstrap_recent_cutoff_us
                checkpoint.payload = next_payload
                checkpoint.save(update_fields=["payload", "updated_at"])
                reconciled += int(should_notify)
            if (
                not users
                and bootstrap_until_user_id is not None
                and last_user_id < bootstrap_until_user_id
            ):
                checkpoint.payload = {
                    "last_user_id": bootstrap_until_user_id
                }
                checkpoint.save(update_fields=["payload", "updated_at"])
        if notification_error is not None:
            raise notification_error
        return reconciled
    except Exception:
        # The drain owner is the fail-open boundary. Re-raise here so it can
        # record one redacted operational event and continue its own queue.
        raise


@receiver(post_save, sender=User, dispatch_uid="notify_admins_new_user")
def notify_admins_on_registration(sender, instance, created, **kwargs):
    if not created or getattr(settings, "TESTING", False):
        return
    # Best-effort fast path; the bounded drain reconciler recovers a missed
    # intent when the User and outbox tables cannot share a transaction.
    # Delivery and provider lookup remain deferred to the notification worker.
    _notify_admins(instance.pk)
