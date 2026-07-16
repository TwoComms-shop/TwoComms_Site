import io
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.db import IntegrityError, models, transaction
from django.db.models import F
from django.contrib.auth.models import User
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from PIL import Image, ImageOps


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True)
    icon = models.ImageField(upload_to='category_icons/', blank=True, null=True)
    cover = models.ImageField(upload_to='category_covers/', blank=True, null=True)
    order = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True, null=True)
    # Phase 10 — H2 over the long SEO text. Empty → use category.name.
    seo_text_title = models.CharField(
        max_length=200,
        blank=True,
        verbose_name='SEO-заголовок над описом',
        help_text='H2 над довгим SEO-текстом категорії. Якщо порожньо — використовується назва категорії.',
    )
    # Phase 10b — short HTML intro shown ABOVE the products grid, with
    # an optional "Розгорнути" collapsible. Mirrors the pattern from
    # AAC.com.ua / retromagaz where the H1 is followed by 1–3 sentences
    # of high-frequency keywords + a collapsed "what is X?" block.
    seo_intro_html = models.TextField(
        blank=True,
        verbose_name='SEO-інтро над сіткою товарів',
        help_text=(
            'Короткий HTML-блок під H1 (1–3 речення з основними ключами + '
            'опційно <details>...</details> або <div data-seo-collapsible> '
            'для розгортуваного блоку). Не показується на /catalog/ без '
            'фільтру категорії.'
        ),
    )
    # Phase 19h (2026-05-10) — admin-editable showcase swatches.
    # Optional list of hex strings (e.g. ["#000000","#fafafa"]) that
    # OVERRIDES the live ``_compute_showcase_swatches`` output for this
    # category on the catalog showcase card. Empty list → use live
    # data (suppressing one-product outliers via min_usage threshold).
    showcase_swatch_hexes = models.JSONField(
        default=list,
        blank=True,
        verbose_name='Свотчі на showcase-картці',
        help_text=(
            'Список hex-кольорів для картки в /catalog/ (наприклад '
            '["#000000","#fafafa"]). Якщо порожньо — обчислюються '
            'автоматично з товарів категорії (відсіюючи одиничні).'
        ),
    )
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    # Phase 21 (2026-05-10) — manual SEO overrides per category. Used
    # by ``pages/catalog.html`` and ``get_category_seo_meta`` so each
    # category page (т-shirt, hoodie, long-sleeve…) can have unique
    # ``<title>`` / ``<h1>`` / meta description copy without touching
    # the visible category name. Empty values → fallback to existing
    # boilerplate defaults (current behaviour preserved).
    seo_title = models.CharField(
        max_length=180,
        blank=True,
        verbose_name='SEO Title',
        help_text=(
            'Кастомний <title> для цієї категорії. Якщо порожньо — '
            'використовується «{Назва} - TwoComms».'
        ),
    )
    seo_h1 = models.CharField(
        max_length=180,
        blank=True,
        verbose_name='SEO H1',
        help_text=(
            'Кастомний H1 для сторінки категорії (для випадку, коли '
            'видимий заголовок має відрізнятися від назви в навігації).'
        ),
    )
    seo_description = models.CharField(
        max_length=320,
        blank=True,
        verbose_name='SEO Description',
        help_text=(
            'Мета-опис категорії (≤ 320 символів). Якщо порожньо — '
            'використовується автогенерований fallback.'
        ),
    )
    # AI-generated content fields
    ai_generation_enabled = models.BooleanField(
        default=False,
        verbose_name='Дозволити AI-генерацію SEO',
        help_text='Якщо вимкнено — AI ніколи не використовується для цієї категорії; '
                  'мета-теги та Schema беруть лише вручну заповнені SEO-поля та fallback.'
    )
    ai_keywords = models.TextField(blank=True, null=True, verbose_name='AI-ключові слова')
    ai_description = models.TextField(blank=True, null=True, verbose_name='AI-опис')
    ai_content_generated = models.BooleanField(default=False, verbose_name='AI-контент згенеровано')
    # SEO timestamps for sitemap lastmod
    created_at = models.DateTimeField(auto_now_add=True, null=True, verbose_name='Створено')
    updated_at = models.DateTimeField(auto_now=True, null=True, verbose_name='Оновлено')

    class Meta:
        ordering = ['order', 'name']
        indexes = [
            models.Index(fields=['is_active'], name='idx_category_active'),
            models.Index(fields=['is_featured'], name='idx_category_featured'),
            models.Index(fields=['order'], name='idx_category_order'),
        ]

    def __str__(self):
        return self.name


class BlogCategory(models.Model):
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="children",
        blank=True,
        null=True,
        verbose_name="Батьківська категорія",
    )
    name = models.CharField(max_length=140, unique=True, verbose_name="Назва")
    slug = models.SlugField(max_length=160, unique=True, verbose_name="URL slug")
    description = models.TextField(blank=True, verbose_name="Опис")
    seo_title = models.CharField(max_length=180, blank=True, verbose_name="SEO Title")
    seo_h1 = models.CharField(max_length=180, blank=True, verbose_name="SEO H1")
    seo_description = models.CharField(max_length=320, blank=True, verbose_name="SEO Description")
    bottom_title = models.CharField(max_length=180, blank=True, verbose_name="Нижній SEO-заголовок")
    bottom_text = models.TextField(blank=True, verbose_name="Нижній SEO-текст")
    is_active = models.BooleanField(default=True, verbose_name="Активна")
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "Категорія блогу"
        verbose_name_plural = "Категорії блогу"
        ordering = ["order", "name"]
        indexes = [
            models.Index(fields=["parent", "is_active", "order"], name="idx_blogcat_parent_active"),
            models.Index(fields=["is_active", "order"], name="idx_blogcat_active_order"),
            models.Index(fields=["slug"], name="idx_blogcat_slug"),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.parent_id and self.pk and self.parent_id == self.pk:
            raise ValidationError({"parent": "Категорія ����е може бути власним батьком."})
        parent = self.parent
        while parent is not None:
            if self.pk and parent.pk == self.pk:
                raise ValidationError({"parent": "Категорія не може бути вкладена у власну дочірню категорію."})
            parent = parent.parent

    def get_absolute_url(self):
        if self.parent_id and self.parent:
            return reverse(
                "blog_category_nested",
                kwargs={"parent_slug": self.parent.slug, "slug": self.slug},
            )
        return reverse("blog_category", kwargs={"slug": self.slug})


class BlogPostQuerySet(models.QuerySet):
    def published(self):
        return (
            self.filter(is_published=True, published_at__lte=timezone.now())
            .exclude(slug="")
            .select_related("category")
        )


class BlogPost(models.Model):
    category = models.ForeignKey(
        BlogCategory,
        on_delete=models.PROTECT,
        related_name="posts",
        verbose_name="Категорія",
    )
    title = models.CharField(max_length=220, verbose_name="Заголовок")
    slug = models.SlugField(max_length=180, unique=True, verbose_name="URL slug")
    excerpt = models.TextField(blank=True, verbose_name="Короткий опис")
    content_html = models.TextField(verbose_name="HTML-контент")
    cover_image = models.ImageField(upload_to="blog/covers/", blank=True, null=True, verbose_name="Обкладинка")
    cover_alt = models.CharField(max_length=180, blank=True, verbose_name="Alt обкладинки")
    cover_caption = models.TextField(blank=True, verbose_name="Підпис обкладинки")
    source_url = models.URLField(blank=True, verbose_name="Джерело")
    cta_label = models.CharField(max_length=140, blank=True, verbose_name="CTA текст")
    cta_url = models.CharField(max_length=300, blank=True, verbose_name="CTA URL")
    cta_text = models.CharField(max_length=260, blank=True, verbose_name="CTA підпис")
    seo_title = models.CharField(max_length=180, blank=True, verbose_name="SEO Title")
    seo_description = models.CharField(max_length=320, blank=True, verbose_name="SEO Description")
    seo_keywords = models.CharField(max_length=320, blank=True, verbose_name="SEO keywords")
    is_published = models.BooleanField(default=True, verbose_name="Опубліковано")
    published_at = models.DateTimeField(default=timezone.now, verbose_name="Дата публікації")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")
    view_count = models.PositiveIntegerField(default=0, verbose_name="Перегляди")
    unique_view_count = models.PositiveIntegerField(default=0, verbose_name="Унікальні перегляди")

    objects = BlogPostQuerySet.as_manager()

    class Meta:
        verbose_name = "Пост блогу"
        verbose_name_plural = "Пости блогу"
        ordering = ["-published_at", "-id"]
        indexes = [
            models.Index(fields=["is_published", "-published_at"], name="idx_blogpost_pub_date"),
            models.Index(fields=["category", "is_published"], name="idx_blogpost_cat_pub"),
            models.Index(fields=["slug"], name="idx_blogpost_slug"),
        ]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("blog_post", kwargs={"slug": self.slug})

    def save(self, *args, **kwargs):
        self._optimize_cover_image()
        super().save(*args, **kwargs)

    def _optimize_cover_image(self):
        if not self.cover_image or getattr(self.cover_image, "_committed", True):
            return

        self.cover_image.file.seek(0)
        with Image.open(self.cover_image.file) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode in ("RGBA", "LA", "P"):
                canvas = Image.new("RGB", image.size, "#0b0b0f")
                if image.mode == "P":
                    image = image.convert("RGBA")
                canvas.paste(image, mask=image.getchannel("A") if "A" in image.getbands() else None)
                image = canvas
            else:
                image = image.convert("RGB")

            image.thumbnail((1600, 1000), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="WEBP", quality=84, method=6)

        stem = Path(self.cover_image.name).stem or f"cover-{uuid.uuid4().hex[:10]}"
        self.cover_image.save(f"{stem}.webp", ContentFile(output.getvalue()), save=False)


class BlogMediaAsset(models.Model):
    file = models.ImageField(upload_to="blog/media/", verbose_name="Файл")
    width = models.PositiveIntegerField(default=0, verbose_name="Ширина")
    height = models.PositiveIntegerField(default=0, verbose_name="Висота")
    alt = models.JSONField(default=dict, blank=True, verbose_name="Alt за мовами")
    caption = models.JSONField(default=dict, blank=True, verbose_name="Підпис за мовами")
    credit = models.JSONField(default=dict, blank=True, verbose_name="Автор/джерело за мовами")
    focal_x = models.PositiveSmallIntegerField(default=50, verbose_name="Фокус X")
    focal_y = models.PositiveSmallIntegerField(default=50, verbose_name="Фокус Y")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "Медіа блогу"
        verbose_name_plural = "Медіа блогу"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.file.name or f"Blog media #{self.pk}"

    def save(self, *args, **kwargs):
        self._read_dimensions()
        super().save(*args, **kwargs)

    def _read_dimensions(self):
        if not self.file:
            return
        try:
            self.file.seek(0)
            with Image.open(self.file) as image:
                self.width, self.height = image.size
        except Exception:
            return
        finally:
            try:
                self.file.seek(0)
            except Exception:
                pass


class BlogPostBlock(models.Model):
    class BlockType(models.TextChoices):
        RICH_TEXT = "rich_text", "Текст"
        IMAGE = "image", "Зображення"
        GALLERY = "gallery", "Галерея"
        YOUTUBE_VIDEO = "youtube_video", "YouTube"
        CTA_GROUP = "cta_group", "CTA / соцмережі"
        PRODUCT_CTA = "product_cta", "Товар"
        METRIC_CARDS = "metric_cards", "Метрики"
        TABLE_SPECS = "table_specs", "Таблиця / specs"
        QUOTE = "quote", "Цитата"
        CALLOUT = "callout", "Callout"
        FAQ = "faq", "FAQ"
        SOURCE_LIST = "source_list", "Джерела"
        LOCKED_CONTENT = "locked_content", "Прихований контент"
        PROMO_CLAIM = "promo_claim", "Промокод"

    post = models.ForeignKey(BlogPost, on_delete=models.CASCADE, related_name="blocks", verbose_name="Пост")
    block_type = models.CharField(max_length=32, choices=BlockType.choices, verbose_name="Тип блоку")
    sort_order = models.PositiveIntegerField(default=0, verbose_name="Порядок")
    is_enabled = models.BooleanField(default=True, verbose_name="Увімкнено")
    payload = models.JSONField(default=dict, blank=True, verbose_name="Дані блоку")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "Блок поста блогу"
        verbose_name_plural = "Блоки постів блогу"
        ordering = ["post", "sort_order", "id"]
        indexes = [
            models.Index(fields=["post", "is_enabled", "sort_order"], name="idx_blogblock_post_order"),
            models.Index(fields=["block_type"], name="idx_blogblock_type"),
        ]

    def __str__(self):
        return f"{self.post_id}:{self.block_type}:{self.sort_order}"


class BlogPromoCampaign(models.Model):
    DISCOUNT_TYPES = [
        ("percentage", _("Відсоток")),
        ("fixed", _("Фіксована сума")),
    ]

    name = models.CharField(max_length=160, verbose_name="Назва кампанії")
    code_prefix = models.CharField(max_length=8, default="BLOG", verbose_name="Префікс коду")
    discount_type = models.CharField(max_length=10, choices=DISCOUNT_TYPES, verbose_name="Тип знижки")
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Значення знижки")
    max_claims = models.PositiveIntegerField(default=0, verbose_name="Максимум видач (0 = безліміт)")
    valid_days = models.PositiveIntegerField(default=14, verbose_name="Днів дії після видачі")
    min_order_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name="Мін. сума")
    one_time_per_user = models.BooleanField(default=True, verbose_name="Одноразово для користувача")
    is_active = models.BooleanField(default=True, verbose_name="Активна")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "Промо-кампанія блогу"
        verbose_name_plural = "Промо-кампанії блогу"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["is_active", "-created_at"], name="idx_blogpromo_active"),
        ]

    def __str__(self):
        return self.name

    @property
    def claims_count(self):
        return self.claims.count()

    def can_claim(self):
        return self.is_active and (self.max_claims == 0 or self.claims.count() < self.max_claims)


