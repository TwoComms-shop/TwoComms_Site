"""Phase 21 (PR-4c) — review submission + voting endpoints.

Mutations are POST-only and CSRF-protected. Submission limits use hashed
database counters across workers. Private state is fetched without caching;
public content passes through the moderation queue.
"""

from __future__ import annotations

import io
import warnings
from uuid import uuid4

from PIL import Image, ImageOps
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.middleware.csrf import get_token
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext as _
from django.contrib.admin.views.decorators import staff_member_required
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponseBadRequest, HttpResponseRedirect, JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from storefront.models import Product

from .forms import ReviewForm
from .models import Review, ReviewImage, ReviewStatus, ReviewVote, ReviewCampaign
from .services.permissions import has_paid_order_with_product
from .write_freeze import review_writes_frozen
from .services.identity import guest_key, owned_reviews, submission_identity
from .services.purchase_invites import (PurchaseReviewError, SESSION_KEY as PURCHASE_REVIEW_SESSION_KEY,
                                       capture_invitation, session_invitation, validate_invitation,
                                       seal_bound_review, purchase_review_context)


log = logging.getLogger(__name__)


# Photo upload caps. Enforced server-side; the template's <input
# type="file" multiple> doesn't natively limit count or size.
_MAX_IMAGES_PER_REVIEW = 5
_MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
_ALLOWED_IMAGE_CT = {"image/jpeg", "image/png", "image/webp"}

# Global fixed-hour limits: 20/IP, 5/guest session, 8/authenticated account.
_GUEST_RATE_WINDOW = 60 * 60  # 1 hour


def _client_ip(request: HttpRequest) -> str:
    # Forwarded headers are client-controlled unless the deployment explicitly
    # establishes a trusted proxy. REMOTE_ADDR is the safe default.
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _anon_key(request: HttpRequest) -> str:
    return guest_key(request, create=True)


def _is_rate_limited(request: HttpRequest, product_id: int) -> bool:
    from .models import ReviewSubmissionWindow
    from django.utils import timezone
    from django.db.models import F
    window = int(timezone.now().timestamp()) // _GUEST_RATE_WINDOW
    identities = [("ip", _client_ip(request), 20)]
    if request.user.is_authenticated:
        identities.append(("user", str(request.user.pk), 8))
    else:
        identities.append(("guest", guest_key(request, create=True), 5))
    # Database counters work across processes and do not depend on cache atomicity.
    for scope, identity, limit in identities:
        key = salted_hmac("reviews.throttle", f"{scope}:{identity}:{window}", algorithm="sha256").hexdigest()
        with transaction.atomic():
            row, _ = ReviewSubmissionWindow.objects.get_or_create(key=key)
            updated = ReviewSubmissionWindow.objects.filter(pk=row.pk, attempts__lt=limit).update(attempts=F("attempts") + 1)
        if not updated:
            return True
    ReviewSubmissionWindow.objects.filter(created_at__lt=timezone.now() - timezone.timedelta(days=2)).delete()
    return False


def _validate_uploaded_images(files):
    """Return a list of cleaned file objects or raise ``ValueError``.

    ``files`` is a ``MultiValueDict.getlist('images')`` result; we
    cap at ``_MAX_IMAGES_PER_REVIEW`` and reject oversize / wrong-MIME
    files outright (no silent truncation — the user deserves to know).
    """
    cleaned = []
    for f in files[: _MAX_IMAGES_PER_REVIEW + 1]:
        if len(cleaned) >= _MAX_IMAGES_PER_REVIEW:
            raise ValueError(f"Максимум {_MAX_IMAGES_PER_REVIEW} фото на відгук.")
        if f.size > _MAX_IMAGE_SIZE_BYTES:
            raise ValueError(f"Фото «{f.name}» більше за 5 МБ.")
        ct = (getattr(f, "content_type", "") or "").lower()
        if ct not in _ALLOWED_IMAGE_CT:
            raise ValueError(
                f"Фото «{f.name}»: дозволені формати JPEG, PNG, WebP."
            )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(f) as source:
                    if source.format not in {"JPEG", "PNG", "WEBP"} or source.width * source.height > 20_000_000:
                        raise ValueError(_("Завелике або непідтримуване фото."))
                    source.load()
                    picture = ImageOps.exif_transpose(source).convert("RGB")
                    picture.thumbnail((2000, 2000))
                    output = io.BytesIO()
                    picture.save(output, format="JPEG", quality=88)
            cleaned.append(ContentFile(output.getvalue(), name=f"{uuid4().hex}.jpg"))
        except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError(_("Не вдалося прочитати фото. Оберіть JPEG, PNG або WebP.")) from exc
    return cleaned


