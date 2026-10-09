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

import uuid

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


class ImmutableReviewProofQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if set(kwargs) - {"revoked_at"}:
            raise ValueError("Purchase review proof is immutable")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValueError("Purchase review proof is immutable")

    def delete(self):
        raise ValueError("Purchase review proof is retained for audit")

    def _raw_delete(self, using):
        raise ValueError("Purchase review proof is retained for audit")


class ReviewPurchaseInvitation(models.Model):
    """Private capability for one purchased catalog line; never an account login."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("management.IgClient", null=True, blank=True,
                              on_delete=models.SET_NULL, db_constraint=False, related_name="review_invitations")
    client_id_snapshot = models.PositiveBigIntegerField()
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, db_constraint=False)
    order_item = models.ForeignKey("orders.OrderItem", on_delete=models.PROTECT, db_constraint=False)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, db_constraint=False)
    assignment_id = models.PositiveBigIntegerField()
    assignment_version = models.PositiveIntegerField()
    reset_audit_id = models.PositiveBigIntegerField(default=0)
    identity_digest = models.CharField(max_length=64)
    signing_key_id = models.CharField(max_length=24)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = ImmutableReviewProofQuerySet.as_manager()

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["client_id_snapshot", "order_item", "assignment_version", "reset_audit_id"],
            name="review_invite_source_once",
        )]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            fields = [f.attname for f in self._meta.concrete_fields if f.name not in {"client", "revoked_at"}]
            previous = type(self).objects.filter(pk=self.pk).values(*fields, "client_id").first()
            if previous and (any(previous[field] != getattr(self, field) for field in fields)
                             or self.client_id not in {previous["client_id"], None}):
                raise ValueError("Purchase review invitation source is immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Purchase review invitation is retained for audit")


class ReviewQuerySet(models.QuerySet):
    def update(self, **kwargs):
        mutable = {"status", "moderation_note", "moderated_by", "moderated_by_id", "moderated_at",
                   "updated_at", "helpful_count", "unhelpful_count"}
        if (set(kwargs) - mutable or "status" in kwargs) and self.filter(purchase_invitation__isnull=False).exists():
            raise ValueError("Bound review content and purchase proof are immutable")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        objs = tuple(objs)
        if any(obj.purchase_invitation_id for obj in objs):
            raise ValueError("Bound review bulk updates are unsupported")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def delete(self):
        if self.filter(purchase_invitation__isnull=False).exists():
            raise ValueError("Bound purchase review is retained for audit")
        return super().delete()

    def _raw_delete(self, using):
        if self.filter(purchase_invitation__isnull=False).exists():
            raise ValueError("Bound purchase review is retained for audit")
        return super()._raw_delete(using)


class Review(models.Model):
    """A user-submitted review for one product."""

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="reviews",
        verbose_name="Товар",
    )
    purchase_invitation = models.OneToOneField(ReviewPurchaseInvitation, null=True, blank=True,
                                              on_delete=models.PROTECT, db_constraint=False,
                                              related_name="review", editable=False)
    purchase_proof_version = models.PositiveSmallIntegerField(default=0, editable=False)
    purchase_content_digest = models.CharField(max_length=64, blank=True, editable=False)
    purchase_proof_signature = models.CharField(max_length=64, blank=True, editable=False)
    objects = ReviewQuerySet.as_manager()

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

    def save(self, *args, **kwargs):
        from django.db import router, transaction
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        # The post-save moderation receiver persists its retry job before this
        # transaction commits. Enqueue failure rolls back approval as well;
        # an on-commit fast path is only an optimization over durable recovery.
        with transaction.atomic(using=using):
            if self.pk:
                previous = type(self).objects.using(using).select_for_update().filter(pk=self.pk).values().first()
                if previous and previous["purchase_invitation_id"]:
                    if self.status == ReviewStatus.APPROVED and previous["status"] != ReviewStatus.APPROVED:
                        from .services.purchase_invites import validate_bound_review
                        validate_bound_review(self, require_approved=False)
                    mutable = {"status", "moderation_note", "moderated_by_id", "moderated_at", "updated_at",
                               "helpful_count", "unhelpful_count", "user_id", "story_confirmed"}
                    # A proof is sealed once, after the database assigned the review ID.
                    if not previous["purchase_proof_signature"]:
                        mutable |= {"purchase_proof_version", "purchase_content_digest", "purchase_proof_signature"}
                    for field in self._meta.concrete_fields:
                        name = field.attname
                        if name not in mutable and previous[name] != getattr(self, name):
                            raise ValueError("Bound review content and purchase proof are immutable")
                    if self.user_id not in {previous["user_id"], None}:
                        raise ValueError("Bound review owner is immutable")
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.purchase_invitation_id:
            raise ValueError("Bound purchase review is retained for audit")
        return super().delete(*args, **kwargs)

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


class ReviewRewardUplift(models.Model):
    """Append-only +5 component; the original UGC grant remains unchanged."""

    reward = models.OneToOneField("management.IgUgcReward", on_delete=models.PROTECT,
                                  db_constraint=False, related_name="review_uplift")
    review = models.OneToOneField(Review, on_delete=models.PROTECT, db_constraint=False,
                                  related_name="reward_uplift")
    added_percent = models.PositiveSmallIntegerField(default=5)
    proof_snapshot = models.JSONField(default=dict)
    proof_digest = models.CharField(max_length=64)
    signing_key_id = models.CharField(max_length=24)
    proof_signature = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    objects = ImmutableReviewProofQuerySet.as_manager()

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(added_percent=5), name="review_uplift_five_only")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values().first()
            if previous and any(previous[f.attname] != getattr(self, f.attname) for f in self._meta.concrete_fields):
                raise ValueError("Review reward uplift is immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Review reward uplift is retained for audit")
