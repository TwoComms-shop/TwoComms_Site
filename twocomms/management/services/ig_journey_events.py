"""Small, source-bound semantic receipts for validated checkout operations."""
from __future__ import annotations

import json
import re

from django.db import connection

from management.models import IgCheckoutRevision, InstagramBotMessage
from management.services.ig_commercial_episodes import append_episode_event


class JourneyEventRejected(ValueError):
    """A proposed journey receipt does not match its persisted authority."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def record_validated_checkout_revision(checkout_revision):
    """Append one optional diagram receipt inside the checkout transaction.

    This records creation of an offer, never its delivery or payment. The
    caller owns the checkout locks and isolates optional failures in a savepoint.
    """
    if not connection.in_atomic_block:
        raise JourneyEventRejected("checkout_transaction_required")
    if (
        not isinstance(checkout_revision, IgCheckoutRevision)
        or checkout_revision._state.adding
        or not checkout_revision.pk
    ):
        raise JourneyEventRejected("persisted_checkout_revision_required")
    saved = IgCheckoutRevision.objects.select_related(
        "proposal__client", "proposal__commercial_episode", "proposal__deal",
    ).filter(pk=checkout_revision.pk).first()
    immutable_fields = (
        "proposal_id", "revision", "digest", "snapshot", "source",
        "evidence_message_ids", "source_watermark_message_id",
    )
    if saved is None or any(
        _canonical(getattr(saved, field)) != _canonical(getattr(checkout_revision, field))
        for field in immutable_fields
    ):
        raise JourneyEventRejected("checkout_revision_binding_changed")
    proposal = saved.proposal
    episode = proposal.commercial_episode
    if (
        episode.client_id != proposal.client_id
        or proposal.deal.client_id != proposal.client_id
        or proposal.client.privacy_erasure_started_at is not None
    ):
        raise JourneyEventRejected("checkout_episode_owner_mismatch")
    if (
        saved.source not in {
            IgCheckoutRevision.Source.BOT_CREATE,
            IgCheckoutRevision.Source.BOT_UPDATE,
        }
        or saved.revision != proposal.revision
        or not re.fullmatch(r"[0-9a-f]{64}", saved.digest or "")
        or saved.digest != proposal.items_digest
        or not isinstance(saved.snapshot, dict)
        or saved.snapshot.get("digest") != saved.digest
    ):
        raise JourneyEventRejected("checkout_revision_authority_mismatch")
    message_ids = saved.evidence_message_ids
    if (
        not isinstance(message_ids, list)
        or len(message_ids) > 40
        or any(type(value) is not int or value <= 0 for value in message_ids)
        or len(set(message_ids)) != len(message_ids)
        or saved.source_watermark_message_id != max(message_ids, default=0)
        or InstagramBotMessage.objects.filter(
            pk__in=message_ids, client_id=proposal.client_id,
        ).count() != len(message_ids)
    ):
        raise JourneyEventRejected("checkout_evidence_owner_mismatch")
    evidence = {
        "schema_version": 1,
        "from_node": "configured_line",
        "to_node": "quoted_offer",
        "trigger": "validated_offer_created",
        "proposal_id": proposal.pk,
        "checkout_revision_id": saved.pk,
        "revision": saved.revision,
        "digest": saved.digest,
        "evidence_message_ids": list(message_ids),
        "authority": "validated_checkout_revision",
    }
    event = append_episode_event(
        episode,
        dedupe_key=f"journey:checkout-revision:{saved.pk}:validated-offer",
        event_type="semantic_transition",
        source="checkout_revision",
        evidence=evidence,
    )
    if (
        event.episode_id != episode.pk
        or event.event_type != "semantic_transition"
        or event.source != "checkout_revision"
        or event.from_state or event.to_state or event.stage
        or _canonical(event.evidence) != _canonical(evidence)
    ):
        raise JourneyEventRejected("journey_event_identity_conflict")
    return event
