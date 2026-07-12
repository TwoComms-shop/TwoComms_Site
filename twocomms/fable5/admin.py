from django.contrib import admin

from .models import (
    ColorProfile,
    FeedImageRule,
    FeedOnlyImage,
    FeedProductRule,
    FeedProfile,
    ProductFitNote,
    VariantDetails,
    VariantFAQ,
    VariantFitRule,
    VariantSizeRule,
)


@admin.register(ColorProfile)
class ColorProfileAdmin(admin.ModelAdmin):
    list_display = ("color", "is_thermo", "thermo_note", "updated_at")
    list_filter = ("is_thermo",)
    search_fields = ("color__name", "color__primary_hex")


@admin.register(VariantDetails)
class VariantDetailsAdmin(admin.ModelAdmin):
    list_display = ("variant", "display_name", "price_delta", "updated_at")
    search_fields = ("variant__product__title", "display_name")


@admin.register(ProductFitNote)
class ProductFitNoteAdmin(admin.ModelAdmin):
    list_display = ("product", "fit_code", "is_enabled", "reason")
    list_filter = ("fit_code", "is_enabled")
    search_fields = ("product__title",)


@admin.register(VariantFitRule)
class VariantFitRuleAdmin(admin.ModelAdmin):
    list_display = ("variant", "fit_code", "is_enabled", "reason")
    list_filter = ("fit_code", "is_enabled")


@admin.register(VariantSizeRule)
class VariantSizeRuleAdmin(admin.ModelAdmin):
    list_display = ("variant", "fit_code", "size", "is_enabled", "stock", "updated_at")
    list_filter = ("is_enabled", "size")
    search_fields = ("variant__product__title",)


@admin.register(VariantFAQ)
class VariantFAQAdmin(admin.ModelAdmin):
    list_display = ("variant", "question_uk", "order", "is_active")


@admin.register(FeedProfile)
class FeedProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "feed_type", "is_active", "default_include")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(FeedProductRule)
class FeedProductRuleAdmin(admin.ModelAdmin):
    list_display = ("feed", "product", "is_included", "updated_at")
    list_filter = ("feed", "is_included")
    search_fields = ("product__title",)


@admin.register(FeedImageRule)
class FeedImageRuleAdmin(admin.ModelAdmin):
    list_display = ("feed", "product", "use_main_image", "is_allowed", "order")
    list_filter = ("feed", "is_allowed")


@admin.register(FeedOnlyImage)
class FeedOnlyImageAdmin(admin.ModelAdmin):
    list_display = ("product", "feed", "alt", "order", "created_at")
