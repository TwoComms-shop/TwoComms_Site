"""One honest holding reply with a real, still-open operator obligation."""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

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
SAFE_RECEIPT_KEY = "provider_safe_reply"
SAFE_VERSION = "revision-provider-safe-reply-v1"
logger = logging.getLogger(__name__)
PARKED_COLLABORATION_CODE = "provider_dispatch_budget"


def parked_collaboration_revision_ids(*, limit=25):
    """Return parked creator offers that still owe their first reply.

    These revisions have already exhausted provider generation and were moved
    to manual debt, so the normal sealed-revision queue cannot reclaim them.
    The selector is deliberately narrow: no delivery effect or proposal may
    exist and the immutable source snapshot must still classify as a creator
    offer when the row is claimed.
    """
    return list(IgCustomerTurnRevision.objects.filter(
        active_slot=1, state=IgCustomerTurnRevision.State.CLAIMED,
        recovery_state="manual", recovery_code=PARKED_COLLABORATION_CODE,
        generation_proposal_digest="", delivery_effects__isnull=True,
    ).order_by("id").values_list("id", flat=True)[:max(0, min(int(limit), 50))])


def claim_parked_collaboration_revision(revision_id, *, settings_id=1, now=None):
    """Reopen one parked creator offer for deterministic holding delivery.

    This rotates only the execution lease. It preserves the sealed snapshot,
    permission epoch, failed Gemini graph, and existing manager debt; no model
    request is admitted by this path.
    """
    from management.services.bot_sales_classifier import extract_collaboration_brief
    from management.services.ig_revision_outbox import revision_has_newer_source

    now = now or timezone.now()
    try:
        revision_id = int(revision_id)
    except (TypeError, ValueError):
        return None, "revision_id_invalid"
    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().filter(
            pk=settings_id, is_enabled=True,
        ).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=revision_id, active_slot=1, state=IgCustomerTurnRevision.State.CLAIMED,
            recovery_state="manual", recovery_code=PARKED_COLLABORATION_CODE,
            generation_proposal_digest="", delivery_effects__isnull=True,
        ).first()
        client = IgClient.objects.select_for_update().filter(
            pk=getattr(revision, "client_id", None),
        ).first()
        if settings_row is None or revision is None or client is None:
            return None, "parked_collaboration_missing"
        if (
            client.reply_permission_epoch != revision.permission_epoch
            or client.hidden_at or client.is_blocked or client.bot_paused
            or client.manager_takeover or client.privacy_erasure_started_at is not None
            or client.stage == IgClient.Stage.SPAM
        ):
            return None, "parked_collaboration_ineligible"
        if revision_has_newer_source(revision):
            return None, "parked_collaboration_newer_source"
        if not revision.snapshot_digest or not _sources_unchanged(revision):
            return None, "parked_collaboration_snapshot_invalid"
        if not any(
            extract_collaboration_brief(str(row.get("text") or ""))
            for row in revision.bundle_snapshot.get("sources", ())
            if row.get("role") == "user"
        ):
            return None, "parked_collaboration_not_creator_offer"
        if not GeminiRequest.objects.filter(
            logical_turn_id=f"ig-revision:{revision.pk}", client_id=client.pk,
            terminal_resolution="failed", winner_attempt__isnull=True,
        ).exists():
            return None, "parked_collaboration_generation_not_failed"
        token = secrets.token_hex(16)
        revision.claim_token = token
        revision.claimed_at = now
        revision.lease_until = now + timedelta(seconds=90)
        revision.save(update_fields=["claim_token", "claimed_at", "lease_until", "updated_at"])
        return revision, token


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
    from management.services.bot_sales_classifier import SUPPORT_RE, extract_collaboration_brief

    intent = build_turn_intent(client, revision)
    if intent.get("purpose") == "purchase_refusal":
        return False
    if intent.get("commerce_evidence_refs") and "retail_consultation" in intent.get("allowed_response_acts", ()):
        return True
    texts = [str(row.get("text") or "") for row in revision.bundle_snapshot.get("sources", ()) if row.get("role") == "user"]
    return bool(
        (intent.get("purpose") == "support" and any(SUPPORT_RE.search(text) for text in texts))
        or manager_case_reason(revision) in {"customer_manager_request", "collaboration_review"}
        or any(extract_collaboration_brief(text) for text in texts)
    )


