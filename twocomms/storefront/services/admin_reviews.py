"""Paginated moderation and an honest launch milestone for the future program."""
from django.core.paginator import Paginator
from django.db.models import Q
from django.urls import reverse


def _augment(reviews):
    out = []
    for r in reviews:
        out.append({
            "id": r.pk, "rating": r.rating, "kind": r.kind,
            "title": r.title, "body": r.body, "body_preview": r.body,
            "author_name": r.author_name, "city": r.city, "pros": r.pros, "cons": r.cons,
            "email": r.email, "user_id": r.user_id, "is_verified_purchase": r.is_verified_purchase,
            "status": r.status, "created_at": r.created_at, "moderation_note": r.moderation_note,
            "images": list(r.images.all()), "is_incentivized_review": r.is_incentivized_review,
            "campaign_opt_in": r.campaign_opt_in, "campaign_entries": r.campaign_entries,
            "story_confirmed": r.story_confirmed,
            "public_anchor": reverse("product", kwargs={"slug": r.product.slug}) + f"?review={r.pk}#review-{r.pk}",
            "product": r.product,
            "stars_html": "★" * r.rating + "☆" * (5-r.rating) if r.rating else "Коментар без оцінки",
        })
    return out


def build_reviews_context(request=None):
    from reviews.models import Review, ReviewStatus, ReviewCampaign
    params = request.GET if request else {}
    status = params.get("review_status", "pending")
    if status not in ReviewStatus.values:
        status = "pending"
    qs = Review.objects.filter(status=status).select_related("product", "user").prefetch_related("images")
    query = params.get("review_q", "")[:100].strip()
    if query:
        qs = qs.filter(Q(body__icontains=query) | Q(author_name__icontains=query) | Q(product__title__icontains=query))
    qs = qs.order_by("created_at" if status == "pending" else "-created_at", "pk")
    page = Paginator(qs, 20).get_page(params.get("review_page"))
    counters = {s: Review.objects.filter(status=s).count() for s in ReviewStatus.values}
    counters["total"] = sum(counters.values())
    milestone = Review.objects.filter(status="approved", kind="review", rating__isnull=False).count()
    return {
        "reviews_rows": _augment(page.object_list), "reviews_page": page,
        "reviews_counters": counters, "review_status": status, "review_q": query,
        "review_milestone": milestone, "review_milestone_percent": min(milestone, 100),
        "review_campaign_config": ReviewCampaign.objects.order_by("-pk").first(),
    }
