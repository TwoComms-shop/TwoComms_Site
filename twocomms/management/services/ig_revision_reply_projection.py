"""DB-only transcript and once-per-logical-reply projections from SENT effects."""
from __future__ import annotations

import json

from django.db import transaction

from management.models import (
    IgClient, IgCustomerTurnRevision, IgRevisionDeliveryEffect,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_revision_outbox import _digest


RECEIPT_KEY = "sent_reply_projection"
ADMISSION_KEY = "reply_projection_admission"
VERSION = "revision-sent-reply-projection-v1"


class ReplyProjectionError(RuntimeError):
    """Retry only the local receipt projection, never the external send."""


def admission_binding(revision, *, plan_digest, settings_id):
    """Ownership stamped by the producer only when a new plan is committed."""
    return {"version": VERSION, "revision_id": revision.pk,
            "snapshot_digest": revision.snapshot_digest, "plan_digest": plan_digest,
            "settings_id": settings_id}


def _count_admitted(revision, first):
    admission = (revision.action_receipts or {}).get(ADMISSION_KEY)
    if admission is None:
        return False
    expected = admission_binding(revision, plan_digest=first.plan_digest, settings_id=first.settings_id_snapshot)
    if not isinstance(admission, dict) or any(admission.get(key) != value for key, value in expected.items()):
        raise ReplyProjectionError("projection_admission_binding_changed")
    return True


def _origin(revision):
    receipts = revision.action_receipts or {}
    if (receipts.get("technical_holding") or {}).get("origin") == "technical_holding":
        return "technical_holding"
    fallback = receipts.get("source_preference_fallback") or {}
    if fallback.get("origin") == "source_preference_fallback":
        return "source_preference_fallback"
    original = (receipts.get("input_decision") or {}).get("origin")
    if original and original != "generate":
        return str(original)
    if revision.generation_proposal_digest:
        return "generate"
    return str(original or "canonical_reply")


def _project_part(client, effect):
    key = f"ig-revision-effect:{effect.pk}"
    content = effect.payload.get("message") or {}
    text = content.get("text") or ""
    attachments = ""
    shown = None
    if effect.group == "catalog_media":
        metadata = effect.projection_metadata or {}
        text = f"(фото товару: {metadata['title']})" if metadata.get("title") else "(фото товару)"
        url = ((content.get("attachment") or {}).get("payload") or {}).get("url") or ""
        attachments = json.dumps([url], ensure_ascii=False) if url else ""
        if metadata.get("product_id") and _digest(metadata) == effect.projection_digest:
            shown = {"position": effect.part_index + 1, "product_id": int(metadata["product_id"]),
                     "title": str(metadata.get("title") or "")}
    source = "revision_holding" if effect.purpose == "technical_holding" else "revision_reply"
    message, created = InstagramBotMessage.objects.get_or_create(
        client=client, synthetic_event_key=key,
        defaults={
            "sender_id": client.igsid, "role": InstagramBotMessage.Role.MODEL,
            "text": text, "attachments": attachments,
            "source": source, "status": InstagramBotMessage.Status.DONE,
            "send_state": "sent", "provider_namespace": effect.provider_namespace,
            "provider_message_id": effect.provider_message_id,
            "provider_created_at": effect.terminal_at, "processed_at": effect.terminal_at,
            "send_completed_at": effect.terminal_at,
            "gemini_request_id": effect.generation_request_id,
            "gemini_model": effect.generation_model,
        },
    )
    if not created:
        if (message.role != "model" or message.source != source
            or message.provider_namespace != effect.provider_namespace
            or message.provider_message_id != effect.provider_message_id):
            raise ReplyProjectionError("transcript_receipt_binding_changed")
        # A restart may find the old transcript-only projector's row. Preserve
        # history identity/content, but repair timestamps from the same receipt.
        fields = []
        for field in ("provider_created_at", "processed_at", "send_completed_at"):
            if getattr(message, field) != effect.terminal_at:
                setattr(message, field, effect.terminal_at)
                fields.append(field)
        if fields:
            message.save(update_fields=fields)
    return message, shown


def project_sent_reply(revision_id):
    """Serialize all counters with their append-only receipt and original times.

    Current permission/head/deadline does not invalidate confirmed historical
    delivery. Partial receipts project history only; they cannot fulfill a reply.
    """
    from management.services.ig_revision_execution import _completion_decision, _whole_sent_proof
    from management.services.ig_revision_recovery import recovery_lineage_for_authority

    first = IgRevisionDeliveryEffect.objects.filter(revision_id=revision_id).order_by("order_index", "id").first()
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if first is None or identity is None:
        return {}
    with transaction.atomic():
        # Match admission/followup lock order, including exact-echo callers.
        settings_row = InstagramBotSettings.objects.select_for_update().filter(pk=first.settings_id_snapshot).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if client is None or revision is None or client.privacy_erasure_started_at is not None:
            return {}
        if settings_row is None:
            raise ReplyProjectionError("projection_settings_missing")
        revision.client = client
        effects = list(revision.delivery_effects.select_for_update().order_by("order_index", "id"))
        proof, reason = _whole_sent_proof(revision, effects)
        if not proof:
            raise ReplyProjectionError(reason)
        sent = [effect for effect in effects if effect.state == effect.State.SENT]
        messages, shown = {}, []
        for effect in sent:
            message, product = _project_part(client, effect)
            messages[effect.pk] = message.pk
            if product:
                shown.append(product)
        if shown:
            context = dict(client.sales_context or {})
            previous = context.get("shown_products") or {}
            if int(previous.get("revision_number") or 0) <= revision.revision:
                context["shown_products"] = {
                    "at": max(effect.terminal_at for effect in sent).isoformat(), "items": shown,
                    "revision_number": revision.revision, "revision_id": revision.pk,
                    "source_message_watermark": max(revision.sources.values_list("message_id", flat=True), default=0),
                }
                if context != client.sales_context:
                    client.sales_context = context
                    client.save(update_fields=["sales_context", "updated_at"])
        complete, _, reason = _completion_decision(effects)
        if not complete or reason != "delivered":
            return {}
        lineage, reason = recovery_lineage_for_authority(revision)
        if reason or not lineage:
            raise ReplyProjectionError(reason or "projection_lineage_missing")
        logical_key = f"revision:{lineage[-1].pk}"
        origin = _origin(revision)
        primary = [effect for effect in sent if effect.group == "substantive_text"
                   and effect.actor == "bot" and effect.purpose == "normal_reply"
                   and str((effect.payload.get("message") or {}).get("text") or "").strip()]
        eligible = bool(primary) and origin not in {"no_reply", "holding", "technical_holding", "followup", "human_reply"}
        count_admitted = _count_admitted(revision, first)
        binding = {
            "version": VERSION, "logical_reply_key": logical_key,
            "revision_id": revision.pk, "snapshot_digest": revision.snapshot_digest,
            "plan_digest": first.plan_digest, "settings_id": settings_row.pk,
            "source_message_ids": [row["message_id"] for row in revision.bundle_snapshot.get("sources", [])],
            "sent_effect_ids": [effect.pk for effect in sent],
            "provider_message_ids": [effect.provider_message_id for effect in sent],
            "reply_message_ids": [messages[effect.pk] for effect in sent],
            "origin": origin, "eligible_primary_reply": eligible,
            "count_admitted": count_admitted,
            "sent_at": max(effect.terminal_at for effect in sent).isoformat(),
        }
        existing = (revision.action_receipts or {}).get(RECEIPT_KEY)
        if existing is not None:
            if not isinstance(existing, dict) or any(existing.get(key) != value for key, value in binding.items()):
                raise ReplyProjectionError("sent_reply_projection_binding_changed")
            return existing
        already_counted = IgCustomerTurnRevision.objects.filter(
            client=client,
            action_receipts__sent_reply_projection__logical_reply_key=logical_key,
            action_receipts__sent_reply_projection__count_delta=1,
        ).exists()
        delta = int(eligible and count_admitted and not already_counted)
        if eligible:
            sent_at = max(effect.terminal_at for effect in sent)
            if client.last_bot_reply_at is None or client.last_bot_reply_at < sent_at:
                client.last_bot_reply_at = sent_at
                client.save(update_fields=["last_bot_reply_at", "updated_at"])
            settings_fields = []
            if settings_row.last_reply_at is None or settings_row.last_reply_at < sent_at:
                settings_row.last_reply_at = sent_at
                settings_fields.append("last_reply_at")
            if delta:
                settings_row.replies_count = int(settings_row.replies_count or 0) + delta
                settings_fields.append("replies_count")
            if settings_fields:
                settings_row.save(update_fields=settings_fields)
        receipt = {
            **binding, "count_delta": delta,
            "count_outcome": (
                "not_primary_reply" if not eligible else
                "legacy_count_requires_review" if not count_admitted else
                "counted" if delta else "logical_reply_already_counted"
            ),
            "funnel_projection": {"outcome": "unsupported", "reason": "original_episode_binding_not_projected"},
        }
        revision.action_receipts = {**(revision.action_receipts or {}), RECEIPT_KEY: receipt}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return receipt