def _collaboration_holding_reply(language: str) -> str:
    """Ask for reviewable creator evidence while keeping acceptance undecided."""
    return {
        "uk": (
            "Дякую за пропозицію співпраці. Я передала її керівництву на розгляд. "
            "Будь ласка, надішліть портфоліо або приклади фото- чи відеоробіт, "
            "результати попередніх проєктів і зручний контакт: телефон, Telegram "
            "або інший спосіб зв'язку. Якщо пропозиція зацікавить команду, з вами зв'яжуться."
        ),
        "ru": (
            "Спасибо за предложение о сотрудничестве. Я передала его руководству "
            "на рассмотрение. Пожалуйста, пришлите портфолио или примеры фото- и "
            "видеоработ, результаты прошлых проектов и удобный контакт: телефон, "
            "Telegram или другой способ связи. Если предложение заинтересует команду, "
            "с вами свяжутся."
        ),
        "en": (
            "Thank you for your collaboration proposal. I have passed it to our "
            "management for review. Please send a portfolio or photo/video work samples, "
            "the results of previous projects, and a convenient contact such as phone or "
            "Telegram. If the team is interested, they will contact you."
        ),
    }.get(language, "")


def _ensure_collaboration_review_case(revision, client):
    """Create an open collaboration decision case from the sealed snapshot."""
    from management.services.instagram_bot import notify_manager

    task = IgFollowUpTask.objects.select_for_update().filter(
        client=client, kind=IgFollowUpTask.Kind.MANAGER_TASK,
        reason="revision_case:collaboration_review",
    ).exclude(status__in=(IgFollowUpTask.Status.COMPLETED, IgFollowUpTask.Status.CANCELLED)).order_by("id").first()
    now = timezone.now()
    source_refs = [
        {"message_id": row["message_id"], "source_digest": row["source_digest"]}
        for row in revision.bundle_snapshot.get("sources", ())
        if row.get("role") == "user"
    ]
    if task is None:
        task = IgFollowUpTask.objects.create(
            client=client, due_at=now, status=IgFollowUpTask.Status.SKIPPED,
            kind=IgFollowUpTask.Kind.MANAGER_TASK, reason="revision_case:collaboration_review",
            manager_approval_status=IgFollowUpTask.ManagerApprovalStatus.PENDING,
            manager_approval_requested_at=now,
            message_text="Клієнт пропонує creator/фото-відео співпрацю: перевірити портфоліо, результати, умови та контакт.",
            event_key=f"ig-revision-case:{client.pk}:{revision.pk}:collaboration_review"[:180],
            trigger=IgFollowUpTask.Trigger.EVENT, event_occurred_at=now,
            policy_started_at=now, policy_version="revision-case-v1",
            skip_reason="human_business_decision_required",
        )
    context = dict(task.manager_context or {})
    known = {item.get("message_id") for item in context.get("sources", ()) if isinstance(item, dict)}
    context["sources"] = [*(context.get("sources") or ()), *[item for item in source_refs if item["message_id"] not in known]][-64:]
    context.update({
        "schema_version": 1, "case_kind": "collaboration_review",
        "latest_revision_id": revision.pk, "snapshot_digest": revision.snapshot_digest,
        "required_decisions": ["portfolio_review", "collaboration_terms"],
        "authority": {"price_confirmed": False, "fulfillment_started": False},
    })
    task.manager_context = context
    task.save(update_fields=["manager_context", "updated_at"])
    key = f"ig-revision-collaboration-case:{task.pk}"
    notify_manager(
        "Перевірте пропозицію creator/фото-відео співпраці клієнта.",
        dedupe_key=key, event_type="escalation", client=client,
        metadata={"revision_id": revision.pk, "manager_task_id": task.pk, "case_kind": "collaboration_review"},
        deliver_immediately=False, raise_on_error=True,
    )
    notification = IgBotNotification.objects.filter(dedupe_key=key, client=client).first()
    return task, notification


