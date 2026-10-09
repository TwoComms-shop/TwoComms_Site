"""Exact purchased-product authority for private Instagram review invitations.

Tokens grant one review capability, never account authentication. Proof reads
perform no writes and take no locks; callers that mint rewards own lock order.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.core.signing import Signer
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

SESSION_KEY = "purchase_review_invitations_v1"
PROOF_VERSION = 1


class PurchaseReviewError(ValueError):
    pass


def _keyring():
    keys = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
    return {hashlib.sha256(str(key).encode()).hexdigest()[:24]: str(key) for key in keys if key}


def _key(key_id):
    try:
        return _keyring()[key_id]
    except KeyError as exc:
        raise PurchaseReviewError("signing_key_unavailable") from exc


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _identity(client, key_id):
    identity = unicodedata.normalize("NFKC", str(client.igsid or "")).strip().casefold()
    if not identity:
        raise PurchaseReviewError("identity_unavailable")
    return hmac.new(_key(key_id).encode(), f"purchase-review-owner:v1:{identity}".encode(), hashlib.sha256).hexdigest()


def _reset_audit_id(client_id):
    from management.ig_bot_models import IgFunnelResetAudit
    return IgFunnelResetAudit.objects.filter(client_id=client_id).order_by("-pk").values_list("pk", flat=True).first() or 0


def _custom_line_ambiguous(order, item):
    """Explicit custom line markers fail closed; mixed catalog orders remain valid."""
    values = item.option_values or {}
    if not isinstance(values, dict):
        return True
    if any(values.get(key) for key in ("custom_print", "custom_print_lead_id", "custom_print_creation_id", "is_custom")):
        return True
    payload = order.payment_payload or {}
    if not isinstance(payload, dict):
        return True
    creation = payload.get("custom_print_creation")
    # Established custom-print checkout keeps its leads separate from actual
    # catalog OrderItems. Unknown structures that claim the selected catalog
    # line as custom are not safe to reinterpret.
    if creation is not None:
        if not isinstance(creation, dict) or not isinstance(creation.get("groups"), list):
            return True
        for group in creation["groups"]:
            if not isinstance(group, dict):
                return True
            for key in ("order_item_id", "order_line_id"):
                if str(group.get(key) or "") == str(item.pk):
                    return True
            if str(group.get("product_id") or "") == str(item.product_id):
                return True
    return False


def validate_purchase_source(*, client, order, order_item, assignment_id=None,
                             assignment_version=None, reset_audit_id=None,
                             identity_digest=None, signing_key_id=None):
    """Reload authoritative ownership/payment/carrier/line state, without locks."""
    from management.ig_bot_models import IgClient, IgOrderAssignment
    from management.services.ig_ugc_rewards import ugc_source_order_disqualified
    from orders.fulfillment_truth import nova_poshta_order_fulfillment_confirmed
    from orders.models import Order, OrderItem

    client = IgClient.objects.filter(pk=getattr(client, "pk", client)).first()
    order = Order.objects.filter(pk=getattr(order, "pk", order)).first()
    item = OrderItem.objects.select_related("product").filter(pk=getattr(order_item, "pk", order_item)).first()
    if client is None or client.privacy_erasure_started_at is not None or client.hidden_at or client.is_blocked:
        raise PurchaseReviewError("privacy_or_identity_unavailable")
    if order is None or order.payment_status != "paid" or not nova_poshta_order_fulfillment_confirmed(order):
        raise PurchaseReviewError("purchase_not_fulfilled")
    if ugc_source_order_disqualified(order):
        raise PurchaseReviewError("source_order_returned_or_refunded")
    assignment = IgOrderAssignment.objects.filter(order_id=order.pk, client_id=client.pk,
                                                  unassigned_at__isnull=True).first()
    if assignment is None or (assignment_id is not None and assignment.pk != assignment_id) or (
        assignment_version is not None and assignment.version != assignment_version
    ):
        raise PurchaseReviewError("assignment_changed")
    reset_id = _reset_audit_id(client.pk)
    if reset_audit_id is not None and reset_id != reset_audit_id:
        raise PurchaseReviewError("reset_boundary_changed")
    if signing_key_id and identity_digest != _identity(client, signing_key_id):
        raise PurchaseReviewError("identity_changed")
    if (item is None or item.order_id != order.pk or not item.product_id or item.is_custom
            or item.item_kind != "clothing" or item.qty < 1
            or item.product.status != "published" or not item.product.slug
            or _custom_line_ambiguous(order, item)):
        raise PurchaseReviewError("line_not_reviewable")
    # A complaint or exchange does not remove the buyer's right to publish an
    # honest review. Service cases suppress the separate reward/offer policy.
    return client, order, item, assignment, reset_id


def eligible_purchase_review_items(*, client, order):
    from orders.models import OrderItem
    items = []
    for item in OrderItem.objects.filter(order_id=getattr(order, "pk", order)).select_related("product").order_by("pk"):
        try:
            validate_purchase_source(client=client, order=order, order_item=item)
        except PurchaseReviewError:
            continue
        items.append(item)
    return items


def eligible_order_items(client, order):
    return eligible_purchase_review_items(client=client, order=order)


def invitation_token(invitation):
    signer = Signer(key=_key(invitation.signing_key_id), fallback_keys=[], salt="reviews.purchase-invite.v1")
    token = signer.sign(invitation.pk.hex)
    if not hmac.compare_digest(_digest(token), invitation.token_hash):
        raise PurchaseReviewError("invitation_token_changed")
    return token


def invitation_url(invitation):
    validate_invitation(invitation)
    base = (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
    return base + reverse("reviews:purchase_invitation", kwargs={"token": invitation_token(invitation)})


@transaction.atomic
def ensure_purchase_review_invitation(*, client, order, order_item, expires_at=None):
    from management.ig_bot_models import IgClient
    from reviews.models import ReviewPurchaseInvitation
    # Serialize source invites on the same client lock used by assignment and
    # privacy operations; no review lock is acquired before this boundary.
    locked_client = IgClient.objects.select_for_update().filter(pk=getattr(client, "pk", client)).first()
    if locked_client is None:
        raise PurchaseReviewError("identity_unavailable")
    client, order, item, assignment, reset_id = validate_purchase_source(
        client=locked_client, order=order, order_item=order_item,
    )
    existing = ReviewPurchaseInvitation.objects.filter(
        client_id_snapshot=client.pk, order_item_id=item.pk,
        assignment_version=assignment.version, reset_audit_id=reset_id,
    ).first()
    if existing:
        validate_invitation(existing)
        invitation_token(existing)
        return existing
    # Retain the original owner digest across server signing-key rotations and
    # subsequent purchases. Removing that retained key fails closed rather
    # than creating a second author identity for the same IG customer.
    owner_anchor = ReviewPurchaseInvitation.objects.filter(client_id_snapshot=client.pk).order_by("created_at", "pk").first()
    key_id = owner_anchor.signing_key_id if owner_anchor else next(iter(_keyring()))
    owner_digest = _identity(client, key_id)
    if owner_anchor and not hmac.compare_digest(owner_digest, owner_anchor.identity_digest):
        raise PurchaseReviewError("identity_changed")
    invitation = ReviewPurchaseInvitation(
        client=client, client_id_snapshot=client.pk, order=order, order_item=item, product=item.product,
        assignment_id=assignment.pk, assignment_version=assignment.version,
        reset_audit_id=reset_id, identity_digest=owner_digest, signing_key_id=key_id,
        expires_at=expires_at or timezone.now() + timedelta(days=90),
    )
    if invitation.expires_at <= timezone.now():
        raise PurchaseReviewError("invitation_expired")
    invitation.token_hash = _digest(Signer(key=_key(key_id), fallback_keys=[], salt="reviews.purchase-invite.v1").sign(invitation.pk.hex))
    invitation.save(force_insert=True)
    return invitation


def validate_invitation(invitation, *, require_live=True):
    if invitation.revoked_at or (require_live and invitation.expires_at <= timezone.now()):
        raise PurchaseReviewError("invitation_expired_or_revoked")
    if invitation.client_id != invitation.client_id_snapshot:
        raise PurchaseReviewError("invitation_owner_unavailable")
    result = validate_purchase_source(
        client=invitation.client_id, order=invitation.order_id, order_item=invitation.order_item_id,
        assignment_id=invitation.assignment_id, assignment_version=invitation.assignment_version,
        reset_audit_id=invitation.reset_audit_id, identity_digest=invitation.identity_digest,
        signing_key_id=invitation.signing_key_id,
    )
    if result[2].product_id != invitation.product_id:
        raise PurchaseReviewError("product_changed")
    return result


def capture_invitation(request, token):
    from reviews.models import ReviewPurchaseInvitation
    if len(token) > 180:
        raise PurchaseReviewError("invalid_invitation")
    try:
        invitation_id = UUID(token.split(":", 1)[0])
    except (ValueError, TypeError) as exc:
        raise PurchaseReviewError("invalid_invitation") from exc
    invitation = ReviewPurchaseInvitation.objects.filter(pk=invitation_id).first()
    if invitation is None or not hmac.compare_digest(invitation.token_hash, _digest(token)):
        raise PurchaseReviewError("invalid_invitation")
    invitation_token(invitation)
    validate_invitation(invitation)
    # An authenticated browser account is never implicitly attached to the IG
    # customer. The limited capability continues to identify the invited owner.
    states = dict(request.session.get(SESSION_KEY) or {})
    states[str(invitation.product_id)] = {"id": str(invitation.pk), "hash": invitation.token_hash}
    request.session[SESSION_KEY] = dict(list(states.items())[-10:])
    return invitation


def session_invitation(request, product, *, require_live=True):
    from reviews.models import ReviewPurchaseInvitation
    state = (request.session.get(SESSION_KEY) or {}).get(str(getattr(product, "pk", product)))
    if not isinstance(state, dict):
        return None
    try:
        invitation_id = UUID(str(state.get("id") or ""))
        invitation = ReviewPurchaseInvitation.objects.filter(pk=invitation_id, product_id=getattr(product, "pk", product)).first()
        if invitation is None or not hmac.compare_digest(str(state.get("hash") or ""), invitation.token_hash):
            return None
        validate_invitation(invitation, require_live=require_live)
        return invitation
    except (ValueError, PurchaseReviewError):
        return None


def invitation_submission_identity(invitation):
    return _digest("purchase-review-submitter:v1:" + invitation.identity_digest)


def _content(review):
    return {key: getattr(review, key) for key in ("kind", "rating", "author_name", "title", "body", "city", "pros", "cons")}


def _review_material(review):
    invitation = review.purchase_invitation
    return {
        "proof_version": PROOF_VERSION, "review_id": review.pk,
        "invitation_id": str(invitation.pk), "client_id": invitation.client_id_snapshot,
        "order_id": invitation.order_id, "order_item_id": invitation.order_item_id,
        "product_id": review.product_id, "assignment_id": invitation.assignment_id,
        "assignment_version": invitation.assignment_version, "reset_audit_id": invitation.reset_audit_id,
        "identity_digest": invitation.identity_digest, "submission_identity": review.submission_identity,
        "kind": review.kind, "rating": review.rating,
        "review_created_at": review.created_at.isoformat(),
        "review_content_digest": _digest(_canonical(_content(review))),
        "signing_key_id": invitation.signing_key_id,
    }


def seal_bound_review(review):
    validate_invitation(review.purchase_invitation)
    if review.kind != "review" or review.rating not in range(1, 6):
        raise PurchaseReviewError("star_review_required")
    if review.submission_identity != invitation_submission_identity(review.purchase_invitation):
        raise PurchaseReviewError("review_owner_changed")
    material = _review_material(review)
    review.purchase_proof_version = PROOF_VERSION
    review.purchase_content_digest = material["review_content_digest"]
    review.purchase_proof_signature = hmac.new(
        _key(material["signing_key_id"]).encode(), ("purchase-review-proof:v1:" + _canonical(material)).encode(), hashlib.sha256,
    ).hexdigest()
    review.save(update_fields=["purchase_proof_version", "purchase_content_digest", "purchase_proof_signature"])
    return review


def validate_bound_review(review, *, client=None, order=None, require_approved=True):
    from reviews.models import Review
    review = Review.objects.select_related("purchase_invitation", "moderated_by").filter(pk=getattr(review, "pk", review)).first()
    if review is None or not review.purchase_invitation_id or review.purchase_proof_version != PROOF_VERSION:
        raise PurchaseReviewError("review_purchase_proof_missing")
    invitation = review.purchase_invitation
    validate_invitation(invitation, require_live=False)
    if client is not None and invitation.client_id_snapshot != getattr(client, "pk", client):
        raise PurchaseReviewError("review_client_mismatch")
    if order is not None and invitation.order_id != getattr(order, "pk", order):
        raise PurchaseReviewError("review_order_mismatch")
    if (review.kind != "review" or review.rating not in range(1, 6)
            or not review.is_verified_purchase or not review.is_incentivized_review
            or (require_approved and review.status != "approved")
            or (require_approved and (review.moderated_at is None or review.moderated_by_id is None))
            or review.product_id != invitation.product_id
            or review.submission_identity != invitation_submission_identity(invitation)):
        raise PurchaseReviewError("review_not_qualified")
    if require_approved and (not review.moderated_by.is_active or not (
        review.moderated_by.is_staff or review.moderated_by.is_superuser
    )):
        raise PurchaseReviewError("review_moderator_unverified")
    material = _review_material(review)
    signature = hmac.new(_key(invitation.signing_key_id).encode(),
                         ("purchase-review-proof:v1:" + _canonical(material)).encode(), hashlib.sha256).hexdigest()
    if (not hmac.compare_digest(material["review_content_digest"], review.purchase_content_digest)
            or not hmac.compare_digest(signature, review.purchase_proof_signature)):
        raise PurchaseReviewError("review_purchase_proof_changed")
    return {**material, "status": review.status, "review_proof_signature": signature,
            "moderated_at": review.moderated_at.isoformat() if review.moderated_at else "",
            "moderated_by_id": review.moderated_by_id}


def eligible_bound_reviews(client, order=None):
    from reviews.models import Review
    queryset = Review.objects.filter(
        purchase_invitation__client_id_snapshot=getattr(client, "pk", client),
        purchase_proof_version=PROOF_VERSION, status="approved", kind="review", rating__range=(1, 5),
    ).order_by("pk")
    if order is not None:
        queryset = queryset.filter(purchase_invitation__order_id=getattr(order, "pk", order))
    return queryset


def purchase_review_context(request, product):
    """Finite private UI state; no token, customer contacts or order identifiers."""
    from reviews.models import Review, ReviewRewardUplift
    state = {"has_valid_invitation": False, "rating_required": False, "email_optional": False,
             "bonus_available": False, "status": "none", "message": ""}
    invitation = session_invitation(request, product, require_live=False)
    if invitation is None:
        return state
    live = invitation.expires_at > timezone.now()
    state.update(has_valid_invitation=live, rating_required=live, email_optional=live)
    from management.services.ig_review_reward import review_uplift_eligibility, validate_review_uplift
    offer = review_uplift_eligibility(invitation.client_id, invitation.order_id)
    review = Review.objects.filter(product=product, submission_identity=invitation_submission_identity(invitation), kind="review").first()
    if review:
        component = ReviewRewardUplift.objects.select_related("reward__promo_code").filter(review=review).first()
        if component is not None:
            try:
                validate_review_uplift(component, component.reward)
            except Exception:
                state.update(status="unavailable", message="Додаткову знижку потрібно перевірити з менеджером.")
            else:
                state.update(status="applied", message="Відгук зараховано. Знижку було збільшено до 15% до тієї самої дати.")
        elif review.status == "pending":
            state.update(status="pending", message="Відгук на модерації. Після схвалення перевіримо можливість додати 5% до невикористаної UGC-знижки 10%.")
        elif review.status == "approved":
            state.update(status="approved", message="Відгук опубліковано. Додаткові 5% можливі лише для чинної невикористаної UGC-знижки 10%.")
        else:
            state.update(status="unavailable", message="Відгук не опубліковано. Додаткова знижка не нарахована.")
        return state
    if not live:
        state.update(status="unavailable", message="Термін запрошення минув.")
        return state
    if offer.get("eligible"):
        state.update(status="eligible", bonus_available=True,
                     message="Залиш чесний відгук з оцінкою 1–5 про придбаний товар. Після модерації до чинної невикористаної UGC-знижки 10% можна додати 5%. Загалом 15%, строк дії не змінюється.")
    else:
        state.update(status="unavailable", message="Можна залишити чесний відгук. Додаткова знижка зараз недоступна.")
    return state
