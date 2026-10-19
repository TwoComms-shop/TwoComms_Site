"""Versioned acquisition cohorts over immutable inquiry/payment observations.

This read model never authorizes a payment. Current purchase units come from
bot_payment_truth; event-time success comes from already recorded payment facts.
Missing identity/history/attribution evidence remains explicit.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from math import sqrt

from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_datetime

VERSION = "ig-commercial-conversion-v1"
OBSERVATION_KEY = "commercial_inquiry_v1"
ATTRIBUTION_KEY = "commercial_attribution_v1"
INQUIRY_EVENT_TYPE = "commercial_inquiry_observed"
COMMERCIAL = {"retail", "custom"}


def _amount(value):
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount >= 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def source_snapshot(attribution=None):
    """Copy a cited observation; never substitute mutable client ad fields."""
    source = attribution if isinstance(attribution, dict) else {}
    quality = source.get("quality", "unknown")
    if quality not in {"known", "unknown", "ambiguous", "legacy"}:
        quality = "unknown"
    citations = source.get("source_message_ids", [])
    if not isinstance(citations, (list, tuple)):
        citations = []
    citations = sorted({int(pk) for pk in citations if str(pk).isdigit()})
    if quality == "known" and not citations:
        quality = "unknown"
    return {
        "version": VERSION,
        "quality": quality,
        "channel": "instagram",
        "ad_id": str(source.get("ad_id") or "")[:64] if quality == "known" else "",
        "campaign_id": str(source.get("campaign_id") or "")[:64] if quality == "known" else "",
        "source": str(source.get("source") or "")[:64] if quality == "known" else "",
        "source_message_ids": citations,
        "reason": str(source.get("reason") or ("source_observed" if quality == "known" else "source_not_observed"))[:120],
    }


def record_commercial_inquiry_observation_in_transaction(
    locked_client, *, source_message_id, classification, history_status="unknown",
    evidence=None, attribution=None,
):
    """Freeze an own inbound classification before bot delivery can succeed/fail.

    ``verified_new`` requires a cited, complete-history identity audit supplied
    by the caller. Absence of orders, a fresh CRM row, readiness and a model's
    guess do not prove that someone is a new customer. Noncommercial messages
    are recorded for coverage and never become retail losses.
    """
    from management.models import IgCommercialEpisodeEvent, InstagramBotMessage
    from management.services.ig_funnel_analytics import _ensure_episode_locked
    from management.services.ig_commercial_episodes import append_episode_event

    if not connection.in_atomic_block:
        raise RuntimeError("inquiry observation requires transaction.atomic")
    row = InstagramBotMessage.objects.get(pk=source_message_id)
    if row.role != "user" or row.client_id != locked_client.pk or row.sender_id != locked_client.igsid:
        raise ValueError("inquiry source must be an own client inbound message")
    evidence = evidence if isinstance(evidence, dict) else {}
    classification = classification if classification in COMMERCIAL | {"service", "collaboration", "ugc", "spam", "unknown"} else "unknown"
    if history_status not in {"verified_new", "repeat", "unknown"}:
        history_status = "unknown"
    if history_status == "verified_new" and not (
        evidence.get("history_complete") is True and evidence.get("identity_evidence_ref")
    ):
        history_status = "unknown"
    if attribution is None:
        prior = IgCommercialEpisodeEvent.objects.filter(
            dedupe_key__startswith=f"commercial-inquiry:v1:{locked_client.pk}:{row.pk}:",
        ).order_by("id").first()
        if prior:
            attribution = (prior.evidence or {}).get(ATTRIBUTION_KEY)
    observed = source_snapshot(attribution)
    # Cited sources must belong to this same durable client/account scope.
    ids = observed["source_message_ids"]
    if ids and InstagramBotMessage.objects.filter(pk__in=ids, client=locked_client, role="user").count() != len(ids):
        raise ValueError("attribution cites foreign or missing inbound evidence")
    episode = _ensure_episode_locked(locked_client)
    return append_episode_event(
        episode, event_type=INQUIRY_EVENT_TYPE,
        dedupe_key=f"commercial-inquiry:v1:{locked_client.pk}:{row.pk}:{classification}",
        source="own_customer_inbound",
        stage=locked_client.stage,
        evidence={OBSERVATION_KEY: {
            "version": VERSION, "source_message_id": row.pk,
            "occurred_at": (row.provider_created_at or row.created_at).isoformat(),
            "classification": classification, "history_status": history_status,
            "history_complete": evidence.get("history_complete") is True,
            "identity_evidence_ref": str(evidence.get("identity_evidence_ref") or "")[:160],
            "classification_basis": str(evidence.get("classification_basis") or "unknown")[:100],
        }, ATTRIBUTION_KEY: observed},
    )


def payment_attribution_snapshot(episode, occurred_at):
    """Last cited non-direct context inside this episode, frozen at payment."""
    from management.models import IgCommercialEpisodeEvent

    rows = list(IgCommercialEpisodeEvent.objects.filter(
        episode=episode, event_type=INQUIRY_EVENT_TYPE,
    ))
    rows.sort(key=lambda event: (_inquiry_occurred_at(event), event.pk))
    latest = None
    for event in rows:
        if _inquiry_occurred_at(event) > occurred_at:
            continue
        evidence = event.evidence or {}
        source = evidence.get(ATTRIBUTION_KEY) if isinstance(evidence, dict) else None
        if not isinstance(source, dict):
            continue
        if source.get("quality") == "ambiguous":
            latest = source
        elif source.get("quality") == "known" and source.get("source") not in {"", "direct", "organic_return"}:
            latest = source
    return source_snapshot(latest)


def _inquiry_occurred_at(event):
    observation = (event.evidence or {}).get(OBSERVATION_KEY) or {}
    at = parse_datetime(str(observation.get("occurred_at") or ""))
    if at is None or timezone.is_naive(at):
        return event.created_at
    return at


def _payment_observations(client, cutoff, *, purchase_units=None):
    """Read canonical units plus dated existing authority; keep gross and net apart."""
    from management.models import IgFunnelStepEvent, IgPaymentReviewDecision
    from management.services.bot_payment_truth import confirmed_purchase_units, manager_confirmed_review_q, verified_payment_deals

    units = {tuple(row["purchase_key"]): row for row in (confirmed_purchase_units(client) if purchase_units is None else purchase_units)}
    facts = {}
    manager_attribution = {}
    for evidence in IgFunnelStepEvent.objects.filter(
        episode__client=client, event_type="payment_confirmed", occurred_at__lt=cutoff,
    ).values_list("evidence", flat=True):
        if evidence.get("decision_id") and isinstance(evidence.get(ATTRIBUTION_KEY), dict):
            manager_attribution.setdefault(evidence["decision_id"], evidence[ATTRIBUTION_KEY])

    def add(key, at, amount, full, authority, episode_id=None, attribution=None):
        if at is None or at >= cutoff or amount is None or (amount < Decimal("200") and not full):
            return
        key = tuple(key)
        # Orderless reviews/deals have no demonstrated economic lineage.
        if key[0] != "order":
            return
        prior = facts.get(key)
        if prior is None or at < prior["paid_at"] or (at == prior["paid_at"] and authority == "recorded_provider_confirmation"):
            current = units.get(key)
            facts[key] = {
                "purchase_key": list(key), "order_id": key[1], "paid_at": at,
                "amount_at_confirmation": amount, "authority": authority,
                "net_amount": current.get("amount") if current else None,
                "retained_confirmed_purchase": current is not None,
                "episode_id": episode_id, "attribution": source_snapshot(attribution),
            }

    for deal in verified_payment_deals(client.deals.all()).select_related("payment_projection", "order"):
        if not deal.order_id:
            continue
        projection = getattr(deal, "payment_projection", None)
        amount = _amount(projection.gross_amount if projection else deal.amount)
        full = deal.pay_type == "online_full" and amount is not None and amount >= deal.amount
        add(("order", deal.order_id), projection.paid_at if projection else deal.paid_at,
            amount, full, "provider_confirmed")
    for review in client.payment_confirmation_reviews.filter(manager_confirmed_review_q()).distinct():
        if not review.order_id:
            continue
        for decision in review.decisions.filter(
            decision="manager_verified", verification_source="manager",
            actor_source__in=("management_user", "telegram_user"),
            verification_scope__in=("prepayment", "full_payment"),
        ).exclude(actor_external_id="").order_by("created_at", "id"):
            add(("order", review.order_id), decision.created_at, _amount(decision.confirmed_amount),
                decision.verification_scope == "full_payment", "manager_verified", attribution=manager_attribution.get(decision.pk))
    # The owner may later cancel a review/order. A source-qualified immutable
    # confirmation decision preserves the gross event; current units determine
    # net retention, never a rewriting of that historical confirmation.
    for decision in IgPaymentReviewDecision.objects.filter(
        client=client, decision="manager_verified", verification_source="manager",
        verification_scope__in=("prepayment", "full_payment"),
        actor_source__in=("management_user", "telegram_user"),
        review_status_after="confirmed", review__order_id__isnull=False,
    ).exclude(actor_external_id="").select_related("review"):
        add(("order", decision.review.order_id), decision.created_at, _amount(decision.confirmed_amount),
            decision.verification_scope == "full_payment", "recorded_manager_confirmation", attribution=manager_attribution.get(decision.pk))
    # An immutable fact recorded by the payment owner preserves gross success
    # after refund/cancel. A screenshot or a naked funnel event cannot qualify.
    for event in IgFunnelStepEvent.objects.filter(
        episode__client=client, event_type="payment_confirmed", occurred_at__lt=cutoff,
    ).select_related("episode__deal", "episode__intended_order"):
        evidence = event.evidence or {}
        provider_id = evidence.get("provider_event_id")
        deal = event.episode.deal
        order_id = event.episode.intended_order_id or (deal.order_id if deal else None)
        if not provider_id or not order_id:
            continue
        provider = client.payment_events.filter(pk=provider_id, amount_valid=True, provider_status="success").first()
        if provider is None or (deal and provider.deal_id != deal.pk):
            continue
        amount = _amount(provider.gross_amount)
        full = evidence.get("payment_kind") == "online_full" and amount is not None and amount >= (_amount(evidence.get("order_total_at_confirmation")) or Decimal("Infinity"))
        add(("order", order_id), event.occurred_at, amount,
            full, "recorded_provider_confirmation",
            event.episode_id, evidence.get(ATTRIBUTION_KEY))
        fact = facts.get(("order", order_id))
        if fact and fact["paid_at"] == event.occurred_at:
            fact["attribution"] = source_snapshot(evidence.get(ATTRIBUTION_KEY))
    discount_observations = {}
    for event in IgFunnelStepEvent.objects.filter(
        episode__client=client, event_type="order_created", occurred_at__lt=cutoff,
    ).order_by("occurred_at", "id"):
        evidence = event.evidence or {}
        amount = _amount(evidence.get("applied_discount_amount"))
        if evidence.get("order_id") and amount is not None:
            discount_observations.setdefault(evidence["order_id"], {
                "amount": str(amount), "promo_code_id": evidence.get("applied_promo_code_id"),
                "basis": "immutable_order_created_observation",
            })
    for fact in facts.values():
        fact["applied_discount"] = discount_observations.get(fact["order_id"])
    refund_observations = {}
    for projection in client.payment_projections.select_related("deal"):
        if projection.deal.order_id:
            refund_observations.setdefault(projection.deal.order_id, []).append({
                "projection_id": projection.pk, "truth": projection.truth,
                "refunded_amount": str(projection.refunded_amount),
            })
    for fact in facts.values():
        fact["refund_observations"] = refund_observations.get(fact["order_id"], [])
    return sorted(facts.values(), key=lambda fact: (fact["paid_at"], fact["order_id"]))


def client_purchase_metrics(client):
    """Existing canonical count and observed received cash; no invoice-value LTV."""
    from management.services.bot_payment_truth import confirmed_purchase_units

    units = confirmed_purchase_units(client)
    known = [unit["amount"] for unit in units if unit["amount"] is not None]
    unknown = sum(unit["amount"] is None for unit in units)
    return {
        "basis": "canonical_confirmed_purchase_units",
        "confirmed_purchase_count": len(units),
        "confirmed_order_purchase_count": sum(unit["order_id"] is not None for unit in units),
        "purchase_lineage_unknown_count": sum(unit["order_id"] is None for unit in units),
        "observed_net_received": str(sum(known, Decimal("0.00"))) if known else None,
        "amount_unknown_purchase_count": unknown,
        "amount_coverage": "partial" if unknown else "complete_observed_units",
        "lifetime_scope": "observed_bound_identity_only",
        "goods_revenue": None,
        "goods_revenue_reason": "received_cash_is_not_delivered_goods_revenue",
    }


def _window(rows, days, cutoff):
    matured = [row for row in rows if row["entered_at"] + timedelta(days=days) <= cutoff]
    paid = [row for row in matured if row["paid_at"] is not None and row["entered_at"] <= row["paid_at"] < row["entered_at"] + timedelta(days=days)]
    n, k = len(matured), len(paid)
    interval = None
    if n:
        z, p = 1.96, k / n
        center = (p + z*z/(2*n)) / (1 + z*z/n)
        margin = z * sqrt(p*(1-p)/n + z*z/(4*n*n)) / (1 + z*z/n)
        interval = [round(100*max(0, center-margin), 2), round(100*min(1, center+margin), 2)]
    return {
        "days": days, "entered": len(rows), "matured": n, "paid": k,
        "rate_pct": round(100*k/n, 2) if n else None,
        "wilson_95_pct": interval, "right_censored": len(rows)-n,
        "pending_paid": sum(row["paid_at"] is not None and row["entered_at"] <= row["paid_at"] < row["entered_at"] + timedelta(days=days) for row in rows if row not in matured),
        "late_paid": sum(row["paid_at"] is not None and row["paid_at"] >= row["entered_at"] + timedelta(days=days) for row in rows),
    }


def build_conversion_cohort_report(since=None, until=None, *, client_ids=None, observed_at=None):
    """First own commercial entry per proven client; no readiness/response gate.

    since/until select inquiry entry dates (half-open). observed_at defines the
    independent outcome cutoff; filtering to a past cohort never truncates its
    payment follow-up window.
    """
    from management.models import IgClient, IgCommercialEpisodeEvent, IgDeal, IgFunnelStepEvent, IgPaymentProjection, IgPaymentReviewDecision

    cutoff = observed_at or timezone.now()
    events = IgCommercialEpisodeEvent.objects.filter(
        event_type=INQUIRY_EVENT_TYPE,
    ).select_related("episode")
    if client_ids is not None:
        events = events.filter(episode__client_id__in=client_ids)
    # Deliberately include hidden/failed clients: visibility and bot success
    # must not remove a valid denominator after entry.
    first = {}
    coverage = {"observed_inquiries": 0, "unclassified": 0, "excluded_noncommercial": 0}
    for event in sorted(events, key=lambda event: (_inquiry_occurred_at(event), event.pk)):
        entered_at = _inquiry_occurred_at(event)
        if entered_at >= cutoff:
            continue
        evidence = event.evidence or {}
        observation = evidence.get(OBSERVATION_KEY)
        if not isinstance(observation, dict):
            continue
        coverage["observed_inquiries"] += 1
        classification = observation.get("classification", "unknown")
        if classification == "unknown":
            coverage["unclassified"] += 1
        elif classification not in COMMERCIAL:
            coverage["excluded_noncommercial"] += 1
        else:
            first.setdefault(event.episode.client_id, (event, observation, entered_at))
    segments = {"verified_new": [], "repeat": [], "unknown": []}
    # Most inquiries never reach a payment. Avoid running the canonical money
    # reader per unpaid lead; this broad candidate gate grants no authority.
    payment_candidates = set(IgDeal.objects.filter(client_id__in=first, paid_at__isnull=False).values_list("client_id", flat=True))
    payment_candidates.update(IgPaymentProjection.objects.filter(client_id__in=first).values_list("client_id", flat=True))
    payment_candidates.update(IgPaymentReviewDecision.objects.filter(client_id__in=first).values_list("client_id", flat=True))
    outcome_events = list(IgFunnelStepEvent.objects.filter(
        episode__client_id__in=first, occurred_at__lt=cutoff,
        event_type__in=("bot_replied_first", "manager_engaged", "payment_confirmed"),
    ).values("episode__client_id", "event_type", "occurred_at", "evidence"))
    outcomes_by_client = {}
    for event in outcome_events:
        outcomes_by_client.setdefault(event["episode__client_id"], []).append(event)
        if event["event_type"] == "payment_confirmed":
            payment_candidates.add(event["episode__client_id"])
    for client in IgClient.objects.filter(pk__in=first):
        event, observation, entered_at = first[client.pk]
        if (since is not None and entered_at < since) or (until is not None and entered_at >= until):
            continue
        segment = observation.get("history_status", "unknown")
        if segment not in segments:
            segment = "unknown"
        if segment == "verified_new" and not (observation.get("history_complete") is True and observation.get("identity_evidence_ref")):
            segment = "unknown"
        payments = _payment_observations(client, cutoff) if client.pk in payment_candidates else []
        if payments and payments[0]["paid_at"] < entered_at:
            segment = "repeat"
        payment = next((fact for fact in payments if fact["paid_at"] >= entered_at), None)
        participation_events = [item for item in outcomes_by_client.get(client.pk, []) if item["occurred_at"] >= entered_at and (payment is None or item["occurred_at"] <= payment["paid_at"])]
        bot = any(item["event_type"] == "bot_replied_first" and (item["evidence"] or {}).get("provider_confirmed") is True for item in participation_events)
        human = bool(payment and "manager" in payment["authority"]) or any(item["event_type"] == "manager_engaged" and (item["evidence"] or {}).get("manager_message_id") for item in participation_events)
        participation = "hybrid" if bot and human else "bot_only" if bot else "human_assisted" if human else "no_response_evidence"
        segments[segment].append({
            "client_id": client.pk, "episode_id": event.episode_id,
            "entered_at": entered_at, "classification": observation["classification"],
            "paid_at": payment["paid_at"] if payment else None,
            "acquisition_source": source_snapshot((event.evidence or {}).get(ATTRIBUTION_KEY)),
            "first_purchase": payment,
            "participation": participation,
        })
    result = {
        "version": VERSION, "timezone": "Europe/Kyiv", "observed_at": cutoff.isoformat(),
        "entry_period": {"since": since.isoformat() if since else None, "until": until.isoformat() if until else None},
        "unit": "unique_verified_new_client_first_commercial_inquiry",
        "success": "canonical_order_confirmed_200_or_full",
        "primary_days": 14, "coverage": coverage,
        "window_boundary": "entry_inclusive_deadline_exclusive",
        "history_basis": "explicit_complete_history_identity_evidence",
        "attribution_basis": "immutable_inquiry_and_payment_observations",
        "attribution_roles": {"acquisition": "first_commercial_entry", "purchase": "last_proven_non_direct_in_episode_before_first_verified_payment"},
        "legacy_attribution": "unknown_not_reconstructed",
        "financial_basis": "gross_success_separate_from_current_net_retention",
        "segments": {},
    }
    for segment, rows in segments.items():
        sources = {}
        purchase_sources = {}
        for row in rows:
            source = row["acquisition_source"]
            key = (source["quality"], source["source"], source["ad_id"])
            group = sources.setdefault(key, {"quality": key[0], "source": key[1], "ad_id": key[2], "rows": []})
            group["rows"].append(row)
            if row["first_purchase"]:
                purchase_source = row["first_purchase"]["attribution"]
                purchase_key = (purchase_source["quality"], purchase_source["source"], purchase_source["ad_id"])
                purchase_group = purchase_sources.setdefault(purchase_key, {"quality": purchase_key[0], "source": purchase_key[1], "ad_id": purchase_key[2], "observed_paid_clients": 0})
                purchase_group["observed_paid_clients"] += 1
        dimensions = {}
        for dimension in ("classification", "participation"):
            dimensions[dimension] = {
                value: _window([row for row in rows if row[dimension] == value], 14, cutoff)
                for value in sorted({row[dimension] for row in rows})
            }
        discounts = {"applied": 0, "not_applied": 0, "unknown": 0}
        for row in rows:
            observation = row["first_purchase"].get("applied_discount") if row["first_purchase"] else None
            discounts["unknown" if observation is None else "applied" if _amount(observation["amount"]) > 0 else "not_applied"] += 1
        result["segments"][segment] = {
            "entered": len(rows), "windows": {str(days): _window(rows, days, cutoff) for days in (7, 14, 30)},
            "gross_paid_clients": sum(row["first_purchase"] is not None for row in rows),
            "retained_paid_clients": sum(bool(row["first_purchase"] and row["first_purchase"]["retained_confirmed_purchase"]) for row in rows),
            "refund_or_cancelled_clients": sum(bool(row["first_purchase"] and not row["first_purchase"]["retained_confirmed_purchase"]) for row in rows),
            "partial_refund_clients": sum(bool(row["first_purchase"] and any(observation["truth"] == "partially_refunded" for observation in row["first_purchase"]["refund_observations"])) for row in rows),
            "provider_refund_evidence_clients": sum(bool(row["first_purchase"] and any(_amount(observation["refunded_amount"]) > 0 for observation in row["first_purchase"]["refund_observations"])) for row in rows),
            "first_confirmation_received": str(sum((row["first_purchase"]["amount_at_confirmation"] for row in rows if row["first_purchase"]), Decimal("0.00"))),
            "first_purchase_current_net_received": str(sum((row["first_purchase"]["net_amount"] for row in rows if row["first_purchase"] and row["first_purchase"]["net_amount"] is not None), Decimal("0.00"))),
            "net_amount_unknown_clients": sum(bool(row["first_purchase"] and row["first_purchase"]["net_amount"] is None) for row in rows),
            "acquisition_sources": [
                {"quality": group["quality"], "source": group["source"], "ad_id": group["ad_id"], "cohort": _window(group["rows"], 14, cutoff)}
                for group in sources.values()
            ],
            "purchase_sources": list(purchase_sources.values()),
            "dimensions": dimensions,
            "applied_discount_coverage": discounts,
            "participation_basis": "provider_confirmed_reply_or_actual_manager_message_or_verified_decision",
        }
    result["primary"] = result["segments"]["verified_new"]["windows"]["14"]
    result["measurement_status"] = "measured" if coverage["observed_inquiries"] else "not_measured"
    result["limitations"] = ["Legacy/unclassified inquiries are not guessed into verified-new cohorts.", "Orderless legacy confirmations have unknown purchase lineage.", "Observed association is not causal uplift or complete cross-channel LTV."]
    return result
