"""Bounded, deterministic reconciliation for pre-durable Instagram opt-outs."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from management.ig_bot_models import (
    IgClient,
    IgConversationAnalysisSnapshot,
)
from management.models import InstagramBotMessage, InstagramBotSettings
from management.services.bot_followups import cancel_pending
from management.services.bot_sales_classifier import is_explicit_opt_out


def _latest_explicit_opt_out(client: IgClient):
    messages = InstagramBotMessage.objects.filter(
        client_id=client.pk,
        role=InstagramBotMessage.Role.USER,
    ).order_by("-created_at", "-id")
    for message in messages.iterator(chunk_size=100):
        if is_explicit_opt_out(message.text):
            return message
    return None


def reconcile_opt_out_backfill(*, limit: int = 100, now=None, dry_run: bool = False) -> dict:
    """Apply only explicit historical consent withdrawals, with a durable cursor."""
    now = now or timezone.now()
    bounded_limit = max(1, min(int(limit), 500))
    settings_obj = InstagramBotSettings.load()
    cursor = int(settings_obj.opt_out_backfill_cursor or 0)
    base = IgClient.objects.order_by("pk")
    clients = list(base.filter(pk__gt=cursor)[:bounded_limit])
    if not clients and cursor:
        cursor = 0
        clients = list(base[:bounded_limit])

    result = {
        "scanned": len(clients),
        "updated": 0,
        "already_opted_in": 0,
        "skipped_existing": 0,
        "ambiguous": 0,
        "cursor_from": cursor,
        "cursor_next": 0,
        "dry_run": bool(dry_run),
    }
    for client in clients:
        message = _latest_explicit_opt_out(client)
        snapshot_exists = IgConversationAnalysisSnapshot.objects.filter(
            client_id=client.pk,
            analysis_model="rules",
            interaction_type=IgConversationAnalysisSnapshot.InteractionType.OPT_OUT,
        ).exists()
        if message is None:
            if snapshot_exists:
                result["ambiguous"] += 1
            continue
        if client.opted_in_at and client.opted_in_at >= message.created_at:
            result["already_opted_in"] += 1
            continue
        active_opt_out = bool(
            client.opted_out_at
            and (not client.opted_in_at or client.opted_in_at < client.opted_out_at)
        )
        if active_opt_out and client.opt_out_message_id == message.pk:
            result["skipped_existing"] += 1
            continue
        result["updated"] += 1
        if dry_run:
            continue
        with transaction.atomic():
            locked = IgClient.objects.select_for_update().get(pk=client.pk)
            latest = _latest_explicit_opt_out(locked)
            if latest is None:
                continue
            if locked.opted_in_at and locked.opted_in_at >= latest.created_at:
                result["updated"] -= 1
                result["already_opted_in"] += 1
                continue
            active = bool(
                locked.opted_out_at
                and (not locked.opted_in_at or locked.opted_in_at < locked.opted_out_at)
            )
            if active and locked.opt_out_message_id == latest.pk:
                result["updated"] -= 1
                result["skipped_existing"] += 1
                continue
            opted_out_at = latest.created_at or now
            locked.opted_out_at = opted_out_at
            locked.opt_out_message_id = latest.pk
            locked.bot_paused = True
            locked.paused_reason = "opt_out"
            locked.paused_at = locked.paused_at or opted_out_at
            locked.save(update_fields=[
                "opted_out_at",
                "opt_out_message_id",
                "bot_paused",
                "paused_reason",
                "paused_at",
                "updated_at",
            ])
            cancel_pending(locked, reason="opt_out")

    if clients:
        result["cursor_next"] = int(clients[-1].pk) if len(clients) >= bounded_limit else 0
        if not dry_run:
            InstagramBotSettings.objects.filter(pk=settings_obj.pk).update(
                opt_out_backfill_cursor=result["cursor_next"]
            )
    return result
