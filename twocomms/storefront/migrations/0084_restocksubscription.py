from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("productcolors", "0007_normalize_slugs"),
        ("storefront", "0083_useraction_unique_order_action"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RestockSubscription",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("size", models.CharField(max_length=20)),
                ("option_values", models.JSONField(blank=True, default=dict)),
                ("option_labels", models.JSONField(blank=True, default=dict)),
                ("channel", models.CharField(choices=[("telegram", "Telegram"), ("phone", "Телефонний дзвінок"), ("email", "Email"), ("whatsapp", "WhatsApp")], max_length=16)),
                ("status", models.CharField(choices=[("draft", "Очікує підтвердження"), ("active", "Очікує наявності"), ("notified", "Клієнта повідомлено"), ("closed", "Закрито"), ("failed", "Помилка повідомлення")], db_index=True, default="active", max_length=16)),
                ("name", models.CharField(blank=True, default="", max_length=160)),
                ("contact", models.CharField(blank=True, default="", max_length=254)),
                ("normalized_contact", models.CharField(blank=True, db_index=True, default="", max_length=254)),
                ("telegram_user_id", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("telegram_chat_id", models.BigIntegerField(blank=True, null=True)),
                ("telegram_username", models.CharField(blank=True, default="", max_length=100)),
                ("verified_phone", models.CharField(blank=True, default="", max_length=32)),
                ("fingerprint", models.CharField(db_index=True, max_length=64)),
                ("browser_session_key", models.CharField(blank=True, default="", max_length=64)),
                ("request_ip_hash", models.CharField(blank=True, default="", max_length=64)),
                ("user_agent", models.CharField(blank=True, default="", max_length=255)),
                ("admin_notified_at", models.DateTimeField(blank=True, null=True)),
                ("customer_notified_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("color_variant", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="restock_subscriptions", to="productcolors.productcolorvariant")),
                ("product", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.CASCADE, related_name="restock_subscriptions", to="storefront.product")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="restock_subscriptions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Очікування наявності",
                "verbose_name_plural": "Очікування наявності",
                "ordering": ("-created_at",),
                "indexes": [models.Index(fields=["product", "size", "status"], name="idx_restock_product_size"), models.Index(fields=["channel", "status", "-created_at"], name="idx_restock_channel_state")],
            },
        ),
    ]
