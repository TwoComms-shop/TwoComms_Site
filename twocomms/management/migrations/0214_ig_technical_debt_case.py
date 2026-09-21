from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("management", "0213_ig_worker_lane_fence"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="IgTechnicalDebtCase",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("case_key", models.CharField(max_length=160, unique=True)),
                ("reason", models.CharField(db_index=True, max_length=64)),
                ("scope", models.CharField(db_index=True, max_length=64)),
                ("status", models.CharField(choices=[("open", "Відкрито"), ("acknowledged", "Прийнято"), ("claimed", "У роботі"), ("resolved", "Вирішено"), ("dismissed", "Відхилено"), ("unknown", "Потрібна перевірка")], db_index=True, default="open", max_length=16)),
                ("first_observed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_observed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("oldest_observed_at", models.DateTimeField(blank=True, null=True)),
                ("last_count", models.PositiveIntegerField(default=0)),
                ("observation_fingerprint", models.CharField(blank=True, default="", max_length=64)),
                ("sample_ids", models.JSONField(blank=True, default=list)),
                ("has_more", models.BooleanField(default=False)),
                ("coverage_complete", models.BooleanField(default=True)),
                ("disposition", models.CharField(blank=True, default="", max_length=64)),
                ("operator_note", models.TextField(blank=True, default="")),
                ("acknowledged_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("evidence", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=models.deletion.SET_NULL, related_name="ig_technical_debt_cases", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "IG technical debt case", "verbose_name_plural": "IG technical debt cases", "ordering": ["status", "first_observed_at", "id"]},
        ),
        migrations.AddIndex(model_name="igtechnicaldebtcase", index=models.Index(fields=["status", "last_observed_at"], name="ig_td_status_seen")),
        migrations.AddIndex(model_name="igtechnicaldebtcase", index=models.Index(fields=["reason", "scope"], name="ig_td_reason_scope")),
    ]