class BlogPromoClaim(models.Model):
    campaign = models.ForeignKey(
        BlogPromoCampaign,
        on_delete=models.PROTECT,
        related_name="claims",
        verbose_name="Кампанія",
    )
    post = models.ForeignKey(BlogPost, on_delete=models.CASCADE, related_name="promo_claims", verbose_name="Пост")
    block = models.ForeignKey(BlogPostBlock, on_delete=models.CASCADE, related_name="promo_claims", verbose_name="Блок")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="blog_promo_claims")
    promo_code = models.OneToOneField("PromoCode", on_delete=models.PROTECT, related_name="blog_claim")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Видача промокоду з блогу"
        verbose_name_plural = "Видачі промокодів з блогу"
        constraints = [
            models.UniqueConstraint(fields=["campaign", "user"], name="uniq_blogpromo_campaign_user"),
            models.UniqueConstraint(fields=["block", "user"], name="uniq_blogpromo_block_user"),
        ]
        indexes = [
            models.Index(fields=["post", "created_at"], name="idx_blogclaim_post_created"),
            models.Index(fields=["user", "created_at"], name="idx_blogclaim_user_created"),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.campaign_id}:{self.promo_code_id}"


class BlogPostView(models.Model):
    post = models.ForeignKey(BlogPost, on_delete=models.CASCADE, related_name="view_events")
    visitor_key = models.CharField(max_length=64)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="blog_post_views",
    )
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.TextField(blank=True)
    path = models.CharField(max_length=300, blank=True)
    views = models.PositiveIntegerField(default=1)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Перегляд поста блогу"
        verbose_name_plural = "Перегляди постів блогу"
        constraints = [
            models.UniqueConstraint(fields=["post", "visitor_key"], name="uniq_blog_post_view_key"),
        ]
        indexes = [
            models.Index(fields=["post", "last_seen"], name="idx_blogview_post_seen"),
            models.Index(fields=["visitor_key"], name="idx_blogview_visitor"),
        ]

    def __str__(self):
        return f"{self.post_id}:{self.visitor_key}"


class Catalog(models.Model):
    name = models.CharField(max_length=200, verbose_name='Назва каталогу')
    slug = models.SlugField(unique=True, verbose_name='URL slug')
    description = models.TextField(blank=True, verbose_name='Опис каталогу')
    order = models.PositiveIntegerField(default=0, verbose_name='Порядок')
    is_active = models.BooleanField(default=True, verbose_name='Активний')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Створено')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Оновлено')

    class Meta:
        verbose_name = 'Каталог'
        verbose_name_plural = 'Каталоги'
        ordering = ['order', 'name']
        indexes = [
            models.Index(fields=['is_active'], name='idx_catalog_active'),
            models.Index(fields=['order', 'name'], name='idx_catalog_order_name'),
        ]

    def __str__(self):
        return self.name


class CatalogOption(models.Model):
    class OptionType(models.TextChoices):
        SIZE = 'size', _('Розмір')
        MATERIAL = 'material', _('Матеріал')
        COLOR = 'color', _('Колір')
        CUSTOM = 'custom', _('Кастомна опція')

    catalog = models.ForeignKey(Catalog, on_delete=models.CASCADE, related_name='options', verbose_name='Каталог')
    name = models.CharField(max_length=200, verbose_name='Назва опції')
    option_type = models.CharField(
        max_length=50,
        choices=OptionType.choices,
        default=OptionType.CUSTOM,
        verbose_name='Тип опції'
    )
    is_required = models.BooleanField(default=True, verbose_name="Обов'язкова")
    is_additional_cost = models.BooleanField(default=False, verbose_name='Додаткова вартість')
    additional_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        verbose_name='Додаткова ціна (грн)'
    )
    help_text = models.CharField(max_length=255, blank=True, verbose_name='Підказка')
    order = models.PositiveIntegerField(default=0, verbose_name='Порядок')

    class Meta:
        verbose_name = 'Опція каталогу'
        verbose_name_plural = 'Опції каталогу'
        ordering = ['order', 'id']
        indexes = [
            models.Index(fields=['catalog', 'order'], name='idx_catalog_option_order'),
            models.Index(fields=['catalog', 'option_type'], name='idx_catalog_option_type'),
        ]
        unique_together = ('catalog', 'name')

    def __str__(self):
        return f'{self.catalog.name}: {self.name}'


class CatalogOptionValue(models.Model):
    option = models.ForeignKey(CatalogOption, on_delete=models.CASCADE, related_name='values', verbose_name='Опція')
    value = models.CharField(max_length=200, verbose_name='Значення')
    display_name = models.CharField(max_length=200, verbose_name='Назва для відображення')
    image = models.ImageField(upload_to='catalog_options/', blank=True, null=True, verbose_name='Зображення')
    order = models.PositiveIntegerField(default=0, verbose_name='Порядок')
    is_default = models.BooleanField(default=False, verbose_name='За замовчуванням')
    metadata = models.JSONField(default=dict, blank=True, verbose_name='Додаткові дані')

    class Meta:
        verbose_name = 'Значення опції'
        verbose_name_plural = 'Значення опцій'
        ordering = ['order', 'id']
        indexes = [
            models.Index(fields=['option', 'order'], name='idx_option_value_order'),
        ]
        unique_together = ('option', 'value')

    def __str__(self):
        return f'{self.display_name} ({self.option.name})'


class SizeGrid(models.Model):
    catalog = models.ForeignKey(
        Catalog,
        on_delete=models.CASCADE,
        related_name='size_grids',
        verbose_name='Каталог'
    )
    name = models.CharField(max_length=200, verbose_name='Назва сітки розмірів')
    image = models.ImageField(upload_to='size_grids/', blank=True, null=True, verbose_name='Зображення сітки розмірів')
    description = models.TextField(blank=True, verbose_name='Опис сітки')
    guide_data = models.JSONField(default=dict, blank=True, verbose_name='Структуровані дані сітки')
    is_active = models.BooleanField(default=True, verbose_name='Активна')
    order = models.PositiveIntegerField(default=0, verbose_name='Порядок')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Створено')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Оновлено')

    class Meta:
        verbose_name = 'Сітка розмірів'
        verbose_name_plural = 'Сітки розмірів'
        ordering = ['order', 'name']
        indexes = [
            models.Index(fields=['catalog', 'order'], name='idx_size_grid_catalog_order'),
            models.Index(fields=['is_active'], name='idx_size_grid_active'),
        ]

    def __str__(self):
        return f'{self.catalog.name}: {self.name}'


class PrintProposal(models.Model):
    """Заявка пользователя на предложенный принт"""
    STATUS_CHOICES = [
        ("pending", _("На розгляді")),
        ("approved", _("Схвалено")),
        ("rejected", _("Відхилено")),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="print_proposals", verbose_name="Користувач")
    image = models.ImageField(upload_to="print_proposals/", blank=True, null=True, verbose_name="Зображення")
    link_url = models.URLField(blank=True, verbose_name="Посилання на зображення")
    description = models.TextField(blank=True, verbose_name="Опис / примітки")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending", verbose_name="Статус")
    awarded_points = models.PositiveIntegerField(default=0, verbose_name="Нараховані бали")
    awarded_promocode = models.ForeignKey(
        "storefront.PromoCode", on_delete=models.SET_NULL, blank=True, null=True, verbose_name="Промокод"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "Пропозиція принта"
        verbose_name_plural = "Пропозиції принтів"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        base = f"{self.user.username} — {self.get_status_display()}"
        if self.awarded_points:
            base += f" (+{self.awarded_points} б.)"
        return base


class CustomPrintLeadStatus(models.TextChoices):
    NEW = "new", _("Нова")
    IN_PROGRESS = "in_progress", _("В роботі")
    CLOSED = "closed", _("Закрита")


class CustomPrintServiceKind(models.TextChoices):
    READY = "ready", _("Маю готовий файл")
    ADJUST = "adjust", _("Потрібно допрацювати файл")
    DESIGN = "design", _("Потрібен дизайн з нуля")


class CustomPrintProductType(models.TextChoices):
    TSHIRT = "tshirt", _("Футболка")
    HOODIE = "hoodie", _("Худі")
    LONGSLEEVE = "longsleeve", _("Лонгслів")
    CUSTOMER_GARMENT = "customer_garment", _("Свій одяг")


class CustomPrintClientKind(models.TextChoices):
    PERSONAL = "personal", _("Для себе")
    BRAND = "brand", _("Для бренду / команди / події")


class CustomPrintBusinessKind(models.TextChoices):
    BULK = "bulk", _("Оптова партія")
    BRANDING = "branding", _("Брендування / мерч")


class CustomPrintSizeMode(models.TextChoices):
    SINGLE = "single", _("Один розмір")
    MIXED = "mixed", _("Мікс")
    MANAGER = "manager", _("Уточню з менеджером")


class CustomPrintContactChannel(models.TextChoices):
    TELEGRAM = "telegram", _("Telegram")
    WHATSAPP = "whatsapp", _("WhatsApp")
    PHONE = "phone", _("Телефон")


class CustomPrintModerationStatus(models.TextChoices):
    DRAFT = "draft", _("Чернетка")
    AWAITING_REVIEW = "awaiting_review", _("На перевірці менеджера")
    APPROVED = "approved", _("Погоджено — оплата доступна")
    REJECTED = "rejected", _("Відхилено менеджером")


class CustomPrintLead(models.Model):
    lead_number = models.CharField(max_length=24, unique=True, blank=True, verbose_name="Номер заявки")
    status = models.CharField(
        max_length=20,
        choices=CustomPrintLeadStatus.choices,
        default=CustomPrintLeadStatus.NEW,
        verbose_name="Статус",
    )
    service_kind = models.CharField(
        max_length=20,
        choices=CustomPrintServiceKind.choices,
        verbose_name="Тип послуги",
    )
    product_type = models.CharField(
        max_length=20,
        choices=CustomPrintProductType.choices,
        verbose_name="Тип виробу",
    )
    placements = models.JSONField(default=list, blank=True, verbose_name="Зони нанесення")
    placement_note = models.CharField(max_length=255, blank=True, verbose_name="Нестандартне розміщення")
    quantity = models.PositiveIntegerField(default=1, verbose_name="Кількість")
    sizes_note = models.CharField(max_length=255, blank=True, verbose_name="Розміри")
    client_kind = models.CharField(
        max_length=20,
        choices=CustomPrintClientKind.choices,
        default=CustomPrintClientKind.PERSONAL,
        verbose_name="Тип клієнта",
    )
    business_kind = models.CharField(
        max_length=20,
        choices=CustomPrintBusinessKind.choices,
        blank=True,
        default="",
        verbose_name="B2B сценарій",
    )
    brand_name = models.CharField(max_length=255, blank=True, verbose_name="Бренд / команда")
    size_mode = models.CharField(
        max_length=20,
        choices=CustomPrintSizeMode.choices,
        blank=True,
        default="",
        verbose_name="Режим розмірів",
    )
    fit = models.CharField(max_length=32, blank=True, default="", verbose_name="Посадка")
    fabric = models.CharField(max_length=32, blank=True, default="", verbose_name="Тканина / рівень")
    color_choice = models.CharField(max_length=64, blank=True, default="", verbose_name="Колір виробу")
    garment_note = models.CharField(max_length=255, blank=True, default="", verbose_name="Опис виробу клієнта")
    file_triage_status = models.CharField(max_length=32, blank=True, default="", verbose_name="Статус triage файлу")
    exit_step = models.CharField(max_length=32, blank=True, default="", verbose_name="Крок safe exit")
    placement_specs_json = models.JSONField(default=list, blank=True, verbose_name="Специфікації зон нанесення")
    pricing_snapshot_json = models.JSONField(default=dict, blank=True, verbose_name="Снапшот прорахунку")
    config_draft_json = models.JSONField(default=dict, blank=True, verbose_name="Чернетка конфігуратора")
    name = models.CharField(max_length=200, verbose_name="Ім'я")
    contact_channel = models.CharField(
        max_length=20,
        choices=CustomPrintContactChannel.choices,
        default=CustomPrintContactChannel.TELEGRAM,
        verbose_name="Канал зв'язку",
    )
    contact_value = models.CharField(max_length=255, verbose_name="Контакт")
    brief = models.TextField(blank=True, verbose_name="Опис задачі")
    source = models.CharField(max_length=50, default="main_custom_print", blank=True, verbose_name="Джерело")
    moderation_status = models.CharField(
        max_length=20,
        choices=CustomPrintModerationStatus.choices,
        default=CustomPrintModerationStatus.DRAFT,
        db_default=CustomPrintModerationStatus.DRAFT,
        verbose_name="Статус модерації кастомного кошика",
    )
    approved_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Ціна після погодження менеджера",
    )
    manager_note = models.TextField(blank=True, default="", verbose_name="Коментар менеджера")
    moderation_token = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="Токен для дій менеджера",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="Коли відреаговано")
    last_notification_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Останнє Telegram-сповіщення",
    )
    notification_count = models.PositiveSmallIntegerField(
        default=0,
        verbose_name="Лічильник Telegram-сповіщень",
    )
    # ── Telegram contact verification (поделился номером через бота) ──
    telegram_verified_user_id = models.BigIntegerField(
        null=True,
        blank=True,
        verbose_name="Telegram user_id (підтверджено через бота)",
    )
    telegram_verified_username = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="Telegram @username (підтверджено)",
    )
    telegram_verified_phone = models.CharField(
        max_length=32,
        blank=True,
        default="",
        verbose_name="Телефон (підтверджено через Telegram)",
    )
    telegram_verified_first_name = models.CharField(
        max_length=120,
        blank=True,
        default="",
        verbose_name="Імʼя у Telegram",
    )
    telegram_verified_last_name = models.CharField(
        max_length=120,
        blank=True,
        default="",
        verbose_name="Прізвище у Telegram",
    )
    telegram_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Коли підтверджено Telegram",
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="custom_print_leads",
        verbose_name="Замовлення",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "Заявка на кастомний принт"
        verbose_name_plural = "Заявки на кастомний принт"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"], name="idx_cprint_status"),
            models.Index(fields=["service_kind"], name="idx_cprint_service"),
            models.Index(fields=["created_at"], name="idx_cprint_created"),
        ]

    def __str__(self):
        return f"{self.lead_number or 'Custom Print'} — {self.name}"

    def save(self, *args, **kwargs):
        attempts = 0
        while True:
            if not self.lead_number:
                self.lead_number = self.generate_lead_number()
            try:
                with transaction.atomic():
                    super().save(*args, **kwargs)
                break
            except IntegrityError:
                attempts += 1
                if attempts >= 5:
                    raise
                self.lead_number = ""

    @property
    def estimate_required(self):
        return bool((self.pricing_snapshot_json or {}).get("estimate_required"))

    @property
    def final_price_value(self):
        """Returns approved_price if set, else snapshot final_total, else 0."""
        if self.approved_price is not None:
            return self.approved_price
        snapshot = self.pricing_snapshot_json or {}
        value = snapshot.get("final_total") or snapshot.get("unit_total") or 0
        from decimal import Decimal, InvalidOperation
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError):
            return Decimal("0")

    def ensure_moderation_token(self, save=True):
        """Generate and persist a random moderation token used for Telegram URL buttons."""
        if not self.moderation_token:
            import secrets
            self.moderation_token = secrets.token_urlsafe(32)
            if save and self.pk:
                type(self).objects.filter(pk=self.pk).update(moderation_token=self.moderation_token)
        return self.moderation_token

    @staticmethod
    def generate_lead_number():
        today = timezone.localdate()
        prefix = f"CP{today.strftime('%d%m%Y')}L"
        counter = 0
        for lead_number in CustomPrintLead.objects.filter(
            lead_number__startswith=prefix
        ).values_list("lead_number", flat=True):
            suffix = str(lead_number).replace(prefix, "", 1)
            if suffix.isdigit():
                counter = max(counter, int(suffix))
        return f"{prefix}{counter + 1:03d}"


