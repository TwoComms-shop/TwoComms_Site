"""One honest holding reply with a real, still-open operator obligation."""
from __future__ import annotations

from django.db import connection, transaction
from django.utils import timezone

from management.models import (
    GeminiRequest, IgBotNotification, IgClient, IgCustomerTurnRevision,
    IgFollowUpTask, IgRevisionDeliveryEffect, InstagramBotSettings,
)
from management.services.ig_revision_authority import (
    CLAIM_PUBLIC_POLICY_INPUTS, build_revision_authority_bindings, check_fact_bindings, check_offer_bindings,
)
from management.services.ig_revision_input import RevisionInputDecision
from management.services.ig_revision_outbox import PublicationBinding, _digest, pre_winner_readiness


RECEIPT_KEY = "technical_holding"
DELIVERY_KEY = "technical_holding_delivery"
PURPOSE = "technical_holding"
VERSION = "revision-technical-holding-v1"


def _sources_unchanged(revision):
    from management.services.ig_turn_revisions import _source_payload

    snapshots = (revision.bundle_snapshot or {}).get("sources") or []
    rows = list(revision.sources.select_related("message").order_by("ordinal", "id"))
    return bool(snapshots and len(rows) == len(snapshots) and all(
        row.message_id == snapshot.get("message_id")
        and row.source_digest == snapshot.get("source_digest")
        and row.message.client_id == revision.client_id
        and _source_payload(row.message, previous=row, ordinal=row.ordinal)["source_digest"] == row.source_digest
        for row, snapshot in zip(rows, snapshots, strict=True)
    ))


def _positive_current_request(client, revision):
    from management.services.ig_turn_intent import build_turn_intent
    from management.services.ig_revision_intents import manager_case_reason
    from management.services.bot_sales_classifier import SUPPORT_RE

    intent = build_turn_intent(client, revision)
    if intent.get("purpose") == "purchase_refusal":
        return False
    if intent.get("commerce_evidence_refs") and "retail_consultation" in intent.get("allowed_response_acts", ()):
        return True
    texts = [str(row.get("text") or "") for row in revision.bundle_snapshot.get("sources", ()) if row.get("role") == "user"]
    return bool((intent.get("purpose") == "support" and any(SUPPORT_RE.search(text) for text in texts))
                or manager_case_reason(revision) == "customer_manager_request")


def holding_receipt_valid(revision, *, text=None):
    """Read again at provider start: a dismissed handoff cannot be promised."""
    receipt = (revision.action_receipts or {}).get(RECEIPT_KEY) or {}
    if (not _sources_unchanged(revision)
        or receipt.get("version") != VERSION or receipt.get("origin") != PURPOSE
        or receipt.get("purpose") != PURPOSE or receipt.get("snapshot_digest") != revision.snapshot_digest
        or receipt.get("reply_digest") != _digest(receipt.get("reply_text") or "")
        or (text is not None and str(text) != receipt.get("reply_text"))):
        return False
    if not GeminiRequest.objects.filter(
        request_id=receipt.get("failed_request_id"), logical_turn_id=f"ig-revision:{revision.pk}",
        client_id=revision.client_id, terminal_resolution="failed", winner_attempt__isnull=True,
    ).exists():
        return False
    task = IgFollowUpTask.objects.filter(
        pk=receipt.get("task_id"), client_id=revision.client_id,
        event_key=f"ig-revision-debt:{revision.pk}", kind="manager_task",
        reason="revision_case:execution_debt",
    ).exclude(status__in=(IgFollowUpTask.Status.COMPLETED, IgFollowUpTask.Status.CANCELLED)).first()
    return bool(task and IgBotNotification.objects.filter(
        pk=receipt.get("notification_id"), client_id=revision.client_id,
        dedupe_key=f"revision-holding-case:{task.pk}",
    ).exists())


