# Fable 5 — початкова міграція. Створює ТІЛЬКИ нові таблиці fable5_*,
# жодна існуюча таблиця storefront / productcolors не змінюється.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("storefront", "0001_initial"),
        ("productcolors", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ColorProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_thermo", models.BooleanField(default=False, verbose_name="Термохромний")),
                ("thermo_note", models.CharField(blank=True, default="", help_text="Наприклад: «Реагує на тепло — змінює відтінок». Показується біля кружечка.", max_length=255, verbose_name="Коротка примітка")),
                ("description", models.TextField(blank=True, default="", help_text="SEO-дружній опис (що таке термохромна тканина, чим відрізняється тощо).", verbose_name="Опис тканини/кольору")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("color", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="fable5_profile", to="productcolors.color")),
            ],
            options={
                "verbose_name": "Fable5: профіль кольору",
                "verbose_name_plural": "Fable5: профілі кольорів",
            },
        ),
        migrations.CreateModel(
            name="VariantDetails",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("display_name", models.CharField(blank=True, default="", help_text="Напр.: «Сіра футболка оверсайз \"Бойова квіточка\"». Порожньо — назва товару.", max_length=220, verbose_name="Назва для цього кольору")),
                ("price_delta", models.IntegerField(default=0, help_text="Напр. +300 за термохромну тканину. Може бути відʼємною.", verbose_name="Надбавка до ціни (грн)")),
                ("price_delta_reason", models.CharField(blank=True, default="", help_text="Показується покупцю: «Для цього кольору ціна +300 грн — термохромна тканина».", max_length=255, verbose_name="Причина надбавки")),
                ("marketing_html", models.TextField(blank=True, default="", help_text="Красивий блок про цю тканину/колір у картці товару.", verbose_name="Маркетинговий опис (HTML)")),
                ("youtube_url", models.URLField(blank=True, default="", help_text="Порожньо — використовується спільне відео товару (Product.video_url).", max_length=500, verbose_name="YouTube для кольору")),
                ("seo_title", models.CharField(blank=True, default="", max_length=180, verbose_name="SEO Title")),
                ("seo_description", models.CharField(blank=True, default="", max_length=320, verbose_name="SEO Description")),
                ("seo_keywords", models.CharField(blank=True, default="", max_length=300, verbose_name="SEO Keywords")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("variant", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="fable5_details", to="productcolors.productcolorvariant")),
            ],
            options={
                "verbose_name": "Fable5: деталі кольору товару",
                "verbose_name_plural": "Fable5: деталі кольорів товарів",
            },
        ),
        migrations.CreateModel(
            name="ProductFitNote",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fit_code", models.SlugField(max_length=50, verbose_name="Код посадки")),
                ("is_enabled", models.BooleanField(default=True, verbose_name="Доступна")),
                ("reason", models.CharField(blank=True, default="", help_text="Пишеться у картці товару під перемикачем посадок.", max_length=255, verbose_name="Причина (якщо вимкнена)")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fable5_fit_notes", to="storefront.product")),
            ],
            options={
                "verbose_name": "Fable5: посадка товару",
                "verbose_name_plural": "Fable5: посадки товарів",
                "unique_together": {("product", "fit_code")},
            },
        ),
        migrations.CreateModel(
            name="VariantFitRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fit_code", models.SlugField(max_length=50, verbose_name="Код посадки")),
                ("is_enabled", models.BooleanField(default=True, verbose_name="Доступна")),
                ("reason", models.CharField(blank=True, default="", max_length=255, verbose_name="Причина (якщо вимкнена)")),
                ("variant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fable5_fit_rules", to="productcolors.productcolorvariant")),
            ],
            options={
                "verbose_name": "Fable5: посадка кольору",
                "verbose_name_plural": "Fable5: посадки кольорів",
                "unique_together": {("variant", "fit_code")},
            },
        ),
        migrations.CreateModel(
            name="VariantSizeRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fit_code", models.SlugField(blank=True, default="", help_text="Порожньо — правило для всіх посадок.", max_length=50, verbose_name="Код посадки")),
                ("size", models.CharField(max_length=12, verbose_name="Розмір")),
                ("is_enabled", models.BooleanField(default=True, verbose_name="Доступний")),
                ("stock", models.IntegerField(blank=True, null=True, verbose_name="Залишок, шт")),
                ("note", models.CharField(blank=True, default="", max_length=255, verbose_name="Примітка")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("variant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fable5_size_rules", to="productcolors.productcolorvariant")),
            ],
            options={
                "verbose_name": "Fable5: розмір кольору",
                "verbose_name_plural": "Fable5: розміри кольорів",
                "ordering": ["variant_id", "fit_code", "id"],
                "unique_together": {("variant", "fit_code", "size")},
            },
        ),
        migrations.CreateModel(
            name="VariantFAQ",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("question_uk", models.CharField(blank=True, default="", max_length=255, verbose_name="Питання (укр)")),
                ("question_ru", models.CharField(blank=True, default="", max_length=255, verbose_name="Питання (рос)")),
                ("question_en", models.CharField(blank=True, default="", max_length=255, verbose_name="Питання (англ)")),
                ("answer_uk", models.TextField(blank=True, default="", verbose_name="Відповідь (укр)")),
                ("answer_ru", models.TextField(blank=True, default="", verbose_name="Відповідь (рос)")),
                ("answer_en", models.TextField(blank=True, default="", verbose_name="Відповідь (англ)")),
                ("order", models.PositiveIntegerField(default=0, verbose_name="Порядок")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активне")),
                ("variant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fable5_faqs", to="productcolors.productcolorvariant")),
            ],
            options={
                "verbose_name": "Fable5: FAQ кольору",
                "verbose_name_plural": "Fable5: FAQ кольорів",
                "ordering": ["order", "id"],
            },
        ),
        migrations.CreateModel(
            name="FeedProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text="Напр.: «Meta DS фід версія 1»", max_length=160, verbose_name="Назва")),
                ("slug", models.SlugField(max_length=160, unique=True, verbose_name="Код фіда")),
                ("feed_type", models.CharField(choices=[("google_merchant", "Google Merchant"), ("meta_ds", "Meta / Facebook DS"), ("custom", "Інший / кастомний")], default="custom", max_length=30, verbose_name="Тип")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активний")),
                ("default_include", models.BooleanField(default=False, help_text="Вимкнено — у фід потрапляють лише товари, явно додані у «Селекції з фід».", verbose_name="Включати товари за замовчуванням")),
                ("settings", models.JSONField(blank=True, default=dict, verbose_name="Налаштування")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Fable5: фід",
                "verbose_name_plural": "Fable5: фіди",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="FeedProductRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_included", models.BooleanField(default=True, verbose_name="Товар у фіді")),
                ("custom_title", models.CharField(blank=True, default="", max_length=220, verbose_name="Тайтл для фіда")),
                ("custom_description", models.TextField(blank=True, default="", verbose_name="Опис для фіда")),
                ("note", models.CharField(blank=True, default="", max_length=255, verbose_name="Примітка")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("feed", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="product_rules", to="fable5.feedprofile")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fable5_feed_rules", to="storefront.product")),
            ],
            options={
                "verbose_name": "Fable5: товар у фіді",
                "verbose_name_plural": "Fable5: товари у фідах",
                "unique_together": {("feed", "product")},
            },
        ),
        migrations.CreateModel(
            name="FeedImageRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("use_main_image", models.BooleanField(default=False, verbose_name="Головне зображення товару")),
                ("is_allowed", models.BooleanField(default=True, verbose_name="Дозволено у фіді")),
                ("order", models.PositiveIntegerField(default=0, verbose_name="Порядок у фіді")),
                ("color_image", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="fable5_feed_rules", to="productcolors.productcolorimage")),
                ("feed", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="image_rules", to="fable5.feedprofile")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fable5_feed_image_rules", to="storefront.product")),
                ("product_image", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="fable5_feed_rules", to="storefront.productimage")),
            ],
            options={
                "verbose_name": "Fable5: картинка у фіді",
                "verbose_name_plural": "Fable5: картинки у фідах",
                "ordering": ["order", "id"],
            },
        ),
        migrations.CreateModel(
            name="FeedOnlyImage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("image", models.ImageField(upload_to="fable5/feed_images/", verbose_name="Зображення")),
                ("alt", models.CharField(blank=True, default="", max_length=200, verbose_name="Alt-текст")),
                ("order", models.PositiveIntegerField(default=0, verbose_name="Порядок")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("feed", models.ForeignKey(blank=True, help_text="Порожньо — доступна будь-якому фіду.", null=True, on_delete=django.db.models.deletion.CASCADE, related_name="extra_images", to="fable5.feedprofile", verbose_name="Фід")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fable5_feed_only_images", to="storefront.product")),
            ],
            options={
                "verbose_name": "Fable5: фід-картинка",
                "verbose_name_plural": "Fable5: фід-картинки",
                "ordering": ["order", "id"],
            },
        ),
    ]
