from django.contrib import admin

from .models import (
    ColorProfile,
    CoverSource,
    EditorDraft,
    FeedImageRule,
    FeedOnlyImage,
    FeedProductRule,
    FeedProfile,
    GarmentFlow,
    GarmentFlowCategory,
    ProductEditorState,
    ProductFitNote,
    ProductImageAltI18n,
    ProductOptionProfile,
    ProductOptionProfileI18n,
    ProductOptionSizeGrid,
    ProductPrintCompatibility,
    ProductPrintLink,
    ProductSizeRule,
    SizeGridProfile,
    VariantCombinationProfile,
    VariantCombinationProfileI18n,
    VariantDetails,
    VariantDetailsI18n,
    VariantFAQ,
    VariantFitRule,
    VariantImageAltI18n,
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


@admin.register(GarmentFlow)
class GarmentFlowAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(ProductOptionProfile)
class ProductOptionProfileAdmin(admin.ModelAdmin):
    list_display = ("product", "option_key", "price_delta", "is_active", "updated_at")
    search_fields = ("product__title", "option_key")


@admin.register(VariantCombinationProfile)
class VariantCombinationProfileAdmin(admin.ModelAdmin):
    list_display = ("variant", "combination_key", "price_delta", "is_active", "updated_at")
    search_fields = ("variant__product__title", "combination_key")


@admin.register(ProductOptionSizeGrid)
class ProductOptionSizeGridAdmin(admin.ModelAdmin):
    list_display = ("product", "option_key", "size_grid", "updated_at")
    search_fields = ("product__title", "option_key", "size_grid__name")


@admin.register(ProductSizeRule)
class ProductSizeRuleAdmin(admin.ModelAdmin):
    list_display = ("product", "option_key", "size", "is_enabled", "updated_at")
    list_filter = ("is_enabled", "size")
    search_fields = ("product__title", "option_key")


@admin.register(ProductPrintLink)
class ProductPrintLinkAdmin(admin.ModelAdmin):
    list_display = ("product", "print_ref", "updated_at")
    search_fields = ("product__title", "print_ref__name")


@admin.register(ProductEditorState)
class ProductEditorStateAdmin(admin.ModelAdmin):
    list_display = ("product", "revision", "updated_by", "updated_at")
    search_fields = ("product__title", "updated_by__username")


admin.site.register(
    [
        VariantDetailsI18n,
        ProductOptionProfileI18n,
        VariantCombinationProfileI18n,
        VariantImageAltI18n,
        ProductImageAltI18n,
        GarmentFlowCategory,
        ProductPrintCompatibility,
        SizeGridProfile,
        CoverSource,
        EditorDraft,
    ]
)
