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


def submission_identity(request):
    if request.user.is_authenticated:
        return salted_hmac("reviews.user", str(request.user.pk), algorithm="sha256").hexdigest()
    return guest_key(request, create=True)


def owned_reviews(request, product):
    from reviews.models import Review
    qs = Review.objects.filter(product=product)
    if request.user.is_authenticated:
        from django.db.models import Q
        key = guest_key(request)
        return qs.filter(Q(user=request.user) | Q(user__isnull=True, anon_key=key)) if key else qs.filter(user=request.user)
    key = guest_key(request)
    return qs.filter(user__isnull=True, anon_key=key) if key else qs.none()