class CustomPrintLeadAttachment(models.Model):
    class AttachmentRole(models.TextChoices):
        DESIGN = "design", _("Макет / дизайн")
        REFERENCE = "reference", _("Референс")

    lead = models.ForeignKey(
        CustomPrintLead,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name="Заявка",
    )
    file = models.FileField(upload_to="custom_print/leads/", verbose_name="Файл")
    placement_zone = models.CharField(max_length=32, blank=True, verbose_name="Зона нанесення")
    attachment_role = models.CharField(
        max_length=20,
        choices=AttachmentRole.choices,
        default=AttachmentRole.DESIGN,
        verbose_name="Роль файлу",
    )
    sort_order = models.PositiveIntegerField(default=0, verbose_name="Порядок")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")

    class Meta:
        verbose_name = "Файл заявки на кастомний принт"
        verbose_name_plural = "Файли заявок на кастомний принт"

    def __str__(self):
        return f"Attachment for {self.lead_id}"


class ProductStatus(models.TextChoices):
    DRAFT = 'draft', _('Чернетка')
    REVIEW = 'review', _('На модерації')
    SCHEDULED = 'scheduled', _('Заплановано')
    PUBLISHED = 'published', _('Опубліковано')
    ARCHIVED = 'archived', _('Архів')


class Product(models.Model):
    title = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='products')
    catalog = models.ForeignKey(
        Catalog,
        on_delete=models.PROTECT,
        related_name='products',
        null=True,
        blank=True,
        verbose_name='Каталог'
    )
    size_grid = models.ForeignKey(
        SizeGrid,
        on_delete=models.SET_NULL,
        related_name='products',
        null=True,
        blank=True,
        verbose_name='Сітка розмірів'
    )
    price = models.PositiveIntegerField(verbose_name='Ціна (грн)', db_index=True)
    # has_discount field removed - see migration 0008_remove_has_discount_field
    # Use @property has_discount below instead (auto-calculated from discount_percent)
    discount_percent = models.PositiveIntegerField(blank=True, null=True)
    featured = models.BooleanField(default=False)
    short_description = models.CharField(max_length=300, blank=True, verbose_name='Короткий опис')
    description = models.TextField(blank=True, verbose_name='Опис (legacy)')
    full_description = models.TextField(blank=True, verbose_name='Повний опис')
    details_text = models.TextField(blank=True, verbose_name='Деталі товару')
    target_audience = models.TextField(blank=True, verbose_name='Кому підійде')
    care_instructions = models.TextField(blank=True, verbose_name='Догляд за товаром')
    main_image = models.ImageField(upload_to='products/', blank=True, null=True)
    home_card_image = models.ImageField(
        upload_to='products/home_cards/',
        blank=True,
        null=True,
        verbose_name='Зображення картки на головній'
    )
    main_image_alt = models.CharField(max_length=200, blank=True, null=True, verbose_name='Alt-текст головного зображення')
    video_url = models.URLField(
        max_length=500,
        blank=True,
        verbose_name='Відео товару (YouTube)',
        help_text=(
            'Посилання на YouTube-відео товару (watch, youtu.be, embed або shorts). '
            'Зʼявиться у галереї товару окремим слайдом та у структурованих даних.'
        ),
    )
    points_reward = models.PositiveIntegerField(default=0, verbose_name='Бали за покупку')
    status = models.CharField(
        max_length=20,
        choices=ProductStatus.choices,
        default=ProductStatus.DRAFT,
        verbose_name='Статус публікації'
    )
    priority = models.PositiveIntegerField(default=0, verbose_name='Пріоритет показу')
    published_at = models.DateTimeField(blank=True, null=True, verbose_name='Опубліковано')
    unpublished_reason = models.CharField(max_length=200, blank=True, verbose_name='Причина відключення')
    last_reviewed_at = models.DateTimeField(blank=True, null=True, verbose_name='Остання модерація')
    seo_title = models.CharField(max_length=160, blank=True, verbose_name='SEO Title')
    seo_description = models.CharField(max_length=320, blank=True, verbose_name='SEO Description')
    seo_keywords = models.CharField(max_length=300, blank=True, verbose_name='SEO ключові слова')
    # Phase 15 — optional admin override for the bottom SEO landing block.
    seo_bottom_html = models.TextField(
        blank=True, default='',
        verbose_name='SEO-блок внизу сторінки (опційно)',
        help_text=(
            'Опційний HTML-блок, який рендериться у нижній частині картки '
            'товару перед футером. Якщо порожній — генерується автоматично '
            'з тем та категорії товару.'
        ),
    )
    seo_schema = models.JSONField(blank=True, default=dict, verbose_name='Structured data')
    # SEO molecular-upgrade US-6 (2026-05-17) — admin-editable per-product
    # search-keywords used by the «Часті пошуки» strip on PDP. Each item
    # is a dict with ``label``, ``url``, optional ``weight`` (sort order,
    # higher first). Custom items always rendered before auto-generated
    # ones so the admin can curate the strip without losing the auto fallback.
    #
    # Stored as a flat list of dicts: ``[{"label": "...", "url": "...",
    # "weight": 100, "description": ""}, ...]``. The url MUST be an
    # internal absolute path or relative to root (e.g. /catalog/tshirts/).
    # Validation lives in ``storefront.services.product_search_keywords``.
    search_keywords = models.JSONField(
        blank=True,
        default=list,
        verbose_name='Часті пошуки (manual override)',
        help_text=(
            'Список словників [{"label": "купити чорну футболку оверсайз", '
            '"url": "/catalog/tshirts/black/", "weight": 100}]. '
            'Auto-generated chips додаються після manual.'
        ),
    )
    recommendation_tags = models.JSONField(blank=True, default=list, verbose_name='Теги рекомендацій')

    # Дропшип цены
    drop_price = models.PositiveIntegerField(default=0, verbose_name='Ціна дропа (грн)')
    recommended_price = models.PositiveIntegerField(default=0, verbose_name='Рекомендована ціна (грн)')

    # Оптовые цены для дропшипа
    wholesale_price = models.PositiveIntegerField(default=0, verbose_name='Оптова ціна (грн)')

    # Поля для определения участия в дропшипе
    is_dropship_available = models.BooleanField(default=True, verbose_name='Доступний для дропшипа')
    dropship_note = models.CharField(max_length=200, blank=True, null=True, verbose_name='Примітка для дропшипа')

    # AI-generated content fields
    ai_generation_enabled = models.BooleanField(
        default=False,
        verbose_name='Дозволити AI-генерацію SEO',
        help_text='Якщо вимкнено — AI не використовується для цього товару. '
                  'Мета-теги та Schema будуть братися лише з вручну заповнених SEO-полів та fallback.'
    )
    ai_keywords = models.TextField(blank=True, null=True, verbose_name='AI-ключові слова')
    ai_description = models.TextField(blank=True, null=True, verbose_name='AI-опис')
    ai_content_generated = models.BooleanField(default=False, verbose_name='AI-контент згенеровано')
    # Fit selector visibility (classic / oversize)
    fit_selector_enabled = models.BooleanField(
        default=True,
        verbose_name='Показувати селектор крою (класика / оверсай��)',
        help_text='Якщо вимкнено — блок з вибором крою не відображається на сторінці товару.'
    )
    # SEO timestamps for sitemap lastmod
    created_at = models.DateTimeField(auto_now_add=True, null=True, verbose_name='Створено')
    updated_at = models.DateTimeField(auto_now=True, null=True, verbose_name='Оновлено')

    def save(self, *args, **kwargs):
        # Синхронизуем legacy-описание с новым полем
        if self.full_description and not self.description:
            self.description = self.full_description
        if self.description and not self.full_description:
            self.full_description = self.description
        if self.full_description and not self.short_description:
            plain = self.full_description.strip()
            if len(plain) > 300:
                self.short_description = plain[:297].rstrip() + '...'
            else:
                self.short_description = plain
        super().save(*args, **kwargs)

    @property
    def has_discount(self):
        """Автоматически определяет, есть ли скидка"""
        return bool(self.discount_percent and self.discount_percent > 0)

    @property
    def final_price(self):
        if self.has_discount:
            return int(self.price*(100-self.discount_percent)/100)
        return self.price

    @property
    def homepage_image(self):
        """Зображення для картки на головній з fallback на поточне прев'ю товару."""
        if self.home_card_image:
            return self.home_card_image
        return self.display_image

    @property
    def display_image(self):
        """Возвращает главное изображение или первую фотографию первого цвета"""
        if self.main_image:
            return self.main_image

        # Если нет главного изображения, ищем в цветах
        # Оптимизация: используем предзагруженные данные, если они есть
        if hasattr(self, '_prefetched_objects_cache') and 'color_variants' in self._prefetched_objects_cache:
            variants = list(self.color_variants.all())
            first_color_variant = variants[0] if variants else None
        else:
            first_color_variant = self.color_variants.first()

        if first_color_variant:
            # Оптимизация для изображений варианта
            if hasattr(first_color_variant, '_prefetched_objects_cache') and 'images' in first_color_variant._prefetched_objects_cache:
                images = list(first_color_variant.images.all())
                first_image = images[0] if images else None
            else:
                first_image = first_color_variant.images.first()

            if first_image:
                return first_image.image

        return None

    @property
    def youtube_id(self):
        """YouTube video id parsed from ``video_url`` (empty if none/invalid)."""
        from storefront.utils.video import extract_youtube_id
        return extract_youtube_id(self.video_url or '')

    @property
    def has_video(self):
        """True when a valid YouTube video is attached to the product."""
        return bool(self.youtube_id)

    @property
    def video_embed_url(self):
        """Privacy-friendly embed URL for the gallery iframe."""
        from storefront.utils.video import youtube_embed_url
        return youtube_embed_url(self.youtube_id)

    @property
    def video_watch_url(self):
        """Canonical watch URL (used by the Merchant feed video_link)."""
        from storefront.utils.video import youtube_watch_url
        return youtube_watch_url(self.youtube_id)

    @property
    def video_thumbnail_url(self):
        """Static poster image served by YouTube's CDN."""
        from storefront.utils.video import youtube_thumbnail_url
        return youtube_thumbnail_url(self.youtube_id)

    def __str__(self):
        return self.title

    def get_drop_price(self, dropshipper=None):
        """Получить цену дропа (оптовая цена) с учетом скидки за успешные дропы"""
        base_price = 0

        if self.category and self.category.slug == 'hoodie':
            base_price = 1350
        elif self.category and self.category.slug == 'long-sleeve':
            return 0
        else:
            title_lower = self.title.lower()
            if 'футболка' in title_lower or 't-shirt' in title_lower:
                base_price = 570
            elif 'худи' in title_lower or 'hoodie' in title_lower or 'флис' in title_lower:
                base_price = 1350
            elif self.wholesale_price > 0:
                return self.wholesale_price
            else:
                return self.drop_price

        # Если не указан дропшипер, возвращаем базовую цену
        if not dropshipper:
            return base_price

        # Рассчитываем скидку за успешные дропы
        from orders.models import DropshipperOrder
        successful_orders = DropshipperOrder.objects.filter(
            dropshipper=dropshipper,
            status='delivered'
        ).count()

        # Скидка 10 грн за каждый успешный дроп
        discount = successful_orders * 10

        # Минимальные цены
        if base_price == 1350:  # худи
            min_price = 1200
        elif base_price == 570:  # футболки
            min_price = 500
        else:
            min_price = base_price

        final_price = max(min_price, base_price - discount)
        return final_price

    def get_recommended_price(self):
        """Получить рекомендованную цену (цена со скидкой +-10%)"""
        if self.has_discount and self.discount_percent:
            discounted_price = self.price * (100 - self.discount_percent) / 100
            # Добавляем +-10% к цене со скидкой
            min_price = int(discounted_price * 0.9)
            max_price = int(discounted_price * 1.1)
            return {
                'min': min_price,
                'max': max_price,
                'base': int(discounted_price)
            }
        return {
            'min': int(self.price * 0.9),
            'max': int(self.price * 1.1),
            'base': self.price
        }

    def is_available_for_dropship(self):
        """Проверить доступность для дропшипа"""
        if not self.is_dropship_available:
            return False
        if self.category and self.category.slug == 'long-sleeve':
            return False
        return True

    def get_offer_id(self, color_variant_id=None, size='S', color_name=None):
        """
        Генерирует offer_id для синхронизации с Google Merchant Feed и пикселями.

        Args:
            color_variant_id: ID цветового варианта (опционально)
            size: Размер (S, M, L, XL, XXL)
            color_name: Название цвета (опционально, для оптимизации)

        Returns:
            str: offer_id в формате TC-{id:04d}-{COLOR}-{SIZE}

        Examples:
            >>> product.get_offer_id()
            'TC-0001-ЧОРНИЙ-S'
            >>> product.get_offer_id(color_variant_id=2, size='M')
            'TC-0001-БІЛИЙ-M'
        """
        from storefront.utils.analytics_helpers import get_offer_id

        # F-018: a cart request can omit color_variant_id even for a product
        # that has real variants. In that case the feed contains the default
        # variant colour, so using the generic Russian fallback fragments the
        # pixel content_id. Resolve the same default/first variant once per
        # Product instance; truly colourless products retain the feed fallback.
        if color_variant_id is None and not color_name:
            sentinel = object()
            default_color = getattr(self, '_offer_id_default_color', sentinel)
            if default_color is sentinel:
                variants = list(self.color_variants.all())
                selected = next(
                    (variant for variant in variants if variant.is_default),
                    variants[0] if variants else None,
                )
                color = getattr(selected, 'color', None) if selected else None
                default_color = getattr(color, 'name', None) or None
                self._offer_id_default_color = default_color
            color_name = default_color
        return get_offer_id(self.id, color_variant_id, size, color_name)

    def get_all_offer_ids(self, sizes=None):
        """
        Генерирует все возможные offer_ids для товара (все цвета × все размеры).

        Args:
            sizes: Список размеров (по умолчанию ['S', 'M', 'L', 'XL', 'XXL'])

        Returns:
            list: Список всех offer_ids
        """
        if sizes is None:
            from storefront.services.size_guides import resolve_product_sizes

            sizes = resolve_product_sizes(self)

        offer_ids = []

        # Получаем цветовые варианты
        color_variants = self.color_variants.all()

        if color_variants.exists():
            # Если есть цветовые варианты - генерируем для каждого
            for variant in color_variants:
                for size in sizes:
                    offer_ids.append(self.get_offer_id(variant.id, size))
        else:
            # Если нет цветовых вариантов - генерируем с default
            for size in sizes:
                offer_ids.append(self.get_offer_id(None, size))

        return offer_ids

    class Meta:
        indexes = [
            models.Index(fields=['-id'], name='idx_product_id_desc'),
            models.Index(fields=['featured'], name='idx_product_featured'),
            models.Index(fields=['is_dropship_available'], name='idx_product_dropship'),
            models.Index(fields=['category', '-id'], name='idx_product_category_id'),
            models.Index(fields=['catalog', 'status'], name='idx_product_catalog_status'),
            models.Index(fields=['status', '-id'], name='idx_product_status_id'),
            models.Index(fields=['priority', '-id'], name='idx_product_priority_id'),
            models.Index(fields=['published_at'], name='idx_product_published_at'),
        ]


