"""Read-only timers for actual current invoices; no inferred lifetime or sending.

Legacy invoices without an attributable issue timestamp are deliberately omitted.
Follow-up and channel-window clocks require separate adapters. A local expired
deadline describes this invoice's recorded lifetime, never payment truth.
"""
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from management.models import (IgCheckoutInvoiceGeneration, IgCheckoutProposal,
    IgClient, IgDeal, IgFunnelResetAudit, InstagramBotMessage)


def invoice_timers(client_id, episode_id, *, now):
    """Return at most one current invoice ring, in at most four SELECTs.

    Caller attaches this only to the current view's guide:payment and supplies
    the same aware ``now`` used for snapshot.server_now. All references are
    internal row IDs; no provider identity, URL, financial payload or PII leaks.
    """
    if (type(client_id) is not int or client_id <= 0 or type(episode_id) is not int
        or episode_id <= 0 or now is None or timezone.is_naive(now)):
        return []
    settled = IgCheckoutInvoiceGeneration.objects.filter(proposal_id=OuterRef("pk")).filter(
        Q(winner_slot=1) | Q(winner_claimed_at__isnull=False) | Q(paid_at__isnull=False)
        | Q(state__in=("winner_claimed", "paid_winner", "late_paid_review", "resource_review")))
    proposals = list(IgCheckoutProposal.objects.select_related(
        "client", "deal__payment_projection", "commercial_episode",
        "current_invoice_generation__payment_attempt",
    ).annotate(_settled=Exists(settled)).filter(
        client_id=client_id, deal__client_id=client_id,
        commercial_episode_id=episode_id, commercial_episode__client_id=client_id,
        client__current_commercial_episode_id=episode_id,
        commercial_episode__open_slot=1, deal__active_checkout_proposal_id=F("pk"),
        assisted_checkout_v2=True, status=IgCheckoutProposal.Status.INVOICE_CREATED,
        superseded_by__isnull=True, winner_invoice_generation__isnull=True,
        paid_at__isnull=True, current_invoice_generation__isnull=False,
    ).order_by("pk")[:2])
    if len(proposals) != 1:
        return []
    proposal = proposals[0]
    client, deal, episode = proposal.client, proposal.deal, proposal.commercial_episode
    if (client.hidden_at or client.privacy_erasure_started_at is not None
        or episode.deal_id != deal.pk or proposal._settled):
        return []
    generation = proposal.current_invoice_generation
    attempt = generation.payment_attempt
    if (generation.proposal_id != proposal.pk or generation.proposal_revision != proposal.revision
        or generation.active_slot != 1 or generation.winner_slot is not None
        or generation.state != generation.State.INVOICE_CREATED
        or generation.provider != "monobank" or not generation.provider_invoice_id
        or attempt is None or attempt.monobank_invoice_id != generation.provider_invoice_id
        or not attempt.invoice_url or attempt.checkout_series_key != generation.series_key
        or attempt.checkout_generation != generation.generation
        or attempt.invoice_expires_at != generation.expires_at
        or attempt.status != attempt.Status.PROCESSING or attempt.order_id
        or attempt.checkout_winner_claimed or (attempt.paid_amount or 0) > 0
        or attempt.provider_recheck_state not in {"", "resolved"}):
        return []
    if (deal.status not in {IgDeal.Status.DRAFT, IgDeal.Status.QUOTED, IgDeal.Status.AWAITING_PAYMENT}
        or deal.order_id or deal.paid_at or deal.paid_amount > 0
        or deal.payment_status in {"paid", "prepaid"}
        or deal.payment_truth not in {IgDeal.PaymentTruth.UNVERIFIED, IgDeal.PaymentTruth.PENDING}):
        return []
    projection = getattr(deal, "payment_projection", None)
    if projection is not None and (
        projection.client_id != client_id or projection.paid_at
        or projection.needs_reconciliation
        or projection.truth not in {IgDeal.PaymentTruth.UNVERIFIED, IgDeal.PaymentTruth.PENDING}
    ):
        return []
    started, due = generation.provider_completed_at, generation.expires_at
    if (not started or not due or timezone.is_naive(started) or timezone.is_naive(due)
        or started > now or due <= started or not generation.provider_started_at
        or generation.provider_started_at > started):
        return []
    reset = IgFunnelResetAudit.objects.filter(client_id=client_id).order_by("-pk").values(
        "reset_after_message_id", "created_at").first()
    floor = int(reset["reset_after_message_id"] or 0) + 1 if reset else 1
    if reset and (proposal.created_at < reset["created_at"]
        or generation.provider_started_at < reset["created_at"]):
        return []
    # Real source references, not a fabricated message at a timestamp boundary.
    refs = {value for value in (episode.opened_watermark_message_id,
        proposal.payment_policy_evidence_message_id, generation.policy_evidence_message_id)
        if value}
    item_evidence = list(proposal.items.order_by("pk").values_list("evidence_message_ids", flat=True)[:17])
    if len(item_evidence) > 16:
        return []
    for values in item_evidence:
        if not isinstance(values, list) or len(values) > 64:
            return []
        if any(type(value) is not int or value <= 0 for value in values):
            return []
        refs.update(values)
    if not refs or len(refs) > 64 or any(value < floor for value in refs):
        return []
    sources = list(InstagramBotMessage.objects.filter(client_id=client_id, pk__in=refs)
        .values_list("pk", "role"))
    if {pk for pk, _role in sources} != refs:
        return []
    user_refs = sorted(pk for pk, role in sources if role == InstagramBotMessage.Role.USER)
    if not user_refs:
        return []
    return [{"id": f"invoice_expiry:{generation.pk}", "kind": "invoice_expiry",
        "label": "Строк рахунку", "started_at": started.isoformat(), "due_at": due.isoformat(),
        "status": "expired" if now >= due else "running", "evidence_refs": [
            {"kind": "proposal", "id": proposal.pk},
            {"kind": "invoice_generation", "id": generation.pk},
            {"kind": "payment_attempt", "id": attempt.pk},
            *({"kind": "message", "id": pk} for pk in user_refs),
        ]}]
