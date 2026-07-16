"""
Fable 5 — розширені моделі для нового уніфікованого редактора товарів.

ВАЖЛИВО: цей застосунок НЕ змінює жодної існуючої таблиці storefront /
productcolors. Тільки нові таблиці з FK/OneToOne на існуючі моделі, тому
старий редактор і сайт продовжують працювати як раніше (старе = бекап).
"""
from django.conf import settings
from django.db import models

from productcolors.models import Color, ProductColorImage, ProductColorVariant
from storefront.models import Category, Product, ProductImage, SizeGrid


LANGUAGE_CHOICES = (("uk", "Українська"), ("ru", "Російська"), ("en", "English"))


class ColorProfile(models.Model):
    """Розширення довідника кольорів: термохром + опис тканини.

    Колір існує один раз у бібліотеці (name + hex + прапорець «термо»),
    без подвійного вводу назви, як було у старій панелі.
    """

    color = models.OneToOneField(
        Color,
        on_delete=models.CASCADE,
        related_name="fable5_profile",
        db_constraint=False,
    )
    is_thermo = models.BooleanField(default=False, verbose_name="Термохромний")
    thermo_note = models.CharField(
        max_length=255, blank=True, default="",
        verbose_name="Коротка примітка",
        help_text="Наприклад: «Реагує на тепло — змінює відтінок». Показується біля кружечка.",
    )
    description = models.TextField(
        blank=True, default="",
        verbose_name="Опис тканини/кольору",
        help_text="SEO-дружній опис (що таке термохромна тканина, чим відрізняється тощо).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fable5: профіль кольору"
        verbose_name_plural = "Fable5: профілі кольорів"

    def __str__(self):
        return f"{self.color}{' (термо)' if self.is_thermo else ''}"


class VariantDetails(models.Model):
    """Кожен колір — «майже окремий товар»: назва, SEO, надбавка до ціни, відео."""

    variant = models.OneToOneField(
        ProductColorVariant,
        on_delete=models.CASCADE,
        related_name="fable5_details",
        db_constraint=False,
    )
    display_name = models.CharField(
        max_length=220, blank=True, default="",
        verbose_name="Назва для цього кольору",
        help_text="Напр.: «Сіра футболка оверсайз \"Бойова квіточка\"». Порожньо — назва товару.",
    )
    price_delta = models.IntegerField(
        default=0, verbose_name="Надбавка до ціни (грн)",
        help_text="Напр. +300 за термохромну тканину. Може бути відʼємною.",
    )
    price_delta_reason = models.CharField(
        max_length=255, blank=True, default="",
        verbose_name="Причина надбавки",
        help_text="Показується покупцю: «Для цього кольору ціна +300 грн — термохромна тканина».",
    )
    marketing_html = models.TextField(
        blank=True, default="", verbose_name="Маркетинговий опис (HTML)",
        help_text="Красивий блок про цю тканину/колір у картці товару.",
    )
    youtube_url = models.URLField(
        max_length=500, blank=True, default="",
        verbose_name="YouTube для кольору",
        help_text="Порожньо — використовується спільне відео товару (Product.video_url).",
    )
    seo_title = models.CharField(max_length=180, blank=True, default="", verbose_name="SEO Title")
    seo_description = models.CharField(max_length=320, blank=True, default="", verbose_name="SEO Description")
    seo_keywords = models.CharField(max_length=300, blank=True, default="", verbose_name="SEO Keywords")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fable5: деталі кольору товару"
        verbose_name_plural = "Fable5: деталі кольорів товарів"

    def __str__(self):
        return f"Details for variant #{self.variant_id}"


class ProductFitNote(models.Model):
    """Доступність посадки (класика/оверсайз) на рівні товару + причина.

    Доповнює storefront.ProductFitOption: там вкл/викл, тут — текст причини,
    який показується покупцю («Для цієї моделі доступний лише оверсайз»).
    """

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="fable5_fit_notes",
        db_constraint=False,
    )
    fit_code = models.SlugField(max_length=50, verbose_name="Код посадки")
    is_enabled = models.BooleanField(default=True, verbose_name="Доступна")
    reason = models.CharField(
        max_length=255, blank=True, default="",
        verbose_name="Причина (якщо вимкнена)",
        help_text="Пишеться у картці товару під перемикачем посадок.",
    )

    class Meta:
        verbose_name = "Fable5: посадка товару"
        verbose_name_plural = "Fable5: посадки товарів"
        unique_together = (("product", "fit_code"),)

    def __str__(self):
        return f"{self.product_id}:{self.fit_code} ({'on' if self.is_enabled else 'off'})"


