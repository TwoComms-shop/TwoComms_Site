"""Bounded audit/apply for historical confirmed revision receipts.

Historical revisions predate the admission stamp used by the current funnel
projection.  They may still have an immutable, provider-confirmed SENT proof
that is safe to project into transcript/timestamps/counters.  Funnel facts
remain unsupported when that admission is absent; this module never infers
them from the mutable current episode.
"""
from __future__ import annotations

from management.models import IgCustomerTurnRevision, IgRevisionDeliveryEffect


def inspect_historical_receipts(*, revision_ids=(), limit=1000, apply=False):
    """Report or explicitly apply old receipt projections without provider I/O."""
    from management.services.ig_revision_execution import _whole_sent_proof
    from management.services.ig_revision_reply_projection import RECEIPT_KEY, project_sent_reply

    requested = {int(value) for value in revision_ids if int(value) > 0}
    candidates = IgCustomerTurnRevision.objects.filter(
        delivery_effects__state=IgRevisionDeliveryEffect.State.SENT,
    ).exclude(
        action_receipts__has_key=RECEIPT_KEY,
    ).order_by("pk")
    if requested:
        candidates = candidates.filter(pk__in=requested)
    rows = []
    applied = 0
    for revision in candidates[: max(1, min(int(limit), 1000))]:
        effects = list(revision.delivery_effects.order_by("order_index", "id"))
        proof, reason = _whole_sent_proof(revision, effects)
        row = {
            "revision_id": revision.pk,
            "client_id": revision.client_id,
            "state": str(revision.state),
            "sent_effects": sum(effect.state == IgRevisionDeliveryEffect.State.SENT for effect in effects),
            "proof": "confirmed" if proof else "unsupported",
            "reason": "ready" if proof else reason,
        }
        if apply and proof:
            try:
                receipt = project_sent_reply(revision.pk)
                row["applied"] = bool(receipt)
                row["count_outcome"] = receipt.get("count_outcome", "") if receipt else ""
                if receipt:
                    applied += 1
            except Exception as exc:  # one bad historical row must not stop the audit
                row["applied"] = False
                row["reason"] = f"{type(exc).__name__.lower()}"
        rows.append(row)
    return {
        "mode": "apply" if apply else "dry_run",
        "requested_revision_ids": sorted(requested),
        "scanned": len(rows),
        "eligible": sum(row["proof"] == "confirmed" for row in rows),
        "applied": applied,
        "provider_calls": 0,
        "rows": rows,
    }
