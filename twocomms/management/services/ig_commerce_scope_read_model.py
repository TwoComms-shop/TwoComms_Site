"""Bounded current positions and actual order history; no work materialization."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from secrets import compare_digest
from types import SimpleNamespace
import json

from django.db import DatabaseError, connection, transaction
from django.db.models import Q
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from management.services.ig_admin_state_capture import valid_identifier
from management.services.ig_client_state_card import assemble_client_state, CAPTURE_SCOPE_KEYS

MAX_READ_QUERIES = 96
HISTORY_LIMIT = 20
MAX_ORDER_ITEMS = 32


class ScopeReadError(RuntimeError):
    pass


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


def _owner(client_id):
    from management.models import IgClient, IgFunnelResetAudit
    row = IgClient.objects.filter(pk=client_id).values("pk", "igsid", "current_commercial_episode_id",
        "reply_permission_epoch", "privacy_erasure_started_at").first()
    if row is not None:
        row["reset"] = IgFunnelResetAudit.objects.filter(client_id=client_id).order_by("-pk").values(
            "pk", "reset_after_message_id").first()
    return row


def _related(row, name):
    return getattr(row, name, None)


def _order_binding(client_id, order_id):
    from management.models import IgOrderAssignment, IgOrderAttribution, IgCommercialEpisode, IgDeal, IgPaymentConfirmationReview
    from orders.models import Order
    if order_id is None:
        return {"status": "unbound", "order_id": None, "reason": ""}
    if not Order.objects.filter(pk=order_id).exists():
        return {"status": "unknown", "order_id": order_id, "reason": "current_order_missing"}
    assignment = IgOrderAssignment.objects.filter(order_id=order_id).values("client_id", "unassigned_at", "version").first()
    if assignment is not None:
        proven = assignment["client_id"] == client_id and assignment["unassigned_at"] is None
        return {"status": "confirmed" if proven else "unknown", "order_id": order_id,
            "source": "active_assignment", "version": assignment["version"], "reason": "" if proven else "current_order_owner_not_captured"}
    attribution = IgOrderAttribution.objects.filter(order_id=order_id).values_list("client_id", flat=True).first()
    episode = IgCommercialEpisode.objects.filter(intended_order_id=order_id).values_list("client_id", flat=True).first()
    legacy_conflict = (attribution not in (None, client_id) or episode not in (None, client_id)
        or IgDeal.objects.filter(order_id=order_id).exclude(client_id=client_id).exists()
        or IgPaymentConfirmationReview.objects.filter(order_id=order_id).exclude(client_id=client_id).exists())
    proven = not legacy_conflict and client_id in (attribution, episode)
    return {"status": "confirmed" if proven else "unknown", "order_id": order_id, "source": "exact_legacy_binding",
        "reason": "" if proven else "current_order_owner_not_captured"}


def _payment(order, episode, client_id, *, projection, decision):
    from management.services.ig_commercial_episodes import payment_truth_snapshot
    unknown = {"status": "unknown", "label": "Оплату за джерелами не підтверджено",
        "order_id": order.pk, "recorded_status": order.payment_status, "snapshot": {}}
    if episode is None or episode.client_id != client_id or episode.intended_order_id != order.pk:
        return unknown
    deal, review = episode.deal, episode.primary_payment_review
    if any(item is not None and (item.client_id != client_id or item.order_id not in (None, order.pk)) for item in (deal, review)):
        return unknown
    if review is not None and review.deal_id not in (None, episode.deal_id):
        return unknown
    snapshot = payment_truth_snapshot(episode=episode, deal=deal, review=review, order=order,
        projection=projection or SimpleNamespace(), decision=decision or SimpleNamespace(),
        allow_deal_review_fallback=False)
    if snapshot.get("order_id") != order.pk or snapshot.get("episode_id") != episode.pk:
        return unknown
    try:
        paid = Decimal(snapshot.get("confirmed_paid_amount") or "0") > 0
    except InvalidOperation:
        paid = False
    label = "Оплата потребує звірки" if snapshot.get("needs_reconciliation") else "Оплату підтверджено" if paid else "Оплату за джерелами не підтверджено"
    return {"status": "reconciliation" if snapshot.get("needs_reconciliation") else "confirmed" if paid else "unknown",
        "label": label, "order_id": order.pk, "recorded_status": order.payment_status, "snapshot": snapshot}


def _unknown_revenue(reason):
    return {"status": "unknown", "label": "Підтверджена виручка", "currency": "UAH", "currency_code": 980,
        "value": None, "gross": None, "refunded": None, "reason": reason,
        "refund_coverage": "unknown", "order_ids": [], "proofs": []}


def _payment_context(candidates):
    """Batch existing receipt owners; never query a latest deal for a client."""
    from management.models import IgDeal, IgPaymentProjection, IgPaymentReviewDecision, IgCheckoutProposal
    ids = [order.pk for order in candidates]
    deals = list(IgDeal.objects.filter(order_id__in=ids).order_by("pk")[:161]) if ids else []
    if len(deals) > 160:
        raise ScopeReadError("order_ownership_budget_exceeded")
    # Include the exact episode's unbound deal for the existing payment label;
    # unbound deals cannot qualify lifetime revenue.
    deal_ids = {deal.pk for deal in deals} | {episode.deal_id for order in candidates
        if (episode := _related(order, "instagram_commercial_episode")) and episode.deal_id}
    projections = {row.deal_id: row for row in IgPaymentProjection.objects.filter(deal_id__in=deal_ids)
        .select_related("last_event")} if deal_ids else {}
    review_ids = {episode.primary_payment_review_id for order in candidates
        if (episode := _related(order, "instagram_commercial_episode")) and episode.primary_payment_review_id}
    decisions = list(IgPaymentReviewDecision.objects.filter(review_id__in=review_ids)
        .order_by("review_id", "-pk")[:161]) if review_ids else []
    if len(decisions) > 160:
        raise ScopeReadError("payment_decision_budget_exceeded")
    latest = {}
    for decision in decisions:
        latest.setdefault(decision.review_id, decision)
    proposals = list(IgCheckoutProposal.objects.filter(deal_id__in=deal_ids)
        .select_related("payment_attempt", "winner_invoice_generation")
        .only("id", "deal_id", "client_id", "commercial_episode_id", "currency", "payment_attempt_id",
            "winner_invoice_generation_id", "assisted_checkout_v2", "payment_attempt__id", "payment_attempt__order_id",
            "payment_attempt__status", "payment_attempt__reference", "payment_attempt__monobank_invoice_id",
            "payment_attempt__payment_amount", "payment_attempt__event_state", "payment_attempt__provider_recheck_state",
            "winner_invoice_generation__id", "winner_invoice_generation__proposal_id", "winner_invoice_generation__payment_attempt_id")
        .order_by("pk")[:161]) if deal_ids else []
    if len(proposals) > 160:
        raise ScopeReadError("payment_proof_budget_exceeded")
    return deals, projections, latest, proposals


def _net_revenue(order, episode, attribution, client_id, deals, projections, proposals, *, ownership_conflict):
    """Revenue is the owned canonical receipt head, including admitted refunds.

    Order totals and chat payment reviews explain service state; they never
    supply this amount. This is an observation of the ledger at capture time,
    not a claim that no future refund is possible.
    """
    from management.models import IgDeal, provider_evidence_signature
    owned = [deal for deal in deals if deal.order_id == order.pk]
    if ownership_conflict or len(owned) != 1 or owned[0].client_id != client_id:
        return _unknown_revenue("payment_order_owner_ambiguous")
    deal = owned[0]
    if ((episode is not None and (episode.client_id != client_id or episode.intended_order_id != order.pk
            or episode.deal_id != deal.pk)) or (attribution is not None and attribution.deal_id not in (None, deal.pk))):
        return _unknown_revenue("payment_order_scope_mismatch")
    projection = projections.get(deal.pk)
    event = projection.last_event if projection is not None else None
    if projection is None or event is None:
        return _unknown_revenue("payment_projection_missing")
    if (projection.client_id != client_id or event.client_id != client_id or event.deal_id != deal.pk
            or event.provider != "monobank" or event.source not in {"provider", "provider_pull", "provider_webhook", "signed_webhook", "provider_attempt"}
            or not event.invoice_id or event.amount_valid is not True or not projection.paid_at
            or not projection.provider_modified_at or event.provider_modified_at != projection.provider_modified_at):
        return _unknown_revenue("payment_receipt_scope_unknown")
    if not isinstance(event.evidence, dict) or not isinstance(event.payload_digest, str) or len(event.payload_digest) != 64:
        return _unknown_revenue("payment_receipt_integrity_unknown")
    if deal.currency != "UAH" or event.currency != "UAH" or str(event.evidence.get("ccy", "980")) != "980":
        return _unknown_revenue("payment_currency_mismatch")
    expected = provider_evidence_signature(deal_id=deal.pk, client_id=client_id, provider=event.provider,
        source=event.source, invoice_id=event.invoice_id, provider_status=event.provider_status, payload_digest=event.payload_digest)
    if not isinstance(event.evidence, dict) or not compare_digest(str(event.evidence.get("signature") or ""), expected):
        return _unknown_revenue("payment_receipt_integrity_unknown")
    if projection.needs_reconciliation or not projection.reconciled_at:
        return _unknown_revenue("payment_refund_coverage_unknown")
    try:
        gross, refunded = Decimal(projection.gross_amount), Decimal(projection.refunded_amount)
        event_gross = Decimal(event.gross_amount)
        # Legacy success may omit finalAmount. Only an admitted, unrefunded
        # success can fill that absence from its canonical gross receipt.
        event_final = Decimal(event.final_amount) if event.final_amount is not None else event_gross
        event_refund = Decimal(event.refunded_amount) if event.refunded_amount is not None else Decimal("0")
        valid = all(value.is_finite() and value == value.quantize(Decimal(".01"))
            for value in (gross, refunded, event_gross, event_final, event_refund))
        valid = valid and gross > 0 and 0 <= refunded <= gross and (event_gross, event_final, event_refund) == (gross, gross-refunded, refunded)
    except (InvalidOperation, TypeError, ValueError):
        valid = False
    if not valid:
        return _unknown_revenue("payment_receipt_amount_unknown")
    truth = projection.truth
    if not ((truth == IgDeal.PaymentTruth.CONFIRMED and refunded == 0 and event.provider_status == "success")
        or (truth == IgDeal.PaymentTruth.PARTIALLY_REFUNDED and 0 < refunded < gross and event.provider_status == "success")
        or (truth == IgDeal.PaymentTruth.REFUNDED and refunded == gross and event.provider_status == "success")
        or (truth == IgDeal.PaymentTruth.REVERSED and refunded == gross and event.provider_status == "reversed")):
        return _unknown_revenue("payment_receipt_truth_unknown")
    hosted = [proposal for proposal in proposals if proposal.deal_id == deal.pk]
    if hosted:
        matching = [proposal for proposal in hosted if proposal.payment_attempt_id
            and proposal.payment_attempt.monobank_invoice_id == event.invoice_id]
        if len(matching) != 1:
            return _unknown_revenue("payment_hosted_owner_unknown")
        proposal = matching[0]; attempt = proposal.payment_attempt; winner = proposal.winner_invoice_generation
        if not isinstance(attempt.event_state, dict):
            return _unknown_revenue("payment_refund_coverage_unknown")
        marker = attempt.event_state.get("payment_settlement_review")
        if (proposal.client_id != client_id or proposal.currency != "UAH" or episode is None
            or proposal.commercial_episode_id != episode.pk or attempt.order_id != order.pk or attempt.status != "converted"
            or attempt.payment_amount != gross or (event.evidence or {}).get("attempt_id") != attempt.pk
            or (event.evidence or {}).get("attempt_reference") != attempt.reference
            or attempt.provider_recheck_state not in {"", "resolved"}
            or (proposal.assisted_checkout_v2 and winner is None)
            or (winner is not None and (winner.proposal_id != proposal.pk or winner.payment_attempt_id != attempt.pk))):
            return _unknown_revenue("payment_hosted_scope_unknown")
        if marker and (not isinstance(marker, dict) or marker.get("reason") != "settlement_partial_refund_review"
            or marker.get("attempt_id") != attempt.pk or marker.get("invoice_id") != event.invoice_id):
            return _unknown_revenue("payment_refund_coverage_unknown")
    elif event.source == "provider_attempt" or deal.invoice_id != event.invoice_id:
        return _unknown_revenue("payment_invoice_owner_unknown")
    proof = {"order_id": order.pk, "deal_id": deal.pk, "projection_id": projection.pk, "event_id": event.pk,
        "provider_modified_at": projection.provider_modified_at.isoformat(), "truth": truth}
    return {"status": "confirmed", "label": "Підтверджена виручка", "currency": "UAH", "currency_code": 980,
        "value": f"{gross-refunded:.2f}", "gross": f"{gross:.2f}", "refunded": f"{refunded:.2f}", "reason": "",
        "refund_coverage": "confirmed_projection_as_of_capture", "order_ids": [order.pk], "proofs": [proof]}


def _lifetime_revenue(orders, coverage):
    if coverage != "complete":
        return _unknown_revenue("order_history_incomplete")
    if not orders:
        return _unknown_revenue("payment_history_empty")
    revenue = [order["revenue"] for order in orders]
    unknown = next((row for row in revenue if row["status"] != "confirmed"), None)
    if unknown:
        return _unknown_revenue(unknown["reason"])
    return {"status": "confirmed", "label": "Підтверджена виручка", "currency": "UAH", "currency_code": 980,
        "value": f"{sum((Decimal(row['value']) for row in revenue), Decimal('0')):.2f}",
        "gross": f"{sum((Decimal(row['gross']) for row in revenue), Decimal('0')):.2f}",
        "refunded": f"{sum((Decimal(row['refunded']) for row in revenue), Decimal('0')):.2f}", "reason": "",
        "refund_coverage": "confirmed_projection_as_of_capture", "order_ids": [order["id"] for order in orders],
        "proofs": [proof for row in revenue for proof in row["proofs"]]}


def _history(client_id):
    from orders.models import Order, OrderItem
    from management.models import IgClient, IgPaymentConfirmationReview
    from management.services.bot_payment_truth import historical_purchase_confirmation
    archive = historical_purchase_confirmation(IgClient.objects.only("id").get(pk=client_id))
    order_fields = {"id", "order_number", "created", "status", "payment_status", "total_sum", "discount_amount"}
    deferred = [field.name for field in Order._meta.concrete_fields if field.name not in order_fields]
    candidates = list(Order.objects.filter(
        Q(instagram_assignment__client_id=client_id) | Q(instagram_attribution__client_id=client_id)
        | Q(instagram_commercial_episode__client_id=client_id) | Q(ig_deals__client_id=client_id)
        | Q(instagram_payment_reviews__client_id=client_id) | Q(instagram_assignment_events__from_client_id=client_id)
        | Q(instagram_assignment_events__to_client_id=client_id)
    ).distinct().select_related("instagram_assignment", "instagram_attribution",
        "instagram_commercial_episode__deal", "instagram_commercial_episode__primary_payment_review",
        "instagram_commercial_episode__primary_payment_review__deal")
        .defer(*deferred).order_by("-created", "-pk")[:HISTORY_LIMIT + 1])
    bounded = len(candidates) > HISTORY_LIMIT
    candidates = candidates[:HISTORY_LIMIT]
    ids = [order.pk for order in candidates]
    deals, projections, decisions, proposals = _payment_context(candidates)
    deal_owners = [{"order_id": deal.order_id, "client_id": deal.client_id} for deal in deals]
    review_owners = list(IgPaymentConfirmationReview.objects.filter(order_id__in=ids).values("order_id", "client_id")[:161])
    if len(deal_owners) > 160 or len(review_owners) > 160:
        raise ScopeReadError("order_ownership_budget_exceeded")
    legacy_owners = deal_owners + review_owners
    conflicts = {item["order_id"] for item in legacy_owners if item["client_id"] != client_id}
    orders, unresolved = [], []
    for order in candidates:
        assignment = _related(order, "instagram_assignment")
        attribution = _related(order, "instagram_attribution")
        episode = _related(order, "instagram_commercial_episode")
        current_assignment = bool(assignment and assignment.client_id == client_id and assignment.unassigned_at is None)
        binding_conflict = order.pk in conflicts or (attribution is not None and attribution.client_id != client_id) or (episode is not None and episode.client_id != client_id)
        legacy_owned = assignment is None and not binding_conflict and (
            attribution is not None and attribution.client_id == client_id or episode is not None and episode.client_id == client_id)
        payment = _payment(order, episode, client_id, projection=projections.get(episode.deal_id) if episode else None,
            decision=decisions.get(episode.primary_payment_review_id) if episode else None)
        if not (current_assignment or legacy_owned):
            # A corrected binding cannot be revived by an old paid review. Keep
            # the qualified historical evidence separate from owned orders.
            unresolved.append({"order_id": order.pk, "reason": "order_ownership_changed_or_conflicting",
                "historical_payment": payment, "editable": False})
            continue
        orders.append({"id": order.pk, "kind": "physical_order", "number": order.order_number,
            "created_at": order.created.isoformat(), "created_label": order.created.strftime("%d.%m.%Y"),
            "status": order.status, "status_label": order.get_status_display(), "payment": payment,
            "ownership": {"source": "active_assignment" if current_assignment else "exact_legacy_binding",
                "assignment_id": assignment.pk if assignment else None, "assignment_version": assignment.version if assignment else None,
                "attribution_id": attribution.pk if attribution else None, "episode_id": episode.pk if episode else None},
            "revenue": _net_revenue(order, episode, attribution, client_id, deals, projections, proposals,
                ownership_conflict=binding_conflict or bool(assignment and assignment.version > 1)), "items": []})
    # Read item material only after ownership admission, never for an unknown
    # or corrected link. No phone/name/address is part of this read model.
    items = list(OrderItem.objects.filter(order_id__in=[order["id"] for order in orders]).select_related("color_variant__color")
        .order_by("order_id", "pk")[:HISTORY_LIMIT * MAX_ORDER_ITEMS + 1])
    if len(items) > HISTORY_LIMIT * MAX_ORDER_ITEMS:
        raise ScopeReadError("order_items_budget_exceeded")
    for order in orders:
        order_items = [item for item in items if item.order_id == order["id"]]
        if len(order_items) > MAX_ORDER_ITEMS:
            raise ScopeReadError("order_items_budget_exceeded")
        order["items"] = [{"id": item.pk, "title": item.title, "product_id": item.product_id, "size": item.size,
            "color": item.color_name_custom or (item.color_variant.color.name if item.color_variant else ""),
            "fit": item.fit_option_label or item.fit_option_code, "quantity": item.qty} for item in order_items]
    coverage = "bounded" if bounded else "uncertain" if unresolved or archive.get("confirmed") else "complete"
    return {"coverage": coverage, "limit": HISTORY_LIMIT, "orders": orders, "unresolved": unresolved,
        "completed_order_count": sum(order["status"] == "done" for order in orders) if coverage == "complete" else None,
        "shown_completed_order_count": sum(order["status"] == "done" for order in orders), "archived_purchase": archive,
        "ltv": _lifetime_revenue(orders, coverage)}


def _history_source(record):
    source = {"status": "confirmed", "authority": record.get("authority", "customer_source"),
        "source_refs": [{"kind": "message", "id": record["source_message_id"], "digest": record["source_digest"],
            "event_at": record.get("event_at")}]}
    if source["authority"] == "audited_correction":
        source["source_refs"].append({"kind": "commerce_transition", "id": record["transition_id"], "authority": "audited_correction"})
    if record.get("copied_from"):
        source["copied_from"] = deepcopy(record["copied_from"])
    return source


def _line_history(item):
    records = item.get("history") or []
    coverage = deepcopy(item.get("history_coverage") or {"complete": False, "event_limit": 8, "reason": "history_not_captured"})
    if not isinstance(records, list) or len(records) > 8:
        raise ScopeReadError("line_history_scope_invalid")
    allowed = {"size", "color", "fit_option_code", "quantity", "product_id", "garment_type"}
    for record in records:
        if (not isinstance(record, dict) or record.get("field") not in allowed
            or record.get("recipient_id") != item["recipient_id"] or not valid_identifier(record.get("source_message_id"))
            or not valid_identifier(record.get("transition_id")) or not isinstance(record.get("source_digest"), str)
            or len(record["source_digest"]) != 64):
            raise ScopeReadError("line_history_scope_invalid")
    rows = []
    for record in records:
        # Initial values are already in the current card. The disclosure keeps
        # genuine superseded changes or a uniquely proven copy origin only.
        if not (record.get("previous_source") or record.get("superseded_by") or record.get("copied_from")):
            continue
        previous = record.get("previous_source")
        prior_record = next((candidate for candidate in records if previous and candidate["transition_id"] == previous.get("transition_id")
            and candidate["field"] == record["field"] and candidate.get("product_id") == record.get("product_id")), None)
        rows.append({"field": record["field"], "value": deepcopy(record["value"]), "source": _history_source(record),
            "transition_id": record["transition_id"], "event_at": record.get("event_at"),
            "previous": deepcopy(record.get("previous")) if prior_record else None,
            "previous_source": _history_source(prior_record) if prior_record else None,
            "superseded_by": record.get("superseded_by"), "copied_from": deepcopy(record.get("copied_from")), "read_only": True})
    return {"status": "captured", "items": rows, "coverage": coverage, "read_only": True}


def _current_lines(capture, client_id, now):
    from storefront.models import Product
    if capture.get("status") != "captured":
        return {"status": "unavailable", "reason": capture.get("reason") or "selection_source_unavailable",
            "scope": {"client_id": client_id, "episode_id": (capture.get("scope") or {}).get("episode_id"),
                "order_id": (capture.get("scope") or {}).get("order_id")}, "lines": [], "active_line_id": None}
    scope = capture.get("scope") or {}
    raw_lines = capture.get("lines")
    if scope.get("client_id") != client_id or not isinstance(raw_lines, list) or len(raw_lines) > 16:
        raise ScopeReadError("current_selection_scope_invalid")
    lines = []
    seen = set()
    for item in raw_lines:
        if (not isinstance(item, dict) or not isinstance(item.get("line_id"), str) or not item["line_id"]
            or len(item["line_id"]) > 128 or item["line_id"] in seen or not isinstance(item.get("recipient_id"), str)
            or not item["recipient_id"] or len(item["recipient_id"]) > 128 or not isinstance(item.get("index"), int)
            or isinstance(item["index"], bool) or not 0 <= item["index"] < 16):
            raise ScopeReadError("current_selection_scope_invalid")
        seen.add(item["line_id"])
        selection = item.get("source_selection") or {}
        if not isinstance(selection, dict):
            raise ScopeReadError("current_selection_scope_invalid")
        boundary = {**scope, "line_id": item.get("line_id"), "recipient_id": item.get("recipient_id"),
            "historical": False, "view_mode": "current_commerce_scope",
            "source_watermark": capture.get("source_watermark") or (capture.get("fence") or {}).get("source_watermark") or {}}
        binding = {key: boundary.get(key) for key in CAPTURE_SCOPE_KEYS}
        state = assemble_client_state(boundary=boundary, components={"source_selection": selection,
            "source_selection_binding": binding}, captured_at=now).as_dict()
        fields = {key.split(".", 1)[1]: value for key, value in state["slots"].items() if key.startswith("choice.")}
        for key, field in fields.items():
            evidence = (item.get("evidence") or {}).get(key) or {}
            if field["status"] == "confirmed" and evidence.get("copied_from"):
                field["copied_from"] = deepcopy(evidence["copied_from"])
        lines.append({"line_id": item.get("line_id"), "recipient_id": item.get("recipient_id"), "index": item.get("index"),
            "active": item.get("line_id") == capture.get("active_line_id"), "fields": fields,
            "history": _line_history(item), "defaults": deepcopy(item.get("defaults") or {}),
            "unsupported_selectors": deepcopy(item.get("unsupported_selectors") or []), "editable": False})
    product_ids = [row["fields"]["product_id"]["value"] for row in lines if row["fields"]["product_id"]["status"] == "confirmed"]
    titles = dict(Product.objects.filter(pk__in=product_ids).values_list("pk", "title"))
    for row in lines:
        product = row["fields"]["product_id"]
        title = titles.get(product["value"]) if product["status"] == "confirmed" else None
        garment = row["fields"].get("garment_type") or {}
        row["title"] = title or ({"tshirt": "Футболка", "hoodie": "Худі"}.get(garment.get("value"))
            if garment.get("status") == "confirmed" else None)
        row["title_source"] = "catalog" if title else "source_requirement" if row["title"] else "unknown"
    return {"status": "captured", "scope": scope, "selection_revision": capture.get("selection_revision"),
        "active_line_id": capture.get("active_line_id"), "lines": lines, "omissions": capture.get("omissions") or []}


def _capture(client_id, selected_order_id, now):
    from management.services.ig_commerce_projection import capture_current_selection_lines
    owner = _owner(client_id)
    if owner is None:
        raise ScopeReadError("client_missing")
    if owner["privacy_erasure_started_at"]:
        raise ScopeReadError("client_erasing")
    source = capture_current_selection_lines(client_id, now=now)
    if not isinstance(source, dict):
        raise ScopeReadError("current_selection_scope_invalid")
    if source.get("status") == "conflict":
        raise ScopeReadError("commerce_scope_changed")
    if source.get("reason") in {"client_erasing", "client_missing"}:
        raise ScopeReadError(source["reason"])
    if source.get("status") == "captured" and (source.get("scope") or {}).get("episode_id") != owner["current_commercial_episode_id"]:
        raise ScopeReadError("current_episode_changed")
    current_source = source
    if source.get("status") != "captured" and not source.get("scope"):
        from management.models import IgCommercialEpisode
        episode = IgCommercialEpisode.objects.filter(pk=owner["current_commercial_episode_id"], client_id=client_id).values("intended_order_id").first()
        current_source = {**source, "scope": {"client_id": client_id, "episode_id": owner["current_commercial_episode_id"],
            "order_id": (episode or {}).get("intended_order_id")}}
    current = _current_lines(current_source, client_id, now)
    history = _history(client_id)
    owned_ids = {order["id"] for order in history["orders"]}
    pinned_order = (current.get("scope") or {}).get("order_id")
    current["order_binding"] = _order_binding(client_id, pinned_order)
    active = next((line for line in current["lines"] if line["active"]), None)
    size = (active or {}).get("fields", {}).get("size") or {}
    proven_size = size.get("status") == "confirmed" or (size.get("authority") == "audited_correction" and size.get("omission_reason") == "requirement_explicitly_cleared")
    current["editable"] = (selected_order_id is None and current.get("status") == "captured" and active is not None
        and proven_size and current["order_binding"]["status"] in {"unbound", "confirmed"})
    current["editable_scope"] = {**current.get("scope", {}), "line_id": active["line_id"], "recipient_id": active["recipient_id"],
        "selection_revision": current.get("selection_revision")} if current["editable"] else None
    for line in current["lines"]:
        line["editable"] = current["editable"] and line["active"]
    if selected_order_id is not None and selected_order_id not in owned_ids:
        raise ScopeReadError("selected_order_not_captured")
    # Both the source adapter and order owner are recomputed before publication;
    # no latest-row fallback can splice another customer's order into this card.
    if (_digest(owner) != _digest(_owner(client_id)) or _digest(source) != _digest(capture_current_selection_lines(client_id, now=now))
        or _digest(history) != _digest(_history(client_id)) or current["order_binding"] != _order_binding(client_id, pinned_order)):
        raise ScopeReadError("commerce_scope_changed")
    return {"schema": "commerce-scope.v1", "status": "captured", "reason": "", "view_mode": "current_commerce_scope",
        "client_id": client_id, "as_of": now.isoformat(), "current": current, "history": history,
        "selection": {"order_id": selected_order_id, "mode": "order" if selected_order_id is not None else "current"}}


def capture_commerce_scope(client_id, *, selected_order_id=None, now=None):
    """Read existing owners only; reject accidental writes within a savepoint."""
    now = now or timezone.now()
    reason = ""; reads = 0
    def readonly(execute, sql, params, many, context):
        nonlocal reason, reads
        verb = sql.lstrip().split(None, 1)[0].upper()
        if verb not in {"SELECT", "BEGIN", "SAVEPOINT", "RELEASE", "COMMIT", "ROLLBACK"}:
            reason = "commerce_scope_side_effect_rejected"; raise ScopeReadError(reason)
        if verb == "SELECT":
            reads += 1
            if reads > MAX_READ_QUERIES:
                reason = "commerce_scope_read_budget_exceeded"; raise ScopeReadError(reason)
        return execute(sql, params, many, context)
    try:
        if not valid_identifier(client_id) or selected_order_id is not None and not valid_identifier(selected_order_id):
            raise ScopeReadError("commerce_scope_identity_invalid")
        with connection.execute_wrapper(readonly):
            with transaction.atomic():
                result = _capture(client_id, selected_order_id, now)
                if reason:
                    raise ScopeReadError(reason)
    except (ScopeReadError, DatabaseError, ImportError, ObjectDoesNotExist) as exc:
        code = str(exc) if isinstance(exc, ScopeReadError) else "commerce_scope_source_unavailable" if isinstance(exc, (ImportError, ObjectDoesNotExist)) else "commerce_scope_read_unavailable"
        result = {"schema": "commerce-scope.v1", "status": "conflict" if code in {"commerce_scope_changed", "current_episode_changed", "selected_order_not_captured"} else "unavailable",
            "reason": code, "view_mode": "current_commerce_scope", "client_id": None, "as_of": now.isoformat(),
            "current": {}, "history": {}, "selection": {}}
    result["diagnostics"] = {"read_only": True, "read_queries": reads, "max_read_queries": MAX_READ_QUERIES}
    return result
