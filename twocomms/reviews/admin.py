"""Admin for the moderation queue.

Workflow:
    1. Reviews land with ``status='pending'``.
    2. Moderators land on the changelist filtered to ``status=pending``
       (default via list_filter); see them in chronological order.
    3. Bulk actions ``approve_selected`` / ``reject_selected`` flip the
       state and stamp ``moderated_by`` + ``moderated_at``.
    4. ``moderation_note`` is editable on the change form for
       institutional memory.
"""

from __future__ import annotations

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from .models import Review, ReviewImage, ReviewStatus, ReviewVote
from .write_freeze import review_writes_frozen


class ReviewWriteFreezeAdminMixin:
    def has_add_permission(self, request):
        return False if review_writes_frozen() else super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return False if review_writes_frozen() else super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False if review_writes_frozen() else super().has_delete_permission(request, obj)

    def get_actions(self, request):
        return {} if review_writes_frozen() else super().get_actions(request)

    @staticmethod
    def _require_writable():
        if review_writes_frozen():
            raise PermissionDenied("Review writes are temporarily frozen.")

    def save_model(self, request, obj, form, change):
        self._require_writable()
        return super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        self._require_writable()
        return super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        self._require_writable()
        return super().delete_queryset(request, queryset)


class ReviewImageInline(ReviewWriteFreezeAdminMixin, admin.TabularInline):
    model = ReviewImage
    extra = 0
    fields = ("image", "order")


@admin.register(Review)
class ReviewAdmin(ReviewWriteFreezeAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "product",
        "rating_stars",
        "author_name",
        "status",
        "is_verified_purchase",
        "helpful_count",
        "created_at",
    )
    list_filter = ("status", "is_verified_purchase", "rating", "created_at")
    list_select_related = ("product", "user")
    search_fields = (
        "id", "title", "body", "author_name", "email",
        "product__title", "product__slug",
    )
    autocomplete_fields = ("product", "user", "moderated_by")
    readonly_fields = (
        "created_at", "updated_at", "moderated_at",
        "helpful_count", "unhelpful_count", "is_verified_purchase",
    )
    inlines = [ReviewImageInline]
    actions = ("approve_selected", "reject_selected")
    fieldsets = (
        ("Контент", {
            "fields": (
                "product", "kind", "rating", "title", "body", "city", "pros", "cons",
                ("author_name", "user"),
                ("email", "anon_key"),
            ),
        }),
        ("Модерація", {
            "fields": (
                "status", "moderation_note",
                ("moderated_by", "moderated_at"),
            ),
            "description": (
                "Перед публікацією переконайтеся, що відгук осмислений, "
                "не містить персональних даних і не порушує політику. "
                "Bulk-actions зверху роблять те саме за один клік."
            ),
        }),
        ("Метрики", {
            "fields": (
                "is_verified_purchase",
                ("helpful_count", "unhelpful_count"),
                ("created_at", "updated_at"),
            ),
            "classes": ("collapse",),
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj is not None and obj.purchase_invitation_id:
            fields = (*fields, "product", "kind", "rating", "title", "body", "city", "pros", "cons",
                      "author_name", "user", "email", "anon_key", "moderated_by")
        return fields

    def save_model(self, request, obj, form, change):
        # Direct status edits need the same genuine staff moderation receipt as
        # bulk actions. Keep ordinary review editing behavior unchanged.
        if change and "status" in form.changed_data:
            obj.moderated_by = request.user
            obj.moderated_at = timezone.now()
        return super().save_model(request, obj, form, change)

    @admin.display(description="Оцінка", ordering="rating")
    def rating_stars(self, obj: Review) -> str:
        if obj.rating is None:
            return "Коментар"
        full = "★" * int(obj.rating)
        empty = "☆" * (5 - int(obj.rating))
        return f"{full}{empty}"

    @admin.action(description="Опублікувати вибрані відгуки")
    def approve_selected(self, request, queryset):
        self._require_writable()
        updated = 0
        for review in queryset.exclude(status=ReviewStatus.APPROVED):
            review.mark_approved(by=request.user)
            updated += 1
        self.message_user(request, f"Опубліковано: {updated}.")

    @admin.action(description="Відхилити вибрані відгуки")
    def reject_selected(self, request, queryset):
        self._require_writable()
        updated = 0
        for review in queryset.exclude(status=ReviewStatus.REJECTED):
            review.mark_rejected(by=request.user)
            updated += 1
        self.message_user(request, f"Відхилено: {updated}.")


@admin.register(ReviewVote)
class ReviewVoteAdmin(ReviewWriteFreezeAdminMixin, admin.ModelAdmin):
    list_display = ("id", "review", "user", "anon_key", "value", "created_at")
    list_filter = ("value", "created_at")
    search_fields = ("review__id", "user__username", "anon_key")
    autocomplete_fields = ("review", "user")
    readonly_fields = ("created_at",)