class VariantFitRule(models.Model):
    """Доступність посадки для конкретного КОЛЬОРУ (напр., термо — лише оверсайз)."""

    variant = models.ForeignKey(
        ProductColorVariant,
        on_delete=models.CASCADE,
        related_name="fable5_fit_rules",
        db_constraint=False,
    )
    fit_code = models.SlugField(max_length=50, verbose_name="Код посадки")
    is_enabled = models.BooleanField(default=True, verbose_name="Доступна")
    reason = models.CharField(max_length=255, blank=True, default="", verbose_name="Причина (якщо вимкнена)")

    class Meta:
        verbose_name = "Fable5: посадка кольору"
        verbose_name_plural = "Fable5: посадки кольорів"
        unique_together = (("variant", "fit_code"),)

    def __str__(self):
        return f"v{self.variant_id}:{self.fit_code} ({'on' if self.is_enabled else 'off'})"


class VariantSizeRule(models.Model):
    """Розмір у межах кольору: вкл/викл + залишок на складі.

    Приклад: для кольору koyote вимкнути S у конкретній футболці.
    stock = NULL — залишок не відстежується (розмір просто ввімкнений).
    """

    variant = models.ForeignKey(
        ProductColorVariant,
        on_delete=models.CASCADE,
        related_name="fable5_size_rules",
        db_constraint=False,
    )
    fit_code = models.SlugField(
        max_length=50, blank=True, default="",
        verbose_name="Код посадки", help_text="Порожньо — правило для всіх посадок.",
    )
    size = models.CharField(max_length=12, verbose_name="Розмір")
    is_enabled = models.BooleanField(default=True, verbose_name="Доступний")
    stock = models.IntegerField(null=True, blank=True, verbose_name="Залишок, шт")
    note = models.CharField(max_length=255, blank=True, default="", verbose_name="Примітка")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fable5: розмір кольору"
        verbose_name_plural = "Fable5: розміри кольорів"
        unique_together = (("variant", "fit_code", "size"),)
        ordering = ["variant_id", "fit_code", "id"]

    def __str__(self):
        return f"v{self.variant_id} {self.size} ({'on' if self.is_enabled else 'off'})"


class VariantFAQ(models.Model):
    """FAQ для конкретного кольору (окремо від загальних ProductFAQ) у 3 мовах."""

    variant = models.ForeignKey(
        ProductColorVariant,
        on_delete=models.CASCADE,
        related_name="fable5_faqs",
        db_constraint=False,
    )
    question_uk = models.CharField(max_length=255, blank=True, default="", verbose_name="Питання (укр)")
    question_ru = models.CharField(max_length=255, blank=True, default="", verbose_name="Питання (рос)")
    question_en = models.CharField(max_length=255, blank=True, default="", verbose_name="Питання (англ)")
    answer_uk = models.TextField(blank=True, default="", verbose_name="Відповідь (укр)")
    answer_ru = models.TextField(blank=True, default="", verbose_name="Відповідь (рос)")
    answer_en = models.TextField(blank=True, default="", verbose_name="Відповідь (англ)")
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")
    is_active = models.BooleanField(default=True, verbose_name="Активне")

    class Meta:
        verbose_name = "Fable5: FAQ кольору"
        verbose_name_plural = "Fable5: FAQ кольорів"
        ordering = ["order", "id"]

    def __str__(self):
        return self.question_uk or self.question_ru or self.question_en or f"FAQ #{self.pk}"


