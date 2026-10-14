"""Owned pre-bank checkout mutations, using the existing quote/proposal engine.

These are checkout-link-holder facts. They never impersonate Instagram ingress.
The immutable receipt is also the source witness consumed by canonical readers.
"""
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import secrets
import uuid

from django.conf import settings
from django.core.signing import salted_hmac
from django.db import transaction
from django.utils import timezone


SCHEMA = "ig-checkout-cart-amendment.v1"
AUTHORITY = "checkout_link_holder"
_ISSUER = object()


class CheckoutAmendmentError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _fail(reason):
    raise CheckoutAmendmentError(reason)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False, default=str).encode()).hexdigest()


def _sign(kind, value, secret=None):
    return salted_hmac(f"management.{SCHEMA}.{kind}", _json(value), secret=secret, algorithm="sha256").hexdigest()


def _valid_signature(kind, value, signature):
    return isinstance(signature, str) and any(secrets.compare_digest(_sign(kind, value, secret), signature)
        for secret in [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", ())])


def native_neutral_question(text):
    """Finite FAQ wording; absence of parsed selectors is never neutrality."""
    normalized = " ".join(str(text or "").casefold().strip().rstrip("?!.").split())
    return normalized in {"яка ціна", "яка ціна товарів", "скільки коштує доставка", "які умови доставки",
        "доставка нп", "а як доставка", "как доставка", "какая цена", "сколько стоит доставка",
        "какие условия доставки", "what is the price", "what does shipping cost", "what are the delivery terms"}


@dataclass(frozen=True)
class VerifiedCheckoutAmendment:
    amendment_id: uuid.UUID
    client_id: int
    proposal_id: int
    source_hmac: str
    _issuer: object


@dataclass(frozen=True)
class VerifiedPendingCheckoutAmendment:
    amendment_id: uuid.UUID
    client_id: int
    source_hmac: str
    _issuer: object


@dataclass(frozen=True)
class VerifiedCancelledCheckoutAmendment:
    amendment_id: uuid.UUID
    client_id: int
    source_hmac: str
    _issuer: object


def verify_native_transition(row, *, now=None, historical=False):
    """Validate a real native source, including its exact before/after head.

    Returns a detached source body; historical verification never gives current
    mutation authority and survives token expiry/revocation after the fact.
    """
    now = now or timezone.now()
    if row.source_message_id is not None or not row.checkout_amendment_id:
        _fail("hosted_transition_source_invalid")
    receipt = row.checkout_amendment
    body = receipt.source_body
    if (not isinstance(body, dict) or body.get("schema") != SCHEMA
            or not _valid_signature("source", body, receipt.source_hmac)
            or not _valid_signature("result", receipt.result, receipt.result_hmac)
            or body.get("amendment_id") != str(receipt.pk)
            or body.get("operation_id") != str(receipt.operation_id)
            or body.get("client_id") != receipt.client_id
            or body.get("proposal_id") != receipt.proposal_id
            or body.get("session_id") != receipt.session_id
            or body.get("token_id") != receipt.token_id
            or body.get("proposal_from_revision") != receipt.from_revision
            or body.get("proposal_to_revision") != receipt.to_revision
            or body.get("input_digest") != receipt.input_digest
            or receipt.result.get("amendment_id") != str(receipt.pk)
            or receipt.result.get("operation_id") != str(receipt.operation_id)
            or receipt.result.get("revision") != receipt.to_revision
            or receipt.result.get("selection_revision") != body.get("selection_to_revision")
            or row.session_id != receipt.session_id
            or row.action != "checkout_cart_amended"
            or row.from_revision != body.get("selection_from_revision")
            or row.to_revision != body.get("selection_to_revision")
            or row.previous_snapshot != body.get("previous_snapshot")
            or row.next_snapshot != body.get("next_snapshot")
            or row.session.client_id != receipt.client_id
            or row.session.generation != body.get("generation")
            or row.session.commercial_episode_id != body.get("episode_id")
            or body.get("actor") != AUTHORITY
            or body.get("token_kind") not in {"bot", "manager"}
            or receipt.created_at > now or row.created_at > now):
        _fail("hosted_transition_unverified")
    if not historical:
        if (row.session.open_slot != 1 or row.session.state != "open"
                or row.session.revision != row.to_revision
                or row.session.snapshot() != row.next_snapshot):
            _fail("hosted_transition_head_changed")
    from management.models import IgCheckoutRevision
    if not IgCheckoutRevision.objects.filter(pk=body.get("origin_revision_id"), proposal_id=receipt.proposal_id,
            revision=receipt.from_revision, digest=body.get("origin_revision_digest")).exists():
        _fail("hosted_transition_origin_changed")
    return deepcopy(body)


def hosted_transition_request(row, *, now=None, historical=False):
    body = verify_native_transition(row, now=now, historical=historical)
    return {"producer": SCHEMA, "actor": AUTHORITY, "amendment_id": body["amendment_id"],
        "operation_id": body["operation_id"], "operation": body["operation"], "line_id": body["line_id"],
        "quantity": body["quantity"], "source_digest": row.checkout_amendment.source_hmac,
        "transition_id": row.pk, "removed": deepcopy(body["removed"])}


def _rows(proposal, revision):
    binding = revision.snapshot.get("source_cart_binding")
    old = list(proposal.items.order_by("position", "pk"))
    if binding is not None:
        if binding.get("schema") not in {"ig-checkout-source-cart.v1", "ig-checkout-source-cart.v2", "ig-checkout-source-cart.v3"} or len(binding.get("lines", [])) != len(old):
            _fail("manager_reapproval_required")
        lines = binding["lines"]
    else:
        lines = [{"line_id": f"proposal:{proposal.pk}:line:{item.position}", "recipient_id": "self"} for item in old]
        from management.models import IgCommerceSelectionSession
        session = IgCommerceSelectionSession.objects.filter(client_id=proposal.client_id,
            commercial_episode_id=proposal.commercial_episode_id, open_slot=1, state="open").first()
        if session and len(session.lines or []) == len(old) and all(
                row.get("product_id") == item.product_id and row.get("size", "") == item.size
                and row.get("quantity", 1) == item.quantity and row.get("line_id")
                for row, item in zip(session.lines, old, strict=True)):
            lines = [{"line_id": row["line_id"], "recipient_id": row.get("recipient_id", "self")} for row in session.lines]
    return [{"line_id": line["line_id"], "recipient_id": line["recipient_id"],
        "product_id": item.product_id, "color_variant_id": item.color_variant_id,
        "size": item.size, "fit_option_code": item.fit_code, "option_values": deepcopy(item.option_values or {}),
        "color": item.color_label, "quantity": item.quantity, "title": item.product_title,
        **({"is_custom": True, "title": item.product_title, "photo_asset_id": frozen.get("photo_asset_id"),
            "photo_digest": frozen.get("photo_digest"), "assisted_line": deepcopy(frozen.get("assisted_line"))}
            if item.product_id is None else {})}
        for item, line, frozen in zip(old, lines, revision.snapshot.get("items", []), strict=True)]


def _specs(lines):
    return [{"product_id": row["product_id"], "color_variant_id": row["color_variant_id"],
        "size": row["size"], "fit_option_code": row["fit_option_code"],
        "option_values": deepcopy(row["option_values"]), "qty": row["quantity"]} for row in lines]


def _untouched(proposal, deal, client, *, pending_review=False, cancelled_scope=False):
    from orders.models import PaymentAttempt
    from management.models import IgCheckoutInvoiceGeneration
    allowed = {"ready", "viewed", "manager_review"} if pending_review else {"ready", "viewed"}
    if cancelled_scope:
        allowed.add("cancelled")
    if (proposal.status not in allowed or proposal.expires_at <= timezone.now()
            or proposal.payment_attempt_id or proposal.current_invoice_generation_id
            or proposal.winner_invoice_generation_id or proposal.details_locked_at or proposal.paid_at
            or proposal.superseded_by_id or proposal.provider_cancellation_event_id
            or proposal.invoice_cancelled_at or deal.order_id or deal.paid_at or deal.paid_amount > 0
            or deal.invoice_id or deal.invoice_url or deal.status in {"paid", "order_created"}
            or deal.payment_truth in {"confirmed", "partially_refunded", "refunded", "reversed"}
            or client.current_commercial_episode_id != proposal.commercial_episode_id
            or deal.active_checkout_proposal_id != proposal.pk
            or proposal.commercial_episode.intended_order_id is not None
            or proposal.commercial_episode.state != "active"
            or proposal.commercial_episode.open_slot != 1
            or proposal.commercial_episode.client_id != client.pk
            or proposal.commercial_episode.deal_id != deal.pk
            or IgCheckoutInvoiceGeneration.objects.filter(proposal_id=proposal.pk).exists()
            or PaymentAttempt.objects.filter(cart_snapshot__proposal_id=str(proposal.public_id)).exists()):
        _fail("payment_locked")


def assert_pending_prebank(proposal, receipt):
    if receipt.proposal_id != proposal.pk or receipt.source_body.get("pending_review") is not True:
        _fail("pending_amendment_owner_changed")
    verify_native_transition(receipt.selection_transition)
    _untouched(proposal, proposal.deal, proposal.client, pending_review=True)


def _project_cancelled_purchase(proposal, receipt):
    """Zero the current draft projection; immutable quote history stays intact."""
    deal = proposal.deal
    deal.amount = Decimal("0.00")
    deal.requested_payment_amount = Decimal("0.00")
    deal.status = deal.Status.CANCELLED
    deal.items.all().delete()
    deal.save(update_fields=["amount", "requested_payment_amount", "status", "updated_at"])
    episode = proposal.commercial_episode
    episode.product_snapshot = []
    episode.price_snapshot = {"catalog_total": "0.00", "negotiated_discount": "0.00",
        "negotiated_total": "0.00", "requested_payment_amount": "0.00", "currency": proposal.currency,
        "state": "cancelled", "checkout_amendment_id": str(receipt.pk),
        "last_approved_total": str(proposal.quoted_total), "proposal_digest": proposal.items_digest}
    episode.save(update_fields=["product_snapshot", "price_snapshot", "updated_at"])


def checkout_cart_state(proposal, token=None):
    """One typed public view of the authoritative payable basket."""
    from management.models import IgCheckoutRevision
    reason = ""
    try:
        _untouched(proposal, proposal.deal, proposal.client)
    except CheckoutAmendmentError as exc:
        reason = exc.reason
    revision = IgCheckoutRevision.objects.filter(proposal=proposal, revision=proposal.revision).select_related(
        "proposal__commercial_episode").first()
    assisted = hasattr(proposal, "assisted_commerce_execution")
    valid_token = (token is not None and token.proposal_id == proposal.pk
        and token.revoked_at is None and token.expires_at > timezone.now())
    if not reason and (not valid_token or token.kind not in {"bot", "manager"}):
        reason = "pay_only_link"
    items = []
    if revision:
        try:
            lines = _rows(proposal, revision)
            items = [{**line, "title": item.product_title, "unit_price": str(item.quoted_unit_price),
                "line_total": str(item.quoted_line_total)} for line, item in zip(lines,
                    proposal.items.order_by("position", "pk"), strict=True)]
        except CheckoutAmendmentError:
            reason = reason or "manager_reapproval_required"
    empty = proposal.status == "cancelled" and not items
    pending = proposal.cart_amendments.filter(source_body__pending_review=True).order_by("-created_at").first()
    cancelled_scope = (proposal.status == "cancelled" and proposal.cart_amendments.filter(
        source_body__cancelled_scope=True).exists())
    if cancelled_scope:
        items, empty = [], True
    if pending is not None and proposal.status == "manager_review":
        requested = pending.source_body.get("requested_quote_lines", [])
        items = [{**deepcopy(row), "title": row.get("title") or next((item["title"] for item in items
            if item["line_id"] == row["line_id"]), ""), "unit_price": None, "line_total": None} for row in requested]
    payment_safe = proposal.status in {"ready", "viewed"}
    generations = proposal.invoice_generations.select_related("payment_attempt")
    generation = (generations.filter(pk=proposal.winner_invoice_generation_id).first()
        if proposal.winner_invoice_generation_id else generations.order_by("-generation").first())
    frozen_attempt = generation.payment_attempt if generation is not None else (
        proposal.payment_attempt if proposal.payment_attempt_id else None)
    if generation is not None:
        from management.services.ig_checkout_generation import _retry_source_is_safe
        payment_safe = (generation.state in {"invoice_created", "failed", "cancelled", "expired"}
            and _retry_source_is_safe(generation, generation.payment_attempt))
    elif proposal.payment_attempt_id:
        attempt = proposal.payment_attempt
        payment_safe = bool(proposal.status == "invoice_created" and attempt.invoice_url
            and not (attempt.event_state or {}).get("invoice_creation_ambiguous"))
    from orders.services.shipping_policy import build_shipping_snapshot, loses_automatic_free_shipping
    free_delivery = None
    if not assisted and frozen_attempt is not None:
        from orders.services.delivery_payment import build_threshold_delivery_contract, DeliveryPaymentError
        from management.services.ig_checkout_payment import checkout_shipping_ack_digest
        contract = (frozen_attempt.cart_snapshot or {}).get("threshold_delivery_contract")
        if contract is not None:
            try:
                if build_threshold_delivery_contract(frozen_attempt, proposal=proposal, generation=generation) != contract:
                    raise DeliveryPaymentError("delivery_policy_changed")
                free_delivery = deepcopy(contract["shipping"])
                free_delivery["ack_required"] = False
                free_delivery["ack_digest"] = checkout_shipping_ack_digest(proposal, free_delivery,
                    promo_code=frozen_attempt.promo_code.code if frozen_attempt.promo_code_id else "")
            except DeliveryPaymentError:
                reason = reason or "delivery_policy_changed"
                payment_safe = False
    elif not assisted and not (pending and proposal.status == "manager_review"):
        free_delivery = build_shipping_snapshot(merchandise_total="0.00" if empty else proposal.quoted_total,
            gross_total="0.00" if empty else proposal.catalog_total,
            discount_amount="0.00" if empty else proposal.negotiated_discount,
            currency=proposal.currency, cart_digest=proposal.items_digest,
            owner={"proposal_id": str(proposal.public_id), "revision": proposal.revision,
                "items_digest": proposal.items_digest})
        prior_eligible = proposal.cart_amendments.filter(source_body__previous_shipping__eligible=True,
            source_body__previous_shipping__authority="automatic_threshold").order_by("-created_at").first()
        free_delivery["ack_required"] = bool(not empty and prior_eligible and loses_automatic_free_shipping(
            prior_eligible.source_body.get("previous_shipping"), free_delivery))
        from management.services.ig_checkout_payment import checkout_shipping_ack_digest
        free_delivery["ack_digest"] = checkout_shipping_ack_digest(proposal, free_delivery)
    paid_via_invoice = None
    if proposal.status == "paid" and frozen_attempt is not None and revision is not None:
        episode = revision.proposal.commercial_episode
        winner_matches = (proposal.winner_invoice_generation_id == generation.pk
            and generation.proposal_id == proposal.pk and generation.proposal_revision == proposal.revision
            and generation.payment_attempt_id == frozen_attempt.pk and generation.winner_slot == 1
            and generation.state == "paid_winner") if generation is not None else (
                proposal.winner_invoice_generation_id is None and proposal.current_invoice_generation_id is None)
        if (winner_matches and proposal.payment_attempt_id == frozen_attempt.pk
                and frozen_attempt.order_id and frozen_attempt.order_id == proposal.deal.order_id
                and episode is not None and episode.intended_order_id == frozen_attempt.order_id
                and episode.client_id == proposal.client_id and episode.deal_id == proposal.deal_id
                and proposal.deal.client_id == proposal.client_id
                and frozen_attempt.status in {"paid", "prepaid", "converted"}
                and frozen_attempt.paid_amount is not None and frozen_attempt.paid_amount > 0):
            paid_via_invoice = str(frozen_attempt.paid_amount)
    return {"schema": "ig-checkout-cart-state.v1", "revision": proposal.revision,
        "paid_via_invoice": paid_via_invoice,
        "free_delivery": free_delivery,
        "digest": proposal.items_digest, "selection_revision": _selection_revision(proposal) or 0,
        "items": items, "total": None if pending and proposal.status == "manager_review" else "0.00" if empty else str(frozen_attempt.payable_amount if frozen_attempt else proposal.quoted_total),
        "catalog_total": None if pending and proposal.status == "manager_review" else "0.00" if empty else str(frozen_attempt.gross_amount if frozen_attempt else proposal.catalog_total),
        "discount": None if pending and proposal.status == "manager_review" else "0.00" if empty else str(frozen_attempt.discount_amount if frozen_attempt else proposal.negotiated_discount),
        "charge_now": None if pending and proposal.status == "manager_review" else "0.00" if empty else str(frozen_attempt.payment_amount if frozen_attempt else proposal.requested_payment_amount),
        "last_approved_total": str(proposal.quoted_total) if cancelled_scope or pending and proposal.status == "manager_review" else None,
        "currency": proposal.currency, "is_prepayment": frozen_attempt.pay_type in {"prepayment", "prepay_200"}
            if frozen_attempt is not None else proposal.pay_type == "prepayment",
        "has_discount": bool(frozen_attempt.discount_amount if frozen_attempt is not None else proposal.negotiated_discount), "can_edit": not reason,
        "can_pay": bool(valid_token and payment_safe and not empty and proposal.expires_at > timezone.now()
            and proposal.status in {"ready", "viewed", "details_locked", "invoice_created"}
            and proposal.deal.active_checkout_proposal_id == proposal.pk
            and proposal.client.current_commercial_episode_id == proposal.commercial_episode_id
            and not proposal.client.privacy_erasure_started_at and not proposal.client.hidden_at
            and not proposal.deal.order_id and not proposal.deal.paid_at
            and not proposal.commercial_episode.intended_order_id),
        "blocked_reason": "empty_cart" if empty else reason,
        "review_needed": proposal.status == "manager_review" or reason == "manager_reapproval_required"}


def _selection_revision(proposal):
    from management.models import IgCommerceSelectionSession
    return IgCommerceSelectionSession.objects.filter(client_id=proposal.client_id,
        commercial_episode_id=proposal.commercial_episode_id, open_slot=1, state="open").values_list("revision", flat=True).first()


def _paid_native_capture_order(receipt, client, *, now=None):
    """Bind a native wish to its actual current winning Order, never a label."""
    from django.db.models import F
    from management.models import IgCheckoutProposal, IgCheckoutCartAmendmentResolution, IgCheckoutRevision
    from orders.services.delivery_payment import threshold_shipping_cart_digest, DeliveryPaymentError
    proposal = IgCheckoutProposal.objects.filter(client=client, commercial_episode_id=client.current_commercial_episode_id,
        deal__active_checkout_proposal_id=F("pk"), status="paid").select_related("deal", "commercial_episode",
            "payment_attempt__order", "winner_invoice_generation").first()
    if proposal is None or not proposal.payment_attempt_id:
        _fail("hosted_paid_order_unverified")
    attempt, episode, deal = proposal.payment_attempt, proposal.commercial_episode, proposal.deal
    if (not attempt.order_id or episode.intended_order_id != attempt.order_id or deal.order_id != attempt.order_id
            or deal.client_id != client.pk or episode.client_id != client.pk
            or attempt.status not in {"paid", "prepaid", "converted"}
            or attempt.order.payment_status not in {"paid", "prepaid", "partial"}
            or attempt.gross_amount != attempt.order.total_sum
            or attempt.discount_amount != attempt.order.discount_amount):
        _fail("hosted_paid_order_unverified")
    if proposal.winner_invoice_generation_id:
        winner = proposal.winner_invoice_generation
        if (winner.proposal_id != proposal.pk or winner.payment_attempt_id != attempt.pk
                or winner.proposal_revision != proposal.revision or winner.winner_slot != 1 or winner.state != "paid_winner"):
            _fail("hosted_paid_winner_unverified")
    revision = IgCheckoutRevision.objects.filter(proposal=proposal, revision=proposal.revision).first()
    provenance = (attempt.cart_snapshot or {}).get("source_cart_provenance") or {}
    owner = provenance.get("owner") or {}
    if (revision is None or revision.digest != proposal.items_digest
            or (attempt.cart_snapshot or {}).get("proposal_id") != str(proposal.public_id)
            or any(owner.get(key) != value for key, value in {"client_id": client.pk,
                "episode_id": episode.pk, "deal_id": deal.pk, "proposal_pk": proposal.pk,
                "proposal_id": str(proposal.public_id), "revision": proposal.revision, "revision_id": revision.pk}.items())):
        _fail("hosted_paid_source_unverified")
    if proposal.pk == receipt.proposal_id:
        native = (revision.snapshot.get("source_cart_capture") or {}).get("fence") or {}
        if native.get("native_amendment_id") != str(receipt.pk) or native.get("native_source_hmac") != receipt.source_hmac:
            _fail("hosted_paid_source_unverified")
    else:
        resolution = IgCheckoutCartAmendmentResolution.objects.filter(amendment=receipt,
            client=client, successor_proposal=proposal).select_related("request", "decision", "execution",
                "successor_proposal", "amendment__selection_transition__session").first()
        if resolution is None:
            _fail("hosted_paid_successor_unverified")
        from management.services.ig_checkout_amendment_successors import _verified_resolution
        from management.services.ig_assisted_commerce import AssistedDecisionError
        try:
            _verified_resolution(resolution, now=now or timezone.now())
        except AssistedDecisionError as exc:
            _fail(getattr(exc, "reason", "hosted_paid_successor_unverified"))
    try:
        if threshold_shipping_cart_digest((attempt.cart_snapshot or {}).get("cart")) != threshold_shipping_cart_digest(
                list(attempt.order.items.order_by("pk"))):
            _fail("hosted_paid_items_unverified")
    except DeliveryPaymentError:
        _fail("hosted_paid_items_unverified")
    return attempt.order_id


def _native_capture(receipt, transition, *, now=None):
    """Detached capture from a verified native witness, preserving old selectors."""
    from management.services.ig_admin_state_capture import _owner_fence, _namespace, _source_watermark
    from management.services.ig_funnel_reset import current_message_floor
    from management.models import IgClient, InstagramBotMessage
    body = verify_native_transition(transition, now=now)
    client = IgClient.objects.get(pk=receipt.client_id)
    owner = _owner_fence(client.pk)
    if not owner or client.privacy_erasure_started_at or client.current_commercial_episode_id != body["episode_id"]:
        _fail("hosted_owner_changed")
    base = body.get("base_capture") or {}
    floor = current_message_floor(client)
    scope = deepcopy(base.get("scope")) or {"client_id": client.pk, "episode_id": body["episode_id"], "order_id": None,
        "reset_id": (owner["reset"] or {}).get("pk"), "reset_floor": floor,
        "source_namespace": _namespace() or "checkout-link"}
    current_order_id = (owner.get("episode") or {}).get("intended_order_id")
    if current_order_id is not None and scope.get("order_id") != current_order_id:
        if _paid_native_capture_order(receipt, client, now=now) != current_order_id:
            _fail("hosted_paid_order_scope_changed")
        scope["order_id"] = current_order_id
    current_namespace = _namespace() or "checkout-link"
    if scope["source_namespace"] != current_namespace:
        _fail("hosted_namespace_changed")
    if scope["reset_floor"] != floor or scope["reset_id"] != (owner["reset"] or {}).get("pk"):
        _fail("hosted_reset_changed")
    source_ids = (base.get("fence") or {}).get("source_ids", [])
    # Original chat facts remain valid only while their exact captured source
    # rows remain unchanged. Native facts never manufacture message IDs.
    if source_ids:
        from management.services.ig_commerce_projection import _source_fence_row
        rows = {row.pk: _source_fence_row(row) for row in InstagramBotMessage.objects.filter(pk__in=source_ids)}
        if len(rows) != len(source_ids) or _digest(rows) != base["fence"]["source_digest"]:
            _fail("hosted_original_source_changed")
    proof = {"amendment_id": str(receipt.pk), "transition_id": transition.pk,
        "source_digest": receipt.source_hmac, "source_kind": SCHEMA, "authority": AUTHORITY,
        "observed_at": receipt.created_at.isoformat()}
    capture_rows = []
    for index, line in enumerate(body["next_snapshot"]["lines"]):
        line_scope = {**scope, "line_id": line["line_id"], "recipient_id": line.get("recipient_id", "self"),
            "session_id": receipt.session_id, "generation": body["generation"], "revision": transition.to_revision}
        line_scope["active_index"] = index
        fields = {key: {"status": "confirmed", "authority": AUTHORITY, "value": deepcopy(line[key]),
            "source": deepcopy(proof)} for key in ("product_id", "color_variant_id", "size", "fit_option_code",
                "option_values", "color", "quantity", "title", "garment_type") if line.get(key) not in (None, "")}
        selection = {"schema": "source-selection.v1", "scope": line_scope, "fields": fields,
            "values": {key: deepcopy(field["value"]) for key, field in fields.items()},
            "evidence": {key: deepcopy(proof) for key in fields}}
        capture_rows.append({"line_id": line["line_id"], "recipient_id": line.get("recipient_id", "self"), "index": index,
            "source_selection": selection, "fields": fields, "evidence": {key: deepcopy(proof) for key in fields},
            "cleared": {}, "history": [], "history_coverage": {"complete": False, "reason": "bounded_native_history"},
            "defaults": {}, "unsupported_selectors": []})
    watermark = _source_watermark(scope, scope["source_namespace"], client.igsid, now or timezone.now(), customer_only=True)
    value = {"schema": "source-selections.v1", "status": "captured", "reason": "", "scope": scope,
        "session_id": receipt.session_id, "generation": body["generation"], "selection_revision": transition.to_revision,
        "active_index": body["next_snapshot"]["active_index"],
        "active_line_id": (capture_rows[body["next_snapshot"]["active_index"]]["line_id"] if capture_rows else None),
        "source_watermark": watermark, "lines": capture_rows, "omissions": [], "coverage_complete": True,
        "transition_limit": 64, "line_limit": 16, "query_limit": 64, "removed": deepcopy(body["removed"]),
        "fence": {"owner_digest": _digest(owner), "namespace": scope["source_namespace"], "source_watermark": watermark,
            "source_ids": source_ids, "source_digest": (base.get("fence") or {}).get("source_digest", _digest({})),
            "snapshot_digest": _digest(body["next_snapshot"]), "native_amendment_id": str(receipt.pk),
            "native_source_hmac": receipt.source_hmac}}
    value["capture_digest"] = _digest(value)
    from management.models import IgCommerceSelectionSession
    final_session = IgCommerceSelectionSession.objects.filter(pk=receipt.session_id, client_id=client.pk,
        open_slot=1, state="open").first()
    if (_owner_fence(client.pk) != owner or final_session is None
            or final_session.snapshot() != body["next_snapshot"]
            or (_namespace() or "checkout-link") != current_namespace
            or _source_watermark(scope, scope["source_namespace"], client.igsid, now or timezone.now(), customer_only=True) != watermark):
        _fail("hosted_capture_changed")
    if source_ids:
        final_sources = {row.pk: _source_fence_row(row) for row in InstagramBotMessage.objects.filter(pk__in=source_ids)}
        if final_sources != rows:
            _fail("hosted_original_source_changed")
    return value


def native_capture_for(client_id, *, now=None):
    """Consumer hook: None means no native head; denial stays explicit."""
    from management.models import IgCommerceSelectionTransition
    row = (IgCommerceSelectionTransition.objects.filter(session__client_id=client_id, session__open_slot=1,
        session__state="open", checkout_amendment__isnull=False).select_related("session", "checkout_amendment")
        .order_by("-to_revision").first())
    if row is None or row.session.revision != row.to_revision:
        return None
    try:
        return _native_capture(row.checkout_amendment, row, now=now)
    except CheckoutAmendmentError as exc:
        return {"schema": "source-selections.v1", "status": "unavailable", "reason": exc.reason,
            "lines": [], "coverage_complete": False}


def native_preferences_for(client, *, episode_id=None, line_id=None):
    capture = native_capture_for(client.pk)
    if capture is None:
        return None
    if capture.get("status") != "captured" or (episode_id is not None and episode_id != capture["scope"]["episode_id"]):
        return {}
    rows = capture["lines"]
    target = line_id if line_id is not None else capture["active_line_id"]
    row = next((row for row in rows if row["line_id"] == target), None)
    if row is None:
        return {}
    values = {key: field["value"] for key, field in row["fields"].items()}
    return {"session_id": capture["session_id"], "generation": capture["generation"],
        "revision": capture["selection_revision"], "active_index": row["index"],
        "episode_id": capture["scope"]["episode_id"], "reset_floor": capture["scope"]["reset_floor"],
        "line_id": row["line_id"], "recipient_id": row["recipient_id"], "line_count": len(rows),
        "product_id": values.get("product_id"), "values": values,
        "evidence": {key: {**field["source"], "authority": AUTHORITY} for key, field in row["fields"].items()},
        "head_transition_id": next(iter(row["evidence"].values()), {}).get("transition_id"),
        "head_digest": capture["fence"]["snapshot_digest"], "removed": capture["removed"]}


def validate_native_capture(capture, *, now=None):
    """Actual native source admission for unified capture/prompt consumers."""
    from management.services.ig_revision_cart_binding import same_checkout_source_capture
    if not isinstance(capture, dict) or not capture.get("fence", {}).get("native_amendment_id"):
        return False
    current = native_capture_for((capture.get("scope") or {}).get("client_id"), now=now)
    return bool(current and same_checkout_source_capture(current, capture))


def validate_native_payment_capture(capture, *, now=None):
    """A later neutral FAQ is observation, never a change to the payable cart.

    Unified generation still uses the exact full capture above. Only frozen
    payment admission uses this narrower semantic comparison, after verifying
    the actual native head, original source rows and every other scope field.
    """
    from management.services.ig_revision_cart_binding import stable_checkout_source_capture
    from management.services.ig_commerce_turns import is_neutral_commerce_source
    from management.models import InstagramBotMessage, IgClient
    now = now or timezone.now()
    if not isinstance(capture, dict) or not capture.get("fence", {}).get("native_amendment_id"):
        return False
    current = native_capture_for((capture.get("scope") or {}).get("client_id"), now=now)
    before = stable_checkout_source_capture(capture)
    after = stable_checkout_source_capture(current)
    if before is None or after is None:
        return False
    if before == after:
        return True
    old_watermark = before.get("source_watermark") or {}
    new_watermark = after.get("source_watermark") or {}
    old_id, new_id = old_watermark.get("message_id") or 0, new_watermark.get("message_id")
    if type(old_id) is not int or type(new_id) is not int or new_id <= old_id:
        return False
    for value in (before, after):
        value.pop("source_watermark", None)
        value["fence"].pop("source_watermark", None)
    if before != after:
        return False
    scope = capture["scope"]
    client = IgClient.objects.filter(pk=scope["client_id"], privacy_erasure_started_at__isnull=True).first()
    if client is None:
        return False
    sources_query = InstagramBotMessage.objects.filter(client=client, sender_id=client.igsid,
        role="user", source="webhook", provider_namespace=scope["source_namespace"],
        pk__gt=old_id, pk__lte=new_id).exclude(status="failed").order_by("pk")
    if transaction.get_connection().in_atomic_block:
        sources_query = sources_query.select_for_update()
    sources = list(sources_query[:65])
    return bool(sources and len(sources) <= 64 and sources[-1].pk == new_id
        and all(is_neutral_commerce_source(source.text) or native_neutral_question(source.text) for source in sources))


def verify_pending_amendment(amendment_id, *, now=None):
    from management.models import IgCheckoutCartAmendment
    now = now or timezone.now()
    try:
        receipt = IgCheckoutCartAmendment.objects.select_related("selection_transition__session", "proposal__client").get(pk=amendment_id)
        body = verify_native_transition(receipt.selection_transition, now=now)
        proposal = receipt.proposal
        if (body.get("pending_review") is not True or proposal.status != "manager_review"
                or proposal.revision != receipt.from_revision or proposal.expires_at <= now
                or proposal.payment_attempt_id or proposal.invoice_generations.exists()
                or proposal.winner_invoice_generation_id or proposal.paid_at
                or proposal.client.current_commercial_episode_id != body["episode_id"]
                or proposal.client.privacy_erasure_started_at or proposal.client.hidden_at):
            _fail("pending_amendment_not_current")
        capture = _native_capture(receipt, receipt.selection_transition, now=now)
        assert_pending_prebank(proposal, receipt)
        execution = getattr(proposal, "assisted_commerce_execution", None)
        if execution is not None:
            from management.services.ig_assisted_checkout import validate_assisted_execution_history
            original = validate_assisted_execution_history(execution, now=now)
            if (original.get("ready") is not True or body.get("original_execution_id") != str(execution.pk)
                    or body.get("original_execution_digest") != execution.envelope_hmac):
                _fail("pending_original_execution_changed")
        return {"ready": True, "reason": "", "amendment": receipt, "body": body,
            "current_capture": capture, "original_execution": execution,
            "verified_amendment": VerifiedPendingCheckoutAmendment(receipt.pk, receipt.client_id, receipt.source_hmac, _ISSUER),
            "proof": {"amendment_id": str(receipt.pk),
                "source_hmac": receipt.source_hmac, "proposal_id": proposal.pk,
                "original_execution_id": body.get("original_execution_id"),
                "original_execution_digest": body.get("original_execution_digest")}}
    except (CheckoutAmendmentError, IgCheckoutCartAmendment.DoesNotExist, ValueError, TypeError) as exc:
        return {"ready": False, "reason": getattr(exc, "reason", "pending_amendment_unverified")}


def verify_cancelled_amendment(amendment_id, *, now=None):
    """Only a real unbanked native cancellation can precede fresh WAIT approval."""
    from management.models import IgCheckoutCartAmendment
    now = now or timezone.now()
    try:
        receipt = IgCheckoutCartAmendment.objects.select_related("selection_transition__session",
            "proposal__client", "proposal__deal", "proposal__commercial_episode").get(pk=amendment_id)
        body = verify_native_transition(receipt.selection_transition, now=now)
        proposal = receipt.proposal
        if (body.get("cancelled_scope") is not True or body.get("pending_review") is not False
                or body.get("requested_quote_lines") != [] or proposal.status != "cancelled"
                or proposal.revision != receipt.from_revision or proposal.items_digest != body.get("origin_revision_digest")):
            _fail("native_cancelled_scope_unverified")
        _untouched(proposal, proposal.deal, proposal.client, cancelled_scope=True)
        execution = getattr(proposal, "assisted_commerce_execution", None)
        if execution is not None:
            from management.services.ig_assisted_checkout import validate_assisted_execution_history
            if (execution.active_slot is not None or execution.release_reason != "cancelled" or not execution.released_at
                    or execution.order_id or execution.settled_at
                    or validate_assisted_execution_history(execution, now=now).get("ready") is not True
                    or body.get("original_execution_id") != str(execution.pk)
                    or body.get("original_execution_digest") != execution.envelope_hmac):
                _fail("native_cancelled_execution_unverified")
        capture = _native_capture(receipt, receipt.selection_transition, now=now)
        return {"ready": True, "reason": "", "amendment": receipt, "body": body, "current_capture": capture,
            "original_execution": execution,
            "verified_amendment": VerifiedCancelledCheckoutAmendment(receipt.pk, receipt.client_id, receipt.source_hmac, _ISSUER)}
    except (CheckoutAmendmentError, IgCheckoutCartAmendment.DoesNotExist, ValueError, TypeError) as exc:
        return {"ready": False, "reason": getattr(exc, "reason", "native_cancelled_scope_unverified")}


def verify_prepared_amendment(cap, *, client, item_specs, binding, capture, pay_type, allow_promo):
    from management.models import IgCheckoutCartAmendment, IgDeal, IgCheckoutProposal
    if type(cap) is not VerifiedCheckoutAmendment or cap._issuer is not _ISSUER or cap.client_id != client.pk:
        _fail("hosted_amendment_requires_native_producer")
    receipt = IgCheckoutCartAmendment.objects.select_related("selection_transition__session").get(pk=cap.amendment_id)
    if receipt.source_hmac != cap.source_hmac or receipt.proposal_id != cap.proposal_id:
        _fail("hosted_amendment_owner_changed")
    deal = IgDeal.objects.select_for_update().get(pk=receipt.proposal.deal_id, client=client)
    proposal = IgCheckoutProposal.objects.select_for_update().select_related("commercial_episode", "client", "deal").get(pk=receipt.proposal_id, deal=deal)
    body = receipt.source_body
    _untouched(proposal, deal, client)
    if (proposal.revision != receipt.from_revision or proposal.items_digest != body.get("expected_digest")
            or proposal.currency != body.get("original_currency") or proposal.currency != "UAH"
            or proposal.pay_type != body.get("original_pay_type") or pay_type != proposal.pay_type
            or bool(allow_promo) != body.get("original_allow_promo")
            or bool(proposal.allow_promo) != body.get("original_allow_promo")):
        _fail("hosted_amendment_quote_changed")
    native = _native_capture(receipt, receipt.selection_transition)
    from management.services.ig_revision_cart_binding import same_checkout_source_capture, validate_quote_source_positions
    if (not same_checkout_source_capture(native, capture)
            or _specs(receipt.source_body["next_snapshot"]["lines"]) != item_specs
            or not validate_quote_source_positions(binding=binding, client_id=client.pk, item_specs=item_specs).ok):
        _fail("hosted_amendment_capture_changed")
    return receipt


@transaction.atomic
def amend_checkout_cart(proposal, *, request, grant, payload):
    from management.models import (IgClient, IgDeal, IgCheckoutProposal, IgCheckoutAccessToken,
        IgCheckoutRevision, IgCheckoutCartAmendment, IgCommerceSelectionSession, IgCommerceSelectionTransition)
    from management.services.ig_checkout_payment import require_checkout_revision
    from management.services.ig_checkout import validate_checkout_items, create_or_update_proposal
    from management.services.ig_revision_checkout import _quote_items
    from management.services.ig_revision_cart_binding import build_cart_binding, bind_quote_lines, frozen_cart_binding_payload
    from management.services.ig_commerce_projection import capture_current_selection_lines, project_active_line_to_legacy_client
    try:
        operation_id = uuid.UUID(str(payload.get("operation_id")))
    except (ValueError, TypeError, AttributeError):
        _fail("operation_invalid")
    if str(operation_id) != str(payload.get("operation_id")):
        _fail("operation_invalid")
    operation, line_id = payload.get("operation"), payload.get("line_id")
    quantity = None
    if operation == "quantity":
        raw = payload.get("quantity")
        if isinstance(raw, bool) or not isinstance(raw, (str, int)) or not 1 <= len(str(raw)) <= 2 or not str(raw).isdigit() or not 1 <= int(raw) <= 50:
            _fail("quantity_invalid")
        quantity = int(raw)
    elif operation != "remove" or payload.get("quantity") not in (None, ""):
        _fail("operation_invalid")
    if not isinstance(line_id, str) or not 1 <= len(line_id) <= 128:
        _fail("line_invalid")
    client = IgClient.objects.select_for_update().get(pk=proposal.client_id)
    deal = IgDeal.objects.select_for_update().get(pk=proposal.deal_id, client=client)
    locked = IgCheckoutProposal.objects.select_for_update().select_related("commercial_episode", "client", "deal").get(pk=proposal.pk, deal=deal)
    token = IgCheckoutAccessToken.objects.select_for_update().filter(pk=grant.get("token_id"), proposal=locked,
        revoked_at__isnull=True, expires_at__gt=timezone.now()).first()
    if (token is None or token.kind not in {"bot", "manager"} or not grant.get("grant_id")
            or grant.get("proposal_id") != str(locked.public_id)
            or int(grant.get("expires_at") or 0) <= int(timezone.now().timestamp())
            or client.privacy_erasure_started_at or client.hidden_at):
        _fail("edit_grant_unavailable")
    inputs = {"proposal_id": locked.pk, "token_id": token.pk, "grant_id": grant["grant_id"],
        "operation_id": str(operation_id), "operation": operation, "line_id": line_id, "quantity": quantity,
        "expected_revision": str(payload.get("expected_revision")), "expected_digest": payload.get("expected_digest"),
        "expected_selection_revision": str(payload.get("expected_selection_revision"))}
    input_digest = _digest(inputs)
    prior = IgCheckoutCartAmendment.objects.filter(operation_id=operation_id).first()
    if prior:
        if (prior.proposal_id != locked.pk or prior.client_id != client.pk or prior.token_id != token.pk
                or prior.input_digest != input_digest or not _valid_signature("result", prior.result, prior.result_hmac)):
            _fail("operation_conflict")
        verify_native_transition(prior.selection_transition, historical=True)
        return {**deepcopy(prior.result), **checkout_cart_state(locked, token),
            "applied_revision": prior.to_revision,
            "replayed": True, "current": checkout_cart_state(locked, token)}
    require_checkout_revision(locked, payload)
    _untouched(locked, deal, client)
    assisted_execution = getattr(locked, "assisted_commerce_execution", None)
    pending_review = bool(assisted_execution or locked.negotiated_discount
        or locked.pay_type != "online_full" or locked.items.filter(product_id__isnull=True).exists())
    if assisted_execution is not None:
        from management.services.ig_assisted_checkout import validate_assisted_proposal
        approved = validate_assisted_proposal(locked, fresh=True)
        if approved.get("ready") is not True:
            _fail(approved.get("reason") or "approval_changed")
    revision = IgCheckoutRevision.objects.get(proposal=locked, revision=locked.revision)
    lines = _rows(locked, revision)
    matches = [index for index, row in enumerate(lines) if row["line_id"] == line_id]
    if len(matches) != 1:
        _fail("line_changed")
    base_capture = (approved["current_capture"] if assisted_execution is not None
        else revision.snapshot.get("source_cart_capture"))
    session = IgCommerceSelectionSession.objects.select_for_update().filter(client=client,
        commercial_episode=locked.commercial_episode, open_slot=1, state="open").first()
    if session is None:
        if payload.get("expected_selection_revision") not in ("0", 0):
            _fail("selection_changed")
        from django.db.models import Max
        generation = (IgCommerceSelectionSession.objects.filter(client=client).aggregate(value=Max("generation"))["value"] or 0) + 1
        session = IgCommerceSelectionSession.objects.create(client=client, commercial_episode=locked.commercial_episode,
            generation=generation, revision=0)
    if str(payload.get("expected_selection_revision")) != str(session.revision):
        _fail("selection_changed")
    if base_capture:
        from management.models import InstagramBotMessage
        source_ids = (base_capture.get("fence") or {}).get("source_ids", [])
        if (not isinstance(source_ids, list) or len(source_ids) > 64
                or any(type(value) is not int or value <= 0 for value in source_ids)
                or source_ids != sorted(set(source_ids))):
            _fail("source_scope_changed")
        if source_ids:
            locked_ids = list(InstagramBotMessage.objects.select_for_update().filter(pk__in=source_ids,
                client=client).order_by("pk").values_list("pk", flat=True))
            if locked_ids != source_ids:
                _fail("source_scope_changed")
        from management.services.ig_revision_cart_binding import same_checkout_source_capture
        current = capture_current_selection_lines(client.pk)
        if not same_checkout_source_capture(base_capture, current):
            _fail("selection_changed")
    elif session.lines:
        # Legacy proposals may establish native origin only when their economic
        # rows match the current draft, never replace another existing wish.
        if len(session.lines) != len(lines) or any(row.get("product_id") != old["product_id"]
                or row.get("size", "") != old["size"] or row.get("quantity", 1) != old["quantity"]
                or row.get("recipient_id", "self") != old["recipient_id"]
                or row.get("fit_option_code", "") != old["fit_option_code"]
                or row.get("color_variant_id") != old["color_variant_id"]
                or row.get("option_values", {}) != old["option_values"]
                or row.get("color", "") != old["color"]
                for row, old in zip(session.lines, lines, strict=True)):
            _fail("selection_changed")
        for old, row in zip(lines, session.lines, strict=True):
            old["line_id"] = row["line_id"]
        matches = [index for index, row in enumerate(lines) if row["line_id"] == line_id]
        if len(matches) != 1:
            _fail("line_changed")
    before = deepcopy(session.snapshot())
    after = deepcopy(before)
    previous_quote_lines = deepcopy(lines)
    target = lines[matches[0]]
    removed = deepcopy((base_capture or {}).get("removed", []))
    if operation == "remove":
        removed.append({"line_id": line_id, "recipient_id": target["recipient_id"],
            "quantity": target["quantity"], "disposition": "cancelled_by_checkout_holder", "operation_id": str(operation_id)})
        lines.pop(matches[0])
    else:
        target["quantity"] = quantity
    next_lines = deepcopy(lines)
    current_line_id = line_id
    if pending_review and base_capture:
        binding = revision.snapshot.get("source_cart_binding") or {}
        current_line_id = next((row.get("current_line_id", line_id) for row in binding.get("line_map", [])
            if row.get("line_id") == line_id), line_id)
        next_lines = deepcopy(before["lines"])
        current_matches = [index for index, row in enumerate(next_lines) if row.get("line_id") == current_line_id]
        if len(current_matches) != 1:
            _fail("pending_line_scope_changed")
        if operation == "remove":
            next_lines.pop(current_matches[0])
        else:
            next_lines[current_matches[0]]["quantity"] = quantity
    after.update(lines=next_lines, revision=session.revision + 1,
        active_index=min(before["active_index"], max(0, len(next_lines) - 1)))
    for line in after["lines"]:
        line.setdefault("recipient_id", "self")
    cancelled_scope = not lines
    previous_shipping = checkout_cart_state(locked, token).get("free_delivery")
    amendment_id = uuid.uuid4()
    body = {"schema": SCHEMA, "amendment_id": str(amendment_id), **inputs, "input_digest": input_digest,
        "client_id": client.pk, "episode_id": locked.commercial_episode_id, "session_id": session.pk,
        "generation": session.generation, "token_kind": token.kind, "actor": AUTHORITY,
        "proposal_from_revision": locked.revision, "proposal_to_revision": locked.revision + 1,
        "selection_from_revision": session.revision, "selection_to_revision": after["revision"],
        "previous_snapshot": before, "next_snapshot": after, "origin_revision_id": revision.pk,
        "origin_revision_digest": revision.digest, "base_capture": deepcopy(base_capture), "removed": removed,
        "pending_review": pending_review and not cancelled_scope, "cancelled_scope": cancelled_scope,
        "requested_quote_lines": deepcopy(lines),
        "previous_quote_lines": previous_quote_lines,
        "previous_shipping": deepcopy(previous_shipping),
        "current_line_id": current_line_id,
        "original_execution_id": str(assisted_execution.pk) if assisted_execution else None,
        "original_currency": locked.currency, "original_pay_type": locked.pay_type,
        "original_allow_promo": bool(locked.allow_promo),
        "original_execution_digest": assisted_execution.envelope_hmac if assisted_execution else None,
        "original_assisted_scope": deepcopy(assisted_execution.envelope["snapshot"]) if assisted_execution else None}
    # Freeze native source before using it for canonical selection/repricing.
    # Result contains source identity only; the replay derives its immutable
    # economic response from the appended checkout revision.
    result = {"amendment_id": str(amendment_id), "revision": locked.revision + 1,
        "selection_revision": after["revision"], "operation_id": str(operation_id)}
    receipt = IgCheckoutCartAmendment.objects.create(id=amendment_id, operation_id=operation_id,
        proposal=locked, client=client, session=session, token=token,
        from_revision=locked.revision, to_revision=locked.revision + 1, input_digest=input_digest,
        source_body=body, source_hmac=_sign("source", body), result=result, result_hmac=_sign("result", result))
    transition = IgCommerceSelectionTransition.objects.create(session=session, source_message=None,
        checkout_amendment=receipt, action="checkout_cart_amended", from_revision=session.revision,
        to_revision=after["revision"], previous_snapshot=before, next_snapshot=after,
        effects={}, reasons=[AUTHORITY], source_order_key=f"hosted:{operation_id}")
    session.lines, session.active_index, session.revision = after["lines"], after["active_index"], after["revision"]
    session.save(update_fields=["lines", "active_index", "revision", "updated_at"])
    project_active_line_to_legacy_client(session, client)
    if cancelled_scope:
        # Nothing remains in this hosted purchase scope. This is cancellation,
        # not a new zero-value agreement. Preserve the old approved economics
        # and leave every deferred WAIT wish in the full canonical session.
        from management.services.ig_inventory import release_proposal_inventory
        locked.status = locked.Status.CANCELLED
        locked.save(update_fields=["status", "updated_at"])
        if assisted_execution is not None:
            execution = type(assisted_execution).objects.select_for_update().get(pk=assisted_execution.pk)
            if execution.active_slot != 1 or execution.order_id or execution.released_at or execution.settled_at:
                _fail("original_execution_terminal")
            execution.active_slot = None
            execution.released_at = timezone.now()
            execution.release_reason = "cancelled"
            execution.save(update_fields=["active_slot", "released_at", "release_reason"])
        release_proposal_inventory(locked, reason=f"checkout_scope_cancelled:{receipt.pk}")
        _project_cancelled_purchase(locked, receipt)
        if assisted_execution is not None and after["lines"] and assisted_execution.envelope["snapshot"].get("deferred_line_ids"):
            from management.services.ig_checkout_amendment_successors import create_amendment_review_request
            from management.services.ig_assisted_commerce import AssistedDecisionError
            try:
                create_amendment_review_request(receipt.pk, operation_id=operation_id)
            except AssistedDecisionError as exc:
                _fail(getattr(exc, "reason", "amendment_review_unavailable"))
        return {**checkout_cart_state(locked, token), "amendment_id": str(receipt.pk),
            "operation_id": str(operation_id), "replayed": False, "review_needed": False,
            "can_pay": False, "can_edit": False, "blocked_reason": "empty_cart"}
    if pending_review:
        locked.status = locked.Status.MANAGER_REVIEW
        locked.save(update_fields=["status", "updated_at"])
        from management.services.ig_inventory import release_proposal_inventory
        release_proposal_inventory(locked, reason=f"amendment_pending_review:{receipt.pk}")
        review_request_id = None
        if assisted_execution is not None:
            from management.services.ig_checkout_amendment_successors import create_amendment_review_request
            from management.services.ig_assisted_commerce import AssistedDecisionError
            try:
                review_request, _created = create_amendment_review_request(receipt.pk, operation_id=operation_id)
            except AssistedDecisionError as exc:
                _fail(getattr(exc, "reason", "amendment_review_unavailable"))
            review_request_id = str(review_request.pk)
        else:
            # Legacy agreed quotes without an execution stay visible as a native
            # manager case; no synthetic Instagram source or approval is made.
            from management.models import IgFollowUpTask
            task, _ = IgFollowUpTask.objects.get_or_create(event_key=f"checkout-amendment-review:{receipt.pk}",
                defaults={"client": client, "deal": deal, "kind": "manager_task", "due_at": timezone.now(),
                    "reason": "checkout_amendment_review", "manager_approval_status": "pending",
                    "manager_approval_requested_at": timezone.now(),
                    "message_text": "Власник посилання змінив склад або кількість товарів. Потрібне нове погодження ціни та умов.",
                    "manager_context": {"checkout_amendment": {"amendment_id": str(receipt.pk),
                        "source_hmac": receipt.source_hmac, "actor": AUTHORITY,
                        "requested_quote_lines": deepcopy(lines), "removed": deepcopy(removed)}}})
            if task.client_id != client.pk or task.deal_id != deal.pk:
                _fail("manager_case_owner_changed")
        return {**checkout_cart_state(locked, token), "amendment_id": str(receipt.pk),
            "operation_id": str(operation_id), "review_request_id": review_request_id,
            "replayed": False, "review_needed": True,
            "can_pay": False, "can_edit": False, "blocked_reason": "manager_reapproval_required"}
    capture = _native_capture(receipt, transition)
    source = build_cart_binding(captured_cart=capture, expected_scope=capture["scope"], checkout_item_limit=12)
    if not source.ok:
        _fail(source.reason)
    specs = _specs(lines)
    evidence_ids = list(revision.evidence_message_ids or [])
    quote = validate_checkout_items(client=client, item_specs=specs, evidence={"message_ids": evidence_ids},
        pay_type="online_full", allow_promo=locked.allow_promo, source_cart_binding=source)
    readiness = {row["line_id"]: {"scope": {**capture["scope"], "line_id": row["line_id"], "recipient_id": row["recipient_id"]},
        "capture_digest": capture["capture_digest"], "readiness": {"applicability_known": True, "can_issue_link": True,
            "missing": [], "quantity": row["quantity"], "product": {"id": row["product_id"]},
            "size": {"selected": row["size"]}, "fit": {"selected": row["fit_option_code"]},
            "color": {"selected_variant_id": row["color_variant_id"]},
            "options": {"selected": row["option_values"]}}} for row in lines}
    bound = bind_quote_lines(binding=source, quoted_items=_quote_items(quote), readiness_by_line=readiness)
    if not bound.ok:
        _fail(bound.reason)
    cap = VerifiedCheckoutAmendment(receipt.pk, client.pk, locked.pk, receipt.source_hmac, _ISSUER)
    updated = create_or_update_proposal(client=client, deal=deal, pay_type="online_full", item_specs=specs,
        evidence={"message_ids": evidence_ids}, allow_promo=locked.allow_promo,
        source_cart_binding=frozen_cart_binding_payload(bound), source_cart_capture=capture,
        verified_checkout_amendment=cap)
    if updated.pk != locked.pk or updated.revision != receipt.to_revision:
        _fail("amendment_proposal_changed")
    return {**checkout_cart_state(updated, token), **result, "replayed": False}