class ProductFitOption(models.Model):
    """Editable fit/cut option shown on product detail pages for supported apparel."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='fit_options')
    code = models.SlugField(max_length=50, verbose_name='Код посадки')
    label = models.CharField(max_length=100, verbose_name='Назва')
    description = models.CharField(max_length=220, blank=True, verbose_name='Опис')
    icon = models.CharField(max_length=64, blank=True, verbose_name='Іконка')
    order = models.PositiveIntegerField(default=0, verbose_name='Порядок')
    is_default = models.BooleanField(default=False, verbose_name='За замовчуванням')
    is_active = models.BooleanField(default=True, verbose_name='Активна')

    class Meta:
        verbose_name = 'Посадка товару'
        verbose_name_plural = 'Посадки товарів'
        ordering = ['order', 'id']
        unique_together = ('product', 'code')
        indexes = [
            models.Index(fields=['product', 'order'], name='idx_fit_product_order'),
            models.Index(fields=['product', 'is_active'], name='idx_fit_product_active'),
            models.Index(fields=['product', 'is_default'], name='idx_fit_product_default'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['product'],
                condition=models.Q(is_default=True),
                name='uniq_default_fit_per_product',
            ),
        ]

    def __str__(self):
        return f'{self.product.title}: {self.label}'

    def save(self, *args, **kwargs):
        if self.is_default and self.product_id:
            ProductFitOption.objects.filter(product_id=self.product_id, is_default=True).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='products/extra/')
    order = models.IntegerField(default=0, db_index=True)
    alt_text = models.CharField(max_length=200, blank=True, null=True, verbose_name='Alt-текст зображення')

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f'Image for {self.product_id}'


class ProductFAQ(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='faqs')
    question = models.CharField(max_length=255, verbose_name=_('Питання'))
    answer = models.TextField(verbose_name=_('Відповідь'))
    order = models.PositiveIntegerField(default=0, verbose_name=_('Порядок'))
    is_active = models.BooleanField(default=True, verbose_name=_('Активне'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Створено'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Оновлено'))

    class Meta:
        verbose_name = _('FAQ товару')
        verbose_name_plural = _('FAQ товарів')
        ordering = ['order', 'id']
        indexes = [
            models.Index(fields=['product', 'order'], name='idx_product_faq_order'),
            models.Index(fields=['product', 'is_active'], name='idx_product_faq_active'),
        ]

    def __str__(self):
        return f'{self.product_id}: {self.question}'


class RestockSubscription(models.Model):
    class Channel(models.TextChoices):
        TELEGRAM = 'telegram', _('Telegram')
        PHONE = 'phone', _('Телефонний дзвінок')
        EMAIL = 'email', _('Email')
        WHATSAPP = 'whatsapp', _('WhatsApp')

    class Status(models.TextChoices):
        DRAFT = 'draft', _('Очікує підтвердження')
        ACTIVE = 'active', _('Очікує наявності')
        NOTIFIED = 'notified', _('Клієнта повідомлено')
        CLOSED = 'closed', _('Закрито')
        FAILED = 'failed', _('Помилка повідомлення')

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='restock_subscriptions',
        db_constraint=False,
    )
    color_variant = models.ForeignKey(
        'productcolors.ProductColorVariant',
        on_delete=models.SET_NULL,
        related_name='restock_subscriptions',
        null=True,
        blank=True,
        db_constraint=False,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='restock_subscriptions',
        null=True,
        blank=True,
    )
    size = models.CharField(max_length=20)
    option_values = models.JSONField(blank=True, default=dict)
    option_labels = models.JSONField(blank=True, default=dict)
    channel = models.CharField(max_length=16, choices=Channel.choices)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    name = models.CharField(max_length=160, blank=True, default='')
    contact = models.CharField(max_length=254, blank=True, default='')
    normalized_contact = models.CharField(max_length=254, blank=True, default='', db_index=True)
    telegram_user_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    telegram_chat_id = models.BigIntegerField(null=True, blank=True)
    telegram_username = models.CharField(max_length=100, blank=True, default='')
    verified_phone = models.CharField(max_length=32, blank=True, default='')
    fingerprint = models.CharField(max_length=64, db_index=True)
    browser_session_key = models.CharField(max_length=64, blank=True, default='')
    request_ip_hash = models.CharField(max_length=64, blank=True, default='')
    user_agent = models.CharField(max_length=255, blank=True, default='')
    admin_notified_at = models.DateTimeField(null=True, blank=True)
    customer_notified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(
                fields=('product', 'size', 'status'),
                name='idx_restock_product_size',
            ),
            models.Index(
                fields=('channel', 'status', '-created_at'),
                name='idx_restock_channel_state',
            ),
        ]
        verbose_name = _('Очікування наявності')
        verbose_name_plural = _('Очікування наявності')

    def __str__(self):
        return f'{self.product_id}:{self.size}:{self.channel}'


class PromoCodeGroup(models.Model):
    """Группа промокодов с ограничением 'один на аккаунт'"""
    name = models.CharField(max_length=100, verbose_name=_('Назва групи'))
    description = models.TextField(blank=True, null=True, verbose_name=_('Опис групи'))
    one_per_account = models.BooleanField(default=True, verbose_name=_('Один промокод на акаунт з групи'))
    is_active = models.BooleanField(default=True, verbose_name=_('Активна'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Створено'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Оновлено'))

    class Meta:
        verbose_name = _('Група промокодів')
        verbose_name_plural = _('Групи промокодів')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['is_active', '-created_at'], name='idx_group_active_created'),
        ]

    def __str__(self):
        return f'{self.name} {"(один на акаунт)" if self.one_per_account else ""}'


class PromoCode(models.Model):
    DISCOUNT_TYPES = [
        ('percentage', _('Відсоток')),
        ('fixed', _('Фіксована сума')),
    ]

    PROMO_TYPES = [
        ('regular', _('Звичайний промокод')),
        ('voucher', _('Ваучер (фіксована сума)')),
        ('grouped', _('Груповий промокод')),
    ]

    # Основні поля
    code = models.CharField(max_length=20, unique=True, blank=True, verbose_name=_('Код промокоду'))
    promo_type = models.CharField(max_length=10, choices=PROMO_TYPES, default='regular', verbose_name=_('Тип промокоду'))
    discount_type = models.CharField(max_length=10, choices=DISCOUNT_TYPES, verbose_name=_('Тип знижки'))
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_('Значення знижки'))
    description = models.TextField(blank=True, null=True, verbose_name=_('Опис'))

    # Группировка
    group = models.ForeignKey(
        PromoCodeGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='promo_codes',
        verbose_name=_('Група')
    )

    # Ограничения использования
    max_uses = models.PositiveIntegerField(default=0, verbose_name=_('Максимальна кількість використань (0 = безліміт)'))
    current_uses = models.PositiveIntegerField(default=0, verbose_name=_('Поточна кількість використань'))
    one_time_per_user = models.BooleanField(default=False, verbose_name=_('Одноразове використання на користувача'))
    min_order_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_('Мінімальна сума замовлення')
    )

    # Период действия
    valid_from = models.DateTimeField(null=True, blank=True, verbose_name=_('Дійсний з'))
    valid_until = models.DateTimeField(null=True, blank=True, verbose_name=_('Дійсний до'))

    # Статус
    is_active = models.BooleanField(default=True, verbose_name=_('Активний'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Створено'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Оновлено'))

    class Meta:
        verbose_name = _('Промокод')
        verbose_name_plural = _('Промокоди')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['is_active', '-created_at'], name='idx_promo_active_created'),
            models.Index(fields=['group', 'is_active'], name='idx_promo_group_active'),
            models.Index(fields=['promo_type', 'is_active'], name='idx_promo_type_active'),
            models.Index(fields=['code'], name='idx_promo_code'),
        ]

    def __str__(self):
        type_label = dict(self.PROMO_TYPES).get(self.promo_type, _('Промокод'))
        return f'{self.code} ({type_label}) - {self.get_discount_display()}'

    def get_discount_display(self):
        """Возвращает отображаемое значение скидки"""
        if self.discount_type == 'percentage':
            return f'{self.discount_value}%'
        else:
            return _('%(value)s грн') % {'value': self.discount_value}

    def is_valid_now(self):
        """Проверяет, действителен ли промокод по времени"""
        now = timezone.now()
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_until and now > self.valid_until:
            return False
        return True

    def can_be_used(self):
        """Проверяет, можно ли использовать промокод (без проверки пользователя)"""
        if not self.is_active:
            return False
        if not self.is_valid_now():
            return False
        if self.max_uses > 0 and self.current_uses >= self.max_uses:
            return False
        return True

    def can_be_used_by_user(self, user):
        """Проверяет, может ли конкретный пользователь использовать промокод"""
        if not user or not user.is_authenticated:
            return False, _('Промокоди доступні тільки для зареєстрованих користувачів')

        if not self.can_be_used():
            if not self.is_active:
                return False, _('Промокод неактивний')
            if not self.is_valid_now():
                return False, _('Промокод недійсний за часом')
            if self.max_uses > 0 and self.current_uses >= self.max_uses:
                return False, _('Промокод вичерпано')
            return False, _('Промокод неактивний або вичерпаний')

        # Проверка one_time_per_user
        if self.one_time_per_user:
            if PromoCodeUsage.objects.filter(user=user, promo_code=self).exists():
                return False, _('Ви вже використали цей промокод')

        # Проверка группы (one_per_account)
        if self.group and self.group.one_per_account:
            # Проверяем, использовал ли пользователь какой-либо промокод из этой группы
            if PromoCodeUsage.objects.filter(user=user, group=self.group).exists():
                return False, _('Ви вже використали промокод з групи "%(group)s"') % {'group': self.group.name}

        return True, 'OK'

    def use(self):
        """Использует промокод (увеличивает счетчик)"""
        if self.can_be_used():
            self.current_uses = F('current_uses') + 1
            self.save(update_fields=['current_uses'])
            return True
        return False

    def record_usage(self, user, order=None):
        """Записывает использование промокода пользователем"""
        if not user or not user.is_authenticated:
            return None

        usage = PromoCodeUsage.objects.create(
            user=user,
            promo_code=self,
            group=self.group,
            order=order
        )
        self.use()
        return usage

    def calculate_discount(self, total_amount):
        """Рассчитывает скидку для указанной суммы"""
        if not self.can_be_used():
            return Decimal('0.00')

        # Проверяем минимальную сумму заказа
        total = Decimal(str(total_amount))

        if self.min_order_amount and total < self.min_order_amount:
            return Decimal('0.00')

        discount_value = Decimal(str(self.discount_value or 0))
        if discount_value <= 0:
            return Decimal('0.00')

        if self.discount_type == 'percentage':
            discount = (total * discount_value) / Decimal('100')
        else:
            # Для ваучеров и фиксированной скидки
            discount = min(discount_value, total)

        if discount > total:
            discount = total

        return discount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    def get_purchases_count(self):
        """Возвращает количество покупок с этим промокодом (исключая отмененные и в обработке)"""
        from orders.models import Order
        return Order.objects.filter(
            promo_code=self,
            status__in=['prep', 'ship', 'done']
        ).count()

    @classmethod
    def generate_code(cls, length=8):
        """Генерирует уникальный код промокода"""
        import random
        import string

        while True:
            # Генерируем код из букв и цифр
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
            # Проверяем, что код уникален
            if not cls.objects.filter(code=code).exists():
                return code


class PromoCodeUsage(models.Model):
    """История использования промокодов пользователями"""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='promo_usages',
        verbose_name=_('Користувач')
    )
    promo_code = models.ForeignKey(
        PromoCode,
        on_delete=models.CASCADE,
        related_name='usages',
        verbose_name=_('Промокод')
    )
    group = models.ForeignKey(
        PromoCodeGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='usages',
        verbose_name=_('Група')
    )
    order = models.ForeignKey(
        'orders.Order',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='promo_usages',
        verbose_name=_('Замовлення')
    )
    used_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Використано'))

    class Meta:
        verbose_name = _('Використання промокоду')
        verbose_name_plural = _('Використання промокодів')
        ordering = ['-used_at']
        indexes = [
            models.Index(fields=['user', 'promo_code'], name='idx_usage_user_promo'),
            models.Index(fields=['user', 'group'], name='idx_usage_user_group'),
            models.Index(fields=['-used_at'], name='idx_usage_date'),
        ]

    def __str__(self):
        return f'{self.user.username} - {self.promo_code.code} ({self.used_at.strftime("%Y-%m-%d %H:%M")})'


class UserPromoCode(models.Model):
    """Промокоди, видані конкретному користувачу (напр., за опитування)."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='promo_grants',
        verbose_name=_('Користувач')
    )
    promo_code = models.ForeignKey(
        PromoCode,
        on_delete=models.CASCADE,
        related_name='user_grants',
        verbose_name=_('Промокод')
    )
    survey_key = models.CharField(max_length=100, verbose_name=_('Ключ опитування'))
    source = models.CharField(max_length=50, default='survey', verbose_name=_('Джерело'))
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Діє до'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Створено'))

    class Meta:
        verbose_name = _('Промокод користувача')
        verbose_name_plural = _('Промокоди користувача')
        unique_together = [('user', 'survey_key')]
        indexes = [
            models.Index(fields=['user', 'survey_key'], name='idx_user_survey_promo'),
            models.Index(fields=['user', 'created_at'], name='idx_user_promo_created'),
        ]

    def __str__(self):
        return f'{self.user.username} - {self.promo_code.code} ({self.survey_key})'


