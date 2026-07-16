from django.contrib import admin, messages
from modeltranslation.admin import TabbedTranslationAdmin
from .models import (
    AnalyticsExclusion,
    BlogCategory,
    BlogMediaAsset,
    BlogPost,
    BlogPostBlock,
    BlogPromoCampaign,
    BlogPromoClaim,
    BlogPostView,
    CatalogColorSeoOverride,
    Category,
    CategoryColorLanding,
    CategorySeoBlock,
    CategorySeoBlockItem,
    CustomPrintLead,
    CustomPrintLeadAttachment,
    PrintProposal,
    Product,
    ProductFitOption,
    ProductImage,
    PushNotificationCampaign,
    PushNotificationDelivery,
    RestockSubscription,
    SurveySession,
    UserPromoCode,
    WebPushDeviceSubscription,
)


@admin.register(RestockSubscription)
class RestockSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        'product', 'size', 'channel', 'status', 'name',
        'normalized_contact', 'notification_attempts', 'next_attempt_at',
        'created_at', 'admin_notified_at', 'customer_notified_at',
    )
    list_filter = ('channel', 'status', 'created_at')
    search_fields = (
        'product__title', 'name', 'contact', 'normalized_contact',
        'telegram_username', 'size',
    )
    readonly_fields = (
        'fingerprint', 'browser_session_key', 'request_ip_hash', 'user_agent',
        'created_at', 'updated_at', 'admin_notified_at', 'customer_notified_at',
        'notification_attempts', 'last_attempt_at', 'delivery_token',
    )
    actions = ('retry_notifications', 'close_subscriptions', 'reopen_subscriptions')

    def _action_message(self, request, text, level=messages.SUCCESS):
        if hasattr(request, '_messages'):
            self.message_user(request, text, level=level)

    def _eligible_automatic_notifications(self, queryset):
        from django.db.models import Q

        return queryset.exclude(
            status__in=(
                RestockSubscription.Status.DRAFT,
                RestockSubscription.Status.SENDING,
            )
        ).filter(
            Q(channel=RestockSubscription.Channel.EMAIL)
            | Q(
                channel=RestockSubscription.Channel.TELEGRAM,
                telegram_chat_id__isnull=False,
            )
        )

    @admin.action(description='Поставити автоматичне повідомлення в чергу')
    def retry_notifications(self, request, queryset):
        from django.utils import timezone

        selected = queryset.count()
        queued = self._eligible_automatic_notifications(queryset).update(
            status=RestockSubscription.Status.ACTIVE,
            next_attempt_at=timezone.now(),
            delivery_token=None,
            last_error='',
            notification_attempts=0,
        )
        self._action_message(
            request,
            f'Поставлено в чергу: {queued}; пропущено: {selected - queued}.',
        )

    @admin.action(description='Закрити вибрані заявки')
    def close_subscriptions(self, request, queryset):
        closed = queryset.exclude(
            status=RestockSubscription.Status.SENDING,
        ).update(
            status=RestockSubscription.Status.CLOSED,
            next_attempt_at=None,
            delivery_token=None,
        )
        self._action_message(request, f'Закрито заявок: {closed}.')

    @admin.action(description='Повторно відкрити вибрані заявки')
    def reopen_subscriptions(self, request, queryset):
        from django.utils import timezone

        selected = queryset.count()
        eligible = queryset.exclude(status__in=(
            RestockSubscription.Status.DRAFT,
            RestockSubscription.Status.SENDING,
        ))
        automatic = self._eligible_automatic_notifications(queryset).update(
            status=RestockSubscription.Status.ACTIVE,
            next_attempt_at=timezone.now(),
            delivery_token=None,
            last_error='',
            notification_attempts=0,
        )
        manual = eligible.filter(channel__in=(
            RestockSubscription.Channel.PHONE,
            RestockSubscription.Channel.WHATSAPP,
        )).update(
            status=RestockSubscription.Status.ACTIVE,
            next_attempt_at=None,
            delivery_token=None,
            last_error='',
            notification_attempts=0,
        )
        reopened = automatic + manual
        self._action_message(
            request,
            f'Повторно відкрито: {reopened}; пропущено: {selected - reopened}.',
        )
from .services.indexnow import (
    get_category_public_url,
    get_product_public_url,
    is_indexnow_configured,
    submit_indexnow_urls,
)