def _redirect_back(request: HttpRequest, product: Product) -> HttpResponseRedirect:
    """After a (form) POST we always bounce to the product page with
    an anchor so the user sees their pending notice / errors. We
    never trust ``HTTP_REFERER`` blindly — must point at our own
    product URL."""
    return HttpResponseRedirect(
        reverse("product", kwargs={"slug": product.slug}) + "#product-reviews"
    )


def _write_freeze_response() -> JsonResponse:
    response = JsonResponse(
        {
            "ok": False,
            "error": "temporarily_unavailable",
            "message": "Відгуки тимчасово недоступні. Спробуйте за хвилину.",
        },
        status=503,
    )
    response["Retry-After"] = "60"
    return response


@require_POST
def submit_review(request: HttpRequest, product_slug: str):
    """Public endpoint — anyone can POST. Auth users get
    ``is_verified_purchase=True`` automatically when they have a paid
    order containing the product.

    Returns:
        JsonResponse for AJAX (``X-Requested-With == XMLHttpRequest``),
        or a 302 to the PDP otherwise.
    """
    if review_writes_frozen():
        return _write_freeze_response()

    product = get_object_or_404(
        Product.objects.filter(status="published"),
        slug=product_slug,
    )

    is_ajax = request.headers.get("X-Requested-With", "") == "XMLHttpRequest"

    invitation = session_invitation(request, product)
    if (request.session.get(PURCHASE_REVIEW_SESSION_KEY) or {}).get(str(product.pk)) and invitation is None:
        msg = _("Запрошення більше не дійсне. Попросіть нове посилання в Instagram.")
        if is_ajax:
            return JsonResponse({"ok": False, "error": msg}, status=403)
        messages.error(request, msg)
        return _redirect_back(request, product)

    if _is_rate_limited(request, product.id):
        msg = _("Забагато спроб. Спробуйте через годину.")
        if is_ajax:
            return JsonResponse({"ok": False, "error": msg}, status=429)
        messages.error(request, msg)
        return _redirect_back(request, product)

    # Keep only bounded text for a progressive (non-JS) retry; no upload data.
    if not is_ajax:
        request.session["review_draft"] = {
            "product_id": product.pk,
            "data": {key: request.POST.get(key, "")[:4000] for key in
                     ("kind", "rating", "title", "body", "author_name", "email", "city", "pros", "cons")},
        }
    form = ReviewForm(request.POST, guest=not request.user.is_authenticated, purchase_invited=invitation is not None)
    if not form.is_valid():
        # Compact error format for AJAX, message-bus for traditional POST.
        if is_ajax:
            return JsonResponse(
                {"ok": False, "errors": form.errors.get_json_data()},
                status=400,
            )
        for error in form.errors.values():
            messages.error(request, error[0])
        return _redirect_back(request, product)

    cleaned = form.cleaned_data
    if cleaned.get("_is_bot"):
        # Honeypot tripped: drop silently without recording raw network data.
        log.info("reviews.honeypot.tripped product=%s", product.id)
        if is_ajax:
            return JsonResponse({"ok": True, "status": "pending"})
        messages.success(request, "Дякуємо! Ваш відгук на модерації.")
        return _redirect_back(request, product)

    existing = owned_reviews(request, product).filter(kind=cleaned["kind"]).first()
    if existing:
        if is_ajax:
            return JsonResponse({"ok": False, "error": _("Ви вже залишили відгук або коментар до цього товару."), "duplicate": True}, status=409)
        messages.info(request, _("Ваш відгук або коментар уже збережено."))
        return _redirect_back(request, product)

    campaign = ReviewCampaign.objects.filter(enabled=True).order_by("-pk").first()
    verified = bool(invitation) or has_paid_order_with_product(request.user, product)
    if cleaned.get("campaign_opt_in") and (not campaign or not verified or cleaned["kind"] != "review" or not cleaned.get("email")):
        return JsonResponse({"ok": False, "error": "Для участі потрібні підтверджена покупка, відгук і email для зв’язку."}, status=400)

    # Photos (optional).
    try:
        images = _validate_uploaded_images(request.FILES.getlist("images"))
    except ValueError as exc:
        if is_ajax:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        messages.error(request, str(exc))
        return _redirect_back(request, product)

    # The invitation identifies the IG owner, never whichever account happens
    # to be logged into this browser. Public account-owned reviews keep their
    # existing account association.
    user = request.user if request.user.is_authenticated and invitation is None else None
    anon_key = "" if user or invitation else _anon_key(request)
    is_verified = bool(invitation) or (has_paid_order_with_product(user, product) if user else False)

    try:
        with transaction.atomic():
            if invitation is not None:
                from management.ig_bot_models import IgClient
                from .models import ReviewPurchaseInvitation
                if IgClient.objects.select_for_update().filter(pk=invitation.client_id).first() is None:
                    raise PurchaseReviewError("invitation_owner_unavailable")
                invitation = ReviewPurchaseInvitation.objects.select_for_update().filter(pk=invitation.pk).first()
                if invitation is None:
                    raise PurchaseReviewError("invitation_unavailable")
                validate_invitation(invitation)
            review = Review.objects.create(
                product=product,
                submission_identity=submission_identity(request, product),
                purchase_invitation=invitation,
                campaign=campaign,
                is_incentivized_review=bool(invitation or campaign),
                campaign_opt_in=bool(cleaned.get("campaign_opt_in")),
                campaign_rules_url=campaign.rules_url if campaign else "",
                kind=cleaned["kind"], city=cleaned.get("city", ""),
                pros=cleaned.get("pros", ""), cons=cleaned.get("cons", ""),
                user=user,
                author_name=cleaned["author_name"],
                email=cleaned.get("email") or "",
                anon_key=anon_key,
                rating=cleaned["rating"],
                title=cleaned.get("title") or "",
                body=cleaned["body"],
                is_verified_purchase=is_verified,
                status=ReviewStatus.PENDING,
            )
            if invitation is not None:
                seal_bound_review(review)
            for idx, f in enumerate(images):
                ReviewImage.objects.create(review=review, image=f, order=idx)
    except PurchaseReviewError:
        msg = _("Покупку для цього запрошення більше не підтверджено. Зверніться в Instagram.")
        if is_ajax:
            return JsonResponse({"ok": False, "error": msg}, status=403)
        messages.error(request, msg)
        return _redirect_back(request, product)
    except IntegrityError:
        if not owned_reviews(request, product).filter(kind=cleaned["kind"]).exists():
            raise
        return JsonResponse({"ok": False, "error": _("Ваш відгук уже збережено."), "duplicate": True}, status=409)
    request.session.pop("review_draft", None)

    if is_ajax:
        return JsonResponse({"ok": True, "status": "pending", "review_id": review.id,
                             "purchase_review_context": purchase_review_context(request, product)})
    messages.success(
        request,
        "Дякуємо! Ваш відгук відправлено на модерацію — після перевірки він зʼявиться на сторінці товару.",
    )
    return _redirect_back(request, product)