class FeedProfile(models.Model):
    """Фід (Google Merchant, Meta DS тощо) — «селекція з фід» в адмінці."""

    FEED_TYPES = [
        ("google_merchant", "Google Merchant"),
        ("meta_ds", "Meta / Facebook DS"),
        ("custom", "Інший / кастомний"),
    ]

    name = models.CharField(max_length=160, verbose_name="Назва", help_text="Напр.: «Meta DS фід версія 1»")
    slug = models.SlugField(max_length=160, unique=True, verbose_name="Код фіда")
    feed_type = models.CharField(max_length=30, choices=FEED_TYPES, default="custom", verbose_name="Тип")
    is_active = models.BooleanField(default=True, verbose_name="Активний")
    default_include = models.BooleanField(
        default=False, verbose_name="Включати товари за замовчуванням",
        help_text="Вимкнено — у фід потрапляють лише товари, явно додані у «Селекції з фід».",
    )
    settings = models.JSONField(default=dict, blank=True, verbose_name="Налаштування")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fable5: фід"
        verbose_name_plural = "Fable5: фіди"
        ordering = ["name"]

    def __str__(self):
        return self.name


class FeedProductRule(models.Model):
    """Участь товару у фіді + кастомні тайтл/опис саме для цього фіда."""

    feed = models.ForeignKey(FeedProfile, on_delete=models.CASCADE, related_name="product_rules")
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="fable5_feed_rules",
        db_constraint=False,
    )
    is_included = models.BooleanField(default=True, verbose_name="Товар у фіді")
    custom_title = models.CharField(max_length=220, blank=True, default="", verbose_name="Тайтл для фіда")
    custom_description = models.TextField(blank=True, default="", verbose_name="Опис для фіда")
    note = models.CharField(max_length=255, blank=True, default="", verbose_name="Примітка")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fable5: товар у фіді"
        verbose_name_plural = "Fable5: товари у фідах"
        unique_together = (("feed", "product"),)

    def __str__(self):
        return f"{self.feed_id}:{self.product_id} ({'in' if self.is_included else 'out'})"


class FeedImageRule(models.Model):
    """Дозвіл/заборона конкретної картинки товару у конкретному фіді.

    Якщо для (feed, product) немає жодного дозволеного правила — фід бере
    картинки як зазвичай. Якщо є хоча б одне is_allowed=True — у фід ідуть
    ТІЛЬКИ дозволені (у вказаному порядку) + FeedOnlyImage.
    """

    feed = models.ForeignKey(FeedProfile, on_delete=models.CASCADE, related_name="image_rules")
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="fable5_feed_image_rules",
        db_constraint=False,
    )
    product_image = models.ForeignKey(
        ProductImage, on_delete=models.CASCADE, null=True, blank=True,
        related_name="fable5_feed_rules", db_constraint=False,
    )
    color_image = models.ForeignKey(
        ProductColorImage, on_delete=models.CASCADE, null=True, blank=True,
        related_name="fable5_feed_rules", db_constraint=False,
    )
    use_main_image = models.BooleanField(default=False, verbose_name="Головне зображення товару")
    is_allowed = models.BooleanField(default=True, verbose_name="Дозволено у фіді")
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок у фіді")

    class Meta:
        verbose_name = "Fable5: картинка у фіді"
        verbose_name_plural = "Fable5: картинки у фідах"
        ordering = ["order", "id"]

    def __str__(self):
        return f"feed{self.feed_id}:p{self.product_id}:img ({'allow' if self.is_allowed else 'deny'})"


class FeedOnlyImage(models.Model):
    """Картинка, прикріплена ТІЛЬКИ до фіда — не показується у картці товару."""

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="fable5_feed_only_images",
        db_constraint=False,
    )
    feed = models.ForeignKey(
        FeedProfile, on_delete=models.CASCADE, null=True, blank=True, related_name="extra_images",
        verbose_name="Фід", help_text="Порожньо — доступна будь-якому фіду.",
    )
    image = models.ImageField(upload_to="fable5/feed_images/", verbose_name="Зображення")
    alt = models.CharField(max_length=200, blank=True, default="", verbose_name="Alt-текст")
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Fable5: фід-картинка"
        verbose_name_plural = "Fable5: фід-картинки"
        ordering = ["order", "id"]

    def __str__(self):
        return f"Feed-only image for product {self.product_id}"


