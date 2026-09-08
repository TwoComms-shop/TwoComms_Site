"""Revision-bound manager cases and deferred technical notifications.

These are local intent writes. The existing notification worker owns delivery;
neither helper calls a provider or changes a customer's commercial stage.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import re

from django.db import connection, transaction
from django.utils import timezone

from management.models import (
    IgBotNotification, IgClient, IgCustomerTurnRevision, IgFollowUpTask,
    InstagramBotSettings,
)
from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings
from management.services.ig_revision_outbox import PublicationBinding, pre_winner_readiness


MANAGER_ACTION = "manager_escalation_intent"
MANAGER_RECEIPT = "manager_handoff"
RATE_RECEIPT = "rate_alert"
_MANAGER_REQUEST = re.compile(r"\b(?:менеджер\w*|оператор\w*|manager|human\s+(?:agent|support)|speak\s+to\s+(?:a\s+)?human)\b", re.I)
_MANAGER_PROMISE = re.compile(r"(?:переда[мю]|передаю|покличу|запрошу|передал\w*|передан\w*|передамо|залуч\w*|подключ\w*|позов\w*|refer|forward|connect)[^.!?\n]{0,65}(?:менеджер\w*|команд\w*|спеціаліст\w*|специалист\w*|manager|team|specialist)", re.I)


@dataclass(frozen=True)
class RevisionIntentResult:
    ready: bool = False
    reason: str = ""
    task_id: int = 0
    notification_id: int = 0
    replayed: bool = False


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def manager_case_reason(revision, response=None):
    from management.services.bot_sales_classifier import SUPPORT_RE, is_explicit_custom_print_request

    texts = [str(row.get("text") or "") for row in revision.bundle_snapshot.get("sources", ()) if row.get("role") == "user"]
    control = response.control if response is not None else {}
    if any(is_explicit_custom_print_request(text) for text in texts) or (
        (control.get("paylink") or control.get("payment"))
        and "custom" in str(revision.client.intent or "").casefold()
    ):
        return "custom_print"
    if any(_MANAGER_REQUEST.search(text) for text in texts):
        return "customer_manager_request"
    if response is not None and (control.get("manager") or _MANAGER_PROMISE.search(response.reply_text)) and any(SUPPORT_RE.search(text) for text in texts):
        return "business_review"
    return ""


def manager_handoff_promised(response):
    return bool(response is not None and _MANAGER_PROMISE.search(response.reply_text))


def _readiness(revision, token, settings_row, authority, epoch, publication):
    return pre_winner_readiness(
        revision.pk, token, settings_id=settings_row.pk,
        settings_permission_epoch=epoch, publication=publication,
        fact_bindings=authority.get("fact_bindings") or (),
        offer_bindings=authority.get("offer_bindings") or (),
        fact_checker=check_fact_bindings, offer_checker=check_offer_bindings,
    )


def ensure_revision_manager_case(revision_id, token, *, settings_id):
    if connection.in_atomic_block:
        return RevisionIntentResult(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionIntentResult(reason="revision_missing")
    from management.services.ig_response_control import ResponseControl, ValidatedResponse
    from management.services.ig_alerts import format_operator_alert
    from management.services.instagram_bot import notify_manager

    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if settings_row is None or client is None or revision is None:
            return RevisionIntentResult(reason="intent_identity_missing")
        proposal = revision.generation_proposal or {}
        if not revision.generation_proposal_digest or _digest(proposal) != revision.generation_proposal_digest:
            return RevisionIntentResult(reason="generation_proposal_changed")
        authority = proposal.get("authority") or {}
        if MANAGER_ACTION not in (authority.get("allowed_actions") or ()):
            return RevisionIntentResult(reason="manager_action_not_authorized")
        execution = proposal.get("execution_binding") or {}
        pub = (proposal.get("policy_manifest") or {}).get("instruction_publication") or {}
        if execution.get("settings_id") != settings_id or not all(key in pub for key in ("id", "version", "hash")):
            return RevisionIntentResult(reason="proposal_execution_binding_missing")
        selection = (revision.action_receipts or {}).get("client_configuration_update") or {}
        effective_authority = selection.get("after_authority") or authority
        ready = _readiness(revision, token, settings_row, effective_authority, execution["settings_permission_epoch"], PublicationBinding(pub["id"], pub["version"], pub["hash"]))
        if not ready.ready:
            return RevisionIntentResult(reason=ready.reasons[0])
        existing = (revision.action_receipts or {}).get(MANAGER_RECEIPT)
        if existing:
            task = IgFollowUpTask.objects.filter(pk=existing.get("task_id"), client=client).first()
            notification = IgBotNotification.objects.filter(pk=existing.get("notification_id"), client=client).first()
            if existing.get("generation_proposal_digest") != revision.generation_proposal_digest or task is None or notification is None:
                return RevisionIntentResult(reason="manager_receipt_invalid")
            return RevisionIntentResult(True, "already_recorded", task.pk, notification.pk, True)
        stored = proposal.get("response") or {}
        response = ValidatedResponse(reply_text=stored.get("reply_text") or "", controls=tuple(ResponseControl(item["kind"], item["value"]) for item in stored.get("controls") or ()))
        revision.client = client
        reason = manager_case_reason(revision, response)
        if not reason:
            return RevisionIntentResult(reason="manager_case_not_requested")
        task_reason = "revision_case:custom_print" if reason == "custom_print" else "revision_case:manager_handoff"
        task = IgFollowUpTask.objects.select_for_update().filter(client=client, kind=IgFollowUpTask.Kind.MANAGER_TASK, reason=task_reason).exclude(status__in=(IgFollowUpTask.Status.COMPLETED, IgFollowUpTask.Status.CANCELLED)).order_by("id").first()
        now = timezone.now()
        source_refs = [{"message_id": row["message_id"], "source_digest": row["source_digest"]} for row in proposal.get("sources", ())]
        if task is None:
            task = IgFollowUpTask.objects.create(
                client=client, due_at=now, status=IgFollowUpTask.Status.SKIPPED,
                kind=IgFollowUpTask.Kind.MANAGER_TASK, reason=task_reason,
                manager_approval_status=IgFollowUpTask.ManagerApprovalStatus.PENDING,
                manager_approval_requested_at=now,
                message_text=("Клієнт просить власний принт: перевірити макет, можливість і вартість." if reason == "custom_print" else "Клієнту потрібна допомога команди: відкрийте поточну розмову."),
                event_key=f"ig-revision-case:{client.pk}:{revision.pk}:{reason}"[:180],
                trigger=IgFollowUpTask.Trigger.EVENT, event_occurred_at=now,
                skip_reason="human_business_decision_required", policy_started_at=now,
                policy_version="revision-case-v1",
            )
        context = dict(task.manager_context or {})
        previous = list(context.get("sources") or [])
        known = {item["message_id"] for item in previous}
        previous.extend(item for item in source_refs if item["message_id"] not in known)
        context.update({
            "schema_version": 1, "case_kind": reason, "sources": previous[-64:],
            "latest_revision_id": revision.pk, "generation_proposal_digest": revision.generation_proposal_digest,
            "required_decisions": (["design_feasibility", "quote_approval"] if reason == "custom_print" else ["customer_request"]),
            "authority": {"price_confirmed": False, "fulfillment_started": False},
        })
        task.manager_context = context
        task.save(update_fields=["manager_context", "updated_at"])
        notification_key = f"ig-revision-case:{task.pk}"
        notified = notify_manager(
            format_operator_alert("Клієнту потрібна допомога команди", event_type="escalation", client_id=client.pk, task_id=task.pk, counts={"sources": len(previous)}, instruction_code="escalation"),
            dedupe_key=notification_key, event_type="escalation", client=client,
            metadata={"revision_id": revision.pk, "manager_task_id": task.pk, "case_kind": reason},
            deliver_immediately=False, raise_on_error=True,
        )
        if not notified:
            raise RuntimeError("manager_notification_not_recorded")
        notification = IgBotNotification.objects.get(dedupe_key=notification_key)
        revision.action_receipts = {**(revision.action_receipts or {}), MANAGER_RECEIPT: {
            "task_id": task.pk, "notification_id": notification.pk, "case_kind": reason,
            "snapshot_digest": revision.snapshot_digest, "generation_proposal_digest": revision.generation_proposal_digest,
            "recorded_at": now.isoformat(),
        }}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return RevisionIntentResult(True, "recorded", task.pk, notification.pk)


def ensure_revision_rate_alert(revision_id, token, *, settings_id):
    if connection.in_atomic_block:
        return RevisionIntentResult(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionIntentResult(reason="revision_missing")
    from management.services.ig_alerts import format_technical_alert
    from management.services.instagram_bot import notify_manager

    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if settings_row is None or client is None or revision is None:
            return RevisionIntentResult(reason="intent_identity_missing")
        receipt = (revision.action_receipts or {}).get("input_decision") or {}
        if receipt.get("reason") != "rate_limited" or receipt.get("snapshot_digest") != revision.snapshot_digest:
            return RevisionIntentResult(reason="rate_alert_not_requested")
        pub = receipt["publication"]
        ready = _readiness(revision, token, settings_row, receipt["authority"], receipt["settings_permission_epoch"], PublicationBinding(pub["id"], pub["version"], pub["hash"]))
        if not ready.ready:
            return RevisionIntentResult(reason=ready.reasons[0])
        existing = (revision.action_receipts or {}).get(RATE_RECEIPT)
        if existing:
            return RevisionIntentResult(True, "already_recorded", notification_id=existing["notification_id"], replayed=True)
        now = timezone.now()
        notification = IgBotNotification.objects.filter(client=client, event_type="sender_rate_limited", created_at__gte=now - timedelta(hours=1)).order_by("id").first()
        if notification is None:
            key = f"ig-revision-rate:{client.pk}:{revision.pk}"
            notify_manager(
                format_technical_alert("Перевищено частоту повідомлень", event_type="sender_rate_limited", client_id=client.pk, message_id=receipt["source_message_ids"][-1], failure_kind="possible_spam", instruction_code="sender_rate_limited"),
                dedupe_key=key, event_type="sender_rate_limited", client=client,
                metadata={"revision_id": revision.pk}, deliver_immediately=False, raise_on_error=True,
            )
            notification = IgBotNotification.objects.get(dedupe_key=key)
        revision.action_receipts = {**(revision.action_receipts or {}), RATE_RECEIPT: {
            "notification_id": notification.pk, "snapshot_digest": revision.snapshot_digest, "recorded_at": now.isoformat(),
        }}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return RevisionIntentResult(True, "recorded", notification_id=notification.pk)