class SurveySession(models.Model):
    """Сесія проходження опитування користувачем."""
    STATUS_CHOICES = [
        ('in_progress', _('В процесі')),
        ('completed', _('Завершено')),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='survey_sessions',
        verbose_name='Користувач'
    )
    survey_key = models.CharField(max_length=100, verbose_name='Ключ опитування')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='in_progress')
    answers = models.JSONField(default=dict, blank=True)
    history = models.JSONField(default=list, blank=True)
    current_question_id = models.CharField(max_length=100, blank=True, null=True)
    back_used = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    module_order = models.JSONField(default=list, blank=True)

    device_type = models.CharField(max_length=20, blank=True, null=True)
    user_agent = models.TextField(blank=True, null=True)

    started_at = models.DateTimeField(auto_now_add=True)
    last_activity_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    awarded_promocode = models.ForeignKey(
        PromoCode,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='survey_sessions',
        verbose_name='Промокод'
    )

    report_status = models.CharField(max_length=20, blank=True, null=True)
    report_file_path = models.CharField(max_length=512, blank=True, null=True)
    last_report_version = models.PositiveIntegerField(default=0)
    last_report_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Сесія опитування'
        verbose_name_plural = 'Сесії опитувань'
        unique_together = [('user', 'survey_key')]
        indexes = [
            models.Index(fields=['survey_key', 'status'], name='idx_survey_status'),
            models.Index(fields=['user', 'survey_key'], name='idx_survey_user_key'),
            models.Index(fields=['last_activity_at'], name='idx_survey_last_activity'),
        ]

    def __str__(self):
        return f'{self.user.username} - {self.survey_key} ({self.status})'


class OfflineStore(models.Model):
    """Модель для оффлайн магазинов"""
    name = models.CharField(max_length=200, verbose_name='Назва магазину')
    address = models.TextField(verbose_name='Адреса')
    phone = models.CharField(max_length=20, blank=True, null=True, verbose_name='Телефон')
    email = models.EmailField(blank=True, null=True, verbose_name='Email')
    working_hours = models.CharField(max_length=100, blank=True, null=True, verbose_name='Робочі години')
    description = models.TextField(blank=True, null=True, verbose_name='Опис')
    is_active = models.BooleanField(default=True, verbose_name='Активний')
    order = models.PositiveIntegerField(default=0, verbose_name='Порядок відображення')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Створено')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Оновлено')

    class Meta:
        verbose_name = 'Оффлайн магазин'
        verbose_name_plural = 'Оффлайн магазини'
        ordering = ['order', 'name']
        indexes = [
            models.Index(fields=['is_active', 'order'], name='idx_store_active_order'),
        ]

    def __str__(self):
        return self.name


# ===== Лёгкая аналитика посещений =====
class SiteSession(models.Model):
    """
    Агрегированная сессионная метрика по посетителю.
    Используем session_key как идентификатор уникального визита (для анонимов),
    а для авторизованных — связываем с пользователем.
    """
    session_key = models.CharField(max_length=40, unique=True, db_index=True)
    visitor_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, null=True)
    is_bot = models.BooleanField(default=False, db_index=True)
    first_seen = models.DateTimeField(auto_now_add=True, db_index=True)
    last_seen = models.DateTimeField(auto_now=True, db_index=True)
    last_path = models.CharField(max_length=512, blank=True)
    pageviews = models.PositiveIntegerField(default=0)
    first_touch_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-last_seen']
        indexes = [
            models.Index(fields=['is_bot', '-last_seen'], name='idx_session_bot_seen'),
        ]

    def __str__(self) -> str:
        return f"{self.session_key} ({'bot' if self.is_bot else 'user'})"

    def get_utm_session(self):
        """Возвращает связанную UTM сессию"""
        return getattr(self, 'utm_data', None)

    def get_utm_source(self):
        """Возвращает utm_source из связанной UTM сессии"""
        utm = self.get_utm_session()
        return utm.utm_source if utm else None

    def get_utm_campaign(self):
        """Возвращает utm_campaign из связанной UTM сессии"""
        utm = self.get_utm_session()
        return utm.utm_campaign if utm else None


class PageView(models.Model):
    """Запись отдельного просмотра страницы"""
    session = models.ForeignKey(SiteSession, on_delete=models.CASCADE, related_name='views')
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    path = models.CharField(max_length=512)
    referrer = models.CharField(max_length=512, blank=True)
    when = models.DateTimeField(auto_now_add=True, db_index=True)
    is_bot = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ['-when']
        indexes = [
            models.Index(fields=['is_bot', '-when'], name='idx_pageview_bot_when'),
        ]

    def __str__(self) -> str:
        return f"{self.path} @ {self.when}"


# ===== Модели для управления оффлайн магазинами =====

class StoreProduct(models.Model):
    """Товар в оффлайн магазине"""
    store = models.ForeignKey(OfflineStore, on_delete=models.CASCADE, related_name='store_products', verbose_name='Магазин')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name='Товар')
    color = models.ForeignKey('productcolors.Color', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Колір')
    size = models.CharField(max_length=10, blank=True, null=True, verbose_name='Розмір')
    quantity = models.PositiveIntegerField(default=1, verbose_name='Кількість')
    cost_price = models.PositiveIntegerField(verbose_name='Собівартість (грн)')
    selling_price = models.PositiveIntegerField(verbose_name='Ціна продажу (грн)')
    is_active = models.BooleanField(default=True, verbose_name='Активний')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Створено')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Оновлено')

    class Meta:
        verbose_name = 'Товар в магазині'
        verbose_name_plural = 'Товари в магазинах'
        ordering = ['-created_at']
        unique_together = [['store', 'product', 'size', 'color']]

    def __str__(self):
        return f"{self.product.title} - {self.store.name}"

    @property
    def margin(self):
        """Маржа товара"""
        return self.selling_price - self.cost_price

    @property
    def total_cost(self):
        return (self.cost_price or 0) * (self.quantity or 0)

    @property
    def total_revenue(self):
        return (self.selling_price or 0) * (self.quantity or 0)

    @property
    def total_margin(self):
        return self.total_revenue - self.total_cost


class StoreOrder(models.Model):
    """Заказ в оффлайн магазине"""
    STATUS_CHOICES = [
        ('draft', 'Чернетка'),
        ('pending', 'В обробці'),
        ('confirmed', 'Підтверджено'),
        ('completed', 'Виконано'),
        ('cancelled', 'Скасовано'),
    ]

    store = models.ForeignKey(OfflineStore, on_delete=models.CASCADE, related_name='store_orders', verbose_name='Магазин')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', verbose_name='Статус')
    notes = models.TextField(blank=True, null=True, verbose_name='Примітки')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Створено')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Оновлено')

    class Meta:
        verbose_name = 'Заказ магазина'
        verbose_name_plural = 'Заказы магазинов'
        ordering = ['-created_at']

    def __str__(self):
        return f"Замовлення #{self.id} - {self.store.name}"


