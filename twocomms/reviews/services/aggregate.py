"""Phase 21 (2026-05-10) — review aggregation.

The single source of truth for "what does the PDP / Product schema
``aggregateRating`` show?". Every other layer (template tags, schema
generator, admin dashboards) MUST go through this module so the
business rule "AggregateRating only at ≥3 approved reviews" is
enforced exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from django.db.models import Avg, Count

from reviews.models import Review, ReviewStatus


# Threshold below which we deliberately HIDE the rating block and
# do NOT emit ``aggregateRating`` JSON-LD.
#
# SEO v1.0 Phase 12 (2026-05-13) — finding (M). Phase 21 ran with
# ``threshold = 3`` to mitigate the "1 review, 5★ looks sketchy"
# anti-spam concern. The deep audit (§6, §13.1.2) demonstrated that
# our PDP traffic is too thin for the catalog to ever reach 3
# approved reviews on most products before the SERP star-rating
# CTR uplift compounds — an extreme cold-start penalty. Google's
# documented contract (`schema.org/AggregateRating`) accepts
# ``reviewCount >= 1`` and the structured-data validator does NOT
# warn at low counts; the manipulation heuristic only fires when
# real-world reviews disagree with the displayed avg. Lowering the
# threshold to 1 unlocks the rich result for first-mover products
# the moment they earn a single approved review, which Search
# Console A/B testing on small e-commerce catalogues consistently
# shows yields a 5–15 % CTR uplift. The companion review-collection
# loop (Phase 13.x coupon → review) keeps quality calibrated.
MIN_APPROVED_REVIEWS_FOR_RATING = 1


@dataclass(frozen=True)
class ProductReviewSummary:
    """Plain data the template / schema layer consumes."""

    count: int                      # always the real approved count
    avg: Optional[float]            # 1.0–5.0, rounded to 1dp; None when empty
    histogram: dict[int, int]       # {5: n5, 4: n4, ..., 1: n1} — zero-filled
    show_rating: bool               # count >= MIN_APPROVED_REVIEWS_FOR_RATING

    @property
    def has_any_approved(self) -> bool:
        return self.count > 0

    # Templates predating this datastore were hardcoded to access
    # ``.average`` (see ``pages/product_detail.html`` line ~197). Keep
    # the alias to avoid forking copy across templates.
    @property
    def average(self) -> Optional[float]:
        return self.avg

    @property
    def histogram_rows(self) -> list[dict]:
        """Iterable form of ``histogram`` ordered 5★ → 1★ for templates.

        Django templates can't index a dict by a variable key, so we
        materialise the rows here. Each row is ``{"star": int, "count":
        int}``; safe to iterate with ``{% for row in ... %}``.
        """
        return [{"star": s, "count": int(self.histogram.get(s, 0))} for s in (5, 4, 3, 2, 1)]


def aggregate_rating_for_product(product) -> ProductReviewSummary:
    """Compute the public review summary for a single product.

    Cheap query: one ``COUNT`` + one ``AVG`` plus a histogram via
    ``values('rating').annotate(Count('id'))`` — all hitting the
    ``rev_status_product_idx`` index.
    """

    qs = Review.objects.filter(
        product=product,
        status=ReviewStatus.APPROVED,
        kind="review", rating__isnull=False,
    )

    agg = qs.aggregate(count=Count("id"), avg=Avg("rating"))
    count = int(agg["count"] or 0)
    avg_value = agg["avg"]
    avg = round(float(avg_value), 1) if avg_value is not None else None

    # Histogram: always include keys 1..5 even if empty so the
    # template can render bars without conditionals.
    histogram = {k: 0 for k in range(1, 6)}
    if count:
        for row in qs.values("rating").annotate(n=Count("id")):
            rating = int(row["rating"])
            if 1 <= rating <= 5:
                histogram[rating] = int(row["n"])

    return ProductReviewSummary(
        count=count,
        avg=avg,
        histogram=histogram,
        show_rating=count >= MIN_APPROVED_REVIEWS_FOR_RATING,
    )


__all__ = [
    "MIN_APPROVED_REVIEWS_FOR_RATING",
    "ProductReviewSummary",
    "aggregate_rating_for_product",
]
