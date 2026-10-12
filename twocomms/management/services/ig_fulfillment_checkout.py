"""Ordinary source-cart checkout after an explicit physical stock recount.

The caller owns the stock transaction and client lock. Manager availability
does not grant purchase, price, payment, order or provider-send authority.
"""
from copy import deepcopy

from django.db import transaction
from django.utils import timezone

from management.services.ig_fulfillment_stock_sync import StockSyncError


class FulfillmentCheckoutError(StockSyncError):
    """A checkout failure must roll back the caller's stock reconciliation."""


def _fail(reason):
    raise FulfillmentCheckoutError(reason)


def _purchase_evidence(cart):
    """Accepted reducer evidence for every current recipient line, not intent."""
    from management.services.ig_response_cart_plan import source_line_preferences
    rows = cart.get("lines") or []
    if not rows:
        return None
    result = []
    for row in rows:
        preferences = source_line_preferences(cart, row)
        proof = preferences.get("evidence", {}).get("purchase_requested")
        if preferences.get("values", {}).get("purchase_requested") is not True or not proof:
            return None
        result.append({"line_id": row["line_id"], "recipient_id": row.get("recipient_id", "self"),
            "source": deepcopy(proof)})
    return result


def _complete_available_cart(request, decision, cart):
    requested = request.envelope.get("lines") or []
    decided = decision.envelope.get("lines") or []
    expected = {(row.get("line_id"), row.get("recipient_id", "self")) for row in cart.get("lines", [])}
    if (not expected or len(expected) != len(cart.get("lines", [])) or len(requested) != len(expected)
        or {(row.get("line_id"), row.get("recipient_id", "self")) for row in requested} != expected
        or len(decided) != len(requested) or {row.get("line_id") for row in decided} != {row["line_id"] for row in requested}):
        _fail("checkout_full_cart_coverage_missing")
    targets = {row["line_id"]: row.get("target") for row in requested}
    for row in decided:
        target = targets[row["line_id"]]
        if (not target or row.get("availability") != "available"
            or row.get("supply_basis") not in {"warehouse_checked", "accessible_unrecorded"}
            or type(row.get("checked_quantity")) is not int
            or row["checked_quantity"] < target.get("quantity", 1000)):
            _fail("checkout_full_cart_not_ready")


def _reservation_proof(proposal, now):
    from management.models import IgCheckoutInventoryReservation as Reservation
    items = {item.pk: item for item in proposal.items.select_for_update().order_by("pk")}
    rows = list(Reservation.objects.select_for_update().filter(proposal=proposal,
        invoice_generation__isnull=True, state=Reservation.State.ACTIVE, expires_at__gt=now).order_by("pk"))
    covered, proof = set(), []
    for row in rows:
        line_ids = row.line_ids
        if (not isinstance(line_ids, list) or not line_ids or any(type(value) is not int for value in line_ids)
            or len(set(line_ids)) != len(line_ids) or any(value not in items or value in covered for value in line_ids)
            or row.quantity != sum(items[value].quantity for value in line_ids)
            or row.allocation_source not in {"warehouse", "catalog_variant"}):
            _fail("checkout_reservation_scope_changed")
        covered.update(line_ids)
        proof.append({"id": row.pk, "allocation_key": row.allocation_key, "quantity": row.quantity,
            "line_ids": list(line_ids), "fingerprint": row.reservation_fingerprint,
            "expires_at": row.expires_at.isoformat()})
    if covered != set(items):
        _fail("checkout_finite_reservation_missing")
    return proof