# ---------------------------------------------------------------------------
# Fable 5 v2: sparse, localised merchandising overrides
# ---------------------------------------------------------------------------


class LocalizedMerchandisingContent(models.Model):
    """Shared fields for concrete language rows at each inheritance layer."""

    lang = models.CharField(max_length=2, choices=LANGUAGE_CHOICES)
    display_name = models.CharField(max_length=220, blank=True, default="")
    short_description = models.TextField(blank=True, default="")
    full_description = models.TextField(blank=True, default="")
    marketing_text = models.TextField(blank=True, default="")
    seo_title = models.CharField(max_length=180, blank=True, default="")
    seo_description = models.CharField(max_length=320, blank=True, default="")
    seo_keywords = models.CharField(max_length=300, blank=True, default="")
    og_title = models.CharField(max_length=180, blank=True, default="")
    og_description = models.CharField(max_length=320, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class VariantDetailsI18n(LocalizedMerchandisingContent):
    details = models.ForeignKey(
        VariantDetails,
        on_delete=models.CASCADE,
        related_name="i18n",
    )

    class Meta:
        verbose_name = "Fable5: локалізація кольору"
        verbose_name_plural = "Fable5: локалізації кольорів"
        constraints = [
            models.UniqueConstraint(
                fields=("details", "lang"),
                name="f5_unique_variant_details_lang",
            )
        ]

    def __str__(self):
        return f"details:{self.details_id}:{self.lang}"


class ProductOptionProfile(models.Model):
    """Product-wide override for an option, e.g. fit=oversize."""

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="fable5_option_profiles",
        db_constraint=False,
    )
    option_key = models.CharField(max_length=160)
    option_values = models.JSONField(default=dict, blank=True)
    price_delta = models.IntegerField(null=True, blank=True, default=None)
    price_delta_reason = models.CharField(max_length=255, blank=True, default="")
    youtube_url = models.URLField(max_length=500, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("product_id", "option_key")
        constraints = [
            models.UniqueConstraint(
                fields=("product", "option_key"),
                name="f5_unique_product_option",
            )
        ]

    def __str__(self):
        return f"p{self.product_id}:{self.option_key}"


class ProductOptionAxisPresentation(models.Model):
    class Presentation(models.TextChoices):
        AUTO = "auto", "Автоматично"
        SWITCH = "switch", "Компактний switch"
        CARDS = "cards", "Картки"

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="fable5_axis_presentations",
        db_constraint=False,
    )
    axis_code = models.SlugField(max_length=50)
    presentation = models.CharField(
        max_length=12,
        choices=Presentation.choices,
        default=Presentation.AUTO,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("product_id", "axis_code")
        constraints = [
            models.UniqueConstraint(
                fields=("product", "axis_code"),
                name="f5_unique_product_axis_presentation",
            )
        ]

    def __str__(self):
        return f"p{self.product_id}:{self.axis_code}:{self.presentation}"


class ProductOptionProfileI18n(LocalizedMerchandisingContent):
    profile = models.ForeignKey(
        ProductOptionProfile,
        on_delete=models.CASCADE,
        related_name="i18n",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("profile", "lang"),
                name="f5_unique_product_option_lang",
            )
        ]

    def __str__(self):
        return f"option:{self.profile_id}:{self.lang}"