def _run_indexnow_action(modeladmin, request, queryset, url_resolver, label):
    """Shared body for IndexNow admin actions on Product / Category querysets."""
    if not is_indexnow_configured():
        modeladmin.message_user(
            request,
            "IndexNow не сконфігуровано: задайте INDEXNOW_KEY у settings.",
            level=messages.ERROR,
        )
        return

    urls = [u for u in (url_resolver(obj) for obj in queryset) if u]
    urls = list(dict.fromkeys(urls))  # dedupe, preserve order
    if not urls:
        modeladmin.message_user(
            request,
            f"Жодного публічного URL серед вибраних {label}: можливо, вони неактивні.",
            level=messages.WARNING,
        )
        return

    ok = submit_indexnow_urls(urls)
    if ok:
        modeladmin.message_user(
            request,
            f"IndexNow прийняв {len(urls)} URL(-ів) для {label}.",
            level=messages.SUCCESS,
        )
    else:
        modeladmin.message_user(
            request,
            f"IndexNow повернув помилку для {label}; деталі — у логах.",
            level=messages.ERROR,
        )


@admin.action(description="Надіслати в IndexNow (поточні URL)")
def submit_products_to_indexnow(modeladmin, request, queryset):
    _run_indexnow_action(modeladmin, request, queryset, get_product_public_url, "товарів")


@admin.action(description="Надіслати в IndexNow (поточні URL)")
def submit_categories_to_indexnow(modeladmin, request, queryset):
    _run_indexnow_action(modeladmin, request, queryset, get_category_public_url, "категорій")


