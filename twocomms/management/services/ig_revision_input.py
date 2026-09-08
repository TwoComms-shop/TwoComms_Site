"""Finite source-only decisions before revision generation.

One append-only receipt makes admission and the per-customer hourly cap replay
safe. Deterministic replies carry this explicit origin, never a Gemini winner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
import json
import re

from django.db import connection, transaction
from django.utils import timezone

from management.models import IgClient, IgCustomerTurn, IgCustomerTurnRevision, InstagramBotMessage, InstagramBotSettings
from management.services.ig_revision_authority import (
    CLAIM_PUBLIC_POLICY_INPUTS, build_revision_authority_bindings, check_fact_bindings, check_offer_bindings,
)
from management.services.ig_revision_outbox import PublicationBinding, pre_winner_readiness


RECEIPT_KEY = "input_decision"
RATE_LIMIT = 25
RATE_WINDOW = timedelta(hours=1)
VERSION = "revision-input-v1"


@dataclass(frozen=True)
class RevisionInputDecision:
    ready: bool = False
    origin: str = ""
    reason: str = ""
    receipt: dict = field(default_factory=dict)
    replayed: bool = False


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def classify_sealed_input(snapshot, client, settings_row):
    """All current sources participate; a trailing reaction never eats a question."""
    from management.services.bot_sales_classifier import NO_BUY_RE, is_explicit_opt_out, is_reaction_only

    sources = snapshot.get("sources") or []
    if not sources or any(row.get("role") != "user" for row in sources):
        return "blocked", "source_role_invalid", ""
    texts = [str(row.get("text") or "").strip() for row in sources]
    media = any(row.get("media_parts") for row in sources)
    if any(is_explicit_opt_out(text) for text in texts):
        return "no_reply", "opt_out", ""
    if client.stage == IgClient.Stage.SPAM:
        return "no_reply", "spam_abuse", ""
    if any(row.get("quick_reply_payload") for row in sources):
        return "postback", "customer_action", ""
    if not media:
        meaningful = [text for text in texts if text and not is_reaction_only(text)]
        if not meaningful:
            return "no_reply", "reaction_only" if any(texts) else "no_reply", ""
        if all(NO_BUY_RE.search(text) and not re.search(r"\w", NO_BUY_RE.sub("", text)) for text in meaningful):
            return "no_reply", "explicit_no_buy", ""
    if not settings_row.ai_enabled:
        # Test every source: a trigger followed by an emoji still triggers once.
        if not any(text == str(settings_row.trigger_text or "") for text in texts):
            return "no_reply", "static_trigger_absent", ""
        reply = str(settings_row.reply_text or "").strip()
        if not reply or len(reply) > 4000:
            return "blocked", "static_reply_invalid", ""
        return "static_reply", "configured_reply", reply
    return "generate", "model_reply", ""


def decide_revision_input(revision_id, token, *, settings_id, now=None):
    if connection.in_atomic_block:
        return RevisionInputDecision(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionInputDecision(reason="revision_missing")
    now = now or timezone.now()
    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if settings_row is None or client is None or revision is None:
            return RevisionInputDecision(reason="input_identity_missing")
        if not revision.snapshot_digest or _digest(revision.bundle_snapshot) != revision.snapshot_digest:
            return RevisionInputDecision(reason="revision_snapshot_invalid")
        existing = (revision.action_receipts or {}).get(RECEIPT_KEY)
        if existing is not None:
            if existing.get("snapshot_digest") != revision.snapshot_digest or existing.get("version") != VERSION:
                return RevisionInputDecision(reason="input_receipt_invalid")
            return RevisionInputDecision(True, existing["origin"], existing["reason"], dict(existing), True)
        publication_row = settings_row.active_instruction_publication
        if publication_row is None:
            return RevisionInputDecision(reason="publication_unavailable")
        publication = PublicationBinding(publication_row.pk, publication_row.version, publication_row.snapshot_hash)
        authority = build_revision_authority_bindings(client, claims=(CLAIM_PUBLIC_POLICY_INPUTS,), settings_obj=settings_row)
        if not authority.ready:
            return RevisionInputDecision(reason=authority.reasons[0])
        readiness = pre_winner_readiness(
            revision.pk, token, settings_id=settings_row.pk,
            settings_permission_epoch=settings_row.reply_permission_epoch, publication=publication,
            fact_bindings=authority.fact_bindings, fact_checker=check_fact_bindings, offer_checker=check_offer_bindings,
            now=now,
        )
        if not readiness.ready:
            return RevisionInputDecision(reason=readiness.reasons[0])
        origin, reason, reply = classify_sealed_input(revision.bundle_snapshot, client, settings_row)
        if origin == "blocked":
            return RevisionInputDecision(reason=reason)
        rate_counted = origin in {"generate", "static_reply", "postback"}
        recent = IgCustomerTurnRevision.objects.filter(
            client=client, created_at__gte=now - RATE_WINDOW,
            action_receipts__input_decision__rate_counted=True,
        )
        already_counted = recent.filter(snapshot_digest=revision.snapshot_digest).exists()
        admitted_count = recent.values("snapshot_digest").distinct().count()
        if rate_counted and not already_counted and admitted_count >= RATE_LIMIT:
            origin, reason, reply, rate_counted = "no_reply", "rate_limited", "", False
        receipt = {
            "version": VERSION, "origin": origin, "reason": reason,
            "snapshot_digest": revision.snapshot_digest,
            "source_message_ids": [int(row["message_id"]) for row in revision.bundle_snapshot["sources"]],
            "settings_id": settings_row.pk, "settings_permission_epoch": settings_row.reply_permission_epoch,
            "publication": {"id": publication.publication_id, "version": publication.version, "hash": publication.snapshot_hash},
            "authority": {"allowed_actions": [], "fact_bindings": list(authority.fact_bindings), "offer_bindings": [], "authority_digest": authority.authority_digest},
            "ai_enabled": bool(settings_row.ai_enabled), "static_reply_text": reply,
            "static_settings_digest": _digest({"ai_enabled": bool(settings_row.ai_enabled), "trigger_text": settings_row.trigger_text, "reply_text": settings_row.reply_text}),
            "rate_counted": rate_counted, "recorded_at": now.isoformat(),
        }
        revision.action_receipts = {**(revision.action_receipts or {}), RECEIPT_KEY: receipt}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return RevisionInputDecision(True, origin, reason, receipt)


def complete_no_reply_input(revision_id, token):
    """Consume a current finite source decision atomically without any send."""
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return False
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if client is None or revision is None or client.privacy_erasure_started_at is not None:
            return False
        receipt = (revision.action_receipts or {}).get(RECEIPT_KEY) or {}
        if receipt.get("origin") != "no_reply" or receipt.get("snapshot_digest") != revision.snapshot_digest:
            return False
        if revision.state == revision.State.PROCESSED:
            return True
        if revision.active_slot != 1 or revision.state != revision.State.CLAIMED or revision.claim_token != token:
            return False
        if revision.delivery_effects.exists() or revision.generation_proposal_digest:
            return False
        source_ids = receipt["source_message_ids"]
        turn = IgCustomerTurn.objects.select_for_update().filter(pk=revision.turn_id).first()
        InstagramBotMessage.objects.filter(pk__in=source_ids, client=client, status__in=("pending", "processing")).update(status="done", processed_at=timezone.now())
        revision.state = revision.State.PROCESSED
        revision.processed_at = timezone.now()
        revision.claim_token, revision.claimed_at, revision.lease_until = "", None, None
        revision.save(update_fields=["state", "processed_at", "claim_token", "claimed_at", "lease_until", "updated_at"])
        if turn and not turn.turn_messages.exclude(message_id__in=source_ids).filter(message__status__in=("pending", "processing")).exists():
            IgCustomerTurn.objects.filter(pk=turn.pk, terminal_reason="").update(
                claim_state=IgCustomerTurn.ClaimState.PROCESSED,
                terminal_reason=IgCustomerTurn.TerminalReason.NO_REPLY_NEEDED,
                processed_at=timezone.now(), updated_at=timezone.now(),
            )
        return True


def record_unavailable_media_reply(revision_id, token, *, settings_id, collection):
    """Store a source-bound clarification when there is nothing available to inspect."""
    from types import SimpleNamespace
    from management.services.instagram_bot import _has_meaningful_media_caption, _media_unavailable_reply

    if connection.in_atomic_block or collection.parts or not collection.coverage.get("total_parts"):
        return RevisionInputDecision(reason="media_clarification_not_applicable")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return RevisionInputDecision(reason="revision_missing")
    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if settings_row is None or client is None or revision is None:
            return RevisionInputDecision(reason="input_identity_missing")
        sources = revision.bundle_snapshot.get("sources") or []
        if any(_has_meaningful_media_caption(SimpleNamespace(text=row.get("text") or "")) for row in sources):
            return RevisionInputDecision(reason="media_clarification_not_applicable")
        binding = collection.binding
        if (binding.get("revision_id") != revision.pk or binding.get("revision_snapshot_digest") != revision.snapshot_digest
            or binding.get("items") or binding.get("digest") != _digest({key: value for key, value in binding.items() if key != "digest"})):
            return RevisionInputDecision(reason="media_clarification_binding_invalid")
        authority = build_revision_authority_bindings(client, claims=(CLAIM_PUBLIC_POLICY_INPUTS,), settings_obj=settings_row)
        if not authority.ready or settings_row.active_instruction_publication is None:
            return RevisionInputDecision(reason="public_policy_unavailable")
        pub = settings_row.active_instruction_publication
        publication = PublicationBinding(pub.pk, pub.version, pub.snapshot_hash)
        ready = pre_winner_readiness(revision.pk, token, settings_id=settings_id, settings_permission_epoch=settings_row.reply_permission_epoch, publication=publication, fact_bindings=authority.fact_bindings, fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
        if not ready.ready:
            return RevisionInputDecision(reason=ready.reasons[0])
        existing = (revision.action_receipts or {}).get("media_unavailable_reply")
        if existing:
            if existing.get("snapshot_digest") != revision.snapshot_digest:
                return RevisionInputDecision(reason="media_clarification_receipt_invalid")
            return RevisionInputDecision(True, "media_unavailable", "owned_media_unavailable", dict(existing), True)
        receipt = {
            "version": "revision-media-clarification-v1", "origin": "media_unavailable", "reason": "owned_media_unavailable",
            "snapshot_digest": revision.snapshot_digest, "source_message_ids": [row["message_id"] for row in sources],
            "media_binding_digest": binding["digest"], "media_coverage": dict(collection.coverage),
            "source_media_outcomes": list(binding.get("outcomes") or []),
            "settings_id": settings_id, "settings_permission_epoch": settings_row.reply_permission_epoch,
            "publication": {"id": pub.pk, "version": pub.version, "hash": pub.snapshot_hash},
            "authority": {"allowed_actions": [], "fact_bindings": list(authority.fact_bindings), "offer_bindings": [], "authority_digest": authority.authority_digest},
            "reply_text": _media_unavailable_reply(client, retry_pending=False), "recorded_at": timezone.now().isoformat(),
        }
        revision.action_receipts = {**(revision.action_receipts or {}), "media_unavailable_reply": receipt}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return RevisionInputDecision(True, "media_unavailable", "owned_media_unavailable", receipt)