def record_technical_holding(revision_id, token, *, settings_id):
    """Admit only after conclusive generation failure and before any send."""
    from management.services.ig_revision_recovery import recovery_lineage_for_authority
    from management.services.ig_response_debt import record_reply_debt
    from management.services.ig_response_guard import ProviderResponseGuard
    from management.services.ig_reply_truth import ReplyTruthContext
    from management.services.instagram_bot import notify_manager

    if connection.in_atomic_block:
        return RevisionInputDecision(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if not identity:
        return RevisionInputDecision(reason="revision_missing")
    try:
        with transaction.atomic():
            settings_row = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=settings_id).first()
            client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
            revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
            if settings_row is None or client is None or revision is None or settings_row.active_instruction_publication is None:
                return RevisionInputDecision(reason="holding_identity_missing")
            revision.client = client
            if revision.generation_proposal_digest:
                return RevisionInputDecision(reason="holding_existing_proposal")
            lineage, reason = recovery_lineage_for_authority(revision)
            if reason or IgRevisionDeliveryEffect.objects.filter(revision_id__in=[row.pk for row in lineage]).exists():
                return RevisionInputDecision(reason="holding_lineage_delivery_or_invalid")
            graph = GeminiRequest.objects.filter(
                logical_turn_id=f"ig-revision:{revision.pk}", client_id=client.pk,
                terminal_resolution="failed", winner_attempt__isnull=True,
            ).first()
            if graph is None:
                return RevisionInputDecision(reason="holding_generation_not_failed")
            if not _sources_unchanged(revision):
                return RevisionInputDecision(reason="holding_sources_changed")
            if not _positive_current_request(client, revision):
                return RevisionInputDecision(reason="holding_current_purpose_ineligible")
            authority = build_revision_authority_bindings(client, claims=(CLAIM_PUBLIC_POLICY_INPUTS,), settings_obj=settings_row)
            if not authority.ready:
                return RevisionInputDecision(reason="holding_authority_unavailable")
            pub = settings_row.active_instruction_publication
            publication = PublicationBinding(pub.pk, pub.version, pub.snapshot_hash)
            ready = pre_winner_readiness(
                revision.pk, token, settings_id=settings_id, settings_permission_epoch=settings_row.reply_permission_epoch,
                publication=publication, fact_bindings=authority.fact_bindings,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings,
            )
            if not ready.ready:
                return RevisionInputDecision(reason=ready.reasons[0])
            existing = (revision.action_receipts or {}).get(RECEIPT_KEY)
            if existing:
                return RevisionInputDecision(holding_receipt_valid(revision), PURPOSE,
                    "holding_already_recorded" if holding_receipt_valid(revision) else "holding_handoff_no_longer_open", existing, True)
            language = client.language if client.language in {"uk", "ru", "en"} else "uk"
            texts = {
                "uk": "Не вдалося надійно підготувати відповідь на ваш запит. Передав питання команді для уточнення.",
                "ru": "Не удалось надёжно подготовить ответ на ваш запрос. Передал вопрос команде для уточнения.",
                "en": "I could not prepare a reliable answer to your request. I have referred your question to the team for clarification.",
            }
            text = texts[language]
            guard = ProviderResponseGuard(context_factory=lambda _control, _reply: ReplyTruthContext())
            if not guard.validate({"reply_text": text, "controls": []}).valid:
                return RevisionInputDecision(reason="holding_local_guard_failed")
            task = record_reply_debt(revision, "provider_candidates_exhausted")
            if task.status in {task.Status.COMPLETED, task.Status.CANCELLED}:
                return RevisionInputDecision(reason="holding_handoff_no_longer_open")
            key = f"revision-holding-case:{task.pk}"
            if not notify_manager(
                "Потрібна відповідь на запит клієнта після технічного збою підготовки відповіді.",
                dedupe_key=key, event_type="escalation", client=client,
                metadata={"revision_id": revision.pk, "manager_task_id": task.pk, "case_kind": "revision_execution_debt"},
                deliver_immediately=False, raise_on_error=True,
            ):
                raise RuntimeError("holding_notification_not_recorded")
            notification = IgBotNotification.objects.get(dedupe_key=key)
            receipt = {
                "version": VERSION, "origin": PURPOSE, "purpose": PURPOSE,
                "snapshot_digest": revision.snapshot_digest,
                "source_message_ids": [row["message_id"] for row in revision.bundle_snapshot.get("sources", [])],
                "failed_request_id": graph.request_id, "task_id": task.pk, "notification_id": notification.pk,
                "settings_id": settings_id, "settings_permission_epoch": settings_row.reply_permission_epoch,
                "publication": {"id": pub.pk, "version": pub.version, "hash": pub.snapshot_hash},
                "authority": {"allowed_actions": [], "fact_bindings": list(authority.fact_bindings),
                              "offer_bindings": [], "authority_digest": authority.authority_digest},
                "reply_text": text, "reply_digest": _digest(text), "language": language,
                "recorded_at": timezone.now().isoformat(), "substantive_obligation": "open_manager_reply",
            }
            revision.action_receipts = {**(revision.action_receipts or {}), RECEIPT_KEY: receipt}
            revision.save(update_fields=["action_receipts", "updated_at"])
            return RevisionInputDecision(True, PURPOSE, "technical_holding_admitted", receipt)
    except Exception:
        return RevisionInputDecision(reason="holding_admission_failed")


