"""Phase 21 (2026-05-10) — product review models.

Three tables:

* ``Review``       — one row per submission. Always created with
  ``status='pending'`` and only surfaces on the PDP after a moderator
  flips it to ``approved``. We keep ``moderation_note`` so the team
  has institutional memory on why something was rejected, and
  ``is_verified_purchase`` is set automatically when the author has a
  paid ``orders.Order`` containing the product (computed at submit
  time, stored on the row so we don't query orders on every render).

* ``ReviewImage`` — up to 5 photos per review. Stored on a separate
  table so admin/UI can paginate / lightbox cleanly without bloating
  the parent row. Field-level limits in forms; here we just track.

* ``ReviewVote``  — one helpful/unhelpful vote per (review, user).
  Anonymous votes use ``anon_key`` (cookie-derived hash) instead of
  ``user`` so guests can vote without authentication while still
  giving us a deduping key.

The aggregate rating helper lives in ``reviews.services.aggregate``
and is the only piece the SEO/schema layer talks to. Models therefore
intentionally avoid denormalised ``rating_avg`` / ``rating_count``
columns: they would drift the moment an admin un-approves a review,
and the helper is cheap (``GROUP BY product_id``) on the indexes
declared below.
"""

from __future__ import annotations

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from storefront.models import Product


class ReviewStatus(models.TextChoices):
    PENDING = "pending", "На модерації"
    APPROVED = "approved", "Опубліковано"
    REJECTED = "rejected", "Відхилено"


class ReviewCampaign(models.Model):
    title = models.CharField(max_length=120, default="Твоя ідея. Наш кастом.")
    rules_url = models.URLField(blank=True)
    enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.enabled and not self.rules_url.startswith("https://"):
            raise ValidationError({"rules_url": "Для запуску потрібне HTTPS-посилання на опубліковані правила."})