class VariantCombinationProfile(models.Model):
    """Exact color × option combination override."""

    variant = models.ForeignKey(
        ProductColorVariant,
        on_delete=models.CASCADE,
        related_name="fable5_combinations",
        db_constraint=False,
    )
    combination_key = models.CharField(max_length=240)
    option_values = models.JSONField(default=dict, blank=True)
    price_delta = models.IntegerField(null=True, blank=True, default=None)
    price_delta_reason = models.CharField(max_length=255, blank=True, default="")
    youtube_url = models.URLField(max_length=500, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("variant_id", "combination_key")
        constraints = [
            models.UniqueConstraint(
                fields=("variant", "combination_key"),
                name="f5_unique_variant_combination",
            )
        ]

    def __str__(self):
        return f"v{self.variant_id}:{self.combination_key}"


class VariantCombinationProfileI18n(LocalizedMerchandisingContent):
    profile = models.ForeignKey(
        VariantCombinationProfile,
        on_delete=models.CASCADE,
        related_name="i18n",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("profile", "lang"),
                name="f5_unique_variant_combo_lang",
            )
        ]

    def __str__(self):
        return f"combination:{self.profile_id}:{self.lang}"


class VariantImageAltI18n(models.Model):
    color_image = models.ForeignKey(
        ProductColorImage,
        on_delete=models.CASCADE,
        related_name="fable5_alts",
        db_constraint=False,
    )
    lang = models.CharField(max_length=2, choices=LANGUAGE_CHOICES)
    alt = models.CharField(max_length=220, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("color_image", "lang"),
                name="f5_unique_color_image_alt_lang",
            )
        ]


class ProductImageAltI18n(models.Model):
    product_image = models.ForeignKey(
        ProductImage,
        on_delete=models.CASCADE,
        related_name="fable5_alts",
        db_constraint=False,
    )
    lang = models.CharField(max_length=2, choices=LANGUAGE_CHOICES)
    alt = models.CharField(max_length=220, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("product_image", "lang"),
                name="f5_unique_product_image_alt_lang",
            )
        ]


# ---------------------------------------------------------------------------
# Garment flows and product-scoped print compatibility
# ---------------------------------------------------------------------------


class GarmentFlow(models.Model):
    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    axes = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    categories = models.ManyToManyField(
        Category,
        through="GarmentFlowCategory",
        related_name="fable5_flows",
        blank=True,
    )

    class Meta:
        ordering = ("name", "code")

    def __str__(self):
        return self.name


class GarmentFlowCategory(models.Model):
    flow = models.ForeignKey(
        GarmentFlow,
        on_delete=models.CASCADE,
        related_name="category_links",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="fable5_flow_links",
        db_constraint=False,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("flow", "category"),
                name="f5_unique_flow_category",
            )
        ]


class ProductPrintLink(models.Model):
    product = models.OneToOneField(
        Product,
        on_delete=models.CASCADE,
        related_name="fable5_print_link",
        db_constraint=False,
    )
    print_ref = models.ForeignKey(
        "warehouse.Print",
        on_delete=models.SET_NULL,
        related_name="fable5_product_links",
        null=True,
        blank=True,
        db_constraint=False,
    )
    note = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"p{self.product_id}:print{self.print_ref_id or '-'}"


class ProductPrintCompatibility(models.Model):
    link = models.ForeignKey(
        ProductPrintLink,
        on_delete=models.CASCADE,
        related_name="compatibility",
    )
    combination_key = models.CharField(max_length=240)
    is_allowed = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("link", "combination_key"),
                name="f5_unique_print_link_combo",
            )
        ]


# ---------------------------------------------------------------------------
# Reusable fit-specific size grids and availability
# ---------------------------------------------------------------------------


class SizeGridProfile(models.Model):
    size_grid = models.OneToOneField(
        SizeGrid,
        on_delete=models.CASCADE,
        related_name="fable5_profile",
        db_constraint=False,
    )
    garment_code = models.SlugField(max_length=50, blank=True, default="")
    option_key = models.CharField(max_length=160, blank=True, default="")
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"grid{self.size_grid_id}:{self.option_key or self.garment_code or '-'}"


