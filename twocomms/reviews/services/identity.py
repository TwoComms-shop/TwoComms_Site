"""Opaque session ownership, independent of IP changes and session-key rotation."""
import secrets

from django.utils.crypto import salted_hmac


SESSION_KEY = "product_review_identity"


def guest_key(request, *, create=False):
    token = request.session.get(SESSION_KEY)
    if not token and create:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = token
    return salted_hmac("reviews.guest", token, algorithm="sha256").hexdigest() if token else ""


def submission_identity(request, product=None):
    if product is not None:
        from .purchase_invites import session_invitation, invitation_submission_identity
        invitation = session_invitation(request, product)
        if invitation is not None:
            return invitation_submission_identity(invitation)
    if request.user.is_authenticated:
        return salted_hmac("reviews.user", str(request.user.pk), algorithm="sha256").hexdigest()
    return guest_key(request, create=True)


def owned_reviews(request, product):
    from reviews.models import Review
    from django.db.models import Q
    from .purchase_invites import session_invitation, invitation_submission_identity
    qs = Review.objects.filter(product=product)
    invitation = session_invitation(request, product, require_live=False)
    if invitation is not None:
        # A purchase invitation proves its own IG author. A different account
        # already logged into this browser must not hide or block that author.
        return qs.filter(submission_identity=invitation_submission_identity(invitation))
    invited_owner = Q(submission_identity=invitation_submission_identity(invitation)) if invitation else Q(pk__in=[])
    if request.user.is_authenticated:
        key = guest_key(request)
        owner = Q(user=request.user) | invited_owner
        if key:
            owner |= Q(user__isnull=True, anon_key=key)
        return qs.filter(owner)
    key = guest_key(request)
    owner = invited_owner
    if key:
        owner |= Q(user__isnull=True, anon_key=key)
    return qs.filter(owner)