def owned_stock_sync_reservation_ids(request, decision, cart, now):
    """Read-only exact initial-offer holds the already verified sync can reuse.

    The sync caller separately fences the signed review and actual source cart.
    No invoice, paid commitment, different cart or historical hold is excluded.
    Stock quantity is deliberately not a source-ownership proof: the explicit
    recount exists precisely because the numeric count may be wrong.
    """
    from management.models import IgCheckoutProposal, IgCheckoutInventoryReservation as Reservation
    from management.services import ig_fulfillment_reviews as reviews
    from management.services.ig_revision_checkout import checkout_owner_scope
    from management.services.ig_revision_cart_binding import same_checkout_source_capture, validate_cart_binding
    from management.services.ig_inventory import _fingerprint
    if (not reviews._verified(request, "request") or not reviews._verified(decision, "decision")
        or decision.request_id != request.pk or decision.envelope.get("request_digest") != request.envelope_hmac
        or request.envelope.get("scope") != "cart" or reviews._cart_identity(cart) != request.envelope.get("cart_identity")):
        return []
    owner = checkout_owner_scope(request.client)
    proposal_id = ((owner or {}).get("deal") or {}).get("active_checkout_proposal_id")
    if not proposal_id:
        return []
    proposal = IgCheckoutProposal.objects.filter(pk=proposal_id, client_id=request.client_id,
        commercial_episode_id=(owner.get("episode") or {}).get("pk"),
        deal_id=owner["deal"]["pk"]).first()
    if (not proposal or proposal.is_expired or proposal.current_invoice_generation_id
        or proposal.winner_invoice_generation_id or proposal.payment_attempt_id
        or proposal.status not in {IgCheckoutProposal.Status.READY, IgCheckoutProposal.Status.VIEWED}):
        return []
    revision = proposal.revisions.filter(revision=proposal.revision).first()
    snapshot = revision.snapshot if revision else {}
    original_capture = snapshot.get("source_cart_capture") if isinstance(snapshot, dict) else None
    if not isinstance(snapshot, dict) or not same_checkout_source_capture(original_capture, cart):
        return []
    binding = snapshot.get("source_cart_binding")
    # The ordinary offer attaches a deal/proposal and legitimately changes the
    # capture's owner_digest. Its immutable binding still hashes the original
    # capture. Exact current source/cart equality above and the live owned
    # proposal checks permit only that self-attachment; they never retarget an
    # old quote to a new source, recipient, selection or quantity.
    bound = validate_cart_binding(binding=binding, current_capture=original_capture,
        expected_scope=cart.get("scope"))
    if bound.status != "ready":
        return []
    items = list(proposal.items.order_by("pk"))
    quote_map = bound.binding.get("quote_line_map") or []
    if len(items) != len(quote_map) or not items:
        return []
    targets = {row["line_id"]: row.get("target") for row in request.envelope["lines"]}
    for item, mapping in zip(items, quote_map):
        target = targets.get(mapping.get("line_id"))
        if not target or any(target.get(key) != value for key, value in (
            ("product_id", item.product_id), ("color_variant_id", item.color_variant_id),
            ("size", item.size), ("fit_option_code", item.fit_code),
            ("option_values", item.option_values), ("quantity", item.quantity))):
            return []
    by_id, covered, result = {item.pk: item for item in items}, set(), []
    rows = list(Reservation.objects.filter(proposal=proposal, invoice_generation__isnull=True,
        state=Reservation.State.ACTIVE, expires_at__gt=now).order_by("pk"))
    for row in rows:
        ids = row.line_ids
        if (not isinstance(ids, list) or not ids or any(type(value) is not int for value in ids)
            or len(set(ids)) != len(ids) or any(value not in by_id or value in covered for value in ids)
            or row.quantity != sum(by_id[value].quantity for value in ids)
            or row.allocation_source not in {"warehouse", "catalog_variant"}
            or row.reservation_fingerprint != _fingerprint(proposal, row.allocation_key)):
            return []
        covered.update(ids)
        result.append(row.pk)
    return result if covered == set(by_id) else []