class StoreOrderItem(models.Model):
    """Элемент заказа в оффлайн магазине"""
    order = models.ForeignKey(StoreOrder, on_delete=models.CASCADE, related_name='order_items', verbose_name='Заказ')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name='Товар')
    color = models.ForeignKey('productcolors.Color', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Колір')
    size = models.CharField(max_length=10, blank=True, null=True, verbose_name='Розмір')
    quantity = models.PositiveIntegerField(default=1, verbose_name='Кількість')
    cost_price = models.PositiveIntegerField(verbose_name='Собівартість (грн)')
    selling_price = models.PositiveIntegerField(verbose_name='Ціна продажу (грн)')

    class Meta:
        verbose_name = 'Товар в заказі'
        verbose_name_plural = 'Товари в заказах'
        ordering = ['id']

    def __str__(self):
        return f"{self.product.title} - {self.order}"

    @property
    def total_price(self):
        """Общая цена элемента заказа"""
        return self.selling_price * self.quantity


class StoreSale(models.Model):
    """Факт продажу товару з офлайн-магазину."""

    store = models.ForeignKey(
        OfflineStore,
        on_delete=models.CASCADE,
        related_name='store_sales',
        verbose_name='Магазин'
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='store_sales',
        verbose_name='Товар'
    )
    color = models.ForeignKey(
        'productcolors.Color',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='Колір'
    )
    size = models.CharField(max_length=10, blank=True, null=True, verbose_name='Розмір')
    quantity = models.PositiveIntegerField(default=1, verbose_name='Кількість')
    cost_price = models.PositiveIntegerField(verbose_name='Собівартість (грн)')
    selling_price = models.PositiveIntegerField(verbose_name='Ціна продажу (грн)')
    sold_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата продажу')
    source_store_product = models.ForeignKey(
        StoreProduct,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sales_history',
        verbose_name='Джерело запису'
    )
    notes = models.CharField(max_length=255, blank=True, null=True, verbose_name='Примітки')

    class Meta:
        verbose_name = 'Проданий товар'
        verbose_name_plural = 'Продані товари'
        ordering = ['-sold_at']

    def __str__(self):
        return f"{self.product.title} — {self.store.name} ({self.quantity} шт.)"

    @property
    def margin(self):
        return (self.selling_price - self.cost_price) * self.quantity

    @property
    def total_revenue(self):
        return self.selling_price * self.quantity

    @property
    def total_cost(self):
        return self.cost_price * self.quantity


class StoreInvoice(models.Model):
    """Накладна магазина"""
    store = models.ForeignKey(OfflineStore, on_delete=models.CASCADE, related_name='invoices', verbose_name='Магазин')
    order = models.ForeignKey(StoreOrder, on_delete=models.CASCADE, related_name='invoices', blank=True, null=True, verbose_name='Заказ')
    file_name = models.CharField(max_length=255, default='', verbose_name='Назва файлу')
    file_path = models.CharField(max_length=500, default='', verbose_name='Шлях до файлу')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Створено')

    class Meta:
        verbose_name = 'Накладна'
        verbose_name_plural = 'Накладні'
        ordering = ['-created_at']

    def __str__(self):
        return f"Накладна #{self.id} - {self.store.name}"


# ===== UTM Tracking & Analytics =====

class UTMSession(models.Model):
    """
    Хранит UTM-параметры для сессии пользователя.
    Связывается с SiteSession для отслеживания всего пути пользователя.
    Включает все необходимые данные для детальной аналитики рекламных кампаний.
    """
    session = models.OneToOneField(
        SiteSession,
        on_delete=models.CASCADE,
        related_name='utm_data',
        null=True,
        blank=True
    )
    session_key = models.CharField(max_length=40, db_index=True, unique=True)
    visitor_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)

    # UTM параметры (стандартные)
    utm_source = models.CharField(max_length=255, db_index=True, blank=True, null=True, verbose_name='Джерело (utm_source)')
    utm_medium = models.CharField(max_length=255, db_index=True, blank=True, null=True, verbose_name='Канал (utm_medium)')
    utm_campaign = models.CharField(max_length=255, db_index=True, blank=True, null=True, verbose_name='Кампанія (utm_campaign)')
    utm_content = models.CharField(max_length=255, db_index=True, blank=True, null=True, verbose_name='Контент/Креатив (utm_content)')
    utm_term = models.CharField(max_length=255, db_index=True, blank=True, null=True, verbose_name='Ключове слово (utm_term)')

    # Платформенные идентификаторы
    fbclid = models.CharField(max_length=255, db_index=True, blank=True, null=True, verbose_name='Facebook Click ID')
    gclid = models.CharField(max_length=255, db_index=True, blank=True, null=True, verbose_name='Google Click ID')
    ttclid = models.CharField(max_length=255, db_index=True, blank=True, null=True, verbose_name='TikTok Click ID')
    fbc = models.CharField(max_length=255, blank=True, null=True, verbose_name='Facebook Click Cookie')
    fbp = models.CharField(max_length=255, blank=True, null=True, verbose_name='Facebook Browser Cookie')

    # Геолокация (определяется по IP)
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True, verbose_name='IP-адреса')
    country = models.CharField(max_length=2, blank=True, null=True, db_index=True, verbose_name='Код країни (ISO 3166-1)')
    country_name = models.CharField(max_length=100, blank=True, null=True, verbose_name='Назва країни')
    city = models.CharField(max_length=100, blank=True, null=True, db_index=True, verbose_name='Місто')
    region = models.CharField(max_length=100, blank=True, null=True, verbose_name='Регіон/Область')
    timezone = models.CharField(max_length=50, blank=True, null=True, verbose_name='Часовий пояс')

    # Устройство и браузер
    device_type = models.CharField(max_length=20, blank=True, null=True, db_index=True, choices=[
        ('desktop', 'Desktop'),
        ('mobile', 'Mobile'),
        ('tablet', 'Tablet'),
        ('unknown', 'Unknown'),
    ], verbose_name='Тип пристрою')
    device_brand = models.CharField(max_length=50, blank=True, null=True, verbose_name='Бренд пристрою')
    device_model = models.CharField(max_length=100, blank=True, null=True, verbose_name='Модель пристрою')
    os_name = models.CharField(max_length=50, blank=True, null=True, db_index=True, verbose_name='Операційна система')
    os_version = models.CharField(max_length=50, blank=True, null=True, verbose_name='Версія ОС')
    browser_name = models.CharField(max_length=50, blank=True, null=True, db_index=True, verbose_name='Браузер')
    browser_version = models.CharField(max_length=50, blank=True, null=True, verbose_name='Версія браузера')
    user_agent = models.TextField(blank=True, null=True, verbose_name='User Agent')

    # Дополнительные данные
    referrer = models.URLField(max_length=512, blank=True, null=True, verbose_name='Реферер')
    landing_page = models.CharField(max_length=512, blank=True, null=True, verbose_name='Посадкова сторінка')

    # Отслеживание повторных визитов
    visit_count = models.PositiveIntegerField(default=1, db_index=True, verbose_name='Кількість візитів')
    is_first_visit = models.BooleanField(default=True, db_index=True, verbose_name='Перший візит')
    is_returning_visitor = models.BooleanField(default=False, db_index=True, verbose_name='Постійний відвідувач')

    # Регистрация пользователя
    user_registered = models.BooleanField(default=False, db_index=True, verbose_name='Користувач зареєструвався')
    user_registered_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата реєстрації')

    # Метаданные
    first_seen = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Перший візит')
    last_seen = models.DateTimeField(auto_now=True, db_index=True, verbose_name='Останній візит')

    # Флаги конверсии
    is_converted = models.BooleanField(default=False, db_index=True, verbose_name='Конверсія відбулася')
    converted_at = models.DateTimeField(null=True, blank=True, verbose_name='Дата конверсії')
    conversion_type = models.CharField(max_length=20, blank=True, null=True, choices=[
        ('lead', 'Лід (передплата)'),
        ('purchase', 'Покупка'),
    ], db_index=True, verbose_name='Тип конверсії')

    class Meta:
        ordering = ['-first_seen']
        indexes = [
            models.Index(fields=['utm_source', 'utm_medium', 'utm_campaign'], name='idx_utm_source_medium_campaign'),
            models.Index(fields=['-first_seen'], name='idx_utm_first_seen'),
            models.Index(fields=['is_converted', '-converted_at'], name='idx_utm_converted'),
            models.Index(fields=['country', 'city'], name='idx_utm_geo'),
            models.Index(fields=['device_type', 'os_name'], name='idx_utm_device'),
            models.Index(fields=['is_returning_visitor', '-first_seen'], name='idx_utm_returning'),
            models.Index(fields=['user_registered', '-first_seen'], name='idx_utm_registered'),
        ]
        verbose_name = 'UTM Сесія'
        verbose_name_plural = 'UTM Сесії'

    def __str__(self):
        return f"UTM: {self.utm_source or 'direct'}/{self.utm_medium or 'none'} - {self.utm_campaign or 'N/A'}"

    @property
    def utm_string(self):
        """Возвращает строковое представление UTM-параметров"""
        parts = []
        if self.utm_source:
            parts.append(f"source={self.utm_source}")
        if self.utm_medium:
            parts.append(f"medium={self.utm_medium}")
        if self.utm_campaign:
            parts.append(f"campaign={self.utm_campaign}")
        if self.utm_content:
            parts.append(f"content={self.utm_content}")
        if self.utm_term:
            parts.append(f"term={self.utm_term}")
        return "&".join(parts) if parts else "direct"

    def mark_as_converted(self, conversion_type='purchase', converted_at=None):
        """Mark the strongest known conversion without downgrading purchase.

        A prepayment can first create a ``lead`` and later become a confirmed
        ``purchase``. That second transition must upgrade the funnel stage and
        use the purchase time (including during a historical reconciliation).
        """
        db_alias = self._state.db or 'default'
        with transaction.atomic(using=db_alias):
            current = (
                type(self)._default_manager.using(db_alias)
                .select_for_update()
                .get(pk=self.pk)
            )
            should_upgrade = (
                conversion_type == 'purchase'
                and current.conversion_type != 'purchase'
            )
            if not current.is_converted or should_upgrade:
                current.is_converted = True
                current.converted_at = converted_at or timezone.now()
                current.conversion_type = conversion_type
                current.save(
                    using=db_alias,
                    update_fields=['is_converted', 'converted_at', 'conversion_type'],
                )

            self.is_converted = current.is_converted
            self.converted_at = current.converted_at
            self.conversion_type = current.conversion_type

    # W2-10/AN-036: «визит» = сессионное окно 30 минут (стандарт GA).
    VISIT_WINDOW_MINUTES = 30

    def increment_visit(self):
        """
        Увеличивает счетчик визитов.

        W2-10 (AN-036): до фикса вызывался на КАЖДЫЙ pageview →
        SELECT+UPDATE на каждый хит и visit_count = число просмотров,
        а не визитов. Теперь новый «визит» засчитывается только если
        с last_seen прошло > VISIT_WINDOW_MINUTES; внутри окна
        обновляется только last_seen (и то не чаще раза в минуту,
        чтобы не писать в БД на каждый хит).
        """
        now = timezone.now()
        if self.last_seen and (now - self.last_seen) <= timedelta(minutes=self.VISIT_WINDOW_MINUTES):
            # Внутри визит-окна: только продлеваем last_seen, максимум раз в минуту
            if (now - self.last_seen) >= timedelta(minutes=1):
                self.last_seen = now
                self.save(update_fields=['last_seen'])
            return
        self.visit_count = F('visit_count') + 1
        self.is_first_visit = False
        self.is_returning_visitor = True
        self.last_seen = now
        self.save(update_fields=['visit_count', 'is_first_visit', 'is_returning_visitor', 'last_seen'])

    def mark_user_registered(self):
        """Отмечает, что пользователь зарегистрировался"""
        if not self.user_registered:
            self.user_registered = True
            self.user_registered_at = timezone.now()
            self.save(update_fields=['user_registered', 'user_registered_at'])


class UserAction(models.Model):
    """
    Отслеживает действия пользователей на сайте.
    Связывается с UTMSession для анализа эффективности рекламы.
    Позволяет построить полную воронку конверсий.
    """
    ACTION_TYPES = [
        ('page_view', 'Перегляд сторінки'),
        ('product_view', 'Перегляд товару'),
        ('add_to_cart', 'Додано в кошик'),
        ('remove_from_cart', 'Видалено з кошика'),
        ('initiate_checkout', 'Початок оформлення'),
        ('lead', 'Лід (передплата)'),
        ('purchase', 'Покупка'),
        ('search', 'Пошук'),
        ('coupon_apply', 'Застосування промокоду'),
        ('custom_print_start', 'Кастомний принт: старт'),
        ('custom_print_step_enter', 'Кастомний принт: вхід у крок'),
        ('custom_print_step_complete', 'Кастомний принт: завершення кроку'),
        ('custom_print_add_to_cart', 'Кастомний принт: додано в кошик'),
        ('custom_print_send_to_manager', 'Кастомний принт: відправлено менеджеру'),
        ('custom_print_safe_exit', 'Кастомний принт: safe exit'),
        ('custom_print_moderation_result', 'Кастомний принт: результат модерації'),
        ('survey_start', 'Опитування: старт'),
        ('survey_answer', 'Опитування: відповідь'),
        ('survey_back', 'Опитування: назад'),
        ('survey_skip', 'Опитування: пропуск'),
        ('survey_close', 'Опитування: закриття'),
        ('survey_complete', 'Опитування: завершення'),
        ('survey_dismiss', 'Опитування: приховано з головної'),
        ('click', 'Клік'),
        ('scroll', 'Прокрутка'),
        ('time_on_page', 'Час на сторінці'),
    ]

    utm_session = models.ForeignKey(
        UTMSession,
        on_delete=models.CASCADE,
        related_name='actions',
        null=True,
        blank=True,
        verbose_name='UTM Сесія'
    )
    site_session = models.ForeignKey(
        SiteSession,
        on_delete=models.CASCADE,
        related_name='user_actions',
        null=True,
        blank=True,
        verbose_name='Сесія сайту'
    )
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='Користувач'
    )

    action_type = models.CharField(max_length=50, choices=ACTION_TYPES, db_index=True, verbose_name='Тип дії')

    # Данные действия
    page_path = models.CharField(max_length=512, blank=True, null=True, verbose_name='Шлях сторінки')
    product_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name='ID товару')
    product_name = models.CharField(max_length=255, blank=True, null=True, verbose_name='Назва товару')
    cart_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name='Сума кошика')
    order_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name='ID замовлення')
    order_number = models.CharField(max_length=20, blank=True, null=True, verbose_name='Номер замовлення')

    # Метаданные
    metadata = models.JSONField(default=dict, blank=True, verbose_name='Метадані')
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Час')

    # Баллы за действие
    points_earned = models.IntegerField(default=0, verbose_name='Нараховані бали')

    class Meta:
        ordering = ['-timestamp']
        constraints = [
            models.UniqueConstraint(
                fields=['action_type', 'order_id'],
                name='uniq_user_action_type_order',
            ),
        ]
        indexes = [
            models.Index(fields=['action_type', '-timestamp'], name='idx_action_type_time'),
            models.Index(fields=['utm_session', 'action_type'], name='idx_action_utm_type'),
            models.Index(fields=['product_id', '-timestamp'], name='idx_action_product'),
            models.Index(fields=['order_id'], name='idx_action_order'),
            # W2-10/DB-002: ускоряет воронку по сессии и дедуп-чек W2-4
            models.Index(fields=['site_session', 'action_type'], name='idx_action_site_type'),
        ]
        verbose_name = 'Дія користувача'
        verbose_name_plural = 'Дії користувачів'

    def __str__(self):
        return f"{self.get_action_type_display()} - {self.timestamp}"