@admin.register(Category)
class CategoryAdmin(TabbedTranslationAdmin):
    list_display = ('name', 'slug', 'order', 'is_active', 'ai_generation_enabled', 'ai_content_generated')
    list_filter = ('is_active', 'is_featured', 'ai_generation_enabled', 'ai_content_generated')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ('created_at', 'updated_at', 'ai_content_generated')
    actions = (submit_categories_to_indexnow,)
    fieldsets = (
        ('Основне', {
            'fields': ('name', 'slug', 'description', 'seo_text_title', 'seo_intro_html', 'order', 'is_active', 'is_featured', 'icon', 'cover'),
        }),
        # Phase 21 (2026-05-10) — manual SEO overrides per category.
        # Empty values fall through to the existing boilerplate, so
        # adding this section is safe for unedited categories.
        ('SEO ручні поля', {
            'fields': ('seo_title', 'seo_h1', 'seo_description'),
            'description': (
                'Кастомні <title>, H1 та meta description для сторінки '
                'категорії. Якщо порожньо — використовується автогенерований '
                'fallback («{Назва} - TwoComms» тощо).'
            ),
        }),
        ('Showcase-картка /catalog/', {
            'fields': ('showcase_swatch_hexes',),
            'description': (
                'JSON-список hex-кольорів для свотчів на головній картці '
                'каталогу (наприклад ["#000000","#fafafa"]). Якщо порожньо — '
                'свотчі обчислюються автоматично з кольорів товарів категорії.'
            ),
            'classes': ('collapse',),
        }),
        ('AI-генерація SEO', {
            'fields': ('ai_generation_enabled', 'ai_keywords', 'ai_description', 'ai_content_generated'),
            'description': (
                'AI використовується ЛИШЕ коли поставлено галочку '
                '«Дозволити AI-генерацію SEO». За замовчуванням мета-теги '
                'формуються з ручних SEO-полів та fallback.'
            ),
            'classes': ('collapse',),
        }),
        ('Службове', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )


@admin.register(BlogCategory)
class BlogCategoryAdmin(TabbedTranslationAdmin):
    list_display = ("name", "slug", "order", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        ("Основне", {
            "fields": ("name", "slug", "description", "order", "is_active"),
        }),
        ("SEO", {
            "fields": ("seo_title", "seo_h1", "seo_description", "bottom_title", "bottom_text"),
        }),
        ("Службове", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )


class BlogPostBlockInline(admin.TabularInline):
    model = BlogPostBlock
    extra = 0
    fields = ("block_type", "sort_order", "is_enabled", "payload")
    ordering = ("sort_order", "id")


@admin.register(BlogPost)
class BlogPostAdmin(TabbedTranslationAdmin):
    list_display = ("title", "category", "is_published", "published_at", "view_count", "unique_view_count")
    list_filter = ("is_published", "category", "published_at")
    search_fields = ("title", "slug", "excerpt", "seo_keywords")
    prepopulated_fields = {"slug": ("title",)}
    readonly_fields = ("created_at", "updated_at", "view_count", "unique_view_count")
    inlines = (BlogPostBlockInline,)
    fieldsets = (
        ("Основне", {
            "fields": ("category", "title", "slug", "excerpt", "content_html", "cover_image", "cover_alt", "cover_caption", "source_url"),
        }),
        ("CTA / перелінковка", {
            "fields": ("cta_label", "cta_url", "cta_text"),
        }),
        ("Публікація", {
            "fields": ("is_published", "published_at"),
        }),
        ("SEO", {
            "fields": ("seo_title", "seo_description", "seo_keywords"),
        }),
        ("Аналітика", {
            "fields": ("view_count", "unique_view_count"),
            "classes": ("collapse",),
        }),
        ("Службове", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )


@admin.register(BlogMediaAsset)
class BlogMediaAssetAdmin(admin.ModelAdmin):
    list_display = ("id", "file", "width", "height", "created_at")
    search_fields = ("file",)
    readonly_fields = ("width", "height", "created_at", "updated_at")


@admin.register(BlogPromoCampaign)
class BlogPromoCampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "code_prefix", "discount_type", "discount_value", "max_claims", "is_active", "created_at")
    list_filter = ("is_active", "discount_type")
    search_fields = ("name", "code_prefix")


@admin.register(BlogPromoClaim)
class BlogPromoClaimAdmin(admin.ModelAdmin):
    list_display = ("campaign", "post", "block", "user", "promo_code", "created_at")
    list_filter = ("campaign", "created_at")
    search_fields = ("campaign__name", "post__title", "user__username", "promo_code__code")
    readonly_fields = ("campaign", "post", "block", "user", "promo_code", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(BlogPostView)
class BlogPostViewAdmin(admin.ModelAdmin):
    list_display = ("post", "visitor_key", "user", "views", "last_seen")
    list_filter = ("last_seen",)
    search_fields = ("post__title", "visitor_key", "user_agent", "path")
    readonly_fields = ("post", "visitor_key", "user", "ip_address", "user_agent", "path", "views", "first_seen", "last_seen")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Product)
class ProductAdmin(TabbedTranslationAdmin):
    list_display = (
        'title', 'category', 'price', 'discount_percent', 'points_reward',
        'featured', 'status', 'fit_selector_enabled', 'ai_generation_enabled',
    )
    list_filter = ('category', 'featured', 'status', 'fit_selector_enabled', 'ai_generation_enabled')
    search_fields = ('title', 'slug', 'seo_title', 'seo_keywords')
    prepopulated_fields = {'slug': ('title',)}
    readonly_fields = ('created_at', 'updated_at', 'ai_content_generated', 'published_at', 'last_reviewed_at')
    actions = (submit_products_to_indexnow,)
    fieldsets = (
        ('Основне', {
            'fields': (
                'title', 'slug', 'category', 'catalog', 'size_grid',
                'price', 'discount_percent', 'featured', 'priority',
                'main_image', 'main_image_alt', 'home_card_image',
            ),
        }),
        ('Описи', {
            'fields': ('short_description', 'full_description', 'description', 'details_text', 'target_audience', 'care_instructions'),
            'classes': ('collapse',),
        }),
        ('SEO (ручне керування)', {
            'fields': ('seo_title', 'seo_description', 'seo_keywords', 'search_keywords', 'seo_bottom_html', 'seo_schema'),
            'description': (
                'Заповніть ці поля, щоб повністю контролювати мета-теги і Schema. '
                'AI ніколи не перезаписує ці поля автоматично.'
            ),
        }),
        ('AI-генерація SEO', {
            'fields': ('ai_generation_enabled', 'ai_keywords', 'ai_description', 'ai_content_generated'),
            'description': (
                'AI використовується тільки якщо поставлено галочку '
                '«Дозволити AI-генерацію SEO». Після цього команда '
                '`generate_ai_content` згенерує ai_keywords/ai_description, '
                'які використовуються як fallback, коли ручні SEO-поля порожні.'
            ),
            'classes': ('collapse',),
        }),
        ('UX товару', {
            'fields': ('fit_selector_enabled', 'recommendation_tags'),
        }),
        ('Дропшип', {
            'fields': (
                'is_dropship_available', 'drop_price', 'recommended_price',
                'wholesale_price', 'dropship_note',
            ),
            'classes': ('collapse',),
        }),
        ('Публікація', {
            'fields': ('status', 'published_at', 'last_reviewed_at', 'unpublished_reason', 'points_reward'),
            'classes': ('collapse',),
        }),
        ('Службове', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ('product', 'image')


@admin.register(ProductFitOption)
class ProductFitOptionAdmin(admin.ModelAdmin):
    list_display = ('product', 'code', 'label', 'order', 'is_default', 'is_active')
    list_filter = ('is_active', 'is_default')
    search_fields = ('product__title', 'code', 'label')


@admin.register(PrintProposal)
class PrintProposalAdmin(admin.ModelAdmin):
    list_display = ('user', 'status', 'awarded_points', 'awarded_promocode', 'created_at')
    list_filter = ('status',)
    search_fields = ('user__username', 'description', 'link_url')


@admin.register(SurveySession)
class SurveySessionAdmin(admin.ModelAdmin):
    list_display = ('user', 'survey_key', 'status', 'started_at', 'last_activity_at')
    list_filter = ('status', 'survey_key')
    search_fields = ('user__username', 'survey_key')


@admin.register(UserPromoCode)
class UserPromoCodeAdmin(admin.ModelAdmin):
    list_display = ('user', 'promo_code', 'survey_key', 'expires_at', 'created_at')
    list_filter = ('survey_key',)
    search_fields = ('user__username', 'promo_code__code', 'survey_key')


class CustomPrintLeadAttachmentInline(admin.TabularInline):
    model = CustomPrintLeadAttachment
    extra = 0


@admin.register(CustomPrintLead)
class CustomPrintLeadAdmin(admin.ModelAdmin):
    list_display = (
        'lead_number',
        'name',
        'service_kind',
        'product_type',
        'contact_channel',
        'status',
        'created_at',
    )
    list_filter = ('service_kind', 'product_type', 'contact_channel', 'status', 'client_kind')
    search_fields = ('lead_number', 'name', 'contact_value', 'brand_name', 'brief')
    readonly_fields = ('lead_number', 'created_at', 'updated_at')
    inlines = [CustomPrintLeadAttachmentInline]


@admin.register(WebPushDeviceSubscription)
class WebPushDeviceSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('endpoint', 'user', 'device_type', 'browser_family', 'is_active', 'last_seen_at')
    list_filter = ('is_active', 'device_type', 'browser_family', 'operating_system')
    search_fields = ('endpoint', 'installation_id', 'user__username')
    readonly_fields = ('subscribed_at', 'updated_at', 'last_seen_at', 'last_success_at', 'last_failure_at')


class PushNotificationDeliveryInline(admin.TabularInline):
    model = PushNotificationDelivery
    extra = 0
    readonly_fields = ('subscription', 'status', 'sent_at', 'displayed_at', 'clicked_at', 'failed_at')
    can_delete = False


@admin.register(PushNotificationCampaign)
class PushNotificationCampaignAdmin(admin.ModelAdmin):
    list_display = ('title', 'status', 'targeted_count', 'sent_count', 'displayed_count', 'clicked_count', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('title', 'body', 'target_url')
    readonly_fields = (
        'created_at',
        'updated_at',
        'sent_started_at',
        'sent_finished_at',
        'targeted_count',
        'sent_count',
        'displayed_count',
        'clicked_count',
        'closed_count',
        'failed_count',
    )
    inlines = [PushNotificationDeliveryInline]


# ===== Phase 10 — Category SEO blocks =====

class CategorySeoBlockItemInline(admin.TabularInline):
    model = CategorySeoBlockItem
    extra = 1
    fields = ('order', 'label', 'url', 'extra')
    ordering = ('order', 'id')


@admin.register(CategorySeoBlock)
class CategorySeoBlockAdmin(admin.ModelAdmin):
    list_display = ('category', 'block_type', 'title', 'is_active', 'order', 'updated_at')
    list_filter = ('block_type', 'is_active', 'category')
    search_fields = ('title', 'category__name', 'category__slug')
    list_editable = ('is_active', 'order')
    ordering = ('category', 'order', 'id')
    inlines = [CategorySeoBlockItemInline]
    fieldsets = (
        ('Основне', {
            'fields': ('category', 'block_type', 'title', 'is_active', 'order'),
        }),
    )


# ===== Phase 19h — colour-aware SEO copy overrides =====

@admin.register(CatalogColorSeoOverride)
class CatalogColorSeoOverrideAdmin(admin.ModelAdmin):
    list_display = ('scope', 'color_slug', 'category', 'h2_short', 'is_active', 'updated_at')
    list_filter = ('scope', 'is_active', 'category')
    search_fields = ('color_slug', 'h2', 'body_html', 'category__slug', 'category__name')
    list_editable = ('is_active',)
    autocomplete_fields = ('category',)
    fieldsets = (
        ('Контекст', {
            'fields': ('scope', 'color_slug', 'category', 'is_active'),
            'description': (
                'Виберіть, де саме застосувати цей override. '
                '«general» — для /catalog/ без фільтра кольору (color_slug і '
                'категорію залиште порожніми). «brand» — для /catalog/?color=… '
                '(заповніть color_slug, категорію залиште порожньою). '
                '«category» — для /catalog/&lt;cat&gt;/?color=… (заповніть і '
                'color_slug, і категорію).'
            ),
        }),
        ('Контент', {
            'fields': ('h2', 'body_html', 'queries_json'),
            'description': (
                'Залиште поле порожнім, щоб використати курований варіант з '
                'коду. Заповніть, щоб override-ити лише цю частину блоку.'
            ),
        }),
    )

    def h2_short(self, obj):  # pragma: no cover - admin display
        text = (obj.h2 or '').strip()
        return (text[:60] + '…') if len(text) > 60 else (text or '—')
    h2_short.short_description = 'H2'


@admin.register(AnalyticsExclusion)
class AnalyticsExclusionAdmin(admin.ModelAdmin):
    list_display = ('kind', 'value', 'note', 'is_active', 'updated_at')
    list_filter = ('kind', 'is_active')
    list_editable = ('is_active',)
    search_fields = ('value', 'note')
    readonly_fields = ('created_at', 'updated_at', 'created_by')
    fieldsets = (
        (None, {
            'fields': ('kind', 'value', 'note', 'is_active'),
            'description': (
                'Виключення трафіку зі статистики. IP підтримує CIDR (10.0.0.0/24). '
                'User-Agent зрівнюється як підрядок (icontains). Шляхи — як префікс. '
                'Зміни застосовуються миттєво (кеш скидається сигналом).'
            ),
        }),
        ('Метадані', {
            'classes': ('collapse',),
            'fields': ('created_at', 'updated_at', 'created_by'),
        }),
    )

    def save_model(self, request, obj, form, change):  # pragma: no cover
        if not obj.pk and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)



# ===== Color × Category landing pages =====
@admin.register(CategoryColorLanding)
class CategoryColorLandingAdmin(admin.ModelAdmin):
    """Admin for indexable colour×category landing pages."""

    list_display = (
        "category",
        "color_slug",
        "seo_title_short",
        "is_published",
        "editorial_length",
        "order",
        "updated_at",
    )
    list_filter = ("is_published", "category")
    list_editable = ("is_published", "order")
    search_fields = (
        "color_slug",
        "seo_title",
        "seo_h1",
        "seo_description",
        "editorial_html",
        "category__name",
        "category__slug",
        "color__name",
    )
    autocomplete_fields = ("category",)
    raw_id_fields = ("color",)
    readonly_fields = ("created_at", "updated_at", "editorial_length")
    fieldsets = (
        ("Контекст", {
            "fields": ("category", "color", "color_slug", "is_published", "order"),
            "description": (
                "URL формується як <code>/catalog/&lt;cat&gt;/&lt;color_slug&gt;/</code>. "
                "<b>color_slug</b> заповниться автоматично з імені кольору, але "
                "ви можете задати кастомне значення вручну."
            ),
        }),
        ("SEO meta", {
            "fields": ("seo_title", "seo_h1", "seo_description"),
        }),
        ("Контент", {
            "fields": ("editorial_html", "editorial_length", "faq_items"),
            "description": (
                "Editorial copy має містити мінімум 800 символів для публікації "
                "(анти-thin-content guard). FAQ — список словників з полями "
                "<code>question</code> і <code>answer</code>."
            ),
        }),
        ("Службове", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def seo_title_short(self, obj):  # pragma: no cover - admin display
        text = (obj.seo_title or "").strip()
        return (text[:60] + "…") if len(text) > 60 else (text or "—")
    seo_title_short.short_description = "SEO Title"

    def editorial_length(self, obj):  # pragma: no cover - admin display
        return len(obj.editorial_html or "")
    editorial_length.short_description = "Довжина editorial (символів)"