class ProductOptionSizeGrid(models.Model):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="fable5_size_grid_assignments",
        db_constraint=False,
    )
    option_key = models.CharField(max_length=160)
    size_grid = models.ForeignKey(
        SizeGrid,
        on_delete=models.PROTECT,
        related_name="fable5_product_assignments",
        db_constraint=False,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("product", "option_key"),
                name="f5_unique_product_option_grid",
            )
        ]

    def __str__(self):
        return f"p{self.product_id}:{self.option_key}:grid{self.size_grid_id}"


class VariantOptionSizeGrid(models.Model):
    """Optional per-colour override of a shared product/fit size grid."""

    variant = models.ForeignKey(
        ProductColorVariant,
        on_delete=models.CASCADE,
        related_name="fable5_size_grid_assignments",
        db_constraint=False,
    )
    option_key = models.CharField(max_length=160)
    size_grid = models.ForeignKey(
        SizeGrid,
        on_delete=models.PROTECT,
        related_name="fable5_variant_assignments",
        db_constraint=False,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("variant", "option_key"),
                name="f5_unique_variant_option_grid",
            )
        ]

    def __str__(self):
        return f"v{self.variant_id}:{self.option_key}:grid{self.size_grid_id}"


class VariantBlankLink(models.Model):
    """Warehouse blank family used by a colour/fit during fulfilment."""

    variant = models.ForeignKey(
        ProductColorVariant,
        on_delete=models.CASCADE,
        related_name="fable5_blank_links",
        db_constraint=False,
    )
    option_key = models.CharField(max_length=160)
    storage_subcategory = models.ForeignKey(
        "warehouse.StorageSubcategory",
        on_delete=models.PROTECT,
        related_name="fable5_variant_links",
        db_constraint=False,
    )
    note = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("variant", "option_key"),
                name="f5_unique_variant_blank_link",
            )
        ]

    def __str__(self):
        return f"v{self.variant_id}:{self.option_key}:blank{self.storage_subcategory_id}"


class ProductSizeRule(models.Model):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="fable5_size_rules",
        db_constraint=False,
    )
    option_key = models.CharField(max_length=160)
    size = models.CharField(max_length=20)
    is_enabled = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("product_id", "option_key", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("product", "option_key", "size"),
                name="f5_unique_product_option_size",
            )
        ]

    def __str__(self):
        return f"p{self.product_id}:{self.option_key}:{self.size}"


# ---------------------------------------------------------------------------
# Cover provenance, optimistic concurrency, and drafts
# ---------------------------------------------------------------------------


class CoverSource(models.Model):
    class SourceType(models.TextChoices):
        UPLOAD = "upload", "Завантажено окремо"
        COLOR_IMAGE = "color_image", "З фото кольору"
        PRODUCT_IMAGE = "product_image", "З галереї товару"

    product = models.OneToOneField(
        Product,
        on_delete=models.CASCADE,
        related_name="fable5_cover",
        db_constraint=False,
    )
    source_type = models.CharField(
        max_length=20,
        choices=SourceType.choices,
        default=SourceType.UPLOAD,
    )
    color_image = models.ForeignKey(
        ProductColorImage,
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
        db_constraint=False,
    )
    product_image = models.ForeignKey(
        ProductImage,
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
        db_constraint=False,
    )
    source_missing = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"p{self.product_id}:{self.source_type}"


class ProductEditorState(models.Model):
    product = models.OneToOneField(
        Product,
        on_delete=models.CASCADE,
        related_name="fable5_editor_state",
        db_constraint=False,
    )
    revision = models.PositiveBigIntegerField(default=0)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="fable5_product_edits",
        null=True,
        blank=True,
        db_constraint=False,
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"p{self.product_id}:r{self.revision}"


class EditorDraft(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="fable5_editor_drafts",
        db_constraint=False,
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="fable5_editor_drafts",
        null=True,
        blank=True,
        db_constraint=False,
    )
    draft_key = models.CharField(max_length=64, default="default")
    payload = models.JSONField(default=dict, blank=True)
    product_revision = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "draft_key"),
                name="f5_unique_user_draft_key",
            )
        ]

    def __str__(self):
        return f"user{self.user_id}:{self.draft_key}"
