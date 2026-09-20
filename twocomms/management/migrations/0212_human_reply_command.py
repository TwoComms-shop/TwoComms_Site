from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("management", "0211_merge_trace_refresh_presence"),
    ]

    operations = [
        migrations.CreateModel(
            name="HumanReplyCommand",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("operation_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("recipient_igsid", models.CharField(max_length=64)),
                ("provider_namespace", models.CharField(blank=True, default="", max_length=128)),
                ("purpose", models.CharField(default="human_reply", max_length=32)),
                ("context_revision", models.CharField(blank=True, default="", max_length=128)),
                ("draft_hash", models.CharField(blank=True, default="", max_length=64)),
                ("text", models.TextField()),
                ("operation_context", models.JSONField(blank=True, default=dict)),
                ("permission_epoch", models.PositiveBigIntegerField(default=0)),
                ("window_deadline", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("state", models.CharField(choices=[("pending", "В черзі"), ("claimed", "Захоплено"), ("provider_started", "Запит почато"), ("sent", "Надіслано"), ("definite_failed", "Підтверджена відмова"), ("unknown", "Результат невідомий"), ("cancelled", "Скасовано")], db_index=True, default="pending", max_length=16)),
                ("failure_code", models.CharField(blank=True, default="", max_length=96)),
                ("provider_message_ids", models.JSONField(blank=True, default=list)),
                ("provider_started_at", models.DateTimeField(blank=True, null=True)),
                ("terminal_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("actor", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="ig_human_reply_commands", to=settings.AUTH_USER_MODEL)),
                ("client", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.CASCADE, related_name="human_reply_commands", to="management.igclient")),
                ("context_message", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="human_reply_context_commands", to="management.instagrambotmessage")),
                ("reply_message", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="human_reply_commands", to="management.instagrambotmessage")),
            ],
            options={"ordering": ["-id"], "indexes": [
                models.Index(fields=["client", "state", "-created_at"], name="ig_human_cmd_client"),
                models.Index(fields=["state", "-created_at"], name="ig_human_cmd_state"),
            ]},
        ),
    ]
