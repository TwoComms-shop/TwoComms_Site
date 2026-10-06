import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("management", "0224_deferred_human_echo")]

    operations = [
        migrations.CreateModel(
            name="IgPaymentObservationSource",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_digest", models.CharField(max_length=64)),
                ("provider_namespace", models.CharField(max_length=128)),
                ("reset_floor", models.PositiveBigIntegerField(default=0)),
                ("state", models.CharField(
                    choices=[
                        ("pending", "Очікує обробки"),
                        ("processing", "Обробляється"),
                        ("applied", "Спостереження застосовано"),
                        ("blocked", "Заблоковано"),
                        ("failed", "Помилка обробки"),
                    ],
                    default="pending", max_length=16,
                )),
                ("claim_token", models.CharField(blank=True, default="", max_length=64)),
                ("lease_until", models.DateTimeField(blank=True, null=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("observed_media_digest", models.CharField(blank=True, default="", max_length=64)),
                ("outcome", models.JSONField(blank=True, default=dict)),
                ("last_error", models.CharField(blank=True, default="", max_length=120)),
                ("observed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.ForeignKey(
                    db_constraint=False, on_delete=django.db.models.deletion.CASCADE,
                    related_name="payment_observation_sources", to="management.igclient",
                )),
                ("message", models.OneToOneField(
                    db_constraint=False, on_delete=django.db.models.deletion.CASCADE,
                    related_name="payment_observation", to="management.instagrambotmessage",
                )),
            ],
            options={
                "indexes": [
                    models.Index(fields=["state", "next_attempt_at"], name="ig_payobs_state_due"),
                    models.Index(fields=["client", "id"], name="ig_payobs_client_id"),
                ],
            },
        ),
    ]