def _neutral_current_request(client, revision):
    """Allow a generic acknowledgement only for an unclassified clean turn."""
    from management.services.ig_turn_intent import _NEGATED_ORDER, build_turn_intent
    from management.services.bot_sales_classifier import (
        COLLAB_RE, NO_BUY_RE, SUPPORT_RE, URL_RE, is_explicit_opt_out,
    )

    if (
        getattr(client, "bot_paused", False)
        or getattr(client, "opted_out_at", None)
        or getattr(client, "manager_takeover", False)
        or getattr(client, "is_blocked", False)
        or getattr(client, "hidden_at", None)
        or getattr(client, "stage", None) == IgClient.Stage.SPAM
    ):
        return False
    intent = build_turn_intent(client, revision)
    if intent.get("purpose") != "unknown":
        return False
    texts = [
        str(row.get("text") or "")
        for row in revision.bundle_snapshot.get("sources", ())
        if row.get("role") == "user"
    ]
    # A link-only, service, collaboration, opt-out, or purchase-refusal source
    # remains a non-answerable turn. Keep the existing no-handoff policy for it
    # even when the provider is unavailable.
    return not any(
        URL_RE.search(text)
        or COLLAB_RE.search(text)
        or SUPPORT_RE.search(text)
        or NO_BUY_RE.search(text)
        or _NEGATED_ORDER.search(text)
        or is_explicit_opt_out(text)
        for text in texts
    )


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
    if not task or not IgBotNotification.objects.filter(
        pk=receipt.get("notification_id"), client_id=revision.client_id,
        dedupe_key=f"revision-holding-case:{task.pk}",
    ).exists():
        return False
    try:
        collaboration_task_id = int(receipt.get("collaboration_task_id") or 0)
    except (TypeError, ValueError):
        return False
    if collaboration_task_id and not IgFollowUpTask.objects.filter(
        pk=collaboration_task_id, client_id=revision.client_id,
        reason="revision_case:collaboration_review",
    ).exclude(status__in=(IgFollowUpTask.Status.COMPLETED, IgFollowUpTask.Status.CANCELLED)).exists():
        return False
    return True