class WebPushDeviceSubscription(models.Model):
    class DeviceType(models.TextChoices):
        DESKTOP = "desktop", _("Desktop")
        MOBILE = "mobile", _("Mobile")
        TABLET = "tablet", _("Tablet")
        UNKNOWN = "unknown", _("Unknown")

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="web_push_subscriptions",
        verbose_name="Користувач",
    )
    installation_id = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        verbose_name="ID браузерної інсталяції",
    )
    endpoint = models.URLField(
        max_length=1000,
        unique=True,
        verbose_name="Push endpoint",
    )
    auth_key = models.CharField(max_length=255, verbose_name="Ключ auth")
    p256dh_key = models.CharField(max_length=255, verbose_name="Ключ p256dh")
    language = models.CharField(max_length=16, blank=True, verbose_name="Мова браузера")
    timezone = models.CharField(max_length=64, blank=True, verbose_name="Часовий пояс")
    browser_family = models.CharField(max_length=64, blank=True, verbose_name="Браузер")
    operating_system = models.CharField(max_length=64, blank=True, verbose_name="ОС")
    device_type = models.CharField(
        max_length=20,
        choices=DeviceType.choices,
        default=DeviceType.UNKNOWN,
        db_index=True,
        verbose_name="Тип пристрою",
    )
    user_agent = models.TextField(blank=True, verbose_name="User Agent")
    last_seen_path = models.CharField(max_length=512, blank=True, verbose_name="Остання сторінка")
    metadata = models.JSONField(default=dict, blank=True, verbose_name="Метадані")
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="Активна")
    subscribed_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Підписано")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")
    last_seen_at = models.DateTimeField(auto_now=True, db_index=True, verbose_name="Остання активність")
    unsubscribed_at = models.DateTimeField(null=True, blank=True, verbose_name="Відписано")
    last_success_at = models.DateTimeField(null=True, blank=True, verbose_name="Остання успішна доставка")
    last_failure_at = models.DateTimeField(null=True, blank=True, verbose_name="Остання помилка")
    failure_count = models.PositiveIntegerField(default=0, verbose_name="Кількість помилок")
    last_error = models.CharField(max_length=255, blank=True, verbose_name="Остання помилка")

    class Meta:
        verbose_name = "Web Push підписка"
        verbose_name_plural = "Web Push підписки"
        ordering = ["-last_seen_at"]
        indexes = [
            models.Index(fields=["is_active", "-last_seen_at"], name="idx_push_sub_active_seen"),
            models.Index(fields=["installation_id", "is_active"], name="idx_push_subscription_install"),
            models.Index(fields=["user", "is_active"], name="idx_push_subscription_user"),
            models.Index(fields=["device_type", "is_active"], name="idx_push_subscription_device"),
        ]

    def __str__(self):
        identity = self.installation_id or self.endpoint
        return f"Push subscription {identity}"

    def mark_inactive(self, error_message=""):
        now = timezone.now()
        update_fields = [
            "is_active",
            "unsubscribed_at",
            "last_failure_at",
            "last_error",
            "updated_at",
        ]
        self.is_active = False
        self.unsubscribed_at = now
        self.last_failure_at = now
        self.last_error = (error_message or "")[:255]
        self.save(update_fields=update_fields)

    def mark_delivery_success(self):
        now = timezone.now()
        self.is_active = True
        self.last_success_at = now
        self.failure_count = 0
        self.last_error = ""
        self.save(update_fields=["is_active", "last_success_at", "failure_count", "last_error", "updated_at"])

    def register_failure(self, error_message=""):
        self.failure_count = (self.failure_count or 0) + 1
        self.last_failure_at = timezone.now()
        self.last_error = (error_message or "")[:255]
        self.save(update_fields=["failure_count", "last_failure_at", "last_error", "updated_at"])


class PushNotificationCampaign(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Чернетка")
        SENDING = "sending", _("Надсилання")
        SENT = "sent", _("Надіслано")
        PARTIAL = "partial", _("Частково доставлено")
        FAILED = "failed", _("Помилка")

    class AudienceMode(models.TextChoices):
        ALL_ACTIVE = "all_active", _("Усі активні підписки")

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="push_campaigns",
        verbose_name="Створив",
    )
    title = models.CharField(max_length=120, verbose_name="Заголовок")
    body = models.CharField(max_length=240, verbose_name="Текст повідомлення")
    target_url = models.CharField(max_length=500, verbose_name="Посилання переходу")
    image = models.ImageField(
        upload_to="push_notifications/",
        blank=True,
        null=True,
        verbose_name="Зображення сповіщення",
    )
    audience_mode = models.CharField(
        max_length=20,
        choices=AudienceMode.choices,
        default=AudienceMode.ALL_ACTIVE,
        verbose_name="Аудиторія",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
        verbose_name="Статус",
    )
    last_error = models.CharField(max_length=255, blank=True, verbose_name="Остання помилка")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")
    sent_started_at = models.DateTimeField(null=True, blank=True, verbose_name="Початок надсилання")
    sent_finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Завершення надсилання")
    targeted_count = models.PositiveIntegerField(default=0, verbose_name="Заплановано")
    sent_count = models.PositiveIntegerField(default=0, verbose_name="Прийнято push-сервісом")
    displayed_count = models.PositiveIntegerField(default=0, verbose_name="Показано")
    clicked_count = models.PositiveIntegerField(default=0, verbose_name="Кліки")
    closed_count = models.PositiveIntegerField(default=0, verbose_name="Закрито")
    failed_count = models.PositiveIntegerField(default=0, verbose_name="Помилки")

    class Meta:
        verbose_name = "Push кампанія"
        verbose_name_plural = "Push кампанії"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="idx_push_campaign_status"),
        ]

    def __str__(self):
        return f"Push #{self.pk or 'new'} — {self.title}"

    @property
    def display_rate(self):
        if not self.sent_count:
            return Decimal("0")
        return (
            Decimal(self.displayed_count or 0) * Decimal("100") / Decimal(self.sent_count)
        ).quantize(Decimal("0.01"))

    @property
    def click_rate(self):
        if not self.displayed_count:
            return Decimal("0")
        return (
            Decimal(self.clicked_count or 0) * Decimal("100") / Decimal(self.displayed_count)
        ).quantize(Decimal("0.01"))

    def sync_delivery_metrics(self):
        deliveries = self.deliveries.all()
        self.targeted_count = deliveries.count()
        self.sent_count = deliveries.filter(sent_at__isnull=False).count()
        self.displayed_count = deliveries.filter(displayed_at__isnull=False).count()
        self.clicked_count = deliveries.filter(clicked_at__isnull=False).count()
        self.closed_count = deliveries.filter(closed_at__isnull=False).count()
        self.failed_count = deliveries.filter(failed_at__isnull=False).count()

        if self.targeted_count == 0:
            self.status = self.Status.DRAFT
        elif self.failed_count and not self.sent_count:
            self.status = self.Status.FAILED
        elif self.failed_count:
            self.status = self.Status.PARTIAL
        elif self.sent_count:
            self.status = self.Status.SENT

        self.save(
            update_fields=[
                "targeted_count",
                "sent_count",
                "displayed_count",
                "clicked_count",
                "closed_count",
                "failed_count",
                "status",
                "updated_at",
            ]
        )


class PushNotificationDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Очікує")
        SENT = "sent", _("Надіслано")
        DISPLAYED = "displayed", _("Показано")
        CLICKED = "clicked", _("Клік")
        CLOSED = "closed", _("Закрито")
        FAILED = "failed", _("Помилка")
        EXPIRED = "expired", _("Недійсна підписка")

    campaign = models.ForeignKey(
        PushNotificationCampaign,
        on_delete=models.CASCADE,
        related_name="deliveries",
        verbose_name="Кампанія",
    )
    subscription = models.ForeignKey(
        WebPushDeviceSubscription,
        on_delete=models.CASCADE,
        related_name="deliveries",
        verbose_name="Підписка",
    )
    event_token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        verbose_name="Токен події",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name="Статус",
    )
    push_service_status_code = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="HTTP статус push-сервісу",
    )
    error_code = models.CharField(max_length=64, blank=True, verbose_name="Код помилки")
    error_message = models.CharField(max_length=255, blank=True, verbose_name="Повідомлення помилки")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name="Надіслано")
    displayed_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name="Показано")
    clicked_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name="Клік")
    closed_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name="Закрито")
    failed_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name="Помилка")

    class Meta:
        verbose_name = "Push доставка"
        verbose_name_plural = "Push доставки"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "subscription"],
                name="uniq_push_delivery_campaign_subscription",
            ),
        ]
        indexes = [
            models.Index(fields=["campaign", "status"], name="idx_push_deliv_cmp_status"),
            models.Index(fields=["subscription", "-created_at"], name="idx_push_delivery_subscription"),
        ]

    def __str__(self):
        return f"Delivery #{self.pk} for campaign #{self.campaign_id}"


# ===== Phase 10 — SEO blocks for category pages =====
#
# Структурированные блоки на странице категории (по образцу retromagaz/aac):
# top_filters / top_queries / top_cards / top_menu / best_prices.
# Длинный SEO-текст хранится в Category.description (уже существует);
# H2-заголовок над ним — в Category.seo_text_title (Phase 10 добавляет).

class CategorySeoBlock(models.Model):
    """Один блок (рейка) на странице категории."""

    BLOCK_TYPES = (
        ("top_filters", "Топ фільтри"),
        ("top_queries", "Топ запити"),
        ("top_cards", "Топ карточки"),
        ("top_menu", "Розділи категорії"),
        ("best_prices", "Найкращі ціни"),
    )

    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="seo_blocks",
        verbose_name="Категорія",
    )
    block_type = models.CharField(
        max_length=20,
        choices=BLOCK_TYPES,
        verbose_name="Тип блоку",
    )
    title = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Заголовок (H2 / H3)",
    )
    is_active = models.BooleanField(default=True, verbose_name="Активний")
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "SEO-блок категорії"
        verbose_name_plural = "SEO-блоки категорій"
        indexes = [
            models.Index(fields=["category", "is_active", "order"],
                         name="idx_seoblock_cat_act_order"),
            models.Index(fields=["block_type"], name="idx_seoblock_type"),
        ]

    def __str__(self):
        return f"{self.category.name} · {self.get_block_type_display()}"


class CategorySeoBlockItem(models.Model):
    """Один элемент внутри SEO-блока: chip / ссылка / закреплённая карточка."""

    block = models.ForeignKey(
        CategorySeoBlock,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="Блок",
    )
    label = models.CharField(max_length=200, verbose_name="Текст")
    url = models.CharField(
        max_length=500,
        blank=True,
        verbose_name="URL (відносний або абсолютний)",
        help_text="Може бути порожнім для top_cards — тоді використовується extra.product_id.",
    )
    extra = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Додаткові дані",
        help_text="Наприклад {'product_id': 42} для top_cards або {'price': 599} для best_prices.",
    )
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Елемент SEO-блоку"
        verbose_name_plural = "Елементи SEO-блоків"
        indexes = [
            models.Index(fields=["block", "order"], name="idx_seoblock_item_order"),
        ]

    def __str__(self):
        return f"{self.block_id} · {self.label}"


# ===== Phase 19h (2026-05-10) — admin-editable colour-aware SEO copy =====
#
# Curated copy for /catalog/ root, /catalog/?color=<slug> and
# /catalog/<cat>/?color=<slug> lives in ``services/color_seo_copy.py``
# as a hand-written palette. To let the team tune the wording without
# a code deploy, this model captures per-(scope, color, category)
# overrides that the service consults *before* falling back to the
# curated palette.