def prepare_fulfillment_checkout(request, decision, *, actor, now=None):
    """Return stock_ready or proposal_ready; typed errors abort the outer sync.

    A stock_ready result has no proposal, token or reservation. A proposal_ready
    result references the ordinary manager workspace. Public bearer tokens,
    invoices and customer sends belong to their existing checkout owners.
    """
    from django.contrib.auth import get_user_model
    from management.bot_access import has_all_bot_capabilities, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
    from management.models import IgClient, IgCheckoutProposal
    from management.services import ig_fulfillment_reviews as reviews
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_checkout import CheckoutConfigurationError, create_or_update_proposal, _same_revision_source_cart
    from management.services.ig_checkout_payment import CheckoutPaymentError, _revalidate_frozen_proposal
    from management.services.ig_inventory import InventoryReservationError, reserve_proposal_inventory
    from management.services.ig_revision_checkout import (checkout_authority_control, checkout_owner_scope,
        rebind_checkout_cart_owner, compact_checkout_control, checkout_artifact_control, lock_checkout_cart_sources)
    from product_catalog.models import ProductInventoryPolicy

    if not transaction.get_connection().in_atomic_block:
        _fail("checkout_stock_transaction_required")
    now = now or timezone.now()
    actor = get_user_model().objects.filter(pk=getattr(actor, "pk", None)).first()
    if not actor or not actor.is_active or not has_all_bot_capabilities(actor,
        OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION):
        _fail("actor_forbidden")
    actual = reviews.Request.objects.select_for_update().select_related("client", "revision").filter(pk=getattr(request, "pk", None)).first()
    actual_decision = reviews.Decision.objects.select_for_update().filter(pk=getattr(decision, "pk", None), request=actual).first() if actual else None
    if (not actual or not actual_decision or not reviews._verified(actual, "request")
        or not reviews._verified(actual_decision, "decision")
        or actual.envelope_hmac != getattr(request, "envelope_hmac", None)
        or actual_decision.envelope_hmac != getattr(decision, "envelope_hmac", None)):
        _fail("checkout_review_evidence_invalid")
    request, decision = actual, actual_decision
    if request.envelope.get("scope") != "cart":
        _fail("checkout_current_cart_required")
    latest = reviews.Request.objects.filter(client_id=request.client_id, source__isnull=False,
        created_at__lte=now).order_by("-source_id", "-created_at", "pk").values_list("pk", flat=True).first()
    if latest != request.pk or request.created_at > now:
        _fail("checkout_review_superseded")
    client = IgClient.objects.select_for_update().get(pk=request.client_id)
    request.client = client
    settings_obj = reviews._settings()
    cart = capture_current_selection_lines(client.pk, now=now)
    if not lock_checkout_cart_sources(client, cart):
        _fail("checkout_cart_sources_unavailable")
    reason = reviews._current_reason(request, settings_obj, now=now, current_source_cart=cart)
    if reason:
        _fail(reason)
    reason = reviews._decision_reason(request, decision, now=now, current_source_cart=cart)
    if reason:
        _fail(reason)
    _complete_available_cart(request, decision, cart)
    purchase = _purchase_evidence(cart)
    common = {"request_id": str(request.pk), "request_digest": request.envelope_hmac,
        "decision_id": str(decision.pk), "decision_digest": decision.envelope_hmac,
        "cart_identity": request.envelope["cart_identity"], "source_scope": deepcopy(request.envelope["source_scope"]),
        "stock_authority": "explicit_absolute_recount", "payment_confirmed": False, "order_created": False}
    if purchase is None:
        return {"ready": True, "status": "stock_ready", "reason": "checkout_purchase_authority_missing",
            "proposal_id": None, "reservation_ids": [], "proof": common,
            "manager_workspace_url": f"https://management.twocomms.shop/bot/?section=clients&client_id={client.pk}"}

    # The canonical source gate above replaces only the legacy text/intent gate.
    # All configuration, independent prices and owner/source quote binding still
    # come from the ordinary backend checkout resolver.
    control, reasons = checkout_authority_control(client, {}, source_cart_capture=cart)
    if reasons:
        _fail(reasons[0])
    before = control["checkout_owner_scope"]
    policies = {row.product_id: row.source for row in ProductInventoryPolicy.objects.filter(
        product_id__in=[item["product_id"] for item in control["items"]])}
    if any(policies.get(item["product_id"]) not in {"warehouse", "catalog_variant"} for item in control["items"]):
        _fail("checkout_finite_inventory_policy_required")
    existing_id = (before.get("deal") or {}).get("active_checkout_proposal_id")
    existing = IgCheckoutProposal.objects.select_for_update().filter(pk=existing_id, client=client).first() if existing_id else None
    pay_type, requested_amount, negotiated_total = "online_full", None, None
    evidence_ids = sorted(set((cart.get("fence") or {}).get("source_ids", [])) | {proof["message_id"] for proof in request.envelope["sources"]})
    if existing:
        if existing.current_invoice_generation_id or existing.winner_invoice_generation_id or existing.payment_attempt_id:
            _fail("checkout_generation_already_started")
        if not _same_revision_source_cart(existing, control["source_cart_binding"], control["source_cart_capture"]):
            _fail("checkout_existing_source_cart_changed")
        # Reuse only the independently priced immutable financial agreement.
        # A new recount never authorizes silently repricing an existing offer,
        # losing a negotiated discount or manufacturing replacement evidence.
        try:
            _revalidate_frozen_proposal(existing)
        except CheckoutPaymentError as exc:
            _fail(exc.code)
        pay_type, requested_amount = existing.pay_type, existing.requested_payment_amount
        negotiated_total = existing.quoted_total if existing.negotiated_discount > 0 else None
        evidence_ids = sorted(set(evidence_ids) | set(existing.revisions.get(revision=existing.revision).evidence_message_ids or []))
    try:
        with transaction.atomic():
            proposal = create_or_update_proposal(client=client, pay_type=pay_type, item_specs=control["items"],
                negotiated_total=negotiated_total, requested_payment_amount=requested_amount, allow_promo=existing.allow_promo if existing else True,
                evidence={"message_ids": evidence_ids}, locale=client.language,
                source_cart_binding=control["source_cart_binding"], source_cart_capture=control["source_cart_capture"],
                checkout_owner_scope=before)
            reserve_proposal_inventory(proposal, require_policy=True)
            reservations = _reservation_proof(proposal, now)
            client.refresh_from_db()
            after = checkout_owner_scope(client)
            owned = rebind_checkout_cart_owner(control, before=before, after=after, checkout=proposal)
            if owned is None:
                _fail("checkout_cart_owner_rebind_failed")
            checkout_revision = proposal.revisions.get(revision=proposal.revision)
            artifact = {"kind": "checkout_revision", "proposal_id": proposal.pk,
                "checkout_revision_id": checkout_revision.pk, "revision": proposal.revision}
            reference = compact_checkout_control(owned, artifact=artifact)["source_cart_reference"]
            persisted, reasons = checkout_artifact_control(client, reference)
            if reasons or persisted.get("items") != owned["items"]:
                _fail("checkout_cart_artifact_rebind_failed")
    except CheckoutConfigurationError as exc:
        _fail(exc.reason or exc.code)
    except InventoryReservationError as exc:
        _fail(exc.reason)
    return {"ready": True, "status": "proposal_ready", "reason": "", "proposal_id": proposal.pk,
        "proposal_public_id": str(proposal.public_id), "proposal_revision": proposal.revision,
        "manager_workspace_url": f"https://management.twocomms.shop/bot/?section=clients&client_id={client.pk}",
        "reservation_ids": [row["id"] for row in reservations],
        "proof": {**common, "purchase_sources": purchase, "owner_before": before, "owner_after": after,
            "source_cart_reference": reference, "quote_digest": checkout_revision.digest,
            "reservations": reservations}}