@require_POST
def vote_review(request: HttpRequest, review_id: int):
    """Helpful / unhelpful vote. JSON-only (called from PDP JS)."""
    if review_writes_frozen():
        return _write_freeze_response()

    try:
        review = Review.objects.get(pk=review_id, status=ReviewStatus.APPROVED)
    except Review.DoesNotExist:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)

    value = (request.POST.get("value") or "").strip()
    if value not in (ReviewVote.HELPFUL, ReviewVote.UNHELPFUL):
        return HttpResponseBadRequest("invalid value")

    user = request.user if request.user.is_authenticated else None
    anon_key = "" if user else _anon_key(request)

    # Upsert: same user/anon flipping their vote rewrites the row
    # rather than creating a duplicate (constraints would block it).
    lookup = {"review": review}
    if user is not None:
        lookup["user"] = user
    else:
        lookup["user__isnull"] = True
        lookup["anon_key"] = anon_key

    existing = ReviewVote.objects.filter(**lookup).first()
    if existing is not None:
        if existing.value == value:
            # idempotent — already voted this way
            pass
        else:
            existing.value = value
            existing.save(update_fields=["value"])
    else:
        ReviewVote.objects.create(
            review=review,
            user=user,
            anon_key=anon_key if user is None else "",
            value=value,
        )

    # Recompute aggregate counters cheaply.
    helpful = ReviewVote.objects.filter(review=review, value=ReviewVote.HELPFUL).count()
    unhelpful = ReviewVote.objects.filter(review=review, value=ReviewVote.UNHELPFUL).count()
    Review.objects.filter(pk=review.pk).update(
        helpful_count=helpful, unhelpful_count=unhelpful
    )

    return JsonResponse({"ok": True, "helpful": helpful, "unhelpful": unhelpful})