def record_technical_holding(revision_id, token, *, settings_id, allow_neutral=False):
    """Admit only after conclusive generation failure and before any send."""
    from management.services.ig_revision_recovery import recovery_lineage_for_authority
    from management.services.bot_sales_classifier import extract_collaboration_brief
    from management.services.ig_response_guard import ProviderResponseGuard
    from management.services.ig_reply_truth import ReplyTruthContext

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
            positive_request = _positive_current_request(client, revision)
            neutral_request = bool(allow_neutral and not positive_request and _neutral_current_request(client, revision))
            if not positive_request and not neutral_request:
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
                allow_expired_holding=(revision.recovery_code == PARKED_COLLABORATION_CODE),
            )
            if not ready.ready:
                return RevisionInputDecision(reason=ready.reasons[0])
            receipt_key = RECEIPT_KEY if positive_request else SAFE_RECEIPT_KEY
            existing = (revision.action_receipts or {}).get(receipt_key)
            if existing:
                if positive_request:
                    valid = holding_receipt_valid(revision)
                    return RevisionInputDecision(valid, PURPOSE,
                        "holding_already_recorded" if valid else "holding_handoff_no_longer_open", existing, True)
                valid = (
                    existing.get("version") == SAFE_VERSION
                    and existing.get("origin") == "provider_safe_reply"
                    and existing.get("purpose") == "normal_reply"
                    and existing.get("snapshot_digest") == revision.snapshot_digest
                    and existing.get("source_message_ids") == [
                        row["message_id"] for row in revision.bundle_snapshot.get("sources", [])
                    ]
                    and existing.get("failed_request_id")
                    and existing.get("reply_digest") == _digest(existing.get("reply_text") or "")
                )
                return RevisionInputDecision(
                    valid,
                    "normal_reply",
                    "provider_safe_reply_already_recorded" if valid else "provider_safe_reply_invalid",
                    existing,
                    True,
                )
            language = client.language if client.language in {"uk", "ru", "en"} else "uk"
            texts = ({
                "uk": "Не вдалося надійно підготувати відповідь на ваш запит. Передав питання команді для уточнення.",
                "ru": "Не удалось надёжно подготовить ответ на ваш запрос. Передал вопрос команде для уточнения.",
                "en": "I could not prepare a reliable answer to your request. I have referred your question to the team for clarification.",
            } if positive_request else {
                "uk": "Дякую за повідомлення. Я уточню деталі й невдовзі відповім вам тут.",
                "ru": "Спасибо за сообщение. Я уточню детали и скоро отвечу вам здесь.",
                "en": "Thanks for your message. I will check the details and reply here shortly.",
            })
            collaboration_request = any(
                extract_collaboration_brief(str(row.get("text") or ""))
                for row in revision.bundle_snapshot.get("sources", ())
                if row.get("role") == "user"
            )
            if collaboration_request:
                text = _collaboration_holding_reply(language)
                collaboration_task, collaboration_notification = _ensure_collaboration_review_case(revision, client)
            else:
                text = texts[language]
                collaboration_task = collaboration_notification = None
            guard = ProviderResponseGuard(context_factory=lambda _control, _reply: ReplyTruthContext())
            if not guard.validate({"reply_text": text, "controls": []}).valid:
                return RevisionInputDecision(reason="holding_local_guard_failed")
            if neutral_request:
                receipt = {
                    "version": SAFE_VERSION, "origin": "provider_safe_reply", "purpose": "normal_reply",
                    "snapshot_digest": revision.snapshot_digest,
                    "source_message_ids": [row["message_id"] for row in revision.bundle_snapshot.get("sources", [])],
                    "failed_request_id": graph.request_id,
                    "settings_id": settings_id, "settings_permission_epoch": settings_row.reply_permission_epoch,
                    "publication": {"id": pub.pk, "version": pub.version, "hash": pub.snapshot_hash},
                    "authority": {"allowed_actions": [], "fact_bindings": list(authority.fact_bindings),
                                  "offer_bindings": [], "authority_digest": authority.authority_digest},
                    "reply_text": text, "reply_digest": _digest(text), "language": language,
                    "recorded_at": timezone.now().isoformat(), "substantive_obligation": "none",
                    "reply_mode": "neutral_ack",
                }
                revision.action_receipts = {**(revision.action_receipts or {}), SAFE_RECEIPT_KEY: receipt}
                revision.save(update_fields=["action_receipts", "updated_at"])
                return RevisionInputDecision(True, "normal_reply", "provider_safe_reply_admitted", receipt)
            from management.services.ig_response_debt import record_reply_debt
            from management.services.instagram_bot import notify_manager
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
                "collaboration_task_id": collaboration_task.pk if collaboration_task else 0,
                "collaboration_notification_id": collaboration_notification.pk if collaboration_notification else 0,
                "settings_id": settings_id, "settings_permission_epoch": settings_row.reply_permission_epoch,
                "publication": {"id": pub.pk, "version": pub.version, "hash": pub.snapshot_hash},
                "authority": {"allowed_actions": [], "fact_bindings": list(authority.fact_bindings),
                              "offer_bindings": [], "authority_digest": authority.authority_digest},
                "reply_text": text, "reply_digest": _digest(text), "language": language,
                "recorded_at": timezone.now().isoformat(), "substantive_obligation": "open_manager_reply",
                "reply_mode": "manager_handoff" if positive_request else "neutral_ack",
            }
            revision.action_receipts = {**(revision.action_receipts or {}), RECEIPT_KEY: receipt}
            revision.save(update_fields=["action_receipts", "updated_at"])
            return RevisionInputDecision(True, PURPOSE, "technical_holding_admitted", receipt)
    except Exception:
        logger.exception("revision provider-safe/technical holding admission failed", extra={"revision_id": revision_id})
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
