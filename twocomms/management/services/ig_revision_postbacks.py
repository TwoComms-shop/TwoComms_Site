"""Source-bound, deterministic postbacks with no model or provider calls."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import re
from typing import Mapping

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from management.models import (
    IgClient, IgCommercialEpisode, IgCustomerTurnRevision, IgDeal, IgFollowUpTask,
    IgLifecycleEvent, IgOrderAssignment, IgOrderAttribution,
    IgSourceActionReceipt,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_revision_actions import _authority_projection
from management.services.ig_revision_authority import (
    CLAIM_PUBLIC_POLICY_INPUTS, build_revision_authority_bindings,
    check_fact_bindings, check_offer_bindings,
)
from management.services.ig_revision_outbox import (
    PublicationBinding, _digest, pre_winner_readiness,
)


RECEIPT_KEY = "postback_decision"
VERSION = "revision-postback-v1"
_CODE = re.compile(r"[a-z0-9_-]{1,32}")


@dataclass(frozen=True)
class RevisionPostbackResult:
    handled: bool = False
    ready: bool = False
    origin: str = "postback"
    reply_text: str = ""
    quick_replies: tuple = ()
    reason: str = "not_handled"
    receipt: dict = field(default_factory=dict)
    replayed: bool = False
    requires_model: bool = False


class _Rollback(Exception):
    pass


def _candidate(snapshot, client_id):
    from management.services.ig_message_templates import parse_payload

    raw = str(snapshot.get("quick_reply_payload") or snapshot.get("text") or "").strip()
    if not raw or len(raw) > 1000:
        return None
    parsed = parse_payload(raw)
    if not parsed or parsed.get("version") != "1":
        return None
    action, args = parsed.get("action"), parsed.get("args") or ()
    if action == "parcel" and len(args) == 2 and args[0] in {"got", "later"}:
        if not re.fullmatch(r"[1-9][0-9]{0,17}", args[1]):
            return None
        return {"action": f"parcel_{args[0]}", "order_id": int(args[1]), "payload_digest": _digest(raw)}
    if action == "diagnostic" and args == ("inout", str(client_id)):
        return {"action": "diagnostic", "order_id": 0, "payload_digest": _digest(raw)}
    if action == "preview" and len(args) == 3 and args[2] == str(client_id) and all(_CODE.fullmatch(value) for value in args[:2]):
        return {"action": "preview", "order_id": 0, "payload_digest": _digest(raw), "variant": args[0], "choice": args[1]}
    return None


def _mixed_sources(snapshots, selected):
    from management.services.bot_sales_classifier import is_reaction_only

    selected_payload = str(selected.get("quick_reply_payload") or selected.get("text") or "").strip()
    for source in snapshots:
        if source.get("role") != "user" or source.get("media_parts"):
            return True
        if source.get("message_id") == selected.get("message_id"):
            continue
        payload = str(source.get("quick_reply_payload") or "").strip()
        if payload and payload != selected_payload:
            return True
        if payload == selected_payload:
            continue
        text = str(source.get("text") or "").strip()
        if text and not is_reaction_only(text):
            return True
    return False


def _action_source_digest(snapshot):
    # Source position can differ in a later bundle; the original customer event
    # identity and content cannot. Capture outcomes are not parcel authority.
    fields = ("message_id", "role", "source_namespace", "provider_message_id", "synthetic_event_key", "text", "provider_created_at", "reply_to_provider_message_id", "quick_reply_payload", "referral")
    return _digest({key: snapshot.get(key) for key in fields})


def _owned_order(client, order_id):
    """An explicit current assignment wins; unlinked/foreign rows never fall back."""
    from orders.models import Order

    order = Order.objects.select_for_update().filter(pk=order_id).first()
    if order is None:
        raise _Rollback("postback_order_not_owned")
    assignment = IgOrderAssignment.objects.select_for_update().filter(order_id=order_id).first()
    if assignment is not None:
        if assignment.client_id != client.pk or assignment.unassigned_at is not None:
            raise _Rollback("postback_order_not_owned")
        return order, {"kind": "assignment", "id": assignment.pk, "version": assignment.version, "order_id": order.pk, "client_id": client.pk}
    from management.services.ig_order_assignments import _legacy_owner_conflict

    if _legacy_owner_conflict(order_id, client.pk):
        raise _Rollback("postback_order_not_owned")
    deals = list(IgDeal.objects.select_for_update().filter(order_id=order_id).order_by("pk").values("pk", "client_id")[:33])
    episodes = list(IgCommercialEpisode.objects.select_for_update().filter(intended_order_id=order_id).values("pk", "client_id")[:33])
    attribution = IgOrderAttribution.objects.select_for_update().filter(order_id=order_id).values("pk", "client_id").first()
    rows = [*deals, *episodes, *([attribution] if attribution else [])]
    if not rows or len(deals) > 32 or len(episodes) > 32 or any(row["client_id"] != client.pk for row in rows):
        raise _Rollback("postback_order_not_owned")
    return order, {
        "kind": "commercial_links", "order_id": order.pk, "client_id": client.pk,
        "deal_ids": [row["pk"] for row in deals],
        "episode_ids": [row["pk"] for row in episodes],
        "attribution_id": attribution["pk"] if attribution else 0,
    }


def _copy(action, language):
    copies = {
        "parcel_got": {
            "uk": "Дякую, що повідомили! Заплановані нагадування про цю посилку скасовано.",
            "ru": "Спасибо, что сообщили! Запланированные напоминания об этой посылке отменены.",
            "en": "Thanks for letting me know! Scheduled reminders about this parcel have been cancelled.",
        },
        "parcel_later": {
            "uk": "Добре, нагадування про цю посилку заплановано.",
            "ru": "Хорошо, напоминание об этой посылке запланировано.",
            "en": "A reminder about this parcel has been scheduled.",
        },
        "diagnostic": {
            "uk": "Тестове натискання отримано ✅",
            "ru": "Тестовое нажатие получено ✅",
            "en": "Test button tap received ✅",
        },
        "preview": {
            "uk": "Демо-натискання отримано ✅ Налаштування не змінено.",
            "ru": "Демо-нажатие получено ✅ Настройки не изменены.",
            "en": "Preview tap received ✅ No settings were changed.",
        },
        "reminder": {
            "uk": "Нагадування про посилку. Якщо вже забрали її — повідомте, будь ласка.",
            "ru": "Напоминание о посылке. Если уже забрали её — сообщите, пожалуйста.",
            "en": "A reminder about your parcel. If you have already picked it up, please let us know.",
        },
    }
    return copies[action][language]


def _apply_parcel(client, source, candidate, language, now):
    order, proof = _owned_order(client, candidate["order_id"])
    reason = f"parcel_reminder:{order.pk}"
    if candidate["action"] == "parcel_got":
        tasks = IgFollowUpTask.objects.filter(
            client=client, reason=reason, status=IgFollowUpTask.Status.PENDING,
        )
        # A stale or inconsistent deal pointer must not let a button act on a
        # different order, even if a legacy reason string was copied.
        tasks = tasks.filter(Q(deal__isnull=True) | Q(deal__client=client, deal__order_id=order.pk))
        task_ids = list(tasks.select_for_update().values_list("pk", flat=True)[:129])
        if len(task_ids) > 128:
            raise _Rollback("postback_reminder_limit")
        tasks.update(status=IgFollowUpTask.Status.CANCELLED, skip_reason="customer_confirmed_pickup", updated_at=now)
        events = IgLifecycleEvent.objects.filter(
            client=client, order=order, kind=IgLifecycleEvent.Kind.PARCEL_ARRIVED,
            state__in=(IgLifecycleEvent.State.PENDING, IgLifecycleEvent.State.WAITING_WINDOW),
        )
        event_ids = list(events.select_for_update().values_list("pk", flat=True)[:129])
        if len(event_ids) > 128:
            raise _Rollback("postback_reminder_limit")
        events.update(state=IgLifecycleEvent.State.CANCELLED, last_error="customer_confirmed_pickup", updated_at=now)
        return {"ownership": proof, "customer_assertion": "parcel_picked_up", "cancelled_task_ids": task_ids, "cancelled_event_ids": event_ids}
    from management.services.ig_postback_router import REMIND_LATER_DELAY_HOURS
    from management.services.ig_ai_reply_recovery import RESPONSE_WINDOW

    anchor = source.provider_created_at or source.message.created_at
    due_at = anchor + timedelta(hours=REMIND_LATER_DELAY_HOURS)
    window_deadline = anchor + RESPONSE_WINDOW
    if due_at <= now or due_at >= window_deadline:
        raise _Rollback("postback_reminder_window_unavailable")
    key = f"revision-parcel:{client.pk}:{source.message_id}:{order.pk}"
    existing = IgFollowUpTask.objects.select_for_update().filter(event_key=key).first()
    if existing is None:
        # A new customer request replaces still-pending reminders for this exact
        # order, while a replay never moves the previously anchored due time.
        pending = IgFollowUpTask.objects.filter(
            client=client, reason=reason, status=IgFollowUpTask.Status.PENDING,
        ).filter(Q(deal__isnull=True) | Q(deal__client=client, deal__order_id=order.pk))
        pending_ids = list(pending.select_for_update().values_list("pk", flat=True)[:129])
        if len(pending_ids) > 128:
            raise _Rollback("postback_reminder_limit")
        pending.update(
            status=IgFollowUpTask.Status.CANCELLED, skip_reason="customer_requested_later", updated_at=now,
        )
        existing = IgFollowUpTask.objects.create(
            client=client, kind=IgFollowUpTask.Kind.MANAGER_TASK,
            reason=reason, due_at=due_at, status=IgFollowUpTask.Status.PENDING,
            trigger=IgFollowUpTask.Trigger.REACTIVE, event_key=key,
            event_occurred_at=anchor, policy_started_at=anchor,
            meta_window_deadline=window_deadline,
            event_payload={"origin": "postback", "source_message_id": source.message_id, "order_id": order.pk, "payload_digest": candidate["payload_digest"]},
            message_text=_copy("reminder", language),
        )
    elif existing.client_id != client.pk or existing.due_at != due_at or existing.reason != reason or existing.event_payload.get("payload_digest") != candidate["payload_digest"]:
        raise _Rollback("postback_reminder_identity_mismatch")
    return {"ownership": proof, "reminder_id": existing.pk, "due_at": due_at.isoformat(), "event_key": key}


def apply_revision_postback(
    revision_id, token, *, source_message_id, settings_id,
    settings_permission_epoch, publication: PublicationBinding, now=None,
):
    """Apply one exact source action and record its deterministic crash receipt."""
    if connection.in_atomic_block:
        return RevisionPostbackResult(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionPostbackResult(reason="revision_missing")
    now = now or timezone.now()
    handled = False
    try:
        with transaction.atomic():
            settings_row = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=settings_id).first()
            client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
            revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
            if settings_row is None or client is None or revision is None:
                raise _Rollback("postback_identity_missing")
            if not revision.snapshot_digest or _digest(revision.bundle_snapshot) != revision.snapshot_digest:
                raise _Rollback("revision_snapshot_invalid")
            snapshots = revision.bundle_snapshot.get("sources") or []
            selected = next((row for row in snapshots if row.get("message_id") == source_message_id), None)
            if selected is None or selected.get("role") != "user":
                raise _Rollback("postback_source_missing")
            candidate = _candidate(selected, client.pk)
            if candidate is None:
                return RevisionPostbackResult()
            requires_model = _mixed_sources(snapshots, selected)
            handled = True
            source = revision.sources.select_for_update().select_related("message").filter(message_id=source_message_id, message__client_id=client.pk, message__role=InstagramBotMessage.Role.USER).first()
            if source is None or source.source_digest != selected.get("source_digest") or source.text != selected.get("text") or source.quick_reply_payload != selected.get("quick_reply_payload"):
                raise _Rollback("postback_source_changed")
            authority = build_revision_authority_bindings(client, claims=(CLAIM_PUBLIC_POLICY_INPUTS,), settings_obj=settings_row)
            if not authority.ready:
                raise _Rollback("postback_authority_unavailable")
            readiness = pre_winner_readiness(
                revision.pk, token, settings_id=settings_id,
                settings_permission_epoch=settings_permission_epoch, publication=publication,
                fact_bindings=authority.fact_bindings, offer_bindings=authority.offer_bindings,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now,
            )
            if not readiness.ready:
                raise _Rollback(readiness.reasons[0])
            binding = {
                "version": VERSION, "origin": "postback", "snapshot_digest": revision.snapshot_digest,
                "source_message_id": source_message_id, "source_digest": source.source_digest,
                "settings_id": settings_id, "settings_permission_epoch": settings_permission_epoch,
                "publication": {"id": publication.publication_id, "version": publication.version, "hash": publication.snapshot_hash},
                "authority": _authority_projection(authority), **candidate,
            }
            global_receipt = IgSourceActionReceipt.objects.select_for_update().filter(source_message_id=source_message_id, kind="postback").first()
            replayed = global_receipt is not None
            if global_receipt is not None:
                if (global_receipt.client_id != client.pk or global_receipt.source_digest != _action_source_digest(selected)
                    or global_receipt.payload_digest != candidate["payload_digest"]
                    or global_receipt.outcome_digest != _digest(global_receipt.outcome)):
                    raise _Rollback("postback_source_receipt_mismatch")
                outcome = dict(global_receipt.outcome)
                if candidate["order_id"]:
                    _order, proof = _owned_order(client, candidate["order_id"])
                    if outcome.get("ownership") != proof:
                        raise _Rollback("postback_order_ownership_changed")
            else:
                language = str(client.language or "uk").casefold()
                language = language if language in {"uk", "ru", "en"} else "uk"
                effect = _apply_parcel(client, source, candidate, language, now) if candidate["order_id"] else {}
                outcome = {**candidate, **effect, "reply_text": _copy(candidate["action"], language), "language": language, "recorded_at": now.isoformat()}
                global_receipt = IgSourceActionReceipt.objects.create(
                    client=client, source_message_id=source_message_id, kind="postback",
                    source_digest=_action_source_digest(selected), payload_digest=candidate["payload_digest"],
                    outcome=outcome, outcome_digest=_digest(outcome), first_revision_id=revision.pk,
                )
            receipt = {**binding, **outcome, "source_action_receipt_id": global_receipt.pk}
            if len(str(receipt)) > 64 * 1024:
                raise _Rollback("postback_receipt_too_large")
            manifest = {
                "version": VERSION, "origin": "postback", "snapshot_digest": revision.snapshot_digest,
                "settings_id": settings_id, "settings_permission_epoch": settings_permission_epoch,
                "publication": binding["publication"],
                "sources": [{"source_message_id": row["message_id"], "source_digest": _action_source_digest(row), **value}
                            for row in snapshots if (value := _candidate(row, client.pk)) is not None],
            }
            previous = (revision.action_receipts or {}).get(RECEIPT_KEY)
            if previous is None:
                revision.action_receipts = {**(revision.action_receipts or {}), RECEIPT_KEY: manifest}
                revision.save(update_fields=["action_receipts", "updated_at"])
            elif previous != manifest:
                raise _Rollback("postback_manifest_changed")
            return RevisionPostbackResult(True, True, reply_text=receipt["reply_text"], reason=candidate["action"], receipt=receipt, replayed=replayed, requires_model=requires_model)
    except _Rollback as exc:
        return RevisionPostbackResult(handled=handled, reason=str(exc))
    except Exception:
        return RevisionPostbackResult(handled=handled, reason="postback_failed")