def finalize_technical_holding(locked, effects):
    """Record only holding delivery; its substantive question stays manual debt."""
    admission = (locked.action_receipts or {}).get(RECEIPT_KEY) or {}
    if admission.get("version") != VERSION or admission.get("snapshot_digest") != locked.snapshot_digest:
        raise ValueError("holding_admission_invalid")
    receipt = {"version": VERSION, "snapshot_digest": locked.snapshot_digest,
               "task_id": admission["task_id"], "notification_id": admission["notification_id"],
               "sent_effect_ids": [effect.pk for effect in effects],
               "provider_message_ids": [effect.provider_message_id for effect in effects],
               "sent_at": max(effect.terminal_at for effect in effects).isoformat(),
               "disposition": "holding_sent_substantive_reply_still_owed"}
    existing = (locked.action_receipts or {}).get(DELIVERY_KEY)
    if existing and existing != receipt:
        raise ValueError("holding_delivery_receipt_changed")
    locked.action_receipts = {**(locked.action_receipts or {}), DELIVERY_KEY: receipt}
    locked.recovery_state = "manual"
    locked.recovery_code = "technical_holding_sent"
    locked.recovery_due_at = None
    locked.claim_token = "debt:holding"
    locked.claimed_at = None
    locked.lease_until = None
    locked.save(update_fields=["action_receipts", "recovery_state", "recovery_code", "recovery_due_at",
                               "claim_token", "claimed_at", "lease_until", "updated_at"])


def confirmed_holding_effect_ids(revision):
    """Historical physical holding proof, independent of later case dismissal."""
    from management.services.ig_revision_execution import _whole_sent_proof

    if revision is None:
        return ()
    admission = (revision.action_receipts or {}).get(RECEIPT_KEY) or {}
    delivery = (revision.action_receipts or {}).get(DELIVERY_KEY) or {}
    effects = list(revision.delivery_effects.order_by("order_index", "id"))
    if (len(effects) != 1 or effects[0].state != "sent" or effects[0].purpose != PURPOSE
        or admission.get("version") != VERSION or delivery.get("version") != VERSION
        or admission.get("snapshot_digest") != revision.snapshot_digest
        or delivery.get("snapshot_digest") != revision.snapshot_digest
        or delivery.get("sent_effect_ids") != [effects[0].pk]
        or delivery.get("provider_message_ids") != [effects[0].provider_message_id]
        or delivery.get("task_id") != admission.get("task_id")
        or delivery.get("notification_id") != admission.get("notification_id")
        or admission.get("reply_text") != (effects[0].payload.get("message") or {}).get("text")
        or not _sources_unchanged(revision) or not _whole_sent_proof(revision, effects)[0]):
        return ()
    return (effects[0].pk,)


def is_confirmed_holding_transcript(message):
    if message.source != "revision_holding" or not message.synthetic_event_key.startswith("ig-revision-effect:"):
        return False
    effect = IgRevisionDeliveryEffect.objects.select_related("revision").filter(
        revision__client_id=message.client_id, provider_message_id=message.provider_message_id,
        provider_namespace=message.provider_namespace, purpose=PURPOSE, state="sent",
    ).first()
    return bool(effect and message.synthetic_event_key == f"ig-revision-effect:{effect.pk}"
                and message.text == (effect.payload.get("message") or {}).get("text")
                and effect.pk in confirmed_holding_effect_ids(effect.revision))