@login_required
def my_reviews(request: HttpRequest):
    """Phase 21 (R12) — personal cabinet section listing the logged-in
    user's reviews grouped by moderation status. Read-only — editing
    requires re-submitting via the PDP form; rejections show the
    moderator note so the user understands why.
    """
    reviews_qs = (
        Review.objects
        .filter(user=request.user)
        .select_related("product")
        .prefetch_related("images")
        .order_by("-created_at")
    )
    counts = {
        "approved": 0,
        "pending": 0,
        "rejected": 0,
    }
    for r in reviews_qs:
        if r.status in counts:
            counts[r.status] += 1
    return render(
        request,
        "pages/my_reviews.html",
        {
            "reviews": reviews_qs,
            "counts": counts,
            "total_reviews": sum(counts.values()),
        },
    )


@never_cache
@require_GET
def review_state(request, product_slug):
    """Private ownership and fresh CSRF, never stored in the shared PDP cache."""
    product = get_object_or_404(Product, slug=product_slug, status="published")
    rows = list(owned_reviews(request, product).order_by("-created_at")[:5])
    html = render_to_string("partials/review_private.html", {"own_reviews": rows}, request=request)
    kinds = sorted({row.kind for row in rows})
    return JsonResponse({"csrf": get_token(request), "html": html, "has_review": bool(rows), "form_complete": "review" in kinds, "submitted_kinds": kinds,
                         "purchase_review_context": purchase_review_context(request, product)})


@never_cache
@require_GET
def purchase_invitation(request, token):
    """Exchange a private capability for session authority, then clean the URL."""
    try:
        invitation = capture_invitation(request, token)
    except PurchaseReviewError:
        response = HttpResponse(_("Посилання на відгук недійсне або термін його дії минув."), status=403)
    else:
        response = HttpResponseRedirect(reverse("product", kwargs={"slug": invitation.product.slug}) + "#product-reviews")
    response["Referrer-Policy"] = "no-referrer"
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@require_GET
def merchant_feed(request):
    from django.http import HttpResponse
    from .services.merchant import build_product_review_feed
    return HttpResponse(build_product_review_feed(), content_type="application/xml; charset=utf-8")


@staff_member_required
@require_POST
def campaign_settings(request):
    from django.core.exceptions import ValidationError
    if review_writes_frozen():
        return _write_freeze_response()
    with transaction.atomic():
        campaign, _ = ReviewCampaign.objects.get_or_create(pk=1)
        campaign = ReviewCampaign.objects.select_for_update().get(pk=campaign.pk)
        campaign.rules_url = (request.POST.get("rules_url") or "").strip()
        campaign.enabled = request.POST.get("enabled") == "on"
        try:
            campaign.full_clean()
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            campaign.save()
            from storefront.services.catalog_helpers import bump_public_product_order_version
            transaction.on_commit(bump_public_product_order_version)
            messages.success(request, "Налаштування програми збережено.")
    return HttpResponseRedirect(reverse("admin_panel") + "?section=reviews")