class CatalogColorSeoOverride(models.Model):
    """Override for the colour-aware SEO copy on catalog screens."""

    SCOPE_GENERAL = "general"     # /catalog/ (no colour, no category)
    SCOPE_BRAND = "brand"         # /catalog/?color=<slug>
    SCOPE_CATEGORY = "category"   # /catalog/<cat>/?color=<slug>

    SCOPE_CHOICES = (
        (SCOPE_GENERAL, "Загальний каталог /catalog/"),
        (SCOPE_BRAND, "Каталог /catalog/?color=<колір>"),
        (SCOPE_CATEGORY, "Категорія /catalog/<cat>/?color=<колір>"),
    )

    scope = models.CharField(
        max_length=12,
        choices=SCOPE_CHOICES,
        verbose_name="Контекст",
    )
    color_slug = models.SlugField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="Slug кольору",
        help_text=(
            "Заповніть для контекстів «brand»/«category» (наприклад "
            "«black», «coyote»). Залиште порожнім для «general»."
        ),
    )
    category = models.ForeignKey(
        Category,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="color_seo_overrides",
        verbose_name="Категорія",
        help_text="Заповніть для контексту «category»; залиште порожнім для «brand»/«general».",
    )
    h2 = models.CharField(
        max_length=300,
        blank=True,
        verbose_name="Заголовок (H2)",
        help_text="Якщо порожньо — використовується курований заголовок з коду.",
    )
    body_html = models.TextField(
        blank=True,
        verbose_name="HTML-параграфи",
        help_text=(
            "Параграфи у форматі HTML (наприклад '<p>Перший абзац…</p>"
            "<p>Другий…</p>'). Дозволені теги <a>, <strong>, <em>. "
            "Якщо порожньо — використовуються куровані параграфи з коду."
        ),
    )
    queries_json = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Чипи-запити",
        help_text=(
            "JSON-масив об’єктів {label, url, freq}, де freq — 'hf' / "
            "'mf' / 'lf'. Якщо порожньо — використовуються куровані з коду."
        ),
    )
    is_active = models.BooleanField(default=True, verbose_name="Активний")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "SEO-копія каталогу за кольором"
        verbose_name_plural = "SEO-копії каталогу за кольорами"
        constraints = [
            models.UniqueConstraint(
                fields=["scope", "color_slug", "category"],
                name="uq_color_seo_override_scope_slug_cat",
            ),
        ]
        indexes = [
            models.Index(fields=["scope", "color_slug"], name="idx_clrseo_scope_slug"),
            models.Index(fields=["category"], name="idx_clrseo_category"),
            models.Index(fields=["is_active"], name="idx_clrseo_active"),
        ]

    def __str__(self):  # pragma: no cover - admin display only
        bits = [self.get_scope_display()]
        if self.color_slug:
            bits.append(self.color_slug)
        if self.category_id:
            bits.append(f"cat#{self.category_id}")
        return " · ".join(bits)


# ===== Аналітичні виключення (внутрішні IP, тестові акаунти, бот-агенти) =====

class AnalyticsExclusion(models.Model):
    """
    Дозволяє виключити трафік від певних IP / користувачів / visitor_id /
    user-agent-патернів / шляхів зі статистики адмін-панелі.

    На відміну від `analytics_noise`, цей список редагується з UI і
    застосовується як до запису нових сесій, так і до фільтрації
    існуючих даних під час побудови віджетів.
    """

    class Kind(models.TextChoices):
        IP = "ip", "IP-адреса"
        USER = "user", "Користувач (ID)"
        VISITOR = "visitor", "Visitor cookie (twc_vid)"
        USER_AGENT = "user_agent", "User-Agent (substring)"
        PATH = "path", "Шлях (startswith)"

    kind = models.CharField(
        max_length=16,
        choices=Kind.choices,
        default=Kind.IP,
        db_index=True,
        verbose_name="Тип",
    )
    value = models.CharField(
        max_length=512,
        verbose_name="Значення",
        help_text=(
            "IP-адреса (192.168.0.1 або CIDR 10.0.0.0/8), user_id, visitor_id, "
            "підрядок User-Agent чи префікс шляху."
        ),
    )
    note = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Коментар",
        help_text="Для чого додано (наприклад: «офіс», «менеджер», «перевірка ботів»).",
    )
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="Активне")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analytics_exclusions",
        verbose_name="Створив",
    )

    class Meta:
        verbose_name = "Аналітичне виключення"
        verbose_name_plural = "Аналітичні виключення"
        ordering = ("-updated_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "value"],
                name="uq_analytics_exclusion_kind_value",
            ),
        ]
        indexes = [
            models.Index(fields=["kind", "is_active"], name="idx_analytics_excl_kind_act"),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin display only
        return f"{self.get_kind_display()}: {self.value}"


# ===== Color × Category landing pages (Phase: color-category-landings) =====
#
# Indexable SEO landing pages for colour × category combinations
# (e.g. /catalog/tshirts/black/). Created from the Django admin by the
# content team, with hand-written editorial copy. Anti-thin guard
# refuses publication when ``editorial_html`` is too short.

class CategoryColorLanding(models.Model):
    """SEO landing page combining a category with a colour.

    Renders at ``/catalog/<cat_slug>/<color_slug>/``. The ``color_slug``
    is auto-derived from ``color.name`` via
    ``productcolors.color_slug_map.english_slug_for_color_name`` so
    landing URLs stay stable even if a colour is renamed.

    A landing returns 404 unless ``is_published=True`` AND the matching
    ``(category, colour)`` slice has at least one published product.
    """

    MIN_EDITORIAL_LENGTH = 800

    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="color_landings",
        verbose_name="Категорія",
    )
    color = models.ForeignKey(
        "productcolors.Color",
        on_delete=models.CASCADE,
        related_name="category_landings",
        verbose_name="Колір",
    )
    color_slug = models.SlugField(
        max_length=64,
        verbose_name="URL slug кольору",
        help_text=(
            "Англійський slug кольору (наприклад 'black' або "
            "'navy-blue'). Заповнюється автоматично з імені кольору."
        ),
    )

    # SEO meta
    seo_title = models.CharField(
        max_length=180,
        verbose_name="SEO Title",
        help_text="<title> сторінки. До 60 символів — оптимум для Google.",
    )
    seo_h1 = models.CharField(
        max_length=180,
        blank=True,
        verbose_name="SEO H1",
        help_text="Якщо порожньо — використовується SEO Title.",
    )
    seo_description = models.CharField(
        max_length=320,
        verbose_name="SEO Description",
        help_text="meta description. 140–160 символів — оптимум.",
    )

    # Content
    editorial_html = models.TextField(
        verbose_name="Editorial copy (HTML)",
        help_text=(
            "Унікальний редакційний текст українською (HTML). "
            "Для публікації потрібно мінімум 800 символів."
        ),
    )
    faq_items = models.JSONField(
        blank=True,
        default=list,
        verbose_name="FAQ Q/A пари",
        help_text=(
            "Список словників: [{'question': '...', 'answer': '...'}, ...]. "
            "Рекомендовано 4–6 пунктів."
        ),
    )

    # Publishing
    is_published = models.BooleanField(default=False, verbose_name="Опубліковано")
    order = models.PositiveIntegerField(default=0, verbose_name="Порядок")

    # Audit
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Створено")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Оновлено")

    class Meta:
        verbose_name = "Color-category landing"
        verbose_name_plural = "Color-category landings"
        ordering = ["category__order", "order", "color_slug"]
        unique_together = (("category", "color_slug"),)
        indexes = [
            models.Index(fields=["is_published"], name="idx_cclanding_published"),
            models.Index(fields=["category", "is_published"], name="idx_cclanding_cat_pub"),
        ]

    def __str__(self):
        cat_name = getattr(self.category, "name", self.category_id) or self.category_id
        return f"{cat_name} / {self.color_slug or self.color_id}"

    # ---- Auto-fill ----

    def _derive_color_slug(self) -> str:
        """Compute the URL slug from the related Colour object.

        Prefers the curated English mapping (so ``Чорний`` → ``black``
        rather than a transliterated ``chornyi``); falls back to a
        slugified hex value when no mapping exists.
        """
        from dtf.utils import build_slug
        from productcolors.color_slug_map import english_slug_for_color_name

        if not self.color_id:
            return ""
        # ``self.color`` may not be loaded yet on a fresh instance.
        try:
            color = self.color
        except Exception:  # pragma: no cover - safety net
            return ""
        name = (getattr(color, "name", "") or "").strip()
        if name:
            mapped = english_slug_for_color_name(name)
            if mapped:
                return mapped
            slugified = build_slug(name, fallback="")
            if slugified:
                return slugified
        primary = (getattr(color, "primary_hex", "") or "").lstrip("#")
        if primary:
            return build_slug(primary, fallback="") or ""
        return ""

    def save(self, *args, **kwargs):
        if not self.color_slug and self.color_id:
            derived = self._derive_color_slug()
            if derived:
                self.color_slug = derived
        super().save(*args, **kwargs)

    # ---- Validation ----

    def full_clean(self, *args, **kwargs):
        """Derive ``color_slug`` before per-field validation runs.

        ``color_slug`` is normally auto-populated in ``save()``, but admin
        forms and ``ModelForm.is_valid()`` invoke ``full_clean()`` first,
        which would otherwise complain about an empty SlugField.
        """
        if not self.color_slug and self.color_id:
            derived = self._derive_color_slug()
            if derived:
                self.color_slug = derived
        return super().full_clean(*args, **kwargs)

    def clean(self):
        """Anti-thin-content guard: published landings must have body copy."""
        from django.core.exceptions import ValidationError

        super().clean()
        if self.is_published:
            length = len((self.editorial_html or "").strip())
            if length < self.MIN_EDITORIAL_LENGTH:
                raise ValidationError({
                    "editorial_html": (
                        "Для публікації потрібно мінімум "
                        f"{self.MIN_EDITORIAL_LENGTH} символів editorial copy "
                        f"(зараз: {length})."
                    ),
                })

    # ---- URL helpers ----

    def get_absolute_url(self) -> str:
        if not self.category_id or not self.color_slug:
            return ""
        return f"/catalog/{self.category.slug}/{self.color_slug}/"

    @property
    def display_h1(self) -> str:
        return (self.seo_h1 or "").strip() or (self.seo_title or "").strip()


# ===== Phase 22 — Google Indexing API audit log =====

class GoogleIndexingSubmission(models.Model):
    """Per-URL audit trail for the Google Indexing API.

    Every notification we send (via ``submit_google_indexing_urls``)
    lands in this table so the admin can see what was indexed today,
    what failed, and which URLs are still pending against the daily
    quota (~200/day by default).

    The unique-per-day key is ``(url, notification_type, day)`` so a
    rerun on the same calendar day can dedupe URLs we already accepted
    instead of burning another quota slot. We keep the raw
    ``submitted_at`` timestamp for analytics and add a denormalised
    ``submission_date`` to make "today's" queries index-friendly.
    """

    NOTIFICATION_URL_UPDATED = "URL_UPDATED"
    NOTIFICATION_URL_DELETED = "URL_DELETED"
    NOTIFICATION_CHOICES = [
        (NOTIFICATION_URL_UPDATED, _("URL_UPDATED")),
        (NOTIFICATION_URL_DELETED, _("URL_DELETED")),
    ]

    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_SUCCESS, _("Прийнято")),
        (STATUS_FAILED, _("Помилка")),
    ]

    url = models.CharField(max_length=512, verbose_name="URL")
    notification_type = models.CharField(
        max_length=16,
        choices=NOTIFICATION_CHOICES,
        default=NOTIFICATION_URL_UPDATED,
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    source = models.CharField(
        max_length=32,
        blank=True,
        help_text="Звідки прилетів пінг: 'admin', 'signal', 'cron', 'manual'.",
    )

    submitted_at = models.DateTimeField(auto_now_add=True)
    submission_date = models.DateField(
        db_index=True,
        help_text="Денна агрегація (UTC дата відправлення) — для дешевих 'сьогодні' запитів.",
    )

    class Meta:
        verbose_name = "Google Indexing submission"
        verbose_name_plural = "Google Indexing submissions"
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=["submission_date", "status"], name="idx_gix_date_status"),
            models.Index(fields=["-submitted_at"], name="idx_gix_submitted_desc"),
        ]

    def __str__(self) -> str:  # pragma: no cover - representation only
        return f"{self.submission_date} {self.status} {self.url}"

    def save(self, *args, **kwargs):
        if not self.submission_date:
            now = timezone.now()
            self.submission_date = now.date()
        super().save(*args, **kwargs)


class QrDeviceGrant(models.Model):
    """Привязка QR-промокода к устройству анонимного посетителя.

    Позволяет показывать один и тот же код тому же человеку (cookie +
    отпечаток устройства: IP + User-Agent + язык) и переносить код в
    профиль после входа.
    """

    device_hash = models.CharField(max_length=64, unique=True, db_index=True)
    promo_code = models.ForeignKey(
        'PromoCode', on_delete=models.CASCADE, related_name='qr_device_grants',
    )
    ip = models.CharField(max_length=45, blank=True, default='')
    user_agent = models.CharField(max_length=300, blank=True, default='')
    visits = models.PositiveIntegerField(default=1)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'QR-видача (пристрій)'
        verbose_name_plural = 'QR-видачі (пристрої)'

    def __str__(self):
        return f"qr-device {self.device_hash[:10]}… → {self.promo_code.code}"