class Review(models.Model):
    """A user-submitted review for one product."""

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="reviews",
        verbose_name="Товар",
    )

    # Author identity. ``user`` is set when the submitter is logged in;
    # for guests we keep ``author_name`` + ``email`` (used only for
    # moderation contact, never displayed publicly) and a session-level
    # ``anon_key`` so we can rate-limit and dedupe.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviews",
        verbose_name="Користувач",
    )
    author_name = models.CharField(
        max_length=80,
        verbose_name="Ім'я для публікації",
        help_text="Відображається на сторінці товару разом із відгуком.",
    )
    email = models.EmailField(
        blank=True,
        verbose_name="Email (для модерації)",
        help_text="Не публікується. Використовується тільки для зв'язку з модератором.",
    )
    anon_key = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        verbose_name="Anon-ключ",
        help_text="Хеш cookie / IP. Дозволяє обмежувати спам гостей.",
    )

    kind = models.CharField(max_length=12, default="review", choices=[("review", "Відгук"), ("comment", "Коментар")])
    city = models.CharField(max_length=80, blank=True)
    pros = models.CharField(max_length=600, blank=True)
    cons = models.CharField(max_length=600, blank=True)
    # Nullable for legacy rows; all new submissions carry a server-derived identity.
    submission_identity = models.CharField(max_length=64, null=True, blank=True, editable=False)
    is_incentivized_review = models.BooleanField(default=False)
    campaign = models.ForeignKey(ReviewCampaign, null=True, blank=True, on_delete=models.PROTECT)
    campaign_opt_in = models.BooleanField(default=False)
    campaign_rules_url = models.URLField(blank=True)
    story_confirmed = models.BooleanField(default=False)

    rating = models.PositiveSmallIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        verbose_name="Оцінка",
        help_text="Ціле число 1-5.",
    )
    title = models.CharField(
        max_length=120,
        blank=True,
        verbose_name="Заголовок",
    )
    body = models.TextField(
        verbose_name="Текст відгуку",
        help_text="Очікуємо щонайменше 20 символів осмисленого тексту.",
    )

    # Moderation lifecycle.
    status = models.CharField(
        max_length=12,
        choices=ReviewStatus.choices,
        default=ReviewStatus.PENDING,
        db_index=True,
        verbose_name="Статус",
    )
    moderation_note = models.TextField(
        blank=True,
        verbose_name="Нотатка модератора",
        help_text="Внутрішня — не показується публічно.",
    )
    moderated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="moderated_reviews",
        verbose_name="Хто модерував",
    )
    moderated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Час модерації",
    )

    # Computed once on submit and frozen — recomputing on every render
    # would force a JOIN to orders; this is cheap and accurate enough.
    is_verified_purchase = models.BooleanField(
        default=False,
        verbose_name="Перевірена покупка",
        help_text="True, якщо в момент створення відгуку у автора був оплачений заказ із цим товаром.",
    )

    helpful_count = models.PositiveIntegerField(
        default=0,
        verbose_name="Корисно",
    )
    unhelpful_count = models.PositiveIntegerField(
        default=0,
        verbose_name="Не корисно",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Відгук"
        verbose_name_plural = "Відгуки"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["product", "submission_identity", "kind"], name="review_product_identity_uniq"),
            models.CheckConstraint(condition=models.Q(rating__isnull=True) | models.Q(rating__gte=1, rating__lte=5), name="review_rating_range"),
        ]
        indexes = [
            # Hot path: PDP renders ``approved`` reviews for a single
            # product newest-first. The composite index makes that a
            # range scan with no sort.
            models.Index(
                fields=["product", "status", "-created_at"],
                name="rev_pdp_lookup_idx",
            ),
            # Helper aggregate (count + AVG) groups by product on
            # ``status='approved'``.
            models.Index(
                fields=["status", "product"],
                name="rev_status_product_idx",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover — admin display
        return f"#{self.pk} {self.product_id} {self.rating}★ {self.status}"

    @property
    def campaign_entries(self):
        if self.campaign_opt_in and self.is_verified_purchase and self.status == ReviewStatus.APPROVED and self.kind == "review":
            return 2 if self.story_confirmed else 1
        return 0

    def mark_approved(self, *, by=None, note: str = "") -> None:
        """Transition to ``approved`` and stamp moderator metadata."""
        self.status = ReviewStatus.APPROVED
        self.moderated_at = timezone.now()
        if by is not None:
            self.moderated_by = by
        if note:
            self.moderation_note = note
        self.save(
            update_fields=[
                "status", "moderated_at", "moderated_by", "moderation_note", "updated_at",
            ]
        )

    def mark_rejected(self, *, by=None, note: str = "") -> None:
        self.status = ReviewStatus.REJECTED
        self.moderated_at = timezone.now()
        if by is not None:
            self.moderated_by = by
        if note:
            self.moderation_note = note
        self.save(
            update_fields=[
                "status", "moderated_at", "moderated_by", "moderation_note", "updated_at",
            ]
        )


def _review_image_upload_path(instance, filename: str) -> str:
    return f"reviews/{instance.review_id}/{filename}"


class ReviewImage(models.Model):
    """A photo attached to a review (max 5 enforced at the form layer)."""

    review = models.ForeignKey(
        Review, on_delete=models.CASCADE, related_name="images",
        verbose_name="Відгук",
    )
    image = models.ImageField(
        upload_to=_review_image_upload_path,
        verbose_name="Фото",
    )
    order = models.PositiveSmallIntegerField(default=0, verbose_name="Порядок")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Фото відгуку"
        verbose_name_plural = "Фото відгуків"
        ordering = ["order", "id"]


class ReviewVote(models.Model):
    """Helpful/unhelpful vote.

    Either ``user`` (registered) or ``anon_key`` (guest) is set —
    enforced via a ``CheckConstraint`` so we never end up with rows
    that can't be deduped.
    """

    HELPFUL = "helpful"
    UNHELPFUL = "unhelpful"
    VOTE_CHOICES = [
        (HELPFUL, "Корисно"),
        (UNHELPFUL, "Не корисно"),
    ]

    review = models.ForeignKey(
        Review, on_delete=models.CASCADE, related_name="votes",
        verbose_name="Відгук",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="review_votes",
    )
    anon_key = models.CharField(max_length=64, blank=True, db_index=True)
    anon_identity = models.GeneratedField(
        expression=models.Case(
            models.When(user__isnull=True, then=models.F("anon_key")),
            default=models.Value(None),
            output_field=models.CharField(max_length=64),
        ),
        output_field=models.CharField(max_length=64),
        db_persist=True,
    )
    value = models.CharField(max_length=10, choices=VOTE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Голос за відгук"
        verbose_name_plural = "Голоси за відгуки"
        constraints = [
            models.UniqueConstraint(
                fields=["review", "user"],
                name="rev_vote_unique_user",
            ),
            models.UniqueConstraint(
                fields=["review", "anon_identity"],
                name="rev_vote_unique_anon",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False)
                    | (models.Q(user__isnull=True) & ~models.Q(anon_key=""))
                ),
                name="rev_vote_user_or_anon_required",
            ),
        ]


class ReviewSubmissionWindow(models.Model):
    """Short-lived, hashed, cross-worker abuse counters; cleaned on intake."""
    key = models.CharField(max_length=64, unique=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
