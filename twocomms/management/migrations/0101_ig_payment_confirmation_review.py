from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("management", "0100_ig_analysis_interaction_taxonomy"),
    ]

    operations = [
        migrations.CreateModel(
            name="IgPaymentConfirmationReview",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("dedupe_key", models.CharField(max_length=160, unique=True)),
                ("status", models.CharField(choices=[("pending", "Очікує підтвердження"), ("confirmed", "Підтверджено менеджером"), ("cancelled", "Скасовано менеджером")], db_index=True, default="pending", max_length=16)),
                ("evidence", models.JSONField(blank=True, default=dict)),
                ("watermark_message_id", models.PositiveBigIntegerField(default=0)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("cancellation_reason", models.CharField(blank=True, default="", max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("cancelled_by", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="cancelled_ig_payment_reviews", to=settings.AUTH_USER_MODEL)),
                ("client", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.CASCADE, related_name="payment_confirmation_reviews", to="management.igclient")),
                ("confirmed_by", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="confirmed_ig_payment_reviews", to=settings.AUTH_USER_MODEL)),
                ("deal", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="payment_confirmation_reviews", to="management.igdeal")),
            ],
            options={
                "verbose_name": "Перевірка оплати Instagram",
                "verbose_name_plural": "Перевірки оплати Instagram",
                "ordering": ["-id"],
                "indexes": [
                    models.Index(fields=["status", "-created_at"], name="ig_payreview_status_dt"),
                    models.Index(fields=["client", "-id"], name="ig_payreview_client_id"),
                ],
            },
        ),
    ]
